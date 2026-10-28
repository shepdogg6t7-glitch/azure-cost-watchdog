"""The only module that talks to Azure.

It is imported lazily by the CLI, so the audit engine, the report renderer and the whole
test suite run without Azure SDKs, credentials or network access.

Everything that mutates a resource goes through :func:`apply_plan`, which re-checks the
plan's own guardrails before acting. Nothing here decides *whether* an action is allowed -
that happens once, in :mod:`costwatchdog.engine`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from costwatchdog.auditlog import AuditLog
from costwatchdog.billing import parse_money
from costwatchdog.models import CostRecord, Resource, UsageMetrics

LOGGER = logging.getLogger(__name__)

# API versions are pinned per resource type because `delete_by_id` requires one. Bump as
# needed; these are the versions generally available at the time of writing.
_API_VERSIONS: dict[str, str] = {
    "microsoft.compute/disks": "2024-03-02",
    "microsoft.compute/snapshots": "2024-03-02",
    "microsoft.network/publicipaddresses": "2024-05-01",
    "microsoft.network/networkinterfaces": "2024-05-01",
    "microsoft.web/serverfarms": "2023-12-01",
}
_DEFAULT_API_VERSION = "2021-04-01"

# Metrics differ per resource type. Container Insights exposes node_* metrics on the
# cluster resource itself; verify names against your cluster before relying on them.
_METRIC_PLANS: dict[str, tuple[str, ...]] = {
    "microsoft.compute/virtualmachines": ("Percentage CPU", "Network In Total", "Network Out Total"),
    "microsoft.compute/virtualmachinescalesets": ("Percentage CPU", "Network In Total", "Network Out Total"),
    "microsoft.containerservice/managedclusters": ("node_cpu_usage_percentage",),
}

#: Approvals that an operator may waive with an explicit apply flag.
_WAIVABLE = {
    "environment=prod": "allow_prod",
    "orphan-deletion-disabled": "allow_orphan_deletion",
    "max-actions-per-run": "always",  # deferred work is simply retried next run
}


class AzureError(RuntimeError):
    """Raised for Azure-side failures the operator needs to see."""


class MissingDependencyError(RuntimeError):
    """Raised when live mode is used without the optional Azure SDKs installed."""


def require_azure_sdk() -> None:
    """Fail early, with an actionable message, when the Azure SDKs are absent."""
    try:
        import azure.identity  # noqa: F401
        import azure.mgmt.resource  # noqa: F401
    except ImportError as error:
        raise MissingDependencyError(
            "this needs the Azure SDKs, which are optional: "
            "pip install azure-identity azure-mgmt-resource azure-mgmt-compute "
            "azure-mgmt-containerservice azure-mgmt-costmanagement azure-mgmt-monitor"
        ) from error


class AzureClients:
    """Thin wrapper over the Azure SDKs used by the watchdog."""

    def __init__(self, subscription_ids: Sequence[str] | None = None, credential: Any = None) -> None:
        self._credential = credential
        self._clients: dict[tuple[str, str], Any] = {}
        if subscription_ids:
            self.subscription_ids = [str(item) for item in subscription_ids]
        else:
            self.subscription_ids = self._discover_subscriptions()

    # -- plumbing -----------------------------------------------------------

    @property
    def credential(self) -> Any:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        return self._credential

    def _client(self, kind: str, subscription_id: str) -> Any:
        key = (kind, subscription_id.lower())
        if key in self._clients:
            return self._clients[key]
        if kind == "resource":
            from azure.mgmt.resource import ResourceManagementClient

            client = ResourceManagementClient(self.credential, subscription_id)
        elif kind == "compute":
            from azure.mgmt.compute import ComputeManagementClient

            client = ComputeManagementClient(self.credential, subscription_id)
        elif kind == "containerservice":
            from azure.mgmt.containerservice import ContainerServiceClient

            client = ContainerServiceClient(self.credential, subscription_id)
        elif kind == "monitor":
            from azure.mgmt.monitor import MonitorManagementClient

            client = MonitorManagementClient(self.credential, subscription_id)
        elif kind == "cost":
            from azure.mgmt.costmanagement import CostManagementClient

            client = CostManagementClient(self.credential)
        else:  # pragma: no cover - programming error
            raise AzureError(f"unknown client kind {kind!r}")
        self._clients[key] = client
        return client

    def _discover_subscriptions(self) -> list[str]:
        from azure.mgmt.resource import SubscriptionClient

        client = SubscriptionClient(self.credential)
        return [subscription.subscription_id for subscription in client.subscriptions.list()]

    # -- inventory ----------------------------------------------------------

    def list_resources(self) -> list[Resource]:
        """List resources with tags across every subscription in scope."""
        resources: list[Resource] = []
        for subscription_id in self.subscription_ids:
            client = self._client("resource", subscription_id)
            for item in client.resources.list(expand="tags"):
                payload = item.as_dict() if hasattr(item, "as_dict") else dict(item)
                resources.append(Resource.from_arm(payload))
        LOGGER.info("inventory: %s resource(s) across %s subscription(s)", len(resources), len(self.subscription_ids))
        return resources

    # -- cost ---------------------------------------------------------------

    def fetch_cost_records(self, days: int = 30) -> list[CostRecord]:
        """Query the Cost Management API for daily actual cost grouped by resource id."""
        from azure.mgmt.costmanagement.models import (
            QueryAggregation,
            QueryDataset,
            QueryDefinition,
            QueryGrouping,
            QueryTimePeriod,
        )

        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        records: list[CostRecord] = []
        for subscription_id in self.subscription_ids:
            client = self._client("cost", subscription_id)
            definition = QueryDefinition(
                type="ActualCost",
                timeframe="Custom",
                time_period=QueryTimePeriod(from_property=start, to=end),
                dataset=QueryDataset(
                    granularity="Daily",
                    aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
                    grouping=[QueryGrouping(type="Dimension", name="ResourceId")],
                ),
            )
            try:
                result = client.query.usage(scope=f"/subscriptions/{subscription_id}", parameters=definition)
            except Exception as error:  # noqa: BLE001 - surface Azure's own message
                raise AzureError(f"cost query failed for {subscription_id}: {error}") from error
            records.extend(_cost_rows_to_records(result, subscription_id))
        LOGGER.info("cost: %s record(s) over %s day(s)", len(records), days)
        return records

    # -- metrics ------------------------------------------------------------

    def fetch_usage_metrics(self, resources: Iterable[Resource], days: int = 30) -> dict[str, UsageMetrics]:
        """Summarize CPU/network telemetry for the resource types that support it."""
        metrics: dict[str, UsageMetrics] = {}
        for resource in resources:
            plan = _METRIC_PLANS.get(resource.kind)
            if plan is None:
                continue
            subscription_id = resource.subscription_id
            if not subscription_id:
                continue
            try:
                series = self._metric_series(subscription_id, resource.id, plan, days)
            except Exception as error:  # noqa: BLE001 - one bad resource must not stop the audit
                LOGGER.warning("metrics unavailable for %s: %s", resource.id, error)
                continue
            cpu_peak, cpu_avg = _cpu_stats(series.get(plan[0], []))
            network_in = _daily_bytes(series.get("Network In Total", []))
            network_out = _daily_bytes(series.get("Network Out Total", []))
            metrics[resource.id.lower()] = UsageMetrics(
                resource_id=resource.id,
                cpu_peak_percent=cpu_peak,
                cpu_avg_percent=cpu_avg,
                network_in_bytes_per_day=network_in,
                network_out_bytes_per_day=network_out,
                last_activity=self._last_administrative_activity(subscription_id, resource.id, days),
                window_days=days,
                source="azure-monitor",
            )
        return metrics

    def _metric_series(
        self, subscription_id: str, resource_id: str, names: Sequence[str], days: int
    ) -> dict[str, list[tuple[datetime, float]]]:
        client = self._client("monitor", subscription_id)
        timespan = f"{_iso(days)}/{_iso(0)}"
        result = client.metrics.list(
            resource_id,
            timespan=timespan,
            interval="PT1H",
            metricnames=",".join(names),
            aggregation="Average,Maximum",
        )
        series: dict[str, list[tuple[datetime, float]]] = {}
        for metric in getattr(result, "value", []) or []:
            points: list[tuple[datetime, float]] = []
            for timeseries in getattr(metric, "timeseries", []) or []:
                for point in getattr(timeseries, "data", []) or []:
                    value = point.maximum if point.maximum is not None else point.average
                    if value is None:
                        continue
                    points.append((point.time_stamp, float(value)))
            series[getattr(metric, "name", {}).get("value", "")] = points
        return series

    def _last_administrative_activity(self, subscription_id: str, resource_id: str, days: int) -> datetime | None:
        """Best-effort "when was this last touched" from the activity log."""
        client = self._client("monitor", subscription_id)
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            events = client.activity_logs.list(
                filter=f"eventTimestamp ge '{start}' and resourceId eq '{resource_id}'",
                select="eventTimestamp,operationName",
            )
        except Exception as error:  # noqa: BLE001 - telemetry is optional
            LOGGER.debug("activity log unavailable for %s: %s", resource_id, error)
            return None
        latest: datetime | None = None
        for event in events:
            timestamp = getattr(event, "event_timestamp", None)
            if timestamp is None:
                continue
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
            if latest is None or timestamp > latest:
                latest = timestamp
        return latest

    # -- execution primitives ----------------------------------------------

    def deallocate(self, resource_id: str) -> None:
        resource_group, name = _group_and_name(resource_id)
        subscription_id = _segment(resource_id, "subscriptions")
        kind = _type_of(resource_id)
        client = self._client("compute", subscription_id)
        if kind == "microsoft.compute/virtualmachinescalesets":
            client.virtual_machine_scale_sets.begin_deallocate(resource_group, name).wait()
            return
        if kind == "microsoft.compute/virtualmachines":
            client.virtual_machines.begin_deallocate(resource_group, name).wait()
            return
        raise AzureError(f"deallocate is not supported for {resource_id}")

    def stop(self, resource_id: str) -> None:
        if "microsoft.containerservice/managedclusters" in resource_id.lower():
            resource_group, name = _group_and_name(resource_id)
            subscription_id = _segment(resource_id, "subscriptions")
            client = self._client("containerservice", subscription_id)
            client.managed_clusters.begin_stop(resource_group, name).wait()
            return
        raise AzureError(f"stop is not supported for {resource_id}")

    def delete_resource(self, resource_id: str) -> None:
        """Delete an orphaned resource by id (disk, snapshot, public IP, NIC, empty plan)."""
        subscription_id = _segment(resource_id, "subscriptions")
        api_version = _API_VERSIONS.get(_type_of(resource_id), _DEFAULT_API_VERSION)
        client = self._client("resource", subscription_id)
        client.resources.begin_delete_by_id(resource_id, api_version=api_version).wait()


def apply_plan(
    plan: Mapping[str, Any],
    *,
    dry_run: bool = True,
    allow_prod: bool = False,
    allow_orphan_deletion: bool = False,
    limit: int | None = None,
    audit_log: AuditLog | None = None,
) -> list[dict[str, Any]]:
    """Execute the actionable decisions in a plan, honouring its own guardrails.

    The plan is the contract: produce it with the policy you intend to run. Decisions that
    carry an approval requirement are skipped unless the matching ``--allow-*`` flag is
    passed. ``environment=prod`` and orphan deletion are the two waivable gates; missing
    tags and low-confidence deletes must be fixed in the data, not force-flagged.
    """
    decisions = [item for item in plan.get("decisions", []) if isinstance(item, Mapping)]
    waivers = {"allow_prod": allow_prod, "allow_orphan_deletion": allow_orphan_deletion}
    clients: AzureClients | None = None
    results: list[dict[str, Any]] = []
    executed = 0

    for decision in decisions:
        action = str(decision.get("action", "none"))
        resource_id = str(decision.get("resource_id", ""))
        entry: dict[str, Any] = {
            "resource_id": resource_id,
            "resource_name": str(decision.get("resource_name", "")),
            "action": action,
            "status": "skipped",
            "detail": "",
        }
        if action in {"none", "notify"}:
            entry["detail"] = "notify-only; nothing to execute"
            results.append(entry)
            continue

        blocking = [
            approval
            for approval in decision.get("approvals_required", []) or []
            if not _waived(str(approval), waivers)
        ]
        if blocking:
            entry["detail"] = "requires approval: " + ", ".join(blocking)
            results.append(entry)
            continue
        if limit is not None and executed >= limit:
            entry["detail"] = f"limit of {limit} action(s) for this run reached"
            results.append(entry)
            continue

        if dry_run:
            entry["status"] = "skipped"
            entry["detail"] = "dry-run: would execute"
            results.append(entry)
            continue

        if clients is None:
            require_azure_sdk()
            clients = AzureClients()
        try:
            if action == "deallocate":
                clients.deallocate(resource_id)
            elif action == "stop":
                clients.stop(resource_id)
            elif action == "delete-orphan":
                clients.delete_resource(resource_id)
            else:
                entry["detail"] = f"no executor for action {action!r}"
                results.append(entry)
                continue
        except Exception as error:  # noqa: BLE001 - keep going through the plan
            entry["status"] = "failed"
            entry["detail"] = str(error)
            _record(audit_log, "action-failed", entry)
            results.append(entry)
            continue

        executed += 1
        entry["status"] = "executed"
        _record(audit_log, "action-executed", entry)
        results.append(entry)

    return results


def _waived(approval: str, waivers: Mapping[str, bool]) -> bool:
    flag = _WAIVABLE.get(approval)
    if flag is None:
        return False
    if flag == "always":
        return True
    return bool(waivers.get(flag, False))


def _record(audit_log: AuditLog | None, event: str, payload: Mapping[str, Any]) -> None:
    if audit_log is not None:
        audit_log.record(event, **dict(payload))


def _cost_rows_to_records(result: Any, subscription_id: str) -> list[CostRecord]:
    """Map a Cost Management query response (columns + rows) onto CostRecord objects."""
    columns = [str(getattr(column, "name", column)) for column in getattr(result, "columns", []) or []]
    rows = getattr(result, "rows", []) or []

    def index_of(*names: str) -> int | None:
        for name in names:
            for position, column in enumerate(columns):
                if column.lower() == name.lower():
                    return position
        return None

    cost_index = index_of("Cost", "PreTaxCost", "CostInBillingCurrency")
    if cost_index is None:
        raise AzureError(f"cost response had no recognizable cost column (got {columns})")
    id_index = index_of("ResourceId")
    currency_index = index_of("BillingCurrency", "Currency")
    usage_date_index = index_of("UsageDate", "UsageDateTime", "Date", "BillingPeriodStartDate")

    records: list[CostRecord] = []
    for row in rows:
        if row is None or len(row) <= cost_index:
            continue
        cost = parse_money(row[cost_index])
        if cost is None:
            continue
        resource_id = str(row[id_index]) if id_index is not None and row[id_index] else ""
        records.append(
            CostRecord(
                resource_id=resource_id,
                cost=cost,
                currency=str(row[currency_index]).upper() if currency_index is not None and row[currency_index] else "USD",
                subscription_id=subscription_id,
                meter_category="CostManagement",
                charge_type="Usage",
                frequency="Usage",
                pricing_model="Actual",
                date=_row_date(row, usage_date_index),
            )
        )
    return records


def _row_date(row: Sequence[Any], index: int | None) -> date | None:
    if index is None or len(row) <= index:
        return None
    raw = row[index]
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw)
    digits = text.replace("-", "").replace("/", "").split("T")[0]
    if len(digits) == 8 and digits.isdigit():
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    return None


def _cpu_stats(points: Sequence[tuple[datetime, float]]) -> tuple[float | None, float | None]:
    values = [value for _, value in points if value is not None]
    if not values:
        return None, None
    return max(values), sum(values) / len(values)


def _daily_bytes(points: Sequence[tuple[datetime, float]]) -> float | None:
    """Network metrics arrive as hourly totals; average them and scale to a day."""
    values = [value for _, value in points if value is not None]
    if not values:
        return None
    return sum(values) / len(values) * 24


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group_and_name(resource_id: str) -> tuple[str, str]:
    return _segment(resource_id, "resourcegroups"), resource_id.rstrip("/").split("/")[-1]


def _segment(resource_id: str, marker: str) -> str:
    parts = [part for part in str(resource_id).split("/") if part]
    lowered = [part.lower() for part in parts]
    if marker in lowered:
        position = lowered.index(marker)
        if position + 1 < len(parts):
            return parts[position + 1]
    return ""


def _type_of(resource_id: str) -> str:
    """`.../providers/Microsoft.Compute/disks/x` -> `microsoft.compute/disks`."""
    lowered = str(resource_id).lower()
    if "/providers/" not in lowered:
        return ""
    parts = [part for part in lowered.split("/providers/")[-1].split("/") if part]
    return "/".join(parts[:2])
