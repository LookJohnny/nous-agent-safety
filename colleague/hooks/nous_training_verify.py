"""Bounded, offline verification of an explicitly specified final JSON result.

This module performs no I/O, executes no source commands and writes no evidence.
Its caller owns capture authorization, request-byte binding, oracle registration
timing, record/revocation compare-and-swap, and retaining conflicting evidence.
"""
import hashlib
import json
import math
import re
import sys

from nous_training_export import record_digest
from nous_training_schema import SchemaError, validate_schema, validate_value

VERSION = "1.0.0"
VERIFIER = "nous.final-json/1"
SCHEMA_PROFILE = "nous.oracle-json-schema/1"
MAX_ORACLE_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_ASSERTIONS = 16
MAX_ORACLE_NODES = 4096
MAX_RESULT_NODES = 16384
MAX_RECORD_NODES = 200000
MAX_DEPTH = 32
MAX_SCHEMA_DEPTH = 16
MAX_INTEGER = (1 << 63) - 1
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SCOPES = {"final_json_task", "final_json_format"}
_SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items",
                "enum", "const", "minimum", "maximum", "minLength", "maxLength",
                "minItems", "maxItems"}


class VerificationError(ValueError):
    """Static reason code, without source text or arbitrary exception details."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


ENVIRONMENT_DIGEST = _sha({
    "verifier": VERIFIER, "version": VERSION, "schema_profile": SCHEMA_PROFILE,
    "python": list(sys.version_info[:3]), "implementation": sys.implementation.name,
    "integer_only": True,
})


def _bounded(value, *, byte_limit, node_limit, prefix, depth_limit=MAX_DEPTH,
             allow_float=False):
    """Bound traversal before encoding; accept only ordinary JSON Python types."""
    stack = [(value, 0)]
    nodes, size = 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > node_limit or depth > depth_limit:
            raise VerificationError(prefix + "_limit_exceeded")
        kind = type(item)
        if kind is dict:
            if len(item) > node_limit - nodes + 1:
                raise VerificationError(prefix + "_limit_exceeded")
            size += 2 + len(item) * 2
            for key, child in item.items():
                if type(key) is not str:
                    raise VerificationError(prefix + "_invalid_json")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif kind is list:
            if len(item) > node_limit - nodes + 1:
                raise VerificationError(prefix + "_limit_exceeded")
            size += 2 + len(item)
            stack.extend((child, depth + 1) for child in item)
        elif kind is str:
            if len(item) > byte_limit:
                raise VerificationError(prefix + "_limit_exceeded")
            try:
                size += len(item.encode("utf-8")) + 2
            except UnicodeError:
                raise VerificationError(prefix + "_invalid_json") from None
        elif kind is int:
            if not -MAX_INTEGER - 1 <= item <= MAX_INTEGER:
                raise VerificationError(prefix + "_integer_out_of_range")
            size += 20
        elif kind is float:
            if not allow_float or not math.isfinite(item):
                raise VerificationError(prefix + "_unsupported_number")
            size += 32
        elif kind is bool or item is None:
            size += 5
        else:
            raise VerificationError(prefix + "_invalid_json")
        if size > byte_limit:
            raise VerificationError(prefix + "_limit_exceeded")
    try:
        encoded = _json(value).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise VerificationError(prefix + "_invalid_json") from None
    if len(encoded) > byte_limit:
        raise VerificationError(prefix + "_limit_exceeded")
    return encoded


def _schema(schema, depth=0):
    if depth > MAX_SCHEMA_DEPTH:
        raise VerificationError("oracle_schema_limit_exceeded")
    if type(schema) is bool:
        return
    if type(schema) is not dict or set(schema) - _SCHEMA_KEYS:
        raise VerificationError("oracle_unsupported_schema")
    if "type" in schema and (type(schema["type"]) is not str or schema["type"] not in
                             {"object", "array", "integer", "number", "string", "boolean", "null"}):
        raise VerificationError("oracle_invalid_schema")
    props = schema.get("properties", {})
    if type(props) is not dict or len(props) > 64:
        raise VerificationError("oracle_invalid_schema")
    for child in props.values():
        _schema(child, depth + 1)
    for name in ("items", "additionalProperties"):
        if name in schema:
            _schema(schema[name], depth + 1)
    if "required" in schema and (type(schema["required"]) is not list or len(schema["required"]) > 64):
        raise VerificationError("oracle_invalid_schema")
    if "enum" in schema and (type(schema["enum"]) is not list or not 1 <= len(schema["enum"]) <= 32):
        raise VerificationError("oracle_invalid_schema")
    for name in ("minLength", "maxLength", "minItems", "maxItems"):
        if name in schema and (type(schema[name]) is not int or not 0 <= schema[name] <= MAX_RESULT_BYTES):
            raise VerificationError("oracle_invalid_schema_bound")
    for name in ("minimum", "maximum"):
        if name in schema and type(schema[name]) is not int:
            raise VerificationError("oracle_invalid_schema_bound")
    for minimum, maximum in (("minimum", "maximum"), ("minLength", "maxLength"), ("minItems", "maxItems")):
        if minimum in schema and maximum in schema and schema[minimum] > schema[maximum]:
            raise VerificationError("oracle_invalid_schema_bound")
    try:
        validate_schema(schema)
    except SchemaError:
        raise VerificationError("oracle_invalid_schema") from None


def validate_oracle(spec, *, require_binding=True):
    """Return an independent normalized oracle; never insert hashes or timing.

    With require_binding=False, a request_sha256-free template is allowed. A
    supplied binding is always checked for syntax. oracle_digest() hashes this
    exact normalized snapshot; binding a template creates a different snapshot.
    """
    if type(require_binding) is not bool:
        raise VerificationError("oracle_invalid_binding_option")
    raw = _bounded(spec, byte_limit=MAX_ORACLE_BYTES, node_limit=MAX_ORACLE_NODES,
                   prefix="oracle")
    spec = json.loads(raw)
    if (type(spec) is not dict or set(spec) - {"schema", "scope", "request_sha256", "assertions"}
            or spec.get("schema") != "nous.oracle/1" or type(spec.get("scope")) is not str
            or spec["scope"] not in _SCOPES):
        raise VerificationError("oracle_invalid_fields")
    binding = spec.get("request_sha256")
    if "request_sha256" in spec:
        if type(binding) is not str or not _SHA.fullmatch(binding):
            raise VerificationError("oracle_invalid_request_binding")
    elif require_binding:
        raise VerificationError("oracle_request_binding_missing")
    assertions = spec.get("assertions")
    if type(assertions) is not list or not 1 <= len(assertions) <= MAX_ASSERTIONS:
        raise VerificationError("oracle_invalid_assertions")
    exact = []
    for assertion in assertions:
        if type(assertion) is not dict:
            raise VerificationError("oracle_invalid_assertion")
        if assertion.get("kind") == "exact_json" and set(assertion) == {"kind", "expected"}:
            exact.append(assertion["expected"])
        elif assertion.get("kind") == "json_schema" and set(assertion) == {"kind", "schema"}:
            _schema(assertion["schema"])
        else:
            raise VerificationError("oracle_invalid_assertion")
    if spec["scope"] == "final_json_task" and not exact:
        raise VerificationError("oracle_task_requires_exact_result")
    if exact:
        if any(_json(value) != _json(exact[0]) for value in exact[1:]):
            raise VerificationError("oracle_inconsistent_assertions")
        for assertion in assertions:
            if assertion["kind"] == "json_schema":
                try:
                    validate_value(exact[0], assertion["schema"])
                except SchemaError:
                    raise VerificationError("oracle_inconsistent_assertions") from None
    return json.loads(raw)


def oracle_digest(spec, *, require_binding=True):
    """Canonical SHA256 of a validated bound oracle or explicit template."""
    return _sha(validate_oracle(spec, require_binding=require_binding))


def _result(text):
    if type(text) is not str or not text.strip():
        raise VerificationError("result_text_missing")
    if len(text) > MAX_RESULT_BYTES:
        raise VerificationError("result_limit_exceeded")
    try:
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            raise VerificationError("result_limit_exceeded")
    except UnicodeError:
        raise VerificationError("result_invalid_json") from None

    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise VerificationError("result_duplicate_json_key")
            output[key] = value
        return output

    def number(_value):
        raise VerificationError("result_unsupported_number")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=number)
    except VerificationError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise VerificationError("result_invalid_json") from None
    _bounded(value, byte_limit=MAX_RESULT_BYTES, node_limit=MAX_RESULT_NODES, prefix="result")
    return value


def _final_text(record, request_sha256, response_unchanged):
    if record.get("schema") not in ("nous.trajectory/3", "nous.trajectory/4"):
        raise VerificationError("unsupported_record_schema")
    if type(response_unchanged) is not bool or response_unchanged is not True:
        raise VerificationError("response_not_unchanged")
    provenance = record.get("provenance")
    if type(provenance) is not dict or provenance.get("request_sha256") != request_sha256:
        raise VerificationError("record_request_binding_mismatch")
    if record.get("schema") == "nous.trajectory/4":
        from nous_training_episode import EpisodeError, validate_episode, final_text
        try:
            validate_episode(record)
            return final_text(record)
        except EpisodeError:
            raise VerificationError("request_exchange_invalid") from None
    try:
        from nous_training_request import RequestError, validate_record
    except ImportError:
        raise VerificationError("request_adapter_unavailable") from None
    try:
        validate_record(record)
    except RequestError:
        raise VerificationError("request_exchange_invalid") from None
    messages = record.get("messages")
    if type(messages) is not list or not messages:
        raise VerificationError("result_text_missing")
    final = messages[-1]
    if type(final) is not dict or final.get("role") != "assistant":
        raise VerificationError("result_final_not_assistant")
    blocks = final.get("content")
    if (type(blocks) is not list or len(blocks) != 1 or type(blocks[0]) is not dict
            or set(blocks[0]) != {"type", "text"} or blocks[0].get("type") != "text"):
        raise VerificationError("result_ambiguous_content")
    return blocks[0]["text"]


def evaluate_record(record, spec, *, request_sha256, response_unchanged=True, request_unchanged=True):
    """Evaluate one trusted capture snapshot, returning only static codes/hashes.

    A candidate is not committed evidence. The caller must verify authorization,
    unchanged record/oracle/revocation generation, and preserve prior negatives.
    request_sha256 denotes actual outbound bytes, supplied by the capture layer;
    it cannot be reconstructed from a possibly scrubbed exchange.
    """
    report = {"schema": "nous.verification/1", "verifier": VERIFIER,
              "status": "indeterminate", "scope": None, "codes": [],
              "record_digest": None, "oracle_sha256": None, "request_sha256": None,
              "result_digest": None, "environment_digest": ENVIRONMENT_DIGEST,
              "evidence_candidate": None}
    try:
        oracle = validate_oracle(spec)
        report.update(scope=oracle["scope"], oracle_sha256=_sha(oracle))
        if type(request_sha256) is not str or not _SHA.fullmatch(request_sha256):
            raise VerificationError("request_binding_invalid")
        report["request_sha256"] = request_sha256
        if oracle["request_sha256"] != request_sha256:
            raise VerificationError("oracle_request_binding_mismatch")
        raw_record = _bounded(record, byte_limit=MAX_RECORD_BYTES, node_limit=MAX_RECORD_NODES,
                              prefix="record", depth_limit=64, allow_float=True)
        record = json.loads(raw_record)
        if type(record) is not dict:
            raise VerificationError("record_invalid_fields")
        report["record_digest"] = record_digest(record)
        if request_unchanged is not True:
            raise VerificationError("request_redacted_or_changed")
        result = _result(_final_text(record, request_sha256, response_unchanged))
        report["result_digest"] = _sha(result)
        matched = True
        for assertion in oracle["assertions"]:
            if assertion["kind"] == "exact_json":
                matched = matched and _json(result) == _json(assertion["expected"])
            else:
                try:
                    validate_value(result, assertion["schema"])
                except SchemaError:
                    matched = False
        report["status"] = "matched" if matched else "mismatched"
        report["codes"] = ["all_assertions_matched" if matched else "result_assertion_mismatch"]
    except VerificationError as error:
        report["codes"] = [str(error)]
    except Exception:
        # No arbitrary adapter/parser exception, source text, or path is emitted.
        report["codes"] = ["verification_internal_error"]
    if (report["scope"] == "final_json_format" or
            (report["scope"] == "final_json_task" and report["status"] != "indeterminate")):
        task = report["scope"] == "final_json_task"
        report["evidence_candidate"] = {
            "kind": "verifier" if task else "result_check",
            "result": ({"matched": "passed", "mismatched": "failed"}[report["status"]]
                       if task else report["status"]),
            "verifier": VERIFIER, "scope": report["scope"],
            **{key: report[key] for key in ("record_digest", "oracle_sha256", "request_sha256",
                                           "result_digest", "environment_digest")},
        }
    return report
