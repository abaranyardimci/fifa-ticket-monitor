# FIFA WC 2026 Ticket Drop Monitor

Polls three FIFA-controlled surfaces every 15 minutes via GitHub Actions and pings
Telegram when something interesting changes — a Last-Minute Sales drop, a sales-phase
update, or a new ticket-related news article.

## Targets

| # | Target | URL | Strategy |
|---|--------|-----|----------|
| 1 | Ticket shop / queue | `https://fwc26-shop-usd.tickets.fifa.com/` | Playwright (Chromium), hash main content |
| 2 | Sales-info page | `https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/ticket-sales` | Try `__NEXT_DATA__` JSON, fall back to Playwright |
| 3 | News index | XML API: `https://cxm-api.fifa.com/fifaplusweb/api/sitemaps/articles/0`<br>Fallback: `https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/news` | XML sitemap (no browser), Playwright fallback |

Target 3 is the highest-priority signal: it diffs a list of seen article URLs
and alerts only when a new article URL or title contains any of `ticket`,
`tickets`, `drop`, `sales-phase`, `last-minute`, `resale`, `marketplace`
(case-insensitive).

## Telegram messages

| Trigger | Message prefix |
|---------|----------------|
| Target 1 changed | `🛒 Shop changed` |
| Target 2 changed | `📄 Sales info page changed` |
| Target 3 new matching article | `🚨 NEW TICKET ARTICLE` *(highest priority)* |
| Target failed 3× in a row | `⚠️ Monitor broken: <target>` |

All messages use Telegram Markdown with a clickable link.

## Setup

### 1. Create a Telegram bot

1. DM [@BotFather](https://t.me/BotFather), `/newbot`, save the token.
2. Send any message to your new bot.
3. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":<number>,...`.

### 2. GitHub repository setup

1. Create a new GitHub repo and push this code.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` — bot token from BotFather
   - `TELEGRAM_CHAT_ID` — your chat id
3. **Settings → Actions → General → Workflow permissions**: ensure **Read and write permissions** is selected so the workflow can commit updated state files.
4. The cron starts firing automatically once the workflow is on the default branch (GitHub may take ~10 min for the first scheduled run).

### 3. First-run behaviour

The first time the monitor sees each target, it establishes a baseline silently
(no alerts). From the second run onward, only changes trigger alerts.

### 4. Test the pipeline

From GitHub:
- **Actions → FIFA WC2026 Ticket Monitor → Run workflow**
- Set **Mode** = `test` → confirms Telegram delivery.
- Set **Mode** = `list` → prints stored state to the run log.

Locally:
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python monitor.py --test
```

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# Send a test alert
python monitor.py --test

# Inspect stored state
python monitor.py --list-targets

# Run a single target
python monitor.py --target news
python monitor.py --target sales_info
python monitor.py --target shop
```

## State files (committed to the repo)

| File | Purpose |
|------|---------|
| `state/news_seen.json` | Map of article URL → `{title, first_seen}` |
| `state/sales_info.hash` | SHA-256 of last-seen sales-info signature |
| `state/shop.hash` | SHA-256 of last-seen shop signature |
| `state/failure_counts.json` | Per-target consecutive-failure counter |

State persists across runs because the workflow commits changes back to `main`
with the message `chore(state): update monitor state [skip ci]`.

## Reliability behaviour

- All HTTP requests use a realistic Chrome User-Agent, 30 s timeout, exponential
  backoff (1 → 2 → 4 → 8 s) on 429 / 5xx / network errors, max 4 attempts.
- Per-target try/except — one target failing never breaks the others.
- After **3 consecutive failures**, fires the "monitor broken" Telegram alert
  and resets the counter (no spam).
- Script always exits 0 so the workflow proceeds to commit state and the next
  cron tick retries.
- Concurrency group prevents an in-progress run from being killed by the next
  cron tick.
- If `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are unset the script logs a single
  warning and runs in **dry-run mode** — alerts are written to the log instead
  of being sent. Useful for local debugging.

## Known limitation: shop target may be blocked from datacenter IPs

The shop URL is behind Akamai. From a residential IP, headless Playwright (with
the included stealth tweaks) usually gets through. From cloud / datacenter IPs
(GitHub Actions, AWS, GCP) Akamai often returns a "Bad request — Reference #..."
block page.

The monitor detects this WAF block page and treats it as a target **failure**
(not a "shop changed" event), so you won't get false-positive alerts. After 3
consecutive blocks (~45 min on the default cron) you'll get one "⚠️ Monitor
broken: shop" alert, then the counter resets and tries again.

If you need reliable shop monitoring, options are:

1. Run the monitor on a self-hosted runner with a residential / mobile IP.
2. Front Playwright with a residential-proxy provider and pass it via the
   `PROXY_URL` env var (you'd need to extend `targets/shop.py` to wire it).
3. Rely on the Target 3 news index — FIFA tends to publish a "Last Minute Sales
   Phase ticket drop" article around real drops, and the news monitor catches
   those reliably (verified against live data).

## Tuning

- Change cron frequency in `.github/workflows/monitor.yml` (line: `cron: '*/15 * * * *'`).
- Add or remove keywords for Target 3 in `targets/news.py` (`KEYWORDS` tuple).
- Adjust hash-exclusion selectors in `targets/shop.py` and `targets/sales_info.py`
  (`_EXCLUDE_SELECTORS`) if a chrome element keeps causing false positives.

## Layout

```
.
├── .github/workflows/monitor.yml
├── monitor.py                 # CLI + orchestrator + failure tracking
├── notifier.py                # Telegram sender, retry, message templates
├── http_utils.py              # Shared requests session + backoff
├── targets/
│   ├── __init__.py            # TargetResult dataclass
│   ├── news.py                # Target 3 — XML sitemap + Playwright fallback
│   ├── sales_info.py          # Target 2 — __NEXT_DATA__ + Playwright fallback
│   └── shop.py                # Target 1 — Playwright
├── state/                     # committed state (hashes + seen-articles JSON)
├── requirements.txt
└── README.md
```
