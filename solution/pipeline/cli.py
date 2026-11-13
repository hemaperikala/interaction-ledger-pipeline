"""Command line interface.

    python -m pipeline ingest <drop dir or parent of drops> [--db pipeline.db]
    python -m pipeline report --out reports/ [--db pipeline.db]
"""
from __future__ import annotations

import argparse
import time

from .db import connect
from .ingest import ingest_path
from .report import write_reports
from .schema import find_schema_dir, load_schemas


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="pipeline.db", help="SQLite file (default: pipeline.db)")
    common.add_argument("--schemas", default=None, help="folder with *.json + dispositions.csv")

    ap = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("ingest", parents=[common], help="ingest a drop (idempotent)")
    p_in.add_argument("path")
    p_rep = sub.add_parser("report", parents=[common], help="write the five report files")
    p_rep.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    schemas, catalog = load_schemas(find_schema_dir(args.schemas))
    conn, hasher = connect(args.db)
    t0 = time.time()
    if args.cmd == "ingest":
        s = ingest_path(conn, hasher, args.path, schemas)
        print(f"done: {s['ingested']} files ingested, {s['skipped']} skipped "
              f"({time.time() - t0:.1f}s)")
    else:
        write_reports(conn, catalog, args.out, schemas)
        print(f"reports written to {args.out} ({time.time() - t0:.1f}s)")
    return 0
