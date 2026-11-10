"""Turn a service's request JSON Schema into simple form fields, and back.

Covers what pydantic emits for flat models: strings, numbers, integers,
booleans, enums, optionals. Anything nested or unusual becomes a ``json`` field
the user fills with a JSON literal, so every schema stays callable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_MISSING = object()


@dataclass
class FieldSpec:
    name: str
    kind: str  # string | number | integer | boolean | enum | json
    title: str = ""
    description: str = ""
    required: bool = False
    nullable: bool = False
    default: Any = _MISSING
    choices: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING

    def initial(self) -> Any:
        """The value a form control starts with: text for inputs, bool for switches."""
        value = None if not self.has_default else self.default
        if self.kind == "boolean":
            return bool(value)
        if value is None:
            return ""
        if self.kind in ("json",):
            return json.dumps(value)
        return str(value)

    def hint(self) -> str:
        parts = []
        if self.minimum is not None and self.maximum is not None:
            parts.append(f"{self.minimum:g} to {self.maximum:g}")
        elif self.minimum is not None:
            parts.append(f"min {self.minimum:g}")
        elif self.maximum is not None:
            parts.append(f"max {self.maximum:g}")
        if self.description:
            parts.append(self.description)
        return " · ".join(parts)


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while "$ref" in schema and seen < 8:
        target: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        schema = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
        seen += 1
    if len(schema.get("allOf", [])) == 1:
        inner = _resolve(schema["allOf"][0], root)
        schema = {**inner, **{k: v for k, v in schema.items() if k != "allOf"}}
    return schema


def fields_from_schema(schema: dict[str, Any]) -> list[FieldSpec]:
    """One field per top-level property. Empty for an empty or non-object schema."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required", []))
    fields = []
    for name, raw in props.items():
        prop = _resolve(raw, schema)
        nullable = False
        variants = prop.get("anyOf") or prop.get("oneOf")
        if variants:
            rest = [v for v in variants if v.get("type") != "null"]
            nullable = len(rest) != len(variants)
            if len(rest) == 1:
                prop = {**_resolve(rest[0], schema), **{k: v for k, v in prop.items() if k not in ("anyOf", "oneOf")}}
            else:
                prop = {**prop, "type": "json"}
        kind = prop.get("type", "json")
        choices = prop.get("enum")
        if "const" in prop:
            choices = [prop["const"]]
        if choices:
            kind = "enum"
        elif kind not in ("string", "number", "integer", "boolean"):
            kind = "json"
        fields.append(
            FieldSpec(
                name=name,
                kind=kind,
                title=prop.get("title", ""),
                description=prop.get("description", ""),
                required=name in required,
                nullable=nullable,
                default=prop.get("default", _MISSING),
                choices=choices,
                minimum=prop.get("minimum", prop.get("exclusiveMinimum")),
                maximum=prop.get("maximum", prop.get("exclusiveMaximum")),
            )
        )
    return fields


def build_payload(fields: list[FieldSpec], values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Convert raw form values to a request dict. Returns ``(payload, errors by field)``."""
    payload: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in fields:
        raw = values.get(f.name)
        if f.kind == "boolean":
            payload[f.name] = bool(raw)
            continue
        text = "" if raw is None else str(raw).strip()
        if text == "":
            if f.required:
                errors[f.name] = "Required"
            continue  # optional and empty: leave it out, the server applies its default
        try:
            if f.kind == "integer":
                payload[f.name] = int(text)
            elif f.kind == "number":
                payload[f.name] = float(text)
            elif f.kind == "json":
                payload[f.name] = json.loads(text)
            elif f.kind == "enum":
                payload[f.name] = next((c for c in f.choices or [] if str(c) == text), text)
            else:
                payload[f.name] = text
        except ValueError:
            errors[f.name] = {
                "integer": "Enter a whole number",
                "number": "Enter a number",
                "json": "Enter valid JSON",
            }.get(f.kind, "Invalid value")
    return payload, errors


def skeleton(schema: dict[str, Any]) -> str:
    """A JSON template for the raw editor: defaults where given, typed blanks elsewhere."""
    blanks = {"string": "", "number": 0.0, "integer": 0, "boolean": False, "json": None}
    out: dict[str, Any] = {}
    for f in fields_from_schema(schema):
        if f.has_default:
            out[f.name] = f.default
        elif f.required:
            out[f.name] = f.choices[0] if f.choices else blanks[f.kind]
    return json.dumps(out, indent=2)
