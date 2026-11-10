# tests/test_security_regression.py
"""Security regression tests for the destructive-action boundary."""
import unittest

from costwatchdog.azure_client import AzureError, apply_plan
from costwatchdog.engine import suggest_command
from costwatchdog.models import Action, Decision, Resource, Severity


def _resource(kind, rid=None):
    rid = rid or f"/subscriptions/sub/resourceGroups/rg/providers/{kind}/r1"
    return Resource(
        id=rid, name="test-resource", type=kind,
        resource_group="rg", subscription_id="sub", location="eastus",
    )

def _decision(action, kind, rid=None):
    return Decision(
        resource=_resource(kind, rid), action=action,
        severity=Severity.INFO, category="test",
        reason="security regression test",
    )

ALLOWED_DELETE_TYPES = (
    "microsoft.compute/disks",
    "microsoft.compute/snapshots",
    "microsoft.network/publicipaddresses",
    "microsoft.network/networkinterfaces",
)

class TestStopServerfarms(unittest.TestCase):
    def test_stop_serverfarms_raises(self):
        with self.assertRaises(AzureError):
            suggest_command(_decision(Action.STOP, "microsoft.web/serverfarms"))

class TestStopManagedClusters(unittest.TestCase):
    def test_stop_managedclusters_emits_aks_stop(self):
        cmd = suggest_command(_decision(
            Action.STOP, "microsoft.containerservice/managedclusters",
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.ContainerService/managedClusters/aks1",
        ))
        self.assertIn("az aks stop", cmd)
        self.assertNotIn("delete", cmd.lower())

class TestDeleteOrphanAllowlist(unittest.TestCase):
    def test_allowed_types_emit_delete(self):
        for kind in ALLOWED_DELETE_TYPES:
            cmd = suggest_command(_decision(Action.DELETE_ORPHAN, kind))
            self.assertIn("delete", cmd.lower(), f"Failed for {kind}")

    def test_disallowed_types_raise(self):
        disallowed = [
            "microsoft.web/serverfarms",
            "microsoft.storage/storageaccounts",
            "microsoft.sql/servers",
        ]
        for kind in disallowed:
            with self.assertRaises(AzureError, msg=f"Did not raise for {kind}"):
                suggest_command(_decision(Action.DELETE_ORPHAN, kind))

class TestApplyPlanRefusesServerfarmsOrphanDelete(unittest.TestCase):
    def test_serverfarms_orphan_delete_fails_at_allowlist(self):
        plan = {"decisions": [{
            "action": "delete-orphan",
            "resource_id": (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/serverfarms/plan1"
            ),
            "resource_name": "plan1",
        }]}
        results = apply_plan(plan, dry_run=False, allow_orphan_deletion=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("not allowed", results[0]["detail"].lower())
