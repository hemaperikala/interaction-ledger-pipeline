"""Loads workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import norm_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple = ()
    values: tuple = ()
    default: str | None = None


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    fields: tuple
    alias_map: dict = field(default_factory=dict)  # normalised header -> field name

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")


class Catalog:
    """Disposition catalogue: (workflow, code) -> (is_terminal, is_success)."""

    def __init__(self, rows: dict):
        self._rows = rows

    def known(self, workflow: str, code) -> bool:
        return (workflow, code) in self._rows

    def is_terminal(self, workflow: str, code) -> bool:
        return self._rows[(workflow, code)][0]

    def is_success(self, workflow: str, code) -> bool:
        return self._rows[(workflow, code)][1]


def find_schema_dir(explicit: str | None = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("AU_SCHEMAS"):
        candidates.append(Path(os.environ["AU_SCHEMAS"]))
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:5]:
        candidates.append(parent / "assignment-a-data" / "schemas")
    candidates.append(Path.cwd() / "assignment-a-data" / "schemas")
    for c in candidates:
        if (c / "dispositions.csv").is_file():
            return c
    raise FileNotFoundError("Could not find assignment-a-data/schemas; pass --schemas DIR")


def load_schemas(schema_dir: Path):
    schemas = {}
    for p in sorted(schema_dir.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = tuple(
            Field(
                name=f["name"], type=f["type"],
                required=bool(f.get("required", False)), pii=bool(f.get("pii", False)),
                reference=bool(f.get("reference", False)),
                aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                default=f.get("default"),
            )
            for f in raw["fields"]
        )
        alias_map = {}
        for f in fields:
            for name in (f.name, *f.aliases):
                alias_map.setdefault(norm_header(name), f.name)
        schemas[raw["workflow_id"]] = WorkflowSchema(raw["workflow_id"], fields, alias_map)

    rows = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows[(r["workflow_id"].strip(), r["code"].strip())] = (
                r["is_terminal"].strip().lower() == "true",
                r["is_success"].strip().lower() == "true",
            )
    return schemas, Catalog(rows)
