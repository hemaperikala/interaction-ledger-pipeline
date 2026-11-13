"""Event JSONL ingestion into the append-only ledger.

Per line, in order: malformed -> duplicate -> store. Linking (orphans) and unknown
dispositions are decided at report time, because a lead may arrive in a later drop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .normalize import parse_phone, parse_timestamp, to_db_ts
from .pii import Hasher


@dataclass
class EventFileStats:
    events_total: int = 0
    events_malformed: int = 0
    events_duplicate: int = 0


def _parse_line(line: str, hasher: Hasher, cache: dict):
    """Returns a row tuple for the events table, or None if malformed."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    eid = obj.get("event_id")
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or str(eid).strip() == "":
        return None
    ts = parse_timestamp(obj.get("occurred_at"))
    if ts is None:  # decision: an event we cannot place in time is malformed
        return None

    wf = obj.get("workflow_id")
    wf = wf if isinstance(wf, str) and wf else None
    phone_hash = None
    raw_phone = obj.get("phone")
    if wf and raw_phone is not None:
        key = (wf, raw_phone)
        if key not in cache:
            canon = parse_phone(str(raw_phone))
            cache[key] = hasher.phone(wf, canon) if canon else None
        phone_hash = cache[key]

    etype = obj.get("event_type")
    call_id = obj.get("call_id")
    payload = obj.get("payload")
    return (
        str(eid).strip(),
        etype if isinstance(etype, str) else None,
        wf,
        str(call_id) if call_id not in (None, "") else None,
        to_db_ts(ts),
        phone_hash,
        json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True),
    )


def ingest_event_file(conn, hasher: Hasher, path, file_id: int) -> EventFileStats:
    stats = EventFileStats()
    cache: dict = {}
    sql = ("INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at,"
           " phone_hash, payload_json, file_id) VALUES (?,?,?,?,?,?,?,?)")
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            stats.events_total += 1
            row = _parse_line(line, hasher, cache)
            if row is None:
                stats.events_malformed += 1
                continue
            cur = conn.execute(sql, (*row, file_id))
            if cur.rowcount == 0:  # event_id already in the ledger (any drop)
                stats.events_duplicate += 1
    return stats
