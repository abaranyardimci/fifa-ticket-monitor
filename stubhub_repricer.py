"""StubHub dynamic ticket repricer (FIFA World Cup listings).

Keeps each managed StubHub listing competitively priced within its section so
StubHub promotes it, while never netting below your cost and maximising profit.

Two halves, one module:

  * READ / RECOMMEND (scheduled, anonymous — no login): opens each event page in
    offscreen system Chrome, clicks "Show more" until every listing is loaded,
    parses the rendered listing rows (section / row / seat / qty / all-in price),
    finds the cheapest comparable in your section, computes a recommended price,
    and emails a recommendation — only when it changes. Never touches money.

  * APPLY (on-demand, authenticated): `--apply <key>` reuses a logged-in Chrome
    profile to set an *approved* price, after re-checking the live market and
    StubHub's own displayed payout (refuses to net below cost). Only this path
    changes money, and only when you run it.

Reality notes baked in (discovered against the live StubHub/viagogo site):
  * StubHub is viagogo under the hood; listings are server-rendered into
    `#listings-container`, NOT exposed as a clean JSON XHR. We parse the DOM.
  * Displayed prices are ALL-IN (include the buyer fee) — "incl. fees". We
    compare all-in-to-all-in (that's what buyers sort on), and convert to a
    seller "list" price (what you type) only for the floor check and apply.
  * You are typically the only seller in your exact row, so the useful
    comparison is section-level (your "category"), with row/seat shown so you
    can judge the premium a better seat deserves. `compare_mode` controls this.

CLI:
    python stubhub_repricer.py                 # scheduled run (recommend + email)
    python stubhub_repricer.py --test          # send a test email
    python stubhub_repricer.py --selftest      # pricing unit asserts (no network)
    python stubhub_repricer.py --list-config   # print parsed config
    python stubhub_repricer.py --list-state    # print stored state
    python stubhub_repricer.py --check  KEY    # scrape one event, print recommendation + ladder
    python stubhub_repricer.py --probe  KEY    # scrape + dump every parsed listing
    python stubhub_repricer.py --login         # one-time StubHub login in the bot profile
    python stubhub_repricer.py --apply  KEY [--dry-run]   # set the approved price (money-safe)
    python stubhub_repricer.py -v              # verbose logging
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

import emailer

LOGGER = logging.getLogger("stubhub_repricer")

NAME = "stubhub_repricer"
SUBJECT_PREFIX = "[StubHub Repricer]"

REPO_DIR = Path(__file__).resolve().parent
CONFIG_FILE = REPO_DIR / "stubhub_listings.json"
STATE_DIR = REPO_DIR / "state"
PRICES_STATE_FILE = STATE_DIR / "stubhub_prices.json"
FAILURE_STATE_FILE = STATE_DIR / "stubhub_repricer_failures.json"
LOCK_FILE = STATE_DIR / ".stubhub_repricer.lock"

CHROME_PROFILE_DIR = Path(
    os.environ.get(
        "STUBHUB_CHROME_PROFILE",
        str(Path.home() / ".config" / "stubhub-repricer" / "chrome-profile"),
    )
)

FAILURE_LIMIT = 3

# Typical viagogo/StubHub buyer fee added on top of the seller's list price to
# form the displayed all-in price. Only used to (a) express your cost floor in
# all-in terms and (b) suggest the list price to type. The authoritative payout
# is read live at apply-time, so an imperfect estimate here is safe.
DEFAULT_BUYER_FEE_PCT = 0.27

# Statuses
ST_PROMOTE = "PROMOTE"            # undercut the cheapest comparable (a drop or sideways move)
ST_RAISE = "RAISE"               # we're under-priced; raise toward the cheapest comparable
ST_HOLD_FLOOR = "HOLD_AT_FLOOR"  # cheapest comp is below our cost floor; hold (not promoted)
ST_CAPPED = "CAPPED_NOT_LOWEST"  # a single-run drop cap stops us short of lowest this run
ST_NO_COMP = "NO_COMP"           # no comparable in scope -> hold
ST_NO_SELLER = "NO_SELLER"       # we're the only listing in the section -> hold (room to raise)
ST_BLOCKED = "BLOCKED"           # scrape returned nothing -> do nothing
ST_PAST = "SKIPPED_PAST"         # event already happened


# ---------- config ----------

@dataclass(frozen=True)
class ListingConfig:
    key: str
    label: str
    event_url: str
    event_datetime: Optional[datetime]
    section: str
    rows: tuple[str, ...]
    our_seat: Optional[str]       # e.g. "24" or "7-8"; used to exclude our own listing
    row_tolerance: int
    compare_mode: str            # "section" (any row in section) or "row" (same/similar row)
    quantity: int
    sell_together: bool
    our_listing_id: Optional[str]
    unit_cost: float
    fee_rate: float              # StubHub seller commission estimate
    buyer_fee_pct: Optional[float]
    undercut_pct: float
    max_drop_pct: float
    value_premium_pct: float     # when you're the best seat: % above the cheapest worse-row seat
    currency: str
    min_change_abs: float
    min_change_pct: float

    @property
    def floor_list(self) -> int:
        """Lowest seller list price whose payout still covers cost."""
        return math.ceil(self.unit_cost / (1.0 - self.fee_rate))

    # Back-compat alias used by --selftest.
    @property
    def floor(self) -> int:
        return self.floor_list

    @property
    def buyer_fee(self) -> float:
        return self.buyer_fee_pct if self.buyer_fee_pct is not None else DEFAULT_BUYER_FEE_PCT

    @property
    def floor_allin(self) -> int:
        return math.ceil(self.floor_list * (1.0 + self.buyer_fee))


class ConfigError(RuntimeError):
    pass


def allin_to_list(allin: float, cfg: ListingConfig) -> float:
    return allin / (1.0 + cfg.buyer_fee)


def payout_from_allin(allin: float, cfg: ListingConfig) -> float:
    return allin_to_list(allin, cfg) * (1.0 - cfg.fee_rate)


def _parse_event_dt(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        LOGGER.warning("could not parse event_datetime %r; treating as undated", value)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce_listing_config(key: str, raw: dict) -> ListingConfig:
    def req(name: str) -> Any:
        if name not in raw or raw[name] in (None, "", []):
            raise ConfigError(f"listing {key!r}: missing required field {name!r}")
        return raw[name]

    rows_val = raw.get("rows") or ([raw["row"]] if raw.get("row") else [])
    rows = tuple(str(r).strip() for r in rows_val if str(r).strip())

    return ListingConfig(
        key=key,
        label=str(raw.get("label", key)),
        event_url=str(req("event_url")),
        event_datetime=_parse_event_dt(raw.get("event_datetime")),
        section=str(req("section")).strip(),
        rows=rows,
        our_seat=(str(raw["our_seat"]).strip() or None) if raw.get("our_seat") else None,
        row_tolerance=int(raw.get("row_tolerance", 0)),
        compare_mode=str(raw.get("compare_mode", "section")).lower().strip(),
        quantity=int(raw.get("quantity", 1)),
        sell_together=bool(raw.get("sell_together", True)),
        our_listing_id=(str(raw["our_listing_id"]).strip() or None) if raw.get("our_listing_id") else None,
        unit_cost=float(req("unit_cost")),
        fee_rate=float(raw.get("fee_rate", 0.15)),
        buyer_fee_pct=(float(raw["buyer_fee_pct"]) if raw.get("buyer_fee_pct") is not None else None),
        undercut_pct=float(raw.get("undercut_pct", 0.01)),
        max_drop_pct=float(raw.get("max_drop_pct", 0.15)),
        value_premium_pct=float(raw.get("value_premium_pct", 0.10)),
        currency=str(raw.get("currency", "USD")),
        min_change_abs=float(raw.get("min_change_abs", 10.0)),
        min_change_pct=float(raw.get("min_change_pct", 0.005)),
    )


def load_config() -> dict[str, ListingConfig]:
    if not CONFIG_FILE.exists():
        raise ConfigError(f"Config not found at {CONFIG_FILE}. See README.")
    try:
        with CONFIG_FILE.open() as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"Could not read {CONFIG_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_FILE} must be a JSON object of listings.")
    out: dict[str, ListingConfig] = {}
    for key, raw in data.items():
        if key.startswith("_"):
            continue
        if not isinstance(raw, dict):
            raise ConfigError(f"listing {key!r} must be an object.")
        out[key] = _coerce_listing_config(key, raw)
    if not out:
        raise ConfigError(f"{CONFIG_FILE} has no listings.")
    return out


# ---------- listing model ----------

@dataclass(frozen=True)
class Listing:
    section: str
    row: str
    seat: str
    quantity: int
    price: float              # ALL-IN (incl. fees), as displayed to buyers
    listing_id: str = ""
    badges: str = ""
    text: str = field(default="", repr=False, compare=False)


# ---------- DOM scraper (Playwright, offscreen system Chrome) ----------

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-position=-2400,-2400",
    "--window-size=1400,1000",
]
_WEBDRIVER_PATCH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

_SHOW_MORE_JS = """() => {
    const els = [...document.querySelectorAll('button, a')];
    const b = els.find(x => /show more/i.test(x.textContent || ''));
    if (!b) return 'none';
    b.scrollIntoView({block: 'center'});
    b.click();
    return 'clicked';
}"""


def _parse_listing_text(text: str) -> Optional[Listing]:
    t = " ".join(text.split())
    if "ticket" not in t:
        return None
    m = re.search(r'Section\s+([A-Za-z0-9 ]+?)(?:\s+Row|\s+Seat|\s+\d+\s+ticket|\s+Eye|\s+No image|$)', t)
    section = m.group(1).strip() if m else ""
    m = re.search(r'\bRow\s+([A-Za-z0-9]+)', t)
    row = m.group(1) if m else ""
    m = re.search(r'\bSeats?\s+([A-Za-z0-9]+(?:\s*-\s*[A-Za-z0-9]+)?)', t)
    seat = re.sub(r"\s*", "", m.group(1)) if m else ""
    m = re.search(r'(\d+)\s+tickets?', t)
    qty = int(m.group(1)) if m else 0
    m = (re.search(r'\$([\d,]+)\s*incl\. fees', t)
         or re.search(r'Now\s+\$([\d,]+)', t)
         or re.search(r'\$([\d,]+)', t))
    if not section or not m:
        return None
    price = float(m.group(1).replace(",", ""))
    badges = " ".join(b for b in ("Best price", "Best deal", "Fan favorite", "Last ticket",
                                  "Only 1 left", "Only 2 left") if b in t)
    return Listing(section=section, row=row, seat=seat, quantity=qty, price=price,
                   badges=badges, text=t[:160])


def _scrape_listings(cfg: ListingConfig):
    """Load every listing for the event and parse them. Returns (listings, meta).

    meta carries the 'Showing N of M' counter and the page title so callers can
    detect a block (empty page) vs a genuinely empty event.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Playwright not installed; run `playwright install chromium`") from exc

    from bs4 import BeautifulSoup

    meta = {"showing": "", "title": "", "clicks": 0}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=False, args=_STEALTH_ARGS)
        try:
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
            ctx.add_init_script(_WEBDRIVER_PATCH)
            page = ctx.new_page()
            page.goto(cfg.event_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)
            page.mouse.move(500, 400)
            page.wait_for_timeout(400)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            meta["title"] = page.title()

            def showing() -> str:
                try:
                    return page.locator(":text('Showing')").first.inner_text(timeout=2000)
                except Exception:  # noqa: BLE001
                    return ""

            prev = ""
            stagnant = 0
            for _ in range(60):
                clicked = page.evaluate(_SHOW_MORE_JS)
                if clicked == "none":
                    break
                meta["clicks"] += 1
                page.wait_for_timeout(1300)
                s = showing()
                mn = re.search(r'Showing\s+(\d+)\s+of\s+(\d+)', s or "")
                if mn and mn.group(1) == mn.group(2):
                    break
                if s and s == prev:
                    stagnant += 1
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(800)
                    if stagnant >= 3:
                        break
                else:
                    stagnant = 0
                prev = s
            meta["showing"] = showing()
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "lxml")
    cont = soup.find(id="listings-container")
    listings: List[Listing] = []
    if cont is not None:
        for k in cont.find_all(recursive=False):
            parsed = _parse_listing_text(k.get_text(" ", strip=True))
            if parsed:
                listings.append(parsed)
    LOGGER.info("%s: %s | clicks=%d parsed=%d listings",
                cfg.key, meta["showing"] or "(no counter)", meta["clicks"], len(listings))
    return listings, meta


# ---------- comparison ----------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _row_num(row: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)\s*", str(row))
    return int(m.group(1)) if m else None


def is_own_listing(cfg: ListingConfig, l: Listing) -> bool:
    if cfg.our_listing_id and l.listing_id and l.listing_id == cfg.our_listing_id:
        return True
    if _norm(l.section) != _norm(cfg.section):
        return False
    if cfg.rows and _norm(l.row) not in {_norm(r) for r in cfg.rows}:
        return False
    if cfg.our_seat:
        return _norm(l.seat) == _norm(cfg.our_seat)
    # No seat given: treat a row match in our section as ours (we're usually the
    # only seller in our row). row must be present to avoid excluding GA rows.
    return bool(cfg.rows) and bool(l.row)


def _my_row_num(cfg: ListingConfig) -> Optional[int]:
    """Our best (lowest) row number. Convention: lower row number = better seat."""
    nums = [n for n in (_row_num(r) for r in cfg.rows) if n is not None]
    return min(nums) if nums else None


def filter_comparables(cfg: ListingConfig, listings: List[Listing]) -> List[Listing]:
    """All listings in our section that could serve the same buyer (qty-compatible),
    excluding our own. Row-quality partitioning happens in the recommender."""
    want = _norm(cfg.section)
    out: List[Listing] = []
    for l in listings:
        if is_own_listing(cfg, l):
            continue
        if _norm(l.section) != want:
            continue
        if l.quantity and l.quantity < cfg.quantity:
            continue
        out.append(l)
    return out


def _floor_to_dollar(x: float) -> int:
    return int(math.floor(x))


# ---------- pricing (pure; unit-agnostic; tested via --selftest) ----------

@dataclass
class Recommendation:
    key: str
    status: str
    current_price: Optional[float]      # all-in
    competitor_min: Optional[float]     # all-in
    recommended_price: Optional[int]    # all-in
    floor: int                          # all-in floor
    payout: Optional[float]             # seller net for the recommendation
    num_comps: int
    list_to_type: Optional[int] = None  # seller list price to enter
    cheapest_row: str = ""
    detail: str = ""
    ladder: List[Listing] = field(default_factory=list)

    @property
    def is_change(self) -> bool:
        if self.recommended_price is None or self.current_price is None:
            return self.recommended_price is not None
        return self.recommended_price != round(self.current_price)


def recommend_price(
    cfg: ListingConfig,
    section_comps: List[Listing],
    current_price: Optional[float],
    *,
    floor: Optional[int] = None,
    payout_fn: Optional[Callable[[int], float]] = None,
) -> Recommendation:
    """Row-quality-aware decision (all values in all-in space in production).

    Partition the section's listings by seat quality vs ours (lower row number =
    better seat):
      * same-or-better rows -> the seats we actually compete with; undercut the
        cheapest of these by ~undercut_pct.
      * worse rows -> we NEVER price below the cheapest of these (a better seat
        should not be cheaper than a worse one — protects your value).
    If there's no same-or-better comp, we don't chase worse rows down; instead we
    price a small premium above the cheapest worse seat. The cost floor and the
    single-run drop cap still apply.
    """
    floor = cfg.floor if floor is None else floor
    pf = payout_fn or (lambda p: round(p * (1.0 - cfg.fee_rate), 2))

    def pay(p: Optional[int]) -> Optional[float]:
        return round(pf(p), 2) if p is not None else None

    n = len(section_comps)
    my_row = _my_row_num(cfg)
    better_equal: List[Listing] = []
    worse: List[Listing] = []
    for l in section_comps:
        rn = _row_num(l.row)
        # Unknown rows are treated as comparable (same-or-better) so we stay
        # competitive rather than ignoring them.
        if my_row is None or rn is None or rn <= my_row:
            better_equal.append(l)
        else:
            worse.append(l)
    value_floor = min((l.price for l in worse), default=None)  # never go below cheapest worse seat

    if not better_equal and not worse:
        rec = round(current_price) if current_price is not None else None
        return Recommendation(cfg.key, ST_NO_SELLER, current_price, None, rec, floor,
                              pay(rec), 0,
                              detail="You're the only listing in this section — likely room to raise.")

    if better_equal:
        cheapest = min(better_equal, key=lambda x: x.price)
        anchor = cheapest.price
        anchor_row = cheapest.row or "?"
        target = _floor_to_dollar(anchor * (1.0 - cfg.undercut_pct))
        if target >= anchor:
            target = int(anchor) - 1
        basis = f"undercut cheapest same-or-better seat ${anchor:,.0f} (row {anchor_row})"
    else:
        # We're the best seat; only worse rows exist. Don't chase them down —
        # price a premium above the cheapest worse seat.
        anchor = value_floor
        anchor_row = "worse rows"
        target = _floor_to_dollar(value_floor * (1.0 + cfg.value_premium_pct))
        basis = (f"no same-or-better seat; price {cfg.value_premium_pct:.0%} above the cheapest "
                 f"worse-row seat ${value_floor:,.0f} to protect your better seat")

    # Value protection: never below the cheapest worse-row seat.
    if value_floor is not None and target < value_floor:
        target = int(value_floor)
        basis += f"; lifted to ${int(value_floor):,} so you're not below a worse row"

    if target < floor:
        return Recommendation(cfg.key, ST_HOLD_FLOOR, current_price, anchor, floor, floor,
                              pay(floor), n, cheapest_row=anchor_row,
                              detail=f"{basis}; but that's below your cost floor ${floor:,} — holding at floor.")

    if current_price is not None and target < current_price * (1.0 - cfg.max_drop_pct):
        capped = int(max(floor, value_floor or 0, _floor_to_dollar(current_price * (1.0 - cfg.max_drop_pct))))
        return Recommendation(cfg.key, ST_CAPPED, current_price, anchor, capped, floor,
                              pay(capped), n, cheapest_row=anchor_row,
                              detail=f"{basis}; capped to ${capped:,} this run (>{cfg.max_drop_pct:.0%} drop limit).")

    status = ST_PROMOTE
    detail = f"{basis} -> ${target:,}."
    if current_price is not None and target > current_price:
        status = ST_RAISE
        detail = f"Raise: {basis} -> ${target:,}."
    return Recommendation(cfg.key, status, current_price, anchor, int(target), floor,
                          pay(int(target)), n, cheapest_row=anchor_row, detail=detail)


# ---------- per-listing evaluation ----------

def evaluate_listing(cfg: ListingConfig, state_entry: dict) -> tuple[Recommendation, Optional[str]]:
    now = datetime.now(timezone.utc)
    if cfg.event_datetime and cfg.event_datetime < now:
        return Recommendation(cfg.key, ST_PAST, None, None, None, cfg.floor_allin, None, 0,
                              detail="Event already happened; skipping."), None

    try:
        listings, meta = _scrape_listings(cfg)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("%s: scrape failed", cfg.key)
        return Recommendation(cfg.key, ST_BLOCKED, None, None, None, cfg.floor_allin, None, 0,
                              detail=f"Scrape failed: {type(exc).__name__}: {exc}"), \
               f"{type(exc).__name__}: {exc}"

    had_data = bool(state_entry.get("last_listing_count"))
    if not listings:
        if had_data:
            return Recommendation(cfg.key, ST_BLOCKED, None, None, None, cfg.floor_allin, None, 0,
                                  detail="Parsed 0 listings but had data before — likely a block."), \
                   "empty scrape with prior state (likely anti-bot block)"
        return Recommendation(cfg.key, ST_BLOCKED, None, None, None, cfg.floor_allin, None, 0,
                              detail="Parsed 0 listings (page empty or blocked)."), \
               "empty scrape (page empty or blocked)"

    own = next((l for l in listings if is_own_listing(cfg, l)), None)
    current = float(own.price) if own else state_entry.get("current_price")
    current_f = float(current) if current is not None else None

    section_comps = filter_comparables(cfg, listings)  # section, qty-ok, excludes ours
    rec = recommend_price(cfg, section_comps, current_f,
                          floor=cfg.floor_allin, payout_fn=lambda a: payout_from_allin(a, cfg))
    rec.num_comps = len(section_comps)
    rec.ladder = sorted(section_comps, key=lambda x: x.price)[:8]
    if rec.recommended_price is not None:
        rec.list_to_type = int(round(allin_to_list(rec.recommended_price, cfg)))
    return rec, None


# ---------- core run ----------

@dataclass
class RunResult:
    success: bool
    error: Optional[str] = None
    info: str = ""
    alerts: List[tuple[str, str, str]] = field(default_factory=list)


def _run_once() -> RunResult:
    try:
        config = load_config()
    except ConfigError as exc:
        return RunResult(success=False, error=str(exc))

    state = _load_prices()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recs: List[Recommendation] = []
    errors: List[str] = []
    changed: List[Recommendation] = []

    for key, cfg in config.items():
        entry = state.get(key, {})
        rec, err = evaluate_listing(cfg, entry)
        recs.append(rec)
        if err:
            errors.append(f"{key}: {err}")
        if rec.status in (ST_PAST, ST_BLOCKED):
            continue

        entry.update({
            "label": cfg.label,
            "section": cfg.section,
            "updated_at": now_iso,
            "last_status": rec.status,
            "last_competitor_min": rec.competitor_min,
            "last_listing_count": rec.num_comps,
        })
        if rec.recommended_price is not None:
            entry["recommended_price"] = rec.recommended_price
            entry["recommended_list"] = rec.list_to_type
        if rec.current_price is not None:
            entry["current_price"] = rec.current_price

        if _is_emailworthy(cfg, rec, entry):
            changed.append(rec)
            entry["last_emailed_price"] = rec.recommended_price
            entry["last_emailed_at"] = now_iso
            entry["last_emailed_status"] = rec.status

        hist = entry.setdefault("history", [])
        hist.append({"t": now_iso, "comp_min": rec.competitor_min,
                     "rec": rec.recommended_price, "status": rec.status})
        del hist[:-30]
        state[key] = entry

    _save_prices(state)
    info = (f"listings={len(recs)} changed={len(changed)} errors={len(errors)} "
            + " ".join(f"{r.key}:{r.status}" for r in recs))

    alerts: List[tuple[str, str, str]] = []
    if changed:
        alerts.append(_msg_recommendations(config, changed))
    if errors and len(errors) == len(recs):
        return RunResult(success=False, error="; ".join(errors), info=info, alerts=alerts)
    return RunResult(success=True, info=info, alerts=alerts)


def _is_emailworthy(cfg: ListingConfig, rec: Recommendation, entry: dict) -> bool:
    notable = rec.status in (ST_HOLD_FLOOR, ST_CAPPED, ST_NO_SELLER)
    if rec.recommended_price is None:
        return False
    last = entry.get("last_emailed_price")
    last_status = entry.get("last_emailed_status")
    if last is None:
        return rec.is_change or notable
    delta = abs(rec.recommended_price - float(last))
    material = delta >= cfg.min_change_abs or (last and delta / float(last) >= cfg.min_change_pct)
    return bool(material or last_status != rec.status)


# ---------- message templates ----------

_STATUS_VERB = {
    ST_PROMOTE: "LOWER", ST_RAISE: "RAISE", ST_HOLD_FLOOR: "HOLD AT FLOOR",
    ST_CAPPED: "LOWER (capped)", ST_NO_COMP: "HOLD", ST_NO_SELLER: "RAISE?",
}


def _apply_cmd(key: str) -> str:
    return f".venv/bin/python stubhub_repricer.py --apply {key}"


def _approve_url(key: str) -> Optional[str]:
    """One-click approve link, served by the local approver daemon. Present only
    when STUBHUB_APPROVE_PORT + STUBHUB_APPROVE_TOKEN are configured (see
    scripts/install_stubhub_approver.sh)."""
    port = os.environ.get("STUBHUB_APPROVE_PORT", "").strip()
    token = os.environ.get("STUBHUB_APPROVE_TOKEN", "").strip()
    if port and token:
        return f"http://127.0.0.1:{port}/approve?key={key}&token={token}"
    return None


def _ladder_text(rec: Recommendation, cfg: ListingConfig) -> str:
    if not rec.ladder:
        return "    (no other listings in this section)"
    lines = []
    for l in rec.ladder:
        seat = f" seat {l.seat}" if l.seat else ""
        badge = f"  [{l.badges}]" if l.badges else ""
        lines.append(f"    row {l.row or '?':<4}{seat:<10} qty {l.quantity}  ${l.price:,.0f}{badge}")
    return "\n".join(lines)


def _msg_recommendations(config: dict[str, ListingConfig], recs: List[Recommendation]) -> tuple[str, str, str]:
    verbs = {_STATUS_VERB.get(r.status, r.status) for r in recs}
    subject = f"{SUBJECT_PREFIX} {len(recs)} update(s): {', '.join(sorted(verbs))}"

    text: List[str] = ["Recommended StubHub price changes (prices are all-in / 'incl. fees'):", ""]
    html_rows: List[str] = []
    for r in recs:
        cfg = config[r.key]
        verb = _STATUS_VERB.get(r.status, r.status)
        cur = f"${r.current_price:,.0f}" if r.current_price is not None else "—"
        comp = f"${r.competitor_min:,.0f} (row {r.cheapest_row})" if r.competitor_min is not None else "—"
        new = f"${r.recommended_price:,}" if r.recommended_price is not None else "—"
        lst = f"${r.list_to_type:,}" if r.list_to_type is not None else "—"
        pay = f"${r.payout:,.0f}" if r.payout is not None else "—"
        url = _approve_url(r.key)
        text += [
            f"### {cfg.label}  [{verb}]",
            f"  Your seat: section {cfg.section}, row {'/'.join(cfg.rows) or '?'}"
            + (f", seat {cfg.our_seat}" if cfg.our_seat else "") + f"  (qty {cfg.quantity})",
            f"  current all-in {cur}   cheapest same-or-better seat {comp}   ({r.num_comps} in-section comps)",
            f"  -> recommend all-in {new}  =  list price to type {lst}   projected payout {pay}  (cost ${cfg.unit_cost:,.0f})",
            f"  {r.detail}",
            "  section price ladder (cheapest first):",
            _ladder_text(r, cfg),
            (f"  APPROVE (click to apply): {url}" if url
             else f"  approve: {_apply_cmd(r.key)}   (add --dry-run to preview)"),
            "",
        ]
        if url:
            approve_html = (
                f"<a href='{_esc(url)}' style='display:inline-block;background:#1a7f37;color:#fff;"
                f"text-decoration:none;padding:7px 14px;border-radius:6px;font-weight:600'>"
                f"✅ Approve &amp; apply</a>")
        else:
            approve_html = f"<code>{_esc(_apply_cmd(r.key))}</code>"
        html_rows.append(
            "<tr>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'><b>{_esc(cfg.label)}</b><br>"
            f"<span style='color:#666;font-size:12px'>Sec {_esc(cfg.section)}, row "
            f"{_esc('/'.join(cfg.rows) or '?')}{_esc(', seat ' + cfg.our_seat if cfg.our_seat else '')}, qty {cfg.quantity}</span></td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd'>{_esc(verb)}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right'>{cur}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right'>{_esc(comp)}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right'><b>{new}</b><br>"
            f"<span style='font-size:11px;color:#666'>list {lst}</span></td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right'>{pay}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;font-size:12px;color:#444'>{_esc(r.detail)}<br>"
            f"{approve_html}</td>"
            "</tr>"
        )

    text += ["The apply step re-checks the live market and StubHub's own payout before setting any price."]
    text_body = "\n".join(text)
    html_body = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111;max-width:1000px\">"
        "<p><b>Recommended StubHub price changes</b> — prices shown are all-in (incl. fees), "
        "the unit buyers sort on.</p>"
        "<table style='border-collapse:collapse;font-size:13px'><thead><tr>"
        + "".join(f"<th style='padding:6px 10px;border:1px solid #ddd;background:#f4f4f4'>{h}</th>"
                  for h in ("Listing", "Action", "Current", "Cheapest comp", "Recommend", "Payout", "Why / approve"))
        + "</tr></thead><tbody>" + "".join(html_rows) + "</tbody></table>"
        "<p style='font-size:12px;color:#555'>Run the approve command for each change you accept. "
        "The apply step re-checks the live market and StubHub's own displayed payout before setting "
        "any price (it refuses to net below your cost).</p></body></html>"
    )
    return subject, text_body, html_body


def _msg_monitor_broken(error_summary: str) -> tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} ⚠️ Repricer broken"
    text = (f"The StubHub repricer failed {FAILURE_LIMIT} consecutive runs.\n"
            f"Last error: {error_summary[:600]}\n\nCheck ~/Library/Logs/stubhub-repricer.log "
            "and re-run with --probe <key>.")
    html = (f"<p>The StubHub repricer failed <b>{FAILURE_LIMIT}</b> consecutive runs.</p>"
            f"<pre style='background:#f4f4f4;padding:8px;font-size:12px'>{_esc(error_summary[:600])}</pre>"
            "<p>Check <code>~/Library/Logs/stubhub-repricer.log</code> and re-run with "
            "<code>--probe &lt;key&gt;</code>.</p>")
    return subject, text, html


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------- channels / dispatch ----------

@dataclass
class Channel:
    name: str
    send_alert: Callable[[str, str, str], None]
    send_test: Callable[[], None]


def _build_channels() -> List[Channel]:
    channels: List[Channel] = []
    try:
        em_cfg = emailer.EmailConfig.from_env()

        def _send(subject: str, text: str, html: str) -> None:
            emailer.send(subject, text, html, config=em_cfg)

        channels.append(Channel(
            name="email", send_alert=_send,
            send_test=lambda: emailer.send(
                f"{SUBJECT_PREFIX} ✅ Test", "StubHub repricer email pipeline OK.",
                "<p>StubHub repricer email pipeline OK.</p>", config=em_cfg)))
    except emailer.EmailConfigError as exc:
        LOGGER.warning("email channel disabled: %s", exc)
    return channels


def _dispatch(alert: tuple[str, str, str], channels: List[Channel]) -> None:
    subject, text, html = alert
    if not channels:
        LOGGER.info("[DRY RUN] would send: %s\n%s", subject, text)
        return
    for ch in channels:
        try:
            ch.send_alert(subject, text, html)
            LOGGER.info("[%s] alert dispatched: %s", ch.name, subject)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] send failed: %s", ch.name, exc)


# ---------- state ----------

def _load_prices() -> dict[str, dict]:
    if not PRICES_STATE_FILE.exists():
        return {}
    try:
        with PRICES_STATE_FILE.open() as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        LOGGER.warning("%s corrupt; resetting.", PRICES_STATE_FILE.name)
        return {}


def _save_prices(state: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PRICES_STATE_FILE.open("w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _load_failures() -> int:
    if not FAILURE_STATE_FILE.exists():
        return 0
    try:
        with FAILURE_STATE_FILE.open() as fh:
            data = json.load(fh)
        return int(data.get(NAME, 0)) if isinstance(data, dict) else 0
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _save_failures(count: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with FAILURE_STATE_FILE.open("w") as fh:
        json.dump({NAME: count}, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


# ---------- apply path (authenticated) ----------

SEL_LOGIN_URL = os.environ.get(
    "STUBHUB_LOGIN_URL", "https://www.stubhub.com/secure/login")
SEL_LISTINGS_URL = os.environ.get(
    "STUBHUB_LISTINGS_URL", "https://my.stubhub.com/listings")


def _is_logged_out(page) -> bool:
    """Logged-out sessions get redirected to a *login* URL when we hit the
    authenticated listings page. That redirect is the reliable signal."""
    return "login" in page.url.lower()


def _launch_persistent(pw, *, headless: bool):
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    lock = CHROME_PROFILE_DIR / "SingletonLock"
    if lock.exists():
        raise RuntimeError(
            f"Chrome profile is locked ({lock}). Close any Chrome using "
            f"{CHROME_PROFILE_DIR} and retry.")
    args = ["--disable-blink-features=AutomationControlled"]
    if headless:
        args += ["--window-position=-2400,-2400", "--window-size=1400,1000"]
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(CHROME_PROFILE_DIR), channel="chrome", headless=False,
        args=args, viewport={"width": 1400, "height": 1000})
    ctx.add_init_script(_WEBDRIVER_PATCH)
    return ctx


def _cmd_login() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        LOGGER.error("Playwright not installed.")
        return 1
    print("Opening StubHub in the repricer's Chrome profile.")
    print("Log in (and clear any challenge), then return here and press Enter.")
    with sync_playwright() as pw:
        ctx = _launch_persistent(pw, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SEL_LOGIN_URL, wait_until="domcontentloaded")
            input("Press Enter once you're logged in... ")
            page.goto(SEL_LISTINGS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if _is_logged_out(page):
                print(f"⚠ Login not detected (still on {page.url}). Try --login again.")
            else:
                print(f"✓ Logged in — your listings page loaded ({page.url}).")
                print("  Tip: open each listing and copy the id from its URL into "
                      "stubhub_listings.json as 'our_listing_id' for the most precise self-exclusion.")
        finally:
            ctx.close()
    return 0


def _cmd_apply(key: str, dry_run: bool) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        LOGGER.error("%s", exc)
        return 1
    if key not in config:
        LOGGER.error("unknown listing key %r (have: %s)", key, ", ".join(config))
        return 1
    cfg = config[key]
    if cfg.event_datetime and cfg.event_datetime < datetime.now(timezone.utc):
        LOGGER.error("%s: event already happened; nothing to do.", key)
        return 1

    lock = _acquire_lock()
    if lock is None:
        LOGGER.error("another repricer run/apply is in progress; try again shortly.")
        return 1
    try:
        return _apply_locked(cfg, key, dry_run)
    finally:
        lock.close()


def _apply_locked(cfg: ListingConfig, key: str, dry_run: bool) -> int:
    state = _load_prices()
    entry = state.get(key, {})
    rec, err = evaluate_listing(cfg, entry)
    if err or rec.status == ST_BLOCKED:
        LOGGER.error("%s: aborting apply — live data unavailable (%s).", key, err or rec.detail)
        return 1
    if rec.recommended_price is None or rec.list_to_type is None:
        LOGGER.error("%s: no actionable price (%s).", key, rec.detail)
        return 1
    if rec.list_to_type < cfg.floor_list:
        LOGGER.error("%s: list-to-type $%s below floor $%s; refusing.",
                     key, f"{rec.list_to_type:,}", f"{cfg.floor_list:,}")
        return 1

    target_list = rec.list_to_type
    LOGGER.info("%s: re-validated -> %s all-in $%s (list $%s, payout ~$%s). %s",
                key, rec.status, f"{rec.recommended_price:,}", f"{target_list:,}",
                f"{rec.payout:,.0f}" if rec.payout else "?", rec.detail)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        LOGGER.error("Playwright not installed.")
        return 1

    with sync_playwright() as pw:
        ctx = _launch_persistent(pw, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SEL_LISTINGS_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            if _is_logged_out(page):
                LOGGER.error("%s: not logged in (redirected to %s). Run: "
                             "python stubhub_repricer.py --login", key, page.url)
                return 1
            applied = _set_listing_price(page, cfg, target_list, dry_run=dry_run)
            if applied is None:
                LOGGER.error("%s: could not complete the edit-price flow (selector drift?). "
                             "No change made.", key)
                return 1
            _, card_payout = applied
            total_cost = cfg.unit_cost * cfg.quantity
            # Deterministic floor (using your calibrated fee) — the real gate, already
            # enforced above via list-to-type >= floor. This is the payout you'll net.
            est_payout = round(target_list * (1.0 - cfg.fee_rate) * cfg.quantity)

            if dry_run:
                LOGGER.info("%s: DRY RUN — would set list $%s/ticket -> you net ~$%s (cost $%s). "
                            "Card currently shows $%s. No change made.",
                            key, f"{target_list:,}", f"{est_payout:,}", f"{total_cost:,.0f}",
                            f"{card_payout:,.0f}" if card_payout else "?")
                return 0

            # Real apply done. card_payout now reflects the NEW price — verify it.
            if card_payout is not None and card_payout < total_cost:
                LOGGER.warning("%s: ⚠ StubHub payout now $%s < total cost $%s — real fee higher "
                               "than expected. Consider reverting in the app.",
                               key, f"{card_payout:,.0f}", f"{total_cost:,.0f}")
            entry["current_list"] = target_list
            entry["last_applied_list"] = target_list
            entry["last_applied_payout"] = card_payout
            entry["last_applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state[key] = entry
            _save_prices(state)
            LOGGER.info("%s: ✓ list set to $%s/ticket (StubHub payout now $%s, cost $%s).",
                        key, f"{target_list:,}",
                        f"{card_payout:,.0f}" if card_payout else "?", f"{total_cost:,.0f}")
            return 0
        finally:
            ctx.close()


def _set_listing_price(page, cfg: ListingConfig, target_list: int, *, dry_run: bool):
    """Drive the real StubHub 'Adjust price' inline editor for our listing.

    Flow (discovered on the live my.stubhub.com/listings dashboard): each listing
    card has an 'Adjust price' button that opens an inline editor with a single
    number input (the seller list price), a live 'You'll get' payout, and
    Save/Cancel. We scope to OUR listing by our_listing_id, fill the number,
    read the payout, then Save (or Cancel on --dry-run). Only the price field is
    ever touched. Returns (verified_list, payout_total) or None.
    """
    # 1. Find OUR listing's card + its "Adjust price" button (scope by listingId
    #    so we never touch or read another listing's data).
    adjust = None
    card = None
    if cfg.our_listing_id:
        link = page.locator(f"a[href*='listingId={cfg.our_listing_id}']").first
        if link.count() > 0:
            try:
                link.scroll_into_view_if_needed(timeout=4000)
            except Exception:  # noqa: BLE001
                pass
            # Nearest ancestor div that holds this listing's Adjust button = its card.
            card = link.locator(
                'xpath=ancestor::div[.//button[contains(normalize-space(.),"Adjust price")]][1]')
            if card.count() > 0:
                cand = card.locator("button:has-text('Adjust price')").first
                if cand.count() > 0:
                    adjust = cand
    if adjust is None:
        allbtns = page.locator("button:has-text('Adjust price')")
        if allbtns.count() == 1:  # unambiguous fallback only
            adjust = allbtns.first
    if adjust is None:
        LOGGER.error("apply: couldn't find the 'Adjust price' button for listing %s "
                     "(section %s).", cfg.our_listing_id, cfg.section)
        return None
    try:
        adjust.scroll_into_view_if_needed(timeout=4000)
        adjust.click(timeout=10_000)
        page.wait_for_timeout(2500)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("apply: couldn't open the price editor: %s", exc)
        return None

    # 2. The price field is the editor's number input.
    field = page.locator("input[type='number']").first
    if field.count() == 0:
        LOGGER.error("apply: price field not found after opening editor.")
        return None
    try:
        before = _money(field.input_value(timeout=4000))
        LOGGER.info("apply: editor open; current list price reads $%s", _fmt(before))
    except Exception:  # noqa: BLE001
        pass
    try:
        field.fill(str(int(target_list)), timeout=8000)
        page.wait_for_timeout(1500)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("apply: couldn't fill price: %s", exc)
        return None

    # 3. Dry-run: read the card's current payout for context, then Cancel.
    #    (The 'You'll get' figure is the CURRENT payout — it only reflects the new
    #    price after Save — so it's context here, not the floor gate.)
    if dry_run:
        payout = _read_payout_from_card(card)
        try:
            page.locator("button:has-text('Cancel')").first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        return (None, payout)

    # 4. Save, then re-read the card's now-updated 'You'll get' payout.
    save = page.locator("button:has-text('Save')").first
    if save.count() == 0:
        LOGGER.error("apply: Save button not found; not setting price.")
        return None
    try:
        save.click(timeout=10_000)
        page.wait_for_timeout(3500)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("apply: Save click failed: %s", exc)
        return None
    return (int(target_list), _read_payout_from_card(card))


def _read_payout_from_card(card) -> Optional[float]:
    """Read 'You'll get $X if your tickets sell' from OUR listing's card element
    (total across all tickets). `card` is already scoped to our listing, so this
    can never read another listing's payout. Returns None if not found."""
    if card is None:
        return None
    try:
        if card.count() == 0:
            return None
        txt = card.inner_text(timeout=4000)
        m = re.search(r"you'?ll get\s*\$?([\d,]+(?:\.\d+)?)", txt, re.I)
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception:  # noqa: BLE001
        pass
    return None


def _money(s: Any) -> Optional[float]:
    m = re.search(r'([\d,]+\.?\d*)', str(s or "").replace(" ", ""))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


# ---------- CLI commands ----------

def _cmd_run() -> int:
    lock = _acquire_lock()
    if lock is None:
        LOGGER.warning("another repricer run/apply is in progress; skipping this firing.")
        return 0
    try:
        channels = _build_channels()
        if not channels:
            LOGGER.warning("DRY RUN: no email channel configured. Alerts logged only.")
        result = _run_once()
        failures = _load_failures()
        if result.success:
            if failures:
                LOGGER.info("recovered after %s failures", failures)
            failures = 0
        else:
            failures += 1
            LOGGER.warning("FAILED (%s consecutive). Error: %s", failures, result.error)
        if result.info:
            LOGGER.info(result.info)
        for alert in result.alerts:
            _dispatch(alert, channels)
        if failures >= FAILURE_LIMIT:
            _dispatch(_msg_monitor_broken(result.error or "unknown"), channels)
            LOGGER.warning("sent 'repricer broken' alert; resetting counter")
            failures = 0
        _save_failures(failures)
        return 0
    finally:
        lock.close()


def _cmd_test() -> int:
    channels = _build_channels()
    if not channels:
        LOGGER.error("No email channel. Set GMAIL_USER + GMAIL_APP_PASSWORD (+ EMAIL_TO).")
        return 1
    rc = 0
    for ch in channels:
        try:
            ch.send_test()
            LOGGER.info("[%s] test sent", ch.name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] test FAILED: %s", ch.name, exc)
            rc = 1
    return rc


def _cmd_list_config() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1
    for key, cfg in config.items():
        when = cfg.event_datetime.isoformat() if cfg.event_datetime else "?"
        print(f"[{key}] {cfg.label}")
        print(f"   event : {when}   {cfg.event_url}")
        print(f"   seat  : sec {cfg.section}, rows {list(cfg.rows)}"
              + (f", seat {cfg.our_seat}" if cfg.our_seat else "") + f", qty {cfg.quantity}")
        print(f"   cost  : ${cfg.unit_cost:,.0f}  sellerfee {cfg.fee_rate:.0%}  buyerfee {cfg.buyer_fee:.0%}")
        print(f"   floor : list ${cfg.floor_list:,}  = all-in ${cfg.floor_allin:,}")
        print(f"   rule  : quality-aware (lower row = better)  undercut {cfg.undercut_pct:.1%}  "
              f"premium {cfg.value_premium_pct:.0%}  max_drop {cfg.max_drop_pct:.0%}  "
              f"listingId {cfg.our_listing_id or '(none)'}")
        print()
    return 0


def _cmd_list_state() -> int:
    state = _load_prices()
    if not state:
        print("No state yet.")
    for key, e in state.items():
        print(f"[{key}] {e.get('label','')}")
        print(f"   current_allin=${_fmt(e.get('current_price'))} comp_min=${_fmt(e.get('last_competitor_min'))}"
              f" rec_allin=${_fmt(e.get('recommended_price'))} rec_list=${_fmt(e.get('recommended_list'))}"
              f" status={e.get('last_status')}")
        print(f"   last_emailed=${_fmt(e.get('last_emailed_price'))} @ {e.get('last_emailed_at','-')}"
              f"  applied_list=${_fmt(e.get('last_applied_list'))} @ {e.get('last_applied_at','-')}")
        print()
    print(f"Consecutive failures: {_load_failures()}")
    return 0


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):,.0f}" if v is not None else "-"
    except (TypeError, ValueError):
        return str(v)


def _print_recommendation(cfg: ListingConfig, rec: Recommendation) -> None:
    print(f"[{cfg.key}] {cfg.label}  status={rec.status}  ({rec.num_comps} in-section comps, lower row=better)")
    print(f"   your seat : sec {cfg.section} row {'/'.join(cfg.rows) or '?'}"
          + (f" seat {cfg.our_seat}" if cfg.our_seat else "")
          + f"   current all-in ${_fmt(rec.current_price)}")
    print(f"   cheapest comparable: ${_fmt(rec.competitor_min)}"
          + (f" (row {rec.cheapest_row})" if rec.cheapest_row else ""))
    print(f"   RECOMMEND all-in ${_fmt(rec.recommended_price)}  -> list price to type ${_fmt(rec.list_to_type)}"
          f"   payout ${_fmt(rec.payout)}  (cost ${cfg.unit_cost:,.0f}, floor list ${cfg.floor_list:,})")
    print(f"   {rec.detail}")
    if rec.ladder:
        print("   section ladder (cheapest first):")
        for l in rec.ladder:
            seat = f" seat {l.seat}" if l.seat else ""
            badge = f"  [{l.badges}]" if l.badges else ""
            print(f"      row {l.row or '?':<4}{seat:<10} qty {l.quantity}  ${l.price:,.0f}{badge}")


def _cmd_check(key: str) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1
    if key not in config:
        print(f"unknown key {key!r}; have: {', '.join(config)}")
        return 1
    cfg = config[key]
    rec, err = evaluate_listing(cfg, _load_prices().get(key, {}))
    _print_recommendation(cfg, rec)
    if err:
        print(f"   ERROR: {err}")
        return 1
    return 0


def _cmd_probe(key: str) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1
    if key not in config:
        print(f"unknown key {key!r}; have: {', '.join(config)}")
        return 1
    cfg = config[key]
    listings, meta = _scrape_listings(cfg)
    print(f"counter: {meta.get('showing')!r}  title: {meta.get('title')!r}")
    print(f"parsed {len(listings)} listings")
    insec = sorted((l for l in listings if _norm(l.section) == _norm(cfg.section)), key=lambda x: x.price)
    print(f"\n=== section {cfg.section} ({len(insec)} listings) ===")
    for l in insec:
        mine = " <-- looks like YOURS" if is_own_listing(cfg, l) else ""
        seat = f" seat {l.seat}" if l.seat else ""
        print(f"  row {l.row or '?':<4}{seat:<10} qty {l.quantity}  ${l.price:,.0f}  [{l.badges}]{mine}")
    out = STATE_DIR / f"probe_{key}.json"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        json.dump([l.__dict__ for l in listings], fh, indent=2, default=str)
    print(f"\nall {len(listings)} parsed listings dumped to {out}")
    return 0


def _cmd_selftest() -> int:
    def mk(cost, fee=0.15, undercut=0.01, max_drop=0.15, qty=1, rows=("10",),
           premium=0.10, buyer=0.25):
        return ListingConfig(
            key="t", label="t", event_url="x", event_datetime=None, section="A", rows=rows,
            our_seat=None, row_tolerance=0, compare_mode="section", quantity=qty,
            sell_together=True, our_listing_id=None, unit_cost=cost, fee_rate=fee,
            buyer_fee_pct=buyer, undercut_pct=undercut, max_drop_pct=max_drop,
            value_premium_pct=premium, currency="USD", min_change_abs=5.0, min_change_pct=0.005)

    def L(row, price, seat="", qty=2, section="A"):
        return Listing(section, str(row), seat, qty, float(price))

    # Floors
    assert mk(2361).floor_list == 2778
    assert mk(900).floor_list == 1059
    assert mk(1125).floor_list == 1324
    assert mk(900).floor_allin == math.ceil(1059 * 1.25) == 1324

    # Undercut cheapest SAME-OR-BETTER seat (lower row = better). Worse row ignored.
    c = mk(900, rows=("10",), qty=2)
    r = recommend_price(c, [L(5, 1500), L(12, 1400)], 1490.0)
    assert r.status == ST_PROMOTE and r.recommended_price == 1485, r

    # Value protection: undercut target would dip below a WORSE row -> lifted.
    r = recommend_price(c, [L(5, 1410), L(12, 1400)], 1490.0)
    assert r.recommended_price == 1400, r  # not below the worse row's $1400

    # Best seat, only a cheaper WORSE row -> DON'T chase it down; premium above it.
    c1 = mk(900, rows=("1",), qty=2, premium=0.10)
    r = recommend_price(c1, [L(10, 1000)], 1200.0)
    assert r.status == ST_PROMOTE and r.recommended_price == 1100, r  # 1000*1.10, never below 1000

    # Best seat, currently under the premium target -> RAISE (not below worse row).
    r = recommend_price(c1, [L(10, 1000)], 1000.0)
    assert r.status == ST_RAISE and r.recommended_price == 1100, r

    # tie-break (undercut 0 -> strictly one below)
    r = recommend_price(mk(900, undercut=0.0, rows=("10",)), [L(5, 1500)], 1490.0)
    assert r.recommended_price == 1499, r

    # Cost floor (all-in): premium target below the all-in floor -> hold at floor.
    c2 = mk(2361, rows=("1",))
    r = recommend_price(c2, [L(18, 2000)], 6000.0, floor=c2.floor_allin,
                        payout_fn=lambda a: payout_from_allin(a, c2))
    assert r.status == ST_HOLD_FLOOR and r.recommended_price == c2.floor_allin, r
    assert abs(payout_from_allin(c2.floor_allin, c2) - 2361) <= 3, r

    # max-drop cap on a big drop toward a better seat.
    r = recommend_price(mk(900, max_drop=0.10, rows=("10",)), [L(5, 1100)], 1400.0)
    assert r.status == ST_CAPPED and r.recommended_price == 1260, r

    # only our listing in section -> NO_SELLER, hold.
    r = recommend_price(mk(900, rows=("10",)), [], 1400.0)
    assert r.status == ST_NO_SELLER and r.recommended_price == 1400, r

    # filter_comparables: section + qty + self-exclusion by row+seat.
    cfg = ListingConfig(**{**mk(900, qty=2, rows=("9",)).__dict__, "our_seat": "7-8"})
    listings = [
        L(9, 4215, "7-8"),               # ours -> excluded
        L(11, 3279),                     # comp
        L(5, 6987),                      # comp
        Listing("B", "9", "", 2, 100.0),  # wrong section
        Listing("A", "3", "", 1, 50.0),   # qty 1 < 2
    ]
    comps = filter_comparables(cfg, listings)
    assert sorted(x.price for x in comps) == [3279.0, 6987.0], comps

    print("✓ selftest passed")
    return 0


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)
    if args.selftest:
        return _cmd_selftest()
    if args.test:
        return _cmd_test()
    if args.list_config:
        return _cmd_list_config()
    if args.list_state:
        return _cmd_list_state()
    if args.probe:
        return _cmd_probe(args.probe)
    if args.check:
        return _cmd_check(args.check)
    if args.login:
        return _cmd_login()
    if args.apply:
        return _cmd_apply(args.apply, dry_run=args.dry_run)
    return _cmd_run()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StubHub dynamic repricer")
    p.add_argument("--test", action="store_true", help="Send a test email and exit")
    p.add_argument("--selftest", action="store_true", help="Pricing unit asserts (no network)")
    p.add_argument("--list-config", action="store_true", help="Print parsed config")
    p.add_argument("--list-state", action="store_true", help="Print stored state")
    p.add_argument("--check", metavar="KEY", help="Scrape one event, print recommendation + ladder")
    p.add_argument("--probe", metavar="KEY", help="Scrape + dump every parsed listing for one event")
    p.add_argument("--login", action="store_true", help="One-time StubHub login in the bot profile")
    p.add_argument("--apply", metavar="KEY", help="Set the approved price (money-safe)")
    p.add_argument("--dry-run", action="store_true", help="With --apply: preview without confirming")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z")


if __name__ == "__main__":
    sys.exit(main())
