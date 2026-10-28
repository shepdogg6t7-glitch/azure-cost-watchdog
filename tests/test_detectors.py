"""Tests for the detectors. Each case pins one heuristic boundary."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from costwatchdog.detectors import (
    CATEGORY_ALREADY_DEALLOCATED,
    CATEGORY_EMPTY_APP_PLAN,
    CATEGORY_IDLE_AKS,
    CATEGORY_IDLE_HOST_POOL,
    CATEGORY_IDLE_VM,
    CATEGORY_ORPHAN_DISK,
    CATEGORY_ORPHAN_NIC,
    CATEGORY_STALE_SNAPSHOT,
    CATEGORY_UNUSED_APP_PLAN,
    CATEGORY_UNUSED_PUBLIC_IP,
    detect,
    requires_telemetry,
)
from costwatchdog.models import Action
from costwatchdog.policy import Policy

from tests.support import metrics as usage
from tests.support import resource

TODAY = date(2026, 10, 27)


class IdleComputeTests(unittest.TestCase):
    def test_idle_vm_is_flagged_with_high_confidence(self) -> None:
        vm = resource("vm-dev-01")
        assessment = detect(vm, usage(vm.id, cpu_peak=0.6, days_since_last_activity=34), Policy(), TODAY)
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment.category, CATEGORY_IDLE_VM)
        self.assertEqual(assessment.suggested_action, Action.DEALLOCATE)
        self.assertEqual(assessment.confidence, "high")
        self.assertEqual(assessment.reclaimable_ratio, Decimal("1"))
        self.assertTrue(any("CPU peak" in item for item in assessment.evidence))

    def test_idle_vm_with_some_load_is_only_medium_confidence(self) -> None:
        vm = resource("vm-dev-03")
        assessment = detect(vm, usage(vm.id, cpu_peak=1.8, days_since_last_activity=34), Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_IDLE_VM)
        self.assertEqual(assessment.confidence, "medium")

    def test_busy_vm_is_left_alone(self) -> None:
        vm = resource("vm-prod-01")
        self.assertIsNone(detect(vm, usage(vm.id, cpu_peak=62.4, network_in=4_000_000_000), Policy(), TODAY))

    def test_noisy_network_disqualifies_idleness(self) -> None:
        vm = resource("vm-chatty")
        idle_metrics = usage(vm.id, cpu_peak=0.5, network_in=50_000_000, days_since_last_activity=30)
        self.assertIsNone(detect(vm, idle_metrics, Policy(), TODAY))

    def test_recent_activity_disqualifies_idleness(self) -> None:
        vm = resource("vm-recent")
        busy_recently = usage(vm.id, cpu_peak=0.4, days_since_last_activity=3)
        self.assertIsNone(detect(vm, busy_recently, Policy(), TODAY))

    def test_missing_telemetry_yields_no_verdict(self) -> None:
        vm = resource("vm-no-metrics")
        self.assertIsNone(detect(vm, None, Policy(), TODAY))
        self.assertTrue(requires_telemetry(vm))

    def test_deallocated_vm_is_reported_as_already_stopped(self) -> None:
        vm = resource("vm-dev-stopped-01", power_state="VM deallocated")
        assessment = detect(vm, usage(vm.id, cpu_peak=0.0, days_since_last_activity=60), Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_ALREADY_DEALLOCATED)
        self.assertEqual(assessment.suggested_action, Action.NONE)
        self.assertEqual(assessment.reclaimable_ratio, Decimal("0"))

    def test_idle_aks_is_stopped_not_deleted(self) -> None:
        aks = resource("aks-prod-01", "microsoft.containerservice/managedclusters", properties={"agentPoolProfiles": [{}, {}]})
        assessment = detect(aks, usage(aks.id, cpu_peak=3.2, network_in=4_800_000, days_since_last_activity=22), Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_IDLE_AKS)
        self.assertEqual(assessment.suggested_action, Action.STOP)
        # Node compute is reclaimed; persistent disks are not.
        self.assertEqual(assessment.reclaimable_ratio, Decimal("0.9"))

    def test_vmss_uses_its_own_category(self) -> None:
        vmss = resource("vmss-1", "microsoft.compute/virtualmachinescalesets")
        assessment = detect(vmss, usage(vmss.id, cpu_peak=0.2, days_since_last_activity=40), Policy(), TODAY)
        self.assertEqual(assessment.category, "idle-vmss")


class SessionHostTests(unittest.TestCase):
    def test_host_pool_without_connections_gets_a_schedule(self) -> None:
        pool = resource("pool-1", "microsoft.desktopvirtualization/hostpools")
        assessment = detect(pool, usage(pool.id, cpu_peak=1.0, connections=0, days_since_last_activity=30), Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_IDLE_HOST_POOL)
        self.assertEqual(assessment.suggested_action, Action.SCHEDULE_SHUTDOWN)

    def test_host_pool_in_use_is_left_alone(self) -> None:
        pool = resource("pool-2", "microsoft.desktopvirtualization/hostpools")
        self.assertIsNone(detect(pool, usage(pool.id, connections=48), Policy(), TODAY))


class AppServicePlanTests(unittest.TestCase):
    def test_empty_plan_is_an_orphan(self) -> None:
        plan = resource("plan-empty-01", "microsoft.web/serverfarms", properties={"numberOfSites": 0})
        assessment = detect(plan, None, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_EMPTY_APP_PLAN)
        self.assertEqual(assessment.suggested_action, Action.DELETE_ORPHAN)
        self.assertEqual(assessment.confidence, "high")

    def test_idle_plan_with_apps_is_notify_only(self) -> None:
        plan = resource("plan-idle", "microsoft.web/serverfarms", properties={"numberOfSites": 2})
        idle = usage(plan.id, cpu_peak=None, requests=0, days_since_last_activity=45)
        assessment = detect(plan, idle, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_UNUSED_APP_PLAN)
        self.assertEqual(assessment.suggested_action, Action.NOTIFY)

    def test_busy_plan_is_left_alone(self) -> None:
        plan = resource("plan-prod-01", "microsoft.web/serverfarms", properties={"numberOfSites": 4})
        self.assertIsNone(detect(plan, usage(plan.id, cpu_peak=None, requests=2_450_122), Policy(), TODAY))


class OrphanTests(unittest.TestCase):
    def test_unattached_disk_is_an_orphan(self) -> None:
        disk = resource("disk-orphan-01", "microsoft.compute/disks", properties={"diskState": "Unattached"})
        assessment = detect(disk, None, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_ORPHAN_DISK)
        self.assertEqual(assessment.confidence, "high")
        self.assertFalse(requires_telemetry(disk))

    def test_disk_attached_outside_the_subscription_scope_is_skipped(self) -> None:
        # Azure reports no managedBy when the owning VM lives in another subscription;
        # deleting then would detach a live disk.
        disk = resource(
            "disk-weird",
            "microsoft.compute/disks",
            properties={"diskState": "Attached", "managedBy": "/subscriptions/other/virtualMachines/vm-9"},
        )
        self.assertIsNone(detect(disk, None, Policy(), TODAY))

    def test_disks_owned_by_a_managed_cluster_are_skipped(self) -> None:
        disk = resource(
            "disk-aks-node",
            "microsoft.compute/disks",
            resource_group="MC_rg-prod_aks-prod-01_eastus",
            properties={"diskState": "Unattached"},
        )
        self.assertIsNone(detect(disk, None, Policy(), TODAY))

    def test_unused_static_ip_is_an_orphan(self) -> None:
        ip = resource(
            "pip-unused-01",
            "microsoft.network/publicipaddresses",
            properties={"publicIPAllocationMethod": "Static", "ipConfiguration": None},
        )
        assessment = detect(ip, None, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_UNUSED_PUBLIC_IP)
        self.assertEqual(assessment.suggested_action, Action.DELETE_ORPHAN)

    def test_attached_ip_is_left_alone(self) -> None:
        ip = resource(
            "pip-prod-01",
            "microsoft.network/publicipaddresses",
            properties={"publicIPAllocationMethod": "Static", "ipConfiguration": {"id": "/x"}},
        )
        self.assertIsNone(detect(ip, None, Policy(), TODAY))

    def test_dynamic_ip_is_left_alone(self) -> None:
        ip = resource(
            "pip-dynamic",
            "microsoft.network/publicipaddresses",
            properties={"publicIPAllocationMethod": "Dynamic", "ipConfiguration": None},
        )
        self.assertIsNone(detect(ip, None, Policy(), TODAY))

    def test_old_snapshot_is_stale(self) -> None:
        snapshot = resource(
            "snap-old-01",
            "microsoft.compute/snapshots",
            properties={"timeCreated": "2026-01-05T09:12:00Z", "creationData": {"sourceResourceId": "/x/disks/d"}},
        )
        assessment = detect(snapshot, None, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_STALE_SNAPSHOT)
        self.assertTrue(any("295 days old" in item for item in assessment.evidence))

    def test_recent_snapshot_is_kept(self) -> None:
        snapshot = resource("snap-new", "microsoft.compute/snapshots", properties={"timeCreated": "2026-10-01T00:00:00Z"})
        self.assertIsNone(detect(snapshot, None, Policy(), TODAY))

    def test_snapshot_without_a_creation_time_is_kept(self) -> None:
        snapshot = resource("snap-unknown", "microsoft.compute/snapshots", properties={})
        self.assertIsNone(detect(snapshot, None, Policy(), TODAY))

    def test_unattached_nic_is_an_orphan(self) -> None:
        nic = resource("nic-free-01", "microsoft.network/networkinterfaces", properties={"virtualMachine": None})
        assessment = detect(nic, None, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_ORPHAN_NIC)

    def test_attached_nic_is_left_alone(self) -> None:
        nic = resource("nic-used", "microsoft.network/networkinterfaces", properties={"virtualMachine": {"id": "/x"}})
        self.assertIsNone(detect(nic, None, Policy(), TODAY))

    def test_unknown_types_are_not_assessed(self) -> None:
        other = resource("kv-1", "microsoft.keyvault/vaults")
        self.assertIsNone(detect(other, None, Policy(), TODAY))


class IdleWindowTests(unittest.TestCase):
    def test_idle_days_can_be_derived_from_a_timestamp(self) -> None:
        vm = resource("vm-derived")
        derived = usage(vm.id, cpu_peak=0.2, days_since_last_activity=None, last_activity=datetime(2026, 9, 25, 10, 0, 0))
        assessment = detect(vm, derived, Policy(), TODAY)
        self.assertEqual(assessment.category, CATEGORY_IDLE_VM)
        self.assertTrue(any("32 days" in item for item in assessment.evidence))


if __name__ == "__main__":
    unittest.main()
