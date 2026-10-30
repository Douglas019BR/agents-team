"""Load and validate agents-team artifacts against the versioned schemas."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

# logical name -> schema file
SCHEMAS = {
    "finding-report": "finding-report.schema.json",
    "state": "state.schema.json",
    "config": "agents-team-config.schema.json",
    "team-config": "team-config.schema.json",
    "agent-definition": "agent-definition.schema.json",
}


@cache
def _registry() -> Registry:
    resources = []
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@cache
def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / SCHEMAS[name]).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def errors(name: str, data: Any) -> list[str]:
    """Human-readable validation errors; empty when the document is valid."""
    found = sorted(validator(name).iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in found]


def load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yml", ".yaml"}:
        return yaml.safe_load(text)
    return json.loads(text)
