"""Tests for the guardrail configuration."""

from __future__ import annotations

import unittest
from decimal import Decimal

from costwatchdog.policy import Policy, PolicyError

from tests.support import POLICY


class PolicyDefaultsTests(unittest.TestCase):
    def test_safe_defaults(self) -> None:
        policy = Policy()
        self.assertFalse(policy.allow_prod_actions)
        self.assertFalse(policy.allow_orphan_deletion)
        self.assertEqual(policy.min_monthly_savings, Decimal("5.00"))
        self.assertEqual(policy.max_actions_per_run, 25)

    def test_environment_and_owner_lookup_is_case_insensitive(self) -> None:
        policy = Policy()
        self.assertEqual(policy.environment_of({"Env": "PROD"}), "prod")
        self.assertEqual(policy.owner_of({"Team": "team-core"}), "team-core")
        self.assertIsNone(policy.environment_of({"cost-center": "123"}))

    def test_protected_tag_only_when_truthy(self) -> None:
        policy = Policy()
        self.assertEqual(policy.protected_tag({"costwatchdog:protected": "true"}), "costwatchdog:protected")
        self.assertIsNone(policy.protected_tag({"costwatchdog:protected": "false"}))
        self.assertIsNone(policy.protected_tag({"owner": "team"}))

    def test_denied_scope(self) -> None:
        policy = Policy(denied_resource_groups=("rg-legacy",), denied_subscriptions=("sub-9",))
        self.assertTrue(policy.is_denied_scope(subscription_id="sub-1", resource_group="RG-Legacy"))
        self.assertTrue(policy.is_denied_scope(subscription_id="SUB-9", resource_group="rg-dev"))
        self.assertFalse(policy.is_denied_scope(subscription_id="sub-1", resource_group="rg-dev"))


class PolicyLoadingTests(unittest.TestCase):
    def test_example_policy_loads(self) -> None:
        policy = Policy.from_mapping(
            {
                "_comment": "explains itself",
                "allow_orphan_deletion": True,
                "min_monthly_savings": 1,
                "denied_resource_groups": ["rg-legacy"],
            }
        )
        self.assertTrue(policy.allow_orphan_deletion)
        self.assertEqual(policy.min_monthly_savings, Decimal("1"))
        self.assertEqual(policy.denied_resource_groups, ("rg-legacy",))
        self.assertIn("_comment", policy.metadata)

    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(PolicyError) as context:
            Policy.from_mapping({"allow_prod_action": True})  # typo: missing 's'
        self.assertIn("allow_prod_action", str(context.exception))

    def test_type_coercion_from_strings(self) -> None:
        policy = Policy.from_mapping({"allow_prod_actions": "yes", "require_owner_tag": "false", "min_idle_days": "3"})
        self.assertTrue(policy.allow_prod_actions)
        self.assertFalse(policy.require_owner_tag)
        self.assertEqual(policy.min_idle_days, 3)

    def test_bad_types_raise(self) -> None:
        with self.assertRaises(PolicyError):
            Policy.from_mapping({"allow_prod_actions": "maybe"})
        with self.assertRaises(PolicyError):
            Policy.from_mapping({"min_idle_days": "soon"})
        with self.assertRaises(PolicyError):
            Policy.from_mapping({"protected_tag_keys": 5})

    def test_roundtrip_through_dict(self) -> None:
        policy = Policy(allow_orphan_deletion=True, denied_resource_groups=("rg-legacy",))
        self.assertEqual(Policy.from_mapping(policy.to_dict()), policy)

    def test_example_file_is_valid(self) -> None:
        from costwatchdog.sources import load_policy

        policy = load_policy(POLICY)
        self.assertTrue(policy.allow_orphan_deletion)
        self.assertFalse(policy.allow_prod_actions)
        self.assertEqual(policy.denied_resource_groups, ("rg-legacy",))


if __name__ == "__main__":
    unittest.main()
