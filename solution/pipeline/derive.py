"""Business rules for derived facts. Pure functions, no database."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Call:
    call_id: str
    lead_id: int
    initiated: str | None = None          # earliest call.initiated timestamp (DB string)
    connected: bool = False
    telephony_failed: bool = False
    sets: list = field(default_factory=list)       # (occurred_at, event_id, code)
    overrides: list = field(default_factory=list)  # (occurred_at, event_id, code)


def effective_disposition(call: Call):
    """Latest override (by occurred_at) wins; else latest disposition.set; else None.

    The ledger is never edited: overrides are just later events.
    """
    if call.overrides:
        return max(call.overrides)[2]
    if call.sets:
        return max(call.sets)[2]
    return None


def lead_bucket(calls: list, is_terminal) -> str:
    """First match wins. `calls` are counted calls (call.initiated present).

    is_terminal(code) -> bool, for the lead's workflow.
    """
    if not calls:
        return "yet_to_reach_out"
    for c in calls:
        code = effective_disposition(c)
        if code is not None and is_terminal(code):
            return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"
