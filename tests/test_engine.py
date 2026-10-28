"""Tests for the decision engine - the module that holds the safety rules."""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from costwatchdog.engine import (
    GUARD_DENIED_SCOPE,
    GUARD_MISSING_OWNER_TAG,
    GUARD_ORPHAN_DELETION_DISABLED,
    GUARD_PROD_ENVIRONMENT,
    GUARD_PROTECTED_TAG,
    GUARD_RATE_LIMIT,
    AuditResult,
    evaluate,
    suggest_command,
)
from costwatchdog.models import Action
from costwatchdog.policy import Policy

from tests.support import metrics as usage
from tests.support import resource, summary

TODAY = date(2026, 10, 27)


def audit(resources, summaries, metrics_map=None, policy=None):
    return evaluate(
        resources,
        summaries,
        metrics=metrics_map or {},
        policy=policy or Policy(),
        today=TODAY,
    )


class ProtectedTagTests(unittest.TestCase):
    def test_protected_resource_is_reported_but_never_actioned(self) -> None:
        vm = resource("vm-dev-02", tags={"env": "dev", "owner": "team", "costwatchdog:protected": "true"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.5, days_since_last_activity=40)}
        result = audit([vm], {vm.id.lower(): summary(vm.id, 70.11)}, metrics_map)

        decision = result.decisions[0]
        self.assertEqual(decision.action, Action.NOTIFY)
        self.assertFalse(decision.is_actionable)
        self.assertIn(f"{GUARD_PROTECTED_TAG}:costwatchdog:protected", decision.safeguards)
        # The money at stake is still reported, so it stays visible.
        self.assertEqual(decision.estimated_monthly_savings, Decimal("70.11"))


class DeniedScopeTests(unittest.TestCase):
    def test_denied_resource_group_downgrades_to_notify(self) -> None:
        vm = resource("vm-legacy-01", resource_group="rg-legacy")
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.3, days_since_last_activity=45)}
        result = audit(
            [vm],
            {vm.id.lower(): summary(vm.id, 70.11)},
            metrics_map,
            Policy(denied_resource_groups=("rg-legacy",)),
        )
        self.assertEqual(result.decisions[0].action, Action.NOTIFY)
        self.assertIn(GUARD_DENIED_SCOPE, result.decisions[0].safeguards)


class ProductionTests(unittest.TestCase):
    def test_prod_requires_approval_by_default(self) -> None:
        vm = resource("vm-prod-01", resource_group="rg-prod", tags={"env": "prod", "owner": "team-core"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.4, days_since_last_activity=40)}
        result = audit([vm], {vm.id.lower(): summary(vm.id, 140.26)}, metrics_map)
        decision = result.decisions[0]
        self.assertEqual(decision.action, Action.DEALLOCATE)
        self.assertTrue(decision.requires_approval)
        self.assertIn(GUARD_PROD_ENVIRONMENT, decision.approvals)
        self.assertEqual(result.pending_monthly_savings, Decimal("140.26"))

    def test_allow_prod_makes_it_actionable(self) -> None:
        vm = resource("vm-prod-01", resource_group="rg-prod", tags={"env": "prod", "owner": "team-core"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.4, days_since_last_activity=40)}
        result = audit(
            [vm],
            {vm.id.lower(): summary(vm.id, 140.26)},
            metrics_map,
            Policy(allow_prod_actions=True),
        )
        self.assertTrue(result.decisions[0].is_actionable)
        self.assertEqual(result.actionable_monthly_savings, Decimal("140.26"))


class MissingMetadataTests(unittest.TestCase):
    def test_missing_owner_tag_blocks_execution(self) -> None:
        vm = resource("vm-ownerless", tags={"env": "dev"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.4, days_since_last_activity=40)}
        result = audit([vm], {vm.id.lower(): summary(vm.id, 60.00)}, metrics_map)
        self.assertIn(GUARD_MISSING_OWNER_TAG, result.decisions[0].approvals)

    def test_missing_environment_tag_blocks_execution(self) -> None:
        vm = resource("vm-untagged", tags={"owner": "team"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.4, days_since_last_activity=40)}
        result = audit([vm], {vm.id.lower(): summary(vm.id, 60.00)}, metrics_map)
        self.assertIn("missing-environment-tag", result.decisions[0].approvals)

    def test_require_owner_tag_can_be_relaxed(self) -> None:
        vm = resource("vm-ownerless", tags={"env": "dev"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.4, days_since_last_activity=40)}
        result = audit(
            [vm],
            {vm.id.lower(): summary(vm.id, 60.00)},
            metrics_map,
            Policy(require_owner_tag=False),
        )
        self.assertTrue(result.decisions[0].is_actionable)


class OrphanDeletionTests(unittest.TestCase):
    def _disk(self):
        return resource(
            "disk-orphan-01",
            "microsoft.compute/disks",
            properties={"diskState": "Unattached"},
        )

    def test_deletion_is_opt_in(self) -> None:
        disk = self._disk()
        result = audit([disk], {disk.id.lower(): summary(disk.id, 19.72)})
        decision = result.decisions[0]
        self.assertEqual(decision.action, Action.NOTIFY)
        self.assertIn(GUARD_ORPHAN_DELETION_DISABLED, decision.safeguards)

    def test_deletion_runs_when_enabled_and_confidence_is_high(self) -> None:
        disk = self._disk()
        result = audit(
            [disk],
            {disk.id.lower(): summary(disk.id, 19.72)},
            policy=Policy(allow_orphan_deletion=True),
        )
        self.assertEqual(result.decisions[0].action, Action.DELETE_ORPHAN)
        self.assertTrue(result.decisions[0].is_actionable)


class ThresholdTests(unittest.TestCase):
    def test_cheap_waste_is_dropped(self) -> None:
        ip = resource(
            "pip-tiny",
            "microsoft.network/publicipaddresses",
            properties={"publicIPAllocationMethod": "Static", "ipConfiguration": None},
        )
        result = audit([ip], {ip.id.lower(): summary(ip.id, 3.65)})
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.dropped_below_threshold, 1)

    def test_threshold_is_configurable(self) -> None:
        ip = resource(
            "pip-tiny",
            "microsoft.network/publicipaddresses",
            properties={"publicIPAllocationMethod": "Static", "ipConfiguration": None},
        )
        result = audit(
            [ip],
            {ip.id.lower(): summary(ip.id, 3.65)},
            policy=Policy(allow_orphan_deletion=True, min_monthly_savings=Decimal("1")),
        )
        self.assertEqual(len(result.decisions), 1)


class RateLimitTests(unittest.TestCase):
    def test_actions_beyond_the_cap_are_deferred_not_dropped(self) -> None:
        resources = []
        summaries = {}
        metrics_map = {}
        for index in range(4):
            vm = resource(f"vm-{index}")
            resources.append(vm)
            summaries[vm.id.lower()] = summary(vm.id, 100 + index)
            metrics_map[vm.id.lower()] = usage(vm.id, cpu_peak=0.5, days_since_last_activity=40)

        result = audit(resources, summaries, metrics_map, Policy(max_actions_per_run=2))

        self.assertEqual(len(result.decisions), 4)
        self.assertEqual(len(result.actionable), 2)
        self.assertEqual(len(result.pending_approval), 2)
        for decision in result.pending_approval:
            self.assertIn(GUARD_RATE_LIMIT, decision.approvals)

    def test_deferred_work_has_the_highest_savings_executed_first(self) -> None:
        resources = []
        summaries = {}
        metrics_map = {}
        for index, amount in enumerate((100, 400, 200)):
            vm = resource(f"vm-{index}")
            resources.append(vm)
            summaries[vm.id.lower()] = summary(vm.id, amount)
            metrics_map[vm.id.lower()] = usage(vm.id, cpu_peak=0.5, days_since_last_activity=40)

        result = audit(resources, summaries, metrics_map, Policy(max_actions_per_run=1))
        self.assertEqual(result.actionable[0].monthly_cost, Decimal("400.00"))


class UnclassifiedTests(unittest.TestCase):
    def test_expensive_resource_without_telemetry_is_reported(self) -> None:
        vm = resource("vm-no-metrics-01")
        result = audit([vm], {vm.id.lower(): summary(vm.id, 70.11)})
        decision = result.decisions[0]
        self.assertEqual(decision.category, "unclassified")
        self.assertEqual(decision.action, Action.NOTIFY)
        self.assertEqual(decision.reclaimable_ratio, Decimal("0"))

    def test_cheap_resource_without_telemetry_is_ignored(self) -> None:
        vm = resource("vm-tiny")
        result = audit([vm], {vm.id.lower(): summary(vm.id, 10.00)})
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.healthy, 1)

    def test_healthy_resource_is_counted_not_reported(self) -> None:
        vm = resource("vm-prod-busy", tags={"env": "prod", "owner": "team-core"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=62.4)}
        result = audit([vm], {vm.id.lower(): summary(vm.id, 140.26)}, metrics_map)
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.healthy, 1)

    def test_structure_only_types_are_not_called_unclassified(self) -> None:
        disk = resource("disk-attached", "microsoft.compute/disks", properties={"managedBy": "/x/vm-1"})
        result = audit([disk], {disk.id.lower(): summary(disk.id, 90.00)})
        self.assertEqual(result.decisions, [])
        self.assertEqual(result.healthy, 1)

    def test_notify_can_be_disabled(self) -> None:
        vm = resource("vm-no-metrics-01")
        result = audit([vm], {vm.id.lower(): summary(vm.id, 70.11)}, policy=Policy(notify_unclassified=False))
        self.assertEqual(result.decisions, [])

    def test_unmonitored_types_are_skipped(self) -> None:
        vault = resource("kv-1", "microsoft.keyvault/vaults")
        result = audit([vault], {vault.id.lower(): summary(vault.id, 900.00)})
        self.assertEqual(result.skipped_types, 1)
        self.assertEqual(result.decisions, [])


class RankingAndSummaryTests(unittest.TestCase):
    def test_decisions_are_ranked_by_severity_then_savings(self) -> None:
        idle_vm = resource("vm-idle")
        disk = resource("disk-orphan-01", "microsoft.compute/disks", properties={"diskState": "Unattached"})
        metrics_map = {idle_vm.id.lower(): usage(idle_vm.id, cpu_peak=0.5, days_since_last_activity=40)}
        result = audit(
            [disk, idle_vm],
            {idle_vm.id.lower(): summary(idle_vm.id, 140.26), disk.id.lower(): summary(disk.id, 500.00)},
            metrics_map,
            Policy(allow_orphan_deletion=True),
        )
        # A high-severity deallocate outranks a medium-severity larger deletion.
        self.assertEqual(result.decisions[0].resource.name, "vm-idle")

    def test_totals_and_counts(self) -> None:
        idle_vm = resource("vm-idle")
        busy_vm = resource("vm-busy")
        metrics_map = {
            idle_vm.id.lower(): usage(idle_vm.id, cpu_peak=0.5, days_since_last_activity=40),
            busy_vm.id.lower(): usage(busy_vm.id, cpu_peak=70.0),
        }
        result = audit(
            [idle_vm, busy_vm],
            {idle_vm.id.lower(): summary(idle_vm.id, 100.00), busy_vm.id.lower(): summary(busy_vm.id, 200.00)},
            metrics_map,
        )
        self.assertIsInstance(result, AuditResult)
        self.assertEqual(result.resources_considered, 2)
        self.assertEqual(result.total_monthly_cost, Decimal("300.00"))
        self.assertEqual(result.identified_monthly_waste, Decimal("100.00"))
        self.assertEqual(result.actionable_monthly_savings, Decimal("100.00"))
        self.assertEqual(result.healthy, 1)
        self.assertEqual(result.observed_days, 30)
        payload = result.to_dict()
        self.assertEqual(payload["schema"], "costwatchdog/plan/v1")
        self.assertEqual(payload["summary"]["total_monthly_cost"], 300.0)


class SuggestedCommandTests(unittest.TestCase):
    def test_commands_match_the_action(self) -> None:
        vm = resource("vm-idle")
        disk = resource("disk-orphan-01", "microsoft.compute/disks", properties={"diskState": "Unattached"})
        metrics_map = {vm.id.lower(): usage(vm.id, cpu_peak=0.5, days_since_last_activity=40)}
        result = audit(
            [vm, disk],
            {vm.id.lower(): summary(vm.id, 140.26), disk.id.lower(): summary(disk.id, 19.72)},
            metrics_map,
            Policy(allow_orphan_deletion=True),
        )
        commands = {decision.resource.name: suggest_command(decision) for decision in result.decisions}
        self.assertTrue(commands["vm-idle"].startswith("az vm deallocate --ids /subscriptions/"))
        self.assertTrue(commands["disk-orphan-01"].startswith("az disk delete --ids /subscriptions/"))

    def test_aks_stop_command(self) -> None:
        aks = resource("aks-1", "microsoft.containerservice/managedclusters")
        metrics_map = {aks.id.lower(): usage(aks.id, cpu_peak=1.0, days_since_last_activity=30)}
        result = audit([aks], {aks.id.lower(): summary(aks.id, 200.00)}, metrics_map)
        self.assertTrue(suggest_command(result.decisions[0]).startswith("az aks stop --ids"))


if __name__ == "__main__":
    unittest.main()
