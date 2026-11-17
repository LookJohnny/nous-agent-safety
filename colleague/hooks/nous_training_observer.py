"""Opt-in observation of immutable client request/response bytes.

No raw body, oracle answer, header, or exception text is written by this module.
This is a local trusted-integration boundary, not authenticated provider proof.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid

import nous_training as training
from nous_training_export import strict_json, record_digest
from nous_training_request import validate_request, normalize_exchange, request_messages, validate_record
from nous_training_transport import decode_response
from nous_training_verify import validate_oracle, evaluate_record

MAX_BYTES = 8 * 1024 * 1024


class ObserverError(ValueError):
    """Static local collection error codes."""


def _body(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ObserverError("request_bytes_required_or_limit_exceeded")
    try:
        return strict_json(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ObserverError("request_invalid_json") from None


@dataclass(frozen=True, repr=False)
class RequestTicket:
    provider: str
    request_bytes: bytes
    binding: tuple
    generation: tuple
    session_hash: str
    oracle_bytes: bytes | None
    manual: bool
    synthetic: bool
    ident: str
    home: str
    oracle_registration: str
    # One finish operation, including failures. Not a retry or relabeling handle.
    _lock: object = field(default_factory=threading.Lock, compare=False)
    _used: list = field(default_factory=list, compare=False)


def begin_request(provider, request_bytes, *, project, session, oracle=None, synthetic=False, manual=False,
                  oracle_registration="before_dispatch"):
    """Freeze the final outgoing bytes and an optional oracle BEFORE dispatch.

    Return None when continuous capture is off. manual=True explicitly permits
    this one exchange without enabling future capture. Credentials stay outside
    the body. No files are written here.
    """
    binding = training._bind_project(project)
    if type(manual) is not bool or type(synthetic) is not bool:
        raise ObserverError("invalid_observer_option")
    if oracle_registration not in ("before_dispatch", "before_response"):
        raise ObserverError("invalid_oracle_registration")
    if not manual and not training._allowed(binding[1], "capture"):
        return None
    with training._Lock():
        if not manual and not training._allowed(binding[1], "capture"):
            return None
        generation = training._revocation_generation(binding[1])
        training._check_project(binding)
    if type(session) is not str or not session or len(session) > 4096:
        raise ObserverError("request_session_required")
    validate_request(provider, _body(request_bytes))
    oracle_bytes = None
    if oracle is not None:
        frozen = validate_oracle(oracle, require_binding=False)
        sha = hashlib.sha256(request_bytes).hexdigest()
        if "request_sha256" in frozen and frozen["request_sha256"] != sha:
            raise ObserverError("oracle_request_binding_mismatch")
        frozen["request_sha256"] = sha
        oracle_bytes = training._json(validate_oracle(frozen)).encode("utf-8")
    return RequestTicket(provider, request_bytes, binding, generation,
                         training._hash(["request_session", session])[:24], oracle_bytes,
                         manual, synthetic, "t_" + uuid.uuid4().hex, str(training.HOME), oracle_registration)


def _authorized(ticket):
    return (str(training.HOME) == ticket.home
            and training._revocation_generation(ticket.binding[1]) == ticket.generation
            and (ticket.manual or training._allowed(ticket.binding[1], "capture")))


def finish_request(ticket, response_bytes, *, content_type="application/json"):
    """Store a scrubbed exchange and bound result evidence; return metadata only.

    Call after observing the complete actual response. Revocation or project
    replacement cannot be undone by a request that was already in flight.
    """
    if ticket is None:
        return {"status": "disabled"}
    if type(ticket) is not RequestTicket:
        raise ObserverError("invalid_request_ticket")
    with ticket._lock:
        if ticket._used:
            raise ObserverError("request_ticket_consumed")
        ticket._used.append(True)
    if not _authorized(ticket):
        return {"status": "disabled"}
    training._check_project(ticket.binding)
    response, transport = decode_response(ticket.provider, response_bytes, content_type)
    exchange = normalize_exchange(ticket.provider, _body(ticket.request_bytes), response, transport=transport)
    messages = request_messages(exchange)
    request_sha = hashlib.sha256(ticket.request_bytes).hexdigest()
    project, pid, _ = ticket.binding
    record = {
        "schema": "nous.trajectory/3", "id": ticket.ident,
        "agent": {"id": ticket.provider, "model": exchange["request"]["model"]},
        "exchange": exchange, "messages": messages,
        "quality": {"complete": True, "issues": [], "missing": []}, "evidence": [],
        "provenance": {"source": "request_observer", "observation_scope": "client_request_response",
            "project_id": pid, "session_id": ticket.session_hash, "session_group_id": ticket.session_hash,
            "synthetic": ticket.synthetic, "captured_at": int(time.time()),
            "request_sha256": request_sha, "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "authorization": "one_shot_import" if ticket.manual else "project_opt_in"},
    }
    record = training.scrub_record(record, project)
    record["provenance"]["request_content_sha256"] = training._hash(record["exchange"]["request"])
    validate_record(record, require_quality=False)
    verification = None
    if ticket.oracle_bytes is not None:
        verification = evaluate_record(record, json.loads(ticket.oracle_bytes), request_sha256=request_sha,
                                       response_unchanged=messages[-1] == record["messages"][-1],
                                       request_unchanged=exchange["request"] == record["exchange"]["request"])
        verification["oracle_registration"] = ticket.oracle_registration
        candidate = verification.get("evidence_candidate")
        if candidate is not None:
            record["evidence"].append({**candidate, "oracle_registration": ticket.oracle_registration, "at": int(time.time())})
    # Evidence is already static codes/hashes. No oracle expected answer persists.
    with training._Lock():
        if not _authorized(ticket):
            return {"status": "disabled"}
        training._check_project(ticket.binding)
        path = training._root() / "records" / (record["id"] + ".json")
        if path.exists() or path.is_symlink():
            raise ObserverError("request_record_already_exists")
        if ticket.manual:
            cfg = training._config()
            state = cfg["projects"].setdefault(pid, {"capture": False, "export": False})
            state["imports"] = sorted(set(state.get("imports", [])) | {record["id"]})
            state.update(purpose="local_training", version=1)
            training._atomic(training._root() / "config.json", cfg)
        training._atomic(path, record)
    return {"status": "collected", "id": record["id"], "schema": record["schema"],
            "record_digest": record_digest(record), "observation_scope": "client_request_response",
            "synthetic": ticket.synthetic, "verification": verification, "content_review_required": True}


def observe_request(provider, request_bytes, send, *, project, session, oracle=None, synthetic=False, manual=False):
    """Transparent integration: send exactly these bytes once, even if capture fails.

    The caller receives its transport response unchanged plus safe capture metadata.
    Transport exceptions propagate to the caller; collection exceptions do not.
    ``send(bytes)`` returns body bytes and content_type (HTTP status optional).
    """
    try:
        ticket = begin_request(provider, request_bytes, project=project, session=session,
                               oracle=oracle, synthetic=synthetic, manual=manual)
    except Exception:
        ticket, error = None, {"status": "collection_failed", "code": "request_capture_start_failed"}
    else:
        error = None
    result = send(request_bytes)
    if error:
        return result, error
    try:
        if result.get("status", 200) != 200:
            return result, {"status": "collection_failed", "code": "response_status_not_success"}
        report = finish_request(ticket, result["body"], content_type=result["content_type"])
    except Exception:
        report = {"status": "collection_failed", "code": "request_capture_finish_failed"}
    return result, report


def capture_file(provider, request_file, *, project, session, oracle_file=None, endpoint=None,
                 api_key_env=None, allow_loopback=False, synthetic=False):
    """Explicit one-shot CLI request; never read keys unless the named env is supplied."""
    from nous_training_transport import post_json, _endpoint
    def read(path, limit):
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise ObserverError("request_file_not_regular")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ObserverError("request_file_not_regular")
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise ObserverError("request_file_size_limit")
            return raw
    official = {"openai-responses": "https://api.openai.com/v1/responses",
                "anthropic-messages": "https://api.anthropic.com/v1/messages"}
    if provider not in official:
        raise ObserverError("unsupported_request_provider")
    endpoint = endpoint or official[provider]
    url = _endpoint(endpoint, allow_loopback)
    if url.scheme == "https" and endpoint != official[provider]:
        raise ObserverError("provider_endpoint_mismatch")
    headers = {"anthropic-version": "2023-06-01"} if provider == "anthropic-messages" else {}
    if api_key_env is not None:
        if url.scheme != "https":
            raise ObserverError("loopback_credentials_not_allowed")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env):
            raise ObserverError("invalid_api_key_environment_name")
        key = os.environ.get(api_key_env)
        if not key:
            raise ObserverError("api_key_environment_missing")
        headers["authorization" if provider == "openai-responses" else "x-api-key"] = (
            "Bearer " + key if provider == "openai-responses" else key)
    raw = read(request_file, MAX_BYTES)
    try:
        spec = strict_json(read(oracle_file, 256 * 1024).decode("utf-8")) if oracle_file else None
    except (ValueError, UnicodeError, RecursionError):
        raise ObserverError("oracle_file_invalid_json") from None
    ticket = begin_request(provider, raw, project=project, session=session, oracle=spec,
                           synthetic=synthetic, manual=True)
    result = post_json(endpoint, raw, headers=headers, allow_loopback=allow_loopback)
    if result["status"] != 200:
        return {"status": "collection_failed", "code": "response_status_not_success", "http_status": result["status"]}
    return finish_request(ticket, result["body"], content_type=result["content_type"])
