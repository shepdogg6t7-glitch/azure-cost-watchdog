"""Render audit results as Markdown (for the repo/PR comment), JSON (for automation) or
a compact console table (for humans running it ad hoc)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterable, Sequence

from costwatchdog.engine import AuditResult, suggest_command
from costwatchdog.models import Action, Decision, money


def _money(amount: Decimal, currency: str) -> str:
    return f"{currency} {money(amount):,.2f}"


def _scope_line(scope: str | None) -> str:
    return scope.strip() if scope else "not specified"


def render_markdown(result: AuditResult, *, mode: str = "dry-run", scope: str | None = None) -> str:
    """Full report: summary table, then every decision grouped by required action."""
    lines: list[str] = []
    lines.append("# Cloud Cost Watchdog - audit report")
    lines.append("")
    lines.append(
        f"_{result.generated_at.isoformat()} · window {result.observed_days} day(s) · "
        f"currency {result.currency} · mode {mode} · scope {_scope_line(scope)}_"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Monthly run rate (priced resources) | {_money(result.total_monthly_cost, result.currency)} |")
    lines.append(f"| Unallocated spend (no resource id) | {_money(result.unallocated_monthly_cost, result.currency)} |")
    lines.append(f"| Identified monthly waste | {_money(result.identified_monthly_waste, result.currency)} |")
    lines.append(
        f"| Actionable now | {_money(result.actionable_monthly_savings, result.currency)} "
        f"({len(result.actionable)} decision(s)) |"
    )
    lines.append(
        f"| Pending approval | {_money(result.pending_monthly_savings, result.currency)} "
        f"({len(result.pending_approval)} decision(s)) |"
    )
    lines.append(f"| Notify only | {len(result.notify_only)} decision(s) |")
    lines.append(f"| Resources considered | {result.resources_considered} |")
    lines.append(f"| Resources with cost data | {result.resources_with_cost} |")
    lines.append(f"| Healthy / no action | {result.healthy} |")
    lines.append(f"| Skipped (type not monitored) | {result.skipped_types} |")
    lines.append(f"| Dropped below threshold | {result.dropped_below_threshold} |")
    lines.append("")

    sections: list[tuple[str, str, Sequence[Decision]]] = [
        (
            "Actionable now",
            "The watchdog would carry these out on the next scheduled run.",
            result.actionable,
        ),
        (
            "Pending approval",
            "Detected and worth money, but a guardrail requires a human decision.",
            result.pending_approval,
        ),
        (
            "Notify only",
            "Reported for review; no automated action is possible or allowed.",
            result.notify_only,
        ),
    ]
    index = 0
    for title, blurb, decisions in sections:
        lines.append(f"## {title} ({len(decisions)})")
        lines.append("")
        lines.append(blurb)
        lines.append("")
        if not decisions:
            lines.append("_None._")
            lines.append("")
            continue
        for decision in decisions:
            index += 1
            lines.extend(_decision_markdown(index, decision, result.currency))
    lines.extend(_methodology(result, mode))
    return "\n".join(lines).rstrip() + "\n"


def _decision_markdown(index: int, decision: Decision, currency: str) -> list[str]:
    resource = decision.resource
    lines = [
        f"### {index}. {resource.name} - `{decision.action.value}` - "
        f"saves {_money(decision.estimated_monthly_savings, currency)}/mo",
        "",
        f"- **Resource**: `{resource.id or 'unknown id'}`",
        f"- **Type / group / subscription**: {resource.display_type or 'unknown'} · "
        f"{resource.resource_group or 'unknown'} · {resource.subscription_id or 'unknown'}",
        f"- **Category**: {decision.category} · **severity**: {decision.severity.value} · "
        f"**confidence**: {decision.confidence}",
        f"- **Monthly cost**: {_money(decision.monthly_cost, currency)} "
        f"(observed over {decision.observed_days} day(s); "
        f"reclaimable share {decision.reclaimable_ratio:.0%})",
        f"- **Why**: {decision.reason}",
    ]
    if decision.evidence:
        lines.append("- **Evidence**:")
        for item in sorted(set(decision.evidence)):
            lines.append(f"  - {item}")
    if decision.safeguards:
        lines.append("- **Safeguards applied**: " + ", ".join(f"`{item}`" for item in decision.safeguards))
    if decision.approvals:
        lines.append("- **Needs approval because**: " + ", ".join(f"`{item}`" for item in decision.approvals))
    if decision.action is not Action.NOTIFY:
        lines.append(f"- **Suggested command**: `{suggest_command(decision)}`")
    lines.append("")
    return lines


def _methodology(result: AuditResult, mode: str) -> list[str]:
    policy = result.policy
    lines = [
        "## How these decisions were made",
        "",
        "* Spend comes from Azure Cost Management line items, normalized to a "
        "30.4375-day month from the export's own date range.",
        "* Usage comes from Azure Monitor. `cpu_peak_percent` is the maximum of hourly "
        "averages over the window, so idleness is judged on the busiest hour, not the mean.",
        f"* Idle means: CPU peak <= {policy.cpu_idle_peak_percent:.1f}%, network <= "
        f"{policy.idle_network_bytes_per_day / 1_048_576:,.1f} MB/day, and no meaningful "
        f"activity for >= {policy.min_idle_days} days.",
        f"* Anything reclaiming less than {_money(policy.min_monthly_savings, result.currency)}"
        "/month is dropped as noise.",
        f"* At most {policy.max_actions_per_run} actions are auto-executed per run; the rest "
        "are deferred to the next run.",
        f"* Production is {'actionable' if policy.allow_prod_actions else 'never auto-actioned'}, "
        f"and orphan deletion is {'enabled' if policy.allow_orphan_deletion else 'disabled'} "
        "in this policy.",
        f"* Any resource tagged {', '.join('`' + key + '`' for key in policy.protected_tag_keys)} "
        "is reported but never touched.",
        f"* Mode: **{mode}**. Dry-run reports make no changes to Azure.",
        "",
    ]
    return lines


def render_json(result: AuditResult, *, mode: str = "dry-run", scope: str | None = None) -> str:
    payload: dict[str, Any] = result.to_dict()
    payload["mode"] = mode
    payload["scope"] = scope or ""
    return json.dumps(payload, indent=2, sort_keys=False)


def render_console(result: AuditResult, *, mode: str = "dry-run", scope: str | None = None) -> str:
    """Compact table for terminal use."""
    lines = [
        f"costwatchdog · {mode} · window {result.observed_days}d · currency {result.currency}"
        + (f" · scope {scope}" if scope else ""),
        "",
        f"  monthly run rate      {_money(result.total_monthly_cost, result.currency)}",
        f"  unallocated spend     {_money(result.unallocated_monthly_cost, result.currency)}",
        f"  identified waste      {_money(result.identified_monthly_waste, result.currency)}",
        f"  actionable now        {_money(result.actionable_monthly_savings, result.currency)}",
        f"  pending approval      {_money(result.pending_monthly_savings, result.currency)}",
        f"  resources considered  {result.resources_considered}"
        f" ({result.healthy} healthy, {result.dropped_below_threshold} below threshold)",
        "",
    ]
    if result.decisions:
        lines.append(f"  {'ACTION':<17} {'SEV':<7} {'SAVINGS/MO':>14}  RESOURCE")
        for decision in result.decisions:
            marker = "!" if decision.is_actionable else "~"
            lines.append(
                f"  {decision.action.value:<17} {decision.severity.value:<7} "
                f"{_money(decision.estimated_monthly_savings, result.currency):>14}  "
                f"{marker} {decision.resource.name}  ({decision.resource.resource_group or 'n/a'})"
            )
        lines.append("")
        lines.append("  ! = executable now   ~ = needs approval / review")
    else:
        lines.append("  No waste found with the current policy.")
    return "\n".join(lines)


def savings_rows(decisions: Iterable[Decision], currency: str) -> list[tuple[str, str, str]]:
    """(action, resource name, savings) rows - handy for CSV export or tests."""
    return [
        (decision.action.value, decision.resource.name, _money(decision.estimated_monthly_savings, currency))
        for decision in decisions
    ]
