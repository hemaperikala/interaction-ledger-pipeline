import csv
import filecmp
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.db import connect
from pipeline.ingest import ingest_path
from pipeline.report import write_reports
from pipeline.schema import find_schema_dir, load_schemas

SCHEMAS, CATALOG = load_schemas(find_schema_dir())
REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"
REPORTS = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
           "data_quality.json"]


def run(db, drops, out, log=lambda *_: None):
    conn, hasher = connect(str(db))
    for d in drops:
        ingest_path(conn, hasher, str(d), SCHEMAS, log)
    write_reports(conn, CATALOG, str(out), SCHEMAS)
    conn.close()


def read(out, name):
    with open(Path(out) / name, newline="") as f:
        return list(csv.DictReader(f))


def write_leads(path, rows, header="customer_name,phone,policy_number\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "".join(rows), encoding="utf-8")


def write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n")


def ev(i, typ, call, ts, phone="9876543210", payload=None, wf="motor_renewal"):
    e = {"event_id": f"e{i}", "event_type": typ, "workflow_id": wf, "call_id": call,
         "occurred_at": ts, "payload": payload or {}}
    if phone:
        e["phone"] = phone
    return e


# ---------------------------------------------------------------- hand-built scenarios
def test_event_before_lead_is_adopted_and_overrides_link_by_call_id(tmp_path):
    d1, d2 = tmp_path / "drop_1", tmp_path / "drop_2"
    write_events(d1 / "events" / "e1.jsonl", [
        ev(1, "call.initiated", "c1", "2026-10-03T19:00:00Z"),            # IST: 4 Oct 00:30
        ev(2, "call.connected", "c1", "2026-10-04 00:30:20"),
        ev(3, "disposition.set", "c1", "2026-10-04T00:32:00+05:30", payload={"disposition_code": "payment_link_sent"}),
        ev(4, "disposition.override", "c1", "2026-10-05T10:00:00Z", phone=None,
           payload={"disposition_code": "renewed", "overridden_by": "s1", "reason": "qa"}),
        ev(5, "call.initiated", "cX", "2026-10-04T10:00:00Z", phone="9000000000"),   # never uploaded
    ])
    write_leads(d2 / "leads" / "motor_renewal" / "batch_003.csv", ["Asha,+91 98765 43210,P1\n"])

    # after drop_1 alone: everything is an orphan, there are no leads
    run(tmp_path / "a.db", [d1], tmp_path / "out1")
    assert json.load(open(tmp_path / "out1" / "data_quality.json"))["orphan_events"] == 5

    # after drop_2: the earlier events now count (state is derived at report time)
    run(tmp_path / "a.db", [d1, d2], tmp_path / "out2")
    q = json.load(open(tmp_path / "out2" / "data_quality.json"))
    assert q["orphan_events"] == 1
    lead = read(tmp_path / "out2", "lead_report.csv")[0]
    assert lead["bucket"] == "completed"                 # via the override
    assert lead["current_disposition"] == "renewed"
    assert lead["ever_success"] == "true"
    assert lead["last_call_at"] == "2026-10-04T00:30:00+05:30"
    assert lead["phone_masked"] == "+91******3210"
    daily = read(tmp_path / "out2", "daily_overview.csv")[0]
    assert daily["date"] == "2026-10-04" and daily["success_leads"] == "1"
    assert daily["connectivity_rate"] == "1.0000"


def test_lead_rules_rejection_merge_blank_rows_and_batch_id(tmp_path):
    d1 = tmp_path / "drop_1" / "leads" / "motor_renewal"
    write_leads(d1 / "batch_001.csv", [
        "Asha,9876543210,P1\n",
        ",,\n",                                  # blank: not counted
        "Bob,12345,P2\n",                        # invalid phone  (row 2)
        " ,12345,P3\n",                          # missing name wins over bad phone (row 3)
        "Cid,9876543211,\n",                     # missing policy (row 4)
        "Dee,9876543212,P4\n",
    ], header="customer_name,phone,policy_number,premium_amount,preferred_language\n")
    write_leads(d1 / "batch_002.csv", [
        "Asha B,+91-98765-43210,P1-NEW,Rs. 500,HI\n",    # duplicate: overwrite non-null only
    ], header="Insured Name,Mobile No,Policy No,Premium,Language,agent_notes\n")
    run(tmp_path / "b.db", [tmp_path / "drop_1"], tmp_path / "out")

    rej = read(tmp_path / "out", "rejections.csv")
    assert [(r["batch_id"], r["row_number"], r["reason"]) for r in rej] == [
        ("batch_001", "2", "invalid_phone"),
        ("batch_001", "3", "missing_required:customer_name"),
        ("batch_001", "4", "missing_required:policy_number"),
    ]
    leads = {r["external_ref"]: r for r in read(tmp_path / "out", "lead_report.csv")}
    assert set(leads) == {"P1-NEW", "P4"}            # reference overwritten by the duplicate
    assert leads["P1-NEW"]["batch_id"] == "batch_001"  # batch_id never changes
    q = json.load(open(tmp_path / "out" / "data_quality.json"))
    assert q["duplicate_lead_rows_merged"] == 1
    assert q["lead_rows_total"] == 6 and q["lead_rows_rejected"] == 3
    assert q["rejections_by_reason"]["missing_required:order_id"] == 0   # zero-filled

    # PII is not stored in plaintext anywhere in the database file
    raw = (tmp_path / "b.db").read_bytes() + (tmp_path / "b.db-wal").read_bytes() \
        if (tmp_path / "b.db-wal").exists() else (tmp_path / "b.db").read_bytes()
    for secret in (b"9876543210", b"Asha", b"Dee"):
        assert secret not in raw


def test_event_line_handling_and_unknown_dispositions(tmp_path):
    d = tmp_path / "drop_1"
    write_leads(d / "leads" / "motor_renewal" / "batch_001.csv", ["Asha,9876543210,P1\n"])
    good = ev(1, "call.initiated", "c1", "2026-10-01T05:00:00Z")
    write_events(d / "events" / "e.jsonl", [
        good, good,                                                  # exact duplicate
        '{"event_id": "e9", "event_type": "call.ini',                # truncated JSON
        '{"event_type": "call.initiated"}',                          # no event_id
        "",                                                           # empty line: not counted
        ev(2, "disposition.set", "c1", "2026-10-01T05:10:00Z", payload={"disposition_code": "RENEWD"}),
        ev(3, "disposition.set", "c1", "2026-10-01T05:11:00Z", payload={"disposition_code": "not_interested"}),
        ev(4, "disposition.set", "c1", "2026-10-01T05:12:00Z", phone="9876543299",
           payload={"disposition_code": "RENEWD"}),                  # orphan (unknown lead) wins over unknown
    ])
    run(tmp_path / "c.db", [d], tmp_path / "out")
    q = json.load(open(tmp_path / "out" / "data_quality.json"))
    assert (q["events_total"], q["malformed_events"], q["duplicate_events_dropped"]) == (7, 2, 1)
    assert (q["orphan_events"], q["unknown_disposition_events"]) == (1, 1)
    lead = read(tmp_path / "out", "lead_report.csv")[0]
    assert lead["current_disposition"] == "not_interested" and lead["bucket"] == "completed"


def test_unknown_override_code_is_ignored(tmp_path):
    d = tmp_path / "drop_1"
    write_leads(d / "leads" / "motor_renewal" / "batch_001.csv", ["Asha,9876543210,P1\n"])
    write_events(d / "events" / "e.jsonl", [
        ev(1, "call.initiated", "c1", "2026-10-01T05:00:00Z"),
        ev(2, "disposition.set", "c1", "2026-10-01T05:10:00Z", payload={"disposition_code": "renewed"}),
        ev(3, "disposition.override", "c1", "2026-10-02T05:10:00Z", phone=None,
           payload={"disposition_code": "bogus"}),
    ])
    run(tmp_path / "d.db", [d], tmp_path / "out")
    assert read(tmp_path / "out", "lead_report.csv")[0]["current_disposition"] == "renewed"


def test_call_without_initiated_is_not_counted(tmp_path):
    d = tmp_path / "drop_1"
    write_leads(d / "leads" / "motor_renewal" / "batch_001.csv", ["Asha,9876543210,P1\n"])
    write_events(d / "events" / "e.jsonl", [ev(1, "call.connected", "c1", "2026-10-01T05:00:00Z")])
    run(tmp_path / "e.db", [d], tmp_path / "out")
    lead = read(tmp_path / "out", "lead_report.csv")[0]
    assert lead["bucket"] == "yet_to_reach_out" and lead["total_calls"] == "0"
    assert read(tmp_path / "out", "daily_overview.csv") == []


def test_changed_file_content_is_refused(tmp_path):
    d = tmp_path / "drop_1"
    write_leads(d / "leads" / "motor_renewal" / "batch_001.csv", ["Asha,9876543210,P1\n"])
    run(tmp_path / "f.db", [d], tmp_path / "out")
    write_leads(d / "leads" / "motor_renewal" / "batch_001.csv", ["Zed,9876543210,P9\n"])
    with pytest.raises(SystemExit):
        run(tmp_path / "f.db", [d], tmp_path / "out")


def test_ledger_is_append_only(tmp_path):
    import sqlite3
    d = tmp_path / "drop_1"
    write_events(d / "events" / "e.jsonl", [ev(1, "call.initiated", "c1", "2026-10-01T05:00:00Z")])
    run(tmp_path / "g.db", [d], tmp_path / "out")
    conn, _ = connect(str(tmp_path / "g.db"))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET call_id='x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events")


# ---------------------------------------------------------------- generated data
@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    if not GENERATOR.exists():
        pytest.skip("generator not found")
    out = {}
    for seed in (42, 7):
        d = tmp_path_factory.mktemp(f"seed{seed}")
        subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small",
                        "--out", str(d)], check=True, capture_output=True)
        out[seed] = d
    return out


def same_reports(a, b):
    return all((Path(a) / n).read_bytes() == (Path(b) / n).read_bytes() for n in REPORTS)


@pytest.mark.parametrize("seed", [42, 7])
def test_incremental_equals_all_at_once_and_reingest_is_noop(generated, tmp_path, seed):
    data = generated[seed]
    d1, d2 = data / "drop_1", data / "drop_2"

    run(tmp_path / "inc.db", [d1], tmp_path / "after1")
    run(tmp_path / "inc.db", [d2], tmp_path / "inc")
    run(tmp_path / "all.db", [data], tmp_path / "all")          # parent folder: drops in name order
    assert same_reports(tmp_path / "inc", tmp_path / "all")

    # re-ingesting already-ingested drops changes nothing
    logs = []
    run(tmp_path / "inc.db", [d1, d2, d1], tmp_path / "again", log=logs.append)
    assert same_reports(tmp_path / "inc", tmp_path / "again")
    assert all("skip" in m for m in logs)

    # drop_1 on its own gives a different (partial) picture: leads/events really are incremental
    assert not same_reports(tmp_path / "after1", tmp_path / "inc")


@pytest.mark.parametrize("seed", [42, 7])
def test_matches_independent_reference_implementation(generated, tmp_path, seed):
    sys.path.insert(0, str(Path(__file__).parent))
    from reference_impl import run as ref_run

    data = generated[seed]
    run(tmp_path / "x.db", [data], tmp_path / "out")
    ref = ref_run([data / "drop_1", data / "drop_2"], SCHEMAS, CATALOG)

    q = json.load(open(tmp_path / "out" / "data_quality.json"))
    for k, v in ref["stats"].items():
        assert q[k] == v, k
    assert q["orphan_events"] == ref["orphan"]
    assert q["unknown_disposition_events"] == ref["unknown"]

    got = [(r["workflow_id"], r["external_ref"], r["batch_id"], r["bucket"], int(r["total_calls"]),
            int(r["connected_calls"]), r["last_call_at"], r["current_disposition"],
            r["ever_success"] == "true") for r in read(tmp_path / "out", "lead_report.csv")]
    assert got == ref["leads"]

    got_batch = {(r["workflow_id"], r["batch_id"]): {k: int(v) for k, v in r.items()
                 if k not in ("workflow_id", "batch_id") and int(v)}
                 for r in read(tmp_path / "out", "batch_summary.csv")}
    ref_batch = {k: {("total_leads" if kk == "total" else kk): vv for kk, vv in v.items() if vv}
                 for k, v in ref["batch"].items()}
    assert got_batch == ref_batch

    got_daily = {(r["workflow_id"], r["date"]): (int(r["total_calls"]), int(r["connected_calls"]),
                 int(r["success_leads"])) for r in read(tmp_path / "out", "daily_overview.csv")}
    assert got_daily == ref["daily"]
    assert [(r["workflow_id"], r["batch_id"], int(r["row_number"]), r["reason"])
            for r in read(tmp_path / "out", "rejections.csv")] == ref["rejections"]


def test_no_plaintext_phone_in_database(generated, tmp_path):
    run(tmp_path / "pii.db", [generated[42]], tmp_path / "out")
    conn, _ = connect(str(tmp_path / "pii.db"))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = (tmp_path / "pii.db").read_bytes()
    assert b"+91" not in raw and b"Sharma" not in raw
