# FIFA WC 2026 Ticket Drop Monitor

Polls three FIFA-controlled surfaces every 15 minutes via GitHub Actions and pings
**both Telegram and email** in parallel when something interesting changes — a
Last-Minute Sales drop, a sales-phase update, or a new ticket-related news article.

> **Sibling monitor**: this repo also runs an independent ATC re-announcement
> monitor (`atc_monitor.py` + `.github/workflows/atc_monitor.yml`) that watches
> `https://www.americanturkishconference.org` for the conference being put back
> on the calendar. It shares only the Telegram/email secrets and the generic
> notifier/emailer/http_utils helpers — separate state files, separate workflow,
> separate cron. See [ATC monitor](#atc-monitor) at the bottom.

Both channels fire on every alert, independently. If one fails (server down,
revoked token, muted chat), the other still gets through. Either channel is
optional — the monitor degrades gracefully to whichever you've configured.

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

## Notifications

Each alert is delivered to every configured channel in parallel:

| Trigger | Telegram message | Email subject |
|---------|------------------|---------------|
| Target 1 changed | `🛒 Shop changed` | `[FIFA Monitor] 🛒 Shop changed` |
| Target 2 changed | `📄 Sales info page changed` | `[FIFA Monitor] 📄 Sales info page changed` |
| Target 3 new matching article | `🚨 NEW TICKET ARTICLE` *(highest priority)* | `[FIFA Monitor] 🚨 NEW TICKET ARTICLE: <title>` |
| Target failed 3× in a row | `⚠️ Monitor broken: <target>` | `[FIFA Monitor] ⚠️ Monitor broken: <target>` |

Telegram messages are Markdown with clickable links. Emails are multipart
(plain text + minimal HTML) sent via Gmail SMTP.

## Setup

### 1. Create a Telegram bot

1. DM [@BotFather](https://t.me/BotFather), `/newbot`, save the token.
2. Send any message to your new bot.
3. Get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":<number>,...`.

### 2. (Optional but recommended) Set up Gmail email channel

Email is a robust safety net for when Telegram push notifications get
suppressed by phone settings (Focus mode, muted chat, app permissions).

1. Enable 2-Step Verification on your Google account if not already on:
   https://myaccount.google.com/security
2. Generate an App Password: https://myaccount.google.com/apppasswords
   - App: "Mail" (or pick "Other" and call it "FIFA Monitor")
   - Save the 16-character password (shown only once).
3. You'll add this as a GitHub secret in the next step.

### 3. GitHub repository setup

1. Create a new GitHub repo and push this code.
2. **Settings → Secrets and variables → Actions → New repository secret** —
   add the secrets for whichever channels you want:
   - `TELEGRAM_BOT_TOKEN` — bot token from BotFather *(Telegram only)*
   - `TELEGRAM_CHAT_ID` — your chat id *(Telegram only)*
   - `GMAIL_USER` — your full Gmail address, e.g. `you@gmail.com` *(email only)*
   - `GMAIL_APP_PASSWORD` — the 16-char App Password *(email only)*
   - `EMAIL_TO` — *optional*; defaults to `GMAIL_USER` if unset
3. **Settings → Actions → General → Workflow permissions**: ensure **Read and write permissions** is selected so the workflow can commit updated state files.
4. The cron starts firing automatically once the workflow is on the default branch (GitHub may take ~10 min for the first scheduled run).

### 4. First-run behaviour

The first time the monitor sees each target, it establishes a baseline silently
(no alerts). From the second run onward, only changes trigger alerts.

### 5. Test the pipeline

From GitHub:
- **Actions → FIFA WC2026 Ticket Monitor → Run workflow**
- Set **Mode** = `test` → sends one test message to *every configured channel*
  (Telegram and/or email). Both should arrive.
- Set **Mode** = `list` → prints stored state and which channels are configured.

Locally:
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
python monitor.py --test
```

### Telegram push troubleshooting

If you see Telegram messages only after opening the app (no banner / sound):

1. Open the bot chat in Telegram → tap the bot name at the top → confirm the
   bell icon is **not crossed out** (chat is unmuted).
2. iOS: **Settings → Notifications → Telegram** → Allow Notifications: ON,
   Sounds: ON, Badges: ON, Show Previews: Always.
3. iOS Focus / DND: ensure Telegram is on the allowed-apps list, or that the
   active Focus mode is off when you want alerts.
4. Phone Settings → Battery → ensure Telegram is not background-restricted.

The email channel is the safety net for when Telegram push gets suppressed.

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
- If **no** notification channel is configured (neither Telegram nor Gmail
  creds present), the script logs a warning and runs in **dry-run mode** —
  alerts are written to the log instead of being sent. Useful for local debugging.
- If only one channel is configured, only that channel fires; the other is
  silently skipped. Each channel is independent — one failing never blocks
  the other.

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
├── .github/workflows/
│   ├── monitor.yml            # FIFA cron (every 15 min)
│   └── atc_monitor.yml        # ATC cron (every 30 min, offset)
├── monitor.py                 # FIFA orchestrator + failure tracking
├── atc_monitor.py             # ATC standalone monitor (separate state)
├── notifier.py                # Telegram sender (shared by both monitors)
├── emailer.py                 # Gmail SMTP sender (shared by both monitors)
├── http_utils.py              # Shared requests session + backoff
├── targets/
│   ├── __init__.py            # TargetResult dataclass
│   ├── news.py                # FIFA target 3 — XML sitemap + Playwright fallback
│   ├── sales_info.py          # FIFA target 2 — __NEXT_DATA__ + Playwright fallback
│   └── shop.py                # FIFA target 1 — Playwright
├── state/                     # committed state for both monitors (atc_* + the rest)
├── requirements.txt
└── README.md
```

---

## ATC monitor

A small, completely separate watcher for the **40th American-Turkish
Conference** website (`https://www.americanturkishconference.org`). Built to
catch the moment the conference is put back on the calendar after its
postponement — registration link, agenda publication, year/date update,
"rescheduled" notice, etc.

### How it works

| Signal | What triggers it |
|--------|------------------|
| 🚨 **NEW SIGNAL** | A high-signal phrase appears for the first time: `register`, `tickets`, `speakers`, `program`/`agenda` (no longer "will be published"), `2026`, `2027`, `rescheduled`, `confirmed`, `live stream`, etc. |
| 📝 **Page changed** | Catch-all — body content hash differs from last run, and no specific keyword fired. Lower priority. |
| ⚠️ **Monitor broken** | 3 consecutive fetch failures in a row (then counter resets). |

Each alert goes to every configured channel (Telegram + email) in parallel,
same secrets as the FIFA monitor. Subjects are prefixed `[ATC Monitor]` so
they're easy to filter / route.

### Files

- `atc_monitor.py` — single self-contained script.
- `.github/workflows/atc_monitor.yml` — cron `7,37 * * * *` (offset from FIFA's `*/15` to avoid `git push` races).
- `state/atc_page.hash` — last-seen content hash.
- `state/atc_keywords_seen.json` — which signal keywords have ever been seen, with first-seen timestamp.
- `state/atc_failures.json` — consecutive-failure counter.

### Running locally

```bash
source .venv/bin/activate
python atc_monitor.py             # one normal run
python atc_monitor.py --test      # test message via every configured channel
python atc_monitor.py --list-state
```

### Customising the keywords

Edit `KEYWORD_SIGNALS` near the top of `atc_monitor.py`. Each entry is a
`(label, regex)` pair — add a new one and it'll fire the first time it
matches the cleaned page text. The negative lookahead in the
`Program / agenda published` pattern is what stops the existing
"Agenda Will Be Published Soon" placeholder from firing on every run.

### Won't this break the FIFA monitor?

No. The two monitors share:
- the same `requirements.txt`
- the same Gmail/Telegram secrets
- generic helpers (`notifier.send`, `emailer.send`, `http_utils.get`)

They do **not** share state files, workflows, cron schedules, or
target/message code. The ATC workflow only `git add`s `state/atc_*` files,
rebases before push to absorb FIFA-monitor commits, and runs at minute :07 /
:37 to stay clear of FIFA's :00 / :15 / :30 / :45 ticks.
