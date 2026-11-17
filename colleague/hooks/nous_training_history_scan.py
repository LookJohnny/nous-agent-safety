"""Bounded, read-only history discovery. Returned paths are private import inputs.

This module never establishes full transcript completeness or training eligibility.
The importer must reread one stable complete snapshot and revalidate its identity.
No contents, instructions, names of tools, or arbitrary metadata enter the report.
"""
import hashlib
import json
import math
import os
import stat


SCHEMA = "nous.training.history-scan/1"
MAX_FILES = 10_000
MAX_ENTRIES = 100_000
MAX_DIRECTORIES = 10_000
MAX_DEPTH = 64
MAX_HEADER_BYTES = 1024 * 1024
MAX_HEADER_LINES = 128
MAX_SKIPPED = 1000
_ROLES = {"sessions", "archived_sessions", "projects", "explicit"}
_CLAUDE_ROWS = {"user", "assistant", "system", "progress", "file-history-snapshot",
                "queue-operation", "last-prompt", "custom-title", "tag", "pr-link", "slug", "summary"}


class HistoryScanError(ValueError):
    """Only a closed static code, never an exception containing source material."""


def _source_id(path):
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def _signature(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _identity(info):
    return info.st_dev, info.st_ino


def _path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise HistoryScanError("invalid_scan_root")
    # Do not erase a symlink-bearing prefix by lexically collapsing link/../.
    if ".." in value.split(os.path.sep):
        raise HistoryScanError("invalid_scan_root")
    return os.path.abspath(os.path.expanduser(value))


def default_roots():
    """Return metadata only; do not create or resolve any directories."""
    codex = _path(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"))
    claude = _path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"))
    return [{"agent": "codex", "path": os.path.join(codex, "sessions"), "source_role": "sessions"},
            {"agent": "codex", "path": os.path.join(codex, "archived_sessions"), "source_role": "archived_sessions"},
            {"agent": "claude", "path": os.path.join(claude, "projects"), "source_role": "projects"}]


def _open_dir(path):
    """Pin each component with openat; a parent symlink is never followed."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HistoryScanError("scan_secure_open_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.path.sep, flags)
    try:
        for part in path.split(os.path.sep):
            if not part:
                continue
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise HistoryScanError("scan_symlink_rejected")
            if not stat.S_ISDIR(before.st_mode):
                raise HistoryScanError("scan_directory_required")
            new_fd = os.open(part, flags, dir_fd=fd)
            if _identity(before) != _identity(os.fstat(new_fd)):
                os.close(new_fd)
                raise HistoryScanError("scan_source_changed")
            os.close(fd)
            fd = new_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_dir(path, info):
    fd = _open_dir(path)
    try:
        if _identity(info) != _identity(os.fstat(fd)):
            raise HistoryScanError("scan_source_changed")
    finally:
        os.close(fd)


def _loads(line):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(_value):
        raise ValueError()
    try:
        result = json.loads(line.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        stack = [(result, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > 100_000 or depth > 64:
                raise ValueError()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError()
            elif isinstance(item, dict):
                stack.extend((x, depth + 1) for pair in item.items() for x in pair)
            elif isinstance(item, list):
                stack.extend((x, depth + 1) for x in item)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise HistoryScanError("scan_header_invalid_json") from None


def _string(value, *, cwd=False):
    limit = 4096 if cwd else 512
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HistoryScanError("scan_identity_invalid")
    if cwd and not os.path.isabs(value):
        raise HistoryScanError("scan_identity_invalid")
    return os.path.normpath(value) if cwd else value


def _metadata(rows, agent, relative):
    ids, cwds = set(), set()
    paired = False
    hints = set()
    roles = set()
    for row in rows:
        if agent == "codex":
            if row.get("type") != "session_meta":
                if "sessionId" in row:
                    raise HistoryScanError("scan_agent_mismatch")
                continue
            meta = row.get("payload")
            if not isinstance(meta, dict):
                raise HistoryScanError("scan_identity_invalid")
            ids.add(_string(meta.get("id")))
            cwds.add(_string(meta.get("cwd"), cwd=True))
            paired = True
            source = meta.get("source")
            if isinstance(source, dict) and "subagent" in source:
                roles.add("subagent")
            if meta.get("parent_thread_id") is not None or meta.get("subagent_history_start_ordinal") is not None:
                roles.add("subagent")
                hints.add("inherited_history")
            if meta.get("forked_from_id") is not None or meta.get("forked_from_ordinal_exclusive") is not None:
                roles.add("branch")
                hints.add("inherited_history")
            if meta.get("history_base") is not None:
                hints.add("inherited_history")
            if meta.get("history_mode", "legacy") != "legacy":
                hints.add("nonlegacy_history")
            if isinstance(source, str) and source in {"cli", "vscode", "exec"}:
                roles.add("main")
        else:
            if row.get("type") == "session_meta":
                raise HistoryScanError("scan_agent_mismatch")
            if row.get("type") not in _CLAUDE_ROWS:
                continue
            if "sessionId" in row:
                ids.add(_string(row["sessionId"]))
            if "cwd" in row:
                cwds.add(_string(row["cwd"], cwd=True))
            if "sessionId" in row and "cwd" in row:
                paired = True
            if row.get("isSidechain") is True:
                roles.add("sidechain")
                hints.add("inherited_history")
            elif row.get("isSidechain") is False:
                roles.add("main")
            if row.get("agentId"):
                roles.add("subagent")
    if len(ids) > 1 or len(cwds) > 1:
        raise HistoryScanError("scan_identity_conflict")
    if not paired or len(ids) != 1 or len(cwds) != 1:
        raise HistoryScanError("scan_identity_unknown")
    if agent == "claude" and "subagents" in relative.split(os.path.sep)[:-1]:
        roles.add("subagent")
        hints.add("subagent_path")
    role = next((x for x in ("subagent", "branch", "sidechain", "main") if x in roles), "unknown")
    if role in {"subagent", "branch", "sidechain"}:
        hints.add(role)
    return {"session_id": next(iter(ids)), "cwd": next(iter(cwds)),
            "branch_role": role, "context_hints": sorted(hints)}


def _header(path, agent, root, expected, compression="none"):
    from nous_training_history_read import HistoryReadError, read_history_header
    try:
        prefix = read_history_header(path, expected, compression,
                                     max_bytes=MAX_HEADER_BYTES, max_lines=MAX_HEADER_LINES)
    except HistoryReadError as exc:
        code = "scan_compressed_unsupported" if str(exc) == "history_compression_unsupported" else str(exc)
        raise HistoryScanError(code) from None
    rows = [_loads(line) for line in prefix["snapshot"].split(b"\n") if line.strip()]
    try:
        identity = _metadata(rows, agent, os.path.relpath(path, root))
    except HistoryScanError as exc:
        if str(exc) == "scan_identity_unknown" and prefix["header"]["limit"] is not None:
            raise HistoryScanError("scan_header_limit") from None
        raise
    parent, name = os.path.split(path)
    fd = _open_dir(parent)
    try:
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(after.st_mode) or _signature(after) != expected:
            raise HistoryScanError("scan_source_changed")
    finally:
        os.close(fd)
    identity.update({"stat": prefix["stat"], "header": prefix["header"], "compression": compression})
    return identity


def scan_history(*, roots=None, max_files=MAX_FILES):
    """Discover candidates without writing state or printing source material.

    ``complete`` means the directory traversal was exhausted, not that candidate
    headers or source histories are complete. ``skipped_counts`` is exhaustive;
    the per-source skipped list is capped. Paths/cwd/session IDs in candidates
    are private importer inputs and must not be copied into public reports.
    """
    report = {"schema": SCHEMA, "complete": True, "candidates": [], "skipped": [],
              "skipped_counts": {}, "skipped_details_truncated": False, "errors": [],
              "counts": {"roots": 0, "directories": 0, "entries": 0, "jsonl_files": 0, "compressed_files": 0, "source_files": 0,
                         "candidates": 0, "skipped": 0}}
    def skip(path, code):
        report["counts"]["skipped"] += 1
        report["skipped_counts"][code] = report["skipped_counts"].get(code, 0) + 1
        if len(report["skipped"]) < MAX_SKIPPED:
            report["skipped"].append({"source_id": _source_id(path), "code": code})
        else:
            report["skipped_details_truncated"] = True
    def stop(code):
        report["complete"] = False
        if code not in report["errors"]:
            report["errors"].append(code)
    try:
        if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
            raise HistoryScanError("invalid_scan_limits")
        roots = default_roots() if roots is None else roots
        if not isinstance(roots, (list, tuple)) or len(roots) > 64:
            raise HistoryScanError("invalid_scan_roots")
        prepared = []
        for root in roots:
            if (not isinstance(root, dict) or set(root) - {"agent", "path", "source_role"}
                    or root.get("agent") not in {"codex", "claude"}
                    or root.get("source_role", "explicit") not in _ROLES):
                raise HistoryScanError("invalid_scan_roots")
            prepared.append((_path(root.get("path")), root["agent"], root.get("source_role", "explicit")))
    except HistoryScanError as exc:
        stop(str(exc))
        return report
    visited, seen_files = set(), set()
    exhausted = False
    for root, agent, source_role in sorted(set(prepared)):
        if exhausted:
            break
        report["counts"]["roots"] += 1
        pending = [(root, 0)]
        while pending and not exhausted:
            directory, depth = pending.pop()
            if (agent, directory) in visited:
                continue
            if depth > MAX_DEPTH:
                skip(directory, "scan_depth_limit")
                stop("scan_depth_limit")
                continue
            if report["counts"]["directories"] >= MAX_DIRECTORIES:
                stop("scan_directory_limit")
                exhausted = True
                break
            fd = None
            try:
                fd = _open_dir(directory)
                directory_info = os.fstat(fd)
                visited.add((agent, directory))
                report["counts"]["directories"] += 1
                names = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if report["counts"]["entries"] + len(names) >= MAX_ENTRIES:
                            stop("scan_entry_limit")
                            exhausted = True
                            break
                        names.append(entry.name)
                report["counts"]["entries"] += len(names)
                if exhausted:
                    break
                children = []
                for name in sorted(names):
                    path = os.path.join(directory, name)
                    # scandir names cannot escape; retain an explicit boundary check.
                    if os.path.commonpath([root, path]) != root:
                        skip(path, "scan_path_escape")
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        skip(path, "scan_symlink_rejected")
                    elif stat.S_ISDIR(info.st_mode):
                        children.append((path, depth + 1))
                    elif name.endswith((".jsonl", ".jsonl.zst")):
                        if not stat.S_ISREG(info.st_mode):
                            skip(path, "scan_regular_file_required")
                            continue
                        if path in seen_files:
                            continue
                        if report["counts"]["source_files"] >= max_files:
                            stop("scan_file_limit")
                            exhausted = True
                            break
                        seen_files.add(path)
                        compression = "zstd" if name.endswith(".jsonl.zst") else "none"
                        report["counts"]["source_files"] += 1
                        report["counts"]["compressed_files" if compression == "zstd" else "jsonl_files"] += 1
                        try:
                            identity = _header(path, agent, root, _signature(info), compression)
                            report["candidates"].append({"path": path, "agent": agent,
                                "source_id": _source_id(path), "source_role": source_role, **identity})
                        except HistoryScanError as exc:
                            skip(path, str(exc))
                        except (OSError, ValueError, TypeError, OverflowError):
                            skip(path, "scan_source_unreadable")
                _check_dir(directory, directory_info)
                pending.extend(reversed(children))
            except FileNotFoundError:
                skip(directory, "scan_root_missing" if directory == root else "scan_source_changed")
                if directory != root:
                    stop("scan_source_changed")
            except HistoryScanError as exc:
                skip(directory, str(exc))
                stop(str(exc))
            except (OSError, ValueError, TypeError, OverflowError):
                skip(directory, "scan_directory_unreadable")
                stop("scan_directory_unreadable")
            finally:
                if fd is not None:
                    os.close(fd)
    report["candidates"].sort(key=lambda item: (item["path"], item["agent"]))
    report["counts"]["candidates"] = len(report["candidates"])
    report["errors"].sort()
    return report
