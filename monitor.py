"""FIFA WC2026 ticket-drop monitor — orchestrator.

Runs each target, sends Telegram alerts for changes, tracks consecutive
failures per target, escalates with a "monitor broken" alert after 3 in a row.

CLI:
    python monitor.py                    # normal run, all targets
    python monitor.py --test             # send a test Telegram, no scraping
    python monitor.py --list-targets     # print stored state
    python monitor.py --target news      # run only one target
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Callable, Dict

import notifier
from targets import TargetResult
from targets import news, sales_info, shop

LOGGER = logging.getLogger("monitor")

FAILURE_LIMIT = 3
FAILURE_STATE_FILE = Path(__file__).resolve().parent / "state" / "failure_counts.json"

TargetRunner = Callable[[], TargetResult]
TargetDescriber = Callable[[], str]

TARGETS: Dict[str, tuple[TargetRunner, TargetDescriber]] = {
    news.NAME: (news.run, news.describe_state),
    sales_info.NAME: (sales_info.run, sales_info.describe_state),
    shop.NAME: (shop.run, shop.describe_state),
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.test:
        return _cmd_test()
    if args.list_targets:
        return _cmd_list_targets()
    return _cmd_run(args.target)


# ---------- subcommands ----------

def _cmd_test() -> int:
    try:
        notifier.send(notifier.test_message())
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Test send failed: %s", exc)
        return 1
    LOGGER.info("Test message sent.")
    return 0


def _cmd_list_targets() -> int:
    failures = _load_failures()
    print("Targets:")
    for name, (_, describe) in TARGETS.items():
        print(f"  - {describe()}")
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

    # Detect Telegram credentials once. If missing, run in dry-run mode so
    # alert messages are logged but not sent — useful for local development.
    sender = _build_sender()

    failures = _load_failures()
    overall_ok = True

    for name, (run, _) in targets_to_run.items():
        LOGGER.info("[%s] running", name)
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001 — last-resort fence
            LOGGER.exception("[%s] runner crashed", name)
            result = TargetResult(target=name, success=False, error=f"crash: {exc}")

        _process_result(result, failures, sender)
        overall_ok = overall_ok and result.success

    _save_failures(failures)
    # Always exit 0 so the workflow proceeds to commit-state and reschedule.
    return 0 if overall_ok else 0


# ---------- result handling ----------

def _build_sender() -> Callable[[str], None]:
    """Return a callable that sends a Telegram message, or a dry-run logger."""
    try:
        cfg = notifier.TelegramConfig.from_env()
    except notifier.TelegramConfigError as exc:
        LOGGER.warning("DRY RUN: %s — alerts will be logged, not sent.", exc)

        def _dry(msg: str) -> None:
            LOGGER.info("[DRY RUN] would send Telegram message:\n%s", msg)

        return _dry

    def _send(msg: str) -> None:
        notifier.send(msg, config=cfg)

    return _send


def _process_result(
    result: TargetResult,
    failures: dict[str, int],
    sender: Callable[[str], None],
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

    # Send any change/new-article alerts.
    for msg in result.messages:
        try:
            sender(msg)
            LOGGER.info("[%s] alert dispatched", result.target)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] failed to send alert: %s", result.target, exc)

    # Escalate after FAILURE_LIMIT consecutive failures, then reset so we
    # don't spam every run.
    if failures.get(result.target, 0) >= FAILURE_LIMIT:
        try:
            sender(notifier.monitor_broken(result.target, result.error or "unknown"))
            LOGGER.warning("[%s] sent 'monitor broken' alert", result.target)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] failed to send broken alert: %s", result.target, exc)
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
    parser.add_argument("--test", action="store_true", help="Send a Telegram test message and exit")
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
