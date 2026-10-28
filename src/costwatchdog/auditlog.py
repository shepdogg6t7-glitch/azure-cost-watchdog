"""Append-only audit trail.

Every audit and every executed action is written as one JSON object per line. The log is
the record of *why* something was stopped, which is the question you get asked later.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    """Writes JSONL events. A ``None`` path disables logging."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + os.linesep)
        return entry

    def record_result(self, result: Any, *, mode: str, scope: str | None = None) -> None:
        """Log a whole audit result: one run event plus one event per decision."""
        decisions = list(getattr(result, "decisions", []))
        self.record(
            "audit",
            mode=mode,
            scope=scope or "",
            currency=result.currency,
            observed_days=result.observed_days,
            resources_considered=result.resources_considered,
            total_monthly_cost=float(result.total_monthly_cost),
            identified_monthly_waste=float(result.identified_monthly_waste),
            actionable_monthly_savings=float(result.actionable_monthly_savings),
            pending_monthly_savings=float(result.pending_monthly_savings),
            decision_count=len(decisions),
        )
        for decision in decisions:
            self.record("decision", mode=mode, **decision.to_dict())
