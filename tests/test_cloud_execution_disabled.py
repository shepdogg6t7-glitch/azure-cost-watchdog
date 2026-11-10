"""Regression tests ensuring cloud execution is not available."""

from __future__ import annotations

import argparse
import unittest

from costwatchdog.cli import build_parser


class CloudExecutionDisabledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.subparsers = next(
            action
            for action in self.parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

    def test_only_audit_command_is_registered(self) -> None:
        self.assertEqual(set(self.subparsers.choices), {"audit"})

    def test_audit_has_no_cloud_or_execution_options(self) -> None:
        audit_parser = self.subparsers.choices["audit"]
        options = {
            option
            for action in audit_parser._actions
            for option in action.option_strings
        }

        forbidden = (
            "--live",
            "--subscriptions",
            "--days",
            "--plan-out",
        )

        for option in forbidden:
            with self.subTest(option=option):
                self.assertNotIn(option, options)


if __name__ == "__main__":
    unittest.main()
