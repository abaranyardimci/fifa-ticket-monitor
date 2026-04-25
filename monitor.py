"""FIFA WC2026 ticket-drop monitor — orchestrator.

Runs each target, fans out alerts to every configured notification channel
(Telegram + email), tracks consecutive failures per target, escalates with a
"monitor broken" alert after 3 in a row.

CLI:
    python monitor.py                    # normal run, all targets
    python monitor.py --test             # send a test message via every channel
    python monitor.py --list-targets     # print stored state
    python monitor.py --target news      # run only one target
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List

import emailer
import notifier
from targets import AlertSpec, TargetResult
from targets import news, sales_info, shop  # noqa: F401 — `shop` available for re-enable

LOGGER = logging.getLogger("monitor")

FAILURE_LIMIT = 3
FAILURE_STATE_FILE = Path(__file__).resolve().parent / "state" / "failure_counts.json"

TargetRunner = Callable[[], TargetResult]
TargetDescriber = Callable[[], str]

# Active targets. The `shop` target is intentionally disabled because the FIFA
# shop is behind Akamai and blocks GitHub Actions datacenter IPs — leaving it
# enabled produces a "monitor broken: shop" alert every ~45 min that drowns out
# real signal. Re-enable by uncommenting the shop line below (and consider
# wiring a residential proxy via the PROXY_URL env var first).
TARGETS: Dict[str, tuple[TargetRunner, TargetDescriber]] = {
    news.NAME: (news.run, news.describe_state),
    sales_info.NAME: (sales_info.run, sales_info.describe_state),
    # shop.NAME: (shop.run, shop.describe_state),
}


# ---------- channels ----------

@dataclass
class Channel:
    """A single notification channel — Telegram or email."""
    name: str
    send_alert: Callable[[AlertSpec], None]
    send_test: Callable[[], None]


def _build_channels() -> List[Channel]:
    """Detect which channels are configured and return their handlers.

    Each channel is independent — if Telegram creds are present but Gmail
    is not, only Telegram fires. If neither is configured, returns []
    and the caller drops into DRY RUN mode.
    """
    channels: List[Channel] = []

    try:
        tg_cfg = notifier.TelegramConfig.from_env()
        channels.append(
            Channel(
                name="telegram",
                send_alert=lambda spec: notifier.send(_telegram_text(spec), config=tg_cfg),
                send_test=lambda: notifier.send(notifier.test_message(), config=tg_cfg),
            )
        )
    except notifier.TelegramConfigError as exc:
        LOGGER.warning("telegram channel disabled: %s", exc)

    try:
        em_cfg = emailer.EmailConfig.from_env()
        channels.append(
            Channel(
                name="email",
                send_alert=lambda spec: emailer.send(*_email_payload(spec), config=em_cfg),
                send_test=lambda: emailer.send(*emailer.test_message(), config=em_cfg),
            )
        )
    except emailer.EmailConfigError as exc:
        LOGGER.warning("email channel disabled: %s", exc)

    return channels


def _telegram_text(spec: AlertSpec) -> str:
    """Render an AlertSpec as a Telegram Markdown message."""
    if spec.kind == "new_article":
        return notifier.new_ticket_article(spec.title, spec.url)
    if spec.kind == "sales_info_changed":
        return notifier.sales_info_changed(spec.url)
    if spec.kind == "shop_changed":
        return notifier.shop_changed(spec.url)
    if spec.kind == "monitor_broken":
        return notifier.monitor_broken(spec.target, spec.error)
    raise ValueError(f"Unknown alert kind for telegram: {spec.kind!r}")


def _email_payload(spec: AlertSpec) -> tuple[str, str, str]:
    """Render an AlertSpec as (subject, body_text, body_html) for email."""
    if spec.kind == "new_article":
        return emailer.new_ticket_article(spec.title, spec.url)
    if spec.kind == "sales_info_changed":
        return emailer.sales_info_changed(spec.url)
    if spec.kind == "shop_changed":
        return emailer.shop_changed(spec.url)
    if spec.kind == "monitor_broken":
        return emailer.monitor_broken(spec.target, spec.error)
    raise ValueError(f"Unknown alert kind for email: {spec.kind!r}")


def _dispatch(spec: AlertSpec, channels: List[Channel]) -> None:
    """Send one alert to every configured channel. Independent per-channel error handling."""
    if not channels:
        LOGGER.info("[DRY RUN] would send alert: kind=%s title=%r url=%s",
                    spec.kind, spec.title, spec.url)
        return
    for channel in channels:
        try:
            channel.send_alert(spec)
            LOGGER.info("[%s] alert dispatched (kind=%s)", channel.name, spec.kind)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] alert send failed: %s", channel.name, exc)


# ---------- entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.test:
        return _cmd_test()
    if args.list_targets:
        return _cmd_list_targets()
    return _cmd_run(args.target)


def _cmd_test() -> int:
    channels = _build_channels()
    if not channels:
        LOGGER.error(
            "No notification channels configured. Set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID "
            "and/or GMAIL_USER+GMAIL_APP_PASSWORD."
        )
        return 1

    failures = 0
    for channel in channels:
        try:
            channel.send_test()
            LOGGER.info("[%s] test message sent", channel.name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] test FAILED: %s", channel.name, exc)
            failures += 1
    return 1 if failures else 0


def _cmd_list_targets() -> int:
    failures = _load_failures()
    print("Targets:")
    for name, (_, describe) in TARGETS.items():
        print(f"  - {describe()}")
    print()
    print("Channels configured:")
    channels = _build_channels()
    if not channels:
        print("  (none — DRY RUN mode)")
    else:
        for ch in channels:
            print(f"  - {ch.name}")
    print()
    print("Consecutive failures:")
    if not failures:
        print("  (none)")
    else:
        for name, count in failures.items():
            print(f"  - {name}: {count}")
    return 0


def _cmd_run(only_target: str | None) -> int:
    targets_to_run = (
        {only_target: TARGETS[only_target]} if only_target else TARGETS
    )

    channels = _build_channels()
    if not channels:
        LOGGER.warning("DRY RUN: no notification channels configured. Alerts will be logged only.")

    failures = _load_failures()
    overall_ok = True

    for name, (run, _) in targets_to_run.items():
        LOGGER.info("[%s] running", name)
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001 — last-resort fence
            LOGGER.exception("[%s] runner crashed", name)
            result = TargetResult(target=name, success=False, error=f"crash: {exc}")

        _process_result(result, failures, channels)
        overall_ok = overall_ok and result.success

    _save_failures(failures)
    # Always exit 0 so the workflow proceeds to commit-state and reschedule.
    return 0 if overall_ok else 0


def _process_result(
    result: TargetResult,
    failures: dict[str, int],
    channels: List[Channel],
) -> None:
    if result.success:
        if failures.get(result.target):
            LOGGER.info("[%s] recovered after %s failures", result.target, failures[result.target])
        failures[result.target] = 0
    else:
        failures[result.target] = failures.get(result.target, 0) + 1
        LOGGER.warning(
            "[%s] FAILED (%s consecutive). Error: %s",
            result.target, failures[result.target], result.error,
        )

    if result.info:
        LOGGER.info("[%s] %s", result.target, result.info)

    # Fan out target alerts to every configured channel.
    for spec in result.messages:
        _dispatch(spec, channels)

    # Escalate after FAILURE_LIMIT consecutive failures, then reset so we
    # don't spam every run.
    if failures.get(result.target, 0) >= FAILURE_LIMIT:
        broken_spec = AlertSpec(
            kind="monitor_broken",
            target=result.target,
            error=result.error or "unknown",
        )
        _dispatch(broken_spec, channels)
        LOGGER.warning("[%s] sent 'monitor broken' alert", result.target)
        failures[result.target] = 0


# ---------- state ----------

def _load_failures() -> dict[str, int]:
    if not FAILURE_STATE_FILE.exists():
        return {}
    try:
        with FAILURE_STATE_FILE.open() as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_failures(failures: dict[str, int]) -> None:
    FAILURE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FAILURE_STATE_FILE.open("w") as fh:
        json.dump(failures, fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------- CLI ----------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FIFA WC2026 ticket drop monitor")
    parser.add_argument("--test", action="store_true",
                        help="Send a test message to every configured channel and exit")
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="Print stored state for each target and exit",
    )
    parser.add_argument(
        "--target",
        choices=sorted(TARGETS.keys()),
        help="Run only the named target (default: all)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging (DEBUG)")
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


if __name__ == "__main__":
    sys.exit(main())
