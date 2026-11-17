"""Install, inspect and remove collection-only hooks without changing authorization.

Only this module's marked command handlers are replaced. Platform settings, safety
hooks and unrelated handlers are preserved. All diagnostics are read-only and avoid
session history, secrets, platform processes and network access.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time

MARKER = "nous-training-v1"
HOOK = Path(__file__).resolve().with_name("nous_training_hook.py")
AGENTS = ("claude", "codex")
MAX_CONFIG_BYTES = 4 * 1024 * 1024
WARNINGS = ["platform_hook_trust_not_verified", "stop_only_interrupts_and_errors_may_need_manual_import",
            "transcript_flush_gaps_may_need_manual_import"]
RUNTIME_FILES = ("nous_training_hook.py", "nous_training_setup.py", "nous_training.py",
                 "nous_training_demo.py", "nous_training_export.py", "nous_training_schema.py",
                 "nous_training_validate.py", "nous_transcript.py", "nous_mask.py",
                 "nous_privacy.py", "nous_ledger.py", "nous_codemode_protocol.py",
                 "nous_codemode_privacy.py", "nous_codemode_training.py", "nous_training_split.py",
                 "nous_training_health.py", "nous_training_audit.py", "nous_training_context.py", "nous_training_diagnose.py")


class SetupError(ValueError):
    """A static, content-free diagnostic code."""


def _home(home):
    return Path(os.path.realpath(home or os.environ.get("NOUS_HOME") or Path.home() / ".nous-colleague"))


def _agents(agent):
    if agent == "both":
        return AGENTS
    if agent not in AGENTS:
        raise SetupError("unsupported_agent")
    return (agent,)


def _path(agent, override):
    if override is not None:
        return Path(os.path.abspath(override))
    if agent == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def _paths(agent, config_path):
    agents = _agents(agent)
    if agent == "both" and config_path is not None and not isinstance(config_path, dict):
        raise SetupError("both_requires_config_path_mapping")
    if isinstance(config_path, dict) and any(name not in config_path for name in agents):
        raise SetupError("config_path_mapping_incomplete")
    result = {name: _path(name, config_path.get(name) if isinstance(config_path, dict) else config_path)
              for name in agents}
    if len(set(map(str, result.values()))) != len(result):
        raise SetupError("platform_config_paths_must_differ")
    return result


def _load(path):
    if path.is_symlink():
        raise SetupError("config_symlink_not_supported")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}, None
    except OSError:
        raise SetupError("config_unreadable") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise SetupError("config_too_large")
    def unique_keys(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SetupError("invalid_config_json") from None
    if not isinstance(data, dict):
        raise SetupError("invalid_config_shape")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("invalid_hooks_shape")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise SetupError("invalid_hook_groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise SetupError("invalid_hook_group")
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise SetupError("invalid_hook_handler")
    return data, raw


def command(agent, home=None, ledger_home=None):
    if agent not in AGENTS:
        raise SetupError("unsupported_agent")
    data_home = _home(home)
    return shlex.join(["/usr/bin/env", "NOUS_HOME=" + str(data_home),
                       "NOUS_LEDGER_HOME=" + str(Path(ledger_home).resolve() if ledger_home else data_home / "ledger"),
                       str(Path(sys.executable).resolve()), str(HOOK), "--agent", agent,
                       "--managed-marker", MARKER])


def _managed_args(handler):
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return None
    try:
        args = shlex.split(handler["command"])
    except ValueError:
        return None
    # Match the generated argument layout, not a substring in arbitrary shell text.
    if (len(args) >= 9 and args[0] == "/usr/bin/env"
            and args[1].startswith("NOUS_HOME=") and args[2].startswith("NOUS_LEDGER_HOME=")
            and os.path.isabs(args[1].split("=", 1)[1]) and os.path.isabs(args[2].split("=", 1)[1])
            and os.path.isabs(args[3]) and os.path.isabs(args[4])
            and Path(args[4]).name == HOOK.name and args[5] == "--agent"
            and args[6] in AGENTS and args[7:9] == ["--managed-marker", MARKER]):
        return args
    return None


def _owned(handler):
    """Only an unchanged generated handler is safe to replace or remove.

    Script/interpreter paths may be from an older installation. User-added
    handler fields, edited timeout or shell syntax are never silently adopted.
    """
    args = _managed_args(handler)
    return bool(args and len(args) == 9 and set(handler) == {"type", "command", "timeout"}
                and type(handler["timeout"]) is int and handler["timeout"] == 20
                and handler["command"] == shlex.join(args))


def _in_scope(args, agent, home, ledger_home=None):
    expected = shlex.split(command(agent, home, ledger_home))
    return bool(args and args[1:3] == expected[1:3] and args[6] == agent)


def _handler(agent, home, ledger_home=None):
    return {"type": "command", "command": command(agent, home, ledger_home), "timeout": 20}


def merge_settings(settings, agent, home=None, ledger_home=None):
    """Upgrade unchanged handlers in place, preserving group behavior and order.

    If a scoped managed command has been edited, preserve it and let doctor report
    the conflict; installing a second unconditional hook could duplicate capture.
    """
    result = copy.deepcopy(settings)
    found = False
    expected_args = shlex.split(command(agent, home, ledger_home))
    for groups in result.get("hooks", {}).values():
        for group in groups:
            for index, handler in enumerate(group["hooks"]):
                args = _managed_args(handler)
                if args and args[1] == expected_args[1] and args[6] == agent:
                    found = True
                    if _owned(handler) and _in_scope(args, agent, home, ledger_home):
                        group["hooks"][index] = _handler(agent, home, ledger_home)
    if not found:
        result.setdefault("hooks", {}).setdefault("Stop", []).append({"hooks": [_handler(agent, home, ledger_home)]})
    return result


def remove_settings(settings, agent, home=None, ledger_home=None):
    """Pure removal: return (settings, count), retaining user group metadata."""
    result = copy.deepcopy(settings)
    removed = 0
    for event, groups in list(result.get("hooks", {}).items()):
        kept = []
        for group in groups:
            handlers = []
            for handler in group["hooks"]:
                if _owned(handler) and _in_scope(_managed_args(handler), agent, home, ledger_home):
                    removed += 1
                else:
                    handlers.append(handler)
            # Only a now-empty generated wrapper is removed. An originally empty
            # group or custom matcher/metadata belongs to the user's settings.
            if handlers or not group["hooks"] or set(group) != {"hooks"}:
                kept.append({**group, "hooks": handlers})
        if kept:
            result["hooks"][event] = kept
        elif groups:
            del result["hooks"][event]
    return result, removed


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".nous-training.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(path, settings, original):
    # Platform editors do not necessarily honor our advisory lock. Detect edits
    # since the read before replacing a file; never merge from a stale snapshot.
    _, current = _load(path)
    if current != original:
        raise SetupError("config_changed_during_update")
    data = (json.dumps(settings, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    backup_path = None
    if original is not None:
        fd, backup_path = tempfile.mkstemp(prefix=path.name + ".bak-nous-training-" + str(time.time_ns()) + "-",
                                            dir=path.parent)
        with os.fdopen(fd, "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
    fd, temporary = tempfile.mkstemp(prefix=".nous-training-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup_path


def _project_state(project, home):
    if not project:
        raise SetupError("project_required")
    pid = "p_" + hashlib.sha256(os.path.realpath(project).encode()).hexdigest()[:24]
    path = home / "training" / "config.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return pid, False, ["training_config_too_large"]
        cfg = json.loads(raw)
        state = cfg.get("projects", {}).get(pid, {})
        if not isinstance(state, dict):
            raise ValueError()
        return pid, state.get("capture") is True, []
    except FileNotFoundError:
        return pid, False, []
    except (OSError, ValueError, AttributeError):
        return pid, False, ["training_config_unreadable"]


def doctor(agent, project, home=None, *, config_path=None, ledger_home=None):
    """Read configuration only. Configured does not imply platform trust or execution."""
    paths, data_home = _paths(agent, config_path), _home(home)
    pid, enabled, state_issues = _project_state(project, data_home)
    reports = []
    for name, path in paths.items():
        issues, count, configured, legacy = [], 0, False, False
        owned_count, modified_count, foreign_count, storage_conflicts = 0, 0, 0, 0
        configured_missing = set()
        try:
            settings, _ = _load(path)
            expected = _handler(name, data_home, ledger_home)
            expected_args = shlex.split(expected["command"])
            if settings.get("disableAllHooks") is True:
                issues.append("platform_hooks_disabled")
            for event, groups in settings.get("hooks", {}).items():
                for group in groups:
                    for handler in group["hooks"]:
                        args = _managed_args(handler)
                        if args:
                            if not _in_scope(args, name, data_home, ledger_home):
                                foreign_count += 1
                                issues.append("managed_hook_agent_mismatch" if args[6] != name else "managed_hook_storage_mismatch")
                                if args[6] == name and args[1] == expected_args[1]:
                                    storage_conflicts += 1
                            else:
                                count += 1
                                owned_count += int(_owned(handler))
                                if not _owned(handler):
                                    modified_count += 1
                                    issues.append("modified_managed_handler_preserved")
                                if args[4] != str(HOOK):
                                    issues.append("outdated_hook_script_path")
                                if not Path(args[4]).is_file():
                                    issues.append("configured_hook_script_missing")
                                missing = [f for f in RUNTIME_FILES if not Path(args[4]).with_name(f).is_file()]
                                if missing:
                                    configured_missing.update(missing)
                                    issues.append("configured_runtime_incomplete")
                                if args[3] != str(Path(sys.executable).resolve()):
                                    issues.append("outdated_python_path")
                                if not Path(args[3]).is_file() or not os.access(args[3], os.X_OK):
                                    issues.append("configured_python_unavailable")
                                if event != "Stop":
                                    issues.append("managed_hook_event_mismatch")
                                if "matcher" in group and group["matcher"] != "":
                                    issues.append("managed_hook_filter_preserved")
                                if set(group) - {"hooks", "matcher"}:
                                    issues.append("managed_hook_group_metadata_unverified")
                                if (event == "Stop" and _owned(handler) and handler == expected and group.get("matcher", "") == ""
                                        and not (set(group) - {"hooks", "matcher"}) and not missing
                                        and Path(args[3]).is_file() and os.access(args[3], os.X_OK)):
                                    configured = True
                        if "nous_hook.py" in str(handler.get("command", "")):
                            legacy = True
            if not configured:
                issues.append("independent_stop_hook_not_configured")
            if count > 1:
                issues.append("duplicate_managed_hooks")
            if legacy:
                issues.append("legacy_safety_hook_may_also_collect")
        except SetupError as error:
            issues.append(str(error))
        issues = sorted(set(issues))
        reports.append({"agent": name, "config_path": str(path), "configured": configured,
                        "hook_count": count, "legacy_hook_present": legacy, "trust": "unknown",
                        "owned_hook_count": owned_count, "modified_hook_count": modified_count,
                        "other_scope_hook_count": foreign_count,
                        "storage_conflict_count": storage_conflicts,
                        "missing_configured_runtime_files": sorted(configured_missing),
                        "platform_disabled": "platform_hooks_disabled" in issues, "issues": issues})
    missing = [name for name in RUNTIME_FILES if not HOOK.with_name(name).is_file()]
    warnings = list(WARNINGS) + state_issues
    git_available = shutil.which("git") is not None
    if not git_available:
        warnings.append("git_unavailable_file_tool_content_will_be_excluded")
    if missing:
        warnings.append("missing_runtime_files")
    if not enabled:
        warnings.append("project_capture_disabled")
    try:
        from nous_training_health import read_health
        health = read_health(data_home, pid)
    except Exception:
        health = {"status": "unavailable", "projects": {}}
        warnings.append("collection_health_unavailable")
    installed = all(r["configured"] for r in reports) and not missing
    configuration_ready = installed and all(not r["platform_disabled"] and r["hook_count"] == 1 for r in reports)
    return {"agent": agent, "installed": installed, "configuration_ready": configuration_ready,
            "capture_enabled": enabled, "runtime_present": not missing, "missing_runtime_files": missing,
            "project_id": pid, "training_home": str(data_home), "agents": reports,
            "capabilities": {"git_available": git_available, "file_exclusion_verified": False},
            "collection_health": health,
            "execution_verified": False, "warnings": warnings}


def setup(agent, project, home=None, *, config_path=None, ledger_home=None):
    """Install independent hooks; project authorization remains the caller's explicit step."""
    paths, data_home = _paths(agent, config_path), _home(home)
    if not project or not Path(project).is_dir():
        raise SetupError("project_directory_required")
    # Preflight both platforms before modifying either file.
    for path in paths.values():
        _load(path)
    if not HOOK.is_file():
        raise SetupError("hook_script_missing")
    if any(not HOOK.with_name(name).is_file() for name in RUNTIME_FILES):
        raise SetupError("runtime_files_missing")
    changes = {}
    for name, path in paths.items():
        with _lock(path):
            settings, original = _load(path)
            updated = merge_settings(settings, name, data_home, ledger_home)
            changed = updated != settings
            backup_path = _write(path, updated, original) if changed else None
        changes[name] = {"changed": changed, "backup_path": backup_path}
    report = doctor(agent, project, data_home, config_path=config_path, ledger_home=ledger_home)
    report["changed"] = any(item["changed"] for item in changes.values())
    for item in report["agents"]:
        item.update(changes[item["agent"]])
    return report


def uninstall(agent, project, home=None, *, config_path=None, ledger_home=None):
    """Remove exact scoped managed handlers; never revoke consent or erase data.

    The hook is a platform configuration entry shared by projects using this
    training home. ``project`` is used only for the unchanged authorization report.
    Missing platform configuration is a successful no-op, even after runtime
    files have moved or been removed.
    """
    paths, data_home = _paths(agent, config_path), _home(home)
    if not project:
        raise SetupError("project_required")
    preflight = {name: _load(path) for name, path in paths.items()}
    changes = {}
    for name, path in paths.items():
        settings, original = preflight[name]
        _, preview_removed = remove_settings(settings, name, data_home, ledger_home)
        # No unnecessary parent directories, lock files, backups or blank config.
        if original is None or not preview_removed:
            changes[name] = {"changed": False, "backup_path": None, "removed_handlers": 0}
            continue
        with _lock(path):
            settings, original = _load(path)
            updated, removed = remove_settings(settings, name, data_home, ledger_home)
            changed = updated != settings
            backup_path = _write(path, updated, original) if changed else None
        changes[name] = {"changed": changed, "backup_path": backup_path, "removed_handlers": removed}
    report = doctor(agent, project, data_home, config_path=config_path, ledger_home=ledger_home)
    report.update(action="uninstall", changed=any(item["changed"] for item in changes.values()),
                  removed_handlers=sum(item["removed_handlers"] for item in changes.values()),
                  uninstalled=all(item["hook_count"] == 0 and item["storage_conflict_count"] == 0 for item in report["agents"]),
                  authorization_changed=False, data_deleted=False, trust_changed=False)
    for item in report["agents"]:
        item.update(changes[item["agent"]])
    return report
