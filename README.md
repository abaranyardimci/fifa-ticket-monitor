# FIFA WC 2026 Ticket Drop Monitor

Polls three FIFA-controlled surfaces every 15 minutes via GitHub Actions and pings
**both Telegram and email** in parallel when something interesting changes — a
Last-Minute Sales drop, a sales-phase update, or a new ticket-related news article.

> **Sibling monitors**: this repo also runs two independent monitors that share
> only the email/Telegram secrets and the generic `notifier`/`emailer`/`http_utils`
> helpers — separate state files, separate workflows, separate crons.
> - **ATC re-announcement monitor** (`atc_monitor.py`) — see [ATC monitor](#atc-monitor).
> - **Palm Beach County court docket monitor** (`pbcourt_monitor.py`) — see
>   [PB Court monitor](#pb-court-monitor).

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
│   ├── monitor.yml              # FIFA cron (every 15 min)
│   ├── atc_monitor.yml          # ATC cron (every 30 min, offset)
│   └── pbcourt_monitor.yml      # PB Court cron (hourly at :47, offset)
├── monitor.py                   # FIFA orchestrator + failure tracking
├── atc_monitor.py               # ATC standalone monitor (separate state)
├── pbcourt_monitor.py           # PB Court docket monitor (separate state)
├── notifier.py                  # Telegram sender (shared)
├── emailer.py                   # Gmail SMTP sender (shared)
├── http_utils.py                # Shared requests session + backoff
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

---

## PB Court monitor

A watcher for a specific case on the **Palm Beach County Clerk eCaseView**
system (`https://appsgp.mypalmbeachclerk.com/eCaseView/`). Hardcoded to case
`50-2025-DR-006596-XXXA-SB` (override via `PB_COURT_CASE_NUMBER` env var).
Emails the user when **new docket entries** appear, with DIN, date,
description, and notes in an HTML table.

**Runs from your Mac, not GitHub Actions.** eCaseView's reCAPTCHA v3 blocks
all headless browsers and any non-residential IP, so the scheduled hourly
check runs via a macOS `launchd` user agent. The GitHub Actions workflow is
retained for **manual** testing only (`test` and `list` modes from the
Actions tab).

**Email-only by design** — Telegram is intentionally skipped here; court
docket notifications go to email for archival/recordkeeping value.

### How it works

| Signal | What triggers it |
|--------|------------------|
| 📁 **N new docket entry/entries** | One or more new entries detected on the case (diff against `state/pbcourt_dockets_seen.json`) |
| ⚠️ **Monitor broken** | 3 consecutive fetch failures (then counter resets) |

Each run, via system Chrome (offscreen, not headless):

1. Loads the eCaseView home page.
2. Clicks **Login as Guest User** (passes reCAPTCHA from your residential IP).
3. Fills `#SearchRequest_CaseNumber` with the case number, clicks `#btnBeginSearch`.
4. Clicks the case in the results table (`button.case-number`).
5. Clicks the **Dockets & Documents** tab.
6. Switches the DataTables length selector to **All** so every entry is rendered.
7. Parses `#docketTable` (columns: icon, icon, DIN, Date, Description, Notes, icon, icon).
8. Diffs entries against the seen state by stable per-entry ID
   `sha1(DIN + Date + Description)[:16]`.

First run establishes a baseline silently (no alerts).

### Files

- `pbcourt_monitor.py` — the monitor script (single file).
- `scripts/pbcourt_run.sh` — launchd wrapper (pulls latest, runs, commits state).
- `scripts/com.user.pbcourt-monitor.plist.tpl` — launchd plist template (substituted by installer).
- `scripts/install_launchd.sh` — installs the launchd job + prompts for Gmail creds.
- `scripts/uninstall_launchd.sh` — removes the launchd job (leaves state + env alone).
- `.github/workflows/pbcourt_monitor.yml` — manual-dispatch workflow (no schedule).
- `state/pbcourt_dockets_seen.json` — map of `entry_id → {din, date, description, notes, first_seen}`.
- `state/pbcourt_failures.json` — consecutive-failure counter.

### Setup on macOS

One-time:

```bash
cd ~/FIFABILET
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium       # for ATC/FIFA tools
# System Chrome is also required (download from google.com/chrome if missing)

# Install the launchd agent (will prompt for GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO)
scripts/install_launchd.sh
```

The installer:
- Creates `~/.config/pbcourt-monitor/env` (chmod 600) with your Gmail creds
- Generates `~/Library/LaunchAgents/com.user.pbcourt-monitor.plist` with the repo path baked in
- Loads it with `launchctl bootstrap gui/$UID …`

After install, the job fires hourly at **minute :47**. launchd skips firings
while the Mac is asleep; it does not run a make-up job on wake.

### Running manually

```bash
# Trigger one run immediately (writes to ~/Library/Logs/pbcourt-monitor.log)
scripts/pbcourt_run.sh

# Or invoke the script directly without the wrapper
.venv/bin/python pbcourt_monitor.py             # one normal run
.venv/bin/python pbcourt_monitor.py --test      # send a test email only
.venv/bin/python pbcourt_monitor.py --list-state
```

### Inspecting / debugging

```bash
# Job status
launchctl print "gui/$UID/com.user.pbcourt-monitor"

# Live log
tail -f ~/Library/Logs/pbcourt-monitor.log

# launchd's own stdout/stderr capture
tail -f ~/Library/Logs/pbcourt-monitor.launchd.out.log
tail -f ~/Library/Logs/pbcourt-monitor.launchd.err.log
```

### Uninstall

```bash
scripts/uninstall_launchd.sh
```

Leaves your env file (`~/.config/pbcourt-monitor/env`) and state files in
the repo alone in case you reinstall later.

### Monitoring a different case

Edit `CASE_NUMBER` at the top of `pbcourt_monitor.py`, OR set the
`PB_COURT_CASE_NUMBER` env var in `~/.config/pbcourt-monitor/env`. After
changing, delete `state/pbcourt_dockets_seen.json` so the next run
re-baselines for the new case.

### Safety guards

- If the parser returns **0 entries** but state has ≥1, the run is treated as
  a **failure** (likely login regression or restricted access), not a
  "everything was deleted" event. State is preserved.
- Hourly polling is well below any reasonable rate-limit threshold.
- System Chrome with `channel="chrome"`, AutomationControlled stealth tweaks,
  and an offscreen visible window (no headless — reCAPTCHA detects it).

### Why not GitHub Actions?

We tried. The eCaseView guest-login flow uses **reCAPTCHA v3** which:
1. Blocks bundled Chromium (Playwright's default) by fingerprint.
2. Blocks any headless browser, including system Chrome in headless mode.
3. Heavily penalizes datacenter IPs (GitHub Actions, AWS, GCP).

The only combination that scores high enough is: system Chrome + visible
window + residential IP. Running locally via launchd satisfies all three.

## StubHub repricer

Keeps your StubHub ticket listings priced as the **lowest comparable in your
section** so StubHub promotes them — while **never netting below your cost** and
maximising profit. Built for 3 FIFA WC 2026 listings but config-driven for any.

**Three parts:**

- **Read / recommend (scheduled, anonymous — no login, `stubhub_repricer.py`):**
  opens each event page in offscreen system Chrome, clicks **"Show more"** until
  every listing is loaded, parses the rendered listing rows (section / row / seat
  / qty / all-in price), finds the cheapest comparable in your section, computes a
  recommended price, and **emails you a recommendation** (only when it changes).
  Never touches money.
- **Approve from any device (email command channel, `stubhub_commander.py`):** the
  recommendation email carries **Approve / Decline / Modify** buttons. Tapping one
  on your iPhone/iPad/anything opens a pre-filled reply; a daemon on the Mac polls
  Gmail over **IMAP (outbound only — no inbound port)**, verifies it, and runs the
  apply path. See [Remote control](#remote-control-approvedeclinemodify-from-any-device).
- **Apply (authenticated):** `--apply <key>` reuses a logged-in Chrome profile to
  set an *approved* price, after re-checking the live market and StubHub's own
  displayed payout. The only path that changes a price.

This is **notify-and-approve** by design: the schedule proposes, you approve from
any device, the Mac applies.

### How StubHub's data actually works (discovered against the live site)

- StubHub runs on **viagogo**; listings are **server-rendered into the DOM**
  (`#listings-container`), not a clean JSON API — so we parse the DOM.
- Only ~10 of N listings load initially (sorted "best deal"), so we click
  **"Show more"** until all are loaded, then filter to your section.
- Displayed prices are **all-in** ("incl. fees" — list price + buyer fee). We
  compare **all-in to all-in** (the unit buyers sort on) and convert to a seller
  **list price** (what you type) using `buyer_fee_pct` only for the floor check
  and the price to type. Real payout is read live at apply-time as the backstop.
- You are usually the **only seller in your exact row**, so the useful
  comparison is **section-level** (`compare_mode: "section"`); the email shows
  the section price ladder with each row/seat so you can judge a seat-quality
  premium. Use `compare_mode: "row"` (+ `row_tolerance`) for strict same-row.

### Pricing logic (all comparison in all-in space)

- `floor_list = ceil(unit_cost / (1 − fee_rate))` — never net below this.
  (Argentina ≈ $2,778 · M89 ≈ $1,059/ea · M103 ≈ $1,324/ea at 15%.)
- `competitorMin` = cheapest comparable in your section (excluding your own,
  matched by section + row + `our_seat`).
- `target = competitorMin × (1 − undercut_pct)`, strictly below (no ties).
- **PROMOTE / RAISE** when above floor (raises if you're under-priced).
- **HOLD_AT_FLOOR** when the cheapest comp is below your floor.
- **CAPPED** when a single-run drop would exceed `max_drop_pct`.
- **NO_COMP / NO_SELLER** → hold (NO_SELLER hints room to raise).

The **authoritative floor check** is at apply-time: it reads StubHub's own
payout and **refuses to set a price whose payout < your cost** (the real
commission is dynamic, not exactly 15%).

### Files

- `stubhub_repricer.py` — the repricer engine + scraper + apply path.
- `stubhub_commander.py` — email command poller (Approve/Decline/Modify over IMAP).
- `stubhub_listings.json` — per-listing config (**no secrets**; fill placeholders).
- `scripts/stubhub_repricer_run.sh` — launchd wrapper (pulls, runs, commits state).
- `scripts/stubhub_commander_run.sh` — launchd wrapper for the command poller.
- `scripts/com.user.stubhub-repricer.plist.tpl` — repricer launchd plist template.
- `scripts/com.user.stubhub-commander.plist.tpl` — commander launchd plist (KeepAlive).
- `scripts/com.user.stubhub-caffeinate.plist.tpl` — keep-awake agent (`caffeinate -s`).
- `scripts/install_stubhub_repricer.sh` / `uninstall_stubhub_repricer.sh`.
- `scripts/install_stubhub_commander.sh` — installs the poller + generates the HMAC secret.
- `scripts/install_stubhub_caffeinate.sh` — installs the keep-awake agent.
- `state/stubhub_prices.json` — per-listing current/recommended/comp-min/history + the
  HMAC of the pending approve code (never the raw code).
- `state/stubhub_repricer_failures.json` — consecutive-failure counter.

> The old localhost-only `stubhub_approver.py` HTTP server is **retired** — its
> `http://127.0.0.1` links only worked on the Mac itself. The email command
> channel replaces it and works from any device.

### Setup

```bash
cd ~/FIFABILET     # venv + system Chrome as above

# 1. Fill stubhub_listings.json: event_url, section, rows, our_seat, quantity,
#    unit_cost. (compare_mode defaults to "section".)

# 2. Sanity-check what the scraper sees for each listing (no email, no changes):
.venv/bin/python stubhub_repricer.py --check argentina_capeverde
#    Confirm the "<-- looks like YOURS" row matches your real seat (via --probe).

# 3. (Optional) Log in once so --apply can edit prices for you later:
.venv/bin/python stubhub_repricer.py --login

# 4. Install the scheduled recommend job (prompts for Gmail creds):
scripts/install_stubhub_repricer.sh

# 5. Install the email command channel (Approve/Decline/Modify from any device):
scripts/install_stubhub_commander.sh

# 6. Keep the Mac awake for 24/7 monitoring (no sudo; requires AC power):
scripts/install_stubhub_caffeinate.sh
#    For lid-closed (clamshell) operation on AC, ALSO run once:
sudo pmset -c disablesleep 1
```

The job fires ~every 3 hours during waking hours (08/11/14/17/20/23 at :17).
If you'd rather not automate the edit, you can ignore `--login`/`--apply`
entirely and just change prices in the StubHub app using the emailed numbers.

> **Gmail IMAP must be on** for the command channel: Gmail (web) → Settings →
> *Forwarding and POP/IMAP* → **Enable IMAP**. The poller signs in with the same
> App Password used for sending.

### Commands

```bash
.venv/bin/python stubhub_repricer.py --selftest        # pricing asserts (no network)
.venv/bin/python stubhub_repricer.py --test            # send a plain test email
.venv/bin/python stubhub_repricer.py --sample-email    # send a SAMPLE recommendation with Approve/Decline/Modify buttons (no scrape, no price change) to test the remote loop
.venv/bin/python stubhub_repricer.py --list-config     # print parsed config
.venv/bin/python stubhub_repricer.py --list-state      # print stored state
.venv/bin/python stubhub_repricer.py --probe  KEY      # scrape + dump every parsed listing in your section
.venv/bin/python stubhub_repricer.py --check  KEY      # scrape + print recommendation + ladder (no email)
.venv/bin/python stubhub_repricer.py --apply  KEY --dry-run            # preview the edit, no confirm
.venv/bin/python stubhub_repricer.py --apply  KEY                      # set the LIVE-recommended price (CLI)
.venv/bin/python stubhub_repricer.py --apply  KEY --price 4500 --check-drift   # APPROVE: set $4,500 all-in, refuse if market drifted
.venv/bin/python stubhub_repricer.py --apply  KEY --price 4500        # MODIFY: set your exact $4,500 all-in (floor-gated)
scripts/stubhub_repricer_run.sh                        # one scheduled-style run (writes to log)
```

### Remote control: Approve / Decline / Modify from any device

Every recommendation email has three buttons:

| Button | What it does | Money? |
|--------|--------------|--------|
| ✅ **Approve** | Apply the price you were emailed. The Mac re-scrapes, and **refuses if the market moved >max(min_change_abs, 3%)** since the email (re-sends a fresh one instead), and refuses to net below cost. | yes |
| ✏️ **Modify** | Edit the **last number in the subject** to your own all-in price, then send. Applied exactly (still floor + live-payout gated). | yes |
| ✖︎ **Decline** | Drop the recommendation, no price change. | no |

Each button opens a pre-filled email whose subject is `ACTION <key> <one-time-code>`.
The Mac's poller verifies (1) the **sender** is you, (2) the **one-time code**
(single-use; state stores only its HMAC, so even a public repo never leaks a
usable code), and (3) the recommendation is **< 36 h old**. Latency is ≈ one poll
(~90 s). Test it safely with `--sample-email` then tapping **Decline** on your
phone.

> Commands must be **sent from your own Gmail address** (`GMAIL_USER` / `EMAIL_TO`).
> If your phone composes from a different account, the poller ignores it.

### Why not fully cloud (Mac off)?

StubHub/viagogo sits behind **DataDome**, which blocks datacenter IPs and headless
browsers — scrapes only succeed from a **residential IP + system Chrome**. The
apply path also needs the **logged-in Chrome profile that lives on this Mac**. So
the Mac is the engine; the email channel just lets you *control* it from anywhere.
The keep-awake agent (`caffeinate -s`, on AC power) keeps it monitoring 24/7.

### Buyer-fee calibration (improves the "list price to type")

Displayed prices are all-in. To turn a target all-in price into the **list price
you type**, the engine divides by `1 + buyer_fee_pct` (default 0.27). To make
this exact, set `buyer_fee_pct` per listing: take any one of your listings, note
its **all-in** price (from `--check`) and the **list price** you actually entered
on StubHub, then `buyer_fee_pct = all_in / list − 1`. Even if it's off, the
apply-time payout check still refuses anything that nets below your cost.

### Safety guards

- **Approved-price binding (drift guard).** Approve applies the *number you saw*,
  not a silent live recompute. If the live recommendation has moved more than
  `max(min_change_abs, 3%)` from what you approved, it **refuses** and a fresh
  recommendation is emailed. (Resale prices can swing fast — this is the single
  human checkpoint, so it enforces your intent.)
- **Partial-render guard.** If the page advertises "Showing N of M" and fewer
  than 60% of M actually rendered, the scrape is treated as a **block** (a thin
  render yields a confident-but-wrong `competitorMin`).
- **Comp-stability guard.** A one-run `competitorMin` swing >60% vs the prior run
  is flagged **low-confidence**: the level is recorded but not emailed/applied
  until a later run confirms it.
- **Cost floor + optional margin.** `floor_list = ceil((unit_cost + min_profit) /
  (1 − fee_rate))`. Set `min_profit` (per ticket, dollars) in `stubhub_listings.json`
  to keep a buffer against the dynamic real commission. Apply refuses any
  list-to-type below this floor.
- **Post-Save payout backstop.** After setting a price, if StubHub's own "You'll
  get" payout is below your cost, the result email is flagged **⚠️ REVERT NOW**
  (the real commission can exceed `fee_rate`).
- **Empty/blocked scrape with prior data → failure** (no drop recommended);
  "repricer broken" email after 3 consecutive failures.
- **Self-exclusion** by section + row + `our_seat` / `our_listing_id` (now also
  populated from the scraped listing link).
- **Single-use approve codes**, sender-verified, 36 h expiry; **only the price
  field is ever edited**; dedicated Chrome profile; outbound-only IMAP (no inbound
  port).

### ⚠️ FIFA cancellation risk (not a code issue — know this)

For WC 2026, FIFA tickets transfer via the FIFA account portal, and FIFA's terms
state they are **non-transferable outside FIFA's own resale marketplace** — FIFA
can cancel tickets resold via third parties (StubHub) without refund. This tool
only optimises price; the underlying cancellation risk is independent of any
pricing logic and is yours to weigh.
