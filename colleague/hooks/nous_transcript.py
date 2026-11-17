"""Normalize local Claude Code / Codex JSONL transcripts without retaining reasoning.

This is an in-memory parser, not a collector: callers must obtain consent and scrub
its result before persistence. Transcript formats are not stable vendor APIs. A
quality report makes lost visible content explicit instead of silently approving it.
Offsets count physical JSONL lines; a malformed unterminated final line is never
consumed. A valid final JSON object is accepted even without a trailing newline.
"""
import copy
import json
import os
import stat


MAX_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_LINES = 1_000_000


class TranscriptReadError(ValueError):
    """A static read/limit diagnostic, without paths or source text."""


def _limits(max_bytes=None, max_line_bytes=None):
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    max_line_bytes = MAX_LINE_BYTES if max_line_bytes is None else max_line_bytes
    if (type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES
            or type(max_line_bytes) is not int or not 1 <= max_line_bytes <= MAX_LINE_BYTES):
        raise TranscriptReadError("invalid_transcript_limits")
    return max_bytes, max_line_bytes


def _check_bytes(data, *, max_bytes=None, max_line_bytes=None):
    """Validate the entire immutable snapshot before any prefix is parsed."""
    max_bytes, max_line_bytes = _limits(max_bytes, max_line_bytes)
    if type(data) is not bytes:
        raise TranscriptReadError("invalid_transcript_bytes")
    if len(data) > max_bytes:
        raise TranscriptReadError("transcript_too_large")
    line_count = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
    if line_count > MAX_LINES:
        raise TranscriptReadError("transcript_too_many_lines")
    # Scan fixed-size chunks, not one Python object per line. Even a file full of
    # newlines has bounded validation memory and does not recreate readlines().
    pending = 0
    for position in range(0, len(data), 64 * 1024):
        chunk = data[position:position + 64 * 1024]
        first = chunk.find(b"\n")
        if first < 0:
            pending += len(chunk)
        else:
            if pending + first + 1 > max_line_bytes:
                raise TranscriptReadError("transcript_line_too_large")
            last = chunk.rfind(b"\n")
            if max_line_bytes < len(chunk) and last > first:
                if max(map(len, chunk[first + 1:last].split(b"\n"))) + 1 > max_line_bytes:
                    raise TranscriptReadError("transcript_line_too_large")
            pending = len(chunk) - last - 1
        if pending > max_line_bytes:
            raise TranscriptReadError("transcript_line_too_large")
    return line_count


def _iter_lines(data):
    """Yield physical byte lines without materializing a list or copying the file."""
    position = 0
    while position < len(data):
        newline = data.find(b"\n", position)
        stop = len(data) if newline < 0 else newline + 1
        yield data[position:stop]
        position = stop


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_transcript_bytes(path, *, max_bytes=None, max_line_bytes=None):
    """Read one stable ordinary file into bounded immutable memory, or fail.

    The initial descriptor size fixes the read budget (size + one probe byte),
    so an appending writer cannot turn this into an unbounded read. Nonblocking
    and no-follow open flags plus pre/post descriptor/path checks reject FIFOs,
    direct symlinks and replacements. Nothing is cached or written to disk.
    This is a consistency check, not a filesystem lock against future writers.
    """
    max_bytes, max_line_bytes = _limits(max_bytes, max_line_bytes)
    fd = None
    try:
        before_path = os.lstat(path)
        if not stat.S_ISREG(before_path.st_mode):
            raise TranscriptReadError("transcript_must_be_regular_file")
        if before_path.st_size < 0:
            raise TranscriptReadError("transcript_unreadable")
        if before_path.st_size > max_bytes:
            raise TranscriptReadError("transcript_too_large")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise TranscriptReadError("transcript_must_be_regular_file")
        if before.st_size < 0:
            raise TranscriptReadError("transcript_unreadable")
        if _signature(before_path) != _signature(before):
            raise TranscriptReadError("transcript_changed_during_read")
        if before.st_size > max_bytes:
            raise TranscriptReadError("transcript_too_large")
        # No readlines(), no read-until-EOF, and no arbitrary user stream.
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(before.st_size + 1)
            after = os.fstat(handle.fileno())
            try:
                after_path = os.lstat(path)
            except OSError:
                raise TranscriptReadError("transcript_changed_during_read") from None
        if (len(data) != before.st_size or not stat.S_ISREG(after_path.st_mode)
                or _signature(before) != _signature(after) or _signature(before) != _signature(after_path)):
            raise TranscriptReadError("transcript_changed_during_read")
        _check_bytes(data, max_bytes=max_bytes, max_line_bytes=max_line_bytes)
        return data
    except TranscriptReadError:
        raise
    except (OSError, ValueError, TypeError, OverflowError):
        raise TranscriptReadError("transcript_unreadable") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


_HIDDEN_BLOCKS = {"thinking", "redacted_thinking", "reasoning", "reasoning_text",
                  "summary_text", "encrypted_reasoning"}
_MEDIA_BLOCKS = {"image", "input_image", "output_image", "audio", "input_audio",
                 "output_audio", "video", "document", "file", "input_file"}
_CLAUDE_METADATA = {"progress", "file-history-snapshot", "queue-operation",
                    "last-prompt", "custom-title", "tag", "pr-link", "slug"}
_CODEX_METADATA = {"session_meta", "turn_context", "event_msg"}
_ROLES = {"system", "developer", "user", "assistant", "tool"}


def _fingerprint(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _loads(value):
    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")
    def unique_pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    return json.loads(value, parse_constant=invalid_constant, object_pairs_hook=unique_pairs)


class _Parser:
    def __init__(self, agent):
        aliases = {"Claude Code": "claude", "Codex": "codex", "claude_code": "claude"}
        self.requested = aliases.get(agent, agent) if isinstance(agent, str) else "unsupported"
        self.agent = "unknown" if self.requested == "auto" else self.requested
        self.messages = []
        self.version = None
        self.model = None
        self.issues = []
        self.missing = ["system_prompt", "tool_definitions"]
        self.seen_records = {}
        self.seen_items = {}
        self.session_ids = set()

    def issue(self, issue, missing=None):
        if issue not in self.issues:
            self.issues.append(issue)
        if missing and missing not in self.missing:
            self.missing.append(missing)

    def source(self, source, selected):
        if self.agent == "unknown":
            self.agent = source
        elif self.agent != source and selected:
            self.issue("mixed_agents" if self.requested == "auto" else "agent_mismatch",
                       "source_format")

    def text_blocks(self, content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        if content is None:
            return []
        if not isinstance(content, list):
            self.issue("invalid_message_content", "visible_content")
            return []
        out = []
        for block in content:
            if not isinstance(block, dict):
                self.issue("unknown_content_block", "visible_content")
                continue
            kind = block.get("type")
            if not isinstance(kind, str):
                self.issue("unknown_content_block", "visible_content")
                continue
            if kind in _HIDDEN_BLOCKS:
                continue
            if kind in {"text", "input_text", "output_text"}:
                if isinstance(block.get("text"), str):
                    out.append({"type": "text", "text": block["text"]})
                else:
                    self.issue("invalid_text", "visible_content")
            elif kind == "refusal":
                if isinstance(block.get("refusal"), str):
                    out.append({"type": "text", "text": block["refusal"]})
                else:
                    self.issue("invalid_text", "visible_content")
            elif kind == "tool_use":
                out.append({"type": "tool_use", "id": block.get("id"),
                            "name": block.get("name"), "input": copy.deepcopy(block.get("input")),
                            "format": "function"})
            elif kind == "tool_result":
                out.append({"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
                            "content": self.result_content(block.get("content")),
                            "is_error": bool(block.get("is_error", False))})
            elif kind in _MEDIA_BLOCKS:
                # Never copy base64, URLs, document bodies, or audio into a text dataset.
                out.append({"type": "image" if "image" in kind else kind, "omitted": True})
                self.issue("media_omitted", "media_content")
            else:
                self.issue("unknown_content_block", "visible_content")
        return out

    def result_content(self, content):
        """Keep JSON values; sanitize recognized content-block arrays recursively."""
        if isinstance(content, list) and any(isinstance(x, dict) and "type" in x for x in content):
            blocks = self.text_blocks(content)
            if all(b.get("type") == "text" for b in blocks):
                return "\n".join(b["text"] for b in blocks)
            return blocks
        return copy.deepcopy(content)

    def add_message(self, role, blocks, row, message=None):
        if not blocks:
            return
        message = message or {}
        if not isinstance(role, str) or role not in _ROLES:
            self.issue("unknown_role", "visible_content")
            return
        item = {"role": role, "id": message.get("id") or row.get("uuid"),
                "ts": row.get("timestamp"), "content": blocks}
        if row.get("isSidechain"):
            item["sidechain"] = True
            if row.get("agentId"):
                item["agent_id"] = row["agentId"]
            self.issue("sidechain", "branch_context")
        if row.get("isCompactSummary"):
            item["compact_summary"] = True
            self.issue("compacted", "pre_compaction_context")
        if row.get("parentUuid"):
            item["parent_id"] = row["parentUuid"]
        model = message.get("model") or self.model
        if isinstance(model, str):
            item["model"] = model
        for key in ("phase", "channel"):
            if isinstance(message.get(key), str):
                item[key] = message[key]
        if role == "system" and "system_prompt" in self.missing:
            self.missing.remove("system_prompt")

        # Claude can emit several rows for one assistant message (text, then tools,
        # then a cumulative snapshot). Merge only adjacent rows of that message;
        # moving a later assistant response before an intervening tool result would
        # corrupt execution order. Never deduplicate user messages by their text.
        previous = self.messages[-1] if self.messages else None
        if (role == "assistant" and item["id"] and previous
                and previous["role"] == role and previous["id"] == item["id"]
                and previous.get("sidechain") == item.get("sidechain")):
            self.merge_content(previous, blocks)
            previous.update({k: v for k, v in item.items() if k not in {"content", "ts"}})
        else:
            self.messages.append(item)

    def merge_content(self, previous, incoming):
        """Merge adjacent rows of one stable message without guessing text deltas.

        A position-preserving snapshot can extend a text block or append blocks.
        Otherwise exact blocks and distinct tool calls can be merged in observation
        order. New text requires either the first text block or an exact overlap
        with the previous suffix; unanchored text may be a delta or a replacement,
        so retain the observation but explicitly disqualify it from strict training.
        """
        current = previous["content"]

        def extends(old, new):
            return (old == new or (old.get("type") == new.get("type") == "text"
                                  and new["text"].startswith(old["text"])))

        if len(incoming) >= len(current) and all(extends(old, new) for old, new in zip(current, incoming)):
            previous["content"] = incoming
            return

        old_keys = [_fingerprint(block) for block in current]
        new_keys = [_fingerprint(block) for block in incoming]
        known = set(old_keys)
        overlap = max((size for size in range(1, min(len(old_keys), len(new_keys)) + 1)
                       if old_keys[-size:] == new_keys[:size]), default=0)
        old_positions = [old_keys.index(key) for key in new_keys if key in known]
        if old_positions != sorted(old_positions):
            self.issue("ambiguous_message_update", "streamed_content")
        has_text = any(block.get("type") == "text" for block in current)
        if has_text and not overlap and any(block.get("type") == "text" and key not in known
                                            for block, key in zip(incoming, new_keys)):
            self.issue("ambiguous_message_update", "streamed_content")
        for block, key in zip(incoming, new_keys):
            if key not in known:
                current.append(block)
                known.add(key)

    def duplicate(self, key, value, registry):
        if not key:
            return False
        if not all(isinstance(part, str) for part in key):
            self.issue("invalid_record_id", "source_identity")
            return False
        fingerprint = _fingerprint(value)
        seen = registry.setdefault(key, set())
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
        return False

    def claude(self, row, selected):
        self.source("claude", selected)
        if isinstance(row.get("version"), str):
            self.version = row["version"]
        message = row.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            self.model = message["model"]
        if not selected:
            return
        if not isinstance(message, dict):
            self.issue("invalid_message", "visible_content")
            return
        if self.duplicate(("claude", row["uuid"]) if row.get("uuid") else None,
                          row, self.seen_records):
            return
        if message.get("channel") == "analysis":
            return
        self.add_message(message.get("role") or row["type"],
                         self.text_blocks(message.get("content")), row, message)

    def codex(self, row, selected):
        self.source("codex", selected)
        payload = row.get("payload")
        kind = row.get("type")
        if not isinstance(payload, dict):
            if selected:
                self.issue("invalid_payload", "visible_content")
            return
        if kind == "session_meta":
            version = payload.get("cli_version") or payload.get("version")
            if isinstance(version, str):
                self.version = version
        if isinstance(payload.get("model"), str):
            self.model = payload["model"]
        if not selected or kind in _CODEX_METADATA:
            # event_msg user_message / agent_message mirror response_item. They
            # are deliberately never used as a second conversation source.
            return
        if kind == "compacted":
            self.issue("compacted", "pre_compaction_context")
            return
        item_type = payload.get("type")
        if not isinstance(item_type, str):
            self.issue("unknown_response_item", "visible_content")
            return
        if item_type in _HIDDEN_BLOCKS or payload.get("channel") == "analysis":
            return
        item_id = payload.get("id")
        if not item_id and item_type in {"function_call", "custom_tool_call",
                                         "function_call_output", "custom_tool_call_output"}:
            item_id = payload.get("call_id")
        key = (item_type, item_id) if item_id else None
        if self.duplicate(key, payload, self.seen_items):
            return
        if item_type == "message":
            self.add_message(payload.get("role"), self.text_blocks(payload.get("content")), row, payload)
        elif item_type in {"function_call", "custom_tool_call"}:
            custom = item_type == "custom_tool_call"
            args = payload.get("input") if custom else payload.get("arguments")
            if not custom and isinstance(args, str):
                try:
                    args = _loads(args)
                except (ValueError, RecursionError):
                    self.issue("invalid_tool_arguments", "tool_arguments")
            elif not custom and "arguments" not in payload:
                self.issue("missing_tool_arguments", "tool_arguments")
            elif custom and "input" not in payload:
                self.issue("missing_tool_arguments", "tool_arguments")
            block = {"type": "tool_use", "id": payload.get("call_id"),
                     "name": payload.get("name"), "input": copy.deepcopy(args),
                     "format": "custom" if custom else "function"}
            self.add_message("assistant", [block], row, payload)
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            if "output" not in payload:
                self.issue("missing_tool_output", "tool_output")
            block = {"type": "tool_result", "tool_use_id": payload.get("call_id"),
                     "content": self.result_content(payload.get("output")),
                     "is_error": bool(payload.get("is_error", False))}
            self.add_message("user", [block], row, payload)
        else:
            self.issue("unknown_response_item", "visible_content")

    def row(self, row, selected):
        if not isinstance(row, dict):
            if selected:
                self.issue("invalid_record", "visible_content")
            return
        kind = row.get("type")
        if not isinstance(kind, str):
            if selected:
                self.issue("unknown_record_type", "visible_content")
            return
        native_session = None
        if kind in {"user", "assistant", "system"} and "sessionId" in row:
            native_session = row["sessionId"]
        elif kind == "session_meta" and isinstance(row.get("payload"), dict):
            native_session = row["payload"].get("id")
        if native_session is not None:
            if isinstance(native_session, str) and 0 < len(native_session) <= 512:
                self.session_ids.add(native_session)
                if len(self.session_ids) > 1:
                    self.issue("mixed_sessions", "session_context")
            else:
                self.issue("invalid_session_identity", "session_context")
        if kind in {"user", "assistant"} or (kind == "system" and isinstance(row.get("message"), dict)):
            self.claude(row, selected)
        elif kind in _CODEX_METADATA or kind in {"response_item", "compacted"}:
            self.codex(row, selected)
        elif kind == "system":
            self.source("claude", selected)
            if selected and row.get("subtype") == "compact_boundary":
                self.issue("compacted", "pre_compaction_context")
        elif kind == "summary":
            if selected:
                self.issue("compacted", "pre_compaction_context")
        elif kind not in _CLAUDE_METADATA and selected:
            self.issue("unknown_record_type", "visible_content")

    def validate_calls(self):
        calls, results = {}, set()
        for message in self.messages:
            for block in message["content"]:
                if block["type"] == "tool_use":
                    call_id = block.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        self.issue("missing_tool_call_id", "tool_linkage")
                        continue
                    if not isinstance(block.get("name"), str) or not block["name"]:
                        self.issue("missing_tool_name", "tool_linkage")
                    if call_id in calls:
                        self.issue("duplicate_tool_call", "tool_linkage")
                    calls[call_id] = block
                elif block["type"] == "tool_result":
                    call_id = block.get("tool_use_id")
                    if not isinstance(call_id, str) or not call_id:
                        self.issue("missing_tool_result_id", "tool_linkage")
                        continue
                    if call_id not in calls:
                        self.issue("orphan_tool_result", "tool_linkage")
                    if call_id in results:
                        self.issue("duplicate_tool_result", "tool_linkage")
                    results.add(call_id)
        if set(calls) - results:
            self.issue("unresolved_tool_call", "tool_linkage")

    def result(self, offset):
        self.validate_calls()
        # Codex can flush a commentary or partial answer before any final answer.
        # Explicit phase/channel metadata is evidence that this is not a finished
        # response. Legacy transcripts without it remain subject to the exporter's
        # final-assistant and human-acceptance gates; do not invent a completion.
        final_assistant = next((m for m in reversed(self.messages) if m["role"] == "assistant"), None)
        if final_assistant:
            phase = final_assistant.get("phase")
            if (phase in {"commentary", "partial_answer"}
                    or (phase != "final_answer" and final_assistant.get("channel") == "commentary")):
                self.issue("incomplete_final", "final_answer")
        if not self.messages:
            self.issue("no_visible_messages", "visible_content")
        return {"messages": self.messages, "version": self.version, "model": self.model,
                "source_session_id": next(iter(self.session_ids)) if len(self.session_ids) == 1 else None,
                "offset": offset, "agent": self.agent,
                "quality": {"complete": not self.issues, "missing": self.missing,
                            "issues": self.issues}}


def _slice_parser(start, end, agent):
    parser = _Parser(agent)
    if parser.requested not in {"auto", "claude", "codex"}:
        parser.issue("unsupported_agent", "source_format")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        parser.issue("invalid_offset")
        start = 0
    if end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < 0):
        parser.issue("invalid_offset")
        end = None
    return parser, start, end


def parse_transcript_bytes(data, start=0, end=None, agent="auto"):
    """Parse one immutable, bounded snapshot without reopening its source.

    Physical-line offsets retain the path API's semantics, including metadata
    before start, shrink reset and non-consumption of a malformed final tail.
    Limit failures discard the whole observation and do not advance the offset.
    """
    parser, start, end = _slice_parser(start, end, agent)
    try:
        line_count = _check_bytes(data)
    except TranscriptReadError as error:
        parser.issue(str(error), "visible_content")
        return parser.result(start)
    if start > line_count:
        start = 0
        parser.issue("offset_reset", "earlier_context")
    stop = line_count if end is None else min(end, line_count)
    if stop < start:
        parser.issue("invalid_offset")
        return parser.result(start)
    offset = start
    for index, raw in enumerate(_iter_lines(data)):
        if index >= stop:
            break
        selected = index >= start
        try:
            row = _loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            # An incomplete tail may be completed by the writer before the next
            # Stop. Do not advance beyond it, even when end includes that line.
            if index == line_count - 1 and not raw.endswith(b"\n"):
                if selected:
                    parser.issue("partial_tail", "visible_content")
                break
            if selected:
                parser.issue("invalid_utf8" if isinstance(error, UnicodeDecodeError) else "malformed_json",
                             "visible_content")
                offset = index + 1
            continue
        parser.row(row, selected)
        if selected:
            offset = index + 1
    return parser.result(offset)


def parse_transcript(path, start=0, end=None, agent="auto"):
    """Safely read a path, then use the immutable snapshot parser.

    start is inclusive and end exclusive, in physical lines. No raw lines or
    paths enter error diagnostics. Callers that need two views of the same source
    should read_transcript_bytes once and pass its bytes to both parsers.
    """
    try:
        data = read_transcript_bytes(path)
    except TranscriptReadError as error:
        parser, start, _ = _slice_parser(start, end, agent)
        code = "transcript_unavailable" if str(error) == "transcript_unreadable" else str(error)
        parser.issue(code, "visible_content")
        return parser.result(start)
    return parse_transcript_bytes(data, start=start, end=end, agent=agent)
