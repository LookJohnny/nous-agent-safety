"""Bounded HTTP/SSE primitives for the explicit request observer.

No file writes, proxy discovery, redirects, logging, or implicit credentials.
Returned bodies and native responses are UNSCRUBBED, in-memory values: callers
must apply project authorization and the privacy pipeline before persistence.
This decoder deliberately rejects hidden reasoning and unsupported stream forms.
"""
import copy
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 16384
MAX_DEPTH = 64
IO_TIMEOUT = 15
TOTAL_TIMEOUT = 30
PROVIDERS = {"openai-responses", "anthropic-messages"}
ENDPOINTS = {"api.openai.com": "/v1/responses", "api.anthropic.com": "/v1/messages"}


class TransportError(ValueError):
    """Only static codes, never provider text, headers, URLs, or body excerpts."""


def _fail(code):
    raise TransportError(code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("duplicate_json_key")
            result[key] = value
        return result
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail("invalid_json_number"))
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > MAX_DEPTH:
                _fail("json_depth_limit")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8", errors="strict")
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                item.encode("utf-8", errors="strict")
            elif isinstance(item, float) and not math.isfinite(item):
                _fail("invalid_json_number")
        return value
    except TransportError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail("invalid_json")


def _identity(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        _fail("invalid_response_identity")
    return value


def _index(value):
    if type(value) is not int or not 0 <= value < MAX_EVENTS:
        _fail("invalid_stream_index")
    return value


def _hidden(value):
    if isinstance(value, dict):
        if value.get("type") in {"reasoning", "thinking", "redacted_thinking", "reasoning_text", "summary_text"}:
            _fail("unsupported_hidden_reasoning")
        for child in value.values():
            _hidden(child)
    elif isinstance(value, list):
        for child in value:
            _hidden(child)


def _openai_item(item, pending=False):
    if not isinstance(item, dict):
        _fail("unsupported_response_item")
    _hidden(item)
    _identity(item.get("id"))
    status = item.get("status")
    if status is not None and status not in (("in_progress", "completed") if pending else ("completed",)):
        _fail("partial_response")
    if item.get("type") == "message":
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            _fail("unsupported_response_item")
        for block in item["content"]:
            if (not isinstance(block, dict) or block.get("type") != "output_text"
                    or not isinstance(block.get("text"), str)):
                _fail("unsupported_response_item")
    elif item.get("type") == "function_call":
        _identity(item.get("call_id"))
        if not isinstance(item.get("name"), str) or not item["name"] or not isinstance(item.get("arguments"), str):
            _fail("unsupported_response_item")
    else:
        _fail("unsupported_response_item")
    return item


def _anthropic_block(block):
    if not isinstance(block, dict):
        _fail("unsupported_response_item")
    _hidden(block)
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return block
    if block.get("type") == "tool_use" and isinstance(block.get("name"), str) and block["name"] and isinstance(block.get("input"), dict):
        _identity(block.get("id"))
        return block
    _fail("unsupported_response_item")


def _native(provider, value):
    if not isinstance(value, dict):
        _fail("invalid_native_response")
    _hidden(value)
    _identity(value.get("id"))
    if provider == "openai-responses":
        if value.get("status") != "completed" or value.get("error") is not None or value.get("incomplete_details") is not None:
            _fail("partial_response")
        if value.get("object", "response") != "response" or not isinstance(value.get("output"), list):
            _fail("invalid_native_response")
        for item in value["output"]:
            _openai_item(item)
        ids = [item["id"] for item in value["output"]]
        if len(ids) != len(set(ids)):
            _fail("duplicate_response_item")
    else:
        if value.get("type") != "message" or value.get("role") != "assistant" or not isinstance(value.get("content"), list):
            _fail("invalid_native_response")
        if value.get("stop_reason") not in ("end_turn", "tool_use", "stop_sequence"):
            _fail("partial_response")
        for block in value["content"]:
            _anthropic_block(block)
        ids = [block["id"] for block in value["content"] if block["type"] == "tool_use"]
        if len(ids) != len(set(ids)):
            _fail("duplicate_response_item")
    return value


def _events(raw):
    try:
        text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        _fail("invalid_sse_encoding")
    if not text.endswith("\n\n"):
        _fail("partial_sse_frame")
    count, sequence = 0, None
    for frame in text.split("\n\n"):
        if not frame:
            continue
        name, data = None, []
        for line in frame.split("\n"):
            if line.startswith(":") or not line:
                continue
            field, sep, value = line.partition(":")
            if not sep:
                _fail("unsupported_sse_field")
            value = value[1:] if value.startswith(" ") else value
            if field == "event" and name is None:
                name = value
            elif field == "data":
                data.append(value)
            else:
                _fail("unsupported_sse_field")
        if not data and name is None:
            continue
        event = _json("\n".join(data))
        if not isinstance(event, dict) or not isinstance(event.get("type"), str) or (name is not None and name != event["type"]):
            _fail("sse_event_type_mismatch")
        if "sequence_number" in event:
            value = event["sequence_number"]
            if type(value) is not int or not 0 <= value < 2 ** 63 or (sequence is not None and value <= sequence):
                _fail("invalid_stream_sequence")
            sequence = value
        count += 1
        if count > MAX_EVENTS:
            _fail("sse_event_limit")
        yield event


def _openai_sse(raw):
    start, terminal, final = None, None, {}
    pending, parts, arguments = {}, {}, {}
    for event in _events(raw):
        kind = event["type"]
        if terminal is not None:
            _fail("duplicate_terminal" if kind == "response.completed" else "event_after_terminal")
        if "reasoning" in kind or "thinking" in kind:
            _fail("unsupported_hidden_reasoning")
        _hidden(event)
        if kind in ("error", "response.failed", "response.incomplete"):
            _fail("non_successful_response")
        if kind == "response.created":
            if start is not None or not isinstance(event.get("response"), dict):
                _fail("duplicate_or_invalid_start")
            start = copy.deepcopy(event["response"])
            _identity(start.get("id"))
            if start.get("output", []) != []:
                _fail("unsupported_stream_start")
            continue
        if start is None:
            _fail("stream_start_missing")
        response = event.get("response")
        for identity in (event.get("response_id"), response.get("id") if isinstance(response, dict) else None):
            if identity is not None and identity != start["id"]:
                _fail("response_id_mismatch")
        if kind == "response.in_progress":
            if not isinstance(response, dict) or response.get("id") != start["id"]:
                _fail("response_id_mismatch")
            continue
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = _openai_item(event.get("item"), pending=kind.endswith("added"))
            if "output_index" in event:
                index = _index(event["output_index"])
            else:
                matches = [i for i, value in pending.items() if value["id"] == item["id"]]
                index = matches[0] if len(matches) == 1 else len(final)
            if index in final:
                _fail("duplicate_response_item")
            if kind.endswith("added"):
                if index in pending:
                    _fail("duplicate_response_item")
                pending[index] = item
                continue
            original = pending.pop(index, None)
            if original and (original["id"] != item["id"] or original["type"] != item["type"]):
                _fail("response_item_mismatch")
            if original and item["type"] == "function_call" and any(original.get(k) != item.get(k) for k in ("call_id", "name")):
                _fail("response_item_mismatch")
            if original and item["type"] == "message" and original["content"] and original["content"] != item["content"]:
                _fail("unsupported_stream_start")
            if item["type"] == "message":
                for (i, j), part in list(parts.items()):
                    if i == index:
                        if j >= len(item["content"]) or part["item_id"] != item["id"] or item["content"][j]["text"] != part["text"]:
                            _fail("stream_content_mismatch")
                        del parts[(i, j)]
            elif index in arguments:
                if arguments[index] != item["arguments"]:
                    _fail("stream_content_mismatch")
                del arguments[index]
            final[index] = item
            continue
        if kind == "response.completed":
            if not isinstance(response, dict) or response.get("id") != start["id"]:
                _fail("response_id_mismatch")
            if pending or parts or arguments or (final and sorted(final) != list(range(len(final)))):
                _fail("partial_response")
            items = [final[i] for i in sorted(final)]
            if "output" in response and final and response["output"] != items:
                _fail("terminal_output_mismatch")
            terminal = {**start, **copy.deepcopy(response)}
            terminal.setdefault("status", "completed")
            # A start's in_progress status cannot substitute for terminal status.
            terminal["status"] = response.get("status", "completed")
            terminal["output"] = copy.deepcopy(response.get("output", items))
            continue
        if kind not in ("response.content_part.added", "response.content_part.done", "response.output_text.delta", "response.output_text.done",
                        "response.function_call_arguments.delta", "response.function_call_arguments.done"):
            _fail("unsupported_sse_event")
        index = _index(event.get("output_index"))
        item = pending.get(index)
        if item is None or event.get("item_id") != item["id"]:
            _fail("response_item_mismatch")
        if "function_call_arguments" in kind:
            if item["type"] != "function_call":
                _fail("response_item_mismatch")
            if kind.endswith("delta"):
                if not isinstance(event.get("delta"), str):
                    _fail("invalid_stream_delta")
                arguments[index] = arguments.get(index, item["arguments"]) + event["delta"]
            else:
                value = event.get("arguments")
                if not isinstance(value, str) or (index in arguments and arguments[index] != value):
                    _fail("stream_content_mismatch")
                arguments[index] = value
            continue
        if item["type"] != "message":
            _fail("response_item_mismatch")
        key = (index, _index(event.get("content_index")))
        if kind == "response.content_part.added":
            block = event.get("part")
            if key in parts or not isinstance(block, dict) or block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                _fail("unsupported_response_item")
            parts[key] = {"text": block["text"], "item_id": item["id"]}
        else:
            if key not in parts:
                _fail("stream_part_missing")
            value = event.get("delta") if kind.endswith("delta") else event.get("text") if kind == "response.output_text.done" else event.get("part", {}).get("text")
            if not isinstance(value, str):
                _fail("invalid_stream_delta")
            if kind.endswith("delta"):
                parts[key]["text"] += value
            elif parts[key]["text"] != value:
                _fail("stream_content_mismatch")
    if terminal is None:
        _fail("terminal_missing")
    return _native("openai-responses", terminal)


def _anthropic_sse(raw):
    message, terminal, stop_seen = None, False, False
    blocks, stopped, fragments = {}, set(), {}
    for event in _events(raw):
        kind = event["type"]
        if terminal:
            _fail("duplicate_terminal" if kind == "message_stop" else "event_after_terminal")
        _hidden(event)
        if kind == "error":
            _fail("non_successful_response")
        if kind == "ping":
            continue
        if kind == "message_start":
            if message is not None or not isinstance(event.get("message"), dict):
                _fail("duplicate_or_invalid_start")
            message = copy.deepcopy(event["message"])
            _identity(message.get("id"))
            if message.get("content") != []:
                _fail("unsupported_stream_start")
            continue
        if message is None:
            _fail("stream_start_missing")
        if event.get("message_id", message["id"]) != message["id"]:
            _fail("response_id_mismatch")
        if kind == "message_delta":
            delta = event.get("delta")
            if stop_seen or not isinstance(delta, dict) or set(delta) - {"stop_reason", "stop_sequence"}:
                _fail("invalid_message_delta")
            message.update(delta)
            usage = event.get("usage", {})
            if not isinstance(usage, dict):
                _fail("invalid_message_delta")
            message["usage"] = {**message.get("usage", {}), **usage}
            stop_seen = True
            continue
        if kind == "message_stop":
            if not stop_seen or set(blocks) != stopped or sorted(blocks) != list(range(len(blocks))):
                _fail("partial_response")
            message["content"] = [blocks[i] for i in sorted(blocks)]
            terminal = True
            continue
        if stop_seen:
            _fail("event_after_message_delta")
        index = _index(event.get("index"))
        if kind == "content_block_start":
            if index in blocks:
                _fail("duplicate_response_item")
            blocks[index] = copy.deepcopy(_anthropic_block(event.get("content_block")))
        elif kind == "content_block_delta":
            block, delta = blocks.get(index), event.get("delta")
            if block is None or index in stopped or not isinstance(delta, dict):
                _fail("invalid_stream_delta")
            if block["type"] == "text" and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                block["text"] += delta["text"]
            elif block["type"] == "tool_use" and delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                if block["input"]:
                    _fail("stream_content_mismatch")
                fragments[index] = fragments.get(index, "") + delta["partial_json"]
            else:
                _fail("unsupported_sse_event")
        elif kind == "content_block_stop":
            if index not in blocks or index in stopped:
                _fail("duplicate_or_missing_block_stop")
            if index in fragments:
                blocks[index]["input"] = _json(fragments[index])
                _anthropic_block(blocks[index])
            stopped.add(index)
        else:
            _fail("unsupported_sse_event")
    if not terminal:
        _fail("terminal_missing")
    return _native("anthropic-messages", message)


def decode_response(provider, body, content_type):
    """Return (validated native response, 'json'|'sse'); never persist raw data."""
    if not isinstance(provider, str) or provider not in PROVIDERS:
        _fail("unsupported_provider")
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY_BYTES:
        _fail("response_size_limit")
    if not isinstance(content_type, str):
        _fail("unsupported_content_type")
    media, *params = [part.strip().lower() for part in content_type.split(";")]
    if any(part not in ('charset=utf-8', 'charset="utf-8"') for part in params):
        _fail("unsupported_content_type")
    try:
        if media == "application/json":
            return _native(provider, _json(body.decode("utf-8", errors="strict"))), "json"
        if media == "text/event-stream":
            return (_openai_sse(body) if provider == "openai-responses" else _anthropic_sse(body)), "sse"
        _fail("unsupported_content_type")
    except TransportError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        _fail("invalid_native_response")


def _endpoint(endpoint, allow_loopback):
    try:
        if not isinstance(endpoint, str) or len(endpoint) > 4096 or any(c.isspace() or ord(c) < 32 for c in endpoint) or any(c in endpoint for c in "\\?#"):
            _fail("endpoint_not_allowed")
        url = urlsplit(endpoint)
        if url.username is not None or url.password is not None or not url.hostname:
            _fail("endpoint_not_allowed")
        official = url.scheme == "https" and url.hostname in ENDPOINTS and url.path == ENDPOINTS[url.hostname] and url.port in (None, 443)
        local = allow_loopback is True and url.scheme == "http" and url.hostname in ("127.0.0.1", "::1") and url.path in ENDPOINTS.values() and url.port is not None and url.port > 0
        if not (official or local):
            _fail("endpoint_not_allowed")
        return url
    except TransportError:
        raise
    except (ValueError, TypeError):
        _fail("endpoint_not_allowed")


def _headers(headers):
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        _fail("headers_not_allowed")
    result = {"content-type": "application/json"}
    seen = set()
    for key, value in headers.items():
        if not isinstance(key, str) or key.lower() not in {"authorization", "x-api-key", "content-type", "accept", "anthropic-version", "openai-version"}:
            _fail("headers_not_allowed")
        name = key.lower()
        if name in seen or not isinstance(value, str) or not value or len(value) > 4096 or any(not 32 <= ord(c) < 127 for c in value):
            _fail("headers_not_allowed")
        seen.add(name)
        if name == "content-type" and value.lower() not in ("application/json", "application/json; charset=utf-8"):
            _fail("headers_not_allowed")
        if name == "accept" and value.lower() not in ("application/json", "text/event-stream"):
            _fail("headers_not_allowed")
        if name == "authorization" and (not value.startswith("Bearer ") or not value[7:].strip()):
            _fail("headers_not_allowed")
        if name.endswith("version") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            _fail("headers_not_allowed")
        result[name] = value
    return result


def post_json(endpoint, request_bytes, *, headers=None, allow_loopback=False):
    """Send precisely these JSON bytes. HTTPConnection never uses proxy env vars.

    Official HTTPS destinations are fixed; loopback HTTP is opt-in for tests.
    Non-redirect HTTP errors return status/body for the caller to classify.
    """
    url = _endpoint(endpoint, allow_loopback)
    allowed_headers = _headers(headers)
    if url.scheme == "http" and {"authorization", "x-api-key"} & allowed_headers.keys():
        _fail("loopback_credentials_not_allowed")
    if not isinstance(request_bytes, bytes) or not request_bytes or len(request_bytes) > MAX_BODY_BYTES:
        _fail("request_size_limit")
    if not isinstance(_json(request_bytes), dict):
        _fail("invalid_json_request")
    connection, watchdog = None, None
    expired = threading.Event()
    deadline = time.monotonic() + TOTAL_TIMEOUT
    try:
        connection = (http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=IO_TIMEOUT, context=ssl.create_default_context())
                      if url.scheme == "https" else http.client.HTTPConnection(url.hostname, url.port, timeout=IO_TIMEOUT))
        connection.request("POST", url.path, body=request_bytes, headers=allowed_headers)
        wire_socket = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("transport_timeout")
        def expire():
            expired.set()
            if wire_socket is not None:
                try:
                    wire_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        # Bound slow headers/chunk frames too, even if bytes keep arriving often
        # enough to reset the per-socket idle timeout.
        watchdog = threading.Timer(remaining, expire)
        watchdog.daemon = True
        watchdog.start()
        response = connection.getresponse()
        if 300 <= response.status < 400:
            _fail("redirect_not_allowed")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            _fail("unsupported_content_encoding")
        length = response.getheader("Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]+", length) or int(length) > MAX_BODY_BYTES):
            _fail("response_size_limit")
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("transport_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(IO_TIMEOUT, remaining))
            chunk = response.read1(min(65536, MAX_BODY_BYTES + 1 - len(body)))
            if expired.is_set() or time.monotonic() >= deadline:
                _fail("transport_timeout")
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                _fail("response_size_limit")
            if not chunk:
                break
        if length is not None and len(body) != int(length):
            _fail("partial_http_body")
        return {"body": bytes(body), "content_type": response.getheader("Content-Type", ""), "status": response.status}
    except TransportError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout, ValueError, OverflowError):
        _fail("transport_timeout" if expired.is_set() else "transport_request_failed")
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if connection is not None:
            try:
                connection.close()
            except (OSError, http.client.HTTPException):
                pass
