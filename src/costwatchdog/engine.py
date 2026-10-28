"""The decision engine: assess -> guardrail -> rank -> cap.

This module is where safety lives. Detectors say "this looks idle"; the engine decides
whether the watchdog is *allowed* to act, and records exactly which rule allowed or
blocked each action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from costwatchdog import detectors
from costwatchdog.models import (
    Action,
    Assessment,
    Decision,
    Resource,
    ResourceCostSummary,
    Severity,
    UsageMetrics,
    money,
    total_savings,
)
from costwatchdog.policy import Policy

#: Guardrail identifiers, surfaced in reports so operators can see why something stopped.
GUARD_PROTECTED_TAG = "protected-tag"
GUARD_DENIED_SCOPE = "denylisted-scope"
GUARD_ORPHAN_DELETION_DISABLED = "orphan-deletion-disabled"
GUARD_LOW_CONFIDENCE = "low-confidence-requires-approval"
GUARD_MISSING_ENV_TAG = "missing-environment-tag"
GUARD_MISSING_OWNER_TAG = "missing-owner-tag"
GUARD_PROD_ENVIRONMENT = "environment=prod"
GUARD_RATE_LIMIT = "max-actions-per-run"


@dataclass
class AuditResult:
    """Everything a report needs about one audit run."""

    decisions: list[Decision]
    policy: Policy
    generated_at: datetime
    observed_days: int
    currency: str = "USD"
    resources_considered: int = 0
    resources_with_cost: int = 0
    total_monthly_cost: Decimal = Decimal("0.00")
    unallocated_monthly_cost: Decimal = Decimal("0.00")
    dropped_below_threshold: int = 0
    healthy: int = 0
    skipped_types: int = 0

    @property
    def actionable(self) -> list[Decision]:
        return [decision for decision in self.decisions if decision.is_actionable]

    @property
    def pending_approval(self) -> list[Decision]:
        return [decision for decision in self.decisions if decision.requires_approval]

    @property
    def notify_only(self) -> list[Decision]:
        return [
            decision
            for decision in self.decisions
            if decision.action in {Action.NOTIFY} and not decision.requires_approval
        ]

    @property
    def identified_monthly_waste(self) -> Decimal:
        return total_savings(self.decisions)

    @property
    def actionable_monthly_savings(self) -> Decimal:
        return total_savings(self.actionable)

    @property
    def pending_monthly_savings(self) -> Decimal:
        return total_savings(self.pending_approval)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "costwatchdog/plan/v1",
            "generated_at": self.generated_at.isoformat(),
            "currency": self.currency,
            "observed_days": self.observed_days,
            "policy": self.policy.to_dict(),
            "summary": {
                "resources_considered": self.resources_considered,
                "resources_with_cost": self.resources_with_cost,
                "total_monthly_cost": float(self.total_monthly_cost),
                "unallocated_monthly_cost": float(self.unallocated_monthly_cost),
                "identified_monthly_waste": float(self.identified_monthly_waste),
                "actionable_monthly_savings": float(self.actionable_monthly_savings),
                "pending_monthly_savings": float(self.pending_monthly_savings),
                "dropped_below_threshold": self.dropped_below_threshold,
                "healthy": self.healthy,
            },
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


def evaluate(
    resources: Iterable[Resource],
    summaries: Mapping[str, ResourceCostSummary],
    *,
    metrics: Mapping[str, UsageMetrics] | None = None,
    policy: Policy | None = None,
    today: date | None = None,
    unallocated: Decimal = Decimal("0.00"),
    generated_at: datetime | None = None,
) -> AuditResult:
    """Assess every resource and return ranked, guardrailed decisions.

    ``summaries`` is keyed by lowercased resource id (see
    :func:`costwatchdog.billing.summarize_costs`).
    """
    active_policy = policy or Policy()
    resource_list = list(resources)
    usage = {key.lower(): value for key, value in (metrics or {}).items()}
    reference = today or datetime.now(timezone.utc).date()
    decisions: list[Decision] = []
    total = Decimal("0.00")
    with_cost = 0
    healthy = 0
    skipped_types = 0
    dropped = 0

    for resource in resource_list:
        key = resource.id.lower()
        summary = summaries.get(key)
        monthly_cost = summary.monthly_run_rate if summary else Decimal("0.00")
        total += monthly_cost
        if summary is not None:
            with_cost += 1
        if resource.kind not in active_policy.monitored_resource_types:
            skipped_types += 1
            continue

        metrics_row = usage.get(key)
        assessment = detectors.detect(resource, metrics_row, active_policy, reference)
        if assessment is None:
            # Only a missing telemetry row is worth reporting as "cannot judge"; a resource
            # whose detector ran and found nothing is simply healthy.
            unjudgeable = metrics_row is None and detectors.requires_telemetry(resource)
            decision = (
                _unclassified(resource, summary, monthly_cost, active_policy) if unjudgeable else None
            )
            if decision is None:
                healthy += 1
                continue
        else:
            decision = _decide(resource, assessment, summary, monthly_cost, active_policy)
            if decision is None:
                if assessment.suggested_action in {Action.NONE, Action.NOTIFY}:
                    healthy += 1
                else:
                    dropped += 1
                continue
        decisions.append(decision)

    decisions = _rank_and_cap(decisions, active_policy)
    return AuditResult(
        decisions=decisions,
        policy=active_policy,
        generated_at=generated_at or datetime.now(timezone.utc),
        observed_days=max((summary.observed_days for summary in summaries.values()), default=30),
        currency=active_policy.currency,
        resources_considered=len(resource_list),
        resources_with_cost=with_cost,
        total_monthly_cost=money(total),
        unallocated_monthly_cost=money(unallocated),
        dropped_below_threshold=dropped,
        healthy=healthy,
        skipped_types=skipped_types,
    )


def _unclassified(
    resource: Resource,
    summary: ResourceCostSummary | None,
    monthly_cost: Decimal,
    policy: Policy,
) -> Decision | None:
    """Report expensive resources the watchdog could not judge (no telemetry)."""
    if not policy.notify_unclassified or monthly_cost < policy.notify_unclassified_above:
        return None
    return Decision(
        resource=resource,
        action=Action.NOTIFY,
        severity=Severity.INFO,
        category="unclassified",
        reason=(
            "No usage telemetry or detector for this resource type; it is expensive but the "
            "watchdog will not guess. Add monitoring or an owner tag to make it actionable."
        ),
        evidence=(
            "no metrics row was supplied for this resource id",
            f"monthly run rate {policy.currency} {monthly_cost:,.2f}",
            f"resource type {resource.display_type or 'unknown'}",
        ),
        confidence="low",
        currency=policy.currency,
        monthly_cost=monthly_cost,
        reclaimable_ratio=Decimal("0"),
        observed_days=summary.observed_days if summary else 0,
    )


def _decide(
    resource: Resource,
    assessment: Assessment,
    summary: ResourceCostSummary | None,
    monthly_cost: Decimal,
    policy: Policy,
) -> Decision | None:
    """Turn an assessment into a decision, applying every guardrail in order."""
    action = assessment.suggested_action
    safeguards: list[str] = []
    approvals: list[str] = []
    savings = money(monthly_cost * assessment.reclaimable_ratio)
    tags = resource.tags

    if action is Action.NONE:
        return None
    # The threshold test runs before the guardrails on purpose: a guardrail that downgrades
    # an action to "notify" must not resurrect waste that is too small to be worth reporting.
    if action is not Action.NOTIFY and savings < policy.min_monthly_savings:
        return None

    protected_key = policy.protected_tag(tags)
    denied_scope = policy.is_denied_scope(
        subscription_id=resource.subscription_id, resource_group=resource.resource_group
    )
    environment = policy.environment_of(tags)
    owner = policy.owner_of(tags)

    # Guardrail 1: an explicit protection tag always wins.
    if protected_key and action is not Action.NOTIFY:
        safeguards.append(f"{GUARD_PROTECTED_TAG}:{protected_key}")
        action = Action.NOTIFY
    # Guardrail 2: deny-listed subscriptions/resource groups are read-only.
    if denied_scope and action is not Action.NOTIFY:
        safeguards.append(GUARD_DENIED_SCOPE)
        action = Action.NOTIFY
    # Guardrail 3: ownership and environment context must exist before money moves.
    if policy.require_owner_tag and not owner:
        approvals.append(GUARD_MISSING_OWNER_TAG)
    if environment is None:
        approvals.append(GUARD_MISSING_ENV_TAG)
    # Guardrail 4: production requires an explicit opt-in.
    if (
        environment in {value.lower() for value in policy.protected_environments}
        and not policy.allow_prod_actions
        and action not in {Action.NOTIFY, Action.NONE}
    ):
        approvals.append(GUARD_PROD_ENVIRONMENT)
    # Guardrail 5: destructive cleanup is opt-in, and low-confidence deletes need a human.
    if action is Action.DELETE_ORPHAN:
        if not policy.allow_orphan_deletion:
            safeguards.append(GUARD_ORPHAN_DELETION_DISABLED)
            action = Action.NOTIFY
        elif assessment.confidence != "high":
            approvals.append(GUARD_LOW_CONFIDENCE)

    severity = _severity(action, savings, policy)
    return Decision(
        resource=resource,
        action=action,
        severity=severity,
        category=assessment.category,
        reason=assessment.reason,
        evidence=assessment.evidence + tuple(safeguards + approvals),
        safeguards=tuple(safeguards),
        approvals=tuple(approvals),
        confidence=assessment.confidence,
        currency=policy.currency,
        monthly_cost=monthly_cost,
        reclaimable_ratio=assessment.reclaimable_ratio,
        observed_days=summary.observed_days if summary else 0,
    )


def _severity(action: Action, savings: Decimal, policy: Policy) -> Severity:
    if action in {Action.NONE, Action.NOTIFY}:
        return Severity.INFO
    if savings >= policy.high_severity_savings:
        return Severity.HIGH
    return Severity.MEDIUM


def _rank_and_cap(decisions: list[Decision], policy: Policy) -> list[Decision]:
    """Sort by impact, then refuse to auto-execute more than the configured rate limit."""
    ranked = sorted(decisions, key=lambda decision: decision.sort_key)
    executable = [decision for decision in ranked if decision.is_actionable]
    overflow = executable[policy.max_actions_per_run :]
    if not overflow:
        return ranked
    limited: list[Decision] = []
    overflow_ids = {id(decision) for decision in overflow}
    for decision in ranked:
        if id(decision) in overflow_ids:
            limited.append(
                Decision(
                    resource=decision.resource,
                    action=decision.action,
                    severity=decision.severity,
                    category=decision.category,
                    reason=decision.reason
                    + f" (deferred: rate limit of {policy.max_actions_per_run} actions per run reached)",
                    evidence=decision.evidence + (f"{GUARD_RATE_LIMIT}={policy.max_actions_per_run}",),
                    safeguards=decision.safeguards + (GUARD_RATE_LIMIT,),
                    approvals=decision.approvals + (GUARD_RATE_LIMIT,),
                    confidence=decision.confidence,
                    currency=decision.currency,
                    monthly_cost=decision.monthly_cost,
                    reclaimable_ratio=decision.reclaimable_ratio,
                    observed_days=decision.observed_days,
                )
            )
        else:
            limited.append(decision)
    return limited


def suggest_command(decision: Decision) -> str:
    """The exact az CLI command that carries out this decision."""
    resource_id = decision.resource.id
    kind = decision.resource.kind
    if decision.action is Action.DEALLOCATE:
        if kind.endswith("virtualmachinescalesets"):
            return f"az vmss deallocate --ids {resource_id}"
        return f"az vm deallocate --ids {resource_id}"
    if decision.action is Action.STOP:
        if kind == "microsoft.containerservice/managedclusters":
            return f"az aks stop --ids {resource_id}"
        if kind == "microsoft.web/serverfarms":
            return f"az appservice plan delete --ids {resource_id} --yes"
        return f"az resource update --ids {resource_id} --set tags.costwatchdog=review"
    if decision.action is Action.DELETE_ORPHAN:
        if kind == "microsoft.compute/disks":
            return f"az disk delete --ids {resource_id} --yes"
        if kind == "microsoft.compute/snapshots":
            return f"az snapshot delete --ids {resource_id} --yes"
        if kind == "microsoft.network/publicipaddresses":
            return f"az network public-ip delete --ids {resource_id}"
        if kind == "microsoft.network/networkinterfaces":
            return f"az network nic delete --ids {resource_id}"
        if kind == "microsoft.web/serverfarms":
            return f"az appservice plan delete --ids {resource_id} --yes"
        return f"az resource delete --ids {resource_id}"
    if decision.action is Action.SCHEDULE_SHUTDOWN:
        return (
            "az resource update --ids "
            f"{resource_id} --set tags.costwatchdog:action=schedule-shutdown"
        )
    return "# review manually - no automated command for this decision"
