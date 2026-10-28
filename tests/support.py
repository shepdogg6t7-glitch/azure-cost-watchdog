"""Helpers used across the test modules. Standard library only."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from costwatchdog.models import DAYS_PER_MONTH, Resource, ResourceCostSummary, UsageMetrics

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
INVENTORY = EXAMPLES / "inventory.json"
COST_CSV = EXAMPLES / "cost-export.csv"
METRICS = EXAMPLES / "metrics.json"
POLICY = EXAMPLES / "policy.json"


def resource(
    name: str = "vm-test-01",
    type_: str = "microsoft.compute/virtualmachines",
    *,
    resource_group: str = "rg-dev",
    subscription: str = "00000000-0000-0000-0000-000000000001",
    tags: Mapping[str, Any] | None = None,
    power_state: str = "VM running",
    sku: str = "Standard_D2s_v5",
    properties: Mapping[str, Any] | None = None,
) -> Resource:
    return Resource(
        id=f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/{type_}/{name}",
        name=name,
        type=type_,
        resource_group=resource_group,
        subscription_id=subscription,
        location="eastus",
        tags=dict(tags or {"env": "dev", "owner": "team-platform"}),
        power_state=power_state,
        sku=sku,
        properties=dict(properties or {}),
    )


def metrics(
    resource_id: str,
    *,
    cpu_peak: float | None = 1.0,
    cpu_avg: float | None = 0.4,
    network_in: float | None = 100_000,
    network_out: float | None = 50_000,
    last_activity: datetime | None = None,
    days_since_last_activity: int | None = 30,
    requests: int | None = None,
    connections: int | None = None,
    window_days: int = 30,
) -> UsageMetrics:
    return UsageMetrics(
        resource_id=resource_id,
        cpu_peak_percent=cpu_peak,
        cpu_avg_percent=cpu_avg,
        network_in_bytes_per_day=network_in,
        network_out_bytes_per_day=network_out,
        request_count_30d=requests,
        connections_30d=connections,
        last_activity=last_activity,
        days_since_last_activity=days_since_last_activity,
        window_days=window_days,
        source="test",
    )


def summary(resource_id: str, monthly: float | Decimal, *, observed_days: int = 30) -> ResourceCostSummary:
    """A cost summary whose monthly run rate is exactly ``monthly``."""
    amount = Decimal(str(monthly))
    return ResourceCostSummary(
        resource_id=resource_id.lower(),
        currency="USD",
        total=amount * observed_days / DAYS_PER_MONTH,
        observed_days=observed_days,
        record_count=observed_days,
    )
