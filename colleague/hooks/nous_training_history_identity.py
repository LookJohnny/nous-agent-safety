"""Full-snapshot identity and ancestry checks for one-shot historical imports.

Pure, bounded, read-only code: no filesystem access and no content persistence.
The caller MUST pin/read the candidate path, compare its complete stat signature,
and pass these exact immutable bytes to the transcript parser too. This function
can check path-hash/size consistency, not authenticate bytes to an inode by itself.
It does not reconstruct branches, resolve parent files, or approve training.
Returned quality findings must be merged into the stored canonical record before
its digest/evidence is computed. Candidate hints may tighten, never relax, checks.
"""
import hashlib
import os

from nous_transcript import _check_bytes, _iter_lines, _loads, TranscriptReadError


_U64 = (1 << 64) - 1
_STAT = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
_ROLES = {"sessions", "archived_sessions", "projects", "explicit"}
_BRANCHES = {"main", "subagent", "branch", "sidechain", "unknown"}
_HINTS = {"inherited_history", "nonlegacy_history", "subagent_path", "subagent", "branch", "sidechain"}
_MESSAGE_ROWS = {"user", "assistant", "system"}
ERROR_CODES = frozenset({
    "history_agent_identity_mismatch", "history_agent_mismatch", "history_candidate_invalid",
    "history_family_mismatch", "history_identity_invalid", "history_identity_missing",
    "history_json_invalid", "history_metadata_invalid", "history_parent_identity_mismatch",
    "history_project_mismatch", "history_session_mismatch", "history_snapshot_limits",
    "history_snapshot_size_mismatch",
})


class HistoryIdentityError(ValueError):
    """Only closed static codes; never include a path, ID, or source excerpt."""


def _string(value, *, path=False):
    if (type(value) is not str or not value or len(value) > (4096 if path else 512)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HistoryIdentityError("history_identity_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise HistoryIdentityError("history_identity_invalid") from None
    if path and not os.path.isabs(value):
        raise HistoryIdentityError("history_identity_invalid")
    return os.path.normpath(value) if path else value


def _ordinal(value):
    if type(value) is not int or not 0 <= value <= _U64:
        raise HistoryIdentityError("history_metadata_invalid")
    return value


def _candidate(candidate, size):
    if not isinstance(candidate, dict):
        raise HistoryIdentityError("history_candidate_invalid")
    try:
        agent = candidate["agent"]
        path = _string(candidate["path"], path=True)
        session = _string(candidate["session_id"])
        cwd = _string(candidate["cwd"], path=True)
        info = candidate["stat"]
        hints = candidate["context_hints"]
        if (agent not in {"codex", "claude"} or candidate["path"] != path
                or candidate["cwd"] != cwd or candidate.get("compression", "none") not in {"none", "zstd"}
                or candidate["source_role"] not in _ROLES
                or candidate["branch_role"] not in _BRANCHES
                or not isinstance(hints, list) or any(type(x) is not str or x not in _HINTS for x in hints)
                or not isinstance(info, dict) or set(info) != _STAT
                or any(type(value) is not int or value < 0 for value in info.values())
                or candidate["source_id"] != hashlib.sha256(os.fsencode(path)).hexdigest()):
            raise HistoryIdentityError("history_candidate_invalid")
    except (KeyError, TypeError, UnicodeError):
        raise HistoryIdentityError("history_candidate_invalid") from None
    # For zstd, stat describes compressed physical bytes. The caller's secure
    # reader must bind those bytes and bound expansion; _check_bytes still
    # checks this complete expanded snapshot before identity parsing.
    if candidate.get("compression", "none") == "none" and info["size"] != size:
        raise HistoryIdentityError("history_snapshot_size_mismatch")
    return agent, session, cwd


class _Validator:
    def __init__(self, candidate, size):
        self.agent, self.session, self.cwd = _candidate(candidate, size)
        self.issues, self.missing = set(), set()
        self.seen_identity = False
        self.contexts = set()
        self.family = None
        self.parents = {}
        self.parent_unknown = set()
        self.message_nodes = set()
        self.order = {}
        self.agent_ids = set()
        if (candidate["branch_role"] in {"subagent", "branch", "sidechain"}
                or set(candidate["context_hints"]) & {"inherited_history", "subagent_path", "subagent", "branch", "sidechain"}
                or (self.agent == "claude" and "subagents" in candidate["path"].split(os.path.sep)[:-1])):
            self.issue("history_inherited_context", "history_ancestry")
        if "nonlegacy_history" in candidate["context_hints"]:
            self.issue("history_nonlegacy_mode", "history_replay_semantics")

    def issue(self, code, missing):
        self.issues.add(code)
        self.missing.add(missing)

    def project(self, value):
        cwd = _string(value, path=True)
        if cwd != self.cwd:
            raise HistoryIdentityError("history_project_mismatch")
        self.contexts.add(cwd)

    def identity(self, value):
        if _string(value) != self.session:
            raise HistoryIdentityError("history_session_mismatch")
        self.seen_identity = True

    def root(self, value):
        value = _string(value)
        if self.family is not None and self.family != value:
            raise HistoryIdentityError("history_family_mismatch")
        self.family = value

    def codex(self, row):
        if "sessionId" in row or row.get("type") in {"user", "assistant"}:
            raise HistoryIdentityError("history_agent_mismatch")
        kind, payload = row.get("type"), row.get("payload")
        if kind in {"session_meta", "turn_context", "event_msg"} and not isinstance(payload, dict):
            raise HistoryIdentityError("history_metadata_invalid")
        if kind == "session_meta":
            self.identity(payload.get("id"))
            self.project(payload.get("cwd"))
            if "session_id" in payload:
                self.root(payload["session_id"])
            for key in ("forked_from_id", "parent_thread_id"):
                if payload.get(key) is not None:
                    _string(payload[key])
                    self.issue("history_inherited_context", "history_ancestry")
            for key in ("forked_from_ordinal_exclusive", "subagent_history_start_ordinal"):
                if payload.get(key) is not None:
                    _ordinal(payload[key])
                    self.issue("history_inherited_context", "history_ancestry")
            mode = payload.get("history_mode", "legacy")
            if type(mode) is not str:
                raise HistoryIdentityError("history_metadata_invalid")
            if mode != "legacy":
                self.issue("history_nonlegacy_mode", "history_replay_semantics")
            base = payload.get("history_base")
            if base is not None:
                if (not isinstance(base, dict)
                        or set(base) != {"thread_id", "end_ordinal_exclusive", "end_byte_offset"}):
                    raise HistoryIdentityError("history_metadata_invalid")
                _string(base["thread_id"])
                _ordinal(base["end_ordinal_exclusive"])
                _ordinal(base["end_byte_offset"])
                self.issue("history_inherited_context", "history_ancestry")
            if "attached_thread" in payload:
                if type(payload["attached_thread"]) is not bool:
                    raise HistoryIdentityError("history_metadata_invalid")
                if payload["attached_thread"]:
                    self.issue("history_inherited_context", "history_ancestry")
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                self.issue("history_inherited_context", "history_ancestry")
            elif source is not None and not isinstance(source, (dict, str)):
                raise HistoryIdentityError("history_metadata_invalid")
        elif kind == "turn_context" and "cwd" in payload:
            self.project(payload["cwd"])
        elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if not isinstance(settings, dict) or "cwd" not in settings:
                raise HistoryIdentityError("history_metadata_invalid")
            self.project(settings["cwd"])

    def claude(self, row, position):
        if row.get("type") in {"session_meta", "response_item", "turn_context"}:
            raise HistoryIdentityError("history_agent_mismatch")
        if "sessionId" in row:
            self.identity(row["sessionId"])
        if "cwd" in row:
            self.project(row["cwd"])
        if "isSidechain" in row:
            if type(row["isSidechain"]) is not bool:
                raise HistoryIdentityError("history_metadata_invalid")
            if row["isSidechain"]:
                self.issue("history_inherited_context", "history_ancestry")
        if row.get("agentId") is not None:
            self.agent_ids.add(_string(row["agentId"]))
            if len(self.agent_ids) > 1:
                raise HistoryIdentityError("history_agent_identity_mismatch")
            self.issue("history_inherited_context", "history_ancestry")
        if "logicalParentUuid" in row and row["logicalParentUuid"] is not None:
            _string(row["logicalParentUuid"])
            self.issue("history_inherited_context", "history_ancestry")
        native_message = row.get("type") in _MESSAGE_ROWS
        node = row.get("uuid")
        if node is None:
            if "parentUuid" in row or native_message:
                self.issue("history_parent_chain_unavailable", "history_branch_context")
            return
        node = _string(node)
        self.order.setdefault(node, position)
        if native_message:
            self.message_nodes.add(node)
        if "parentUuid" not in row:
            self.parent_unknown.add(node)
            if native_message:
                self.issue("history_parent_chain_unavailable", "history_branch_context")
            return
        parent = row["parentUuid"]
        if parent is not None:
            parent = _string(parent)
        if node in self.parents and self.parents[node] != parent:
            raise HistoryIdentityError("history_parent_identity_mismatch")
        self.parents[node] = parent

    def graph(self):
        children = {}
        roots = 0
        for node, parent in self.parents.items():
            if parent is None:
                roots += int(node in self.message_nodes)
                continue
            children[parent] = children.get(parent, 0) + 1
            if parent not in self.parents and parent not in self.parent_unknown:
                self.issue("history_missing_parent", "history_branch_context")
            elif parent in self.parent_unknown:
                self.issue("history_parent_chain_unavailable", "history_branch_context")
            elif self.order[parent] >= self.order[node]:
                self.issue("history_parent_order", "history_branch_context")
        if any(count > 1 for count in children.values()) or roots > 1:
            self.issue("history_branch", "history_branch_context")
        # Iterative graph walk; deep or cyclic chains never recurse.
        finished = set()
        for start in self.parents:
            if start in finished:
                continue
            path = set()
            node = start
            while node is not None and node in self.parents and node not in finished:
                if node in path:
                    self.issue("history_parent_cycle", "history_branch_context")
                    break
                path.add(node)
                node = self.parents[node]
            finished.update(path)

    def result(self):
        if not self.seen_identity or not self.contexts:
            raise HistoryIdentityError("history_identity_missing")
        if self.agent == "claude":
            self.graph()
        family = self.family or self.session
        # Domain separation prevents cross-provider identity collisions. This
        # is a grouping hint only, never evidence of a reconstructed history.
        digest = hashlib.sha256(("nous.history.family/1\0" + self.agent + "\0" + family).encode()).hexdigest()
        return {"issues": sorted(self.issues), "missing": sorted(self.missing),
                "session_group_id": digest[:24], "project_contexts": len(self.contexts)}


def validate_snapshot(snapshot, candidate):
    """Recheck one complete immutable snapshot; reject identity ambiguity.

    Unsupported ancestry is an explicit quality gap, not synthesized context.
    Malformed JSON, duplicate keys, or invalid metadata fail closed because a
    later identity cannot safely be inferred from a partially parsed document.
    """
    try:
        _check_bytes(snapshot)
    except TranscriptReadError:
        raise HistoryIdentityError("history_snapshot_limits") from None
    check = _Validator(candidate, len(snapshot))
    for position, line in enumerate(_iter_lines(snapshot)):
        try:
            row = _loads(line.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise HistoryIdentityError("history_json_invalid") from None
        if not isinstance(row, dict) or not isinstance(row.get("type"), str):
            raise HistoryIdentityError("history_metadata_invalid")
        if check.agent == "codex":
            check.codex(row)
        else:
            check.claude(row, position)
    return check.result()
