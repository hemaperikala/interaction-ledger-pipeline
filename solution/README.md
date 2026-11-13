# Assignment A: Interaction Ledger & Reporting Pipeline

Python 3.9+, standard library only (pytest for tests).

```bash
cd solution
python -m pipeline ingest ../data/drop_1          # idempotent, safe to re-run
python -m pipeline ingest ../data/drop_2
python -m pipeline report --out reports/
python -m pytest -q
```

* `ingest` takes one drop, or a parent folder of drops (processed in natural name order).
* Options for both commands: `--db pipeline.db` (default, in the current folder), `--schemas DIR`
  (default: auto-detected `assignment-a-data/schemas`).
* **New dataset (e.g. another seed) = new database**: delete `pipeline.db*` or pass a fresh `--db`.
  The pipeline refuses to re-ingest a file under the same drop/path if its content changed.
* PII key: env `AU_PII_KEY`, otherwise a random key is created in `<db>.key` on first use.
  Keep it with the database; without it, new events cannot be matched to existing leads.

Layout: `normalize.py` (parsers) · `schema.py` · `pii.py` · `db.py` (DDL) · `ingest_leads.py` ·
`ingest_events.py` · `ingest.py` (orchestration/idempotency) · `derive.py` (disposition & bucket
rules) · `report.py` · `cli.py`.
