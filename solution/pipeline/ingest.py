"""Orchestrates ingestion of one drop (or a folder of drops). Idempotent per file."""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from .ingest_events import ingest_event_file
from .ingest_leads import ingest_lead_file


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_drops(path: Path) -> list[Path]:
    """A drop has leads/ and/or events/. A parent folder of drops is processed in name order."""
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    drops = sorted((p for p in path.iterdir()
                    if p.is_dir() and ((p / "leads").is_dir() or (p / "events").is_dir())),
                   key=_natural_key)
    if not drops:
        raise SystemExit(f"{path}: no leads/ or events/ folder found")
    return drops


def ingest_path(conn, hasher, path: str, schemas: dict, log=print) -> dict:
    summary = {"ingested": 0, "skipped": 0}
    for drop in find_drops(Path(path)):
        for kind, wf, file in _list_files(drop):
            rel = file.relative_to(drop).as_posix()
            _ingest_file(conn, hasher, schemas, drop.name, rel, kind, wf, file, summary, log)
    return summary


def _list_files(drop: Path):
    leads_dir = drop / "leads"
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            for f in sorted(wf_dir.glob("*.csv")):
                yield "leads", wf_dir.name, f
    events_dir = drop / "events"
    if events_dir.is_dir():
        for f in sorted(events_dir.glob("*.jsonl")):
            yield "events", None, f


def _ingest_file(conn, hasher, schemas, drop_name, rel, kind, wf, file, summary, log):
    sha = _sha256(file)
    prev = conn.execute("SELECT sha256 FROM ingested_files WHERE drop_name=? AND relpath=?",
                        (drop_name, rel)).fetchone()
    if prev:
        if prev[0] != sha:
            raise SystemExit(f"{drop_name}/{rel} was ingested before with DIFFERENT content. "
                             "Use a fresh --db for a new dataset.")
        log(f"skip   {drop_name}/{rel} (already ingested)")
        summary["skipped"] += 1
        return
    if kind == "leads" and wf not in schemas:
        log(f"warn   {drop_name}/{rel}: no schema for workflow {wf!r}, skipped", )
        return

    batch_id = file.stem if kind == "leads" else None
    conn.execute("BEGIN")  # whole file is one transaction: a crash leaves no half-ingested file
    try:
        cur = conn.execute(
            "INSERT INTO ingested_files (drop_name, relpath, kind, workflow_id, batch_id, sha256)"
            " VALUES (?,?,?,?,?,?)", (drop_name, rel, kind, wf, batch_id, sha))
        file_id = cur.lastrowid
        if kind == "leads":
            st = ingest_lead_file(conn, hasher, schemas[wf], file, file_id, batch_id)
            conn.execute("UPDATE ingested_files SET rows_total=?, rows_merged=?, field_warnings=?"
                         " WHERE id=?", (st.rows_total, st.rows_merged, st.field_warnings, file_id))
            log(f"leads  {drop_name}/{rel}: {st.rows_total} rows, {st.rows_merged} merged")
        else:
            st = ingest_event_file(conn, hasher, file, file_id)
            conn.execute("UPDATE ingested_files SET events_total=?, events_malformed=?,"
                         " events_duplicate=? WHERE id=?",
                         (st.events_total, st.events_malformed, st.events_duplicate, file_id))
            log(f"events {drop_name}/{rel}: {st.events_total} lines, "
                f"{st.events_malformed} malformed, {st.events_duplicate} duplicate")
        conn.execute("COMMIT")
        summary["ingested"] += 1
    except BaseException:
        conn.execute("ROLLBACK")
        raise
