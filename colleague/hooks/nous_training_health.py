"""Bounded local capture health, without transcript text or source paths.

Callers own collection authorization: pass authorized=True only while holding
the collector's permission/revocation lock. Reads never create directories. A
pending result is an observation, not proof that a process is currently alive.
"""
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

SCHEMA = "nous.training.health/1"
MAX_PROJECTS = 128
MAX_PENDING = 8
MAX_STATE_BYTES = 1024 * 1024
COUNTER_MAX = 2**31 - 1
MAX_TIMESTAMP = 32503680000
LOCK_TIMEOUT = 0.5
AGENTS = frozenset({"claude", "codex", "unknown"})
FAILURE_CODES = frozenset({
    "collection_failed", "invalid_hook_input", "incomplete_hook_input",
    "transcript_missing", "transcript_unreadable", "transcript_too_large",
    "no_supported_messages", "record_too_large", "privacy_failed",
    "invalid_transcript", "stop_reply_not_flushed", "partial_tail",
    "collection_cancelled",
    "transcript_changed_during_read", "transcript_must_be_regular_file", "transcript_line_too_large", "transcript_too_many_lines",
    "transcript_session_mismatch",
    "project_changed_during_collection", "project_unavailable",
})
_PROJECT = re.compile(r"^p_[0-9a-f]{24}$")
_RECORD = re.compile(r"^t_[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_COUNTS = ("attempt_count", "captured_count", "failed_count", "abandoned_count")
_TIMES = ("last_attempt_at", "last_success_at", "last_failure_at", "last_abandoned_at")
_ROW_KEYS = {*_COUNTS, *_TIMES, "updated_at", "last_error_code", "last_record_id", "last_status", "pending"}


class HealthError(ValueError):
    """Static internal diagnostic; filesystem exception text is never returned."""


def _fail(code):
    raise HealthError(code)


def _identifier(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _identity(project_id, agent=None):
    if not _identifier(project_id, _PROJECT) or (agent is not None and (not isinstance(agent, str) or agent not in AGENTS)):
        _fail("health_invalid_identity")


def _now(value):
    if value is None:
        value = int(time.time())
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP:
        _fail("health_invalid_timestamp")
    return value


def _increment(value):
    return min(value + 1, COUNTER_MAX)


def _new_state():
    return {"schema": SCHEMA, "projects": {}, "evicted_projects": 0}


def _new_row():
    return {**{key: 0 for key in _COUNTS}, **{key: None for key in _TIMES},
            "updated_at": 0, "last_error_code": None, "last_record_id": None,
            "last_status": "attempted", "pending": {}}


def _validate(state):
    if not isinstance(state, dict) or set(state) != {"schema", "projects", "evicted_projects"} or state["schema"] != SCHEMA:
        _fail("health_state_corrupt")
    if type(state["evicted_projects"]) is not int or not 0 <= state["evicted_projects"] <= COUNTER_MAX:
        _fail("health_state_corrupt")
    projects = state["projects"]
    if not isinstance(projects, dict) or len(projects) > MAX_PROJECTS:
        _fail("health_state_corrupt")
    for project_id, agents in projects.items():
        if not _identifier(project_id, _PROJECT) or not isinstance(agents, dict) or not agents or set(agents) - AGENTS:
            _fail("health_state_corrupt")
        for row in agents.values():
            if not isinstance(row, dict) or set(row) != _ROW_KEYS:
                _fail("health_state_corrupt")
            if any(type(row[k]) is not int or not 0 <= row[k] <= COUNTER_MAX for k in _COUNTS):
                _fail("health_state_corrupt")
            if type(row["updated_at"]) is not int or not 0 <= row["updated_at"] <= MAX_TIMESTAMP:
                _fail("health_state_corrupt")
            for key in _TIMES:
                value = row[key]
                if value is not None and (type(value) is not int or not 0 <= value <= row["updated_at"]):
                    _fail("health_state_corrupt")
            if row["last_status"] not in ("attempted", "collected", "failed"):
                _fail("health_state_corrupt")
            if row["last_error_code"] is not None and (not isinstance(row["last_error_code"], str)
                    or row["last_error_code"] not in FAILURE_CODES | {"collection_abandoned"}):
                _fail("health_state_corrupt")
            if row["last_record_id"] is not None and not _identifier(row["last_record_id"], _RECORD):
                _fail("health_state_corrupt")
            pending = row["pending"]
            if not isinstance(pending, dict) or len(pending) > MAX_PENDING:
                _fail("health_state_corrupt")
            if any(not _identifier(key, _TOKEN) or type(at) is not int or not 0 <= at <= row["updated_at"]
                   for key, at in pending.items()):
                _fail("health_state_corrupt")


def _directory(home, create=False):
    home = Path(home)
    for index, path in enumerate((home, home / "training", home / "training" / "health")):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass  # Another authorized writer may create this level first.
            info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail("health_unsafe_path")
        # Do not change permissions of a pre-existing shared application home.
        # The health directory itself must always be private.
        if index == 2 and stat.S_IMODE(info.st_mode) != 0o700:
            _fail("health_permissions")
    return home / "training" / "health"


def _regular(path, *, write=False, create=False):
    flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if create:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                os.fchmod(fd, 0o600)
            except FileExistsError:
                fd = os.open(path, flags)
        else:
            fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        _fail("health_unsafe_path")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        _fail("health_permissions")
    return fd


@contextmanager
def _locked(home, *, create):
    folder = _directory(home, create)
    if folder is None:
        yield None
        return
    fd = _regular(folder / ".lock", write=True, create=create)
    if fd is None:
        _fail("health_lock_missing")
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _fail("health_busy")
                time.sleep(0.005)
        yield folder
    finally:
        os.close(fd)


def _load(folder):
    fd = _regular(folder / "state.json")
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_STATE_BYTES:
            _fail("health_state_too_large")
        raw = handle.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        _fail("health_state_too_large")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                _fail("health_state_corrupt")
            out[key] = value
        return out
    try:
        state = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail("health_state_corrupt"))
    except (ValueError, UnicodeError, RecursionError):
        _fail("health_state_corrupt")
    _validate(state)
    return state


def _save(folder, state):
    _validate(state)
    raw = (json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > MAX_STATE_BYTES:
        _fail("health_state_too_large")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, folder / "state.json")
        _sync_directory(folder)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sync_directory(folder):
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _public(row):
    return {**{key: copy.deepcopy(value) for key, value in row.items() if key != "pending"},
            "pending_count": len(row["pending"]),
            "counters_saturated": any(row[key] == COUNTER_MAX for key in _COUNTS)}


def _error(error):
    code = str(error) if isinstance(error, HealthError) else "health_io_failed"
    return {"status": "unavailable", "error": code}


def record_attempt(home, project_id, agent, *, authorized=False, now=None):
    """Record one collector attempt and return a random completion token.

    False/non-boolean authorization returns before inspecting paths or values.
    The caller must check authorization under its own revocation lock.
    """
    if authorized is not True:
        return {"status": "disabled"}
    try:
        _identity(project_id, agent)
        moment = _now(now)
        with _locked(home, create=True) as folder:
            state = _load(folder) or _new_state()
            projects = state["projects"]
            if project_id not in projects:
                if len(projects) >= MAX_PROJECTS:
                    oldest = min(projects, key=lambda pid: (max(row["updated_at"] for row in projects[pid].values()), pid))
                    del projects[oldest]
                    state["evicted_projects"] = _increment(state["evicted_projects"])
                projects[project_id] = {}
            row = projects[project_id].setdefault(agent, _new_row())
            if len(row["pending"]) >= MAX_PENDING:
                oldest = min(row["pending"], key=lambda token: (row["pending"][token], token))
                del row["pending"][oldest]
                row["abandoned_count"] = _increment(row["abandoned_count"])
                row["last_abandoned_at"] = max(row["last_abandoned_at"] or 0, moment)
                row["last_error_code"] = "collection_abandoned"
            for _ in range(8):
                token = secrets.token_hex(16)
                if token not in row["pending"]:
                    break
            else:
                _fail("health_token_generation_failed")
            row["pending"][token] = moment
            row["attempt_count"] = _increment(row["attempt_count"])
            row["last_attempt_at"] = max(row["last_attempt_at"] or 0, moment)
            if moment >= row["updated_at"]:
                row["last_status"] = "attempted"
            row["updated_at"] = max(row["updated_at"], moment)
            _save(folder, state)
            return {"status": "recorded", "attempt_id": token, "state": _public(row)}
    except (HealthError, OSError, ValueError, TypeError, RecursionError) as error:
        return _error(error)


def record_result(home, project_id, agent, attempt_id, *, authorized=False,
                  status, record_id=None, error_code=None, now=None):
    """Finish one known attempt at most once; never creates absent health state."""
    if authorized is not True:
        return {"status": "disabled"}
    try:
        _identity(project_id, agent)
        if not _identifier(attempt_id, _TOKEN) or status not in ("collected", "failed"):
            _fail("health_invalid_result")
        if status == "collected" and not _identifier(record_id, _RECORD):
            _fail("health_invalid_record_id")
        # Never persist an arbitrary exception message even if it resembles a code.
        code = error_code if isinstance(error_code, str) and error_code in FAILURE_CODES else "collection_failed"
        moment = _now(now)
        with _locked(home, create=False) as folder:
            if folder is None:
                return {"status": "missing"}
            state = _load(folder)
            if state is None:
                return {"status": "missing"}
            row = state["projects"].get(project_id, {}).get(agent)
            if row is None or attempt_id not in row["pending"]:
                return {"status": "ignored", "error": "health_attempt_not_pending"}
            del row["pending"][attempt_id]
            if status == "collected":
                row["captured_count"] = _increment(row["captured_count"])
                if row["last_success_at"] is None or moment >= row["last_success_at"]:
                    row["last_success_at"], row["last_record_id"] = moment, record_id
            else:
                row["failed_count"] = _increment(row["failed_count"])
                row["last_failure_at"] = max(row["last_failure_at"] or 0, moment)
                if moment >= max(row["last_failure_at"] or 0, row["last_abandoned_at"] or 0):
                    row["last_error_code"] = code
            if moment >= row["updated_at"]:
                row["last_status"] = status
            row["updated_at"] = max(row["updated_at"], moment)
            _save(folder, state)
            return {"status": "recorded", "state": _public(row)}
    except (HealthError, OSError, ValueError, TypeError, RecursionError) as error:
        return _error(error)


def read_health(home, project_id=None):
    """Read bounded atomic state without creating files or scanning any history."""
    try:
        if project_id is not None:
            _identity(project_id)
        folder = _directory(home, False)
        state = _load(folder) if folder is not None else None
        if state is None:
            return {"status": "missing", "schema": SCHEMA, "projects": {}}
        projects = {pid: {agent: _public(row) for agent, row in agents.items()}
                    for pid, agents in state["projects"].items() if project_id is None or project_id == pid}
        return {"status": "ok", "schema": SCHEMA, "projects": projects,
                "evicted_projects": state["evicted_projects"],
                "limits": {"projects": MAX_PROJECTS, "pending_per_platform": MAX_PENDING, "counter": COUNTER_MAX}}
    except (HealthError, OSError, ValueError, TypeError, RecursionError) as error:
        result = _error(error)
        result["status"] = "corrupt" if result["error"] in {"health_state_corrupt", "health_state_too_large"} else "unreadable"
        result["projects"] = {}
        return result


def purge_health(home, project_id=None):
    """Remove one project's health or all health; does not create absent paths.

    Caller authorization is supplied by the enclosing revoke/delete operation.
    Global purge can remove a corrupt state without reading its unsafe body.
    The existing lock remains, so concurrent authorized writers keep one lock.
    """
    try:
        if project_id is not None:
            _identity(project_id)
        with _locked(home, create=False) as folder:
            if folder is None:
                return {"status": "missing"}
            if project_id is None:
                fd = _regular(folder / "state.json")
                if fd is None:
                    return {"status": "missing"}
                os.close(fd)
                os.unlink(folder / "state.json")
                _sync_directory(folder)
                return {"status": "purged"}
            state = _load(folder)
            if state is None or project_id not in state["projects"]:
                return {"status": "missing"}
            del state["projects"][project_id]
            _save(folder, state)
            return {"status": "purged"}
    except (HealthError, OSError, ValueError, TypeError, RecursionError) as error:
        return _error(error)
