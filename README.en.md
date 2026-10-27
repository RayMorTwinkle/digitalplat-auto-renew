# DigitalPlat Free Domain Auto-Renew (merged)

Weekly GitHub Actions job that checks your DigitalPlat domains and renews them
for free once they enter the 120-day window before expiry. Standard library
only, no third-party dependencies.

## Why merged

It is based on [OUBIGFA/DigitalPlat-Domains-auto-renew](https://github.com/OUBIGFA/DigitalPlat-Domains-auto-renew)
(the upstream template: stdlib client, `Idempotency-Key`, `domain_id` fallback,
`lifecycle_type=permanent` skip, state file, tests), plus the only two useful
pieces from [xz0609/digitalplat-auto-renew](https://github.com/xz0609/digitalplat-auto-renew):
the weekly keepalive commit (GitHub disables scheduled workflows after 60 days
of repository inactivity) and log masking of domain names.

Added on top: WAF/Cloudflare 403 retry with a browser User-Agent fallback
(so `cloudscraper` is no longer needed) and a GitHub Actions job summary.

## Schedule

- Runs every Monday at `04:17 UTC` (`cron: "17 4 * * 1"`).
- Renews only when `days_remaining <= 120` (configurable).
- Calls `POST /domains/{domain}/renew` with `{"renewal_type": "free", "years": 1}`.

## Setup

Add the secret `DIGITALPLAT_API_TOKEN` (a `dp_live_...` key from
<https://dash.domain.digitalplat.org/dashboard/api/keys>) under
`Settings -> Secrets and variables -> Actions`.

Optional variables: `DIGITALPLAT_DOMAINS` (empty means "all domains on the
account"), `DIGITALPLAT_RENEW_BEFORE_DAYS` (default `120`),
`DIGITALPLAT_RENEWAL_TYPE`, `DIGITALPLAT_RENEWAL_YEARS`, `DIGITALPLAT_API_BASE`.

## Local usage

```bash
DIGITALPLAT_API_TOKEN=dp_live_xxx DIGITALPLAT_DOMAINS=your.dpdns.org \
  python3 scripts/digitalplat_auto_renew.py --dry-run
python3 -m unittest discover -s tests -v
```

## License

MIT, see [LICENSE](LICENSE).
