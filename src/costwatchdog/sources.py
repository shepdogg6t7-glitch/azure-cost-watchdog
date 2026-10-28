"""Loaders for the offline (fixture) data sources and the audit plan.

Fixtures use the same shapes Azure returns, so a captured Resource Graph query, an
exported CSV and a metrics dump can be replayed without credentials - which is also how
the test suite runs.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from costwatchdog.models import Resource, UsageMetrics
from costwatchdog.policy import Policy, PolicyError


class SourceError(ValueError):
    """Raised when a fixture file cannot be interpreted."""


def load_json(path: str | Path) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        raise SourceError(f"file not found: {file_path}")
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SourceError(f"{file_path} is not valid JSON: {error}") from error


def dump_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return file_path


def load_resources(path: str | Path) -> list[Resource]:
    """Accept a Resource Graph response, an ARM list response, or a bare array."""
    payload = load_json(path)
    if isinstance(payload, Mapping):
        rows = payload.get("data", payload.get("value"))
    else:
        rows = payload
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
        raise SourceError(f"{path}: expected a list of resources under 'data' or 'value'")
    resources = [Resource.from_arm(row) for row in rows if isinstance(row, Mapping)]
    if not resources:
        raise SourceError(f"{path}: no resources found")
    return resources


def load_metrics(path: str | Path | None) -> dict[str, UsageMetrics]:
    """Metrics keyed by resource id, e.g. ``{"<resource id>": {"cpu_peak_percent": 1.4}}``."""
    if path is None:
        return {}
    payload = load_json(path)
    if isinstance(payload, Mapping) and "metrics" in payload and isinstance(payload["metrics"], Mapping):
        payload = payload["metrics"]
    if not isinstance(payload, Mapping):
        raise SourceError(f"{path}: expected an object keyed by resource id")
    metrics: dict[str, UsageMetrics] = {}
    for resource_id, values in payload.items():
        if not isinstance(values, Mapping):
            raise SourceError(f"{path}: metrics for {resource_id} must be an object")
        metrics[str(resource_id).lower()] = UsageMetrics(
            resource_id=str(resource_id),
            cpu_peak_percent=_optional_float(values.get("cpu_peak_percent")),
            cpu_avg_percent=_optional_float(values.get("cpu_avg_percent")),
            network_in_bytes_per_day=_optional_float(values.get("network_in_bytes_per_day")),
            network_out_bytes_per_day=_optional_float(values.get("network_out_bytes_per_day")),
            request_count_30d=_optional_int(values.get("request_count_30d")),
            connections_30d=_optional_int(values.get("connections_30d")),
            last_activity=_optional_datetime(values.get("last_activity_utc")),
            days_since_last_activity=_optional_int(values.get("days_since_last_activity")),
            window_days=_optional_int(values.get("window_days")) or 30,
            source=str(values.get("source") or "fixture"),
        )
    return metrics


def load_cost_records(path: str | Path, *, currency: str | None = None):
    """Thin wrapper so the CLI has one import for cost loading."""
    from costwatchdog.billing import parse_cost_csv

    return parse_cost_csv(path, currency=currency)


def load_policy(path: str | Path | None) -> Policy:
    if path is None:
        return Policy()
    payload = load_json(path)
    if not isinstance(payload, Mapping):
        raise PolicyError(f"{path}: policy must be a JSON object")
    return Policy.from_mapping(payload)


def load_plan(path: str | Path) -> dict[str, Any]:
    payload = load_json(path)
    if not isinstance(payload, Mapping) or "decisions" not in payload:
        raise SourceError(f"{path}: not a costwatchdog plan (missing 'decisions')")
    return dict(payload)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise SourceError(f"expected a number, got {value!r}") from error


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SourceError(f"expected an integer, got {value!r}") from error


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError as error:
        raise SourceError(f"expected an ISO-8601 timestamp, got {value!r}") from error


def parse_amount(value: Any) -> Decimal:
    """Parse a CLI money argument (``500``, ``500.25``, ``"$1,200"``)."""
    from costwatchdog.billing import parse_money

    parsed = parse_money(value)
    if parsed is None:
        raise SourceError(f"could not parse an amount from {value!r}")
    return parsed
