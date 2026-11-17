"""Synthetic API exchanges only; no network, credentials, user history or tools."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_training_request as request


def openai_request():
    return {"model": "synthetic-model", "input": [{"role": "user", "content": "Return exactly {\"answer\":4}."}]}


def openai_response():
    return {"id": "synthetic-response", "object": "response", "model": "synthetic-model", "status": "completed",
        "error": None, "incomplete_details": None,
        "output": [{"id": "synthetic-message", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": '{"answer":4}', "annotations": []}]}],
        "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}


def anthropic_request():
    return {"model": "synthetic-model", "max_tokens": 100,
            "messages": [{"role": "user", "content": "Return exactly {\"answer\":4}."}]}


def anthropic_response():
    return {"id": "synthetic-response", "type": "message", "role": "assistant", "model": "synthetic-model",
            "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": '{"answer":4}'}],
            "usage": {"input_tokens": 12, "output_tokens": 5}}


def exchange(provider="openai-responses"):
    build_request, build_response = ((openai_request, openai_response) if provider == "openai-responses"
                                     else (anthropic_request, anthropic_response))
    return request.normalize_exchange(provider, build_request(), build_response())


def record(provider="openai-responses"):
    value = exchange(provider)
    return {"schema": "nous.trajectory/3", "exchange": value, "messages": request.request_messages(value),
            "quality": {"complete": True, "issues": [], "missing": []},
            "privacy": {"scrubbed": True, "scrubber_version": "synthetic"},
            "provenance": {"request_content_sha256": hashlib.sha256(request._json(value["request"]).encode()).hexdigest()}}


def function_tool():
    return {"type": "function", "name": "lookup", "description": "Synthetic catalog only; never executed.",
            "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                           "required": ["key"], "additionalProperties": False}, "strict": True}


def tool_exchange(provider="openai-responses", *, parallel=False):
    value = exchange(provider); value["schema"] = request.TOOL_SCHEMA
    tool = function_tool()
    ids = ["call-1", "call-2"] if parallel else ["call-1"]
    if provider == "openai-responses":
        value["request"]["tools"] = [tool]
        value["response"]["output"] = [{"type": "function_call", "id": "item-" + ident,
            "call_id": ident, "name": "lookup", "arguments": '{"key":"x"}', "status": "completed"} for ident in ids]
    else:
        value["request"]["tools"] = [{"name": "lookup", "input_schema": tool["parameters"]}]
        value["response"].update(stop_reason="tool_use", content=[
            {"type": "tool_use", "id": ident, "name": "lookup", "input": {"key": "x"}} for ident in ids])
    return value


def closed_tool_exchange(provider="openai-responses", *, parallel=False, error=False):
    value = tool_exchange(provider, parallel=parallel)
    ids = ["call-2", "call-1"] if parallel else ["call-1"]
    if provider == "openai-responses":
        value["request"]["input"] += copy.deepcopy(value["response"]["output"])
        value["request"]["input"] += [{"type": "function_call_output", "call_id": ident,
            "output": '{"error":"synthetic_transient"}' if error else "synthetic result"} for ident in ids]
        value["response"] = openai_response()
    else:
        value["request"]["messages"] += [
            {"role": "assistant", "content": copy.deepcopy(value["response"]["content"])},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident,
                "content": "synthetic failure" if error else "synthetic result", "is_error": error} for ident in ids]}]
        value["response"] = anthropic_response()
    return value


class RequestExchange(unittest.TestCase):
    def test_exchange2_pending_tools_are_complete_observations_not_final_answers(self):
        for provider in request.PROVIDERS:
            value = tool_exchange(provider, parallel=True)
            original = copy.deepcopy(value)
            request.validate_request(provider, value["request"], schema=request.TOOL_SCHEMA)
            request.validate_exchange(value)
            self.assertEqual(value, original)
            self.assertEqual(request.exchange_state(value), {"response_kind": "tool_calls", "pending_call_ids": ["call-1", "call-2"],
                "history_call_ids": [], "error_call_ids": [], "history_tool_result_count": 0})
            sample = request.sample_for_exchange(value)
            self.assertTrue(request.validate_exchange_sample(sample))
            self.assertEqual(json.loads(sample["prompt"])["schema"], "nous.request-tool-sft/1")
            self.assertNotIn("quality", sample)

    def test_exchange2_pending_calls_obey_explicit_tool_choice_and_parallel_settings(self):
        for provider in request.PROVIDERS:
            value = tool_exchange(provider)
            value["request"]["tool_choice"] = "none" if provider == "openai-responses" else {"type": "none"}
            self.rejected(value, "request_tool_choice_violation")
            value = tool_exchange(provider, parallel=True)
            if provider == "openai-responses":
                value["request"]["parallel_tool_calls"] = False
            else:
                value["request"]["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
            self.rejected(value, "request_parallel_tool_violation")

    def test_exchange2_mixed_visible_commentary_and_tools_preserve_native_blocks(self):
        value = tool_exchange(); value["response"]["output"].insert(0, {"type": "message", "role": "assistant",
            "status": "completed", "phase": "commentary", "content": [{"type": "output_text", "text": "Checking."}]})
        self.assertEqual(request.request_messages(value)[-2]["content"][0]["text"], "Checking.")
        value = closed_tool_exchange("anthropic-messages", parallel=True)
        value["request"]["messages"][1]["content"].insert(1, {"type": "text", "text": "Checking both."})
        request.validate_exchange(value)
        self.assertEqual(json.loads(request.sample_for_exchange(value)["prompt"])["request"], value["request"])

    def test_exchange2_closed_parallel_results_preserve_source_order(self):
        for provider in request.PROVIDERS:
            value = closed_tool_exchange(provider, parallel=True)
            state = request.exchange_state(value)
            self.assertEqual(state["response_kind"], "final_text")
            self.assertEqual(state["history_call_ids"], ["call-1", "call-2"])
            self.assertEqual(state["pending_call_ids"], [])
            sample = request.sample_for_exchange(value)
            self.assertEqual(json.loads(sample["prompt"])["request"], value["request"])

    def test_exchange2_explicit_errors_are_preserved_without_text_inference(self):
        for provider in request.PROVIDERS:
            value = closed_tool_exchange(provider, error=True)
            state = request.exchange_state(value)
            self.assertEqual(state["error_call_ids"], ["call-1"] if provider == "anthropic-messages" else [])
            mirror = request.request_messages(value)
            if provider == "anthropic-messages":
                self.assertTrue(mirror[-2]["content"][0]["is_error"])
            self.assertIn("synthetic", json.dumps(request.sample_for_exchange(value)))

    def test_exchange2_retry_after_failure_requires_new_call_id(self):
        for provider in request.PROVIDERS:
            value = closed_tool_exchange(provider, error=True)
            retry = tool_exchange(provider)["response"]
            if provider == "openai-responses":
                retry["output"][0]["call_id"] = "retry-1"
            else:
                retry["content"][0]["id"] = "retry-1"
            value["response"] = retry
            self.assertEqual(request.exchange_state(value)["pending_call_ids"], ["retry-1"])
            if provider == "openai-responses":
                value["response"]["output"][0]["call_id"] = "call-1"
            else:
                value["response"]["content"][0]["id"] = "call-1"
            self.rejected(value, "invalid_request_tool_linkage")

    def test_exchange2_cannot_be_silently_exported_through_v3_or_old_serialization(self):
        value = closed_tool_exchange()
        source = record(); source["exchange"] = value
        source["messages"] = request.request_messages(value)
        source["provenance"]["request_content_sha256"] = hashlib.sha256(request._json(value["request"]).encode()).hexdigest()
        with self.assertRaisesRegex(request.RequestError, "^request_tool_exchange_requires_episode$"):
            request.sample_for(source)
        with self.assertRaises(request.RequestError):
            request.validate_sample(request.sample_for_exchange(value))
        with self.assertRaises(request.RequestError):
            request.validate_exchange_sample(request.sample_for(record()))
        with self.assertRaises(request.RequestError):
            request.normalize_exchange(value["provider"], value["request"], value["response"])
        self.assertEqual(request.normalize_exchange(value["provider"], value["request"], value["response"],
                         schema=request.TOOL_SCHEMA), value)

    def test_exchange2_missing_duplicate_or_orphan_results_are_rejected(self):
        for provider in request.PROVIDERS:
            for mode in ("missing", "duplicate", "orphan"):
                value = closed_tool_exchange(provider, parallel=True)
                results = value["request"]["input"] if provider == "openai-responses" else value["request"]["messages"][-1]["content"]
                if mode == "missing":
                    results.pop()
                elif mode == "duplicate":
                    results.append(copy.deepcopy(results[-1]))
                else:
                    results[-1]["call_id" if provider == "openai-responses" else "tool_use_id"] = "missing-call"
                self.rejected(value)

    def test_exchange2_duplicate_historical_and_pending_calls_are_rejected(self):
        for provider in request.PROVIDERS:
            value = tool_exchange(provider, parallel=True)
            calls = value["response"]["output" if provider == "openai-responses" else "content"]
            calls[1]["call_id" if provider == "openai-responses" else "id"] = "call-1"
            self.rejected(value, "invalid_request_tool_linkage")
            value = closed_tool_exchange(provider, parallel=True)
            calls = value["request"]["input"] if provider == "openai-responses" else value["request"]["messages"][1]["content"]
            index = 2 if provider == "openai-responses" else 1
            calls[index]["call_id" if provider == "openai-responses" else "id"] = "call-1"
            self.rejected(value, "invalid_request_tool_linkage")

    def test_exchange2_out_of_order_result_before_call_is_rejected(self):
        for provider in request.PROVIDERS:
            value = closed_tool_exchange(provider)
            history = value["request"]["input" if provider == "openai-responses" else "messages"]
            history[1], history[2] = history[2], history[1]
            self.rejected(value, "invalid_request_tool_linkage")

    def test_exchange2_parallel_batch_cannot_interleave_new_call_with_partial_results(self):
        value = closed_tool_exchange(parallel=True)
        history = value["request"]["input"]
        new = copy.deepcopy(history[1]); new["call_id"] = "later-call"
        history.insert(-1, new)
        self.rejected(value, "request_tool_batch_interleaved")
        value = closed_tool_exchange(parallel=True)
        value["request"]["input"].insert(-1, {"type": "additional_tools", "role": "developer", "tools": [function_tool()]})
        self.rejected(value, "request_tool_batch_interleaved")

    def test_exchange2_anthropic_results_are_immediate_and_before_text(self):
        value = closed_tool_exchange("anthropic-messages", parallel=True)
        results = value["request"]["messages"][-1]["content"]
        value["request"]["messages"].append({"role": "user", "content": [results.pop()]})
        self.rejected(value, "request_tool_results_missing")
        value = closed_tool_exchange("anthropic-messages")
        value["request"]["messages"][-1]["content"].insert(0, {"type": "text", "text": "too early"})
        self.rejected(value, "request_tool_results_missing")
        value = closed_tool_exchange("anthropic-messages")
        value["request"]["messages"][-1]["content"].append({"type": "text", "text": "after result"})
        request.validate_exchange(value)

    def test_exchange2_dynamic_catalog_must_precede_use_without_redefinition(self):
        value = closed_tool_exchange(); tool = value["request"].pop("tools")[0]
        value["request"]["input"].insert(1, {"type": "additional_tools", "role": "developer", "tools": [tool]})
        request.validate_exchange(value)
        late = copy.deepcopy(value)
        late["request"]["input"].append(late["request"]["input"].pop(1))
        self.rejected(late, "request_function_contract_missing_or_ambiguous")
        changed = copy.deepcopy(tool); changed["parameters"]["properties"]["key"]["type"] = "integer"
        value["request"]["input"].append({"type": "additional_tools", "role": "developer", "tools": [changed]})
        self.rejected(value, "request_tool_contract_changed")

    def test_exchange2_namespace_calls_and_result_identity_are_exact(self):
        value = closed_tool_exchange(); value["request"]["tools"] = [
            {"type": "namespace", "name": "left", "tools": [function_tool()]},
            {"type": "namespace", "name": "right", "tools": [function_tool()]}]
        call, result = value["request"]["input"][1:]
        call["namespace"] = "left"; result.update(name="lookup", namespace="left")
        request.validate_exchange(value)
        self.assertEqual(request.request_messages(value)[1]["content"][0]["name"], "left.lookup")
        result["namespace"] = "right"
        self.rejected(value, "request_tool_result_identity_mismatch")
        result["namespace"] = "left"; call.pop("namespace")
        self.rejected(value, "request_function_contract_missing_or_ambiguous")

    def test_exchange2_function_result_text_arrays_and_empty_anthropic_results_are_native(self):
        value = closed_tool_exchange(); value["request"]["input"][-1]["output"] = [{"type": "input_text", "text": "native"}]
        request.validate_exchange(value)
        self.assertEqual(request.request_messages(value)[-2]["content"], [{"type": "text", "text": "native"}])
        value["request"]["input"][-1]["output"] = [{"type": "input_image", "image_url": "PRIVATE"}]
        self.rejected(value, "unsupported_request_tool_output")
        for content in (None, [], ""):
            value = closed_tool_exchange("anthropic-messages")
            block = value["request"]["messages"][-1]["content"][0]
            if content is None:
                block.pop("content")
            else:
                block["content"] = content
            request.validate_exchange(value)
            request.request_messages(value)

    def test_exchange2_error_flag_is_boolean_not_truthy(self):
        for flag in (None, 0, "true", {}):
            value = closed_tool_exchange("anthropic-messages")
            value["request"]["messages"][-1]["content"][0]["is_error"] = flag
            self.rejected(value, "invalid_request_boolean")

    def test_exchange2_response_tool_schema_and_completion_status_are_checked(self):
        for provider in request.PROVIDERS:
            value = tool_exchange(provider)
            if provider == "openai-responses":
                value["response"]["output"][0]["arguments"] = '{"key":9}'
            else:
                value["response"]["content"][0]["input"] = {"key": 9}
            self.rejected(value, "request_tool_arguments_mismatch")
        value = tool_exchange(); value["response"]["output"][0]["status"] = "in_progress"
        self.rejected(value, "request_response_incomplete")
        value = tool_exchange("anthropic-messages"); value["response"]["stop_reason"] = "max_tokens"
        self.rejected(value, "request_response_incomplete")

    def test_exchange2_parameter_parsing_rejects_duplicates_and_regex_execution(self):
        value = tool_exchange(); value["response"]["output"][0]["arguments"] = '{"key":"x","key":"y"}'
        self.rejected(value, "duplicate_request_json_key")
        value = tool_exchange(); value["request"]["tools"][0]["parameters"]["properties"]["key"]["pattern"] = "(a+)+$"
        with mock.patch.object(request, "validate_value", side_effect=AssertionError("must not run matcher")):
            self.rejected(value, "request_tool_schema_execution_unsupported")
        value = tool_exchange()
        with mock.patch.object(request, "MAX_ARGUMENT_SCHEMA_WORK", 1):
            self.rejected(value, "request_tool_validation_limit")

    def test_exchange2_limits_call_count_and_preserves_argument_string_bytes(self):
        value = tool_exchange(parallel=True)
        with mock.patch.object(request, "MAX_TOOL_CALLS", 1):
            self.rejected(value, "request_tool_call_limit")
        value = tool_exchange(); native = ' { "key" : "x" } '
        value["response"]["output"][0]["arguments"] = native
        sample = request.sample_for_exchange(value)
        self.assertEqual(json.loads(sample["completion"])["response"]["output"][0]["arguments"], native)

    def test_exchange2_sample_revalidation_checks_target_and_version(self):
        sample = request.sample_for_exchange(tool_exchange())
        target = json.loads(sample["completion"]); target["response"]["output"][0]["name"] = "missing"
        with self.assertRaisesRegex(request.RequestError, "request_function_contract_missing_or_ambiguous"):
            request.validate_exchange_sample({**sample, "completion": request._json(target) + "\n"})
        with self.assertRaises(request.RequestError):
            request.validate_exchange_sample({**sample, "prompt": " " + sample["prompt"]})

    def test_exchange2_custom_server_reasoning_and_media_are_never_promoted(self):
        for kind in ("custom_tool_call", "web_search_call", "reasoning"):
            value = tool_exchange(); value["response"]["output"].append({"type": kind, "input": "PRIVATE"})
            self.rejected(value)
        value = closed_tool_exchange(); value["request"]["input"][1]["type"] = "custom_tool_call"
        self.rejected(value, "request_tool_history_not_supported")

    def test_client_metadata_has_separate_bounded_feature_descriptor_contract(self):
        value = exchange()
        value["request"]["client_metadata"] = {"feature_descriptor": "x" * 1024}
        request.validate_exchange(value)
        self.assertEqual(value["request"]["client_metadata"]["feature_descriptor"], "x" * 1024)
        value["request"]["client_metadata"] = {"feature_descriptor": "x" * 16385}
        self.rejected(value, "invalid_request_client_metadata")

    def test_codex_client_metadata_and_include_are_preserved_native_settings(self):
        value = exchange()
        value["request"].update(client_metadata={"origin": "synthetic", "empty": ""},
                                include=["reasoning.encrypted_content"], reasoning={"effort": "none"})
        normalized = request.normalize_exchange(value["provider"], value["request"], value["response"])
        self.assertEqual(normalized, value)
        for item in (None, []):
            value["request"]["include"] = item
            request.validate_exchange(value)

    def test_codex_setting_support_does_not_admit_actual_hidden_content(self):
        for area in ("input", "output"):
            value = exchange(); value["request"]["include"] = ["reasoning.encrypted_content"]
            hidden = {"type": "reasoning", "encrypted_content": "PRIVATE_ENCRYPTED"}
            if area == "input":
                value["request"]["input"].insert(0, hidden)
            else:
                value["response"]["output"].insert(0, hidden)
            self.rejected(value)
        value = exchange(); value["response"]["output"][0]["encrypted_content"] = "PRIVATE_ENCRYPTED"
        self.rejected(value)
        value = exchange(); value["request"]["reasoning"] = {"summary": "PRIVATE_UNKNOWN"}
        self.rejected(value, "unsupported_request_reasoning_summary")

    def test_known_reasoning_summary_request_settings_serialize_without_defaults(self):
        for summary in ("auto", "concise", "detailed", "none", None):
            source = record()
            source["exchange"]["request"]["reasoning"] = {"summary": summary}
            source["provenance"]["request_content_sha256"] = hashlib.sha256(
                request._json(source["exchange"]["request"]).encode()).hexdigest()
            sample = request.sample_for(source)
            self.assertEqual(json.loads(sample["prompt"])["request"]["reasoning"], {"summary": summary})
            self.assertNotIn("summary", json.loads(sample["completion"])["response"])

    def test_reasoning_summary_settings_do_not_admit_actual_summary_or_context(self):
        for hidden in ({"type": "reasoning", "summary": [{"type": "summary_text", "text": "PRIVATE_HIDDEN"}]},
                       {"type": "reasoning", "encrypted_content": "PRIVATE_HIDDEN"}):
            value = exchange(); value["request"]["reasoning"] = {"summary": "auto"}
            value["response"]["output"].insert(0, hidden)
            self.rejected(value)
        value = exchange(); value["request"]["reasoning"] = {"context": "all_turns"}
        self.rejected(value, "unsupported_request_reasoning_field")
        value = exchange(); value["request"]["input"].insert(0,
            {"type": "configuration_update", "reasoning": {"summary": "auto"}})
        self.rejected(value, "unsupported_request_reasoning_field")

    def test_codex_metadata_limits_and_closed_selectors(self):
        for metadata in ({"key": 1}, {"long": "a" * 16385}, {"a" * 129: "value"}, {str(i): "value" for i in range(65)}):
            value = exchange(); value["request"]["client_metadata"] = metadata
            self.rejected(value)
        for include in (["output.encrypted_content"], ["unknown"], ["reasoning.encrypted_content"] * 2,
                        ["reasoning.encrypted_content", "output.encrypted_content"], "reasoning.encrypted_content"):
            value = exchange(); value["request"]["include"] = include
            self.rejected(value, "unsupported_request_include")

    def test_unbound_alternative_context_views_are_rejected_even_for_candidates(self):
        for field in ("protocol", "events", "context", "tool_contracts", "tools"):
            value = record()
            value[field] = {"PRIVATE_CONTEXT": "ignored duplicate"}
            for require_quality in (True, False):
                with self.assertRaisesRegex(request.RequestError, "^request_unbound_context_extension$"):
                    request.validate_record(value, require_quality=require_quality)

    def rejected(self, body, code=None):
        with self.assertRaises(request.RequestError) as raised:
            request.validate_exchange(body)
        if code:
            self.assertEqual(str(raised.exception), code)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_supported_providers_roundtrip_without_default_insertion(self):
        for provider in request.PROVIDERS:
            value = exchange(provider)
            original = copy.deepcopy(value)
            request.validate_exchange(value)
            self.assertEqual(value, original)
            self.assertNotIn("stream", value["request"])
            self.assertNotIn("tools", value["request"])
            self.assertNotIn("system", value["request"])
            self.assertNotIn("instructions", value["request"])
            self.assertEqual(request.request_messages(value)[-1],
                             {"role": "assistant", "content": [{"type": "text", "text": '{"answer":4}'}]})

    def test_normalize_and_validate_request_return_independent_copies(self):
        outgoing = openai_request()
        checked = request.validate_request("openai-responses", outgoing)
        value = request.normalize_exchange("openai-responses", outgoing, openai_response())
        outgoing["input"][0]["content"] = "changed"
        self.assertNotEqual(value["request"], outgoing)
        self.assertNotEqual(checked, outgoing)

    def test_native_instructions_system_and_order_are_preserved_without_role_guess(self):
        for provider, field in (("openai-responses", "instructions"), ("anthropic-messages", "system")):
            value = exchange(provider)
            value["request"][field] = "PRIVATE_NATIVE_TOP_LEVEL_CONTEXT"
            messages = request.request_messages(value)
            self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
            source = record(provider)
            source.update(exchange=value, messages=messages)
            source["provenance"]["request_content_sha256"] = hashlib.sha256(request._json(value["request"]).encode()).hexdigest()
            sample = request.sample_for(source)
            self.assertEqual(json.loads(sample["prompt"])["request"][field], "PRIVATE_NATIVE_TOP_LEVEL_CONTEXT")
            self.assertNotIn("PRIVATE_NATIVE_TOP_LEVEL_CONTEXT", sample["completion"])

    def test_explicit_native_message_roles_remain_distinct(self):
        value = exchange()
        value["request"]["input"][:0] = [{"role": "system", "content": "synthetic system"},
                                         {"role": "developer", "content": "synthetic developer"}]
        self.assertEqual([m["role"] for m in request.request_messages(value)], ["system", "developer", "user", "assistant"])

    def test_response_audit_fields_are_bound_but_not_supervised_targets(self):
        source = record()
        sample = request.sample_for(source)
        self.assertTrue(request.validate_sample(sample))
        self.assertEqual(json.loads(sample["completion"]), {"response": {"output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": '{"answer":4}', "annotations": []}]}]}})
        for forbidden in ("synthetic-response", "synthetic-message", '"usage"', '"status"'):
            self.assertNotIn(forbidden, sample["completion"])
        self.assertIn("id", source["exchange"]["response"])

    def test_input_history_is_prompt_only_and_cannot_become_an_extra_target(self):
        source = record()
        source["exchange"]["request"]["input"][:0] = [{"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer only in prompt"}]
        source["messages"] = request.request_messages(source["exchange"])
        source["provenance"]["request_content_sha256"] = hashlib.sha256(request._json(source["exchange"]["request"]).encode()).hexdigest()
        sample = request.sample_for(source)
        self.assertIn("Earlier answer only in prompt", sample["prompt"])
        self.assertNotIn("Earlier answer only in prompt", sample["completion"])

    def test_sse_means_only_gateway_transport_and_requires_full_final_response(self):
        for provider in request.PROVIDERS:
            value = exchange(provider)
            value["transport"] = "sse"
            self.rejected(value, "request_transport_mismatch")
            value["request"]["stream"] = True
            request.validate_exchange(value)
            value["response"]["status" if provider == "openai-responses" else "stop_reason"] = "incomplete"
            self.rejected(value, "request_response_incomplete")
        value = exchange(); value["request"]["stream"] = True
        self.rejected(value, "request_transport_mismatch")

    def test_server_side_context_and_transform_flags_fail_closed(self):
        for key, value in (("previous_response_id", "PRIVATE_REMOTE"), ("conversation", {"id": "PRIVATE_REMOTE"}),
                           ("prompt", {"id": "PRIVATE_REMOTE"}), ("context_management", []),
                           ("background", True), ("truncation", "auto")):
            source = exchange(); source["request"][key] = value
            self.rejected(source)
        for key in ("container", "context_management", "mcp_servers"):
            source = exchange("anthropic-messages"); source["request"][key] = {}
            self.rejected(source, "request_server_context_unsupported")
        source = exchange("anthropic-messages"); source["response"]["input_transformations"] = [{"private": "PRIVATE"}]
        self.rejected(source, "unsupported_response_context")

    def test_ordinary_function_definitions_are_context_not_tool_executions(self):
        value = exchange(); value["request"]["tools"] = [function_tool()]
        request.validate_exchange(value)
        self.assertEqual(len(request.request_messages(value)), 2)
        anthropic = exchange("anthropic-messages")
        tool = function_tool()
        anthropic["request"]["tools"] = [{"name": tool["name"], "input_schema": tool["parameters"]}]
        request.validate_exchange(anthropic)

    def test_native_additional_catalog_and_configuration_are_kept_without_fake_messages(self):
        value = exchange()
        custom = {"type": "custom", "name": "exec", "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"}}
        value["request"]["input"][:0] = [{"type": "additional_tools", "role": "developer",
            "tools": [{"type": "namespace", "name": "functions", "tools": [function_tool(), custom]}]},
            {"type": "configuration_update", "reasoning": {"effort": "low"}}]
        native = copy.deepcopy(value["request"])
        request.validate_exchange(value)
        self.assertEqual(value["request"], native)
        self.assertEqual(len(request.request_messages(value)), 2)

    def test_catalog_redefinition_and_unknown_semantics_are_rejected(self):
        value = exchange(); value["request"]["tools"] = [function_tool()]
        changed = function_tool(); changed["parameters"]["properties"]["key"]["type"] = "integer"
        value["request"]["input"].insert(0, {"type": "additional_tools", "role": "developer", "tools": [changed]})
        self.rejected(value, "request_tool_contract_changed")
        value = exchange(); tool = function_tool(); tool["parameters"]["PRIVATE_UNKNOWN_KEY"] = "PRIVATE"
        value["request"]["tools"] = [tool]
        self.rejected(value, "unsupported_request_tool_schema")
        value = exchange(); value["request"]["tools"] = [{"type": "web_search"}]
        self.rejected(value, "unsupported_request_tool")

    def test_actual_tool_history_is_rejected_even_when_closed_and_defined(self):
        value = exchange(); value["request"]["tools"] = [function_tool()]
        value["request"]["input"] += [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": '{"key":"x"}'},
            {"type": "function_call_output", "call_id": "call-1", "output": "PRIVATE_FILE_BODY"}]
        self.rejected(value, "request_tool_history_not_supported")
        with self.assertRaisesRegex(request.RequestError, "^request_tool_history_not_supported$"):
            request.validate_request(value["provider"], value["request"])
        value = exchange("anthropic-messages")
        value["request"]["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "PRIVATE_FILE_BODY"}]}]
        self.rejected(value, "request_tool_history_not_supported")

    def test_final_response_tool_calls_media_reasoning_and_refusal_are_unsupported(self):
        for kind in ("function_call", "custom_tool_call", "reasoning", "web_search_call"):
            value = exchange(); value["response"]["output"] = [{"type": kind, "private": "PRIVATE"}]
            self.rejected(value)
        for block in ({"type": "input_image", "image_url": "PRIVATE"}, {"type": "thinking", "thinking": "PRIVATE"},
                      {"type": "refusal", "refusal": "PRIVATE"}):
            value = exchange(); value["response"]["output"][0]["content"] = [block]
            self.rejected(value)
            value = exchange("anthropic-messages"); value["response"]["content"] = [block]
            self.rejected(value)

    def test_hidden_reasoning_or_media_in_input_is_never_silently_removed(self):
        for content in ([{"type": "reasoning", "text": "PRIVATE"}], [{"type": "input_image", "image_url": "PRIVATE"}]):
            value = exchange(); value["request"]["input"][0]["content"] = content
            self.rejected(value)
        value = exchange(); value["request"]["input"].insert(0, {"type": "reasoning", "encrypted_content": "PRIVATE"})
        self.rejected(value, "hidden_reasoning_unsupported")
        value = exchange("anthropic-messages"); value["request"]["thinking"] = {"type": "adaptive"}
        self.rejected(value, "hidden_reasoning_unsupported")

    def test_terminal_flags_and_single_text_target_are_required(self):
        for status in ("in_progress", "incomplete", "failed", "cancelled", None):
            value = exchange(); value["response"]["status"] = status
            self.rejected(value)
        for reason in ("max_tokens", "tool_use", "pause_turn", "stop_sequence", None):
            value = exchange("anthropic-messages"); value["response"]["stop_reason"] = reason
            self.rejected(value)
        value = exchange(); value["response"]["output"][0]["status"] = "in_progress"
        self.rejected(value, "request_response_incomplete")
        value = exchange(); value["response"]["output"] *= 2
        self.rejected(value, "request_final_text_required")
        value = exchange(); value["response"]["output"][0]["content"] *= 2
        self.rejected(value, "request_final_text_required")
        value = exchange(); value["response"]["error"] = {"message": "PRIVATE"}
        self.rejected(value, "request_response_incomplete")

    def test_bad_field_types_and_unknown_payloads_return_static_errors(self):
        mutations = [("background", 0.0), ("temperature", True), ("stream", "false"), ("max_output_tokens", 0),
                     ("tool_choice", {"type": "function", "name": "PRIVATE"}), ("PRIVATE", {"prompt": "PRIVATE"})]
        for key, item in mutations:
            value = exchange(); value["request"][key] = item
            self.rejected(value)
        value = exchange(); value["response"]["PRIVATE_RESPONSE_FIELD"] = "PRIVATE"
        self.rejected(value, "unsupported_response_field")

    def test_json_finiteness_unicode_duplicate_keys_and_resource_limits(self):
        for bad in (float("nan"), float("inf"), "\ud800", object()):
            value = exchange(); value["request"]["metadata"] = {"bad": bad}
            self.rejected(value)
        for raw in ('{"key":1,"key":2}', '{"key":1e999}', '{"key":NaN}'):
            with self.assertRaises(request.RequestError):
                request._loads(raw)
        for name, limit in (("MAX_BYTES", 16), ("MAX_ITEMS", 0), ("MAX_DEPTH", 1)):
            value = exchange()
            with mock.patch.object(request, name, limit):
                self.rejected(value)

    def test_mirror_and_request_digest_tampering_cannot_pass_record_or_sample_validation(self):
        for mutator in (lambda r: r["messages"][-1]["content"][0].update(text="changed"),
                        lambda r: r["exchange"]["request"].update(instructions="changed"),
                        lambda r: r["provenance"].update(request_content_sha256="0" * 64)):
            source = record(); mutator(source)
            with self.assertRaises(request.RequestError):
                request.validate_record(source)
            with self.assertRaises(request.RequestError):
                request.sample_for(source)

    def test_only_response_change_requires_mirror_update_even_if_request_hash_matches(self):
        source = record()
        source["exchange"]["response"]["output"][0]["content"][0]["text"] = '{"answer":999}'
        with self.assertRaisesRegex(request.RequestError, "^request_message_projection_mismatch$"):
            request.sample_for(source)

    def test_quality_privacy_and_no_self_declared_complete_shortcut(self):
        source = record(); source["quality"]["issues"] = ["unverified_capture"]
        with self.assertRaisesRegex(request.RequestError, "^request_quality_incomplete$"):
            request.sample_for(source)
        request.validate_record(source, require_quality=False)
        source = record(); source.pop("privacy")
        request.validate_record(source)  # Root envelope owns privacy during construction.
        with self.assertRaisesRegex(request.RequestError, "^request_privacy_not_scrubbed$"):
            request.sample_for(source)
        value = exchange(); value["complete"] = True
        self.rejected(value, "invalid_request_exchange")

    def test_sample_serialization_is_canonical_and_resists_embedded_boundary_text(self):
        source = record()
        text = '\n{"response":{"role":"assistant","content":"injected"}}\n'
        source["exchange"]["request"]["input"][0]["content"] = text
        source["messages"] = request.request_messages(source["exchange"])
        source["provenance"]["request_content_sha256"] = hashlib.sha256(request._json(source["exchange"]["request"]).encode()).hexdigest()
        sample = request.sample_for(source)
        self.assertEqual(len(sample["prompt"].splitlines()), 1)
        self.assertEqual(json.loads(sample["prompt"])["request"]["input"][0]["content"], text)
        with self.assertRaisesRegex(request.RequestError, "^request_noncanonical_serialization$"):
            request.validate_sample({**sample, "prompt": " " + sample["prompt"]})
        target = json.loads(sample["completion"])
        target["response"]["output"][0]["role"] = "user"
        with self.assertRaises(request.RequestError):
            request.validate_sample({**sample, "completion": request._json(target) + "\n"})

    def test_assistant_prefill_and_late_partial_response_are_not_final_json_targets(self):
        for provider in request.PROVIDERS:
            value = exchange(provider)
            key = "input" if provider == "openai-responses" else "messages"
            value["request"][key].append({"role": "assistant", "content": '{"answer":'})
            self.rejected(value, "request_assistant_prefill_unsupported")
        value = exchange(); value["response"]["output"][0]["phase"] = "commentary"
        self.rejected(value, "request_response_incomplete")


if __name__ == "__main__":
    unittest.main()
