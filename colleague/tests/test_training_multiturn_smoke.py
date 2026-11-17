"""Synthetic HTTP/MCP frames only: no platform process, socket or model call."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "hooks"))
import training_multiturn_smoke as smoke
from nous_training_transport import decode_response


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def request(agent, step=0, namespace=False):
    name = "mcp__nous_fixture__lookup"
    catalog = ({"name": name, "description": smoke.TOOL["description"], "input_schema": copy.deepcopy(smoke.TOOL["inputSchema"])}
               if agent == "claude" else {"type": "function", "name": name, "description": smoke.TOOL["description"], "parameters": copy.deepcopy(smoke.TOOL["inputSchema"])})
    tools = [{"type": "namespace", "name": "fixture", "tools": [catalog]}] if namespace else [catalog]
    contract = {"name": name, "namespace": "fixture" if namespace else None}
    items = [{"role": "user", "content": [{"type": "text" if agent == "claude" else "input_text", "text": smoke.PROMPT}]}]
    for index in range(step):
        native, _ = decode_response("anthropic-messages" if agent == "claude" else "openai-responses", smoke.response_bytes(agent, index, contract), "text/event-stream")
        payload = smoke.FAILURE if index == 0 else smoke.SUCCESS
        if agent == "claude":
            items += [{"role": "assistant", "content": native["content"]},
                      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_fixture_" + str(index + 1),
                                                     "is_error": index == 0, "content": [{"type": "text", "text": json.dumps(payload)}]}]}]
        else:
            items += native["output"] + [{"type": "function_call_output", "call_id": "call_fixture_" + str(index + 1),
                                           "output": [{"type": "input_text", "text": json.dumps(payload)}]}]
    value = {"model": smoke.CLAUDE_MODEL if agent == "claude" else smoke.MODEL, "stream": True, "tools": tools,
             "messages" if agent == "claude" else "input": items}
    if agent == "claude":
        value["max_tokens"] = 128
    return value


class MultiturnFixtureTests(unittest.TestCase):
    def exchange(self, state, value, *, path=None, key=None, extra="", fail_send=False):
        raw = encoded(value)
        route = path if path is not None else "/v1/messages" if state.agent == "claude" else "/v1/responses"
        auth = ("x-api-key: " + (smoke.DUMMY_KEY if key is None else key) + "\r\n") if state.agent == "claude" or key is not None else ""
        class Socket:
            def __init__(self):
                self.written = bytearray()
            def makefile(self, *_args):
                head = f"POST {route} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n{auth}{extra}\r\n".encode()
                return io.BytesIO(head + raw)
            def settimeout(self, *_args):
                pass
            def shutdown(self, *_args):
                pass
            def sendall(self, data):
                if fail_send and bytes(data).startswith(b"event:"):
                    raise OSError("SYNTHETIC_PRIVATE_SEND_ERROR")
                self.written.extend(data)
        sock = Socket()
        smoke.handler_class(state)(sock, ("127.0.0.1", 1), object())
        return bytes(sock.written)

    def test_default_plan_never_starts_cli_or_socket(self):
        with mock.patch.object(smoke, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(smoke.main(["--agent", "claude"]), 0)
        run.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual((result["status"], result["max_requests"], result["max_mcp_calls"]), ("plan_only", 3, 2))
        self.assertFalse(result["real_model"])

    def test_codex_config_is_process_scoped_and_one_mcp(self):
        root = Path('/synthetic/with.dots/"quoted"')
        args = smoke.command("codex", root, 12345, "/synthetic/codex")
        values = [args[index + 1] for index, arg in enumerate(args) if arg == "-c"]
        config = tomllib.loads("\n".join(values))
        self.assertEqual(set(config["mcp_servers"]), {smoke.SERVER})
        self.assertEqual(config["mcp_servers"][smoke.SERVER]["enabled_tools"], ["lookup"])
        self.assertEqual(config["projects"], {str(root / "project"): {"trust_level": "trusted"}})
        self.assertFalse(config["features"]["hooks"])
        self.assertFalse(config["model_providers"]["nous_fixture"]["requires_openai_auth"])
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertNotIn("--trust", args)
        self.assertEqual(args[-1], smoke.PROMPT)

    def test_claude_configuration_uses_bare_no_persistence_and_only_fixture_tools(self):
        args = smoke.command("claude", Path("/synthetic"), 12345, "/synthetic/claude")
        for flag in ("--bare", "--restricted", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__nous_fixture__lookup")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "0.75")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_child_environment_preserves_home_but_drops_all_real_credentials(self):
        source = {"HOME": "/synthetic/home", "CODEX_HOME": "/synthetic/codex", "PATH": "/bin", "ANTHROPIC_API_KEY": "SYNTHETIC_REAL_KEY",
                  "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_TOKEN", "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS", "CLAUDE_CODE_USE_BEDROCK": "1",
                  "HTTP_PROXY": "http://synthetic", "CLAUDE_CODE_EXTRA_BODY": "private", "OPENAI_API_KEY": "SYNTHETIC_OPENAI"}
        with mock.patch.dict("os.environ", source, clear=True):
            env = smoke.child_environment("claude", Path("/synthetic"), 12345)
        self.assertEqual(env["HOME"], source["HOME"])
        self.assertEqual(env["CODEX_HOME"], source["CODEX_HOME"])
        self.assertEqual(env["ANTHROPIC_API_KEY"], smoke.DUMMY_KEY)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:12345")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/synthetic/private/claude-config")
        self.assertNotIn("SYNTHETIC_", json.dumps(env))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_actual_mcp_discovery_and_error_recovery_are_executed(self):
        values = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lookup", "arguments": {"key": "beta"}}},
                  {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lookup", "arguments": {"key": "beta"}}}]
        source, output, saved = io.BytesIO(b"\n".join(encoded(value) for value in values) + b"\n"), io.StringIO(), {}
        self.assertEqual(smoke.serve_mcp(source, output, lambda name, value: saved.update({name: copy.deepcopy(value)})), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[1]["result"], saved["tools-list.json"])
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertFalse(responses[3]["result"]["isError"])
        self.assertEqual(json.loads(responses[3]["result"]["content"][0]["text"]), smoke.SUCCESS)
        self.assertEqual(saved["mcp-audit.json"]["calls"], 2)
        self.assertEqual(saved["mcp-audit.json"]["transient_errors"], 1)

    def test_mcp_never_logs_unexpected_input_or_identifier(self):
        value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "SYNTHETIC_PRIVATE_TOOL", "arguments": {"secret": "SYNTHETIC_PRIVATE_VALUE"}}}
        saved, output = {}, io.StringIO()
        smoke.serve_mcp(io.BytesIO(encoded(value) + b"\n"), output, lambda name, value: saved.update({name: copy.deepcopy(value)}))
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(saved))
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())
        self.assertEqual(saved["mcp-audit.json"]["unexpected_calls"], 1)

    def test_mcp_rejects_oversize_and_unknown_protocol(self):
        for value in (b"x" * (smoke.MAX_MCP_LINE + 1), encoded({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "unknown"}})):
            saved = {}
            self.assertEqual(smoke.serve_mcp(io.BytesIO(value + b"\n"), io.StringIO(), lambda name, data: saved.update({name: copy.deepcopy(data)})), 2)
            self.assertEqual(saved["mcp-audit.json"]["invalid_requests"], 1)

    def test_all_three_scripted_responses_are_complete_native_streams(self):
        for agent in ("codex", "claude"):
            contract = smoke.fixture_contract(request(agent), agent)
            for step in range(3):
                result, label = decode_response("anthropic-messages" if agent == "claude" else "openai-responses",
                                                smoke.response_bytes(agent, step, contract), "text/event-stream")
                self.assertEqual(label, "sse")
                content = result["content"] if agent == "claude" else result["output"]
                self.assertEqual(content[0]["type"], ("tool_use" if agent == "claude" else "function_call") if step < 2 else ("text" if agent == "claude" else "message"))

    def test_namespace_is_native_field_not_flattened_tool_name(self):
        contract = smoke.fixture_contract(request("codex", namespace=True), "codex")
        result, _ = decode_response("openai-responses", smoke.response_bytes("codex", 0, contract), "text/event-stream")
        self.assertEqual(result["output"][0]["namespace"], "fixture")
        self.assertEqual(result["output"][0]["name"], contract["name"])
        self.assertNotIn("fixture.", result["output"][0]["name"])

    def test_histories_require_real_call_result_replay_for_both_providers(self):
        for agent in ("codex", "claude"):
            for step in range(3):
                value = request(agent, step)
                report = smoke.inspect_history(value, agent, smoke.fixture_contract(value, agent), step)
                self.assertEqual(report["native_calls"], step)
                self.assertEqual(report["native_results"], step)
                self.assertEqual(report["success_replayed"], step == 2)

    def test_missing_result_wrong_call_or_unobserved_output_cannot_advance(self):
        for agent in ("codex", "claude"):
            value = request(agent, 1)
            key = "messages" if agent == "claude" else "input"
            value[key].pop()
            with self.assertRaises(ValueError):
                smoke.inspect_history(value, agent, smoke.fixture_contract(value, agent), 1)
            value = request(agent, 1)
            if agent == "claude":
                value[key][-1]["content"][0]["tool_use_id"] = "different"
            else:
                value[key][-1]["call_id"] = "different"
            with self.assertRaises(ValueError):
                smoke.inspect_history(value, agent, smoke.fixture_contract(value, agent), 1)
            value = request(agent, 1)
            target = value[key][-1]["content"][0] if agent == "claude" else value[key][-1]
            target["content" if agent == "claude" else "output"] = json.dumps(smoke.SUCCESS)
            with self.assertRaises(ValueError):
                smoke.inspect_history(value, agent, smoke.fixture_contract(value, agent), 1)

    def test_anthropic_error_flag_and_codex_namespace_preserved(self):
        value = request("claude", 1)
        value["messages"][-1]["content"][0]["is_error"] = False
        with self.assertRaises(ValueError):
            smoke.inspect_history(value, "claude", smoke.fixture_contract(value, "claude"), 1)
        value = request("codex", 1, namespace=True)
        del value["input"][1]["namespace"]
        with self.assertRaises(ValueError):
            smoke.inspect_history(value, "codex", smoke.fixture_contract(value, "codex"), 1)

    def test_unexpected_catalog_or_changed_schema_fails_closed(self):
        for agent in ("codex", "claude"):
            value = request(agent)
            value["tools"].append(copy.deepcopy(value["tools"][0]))
            with self.assertRaises(ValueError):
                smoke.fixture_contract(value, agent)
            value = request(agent)
            value["tools"][0]["input_schema" if agent == "claude" else "parameters"]["properties"]["key"]["enum"] = ["other"]
            with self.assertRaises(ValueError):
                smoke.fixture_contract(value, agent)

    def test_codex_native_function_catalog_is_preserved_but_only_fixture_called(self):
        value = request("codex")
        extras = [{"type": "function", "name": name, "parameters": {"type": "object", "properties": {}}, "description": None}
                  for name in sorted(smoke.RESOURCE_BUILTINS)]
        value["tools"].append({"type": "namespace", "name": "functions", "tools": extras})
        original = copy.deepcopy(value)
        contract = smoke.fixture_contract(value, "codex")
        self.assertEqual(contract["name"], "mcp__nous_fixture__lookup")
        summary = smoke.catalog_summary(value, "codex")
        self.assertEqual(summary["types"]["function"], 4)
        self.assertEqual(summary["fixture_count"], 1)
        self.assertTrue(all(summary["known_builtins"][name] for name in smoke.RESOURCE_BUILTINS))
        self.assertEqual(value, original)
        native, _ = decode_response("openai-responses", smoke.response_bytes("codex", 0, contract), "text/event-stream")
        self.assertNotIn(native["output"][0]["name"], smoke.RESOURCE_BUILTINS)
        for mutate in (lambda item: item.update(type="custom"), lambda item: item.update(parameters=[])):
            changed = copy.deepcopy(value)
            mutate(changed["tools"][-1]["tools"][0])
            with self.assertRaises(ValueError):
                smoke.fixture_contract(changed, "codex")
        changed = copy.deepcopy(value)
        changed["tools"][-1]["tools"].append({"type": "function", "name": "SYNTHETIC_OTHER_NATIVE", "parameters": {"type": "object"}})
        self.assertEqual(smoke.fixture_contract(changed, "codex"), contract)
        self.assertNotIn("SYNTHETIC_OTHER_NATIVE", json.dumps(smoke.catalog_summary(changed, "codex")))

    def test_catalog_diagnostics_never_copy_actual_names_or_descriptions(self):
        value = request("codex")
        value["tools"].append({"type": "SYNTHETIC_UNKNOWN_TYPE", "name": "SYNTHETIC_UNKNOWN_NAME", "description": "SYNTHETIC_PRIVATE_TEXT"})
        summary = smoke.catalog_summary(value, "codex")
        self.assertEqual(summary["types"]["unknown"], 1)
        self.assertNotIn("SYNTHETIC_", json.dumps(summary))

    def test_only_exact_owned_capture_error_classes_and_codes_are_reported(self):
        from nous_training_episode import EpisodeError
        from nous_training_request import RequestError
        from nous_training_observer import ObserverError
        allowed = [(EpisodeError, "episode_request_context_changed"), (RequestError, "unsupported_request_field"),
                   (ObserverError, "episode_handle_consumed")]
        for cls, code in allowed:
            self.assertEqual(smoke.safe_capture_error(cls(code)), code)
            self.assertEqual(smoke.safe_capture_error(cls("SYNTHETIC_PRIVATE_ERROR")), "capture_error")
            self.assertEqual(smoke.safe_capture_error(ValueError(code)), "capture_error")
            class UntrustedSubclass(cls):
                pass
            self.assertEqual(smoke.safe_capture_error(UntrustedSubclass(code)), "capture_error")

    def test_context_differences_are_closed_and_do_not_modify_requests(self):
        before = request("claude", 1)
        before["metadata"] = {"user_id": "SYNTHETIC_USER_ONE", "SYNTHETIC_PRIVATE_KEY": "SYNTHETIC_VALUE"}
        after = copy.deepcopy(before)
        after["metadata"]["user_id"] = "SYNTHETIC_USER_TWO"
        after["metadata"]["SYNTHETIC_PRIVATE_KEY"] = "CHANGED"
        after["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
        saved = copy.deepcopy((before, after))
        result = smoke.context_changes(before, after, "claude")
        self.assertTrue(result["changed_fields"]["metadata"])
        self.assertTrue(result["metadata"]["metadata"]["known_fields_changed"]["user_id"])
        self.assertTrue(result["metadata"]["metadata"]["unknown_values_changed"])
        self.assertFalse(result["history_prefix_equal"])
        self.assertTrue(result["history_prefix_without_cache_equal"])
        self.assertNotIn("SYNTHETIC_", json.dumps(result))
        self.assertEqual((before, after), saved)
        after["messages"][1]["content"][0]["id"] = "changed_semantic_tool_id"
        self.assertFalse(smoke.context_changes(before, after, "claude")["history_prefix_without_transport_metadata_equal"])

    def test_capture_failure_report_preserves_static_stage_and_step(self):
        from nous_training_episode import EpisodeError
        bridge = mock.Mock()
        bridge.finish.side_effect = EpisodeError("episode_history_prefix_changed")
        state = smoke.FixtureState("claude", bridge)
        self.exchange(state, request("claude"))
        self.assertEqual(state.report["capture_errors"], [{"stage": "finish", "step": 0, "code": "episode_history_prefix_changed"}])
        self.assertEqual(state.report["step_observations"], 0)

    def test_string_text_alias_diagnostics_only_emit_types_lengths_and_equalities(self):
        before = request("claude")
        before["messages"][0]["content"] = "SYNTHETIC_PRIVATE_TEXT"
        after = copy.deepcopy(before)
        after["messages"][0]["content"] = [{"type": "text", "text": "SYNTHETIC_PRIVATE_TEXT", "cache_control": {"type": "ephemeral"}}]
        saved = copy.deepcopy((before, after))
        report = smoke.context_changes(before, after, "claude")
        self.assertFalse(report["history_prefix_equal"])
        self.assertFalse(report["history_prefix_without_cache_equal"])
        self.assertTrue(report["history_prefix_text_alias_without_cache_equal"])
        position = report["prefix_positions"][0]
        self.assertEqual(position["before"]["container"], "string")
        self.assertEqual(position["after"]["container"], "list")
        self.assertTrue(position["plain_text_hash_equal"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(report))
        self.assertEqual((before, after), saved)
        after["messages"][0]["content"][0]["text"] = "different"
        report = smoke.context_changes(before, after, "claude")
        self.assertFalse(report["history_prefix_text_alias_without_cache_equal"])
        self.assertFalse(report["prefix_positions"][0]["plain_text_hash_equal"])

    def test_exact_codex_mcp_wall_time_wrapper_matches_error_and_success(self):
        header = "Wall time: 0.0004 seconds\nOutput:"
        for expected in (smoke.FAILURE, smoke.SUCCESS):
            payload = json.dumps(expected)
            items = [{"type": "input_text", "text": header}, {"type": "input_text", "text": payload}]
            self.assertEqual(smoke.payload_envelope(items, expected, "codex"), "codex_wall_time_items")
            self.assertEqual(smoke.payload_envelope(header + "\n" + payload, expected, "codex"), "codex_wall_time_text")
            self.assertIsNone(smoke.payload_envelope(items, expected, "claude"))
        value = request("codex", 2)
        for item in value["input"]:
            if item.get("type") == "function_call_output":
                item["output"].insert(0, {"type": "input_text", "text": header})
        original = copy.deepcopy(value)
        report = smoke.inspect_history(value, "codex", smoke.fixture_contract(value, "codex"), 2)
        self.assertEqual(report["result_envelopes"], ["codex_wall_time_items"] * 2)
        self.assertEqual(value, original)

    def test_wall_time_matcher_rejects_arbitrary_prefixes_trailers_and_wrong_json(self):
        payload = json.dumps(smoke.FAILURE)
        valid = "Wall time: 0.0004 seconds\nOutput:\n" + payload
        for raw in ("private prefix\n" + valid, valid + "\ntrailer", valid.replace("0.0004", "-0.0004"),
                    valid.replace("0.0004", "0.004"), valid.replace("0.0004", "999.0000"),
                    valid.replace("fixture_transient", "other"), "MCP tool error: " + payload):
            self.assertIsNone(smoke.payload_envelope(raw, smoke.FAILURE, "codex"))
        items = [{"type": "input_text", "text": "Wall time: 0.0004 seconds\nOutput:"}, {"type": "input_text", "text": payload}]
        self.assertIsNone(smoke.payload_envelope(items + [{"type": "input_text", "text": "extra"}], smoke.FAILURE, "codex"))
        items[0]["unknown"] = "private"
        self.assertIsNone(smoke.payload_envelope(items, smoke.FAILURE, "codex"))

    def test_native_history_failures_have_distinct_static_codes(self):
        cases = [("call_id", "different", 1, "fixture_call_identity_mismatch"),
                 ("name", "different", 1, "fixture_call_name_mismatch"),
                 ("namespace", "different", 1, "fixture_call_namespace_mismatch"),
                 ("arguments", '{"key":"other"}', 1, "fixture_call_arguments_mismatch"),
                 ("call_id", "different", 2, "fixture_result_identity_mismatch"),
                 ("output", "private wrong output", 2, "fixture_result_content_mismatch")]
        for key, value, index, code in cases:
            raw = request("codex", 1)
            raw["input"][index][key] = value
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "^" + code + "$"):
                smoke.inspect_history(raw, "codex", smoke.fixture_contract(raw, "codex"), 1)

    def test_request_limit_and_partial_history_never_create_next_ticket(self):
        for agent in ("codex", "claude"):
            bridge = mock.Mock()
            state = smoke.FixtureState(agent, bridge)
            for step in range(3):
                ticket, body = state.prepare(encoded(request(agent, step)))
                self.assertIsNotNone(ticket)
                self.assertTrue(body)
            with self.assertRaises(ValueError):
                state.prepare(encoded(request(agent, 2)))
            self.assertEqual(bridge.begin.call_count, 3)
            state = smoke.FixtureState(agent, mock.Mock())
            with self.assertRaises(ValueError):
                state.prepare(encoded(request(agent, 1)))
            state.bridge.begin.assert_not_called()

    def test_unsupported_observation_never_fakes_capture(self):
        state = smoke.FixtureState("codex", mock.Mock())
        state.bridge.begin.side_effect = RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        ticket, body = state.prepare(encoded(request("codex")))
        self.assertIsNone(ticket)
        self.assertTrue(body)
        self.assertEqual(state.report["diagnostics"], ["request_capture_unsupported"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(state.report))

    def test_audit_requires_real_error_and_success_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "audit").mkdir()
            self.assertFalse(smoke.audit_summary(root)["valid"])
            valid = {"initializations": 1, "tools_lists": 1, "calls": 2, "transient_errors": 1, "successes": 1, "unexpected_calls": 0, "invalid_requests": 0}
            smoke.atomic_json(root / "audit/mcp-audit.json", valid)
            self.assertTrue(smoke.audit_summary(root)["valid"])
            valid["unexpected_calls"] = 1
            smoke.atomic_json(root / "audit/mcp-audit.json", valid)
            self.assertFalse(smoke.audit_summary(root)["valid"])
            valid["SYNTHETIC_UNKNOWN_KEY"] = "SYNTHETIC_PRIVATE_VALUE"
            smoke.atomic_json(root / "audit/mcp-audit.json", valid)
            self.assertEqual(smoke.audit_summary(root), {"valid": False})

    def test_http_three_rounds_capture_actual_sent_bytes(self):
        for agent in ("codex", "claude"):
            bridge = mock.Mock()
            bridge.finish.return_value = {"status": "captured"}
            state = smoke.FixtureState(agent, bridge)
            for step in range(3):
                value = request(agent, step)
                output = self.exchange(state, value)
                self.assertTrue(output.endswith(state.bodies[step]))
                self.assertEqual(bridge.begin.call_args.args, (encoded(value),))
                self.assertEqual(bridge.finish.call_args.args[1], state.bodies[step])
            self.assertEqual(state.report["step_observations"], 3)
            self.assertEqual(state.report["step_capture_statuses"], ["captured"] * 3)
            self.assertEqual(state.report["responses_sent"], 3)
            self.assertEqual(state.report["diagnostics"], [])
            self.assertTrue(state.report["histories"][-1]["success_replayed"])

    def test_http_does_not_count_disabled_or_rejected_capture(self):
        for status in ("disabled", "rejected", "unknown"):
            bridge = mock.Mock()
            bridge.finish.return_value = {"status": status}
            state = smoke.FixtureState("codex", bridge)
            output = self.exchange(state, request("codex"))
            self.assertTrue(output.endswith(state.bodies[0]))
            self.assertEqual(state.report["step_observations"], 0)
            self.assertIn("response_capture_rejected", state.report["diagnostics"])

    def test_failed_send_never_finalizes_step(self):
        bridge = mock.Mock()
        state = smoke.FixtureState("codex", bridge)
        self.exchange(state, request("codex"), fail_send=True)
        bridge.begin.assert_called_once()
        bridge.finish.assert_not_called()
        self.assertEqual(state.report["responses_sent"], 0)
        self.assertIn("fixture_handler_failed", state.report["diagnostics"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(state.report))

    def test_http_rejects_real_key_query_extra_request_and_wrong_history(self):
        for options in ({"key": "SYNTHETIC_REAL_KEY"}, {"path": "/v1/messages?arbitrary=true"},
                        {"extra": "Content-Encoding: gzip\r\n"}, {"extra": "Authorization: Bearer SYNTHETIC_SECRET\r\n"}):
            state = smoke.FixtureState("claude", mock.Mock())
            self.exchange(state, request("claude"), **options)
            state.bridge.begin.assert_not_called()
            self.assertEqual(state.report["responses_sent"], 0)
            self.assertNotIn("SYNTHETIC_", json.dumps(state.report))
        state = smoke.FixtureState("codex", mock.Mock())
        state.bridge.finish.return_value = {"status": "captured"}
        for step in range(3):
            self.exchange(state, request("codex", step))
        self.exchange(state, request("codex", 2))
        self.assertIn("more_than_three_requests", state.report["diagnostics"])
        self.assertEqual(state.bridge.begin.call_count, 3)
        state = smoke.FixtureState("codex", mock.Mock())
        self.exchange(state, request("codex", 1))
        self.assertIn("fixture_history_count_mismatch", state.report["diagnostics"])
        state.bridge.begin.assert_not_called()

    def test_claude_known_beta_route_still_requires_exact_dummy_key(self):
        state = smoke.FixtureState("claude", mock.Mock())
        state.bridge.finish.return_value = {"status": "captured"}
        output = self.exchange(state, request("claude"), path="/v1/messages?beta=true")
        self.assertTrue(output.endswith(state.bodies[0]))
        self.assertEqual(state.report["diagnostics"], [])

    def test_observer_episode_integration_with_synthetic_http_bytes(self):
        import nous_ledger
        import nous_training as training
        from nous_training_validate import validate_dataset
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / "project"
                project.mkdir()
                with mock.patch.object(training, "HOME", root / "home"), \
                     mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-training-key-32-bytes!!"):
                    bridge = smoke.EpisodeBridge(agent, project)
                    state = smoke.FixtureState(agent, bridge)
                    for step in range(3):
                        self.exchange(state, request(agent, step))
                    self.assertEqual(state.report["diagnostics"], [])
                    self.assertEqual(state.report["step_observations"], 3)
                    report = bridge.complete()
                    self.assertEqual(report["status"], "captured")
                    self.assertEqual(report["steps"], 3)
                    self.assertTrue(report["quality"]["complete"])
                    training.export(root / "dataset", profile="request-episode-sft-v1")
                    validation = validate_dataset(root / "dataset")
                    self.assertTrue(validation["valid"], validation)
                    self.assertEqual(validation["counters"]["trajectories"], 1)
                    self.assertEqual(validation["counters"]["sft"], 0)


if __name__ == "__main__":
    unittest.main()
