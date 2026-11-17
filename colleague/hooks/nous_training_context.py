"""Content-free observations over one explicitly selected Codex source snapshot.

This is not a collector, repair, request reconstruction, or eligibility verifier.
No source text, tool names, session IDs, paths, or runtime reference hashes leave
this module. The whole-source SHA-256 binds the report and is not anonymization.
"""
import hashlib
import json
import math
import re

from nous_codemode_protocol import OBSERVED_RUNTIME_VERSION
from nous_transcript import TranscriptReadError, _iter_lines, read_transcript_bytes

SCHEMA = "nous.training.context/1"
MAX_TARGETS = 128
MAX_OBSERVATIONS = 100_000
MAX_REPORT_BYTES = 256 * 1024
_U64_MAX = (1 << 64) - 1
_HASH = re.compile(r"^[0-9a-f]{40}$")
_HIDDEN = frozenset({"reasoning", "analysis", "thinking", "redacted_thinking",
                     "reasoning_text", "summary_text", "encrypted_reasoning"})
_METADATA = frozenset({"history_sequence", "sampled_through_sequence", "client_authored",
                       "harness_authored_configuration", "fallback_token_limit_override",
                       "user_input_order", "retained_source", "mcp_attribution"})
_ROW_TYPES = frozenset({"session_meta", "response_item", "world_state", "turn_context",
                        "event_msg", "token_usage_record"})
_READ_ERRORS = frozenset({"transcript_must_be_regular_file", "transcript_too_large",
                          "transcript_line_too_large", "transcript_changed_during_read",
                          "transcript_too_many_lines", "transcript_unreadable"})


class _Invalid(ValueError):
    pass


def _loads(line):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise _Invalid("duplicate_json_key")
            value[key] = item
        return value
    def constant(_value):
        raise _Invalid("nonfinite_json_number")
    def check(value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise _Invalid("invalid_source_unicode") from None
        elif isinstance(value, float) and not math.isfinite(value):
            raise _Invalid("nonfinite_json_number")
        elif isinstance(value, dict):
            for key, child in value.items():
                check(key)
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        check(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, _Invalid):
            raise
        raise _Invalid("malformed_source_json") from None


def _base():
    return {
        "schema": SCHEMA, "valid": False, "status": "invalid", "scan_complete": False,
        "request_context_verified": False, "request_replay": "missing_evidence",
        "training_eligibility": "not_evaluated", "source_sha256": None,
        "source_bytes": None, "source_lines": None,
        "runtime": {"observed_profile": "unknown", "producer_identity": "unverified",
                    "version_consistency": "missing", "session_consistency": "missing"},
        "ordering": {"storage_ordinal": {"present": 0, "invalid": 0, "nonmonotonic": 0},
                     "history_sequence": {"present": 0, "invalid": 0, "duplicates": 0,
                                          "nonmonotonic": 0, "minimum": None, "maximum": None},
                     "sampled_through_sequence": {"present": 0, "invalid": 0, "nonmonotonic": 0,
                         "ahead_of_observed_history": 0, "not_observed_before": 0}},
        "observed_context": {key: 0 for key in (
            "base_instructions", "system_messages", "developer_messages", "tool_catalog_events", "tool_contracts",
            "configuration_updates", "world_state_full", "world_state_patch", "turn_context",
            "thread_settings", "base_native_exact_alias_count", "unknown_record_types",
            "unknown_record_fields", "unknown_metadata_fields", "unknown_response_items",
            "unsupported_visible_content", "blank_lines")},
        "metadata_presence": {key: 0 for key in sorted(_METADATA)},
        "reference_observations": {"semantics": "whole_source_cooccurrence_no_temporal_effectiveness",
            "model_instructions": {"exact_match": 0, "unmatched": 0, "invalid": 0},
            "tool_definitions": {"exact_match": 0, "unmatched": 0, "invalid": 0},
            "model_catalog_opaque": 0, "uncomparable_tool_definitions": 0},
        "targets": [], "errors": [], "gaps": [],
        "actions": [
            {"kind": "locate_observed_events", "status": "not_evaluated"},
            {"kind": "reread_original_source", "status": "not_evaluated"},
            {"kind": "capture_request_evidence", "status": "required"},
            {"kind": "implement_versioned_semantics", "status": "required"},
            {"kind": "review_content_and_outcome", "status": "not_evaluated"}],
    }


def _invalid(code, source=None):
    report = _base()
    if source:
        report.update({key: source[key] for key in ("source_sha256", "source_bytes", "source_lines")})
    report["errors"] = [code]
    return report


def _number(value):
    return type(value) is int and 0 <= value <= _U64_MAX


def _text(payload):
    """Only exact single native text blocks can alias a base text."""
    blocks = payload.get("content")
    if (isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict)
            and blocks[0].get("type") in {"input_text", "output_text", "text"}
            and isinstance(blocks[0].get("text"), str)):
        return blocks[0]["text"]
    return None


def _visible_message(payload, counts):
    if payload.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
        raise _Invalid("invalid_message_role")
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise _Invalid("invalid_visible_content")
    visible = False
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise _Invalid("invalid_visible_content")
        kind = block["type"]
        if kind in _HIDDEN:
            continue
        if kind in {"input_text", "output_text", "text", "refusal"}:
            if not isinstance(block.get("refusal" if kind == "refusal" else "text"), str):
                raise _Invalid("invalid_visible_content")
            visible = True
        else:
            counts["unsupported_visible_content"] += 1
    return visible


def _fragment(role, text):
    # Pinned Codex world-state fragment fingerprint; not a model request hash.
    digest = hashlib.sha1(b"codex-world-state-fragment-v1\0")
    for value in (role, text):
        raw = value.replace("\r\n", "\n").encode("utf-8")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _json_reference(value):
    # serde_json and Python float representations can differ. Do not compare
    # floating-point or out-of-range integer schemas with guessed serialization.
    def supported(item):
        if item is None or isinstance(item, (str, bool)):
            return True
        if type(item) is int:
            return -(1 << 63) <= item <= _U64_MAX
        if isinstance(item, list):
            return all(supported(child) for child in item)
        if isinstance(item, dict):
            return all(isinstance(key, str) and supported(child) for key, child in item.items())
        return False
    if not supported(value):
        return None
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _catalog(items, observed, report):
    if not isinstance(items, list):
        raise _Invalid("invalid_tool_catalog_shape")
    pairs = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise _Invalid("invalid_tool_catalog_shape")
        name = item["name"]
        if item.get("type") == "namespace":
            children = item.get("tools")
            if not isinstance(children, list):
                raise _Invalid("invalid_tool_catalog_shape")
            pairs.append((name, {key: value for key, value in item.items() if key != "tools"}))
            for child in children:
                if not isinstance(child, dict) or not isinstance(child.get("name"), str):
                    raise _Invalid("invalid_tool_catalog_shape")
                if child.get("type") == "namespace":
                    report["reference_observations"]["uncomparable_tool_definitions"] += 1
                    continue
                pairs.append((name + "." + child["name"], child))
        else:
            pairs.append((name, item))
        if len(pairs) + len(observed) > MAX_OBSERVATIONS:
            raise _Invalid("context_observation_limit")
    report["observed_context"]["tool_contracts"] += len(pairs)
    for name, value in pairs:
        digest = _json_reference(value)
        if digest is None:
            report["reference_observations"]["uncomparable_tool_definitions"] += 1
        else:
            observed.add((name, digest))


def _inspect(data):
    report = _base()
    report.update(source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data),
                  source_lines=data.count(b"\n") + int(bool(data) and not data.endswith(b"\n")))
    context, ordering = report["observed_context"], report["ordering"]
    first_version, first_session = None, None
    version_seen = session_seen = 0
    versions_changed = sessions_changed = False
    versions_known = True
    bases, developers, fragments, tools, references = set(), [], set(), set(), []
    sequences = set()
    last_sequence = last_ordinal = last_watermark = None
    metadata_rows = 0
    try:
        for line, raw in enumerate(_iter_lines(data), 1):
            if not raw.strip():
                context["blank_lines"] += 1
                continue
            row = _loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                raise _Invalid("unsupported_source_format")
            payload, kind = row["payload"], row.get("type")
            if not isinstance(kind, str):
                raise _Invalid("unsupported_source_format")
            if kind not in _ROW_TYPES:
                context["unknown_record_types"] += 1
            context["unknown_record_fields"] += len(set(row) - {"type", "payload", "timestamp", "ordinal", "metadata"})
            if "ordinal" in row:
                ordinal = row["ordinal"]
                ordering["storage_ordinal"]["present"] += 1
                if not _number(ordinal):
                    ordering["storage_ordinal"]["invalid"] += 1
                else:
                    if last_ordinal is not None and ordinal <= last_ordinal:
                        ordering["storage_ordinal"]["nonmonotonic"] += 1
                    last_ordinal = ordinal
            sequence = watermark = None
            relation = "absent"
            if "metadata" in row:
                metadata_rows += 1
                if metadata_rows > MAX_OBSERVATIONS:
                    raise _Invalid("context_observation_limit")
                metadata = row["metadata"]
                if kind != "response_item" or not isinstance(metadata, dict):
                    raise _Invalid("invalid_history_metadata")
                context["unknown_metadata_fields"] += len(set(metadata) - _METADATA)
                for key in _METADATA:
                    if key in metadata:
                        report["metadata_presence"][key] += 1
                sequence = metadata.get("history_sequence")
                watermark = metadata.get("sampled_through_sequence")
                prior_max = ordering["history_sequence"]["maximum"]
                if sequence is not None:
                    stats = ordering["history_sequence"]
                    stats["present"] += 1
                    if not _number(sequence):
                        stats["invalid"] += 1
                        sequence = None
                    else:
                        stats["duplicates"] += int(sequence in sequences)
                        stats["nonmonotonic"] += int(last_sequence is not None and sequence <= last_sequence)
                        stats["minimum"] = sequence if stats["minimum"] is None else min(stats["minimum"], sequence)
                        stats["maximum"] = sequence if stats["maximum"] is None else max(stats["maximum"], sequence)
                        last_sequence = sequence
                if watermark is not None:
                    stats = ordering["sampled_through_sequence"]
                    stats["present"] += 1
                    if not _number(watermark):
                        stats["invalid"] += 1
                        watermark, relation = None, "invalid"
                    else:
                        stats["nonmonotonic"] += int(last_watermark is not None and watermark < last_watermark)
                        last_watermark = watermark
                        relation = "zero_checkpoint" if watermark == 0 else (
                            "observed_before" if watermark in sequences else "not_observed_before")
                        if watermark and (prior_max is None or watermark > prior_max):
                            stats["ahead_of_observed_history"] += 1
                        if watermark and watermark not in sequences:
                            stats["not_observed_before"] += 1
                if sequence is not None:
                    sequences.add(sequence)
            if kind == "session_meta":
                version = payload.get("cli_version")
                version_seen += 1
                if version_seen == 1:
                    first_version = version
                elif version != first_version:
                    versions_changed = True
                versions_known = versions_known and version == OBSERVED_RUNTIME_VERSION
                if "id" in payload:
                    identity = payload["id"]
                    if not isinstance(identity, str) or not identity.strip():
                        raise _Invalid("invalid_session_identity")
                    session_seen += 1
                    if session_seen == 1:
                        first_session = identity
                    elif identity != first_session:
                        sessions_changed = True
                base = payload.get("base_instructions")
                text = base if isinstance(base, str) else base.get("text") if isinstance(base, dict) else None
                if isinstance(text, str) and text:
                    context["base_instructions"] += 1
                    bases.add(text)
            elif kind == "response_item":
                event = payload.get("type")
                if event in _HIDDEN or payload.get("channel") in _HIDDEN or payload.get("phase") in _HIDDEN:
                    continue
                visible_message = False
                if event == "message":
                    visible_message = _visible_message(payload, context) and payload.get("role") == "assistant"
                    text = _text(payload)
                    role = payload.get("role")
                    if role == "system":
                        context["system_messages"] += 1
                    if role == "developer":
                        context["developer_messages"] += 1
                        if text is not None:
                            developers.append(text)
                    if role in {"system", "developer"} and text is not None:
                        fragments.add(_fragment(role, text))
                elif event == "additional_tools":
                    context["tool_catalog_events"] += 1
                    _catalog(payload.get("tools"), tools, report)
                elif event == "configuration_update":
                    context["configuration_updates"] += 1
                elif event not in {"custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output"}:
                    context["unknown_response_items"] += 1
                if visible_message or event in {"custom_tool_call", "function_call"}:
                    if len(report["targets"]) >= MAX_TARGETS:
                        raise _Invalid("context_target_limit")
                    report["targets"].append({"source_line": line, "type": event,
                        "history_sequence": sequence, "sampled_through_sequence": watermark,
                        "watermark_relation": relation, "request_context": "unverified"})
            elif kind == "world_state":
                if type(payload.get("full")) is not bool or not isinstance(payload.get("state"), dict):
                    raise _Invalid("invalid_world_state")
                context["world_state_full" if payload["full"] else "world_state_patch"] += 1
                state = payload["state"]
                model = state.get("model")
                if isinstance(model, dict) and "instructions" in model:
                    references.append(("model_instructions", None, model["instructions"]))
                for key in ("tools", "top_level_tools"):
                    value = state.get(key)
                    if isinstance(value, dict):
                        references.extend(("tool_definitions", name, digest) for name, digest in value.items())
                context_hash = state.get("model_catalog")
                if isinstance(context_hash, str) and context_hash:
                    report["reference_observations"]["model_catalog_opaque"] += 1
            elif kind == "turn_context":
                context["turn_context"] += 1
            elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
                context["thread_settings"] += 1
            if len(bases) + len(developers) + len(fragments) + len(tools) + len(references) > MAX_OBSERVATIONS:
                raise _Invalid("context_observation_limit")
        context["base_native_exact_alias_count"] = sum(text in bases for text in developers)
        for category, name, value in references:
            counts = report["reference_observations"][category]
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                counts["invalid"] += 1
            else:
                matched = value in fragments if category == "model_instructions" else (name, value) in tools
                counts["exact_match" if matched else "unmatched"] += 1
        runtime = report["runtime"]
        runtime["observed_profile"] = "known" if version_seen and versions_known and not versions_changed else "unknown"
        runtime["version_consistency"] = "changed" if versions_changed else "consistent" if version_seen else "missing"
        runtime["session_consistency"] = "mixed" if sessions_changed else "consistent" if session_seen else "missing"
        report["errors"] = (["unsupported_runtime_profile"] if runtime["observed_profile"] != "known" else [])
        if sessions_changed:
            report["errors"].append("mixed_sessions")
        if versions_changed:
            report["errors"].append("runtime_version_changed")
        if any(value["invalid"] for value in ordering.values()):
            report["errors"].append("invalid_sequence_metadata")
        if ordering["storage_ordinal"]["nonmonotonic"]:
            report["errors"].append("invalid_storage_order")
        report["gaps"] = ["request_snapshot_missing", "sampling_membership_unverified", "context_materialization_unverified"]
        for key, code in (("world_state_full", "world_state_semantics_unverified"),
                          ("world_state_patch", "world_state_semantics_unverified"),
                          ("turn_context", "turn_context_semantics_unverified"),
                          ("thread_settings", "thread_settings_semantics_unverified"),
                          ("unknown_record_types", "unknown_source_event"),
                          ("unknown_record_fields", "unknown_source_fields"),
                          ("unknown_response_items", "unknown_visible_event"),
                          ("unsupported_visible_content", "unsupported_visible_content"),
                          ("unknown_metadata_fields", "unknown_history_metadata_fields")):
            if context[key] and code not in report["gaps"]:
                report["gaps"].append(code)
        report.update(valid=not report["errors"], status="invalid" if report["errors"] else "observed", scan_complete=True)
        report["actions"][0]["status"] = "observed"
        report["actions"][1]["status"] = "inspected"
        if len(json.dumps(report, sort_keys=True).encode()) > MAX_REPORT_BYTES:
            raise _Invalid("context_report_limit")
        return report
    except _Invalid as error:
        return _invalid(str(error), report)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return _invalid("invalid_source_value", report)


def inspect_source(path):
    """Inspect an explicitly authorized source; never create state or repair data.

    A successful diagnostic validates only this observation's supported source
    shape. Every report leaves request_context_verified False, including fixtures.
    """
    try:
        data = read_transcript_bytes(path)
    except TranscriptReadError as error:
        code = str(error) if str(error) in _READ_ERRORS else "transcript_unreadable"
        return _invalid(code)
    except (OSError, ValueError, TypeError):
        return _invalid("transcript_unreadable")
    return _inspect(data)
