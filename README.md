# DigitalPlat 免费域名自动续期（合并增强版）

> 每周自动检查 DigitalPlat 账户下的域名，进入「到期前 120 天」续期窗口后自动免费续期。
> 纯 Python 标准库实现，零第三方依赖；由 **GitHub Actions** 定时驱动。

本仓库合并了两个上游项目的长处，代码基线来自更活跃、修过真实 bug 的那个：

| | [OUBIGFA/DigitalPlat-Domains-auto-renew](https://github.com/OUBIGFA/DigitalPlat-Domains-auto-renew) | [xz0609/digitalplat-auto-renew](https://github.com/xz0609/digitalplat-auto-renew) | 本仓库 |
| --- | --- | --- | --- |
| 定位 | **正源 / 模板仓库**（另一仓库的 `sync-upstream.yml` 指向它） | 下游副本 | 合并增强 |
| 最近实质性修复 | 6 次（幂等键、`domain_id` 回退、`urllib` 导入、永久域名跳过 404 等） | 0 次（提交是把代码**回退**到旧版 + keepalive） | 全部继承 |
| 依赖 | 无（标准库） | 需 `cloudscraper` | 无（标准库 + UA 回退重试） |
| `permanent` 永久域名 | ✅ 跳过，不报错 | ❌ 会打续期接口拿到 404 | ✅ 跳过 |
| 续期幂等 | ✅ `Idempotency-Key` | ❌ | ✅ |
| 状态文件 | ✅ `state/domains-state.json` | ❌ | ✅ |
| 单元测试 | ✅ 13 个 | ❌ | ✅ 19 个 |
| keepalive 心跳 | ❌ | ✅ | ✅ 已合并 |
| 日志域名脱敏 | ❌ | ✅ | ✅ 已合并 |
| Actions Job Summary | ❌ | ❌ | ✅ 新增 |
| Cloudflare/WAF 403 重试 | 依赖 cloudscraper | 同左 | ✅ 内置浏览器 UA 回退，无需依赖 |

**结论：以 OUBIGFA 版为基线，把 xz0609 版唯一有价值的两点（keepalive 心跳、日志脱敏）合进来，再去掉 cloudscraper 依赖、补上 WAF 重试和运行摘要。**

## 运行周期

| 项目 | 值 |
| --- | --- |
| 检查频率 | **每周 1 次**（`cron: "17 4 * * 1"`，即每周一 04:17 UTC = **北京时间周一 12:17**） |
| 续期触发条件 | 剩余天数 ≤ `DIGITALPLAT_RENEW_BEFORE_DAYS`（默认 **120 天**） |
| 每次续期时长 | `renewal_type=free`，`years=1`（一次续 1 年） |
| 未进入窗口时 | 只记录状态，不调用续期接口 |
| 手动触发 | Actions 页面 → `DigitalPlat Auto Renew` → `Run workflow` |

## 原理

```
GitHub Actions 定时器 (cron, 每周一)
        │
        ├─ checkout 仓库 + 装 Python 3.12
        ├─ 检查 Secret 是否存在（缺 Token 时定时任务静默跳过，手动运行则报错）
        ├─ 跑单元测试
        ├─ 执行 scripts/digitalplat_auto_renew.py
        │     ├─ GET  https://domain-api.digitalplat.org/api/v1/domains       ← Bearer Token
        │     ├─ 算 days_remaining = expires_at - today
        │     ├─ 永久域名(lifecycle_type=permanent) → 跳过
        │     ├─ days_remaining > 120 → 跳过
        │     └─ days_remaining ≤ 120 → POST /domains/{domain}/renew
        │            body: {"renewal_type": "free", "years": 1}
        │            header: Idempotency-Key: <uuid4>（防重复续费）
        ├─ 写 state/domains-state.json + Job Summary
        └─ 提交 state + .keepalive 回仓库（同时防止 GitHub 因 60 天无活动停用定时任务）
```

要点：

- **是的，完全依赖 GitHub Actions**。仓库没有服务器、没有常驻进程，唯一"活着"的东西就是 workflow 的 cron。
- GitHub 的 `schedule` 事件本身有几分钟到几十分钟的延迟，属正常现象，不影响 120 天的窗口冗余。
- 仓库连续 **60 天没有任何提交**，GitHub 会自动停用定时工作流；本仓库每周都会提交 `state/` 与 `.keepalive`，所以不会触发。
- 续期接口对 `permanent` 域名返回 `404 registered_domain_not_found`，这是"该域名不需要续期"，不是错误——脚本会记为 `not_applicable` 并正常退出。

## 部署（本仓库已配置完成）

必需 Secret（`Settings -> Secrets and variables -> Actions -> Secrets`）：

| 名称 | 说明 |
| --- | --- |
| `DIGITALPLAT_API_TOKEN` | DigitalPlat API Key，`dp_live_...` 开头。在 <https://dash.domain.digitalplat.org/dashboard/api/keys> 创建 |
| `DIGITALPLAT_DOMAINS` | 需要续期的域名，换行/逗号/分号/空格分隔均可。**故意放 Secret 而不是 Variable**：Secret 的值会被 GitHub 自动脱敏，日志里显示为 `***`。留空 = 托管账户下全部域名 |

可选 Variables（`Settings -> Secrets and variables -> Actions -> Variables`）：

| 名称 | 默认 | 说明 |
| --- | --- | --- |
| `DIGITALPLAT_RENEW_BEFORE_DAYS` | `120` | 提前多少天开始续期 |
| `DIGITALPLAT_RENEWAL_TYPE` | `free` | 续期类型 |
| `DIGITALPLAT_RENEWAL_YEARS` | `1` | 每次续几年 |
| `DIGITALPLAT_API_BASE` | `https://domain-api.digitalplat.org/api/v1` | API 地址，官方换域名时改这里 |

> 兼容性：`DIGITALPLAT_DOMAINS` 放 Variable 也能跑（workflow 里写的是 `${{ vars.DIGITALPLAT_DOMAINS || secrets.DIGITALPLAT_DOMAINS }}`），但那样域名是明文。

## 本地运行

```bash
# 只看结果不续期（推荐先跑这个）
DIGITALPLAT_API_TOKEN=dp_live_xxx DIGITALPLAT_DOMAINS=your.dpdns.org \
  python3 scripts/digitalplat_auto_renew.py --dry-run

# 真实执行
DIGITALPLAT_API_TOKEN=dp_live_xxx DIGITALPLAT_DOMAINS=your.dpdns.org \
  python3 scripts/digitalplat_auto_renew.py

# 单元测试
python3 -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:DIGITALPLAT_API_TOKEN="dp_live_xxx"; $env:DIGITALPLAT_DOMAINS="your.dpdns.org"
python scripts\digitalplat_auto_renew.py --dry-run
```

## 故障排查

### 1. 运行几秒就失败：`The job was not started because recent account payments have failed...`

这不是代码问题，是 GitHub 账号层面的 Actions 计费状态。**私有仓库**的 Actions 消耗账号额度，
一旦账号有未结清账单 / 支付失败 / spending limit 为 0，私有仓库的工作流就会直接被拒绝启动
（同一账号下的公开仓库不受影响）。

处理方式三选一：

- **修计费**：<https://github.com/settings/billing> 检查付款方式、未支付账单、Spending limit，改完立刻恢复，仓库继续保持 Private。
- **转公开**：`Settings -> General -> Change visibility -> Public`。公开仓库的 Actions 分钟数免费且不限量，但 `state/domains-state.json` 会变成公开可见（域名与到期日）。
- **本机兜底**（完全不依赖 GitHub）：

  ```bash
  mkdir -p "$HOME/Library/Application Support/digitalplat-auto-renew"
  cat > "$HOME/Library/Application Support/digitalplat-auto-renew/env" <<'ENV'
  DIGITALPLAT_API_TOKEN=dp_live_xxx
  DIGITALPLAT_DOMAINS=your.dpdns.org
  ENV
  chmod 600 "$HOME/Library/Application Support/digitalplat-auto-renew/env"

  bash scripts/local_run.sh --dry-run   # 先试跑
  ```

  想每周自动跑，把 `scripts/com.ray.digitalplat-autorenew.plist` 里的
  `REPLACE_WITH_ABSOLUTE_PATH` 换成仓库绝对路径，然后：

  ```bash
  cp scripts/com.ray.digitalplat-autorenew.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.ray.digitalplat-autorenew.plist
  ```

  日志在 `/tmp/digitalplat-autorenew.log`。

### 2. `DIGITALPLAT_API_TOKEN 未配置，跳过本次定时检查`

只在 Secret 缺失时出现：`Settings -> Secrets and variables -> Actions -> Secrets` 添加 `DIGITALPLAT_API_TOKEN`。

### 3. 续期返回 `404 registered_domain_not_found`

该域名是 `lifecycle_type=permanent`（永久域名）或未登记在可续期列表里 —— **不需要续期**，
脚本会记为 `not_applicable` 并正常退出。

## 文件说明

```
.github/workflows/digitalplat-auto-renew.yml   每周定时工作流
scripts/digitalplat_auto_renew.py              续期脚本（标准库，零依赖）
tests/                                         单元测试（19 个）
state/domains-state.json                       每次运行的状态快照（自动生成并提交）
.keepalive                                     心跳文件（防定时任务被停用）
```

## 安全提示

- API Token 与域名都只存在于 GitHub Secret，不会进仓库、不会出现在日志（Secret 值由 GitHub 自动脱敏为 `***`，脚本还会对每个域名再 `::add-mask::` 一次）。
- 会被提交的 `state/domains-state.json` 和 Actions 的 Job Summary 里，域名一律**打码**（`m*******e.dpdns.org` → `m*******e.dpdns.org`），只保留主域名后缀和到期信息。
- 如果 Token 泄露：立刻到 dashboard 删除该 Key 并重建。
- 仓库即使 Public 也不会暴露完整域名与 Token；但仍建议保持 Private 以减少信息面。

## 致谢 / 许可

- 代码基线：[OUBIGFA/DigitalPlat-Domains-auto-renew](https://github.com/OUBIGFA/DigitalPlat-Domains-auto-renew)（MIT）
- keepalive 与日志脱敏思路：[xz0609/digitalplat-auto-renew](https://github.com/xz0609/digitalplat-auto-renew)（MIT）
- 本仓库同样以 MIT 许可发布，见 [LICENSE](LICENSE)。
