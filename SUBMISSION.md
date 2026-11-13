# Submission

## Candidate
- **Name:** Hemalatha Perikala
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 4 hours

## How to run
```bash
git clone <repo> && cd <repo>
python -m venv .venv && .venv\Scripts\activate      # Windows (use source .venv/bin/activate on Linux/macOS)
pip install pytest                                    # only needed for tests; the pipeline is stdlib-only
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

cd solution
python -m pipeline ingest ../data/drop_1
python -m pipeline ingest ../data/drop_2
python -m pipeline report --out reports/
python -m pytest -q
```
Use a fresh `--db` (or delete `pipeline.db*`) when switching to a different seed. More in `solution/README.md`.

## Design

**Principle: store facts at ingest, derive everything at report time.** Ingest only validates,
normalises and appends. Linking events to leads, orphan status, effective dispositions and buckets are
computed from the database each time `report` runs. That is what makes "an event that was an orphan
after drop_1 counts once its lead arrives in drop_2" and "drop_1 then drop_2 == everything at once"
true by construction, with no back-fill step to get wrong.

SQLite, 5 tables (`solution/pipeline/db.py`):

| Table | Purpose |
|---|---|
| `ingested_files` | One row per file, `UNIQUE(drop_name, relpath)` + sha256 + per-file counters (rows, merges, warnings, malformed/duplicate events). The unique key makes re-ingest a no-op; a changed file under the same key is refused. |
| `leads` | One row per `(workflow_id, phone_hash)` (UNIQUE). `batch_id` is set once, by the first valid row. `external_ref` = the schema's `reference` field. Other non-PII fields in `fields_json`. |
| `lead_rejections` | `(file_id, row_number, reason)`. No PII. |
| `events` | The append-only ledger. `event_id` UNIQUE (dedupe across drops). `UPDATE`/`DELETE` are blocked by triggers. Stores every well-formed, non-duplicate event, linked or not. |
| `meta` | Key-check value so a wrong PII key is detected instead of silently mis-matching. |

**Each file is ingested in one transaction.** A crash mid-file rolls back, and the file is simply
processed again on the next run.

**PII at rest.** No plaintext phone or name is stored. The phone is `HMAC-SHA256(key, "phone:<workflow>|+91…")`,
used as the join key between leads and events (events carry only the hash too); the last 4 digits are
kept for the masked report column. `customer_name` (the other `pii: true` field) is stored only as a keyed
digest. The key is outside the DB (env var, or a `.key` file next to it). Tested: a byte-scan of the DB file
finds no phone or name. Trade-off below.

**Report time.** Events are joined to leads in SQL on `(workflow_id, phone_hash)`; overrides (no phone) are
linked through the `call_id` of the call's other events. Calls are grouped in Python; the rules live in
`derive.py` as small pure functions (`effective_disposition`, `lead_bucket`) so they are unit-tested directly.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Orphan and unknown-disposition status decided at report time, not ingest | A lead may arrive in a later drop; the spec says counts reflect the final state | Re-scan "pending orphans" on every lead ingest (more state, more bugs) |
| Orphan check comes before the unknown-disposition check | Spec order. An event with an unknown code *and* an unknown lead counts only as an orphan | – |
| Event with unparseable `occurred_at` counts as malformed | We cannot place it in time; spec only defines malformed as bad JSON / no `event_id` | Store with NULL time and treat as orphan |
| Required, non-phone field present but invalid → `missing_required:<field>` | Spec defines only two reasons; an unusable required value is as good as missing. Not exercised by the given schemas | Add a new reason code |
| Phone checked per schema order: missing required name beats invalid phone | "First failure in schema order" | – |
| Overrides link by `call_id` only (even if they carry a phone) | Spec says so; avoids two linking paths | Fall back to phone |
| Ties on `occurred_at` broken by `event_id` | Deterministic output | Ingest order (depends on drop order) |
| Decimal rounding is half-up | Finance convention; Python's default is banker's. Not used in any report | – |
| Duplicate overwrites `external_ref` if its value is non-null | "Last write wins on non-null fields" applies to every field; reference included | Freeze the first ref |
| Defaults applied after every merge to still-null fields | Spec; `en` for language | – |
| `batch_summary` lists only batches that created at least one lead | Rows come from leads; an all-rejected batch has no leads | Emit a zero row |
| Two columns mapping to the same field: first column wins | Spec is silent | Merge / error |
| Names stored as digest only (no reversible encryption) | This is a reporting ledger and never needs to read names back; avoids a crypto dependency and rolling my own cipher | Fernet/AES-GCM with a KMS key |
| Files keyed by `(drop folder name, relative path)` | Cheap, readable; sha256 catches silent content changes | Content hash alone (would double-ingest a regenerated dataset) |

## Failure modes considered
- **Re-run of a drop:** every file is skipped (tested; 0.1s on the large dataset). Drop 2 alone, drop 1+2, and 1+2+1 all give byte-identical reports to a single all-at-once ingest (tested on two seeds).
- **Crash mid-ingest:** one transaction per file, so it is all-or-nothing.
- **Events before leads / overrides in a different drop than their call:** handled by report-time linking (tested).
- **Bad input:** truncated JSON, non-object JSON, missing `event_id`, blank CSV rows, short rows, BOM, unknown/extra columns, unknown workflow folder (skipped with a warning).
- **Wrong or lost PII key:** detected via the key check; the run aborts rather than mis-linking.
- **Content changed under the same path:** refused with a clear message.
- **Not handled:** concurrent writers (SQLite, single process assumed); `rejections.csv`/`lead_report` are fully rebuilt each time (fine at this size); if the key file is lost the DB can't be extended; a `call_id` shared by two different leads is ignored beyond the first.

## Testing
82 tests (`python -m pytest -q`, ~3s):
- Parsers: all six phone formats, every invalid-phone shape, day-first dates incl. leap years and impossible dates, currency prefixes, rounding, enums, header normalisation, the three timestamp forms and IST date rollover.
- Rules: effective disposition (override beats later set, latest override wins, unknown override ignored), all four buckets including override-undoes-terminal.
- Scenarios built by hand: event-before-lead, override linking, rejection order and row numbering with blank rows, merge semantics, malformed/duplicate/orphan/unknown counting, changed-file refusal, append-only triggers, no plaintext PII in the DB file.
- **Cross-check:** `tests/reference_impl.py` is a separate, naive in-memory implementation of the spec; every report is compared against it on seeds 42 and 7. Caveat: it shares the field-validation function with the pipeline, so the parsers are covered by the unit tests rather than the cross-check.
- Not tested: concurrent ingest, disk-full/kill -9 at arbitrary points (relies on SQLite transactions).

## Results
Large dataset (`--scale large`, seed 42: ~144k lead rows, ~633k event lines), measured in the Linux sandbox where I developed it (not re-run on my laptop):
- ingest drop_1: 16.1s, drop_2: 21.4s (total **~38s**), report: **~7s**; re-ingest: 0.1s. DB ~264 MB.
- Small seed 42: 2,057 lead rows, 108 rejected, 57 merged duplicates, 8,745 events, 8 malformed, 237 duplicates, 462 orphans, 18 unknown dispositions.
- Surprises: ~5% of lead rows are rejected; many orphans are legitimate (calls to numbers never uploaded, or to rows that were rejected).

## If I had another day
1. Truly incremental reporting: keep per-call and per-lead summary tables updated at ingest and only recompute leads touched by a new file. Trade-off: more state and invalidation logic (a new lead can change old events' status), so I chose correctness-by-recomputation first.
2. Move aggregation into SQL (window functions for latest-per-call) and benchmark against the Python grouping.
3. Reversible field encryption (AES-GCM, key from a KMS) if names must ever be read back; key rotation.
4. Production at 100k leads/day: object-store landing zone → scheduled job (hourly) that ingests new files only (the `ingested_files` ledger is already the idempotency token); Postgres instead of SQLite; alert on rejection-rate or orphan-rate spikes; quarantine tables for malformed lines instead of counts; backfills = re-run `report` (derived), or replay files into a fresh DB; failed files retried independently because each file is atomic.

