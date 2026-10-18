"""Checking what a model returns against a fixed JSON schema (product-design.md §17.3: "anything that doesn't
validate is dropped").

A small subset of JSON Schema, enough for the Assistant's own schemas: ``type`` (one or a list), ``properties``,
``required``, ``additionalProperties: false``, ``items``, ``enum``, ``minLength``/``maxLength``,
``minItems``/``maxItems``, ``minimum``/``maximum`` and ``pattern`` (anchored with ``fullmatch``; keep patterns
simple). ``clean`` is lenient where that is safe: unknown object keys are removed, and an array keeps the items
that validate and drops the rest, up to ``maxItems``. Anything else that doesn't fit raises ``Invalid``.
"""

from __future__ import annotations

import json
import re
from typing import Any

WIRE_KEYS = {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}


class Invalid(ValueError):
    pass


def _type_ok(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return False


def clean(value: Any, schema: dict, path: str = "$") -> Any:
    """``value`` checked against ``schema``, with unknown keys removed and invalid array items dropped."""
    types = schema.get("type")
    if types is not None:
        options = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t) for t in options):
            raise Invalid(f"{path}: expected {'/'.join(options)}")
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid(f"{path}: not one of the allowed values")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise Invalid(f"{path}: wrong length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise Invalid(f"{path}: doesn't match")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            raise Invalid(f"{path}: out of range")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                raise Invalid(f"{path}: {key} is missing")
        out = {}
        for key, item in value.items():
            if key in props:
                out[key] = clean(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties", True) is not False:
                out[key] = item
        return out
    if isinstance(value, list):
        item_schema = schema.get("items")
        out_list = []
        for i, item in enumerate(value):
            if item_schema is None:
                out_list.append(item)
                continue
            try:
                out_list.append(clean(item, item_schema, f"{path}[{i}]"))
            except Invalid:
                continue  # one bad item doesn't spoil the rest
            if "maxItems" in schema and len(out_list) >= schema["maxItems"]:
                break
        if len(out_list) < schema.get("minItems", 0):
            raise Invalid(f"{path}: too few items")
        return out_list
    return value


def wire_schema(schema: dict) -> dict:
    """The schema as sent to an endpoint's structured-output option: only the keywords every provider accepts, and
    ``additionalProperties: false`` on every object. Lengths and ranges are checked here, after the answer."""
    out: dict[str, Any] = {k: v for k, v in schema.items() if k in WIRE_KEYS}
    if "properties" in out:
        out["properties"] = {k: wire_schema(v) for k, v in out["properties"].items()}
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = wire_schema(out["items"])
    types = out.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types):
        out["additionalProperties"] = False
    return out


def extract_json(text: str) -> Any:
    """The JSON value in a model's answer: the whole text, or inside a ``` fence, or from the first ``{`` to the
    last ``}``. Raises Invalid."""
    text = text.strip()
    candidates = [text]
    if "```" in text:
        start = text.find("```")
        body = text[start + 3:]
        body = body[body.find("\n") + 1:] if "\n" in body else body
        end = body.find("```")
        candidates.append(body[:end] if end != -1 else body)
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            continue
    raise Invalid("The answer isn't JSON")
