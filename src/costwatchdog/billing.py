"""Parse Azure Cost Management exports into per-resource monthly run rates.

The engine needs one number per resource: *what does this cost per month?* Cost exports
are line items with a usage window, so this module aggregates them and normalizes to a
30.4375-day month. Rows that cannot be attributed to a resource id are kept out of the
per-resource view but still counted in the unallocated total.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from costwatchdog.models import (
    DAYS_PER_MONTH,
    CostRecord,
    ResourceCostSummary,
    money,
    parse_tag_string,
)

# Export schema drifted over the years and depends on the export type (actual vs
# amortized, legacy vs modern). Accept the common spellings for each logical field.
COLUMNS: dict[str, tuple[str, ...]] = {
    "resource_id": ("ResourceId", "ResourceID", "resourceId", "InstanceId", "ResourceUri"),
    "resource_name": ("ResourceName", "Resource", "resourceName"),
    "resource_type": ("ResourceType", "resourceType", "ConsumedService"),
    "resource_group": ("ResourceGroup", "ResourceGroupName", "resourceGroup"),
    "subscription_id": ("SubscriptionId", "SubscriptionID", "subscriptionId", "SubscriptionGuid"),
    "location": ("ResourceLocation", "Location", "resourceLocation"),
    "meter_category": ("MeterCategory", "ServiceName", "ServiceFamily"),
    "meter_subcategory": ("MeterSubCategory", "MeterSubcategory"),
    "meter_name": ("MeterName",),
    "quantity": ("Quantity", "UsageQuantity", "quantity"),
    "cost": ("CostInBillingCurrency", "Cost", "PreTaxCost", "CostInUSD", "costInBillingCurrency"),
    "currency": ("BillingCurrency", "BillingCurrencyCode", "Currency", "currency"),
    "charge_type": ("ChargeType", "chargeType"),
    "frequency": ("Frequency", "frequency"),
    "pricing_model": ("PricingModel", "pricingModel"),
    "benefit_name": ("BenefitName", "benefitName", "BenefitId"),
    "tags": ("ResourceTags", "Tags", "tags"),
    "date": ("Date", "UsageDate", "UsageDateTime", "date"),
}


def _pick(row: Mapping[str, Any], logical: str) -> str:
    """Return the first non-empty column value for a logical field."""
    for candidate in COLUMNS[logical]:
        if candidate in row:
            value = row[candidate]
            if value is not None and str(value).strip() != "":
                return str(value).strip()
    return ""


def parse_money(raw: Any) -> Decimal | None:
    """Parse export money: thousands separators, currency symbols, (1.23) negatives."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("$", "").replace("\u00a0", "").replace(" ", "")
    for symbol in ("USD", "EUR", "GBP", "AUD", "CAD", "JPY"):
        text = text.replace(symbol, "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_usage_date(raw: Any) -> date | None:
    """Parse the several date spellings that appear in cost exports."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    compact = text.split("T")[0].replace("-", "")
    if len(compact) == 8 and compact.isdigit():
        # 20260801 (separator-free only, or 08/01/2026 would be misread)
        try:
            return date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))
        except ValueError:
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.split("+")[0], fmt).date()
        except ValueError:
            continue
    return None


def _decimal_or_zero(raw: Any) -> Decimal:
    parsed = parse_money(raw)
    return parsed if parsed is not None else Decimal("0")


def parse_cost_csv(
    source: str | Path | io.TextIOBase,
    *,
    currency: str | None = None,
) -> list[CostRecord]:
    """Parse a Cost Management export from a path or file-like object.

    ``currency`` filters to a single billing currency, which matters because a tenant can
    bill different subscriptions in different currencies and summing them is meaningless.
    """
    if hasattr(source, "read"):
        text = source.read()
        handle = io.StringIO(text)
        close = True
    else:
        handle = Path(source).open("r", encoding="utf-8-sig", newline="")
        close = True
    try:
        reader = csv.DictReader(handle)
        records: list[CostRecord] = []
        for row in reader:
            if not row:
                continue
            cost = parse_money(row.get(_first_present(row, "cost")) or _pick(row, "cost"))
            if cost is None:
                continue
            row_currency = _pick(row, "currency") or (currency or "USD")
            if currency and row_currency.upper() != currency.upper():
                continue
            records.append(
                CostRecord(
                    resource_id=_pick(row, "resource_id"),
                    cost=cost,
                    currency=row_currency.upper(),
                    resource_name=_pick(row, "resource_name"),
                    resource_type=_pick(row, "resource_type"),
                    resource_group=_pick(row, "resource_group"),
                    subscription_id=_pick(row, "subscription_id"),
                    meter_category=_pick(row, "meter_category"),
                    meter_subcategory=_pick(row, "meter_subcategory"),
                    meter_name=_pick(row, "meter_name"),
                    quantity=_decimal_or_zero(_pick(row, "quantity")),
                    charge_type=_pick(row, "charge_type"),
                    frequency=_pick(row, "frequency"),
                    pricing_model=_pick(row, "pricing_model"),
                    benefit_name=_pick(row, "benefit_name"),
                    tags=parse_tag_string(_pick(row, "tags")),
                    date=parse_usage_date(_pick(row, "date")),
                )
            )
        return records
    finally:
        if close:
            handle.close()


def _first_present(row: Mapping[str, Any], logical: str) -> str:
    for candidate in COLUMNS[logical]:
        if candidate in row:
            return candidate
    return COLUMNS[logical][0]


def observed_days(records: Sequence[CostRecord], *, fallback: int | None = None) -> int:
    """Length of the reporting window in days, from the export's own dates."""
    dates = [record.date for record in records if record.date is not None]
    if dates:
        return max((max(dates) - min(dates)).days + 1, 1)
    if fallback:
        return max(int(fallback), 1)
    return int(DAYS_PER_MONTH)


def summarize_costs(
    records: Iterable[CostRecord],
    *,
    period_days: int | None = None,
) -> tuple[dict[str, ResourceCostSummary], Decimal]:
    """Aggregate records per resource id, case-insensitively.

    Returns ``(summaries_by_lowercased_id, unallocated_total)``. Unallocated spend is
    marketplace/one-off charges with no resource id, which the watchdog cannot act on.
    """
    rows = list(records)
    window = observed_days(rows, fallback=period_days)
    grouped: dict[str, dict[str, Any]] = {}
    unallocated = Decimal("0")
    for record in rows:
        key = record.resource_id.lower()
        if not key:
            unallocated += record.cost
            continue
        bucket = grouped.setdefault(
            key,
            {
                "total": Decimal("0"),
                "currency": record.currency or "USD",
                "by_meter": {},
                "by_charge_type": {},
                "record_count": 0,
                "resource_name": record.resource_name,
                "resource_type": record.resource_type,
                "resource_group": record.resource_group,
                "subscription_id": record.subscription_id,
            },
        )
        bucket["total"] += record.cost
        bucket["record_count"] += 1
        meter = record.meter_name or record.meter_category or record.meter_subcategory or "other"
        bucket["by_meter"][meter] = bucket["by_meter"].get(meter, Decimal("0")) + record.cost
        charge = record.charge_type or "Usage"
        bucket["by_charge_type"][charge] = bucket["by_charge_type"].get(charge, Decimal("0")) + record.cost
        for field in ("resource_name", "resource_type", "resource_group", "subscription_id"):
            if not bucket[field] and record.__dict__.get(field):
                bucket[field] = record.__dict__[field]

    summaries: dict[str, ResourceCostSummary] = {}
    for key, bucket in grouped.items():
        # Totals stay unrounded: normalizing a 3-day window up to a month multiplies any
        # rounding error by ~10, so quantize at display time (ResourceCostSummary does it
        # for the monthly rate) rather than here.
        summaries[key] = ResourceCostSummary(
            resource_id=key,
            currency=bucket["currency"],
            total=bucket["total"],
            observed_days=window,
            by_meter=dict(bucket["by_meter"]),
            by_charge_type=dict(bucket["by_charge_type"]),
            record_count=bucket["record_count"],
            resource_name=bucket["resource_name"],
            resource_type=bucket["resource_type"],
            resource_group=bucket["resource_group"],
            subscription_id=bucket["subscription_id"],
        )
    return summaries, unallocated
