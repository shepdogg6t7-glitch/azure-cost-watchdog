"""Guardrail configuration.

The policy is intentionally explicit and validated: an unknown key raises instead of
being silently ignored, because a typo in a safety setting should stop the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence


class PolicyError(ValueError):
    """Raised when a policy document is malformed."""


DEFAULT_MONITORED_TYPES: tuple[str, ...] = (
    "microsoft.compute/virtualmachines",
    "microsoft.compute/virtualmachinescalesets",
    "microsoft.compute/disks",
    "microsoft.compute/snapshots",
    "microsoft.containerservice/managedclusters",
    "microsoft.web/serverfarms",
    "microsoft.network/publicipaddresses",
    "microsoft.network/networkinterfaces",
    "microsoft.desktopvirtualization/hostpools",
)


@dataclass(frozen=True)
class Policy:
    """Everything the engine is allowed to consider, and the limits it must respect."""

    currency: str = "USD"
    min_monthly_savings: Decimal = Decimal("5.00")
    min_idle_days: int = 14
    cpu_idle_peak_percent: float = 5.0
    idle_network_bytes_per_day: int = 5_000_000
    stale_snapshot_days: int = 90
    max_actions_per_run: int = 25
    high_severity_savings: Decimal = Decimal("100.00")
    allow_prod_actions: bool = False
    allow_orphan_deletion: bool = False
    require_owner_tag: bool = True
    notify_unclassified: bool = True
    notify_unclassified_above: Decimal = Decimal("50.00")
    protected_tag_keys: tuple[str, ...] = (
        "costwatchdog:protected",
        "do-not-stop",
        "protected",
    )
    environment_tag_keys: tuple[str, ...] = ("env", "environment")
    owner_tag_keys: tuple[str, ...] = ("owner", "costwatchdog:owner", "team")
    protected_environments: tuple[str, ...] = ("prod", "production")
    denied_subscriptions: tuple[str, ...] = ()
    denied_resource_groups: tuple[str, ...] = ()
    monitored_resource_types: tuple[str, ...] = DEFAULT_MONITORED_TYPES
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------------

    def environment_of(self, tags: Mapping[str, str]) -> str | None:
        lowered = {str(key).lower(): str(value).strip().lower() for key, value in tags.items()}
        for key in self.environment_tag_keys:
            if key.lower() in lowered:
                return lowered[key.lower()]
        return None

    def owner_of(self, tags: Mapping[str, str]) -> str | None:
        lowered = {str(key).lower(): str(value).strip() for key, value in tags.items()}
        for key in self.owner_tag_keys:
            if key.lower() in lowered and lowered[key.lower()]:
                return lowered[key.lower()]
        return None

    def protected_tag(self, tags: Mapping[str, str]) -> str | None:
        for key in self.protected_tag_keys:
            for raw_key, raw_value in tags.items():
                if str(raw_key).lower() == key.lower() and str(raw_value).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    return raw_key
        return None

    def is_denied_scope(self, *, subscription_id: str, resource_group: str) -> bool:
        subs = {value.lower() for value in self.denied_subscriptions}
        groups = {value.lower() for value in self.denied_resource_groups}
        return subscription_id.lower() in subs or resource_group.lower() in groups

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "min_monthly_savings": str(self.min_monthly_savings),
            "min_idle_days": self.min_idle_days,
            "cpu_idle_peak_percent": self.cpu_idle_peak_percent,
            "idle_network_bytes_per_day": self.idle_network_bytes_per_day,
            "stale_snapshot_days": self.stale_snapshot_days,
            "max_actions_per_run": self.max_actions_per_run,
            "high_severity_savings": str(self.high_severity_savings),
            "allow_prod_actions": self.allow_prod_actions,
            "allow_orphan_deletion": self.allow_orphan_deletion,
            "require_owner_tag": self.require_owner_tag,
            "notify_unclassified": self.notify_unclassified,
            "notify_unclassified_above": str(self.notify_unclassified_above),
            "protected_tag_keys": list(self.protected_tag_keys),
            "environment_tag_keys": list(self.environment_tag_keys),
            "owner_tag_keys": list(self.owner_tag_keys),
            "protected_environments": list(self.protected_environments),
            "denied_subscriptions": list(self.denied_subscriptions),
            "denied_resource_groups": list(self.denied_resource_groups),
            "monitored_resource_types": list(self.monitored_resource_types),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Policy":
        """Build a policy from a JSON document, rejecting unknown keys."""
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise PolicyError("policy must be a JSON object")
        # Keys beginning with "_" are treated as comments/metadata: JSON has no comments,
        # and a policy file benefits from explaining itself.
        comments = {key: value for key, value in data.items() if str(key).startswith("_")}
        known = set(cls().to_dict()) | {"metadata"}
        unknown = sorted(set(data) - known - set(comments))
        if unknown:
            raise PolicyError(
                "unknown policy key(s): " + ", ".join(unknown) + f"; known keys: {', '.join(sorted(known))}"
            )
        fields: dict[str, Any] = {}
        for key, value in data.items():
            if value is None or str(key).startswith("_"):
                continue
            if key in {"min_monthly_savings", "high_severity_savings", "notify_unclassified_above"}:
                try:
                    fields[key] = Decimal(str(value))
                except Exception as error:  # noqa: BLE001 - surface the offending value
                    raise PolicyError(f"policy key {key!r} must be a number, got {value!r}") from error
            elif key in {
                "protected_tag_keys",
                "environment_tag_keys",
                "owner_tag_keys",
                "protected_environments",
                "denied_subscriptions",
                "denied_resource_groups",
                "monitored_resource_types",
            }:
                fields[key] = _string_tuple(key, value)
            elif key in {"allow_prod_actions", "allow_orphan_deletion", "require_owner_tag", "notify_unclassified"}:
                fields[key] = _boolean(key, value)
            elif key in {"cpu_idle_peak_percent"}:
                fields[key] = float(value)
            elif key in {"min_idle_days", "idle_network_bytes_per_day", "stale_snapshot_days", "max_actions_per_run"}:
                try:
                    fields[key] = int(value)
                except (TypeError, ValueError) as error:
                    raise PolicyError(f"policy key {key!r} must be an integer, got {value!r}") from error
            else:
                fields[key] = str(value)
        if comments:
            fields["metadata"] = {**dict(fields.get("metadata") or {}), **comments}
        return cls(**fields)


def _string_tuple(key: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    raise PolicyError(f"policy key {key!r} must be a list of strings")


def _boolean(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    raise PolicyError(f"policy key {key!r} must be a boolean")
