#!/usr/bin/env python3
"""Strict, local training dataset export. Standard library only; no upload.

Canonical messages use the transcript adapter's text/tool_use/tool_result blocks.
The default profile is TRL conversational JSONL (function.arguments is an object).
The optional openai profile serializes arguments as a JSON string. Neither profile
invents system prompts, tool definitions, outcomes, or preference annotations.

Reviewers/verifiers must bind their evidence to record_digest(record); annotating
one successful tool invocation is not evidence that the entire trajectory passed.
Preferences are separately annotated records, never inferred from adjacent steps.
"""
import collections
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from nous_training_schema import SchemaError, validate_tools, validate_value

SCHEMA = "nous.trajectory/1"
VERSION = "2.4.0"
CODEMODE_PROFILE = "codemode-events-v1"
REQUEST_PROFILE = "request-sft-v1"
EPISODE_PROFILE = "request-episode-sft-v1"
PROFILES = ("trl", "openai", CODEMODE_PROFILE, REQUEST_PROFILE, EPISODE_PROFILE)
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class InvalidRecord(ValueError):
    """Machine-readable reason; never contains user content."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def record_digest(record):
    """V1 hashes remain stable; V2 binds context, completeness and source groups.

    Capture timestamps are intentionally excluded so an unchanged Stop replay
    does not invalidate a review. Group identities affect split eligibility and
    therefore cannot be reassigned while retaining an earlier V2 annotation.
    """
    if record.get("schema") == "nous.trajectory/4":
        from nous_training_episode import EpisodeError, episode_digest
        try:
            return episode_digest(record)
        except EpisodeError as error:
            raise InvalidRecord(str(error)) from None
    if record.get("schema") == "nous.trajectory/3":
        provenance = record.get("provenance", {})
        if not isinstance(provenance, dict):
            raise InvalidRecord("invalid_trajectory_provenance")
        return _digest({"schema": record["schema"], "exchange": record.get("exchange"),
                        "messages": record.get("messages"), "quality": record.get("quality"),
                        "provenance": {key: provenance.get(key) for key in (
                            "source", "project_id", "session_id", "session_group_id", "source_transcript_id",
                            "request_sha256", "response_sha256", "request_content_sha256",
                            "observation_scope", "synthetic", "authorization")}})
    if record.get("schema") == "nous.trajectory/2":
        provenance = record.get("provenance", {})
        if not isinstance(provenance, dict):
            raise InvalidRecord("invalid_trajectory_provenance")
        return _digest({"schema": record["schema"], "messages": record.get("messages"),
                        "tools": record.get("tools", []), "protocol": record.get("protocol"),
                        "quality": record.get("quality"),
                        "provenance": {key: provenance.get(key) for key in
                                       ("project_id", "session_id", "session_group_id", "source_transcript_id")}})
    return _digest({"messages": record.get("messages"), "tools": record.get("tools", [])})


def preference_digest(record):
    """V1 retains its historical digest; new V2 also binds explicit source IDs.

    An input without a schema is a new V2 preference. Never promote an existing
    V1 annotation or repair a stale signature automatically.
    """
    content = {key: record.get(key, [] if key == "tools" else None)
               for key in ("prompt", "chosen", "rejected", "tools")}
    schema = record.get("schema", "nous.preference/2")
    if schema == "nous.preference/1":
        return _digest(content)
    if schema != "nous.preference/2":
        raise InvalidRecord("unsupported_preference_schema")
    provenance = record.get("provenance", {})
    if not isinstance(provenance, dict):
        raise InvalidRecord("invalid_preference_provenance")
    return _digest({"schema": schema, **content,
                    "source_record_id": provenance.get("source_record_id"),
                    "project_id": provenance.get("project_id")})


def _safe_id(value):
    if isinstance(value, str) and _ID.fullmatch(value):
        return value
    return "sha256:" + _digest(value)


def _privacy(record):
    privacy = record.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("scrubbed") is not True:
        raise InvalidRecord("privacy_not_scrubbed")
    if not isinstance(privacy.get("scrubber_version"), str) or not privacy["scrubber_version"].strip():
        raise InvalidRecord("privacy_version_missing")


def _envelope(record, preference=False):
    if not isinstance(record, dict):
        raise InvalidRecord("record_not_object")
    try:
        _json(record).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise InvalidRecord("invalid_json_value") from None
    if not isinstance(record.get("id"), str) or not record["id"].strip():
        raise InvalidRecord("record_id_missing")
    if not preference and record.get("schema") not in (SCHEMA, "nous.trajectory/2", "nous.trajectory/3", "nous.trajectory/4"):
        raise InvalidRecord("unsupported_schema")
    if not preference:
        if ("episode" in record or "step_evidence" in record or "supervision_policy" in record) and record.get("schema") != "nous.trajectory/4":
            raise InvalidRecord("unbound_episode_extension")
        if "exchange" in record and record.get("schema") != "nous.trajectory/3":
            raise InvalidRecord("unbound_request_extension")
        if record.get("schema") == "nous.trajectory/4":
            from nous_training_episode import EpisodeError, validate_episode
            try:
                validate_episode(record, require_quality=False)
            except EpisodeError as error:
                raise InvalidRecord(str(error)) from None
        elif record.get("schema") == "nous.trajectory/3":
            from nous_training_request import RequestError, validate_record
            try:
                validate_record(record, require_quality=False)
            except RequestError as error:
                raise InvalidRecord(str(error)) from None
        elif record.get("schema") == "nous.trajectory/2":
            from nous_codemode_protocol import ProtocolError, validate_protocol_shape
            try:
                validate_protocol_shape(record.get("protocol"))
            except ProtocolError as error:
                raise InvalidRecord(str(error)) from None
        elif any(key in record for key in ("protocol", "events", "context", "tool_contracts", "exchange")):
            raise InvalidRecord("unbound_protocol_extension")
    if preference and record.get("schema", "nous.preference/2") not in ("nous.preference/1", "nous.preference/2"):
        raise InvalidRecord("unsupported_preference_schema")
    _privacy(record)


def _tool_schemas(tools):
    try:
        return validate_tools(tools)
    except SchemaError as error:
        raise InvalidRecord(str(error)) from None


def strict_json(text):
    """Reject duplicate keys and non-JSON constants instead of normalizing them."""
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise InvalidRecord("duplicate_json_key")
            out[key] = value
        return out
    def constant(_):
        raise InvalidRecord("invalid_json_constant")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        _json(value).encode("utf-8")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, InvalidRecord):
            raise
        raise InvalidRecord("invalid_json") from None


def _content(value):
    if isinstance(value, str):
        return value
    return _json(value)


def validate_canonical_messages(messages):
    """Allow known partial observations while refusing malformed/unknown blocks."""
    if not isinstance(messages, list) or not messages:
        raise InvalidRecord("empty_messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise InvalidRecord("invalid_message_role")
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                raise InvalidRecord("empty_text_message")
            continue
        if not isinstance(content, list) or not content:
            raise InvalidRecord("empty_message_content")
        for block in content:
            if not isinstance(block, dict):
                raise InvalidRecord("invalid_content_block")
            kind = block.get("type")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise InvalidRecord("invalid_text_content")
            elif kind == "tool_use":
                if message["role"] != "assistant":
                    raise InvalidRecord("tool_call_wrong_role")
                if not all(isinstance(block.get(key), str) and block[key] for key in ("id", "name")):
                    raise InvalidRecord("invalid_tool_call")
                if "input" not in block or block.get("format", "function") not in ("function", "custom"):
                    raise InvalidRecord("invalid_tool_call")
            elif kind == "tool_result":
                if message["role"] not in ("user", "tool"):
                    raise InvalidRecord("tool_result_wrong_role")
                if not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"] or "content" not in block:
                    raise InvalidRecord("invalid_tool_result")
                if "is_error" in block and type(block["is_error"]) is not bool:
                    raise InvalidRecord("invalid_tool_result_error_flag")
            elif kind == "image" and block.get("omitted") is True:
                continue
            else:
                raise InvalidRecord("unsupported_content_block")


def convert_messages(messages, *, profile="trl"):
    """Convert observed blocks without fabricating context; validate block shapes.

    Tool pairing and presence of real tool schemas are checked separately, so a
    partial but structurally valid observation can still enter trajectories.jsonl.
    """
    if profile not in PROFILES:
        raise ValueError("unsupported profile")
    if not isinstance(messages, list) or not messages:
        raise InvalidRecord("empty_messages")
    out = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise InvalidRecord("invalid_message_role")
        if message.get("sidechain") or message.get("compact_summary"):
            raise InvalidRecord("branched_or_compacted_context")
        role = message["role"]
        raw = message.get("content")
        blocks = [{"type": "text", "text": raw}] if isinstance(raw, str) else raw
        if not isinstance(blocks, list) or not blocks:
            raise InvalidRecord("empty_message_content")
        text, calls = [], []

        def flush():
            if not text and not calls:
                return
            item = {"role": role, "content": "\n".join(text)}
            if calls:
                item["tool_calls"] = list(calls)
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise InvalidRecord("tool_result_id_missing")
                item["tool_call_id"] = call_id
            # Codex emits parallel calls as separate response_item records. A
            # tool-only run is one assistant tool-call message in chat datasets.
            if (calls and not item["content"] and out and out[-1]["role"] == "assistant"
                    and out[-1].get("tool_calls") and not out[-1].get("content")):
                out[-1]["tool_calls"].extend(item["tool_calls"])
            else:
                out.append(item)
            text.clear()
            calls.clear()

        for block in blocks:
            if not isinstance(block, dict):
                raise InvalidRecord("invalid_content_block")
            kind = block.get("type")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise InvalidRecord("invalid_text_content")
                text.append(block["text"])
            elif kind == "tool_use":
                if role != "assistant":
                    raise InvalidRecord("tool_call_wrong_role")
                if block.get("format", "function") != "function":
                    raise InvalidRecord("unsupported_custom_tool")
                call_id, name = block.get("id"), block.get("name")
                if not isinstance(call_id, str) or not call_id:
                    raise InvalidRecord("tool_call_id_missing")
                if not isinstance(name, str) or not name:
                    raise InvalidRecord("tool_call_name_missing")
                args = block.get("input")
                if not isinstance(args, dict):
                    raise InvalidRecord("tool_arguments_not_object")
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": args if profile == "trl" else _json(args)}})
            elif kind == "tool_result":
                if role not in ("user", "tool"):
                    raise InvalidRecord("tool_result_wrong_role")
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or not call_id:
                    raise InvalidRecord("tool_result_id_missing")
                if "content" not in block:
                    raise InvalidRecord("tool_result_content_missing")
                if "is_error" in block and type(block["is_error"]) is not bool:
                    raise InvalidRecord("invalid_tool_result_error_flag")
                flush()
                result = ({"is_error": True, "content": block["content"]}
                          if block.get("is_error") is True else block["content"])
                out.append({"role": "tool", "tool_call_id": call_id,
                            "content": _content(result)})
            else:
                raise InvalidRecord("unsupported_content_block")
        flush()
    if not out:
        raise InvalidRecord("empty_messages")
    return out


def validate_messages(messages, tools=(), *, require_final=True, profile="trl"):
    """Check role ordering, complete tool round trips, and observed schema coverage."""
    if profile not in PROFILES:
        raise InvalidRecord("unsupported_profile")
    if not isinstance(messages, list) or not messages:
        raise InvalidRecord("empty_messages")
    schemas = _tool_schemas(list(tools))
    pending, seen = {}, set()
    has_user, started = False, False
    for message in messages:
        if (not isinstance(message, dict) or set(message) - {"role", "content", "tool_calls", "tool_call_id", "name"}
                or message.get("role") not in ("system", "developer", "user", "assistant", "tool")):
            raise InvalidRecord("invalid_training_message")
        role = message["role"]
        content = message.get("content")
        if not isinstance(content, str):
            raise InvalidRecord("invalid_training_content")
        calls = message.get("tool_calls", [])
        if "tool_calls" in message and (role != "assistant" or not isinstance(calls, list) or not calls):
            raise InvalidRecord("invalid_training_tool_calls")
        if role != "tool" and ("tool_call_id" in message or "name" in message):
            raise InvalidRecord("unexpected_tool_result_fields")
        if pending and role != "tool":
            raise InvalidRecord("tool_results_missing")
        if role in ("system", "developer"):
            if started:
                raise InvalidRecord("late_system_message")
        else:
            started = True
        if role == "user":
            has_user = True
        if role == "assistant" and not has_user:
            raise InvalidRecord("user_prompt_missing")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise InvalidRecord("tool_result_id_missing")
            if call_id not in pending:
                raise InvalidRecord("orphan_or_duplicate_tool_result")
            if "name" in message and message["name"] != pending[call_id]:
                raise InvalidRecord("tool_result_name_mismatch")
            del pending[call_id]
        elif not calls and (not isinstance(content, str) or not content.strip()):
            raise InvalidRecord("empty_text_message")
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                    or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"]):
                raise InvalidRecord("invalid_training_tool_call")
            fn = call.get("function")
            if not isinstance(fn, dict) or set(fn) != {"name", "arguments"} or not isinstance(fn.get("name"), str):
                raise InvalidRecord("invalid_training_function")
            call_id, name = call["id"], call["function"]["name"]
            if call_id in seen:
                raise InvalidRecord("duplicate_tool_call_id")
            if name not in schemas:
                raise InvalidRecord("tool_schema_missing")
            args = fn["arguments"]
            if profile == "openai":
                if not isinstance(args, str):
                    raise InvalidRecord("tool_arguments_not_json_string")
                args = strict_json(args)
            if not isinstance(args, dict):
                raise InvalidRecord("tool_arguments_not_object")
            try:
                validate_value(args, schemas[name])
            except SchemaError as error:
                raise InvalidRecord(str(error)) from None
            pending[call_id] = name
            seen.add(call_id)
    if pending:
        raise InvalidRecord("tool_results_missing")
    if not has_user:
        raise InvalidRecord("user_prompt_missing")
    if require_final and (messages[-1]["role"] != "assistant" or
                          not messages[-1].get("content", "").strip() or
                          messages[-1].get("tool_calls")):
        raise InvalidRecord("assistant_final_missing")


def _quality(record):
    quality = record.get("quality")
    if not isinstance(quality, dict):
        raise InvalidRecord("quality_missing")
    if quality.get("complete") is not True:
        raise InvalidRecord("incomplete_trajectory")
    issues, missing = quality.get("issues"), quality.get("missing")
    if not isinstance(issues, list) or not isinstance(missing, list):
        raise InvalidRecord("invalid_quality_report")
    if any(not isinstance(x, str) for x in issues + missing):
        raise InvalidRecord("invalid_quality_report")
    if issues:
        raise InvalidRecord("source_quality_issues")
    allowed = {"system_prompt"}
    if not any(b.get("type") == "tool_use" for m in record["messages"]
               for b in (m.get("content") if isinstance(m.get("content"), list) else [])
               if isinstance(b, dict)):
        allowed.add("tool_definitions")
    if set(missing) - allowed:
        raise InvalidRecord("missing_context")
    for message in record["messages"]:
        if message.get("sidechain") or message.get("compact_summary"):
            raise InvalidRecord("branched_or_compacted_context")


def _evidence(record, digest, preference=False):
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list):
        raise InvalidRecord("invalid_evidence")
    valid = []
    for item in evidence:
        if not isinstance(item, dict):
            raise InvalidRecord("invalid_evidence")
        if item.get("record_digest") != digest:
            continue
        kind, result = item.get("kind"), item.get("result")
        if result in ("rejected", "failed", "tie", "abstain"):
            raise InvalidRecord("negative_or_conflicting_evidence")
        expected = {"human_preference": ("chosen", "reviewer"),
                    "verifier_preference": ("chosen", "verifier")} if preference else {
                        "human_review": ("accepted", "reviewer"), "verifier": ("passed", "verifier")}
        if kind in expected:
            outcome, author_key = expected[kind]
            if result == outcome and isinstance(item.get(author_key), str) and item[author_key].strip():
                valid.append(kind)
    if not valid:
        raise InvalidRecord("explicit_preference_missing" if preference else "verified_outcome_missing")
    return sorted(set(valid))


def _lineage(record, digest, line, kind, evidence):
    source = record.get("provenance", {})
    source = source if isinstance(source, dict) else {}
    provenance = {key: _safe_id(source[key]) for key in ("source", "session_id", "session_group_id", "source_transcript_id", "project_id", "source_record_id") if key in source}
    if isinstance(source.get("source_sha256"), str) and _SHA.fullmatch(source["source_sha256"]):
        provenance["source_sha256"] = source["source_sha256"]
    return {"id": _safe_id(record["id"]), "record_digest": digest, "line": line,
            "format": kind, "provenance": provenance, "evidence_kinds": evidence}


class _Jsonl:
    def __init__(self, path):
        self.handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self.sha = hashlib.sha256()
        self.rows = self.bytes = 0

    def write(self, item):
        data = (_json(item) + "\n").encode("utf-8")
        self.handle.write(data)
        self.sha.update(data)
        self.rows += 1
        self.bytes += len(data)
        return self.rows

    def close(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()

    def summary(self):
        return {"records": self.rows, "bytes": self.bytes, "sha256": self.sha.hexdigest()}


def validate_sample(sample, kind, profile="trl"):
    """Strict validation of already-converted training rows, also used offline."""
    if profile == EPISODE_PROFILE:
        if kind != "sft":
            raise InvalidRecord("episode_preferences_not_supported")
        from nous_training_episode import EpisodeError, validate_sample as validate_episode_sample
        try:
            validate_episode_sample(sample)
        except EpisodeError as error:
            raise InvalidRecord(str(error)) from None
        return
    if profile == REQUEST_PROFILE:
        if kind != "sft":
            raise InvalidRecord("request_preferences_not_supported")
        from nous_training_request import RequestError, validate_sample as validate_request_sample
        try:
            validate_request_sample(sample)
        except RequestError as error:
            raise InvalidRecord(str(error)) from None
        return
    if profile == CODEMODE_PROFILE:
        if kind != "sft":
            raise InvalidRecord("codemode_preferences_not_supported")
        from nous_codemode_training import CodeModeError, validate_sample as validate_codemode
        try:
            validate_codemode(sample)
        except CodeModeError as error:
            raise InvalidRecord(str(error)) from None
        return
    required = {"messages"} if kind == "sft" else {"prompt", "chosen", "rejected"}
    if kind not in ("sft", "dpo") or not isinstance(sample, dict) or not required <= set(sample) or set(sample) - required - {"tools"}:
        raise InvalidRecord("invalid_training_fields")
    tools = sample.get("tools", [])
    _tool_schemas(tools)
    if kind == "sft":
        validate_messages(sample["messages"], tools, profile=profile)
        return
    prompt, chosen, rejected = (sample[k] for k in ("prompt", "chosen", "rejected"))
    if not all(isinstance(x, list) and x for x in (prompt, chosen, rejected)):
        raise InvalidRecord("empty_messages")
    if chosen == rejected:
        raise InvalidRecord("identical_preference_completions")
    if not isinstance(prompt[-1], dict) or prompt[-1].get("role") not in ("user", "tool"):
        raise InvalidRecord("preference_prompt_not_ready")
    if any(not isinstance(x[0], dict) or x[0].get("role") != "assistant" for x in (chosen, rejected)):
        raise InvalidRecord("preference_completion_not_assistant")
    if any(not isinstance(m, dict) or m.get("role") in ("user", "system", "developer") for m in chosen + rejected):
        raise InvalidRecord("preference_completion_contains_prompt")
    validate_messages(prompt + chosen, tools, profile=profile)
    validate_messages(prompt + rejected, tools, profile=profile)


def sample_for(record, kind, profile="trl", *, require_quality=True, target=None):
    if profile == EPISODE_PROFILE:
        if kind != "sft":
            raise InvalidRecord("episode_preferences_not_supported")
        from nous_training_episode import EpisodeError, sample_for as episode_sample
        try:
            return episode_sample(record, target, require_quality=require_quality)
        except EpisodeError as error:
            raise InvalidRecord(str(error)) from None
    if record.get("schema") == "nous.trajectory/4":
        raise InvalidRecord("episode_requires_episode_profile")
    if profile == REQUEST_PROFILE:
        if kind != "sft":
            raise InvalidRecord("request_preferences_not_supported")
        if target is not None:
            raise InvalidRecord("unexpected_target_boundary")
        from nous_training_request import RequestError, sample_for as request_sample
        try:
            return request_sample(record, require_quality=require_quality)
        except RequestError as error:
            raise InvalidRecord(str(error)) from None
    if profile == CODEMODE_PROFILE:
        if kind != "sft":
            raise InvalidRecord("codemode_preferences_not_supported")
        from nous_codemode_training import CodeModeError, sample_for as codemode_sample
        try:
            return codemode_sample(record, target, require_quality=require_quality)
        except CodeModeError as error:
            raise InvalidRecord(str(error)) from None
    if target is not None:
        raise InvalidRecord("unexpected_target_boundary")
    if record.get("schema") == "nous.trajectory/2":
        raise InvalidRecord("protocol_requires_codemode_profile")
    if record.get("schema") == "nous.trajectory/3":
        raise InvalidRecord("exchange_requires_request_profile")
    if kind == "sft":
        if require_quality:
            _quality(record)
        sample = {"messages": convert_messages(record.get("messages"), profile=profile)}
    else:
        sample = {k: convert_messages(record.get(k), profile=profile) for k in ("prompt", "chosen", "rejected")}
    if record.get("tools"):
        sample["tools"] = record["tools"]
    validate_sample(sample, kind, profile)
    return sample


def samples_for(record, kind, profile="trl", *, require_quality=True):
    """Training rows with explicit source event boundaries, never inferred labels."""
    if profile == EPISODE_PROFILE:
        if kind != "sft":
            raise InvalidRecord("episode_preferences_not_supported")
        from nous_training_episode import EpisodeError, samples
        try:
            return samples(record, require_quality=require_quality)
        except EpisodeError as error:
            raise InvalidRecord(str(error)) from None
    if profile != CODEMODE_PROFILE:
        return [(sample_for(record, kind, profile, require_quality=require_quality), None)]
    if kind != "sft":
        raise InvalidRecord("codemode_preferences_not_supported")
    from nous_codemode_training import CodeModeError, samples
    try:
        return samples(record, require_quality=require_quality)
    except CodeModeError as error:
        raise InvalidRecord(str(error)) from None


def training_evidence(record, digest, kind, profile):
    evidence = _evidence(record, digest, preference=kind == "dpo")
    if profile not in (CODEMODE_PROFILE, REQUEST_PROFILE, EPISODE_PROFILE):
        return evidence
    # Outcome review does not attest that arbitrary custom code avoided private files.
    # This independent review is over the exact scrubbed context, not over a tool name.
    valid = []
    for entry in record.get("evidence", []):
        if entry.get("record_digest") != digest:
            continue
        expected = {"content_review": ("cleared", "reviewer"),
                    "content_verifier": ("passed", "verifier")}.get(entry.get("kind"))
        if expected and entry.get("result") == expected[0] and isinstance(entry.get(expected[1]), str) and entry[expected[1]].strip():
            valid.append(entry["kind"])
    if not valid:
        raise InvalidRecord("custom_content_review_missing")
    return sorted(set(evidence + valid))


def formats_for(profile):
    if profile == EPISODE_PROFILE:
        return {"sft": "standard-prompt-completion", "dpo": "unsupported",
                "serialization": "nous.request-tool-sft/1", "tool_arguments": "native-function-only",
                "tool_error_content": "native-observed", "loss_scope": "assistant-completion-only",
                "supervision_policy": "accepted_task_reviewed_steps"}
    if profile == REQUEST_PROFILE:
        return {"sft": "standard-prompt-completion", "dpo": "unsupported",
                "serialization": "nous.request-sft/1", "tool_arguments": "native-request",
                "tool_error_content": "unsupported", "loss_scope": "assistant-completion-only"}
    if profile == CODEMODE_PROFILE:
        return {"sft": "standard-prompt-completion", "dpo": "unsupported",
                "serialization": "nous.observed-visible-events/1", "tool_arguments": "native-custom-or-function",
                "tool_error_content": "native-observed", "loss_scope": "assistant-completion-only"}
    return {"sft": "conversational-messages", "dpo": "explicit-prompt-chosen-rejected",
            "tool_arguments": "object" if profile == "trl" else "json-string",
            "tool_error_content": "json-object-is_error-content"}


def negative_evidence(record, digest):
    evidence = record.get("evidence", [])
    return isinstance(evidence, list) and any(
        isinstance(e, dict) and e.get("record_digest") == digest and
        e.get("result") in ("rejected", "failed", "tie", "abstain") for e in evidence)


def negative_samples_for(record, kind, profile, digest):
    """Quarantine global negatives or individually rejected episode decisions."""
    if negative_evidence(record, digest):
        return [sample for sample, _ in samples_for(record, kind, profile, require_quality=False)]
    if kind == "sft" and profile == EPISODE_PROFILE and record.get("schema") == "nous.trajectory/4":
        from nous_training_episode import EpisodeError, negative_targets
        try:
            targets = negative_targets(record)
        except EpisodeError as error:
            raise InvalidRecord(str(error)) from None
        return [sample_for(record, kind, profile, require_quality=False, target=target) for target in targets]
    return []


def preference_key(sample):
    chosen, rejected = _digest(sample["chosen"]), _digest(sample["rejected"])
    key = _digest({"prompt": sample["prompt"], "tools": sample.get("tools", []),
                   "responses": sorted([chosen, rejected])})
    return key, int(chosen > rejected)


def preference_source_matches(preference, trajectory):
    """Whether the exact normalized prompt/tools were observed at this source.

    This helper deliberately does not inspect source IDs or project ownership, so
    a splitter can conservatively connect every matching task across sources.
    Arbitrary later assistant answers do not have to equal the chosen revision.
    """
    if trajectory.get("schema") != SCHEMA:
        return False
    try:
        if _json(preference.get("tools", [])) != _json(trajectory.get("tools", [])):
            return False
        prompt = convert_messages(preference.get("prompt"), profile="trl")
        observed = convert_messages(trajectory.get("messages"), profile="trl")
        return (bool(prompt) and len(prompt) <= len(observed)
                and _json(observed[:len(prompt)]) == _json(prompt))
    except InvalidRecord:
        return False


def validate_preference_source(preference, trajectories):
    """Validate an optional explicit source without re-signing its annotation.

    A standalone preference remains supported by the low-level API. Once a
    source_record_id is supplied it must reference an observed matching prefix
    and tool context. Ordered V2 event sources are not supported by DPO yet.
    """
    provenance = preference.get("provenance", {})
    if not isinstance(provenance, dict):
        raise InvalidRecord("invalid_preference_provenance")
    rid = provenance.get("source_record_id")
    if rid is None:
        return
    if not isinstance(rid, str) or not rid.strip():
        raise InvalidRecord("preference_source_record_missing")
    project = provenance.get("project_id")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise InvalidRecord("invalid_preference_project")
    found, matched, ordered = False, False, False
    for source in trajectories:
        if source.get("id") != rid:
            continue
        found = True
        if source.get("schema") in ("nous.trajectory/2", "nous.trajectory/3", "nous.trajectory/4"):
            ordered = True
            continue
        source_provenance = source.get("provenance", {})
        if project is not None and (not isinstance(source_provenance, dict) or source_provenance.get("project_id") != project):
            continue
        matched |= preference_source_matches(preference, source)
    if not found:
        raise InvalidRecord("preference_source_record_missing")
    if ordered:
        raise InvalidRecord("preference_ordered_source_not_supported")
    if not matched:
        raise InvalidRecord("preference_source_context_mismatch")


def export_dataset(trajectories, out_dir, *, preferences=(), profile="trl"):
    """Publish only after scanning all evidence; private SQLite spools body data.

    Canonical observations and explicit preferences preserve audit evidence.
    Conflicting labels and reverse preference pairs are excluded independent of
    input order. Exceptions leave no complete manifest. No network is used.
    """
    if profile not in PROFILES:
        raise ValueError("unsupported training profile")
    os.mkdir(out_dir, mode=0o700)
    writers, lineage, scrubbers = {}, [], set()
    counts, reasons = collections.Counter(), collections.Counter()

    def reject(record, reason, stage, digest=None):
        item = {"stage": stage, "reason": reason}
        if isinstance(record, dict) and "id" in record:
            try:
                item["id"] = _safe_id(record["id"])
            except (TypeError, ValueError, RecursionError, UnicodeError):
                pass
        if digest:
            item["record_digest"] = digest
        writers["rejected.jsonl"].write(item)
        reasons[stage + ":" + reason] += 1

    try:
        for name in ("trajectories.jsonl", "preferences.jsonl", "sft.jsonl", "dpo.jsonl", "rejected.jsonl"):
            writers[name] = _Jsonl(os.path.join(out_dir, name))
        with tempfile.TemporaryDirectory(prefix="nous-training-spool-") as temp:
            db = sqlite3.connect(os.path.join(temp, "candidates.sqlite"))
            os.chmod(os.path.join(temp, "candidates.sqlite"), 0o600)
            try:
                db.executescript("""
                    CREATE TABLE canonical (kind TEXT, identity TEXT, line INTEGER, PRIMARY KEY(kind, identity));
                    CREATE TABLE source_records (id TEXT, identity TEXT PRIMARY KEY, data TEXT);
                    CREATE INDEX source_ids ON source_records(id);
                    CREATE TABLE blocked (kind TEXT, digest TEXT, PRIMARY KEY(kind, digest));
                    CREATE TABLE blocked_samples (kind TEXT, digest TEXT, PRIMARY KEY(kind, digest));
                    CREATE TABLE candidates (seq INTEGER PRIMARY KEY, kind TEXT, digest TEXT,
                        sample_digest TEXT, pair_key TEXT, orientation INTEGER, sample TEXT, meta TEXT);
                    CREATE INDEX pair_lookup ON candidates(pair_key, orientation);
                    CREATE TABLE emitted (kind TEXT, digest TEXT, line INTEGER, PRIMARY KEY(kind, digest));
                """)
                for kind, records in (("sft", trajectories), ("dpo", preferences)):
                    source_file = "trajectories.jsonl" if kind == "sft" else "preferences.jsonl"
                    source_kind = "trajectory" if kind == "sft" else "preference"
                    for record in records:
                        counts["input_trajectories" if kind == "sft" else "input_preferences"] += 1
                        digest = None
                        try:
                            _envelope(record, preference=kind == "dpo")
                            digest = record_digest(record) if kind == "sft" else preference_digest(record)
                            for key in (("messages",) if kind == "sft" else ("prompt", "chosen", "rejected")):
                                validate_canonical_messages(record.get(key))
                            if not isinstance(record.get("tools", []), list):
                                raise InvalidRecord("invalid_tool_schemas")
                        except InvalidRecord as error:
                            reject(record, str(error), "canonical" if kind == "sft" else "preference", digest)
                            continue
                        if kind == "dpo":
                            record = {**record, "schema": record.get("schema", "nous.preference/2")}
                        scrubbers.add(record["privacy"]["scrubber_version"])
                        # Keep changed annotations even when observation content is identical.
                        identity = _digest(record)
                        if kind == "sft":
                            db.execute("INSERT OR IGNORE INTO source_records VALUES (?,?,?)", (record["id"], identity, _json(record)))
                        old = db.execute("SELECT line FROM canonical WHERE kind=? AND identity=?", (kind, identity)).fetchone()
                        if old:
                            source_line = old[0]
                            reject(record, "duplicate_trajectory" if kind == "sft" else "duplicate_preference_observation", source_kind, digest)
                        else:
                            source_line = writers[source_file].write(record)
                            db.execute("INSERT INTO canonical VALUES (?,?,?)", (kind, identity, source_line))
                            lineage.append(_lineage(record, digest, source_line, source_kind, []))
                        if negative_evidence(record, digest):
                            db.execute("INSERT OR IGNORE INTO blocked VALUES (?,?)", (kind, digest))
                        try:
                            for negative_sample in negative_samples_for(record, kind, profile, digest):
                                db.execute("INSERT OR IGNORE INTO blocked_samples VALUES (?,?)", (kind, _digest(negative_sample)))
                        except InvalidRecord:
                            pass
                        try:
                            candidates = samples_for(record, kind, profile)
                            evidence = training_evidence(record, digest, kind, profile)
                            if kind == "dpo":
                                source = record.get("provenance", {})
                                rid = source.get("source_record_id") if isinstance(source, dict) else None
                                rows = db.execute("SELECT data FROM source_records WHERE id=?", (rid,)) if isinstance(rid, str) else ()
                                validate_preference_source(record, (strict_json(row[0]) for row in rows))
                        except InvalidRecord as error:
                            reject(record, str(error), kind, digest)
                            continue
                        for sample, target in candidates:
                            meta = _lineage(record, digest, 0, kind, evidence)
                            meta.update(source_file=source_file, source_line=source_line, sample_digest=_digest(sample))
                            if target is not None:
                                meta["target"] = target
                            pair, orientation = preference_key(sample) if kind == "dpo" else (None, None)
                            db.execute("INSERT INTO candidates(kind,digest,sample_digest,pair_key,orientation,sample,meta) VALUES (?,?,?,?,?,?,?)",
                                       (kind, digest, meta["sample_digest"], pair, orientation, _json(sample), _json(meta)))
                db.commit()
                for kind, digest, sample_digest, pair, orientation, raw, raw_meta in db.execute(
                        "SELECT kind,digest,sample_digest,pair_key,orientation,sample,meta FROM candidates ORDER BY seq"):
                    meta = strict_json(raw_meta)
                    if (db.execute("SELECT 1 FROM blocked WHERE kind=? AND digest=?", (kind, digest)).fetchone()
                            or db.execute("SELECT 1 FROM blocked_samples WHERE kind=? AND digest=?", (kind, sample_digest)).fetchone()):
                        reject(meta, "cross_record_evidence_conflict", kind, digest)
                        continue
                    if kind == "dpo" and db.execute("SELECT 1 FROM candidates WHERE pair_key=? AND orientation!=? LIMIT 1", (pair, orientation)).fetchone():
                        reject(meta, "conflicting_preference_direction", kind, digest)
                        continue
                    prior = db.execute("SELECT line FROM emitted WHERE kind=? AND digest=?", (kind, sample_digest)).fetchone()
                    if prior:
                        meta["line"] = prior[0]
                        meta["deduplicated"] = True
                        lineage.append(meta)
                        reject(meta, "duplicate_sft" if kind == "sft" else "duplicate_preference", kind, digest)
                        continue
                    meta["line"] = writers[kind + ".jsonl"].write(strict_json(raw))
                    lineage.append(meta)
                    db.execute("INSERT INTO emitted VALUES (?,?,?)", (kind, sample_digest, meta["line"]))
            finally:
                db.close()
    finally:
        for writer in writers.values():
            writer.close()

    manifest = {"schema": "nous.training.manifest/2", "exporter_version": VERSION,
                "complete": True, "created_at": datetime.now(timezone.utc).isoformat(), "profile": profile,
                "formats": formats_for(profile),
                "counts": {"input_trajectories": counts["input_trajectories"], "input_preferences": counts["input_preferences"]},
                "files": {name: writer.summary() for name, writer in writers.items()},
                "rejections": dict(sorted(reasons.items())), "scrubber_versions": sorted(scrubbers),
                "lineage": lineage,
                "quality_policy": {"evidence_required": True, "preference_inference": False,
                                   "missing_system_prompt_allowed": profile not in (CODEMODE_PROFILE, REQUEST_PROFILE, EPISODE_PROFILE), "tool_schema_required": True,
                                   "complete_tool_roundtrip_required": True,
                                   "json_schema_argument_validation": "supported-subset-v1",
                                   "unsupported_schema_keywords": "reject-training",
                                   "cross_record_conflicts": "exclude",
                                   "split": "unsplit; split by source session/project before training"}}
    path = os.path.join(out_dir, "manifest.json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


export_trajectories = export_dataset
