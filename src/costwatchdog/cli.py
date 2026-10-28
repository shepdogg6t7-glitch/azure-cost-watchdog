"""Command line interface.

Two verbs, deliberately: ``audit`` (read-only, safe to run anywhere) and ``apply``
(executes a previously reviewed plan, dry-run unless ``--yes`` is given).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from costwatchdog import __version__
from costwatchdog.auditlog import AuditLog
from costwatchdog.azure_client import AzureError, MissingDependencyError
from costwatchdog.billing import summarize_costs
from costwatchdog.engine import AuditResult, evaluate
from costwatchdog.policy import Policy, PolicyError
from costwatchdog.report import render_console, render_json, render_markdown
from costwatchdog.sources import (
    SourceError,
    dump_json,
    load_cost_records,
    load_metrics,
    load_plan,
    load_policy,
    load_resources,
    parse_amount,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_WASTE_BUDGET_EXCEEDED = 3
EXIT_EXECUTION_FAILED = 4

DEFAULT_REPORT = "reports/costwatchdog-report.md"
DEFAULT_AUDIT_LOG = "audit/costwatchdog.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="costwatchdog",
        description="Find idle and orphaned Azure resources, report the waste, and deallocate behind guardrails.",
    )
    parser.add_argument("--version", action="version", version=f"costwatchdog {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser(
        "audit",
        help="assess resources and write a report (read-only unless a plan is applied later)",
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
    audit.add_argument("--plan-out", help="also write an executable plan JSON for `costwatchdog apply`")
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
    audit.add_argument("--live", action="store_true", help="pull data from Azure instead of fixture files")
    audit.add_argument(
        "--subscriptions",
        help="comma-separated subscription ids for --live (default: all visible subscriptions)",
    )
    audit.add_argument("--days", type=int, default=30, help="lookback window for --live (default: 30)")
    audit.add_argument("--quiet", action="store_true", help="only print the report path")

    apply_cmd = subparsers.add_parser(
        "apply",
        help="execute a plan produced by `audit` (dry-run unless --yes is passed)",
    )
    apply_cmd.add_argument("--plan", required=True, help="plan JSON from `audit --plan-out`")
    apply_cmd.add_argument("--yes", action="store_true", help="actually execute (default is dry-run)")
    apply_cmd.add_argument("--allow-prod", action="store_true", help="also execute decisions blocked by environment=prod")
    apply_cmd.add_argument(
        "--allow-orphan-deletion",
        action="store_true",
        help="also execute delete-orphan decisions",
    )
    apply_cmd.add_argument("--limit", type=int, help="execute at most this many actions in this run")
    apply_cmd.add_argument("--audit-log", help=f"JSONL audit trail (default: {DEFAULT_AUDIT_LOG})")
    apply_cmd.add_argument("--no-audit-log", action="store_true", help="do not write an audit trail")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            return _run_audit(args, parser)
        return _run_apply(args, parser)
    except (SourceError, PolicyError, MissingDependencyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except AzureError as error:
        print(f"azure error: {error}", file=sys.stderr)
        return EXIT_EXECUTION_FAILED
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130


def _run_audit(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    policy = load_policy(args.policy)
    if args.currency:
        policy = _with_currency(policy, args.currency)
    today = _parse_today(args.today, parser)

    if args.live:
        resources, records, metrics_map, scope = _load_live(args)
    else:
        if not args.cost_csv or not args.inventory:
            parser.error("audit needs --cost-csv and --inventory (or --live)")
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
    audit_log.record_result(result, mode="dry-run", scope=scope)

    written = _write_outputs(args, result, scope)
    if not args.quiet:
        print(render_console(result, mode="dry-run", scope=scope))
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
        _write(out_path, render_markdown(result, mode="dry-run", scope=scope))
        written.append(out_path)
    if args.format in {"json", "both"}:
        json_path = out_path.with_suffix(".json") if args.format == "both" else out_path
        _write(json_path, render_json(result, mode="dry-run", scope=scope))
        written.append(json_path)
    if args.plan_out:
        plan_path = dump_json(args.plan_out, result.to_dict())
        written.append(plan_path)
    return written


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_live(args: argparse.Namespace):
    from costwatchdog.azure_client import AzureClients, require_azure_sdk

    require_azure_sdk()

    subscriptions = [item.strip() for item in (args.subscriptions or "").split(",") if item.strip()]
    clients = AzureClients(subscriptions or None)
    resources = clients.list_resources()
    records = clients.fetch_cost_records(days=args.days)
    metrics_map = clients.fetch_usage_metrics(resources, days=args.days)
    scope = args.scope or f"live: {', '.join(clients.subscription_ids)}"
    return resources, records, metrics_map, scope


def _run_apply(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    plan = load_plan(args.plan)
    from costwatchdog.azure_client import apply_plan, require_azure_sdk

    if args.yes:
        # A dry-run needs no credentials; executing does.
        require_azure_sdk()

    audit_log = AuditLog(None if args.no_audit_log else (args.audit_log or DEFAULT_AUDIT_LOG))
    dry_run = not args.yes
    results = apply_plan(
        plan,
        dry_run=dry_run,
        allow_prod=args.allow_prod,
        allow_orphan_deletion=args.allow_orphan_deletion,
        limit=args.limit,
        audit_log=audit_log,
    )
    executed = [item for item in results if item["status"] == "executed"]
    skipped = [item for item in results if item["status"] == "skipped"]
    failed = [item for item in results if item["status"] == "failed"]

    print(f"costwatchdog apply · {'dry-run' if dry_run else 'executing'} · {len(results)} decision(s)")
    for item in results:
        print(f"  {item['status']:<9} {item['action']:<17} {item['resource_name']}  {item.get('detail', '')}")
    print(f"\n  executed {len(executed)} · skipped {len(skipped)} · failed {len(failed)}")
    if dry_run:
        print("  this was a dry-run; re-run with --yes to make changes")
    return EXIT_EXECUTION_FAILED if failed else EXIT_OK


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
