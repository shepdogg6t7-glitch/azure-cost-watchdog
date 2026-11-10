# tests/test_security_regression.py
"""Security regression tests for the destructive-action boundary."""
import pytest

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


class TestStopServerfarms:
    def test_stop_serverfarms_raises(self):
        with pytest.raises(AzureError):
            suggest_command(_decision(Action.STOP, "microsoft.web/serverfarms"))


class TestStopManagedClusters:
    def test_stop_managedclusters_emits_aks_stop(self):
        cmd = suggest_command(_decision(
            Action.STOP, "microsoft.containerservice/managedclusters",
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.ContainerService/managedClusters/aks1",
        ))
        assert "az aks stop" in cmd
        assert "delete" not in cmd.lower()


class TestDeleteOrphanAllowlist:
    @pytest.mark.parametrize("kind", ALLOWED_DELETE_TYPES)
    def test_allowed_types_emit_delete(self, kind):
        cmd = suggest_command(_decision(Action.DELETE_ORPHAN, kind))
        assert "delete" in cmd.lower()

    @pytest.mark.parametrize("kind", [
        "microsoft.web/serverfarms",
        "microsoft.storage/storageaccounts",
        "microsoft.sql/servers",
    ])
    def test_disallowed_types_raise(self, kind):
        with pytest.raises(AzureError):
            suggest_command(_decision(Action.DELETE_ORPHAN, kind))


class TestApplyPlanRefusesServerfarmsOrphanDelete:
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
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "not allowed" in results[0]["detail"].lower()
