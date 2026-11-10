"""Tests for Azure action execution and audit logging."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from costwatchdog.auditlog import AuditLog
from costwatchdog.azure_client import AzureClients, AzureError, apply_plan


SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"

VM_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Compute/virtualMachines/vm-test"
)

VMSS_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Compute/virtualMachineScaleSets/vmss-test"
)

AKS_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-prod/"
    "providers/Microsoft.ContainerService/managedClusters/aks-test"
)

DISK_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Compute/disks/disk-test"
)

SNAPSHOT_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Compute/snapshots/snapshot-test"
)

PUBLIC_IP_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Network/publicIPAddresses/pip-test"
)

NIC_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Network/networkInterfaces/nic-test"
)

SERVER_FARM_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
    "providers/Microsoft.Web/serverfarms/plan-test"
)


def decision(action, resource_id=VM_ID, approvals=None):
    return {
        "resource_id": resource_id,
        "resource_name": resource_id.rsplit("/", 1)[-1],
        "action": action,
        "approvals_required": approvals or [],
    }


def read_events(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class ApplyPlanTests(unittest.TestCase):

    def test_dry_run_never_initializes_azure_clients(self):
        with (
            patch("costwatchdog.azure_client.require_azure_sdk") as sdk,
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                {"decisions": [decision("deallocate")]},
                dry_run=True,
            )

        self.assertEqual(results[0]["status"], "skipped")
        sdk.assert_not_called()
        clients.assert_not_called()

    def test_production_action_requires_approval(self):
        plan = {
            "decisions": [
                decision("stop", AKS_ID, ["environment=prod"])
            ]
        }

        with patch("costwatchdog.azure_client.AzureClients") as clients:
            results = apply_plan(plan, dry_run=False)

        self.assertEqual(results[0]["status"], "skipped")
        clients.assert_not_called()

    def test_orphan_deletion_requires_approval(self):
        plan = {
            "decisions": [
                decision("delete-orphan", DISK_ID,
                         ["orphan-deletion-disabled"])
            ]
        }

        with patch("costwatchdog.azure_client.AzureClients") as clients:
            results = apply_plan(plan, dry_run=False)

        self.assertEqual(results[0]["status"], "skipped")
        clients.assert_not_called()

    def test_vm_deallocation_dispatches_correctly(self):
        with (
            patch("costwatchdog.azure_client.require_azure_sdk"),
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                {"decisions": [decision("deallocate", VM_ID)]},
                dry_run=False,
            )

        clients.return_value.deallocate.assert_called_once_with(VM_ID)
        self.assertEqual(results[0]["status"], "executed")

    def test_vmss_deallocation_dispatches_correctly(self):
        with (
            patch("costwatchdog.azure_client.require_azure_sdk"),
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                {"decisions": [decision("deallocate", VMSS_ID)]},
                dry_run=False,
            )

        clients.return_value.deallocate.assert_called_once_with(VMSS_ID)
        self.assertEqual(results[0]["status"], "executed")

    def test_aks_stop_requires_and_accepts_prod_approval(self):
        plan = {
            "decisions": [
                decision("stop", AKS_ID, ["environment=prod"])
            ]
        }

        with (
            patch("costwatchdog.azure_client.require_azure_sdk"),
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                plan, dry_run=False, allow_prod=True
            )

        clients.return_value.stop.assert_called_once_with(AKS_ID)
        self.assertEqual(results[0]["status"], "executed")

    def test_orphan_delete_dispatches_correctly(self):
        plan = {
            "decisions": [
                decision("delete-orphan", DISK_ID,
                         ["orphan-deletion-disabled"])
            ]
        }

        with (
            patch("costwatchdog.azure_client.require_azure_sdk"),
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                plan,
                dry_run=False,
                allow_orphan_deletion=True,
            )

        clients.return_value.delete_resource.assert_called_once_with(DISK_ID)
        self.assertEqual(results[0]["status"], "executed")

    def test_azure_failure_is_returned_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            with (
                patch("costwatchdog.azure_client.require_azure_sdk"),
                patch("costwatchdog.azure_client.AzureClients") as clients,
            ):
                clients.return_value.deallocate.side_effect = RuntimeError(
                    "simulated Azure failure"
                )
                results = apply_plan(
                    {"decisions": [decision("deallocate")]},
                    dry_run=False,
                    audit_log=AuditLog(log_path),
                )

            self.assertEqual(results[0]["status"], "failed")
            self.assertIn("simulated Azure failure", results[0]["detail"])
            events = read_events(log_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "action-failed")

    def test_successful_action_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            with (
                patch("costwatchdog.azure_client.require_azure_sdk"),
                patch("costwatchdog.azure_client.AzureClients"),
            ):
                results = apply_plan(
                    {"decisions": [decision("deallocate")]},
                    dry_run=False,
                    audit_log=AuditLog(log_path),
                )

            self.assertEqual(results[0]["status"], "executed")
            events = read_events(log_path)
            self.assertEqual(events[0]["event"], "action-executed")

    def test_notify_only_action_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            results = apply_plan(
                {"decisions": [decision("notify")]},
                audit_log=AuditLog(log_path),
            )

            self.assertEqual(results[0]["status"], "skipped")
            events = read_events(log_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "action-skipped")

    def test_approval_blocked_action_is_logged(self):
        plan = {
            "decisions": [
                decision("stop", AKS_ID, ["environment=prod"])
            ]
        }

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            results = apply_plan(
                plan,
                dry_run=False,
                audit_log=AuditLog(log_path),
            )

            self.assertEqual(results[0]["status"], "skipped")
            events = read_events(log_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "action-skipped")

    def test_dry_run_action_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            results = apply_plan(
                {"decisions": [decision("deallocate")]},
                dry_run=True,
                audit_log=AuditLog(log_path),
            )

            self.assertEqual(results[0]["status"], "skipped")
            events = read_events(log_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "action-skipped")

    def test_unsupported_action_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            with (
                patch("costwatchdog.azure_client.require_azure_sdk"),
                patch("costwatchdog.azure_client.AzureClients"),
            ):
                results = apply_plan(
                    {"decisions": [decision("schedule-shutdown")]},
                    dry_run=False,
                    audit_log=AuditLog(log_path),
                )

            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("no executor", results[0]["detail"])
            events = read_events(log_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "action-skipped")


    def test_action_limit_skip_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            with (
                patch("costwatchdog.azure_client.require_azure_sdk"),
                patch("costwatchdog.azure_client.AzureClients") as clients,
            ):
                results = apply_plan(
                    {
                        "decisions": [
                            decision("deallocate", VM_ID),
                            decision("deallocate", VMSS_ID),
                        ]
                    },
                    dry_run=False,
                    limit=1,
                    audit_log=AuditLog(log_path),
                )

            self.assertEqual(results[0]["status"], "executed")
            self.assertEqual(results[1]["status"], "skipped")
            clients.return_value.deallocate.assert_called_once_with(VM_ID)

            events = read_events(log_path)
            self.assertEqual(
                [event["event"] for event in events],
                ["action-executed", "action-skipped"],
            )


    def test_all_allowlisted_resource_types_dispatch_for_deletion(self):
        allowed_ids = [
            DISK_ID,
            SNAPSHOT_ID,
            PUBLIC_IP_ID,
            NIC_ID,
            SERVER_FARM_ID,
        ]
        plan = {
            "decisions": [
                decision(
                    "delete-orphan",
                    resource_id,
                    ["orphan-deletion-disabled"],
                )
                for resource_id in allowed_ids
            ]
        }

        with (
            patch("costwatchdog.azure_client.require_azure_sdk"),
            patch("costwatchdog.azure_client.AzureClients") as clients,
        ):
            results = apply_plan(
                plan,
                dry_run=False,
                allow_orphan_deletion=True,
            )

        self.assertEqual(
            [result["status"] for result in results],
            ["executed"] * len(allowed_ids),
        )
        self.assertEqual(
            clients.return_value.delete_resource.call_count,
            len(allowed_ids),
        )
        self.assertEqual(
            [
                call.args[0]
                for call in clients.return_value.delete_resource.call_args_list
            ],
            allowed_ids,
        )

    def test_unsupported_and_malformed_deletions_fail_before_azure_setup(self):
        invalid_ids = [
            VM_ID,
            AKS_ID,
            (
                f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/"
                "providers/Microsoft.Storage/storageAccounts/storage-test"
            ),
            "not-an-azure-resource-id",
            (
                "/resourceGroups/rg-dev/"
                "providers/Microsoft.Compute/disks/disk-test"
            ),
        ]

        for resource_id in invalid_ids:
            with self.subTest(resource_id=resource_id):
                with (
                    patch("costwatchdog.azure_client.require_azure_sdk") as sdk,
                    patch("costwatchdog.azure_client.AzureClients") as clients,
                ):
                    results = apply_plan(
                        {
                            "decisions": [
                                decision(
                                    "delete-orphan",
                                    resource_id,
                                    ["orphan-deletion-disabled"],
                                )
                            ]
                        },
                        dry_run=False,
                        allow_orphan_deletion=True,
                    )

                self.assertEqual(results[0]["status"], "failed")
                sdk.assert_not_called()
                clients.assert_not_called()

    def test_invalid_deletion_is_recorded_as_action_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "actions.jsonl"

            with (
                patch("costwatchdog.azure_client.require_azure_sdk") as sdk,
                patch("costwatchdog.azure_client.AzureClients") as clients,
            ):
                results = apply_plan(
                    {
                        "decisions": [
                            decision(
                                "delete-orphan",
                                VM_ID,
                                ["orphan-deletion-disabled"],
                            )
                        ]
                    },
                    dry_run=False,
                    allow_orphan_deletion=True,
                    audit_log=AuditLog(log_path),
                )

            self.assertEqual(results[0]["status"], "failed")
            events = read_events(log_path)
            self.assertEqual(
                [event["event"] for event in events],
                ["action-failed"],
            )
            sdk.assert_not_called()
            clients.assert_not_called()

    def test_direct_delete_rejects_unsupported_type_before_client_creation(self):
        client_wrapper = object.__new__(AzureClients)

        with patch.object(client_wrapper, "_client") as client:
            with self.assertRaises(AzureError):
                client_wrapper.delete_resource(VM_ID)

        client.assert_not_called()

    def test_direct_delete_rejects_malformed_id_before_client_creation(self):
        client_wrapper = object.__new__(AzureClients)
        malformed_id = (
            "/resourceGroups/rg-dev/"
            "providers/Microsoft.Compute/disks/disk-test"
        )

        with patch.object(client_wrapper, "_client") as client:
            with self.assertRaises(AzureError):
                client_wrapper.delete_resource(malformed_id)

        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
