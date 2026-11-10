"""Command line interface for local, offline cost and resource audits."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from costwatchdog import __version__
from costwatchdog.auditlog import AuditLog
from costwatchdog.billing import summarize_costs
from costwatchdog.engine import AuditResult, evaluate
from costwatchdog.policy import Policy, PolicyError
from costwatchdog.report import render_console, render_json, render_markdown
from costwatchdog.sources import (
    SourceError,
    load_cost_records,
    load_metrics,
    load_policy,
    load_resources,
    parse_amount,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_WASTE_BUDGET_EXCEEDED = 3

DEFAULT_REPORT = "reports/costwatchdog-report.md"
DEFAULT_AUDIT_LOG = "audit/costwatchdog.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="costwatchdog",
        description="Analyze local resource and cost data and produce informational recommendations.",
    )
    parser.add_argument("--version", action="version", version=f"costwatchdog {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser(
        "audit",
        help="analyze local data and write an informational report",
    )
    audit.add_argument("--cost-csv", help="Azure Cost Management export (CSV)")
    audit.add_argument("--inventory", help="resource inventory JSON (Resource Graph or ARM list)")
    audit.add_argument("--metrics", help="usage metrics JSON keyed by resource id")
    audit.add_argument("--policy", help="policy JSON (see examples/policy.json)")
    audit.add_argument("--currency", help="only consider cost rows in this currency")
    audit.add_argument("--period-days", type=int, help="force the billing window length")
    audit.add_argument(
        "--format",
        choices=("md", "json", "both", "console"),
        default="md",
        help="output format; 'both' writes Markdown and JSON next to each other",
    )
    audit.add_argument("--out", help=f"report path (default: {DEFAULT_REPORT})")
    audit.add_argument("--audit-log", help=f"JSONL audit trail (default: {DEFAULT_AUDIT_LOG})")
    audit.add_argument(
        "--no-audit-log",
        action="store_true",
        help="do not write an audit trail",
    )
    audit.add_argument(
        "--fail-on-waste",
        help="exit 3 when identified monthly waste exceeds this amount (for CI gates)",
    )
    audit.add_argument("--scope", help="label for the report header, e.g. 'prod subscriptions'")
    audit.add_argument("--today", help="override today's date (YYYY-MM-DD) for reproducible runs")
    audit.add_argument("--quiet", action="store_true", help="only print the report path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_audit(args, parser)
    except (SourceError, PolicyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130


def _run_audit(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    policy = load_policy(args.policy)
    if args.currency:
        policy = _with_currency(policy, args.currency)
    today = _parse_today(args.today, parser)

    if not args.cost_csv or not args.inventory:
        parser.error("audit needs local --cost-csv and --inventory files")
    resources = load_resources(args.inventory)
    records = load_cost_records(args.cost_csv, currency=args.currency or policy.currency)
    metrics_map = load_metrics(args.metrics)
    scope = args.scope or f"{Path(args.inventory).name} + {Path(args.cost_csv).name}"

    summaries, unallocated = summarize_costs(records, period_days=args.period_days)
    result = evaluate(
        resources,
        summaries,
        metrics=metrics_map,
        policy=policy,
        today=today,
        unallocated=unallocated,
        generated_at=_now(today),
    )

    audit_log = AuditLog(None if args.no_audit_log else (args.audit_log or DEFAULT_AUDIT_LOG))
    audit_log.record_result(result, mode="offline-audit", scope=scope)

    written = _write_outputs(args, result, scope)
    if not args.quiet:
        print(render_console(result, mode="offline-audit", scope=scope))
        for path in written:
            print(f"\nwrote {path}")

    if args.fail_on_waste:
        threshold = parse_amount(args.fail_on_waste)
        if result.identified_monthly_waste > threshold:
            print(
                f"error: identified monthly waste {result.currency} {result.identified_monthly_waste:,.2f} "
                f"exceeds budget {result.currency} {threshold:,.2f}",
                file=sys.stderr,
            )
            return EXIT_WASTE_BUDGET_EXCEEDED
    return EXIT_OK


def _write_outputs(args: argparse.Namespace, result: AuditResult, scope: str) -> list[Path]:
    written: list[Path] = []
    out_path = Path(args.out or DEFAULT_REPORT)
    if args.format in {"md", "both"}:
        _write(out_path, render_markdown(result, mode="offline-audit", scope=scope))
        written.append(out_path)
    if args.format in {"json", "both"}:
        json_path = out_path.with_suffix(".json") if args.format == "both" else out_path
        _write(json_path, render_json(result, mode="offline-audit", scope=scope))
        written.append(json_path)
    return written


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _parse_today(value: str | None, parser: argparse.ArgumentParser) -> date:
    if not value:
        return datetime.now(timezone.utc).date()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        parser.error("--today must look like YYYY-MM-DD")
        raise  # pragma: no cover - argparse exits


def _now(today: date) -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(year=today.year, month=today.month, day=today.day)


def _with_currency(policy: Policy, currency: str) -> Policy:
    return replace(policy, currency=currency.upper())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
