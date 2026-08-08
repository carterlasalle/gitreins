"""
Output-schema helpers for the generic agent runtime (R2.2).

- ``schema_to_prompt`` — render a dataclass / TypedDict / dict schema as a
  JSON-format instruction for the LLM.
- ``parse_response`` — robustly extract a JSON object from LLM output
  (markdown fences, surrounding text) and instantiate/validate it against
  the output schema (dataclass, TypedDict, or dict).

Parsing strategies mirror AgenticEvaluator._parse_verdict: strip fences,
find JSON object boundaries, json.loads.
"""

import dataclasses
import json
import re
import typing
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

T = TypeVar("T")


class SchemaError(ValueError):
    """Raised when LLM output cannot be parsed/validated against a schema."""


# ── Schema → prompt rendering ───────────────────────────────


def _type_name(annotation: Any) -> str:
    """Compact human-readable name for a type annotation."""
    if annotation is Any or annotation is None:
        return "any"
    origin = get_origin(annotation)
    if origin is typing.Union or (origin is not None and hasattr(origin, "__origin__")):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) != len(get_args(annotation)):
            return " | ".join(_type_name(a) for a in args) + " | null"
        return " | ".join(_type_name(a) for a in args)
    if origin is list or origin is typing.List:
        return f"list[{_type_name(get_args(annotation)[0])}]"
    if origin is dict or origin is typing.Dict:
        inner = get_args(annotation)
        if len(inner) == 2:
            return f"dict[{_type_name(inner[0])}, {_type_name(inner[1])}]"
        return "dict"
    if dataclasses.is_dataclass(annotation):
        return getattr(annotation, "__name__", str(annotation))
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def schema_to_prompt(output_schema: type) -> str:
    """Render an output schema as an instruction telling the LLM the JSON shape.

    Supports dataclasses (field docstrings via ``metadata={"description": ...}``),
    TypedDicts, and raw dict schemas.
    """
    if isinstance(output_schema, dict):
        return f"Output ONLY a JSON object matching this schema: {json.dumps(output_schema)}"

    if dataclasses.is_dataclass(output_schema):
        lines = ["Output ONLY a JSON object with exactly these fields:"]
        for f in dataclasses.fields(output_schema):
            desc = (f.metadata or {}).get("description", "")
            suffix = f" — {desc}" if desc else ""
            lines.append(f'  "{f.name}": {_type_name(f.type)}{suffix}')
        return "\n".join(lines)

    if _is_typed_dict(output_schema):
        try:
            hints = get_type_hints(output_schema)
        except Exception:  # noqa: BLE001 — forward refs etc.; degrade gracefully
            hints = getattr(output_schema, "__annotations__", {})
        lines = ["Output ONLY a JSON object with exactly these fields:"]
        for fname, ftype in hints.items():
            lines.append(f'  "{fname}": {_type_name(ftype)}')
        return "\n".join(lines)

    return f"Output a JSON object representing: {output_schema.__name__}"


def _is_typed_dict(cls: type) -> bool:
    try:
        return typing.is_typeddict(cls)
    except (AttributeError, TypeError):
        return False


# ── Response parsing ────────────────────────────────────────


def _extract_json_object(content: str) -> dict:
    """Pull the first top-level JSON object out of LLM output.

    Strategy 1: strip markdown fences. Strategy 2: find JSON object
    boundaries. Raises SchemaError when no object can be parsed.
    """
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        json_str = cleaned[start : end + 1]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise SchemaError(f"Invalid JSON in response: {e}") from e
        if not isinstance(data, dict):
            raise SchemaError(f"Response JSON is not an object: {type(data).__name__}")
        return data

    raise SchemaError(f"No JSON object found in response (first 200 chars): {content[:200]!r}")


def parse_response(content: str, output_schema: type[T]) -> T:
    """Parse LLM output into an instance of ``output_schema``.

    Supported schema types:
      - dataclass: nested dataclass/list/dict fields are instantiated and
        values coerced to the declared types; unknown keys are dropped;
        missing fields fall back to defaults when available.
      - TypedDict / dict: values coerced per the declared annotations.
      - ``dict`` (untyped): the parsed object is returned as-is.

    Raises SchemaError when the content is not parseable or validation fails.
    """
    data = _extract_json_object(content)
    return _instantiate(data, output_schema)


def _instantiate(data: dict, output_schema: type[T]) -> T:  # type: ignore[type-var]
    """Build an instance of ``output_schema`` from a dict, coercing values."""
    if isinstance(output_schema, dict):
        return data  # type: ignore[return-value]

    if dataclasses.is_dataclass(output_schema):
        try:
            hints = {f.name: f.type for f in dataclasses.fields(output_schema)}
        except Exception:  # noqa: BLE001
            hints = getattr(output_schema, "__annotations__", {})
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(output_schema):
            if f.name not in data:
                if f.default is not dataclasses.MISSING:
                    continue  # rely on the dataclass default
                if f.default_factory is not dataclasses.MISSING:
                    continue
                raise SchemaError(f"Missing required field {f.name!r} for {output_schema.__name__}")
            kwargs[f.name] = _coerce(data[f.name], hints.get(f.name, Any))
        return output_schema(**kwargs)

    if _is_typed_dict(output_schema):
        hints = {}
        try:
            hints = get_type_hints(output_schema)
        except Exception:  # noqa: BLE001 — forward refs; skip coercion
            pass
        coerced = {k: _coerce(v, hints.get(k, Any)) if hints else v for k, v in data.items()}
        return coerced  # type: ignore[return-value]

    if output_schema is dict or output_schema is object:
        return data  # type: ignore[return-value]

    raise SchemaError(
        f"Unsupported output schema type: {output_schema!r} (supported: dataclass, TypedDict, dict)"
    )


def _coerce(value: Any, annotation: Any) -> Any:
    """Coerce a parsed JSON value to ``annotation`` (best-effort)."""
    if annotation is Any or annotation is None:
        return value

    # None handling for Optional[X]
    if value is None:
        args = get_args(annotation)
        if args and type(None) in args:
            return None
        return None

    origin = get_origin(annotation)

    # Union / Optional — coerce with the first member that succeeds
    if origin is typing.Union or (origin is not None and hasattr(origin, "__origin__")):
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            try:
                return _coerce(value, arg)
            except (TypeError, ValueError, SchemaError):
                continue
        return value

    if origin in (list, typing.List):
        inner = get_args(annotation)[0] if get_args(annotation) else Any
        if isinstance(value, list):
            return [_coerce(v, inner) for v in value]
        return value

    if origin in (dict, typing.Dict):
        args = get_args(annotation)
        vtype = args[1] if len(args) == 2 else Any
        if isinstance(value, dict):
            return {k: _coerce(v, vtype) for k, v in value.items()}
        return value

    # Nested dataclass
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if isinstance(value, dict):
            return _instantiate(value, annotation)
        return value

    # Scalar coercion
    if annotation is str:
        return value if isinstance(value, str) else str(value)
    if annotation is int:
        return int(value) if not isinstance(value, bool) else int(value)
    if annotation is float:
        return float(value)
    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y", "on")
        return bool(value)

    if isinstance(annotation, type) and annotation in (str, int, float, bool):
        return annotation(value)

    return value
