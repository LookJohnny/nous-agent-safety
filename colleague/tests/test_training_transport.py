"""Synthetic bytes and mocked sockets only; never starts a CLI or external HTTP."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import tomllib
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "hooks"))
import nous_training_transport as tr
sys.path.insert(0, str(BASE / "scripts"))
import training_request_smoke as smoke


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def sse(events):
    return b"".join(b"event: " + event["type"].encode() + b"\ndata: " + encoded(event) + b"\n\n" for event in events)


def message(text='{"answer":4}'):
    return {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


def openai_events():
    return [{"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": message()},
            {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed"}}]


def native():
    return {"id": "resp_fixture", "object": "response", "status": "completed", "output": [message()]}


def anthropic_events():
    return [{"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant", "content": [],
                                                 "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"answer":4}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 4}},
            {"type": "message_stop"}]


class DecoderTests(unittest.TestCase):
    def rejects(self, events, code, provider="openai-responses"):
        with self.assertRaisesRegex(tr.TransportError, "^" + code + "$"):
            tr.decode_response(provider, sse(events), "text/event-stream")

    def test_json_and_three_event_openai_are_same_visible_output(self):
        plain, label = tr.decode_response("openai-responses", encoded(native()), "application/json; charset=utf-8")
        stream, kind = tr.decode_response("openai-responses", sse(openai_events()), "text/event-stream")
        self.assertEqual((label, kind), ("json", "sse"))
        self.assertEqual(plain["output"], stream["output"])
        self.assertEqual(stream["status"], "completed")

    def test_official_minimal_fixture_without_indices_or_terminal_output(self):
        events = openai_events()
        del events[1]["output_index"]
        del events[2]["response"]["status"]
        result, _ = tr.decode_response("openai-responses", sse(events), "text/event-stream")
        self.assertEqual(result["output"], [message()])

    def test_full_text_delta_and_terminal_agree(self):
        item = message("hello")
        events = [openai_events()[0],
                  {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                  {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "item_id": item["id"], "part": {"type": "output_text", "text": ""}},
                  {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": item["id"], "delta": "hello"},
                  {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": item["id"], "text": "hello"},
                  {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "item_id": item["id"], "part": {"type": "output_text", "text": "hello"}},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed", "output": [item]}}]
        self.assertEqual(tr.decode_response("openai-responses", sse(events), "text/event-stream")[0]["output"], [item])
        events[3]["delta"] = "wrong"
        self.rejects(events, "stream_content_mismatch")

    def test_function_call_arguments_are_reassembled_and_checked(self):
        item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "lookup", "arguments": '{"key":"beta"}', "status": "completed"}
        events = [openai_events()[0],
                  {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
                  {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
                  {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
                  {"type": "response.output_item.done", "output_index": 0, "item": item}, openai_events()[-1]]
        self.assertEqual(tr.decode_response("openai-responses", sse(events), "text/event-stream")[0]["output"], [item])
        events[-2]["item"] = {**item, "call_id": "changed"}
        self.rejects(events, "response_item_mismatch")

    def test_truncated_stream_and_frame_are_rejected(self):
        self.rejects(openai_events()[:-1], "terminal_missing")
        with self.assertRaisesRegex(tr.TransportError, "partial_sse_frame"):
            tr.decode_response("openai-responses", sse(openai_events())[:-1], "text/event-stream")

    def test_duplicate_terminal_and_events_after_terminal_are_rejected(self):
        self.rejects(openai_events() + [openai_events()[-1]], "duplicate_terminal")
        self.rejects(openai_events() + [openai_events()[1]], "event_after_terminal")

    def test_response_and_item_identity_mismatch_are_rejected(self):
        events = openai_events()
        events[-1]["response"]["id"] = "resp_other"
        self.rejects(events, "response_id_mismatch")
        events = openai_events()
        events[1]["response_id"] = "resp_other"
        self.rejects(events, "response_id_mismatch")

    def test_terminal_output_cannot_replace_observed_output(self):
        events = openai_events()
        events[-1]["response"]["output"] = [message("different")]
        self.rejects(events, "terminal_output_mismatch")

    def test_pending_and_index_gap_are_rejected(self):
        events = openai_events()
        events[1]["type"] = "response.output_item.added"
        self.rejects(events, "partial_response")
        events = openai_events()
        events[1]["output_index"] = 1
        self.rejects(events, "partial_response")
        events[1]["output_index"] = True
        self.rejects(events, "invalid_stream_index")

    def test_hidden_reasoning_never_returns_native_output(self):
        for kind in ("reasoning", "thinking", "redacted_thinking"):
            events = openai_events()
            events[1]["item"] = {"type": kind, "id": "hidden", "summary": [{"text": "SYNTHETIC_HIDDEN_SENTINEL"}]}
            self.rejects(events, "unsupported_hidden_reasoning")
        events = openai_events()
        events.insert(1, {"type": "response.reasoning_text.delta", "delta": "SYNTHETIC_HIDDEN_SENTINEL"})
        self.rejects(events, "unsupported_hidden_reasoning")

    def test_unknown_events_or_items_are_not_silently_skipped(self):
        events = openai_events()
        events.insert(1, {"type": "response.future_context", "secret": "SYNTHETIC_SENTINEL"})
        self.rejects(events, "unsupported_sse_event")
        events = openai_events()
        events[1]["item"] = {"type": "custom_tool_call", "id": "custom", "input": "synthetic"}
        self.rejects(events, "unsupported_response_item")

    def test_failed_and_incomplete_are_not_terminal_success(self):
        for kind in ("error", "response.failed", "response.incomplete"):
            self.rejects([openai_events()[0], {"type": kind, "message": "SYNTHETIC_PRIVATE_ERROR"}], "non_successful_response")
        value = native()
        value["status"] = "incomplete"
        with self.assertRaisesRegex(tr.TransportError, "partial_response"):
            tr.decode_response("openai-responses", encoded(value), "application/json")

    def test_duplicate_keys_nonfinite_unicode_and_depth_rejected(self):
        cases = [(b'{"id":"one","id":"two"}', "duplicate_json_key"),
                 (b'{"number":1e999}', "invalid_json_number"),
                 (b'{"number":NaN}', "invalid_json_number"),
                 (b'{"key":"\\ud800"}', "invalid_json"),
                 (b'{"\\ud800":1}', "invalid_json"),
                 (b'[' * 66 + b'0' + b']' * 66, "json_depth_limit")]
        for body, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(tr.TransportError, "^" + code + "$"):
                tr.decode_response("openai-responses", body, "application/json")

    def test_size_event_count_and_content_type_bounds(self):
        with mock.patch.object(tr, "MAX_BODY_BYTES", 3):
            with self.assertRaisesRegex(tr.TransportError, "response_size_limit"):
                tr.decode_response("openai-responses", b"1234", "application/json")
        with mock.patch.object(tr, "MAX_EVENTS", 2):
            self.rejects(openai_events(), "sse_event_limit")
        for media in ("text/plain", "application/json; charset=utf-16", None):
            with self.subTest(media=media), self.assertRaisesRegex(tr.TransportError, "unsupported_content_type"):
                tr.decode_response("openai-responses", b"{}", media)

    def test_sse_sequence_and_event_type_must_be_consistent(self):
        events = openai_events()
        for event, seq in zip(events, (1, 3, 2)):
            event["sequence_number"] = seq
        self.rejects(events, "invalid_stream_sequence")
        with self.assertRaisesRegex(tr.TransportError, "sse_event_type_mismatch"):
            tr.decode_response("openai-responses", b'event: different\ndata: {"type":"response.created"}\n\n', "text/event-stream")

    def test_anthropic_text_stream_and_usage_merge(self):
        result, label = tr.decode_response("anthropic-messages", sse(anthropic_events()), "text/event-stream")
        self.assertEqual(result["content"], [{"type": "text", "text": '{"answer":4}'}])
        self.assertEqual(result["usage"], {"input_tokens": 1, "output_tokens": 4})
        self.assertEqual(label, "sse")
        self.assertEqual(tr.decode_response("anthropic-messages", encoded(result), "application/json")[0], result)

    def test_anthropic_tool_use_fragments_strict_json(self):
        events = anthropic_events()
        events[1]["content_block"] = {"type": "tool_use", "id": "tool_fixture", "name": "lookup", "input": {}}
        events[2]["delta"] = {"type": "input_json_delta", "partial_json": '{"key":"beta"}'}
        events[-2]["delta"]["stop_reason"] = "tool_use"
        result, _ = tr.decode_response("anthropic-messages", sse(events), "text/event-stream")
        self.assertEqual(result["content"][0]["input"], {"key": "beta"})
        events[2]["delta"]["partial_json"] = '{"key":1,"key":2}'
        self.rejects(events, "duplicate_json_key", "anthropic-messages")

    def test_anthropic_missing_stop_duplicate_terminal_and_identity(self):
        self.rejects(anthropic_events()[:-1], "terminal_missing", "anthropic-messages")
        events = anthropic_events()
        del events[3]
        self.rejects(events, "partial_response", "anthropic-messages")
        self.rejects(anthropic_events() + [{"type": "message_stop"}], "duplicate_terminal", "anthropic-messages")
        events = anthropic_events()
        events[-1]["message_id"] = "different"
        self.rejects(events, "response_id_mismatch", "anthropic-messages")


class HttpTests(unittest.TestCase):
    def setup_connection(self, status=200, headers=None, chunks=None):
        response = mock.Mock(status=status)
        values = {"Content-Type": "application/json", **(headers or {})}
        response.getheader.side_effect = lambda key, default=None: values.get(key, default)
        response.read1.side_effect = list(chunks or [b"{}", b""])
        connection = mock.Mock()
        connection.getresponse.return_value = response
        return connection

    def test_exact_bytes_official_tls_and_proxy_env_ignored(self):
        connection = self.setup_connection()
        body = b'{ "model": "synthetic", "input": [] }\n'
        with mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection) as factory, \
             mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid", "ALL_PROXY": "http://proxy.invalid"}):
            result = tr.post_json("https://api.openai.com/v1/responses", body, headers={"Authorization": "Bearer synthetic-only"})
        self.assertEqual(result, {"body": b"{}", "status": 200, "content_type": "application/json"})
        self.assertEqual(factory.call_args.args, ("api.openai.com", 443))
        self.assertIsNotNone(factory.call_args.kwargs["context"])
        self.assertEqual(connection.request.call_args.args, ("POST", "/v1/responses"))
        self.assertEqual(connection.request.call_args.kwargs["body"], body)
        connection.close.assert_called_once()

    def test_destination_allowlist_rejects_before_connection(self):
        values = ["http://api.openai.com/v1/responses", "https://api.openai.com.evil/v1/responses", "https://other.invalid/v1/responses",
                  "https://user:pass@api.openai.com/v1/responses", "https://api.openai.com/v1/responses?", "https://api.openai.com/v1/responses#",
                  "https://api.openai.com:444/v1/responses", "https://api.openai.com/v1/../responses", "http://127.0.0.1:8080/v1/responses",
                  "https://api.openai.com/v1/messages"]
        with mock.patch.object(tr.http.client, "HTTPSConnection") as factory:
            for endpoint in values:
                with self.subTest(endpoint=endpoint), self.assertRaisesRegex(tr.TransportError, "endpoint_not_allowed"):
                    tr.post_json(endpoint, b"{}")
        factory.assert_not_called()

    def test_loopback_requires_explicit_flag_exact_ip_and_no_credentials(self):
        connection = self.setup_connection()
        with mock.patch.object(tr.http.client, "HTTPConnection", return_value=connection) as factory:
            tr.post_json("http://127.0.0.1:8080/v1/responses", b"{}", allow_loopback=True)
        self.assertEqual(factory.call_args.args, ("127.0.0.1", 8080))
        for endpoint in ("http://localhost:8080/v1/responses", "http://127.0.0.2:8080/v1/responses", "http://127.0.0.1:8080/other"):
            with self.assertRaisesRegex(tr.TransportError, "endpoint_not_allowed"):
                tr.post_json(endpoint, b"{}", allow_loopback=True)
        with self.assertRaisesRegex(tr.TransportError, "loopback_credentials_not_allowed"):
            tr.post_json("http://127.0.0.1:8080/v1/responses", b"{}", allow_loopback=True, headers={"x-api-key": "synthetic"})

    def test_headers_disallow_injection_cookie_host_and_ambiguous_case(self):
        for headers in ({"Cookie": "synthetic"}, {"Host": "other.invalid"}, {"Authorization": "Bearer a\nb"},
                        {"Accept": "*/*"}, {"Content-Type": "text/plain"}, {"x-api-key": "a", "X-API-KEY": "b"},
                        {"anthropic-version": "synthetic"}):
            with self.subTest(keys=list(headers)), self.assertRaisesRegex(tr.TransportError, "headers_not_allowed"):
                tr.post_json("https://api.anthropic.com/v1/messages", b"{}", headers=headers)

    def test_redirect_compression_and_declared_oversize_rejected(self):
        cases = [(302, {}, "redirect_not_allowed"), (200, {"Content-Encoding": "gzip"}, "unsupported_content_encoding"),
                 (200, {"Content-Length": str(tr.MAX_BODY_BYTES + 1)}, "response_size_limit")]
        for status, headers, code in cases:
            connection = self.setup_connection(status, headers)
            with self.subTest(code=code), mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), \
                 self.assertRaisesRegex(tr.TransportError, code):
                tr.post_json("https://api.openai.com/v1/responses", b"{}")
            connection.close.assert_called_once()

    def test_actual_oversize_partial_and_http_error_static(self):
        cases = [({}, [b"12345", b""], "response_size_limit"), ({"Content-Length": "3"}, [b"12", b""], "partial_http_body")]
        for headers, chunks, code in cases:
            connection = self.setup_connection(headers=headers, chunks=chunks)
            with mock.patch.object(tr, "MAX_BODY_BYTES", 4), mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), \
                 self.assertRaisesRegex(tr.TransportError, code):
                tr.post_json("https://api.openai.com/v1/responses", b"{}")
        connection = self.setup_connection()
        connection.request.side_effect = OSError("SYNTHETIC_PRIVATE_HOST_AND_TOKEN")
        with mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), self.assertRaisesRegex(tr.TransportError, "^transport_request_failed$"):
            tr.post_json("https://api.openai.com/v1/responses", b"{}")

    def test_request_validation_precedes_network(self):
        with mock.patch.object(tr.http.client, "HTTPSConnection") as factory:
            for body in (b"[]", b'{"x":1,"x":2}', b"{bad", b"\xff"):
                with self.assertRaises(tr.TransportError):
                    tr.post_json("https://api.openai.com/v1/responses", body)
        factory.assert_not_called()

    def test_setup_exhausting_total_deadline_closes_without_starting_watchdog(self):
        connection = self.setup_connection()
        now = [100.0]

        def slow_tls_setup():
            now[0] += 2
            return mock.sentinel.tls_context

        # CA loading can consume the deadline before a watchdog exists. This is
        # a valid timeout path, and must not depend on the host's CA load speed.
        with mock.patch.object(tr, "TOTAL_TIMEOUT", 1), \
             mock.patch.object(tr.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(tr.ssl, "create_default_context", side_effect=slow_tls_setup), \
             mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), \
             mock.patch.object(tr.threading, "Timer") as timer, \
             self.assertRaisesRegex(tr.TransportError, "^transport_timeout$"):
            tr.post_json("https://api.openai.com/v1/responses", b"{}")
        timer.assert_not_called()
        connection.getresponse.assert_not_called()
        connection.sock.shutdown.assert_not_called()
        connection.close.assert_called_once()

    def test_total_read_deadline_cannot_be_extended_by_slow_response(self):
        connection = self.setup_connection()
        now = [100.0]

        def slow_chunk(_limit):
            now[0] += 1
            return b"x"

        connection.getresponse.return_value.read1.side_effect = slow_chunk
        # No real timer thread: each chunk advances a controlled clock while
        # arriving sooner than the idle timeout. The absolute deadline wins.
        with mock.patch.object(tr, "TOTAL_TIMEOUT", 3), mock.patch.object(tr, "IO_TIMEOUT", 10), \
             mock.patch.object(tr.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(tr.ssl, "create_default_context", return_value=mock.sentinel.tls_context), \
             mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), \
             mock.patch.object(tr.threading, "Timer") as timer, \
             self.assertRaisesRegex(tr.TransportError, "^transport_timeout$"):
            tr.post_json("https://api.openai.com/v1/responses", b"{}")
        timer.assert_called_once()
        self.assertEqual(timer.call_args.args[0], 3)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        self.assertEqual(connection.getresponse.return_value.read1.call_count, 3)
        self.assertEqual(connection.sock.settimeout.call_args_list, [mock.call(3), mock.call(2), mock.call(1)])
        connection.sock.shutdown.assert_not_called()
        connection.close.assert_called_once()

    def test_watchdog_interrupts_socket_and_returns_static_timeout(self):
        connection = self.setup_connection()
        with mock.patch.object(tr, "TOTAL_TIMEOUT", 3), \
             mock.patch.object(tr.time, "monotonic", return_value=100.0), \
             mock.patch.object(tr.ssl, "create_default_context", return_value=mock.sentinel.tls_context), \
             mock.patch.object(tr.http.client, "HTTPSConnection", return_value=connection), \
             mock.patch.object(tr.threading, "Timer") as timer:
            def interrupted_read(_limit):
                timer.call_args.args[1]()  # Fire the real watchdog callback during a read.
                raise OSError("SYNTHETIC_PRIVATE_SOCKET_ERROR")

            connection.getresponse.return_value.read1.side_effect = interrupted_read
            with self.assertRaisesRegex(tr.TransportError, "^transport_timeout$"):
                tr.post_json("https://api.openai.com/v1/responses", b"{}")
        timer.assert_called_once()
        self.assertEqual(timer.call_args.args[0], 3)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        connection.sock.shutdown.assert_called_once_with(tr.socket.SHUT_RDWR)
        connection.close.assert_called_once()


class RequestFixtureTests(unittest.TestCase):
    def request(self):
        return encoded({"model": smoke.MODEL, "input": [{"role": "user", "content": [{"type": "input_text", "text": smoke.PROMPT}]}],
                        "stream": True, "tools": []})

    def exchange(self, state, raw=None, path="/v1/responses", extra="", method="POST", fail_write=False):
        raw = self.request() if raw is None else raw
        class Socket:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *_args):
                headers = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n{extra}\r\n".encode()
                return io.BytesIO(headers + raw)
            def settimeout(self, *_args):
                pass
            def sendall(self, data):
                if fail_write and data == state.body:
                    raise OSError("SYNTHETIC_PRIVATE_FAILURE")
                self.output.extend(data)
        sock = Socket()
        smoke.handler_class(state)(sock, ("127.0.0.1", 1), object())
        return bytes(sock.output)

    def test_plan_does_not_start_process_server_or_write(self):
        with mock.patch.object(smoke, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(smoke.main([]), 0)
        run.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["status"], "plan_only")

    def test_config_uses_unauthenticated_local_provider_without_home_override(self):
        args = smoke.command(Path('/synthetic/a.b/"c"'), 12345)
        values = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
        parsed = tomllib.loads("\n".join(values))
        self.assertEqual(parsed["model_provider"], "nous_fixture")
        provider = parsed["model_providers"]["nous_fixture"]
        self.assertFalse(provider["requires_openai_auth"])
        self.assertFalse(provider["supports_websockets"])
        self.assertEqual(provider["base_url"], "http://127.0.0.1:12345/v1")
        self.assertEqual(provider["request_max_retries"], 0)
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, args)
        for flag in ("hooks", "apps", "plugins", "shell_tool", "memories", "multi_agent", "enable_request_compression"):
            self.assertFalse(parsed["features"][flag])
        self.assertEqual(parsed["mcp_servers"], {})
        with mock.patch.dict("os.environ", {"HOME": "/synthetic/home", "CODEX_HOME": "/synthetic/codex", "OPENAI_API_KEY": "secret", "HTTPS_PROXY": "proxy"}):
            env = smoke.child_environment()
        self.assertEqual(env["HOME"], "/synthetic/home")
        self.assertEqual(env["CODEX_HOME"], "/synthetic/codex")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_fixture_native_fields_are_actually_on_wire(self):
        result, kind = tr.decode_response("openai-responses", smoke.response_bytes(), smoke.CONTENT_TYPE)
        self.assertEqual(kind, "sse")
        self.assertEqual(result["model"], smoke.MODEL)
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["status"], "completed")
        self.assertEqual(result["output"][0]["content"][0]["text"], smoke.FINAL)
        self.assertEqual(smoke.response_bytes().count(b"event:"), 3)

    def test_shape_report_never_echoes_unknown_keys_values_or_tool_names(self):
        request = json.loads(self.request())
        request["SYNTHETIC_UNKNOWN_FIELD"] = "SYNTHETIC_BODY_SECRET"
        request["tools"] = [{"type": "SYNTHETIC_UNKNOWN_TOOL", "name": "SYNTHETIC_NAME"}]
        shape = smoke.safe_request_shape(encoded(request))
        safe = json.dumps(shape)
        self.assertNotIn("SYNTHETIC_", safe)
        self.assertEqual(shape["unknown_field_count"], 1)
        self.assertEqual(shape["tool_type_counts"], {"unknown": 1})
        self.assertTrue(shape["fixture_prompt_present"])

    def test_shape_reports_only_known_native_settings_and_closed_value_buckets(self):
        request = json.loads(self.request())
        request.update(client_metadata={"PRIVATE_METADATA_KEY": "PRIVATE_METADATA_VALUE"},
            include=["reasoning.encrypted_content", "PRIVATE_SELECTOR"],
            reasoning={"effort": "none", "summary": "PRIVATE_SUMMARY", "PRIVATE_FIELD": "PRIVATE_BODY"})
        shape = smoke.safe_request_shape(encoded(request))
        self.assertTrue(shape["fields_present"]["client_metadata"])
        self.assertEqual(shape["unknown_field_count"], 0)
        self.assertEqual(shape["client_metadata_shape"], {"is_object": True, "entries": 1, "string_map": True,
                                                         "max_key_characters": 20, "max_value_characters": 22})
        self.assertEqual(shape["include_shape"]["unknown_items"], 1)
        self.assertTrue(shape["include_shape"]["known_selectors_present"]["reasoning.encrypted_content"])
        self.assertEqual(shape["reasoning_shape"]["effort"], "none")
        self.assertEqual(shape["reasoning_shape"]["summary"], "unknown")
        self.assertEqual(shape["reasoning_shape"]["unknown_fields"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(shape))

    def test_request_errors_are_typed_and_allowlisted_without_exception_leakage(self):
        bridge = mock.Mock()
        bridge.begin.side_effect = smoke.RequestError("unsupported_request_include")
        state = smoke.FixtureState(bridge)
        self.assertTrue(self.exchange(state).endswith(state.body))
        self.assertEqual(state.report["request_capture_code"], "unsupported_request_include")
        for error in (ValueError("unsupported_request_include"), smoke.RequestError("PRIVATE_ERROR")):
            self.assertEqual(smoke.safe_capture_error(error), "capture_error")


    def test_begin_precedes_wire_and_finish_observes_identical_bytes(self):
        observed = []
        bridge = mock.Mock()
        bridge.begin.side_effect = lambda raw: observed.append(("begin", raw)) or "ticket"
        bridge.finish.side_effect = lambda ticket, raw: observed.append(("finish", ticket, raw)) or {"collected": True}
        state = smoke.FixtureState(bridge)
        wire = self.exchange(state)
        self.assertTrue(wire.endswith(state.body))
        self.assertEqual(observed, [("begin", self.request()), ("finish", "ticket", state.body)])
        self.assertTrue(state.report["capture_started_before_response"])
        self.assertTrue(state.report["response_sent"])

    def test_unsupported_request_still_reaches_client_but_has_no_capture(self):
        bridge = mock.Mock()
        bridge.begin.side_effect = ValueError("SYNTHETIC_PRIVATE_ERROR")
        state = smoke.FixtureState(bridge)
        self.assertTrue(self.exchange(state).endswith(state.body))
        self.assertFalse(state.report["capture_started_before_response"])
        self.assertEqual(state.report["diagnostics"], ["request_capture_unsupported"])
        bridge.finish.assert_not_called()
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(state.report))

    def test_failed_send_cannot_finalize_observation(self):
        bridge = mock.Mock()
        state = smoke.FixtureState(bridge)
        self.exchange(state, fail_write=True)
        bridge.begin.assert_called_once()
        bridge.finish.assert_not_called()
        self.assertFalse(state.report["response_sent"])

    def test_single_exact_path_no_auth_or_compression(self):
        for values, expected in (({"path": "/v1/responses?test"}, "unexpected_request_path"),
                                 ({"extra": "Authorization: Bearer synthetic\r\n"}, "unexpected_auth_header"),
                                 ({"extra": "Content-Encoding: gzip\r\n"}, "unsupported_request_encoding"),
                                 ({"extra": "Content-Length: 1\r\n"}, "request_length_rejected"),
                                 ({"method": "GET"}, "unexpected_http_method")):
            bridge = mock.Mock()
            state = smoke.FixtureState(bridge)
            self.exchange(state, **values)
            self.assertIn(expected, state.report["diagnostics"])
            bridge.begin.assert_not_called()
        state = smoke.FixtureState(mock.Mock())
        self.exchange(state)
        self.exchange(state)
        self.assertEqual(state.report["requests"], 2)
        self.assertIn("more_than_one_request", state.report["diagnostics"])

    def test_no_fixture_match_does_not_register_oracle(self):
        value = json.loads(self.request())
        value["input"][0]["content"][0]["text"] = "not the synthetic task"
        state = smoke.FixtureState(mock.Mock())
        self.exchange(state, raw=encoded(value))
        self.assertIn("fixture_request_shape_rejected", state.report["diagnostics"])
        state.bridge.begin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
