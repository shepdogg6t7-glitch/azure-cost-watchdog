"""Detectors: does this resource look idle, orphaned, or fine?

Each detector is a pure function of (resource, metrics, policy) so the heuristics can be
tested against fixtures instead of a live subscription. Detectors only *assess*; the
guardrails and execution lives in :mod:`costwatchdog.engine`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

from costwatchdog.models import Action, Assessment, Resource, Severity, UsageMetrics
from costwatchdog.policy import Policy

CATEGORY_IDLE_VM = "idle-vm"
CATEGORY_IDLE_VMSS = "idle-vmss"
CATEGORY_IDLE_AKS = "idle-aks"
CATEGORY_IDLE_HOST_POOL = "idle-session-host-pool"
CATEGORY_EMPTY_APP_PLAN = "empty-app-service-plan"
CATEGORY_UNUSED_APP_PLAN = "unused-app-service-plan"
CATEGORY_ORPHAN_DISK = "orphan-disk"
CATEGORY_UNUSED_PUBLIC_IP = "unused-public-ip"
CATEGORY_STALE_SNAPSHOT = "stale-snapshot"
CATEGORY_ORPHAN_NIC = "orphan-nic"
CATEGORY_ALREADY_DEALLOCATED = "already-deallocated"
CATEGORY_NO_WASTE = "no-waste"

#: Disks and NICs in these resource groups belong to a platform (AKS creates MC_*).
_PLATFORM_MANAGED_PREFIXES = ("mc_", "mcs_")

#: Types whose verdict depends on usage telemetry. For these, a missing metrics row means
#: "cannot judge" (reported as unclassified) rather than "nothing to do". Structure-only
#: types (disks, snapshots, IPs, NICs) are judged from their own properties.
TELEMETRY_RESOURCE_TYPES = frozenset(
    {
        "microsoft.compute/virtualmachines",
        "microsoft.compute/virtualmachinescalesets",
        "microsoft.containerservice/managedclusters",
        "microsoft.desktopvirtualization/hostpools",
        "microsoft.web/serverfarms",
    }
)


def requires_telemetry(resource: Resource) -> bool:
    """True when the detector for this type cannot reach a verdict without metrics."""
    return resource.kind in TELEMETRY_RESOURCE_TYPES


def _properties(resource: Resource) -> dict[str, Any]:
    return dict(resource.properties) if isinstance(resource.properties, Mapping) else {}


def _friendly(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _cpu_evidence(metrics: UsageMetrics, policy: Policy, window: int) -> list[str]:
    evidence: list[str] = []
    if metrics.cpu_peak_percent is not None:
        evidence.append(
            f"CPU peak hourly average {metrics.cpu_peak_percent:.1f}% over {window}d "
            f"(idle threshold {policy.cpu_idle_peak_percent:.1f}%)"
        )
    if metrics.cpu_avg_percent is not None:
        evidence.append(f"CPU average {metrics.cpu_avg_percent:.1f}% over {window}d")
    if metrics.network_in_bytes_per_day is not None:
        evidence.append(f"Network in {_megabytes(metrics.network_in_bytes_per_day)}/day")
    if metrics.network_out_bytes_per_day is not None:
        evidence.append(f"Network out {_megabytes(metrics.network_out_bytes_per_day)}/day")
    return evidence


def _megabytes(bytes_per_day: float) -> str:
    return f"{bytes_per_day / 1_048_576:,.2f} MB"


def _activity_evidence(metrics: UsageMetrics, idle_days: int | None, policy: Policy) -> list[str]:
    if idle_days is None:
        return [f"no activity timestamp in telemetry (requires {policy.min_idle_days}d of quiet)"]
    return [f"no meaningful activity for {idle_days} days (requires {policy.min_idle_days}d)"]


def _is_idle(metrics: UsageMetrics | None, policy: Policy, today: date) -> tuple[bool, int | None, list[str]]:
    """Shared idle test for compute-shaped resources."""
    if metrics is None:
        return False, None, []
    idle_days = metrics.idle_days(today)
    evidence: list[str] = []
    if metrics.cpu_peak_percent is None:
        return False, idle_days, ["no CPU telemetry in the monitoring window"]
    evidence.extend(_cpu_evidence(metrics, policy, metrics.window_days))
    evidence.extend(_activity_evidence(metrics, idle_days, policy))
    quiet_cpu = metrics.cpu_peak_percent <= policy.cpu_idle_peak_percent
    quiet_network = (
        metrics.network_in_bytes_per_day is None
        or metrics.network_in_bytes_per_day <= policy.idle_network_bytes_per_day
    )
    long_enough_idle = idle_days is None or idle_days >= policy.min_idle_days
    return (quiet_cpu and quiet_network and long_enough_idle), idle_days, evidence


def _confidence(metrics: UsageMetrics, idle_days: int | None) -> str:
    if metrics.cpu_peak_percent is None:
        return "low"
    near_zero = metrics.cpu_peak_percent <= 1.0
    quiet_net = metrics.network_in_bytes_per_day is not None and metrics.network_in_bytes_per_day < 1_048_576
    if idle_days is None:
        return "low"
    if near_zero and quiet_net:
        return "high"
    return "medium"


# --------------------------------------------------------------------------- #
# Compute-shaped resources
# --------------------------------------------------------------------------- #


def detect_idle_compute(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    kind = resource.kind
    idle, idle_days, evidence = _is_idle(metrics, policy, today)
    if resource.is_deallocated():
        return Assessment(
            category=CATEGORY_ALREADY_DEALLOCATED,
            suggested_action=Action.NONE,
            severity=Severity.INFO,
            reason="Resource is already stopped/deallocated; compute spend is not accruing",
            evidence=(f"powerState={resource.power_state}",),
            confidence="high",
            reclaimable_ratio=Decimal("0"),
        )
    if metrics is None or not idle:
        return None
    if kind == "microsoft.containerservice/managedclusters":
        nodes = _properties(resource).get("agentPoolProfiles") or []
        return Assessment(
            category=CATEGORY_IDLE_AKS,
            suggested_action=Action.STOP,
            severity=Severity.HIGH,
            reason="AKS cluster is idle: node pools keep billing while no workload uses them",
            evidence=tuple(evidence)
            + (f"{len(nodes)} agent pool(s) present; stopping the cluster deallocates nodes",)
            + ("node OS disks keep billing while stopped",),
            confidence=_confidence(metrics, idle_days),
            # Node compute is reclaimed; persistent disks are not.
            reclaimable_ratio=Decimal("0.9"),
            cleanup_hint="az aks stop",
        )
    category = CATEGORY_IDLE_VMSS if kind.endswith("virtualmachinescalesets") else CATEGORY_IDLE_VM
    return Assessment(
        category=category,
        suggested_action=Action.DEALLOCATE,
        severity=Severity.HIGH,
        reason="Resource is idle: deallocating stops compute billing while keeping the disks",
        evidence=tuple(evidence) + ("deallocating preserves disks and configuration",),
        confidence=_confidence(metrics, idle_days),
        reclaimable_ratio=Decimal("1"),
    )


def detect_idle_host_pool(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    if metrics is None:
        return None
    connections = metrics.connections_30d
    idle_days = metrics.idle_days(today)
    if connections is None and idle_days is None:
        return None
    if connections is not None and connections > 0:
        return None
    if idle_days is not None and idle_days < policy.min_idle_days:
        return None
    evidence = [
        f"no user connections in {metrics.window_days}d"
        if connections is not None
        else "no connection telemetry available"
    ]
    evidence.extend(_activity_evidence(metrics, idle_days, policy))
    evidence.append("session hosts bill per running hour, including out of hours")
    return Assessment(
        category=CATEGORY_IDLE_HOST_POOL,
        suggested_action=Action.SCHEDULE_SHUTDOWN,
        severity=Severity.MEDIUM,
        reason="Session host pool has no active users; host VMs only need to run during business hours",
        evidence=tuple(evidence),
        confidence="medium" if connections is not None else "low",
        reclaimable_ratio=Decimal("0.6"),
        cleanup_hint="scale session hosts to zero outside business hours",
    )


def detect_app_service_plan(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    properties = _properties(resource)
    sites = properties.get("numberOfSites")
    requests = metrics.request_count_30d if metrics else None
    if isinstance(sites, int) and sites == 0:
        return Assessment(
            category=CATEGORY_EMPTY_APP_PLAN,
            suggested_action=Action.DELETE_ORPHAN,
            severity=Severity.MEDIUM,
            reason="App Service plan hosts no apps but keeps billing for its reserved capacity",
            evidence=("numberOfSites=0", f"sku={resource.sku or 'unknown'}"),
            confidence="high",
            reclaimable_ratio=Decimal("1"),
            cleanup_hint="delete the empty plan",
        )
    if requests is not None and requests == 0 and (metrics is None or (metrics.idle_days(today) or 0) >= policy.min_idle_days):
        return Assessment(
            category=CATEGORY_UNUSED_APP_PLAN,
            suggested_action=Action.NOTIFY,
            severity=Severity.MEDIUM,
            reason="Plan receives no requests; likely redundant after a migration",
            evidence=(f"{sites if sites is not None else 'unknown'} app(s) attached", "no requests in 30d"),
            confidence="medium",
            reclaimable_ratio=Decimal("0.5"),
            cleanup_hint="consolidate the apps onto a shared plan",
        )
    return None


def detect_orphan_disk(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    properties = _properties(resource)
    if resource.resource_group.lower().startswith(_PLATFORM_MANAGED_PREFIXES):
        return None
    attached_to = str(properties.get("managedBy") or "").strip()
    disk_state = str(properties.get("diskState") or "").strip().lower()
    if attached_to:
        return None
    explicitly_unattached = disk_state == "unattached"
    if not explicitly_unattached and disk_state not in {"", "ready to upload", "available"}:
        return None
    size = properties.get("diskSizeGB")
    evidence = [
        f"diskState={properties.get('diskState') or 'unknown'}",
        f"managedBy={attached_to or 'none'}",
    ]
    if size is not None:
        evidence.append(f"size {size} GiB")
    evidence.append("unattached managed disks bill at full provisioned size")
    return Assessment(
        category=CATEGORY_ORPHAN_DISK,
        suggested_action=Action.DELETE_ORPHAN,
        severity=Severity.MEDIUM,
        reason="Managed disk is not attached to any VM (or is not attached in this subscription)",
        evidence=tuple(evidence),
        confidence="high" if explicitly_unattached else "medium",
        reclaimable_ratio=Decimal("1"),
        cleanup_hint="verify with the owning team, then delete the disk",
    )


def detect_unused_public_ip(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    properties = _properties(resource)
    if properties.get("ipConfiguration"):
        return None
    method = str(properties.get("publicIPAllocationMethod") or properties.get("publicIPAddressVersion") or "").lower()
    if "static" not in method:
        # Dynamic addresses are only billed while allocated to a running resource.
        return None
    return Assessment(
        category=CATEGORY_UNUSED_PUBLIC_IP,
        suggested_action=Action.DELETE_ORPHAN,
        severity=Severity.MEDIUM,
        reason="Static public IP is not associated with any resource but bills hourly",
        evidence=(
            "ipConfiguration=none",
            f"allocationMethod={properties.get('publicIPAllocationMethod') or 'Static'}",
            "static public IPs bill whether or not they are attached",
        ),
        confidence="high",
        reclaimable_ratio=Decimal("1"),
        cleanup_hint="confirm nothing external points at the address, then delete it",
    )


def detect_stale_snapshot(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    properties = _properties(resource)
    created_raw = properties.get("timeCreated") or properties.get("creationTime")
    created = _parse_timestamp(created_raw)
    if created is None:
        return None
    created_date = created.date()
    age_days = (today - created_date).days
    if age_days < policy.stale_snapshot_days:
        return None
    source = ""
    creation_data = properties.get("creationData")
    if isinstance(creation_data, Mapping):
        source = str(creation_data.get("sourceResourceId") or "")
    evidence = [
        f"created {created_date.isoformat()} ({age_days} days old, threshold {policy.stale_snapshot_days}d)",
        "snapshots bill for stored data for as long as they exist",
    ]
    if source:
        evidence.append(f"source disk {source}")
    return Assessment(
        category=CATEGORY_STALE_SNAPSHOT,
        suggested_action=Action.DELETE_ORPHAN,
        severity=Severity.INFO,
        reason="Snapshot is older than the retention policy window",
        evidence=tuple(evidence),
        confidence="high",
        reclaimable_ratio=Decimal("1"),
        cleanup_hint="confirm the retention requirement, then delete the snapshot",
    )


def detect_orphan_nic(
    resource: Resource, metrics: UsageMetrics | None, policy: Policy, today: date
) -> Assessment | None:
    properties = _properties(resource)
    if properties.get("virtualMachine"):
        return None
    return Assessment(
        category=CATEGORY_ORPHAN_NIC,
        suggested_action=Action.DELETE_ORPHAN,
        severity=Severity.INFO,
        reason="Network interface is not attached to a VM",
        evidence=("virtualMachine=none", "NICs themselves are free, but they leak quota and hide cleanup work"),
        confidence="high",
        reclaimable_ratio=Decimal("1"),
        cleanup_hint="delete the interface after confirming nothing references it",
    )


_DETECTORS = {
    "microsoft.compute/virtualmachines": detect_idle_compute,
    "microsoft.compute/virtualmachinescalesets": detect_idle_compute,
    "microsoft.containerservice/managedclusters": detect_idle_compute,
    "microsoft.desktopvirtualization/hostpools": detect_idle_host_pool,
    "microsoft.web/serverfarms": detect_app_service_plan,
    "microsoft.compute/disks": detect_orphan_disk,
    "microsoft.network/publicipaddresses": detect_unused_public_ip,
    "microsoft.compute/snapshots": detect_stale_snapshot,
    "microsoft.network/networkinterfaces": detect_orphan_nic,
}


def detect(
    resource: Resource,
    metrics: UsageMetrics | None,
    policy: Policy,
    today: date | None = None,
) -> Assessment | None:
    """Dispatch to the detector for this resource type.

    Returns ``None`` when the resource looks healthy, when there is no detector for the
    type, or when telemetry is too thin to judge (the engine reports that separately).
    """
    detector = _DETECTORS.get(resource.kind)
    if detector is None:
        return None
    reference = today or datetime.utcnow().date()
    return detector(resource, metrics, policy, reference)


def _parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.split("+")[0], fmt)
        except ValueError:
            continue
    return None
