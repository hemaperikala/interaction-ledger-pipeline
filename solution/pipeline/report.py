"""Builds the five report files from the database. Everything is derived at report time."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from .derive import Call, effective_disposition, lead_bucket
from .normalize import ist_date, ist_iso
from .schema import Catalog

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _collect_calls(conn, catalog: Catalog, lead_wf: dict):
    """Link events to leads and group them into calls. Returns (calls, counters)."""
    calls: dict[str, Call] = {}
    call_lead: dict[str, int] = {}
    linked = unknown = 0

    # 1) Everything except overrides links by (workflow_id, phone_hash).
    rows = conn.execute(
        "SELECT e.event_id, e.event_type, e.call_id, e.occurred_at, e.payload_json, l.id "
        "FROM events e JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash "
        "WHERE e.event_type IS NOT 'disposition.override' ORDER BY e.seq")
    for event_id, etype, call_id, ts, payload_json, lead_id in rows:
        linked += 1
        wf = lead_wf[lead_id]
        if call_id is None:
            continue
        call = calls.get(call_id)
        if call is None:
            call = calls[call_id] = Call(call_id, lead_id)
            call_lead[call_id] = lead_id
        if call.lead_id != lead_id:
            continue  # call_id reused by another lead: not expected, ignore the stranger
        if etype == "call.initiated":
            if call.initiated is None or ts < call.initiated:
                call.initiated = ts
        elif etype == "call.connected":
            call.connected = True
        elif etype == "call.telephony_failed":
            call.telephony_failed = True
        elif etype == "disposition.set":
            code = json.loads(payload_json).get("disposition_code")
            if catalog.known(wf, code):
                call.sets.append((ts, event_id, code))
            else:
                unknown += 1

    # 2) Overrides have no phone: they link through the call_id of the call's other events.
    rows = conn.execute("SELECT event_id, call_id, occurred_at, payload_json FROM events "
                        "WHERE event_type = 'disposition.override' ORDER BY seq")
    for event_id, call_id, ts, payload_json in rows:
        lead_id = call_lead.get(call_id)
        if lead_id is None:
            continue  # orphan
        linked += 1
        code = json.loads(payload_json).get("disposition_code")
        if catalog.known(lead_wf[lead_id], code):
            calls[call_id].overrides.append((ts, event_id, code))
        else:
            unknown += 1
    return calls, linked, unknown


def build_report(conn, catalog: Catalog) -> dict:
    leads = conn.execute(
        "SELECT id, workflow_id, batch_id, external_ref, phone_last4 FROM leads").fetchall()
    lead_wf = {r[0]: r[1] for r in leads}

    calls, linked, unknown = _collect_calls(conn, catalog, lead_wf)
    total_stored = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    orphans = total_stored - linked

    by_lead = defaultdict(list)
    for c in calls.values():
        if c.initiated is not None:  # a call counts only if its call.initiated is present
            by_lead[c.lead_id].append(c)

    lead_rows, batch = [], defaultdict(lambda: dict.fromkeys(BUCKETS, 0) | {"total": 0})
    daily = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})

    for lead_id, wf, batch_id, ext_ref, last4 in leads:
        lc = sorted(by_lead.get(lead_id, []), key=lambda c: (c.initiated, c.call_id))
        eff = [effective_disposition(c) for c in lc]
        bucket = lead_bucket(lc, lambda code, wf=wf: catalog.is_terminal(wf, code))
        ever_success = any(code is not None and catalog.is_success(wf, code) for code in eff)
        current = next((code for code in reversed(eff) if code is not None), None)

        lead_rows.append({
            "workflow_id": wf, "external_ref": ext_ref, "batch_id": batch_id,
            "phone_masked": "+91******" + last4, "bucket": bucket,
            "total_calls": len(lc), "connected_calls": sum(c.connected for c in lc),
            "last_call_at": ist_iso(lc[-1].initiated) if lc else "",
            "current_disposition": current or "", "ever_success": "true" if ever_success else "false",
        })
        b = batch[(wf, batch_id)]
        b["total"] += 1
        b[bucket] += 1

        for c, code in zip(lc, eff):
            d = daily[(wf, ist_date(c.initiated))]
            d["total"] += 1
            d["connected"] += c.connected
            if code is not None and catalog.is_success(wf, code):
                d["success"].add(lead_id)

    lead_rows.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    return {"lead_rows": lead_rows, "batch": batch, "daily": daily,
            "orphans": orphans, "unknown": unknown}


def write_reports(conn, catalog: Catalog, out_dir: str, schemas: dict | None = None) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    r = build_report(conn, catalog)

    def write_csv(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)

    write_csv("batch_summary.csv",
              ["workflow_id", "batch_id", "total_leads", *BUCKETS],
              [[wf, bid, b["total"], *(b[k] for k in BUCKETS)]
               for (wf, bid), b in sorted(r["batch"].items())])

    write_csv("daily_overview.csv",
              ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
               "success_leads"],
              [[wf, d, v["total"], v["connected"], f"{v['connected'] / v['total']:.4f}",
                len(v["success"])] for (wf, d), v in sorted(r["daily"].items())])

    cols = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
            "connected_calls", "last_call_at", "current_disposition", "ever_success"]
    write_csv("lead_report.csv", cols, [[row[c] for c in cols] for row in r["lead_rows"]])

    rej = conn.execute(
        "SELECT f.workflow_id, f.batch_id, r.row_number, r.reason FROM lead_rejections r "
        "JOIN ingested_files f ON f.id = r.file_id ORDER BY 1, 2, 3").fetchall()
    write_csv("rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"], rej)

    sums = conn.execute(
        "SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_merged),0), "
        "COALESCE(SUM(field_warnings),0), COALESCE(SUM(events_total),0), "
        "COALESCE(SUM(events_malformed),0), COALESCE(SUM(events_duplicate),0) "
        "FROM ingested_files").fetchone()
    by_reason = {"invalid_phone": 0}
    for sch in (schemas or {}).values():  # list every possible reason, even at 0
        for f in sch.fields:
            if f.required:
                by_reason[f"missing_required:{f.name}"] = 0
    for reason, n in conn.execute(
            "SELECT reason, COUNT(*) FROM lead_rejections GROUP BY reason ORDER BY reason"):
        by_reason[reason] = n
    quality = {
        "lead_rows_total": sums[0],
        "lead_rows_rejected": len(rej),
        "rejections_by_reason": dict(sorted(by_reason.items())),
        "duplicate_lead_rows_merged": sums[1],
        "field_warnings": sums[2],
        "events_total": sums[3],
        "malformed_events": sums[4],
        "duplicate_events_dropped": sums[5],
        "orphan_events": r["orphans"],
        "unknown_disposition_events": r["unknown"],
    }
    (out / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
