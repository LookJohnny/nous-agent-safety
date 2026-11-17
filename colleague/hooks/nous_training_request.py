"""Closed native request/response observations; no transport, tools or persistence.

The serialized training prompt is an observed API body, not a provider's hidden
model template. Caller-supplied flags or hashes never prove a request was sent.
Collectors separately establish authorization, capture provenance and privacy.
"""
import copy
import hashlib
import json
import math

from nous_training_schema import SchemaError, validate_schema, validate_value

SCHEMA = "nous.request-exchange/1"
TOOL_SCHEMA = "nous.request-exchange/2"
PROFILE = "request-sft-v1"
SERIALIZATION = "nous.request-sft/1"
TOOL_SERIALIZATION = "nous.request-tool-sft/1"
PROVIDERS = {"openai-responses", "anthropic-messages"}
MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 4096
MAX_DEPTH = 64
MAX_TOOL_CALLS = 128
MAX_ARGUMENT_BYTES = 256 * 1024
MAX_ARGUMENT_SCHEMA_WORK = 250000
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_HIDDEN = {"reasoning", "analysis", "thinking", "redacted_thinking", "encrypted_reasoning"}


class RequestError(ValueError):
    """Static diagnostics without source text or unknown field names."""


def _fail(code="invalid_request_exchange"):
    raise RequestError(code)


def _json(value):
    def check(item, depth=0):
        if depth > MAX_DEPTH:
            _fail("request_structure_too_deep")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                _fail("invalid_request_json")
            return
        if type(item) is list:
            if len(item) > MAX_ITEMS:
                _fail("request_item_limit")
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            if len(item) > MAX_ITEMS:
                _fail("request_item_limit")
            for child in item.values():
                check(child, depth + 1)
            return
        _fail("invalid_request_json")
    try:
        check(value)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(result.encode("utf-8")) > MAX_BYTES:
            _fail("request_size_limit")
        return result
    except RequestError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail("invalid_request_json")


def _loads(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail("duplicate_request_json_key")
            value[key] = item
        return value
    def constant(_value):
        _fail("invalid_request_json")
    if not isinstance(text, str):
        _fail("invalid_request_json")
    try:
        if len(text.encode("utf-8")) > MAX_BYTES:
            _fail("request_size_limit")
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        _json(value)
        return value
    except RequestError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail("invalid_request_json")


def _keys(value, required=(), optional=(), code="unsupported_request_field"):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        _fail(code)


def _string(value, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail("invalid_request_string")


def _enum(value, choices, code="unsupported_request_setting"):
    if not isinstance(value, str) or value not in choices:
        _fail(code)


def _number(value, low=0, high=None, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value)
            or value < low or (high is not None and value > high)):
        _fail("invalid_request_number")


def _optional(value, name, check):
    if name in value and value[name] is not None:
        check(value[name])


def _bool(value):
    if type(value) is not bool:
        _fail("invalid_request_boolean")


def _empty(value):
    if value is not None and value != [] and value != {}:
        _fail("unsupported_request_context")


def _metadata(value):
    if not isinstance(value, dict) or len(value) > 16:
        _fail("invalid_request_metadata")
    for key, item in value.items():
        _string(key)
        _string(item, empty=True)
        if len(key) > 64 or len(item) > 512:
            _fail("invalid_request_metadata")


def _cache(value):
    _keys(value, {"type"}, {"ttl"})
    _enum(value["type"], {"ephemeral"})
    _optional(value, "ttl", lambda x: _enum(x, {"5m", "1h"}))


def _client_metadata(value):
    # Codex client metadata is a separate string map, not the public Responses
    # `metadata` field's 512-character value contract. Feature descriptors can
    # exceed that limit. Bound the complete JSON and individual values instead.
    if not isinstance(value, dict) or len(value) > 64:
        _fail("invalid_request_client_metadata")
    if len(_json(value).encode()) > 128 * 1024:
        _fail("invalid_request_client_metadata")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(item, str) or len(item) > 16384:
            _fail("invalid_request_client_metadata")


def _reasoning(value, *, allow_summary=False):
    _keys(value, (), {"effort", "summary"} if allow_summary else {"effort"}, "unsupported_request_reasoning_field")
    _optional(value, "effort", lambda x: _enum(x, _EFFORTS))
    if allow_summary:
        _optional(value, "summary", lambda x: _enum(x, {"auto", "concise", "detailed", "none"},
                                                   "unsupported_request_reasoning_summary"))


def _text_config(value):
    _keys(value, (), {"format", "verbosity"})
    if "format" in value and value["format"] is not None:
        _keys(value["format"], {"type"})
        _enum(value["format"]["type"], {"text"}, "unsupported_response_format")
    _optional(value, "verbosity", lambda x: _enum(x, {"low", "medium", "high"}))


def _schema(value):
    if not isinstance(value, dict):
        _fail("invalid_request_tool_schema")
    try:
        validate_schema(value)
    except SchemaError:
        _fail("unsupported_request_tool_schema")


def _contracts(tools, provider, catalog=None, prefix="", depth=0):
    catalog = {} if catalog is None else catalog
    if not isinstance(tools, list) or depth > 4:
        _fail("invalid_request_tool_catalog")
    for tool in tools:
        if not isinstance(tool, dict):
            _fail("invalid_request_tool_catalog")
        if provider == "anthropic-messages":
            _keys(tool, {"name", "input_schema"}, {"description", "strict", "cache_control"})
            kind = "function"
            _schema(tool["input_schema"])
            _optional(tool, "cache_control", _cache)
        else:
            kind = tool.get("type")
            if kind == "namespace":
                _keys(tool, {"type", "name", "tools"}, {"description"})
            elif kind == "function":
                _keys(tool, {"type", "name", "parameters"}, {"description", "strict"})
                _schema(tool["parameters"])
            elif kind == "custom":
                _keys(tool, {"type", "name"}, {"description", "format"})
                form = tool.get("format")
                if form is not None:
                    if not isinstance(form, dict):
                        _fail("unsupported_request_custom_format")
                    if form.get("type") == "text":
                        _keys(form, {"type"})
                    elif form.get("type") == "grammar":
                        _keys(form, {"type", "syntax", "definition"})
                        _enum(form["syntax"], {"lark", "regex"})
                        _string(form["definition"])
                    else:
                        _fail("unsupported_request_custom_format")
            else:
                _fail("unsupported_request_tool")
        _string(tool["name"])
        _optional(tool, "description", lambda x: _string(x, empty=True))
        _optional(tool, "strict", _bool)
        name = prefix + tool["name"]
        if kind == "namespace":
            _contracts(tool["tools"], provider, catalog, name + ".", depth + 1)
        else:
            entry = {"kind": kind, "name": tool["name"], "schema": tool.get("parameters", tool.get("input_schema")), "native": tool}
            if name in catalog and _json(catalog[name]["native"]) != _json(tool):
                _fail("request_tool_contract_changed")
            catalog[name] = entry
    return catalog


def _call_budget(arguments, schema):
    """The old schema matcher has no execution budget and supports Python regex.

    Tool execution history opts into a smaller non-regex subset. An upper bound
    on the schema/value node product also bounds composition/array traversal.
    """
    def nodes(value):
        stack, count = [value], 0
        while stack:
            item = stack.pop(); count += 1
            if count > 4096:
                _fail("request_tool_validation_limit")
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return count
    stack = [schema]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        if "pattern" in item or item.get("uniqueItems") is True:
            _fail("request_tool_schema_execution_unsupported")
        if isinstance(item.get("enum"), list) and len(item["enum"]) > 128:
            _fail("request_tool_validation_limit")
        stack.extend(item.get("properties", {}).values())
        stack.extend(item.get(key) for key in ("items", "additionalProperties"))
        for key in ("anyOf", "oneOf", "allOf"):
            stack.extend(item.get(key, []))
    if (len(_json(arguments).encode()) > MAX_ARGUMENT_BYTES
            or nodes(arguments) * nodes(schema) > MAX_ARGUMENT_SCHEMA_WORK):
        _fail("request_tool_validation_limit")


def _call(name, arguments, catalog, namespace=None):
    _string(name)
    if namespace is not None:
        _string(namespace)
        choices = [catalog[namespace + "." + name]] if namespace + "." + name in catalog else []
    else:
        choices = [catalog[name]] if name in catalog else [entry for entry in catalog.values() if entry["name"] == name]
    if len(choices) != 1 or choices[0]["kind"] != "function":
        _fail("request_function_contract_missing_or_ambiguous")
    if not isinstance(arguments, dict):
        _fail("invalid_request_tool_arguments")
    _call_budget(arguments, choices[0]["schema"])
    try:
        validate_value(arguments, choices[0]["schema"])
    except SchemaError:
        _fail("request_tool_arguments_mismatch")


def _function_output(value):
    if isinstance(value, str):
        _string(value, empty=True)
    elif isinstance(value, list):
        for block in value:
            _keys(block, {"type", "text"}, (), "unsupported_request_tool_output")
            _enum(block["type"], {"input_text"}, "unsupported_request_tool_output")
            _string(block["text"], empty=True)
    else:
        _fail("unsupported_request_tool_output")


def _openai_text(content, *, output=False):
    if isinstance(content, str) and not output:
        _string(content)
        return
    if not isinstance(content, list) or not content:
        _fail("invalid_request_content")
    for block in content:
        if not isinstance(block, dict):
            _fail("invalid_request_content")
        if block.get("type") in _HIDDEN:
            _fail("hidden_reasoning_unsupported")
        _keys(block, {"type", "text"}, {"annotations", "logprobs"})
        _enum(block["type"], {"output_text"} if output else {"input_text", "output_text", "text"}, "unsupported_request_content")
        _string(block["text"], empty=not output)
        for field in ("annotations", "logprobs"):
            if field in block and block[field] != []:
                _fail("unsupported_request_content_metadata")


def _input_openai(value, catalog):
    if isinstance(value, str):
        _string(value)
        return {"catalog": catalog, "calls": {}, "error_ids": set()}
    if not isinstance(value, list) or not value:
        _fail("request_input_missing")
    seen, pending, has_user, last_role = {}, set(), False, None
    outputs_started = False
    for item in value:
        if not isinstance(item, dict):
            _fail("invalid_request_input")
        kind = item.get("type", "message" if "role" in item else None)
        if kind in _HIDDEN or item.get("phase") in _HIDDEN or item.get("channel") in _HIDDEN:
            _fail("hidden_reasoning_unsupported")
        if kind == "message":
            _keys(item, {"role", "content"}, {"type", "id", "status", "phase"})
            _enum(item["role"], {"system", "developer", "user", "assistant"}, "unsupported_request_role")
            if pending:
                _fail("request_tool_results_missing")
            _optional(item, "id", _string)
            _optional(item, "status", lambda x: _enum(x, {"completed"}, "request_input_incomplete"))
            _optional(item, "phase", lambda x: _enum(x, {"commentary", "final_answer", "final"}))
            _openai_text(item["content"])
            has_user |= item["role"] == "user"
            last_role = item["role"]
        elif kind == "additional_tools":
            if pending:
                _fail("request_tool_batch_interleaved")
            _keys(item, {"type", "role", "tools"}, {"id"})
            _enum(item["role"], {"developer"})
            _optional(item, "id", _string)
            _contracts(item["tools"], "openai-responses", catalog)
        elif kind == "configuration_update":
            if pending:
                _fail("request_tool_batch_interleaved")
            _keys(item, {"type", "reasoning"})
            _reasoning(item["reasoning"])
        elif kind == "function_call":
            _keys(item, {"type", "call_id", "name", "arguments"}, {"id", "status", "namespace"})
            _string(item["call_id"])
            _optional(item, "id", _string)
            _optional(item, "status", lambda x: _enum(x, {"completed"}, "request_input_incomplete"))
            if not has_user or item["call_id"] in seen:
                _fail("invalid_request_tool_linkage")
            if outputs_started:
                _fail("request_tool_batch_interleaved")
            if len(seen) >= MAX_TOOL_CALLS:
                _fail("request_tool_call_limit")
            _call(item["name"], _loads(item["arguments"]), catalog, item.get("namespace"))
            seen[item["call_id"]] = (item["name"], item.get("namespace")); pending.add(item["call_id"])
            last_role = "assistant"
        elif kind == "function_call_output":
            _keys(item, {"type", "call_id", "output"}, {"id", "name", "namespace", "status"})
            _string(item["call_id"])
            _optional(item, "id", _string)
            _optional(item, "status", lambda x: _enum(x, {"completed"}, "request_input_incomplete"))
            if item["call_id"] not in pending:
                _fail("invalid_request_tool_linkage")
            name, namespace = seen[item["call_id"]]
            if ("name" in item and item["name"] != name) or ("namespace" in item and item["namespace"] != namespace):
                _fail("request_tool_result_identity_mismatch")
            _function_output(item["output"])
            pending.remove(item["call_id"])
            outputs_started = bool(pending)
            last_role = "tool"
        else:
            _fail("unsupported_request_input_item")
    if pending:
        _fail("request_tool_results_missing")
    if not has_user:
        _fail("request_user_input_missing")
    if last_role == "assistant":
        _fail("request_assistant_prefill_unsupported")
    return {"catalog": catalog, "calls": seen, "error_ids": set()}


_OPENAI_SETTINGS = {"instructions", "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p",
    "max_output_tokens", "text", "reasoning", "stream", "store", "background", "previous_response_id",
    "conversation", "truncation", "metadata", "service_tier", "include", "prompt_cache_key",
    "safety_identifier", "user", "top_logprobs", "context_management", "prompt", "max_tool_calls",
    "prompt_cache_retention", "prompt_cache_options", "client_metadata"}


def _openai_settings(value):
    _optional(value, "instructions", lambda x: _string(x, empty=True))
    for field in ("previous_response_id", "conversation", "prompt", "context_management", "prompt_cache_options"):
        if field in value and value[field] is not None:
            _fail("request_server_context_unsupported")
    if value.get("background") is not None:
        _bool(value["background"])
        if value["background"]:
            _fail("request_background_unsupported")
    _optional(value, "truncation", lambda x: _enum(x, {"disabled"}, "request_truncation_unsupported"))
    _optional(value, "tool_choice", lambda x: _enum(x, {"auto", "none"}, "unsupported_request_tool_choice"))
    _optional(value, "temperature", lambda x: _number(x, 0, 2))
    _optional(value, "top_p", lambda x: _number(x, 0, 1))
    _optional(value, "max_output_tokens", lambda x: _number(x, 1, integer=True))
    _optional(value, "max_tool_calls", lambda x: _number(x, 1, integer=True))
    _optional(value, "top_logprobs", lambda x: _number(x, 0, 0, integer=True))
    _optional(value, "text", _text_config)
    _optional(value, "reasoning", lambda x: _reasoning(x, allow_summary=True))
    for field in ("stream", "store", "parallel_tool_calls"):
        _optional(value, field, _bool)
    for field in ("prompt_cache_key", "safety_identifier", "user"):
        _optional(value, field, _string)
    _optional(value, "metadata", _metadata)
    _optional(value, "client_metadata", _client_metadata)
    _optional(value, "service_tier", lambda x: _enum(x, {"auto", "default", "flex", "priority", "fast", "ultrafast"}))
    _optional(value, "prompt_cache_retention", lambda x: _enum(x, {"in_memory", "24h"}))
    # This selector asks for a field; it is not itself hidden content. Actual
    # reasoning/encrypted input or output is still rejected by the closed shapes.
    if "include" in value and value["include"] not in (None, [], ["reasoning.encrypted_content"]):
        _fail("unsupported_request_include")


def _anthropic_text(value):
    if isinstance(value, str):
        _string(value)
        return
    if not isinstance(value, list) or not value:
        _fail("invalid_request_content")
    for block in value:
        _keys(block, {"type", "text"}, {"cache_control", "citations"})
        _enum(block["type"], {"text"}, "unsupported_request_content")
        _string(block["text"])
        _optional(block, "cache_control", _cache)
        if "citations" in block and block["citations"] not in (None, []):
            _fail("unsupported_request_content_metadata")


def _input_anthropic(messages, catalog):
    if not isinstance(messages, list) or not messages:
        _fail("request_input_missing")
    seen, pending, has_user, errors = {}, set(), False, set()
    for message in messages:
        _keys(message, {"role", "content"})
        _enum(message["role"], {"user", "assistant"}, "unsupported_request_role")
        role, content = message["role"], message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or not blocks:
            _fail("invalid_request_content")
        # Every pending batch must be resolved in the immediately following user
        # message, before text. Splitting it over multiple requests is unsupported.
        resolving = bool(pending)
        if resolving and role != "user":
            _fail("request_tool_results_missing")
        result_prefix_ended = False
        for block in blocks:
            if not isinstance(block, dict):
                _fail("invalid_request_content")
            kind = block.get("type")
            if kind in _HIDDEN:
                _fail("hidden_reasoning_unsupported")
            if kind == "text":
                if pending and role != "assistant":
                    _fail("request_tool_results_missing")
                result_prefix_ended = True
                _anthropic_text([block])
                has_user |= role == "user"
            elif kind == "tool_use":
                _keys(block, {"type", "id", "name", "input"})
                _string(block["id"])
                if role != "assistant" or not has_user or block["id"] in seen:
                    _fail("invalid_request_tool_linkage")
                if len(seen) >= MAX_TOOL_CALLS:
                    _fail("request_tool_call_limit")
                _call(block["name"], block["input"], catalog)
                seen[block["id"]] = (block["name"], None); pending.add(block["id"])
            elif kind == "tool_result":
                _keys(block, {"type", "tool_use_id"}, {"content", "is_error", "cache_control"})
                _string(block["tool_use_id"])
                if "is_error" in block:
                    _bool(block["is_error"])
                _optional(block, "cache_control", _cache)
                if role != "user" or block["tool_use_id"] not in pending or result_prefix_ended:
                    _fail("invalid_request_tool_linkage")
                if "content" not in block or block["content"] == []:
                    pass
                elif isinstance(block["content"], str):
                    _string(block["content"], empty=True)
                else:
                    _anthropic_text(block["content"])
                pending.remove(block["tool_use_id"])
                if block.get("is_error") is True:
                    errors.add(block["tool_use_id"])
            else:
                _fail("unsupported_request_content")
        if resolving and pending:
            _fail("request_tool_results_missing")
    if pending:
        _fail("request_tool_results_missing")
    if not has_user:
        _fail("request_user_input_missing")
    if messages[-1]["role"] == "assistant":
        _fail("request_assistant_prefill_unsupported")
    return {"catalog": catalog, "calls": seen, "error_ids": errors}


def _request(provider, request, *, allow_tools=False):
    _enum(provider, PROVIDERS, "unsupported_request_provider")
    # First release deliberately excludes executed tools: the legacy file-tool
    # exclusion path does not yet cover every native exchange representation.
    # Definitions are observed context and are never treated as executions.
    if isinstance(request, dict):
        history = request.get("input", []) if provider == "openai-responses" else request.get("messages", [])
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ({"custom_tool_call", "custom_tool_call_output"} if allow_tools else
                                        {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}):
                    _fail("request_tool_history_not_supported")
                blocks = item.get("content", [])
                if isinstance(blocks, list) and any(isinstance(block, dict) and block.get("type") in
                        ({"server_tool_use"} if allow_tools else {"tool_use", "tool_result", "server_tool_use"}) for block in blocks):
                    _fail("request_tool_history_not_supported")
    if provider == "openai-responses":
        _keys(request, {"model", "input"}, _OPENAI_SETTINGS)
        _openai_settings(request)
        catalog = _contracts(request.get("tools", []), provider)
        state = _input_openai(request["input"], catalog)
    else:
        _keys(request, {"model", "messages", "max_tokens"}, {"system", "tools", "tool_choice", "temperature",
            "top_p", "top_k", "stop_sequences", "stream", "thinking", "metadata", "service_tier", "cache_control",
            "container", "context_management", "mcp_servers", "output_config"})
        _number(request["max_tokens"], 1, integer=True)
        _optional(request, "system", _anthropic_text)
        for field in ("container", "context_management", "mcp_servers"):
            if field in request and request[field] is not None:
                _fail("request_server_context_unsupported")
        if "thinking" in request and request["thinking"] is not None:
            _keys(request["thinking"], {"type"})
            _enum(request["thinking"]["type"], {"disabled"}, "hidden_reasoning_unsupported")
        if "tool_choice" in request and request["tool_choice"] is not None:
            _keys(request["tool_choice"], {"type"}, {"disable_parallel_tool_use"})
            _enum(request["tool_choice"]["type"], {"auto", "none"}, "unsupported_request_tool_choice")
            _optional(request["tool_choice"], "disable_parallel_tool_use", _bool)
        if "output_config" in request and request["output_config"] is not None:
            _keys(request["output_config"], (), {"effort"})
            _optional(request["output_config"], "effort", lambda x: _enum(x, {"low", "medium", "high", "xhigh", "max"}))
        _optional(request, "temperature", lambda x: _number(x, 0, 1))
        _optional(request, "top_p", lambda x: _number(x, 0, 1))
        _optional(request, "top_k", lambda x: _number(x, 0, integer=True))
        _optional(request, "stream", _bool)
        _optional(request, "metadata", _metadata)
        _optional(request, "cache_control", _cache)
        _optional(request, "service_tier", lambda x: _enum(x, {"auto", "standard_only"}))
        if "stop_sequences" in request:
            if not isinstance(request["stop_sequences"], list):
                _fail("unsupported_request_setting")
            for text in request["stop_sequences"]:
                _string(text)
        catalog = _contracts(request.get("tools", []), provider)
        state = _input_anthropic(request["messages"], catalog)
    _string(request["model"])
    return state


def validate_request(provider, request, *, schema=SCHEMA):
    """Validate the exact supported outgoing native body; do not insert defaults."""
    _json(request)
    try:
        _enum(schema, {SCHEMA, TOOL_SCHEMA}, "unsupported_request_schema")
        _request(provider, request, allow_tools=schema == TOOL_SCHEMA)
    except RequestError:
        raise
    except (TypeError, KeyError, ValueError, RecursionError, OverflowError):
        _fail("invalid_request_body")
    return copy.deepcopy(request)


def _usage(value, provider):
    allowed = {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "input_tokens_details", "output_tokens_details", "cache_creation", "server_tool_use",
        "inference_geo", "service_tier", "speed"}
    _keys(value, (), allowed, "unsupported_response_usage")
    nested = {"input_tokens_details": {"cached_tokens", "cache_write_tokens"},
        "output_tokens_details": {"reasoning_tokens", "thinking_tokens"},
        "cache_creation": {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"},
        "server_tool_use": {"web_search_requests", "web_fetch_requests"}}
    for key, item in value.items():
        if item is None:
            continue
        if key in {"inference_geo", "service_tier", "speed"}:
            _string(item)
        elif key in nested:
            _keys(item, (), nested[key], "unsupported_response_usage")
            for number in item.values():
                _number(number, 0, integer=True)
                if key == "server_tool_use" and number:
                    _fail("request_server_context_unsupported")
        else:
            _number(item, 0, integer=True)


def _target(provider, value):
    if provider == "openai-responses":
        _keys(value, {"output"}, (), "invalid_request_target")
        output = value["output"]
        if not isinstance(output, list) or len(output) != 1:
            _fail("request_final_text_required")
        message = output[0]
        _keys(message, {"type", "role", "content"}, {"phase"}, "unsupported_response_output")
        _enum(message["type"], {"message"}, "request_final_text_required")
        _enum(message["role"], {"assistant"}, "request_final_text_required")
        _optional(message, "phase", lambda x: _enum(x, {"final_answer", "final"}, "request_response_incomplete"))
        if not isinstance(message["content"], list) or len(message["content"]) != 1:
            _fail("request_final_text_required")
        _openai_text(message["content"], output=True)
    else:
        _keys(value, {"role", "content"}, (), "invalid_request_target")
        _enum(value["role"], {"assistant"}, "request_final_text_required")
        if not isinstance(value["content"], list) or len(value["content"]) != 1:
            _fail("request_final_text_required")
        block = value["content"][0]
        _keys(block, {"type", "text"}, {"citations"}, "unsupported_response_output")
        _enum(block["type"], {"text"}, "request_final_text_required")
        _string(block["text"])
        if "citations" in block and block["citations"] not in (None, []):
            _fail("unsupported_request_content_metadata")


def _response(provider, response):
    if provider == "openai-responses":
        _keys(response, {"id", "object", "model", "status", "output"},
            {"created_at", "completed_at", "error", "incomplete_details", "usage"} | (_OPENAI_SETTINGS - {"stream"}),
            "unsupported_response_field")
        _enum(response["object"], {"response"}, "invalid_response_body")
        _enum(response["status"], {"completed"}, "request_response_incomplete")
        if response.get("error") is not None or response.get("incomplete_details") is not None:
            _fail("request_response_incomplete")
        _openai_settings(response)
        if "tools" in response:
            _contracts(response["tools"], provider)
        for key in ("created_at", "completed_at"):
            _optional(response, key, lambda x: _number(x, 0))
        output = response["output"]
        if not isinstance(output, list) or len(output) != 1:
            _fail("request_final_text_required")
        item = output[0]
        _keys(item, {"type", "role", "content", "status"}, {"id", "phase"}, "unsupported_response_output")
        _enum(item["status"], {"completed"}, "request_response_incomplete")
        _optional(item, "id", _string)
        target = {"output": [{key: value for key, value in item.items() if key not in {"id", "status"}}]}
    else:
        _keys(response, {"id", "type", "role", "model", "content", "stop_reason"},
            {"stop_sequence", "usage", "stop_details", "container", "context_management", "diagnostics", "input_transformations"},
            "unsupported_response_field")
        _enum(response["type"], {"message"}, "invalid_response_body")
        _enum(response["stop_reason"], {"end_turn"}, "request_response_incomplete")
        if response.get("stop_sequence") is not None:
            _fail("request_response_incomplete")
        for field in ("stop_details", "container", "context_management", "diagnostics"):
            if response.get(field) is not None:
                _fail("unsupported_response_context")
        if response.get("input_transformations") not in (None, []):
            _fail("unsupported_response_context")
        target = {"role": response["role"], "content": response["content"]}
    _string(response["id"])
    _string(response["model"])
    _optional(response, "usage", lambda x: _usage(x, provider))
    _target(provider, target)
    return target


def _tool_target(provider, target, state):
    """Validate a native completed assistant tool batch, without executing it."""
    calls, started = {}, False
    if provider == "openai-responses":
        _keys(target, {"output"}, (), "invalid_request_target")
        items = target["output"]
        if not isinstance(items, list) or not items:
            _fail("request_tool_response_required")
        for item in items:
            if isinstance(item, dict) and item.get("type") == "message":
                if started:
                    _fail("request_tool_batch_interleaved")
                _keys(item, {"type", "role", "content"}, {"phase"}, "unsupported_response_output")
                _enum(item["role"], {"assistant"}, "unsupported_response_output")
                _optional(item, "phase", lambda x: _enum(x, {"commentary"}, "request_response_incomplete"))
                _openai_text(item["content"], output=True)
                continue
            _keys(item, {"type", "call_id", "name", "arguments"}, {"namespace"}, "unsupported_response_output")
            _enum(item["type"], {"function_call"}, "unsupported_response_output")
            _string(item["call_id"])
            ident = item["call_id"]
            if ident in calls or ident in state["calls"]:
                _fail("invalid_request_tool_linkage")
            if len(calls) + len(state["calls"]) >= MAX_TOOL_CALLS:
                _fail("request_tool_call_limit")
            _call(item["name"], _loads(item["arguments"]), state["catalog"], item.get("namespace"))
            calls[ident] = (item["name"], item.get("namespace")); started = True
    else:
        _keys(target, {"role", "content", "stop_reason"}, (), "invalid_request_target")
        _enum(target["role"], {"assistant"}, "unsupported_response_output")
        _enum(target["stop_reason"], {"tool_use"}, "request_response_incomplete")
        if not isinstance(target["content"], list) or not target["content"]:
            _fail("request_tool_response_required")
        for block in target["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                _keys(block, {"type", "text"}, {"citations"}, "unsupported_response_output")
                _anthropic_text([block])
                continue
            _keys(block, {"type", "id", "name", "input"}, (), "unsupported_response_output")
            _enum(block["type"], {"tool_use"}, "unsupported_response_output")
            _string(block["id"])
            ident = block["id"]
            if ident in calls or ident in state["calls"]:
                _fail("invalid_request_tool_linkage")
            if len(calls) + len(state["calls"]) >= MAX_TOOL_CALLS:
                _fail("request_tool_call_limit")
            _call(block["name"], block["input"], state["catalog"])
            calls[ident] = (block["name"], None)
    if not calls:
        _fail("request_tool_response_required")
    if len(calls) + len(state["calls"]) > MAX_TOOL_CALLS:
        _fail("request_tool_call_limit")
    return calls


def _response_tools(provider, response, state):
    pending = (provider == "openai-responses" and isinstance(response, dict)
               and isinstance(response.get("output"), list)
               and any(isinstance(item, dict) and item.get("type") == "function_call" for item in response["output"]))
    pending |= provider == "anthropic-messages" and isinstance(response, dict) and response.get("stop_reason") == "tool_use"
    if not pending:
        target = _response(provider, response)
        if provider == "anthropic-messages":
            target = {**target, "stop_reason": response["stop_reason"]}
        return target, {}
    if provider == "openai-responses":
        _keys(response, {"id", "object", "model", "status", "output"},
            {"created_at", "completed_at", "error", "incomplete_details", "usage"} | (_OPENAI_SETTINGS - {"stream"}),
            "unsupported_response_field")
        _enum(response["object"], {"response"}, "invalid_response_body")
        _enum(response["status"], {"completed"}, "request_response_incomplete")
        if response.get("error") is not None or response.get("incomplete_details") is not None:
            _fail("request_response_incomplete")
        _openai_settings(response)
        if "tools" in response:
            _contracts(response["tools"], provider)
        for key in ("created_at", "completed_at"):
            _optional(response, key, lambda x: _number(x, 0))
        items = []
        for item in response["output"]:
            if not isinstance(item, dict):
                _fail("unsupported_response_output")
            if item.get("type") == "message":
                _keys(item, {"type", "role", "content", "status"}, {"id", "phase"}, "unsupported_response_output")
                _enum(item["status"], {"completed"}, "request_response_incomplete")
            else:
                _keys(item, {"type", "call_id", "name", "arguments"}, {"id", "status", "namespace"}, "unsupported_response_output")
                _optional(item, "status", lambda x: _enum(x, {"completed"}, "request_response_incomplete"))
            _optional(item, "id", _string)
            items.append({key: value for key, value in item.items() if key not in {"id", "status"}})
        target = {"output": items}
    else:
        _keys(response, {"id", "type", "role", "model", "content", "stop_reason"},
            {"stop_sequence", "usage", "stop_details", "container", "context_management", "diagnostics", "input_transformations"},
            "unsupported_response_field")
        _enum(response["type"], {"message"}, "invalid_response_body")
        if response.get("stop_sequence") is not None:
            _fail("request_response_incomplete")
        for key in ("stop_details", "container", "context_management", "diagnostics"):
            if response.get(key) is not None:
                _fail("unsupported_response_context")
        if response.get("input_transformations") not in (None, []):
            _fail("unsupported_response_context")
        target = {key: response[key] for key in ("role", "content", "stop_reason")}
    _string(response["id"])
    _string(response["model"])
    _optional(response, "usage", lambda x: _usage(x, provider))
    calls = _tool_target(provider, target, state)
    return target, calls


def _check_response_tool_choice(provider, native_request, pending):
    if not pending:
        return
    choice = native_request.get("tool_choice")
    if (provider == "openai-responses" and choice == "none") or (
            provider == "anthropic-messages" and isinstance(choice, dict) and choice.get("type") == "none"):
        _fail("request_tool_choice_violation")
    if len(pending) > 1 and ((provider == "openai-responses" and native_request.get("parallel_tool_calls") is False) or
            (provider == "anthropic-messages" and isinstance(choice, dict) and choice.get("disable_parallel_tool_use") is True)):
        _fail("request_parallel_tool_violation")


def normalize_exchange(provider, request, response, *, transport="json", schema=SCHEMA):
    _json({"request": request, "response": response})
    value = {"schema": schema, "provider": provider, "transport": transport,
             "request": copy.deepcopy(request), "response": copy.deepcopy(response)}
    validate_exchange(value)
    return value


def validate_exchange(exchange):
    _json(exchange)
    try:
        _keys(exchange, {"schema", "provider", "transport", "request", "response"}, (), "invalid_request_exchange")
        if exchange["schema"] not in {SCHEMA, TOOL_SCHEMA}:
            _fail("unsupported_request_schema")
        _enum(exchange["provider"], PROVIDERS, "unsupported_request_provider")
        _enum(exchange["transport"], {"json", "sse"}, "unsupported_request_transport")
        state = _request(exchange["provider"], exchange["request"], allow_tools=exchange["schema"] == TOOL_SCHEMA)
        stream = exchange["request"].get("stream")
        if (exchange["transport"] == "sse" and stream is not True) or (exchange["transport"] == "json" and stream is True):
            _fail("request_transport_mismatch")
        if exchange["schema"] == TOOL_SCHEMA:
            _, pending = _response_tools(exchange["provider"], exchange["response"], state)
            _check_response_tool_choice(exchange["provider"], exchange["request"], pending)
        else:
            _response(exchange["provider"], exchange["response"])
    except RequestError:
        raise
    except (TypeError, KeyError, ValueError, RecursionError, OverflowError):
        _fail("invalid_request_exchange")


def request_messages(exchange):
    """Return a visible UI projection. Native instructions/settings stay native."""
    validate_exchange(exchange)
    provider, request = exchange["provider"], exchange["request"]
    messages = []
    if provider == "openai-responses":
        items = request["input"]
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        for item in items:
            kind = item.get("type", "message" if "role" in item else None)
            if kind == "message":
                content = item["content"]
                texts = [content] if isinstance(content, str) else [block["text"] for block in content]
                messages.append({"role": item["role"], "content": [{"type": "text", "text": text} for text in texts]})
            elif kind == "function_call":
                messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": item["call_id"],
                    "name": (item["namespace"] + "." if item.get("namespace") else "") + item["name"],
                    "input": _loads(item["arguments"])}]})
            elif kind == "function_call_output":
                output = item["output"]
                blocks = [{"type": "text", "text": output}] if isinstance(output, str) else [
                    {"type": "text", "text": block["text"]} for block in output]
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": blocks})
        for item in exchange["response"]["output"]:
            if item["type"] == "function_call":
                messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": item["call_id"],
                    "name": (item["namespace"] + "." if item.get("namespace") else "") + item["name"],
                    "input": _loads(item["arguments"])}]})
            else:
                messages.append({"role": "assistant", "content": [{"type": "text", "text": b["text"]} for b in item["content"]]})
    else:
        for item in request["messages"]:
            blocks = [{"type": "text", "text": item["content"]}] if isinstance(item["content"], str) else item["content"]
            content = []
            for block in blocks:
                if block["type"] == "text":
                    content.append({"type": "text", "text": block["text"]})
                elif block["type"] == "tool_use":
                    content.append({key: copy.deepcopy(block[key]) for key in ("type", "id", "name", "input")})
                else:
                    result = block.get("content", "")
                    if isinstance(result, list):
                        result = [{"type": "text", "text": b["text"]} for b in result]
                    content.append({"type": "tool_result", "tool_use_id": block["tool_use_id"], "content": copy.deepcopy(result),
                                    **({"is_error": block["is_error"]} if "is_error" in block else {})})
            messages.append({"role": item["role"], "content": content})
        content = []
        for block in exchange["response"]["content"]:
            content.append({"type": "text", "text": block["text"]} if block["type"] == "text" else
                           {key: copy.deepcopy(block[key]) for key in ("type", "id", "name", "input")})
        messages.append({"role": "assistant", "content": content})
    return messages


def _exchange_parts(exchange):
    validate_exchange(exchange)
    state = _request(exchange["provider"], exchange["request"], allow_tools=exchange["schema"] == TOOL_SCHEMA)
    if exchange["schema"] == TOOL_SCHEMA:
        target, pending = _response_tools(exchange["provider"], exchange["response"], state)
    else:
        target, pending = _response(exchange["provider"], exchange["response"]), {}
    return state, target, pending


def exchange_state(exchange):
    """Derived linkage facts, not task success or privacy/evidence approval.

    Identifiers are source values for internal binding; do not put them in a
    content-free public diagnostic. OpenAI output strings never imply is_error.
    """
    state, _, pending = _exchange_parts(exchange)
    return {"response_kind": "tool_calls" if pending else "final_text",
            "pending_call_ids": list(pending), "history_call_ids": list(state["calls"]),
            "error_call_ids": [ident for ident in state["calls"] if ident in state["error_ids"]],
            "history_tool_result_count": len(state["calls"])}


def response_target(exchange):
    """Return the native assistant target projection, without context or evidence.

    Audit-only OpenAI item id/status are omitted; call_id and namespace remain.
    Exchange/2 Anthropic targets retain the observed stop_reason. Use the original
    response, not this projection, to verify the next request's history prefix.
    """
    return copy.deepcopy(_exchange_parts(exchange)[1])


def sample_for_exchange(exchange):
    """Pure exchange/2 serialization; this function confers no SFT eligibility.

    The versioned episode exporter must check authorization, tool/file privacy,
    exact cross-request links, task outcome and separate tool-step reviews.
    """
    if not isinstance(exchange, dict) or exchange.get("schema") != TOOL_SCHEMA:
        _fail("request_tool_exchange_required")
    target = response_target(exchange)
    sample = {"prompt": _json({"schema": TOOL_SERIALIZATION, "provider": exchange["provider"],
                                "request": exchange["request"]}) + "\n",
              "completion": _json({"response": target}) + "\n"}
    validate_exchange_sample(sample)
    return sample


def validate_exchange_sample(sample):
    """Independently validate the versioned step serialization and tool linkage."""
    _keys(sample, {"prompt", "completion"}, (), "invalid_request_sample")
    if not all(isinstance(sample[key], str) for key in sample):
        _fail("invalid_request_sample")
    _json(sample)
    prompt, completion = (_loads(sample[key]) for key in ("prompt", "completion"))
    if any(sample[key] != _json(value) + "\n" for key, value in (("prompt", prompt), ("completion", completion))):
        _fail("request_noncanonical_serialization")
    _keys(prompt, {"schema", "provider", "request"}, (), "invalid_request_sample_header")
    _keys(completion, {"response"}, (), "invalid_request_sample_target")
    if prompt["schema"] != TOOL_SERIALIZATION:
        _fail("unsupported_request_serialization")
    validate_request(prompt["provider"], prompt["request"], schema=TOOL_SCHEMA)
    state = _request(prompt["provider"], prompt["request"], allow_tools=True)
    target, provider = completion["response"], prompt["provider"]
    try:
        pending = (provider == "openai-responses" and isinstance(target, dict)
                   and isinstance(target.get("output"), list)
                   and any(isinstance(item, dict) and item.get("type") == "function_call" for item in target["output"]))
        pending |= provider == "anthropic-messages" and isinstance(target, dict) and target.get("stop_reason") == "tool_use"
        if pending:
            calls = _tool_target(provider, target, state)
            _check_response_tool_choice(provider, prompt["request"], calls)
        elif provider == "anthropic-messages":
            _keys(target, {"role", "content", "stop_reason"}, (), "invalid_request_sample_target")
            _enum(target["stop_reason"], {"end_turn"}, "request_response_incomplete")
            _target(provider, {key: target[key] for key in ("role", "content")})
        else:
            _target(provider, target)
    except RequestError:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("invalid_request_sample_target")
    return True


def validate_record(record, *, require_quality=True):
    _json(record)
    if not isinstance(record, dict) or record.get("schema") != "nous.trajectory/3":
        _fail("request_trajectory_required")
    if {"protocol", "events", "context", "tool_contracts", "tools"} & record.keys():
        _fail("request_unbound_context_extension")
    validate_exchange(record.get("exchange"))
    if record["exchange"]["schema"] != SCHEMA:
        _fail("request_tool_exchange_requires_episode")
    if _json(record.get("messages")) != _json(request_messages(record["exchange"])):
        _fail("request_message_projection_mismatch")
    provenance = record.get("provenance")
    digest = hashlib.sha256(_json(record["exchange"]["request"]).encode("utf-8")).hexdigest()
    if not isinstance(provenance, dict) or provenance.get("request_content_sha256") != digest:
        _fail("request_content_digest_mismatch")
    if require_quality:
        quality = record.get("quality")
        if (not isinstance(quality, dict) or quality.get("complete") is not True
                or quality.get("issues") != [] or quality.get("missing") != []):
            _fail("request_quality_incomplete")


def sample_for(record, *, require_quality=True):
    validate_record(record, require_quality=require_quality)
    privacy = record.get("privacy")
    if not isinstance(privacy, dict) or privacy.get("scrubbed") is not True:
        _fail("request_privacy_not_scrubbed")
    exchange = record["exchange"]
    sample = {"prompt": _json({"schema": SERIALIZATION, "provider": exchange["provider"], "request": exchange["request"]}) + "\n",
              "completion": _json({"response": _response(exchange["provider"], exchange["response"])}) + "\n"}
    validate_sample(sample)
    return sample


def validate_sample(sample):
    _keys(sample, {"prompt", "completion"}, (), "invalid_request_sample")
    if not all(isinstance(sample[key], str) for key in sample):
        _fail("invalid_request_sample")
    _json(sample)
    prompt, completion = (_loads(sample[key]) for key in ("prompt", "completion"))
    if any(sample[key] != _json(value) + "\n" for key, value in (("prompt", prompt), ("completion", completion))):
        _fail("request_noncanonical_serialization")
    _keys(prompt, {"schema", "provider", "request"}, (), "invalid_request_sample_header")
    _keys(completion, {"response"}, (), "invalid_request_sample_target")
    if prompt["schema"] != SERIALIZATION:
        _fail("unsupported_request_serialization")
    validate_request(prompt["provider"], prompt["request"])
    try:
        _target(prompt["provider"], completion["response"])
    except RequestError:
        raise
    except (KeyError, TypeError, ValueError):
        _fail("invalid_request_sample_target")
    return True
