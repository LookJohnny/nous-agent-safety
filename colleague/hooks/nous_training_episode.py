"""Integrity and selective supervision for observed multi-request tool episodes.

Native snapshots are never rebuilt or completed from assumptions. This module
does not execute tools, infer success, or write data. Whole-task outcome/content
evidence remains the enclosing exporter's responsibility; an intermediate tool
decision additionally needs its own digest-bound step review.
"""
import hashlib
import json
import math
import re

SCHEMA = "nous.trajectory/4"
EPISODE_SCHEMA = "nous.request-episode/1"
PROFILE = "request-episode-sft-v1"
POLICY = "accepted_task_reviewed_steps"
MAX_STEPS = 64
MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 300000
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITIES = ("project_id", "session_id", "session_group_id")
_STEP_PROVENANCE = {*_IDENTITIES, "request_sha256", "response_sha256",
                    "request_content_sha256", "response_content_sha256"}
_PROVENANCE = ("source", *_IDENTITIES, "source_transcript_id", "request_sha256", "response_sha256",
               "observation_scope", "synthetic", "authorization")


class EpisodeError(ValueError):
    """Static diagnostics without source text, IDs, paths, or tool names."""


def _fail(code):
    raise EpisodeError(code)


def _json(value):
    stack, nodes, size = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > 64:
            _fail("episode_structure_limit")
        kind = type(item)
        if kind is dict:
            if len(item) > MAX_NODES or any(type(key) is not str for key in item):
                _fail("episode_invalid_json")
            stack.extend((child, depth + 1) for pair in item.items() for child in pair)
            size += 2 + len(item) * 2
        elif kind is list:
            if len(item) > MAX_NODES:
                _fail("episode_structure_limit")
            stack.extend((child, depth + 1) for child in item)
            size += 2 + len(item)
        elif kind is str:
            if len(item) > MAX_BYTES:
                _fail("episode_size_limit")
            try:
                size += len(item.encode("utf-8")) + 2
            except UnicodeError:
                _fail("episode_invalid_json")
        elif kind is int:
            if not -(1 << 63) <= item < (1 << 63):
                _fail("episode_invalid_json")
            size += 20
        elif kind is float:
            if not math.isfinite(item):
                _fail("episode_invalid_json")
            size += 32
        elif item is None or kind is bool:
            size += 5
        else:
            _fail("episode_invalid_json")
        if size > MAX_BYTES:
            _fail("episode_size_limit")
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_BYTES:
            _fail("episode_size_limit")
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("episode_invalid_json")


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        _fail("episode_invalid_fields")


def _quality(value, require_quality):
    _keys(value, {"complete", "missing", "issues"})
    if type(value["complete"]) is not bool or any(type(value[key]) is not list or any(type(x) is not str for x in value[key])
                                               for key in ("missing", "issues")):
        _fail("episode_invalid_quality")
    if require_quality and (value["complete"] is not True or value["missing"] or value["issues"]):
        _fail("episode_quality_incomplete")


def _privacy(value, require_quality, *, step=False):
    if (type(value) is not dict or type(value.get("scrubbed")) is not bool
            or type(value.get("scrubber_version")) is not str or not value["scrubber_version"].strip()):
        _fail("episode_invalid_privacy")
    if step and type(value.get("tool_history_checked")) is not bool:
        _fail("episode_invalid_privacy")
    if require_quality and (value["scrubbed"] is not True or (step and value["tool_history_checked"] is not True)):
        _fail("episode_privacy_not_checked")


def episode_digest(record):
    """Bind all native steps, UI projection, source identities and quality.

    Evidence lists and privacy counters are excluded to avoid signature cycles.
    All per-step provenance remains bound; top-level capture timestamps do not.
    """
    _json(record)
    if type(record) is not dict or record.get("schema") != SCHEMA or type(record.get("provenance")) is not dict:
        _fail("episode_record_required")
    episode = record.get("episode")
    if type(episode) is not dict or type(episode.get("steps")) is not list:
        _fail("episode_invalid_fields")
    bound = {"schema": episode.get("schema"), "provider": episode.get("provider"), "steps": []}
    for step in episode["steps"]:
        if type(step) is not dict or type(step.get("privacy")) is not dict:
            _fail("episode_invalid_fields")
        bound["steps"].append({**{key: step.get(key) for key in ("index", "exchange", "provenance", "quality")},
                               "privacy": {key: step["privacy"].get(key) for key in ("scrubbed", "tool_history_checked")}})
    privacy = record.get("privacy", {})
    if type(privacy) is not dict:
        _fail("episode_invalid_privacy")
    return _digest({"schema": SCHEMA, "episode": bound, "messages": record.get("messages"),
                    "quality": record.get("quality"), "supervision_policy": record.get("supervision_policy"),
                    "privacy": {key: privacy.get(key) for key in ("scrubbed", "tool_history_checked")},
                    "provenance": {key: record["provenance"].get(key) for key in _PROVENANCE}})


def step_digest(record, index):
    steps = record.get("episode", {}).get("steps", []) if type(record) is dict else []
    if type(index) is not int or not 0 <= index < len(steps):
        _fail("episode_target_invalid")
    return _step_hash(episode_digest(record), index)


def _step_hash(digest, index):
    return _digest({"schema": "nous.episode-step/1", "episode_digest": digest, "step": index})


def _response_items(provider, response):
    if provider == "openai-responses":
        return response["output"]
    return [{"role": response["role"], "content": response["content"]}]


def _anthropic_message_view(messages):
    """Comparison only: native message text aliases and known cache markers.

    Exchange validation runs first. Never recurse into tool input, result text
    or nested JSON: user fields named cache_control remain semantic content.
    The original requests, response targets and digests use untouched snapshots.
    """
    result = []
    for message in messages:
        content = message["content"]
        if type(content) is str:
            content = [{"type": "text", "text": content}]
        else:
            content = [{key: value for key, value in block.items()
                        if key != "cache_control" or block["type"] not in {"text", "tool_result"}}
                       for block in content]
        result.append({**message, "content": content})
    return result


def _history_equal(provider, left, right):
    if provider == "anthropic-messages":
        return _json(_anthropic_message_view(left)) == _json(_anthropic_message_view(right))
    return _json(left) == _json(right)


def _observed_output_equal(provider, left, right):
    if provider != "openai-responses":
        return _history_equal(provider, left, right)
    # Provider response item IDs/status are audit metadata. call_id, namespace,
    # name, argument strings, content and phase are never stripped or rewritten.
    def visible(items):
        return [{key: value for key, value in item.items() if key not in {"id", "status"}} for item in items]
    return _json(visible(left)) == _json(visible(right))


def _link(provider, previous, current, pending):
    history_key = "input" if provider == "openai-responses" else "messages"
    before, after = previous["request"], current["request"]
    if _json({k: v for k, v in before.items() if k != history_key}) != _json({k: v for k, v in after.items() if k != history_key}):
        _fail("episode_request_context_changed")
    prefix, history = before[history_key], after[history_key]
    if type(prefix) is not list or type(history) is not list:
        _fail("episode_history_list_required")
    if len(history) <= len(prefix) or not _history_equal(provider, history[:len(prefix)], prefix):
        _fail("episode_history_prefix_changed")
    output = _response_items(provider, previous["response"])
    copied = history[len(prefix):len(prefix) + len(output)]
    if len(copied) != len(output) or not _observed_output_equal(provider, output, copied):
        _fail("episode_observed_response_missing_or_changed")
    tail = history[len(prefix) + len(output):]
    results = []
    if provider == "openai-responses":
        for item in tail:
            if type(item) is not dict or item.get("type") != "function_call_output":
                _fail("episode_unobserved_context_append")
            results.append(item["call_id"])
    else:
        # One actual native user message holds all results; additional user text
        # is a changed task, not an inferred continuation of this episode.
        if len(tail) != 1 or tail[0].get("role") != "user" or type(tail[0].get("content")) is not list:
            _fail("episode_unobserved_context_append")
        for block in tail[0]["content"]:
            if block.get("type") != "tool_result":
                _fail("episode_unobserved_context_append")
            results.append(block["tool_use_id"])
    if len(results) != len(set(results)) or set(results) != set(pending):
        _fail("episode_tool_results_missing_or_duplicate")


def _validate(record, require_quality):
    from nous_training_request import RequestError, validate_exchange, request_messages, exchange_state
    _json(record)
    if type(record) is not dict or record.get("schema") != SCHEMA:
        _fail("episode_record_required")
    if {"exchange", "protocol", "events", "context", "tools", "tool_contracts"} & record.keys():
        _fail("episode_unbound_context_extension")
    if record.get("supervision_policy") != POLICY:
        _fail("episode_supervision_policy_required")
    episode = record.get("episode")
    _keys(episode, {"schema", "provider", "steps"})
    if episode["schema"] != EPISODE_SCHEMA or episode["provider"] not in ("openai-responses", "anthropic-messages"):
        _fail("episode_schema_or_provider_invalid")
    steps = episode["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= MAX_STEPS:
        _fail("episode_step_limit")
    top = record.get("provenance")
    if type(top) is not dict or any(type(top.get(k)) is not str or not top[k] for k in _IDENTITIES):
        _fail("episode_source_identity_missing")
    _quality(record.get("quality"), require_quality)
    _privacy(record.get("privacy"), require_quality)
    states, requests, responses, calls, response_ids = [], set(), set(), set(), set()
    for index, step in enumerate(steps):
        _keys(step, {"index", "exchange", "provenance", "quality", "privacy"})
        if type(step["index"]) is not int or step["index"] != index:
            _fail("episode_step_order_invalid")
        provenance, exchange = step["provenance"], step["exchange"]
        _keys(provenance, _STEP_PROVENANCE)
        if any(provenance[k] != top[k] for k in _IDENTITIES):
            _fail("episode_cross_source_step")
        for key in _STEP_PROVENANCE - set(_IDENTITIES):
            if type(provenance[key]) is not str or not _SHA.fullmatch(provenance[key]):
                _fail("episode_invalid_source_digest")
        if provenance["request_sha256"] in requests or provenance["response_sha256"] in responses:
            _fail("episode_duplicate_snapshot")
        requests.add(provenance["request_sha256"]); responses.add(provenance["response_sha256"])
        if type(exchange) is not dict or exchange.get("schema") != "nous.request-exchange/2" or exchange.get("provider") != episode["provider"]:
            _fail("episode_exchange_schema_or_provider_mismatch")
        try:
            validate_exchange(exchange)
            state = exchange_state(exchange)
        except RequestError:
            _fail("episode_native_exchange_invalid")
        if provenance["request_content_sha256"] != _digest(exchange["request"]) or provenance["response_content_sha256"] != _digest(exchange["response"]):
            _fail("episode_content_digest_mismatch")
        _quality(step["quality"], require_quality)
        _privacy(step["privacy"], require_quality, step=True)
        response_id = exchange["response"]["id"]
        if response_id in response_ids:
            _fail("episode_duplicate_response_identity")
        response_ids.add(response_id)
        if index == 0:
            if state["history_call_ids"]:
                _fail("episode_initial_tool_history")
            if top.get("request_sha256") != provenance["request_sha256"]:
                _fail("episode_initial_request_digest_mismatch")
        else:
            if states[-1]["response_kind"] != "tool_calls":
                _fail("episode_continues_after_final")
            if steps[0]["exchange"]["response"]["model"] != exchange["response"]["model"]:
                _fail("episode_response_model_changed")
            _link(episode["provider"], steps[index - 1]["exchange"], exchange, states[-1]["pending_call_ids"])
        if calls & set(state["pending_call_ids"]):
            _fail("episode_reused_call_id")
        calls.update(state["pending_call_ids"])
        states.append(state)
    if require_quality and states[-1]["response_kind"] != "final_text":
        _fail("episode_final_response_missing")
    try:
        messages = request_messages(steps[-1]["exchange"])
    except RequestError:
        _fail("episode_native_exchange_invalid")
    if _json(record.get("messages")) != _json(messages):
        _fail("episode_message_projection_mismatch")
    if type(record.get("step_evidence", [])) is not list:
        _fail("episode_invalid_step_evidence")
    for item in record.get("step_evidence", []) if require_quality else []:
        _keys(item, {"kind", "result", "reviewer", "episode_digest", "step_digest", "step"}, {"at"})
        if (item["kind"] != "step_review" or item["result"] not in ("accepted", "rejected")
                or type(item["reviewer"]) is not str or not item["reviewer"].strip()
                or type(item["step"]) is not int or not 0 <= item["step"] < len(steps)
                or any(type(item[key]) is not str or not _SHA.fullmatch(item[key]) for key in ("episode_digest", "step_digest"))):
            _fail("episode_invalid_step_evidence")
    return states


def validate_episode(record, *, require_quality=True):
    try:
        _validate(record, require_quality)
    except EpisodeError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        _fail("episode_invalid_record")


def _step_review(record, index, digest=None):
    accepted, negative = False, False
    digest = episode_digest(record) if digest is None else digest
    step = _step_hash(digest, index)
    for item in record.get("step_evidence", []):
        if type(item) is not dict:
            continue
        if (item.get("episode_digest") != digest or item.get("step_digest") != step
                or type(item.get("step")) is not int or item["step"] != index):
            continue
        if item.get("kind") != "step_review":
            continue
        if item.get("result") in ("rejected", "failed", "tie", "abstain"):
            negative = True
        if (item.get("result") == "accepted" and type(item.get("reviewer")) is str
                and item["reviewer"].strip()):
            accepted = True
    return "negative" if negative else "accepted" if accepted else "unreviewed"


def negative_targets(record):
    """Bound negative step labels used only to quarantine matching sample aliases."""
    validate_episode(record, require_quality=False)
    digest = episode_digest(record)
    return [{"step": index} for index in range(len(record["episode"]["steps"]))
            if _step_review(record, index, digest) == "negative"]


def _target(target, count):
    if (type(target) is not dict or set(target) != {"step"} or type(target["step"]) is not int
            or not 0 <= target["step"] < count):
        _fail("episode_target_invalid")
    return target["step"]


def sample_for(record, target, *, require_quality=True):
    from nous_training_request import RequestError, sample_for_exchange
    validate_episode(record, require_quality=require_quality)
    steps = record["episode"]["steps"]
    index = _target(target, len(steps))
    if require_quality:
        state = _step_review(record, index)
        if state == "negative" or (index != len(steps) - 1 and state != "accepted"):
            _fail("episode_step_review_missing_or_negative")
    try:
        return sample_for_exchange(steps[index]["exchange"])
    except RequestError:
        _fail("episode_sample_invalid")


def samples(record, *, require_quality=True):
    """Final response plus explicitly reviewed intermediate decisions.

    require_quality=False is exclusively for negative-evidence quarantine: it
    yields structurally valid observed targets regardless of positive labels.
    It is not an alternative route for training incomplete or unreviewed data.
    """
    from nous_training_request import RequestError, sample_for_exchange
    validate_episode(record, require_quality=require_quality)
    last = len(record["episode"]["steps"]) - 1
    digest, output = episode_digest(record), []
    for index, step in enumerate(record["episode"]["steps"]):
        if require_quality:
            state = _step_review(record, index, digest)
            if state == "negative" or (index != last and state != "accepted"):
                continue
        try:
            output.append((sample_for_exchange(step["exchange"]), {"step": index}))
        except RequestError:
            _fail("episode_sample_invalid")
    return output


def validate_sample(sample):
    from nous_training_request import RequestError, validate_exchange_sample
    try:
        validate_exchange_sample(sample)
    except RequestError:
        _fail("episode_sample_invalid")


def final_text(record):
    validate_episode(record)
    return record["messages"][-1]["content"][0]["text"]
