#!/usr/bin/env python3
"""Local, opt-in training trajectories. No network or automatic labels.

Explicit import is one-time permission. enable/disable controls future Stop hooks per
project. Files are masked before storage; export rechecks current local permission.
See docs/TRAINING.md for schemas, supported formats and quality limitations.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

HOME = Path(os.environ.get("NOUS_HOME") or Path.home() / ".nous-colleague")
MAX_RECORD_BYTES = 16 * 1024 * 1024
SCRUBBER_VERSION = "training/2+p2.2-m2"


class TrainingError(ValueError):
    """A static reason code that is safe to print without source content."""


def _root():
    return HOME / "training"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".training-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(_json(value) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class _Lock:
    def __enter__(self):
        _root().mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(_root() / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        os.close(self.fd)


class _SessionLock:
    def __init__(self, project, session, *, scope="session"):
        self.name = _hash([scope, project, str(session)])

    def __enter__(self):
        _root().mkdir(mode=0o700, parents=True, exist_ok=True)
        folder = _root() / "locks"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(folder / (self.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        os.close(self.fd)


def _config():
    try:
        value = json.loads((_root() / "config.json").read_text())
        return value if isinstance(value, dict) and isinstance(value.get("projects"), dict) else {"projects": {}}
    except (OSError, ValueError):
        return {"projects": {}}


def project_id(project):
    if not project:
        raise TrainingError("project is required")
    return _project_id_for_path(os.path.realpath(project))


def _project_id_for_path(path):
    return "p_" + hashlib.sha256(path.encode()).hexdigest()[:24]


def _directory_identity(path):
    try:
        value = os.lstat(path)
    except OSError:
        return None
    return (value.st_dev, value.st_ino) if stat.S_ISDIR(value.st_mode) else None


def _bind_project(project):
    """Bind one operation to a canonical directory, never re-resolve its alias."""
    if not project:
        raise TrainingError("project is required")
    path = os.path.realpath(project)
    return (path, _project_id_for_path(path), _directory_identity(path))


def _check_project(binding):
    path, _pid, original = binding
    if original is None:
        raise TrainingError("project_unavailable")
    if _directory_identity(path) != original:
        raise TrainingError("project_changed_during_collection")


def set_project(project, enabled, *, purge=False, one_time=False):
    pid = project_id(project)
    purge_errors = []
    with _Lock():
        cfg = _config()
        generation = cfg["projects"].get(pid, {}).get("revocation_generation", 0) + (0 if enabled else 1)
        cfg["projects"][pid] = {"capture": bool(enabled and not one_time), "export": bool(enabled),
                                "purpose": "local_training", "version": 1, "revocation_generation": generation}
        _atomic(_root() / "config.json", cfg)
        if purge:
            from nous_training_health import purge_health
            if purge_health(HOME, pid).get("status") == "unavailable":
                purge_errors.append("health_purge_failed")
            for path in (_root() / "records").glob("*.json"):
                try:
                    if json.loads(path.read_text()).get("provenance", {}).get("project_id") == pid:
                        path.unlink()
                except (OSError, ValueError, AttributeError):
                    # A corrupt record cannot safely be assigned to a project.
                    # Keep trying other records and report incomplete deletion.
                    purge_errors.append("records_purge_failed")
    if purge_errors:
        raise TrainingError("records_purge_failed" if "records_purge_failed" in purge_errors else "health_purge_failed")
    return pid


def revoke_all(purge=True):
    """Called by the data program's revocation path, including before a future regrant."""
    if not _root().exists():
        return {"status": "revoked", "purge": bool(purge), "cleanup_complete": True, "errors": []}
    purge_errors = []
    with _Lock():
        cfg = _config()
        cfg["revocation_generation"] = cfg.get("revocation_generation", 0) + 1
        for p in cfg["projects"].values():
            p.update(capture=False, export=False, imports=[])
        _atomic(_root() / "config.json", cfg)
        if purge:
            from nous_training_health import purge_health
            if purge_health(HOME).get("status") == "unavailable":
                purge_errors.append("health_purge_failed")
            for path in (_root() / "records").glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    purge_errors.append("records_purge_failed")
    return {"status": "revoked", "purge": bool(purge), "cleanup_complete": not purge_errors,
            "errors": sorted(set(purge_errors))}


def _allowed(pid, scope):
    return _config()["projects"].get(pid, {}).get(scope) is True


def _allowed_record(record):
    state = _config()["projects"].get(record.get("provenance", {}).get("project_id"), {})
    return state.get("export") is True or record.get("id") in state.get("imports", [])


def _revocation_generation(pid):
    cfg = _config()
    return (cfg.get("revocation_generation", 0), cfg["projects"].get(pid, {}).get("revocation_generation", 0))


def _structured_write_operand(record, block, project, write_targets):
    """Recognize native Write operands only after current exclusion checks.

    Relocated targets are an ephemeral caller capability, never canonical data.
    Their exact call/path/body binding and current target are checked here again.
    This changes path interpretation only; the normal content masker still runs.
    """
    import nous_privacy
    inp = block.get("input")
    mapped = write_targets.get(block.get("id")) if write_targets else None
    eligible = (record.get("schema") == "nous.trajectory/1"
                and record.get("agent", {}).get("id") == "claude"
                and block.get("type") == "tool_use" and block.get("name") == "Write"
                and block.get("format") == "function" and isinstance(inp, dict)
                and set(inp) == {"file_path", "content"}
                and type(inp["file_path"]) is str and type(inp["content"]) is str)
    if mapped is not None and not eligible:
        raise TrainingError("write_target_binding_invalid")
    if not eligible or (not project and mapped is None):
        return False, None
    def safe_path(value):
        return (type(value) is str and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                and ".." not in Path(value).parts and os.path.normpath(value) == value)
    def no_links(path, allow_missing=False):
        for part in reversed((path, *path.parents)):
            try:
                if stat.S_ISLNK(os.lstat(part).st_mode):
                    return False
            except FileNotFoundError:
                return allow_missing
        return True
    original = inp["file_path"]
    if not safe_path(original):
        return True, "privacy_exclusion_unverified"
    if nous_privacy.excluded_name(original):
        return True, "excluded_write_operand"
    try:
        if mapped is not None:
            if (type(mapped) is not dict or set(mapped) != {"original_path", "content_sha256", "target_path", "target_project"}
                    or mapped["original_path"] != original or not os.path.isabs(original)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(mapped["content_sha256"]))
                    or hashlib.sha256(inp["content"].encode()).hexdigest() != mapped["content_sha256"]):
                raise TrainingError("write_target_binding_invalid")
            if not no_links(Path(original), allow_missing=True):
                return True, "privacy_exclusion_unverified"
            target_name, project_name = mapped["target_path"], mapped["target_project"]
        else:
            project_name = str(project)
            target_name = original if os.path.isabs(original) else os.path.join(project_name, original)
        if (not safe_path(project_name) or not safe_path(target_name)
                or not os.path.isabs(project_name) or not os.path.isabs(target_name)):
            return True, "privacy_exclusion_unverified"
        root, target = Path(project_name), Path(target_name)
        if (not root.is_dir() or target == root or root not in target.parents
                or not no_links(root) or not no_links(target, allow_missing=mapped is None)):
            return True, "privacy_exclusion_unverified"
        reason = nous_privacy.excluded(str(target), str(root), strict=True)
        if reason:
            return True, reason
        if mapped is not None:
            before = os.lstat(target)
            if not stat.S_ISREG(before.st_mode) or before.st_size > nous_privacy.MAX_BYTES:
                return True, "privacy_exclusion_unverified"
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                opened = os.fstat(fd)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    body = stream.read(nous_privacy.MAX_BYTES + 1)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            sig = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (sig(before) != sig(opened) or sig(opened) != sig(after) or sig(after) != sig(os.lstat(target))
                    or not no_links(target) or len(body) > nous_privacy.MAX_BYTES
                    or hashlib.sha256(body).hexdigest() != mapped["content_sha256"]):
                return True, "privacy_exclusion_unverified"
            reason = nous_privacy.excluded(str(target), str(root), strict=True)
            if reason:
                return True, reason
    except TrainingError:
        raise
    except (OSError, ValueError, TypeError):
        return True, "privacy_exclusion_unverified"
    return True, None


def scrub_record(record, project="", *, _episode_checked=False, _write_targets=None):
    """Mask values without serializing protocol keys/call IDs through the masker.

    Secret masking must succeed before the older PII scrubber is used (that scrubber
    tolerates masking failures). No constant fallback pseudonym key is used here.
    """
    import nous_mask
    import nous_privacy
    import nous_ledger
    key = nous_ledger._key()
    if not isinstance(key, bytes) or len(key) < 16:
        raise TrainingError("privacy key unavailable")
    counts = {}
    changed_type = False

    def clean(value, secret_fields=True):
        nonlocal changed_type
        if isinstance(value, str):
            text = nous_mask.mask(value, env_file=False)
            text, found = nous_privacy.scrub(text, key=key)
            text = re.sub(r"●●●（已隐藏 \d+ 位）", "<REDACTED_SECRET>", text)
            for k, v in found.items():
                counts[k] = counts.get(k, 0) + v
            if text != value:
                counts["changed_values"] = counts.get("changed_values", 0) + 1
            # Local home/project paths are not needed to identify the contributor.
            for prefix, tag in sorted([(str(Path.home()), "<HOME>"), (os.path.realpath(project), "<PROJECT>")]
                                      if project else [(str(Path.home()), "<HOME>")], reverse=True):
                if prefix != "/":
                    text = text.replace(prefix, tag)
            return text
        if isinstance(value, list):
            return [clean(v, secret_fields) for v in value]
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                name = clean(k, False)
                if name in out:
                    raise TrainingError("redacted_key_collision")
                if secret_fields and v and re.fullmatch(r"(?i)(phone|mobile|telephone|email|id[_-]?card|ssn|card[_-]?number)", k):
                    if not isinstance(v, str):
                        changed_type = True
                    out[name] = v if isinstance(v, str) and re.fullmatch(r"<PII_[0-9a-f]{4}>", v) else nous_privacy._tag("PII", str(v), key)
                else:
                    out[name] = ("<SECRET>" if secret_fields and re.fullmatch(r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|private[_-]?key|密码|密钥|口令|令牌)", k)
                                 and v else clean(v, secret_fields))
            return out
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TrainingError("unsupported value")

    result = copy.deepcopy(record)
    if _write_targets is not None:
        if (type(_write_targets) is not dict or not _write_targets or any(type(v) is not dict for v in _write_targets.values())
                or result.get("schema") != "nous.trajectory/1"
                or result.get("agent", {}).get("id") != "claude"):
            raise TrainingError("write_target_binding_invalid")
        calls = [b for m in result.get("messages", []) for b in m.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        if any(type(key) is not str or sum(b.get("id") == key for b in calls) != 1 for key in _write_targets):
            raise TrainingError("write_target_binding_invalid")
    structured_write_checked = False
    if result.get("schema") == "nous.trajectory/4":
        from nous_training_episode import validate_episode
        from nous_training_request import request_messages
        validate_episode(result, require_quality=False)
        for step in result["episode"]["steps"]:
            checked = step["privacy"].get("scrubbed") is True and step["privacy"].get("tool_history_checked") is True
            if not checked and not project:
                raise TrainingError("request_file_exclusion_unverified")
            safe = scrub_record({"schema": "nous.request-step/1", "exchange": step["exchange"],
                                 "quality": step["quality"]}, project, _episode_checked=checked)
            step.update(exchange=safe["exchange"], quality=safe["quality"], privacy=safe["privacy"])
        # Evidence is scrubbed separately; semantic bindings are never re-signed.
        aux = scrub_record({"schema": "nous.trajectory/1", "agent": result.get("agent", {}),
                            "evidence": result.get("evidence", []), "step_evidence": result.get("step_evidence", []),
                            "quality": result["quality"], "messages": []}, project)
        result.update(agent=aux["agent"], evidence=aux["evidence"], step_evidence=aux["step_evidence"])
        result["messages"] = request_messages(result["episode"]["steps"][-1]["exchange"])
        result["privacy"] = {"scrubbed": True, "scrubber_version": "training/4-request-tools+p2.2-m2",
                             "tool_history_checked": all(s["privacy"]["tool_history_checked"] for s in result["episode"]["steps"])}
        validate_episode(result, require_quality=False)
        return result
    if "episode" in result:
        raise TrainingError("unbound_episode_extension")
    excluded_calls = set()
    exclusion_unverified = False
    metadata_changed = False
    if "exchange" in result:
        if result.get("schema") not in ("nous.trajectory/3", "nous.request-step/1"):
            raise TrainingError("unbound_request_extension")
        from nous_training_request import validate_exchange, request_messages
        validate_exchange(result["exchange"])
        expected = "nous.request-exchange/2" if result.get("schema") == "nous.request-step/1" else "nous.request-exchange/1"
        if result["exchange"]["schema"] != expected:
            raise TrainingError("request_exchange_schema_mismatch")
        if expected == "nous.request-exchange/2":
            from nous_training_request_privacy import scrub_tools
            before_tools = result["exchange"]
            result["exchange"] = scrub_tools(before_tools, clean=clean, project=project, previously_checked=_episode_checked)
            if before_tools != result["exchange"]:
                result["quality"]["complete"] = False
                result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"request_tool_content_redacted"})
        # Closed native schemas are not user data dictionaries: a property named
        # email/password must not cause an entire tool schema to become a tag.
        original_exchange = copy.deepcopy(result["exchange"])
        result["exchange"] = clean(result["exchange"], False)
        sensitive = re.compile(r"(?i)^(password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|private[_-]?key|密码|密钥|口令|令牌|phone|mobile|telephone|email|id[_-]?card|ssn|card[_-]?number)$")
        def schema_values(value, private=False):
            if not isinstance(value, dict):
                return value
            safe = {}
            for key, child in value.items():
                if key in {"default", "examples", "enum", "const"}:
                    if not private:
                        safe[key] = clean(child, True)
                elif key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}:
                    safe[key] = {name: schema_values(sub, private or bool(sensitive.fullmatch(name)))
                                 for name, sub in child.items()}
                elif key in {"items", "additionalProperties", "contains", "not", "if", "then", "else"}:
                    safe[key] = schema_values(child, private)
                elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
                    safe[key] = [schema_values(sub, private) for sub in child]
                else:
                    safe[key] = child
            return safe
        def contracts(tools):
            for tool in tools:
                for field in ("parameters", "input_schema"):
                    if field in tool:
                        tool[field] = schema_values(tool[field])
                if "tools" in tool:
                    contracts(tool["tools"])
        for side in ("request", "response"):
            body = result["exchange"][side]
            for field in ("metadata", "client_metadata"):
                if field in body:
                    body[field] = clean(body[field], True)
            contracts(body.get("tools", []))
            if body.get("tools") != original_exchange[side].get("tools"):
                result["quality"]["complete"] = False
                result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"contract_redacted"})
        inputs = result["exchange"]["request"].get("input")
        if isinstance(inputs, list):
            original_inputs = original_exchange["request"]["input"]
            for item, original in zip(inputs, original_inputs):
                if item.get("type") == "additional_tools":
                    contracts(item["tools"])
                    if item != original:
                        result["quality"]["complete"] = False
                        result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"contract_redacted"})
        validate_exchange(result["exchange"])
        result["messages"] = request_messages(result["exchange"])
    elif result.get("schema") == "nous.trajectory/3":
        raise TrainingError("request_exchange_required")

    def identifier(value):
        nonlocal metadata_changed
        if value is None:
            return None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) and clean(value) == value:
            return value
        metadata_changed = True
        return "redacted_" + _hash(value)[:24]

    if "protocol" in result:
        if result.get("schema") != "nous.trajectory/2":
            raise TrainingError("unbound_protocol_extension")
        from nous_codemode_privacy import scrub_protocol
        protocol, protocol_issues, blocked, needs_review = scrub_protocol(
            result["protocol"], clean=clean, identifier=identifier, project=project)
        result["protocol"] = protocol
        excluded_calls.update(blocked)
        if protocol_issues:
            result["quality"]["complete"] = False
            result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | set(protocol_issues))
    else:
        needs_review = False
        if result.get("schema") == "nous.trajectory/2":
            raise TrainingError("codemode_protocol_required")
    if any(key in result for key in ("events", "context", "tool_contracts")):
        raise TrainingError("unbound_protocol_extension")

    for msg in result.get("messages", []):
        for field in ("id", "parent_id", "agent_id", "tool_call_id"):
            if field in msg:
                msg[field] = identifier(msg[field])
        stamp = msg.get("ts")
        if stamp is not None and not ((isinstance(stamp, (int, float)) and 0 < stamp < 32503680000)
                or (isinstance(stamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)", stamp))):
            msg.pop("ts", None)
            metadata_changed = True
        for field in ("phase", "channel"):
            if field in msg and msg[field] not in ("final", "final_answer", "commentary", "partial_answer", "summary"):
                msg.pop(field)
                metadata_changed = True
        if not isinstance(msg.get("content"), list):
            continue
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            for field in ("id", "tool_use_id", "name"):
                if field in block:
                    block[field] = identifier(block[field])
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            candidates = []
            if isinstance(inp, dict):
                candidates = [v for k, v in inp.items() if k in ("file_path", "path", "filename", "target_file") and isinstance(v, str)]
            structured, write_reason = _structured_write_operand(result, block, project, _write_targets)
            structured_write_checked |= structured and write_reason is None
            exclusion_reasons = ([write_reason] if structured else [nous_privacy.excluded(os.path.join(project, p) if not os.path.isabs(p) else p,
                                                       project, strict=True)
                                 for p in candidates] if project else [])
            file_excluded = any(exclusion_reasons)
            exclusion_unverified |= "privacy_exclusion_unverified" in exclusion_reasons
            # Recognize explicit sensitive file operands in shell/custom inputs too.
            raw = _json({"file_path": inp["file_path"]}) if structured else _json(inp)
            candidates.extend(re.findall(r"[^\s\"'\\]+", raw))
            if block.get("id") in excluded_calls or file_excluded or any(nous_privacy.excluded_name(p) for p in candidates):
                excluded_calls.add(block.get("id"))
                block["input"] = {"omitted": "excluded_file"}
    for msg in result.get("messages", []):
        # Protocol IDs, types and roles stay intact; content values get scrubbed.
        if msg.get("role") == "tool" and msg.get("tool_call_id") in excluded_calls:
            # Canonical callers may use a role=tool message rather than a
            # tool_result block. Its identifier was normalized in the first pass;
            # remove the entire result regardless of its original content shape.
            msg["content"] = "[excluded file content omitted]"
        if isinstance(msg.get("content"), str):
            msg["content"] = clean(msg["content"])
            continue
        for block in msg.get("content", []):
            if block.get("type") == "tool_result" and block.get("tool_use_id") in excluded_calls:
                block["content"] = "[excluded file content omitted]"
            for field in ("text", "input", "content"):
                if field in block:
                    block[field] = clean(block[field])
        if msg.get("model"):
            msg["model"] = clean(msg["model"])
    if "tools" in result:
        result["tools"] = clean(result["tools"], False)
    if "protocol" in result and excluded_calls:
        from nous_codemode_privacy import omit_calls
        result["protocol"] = omit_calls(result["protocol"], excluded_calls)
    result["agent"] = clean(result.get("agent", {}))
    # Cryptographic bindings are structured metadata, not phone/card text.
    # Generic PII matching can otherwise corrupt a valid SHA256 by chance when
    # its hex string contains eleven consecutive decimal digits. Exempt only
    # named full-length digest fields; all narrative values still get scrubbed.
    digest_fields = {"record_digest", "oracle_sha256", "request_sha256", "result_digest", "environment_digest", "episode_digest", "step_digest"}
    def evidence_value(entry):
        if not isinstance(entry, dict):
            return clean(entry, False)
        safe = {}
        for key, value in entry.items():
            name = clean(key, False)
            if name in safe:
                raise TrainingError("redacted_key_collision")
            safe[name] = (value if key in digest_fields and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                          else clean(value, False))
        return safe
    evidence = result.get("evidence", [])
    result["evidence"] = [evidence_value(entry) for entry in evidence] if isinstance(evidence, list) else clean(evidence, False)
    if "step_evidence" in result:
        result["step_evidence"] = [evidence_value(entry) for entry in result["step_evidence"]]
    if excluded_calls:
        result["quality"]["complete"] = False
        result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"excluded_file_content"})
    if exclusion_unverified:
        result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"privacy_exclusion_unverified"})
    if changed_type:
        result["quality"]["complete"] = False
        result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"pii_type_changed"})
    if metadata_changed:
        result["quality"]["complete"] = False
        result["quality"]["issues"] = sorted(set(result["quality"].get("issues", [])) | {"redacted_metadata"})
    result["privacy"] = {"scrubbed": True, "scrubber_version":
                         "training/3-request+p2.2-m2" if result.get("schema") == "nous.trajectory/3" else SCRUBBER_VERSION,
                         "counts": counts}
    if structured_write_checked and result.get("schema") == "nous.trajectory/1":
        result["privacy"]["scrubber_version"] = "training/2-write-operands+p2.2-m2"
    if result.get("schema") in ("nous.trajectory/3", "nous.request-step/1"):
        # Rebuild the UI mirror from the scrubbed native snapshot. Training uses
        # the native representation, never this role-normalized mirror.
        from nous_training_request import request_messages
        result["messages"] = request_messages(result["exchange"])
        result["privacy"]["request_content_review_required"] = True
        if result.get("schema") == "nous.request-step/1":
            result["privacy"]["tool_history_checked"] = True
    if "protocol" in result:
        result["privacy"]["custom_content_review_required"] = needs_review
        result["privacy"]["codemode_content_review_required"] = True
    # Reject oversized or non-JSON values; never silently cut a training example.
    if len(_json(result).encode()) > MAX_RECORD_BYTES:
        raise TrainingError("record_too_large")
    return result


def build_record(transcript, project, *, agent="auto", session=None, context=None, last_reply=None, _source_path=None,
                 _expected_session=None, _project_binding=None, _snapshot=None):
    from nous_transcript import read_transcript_bytes, parse_transcript_bytes, TranscriptReadError, _check_bytes
    binding = _project_binding if _project_binding is not None else _bind_project(project)
    project, pid, _ = binding
    _check_project(binding)
    # Resolve an explicit alias once, then bind the read, both parsers, and source
    # identity to that same path. Raw bytes remain in memory and are never spooled.
    source = _source_path if _source_path is not None else os.path.realpath(transcript)
    try:
        snapshot = read_transcript_bytes(source) if _snapshot is None else _snapshot
        if _snapshot is not None:
            _check_bytes(snapshot)
    except TranscriptReadError as exc:
        raise TrainingError(str(exc)) from None
    _check_project(binding)
    parsed = parse_transcript_bytes(snapshot, agent=agent)
    if (_expected_session is not None and parsed.get("source_session_id") is not None
            and parsed["source_session_id"] != _expected_session):
        raise TrainingError("transcript_session_mismatch")
    messages = parsed["messages"]
    quality = copy.deepcopy(parsed["quality"])
    protocol = None
    if parsed.get("agent") == "codex":
        from nous_codemode_protocol import parse_protocol_bytes
        candidate = parse_protocol_bytes(snapshot)
        if any(e.get("type") in {"custom_tool_call", "additional_tools", "configuration_update", "world_state"}
               for e in candidate.get("events", [])):
            protocol = candidate
            quality = copy.deepcopy(candidate["quality"])
            for issue, missing in {"mixed_sessions": "session_context", "invalid_session_identity": "session_context",
                                   "partial_tail": "visible_content"}.items():
                if issue in parsed["quality"]["issues"]:
                    quality["complete"] = False
                    quality["issues"] = sorted(set(quality["issues"]) | {issue})
                    quality["missing"] = sorted(set(quality["missing"]) | {missing})
    if last_reply:
        # Stop can arrive before the final reply is flushed. Do not invent its place
        # in a tool sequence: mark missing instead, and let a later import recover it.
        tail = messages[-1] if messages else {}
        text = "\n".join(b.get("text", "") for b in tail.get("content", []) if b.get("type") == "text")
        if tail.get("role") != "assistant" or text.strip() != last_reply.strip():
            quality["complete"] = False
            quality["issues"] = sorted(set(quality.get("issues", [])) | {"stop_reply_not_flushed"})
    if not messages:
        raise TrainingError("no_supported_messages")
    sid = hashlib.sha256(str(session or source).encode()).hexdigest()[:24]
    transcript_id = _hash(["transcript", source])[:24]
    source_session = parsed.get("source_session_id")
    session_group = _hash(["native_session", source_session])[:24] if source_session else transcript_id
    record = {"schema": "nous.trajectory/1", "id": "t_" + _hash([pid, sid])[:32],
              "agent": {"id": parsed.get("agent") or agent, "version": parsed.get("version"), "model": parsed.get("model")},
              "messages": messages, "quality": quality, "evidence": [],
              "provenance": {"source": "local_transcript", "session_id": sid, "session_group_id": session_group,
                             "source_transcript_id": transcript_id, "project_id": pid,
                             "line_offset": parsed.get("offset"), "captured_at": int(time.time())}}
    if protocol is not None:
        record.update(schema="nous.trajectory/2", protocol=protocol)
        if context:
            raise TrainingError("codemode_context_requires_ordered_source")
    if context:
        if not isinstance(context, dict) or not isinstance(context.get("tools", []), list):
            raise TrainingError("context must contain a tools array")
        if "tools" in context:
            record["tools"] = context["tools"]
            record["quality"]["missing"] = [m for m in quality.get("missing", []) if m != "tool_definitions"]
        record["provenance"]["tools_source"] = "operator_supplied_context"
    record = scrub_record(record, project)
    _check_project(binding)
    record["provenance"]["source_sha256"] = _hash(record["messages"])
    return record


def collect(transcript, project, *, agent="auto", session=None, context=None, last_reply=None, manual=False,
            _expected_generation=None, _source_path=None, _expected_session=None, _project_binding=None):
    binding = _project_binding if _project_binding is not None else _bind_project(project)
    project, pid, _ = binding
    if not manual and not _allowed(pid, "capture"):
        return {"status": "disabled"}
    # Collection and recovery share both source and record locks. The former
    # covers differing import aliases; the latter covers copied paths using the
    # same canonical session. Revocation can still proceed while parsing.
    source = _source_path if _source_path is not None else os.path.realpath(transcript)
    sid = hashlib.sha256(str(session or source).encode()).hexdigest()[:24]
    rid = "t_" + _hash([pid, sid])[:32]
    with _SessionLock(pid, source, scope="source"), _SessionLock(pid, rid, scope="record"):
        if not manual and not _allowed(pid, "capture"):
            return {"status": "disabled"}
        generation = _revocation_generation(pid)
        if _expected_generation is not None and generation != _expected_generation:
            return {"status": "disabled"}
        _check_project(binding)
        record = build_record(source, project, agent=agent, session=session, context=context, last_reply=last_reply,
                              _source_path=source, _expected_session=_expected_session, _project_binding=binding)
        from nous_training_export import record_digest
        with _Lock():
            # Revocation while a slow transcript is read must take effect before writing.
            if _revocation_generation(pid) != generation:
                return {"status": "disabled"}
            if not manual and not _allowed(pid, "capture"):
                return {"status": "disabled"}
            path = _root() / "records" / (record["id"] + ".json")
            try:
                old = json.loads(path.read_text())
            except (OSError, ValueError):
                old = {}
            if (old.get("schema") == record.get("schema") == "nous.trajectory/1"
                    and old.get("messages") == record.get("messages")
                    and old.get("tools") and not record.get("tools")):
                record["tools"] = old["tools"]
                record["provenance"]["tools_source"] = old["provenance"].get("tools_source")
                record["quality"]["missing"] = [x for x in record["quality"]["missing"] if x != "tool_definitions"]
            if record_digest(old) == record_digest(record):
                record["evidence"] = old.get("evidence", [])
            _check_project(binding)
            if manual:
                cfg = _config()
                state = cfg["projects"].setdefault(pid, {"capture": False, "export": False})
                state["imports"] = sorted(set(state.get("imports", [])) | {record["id"]})
                state.update(purpose="local_training", version=1)
                record["provenance"]["authorization"] = "one_shot_import"
                _atomic(_root() / "config.json", cfg)
            else:
                record["provenance"]["authorization"] = "project_opt_in"
            _atomic(path, record)
        return {"status": "collected", "id": record["id"], "messages": len(record["messages"]), "quality": record["quality"]}


def _health_begin(pid, agent):
    """Consent and health mutation share the revocation lock; no source content."""
    with _Lock():
        if not _allowed(pid, "capture"):
            return None, None
        generation = _revocation_generation(pid)
        try:
            from nous_training_health import record_attempt
            entry = record_attempt(HOME, pid, agent, authorized=True)
            return entry.get("attempt_id"), generation
        except Exception:
            return None, generation


def _health_finish(pid, agent, attempt, generation, result, error_code=None):
    if not attempt:
        return
    try:
        with _Lock():
            if not _allowed(pid, "capture") or _revocation_generation(pid) != generation:
                return
            from nous_training_health import record_result
            record_result(HOME, pid, agent, attempt, authorized=True,
                          status="collected" if result.get("status") == "collected" else "failed",
                          record_id=result.get("id"), error_code=error_code)
    except Exception:
        # Diagnostics are best-effort and must not break the collection path.
        pass


def capture_stop(event, data):
    """Hook integration. Returns safe metadata only; never interrupts the user's task."""
    attempt, generation, pid = None, None, None
    agent = event.get("agent") if event.get("agent") in ("claude", "codex") else "unknown"
    try:
        if event.get("event") != "Stop" or not data.get("transcript_path") or not data.get("cwd"):
            return None
        binding = _bind_project(data["cwd"])
        project, pid, _ = binding
        if not _allowed(pid, "capture"):
            return None
        attempt, generation = _health_begin(pid, agent)
        if generation is None:
            return None
        source = os.path.realpath(data["transcript_path"])
        # One bounded retry covers a writer finishing the same Stop's snapshot.
        # Other read/privacy failures are not retried and no missing text is made up.
        for index in range(2):
            try:
                result = collect(source, project, agent=event.get("agent", "auto"),
                                 session=data.get("session_id"), last_reply=data.get("last_assistant_message"),
                                 _expected_generation=generation, _source_path=source,
                                 _expected_session=data.get("session_id"), _project_binding=binding)
            except TrainingError as exc:
                if index == 0 and str(exc) == "transcript_changed_during_read":
                    time.sleep(.1)
                    continue
                raise
            if index == 0 and set(result.get("quality", {}).get("issues", [])) & {"partial_tail", "stop_reply_not_flushed"}:
                time.sleep(.1)
                continue
            break
        _health_finish(pid, agent, attempt, generation, result)
        return result
    except Exception as exc:
        # No exception text: parsers and masking libraries may include source text.
        code = ("transcript_missing" if isinstance(exc, FileNotFoundError)
                else "transcript_unreadable" if isinstance(exc, PermissionError)
                else str(exc) if isinstance(exc, TrainingError) and str(exc) in {
                    "transcript_changed_during_read", "transcript_unreadable", "transcript_too_large",
                    "transcript_line_too_large", "transcript_too_many_lines", "transcript_must_be_regular_file",
                    "transcript_session_mismatch", "project_unavailable",
                    "project_changed_during_collection"} else "collection_failed")
        _health_finish(pid, agent, attempt, generation, {"status": "collection_failed"}, code)
        return {"status": "collection_failed"}


def recover(record_id, transcript, project, *, agent="auto", context=None):
    """Refresh one authorized record from its explicit original source, never scan history.

    Compare-and-swap protects concurrent collection/review. Revoked authorization
    cannot be restored by recovery; a new explicit import is a separate operation.
    """
    if not isinstance(record_id, str) or not re.fullmatch(r"t_[0-9a-f]{32}", record_id):
        raise TrainingError("invalid_record_id")
    binding = _bind_project(project)
    project, pid, _ = binding
    path = _root() / "records" / (record_id + ".json")
    if not path.is_file() or path.is_symlink():
        raise TrainingError("recovery_record_unavailable")
    source = os.path.realpath(transcript)
    with _SessionLock(pid, source, scope="source"), _SessionLock(pid, record_id, scope="record"):
        return _recover_locked(record_id, source, project, pid, path, agent=agent, context=context,
                               project_binding=binding)


def _recover_locked(record_id, transcript, project, pid, path, *, agent, context, project_binding):
    from nous_training_export import record_digest
    with _Lock():
        raw = path.read_bytes()
        if len(raw) > MAX_RECORD_BYTES:
            raise TrainingError("record_too_large")
        old = json.loads(raw)
        provenance = old.get("provenance", {})
        if old.get("id") != record_id or provenance.get("project_id") != pid:
            raise TrainingError("recovery_project_mismatch")
        if not _allowed_record(old):
            raise TrainingError("recovery_authorization_revoked")
        source_id = _hash(["transcript", transcript])[:24]
        if not provenance.get("session_id") or provenance.get("source_transcript_id") != source_id:
            raise TrainingError("recovery_source_mismatch")
        generation = _revocation_generation(pid)
    record = build_record(transcript, project, agent=agent, context=context, _source_path=transcript,
                          _project_binding=project_binding)
    if record.get("agent", {}).get("id") != old.get("agent", {}).get("id"):
        raise TrainingError("recovery_agent_mismatch")
    old_group = provenance.get("session_group_id")
    new_group = record["provenance"].get("session_group_id")
    if old_group not in (None, source_id, new_group):
        raise TrainingError("recovery_session_mismatch")
    old_offset = provenance.get("line_offset")
    new_offset = record["provenance"].get("line_offset")
    if type(old_offset) is int and type(new_offset) is int and new_offset < old_offset:
        raise TrainingError("recovery_source_regressed")
    record["id"] = record_id
    record["provenance"]["session_id"] = provenance["session_id"]
    record["provenance"]["authorization"] = provenance.get("authorization", "project_opt_in")
    if provenance.get("synthetic") is True:
        record["provenance"]["synthetic"] = True
        if provenance.get("source") in ("synthetic_fixture", "live_synthetic_fixture"):
            record["provenance"]["source"] = provenance["source"]
    if (old.get("schema") == record.get("schema") == "nous.trajectory/1"
            and old.get("messages") == record.get("messages")
            and old.get("tools") and not record.get("tools")):
        record["tools"] = old["tools"]
        record["provenance"]["tools_source"] = provenance.get("tools_source")
        record["quality"]["missing"] = [m for m in record["quality"]["missing"] if m != "tool_definitions"]
    unchanged = record_digest(old) == record_digest(record)
    if unchanged:
        record["evidence"] = old.get("evidence", [])
    with _Lock():
        if _revocation_generation(pid) != generation or not _allowed_record(old):
            raise TrainingError("recovery_authorization_revoked")
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise TrainingError("record_changed_during_recovery")
        _check_project(project_binding)
        _atomic(path, record)
    return {"status": "recovered", "id": record_id, "content_changed": not unchanged,
            "reviews_preserved": len(record["evidence"]), "quality": record["quality"]}


def review(record_id, reviewer, result, *, purpose="outcome"):
    from nous_training_export import record_digest
    allowed = ("cleared", "rejected") if purpose == "content" else ("accepted", "rejected")
    if purpose not in ("outcome", "content") or result not in allowed:
        raise TrainingError("invalid_review_result")
    if not re.fullmatch(r"t_[0-9a-f]{32}", record_id) or not reviewer.strip():
        raise TrainingError("valid record id and reviewer are required")
    with _Lock():
        path = _root() / "records" / (record_id + ".json")
        record = json.loads(path.read_text())
        if purpose == "content" and record.get("schema") not in ("nous.trajectory/2", "nous.trajectory/3", "nous.trajectory/4"):
            raise TrainingError("content_review_requires_codemode")
        if not _allowed_record(record):
            raise TrainingError("project is disabled")
        # Reviewer is a user-selected pseudonym, never read from the OS account.
        entry = {"kind": "content_review" if purpose == "content" else "human_review", "result": result, "reviewer": reviewer,
                 "record_digest": record_digest(record), "at": int(time.time())}
        record["evidence"].append(entry)
        record = scrub_record(record)
        _atomic(path, record)
    return {"id": record_id, "result": result, "purpose": purpose}


def review_step(record_id, step, reviewer, result):
    from nous_training_episode import validate_episode, episode_digest, step_digest
    if (result not in ("accepted", "rejected") or type(step) is not int
            or not re.fullmatch(r"t_[0-9a-f]{32}", record_id) or not reviewer.strip()):
        raise TrainingError("invalid_step_review")
    with _Lock():
        path = _root() / "records" / (record_id + ".json")
        record = json.loads(path.read_text())
        if not _allowed_record(record):
            raise TrainingError("project is disabled")
        validate_episode(record, require_quality=False)
        entry = {"kind": "step_review", "result": result, "reviewer": reviewer, "step": step,
                 "episode_digest": episode_digest(record), "step_digest": step_digest(record, step), "at": int(time.time())}
        record.setdefault("step_evidence", []).append(entry)
        record = scrub_record(record)
        _atomic(path, record)
    return {"id": record_id, "step": step, "result": result}


def export(out, *, profile="trl", preferences=()):
    from nous_training_export import export_dataset
    # Lock covers permission checks and export so revoke cannot race publication.
    with _Lock():
        def records():
            for path in sorted((_root() / "records").glob("*.json")):
                record = json.loads(path.read_text())
                if _allowed_record(record):
                    yield scrub_record(record)
        def pairs():
            for pref in preferences:
                provenance = pref.get("provenance", {})
                rid = provenance.get("source_record_id", "")
                if not re.fullmatch(r"t_[0-9a-f]{32}", rid):
                    raise TrainingError("preference_source_required")
                source = json.loads((_root() / "records" / (rid + ".json")).read_text())
                if not _allowed_record(source):
                    raise TrainingError("preference_project_disabled")
                if "project_id" in provenance and provenance["project_id"] != source["provenance"]["project_id"]:
                    raise TrainingError("preference_project_mismatch")
                lengths = [len(pref.get(k, [])) for k in ("prompt", "chosen", "rejected")]
                stub = {"messages": sum((pref[k] for k in ("prompt", "chosen", "rejected")), []),
                        "agent": {}, "quality": {"complete": True, "missing": [], "issues": []},
                        "evidence": pref.get("evidence", []), "tools": pref.get("tools", [])}
                clean = scrub_record(stub)
                if not clean["quality"]["complete"]:
                    raise TrainingError("preference_excluded_content")
                # Never re-sign an existing review after masking changed its content.
                result = {"schema": pref.get("schema", "nous.preference/2"),
                          "id": "pref_" + _hash(pref.get("id"))[:24], "privacy": clean["privacy"],
                          "provenance": {"source_record_id": rid},
                          "tools": clean["tools"], "evidence": clean["evidence"]}
                if "project_id" in provenance:
                    result["provenance"]["project_id"] = provenance["project_id"]
                at = 0
                for k, n in zip(("prompt", "chosen", "rejected"), lengths):
                    result[k] = clean["messages"][at:at+n]
                    at += n
                yield result
        return export_dataset(records(), out, preferences=pairs(), profile=profile)


def list_records(project=None, limit=100):
    """A content-free local inventory for selecting records to review."""
    if not 1 <= limit <= 10000:
        raise TrainingError("invalid_limit")
    pid = project_id(project) if project else None
    result = []
    for path in sorted((_root() / "records").glob("*.json")):
        record = json.loads(path.read_text())
        if pid and record.get("provenance", {}).get("project_id") != pid:
            continue
        result.append({"id": record["id"], "agent": record.get("agent"),
                       "messages": len(record.get("messages", [])), "quality": record.get("quality"),
                       "reviews": len(record.get("evidence", [])), "export_allowed": _allowed_record(record),
                       "path": str(path)})
        if len(result) >= limit:
            break
    return {"records": result, "limit": limit}


def main(argv=None):
    # CLI and GUI hooks must use the same pseudonym key when NOUS_HOME is set.
    os.environ.setdefault("NOUS_LEDGER_HOME", str(HOME / "ledger"))
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("setup", "doctor", "uninstall"):
        p = sub.add_parser(name, help="remove this training home's shared platform hook; project authorization and data remain" if name == "uninstall" else None)
        p.add_argument("--project", required=True)
        p.add_argument("--agent", choices=("claude", "codex", "both"), default="both")
    p = sub.add_parser("list")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("validate")
    p.add_argument("--dataset", required=True)
    p = sub.add_parser("audit", help="summarize validated dataset quality without printing source content")
    p.add_argument("--dataset", required=True)
    p.add_argument("--require-training-rows", action="store_true", help="exit nonzero when no SFT/DPO rows are available")
    p = sub.add_parser("diagnose", help="explain validated candidates and required evidence without changing records")
    p.add_argument("--dataset", required=True)
    p.add_argument("--require-training-rows", action="store_true", help="exit nonzero when the existing export has no SFT/DPO rows")
    p = sub.add_parser("inspect-source", help="inspect one explicit Codex transcript for context gaps; never scans history")
    p.add_argument("--transcript", required=True)
    p = sub.add_parser("split", help="group validated training rows by source project or session")
    p.add_argument("--dataset", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--group-by", choices=("project", "session"), default="project")
    p.add_argument("--eval-ratio", type=float, default=0.1)
    p.add_argument("--seed", default="nous-v1")
    p = sub.add_parser("demo")
    p.add_argument("--out", required=True)
    p.add_argument("--profile", choices=("trl", "codemode-events-v1", "request-sft-v1", "request-episode-sft-v1"), default="trl")
    p = sub.add_parser("capture-request", help="send one explicit native request and store its scrubbed exchange locally")
    p.add_argument("--provider", required=True, choices=("openai-responses", "anthropic-messages"))
    p.add_argument("--request", required=True, help="native request JSON file; sent exactly as supplied")
    p.add_argument("--project", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--oracle", help="pre-dispatch expected-result specification JSON")
    p.add_argument("--endpoint", help="official endpoint or explicit local test endpoint")
    p.add_argument("--api-key-env", help="explicit environment variable name; value is never stored")
    p.add_argument("--allow-loopback", action="store_true")
    p.add_argument("--synthetic", action="store_true")
    for name in ("enable", "disable"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        if name == "disable":
            p.add_argument("--purge", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("health", help="read local collection diagnostics without inspecting platform history")
    p.add_argument("--project")
    p = sub.add_parser("history-scan", help="make a read-only local history import plan")
    p.add_argument("--out", required=True)
    p.add_argument("--agent", choices=("both", "claude", "codex"), default="both")
    p.add_argument("--project", action="append", help="optional exact original project path; default all local projects")
    p.add_argument("--max-files", type=int, default=10000)
    p = sub.add_parser("history-import", help="import a reviewed local history plan once; never enable continuous capture")
    p.add_argument("--plan", required=True)
    p.add_argument("--out", required=True, help="new private report and dataset directory")
    p = sub.add_parser("import")
    p.add_argument("--transcript", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--agent", default="auto", choices=("auto", "claude", "codex"))
    p.add_argument("--session")
    p.add_argument("--context", help="JSON containing the actual tools schemas used in this session")
    p = sub.add_parser("recover", help="refresh one existing authorized record from its original transcript")
    p.add_argument("--id", required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--agent", default="auto", choices=("auto", "claude", "codex"))
    p.add_argument("--context")
    p = sub.add_parser("review")
    p.add_argument("--id", required=True)
    p.add_argument("--reviewer", required=True, help="reviewer pseudonym")
    p.add_argument("--result", choices=("accepted", "rejected"), required=True)
    p = sub.add_parser("review-content", help="review the exact scrubbed event or request contents, separately from task outcome")
    p.add_argument("--id", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--result", choices=("cleared", "rejected"), required=True)
    p = sub.add_parser("review-step", help="review one observed tool decision independently of final task success")
    p.add_argument("--id", required=True)
    p.add_argument("--step", type=int, required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--result", choices=("accepted", "rejected"), required=True)
    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p.add_argument("--profile", choices=("trl", "openai", "codemode-events-v1", "request-sft-v1", "request-episode-sft-v1"), default="trl")
    p.add_argument("--preferences", help="JSONL of explicitly annotated pairs bound to source_record_id")
    args = ap.parse_args(argv)
    try:
        if args.command in ("setup", "doctor", "uninstall"):
            import nous_training_setup
            if args.command == "setup":
                if not Path(args.project).is_dir():
                    raise TrainingError("project_directory_required")
                result = nous_training_setup.setup(args.agent, args.project, home=HOME, ledger_home=os.environ["NOUS_LEDGER_HOME"])
                if result.get("installed"):
                    set_project(args.project, True)
                    result["capture_enabled"] = True
                else:
                    print(_json(result))
                    return 1
            elif args.command == "uninstall":
                result = nous_training_setup.uninstall(args.agent, args.project, home=HOME, ledger_home=os.environ["NOUS_LEDGER_HOME"])
            else:
                result = nous_training_setup.doctor(args.agent, args.project, home=HOME, ledger_home=os.environ["NOUS_LEDGER_HOME"])
        elif args.command == "list":
            result = list_records(args.project, args.limit)
        elif args.command == "validate":
            from nous_training_validate import validate_dataset
            result = validate_dataset(args.dataset)
            print(_json(result))
            return 0 if result.get("valid") else 1
        elif args.command == "audit":
            from nous_training_audit import audit_dataset
            result = audit_dataset(args.dataset)
            print(_json(result))
            return 0 if result.get("structural_valid") and (not args.require_training_rows or result.get("training_rows_present")) else 1
        elif args.command == "diagnose":
            from nous_training_diagnose import diagnose_dataset
            result = diagnose_dataset(args.dataset)
            print(_json(result))
            return 0 if result.get("structural_valid") and result.get("diagnosis_available") and (not args.require_training_rows or result.get("training_rows_present")) else 1
        elif args.command == "inspect-source":
            from nous_training_context import inspect_source
            result = inspect_source(args.transcript)
            print(_json(result))
            return 0 if result.get("valid") else 1
        elif args.command == "split":
            from nous_training_split import split_dataset
            result = split_dataset(args.dataset, args.out, group_by=args.group_by, eval_ratio=args.eval_ratio, seed=args.seed)
        elif args.command == "demo":
            import subprocess, sys
            runner = Path(__file__).with_name("nous_training_episode_demo.py" if args.profile == "request-episode-sft-v1" else "nous_training_request_demo.py" if args.profile == "request-sft-v1" else "nous_training_demo.py")
            return subprocess.run([sys.executable, "-B", str(runner), "--out", args.out, "--profile", args.profile]).returncode
        elif args.command == "capture-request":
            from nous_training_observer import capture_file
            result = capture_file(args.provider, args.request, project=args.project, session=args.session,
                                  oracle_file=args.oracle, endpoint=args.endpoint, api_key_env=args.api_key_env,
                                  allow_loopback=args.allow_loopback, synthetic=args.synthetic)
            print(_json(result))
            return 0 if result.get("status") == "collected" else 1
        elif args.command in ("enable", "disable"):
            result = {"project_id": set_project(args.project, args.command == "enable", purge=getattr(args, "purge", False)),
                      "capture": args.command == "enable"}
        elif args.command == "status":
            result = _config()
            result["stored_records"] = len(list((_root() / "records").glob("*.json")))
        elif args.command == "health":
            from nous_training_health import read_health
            result = read_health(HOME, project_id(args.project) if args.project else None)
        elif args.command == "history-scan":
            from nous_training_history import make_plan, write_plan
            result = write_plan(make_plan(agent=args.agent, projects=args.project, max_files=args.max_files), args.out)
            print(_json(result))
            return 0 if result["scan_complete"] else 1
        elif args.command == "history-import":
            from nous_training_history import read_plan, import_plan
            result = import_plan(read_plan(args.plan), args.out)
            print(_json({"report":str(Path(args.out) / "report.json"), "processing_complete":result["processing_complete"],
                         "all_sources_imported":result["all_sources_imported"], "counts":result["counts"],
                         "reason_counts":result["reason_counts"], "datasets":result["datasets"],
                         "continuous_capture_enabled":False, "upload_performed":False}))
            return 0 if result["all_sources_imported"] else 2
        elif args.command == "import":
            context = json.loads(Path(args.context).read_text()) if args.context else None
            result = collect(args.transcript, args.project, agent=args.agent, session=args.session, context=context, manual=True)
        elif args.command == "recover":
            context = json.loads(Path(args.context).read_text()) if args.context else None
            result = recover(args.id, args.transcript, args.project, agent=args.agent, context=context)
        elif args.command == "review":
            result = review(args.id, args.reviewer, args.result)
        elif args.command == "review-content":
            result = review(args.id, args.reviewer, args.result, purpose="content")
        else:
            with open(args.preferences, encoding="utf-8") if args.preferences else __import__("contextlib").nullcontext([]) as rows:
                result = export(args.out, profile=args.profile, preferences=(json.loads(line) for line in rows if line.strip()))
        print(_json(result))
        return 0
    except Exception as exc:
        safe_domain_error = (type(exc).__module__, type(exc).__name__) in {
            ("nous_training_setup", "SetupError"), ("nous_training_split", "SplitError"),
            ("nous_training_observer", "ObserverError"), ("nous_training_request", "RequestError"),
            ("nous_training_verify", "VerificationError"), ("nous_training_transport", "TransportError"),
            ("nous_codemode_protocol", "ProtocolError"), ("nous_codemode_privacy", "ProtocolPrivacyError")}
        reason = str(exc) if isinstance(exc, TrainingError) or safe_domain_error else "operation_failed"
        print(_json({"error": reason, "type": type(exc).__name__}), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
