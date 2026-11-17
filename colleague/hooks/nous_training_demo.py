#!/usr/bin/env python3
"""Offline smoke test using handwritten synthetic fixtures, never user history."""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile


def run(out, profile="trl"):
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("demo_output_exists")
    fixtures = Path(__file__).resolve().parents[1] / "examples" / "training"
    with tempfile.TemporaryDirectory(prefix="nous-synthetic-demo-") as sandbox:
        # This entrypoint runs in its own process, so module-level paths cannot
        # retain a previous caller's real Nous/ledger directory.
        os.environ["NOUS_HOME"] = sandbox
        os.environ["NOUS_LEDGER_HOME"] = str(Path(sandbox) / "ledger")
        import nous_training as training
        from nous_training_export import preference_digest
        from nous_training_validate import validate_dataset
        project = Path(sandbox) / "synthetic-project"
        project.mkdir()
        if profile == "codemode-events-v1":
            from nous_training_export import record_digest
            result = training.collect(fixtures / "codemode.jsonl", str(project), agent="codex", manual=True)
            path = training._root() / "records" / (result["id"] + ".json")
            record = json.loads(path.read_text())
            assert record["quality"] == {"complete": True, "issues": [], "missing": []}
            events = record["protocol"]["events"]
            calls = [event["payload"] for event in events if event["type"] == "custom_tool_call"]
            assert len(calls) == 1 and calls[0]["input"] == 'text(await tools.mcp__fixture__lookup({key:"beta"}));\n'
            assert events[-1]["payload"]["content"] == [{"type": "output_text", "text": "The synthetic value is 37."}]
            record["provenance"].update(source="synthetic_fixture", synthetic=True)
            digest = record_digest(record)
            record["evidence"] = [{"kind": kind, "result": "passed", "verifier": "handwritten-codemode-fixture-v1",
                                   "record_digest": digest} for kind in ("verifier", "content_verifier")]
            training._atomic(path, record)
            manifest = training.export(str(out), profile=profile)
            manifest.update(synthetic=True, notice="Handwritten CodeMode fixture; no model run, user history, or measured model quality.")
            training._atomic(out / "manifest.json", manifest)
            return {"synthetic": True, "profile": profile, "out": str(out), "files": manifest["files"],
                    "validation": validate_dataset(out)}
        context = json.loads((fixtures / "tools.json").read_text())
        records = []
        for agent in ("claude", "codex"):
            result = training.collect(fixtures / (agent + ".jsonl"), str(project),
                                      agent=agent, context=context, manual=True)
            path = training._root() / "records" / (result["id"] + ".json")
            record = json.loads(path.read_text())
            expected = "The synthetic fixture has no missing entry; beta has value 3."
            assert record["messages"][-1]["content"] == [{"type": "text", "text": expected}]
            record["provenance"].update(source="synthetic_fixture", synthetic=True)
            training._atomic(path, record)
            training.review(result["id"], "synthetic-fixture-author", "accepted")
            records.append(json.loads(path.read_text()))
        source = records[0]
        pair = {"id": "synthetic-explicit-preference", "prompt": copy.deepcopy(source["messages"][:-1]),
                "chosen": copy.deepcopy(source["messages"][-1:]),
                "rejected": [{"role": "assistant", "content": [{"type": "text", "text":
                              "Synthetic incorrect answer: missing exists and beta has value 99."}]}],
                "tools": source["tools"], "provenance": {"source_record_id": source["id"],
                                                         "project_id": source["provenance"]["project_id"]}}
        pair["evidence"] = [{"kind": "human_preference", "result": "chosen", "reviewer": "synthetic-fixture-author",
                             "record_digest": preference_digest(pair)}]
        manifest = training.export(str(out), preferences=[pair])
        manifest["synthetic"] = True
        manifest["notice"] = "Handwritten demo data only; no real user history or production task outcomes."
        training._atomic(out / "manifest.json", manifest)
        report = validate_dataset(out)
        return {"synthetic": True, "out": str(out), "files": manifest["files"], "validation": report}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--profile", choices=("trl", "codemode-events-v1"), default="trl")
    args = parser.parse_args(argv)
    try:
        result = run(args.out, args.profile)
    except Exception as exc:
        # Avoid leaking a path or fixture body through a third-party exception.
        print(json.dumps({"error": "demo_failed", "type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["validation"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
