"""Native function-tool privacy checks; no tool execution or file content storage.

Opaque execution and excluded file operands are rejected before persistence.
The caller applies its existing text/PII scrubber and revalidates native schema.
"""
import copy
import json
import os
import re
import stat
import subprocess

import nous_privacy
from nous_training_export import strict_json

PATH_KEYS = {"path", "paths", "file", "files", "file_path", "file_paths", "filename", "filenames",
             "target_file", "source_file", "directory", "directories", "cwd", "workdir"}
OPAQUE_KEYS = {"command", "commands", "cmd", "script", "code", "shell_command", "program", "argv"}
OPAQUE_NAMES = re.compile(r"(?:^|[_.-])(exec|execute|shell|bash|terminal|run_command|run_code|eval)(?:$|[_.-])", re.I)


class RequestPrivacyError(ValueError):
    """Static codes only."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _check_arguments(name, value, project, *, previously_checked=False):
    if OPAQUE_NAMES.search(name):
        raise RequestPrivacyError("request_opaque_tool_execution")
    paths, strings = [], []
    def walk(item, path_operand=False, depth=0):
        if depth > 64:
            raise RequestPrivacyError("request_tool_privacy_depth")
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in OPAQUE_KEYS and child:
                    raise RequestPrivacyError("request_opaque_tool_execution")
                walk(child, path_operand or key.lower() in PATH_KEYS, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, path_operand, depth + 1)
        elif isinstance(item, str):
            strings.append(item)
            if path_operand:
                paths.append(item)
    walk(value)
    for value in strings:
        if any(nous_privacy.excluded_name(token) for token in re.findall(r"[^\s\"'\\]+", value)):
            raise RequestPrivacyError("request_excluded_tool_content")
    if len(paths) > 32:
        raise RequestPrivacyError("request_tool_privacy_path_limit")
    checked_paths = set()
    for path in ([] if previously_checked else paths):
        if not project:
            raise RequestPrivacyError("request_file_exclusion_unverified")
        full = os.path.abspath(path if os.path.isabs(path) else os.path.join(project, path))
        resolved = os.path.realpath(full)
        if os.path.commonpath((os.path.realpath(project), resolved)) != os.path.realpath(project):
            raise RequestPrivacyError("request_external_file_tool")
        if nous_privacy.excluded_name(full) or nous_privacy.excluded_name(resolved):
            raise RequestPrivacyError("request_excluded_tool_content")
        # Existing regular files only. Nonblocking/no-follow prevents FIFO/device
        # reads and final-component link races. Probe at most 8 KiB for binary
        # exclusion; never retain the probe or include it in a diagnostic.
        try:
            before = os.stat(resolved)
            if not stat.S_ISREG(before.st_mode) or before.st_size > nous_privacy.MAX_BYTES:
                raise RequestPrivacyError("request_excluded_tool_content")
            fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise RequestPrivacyError("request_file_exclusion_unverified")
                if b"\0" in stream.read(8192):
                    raise RequestPrivacyError("request_excluded_tool_content")
        except OSError:
            raise RequestPrivacyError("request_file_exclusion_unverified") from None
        checked_paths.update((full, resolved))
    if checked_paths:
        try:
            result = subprocess.run(["git", "-C", project, "check-ignore", "-q", "--stdin", "-z"],
                                    input=b"\0".join(p.encode() for p in sorted(checked_paths)) + b"\0",
                                    capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            raise RequestPrivacyError("request_file_exclusion_unverified") from None
        if result.returncode == 0:
            raise RequestPrivacyError("request_excluded_tool_content")
        if type(result.returncode) is not int or result.returncode != 1:
            raise RequestPrivacyError("request_file_exclusion_unverified")


def scrub_tools(exchange, *, clean, project="", previously_checked=False):
    """Check all native calls and scrub structured arguments/results in a copy.

    ``clean`` is the collector's field-aware scrub callback. Known IDs are not
    text and are left to the native snapshot scrubber, which must reject any
    changed linkage instead of reconnecting unrelated calls.
    """
    value = copy.deepcopy(exchange)
    provider = value["provider"]
    calls, results = [], []
    if provider == "openai-responses":
        items = value["request"].get("input", [])
        if isinstance(items, list):
            calls.extend(item for item in items if item.get("type") == "function_call")
            results.extend(item for item in items if item.get("type") == "function_call_output")
        calls.extend(item for item in value["response"].get("output", []) if item.get("type") == "function_call")
        for call in calls:
            arguments = strict_json(call["arguments"])
            _check_arguments(((call.get("namespace") or "") + "." + call["name"]).lstrip("."), arguments, project, previously_checked=previously_checked)
            safe = clean(arguments, True)
            if safe != arguments:
                call["arguments"] = _json(safe)
        for result in results:
            content = result["output"]
            blocks = [{"type": "input_text", "text": content}] if isinstance(content, str) else content
            for block in blocks:
                raw = block["text"]
                try:
                    parsed = strict_json(raw)
                except ValueError:
                    block["text"] = clean(raw, False)
                else:
                    safe = clean(parsed, True)
                    block["text"] = raw if safe == parsed else _json(safe)
            result["output"] = blocks[0]["text"] if isinstance(content, str) else blocks
    elif provider == "anthropic-messages":
        for message in value["request"]["messages"]:
            content = message["content"]
            if isinstance(content, list):
                calls.extend(block for block in content if block["type"] == "tool_use")
                results.extend(block for block in content if block["type"] == "tool_result")
        calls.extend(block for block in value["response"]["content"] if block["type"] == "tool_use")
        for call in calls:
            _check_arguments(call["name"], call["input"], project, previously_checked=previously_checked)
            call["input"] = clean(call["input"], True)
        for result in results:
            if "content" not in result:
                continue
            content = result["content"]
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            for block in blocks:
                raw = block["text"]
                try:
                    parsed = strict_json(raw)
                except ValueError:
                    block["text"] = clean(raw, False)
                else:
                    safe = clean(parsed, True)
                    block["text"] = raw if safe == parsed else _json(safe)
            result["content"] = blocks[0]["text"] if isinstance(content, str) else blocks
    else:
        raise RequestPrivacyError("request_privacy_provider_unsupported")
    return value
