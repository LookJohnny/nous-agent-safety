"""Synthetic pure JSON oracle verification; no model calls or user records."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
import nous_training_verify as verify
from nous_training_export import record_digest

REQUEST_SHA = hashlib.sha256(b"synthetic-request-bytes").hexdigest()


def oracle(*, scope="final_json_task", expected=None):
    return {"schema": "nous.oracle/1", "scope": scope, "request_sha256": REQUEST_SHA,
            "assertions": [{"kind": "exact_json", "expected": {"answer": 4} if expected is None else expected}]}


def record(result='{"answer":4}', *, provider="openai-responses"):
    if provider == "openai-responses":
        request = {"model": "synthetic-model", "input": "Return the JSON answer to 2 + 2."}
        response = {"id": "synthetic-response", "object": "response", "status": "completed", "model": "synthetic-model",
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": result}]}]}
    else:
        request = {"model": "synthetic-model", "max_tokens": 128,
                   "messages": [{"role": "user", "content": "Return the JSON answer to 2 + 2."}]}
        response = {"id": "synthetic-response", "type": "message", "role": "assistant", "model": "synthetic-model",
                    "stop_reason": "end_turn", "content": [{"type": "text", "text": result}]}
    exchange = {"schema": "nous.request-exchange/1", "provider": provider, "transport": "json",
                "request": request, "response": response}
    from nous_training_request import request_messages
    return {"schema": "nous.trajectory/3", "id": "synthetic-record",
            "agent": {"id": "unknown"}, "exchange": exchange,
            "messages": request_messages(exchange), "quality": {"complete": True, "missing": [], "issues": []},
            "privacy": {"scrubbed": True, "scrubber_version": "synthetic/1"},
            "provenance": {"request_sha256": REQUEST_SHA,
                           "request_content_sha256": hashlib.sha256(json.dumps(request, sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
                           "project_id": "synthetic-project",
                           "session_id": "synthetic-session"}, "evidence": []}


class OracleValidation(unittest.TestCase):
    def invalid(self, spec, code):
        with self.assertRaises(verify.VerificationError) as context:
            verify.validate_oracle(spec)
        self.assertEqual(str(context.exception), code)

    def test_normalized_snapshot_is_independent_and_order_invariant(self):
        source = oracle()
        normalized = verify.validate_oracle(source)
        digest = verify.oracle_digest(source)
        source["assertions"][0]["expected"]["answer"] = 9
        self.assertEqual(normalized["assertions"][0]["expected"], {"answer": 4})
        self.assertEqual(digest, verify.oracle_digest(dict(reversed(list(normalized.items())))))

    def test_template_has_no_implied_registration_or_binding(self):
        spec = oracle()
        del spec["request_sha256"]
        self.invalid(spec, "oracle_request_binding_missing")
        normalized = verify.validate_oracle(spec, require_binding=False)
        self.assertNotIn("request_sha256", normalized)
        self.assertNotIn("timing", normalized)
        self.assertNotEqual(verify.oracle_digest(normalized, require_binding=False), verify.oracle_digest(oracle()))

    def test_optional_binding_still_rejects_invalid_supplied_value(self):
        for value in (None, "", "A" * 64, "private-secret"):
            with self.subTest(value=value), self.assertRaisesRegex(verify.VerificationError, "^oracle_invalid_request_binding$"):
                verify.validate_oracle({**oracle(), "request_sha256": value}, require_binding=False)

    def test_task_scope_requires_full_exact_oracle(self):
        spec = oracle()
        spec["assertions"] = [{"kind": "json_schema", "schema": {"type": "object"}}]
        self.invalid(spec, "oracle_task_requires_exact_result")
        spec["scope"] = "final_json_format"
        self.assertEqual(verify.validate_oracle(spec)["scope"], "final_json_format")

    def test_oracle_fields_and_scope_are_closed(self):
        for extra in ({"command": "private shell"}, {"pre_registered": True}, {"scope": ["bad"]}):
            with self.subTest(extra=extra):
                self.invalid({**oracle(), **extra}, "oracle_invalid_fields")

    def test_assertion_fields_are_closed(self):
        for assertion in ({"kind": "command", "argv": ["false"]},
                          {"kind": "exact_json", "expected": {}, "partial": True}, {"kind": "exact_json"}):
            spec = oracle()
            spec["assertions"] = [assertion]
            self.invalid(spec, "oracle_invalid_assertion")

    def test_float_and_non_json_expected_are_rejected(self):
        for value, code in ((4.0, "oracle_unsupported_number"), (float("nan"), "oracle_unsupported_number"),
                            ({1, 2}, "oracle_invalid_json"), (1 << 64, "oracle_integer_out_of_range")):
            with self.subTest(code=code):
                self.invalid(oracle(expected=value), code)

    def test_pattern_composition_references_and_quadratic_unique_are_rejected(self):
        for schema in ({"pattern": "(a+)+$"}, {"$ref": "private-path"}, {"oneOf": [{}, {}]},
                       {"uniqueItems": True}, {"multipleOf": 2}, {"description": "private"}):
            spec = oracle(scope="final_json_format")
            spec["assertions"] = [{"kind": "json_schema", "schema": schema}]
            self.invalid(spec, "oracle_unsupported_schema")

    def test_schema_invalid_types_and_bounds_are_rejected(self):
        for schema in ({"type": "unknown"}, {"type": ["integer", "null"]}, {"required": [True]},
                       {"properties": []}, {"enum": []}, {"enum": [True, True]}):
            spec = oracle(scope="final_json_format")
            spec["assertions"] = [{"kind": "json_schema", "schema": schema}]
            self.invalid(spec, "oracle_invalid_schema")
        for schema in ({"minItems": True}, {"maxLength": 1 << 20}, {"minimum": 9, "maximum": 1}):
            spec["assertions"] = [{"kind": "json_schema", "schema": schema}]
            self.invalid(spec, "oracle_invalid_schema_bound")

    def test_contradictory_assertions_are_rejected(self):
        spec = oracle()
        spec["assertions"].append({"kind": "exact_json", "expected": {"answer": 5}})
        self.invalid(spec, "oracle_inconsistent_assertions")
        spec["assertions"][1] = {"kind": "json_schema", "schema": {"type": "array"}}
        self.invalid(spec, "oracle_inconsistent_assertions")

    def test_oracle_limits_fail_instead_of_truncate(self):
        self.invalid(oracle(expected="x" * (verify.MAX_ORACLE_BYTES + 1)), "oracle_limit_exceeded")
        spec = oracle()
        spec["assertions"] *= verify.MAX_ASSERTIONS + 1
        self.invalid(spec, "oracle_invalid_assertions")
        value = 1
        for _ in range(40):
            value = [value]
        self.invalid(oracle(expected=value), "oracle_limit_exceeded")


class Evaluation(unittest.TestCase):
    def evaluate(self, value=None, spec=None, **kwargs):
        return verify.evaluate_record(record() if value is None else value, oracle() if spec is None else spec,
                                      request_sha256=kwargs.pop("request_sha256", REQUEST_SHA), **kwargs)

    def indeterminate(self, value, code, **kwargs):
        report = self.evaluate(value, **kwargs)
        self.assertEqual(report["status"], "indeterminate", report)
        self.assertEqual(report["codes"], [code])
        self.assertIsNone(report["evidence_candidate"])
        return report

    def test_exact_match_binds_all_digests_without_writing_evidence(self):
        value = record()
        before = copy.deepcopy(value)
        report = self.evaluate(value)
        self.assertEqual(report["status"], "matched", report)
        self.assertEqual(report["record_digest"], record_digest(value))
        self.assertEqual(report["oracle_sha256"], verify.oracle_digest(oracle()))
        self.assertEqual(report["request_sha256"], REQUEST_SHA)
        self.assertEqual(report["result_digest"], hashlib.sha256(b'{"answer":4}').hexdigest())
        self.assertEqual(report["environment_digest"], verify.ENVIRONMENT_DIGEST)
        self.assertEqual(report["evidence_candidate"]["kind"], "verifier")
        self.assertEqual(report["evidence_candidate"]["result"], "passed")
        self.assertEqual(value, before)

    def test_anthropic_final_is_supported_without_model_call(self):
        self.assertEqual(self.evaluate(record(provider="anthropic-messages"))["status"], "matched")

    def test_wrong_result_is_negative_candidate(self):
        report = self.evaluate(record('{"answer":5}'))
        self.assertEqual(report["status"], "mismatched", report)
        self.assertEqual(report["evidence_candidate"]["result"], "failed")

    def test_exact_match_rejects_extra_and_missing_fields(self):
        for text in ('{"answer":4,"extra":true}', '{}', '{"answer":"4"}', '{"answer":true}'):
            with self.subTest(text=text):
                self.assertEqual(self.evaluate(record(text))["status"], "mismatched")

    def test_boolean_is_not_integer(self):
        self.assertEqual(self.evaluate(record("true"), oracle(expected=1))["status"], "mismatched")
        self.assertEqual(self.evaluate(record("1"), oracle(expected=True))["status"], "mismatched")

    def test_object_order_and_json_whitespace_do_not_change_result_digest(self):
        spec = oracle(expected={"a": [1, None], "b": True})
        left = self.evaluate(record('{"a":[1,null],"b":true}'), spec)
        right = self.evaluate(record(' { "b": true, "a": [1, null] } \n'), spec)
        self.assertEqual(left["status"], "matched")
        self.assertEqual(left["result_digest"], right["result_digest"])

    def test_schema_only_never_creates_outcome_evidence(self):
        spec = oracle(scope="final_json_format")
        spec["assertions"] = [{"kind": "json_schema", "schema": {
            "type": "object", "properties": {"answer": {"type": "integer", "minimum": 0, "maximum": 10}},
            "required": ["answer"], "additionalProperties": False}}]
        for result, status in (('{"answer":4}', "matched"), ('{"answer":true}', "mismatched"),
                               ('{"answer":11}', "mismatched"), ('not-json', "indeterminate")):
            report = self.evaluate(record(result), spec)
            self.assertEqual(report["status"], status, report)
            self.assertEqual(report["evidence_candidate"]["kind"], "result_check")
            self.assertEqual(report["evidence_candidate"]["result"], status)

    def test_schema_array_const_enum_and_lengths(self):
        spec = oracle(scope="final_json_format")
        spec["assertions"] = [{"kind": "json_schema", "schema": {"type": "array", "minItems": 1,
            "maxItems": 2, "items": {"type": "string", "enum": ["ok", "yes"], "minLength": 2, "maxLength": 3}}}]
        self.assertEqual(self.evaluate(record('["ok"]'), spec)["status"], "matched")
        self.assertEqual(self.evaluate(record('["bad"]'), spec)["status"], "mismatched")
        spec["assertions"] = [{"kind": "json_schema", "schema": {"const": True}}]
        self.assertEqual(self.evaluate(record('1'), spec)["status"], "mismatched")

    def test_float_nan_and_out_of_range_results_are_indeterminate(self):
        for text, code in (("4.0", "result_unsupported_number"), ("4e0", "result_unsupported_number"),
                           ("NaN", "result_unsupported_number"), (str(1 << 64), "result_integer_out_of_range")):
            self.indeterminate(record(text), code)

    def test_plain_json_only_without_duplicate_keys_or_fence_scraping(self):
        for text, code in (('{"answer":4,"answer":5}', "result_duplicate_json_key"),
                           ('```json\n{"answer":4}\n```', "result_invalid_json"),
                           ('prefix {"answer":4}', "result_invalid_json"),
                           ('{"answer":', "result_invalid_json")):
            self.indeterminate(record(text), code)

    def test_request_binding_mismatch_cannot_pass(self):
        self.indeterminate(record(), "oracle_request_binding_mismatch", request_sha256="b" * 64)
        value = record()
        value["provenance"]["request_sha256"] = "b" * 64
        self.indeterminate(value, "record_request_binding_mismatch")
        self.indeterminate(record(), "request_binding_invalid", request_sha256="private-raw-id")

    def test_privacy_changed_response_cannot_pass(self):
        self.indeterminate(record(), "response_not_unchanged", response_unchanged=False)
        self.indeterminate(record(), "response_not_unchanged", response_unchanged="true")

    def test_legacy_schema_does_not_imply_request_capture(self):
        for schema in ("nous.trajectory/1", "nous.trajectory/2"):
            value = record()
            value["schema"] = schema
            self.indeterminate(value, "unsupported_record_schema")

    def test_mirror_mutation_cannot_verify_a_different_answer(self):
        value = record('{"answer":5}')
        value["messages"][-1]["content"][0]["text"] = '{"answer":4}'
        self.indeterminate(value, "request_exchange_invalid")

    def test_incomplete_native_response_and_quality_cannot_pass(self):
        value = record()
        value["exchange"]["response"]["status"] = "incomplete"
        self.indeterminate(value, "request_exchange_invalid")
        value = record()
        value["quality"]["complete"] = False
        self.indeterminate(value, "request_exchange_invalid")
        value = record()
        value["quality"]["issues"] = ["private-unknown-issue"]
        report = self.indeterminate(value, "request_exchange_invalid")
        self.assertNotIn("private-unknown-issue", json.dumps(report))

    def test_indeterminate_does_not_delete_or_replace_prior_negative(self):
        value = record()
        value["evidence"] = [{"kind": "human_review", "result": "rejected", "reviewer": "private-reviewer",
                              "record_digest": record_digest(value)}]
        before = copy.deepcopy(value)
        self.indeterminate(value, "response_not_unchanged", response_unchanged=False)
        self.assertEqual(value, before)

    def test_output_does_not_contain_result_or_oracle_or_private_metadata(self):
        value = record('{"private-answer":"secret"}')
        value["provenance"]["private-path"] = "/private/user/path"
        report = self.evaluate(value, oracle(expected={"private-oracle": "private-expected"}))
        rendered = json.dumps(report)
        for private in ("private-answer", "secret", "private-path", "/private/user/path", "private-oracle", "private-expected"):
            self.assertNotIn(private, rendered)

    def test_bounds_refuse_without_truncation(self):
        with mock.patch.object(verify, "MAX_RESULT_BYTES", 8):
            self.indeterminate(record(), "result_limit_exceeded")
        with mock.patch.object(verify, "MAX_RECORD_BYTES", 8):
            self.indeterminate(record(), "record_limit_exceeded")
        with mock.patch.object(verify, "MAX_RESULT_NODES", 2):
            self.indeterminate(record(), "result_limit_exceeded")

    def test_unknown_exception_is_static(self):
        with mock.patch.object(verify, "record_digest", side_effect=RuntimeError("private-error-path")):
            report = self.indeterminate(record(), "verification_internal_error")
        self.assertNotIn("private-error-path", json.dumps(report))

    def test_raw_request_bytes_hash_and_content_hash_are_distinct_bindings(self):
        value = record()
        self.assertNotEqual(value["provenance"]["request_sha256"], value["provenance"]["request_content_sha256"])
        self.assertEqual(self.evaluate(value)["status"], "matched")
        value["provenance"]["request_content_sha256"] = REQUEST_SHA
        self.indeterminate(value, "request_exchange_invalid")

    def test_evaluation_has_no_file_network_or_process_actions(self):
        import socket
        import subprocess
        value = record()
        with mock.patch("builtins.open", side_effect=AssertionError("no file access")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("no network")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no processes")):
            self.assertEqual(self.evaluate(value)["status"], "matched")

    def test_json_root_null_and_scalar_and_nested_values(self):
        for value in (None, False, 0, -9223372036854775808, "text", [1, None, {"x": True}]):
            spec = oracle()
            spec["assertions"][0]["expected"] = value
            self.assertEqual(self.evaluate(record(json.dumps(value)), spec)["status"], "matched")


if __name__ == "__main__":
    unittest.main()
