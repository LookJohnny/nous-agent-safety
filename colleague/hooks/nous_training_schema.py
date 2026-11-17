"""Conservative JSON Schema subset for observed tool schemas and arguments.

Unsupported keywords fail closed. This is deliberately not a general JSON Schema
implementation; patterns use Python's re syntax, and references are unsupported.
"""
import json
import math
import re
from decimal import Decimal, InvalidOperation


class SchemaError(ValueError):
    """Static, content-free validation reason."""


TYPES = {"null", "boolean", "object", "array", "number", "integer", "string"}
ANNOTATIONS = {"title", "description", "default", "examples", "$comment", "$id", "$schema",
               "deprecated", "readOnly", "writeOnly"}
KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
            "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
            "minProperties", "maxProperties", "anyOf", "oneOf", "allOf"} | ANNOTATIONS


def _number(value):
    return type(value) is int or (isinstance(value, float) and math.isfinite(value))


def _same(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    return left == right


def validate_schema(schema, depth=0):
    if depth > 64:
        raise SchemaError("tool_schema_too_deep")
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError("invalid_tool_schema")
    if set(schema) - KEYWORDS:
        raise SchemaError("unsupported_tool_schema_keyword")
    if "type" in schema:
        value = schema["type"]
        types = value if isinstance(value, list) else [value]
        if (not types or any(not isinstance(t, str) or t not in TYPES for t in types)
                or len(set(types)) != len(types)):
            raise SchemaError("invalid_tool_schema_type")
    for key in ("title", "description", "$comment", "$id", "$schema"):
        if key in schema and not isinstance(schema[key], str):
            raise SchemaError("invalid_tool_schema_annotation")
    for key in ("deprecated", "readOnly", "writeOnly"):
        if key in schema and not isinstance(schema[key], bool):
            raise SchemaError("invalid_tool_schema_annotation")
    if "examples" in schema and not isinstance(schema["examples"], list):
        raise SchemaError("invalid_tool_schema_annotation")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict) or any(not isinstance(k, str) for k in props):
            raise SchemaError("invalid_tool_schema_properties")
        for child in props.values():
            validate_schema(child, depth + 1)
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list) or any(not isinstance(k, str) for k in required)
                or len(set(required)) != len(required)):
            raise SchemaError("invalid_tool_schema_required")
    for key in ("additionalProperties", "items"):
        if key in schema:
            validate_schema(schema[key], depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            branches = schema[key]
            if not isinstance(branches, list) or not branches:
                raise SchemaError("invalid_tool_schema_composition")
            for branch in branches:
                validate_schema(branch, depth + 1)
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not choices:
            raise SchemaError("invalid_tool_schema_enum")
        if any(_same(value, old) for i, value in enumerate(choices) for old in choices[:i]):
            raise SchemaError("invalid_tool_schema_enum")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and (not _number(schema[key]) or (key == "multipleOf" and schema[key] <= 0)):
            raise SchemaError("invalid_tool_schema_bound")
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise SchemaError("invalid_tool_schema_bound")
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        raise SchemaError("invalid_tool_schema_unique_items")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaError("invalid_tool_schema_pattern")
        try:
            re.compile(schema["pattern"])
        except re.error:
            raise SchemaError("invalid_tool_schema_pattern") from None


def _is_type(value, kind):
    return {"null": value is None, "boolean": type(value) is bool,
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "number": _number(value),
            "integer": _number(value) and (not isinstance(value, float) or value.is_integer()),
            "string": isinstance(value, str)}[kind]


def _matches(value, schema):
    if isinstance(schema, bool):
        return schema
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, kind) for kind in kinds):
            return False
    if "enum" in schema and not any(_same(value, choice) for choice in schema["enum"]):
        return False
    if "const" in schema and not _same(value, schema["const"]):
        return False
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            results = [_matches(value, child) for child in schema[key]]
            if not ({"anyOf": any(results), "oneOf": sum(results) == 1, "allOf": all(results)}[key]):
                return False
    if isinstance(value, dict):
        if any(k not in value for k in schema.get("required", [])):
            return False
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", math.inf):
            return False
        props = schema.get("properties", {})
        for key, child in value.items():
            if not _matches(child, props.get(key, schema.get("additionalProperties", True))):
                return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            return False
        if "items" in schema and not all(_matches(v, schema["items"]) for v in value):
            return False
        if schema.get("uniqueItems") and any(_same(v, old) for i, v in enumerate(value) for old in value[:i]):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if _number(value):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            return False
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get("exclusiveMaximum", math.inf):
            return False
        if "multipleOf" in schema:
            try:
                if Decimal(str(value)) % Decimal(str(schema["multipleOf"])) != 0:
                    return False
            except InvalidOperation:
                return False
    return True


def validate_value(value, schema):
    validate_schema(schema)
    if not _matches(value, schema):
        raise SchemaError("tool_argument_schema_mismatch")


def validate_tools(tools):
    if not isinstance(tools, list):
        raise SchemaError("invalid_tool_schemas")
    schemas = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or set(tool) - {"type", "function", "return"}:
            raise SchemaError("unsupported_tool_schema")
        fn = tool.get("function")
        if (not isinstance(fn, dict) or set(fn) - {"name", "description", "parameters", "strict"}
                or not isinstance(fn.get("name"), str) or not fn["name"].strip()):
            raise SchemaError("invalid_tool_schema_name")
        if fn["name"] in schemas:
            raise SchemaError("duplicate_tool_schema")
        if "description" in fn and not isinstance(fn["description"], str):
            raise SchemaError("invalid_tool_schema_annotation")
        if "strict" in fn and type(fn["strict"]) is not bool:
            raise SchemaError("invalid_tool_schema_annotation")
        params = fn.get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            raise SchemaError("invalid_tool_parameters")
        validate_schema(params)
        if "return" in tool:
            validate_schema(tool["return"])
        schemas[fn["name"]] = params
    return schemas
