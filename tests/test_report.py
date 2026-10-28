"""Tests for report rendering."""

from __future__ import annotations

import json
import unittest
from datetime import date

from costwatchdog.billing import parse_cost_csv, summarize_costs
from costwatchdog.engine import evaluate
from costwatchdog.report import render_console, render_json, render_markdown
from costwatchdog.sources import load_metrics, load_policy, load_resources

from tests.support import COST_CSV, INVENTORY, METRICS, POLICY

TODAY = date(2026, 10, 27)


def fixture_result():
    resources = load_resources(INVENTORY)
    summaries, unallocated = summarize_costs(parse_cost_csv(COST_CSV))
    return evaluate(
        resources,
        summaries,
        metrics=load_metrics(METRICS),
        policy=load_policy(POLICY),
        today=TODAY,
        unallocated=unallocated,
    )


class MarkdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = fixture_result()
        cls.markdown = render_markdown(cls.result, mode="dry-run", scope="fixtures")

    def test_summary_table(self) -> None:
        self.assertIn("| Monthly run rate (priced resources) | USD 886.80 |", self.markdown)
        self.assertIn("| Identified monthly waste | USD 512.78 |", self.markdown)
        self.assertIn("| Actionable now | USD 183.18 (5 decision(s)) |", self.markdown)
        self.assertIn("| Pending approval | USD 189.34 (1 decision(s)) |", self.markdown)
        self.assertIn("scope fixtures", self.markdown)

    def test_sections_are_grouped_by_required_action(self) -> None:
        self.assertIn("## Actionable now (5)", self.markdown)
        self.assertIn("## Pending approval (1)", self.markdown)
        self.assertIn("## Notify only (3)", self.markdown)

    def test_actionable_decision_includes_evidence_and_command(self) -> None:
        self.assertIn("### 1. vm-dev-01 - `deallocate` - saves USD 140.26/mo", self.markdown)
        self.assertIn("az vm deallocate --ids /subscriptions/00000000", self.markdown)
        self.assertIn("no meaningful activity for 34 days (requires 14d)", self.markdown)

    def test_guardrails_are_visible_in_the_report(self) -> None:
        self.assertIn("`protected-tag:costwatchdog:protected`", self.markdown)
        self.assertIn("`denylisted-scope`", self.markdown)
        self.assertIn("`environment=prod`", self.markdown)
        self.assertIn("**Needs approval because**: `environment=prod`", self.markdown)
        # The example policy enables orphan deletion, so that safeguard must not appear.
        self.assertIn("orphan deletion is enabled in this policy", self.markdown)
        self.assertNotIn("orphan-deletion-disabled", self.markdown)

    def test_methodology_section_documents_thresholds(self) -> None:
        self.assertIn("30.4375-day month", self.markdown)
        self.assertIn("never auto-actioned", self.markdown)
        self.assertIn("Mode: **dry-run**", self.markdown)

    def test_empty_result_renders_without_decisions(self) -> None:
        self.assertTrue(self.result.decisions)  # sanity: the fixture has decisions
        empty = render_markdown(
            evaluate([], {}, today=TODAY, policy=load_policy(POLICY)), mode="dry-run", scope="empty"
        )
        self.assertIn("## Actionable now (0)", empty)
        self.assertIn("_None._", empty)


class JsonTests(unittest.TestCase):
    def test_json_round_trip(self) -> None:
        result = fixture_result()
        payload = json.loads(render_json(result, mode="dry-run", scope="fixtures"))
        self.assertEqual(payload["schema"], "costwatchdog/plan/v1")
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["summary"]["actionable_monthly_savings"], 183.18)
        self.assertEqual(len(payload["decisions"]), len(result.decisions))
        first = payload["decisions"][0]
        self.assertEqual(first["resource_name"], "vm-dev-01")
        self.assertTrue(first["actionable"])
        self.assertEqual(first["action"], "deallocate")


class ConsoleTests(unittest.TestCase):
    def test_console_table(self) -> None:
        text = render_console(fixture_result(), mode="dry-run", scope="fixtures")
        self.assertIn("costwatchdog · dry-run · window 3d", text)
        self.assertIn("ACTION", text)
        self.assertIn("! vm-dev-01", text)
        self.assertIn("~ aks-prod-01", text)


if __name__ == "__main__":
    unittest.main()
