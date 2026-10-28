"""End-to-end tests: run the CLI the way an operator would, over the example fixtures."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costwatchdog import cli

from tests.support import COST_CSV, INVENTORY, METRICS, POLICY

TODAY = "2026-10-27"


def azure_sdk_installed() -> bool:
    try:
        import azure.identity  # noqa: F401
    except ImportError:
        return False
    return True


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.report = self.dir / "report.md"
        self.plan = self.dir / "plan.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def audit_args(self, *extra: str) -> list[str]:
        return [
            "audit",
            "--cost-csv", str(COST_CSV),
            "--inventory", str(INVENTORY),
            "--metrics", str(METRICS),
            "--policy", str(POLICY),
            "--today", TODAY,
            "--no-audit-log",
            "--quiet",
            "--format", "both",
            "--out", str(self.report),
            "--plan-out", str(self.plan),
            *extra,
        ]

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class AuditEndToEndTests(CliTestCase):
    def test_audit_writes_report_and_plan(self) -> None:
        code, _, _ = self.run_cli(self.audit_args())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(self.report.exists())
        self.assertTrue(self.report.with_suffix(".json").exists())
        self.assertTrue(self.plan.exists())

        plan = json.loads(self.plan.read_text())
        self.assertEqual(len(plan["decisions"]), 9)
        self.assertEqual(plan["summary"]["resources_considered"], 14)
        self.assertEqual(plan["summary"]["identified_monthly_waste"], 512.78)

        actions = {decision["resource_name"]: decision["action"] for decision in plan["decisions"]}
        self.assertEqual(actions["vm-dev-01"], "deallocate")
        self.assertEqual(actions["aks-prod-01"], "stop")
        self.assertEqual(actions["vm-dev-02"], "notify")

        report = self.report.read_text()
        self.assertIn("Cloud Cost Watchdog", report)
        self.assertIn("vm-dev-01", report)

    def test_waste_budget_gate(self) -> None:
        code, _, err = self.run_cli(self.audit_args("--fail-on-waste", "1000"))
        self.assertEqual(code, cli.EXIT_OK)

        code, _, err = self.run_cli(self.audit_args("--fail-on-waste", "400"))
        self.assertEqual(code, cli.EXIT_WASTE_BUDGET_EXCEEDED)
        self.assertIn("exceeds budget", err)

    def test_audit_writes_an_audit_trail(self) -> None:
        log = self.dir / "audit.jsonl"
        argv = [item for item in self.audit_args() if item != "--no-audit-log"]
        code, _, _ = self.run_cli([*argv, "--audit-log", str(log)])
        self.assertEqual(code, cli.EXIT_OK)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(events[0]["event"], "audit")
        self.assertEqual(len(events), 1 + 9)

    def test_currency_filter_can_exclude_everything(self) -> None:
        code, _, _ = self.run_cli(self.audit_args("--currency", "EUR"))
        self.assertEqual(code, cli.EXIT_OK)
        plan = json.loads(self.plan.read_text())
        self.assertEqual(plan["summary"]["total_monthly_cost"], 0.0)

    def test_missing_required_inputs_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as context:
            self.run_cli(["audit", "--cost-csv", str(COST_CSV)])
        self.assertEqual(context.exception.code, cli.EXIT_USAGE)

    def test_bad_policy_is_reported_not_traced(self) -> None:
        bad = self.dir / "policy.json"
        bad.write_text(json.dumps({"allow_prod_action": True}))  # typo on purpose
        argv = [item if item != str(POLICY) else str(bad) for item in self.audit_args()]
        code, _, err = self.run_cli(argv)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unknown policy key", err)

    def test_bad_metrics_file_is_reported(self) -> None:
        bad = self.dir / "metrics.json"
        bad.write_text("{not json")
        argv = [item if item != str(METRICS) else str(bad) for item in self.audit_args()]
        code, _, err = self.run_cli(argv)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("not valid JSON", err)


class ApplyEndToEndTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.run_cli(self.audit_args())[0], cli.EXIT_OK)

    def test_dry_run_executes_nothing(self) -> None:
        code, out, _ = self.run_cli(["apply", "--plan", str(self.plan), "--no-audit-log"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("dry-run", out)
        self.assertIn("dry-run: would execute", out)
        self.assertIn("executed 0", out)
        self.assertIn("requires approval: environment=prod", out)

    def test_notify_decisions_are_never_executed(self) -> None:
        _, out, _ = self.run_cli(["apply", "--plan", str(self.plan), "--no-audit-log"])
        self.assertIn("notify-only; nothing to execute", out)

    def test_plan_file_must_be_a_plan(self) -> None:
        broken = self.dir / "broken.json"
        broken.write_text(json.dumps({"hello": "world"}))
        code, _, err = self.run_cli(["apply", "--plan", str(broken), "--no-audit-log"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("not a costwatchdog plan", err)

    @unittest.skipIf(
        azure_sdk_installed(),
        "Azure SDKs are installed, so the missing-dependency path is not exercised here",
    )
    def test_executing_without_the_azure_sdk_fails_clearly(self) -> None:
        code, _, err = self.run_cli(["apply", "--plan", str(self.plan), "--yes", "--no-audit-log"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("needs the Azure SDKs", err)


if __name__ == "__main__":
    unittest.main()
