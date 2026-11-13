"""SQLite schema and connection helper."""
from __future__ import annotations

import sqlite3

from .pii import Hasher, load_key

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

-- One row per ingested file. UNIQUE(drop_name, relpath) is what makes re-ingest a no-op.
CREATE TABLE IF NOT EXISTS ingested_files (
    id            INTEGER PRIMARY KEY,
    drop_name     TEXT NOT NULL,
    relpath       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id   TEXT,              -- leads files only
    batch_id      TEXT,              -- leads files only (file stem)
    sha256        TEXT NOT NULL,
    rows_total    INTEGER NOT NULL DEFAULT 0,   -- non-blank data rows
    rows_merged   INTEGER NOT NULL DEFAULT 0,   -- valid rows that merged into an existing lead
    field_warnings INTEGER NOT NULL DEFAULT 0,  -- only counted for non-rejected rows
    events_total  INTEGER NOT NULL DEFAULT 0,   -- every non-empty line
    events_malformed INTEGER NOT NULL DEFAULT 0,
    events_duplicate INTEGER NOT NULL DEFAULT 0,
    UNIQUE (drop_name, relpath)
);

-- A lead = (workflow, phone). No plaintext PII: phone is a keyed hash + last 4 digits.
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,
    phone_last4  TEXT NOT NULL,
    batch_id     TEXT NOT NULL,       -- batch of the FIRST valid row; never changes
    external_ref TEXT NOT NULL,       -- the schema's "reference" field (policy_number / order_id)
    fields_json  TEXT NOT NULL,       -- non-PII fields (expiry, premium, language, ...)
    pii_json     TEXT NOT NULL,       -- other PII fields as keyed digests only
    file_id      INTEGER NOT NULL REFERENCES ingested_files(id),
    UNIQUE (workflow_id, phone_hash)
);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads (workflow_id, batch_id);

-- Rejected lead rows. No PII, only the reason.
CREATE TABLE IF NOT EXISTS lead_rejections (
    file_id    INTEGER NOT NULL REFERENCES ingested_files(id),
    row_number INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    PRIMARY KEY (file_id, row_number)
);

-- The interaction ledger: append-only (enforced by triggers below).
-- Every well-formed, non-duplicate event is stored, even if its lead is unknown yet.
-- Linking to leads happens at report time, so late-arriving leads "adopt" earlier events.
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,   -- ingest order
    event_id     TEXT NOT NULL UNIQUE,                -- dedupe key, across all drops
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT NOT NULL,                       -- UTC, fixed width, sortable
    phone_hash   TEXT,                                -- NULL when absent/invalid
    payload_json TEXT NOT NULL,
    file_id      INTEGER NOT NULL REFERENCES ingested_files(id)
);
CREATE INDEX IF NOT EXISTS idx_events_phone ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


def connect(db_path: str) -> tuple[sqlite3.Connection, Hasher]:
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit BEGIN/COMMIT
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(DDL)
    hasher = Hasher(load_key(db_path))
    row = conn.execute("SELECT value FROM meta WHERE key='pii_key_check'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('pii_key_check', ?)", (hasher.key_check(),))
    elif row[0] != hasher.key_check():
        raise SystemExit("PII key does not match the one this database was created with "
                         "(set AU_PII_KEY or restore the .key file).")
    return conn, hasher
