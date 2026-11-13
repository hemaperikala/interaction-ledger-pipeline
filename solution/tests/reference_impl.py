"""Independent, deliberately naive in-memory implementation of the spec.

Used only by tests as a cross-check against the SQLite pipeline. Reads every file in
order, keeps everything in dicts, no database.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if len(d) == 10 and d[0] in "6789" else None


def run(data_dirs, schemas, catalog):
    """schemas: pipeline.schema objects (reused only for field definitions)."""
    from pipeline.ingest_leads import map_header, validate_row  # same field rules are fine here

    leads = {}            # (wf, ph) -> dict
    rejections = []       # (wf, batch, row, reason)
    stats = defaultdict(int)
    events = []
    seen = set()

    for d in data_dirs:
        d = Path(d)
        for wf_dir in sorted((d / "leads").iterdir()):
            sch = schemas[wf_dir.name]
            for f in sorted(wf_dir.glob("*.csv")):
                rows = list(csv.reader(open(f, newline="", encoding="utf-8-sig")))
                cm = map_header(sch, rows[0])
                n = 0
                for row in rows[1:]:
                    if all(not c.strip() for c in row):
                        continue
                    n += 1
                    stats["lead_rows_total"] += 1
                    cells = {nm: (row[i].strip() if i < len(row) else "") for i, nm in cm.items()}
                    reason, vals, warn = validate_row(sch, cells)
                    if reason:
                        rejections.append((wf_dir.name, f.stem, n, reason))
                        continue
                    stats["field_warnings"] += warn
                    key = (wf_dir.name, vals[sch.phone_field.name][-10:])
                    ref = sch.reference_field.name
                    if key in leads:
                        stats["duplicate_lead_rows_merged"] += 1
                        if vals[ref] is not None:
                            leads[key]["ref"] = vals[ref]
                    else:
                        leads[key] = {"ref": vals[ref], "batch": f.stem}
        for f in sorted((d / "events").glob("*.jsonl")):
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                stats["events_total"] += 1
                try:
                    e = json.loads(line)
                    eid = e["event_id"]
                    assert isinstance(e, dict) and eid
                except Exception:
                    stats["malformed_events"] += 1
                    continue
                if eid in seen:
                    stats["duplicate_events_dropped"] += 1
                    continue
                seen.add(eid)
                events.append(e)

    def ts(s):
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1] + "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)

    # link
    call_lead = {}
    calls = defaultdict(lambda: {"init": None, "conn": False, "fail": False, "set": [], "ovr": []})
    orphan = unknown = 0
    non_ovr = [e for e in events if e["event_type"] != "disposition.override"]
    ovr = [e for e in events if e["event_type"] == "disposition.override"]
    for e in non_ovr:
        p = phone(e.get("phone"))
        key = (e["workflow_id"], p)
        if p is None or key not in leads:
            orphan += 1
            continue
        call_lead[e["call_id"]] = key
        c = calls[e["call_id"]]
        t = ts(e["occurred_at"])
        et = e["event_type"]
        if et == "call.initiated":
            c["init"] = t if c["init"] is None else min(c["init"], t)
        elif et == "call.connected":
            c["conn"] = True
        elif et == "call.telephony_failed":
            c["fail"] = True
        elif et == "disposition.set":
            code = e["payload"].get("disposition_code")
            if (key[0], code) in catalog._rows:
                c["set"].append((t, e["event_id"], code))
            else:
                unknown += 1
    for e in ovr:
        key = call_lead.get(e["call_id"])
        if key is None:
            orphan += 1
            continue
        code = e["payload"].get("disposition_code")
        if (key[0], code) in catalog._rows:
            calls[e["call_id"]]["ovr"].append((ts(e["occurred_at"]), e["event_id"], code))
        else:
            unknown += 1

    def eff(c):
        pool = c["ovr"] or c["set"]
        return max(pool)[2] if pool else None

    per_lead = defaultdict(list)
    for cid, c in calls.items():
        if c["init"] is not None:
            per_lead[call_lead[cid]].append((c["init"], cid, c))
    out_leads, batch, daily = [], defaultdict(lambda: defaultdict(int)), defaultdict(lambda: [0, 0, set()])
    for key, L in leads.items():
        wf = key[0]
        cl = sorted(per_lead.get(key, []), key=lambda x: (x[0], x[1]))
        effs = [eff(c) for _, _, c in cl]
        if not cl:
            b = "yet_to_reach_out"
        elif any(x and catalog.is_terminal(wf, x) for x in effs):
            b = "completed"
        elif all(c["fail"] for _, _, c in cl):
            b = "in_progress_telephony_failed"
        else:
            b = "in_progress_dialed"
        batch[(wf, L["batch"])][b] += 1
        batch[(wf, L["batch"])]["total"] += 1
        for (t, _, c), x in zip(cl, effs):
            dd = daily[(wf, t.astimezone(IST).date().isoformat())]
            dd[0] += 1
            dd[1] += c["conn"]
            if x and catalog.is_success(wf, x):
                dd[2].add(key)
        cur = next((x for x in reversed(effs) if x), "")
        out_leads.append((wf, L["ref"], L["batch"], b, len(cl), sum(c["conn"] for _, _, c in cl),
                          cl[-1][0].astimezone(IST).isoformat(timespec="seconds") if cl else "",
                          cur, any(x and catalog.is_success(wf, x) for x in effs)))
    return {"stats": dict(stats), "orphan": orphan, "unknown": unknown, "leads": sorted(out_leads),
            "batch": {k: dict(v) for k, v in batch.items()},
            "daily": {k: (v[0], v[1], len(v[2])) for k, v in daily.items()},
            "rejections": sorted(rejections)}
