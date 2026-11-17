"""Explicit local multi-request capture. Raw bytes stay in memory only.

One serial episode handle, one in-flight ticket, at most 64 observed requests.
Only finish_episode persists a fully checked scrubbed candidate; process loss
before that point discards in-flight capture, never reconstructs missing steps.
"""
from dataclasses import dataclass, field
import copy
import hashlib
import json
import threading
import time
import uuid

import nous_training as training
from nous_training_observer import ObserverError, _body
from nous_training_request import TOOL_SCHEMA, validate_request, normalize_exchange, request_messages, exchange_state
from nous_training_transport import decode_response
from nous_training_verify import validate_oracle, evaluate_record
from nous_training_episode import SCHEMA, EPISODE_SCHEMA, POLICY, MAX_STEPS, validate_episode, episode_digest


@dataclass(frozen=True, repr=False)
class EpisodeHandle:
    provider: str
    binding: tuple
    generation: tuple
    session_hash: str
    oracle_template: bytes | None
    manual: bool
    synthetic: bool
    ident: str
    home: str
    oracle_registration: str
    _lock: object = field(default_factory=threading.Lock, compare=False)
    _state: dict = field(default_factory=lambda: {"steps": [], "pending": None, "closed": False,
                                               "request_unchanged": True, "response_unchanged": True}, compare=False)


@dataclass(frozen=True, repr=False)
class EpisodeTicket:
    owner: object
    index: int
    request_bytes: bytes


def begin_episode(provider, *, project, session, oracle=None, manual=False, synthetic=False,
                  oracle_registration="before_dispatch"):
    binding = training._bind_project(project)
    if provider not in ("openai-responses", "anthropic-messages"):
        raise ObserverError("unsupported_request_provider")
    if type(manual) is not bool or type(synthetic) is not bool:
        raise ObserverError("invalid_observer_option")
    if oracle_registration not in ("before_dispatch", "before_response"):
        raise ObserverError("invalid_oracle_registration")
    if not manual and not training._allowed(binding[1], "capture"):
        return None
    if type(session) is not str or not session or len(session) > 4096:
        raise ObserverError("request_session_required")
    frozen = training._json(validate_oracle(oracle, require_binding=False)).encode() if oracle is not None else None
    with training._Lock():
        if not manual and not training._allowed(binding[1], "capture"):
            return None
        generation = training._revocation_generation(binding[1])
        training._check_project(binding)
    return EpisodeHandle(provider, binding, generation, training._hash(["request_episode_session", session])[:24],
                         frozen, manual, synthetic, "t_" + uuid.uuid4().hex, str(training.HOME), oracle_registration)


def _authorized(handle):
    return (str(training.HOME) == handle.home
            and training._revocation_generation(handle.binding[1]) == handle.generation
            and (handle.manual or training._allowed(handle.binding[1], "capture")))


def _check(handle):
    if type(handle) is not EpisodeHandle:
        raise ObserverError("episode_handle_invalid")
    if handle._state["closed"]:
        raise ObserverError("episode_handle_consumed")


def begin_step(handle, request_bytes):
    if handle is None:
        return None
    if type(handle) is not EpisodeHandle:
        raise ObserverError("episode_handle_invalid")
    with handle._lock:
        _check(handle)
        state = handle._state
        if not _authorized(handle):
            state["closed"] = True
            return None
        training._check_project(handle.binding)
        if state["pending"] is not None:
            raise ObserverError("episode_step_in_flight")
        if len(state["steps"]) >= MAX_STEPS:
            state["closed"] = True
            raise ObserverError("episode_step_limit")
        try:
            validate_request(handle.provider, _body(request_bytes), schema=TOOL_SCHEMA)
            if not state["steps"] and handle.oracle_template is not None:
                oracle = json.loads(handle.oracle_template)
                sha = hashlib.sha256(request_bytes).hexdigest()
                if "request_sha256" in oracle and oracle["request_sha256"] != sha:
                    raise ObserverError("oracle_request_binding_mismatch")
                oracle["request_sha256"] = sha
                state["oracle"] = validate_oracle(oracle)
            ticket = EpisodeTicket(handle, len(state["steps"]), request_bytes)
            state["pending"] = ticket
            return ticket
        except Exception:
            state["closed"] = True
            raise


def _record(handle, steps):
    issues = sorted({issue for step in steps for issue in step["quality"]["issues"]})
    missing = sorted({item for step in steps for item in step["quality"]["missing"]})
    if exchange_state(steps[-1]["exchange"])["response_kind"] != "final_text":
        missing = sorted(set(missing) | {"episode_final_response"})
    complete = not issues and not missing and all(s["quality"]["complete"] for s in steps)
    return {"schema": SCHEMA, "id": handle.ident,
            "agent": {"id": handle.provider, "model": steps[-1]["exchange"]["request"]["model"]},
            "episode": {"schema": EPISODE_SCHEMA, "provider": handle.provider, "steps": copy.deepcopy(steps)},
            "messages": request_messages(steps[-1]["exchange"]),
            "quality": {"complete": complete, "issues": issues, "missing": missing},
            "privacy": {"scrubbed": True, "scrubber_version": "training/4-request-tools+p2.1", "tool_history_checked": True},
            "supervision_policy": POLICY, "evidence": [], "step_evidence": [],
            "provenance": {"source": "request_episode_observer", "observation_scope": "client_request_response_episode",
                "project_id": handle.binding[1], "session_id": handle.session_hash, "session_group_id": handle.session_hash,
                "request_sha256": steps[0]["provenance"]["request_sha256"], "synthetic": handle.synthetic,
                "authorization": "one_shot_import" if handle.manual else "project_opt_in", "captured_at": int(time.time())}}


def finish_step(handle, ticket, response_bytes, *, content_type="application/json"):
    if handle is None or ticket is None:
        return {"status": "disabled"}
    if type(handle) is not EpisodeHandle:
        raise ObserverError("episode_handle_invalid")
    with handle._lock:
        _check(handle)
        state = handle._state
        if type(ticket) is not EpisodeTicket or ticket.owner is not handle or state["pending"] is not ticket:
            raise ObserverError("episode_ticket_invalid_or_consumed")
        state["pending"] = None
        try:
            if not _authorized(handle):
                state["closed"] = True
                return {"status": "disabled"}
            training._check_project(handle.binding)
            response, transport = decode_response(handle.provider, response_bytes, content_type)
            exchange = normalize_exchange(handle.provider, _body(ticket.request_bytes), response,
                                          transport=transport, schema=TOOL_SCHEMA)
            safe = training.scrub_record({"schema": "nous.request-step/1", "exchange": exchange,
                                          "quality": {"complete": True, "issues": [], "missing": []}}, handle.binding[0])
            step = {"index": ticket.index, "exchange": safe["exchange"], "quality": safe["quality"], "privacy": safe["privacy"],
                    "provenance": {"project_id": handle.binding[1], "session_id": handle.session_hash,
                        "session_group_id": handle.session_hash,
                        "request_sha256": hashlib.sha256(ticket.request_bytes).hexdigest(),
                        "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                        "request_content_sha256": training._hash(safe["exchange"]["request"]),
                        "response_content_sha256": training._hash(safe["exchange"]["response"])}}
            candidate = _record(handle, state["steps"] + [step])
            validate_episode(candidate, require_quality=False)
            if not _authorized(handle):
                state["closed"] = True
                return {"status": "disabled"}
            state["request_unchanged"] &= exchange["request"] == safe["exchange"]["request"]
            state["response_unchanged"] &= exchange["response"] == safe["exchange"]["response"]
            state["steps"].append(step)
            return {"status": "captured", "step": ticket.index, "quality": step["quality"],
                    "response_kind": exchange_state(safe["exchange"])["response_kind"]}
        except Exception:
            state["closed"] = True
            state["steps"].clear()
            raise


def finish_episode(handle):
    if handle is None:
        return {"status": "disabled"}
    if type(handle) is not EpisodeHandle:
        raise ObserverError("episode_handle_invalid")
    with handle._lock:
        _check(handle)
        state = handle._state
        if state["pending"] is not None:
            raise ObserverError("episode_step_in_flight")
        state["closed"] = True
        if not _authorized(handle):
            state["steps"].clear()
            return {"status": "disabled"}
        if not state["steps"]:
            raise ObserverError("episode_empty")
        record = _record(handle, state["steps"])
        state["steps"].clear()
        validate_episode(record, require_quality=False)
        verification = None
        if "oracle" in state:
            verification = evaluate_record(record, state.pop("oracle"), request_sha256=record["provenance"]["request_sha256"],
                                           request_unchanged=state["request_unchanged"], response_unchanged=state["response_unchanged"])
            verification["oracle_registration"] = handle.oracle_registration
            if verification.get("evidence_candidate") is not None:
                record["evidence"].append({**verification["evidence_candidate"], "oracle_registration": handle.oracle_registration,
                                           "at": int(time.time())})
        # Re-scrub without regenerating hashes, then revalidate the immutable chain.
        record = training.scrub_record(record)
        with training._Lock():
            if not _authorized(handle):
                return {"status": "disabled"}
            training._check_project(handle.binding)
            path = training._root() / "records" / (record["id"] + ".json")
            if path.exists() or path.is_symlink():
                raise ObserverError("request_record_already_exists")
            if handle.manual:
                cfg = training._config()
                consent = cfg["projects"].setdefault(handle.binding[1], {"capture": False, "export": False})
                consent["imports"] = sorted(set(consent.get("imports", [])) | {record["id"]})
                consent.update(purpose="local_training", version=1)
                training._atomic(training._root() / "config.json", cfg)
            training._atomic(path, record)
        return {"status": "captured", "id": record["id"], "schema": SCHEMA, "steps": len(record["episode"]["steps"]),
                "record_digest": episode_digest(record), "quality": record["quality"], "synthetic": handle.synthetic,
                "verification": verification, "content_review_required": True, "tool_step_review_required": True}
