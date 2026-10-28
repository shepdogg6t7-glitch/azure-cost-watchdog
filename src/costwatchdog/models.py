"""Plain data structures shared by the watchdog engine.

Nothing here imports an Azure SDK, reads a file, or touches the network, so the whole
decision path can be unit tested with fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Iterable, Mapping

#: Azure billing months average 30.4375 days (365.25 / 12).
DAYS_PER_MONTH = Decimal("30.4375")
_CENTS = Decimal("0.01")
TRUTHY_TAG_VALUES = frozenset({"1", "true", "yes", "y", "on"})


class Action(str, Enum):
    """What the watchdog is willing to do about a resource."""

    NONE = "none"
    NOTIFY = "notify"
    DEALLOCATE = "deallocate"
    STOP = "stop"
    DELETE_ORPHAN = "delete-orphan"
    SCHEDULE_SHUTDOWN = "schedule-shutdown"


class Severity(str, Enum):
    INFO = "info"
    MEDIUM = "medium"
    HIGH = "high"


_SEVERITY_RANK = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.INFO: 2}
_ACTION_RANK = {
    Action.DEALLOCATE: 0,
    Action.DELETE_ORPHAN: 1,
    Action.STOP: 2,
    Action.SCHEDULE_SHUTDOWN: 3,
    Action.NOTIFY: 4,
    Action.NONE: 5,
}


def money(value: Any) -> Decimal:
    """Coerce a value to a 2-decimal money amount without binary float noise."""
    if isinstance(value, Decimal):
        amount = value
    else:
        amount = Decimal(str(value))
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


def parse_tag_string(raw: Any) -> dict[str, str]:
    """Parse the two shapes Azure hands back for tags.

    Resource Graph and ARM return a dict; Cost Management exports flatten tags into a
    single string, either ``key:value;key2:value2`` or a JSON object. Both are supported.
    """
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(k): str(v) for k, v in raw.items()}
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return {str(k): str(v) for k, v in parsed.items()}
    tags: dict[str, str] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip().strip(",")
        if not chunk:
            continue
        for separator in (":", "="):
            if separator in chunk:
                key, _, value = chunk.partition(separator)
                key = key.strip().strip('"').strip()
                if key:
                    tags[key] = value.strip().strip('"')
                break
    return tags


def _dig(payload: Mapping[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _segment(resource_id: str, marker: str) -> str:
    parts = [part for part in str(resource_id).split("/") if part]
    lowered = [part.lower() for part in parts]
    if marker in lowered:
        index = lowered.index(marker)
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _pretty_arm_type(arm_type: str) -> str:
    """`microsoft.compute/virtualmachines` -> `Microsoft.Compute/virtualMachines`."""
    if "/" not in arm_type:
        return arm_type
    provider, _, rest = arm_type.partition("/")
    return f"{provider.title()}/{rest}"


@dataclass(frozen=True)
class Resource:
    """A resource as seen by the watchdog (from Resource Graph, ARM, or a fixture)."""

    id: str
    name: str
    type: str
    resource_group: str = ""
    subscription_id: str = ""
    location: str = ""
    tags: Mapping[str, str] = field(default_factory=dict)
    power_state: str = ""
    sku: str = ""
    properties: Mapping[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Normalized resource type, e.g. ``microsoft.compute/virtualmachines``."""
        return self.type.lower()

    @property
    def display_type(self) -> str:
        return _pretty_arm_type(self.type) if self.type else "unknown"

    def tag(self, *names: str) -> str | None:
        """Case-insensitive tag lookup; returns the first matching name."""
        lowered = {str(key).lower(): value for key, value in self.tags.items()}
        for name in names:
            candidate = name.lower()
            if candidate in lowered:
                return str(lowered[candidate])
        return None

    def tag_bool(self, *names: str) -> bool:
        value = self.tag(*names)
        return value is not None and value.strip().lower() in TRUTHY_TAG_VALUES

    def is_deallocated(self) -> bool:
        state = self.power_state.lower()
        if not state:
            return False
        # Resource Graph reports "VM deallocated"; ARM reports "deallocated"/"stopped".
        return "deallocat" in state or state.endswith("stopped")

    @classmethod
    def from_arm(cls, payload: Mapping[str, Any]) -> "Resource":
        """Build a Resource from ARM list output or a Resource Graph row."""
        raw_id = payload.get("id") or payload.get("resourceId") or ""
        resource_id = str(raw_id)
        properties = payload.get("properties")
        if not isinstance(properties, Mapping):
            properties = {}
        name = payload.get("name") or (resource_id.rstrip("/").split("/")[-1] if resource_id else "")
        power_state = (
            payload.get("powerState")
            or properties.get("powerState")
            or _dig(properties, "extended", "instanceView", "powerState", "code")
            or ""
        )
        sku = payload.get("sku")
        if isinstance(sku, Mapping):
            sku_name = str(sku.get("name") or sku.get("tier") or "")
        else:
            sku_name = str(sku or properties.get("sku") or "")
        return cls(
            id=resource_id,
            name=str(name),
            type=str(payload.get("type") or payload.get("resourceType") or ""),
            resource_group=str(
                payload.get("resourceGroup")
                or payload.get("resource_group")
                or _segment(resource_id, "resourcegroups")
            ),
            subscription_id=str(
                payload.get("subscriptionId")
                or payload.get("subscription_id")
                or _segment(resource_id, "subscriptions")
            ),
            location=str(payload.get("location") or ""),
            tags=parse_tag_string(payload.get("tags")),
            power_state=str(power_state),
            sku=sku_name,
            properties=properties,
        )


@dataclass(frozen=True)
class CostRecord:
    """One row of an Azure Cost Management export."""

    resource_id: str
    cost: Decimal
    currency: str = ""
    resource_name: str = ""
    resource_type: str = ""
    resource_group: str = ""
    subscription_id: str = ""
    meter_category: str = ""
    meter_subcategory: str = ""
    meter_name: str = ""
    quantity: Decimal = Decimal("0")
    charge_type: str = ""
    frequency: str = ""
    pricing_model: str = ""
    benefit_name: str = ""
    tags: Mapping[str, str] = field(default_factory=dict)
    date: date | None = None

    @property
    def is_amortized(self) -> bool:
        """True for amortized reservation/savings-plan rows (already spread over usage)."""
        model = self.pricing_model.lower()
        return bool(model) and model not in {"on-demand", "ondemand", "spot", "none"}


@dataclass(frozen=True)
class ResourceCostSummary:
    """Aggregated spend for a single resource over the observed window."""

    resource_id: str
    currency: str
    total: Decimal
    observed_days: int
    by_meter: Mapping[str, Decimal] = field(default_factory=dict)
    by_charge_type: Mapping[str, Decimal] = field(default_factory=dict)
    record_count: int = 0
    resource_name: str = ""
    resource_type: str = ""
    resource_group: str = ""
    subscription_id: str = ""

    @property
    def monthly_run_rate(self) -> Decimal:
        """Normalize the observed total to a 30.44-day month."""
        days = max(self.observed_days, 1)
        return money(self.total / Decimal(days) * DAYS_PER_MONTH)


@dataclass(frozen=True)
class UsageMetrics:
    """Usage signals for a resource, summarized over a window.

    Azure Monitor hands back hourly averages, so ``cpu_peak_percent`` is the
    conservative high-water mark (max of hourly averages) rather than a true p95.
    Fixtures can supply a real percentile in the same field.
    """

    resource_id: str
    cpu_peak_percent: float | None = None
    cpu_avg_percent: float | None = None
    network_in_bytes_per_day: float | None = None
    network_out_bytes_per_day: float | None = None
    request_count_30d: int | None = None
    connections_30d: int | None = None
    last_activity: datetime | None = None
    days_since_last_activity: int | None = None
    window_days: int = 30
    source: str = "unknown"

    def idle_days(self, today: date | None = None) -> int | None:
        """Days since the last observed activity, derived from the timestamp if needed."""
        if self.days_since_last_activity is not None:
            return self.days_since_last_activity
        if self.last_activity is None:
            return None
        reference = today or datetime.now().date()
        last = self.last_activity.date() if isinstance(self.last_activity, datetime) else self.last_activity
        return max((reference - last).days, 0)


@dataclass(frozen=True)
class Assessment:
    """A detector's verdict about a single resource."""

    category: str
    suggested_action: Action
    severity: Severity
    reason: str
    evidence: tuple[str, ...] = ()
    confidence: str = "medium"
    reclaimable_ratio: Decimal = Decimal("0")
    # Set when the work isn't a simple delete/stop of this exact resource.
    cleanup_hint: str = ""


@dataclass(frozen=True)
class Decision:
    """An assessed resource plus the spending numbers and guardrails that apply."""

    resource: Resource
    action: Action
    severity: Severity
    category: str
    reason: str
    evidence: tuple[str, ...] = ()
    safeguards: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    confidence: str = "medium"
    currency: str = "USD"
    monthly_cost: Decimal = Decimal("0.00")
    reclaimable_ratio: Decimal = Decimal("0")
    observed_days: int = 0
    audit_events: tuple[str, ...] = ()

    @property
    def estimated_monthly_savings(self) -> Decimal:
        """Money at stake each month. Quantized so reports are stable and testable."""
        return money(self.monthly_cost * self.reclaimable_ratio)

    @property
    def requires_approval(self) -> bool:
        return bool(self.approvals)

    @property
    def is_actionable(self) -> bool:
        """True when the watchdog may execute this without further sign-off."""
        return self.action not in {Action.NONE, Action.NOTIFY} and not self.approvals

    @property
    def sort_key(self) -> tuple:
        return (
            _SEVERITY_RANK.get(self.severity, 9),
            _ACTION_RANK.get(self.action, 9),
            -float(self.estimated_monthly_savings),
            self.resource.name.lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form used for ``plan.json`` and ``--format json``."""
        return {
            "resource_id": self.resource.id,
            "resource_name": self.resource.name,
            "resource_type": self.resource.type,
            "resource_group": self.resource.resource_group,
            "subscription_id": self.resource.subscription_id,
            "category": self.category,
            "action": self.action.value,
            "severity": self.severity.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "safeguards": list(self.safeguards),
            "approvals_required": list(self.approvals),
            "currency": self.currency,
            "monthly_cost": float(self.monthly_cost),
            "estimated_monthly_savings": float(self.estimated_monthly_savings),
            "requires_approval": self.requires_approval,
            "actionable": self.is_actionable,
        }


def total_savings(decisions: Iterable[Decision], *, actionable_only: bool = False) -> Decimal:
    """Sum estimated monthly savings across decisions."""
    total = Decimal("0")
    for decision in decisions:
        if actionable_only and not decision.is_actionable:
            continue
        total += decision.estimated_monthly_savings
    return money(total)
