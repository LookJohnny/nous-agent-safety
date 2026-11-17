#!/usr/bin/env python3
"""Offline synthetic request/verification/export acceptance; no model or HTTP."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def run(out):
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("demo_output_exists")
    with tempfile.TemporaryDirectory(prefix="nous-request-demo-") as folder:
        os.environ["NOUS_HOME"] = folder
        os.environ["NOUS_LEDGER_HOME"] = str(Path(folder) / "ledger")
        import nous_training as training
        from nous_training_observer import observe_request
        from nous_training_validate import validate_dataset
        project = Path(folder) / "synthetic-project"
        project.mkdir()
        counts = {"matched": 0, "mismatched": 0, "indeterminate": 0}
        for index in range(30):
            provider = "openai-responses" if index % 2 == 0 else "anthropic-messages"
            total = index + 2
            prompt = f"Synthetic arithmetic fixture {index}: return the sum of {index} and 2 as a JSON object with key answer."
            answer = json.dumps({"answer": total + (1 if index in (24, 25) else 0)}, separators=(",", ":"))
            if provider == "openai-responses":
                request = {"model": "synthetic-fixture", "instructions": "Return JSON only.", "input": prompt}
                response = {"id": f"response_{index}", "model": "synthetic-fixture", "object": "response", "status": "completed",
                    "output": [{"id": f"message_{index}", "type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": answer}]}]}
            else:
                request = {"model": "synthetic-fixture", "max_tokens": 100, "system": "Return JSON only.",
                           "messages": [{"role": "user", "content": prompt}]}
                response = {"id": f"message_{index}", "model": "synthetic-fixture", "type": "message", "role": "assistant",
                            "stop_reason": "end_turn", "content": [{"type": "text", "text": answer}]}
            raw = training._json(request).encode()
            response_bytes = training._json(response).encode()
            scope = "final_json_format" if index in (26, 27) else "final_json_task"
            oracle = {"schema": "nous.oracle/1", "scope": scope,
                      "assertions": [{"kind": "exact_json", "expected": {"answer": total}}]}
            def send(actual):
                if actual != raw:
                    raise ValueError("fixture_dispatch_changed")
                return {"body": response_bytes, "content_type": "application/json", "status": 200}
            returned, result = observe_request(provider, raw, send, project=str(project), session=f"fixture-{index // 2}",
                                               oracle=oracle, synthetic=True, manual=True)
            if result.get("status") != "collected" or returned["body"] != response_bytes:
                raise ValueError("fixture_capture_failed")
            counts[result["verification"]["status"]] += 1
            # Explicit fixture-only content check. This is never a general content
            # verifier for user requests: the complete inputs/outputs are fixed here.
            path = training._root() / "records" / (result["id"] + ".json")
            record = json.loads(path.read_bytes())
            if record["exchange"]["request"] != request or record["exchange"]["response"] != response:
                raise ValueError("fixture_content_changed")
            if index < 28:
                training.review(result["id"], "synthetic-fixture-content-check", "cleared", purpose="content")
        manifest = training.export(out, profile="request-sft-v1")
        if manifest["files"]["sft.jsonl"]["records"] != 24:
            raise ValueError("fixture_eligibility_mismatch")
        manifest.update(synthetic=True, notice="Handwritten request/response fixtures only; no model inference, user data, or model-quality measurement.")
        training._atomic(out / "manifest.json", manifest)
        return {"synthetic": True, "profile": "request-sft-v1", "out": str(out), "files": manifest["files"],
                "fixture_count": 30, "verification_counts": counts, "model_inference": False,
                "validation": validate_dataset(out)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--profile", choices=("request-sft-v1",), default="request-sft-v1")
    args = parser.parse_args(argv)
    try:
        report = run(args.out)
    except Exception as error:
        print(json.dumps({"error": "request_demo_failed", "type": type(error).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["validation"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
