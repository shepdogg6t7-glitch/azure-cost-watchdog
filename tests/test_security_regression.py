"""Security regression tests for offline, recommendation-only behavior."""

import unittest

from costwatchdog.engine import suggest_recommendation
from costwatchdog.models import Action, Decision, Resource, Severity


def _decision(action, kind):
    resource = Resource(
        id="local-resource-001",
        name="test-resource",
        type=kind,
        resource_group="local-group",
        subscription_id="local",
        location="local",
    )

    return Decision(
        resource=resource,
        action=action,
        severity=Severity.INFO,
        category="test",
        reason="offline security regression test",
    )


class TestRecommendationsAreNonExecutable(unittest.TestCase):
    def test_recommendations_never_contain_cloud_commands(self):
        kinds = (
            "microsoft.compute/virtualmachines",
            "microsoft.compute/disks",
            "microsoft.containerservice/managedclusters",
            "microsoft.web/serverfarms",
            "microsoft.storage/storageaccounts",
            "microsoft.sql/servers",
        )

        actions = (
            Action.DEALLOCATE,
            Action.STOP,
            Action.DELETE_ORPHAN,
            Action.SCHEDULE_SHUTDOWN,
            Action.NOTIFY,
        )

        for kind in kinds:
            for action in actions:
                with self.subTest(kind=kind, action=action):
                    recommendation = suggest_recommendation(
                        _decision(action, kind)
                    )

                    self.assertIsInstance(recommendation, str)
                    self.assertTrue(recommendation.strip())
                    self.assertNotIn("az ", recommendation.lower())
                    self.assertNotIn("--ids", recommendation.lower())
                    self.assertNotIn("powershell", recommendation.lower())

    def test_orphan_recommendation_requires_manual_verification(self):
        recommendation = suggest_recommendation(
            _decision(
                Action.DELETE_ORPHAN,
                "microsoft.compute/disks",
            )
        )

        self.assertIn("orphaned", recommendation)
        self.assertIn("Verify ownership", recommendation)
        self.assertIn("backups", recommendation)

    def test_unsupported_action_defaults_to_manual_review(self):
        recommendation = suggest_recommendation(
            _decision(
                "unexpected-action",
                "local/unknown",
            )
        )

        self.assertIn("Review this finding manually", recommendation)


if __name__ == "__main__":
    unittest.main()
