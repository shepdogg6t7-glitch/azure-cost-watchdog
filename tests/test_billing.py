"""Tests for Cost Management export handling."""

from __future__ import annotations

import io
import unittest
from datetime import date
from decimal import Decimal

from costwatchdog.billing import (
    observed_days,
    parse_cost_csv,
    parse_money,
    parse_usage_date,
    summarize_costs,
)

from tests.support import COST_CSV

HEADER = (
    "SubscriptionId,ResourceGroup,ResourceId,ResourceType,ResourceName,MeterCategory,MeterName,"
    "Quantity,EffectivePrice,CostInBillingCurrency,BillingCurrency,ResourceTags,ChargeType,PricingModel,Date"
)


def csv_text(*rows: str) -> io.StringIO:
    return io.StringIO("\n".join([HEADER, *rows]) + "\n")


class MoneyParsingTests(unittest.TestCase):
    def test_common_forms(self) -> None:
        self.assertEqual(parse_money("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_money("(2.50)"), Decimal("-2.50"))
        self.assertEqual(parse_money("12 USD"), Decimal("12"))
        self.assertEqual(parse_money("0"), Decimal("0"))

    def test_unparseable_and_empty(self) -> None:
        self.assertIsNone(parse_money(""))
        self.assertIsNone(parse_money(None))
        self.assertIsNone(parse_money("n/a"))


class DateParsingTests(unittest.TestCase):
    def test_supported_spellings(self) -> None:
        expected = date(2026, 8, 1)
        for raw in ("20260801", "2026-08-01", "2026-08-01T00:00:00Z", "08/01/2026"):
            self.assertEqual(parse_usage_date(raw), expected, raw)

    def test_unknown_formats(self) -> None:
        self.assertIsNone(parse_usage_date("not a date"))
        self.assertIsNone(parse_usage_date(""))


class CsvParsingTests(unittest.TestCase):
    def test_fixture_export(self) -> None:
        records = parse_cost_csv(COST_CSV)
        self.assertEqual(len(records), 39)
        self.assertEqual(observed_days(records), 3)
        first = next(record for record in records if record.resource_name == "vm-dev-01")
        self.assertEqual(first.cost, Decimal("4.608"))
        self.assertEqual(first.date, date(2026, 8, 1))
        self.assertEqual(first.tags["env"], "dev")
        self.assertEqual(first.currency, "USD")

    def test_rows_without_a_cost_are_skipped(self) -> None:
        records = parse_cost_csv(
            csv_text(
                "sub,rg,/subscriptions/sub/providers/Microsoft.Compute/disks,"
                "Microsoft.Compute/disks,none,Storage,P10,24,0.027,,USD,env:dev,Usage,OnDemand,2026-08-01"
            )
        )
        self.assertEqual(records, [])

    def test_currency_filter(self) -> None:
        def rows() -> io.StringIO:
            return csv_text(
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,1,1,10.00,USD,env:dev,Usage,OnDemand,2026-08-01",
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,1,1,10.00,EUR,env:dev,Usage,OnDemand,2026-08-01",
            )

        self.assertEqual(len(parse_cost_csv(rows(), currency="USD")), 1)
        self.assertEqual(len(parse_cost_csv(rows(), currency="eur")), 1)
        self.assertEqual(len(parse_cost_csv(rows())), 2)


class SummarizeTests(unittest.TestCase):
    def test_fixture_run_rates(self) -> None:
        summaries, unallocated = summarize_costs(parse_cost_csv(COST_CSV))
        self.assertEqual(unallocated, Decimal("0"))
        by_name = {key.split("/")[-1]: value.monthly_run_rate for key, value in summaries.items()}
        self.assertEqual(by_name["vm-dev-01"], Decimal("140.26"))
        self.assertEqual(by_name["aks-prod-01"], Decimal("210.38"))
        self.assertEqual(by_name["snap-old-01"], Decimal("6.40"))
        self.assertEqual(sum(by_name.values()), Decimal("886.80"))

    def test_short_window_is_normalized_not_multiplied_naively(self) -> None:
        records = parse_cost_csv(
            csv_text(
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,24,1,10.00,USD,env:dev,Usage,OnDemand,2026-08-01"
            )
        )
        summaries, _ = summarize_costs(records)
        # One day of $10 normalizes to a 30.4375 day month.
        self.assertEqual(summaries["/r/1"].monthly_run_rate, Decimal("304.38"))

    def test_rounding_does_not_compound_on_short_windows(self) -> None:
        rows = [
            f"sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,24,1,4.608,USD,env:dev,Usage,OnDemand,2026-08-0{day}"
            for day in (1, 2, 3)
        ]
        summaries, _ = summarize_costs(parse_cost_csv(csv_text(*rows)))
        self.assertEqual(summaries["/r/1"].monthly_run_rate, Decimal("140.26"))

    def test_unallocated_spend_is_reported_separately(self) -> None:
        records = parse_cost_csv(
            csv_text(
                "sub,rg,,Marketplace,marketplace-thing,Marketplace,Other,1,5,5.00,USD,,Usage,OnDemand,2026-08-01"
            )
        )
        summaries, unallocated = summarize_costs(records)
        self.assertEqual(summaries, {})
        self.assertEqual(unallocated, Decimal("5.00"))

    def test_line_items_are_grouped_by_meter(self) -> None:
        records = parse_cost_csv(
            csv_text(
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,24,1,10.00,USD,env:dev,Usage,OnDemand,2026-08-01",
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Virtual Machines,D2s,24,1,5.00,USD,env:dev,Usage,OnDemand,2026-08-01",
            )
        )
        summaries, _ = summarize_costs(records)
        self.assertEqual(summaries["/r/1"].by_meter, {"P10": Decimal("10.00"), "D2s": Decimal("5.00")})
        self.assertEqual(summaries["/r/1"].record_count, 2)

    def test_period_days_override_is_used_when_dates_are_missing(self) -> None:
        records = parse_cost_csv(
            csv_text(
                "sub,rg,/r/1,Microsoft.Compute/disks,a,Storage,P10,24,1,30.00,USD,env:dev,Usage,OnDemand,"
            )
        )
        summaries, _ = summarize_costs(records, period_days=10)
        self.assertEqual(summaries["/r/1"].observed_days, 10)
        self.assertEqual(summaries["/r/1"].monthly_run_rate, Decimal("91.31"))


if __name__ == "__main__":
    unittest.main()
