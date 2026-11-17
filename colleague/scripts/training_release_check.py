#!/usr/bin/env python3
"""Offline checks for the macOS developer-alpha training collector.

No installation, platform history, live hooks, external network, or user data are used.
The default checks source tests; --smoke-only checks the minimal unpacked package.
"""
import sys

# sys is built in. Refuse unsafe CLI startup before importing any shadowable
# standard-library name. This cannot authenticate an already modified checker.
if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("nous-release: isolated_startup_required; use python -I -S\n")
    raise SystemExit(2)

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile

RUNTIME_MODULES = (
    "nous_training", "nous_training_hook", "nous_training_setup", "nous_training_demo",
    "nous_training_export", "nous_training_validate", "nous_training_schema", "nous_transcript",
    "nous_mask", "nous_privacy", "nous_ledger",
    "nous_codemode_protocol", "nous_codemode_privacy", "nous_codemode_training", "nous_training_split", "nous_training_health", "nous_training_audit",
    "nous_training_context", "nous_training_diagnose",
    "nous_training_request", "nous_training_verify", "nous_training_transport",
    "nous_training_observer", "nous_training_request_demo",
    "nous_training_history", "nous_training_history_scan", "nous_training_history_read",
    "nous_training_history_identity", "nous_training_history_privacy",
    "nous_training_claude_replay", "nous_training_anchor_recovery", "nous_training_live_bridge",
    "nous_training_episode", "nous_training_episode_capture", "nous_training_request_privacy", "nous_training_episode_demo",
)
RUNTIME_FILES = tuple("hooks/" + name + ".py" for name in RUNTIME_MODULES)
FIXTURE_FILES = ("examples/training/claude.jsonl", "examples/training/codex.jsonl", "examples/training/codemode.jsonl",
                 "examples/training/tools.json", "examples/training/README.md")
LIVE_FIXTURE_FILES = (
    "examples/live_tasks/claude/project/TASK.md", "examples/live_tasks/claude/project/solution.py",
    "examples/live_tasks/claude/project/test_public.py", "examples/live_tasks/claude/project/public_cases.json",
    "examples/live_tasks/codex/project/TASK.md", "examples/live_tasks/codex/project/solution.py",
    "examples/live_tasks/codex/project/test_public.py", "examples/live_tasks/codex/project/public_cases.json",
    "examples/live_tasks/private/oracle.py", "examples/live_tasks/private/tasks.json",
    "examples/live_tasks/private/baseline-manifests.json",
)
RELEASE_FILES = RUNTIME_FILES + FIXTURE_FILES + LIVE_FIXTURE_FILES + (
    "TRAINING_VERSION", "docs/TRAINING.md", "docs/TRAINING_RELEASE.md",
    "scripts/training_release_check.py", "scripts/training_build.py",
    "scripts/training_trainer_smoke.py", "requirements-training-smoke.txt",
    "docs/CODEMODE.md", "docs/TRAINING_TRAINER.md", "docs/TRAINING_SPLIT.md",
    "docs/TRAINING_HEALTH.md", "docs/TRAINING_LIFECYCLE.md", "docs/TRAINING_CODEX_RUNTIME.md",
    "docs/TRAINING_AUDIT.md",
    "docs/TRAINING_CONTEXT.md", "docs/TRAINING_DIAGNOSE.md",
    "docs/TRAINING_REQUEST.md", "docs/TRAINING_VERIFY.md",
    "docs/TRAINING_TRANSPORT.md", "docs/TRAINING_OBSERVER.md",
    "docs/TRAINING_EPISODE.md", "docs/TRAINING_EPISODE_CAPTURE.md", "docs/TRAINING_MULTITURN_LIVE.md",
    "docs/TRAINING_STATUS_PHASE7.md", "docs/TRAINING_HISTORY.md",
    "docs/TRAINING_CLAUDE_REPLAY.md", "docs/TRAINING_HISTORY_REPAIR.md",
    "scripts/training_real_multiturn.py", "scripts/training_header_probe.py",
    "scripts/training_live_smoke.py", "scripts/training_multiturn_smoke.py", "scripts/training_request_smoke.py",
    "docs/TRAINING_LIVE_BRIDGE.md", "docs/TRAINING_REAL_MULTITURN.md", "docs/TRAINING_LIVE.md",
)
TARGET_PYTHON = ("3.11", "3.12", "3.13", "3.14")
OPTIONAL_CAPABILITIES = [{"name": "git", "purpose": "repository_file_exclusion_checks",
                          "unavailable_or_unverifiable": "omit_file_tool_content_and_mark_quality_gap",
                          "scope": "git_and_verifiable_repository_required_for_file_tool_content"},
                         {"name":"macos_sandbox_exec", "purpose":"optional_controlled_live_artifact_verification",
                          "requires":"macOS /usr/bin/sandbox-exec plus closed AST and subprocess limits",
                          "unavailable":"artifact_verification_blocked; no model run from this acceptance runner"},
                         {"name":"stdlib_zstd", "purpose":"compressed_historical_transcript_import",
                          "requires":"Python 3.14 compression.zstd", "unavailable":"compressed_sources_reported_unsupported"}]
REQUIRED_TESTS = {"test_training_cli", "test_training_collection", "test_training_export",
                  "test_training_setup", "test_training_validate", "test_training_release", "test_transcript",
                  "test_training_codemode", "test_training_sources", "test_training_split", "test_codemode_protocol",
                  "test_codemode_privacy", "test_codemode_training", "test_training_health", "test_training_recovery", "test_training_audit",
                  "test_training_context", "test_training_diagnose", "test_training_request",
                  "test_training_verify", "test_training_transport", "test_training_observer",
                  "test_training_request_pipeline", "test_training_episode", "test_training_episode_capture",
                  "test_training_request_privacy", "test_training_multiturn_smoke",
                  "test_training_history", "test_training_history_scan", "test_training_history_identity", "test_training_history_read",
                  "test_training_claude_replay", "test_training_anchor_recovery",
                  "test_training_secret_sweep", "test_training_write_operands",
                  "test_training_live_bridge", "test_training_real_multiturn", "test_training_live_smoke"}


def runtime_inventory(root):
    root = Path(root)
    return {"required": list(RUNTIME_FILES),
            "missing": [name for name in RUNTIME_FILES if not (root / name).is_file()],
            "symlinks": [name for name in RUNTIME_FILES if (root / name).is_symlink()]}


def isolated_env(root):
    """Only task-specific NOUS variables are redirected; OS HOME is never changed."""
    root = Path(root)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NOUS_", "PYTHON"))}
    env.update(NOUS_HOME=str(root / "nous"), NOUS_LEDGER_HOME=str(root / "ledger"),
               NOUS_VAULT_INDEX=str(root / "vault.json"), NOUS_VAULT_SERVICE="center.nous.release-test",
               NOUS_MANAGED=str(root / "managed.json"), NOUS_NO_CLIPBOARD="1",
               NOUS_LOCAL_API="http://127.0.0.1:9", PYTHONDONTWRITEBYTECODE="1")
    return env


def _run(root, env, args, *, timeout=120):
    # -S prevents installed third-party modules from satisfying missing dependencies.
    # -I excludes script/cwd and user-site injection before any package imports.
    if args[:2] == ["-m", "unittest"]:
        launch = ("import sys,unittest; sys.path.insert(0,sys.argv[1]); "
                  "unittest.main(module=None,argv=['unittest',*sys.argv[2:]])")
        args = ["-c", launch, str(root), *args[2:]]
    elif args and not args[0].startswith("-"):
        launch = ("import runpy,sys; from pathlib import Path; p=Path(sys.argv[1]).resolve(); "
                  "sys.path.insert(0,str(p.parent)); sys.argv=sys.argv[1:]; "
                  "runpy.run_path(str(p),run_name='__main__')")
        args = ["-c", launch, *args]
    return subprocess.run([sys.executable, "-I", "-S", "-B", *args], cwd=root,
                          env=env, text=True, capture_output=True, timeout=timeout)


def _manifest_integrity(root, *, required=False):
    path = root / "release-manifest.json"
    if path.is_symlink():
        return {"status": "failed", "code": "release_manifest_symlink"}
    if not path.exists():
        if required:
            return {"status": "failed", "code": "package_manifest_required"}
        return {"status": "not_applicable", "reason": "source_checkout"}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entries = manifest["files"]
        if (manifest.get("schema") != "nous.training.release/1" or not isinstance(entries, list)
                or {x["path"] for x in entries} != set(RELEASE_FILES) or len(entries) != len(RELEASE_FILES)):
            return {"status": "failed", "code": "invalid_release_manifest"}
        if (manifest.get("external_python_dependencies") != []
                or manifest.get("optional_runtime_capabilities") != OPTIONAL_CAPABILITIES
                or manifest.get("runtime_files") != list(RUNTIME_FILES)):
            return {"status": "failed", "code": "invalid_release_capability_declaration"}
        expected_files = set(RELEASE_FILES) | {"release-manifest.json"}
        expected_dirs = {str(parent) for name in expected_files for parent in Path(name).parents if str(parent) != "."}
        # Validate the complete tree before executing any runtime. In particular,
        # unlisted hooks/hashlib.py, scripts/json.py and importable directories are
        # not harmless extras: they can shadow standard-library imports.
        for member in root.rglob("*"):
            name = member.relative_to(root).as_posix()
            if member.is_symlink():
                return {"status": "failed", "code": "unlisted_or_linked_package_entry"}
            if member.is_dir():
                if name not in expected_dirs:
                    return {"status": "failed", "code": "unlisted_package_directory"}
            elif not member.is_file() or name not in expected_files:
                return {"status": "failed", "code": "unlisted_package_file"}
        for item in entries:
            file = root / item["path"]
            if (file.is_symlink() or not file.is_file()
                    or any(p.is_symlink() for p in file.parents if p != root and root in p.parents)):
                return {"status": "failed", "code": "release_file_missing_or_symlink"}
            raw = file.read_bytes()
            if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                return {"status": "failed", "code": "release_file_hash_mismatch"}
        version = (root / "TRAINING_VERSION").read_text().strip()
        if manifest.get("version") != version:
            return {"status": "failed", "code": "release_version_mismatch"}
        return {"status": "passed", "checked_files": len(entries)}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "failed", "code": "invalid_release_manifest"}


def check_release(source_root, *, smoke_only=False):
    root = Path(source_root).resolve()
    report = {"schema": "nous.training.release-check/1", "scope": "package-smoke" if smoke_only else "source-tests-and-smoke",
              "release_channel": "developer-alpha", "published": False,
              "host": {"system": platform.system(), "python": platform.python_version()},
              "target_python_versions": list(TARGET_PYTHON),
              "matrix_status": "not_verified_by_this_single_host_run",
              "real_platform_acceptance": "not_run", "checks": []}

    def add(name, status="passed", **detail):
        report["checks"].append({"name": name, "status": status, **detail})

    inventory = runtime_inventory(root)
    integrity = _manifest_integrity(root, required=smoke_only)
    add("release_manifest", **integrity)
    if integrity["status"] == "failed":
        report["passed"] = False
        return report
    if not smoke_only and not all((root / "tests" / (name + ".py")).is_file() for name in REQUIRED_TESTS):
        add("source_tests", "failed", code="source_test_suite_missing")
        report["passed"] = False
        return report
    add("runtime_inventory", "failed" if inventory["missing"] or inventory["symlinks"] else "passed", **inventory)
    if platform.system() not in ("Darwin", "Linux"):
        add("platform", "failed", code="unsupported_platform")
    if sys.version_info < (3, 11):
        add("python", "failed", code="python_3_11_required")
    try:
        version = (root / "TRAINING_VERSION").read_text(encoding="utf-8").strip()
        if version != "0.1.0a9":
            raise ValueError()
        report["version"] = version
        for name in RUNTIME_FILES + ("scripts/training_release_check.py", "scripts/training_build.py"):
            ast.parse((root / name).read_text(encoding="utf-8"), filename=name, feature_version=(3, 11))
        add("python_3_11_syntax")
    except (OSError, ValueError, SyntaxError):
        add("python_3_11_syntax", "failed", code="version_or_source_invalid")
    if any(x["status"] == "failed" for x in report["checks"]):
        report["passed"] = False
        return report

    with tempfile.TemporaryDirectory(prefix="nous-release-check-") as temporary:
        temp = Path(temporary)
        env = isolated_env(temp)
        project = temp / "synthetic-project"
        project.mkdir()
        try:
            code = ("import importlib, json, sys; sys.path.insert(0, sys.argv[1]); "
                    "names=json.loads(sys.argv[2]); [importlib.import_module(n) for n in names]; "
                    "print(json.dumps({'imported':names}))")
            process = _run(root, env, ["-c", code, str(root / "hooks"), json.dumps(RUNTIME_MODULES)])
            add("all_runtime_imports", "passed" if process.returncode == 0 else "failed", returncode=process.returncode)

            process = _run(root, env, [str(root / "hooks/nous_training.py"), "--help"])
            add("cli_help", "passed" if process.returncode == 0 and "validate" in process.stdout else "failed", returncode=process.returncode)

            # Explicit configuration paths ensure doctor cannot inspect live agent settings.
            code = ("import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                    "import nous_training_setup as s; t=Path(sys.argv[2]); "
                    "r=s.doctor('both',str(t/'synthetic-project'),home=t/'nous',ledger_home=t/'ledger',"
                    "config_path={'claude':t/'claude.json','codex':t/'codex.json'}); print(json.dumps(r))")
            process = _run(root, env, ["-c", code, str(root / "hooks"), str(temp)])
            diagnostic = json.loads(process.stdout) if process.returncode == 0 else {}
            valid = process.returncode == 0 and diagnostic.get("runtime_present") is True and diagnostic.get("execution_verified") is False
            add("isolated_doctor", "passed" if valid else "failed", returncode=process.returncode,
                live_platform_execution="not_run", capabilities=diagnostic.get("capabilities", {}))

            if smoke_only:
                add("source_tests", "not_run", reason="explicit_package_smoke_scope")
            else:
                files = sorted([*(root / "tests").glob("test_training*.py"), *(root / "tests").glob("test_codemode*.py")])
                transcript = root / "tests/test_transcript.py"
                if transcript.is_file():
                    files.append(transcript)
                names = ["tests." + file.stem for file in files]
                if not REQUIRED_TESTS <= {file.stem for file in files}:
                    add("source_tests", "failed", code="source_test_suite_missing")
                else:
                    process = _run(root, env, ["-m", "unittest", *names], timeout=300)
                    found = re.search(r"Ran (\d+) tests?", process.stderr)
                    skipped = re.search(r"skipped=(\d+)", process.stderr)
                    add("source_tests", "passed" if process.returncode == 0 else "failed", returncode=process.returncode,
                        modules=names, tests=int(found.group(1)) if found else None,
                        skipped=int(skipped.group(1)) if skipped else 0)
                    if process.returncode:
                        # Test names/tracebacks concern only synthetic fixtures; keep diagnosis in stderr.
                        sys.stderr.write(process.stderr[-16000:])

            demo = temp / "demo-export"
            process = _run(root, env, [str(root / "hooks/nous_training.py"), "demo", "--out", str(demo)])
            payload = json.loads(process.stdout) if process.returncode == 0 else {}
            valid = process.returncode == 0 and payload.get("synthetic") is True and payload.get("validation", {}).get("valid") is True
            add("synthetic_demo", "passed" if valid else "failed", returncode=process.returncode)
            if valid:
                process = _run(root, env, [str(root / "hooks/nous_training.py"), "validate", "--dataset", str(demo)])
                payload = json.loads(process.stdout) if process.returncode == 0 else {}
                add("offline_dataset_validation", "passed" if process.returncode == 0 and payload.get("valid") is True else "failed",
                    returncode=process.returncode, counters=payload.get("counters", {}))
                process = _run(root, env, [str(root / "hooks/nous_training.py"), "audit", "--dataset", str(demo), "--require-training-rows"])
                audit = json.loads(process.stdout) if process.returncode == 0 else {}
                add("offline_dataset_audit", "passed" if process.returncode == 0 and audit.get("structural_valid") is True
                    and audit.get("training_rows_present") is True else "failed",
                    returncode=process.returncode, counts=audit.get("counts", {}))
                process = _run(root, env, [str(root / "hooks/nous_training.py"), "diagnose", "--dataset", str(demo), "--require-training-rows"])
                diagnosis = json.loads(process.stdout) if process.returncode == 0 else {}
                add("offline_dataset_diagnosis", "passed" if process.returncode == 0
                    and diagnosis.get("structural_valid") is True and diagnosis.get("diagnosis_available") is True
                    and diagnosis.get("training_eligibility_not_reassessed") is True else "failed", returncode=process.returncode)
            else:
                add("offline_dataset_validation", "not_run", reason="demo_failed")
                add("offline_dataset_audit", "not_run", reason="demo_failed")
                add("offline_dataset_diagnosis", "not_run", reason="demo_failed")
            # This stamped fixture checks diagnostics, never producer identity or
            # a real runtime. The report must still refuse request completeness.
            source_fixture = temp / "synthetic-context.jsonl"
            fixture_rows = [json.loads(line) for line in (root / "examples/training/codemode.jsonl").read_text().splitlines()]
            fixture_rows[0]["payload"].update(cli_version="0.169.0-alpha.11.1", id="synthetic-release-context")
            source_fixture.write_text("".join(json.dumps(row) + "\n" for row in fixture_rows))
            process = _run(root, env, [str(root / "hooks/nous_training.py"), "inspect-source", "--transcript", str(source_fixture)])
            observation = json.loads(process.stdout) if process.returncode == 0 else {}
            add("synthetic_context_diagnosis", "passed" if process.returncode == 0 and observation.get("valid") is True
                and observation.get("request_context_verified") is False else "failed", returncode=process.returncode,
                actual_request_replay="not_verified")
            codemode_demo = temp / "codemode-demo"
            process = _run(root, env, [str(root / "hooks/nous_training.py"), "demo", "--profile", "codemode-events-v1", "--out", str(codemode_demo)])
            payload = json.loads(process.stdout) if process.returncode == 0 else {}
            valid = (process.returncode == 0 and payload.get("synthetic") is True
                     and payload.get("validation", {}).get("valid") is True
                     and payload.get("files", {}).get("sft.jsonl", {}).get("records", 0) > 0)
            add("synthetic_codemode_demo", "passed" if valid else "failed", returncode=process.returncode)
            request_demo = temp / "request-demo"
            process = _run(root, env, [str(root / "hooks/nous_training.py"), "demo", "--profile", "request-sft-v1", "--out", str(request_demo)])
            payload = json.loads(process.stdout) if process.returncode == 0 else {}
            valid = (process.returncode == 0 and payload.get("synthetic") is True
                     and payload.get("profile") == "request-sft-v1"
                     and payload.get("validation", {}).get("valid") is True
                     and payload.get("files", {}).get("sft.jsonl", {}).get("records", 0) > 0
                     and payload.get("files", {}).get("dpo.jsonl", {}).get("records") == 0)
            add("synthetic_request_demo", "passed" if valid else "failed", returncode=process.returncode,
                live_platform_execution="not_run")
            if valid:
                process = _run(root, env, [str(root / "hooks/nous_training.py"), "validate", "--dataset", str(request_demo)])
                payload = json.loads(process.stdout) if process.returncode == 0 else {}
                add("offline_request_dataset_validation", "passed" if process.returncode == 0
                    and payload.get("valid") is True else "failed", returncode=process.returncode,
                    counters=payload.get("counters", {}))
            else:
                add("offline_request_dataset_validation", "not_run", reason="request_demo_failed")
            episode_demo = temp / "episode-demo"
            process = _run(root, env, [str(root / "hooks/nous_training.py"), "demo", "--profile", "request-episode-sft-v1", "--out", str(episode_demo)])
            payload = json.loads(process.stdout) if process.returncode == 0 else {}
            valid = process.returncode == 0 and payload.get("synthetic") is True and payload.get("valid") is True and payload.get("counts", {}).get("sft") == 12
            add("synthetic_episode_demo", "passed" if valid else "failed", returncode=process.returncode,
                live_platform_execution="not_run", counters=payload.get("counts", {}))
            if valid:
                for command in ("validate", "audit", "diagnose"):
                    process = _run(root, env, [str(root / "hooks/nous_training.py"), command, "--dataset", str(episode_demo / "dataset")])
                    payload = json.loads(process.stdout) if process.returncode == 0 else {}
                    add("offline_episode_" + command, "passed" if process.returncode == 0 and
                        payload.get("valid" if command == "validate" else "structural_valid") is True else "failed",
                        returncode=process.returncode)
            process = _run(root, env, [str(root / "scripts/training_real_multiturn.py"), "--help"])
            add("controlled_live_cli_help", "passed" if process.returncode == 0 else "failed", returncode=process.returncode,
                real_model_execution="not_run_by_offline_core_check")
            process = _run(root, env, [str(root / "scripts/training_trainer_smoke.py"), "--help"])
            add("optional_trainer_cli", "passed" if process.returncode == 0 else "failed", returncode=process.returncode,
                actual_training="not_run_by_offline_core_check")
        except subprocess.TimeoutExpired:
            add("execution", "failed", code="check_timeout")
        except (OSError, ValueError, TypeError):
            add("execution", "failed", code="check_failed")
    report["passed"] = not any(x["status"] == "failed" for x in report["checks"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = check_release(args.source_root, smoke_only=args.smoke_only)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
