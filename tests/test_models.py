"""Tests for the pure data structures."""

from __future__ import annotations

import unittest
from decimal import Decimal

from costwatchdog.models import (
    Action,
    Decision,
    Resource,
    Severity,
    money,
    parse_tag_string,
    total_savings,
)


class TagParsingTests(unittest.TestCase):
    def test_export_delimited_form(self) -> None:
        tags = parse_tag_string("env:dev;owner:team-platform;workload:batch")
        self.assertEqual(tags, {"env": "dev", "owner": "team-platform", "workload": "batch"})

    def test_equals_delimited_form(self) -> None:
        self.assertEqual(parse_tag_string("env=prod;cost=9"), {"env": "prod", "cost": "9"})

    def test_json_object_form(self) -> None:
        tags = parse_tag_string('{"env": "dev", "owner": "team-core"}')
        self.assertEqual(tags, {"env": "dev", "owner": "team-core"})

    def test_values_may_contain_separators(self) -> None:
        tags = parse_tag_string("url:https://example.test/a;env:dev")
        self.assertEqual(tags["url"], "https://example.test/a")
        self.assertEqual(tags["env"], "dev")

    def test_mapping_passthrough_and_empty(self) -> None:
        self.assertEqual(parse_tag_string({"a": 1}), {"a": "1"})
        self.assertEqual(parse_tag_string(None), {})
        self.assertEqual(parse_tag_string(""), {})
        self.assertEqual(parse_tag_string(";;"), {})

    def test_malformed_entries_are_skipped(self) -> None:
        self.assertEqual(parse_tag_string("garbage;env:dev;;"), {"env": "dev"})


class ResourceTests(unittest.TestCase):
    def test_from_arm_extracts_ids_tags_and_sku(self) -> None:
        row = {
            "id": "/subscriptions/sub-1/resourceGroups/rg-dev/providers/Microsoft.Compute/virtualMachines/vm-1",
            "name": "vm-1",
            "type": "microsoft.compute/virtualmachines",
            "tags": {"env": "Dev", "costwatchdog:Protected": "TRUE"},
            "sku": {"name": "Standard_D4s_v5"},
            "properties": {"extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}},
        }
        resource = Resource.from_arm(row)
        self.assertEqual(resource.resource_group, "rg-dev")
        self.assertEqual(resource.subscription_id, "sub-1")
        self.assertEqual(resource.sku, "Standard_D4s_v5")
        self.assertEqual(resource.tag("ENV"), "Dev")
        self.assertTrue(resource.tag_bool("costwatchdog:protected"))
        self.assertFalse(resource.is_deallocated())

    def test_id_segments_are_derived_when_fields_are_absent(self) -> None:
        row = {"id": "/subscriptions/sub-9/resourceGroups/rg-x/providers/Microsoft.Compute/disks/d1"}
        resource = Resource.from_arm(row)
        self.assertEqual(resource.name, "d1")
        self.assertEqual(resource.resource_group, "rg-x")
        self.assertEqual(resource.subscription_id, "sub-9")

    def test_deallocated_detection_covers_both_spellings(self) -> None:
        running = Resource.from_arm({"id": "/x", "type": "t", "powerState": "VM running"})
        stopped = Resource.from_arm({"id": "/x", "type": "t", "powerState": "VM deallocated"})
        arm_stopped = Resource.from_arm({"id": "/x", "type": "t", "properties": {"powerState": "stopped"}})
        self.assertFalse(running.is_deallocated())
        self.assertTrue(stopped.is_deallocated())
        self.assertTrue(arm_stopped.is_deallocated())

    def test_display_type_is_prettified(self) -> None:
        resource = Resource.from_arm({"id": "/x", "type": "microsoft.web/serverfarms", "name": "p"})
        self.assertEqual(resource.display_type, "Microsoft.Web/serverfarms")


class MoneyTests(unittest.TestCase):
    def test_half_up_rounding(self) -> None:
        self.assertEqual(money("304.375"), Decimal("304.38"))
        self.assertEqual(money(0.005), Decimal("0.01"))
        self.assertEqual(money(Decimal("1.004")), Decimal("1.00"))

    def test_decision_to_dict_and_savings(self) -> None:
        resource = Resource.from_arm({"id": "/x", "type": "t", "name": "r"})
        decision = Decision(
            resource=resource,
            action=Action.DEALLOCATE,
            severity=Severity.HIGH,
            category="idle-vm",
            reason="idle",
            currency="USD",
            monthly_cost=Decimal("140.26"),
            reclaimable_ratio=Decimal("1"),
        )
        self.assertEqual(decision.estimated_monthly_savings, Decimal("140.26"))
        self.assertTrue(decision.is_actionable)
        self.assertFalse(decision.requires_approval)
        self.assertEqual(decision.to_dict()["action"], "deallocate")
        self.assertEqual(total_savings([decision]), Decimal("140.26"))

    def test_actionable_only_filter(self) -> None:
        resource = Resource.from_arm({"id": "/x", "type": "t", "name": "r"})
        blocked = Decision(
            resource=resource,
            action=Action.STOP,
            severity=Severity.HIGH,
            category="idle-aks",
            reason="idle",
            approvals=("environment=prod",),
            monthly_cost=Decimal("100.00"),
            reclaimable_ratio=Decimal("0.9"),
        )
        self.assertFalse(blocked.is_actionable)
        self.assertEqual(total_savings([blocked]), Decimal("90.00"))
        self.assertEqual(total_savings([blocked], actionable_only=True), Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
