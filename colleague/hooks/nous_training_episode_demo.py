"""Synthetic multi-request dataset exercising selective tool-step supervision.

No real provider, credentials, user history, file tools, or network are used.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
import nous_training_episode as episode
from nous_training_request import normalize_exchange, request_messages

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _fixture(provider="openai-responses", *, parallel=False, error=False):
    schema = {"type": "object", "properties": {"key": {"type": "string"}},
              "required": ["key"], "additionalProperties": False}
    if provider == "openai-responses":
        request = {"model": "synthetic", "tools": [{"type": "function", "name": "lookup", "parameters": schema}],
                   "input": [{"type": "message", "role": "user", "content": "Return a JSON answer using the synthetic lookup."}]}
    else:
        request = {"model": "synthetic", "max_tokens": 256, "tools": [{"name": "lookup", "input_schema": schema}],
                   "messages": [{"role": "user", "content": "Return a JSON answer using the synthetic lookup."}]}
    steps = []
    for index in range(3):
        final = index == 2
        call_ids = ["call-" + str(index)] + (["parallel-call"] if parallel and index == 0 else [])
        if provider == "openai-responses":
            output = ([{"type": "message", "id": "message-final", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"answer":4}'}]}] if final else [
                       {"type": "function_call", "id": "item-" + call, "status": "completed", "call_id": call,
                        "name": "lookup", "arguments": '{"key":"alpha"}'} for call in call_ids])
            response = {"id": "response-" + str(index), "object": "response", "model": "synthetic",
                        "status": "completed", "output": output}
        else:
            content = ([{"type": "text", "text": '{"answer":4}'}] if final else [
                       {"type": "tool_use", "id": call, "name": "lookup", "input": {"key": "alpha"}} for call in call_ids])
            response = {"id": "response-" + str(index), "type": "message", "role": "assistant", "model": "synthetic",
                        "stop_reason": "end_turn" if final else "tool_use", "content": content}
        exchange = normalize_exchange(provider, request, response, schema="nous.request-exchange/2")
        steps.append({"index": index, "exchange": exchange,
            "provenance": {"project_id": "synthetic-project", "session_id": "synthetic-session", "session_group_id": "synthetic-group",
                           "request_sha256": digest(request), "response_sha256": digest(response),
                           "request_content_sha256": digest(request), "response_content_sha256": digest(response)},
            "quality": {"complete": True, "issues": [], "missing": []},
            "privacy": {"scrubbed": True, "tool_history_checked": True, "scrubber_version": "synthetic/1", "counts": {}}})
        request = copy.deepcopy(request)
        if provider == "openai-responses":
            request["input"].extend(copy.deepcopy(output))
            request["input"].extend({"type": "function_call_output", "call_id": call, "output": '{"value":4}'}
                                    for call in reversed(call_ids))
        else:
            request["messages"].append({"role": "assistant", "content": copy.deepcopy(content)})
            request["messages"].append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call, "content": "synthetic failure" if error and index == 0 else '{"value":4}',
                 **({"is_error": True} if error and index == 0 else {})} for call in reversed(call_ids)]})
    return {"schema": episode.SCHEMA, "id": "synthetic-episode", "supervision_policy": episode.POLICY,
            "episode": {"schema": episode.EPISODE_SCHEMA, "provider": provider, "steps": steps},
            "messages": request_messages(steps[-1]["exchange"]), "quality": {"complete": True, "issues": [], "missing": []},
            "privacy": {"scrubbed": True, "tool_history_checked": True, "scrubber_version": "synthetic/1", "counts": {}},
            "provenance": {"source": "request_episode_observer", "project_id": "synthetic-project", "session_id": "synthetic-session",
                           "session_group_id": "synthetic-group", "request_sha256": steps[0]["provenance"]["request_sha256"]},
            "evidence": [], "step_evidence": []}



def run(out):
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("demo_output_must_be_new")
    out.mkdir(mode=0o700)
    # Explicit demo redirects task-specific state only, never HOME/CODEX_HOME.
    os.environ["NOUS_HOME"] = str(out / "private")
    os.environ["NOUS_LEDGER_HOME"] = str(out / "private/ledger")
    import nous_training as training
    training.HOME = out / "private"
    import nous_training_episode_capture as capture
    from nous_training_validate import validate_dataset
    project = out / "project"
    project.mkdir()
    reports = []
    for provider in ("openai-responses", "anthropic-messages"):
        for scenario in ("reviewed", "unreviewed_steps", "rejected_step", "wrong_task", "content_unreviewed"):
            value = _fixture(provider, parallel=scenario == "reviewed", error=True)
            for step in value["episode"]["steps"]:
                for side in ("request", "response"):
                    step["exchange"][side]["model"] = "synthetic-" + scenario
            oracle = {"schema": "nous.oracle/1", "scope": "final_json_task",
                      "assertions": [{"kind": "exact_json", "expected": {"answer": 5 if scenario == "wrong_task" else 4}}]}
            handle = capture.begin_episode(provider, project=str(project), session=provider + scenario,
                                           oracle=oracle, manual=True, synthetic=True)
            for step in value["episode"]["steps"]:
                ex = step["exchange"]
                ticket = capture.begin_step(handle, json.dumps(ex["request"]).encode())
                capture.finish_step(handle, ticket, json.dumps(ex["response"]).encode())
            report = capture.finish_episode(handle)
            if scenario != "content_unreviewed":
                training.review(report["id"], "synthetic-content-fixture", "cleared", purpose="content")
            if scenario != "unreviewed_steps":
                for index in (0, 1):
                    training.review_step(report["id"], index, "synthetic-step-fixture",
                                         "rejected" if scenario == "rejected_step" and index == 0 else "accepted")
            reports.append({"provider": provider, "scenario": scenario, "id": report["id"], "steps": report["steps"],
                            "verification": report["verification"]["status"]})
    training.export(out / "dataset", profile=episode.PROFILE)
    manifest_path = out / "dataset/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(synthetic=True, notice="Handwritten native multi-request fixtures only; no real model inference or user data.")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    result = validate_dataset(out / "dataset")
    report = {"schema": "nous.episode-demo/1", "synthetic": True, "real_model": False,
              "profile": episode.PROFILE, "scenarios": reports, "validation": result}
    (out / "demo-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--profile", choices=[episode.PROFILE], default=episode.PROFILE)
    args = parser.parse_args()
    report = run(args.out)
    print(json.dumps({"synthetic": True, "valid": report["validation"]["valid"], "counts": report["validation"]["counters"]}))
    return 0 if report["validation"]["valid"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
