#!/usr/bin/env python3
"""DigitalPlat 免费域名自动续期脚本（零第三方依赖，仅用 Python 标准库）。

设计来源（合并了两个上游项目的长处）：
  * OUBIGFA/DigitalPlat-Domains-auto-renew —— 主要逻辑基线：纯标准库实现、
    Idempotency-Key、domain_id 回退、permanent 域名跳过、状态文件、单元测试。
  * xz0609/digitalplat-auto-renew —— keepalive 心跳提交（防止 GitHub 因仓库
    60 天无提交而自动停用定时工作流）、日志域名脱敏。

本脚本额外增强：
  * 403/429/5xx 与 Cloudflare 挑战页的自动重试（带浏览器 UA 回退），
    因此不再需要 cloudscraper 这类第三方库。
  * 写 GitHub Actions Step Summary，运行结果在 Actions 页面直接可读。
  * 在 Actions 环境下自动 ::add-mask:: 域名，避免明文出现在日志里。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_BASE = "https://domain-api.digitalplat.org/api/v1"
DATE_FORMAT = "%Y-%m-%d"
DEFAULT_RENEW_BEFORE_DAYS = 120
DEFAULT_RENEWAL_TYPE = "free"
DEFAULT_RENEWAL_YEARS = 1
MAX_ATTEMPTS = 3

# 仅在首次请求被 WAF/Cloudflare 拦截时启用的回退请求头。
BROWSER_FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}

NAME_KEYS = ("domain", "name", "domain_name", "domainName", "full_domain", "fullDomain")
EXPIRY_KEYS = (
    "expiry_date",
    "expires_at",
    "expiryDate",
    "expiresAt",
    "expiration_date",
    "expirationDate",
    "expire_at",
    "expire_date",
    "expires",
    "expiration",
    "expiry",
)
RENEWABLE_KEYS = ("can_free_renew", "can_renew", "renewable", "is_renewable", "canRenew", "canFreeRenew")
STATUS_KEYS = ("status", "domain_status", "state")


@dataclass
class DomainRecord:
    name: str
    raw: dict[str, Any]
    expiry_date: datetime
    status: str | None
    can_renew: bool | None

    @property
    def days_remaining(self) -> int:
        now = datetime.now(timezone.utc)
        return (self.expiry_date.date() - now.date()).days

    @property
    def is_permanent(self) -> bool:
        return str(self.raw.get("lifecycle_type")).strip().lower() == "permanent"


@dataclass
class RunResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    renewed: int = 0


def actions_mask(value: str) -> None:
    """在 GitHub Actions 中把域名登记为需要脱敏的字符串。"""
    if value and os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


class DigitalPlatClient:
    def __init__(self, api_token: str, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "digitalplat-auto-renew/2.0 (+https://github.com/OUBIGFA/DigitalPlat-Domains-auto-renew)",
        }

    def _request(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.api_base}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        text = ""
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            req_headers = dict(self.headers)
            if extra_headers:
                req_headers.update(extra_headers)
            if attempt > 1:
                # 第 2 次起伪装成浏览器，绕过 WAF/Cloudflare 的默认 UA 拦截。
                req_headers.update(BROWSER_FALLBACK_HEADERS)
            request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    text = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = f"DigitalPlat HTTP {exc.code}: {detail[:500]}"
                # 续期请求带 Idempotency-Key，因此 5xx 重试不会重复续费。
                retryable = exc.code in (403, 408, 425, 429, 500, 502, 503, 504)
                if not retryable or attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(message) from exc
                print(f"[WARN] {message[:180]} -> retry {attempt}/{MAX_ATTEMPTS - 1}", file=sys.stderr)
                time.sleep(2 * attempt)
            except urllib.error.URLError as exc:
                message = f"DigitalPlat network error: {exc}"
                if attempt >= MAX_ATTEMPTS:
                    raise RuntimeError(message) from exc
                print(f"[WARN] {message} -> retry {attempt}/{MAX_ATTEMPTS - 1}", file=sys.stderr)
                time.sleep(2 * attempt)

        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DigitalPlat returned non-JSON response: {text[:200]}") from exc
        if not isinstance(parsed, (dict, list)):
            raise RuntimeError(f"DigitalPlat returned unexpected response type: {type(parsed)}")
        return parsed

    def list_domains(self) -> list[dict[str, Any]]:
        data = self._request("/domains")
        payload = unwrap_response(data)
        domains = payload.get("domains") if isinstance(payload, dict) else payload
        if not isinstance(domains, list):
            raise RuntimeError(f"DigitalPlat domain list response is missing domains: {data}")
        return [item for item in domains if isinstance(item, dict)]

    def get_domain(self, domain: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(str(domain), safe="")
        data = self._request(f"/domains/{encoded}")
        payload = unwrap_response(data)
        record = payload.get("domain", payload) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            raise RuntimeError(f"DigitalPlat domain detail response is missing domain: {data}")
        return record

    def renew_domain(
        self,
        domain: str,
        renewal_type: str,
        years: int,
        domain_id: Any = None,
    ) -> Any:
        payload = {"renewal_type": renewal_type, "years": years}
        encoded = urllib.parse.quote(str(domain), safe="")
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        try:
            return unwrap_response(
                self._request(f"/domains/{encoded}/renew", method="POST", payload=payload, extra_headers=headers)
            )
        except RuntimeError as exc:
            if "registered_domain_not_found" in str(exc) and domain_id:
                print(f"[DEBUG] /domains/{encoded}/renew -> 404, retrying with id={domain_id} ...")
                return unwrap_response(
                    self._request(
                        f"/domains/{domain_id}/renew",
                        method="POST",
                        payload=payload,
                        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
                    )
                )
            raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DigitalPlat 免费域名自动续期（每周检查）。")
    parser.add_argument("--state", default="state/domains-state.json", help="状态文件路径。")
    parser.add_argument("--dry-run", action="store_true", help="只评估，不续期、不写状态。")
    parser.add_argument("--fixture", help="用本地域名列表 JSON 代替真实 API 调用。")
    return parser.parse_args(argv)


def unwrap_response(data: Any) -> Any:
    if isinstance(data, dict):
        if "success" in data and data.get("success") is False:
            message = data.get("error") or data.get("message") or data
            raise RuntimeError(f"DigitalPlat API returned an error: {message}")
        if data.get("status") == "error":
            message = data.get("message") or data.get("error") or data
            raise RuntimeError(f"DigitalPlat API returned an error: {message}")
        if "code" in data and data["code"] not in (0, 200, "0", "200"):
            message = data.get("message") or data.get("msg") or data
            raise RuntimeError(f"DigitalPlat API returned code {data['code']}: {message}")
        if "data" in data and data["data"] is not None:
            return data["data"]
    return data


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc


def parse_domain_variable() -> list[str]:
    """读取 DIGITALPLAT_DOMAINS（支持换行 / 逗号 / 分号 / 空格分隔，自动去重）。

    返回空列表表示"托管账户下全部域名"。
    """
    raw = os.getenv("DIGITALPLAT_DOMAINS")
    if not raw:
        return []
    normalized = raw.replace(";", "\n").replace(",", "\n").replace(" ", "\n")
    domains = [line.strip().lower() for line in normalized.splitlines() if line.strip()]
    seen: set[str] = set()
    unique_domains: list[str] = []
    duplicates: list[str] = []
    for domain in domains:
        if domain in seen:
            duplicates.append(domain)
        else:
            seen.add(domain)
            unique_domains.append(domain)
    if duplicates:
        print(
            f"[WARN] DIGITALPLAT_DOMAINS contains duplicates "
            f"({', '.join(sorted(set(duplicates)))}). Automatically deduplicating."
        )
    return unique_domains


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"domains": {}}
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"domains": {}}
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        print(f"[WARN] State file is not valid JSON, starting fresh: {path}", file=sys.stderr)
    return {"domains": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_datetime(value: Any) -> datetime:
    if value is None or value == "":
        raise ValueError("empty date")

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        raise ValueError("empty date")

    if text.isdigit():
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
        if len(text) in (10, 13):
            ts = float(text)
            if len(text) == 13:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

    text = text.replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    raise ValueError(f"Unsupported date format: {value}")


def pick_first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_domain(raw: dict[str, Any]) -> DomainRecord:
    name = pick_first(raw, NAME_KEYS)
    if not name:
        raise RuntimeError(f"Domain record is missing a domain name: {raw}")
    expiry = pick_first(raw, EXPIRY_KEYS)
    if not expiry:
        raise RuntimeError(f"Domain record is missing expiry_date: {raw}")
    can_renew = pick_first(raw, RENEWABLE_KEYS)
    if isinstance(can_renew, str):
        can_renew = can_renew.strip().lower() in ("1", "true", "yes", "y")
    elif can_renew is not None:
        can_renew = bool(can_renew)
    status = pick_first(raw, STATUS_KEYS)
    return DomainRecord(
        name=str(name).strip().lower(),
        raw=raw,
        expiry_date=parse_datetime(expiry),
        status=None if status is None else str(status),
        can_renew=can_renew,
    )


def load_fixture(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = unwrap_response(data)
    if isinstance(data, dict):
        domains = data.get("domains")
        if isinstance(domains, list):
            return [item for item in domains if isinstance(item, dict)]
        domain = data.get("domain")
        if isinstance(domain, dict):
            return [domain]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    raise RuntimeError(f"Fixture must contain a domains list or domain object: {path}")


def should_renew(record: DomainRecord, renew_before_days: int) -> bool:
    if record.can_renew is False:
        return False
    if record.is_permanent:
        return False
    if str(record.status or "").strip().lower() in ("expired", "deleted", "suspended", "pending_delete"):
        return False
    return record.days_remaining <= renew_before_days


def update_state_for_record(
    state: dict[str, Any],
    record: DomainRecord,
    action: str,
    renew_before_days: int,
) -> bool:
    domains = state.setdefault("domains", {})
    item = domains.setdefault(record.name, {})
    new_item = {
        "expiry_date": record.expiry_date.strftime(DATE_FORMAT),
        "days_remaining": record.days_remaining,
        "renew_before_days": renew_before_days,
        "lifecycle_type": record.raw.get("lifecycle_type"),
        "status": record.status,
        "last_action": action,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    changed = False
    for key, value in new_item.items():
        if item.get(key) != value:
            item[key] = value
            changed = True
    return changed


def remove_stale_domains(state: dict[str, Any], active_domains: set[str]) -> bool:
    if not active_domains:
        return False
    domains = state.setdefault("domains", {})
    stale = [domain for domain in domains if domain not in active_domains]
    for domain in stale:
        del domains[domain]
    return bool(stale)


def write_step_summary(result: RunResult, renew_before_days: int, dry_run: bool) -> None:
    """把运行结果写到 GitHub Actions 的 Job Summary。"""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## DigitalPlat 续期检查",
        "",
        f"- 运行时间（UTC）：`{datetime.now(timezone.utc).isoformat(timespec='seconds')}`",
        f"- 续期窗口：到期前 **{renew_before_days}** 天",
        f"- 本次续期数量：**{result.renewed}**",
        f"- Dry run：`{str(dry_run).lower()}`",
        "",
        "| 域名 | 到期日 | 剩余天数 | lifecycle | 结果 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in result.rows:
        lines.append(
            f"| `{row['domain']}` | {row['expiry_date']} | {row['days_remaining']} | "
            f"{row.get('lifecycle_type') or '-'} | {row['action']} |"
        )
    if result.errors:
        lines += ["", "### 错误", ""]
        lines += [f"- {error}" for error in result.errors]
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = Path(args.state)
    managed_names = parse_domain_variable()
    for domain in managed_names:
        actions_mask(domain)
    renew_before_days = optional_int_env("DIGITALPLAT_RENEW_BEFORE_DAYS", DEFAULT_RENEW_BEFORE_DAYS)
    renewal_type = os.getenv("DIGITALPLAT_RENEWAL_TYPE") or DEFAULT_RENEWAL_TYPE
    renewal_years = optional_int_env("DIGITALPLAT_RENEWAL_YEARS", DEFAULT_RENEWAL_YEARS)

    client: DigitalPlatClient | None = None
    if args.fixture:
        raw_domains = load_fixture(Path(args.fixture))
    else:
        token = os.getenv("DIGITALPLAT_API_TOKEN")
        if not token:
            print("[FATAL] Missing required environment variable: DIGITALPLAT_API_TOKEN", file=sys.stderr)
            print(
                "请在仓库 Settings -> Secrets and variables -> Actions 中添加 DIGITALPLAT_API_TOKEN。",
                file=sys.stderr,
            )
            return 1
        api_base = os.getenv("DIGITALPLAT_API_BASE") or API_BASE
        client = DigitalPlatClient(token, api_base)
        try:
            raw_domains = client.list_domains()
        except Exception as exc:
            print(f"[FATAL] Failed to list domains from API: {exc}", file=sys.stderr)
            return 1

    # 稳健地建立 域名 -> 原始记录 的映射（跳过字段不完整的记录）
    domain_map: dict[str, dict[str, Any]] = {}
    for raw in raw_domains:
        if not isinstance(raw, dict):
            continue
        name = pick_first(raw, NAME_KEYS)
        if name:
            domain_map[str(name).strip().lower()] = raw

    # 未显式配置 DIGITALPLAT_DOMAINS 时，托管账户下全部域名
    if not managed_names:
        if domain_map:
            managed_names = list(domain_map.keys())
            for domain in managed_names:
                actions_mask(domain)
            print(f"[INFO] DIGITALPLAT_DOMAINS 未配置，自动托管账户下全部 {len(managed_names)} 个域名。")
        elif client is not None:
            print("[INFO] 账户下没有域名，且 DIGITALPLAT_DOMAINS 未配置，无需处理。")
            return 0
        else:
            print("[FATAL] 既没有指定域名，fixture 里也没有域名。", file=sys.stderr)
            return 1

    state = load_state(state_path)
    state_changed = remove_stale_domains(state, set(managed_names))
    result = RunResult()

    print(f"UTC now: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"Renewal window: {renew_before_days} day(s) before expiry")

    for domain in managed_names:
        raw = domain_map.get(domain)
        if raw is None and client is not None:
            try:
                raw = client.get_domain(domain)
            except Exception as exc:
                result.errors.append(f"{domain}: failed to fetch detail: {exc}")
                continue
        if raw is None:
            result.errors.append(f"{domain}: not found in DigitalPlat account")
            continue

        if not pick_first(raw, EXPIRY_KEYS) and client is not None:
            try:
                detail = client.get_domain(domain)
                if isinstance(detail, dict):
                    raw = {**raw, **detail}
            except Exception as exc:
                print(f"[WARN] Failed to fetch additional detail for {domain}: {exc}")

        try:
            record = normalize_domain(raw)
        except Exception as exc:
            result.errors.append(f"{domain}: failed to parse domain record: {exc}")
            continue

        domain_id = pick_first(raw, ("id", "domain_id", "registered_domain_id"))
        print(
            f"[CHECK] {record.name} expiry_date={record.expiry_date.strftime(DATE_FORMAT)} "
            f"days_remaining={record.days_remaining} lifecycle={record.raw.get('lifecycle_type') or '-'} "
            f"status={record.status or '-'}"
        )

        def record_row(action: str) -> None:
            result.rows.append(
                {
                    "domain": record.name,
                    "expiry_date": record.expiry_date.strftime(DATE_FORMAT),
                    "days_remaining": record.days_remaining,
                    "lifecycle_type": record.raw.get("lifecycle_type"),
                    "action": action,
                }
            )

        if record.is_permanent:
            print(f"[SKIP] {record.name} 是永久域名（lifecycle_type=permanent），无需续期。")
            record_row("permanent / 无需续期")
            state_changed = update_state_for_record(state, record, "permanent", renew_before_days) or state_changed
            continue

        if not should_renew(record, renew_before_days):
            print(
                f"[SKIP] {record.name} 尚未进入续期窗口（剩余 {record.days_remaining} 天，"
                f"窗口为到期前 {renew_before_days} 天）。"
            )
            record_row("skipped / 未到窗口")
            state_changed = update_state_for_record(state, record, "skipped", renew_before_days) or state_changed
            continue

        if args.dry_run:
            print(f"[DRY-RUN] 将会续期 {record.name}（renewal_type={renewal_type} years={renewal_years}）。")
            record_row("dry-run / 将续期")
            continue

        if client is None:
            print(f"[FIXTURE] 将会续期 {record.name}。")
            record_row("fixture / 将续期")
            continue

        try:
            renewed_raw = client.renew_domain(record.name, renewal_type, renewal_years, domain_id=domain_id)
        except Exception as exc:
            if "registered_domain_not_found" in str(exc):
                print(f"[SKIP] {record.name}: 该域名不支持/不需要走续期接口（registered_domain_not_found）。")
                record_row("not_applicable / 不支持续期接口")
                state_changed = (
                    update_state_for_record(state, record, "not_applicable", renew_before_days) or state_changed
                )
                continue
            result.errors.append(f"{record.name}: renewal request failed: {exc}")
            record_row("failed / 续期失败")
            state_changed = (
                update_state_for_record(state, record, "renew_failed", renew_before_days) or state_changed
            )
            continue

        renewed_record: DomainRecord | None = None
        if isinstance(renewed_raw, dict):
            candidate = renewed_raw.get("domain", renewed_raw)
            if isinstance(candidate, dict):
                try:
                    renewed_record = normalize_domain(candidate)
                except Exception:
                    renewed_record = None

        if renewed_record is None:
            try:
                renewed_record = normalize_domain(client.get_domain(record.name))
            except Exception as exc:
                print(f"[WARN] Could not re-fetch details after renewal for {record.name}: {exc}")

        result.renewed += 1
        final_record = renewed_record if renewed_record is not None else record
        state_changed = update_state_for_record(state, final_record, "renewed", renew_before_days) or state_changed

        if renewed_record is not None:
            print(
                f"[RENEWED] {renewed_record.name} new_expiry_date="
                f"{renewed_record.expiry_date.strftime(DATE_FORMAT)} "
                f"days_remaining={renewed_record.days_remaining}"
            )
            result.rows.append(
                {
                    "domain": renewed_record.name,
                    "expiry_date": renewed_record.expiry_date.strftime(DATE_FORMAT),
                    "days_remaining": renewed_record.days_remaining,
                    "lifecycle_type": renewed_record.raw.get("lifecycle_type"),
                    "action": "renewed / 已续期",
                }
            )
        else:
            print(f"[RENEWED] {record.name} renewal request acknowledged successfully.")
            record_row("renewed / 已提交续期")

    state.setdefault("domains", {})
    state["last_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state_changed = True  # last_run_at 每次都会变，保证每周有一次提交（兼作 keepalive）
    if state_changed and not args.dry_run:
        save_state(state_path, state)
        print(f"[WRITE] Updated {state_path}")

    write_step_summary(result, renew_before_days, args.dry_run)

    if result.errors:
        for error in result.errors:
            print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    if result.renewed == 0:
        print("[DONE] 本次运行没有域名需要续期。")
    else:
        print(f"[DONE] 本次续期了 {result.renewed} 个域名。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
