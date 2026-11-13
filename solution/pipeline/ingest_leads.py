"""Lead CSV ingestion: header mapping, validation, rejection, merging."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass

from .normalize import norm_header, parse_date, parse_decimal, parse_enum, parse_phone
from .pii import Hasher
from .schema import Field, WorkflowSchema


@dataclass
class LeadFileStats:
    rows_total: int = 0
    rows_merged: int = 0
    field_warnings: int = 0


def parse_value(f: Field, raw: str):
    if f.type == "string":
        return raw
    if f.type == "phone":
        return parse_phone(raw)
    if f.type == "date":
        return parse_date(raw)
    if f.type == "decimal":
        return parse_decimal(raw)
    if f.type == "enum":
        return parse_enum(raw, f.values)
    raise ValueError(f"unknown field type {f.type!r}")


def validate_row(schema: WorkflowSchema, cells: dict):
    """cells: field name -> raw string (already trimmed) or ''.

    Returns (reject_reason | None, values, warnings). Walks fields in schema order;
    the first failure is the row's single rejection reason.
    """
    values, warnings = {}, 0
    for f in schema.fields:
        raw = cells.get(f.name, "")
        if raw == "":
            if f.required:
                return f"missing_required:{f.name}", None, 0
            values[f.name] = None
            continue
        v = parse_value(f, raw)
        if v is None:
            if f.type == "phone":
                return "invalid_phone", None, 0
            if f.required:  # spec is silent: an unusable required value counts as missing
                return f"missing_required:{f.name}", None, 0
            warnings += 1
        values[f.name] = v
    return None, values, warnings


def map_header(schema: WorkflowSchema, header: list) -> dict:
    """column index -> field name. First matching column wins; unmatched columns ignored."""
    idx_to_field, seen = {}, set()
    for i, h in enumerate(header):
        name = schema.alias_map.get(norm_header(h))
        if name and name not in seen:
            idx_to_field[i] = name
            seen.add(name)
    return idx_to_field


def ingest_lead_file(conn, hasher: Hasher, schema: WorkflowSchema, path, file_id: int,
                     batch_id: str) -> LeadFileStats:
    stats = LeadFileStats()
    ref = schema.reference_field.name
    phone_name = schema.phone_field.name
    pii_names = {f.name for f in schema.fields if f.pii and f.type != "phone"}

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return stats
        col_map = map_header(schema, header)

        for row in reader:
            if all(c.strip() == "" for c in row):
                continue  # blank row: skipped, not counted
            stats.rows_total += 1
            row_number = stats.rows_total
            cells = {name: (row[i].strip() if i < len(row) else "") for i, name in col_map.items()}

            reason, values, warnings = validate_row(schema, cells)
            if reason:
                conn.execute("INSERT INTO lead_rejections VALUES (?,?,?)",
                             (file_id, row_number, reason))
                continue
            stats.field_warnings += warnings

            phone = values[phone_name]
            phone_hash = hasher.phone(schema.workflow_id, phone)
            existing = conn.execute(
                "SELECT id, external_ref, fields_json, pii_json FROM leads "
                "WHERE workflow_id=? AND phone_hash=?", (schema.workflow_id, phone_hash)).fetchone()

            if existing is None:
                fields = {k: v for k, v in values.items()
                          if k not in (phone_name, ref) and k not in pii_names}
                pii = {k: hasher.digest(f"pii:{schema.workflow_id}:{k}", values[k])
                       for k in pii_names if values[k] is not None}
                _apply_defaults(schema, fields)
                conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref,"
                    " fields_json, pii_json, file_id) VALUES (?,?,?,?,?,?,?,?)",
                    (schema.workflow_id, phone_hash, phone[-4:], batch_id, values[ref],
                     json.dumps(fields, sort_keys=True), json.dumps(pii, sort_keys=True), file_id))
            else:
                # Duplicate: last write wins, but only for non-null values. batch_id is untouched.
                lead_id, ext_ref, fields_json, pii_json = existing
                fields, pii = json.loads(fields_json), json.loads(pii_json)
                for k, v in values.items():
                    if v is None or k == phone_name:
                        continue
                    if k == ref:
                        ext_ref = v
                    elif k in pii_names:
                        pii[k] = hasher.digest(f"pii:{schema.workflow_id}:{k}", v)
                    else:
                        fields[k] = v
                _apply_defaults(schema, fields)
                conn.execute("UPDATE leads SET external_ref=?, fields_json=?, pii_json=? WHERE id=?",
                             (ext_ref, json.dumps(fields, sort_keys=True),
                              json.dumps(pii, sort_keys=True), lead_id))
                stats.rows_merged += 1
    return stats


def _apply_defaults(schema: WorkflowSchema, fields: dict) -> None:
    """Defaults fill fields that are still null/absent after merging."""
    for f in schema.fields:
        if f.default is not None and f.type != "phone" and not f.pii and not f.reference:
            if fields.get(f.name) is None:
                fields[f.name] = f.default
