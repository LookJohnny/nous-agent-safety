"""Synthetic byte-boundary, authorization and privacy regression tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import nous_ledger
import nous_training as training
import nous_training_observer as observer
import nous_training_transport as transport_module
from nous_training_export import record_digest
from nous_training_validate import validate_dataset


def request(text="Return 4 in JSON"):
    return {"model": "fixture", "instructions": "Synthetic task", "input": text}


def response(text='{"answer":4}'):
    return {"id": "resp_fixture", "object": "response", "model": "fixture", "status": "completed",
            "output": [{"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text}]}]}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def oracle(expected=None, scope="final_json_task"):
    return {"schema": "nous.oracle/1", "scope": scope,
            "assertions": [{"kind": "exact_json", "expected": {"answer": 4} if expected is None else expected}]}


class Observer(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nous-observer-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        for patch in (mock.patch.object(training, "HOME", self.home),
                      mock.patch.object(nous_ledger, "_key", return_value=b"synthetic-training-key-32-bytes!!")):
            patch.start()
            self.addCleanup(patch.stop)

    def begin(self, raw=None, spec=None, **options):
        return observer.begin_request("openai-responses", raw or encoded(request()),
            project=str(self.project), session="fixture-session", oracle=oracle() if spec is None else spec,
            synthetic=True, **options)

    def stored(self, report):
        return json.loads((self.home / "training/records" / (report["id"] + ".json")).read_bytes())

    def finish(self, ticket, value=None):
        return observer.finish_request(ticket, encoded(response() if value is None else value))

    def test_disabled_does_not_parse_or_create_state(self):
        self.assertIsNone(self.begin(b"invalid"))
        self.assertFalse(self.home.exists())

    def test_opt_in_revoke_regrant_inflight_cannot_write(self):
        training.set_project(str(self.project), True)
        ticket = self.begin()
        training.set_project(str(self.project), False)
        training.set_project(str(self.project), True)
        self.assertEqual(self.finish(ticket), {"status": "disabled"})
        self.assertFalse((self.home / "training/records").exists())

    def test_manual_global_revoke_also_cancels(self):
        ticket = self.begin(manual=True)
        training.revoke_all()
        self.assertEqual(self.finish(ticket), {"status": "disabled"})

    def test_project_replacement_prevents_write(self):
        ticket = self.begin(manual=True)
        self.project.rename(self.root / "previous-project")
        self.project.mkdir()
        with self.assertRaisesRegex(training.TrainingError, "project_changed"):
            self.finish(ticket)
        self.assertFalse((self.home / "training/records").exists())

    def test_oracle_frozen_and_manual_not_continuous(self):
        spec = oracle()
        ticket = self.begin(spec=spec, manual=True)
        spec["assertions"][0]["expected"] = {"answer": 999}
        result = self.finish(ticket)
        self.assertEqual(result["verification"]["status"], "matched")
        record = self.stored(result)
        self.assertEqual(record["provenance"]["request_sha256"], hashlib.sha256(ticket.request_bytes).hexdigest())
        self.assertEqual(record["evidence"][0]["record_digest"], record_digest(record))
        self.assertEqual(record["evidence"][0]["oracle_registration"], "before_dispatch")
        self.assertFalse(training._allowed(training.project_id(str(self.project)), "capture"))
        self.assertTrue(training._allowed_record(record))
        self.assertNotIn('"answer": 999', json.dumps(result))
        with self.assertRaisesRegex(observer.ObserverError, "ticket_consumed"):
            self.finish(ticket)

    def test_task_verifier_does_not_clear_content(self):
        result = self.finish(self.begin(manual=True))
        first = training.export(self.root / "unreviewed", profile="request-sft-v1")
        self.assertEqual(first["files"]["sft.jsonl"]["records"], 0)
        training.review(result["id"], "fixture-content-review", "cleared", purpose="content")
        second = training.export(self.root / "reviewed", profile="request-sft-v1")
        self.assertEqual(second["files"]["sft.jsonl"]["records"], 1)
        self.assertTrue(validate_dataset(self.root / "reviewed")["valid"])
        self.assertEqual(self.stored(result)["privacy"]["request_content_review_required"], True)

    def test_mismatch_survives_positive_human_annotations(self):
        result = self.finish(self.begin(manual=True), response('{"answer":5}'))
        self.assertEqual(result["verification"]["status"], "mismatched")
        training.review(result["id"], "reviewer", "accepted")
        training.review(result["id"], "reviewer", "cleared", purpose="content")
        manifest = training.export(self.root / "negative", profile="request-sft-v1")
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_format_only_is_not_outcome_evidence(self):
        spec = {"schema": "nous.oracle/1", "scope": "final_json_format", "assertions": [
            {"kind": "json_schema", "schema": {"type": "object"}}]}
        result = self.finish(self.begin(spec=spec, manual=True))
        self.assertEqual(self.stored(result)["evidence"][0]["kind"], "result_check")
        training.review(result["id"], "reviewer", "cleared", purpose="content")
        manifest = training.export(self.root / "format", profile="request-sft-v1")
        self.assertEqual(manifest["files"]["sft.jsonl"]["records"], 0)

    def test_redacted_final_cannot_pass_and_no_raw_or_oracle_persisted(self):
        private = "fixture-person@synthetic.invalid"
        ticket = self.begin(encoded(request(private)), spec=oracle({"answer": private}), manual=True)
        result = self.finish(ticket, response(json.dumps({"answer": private})))
        self.assertEqual(result["verification"]["status"], "indeterminate")
        self.assertEqual(self.stored(result)["evidence"], [])
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(private.encode(), path.read_bytes())
        self.assertNotIn(private, repr(ticket))

    def test_revocation_during_scrubbing_wins(self):
        ticket = self.begin(manual=True)
        original = training.scrub_record
        def revoke(record, project):
            value = original(record, project)
            training.revoke_all()
            return value
        with mock.patch.object(training, "scrub_record", side_effect=revoke):
            self.assertEqual(self.finish(ticket), {"status": "disabled"})
        self.assertFalse((self.home / "training/records").exists())

    def test_changed_prompt_does_not_verify_unrelated_scrubbed_task(self):
        result = self.finish(self.begin(encoded(request("fixture-person@synthetic.invalid")), manual=True))
        self.assertEqual(result["verification"]["status"], "indeterminate")
        self.assertIn("request_redacted_or_changed", result["verification"]["codes"])

    def test_sensitive_schema_literals_and_metadata_do_not_persist(self):
        req = request()
        req["metadata"] = {"api_key": "short-private"}
        req["client_metadata"] = {"api_key": "short-client-secret"}
        req["tools"] = [{"type": "function", "name": "fixture", "parameters": {"type": "object",
            "properties": {"password": {"type": "string", "default": "hunter2", "examples": ["shortsecret"],
                                           "enum": ["secretoption"]}}}}]
        result = self.finish(self.begin(encoded(req), manual=True))
        record = self.stored(result)
        self.assertFalse(record["quality"]["complete"])
        self.assertIn("contract_redacted", record["quality"]["issues"])
        self.assertEqual(result["verification"]["status"], "indeterminate")
        for path in self.home.rglob("*"):
            if path.is_file():
                for secret in (b"short-private", b"short-client-secret", b"hunter2", b"shortsecret", b"secretoption"):
                    self.assertNotIn(secret, path.read_bytes())

    def test_inline_additional_tools_receive_same_schema_scrubbing(self):
        req = request()
        tool = {"type": "function", "name": "fixture", "parameters": {"type": "object", "properties": {
            "password": {"type": "string", "default": "shortsecret", "examples": ["hunter2"]}}}}
        req["input"] = [{"type": "additional_tools", "role": "developer", "tools": [tool]},
                        {"role": "user", "content": "Return 4 in JSON"}]
        result = self.finish(self.begin(encoded(req), manual=True))
        record = self.stored(result)
        self.assertIn("contract_redacted", record["quality"]["issues"])
        self.assertFalse(record["quality"]["complete"])
        for secret in ("shortsecret", "hunter2"):
            self.assertNotIn(secret, json.dumps(record))

    def test_fifo_and_symlink_request_files_rejected_without_open(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        link = self.root / "link"
        link.symlink_to(fifo)
        for path in (fifo, link):
            with self.assertRaisesRegex(observer.ObserverError, "request_file_not_regular"):
                observer.capture_file("openai-responses", path, project=str(self.project), session="fifo")

    def test_mask_failure_never_writes_body_or_import_grant(self):
        ticket = self.begin(manual=True)
        with mock.patch.object(training, "scrub_record", side_effect=RuntimeError("sensitive-error")):
            with self.assertRaises(RuntimeError):
                self.finish(ticket)
        self.assertEqual(training._config()["projects"], {})
        self.assertFalse((self.home / "training/records").exists())

    def test_wrapper_preserves_exact_dispatch_and_response(self):
        raw = encoded(request())
        transport = {"body": encoded(response()), "content_type": "application/json", "status": 200}
        send = mock.Mock(return_value=transport)
        observed, report = observer.observe_request("openai-responses", raw, send,
            project=str(self.project), session="one", oracle=oracle(), manual=True, synthetic=True)
        self.assertIs(observed, transport)
        self.assertIs(send.call_args.args[0], raw)
        self.assertEqual(report["status"], "collected")
        send.assert_called_once()

    def test_collection_failure_does_not_interrupt_dispatch(self):
        transport = {"body": b"provider-private-response", "content_type": "application/json"}
        send = mock.Mock(return_value=transport)
        observed, report = observer.observe_request("openai-responses", b"invalid", send,
            project=str(self.project), session="one", manual=True)
        self.assertIs(observed, transport)
        self.assertEqual(report["status"], "collection_failed")
        self.assertNotIn("private", str(report))
        send.assert_called_once_with(b"invalid")

    def test_duplicate_keys_and_binding_mismatch_fail_before_capture(self):
        with self.assertRaisesRegex(observer.ObserverError, "invalid_json"):
            self.begin(b'{"model":"a","model":"b","input":"c"}', manual=True)
        spec = oracle()
        spec["request_sha256"] = "0" * 64
        with self.assertRaisesRegex(observer.ObserverError, "binding_mismatch"):
            self.begin(spec=spec, manual=True)

    def test_explicit_file_dispatch_and_oracle_preflight(self):
        incoming, rules = self.root / "request.json", self.root / "oracle.json"
        incoming.write_bytes(encoded(request()))
        rules.write_bytes(encoded(oracle()))
        returned = {"body": encoded(response()), "content_type": "application/json", "status": 200}
        with mock.patch.object(transport_module, "post_json", return_value=returned) as send:
            report = observer.capture_file("openai-responses", incoming, project=str(self.project),
                session="file", oracle_file=rules, endpoint="http://127.0.0.1:5678/v1/responses", allow_loopback=True,
                synthetic=True)
        self.assertEqual(report["verification"]["status"], "matched")
        self.assertEqual(send.call_args.args[1], incoming.read_bytes())
        rules.write_bytes(b'{"schema":"nous.oracle/1","schema":"duplicate"}')
        with mock.patch.object(transport_module, "post_json") as send:
            with self.assertRaisesRegex(observer.ObserverError, "oracle_file_invalid_json"):
                observer.capture_file("openai-responses", incoming, project=str(self.project), session="bad", oracle_file=rules)
            send.assert_not_called()

    def test_no_credentials_to_loopback_or_wrong_provider(self):
        with mock.patch.object(transport_module, "post_json") as send:
            with self.assertRaisesRegex(observer.ObserverError, "loopback_credentials_not_allowed"):
                observer.capture_file("openai-responses", "missing", project=str(self.project), session="key",
                    endpoint="http://127.0.0.1:5678/v1/responses", allow_loopback=True, api_key_env="FIXTURE_KEY")
            with self.assertRaisesRegex(observer.ObserverError, "provider_endpoint_mismatch"):
                observer.capture_file("openai-responses", "missing", project=str(self.project), session="key",
                    endpoint="https://api.anthropic.com/v1/messages", api_key_env="FIXTURE_KEY")
            send.assert_not_called()

    def test_http_error_is_not_captured(self):
        incoming = self.root / "request.json"
        incoming.write_bytes(encoded(request()))
        returned = {"body": b"private-provider-error", "content_type": "application/json", "status": 401}
        with mock.patch.object(transport_module, "post_json", return_value=returned):
            report = observer.capture_file("openai-responses", incoming, project=str(self.project), session="error")
        self.assertEqual(report["http_status"], 401)
        self.assertNotIn("private", str(report))
        self.assertFalse((self.home / "training/records").exists())

    def test_digest_bindings_with_phone_substring_are_preserved_but_prose_is_scrubbed(self):
        report = self.finish(self.begin(manual=True))
        record = self.stored(report)
        # This is a syntactically valid SHA256-like fixture whose decimal run
        # triggered false-positive phone masking in review/export previously.
        digest = "a" * 20 + "13800138000" + "b" * 33
        keys = ("record_digest", "oracle_sha256", "request_sha256", "result_digest", "environment_digest")
        record["evidence"] = [{**dict.fromkeys(keys, digest), "note": "phone 13800138000",
                               "invalid_digest": "13800138000"}]
        value = training.scrub_record(record)["evidence"][0]
        self.assertTrue(all(value[key] == digest for key in keys))
        self.assertNotIn("13800138000", value["note"])
        self.assertNotIn("13800138000", value["invalid_digest"])
        record["evidence"][0]["record_digest"] = "13800138000"
        self.assertNotIn("13800138000", training.scrub_record(record)["evidence"][0]["record_digest"])


if __name__ == "__main__":
    unittest.main()
