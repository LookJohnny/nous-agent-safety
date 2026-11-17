"""Read-only, bounded candidate checklists over one verified private snapshot.

This describes the existing export and independent evidence dimensions. It never
edits records, creates annotations, or makes a new training eligibility decision.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import tempfile

import nous_training_audit as audit
from nous_training_export import (
    CODEMODE_PROFILE, REQUEST_PROFILE, EPISODE_PROFILE, InvalidRecord, _evidence, _quality, _safe_id,
    negative_evidence, record_digest, training_evidence,
)
from nous_training_validate import validate_dataset

SCHEMA = "nous.training.diagnosis/1"
MAX_DIAGNOSIS_RECORDS = 1000
MAX_REPORT_BYTES = 8 * 1024 * 1024
AUDIT_ERRORS = frozenset({"dataset_changed_during_audit", "audit_source_unavailable", "audit_manifest_too_large",
                          "audit_dataset_too_large", "audit_line_too_large", "audit_too_many_records", "dataset_validation_failed"})
NONBLOCKING_REJECTIONS = frozenset({"duplicate_sft", "duplicate_preference", "duplicate_trajectory", "duplicate_preference_observation"})
CONFLICTS = frozenset({"negative_or_conflicting_evidence", "cross_record_evidence_conflict", "conflicting_preference_direction"})
TAIL_GAPS = frozenset({"partial_tail", "stop_reply_not_flushed", "incomplete_final", "final_answer", "terminal_response",
                       "transcript_changed_during_read", "unresolved_tool_call", "pending_tool_calls", "missing_tool_output",
                       "tool_linkage", "tool_output", "codemode_pending_tool_call", "codemode_final_response_missing",
                       "episode_final_response", "episode_final_response_missing"})
RUNTIME_GAPS = frozenset({"unresolved_world_state", "unresolved_turn_context", "unresolved_thread_settings",
                          "unresolved_history_metadata", "sampling_context", "runtime_context", "runtime_context_semantics",
                          "runtime_configuration", "runtime_version_changed", "unsupported_configuration_update",
                          "unsupported_history_metadata_fields", "unsupported_world_state_fields", "codemode_unresolved_runtime_context",
                          "history_nonlegacy_mode", "history_replay_semantics"})
CONTEXT_GAPS = frozenset({"system_prompt", "branch_context", "pre_compaction_context", "session_context",
                          "earlier_context", "visible_content", "streamed_content", "compacted", "sidechain", "request_context"})
ADAPTER_GAPS = RUNTIME_GAPS | frozenset({"unknown_response_item", "unknown_record_type", "unknown_event_message",
    "unsupported_runtime_execution", "unsupported_tool_contract", "unknown_content_block", "media_omitted",
    "unsupported_content", "unsupported_content_fields", "codemode_unsupported_event", "codemode_unsupported_contract",
    "codemode_unsupported_grammar", "unsupported_custom_tool", "unsupported_record_fields", "unsupported_event_fields",
    "unsupported_runtime_execution_fields", "unsupported_runtime_result_fields", "unsupported_tool_contract_fields",
    "historical_native_protocol_unavailable"})
HISTORICAL_WORKSPACE_GAPS = frozenset({"historical_workspace", "historical_workspace_unavailable"})
HISTORICAL_WORKSPACE_STATE_GAPS = frozenset({"historical_workspace_state_unverified", "historical_workspace_state"})
HISTORICAL_SOURCE_GAPS = frozenset({"historical_tool_payloads_omitted", "historical_native_protocol_unavailable"})
HISTORY_ANCESTRY_GAPS = frozenset({"history_inherited_context", "history_ancestry", "history_branch",
    "history_branch_context", "history_missing_parent", "history_parent_order", "history_parent_chain_unavailable",
    "history_parent_cycle"})


def _base():
    return {"schema": SCHEMA, "status": "not_diagnosed", "structural_valid": False, "diagnosis_available": False,
            "training_rows_present": False, "training_eligibility_not_reassessed": True,
            "dataset_manifest_sha256": None, "counts": None, "existing_export_view": None,
            "aggregate": None, "records": [], "reasons": [], "validation_errors": [],
            "limits": {"canonical_trajectories": MAX_DIAGNOSIS_RECORDS, "report_bytes": MAX_REPORT_BYTES},
            "known_facts_scope": "validated_snapshot_and_declared_metadata",
            "required_evidence_scope": "checklist_only_not_a_positive_label"}


def _code(value):
    return audit._bucket(value, audit.VALIDATION_CODES)


def _gap_list(value):
    if not isinstance(value, list):
        return ["unknown"]
    return sorted({audit._bucket(item, audit.GAP_CODES) for item in value})


def _declared_quality(value):
    value = value if isinstance(value, dict) else {}
    complete = value.get("complete")
    return {"complete": complete if type(complete) is bool else None,
            "issues": _gap_list(value.get("issues")), "missing": _gap_list(value.get("missing"))}


def _quality_facts(record):
    declared = _declared_quality(record.get("quality"))
    protocol = _declared_quality(record.get("protocol", {}).get("quality")) if record["schema"] == "nous.trajectory/2" else None
    issues, missing, codes = set(declared["issues"]), set(declared["missing"]), set()
    if protocol is not None:
        issues.update(protocol["issues"])
        missing.update(protocol["missing"])
        # CodeMode requires complete context on both views, with no exceptions.
        if any(q["complete"] is not True or q["issues"] or q["missing"] for q in (declared, protocol)):
            codes.add("codemode_quality_incomplete")
        allowed = set()
    elif record["schema"] == "nous.trajectory/4":
        from nous_training_request import exchange_state
        observations = [declared] + [_declared_quality(step["quality"]) for step in record["episode"]["steps"]]
        for q in observations:
            issues.update(q["issues"]); missing.update(q["missing"])
        if any(q["complete"] is not True or q["issues"] or q["missing"] for q in observations):
            codes.add("episode_quality_incomplete")
        if (record["privacy"].get("scrubbed") is not True or any(
                step["privacy"].get("scrubbed") is not True or step["privacy"].get("tool_history_checked") is not True
                for step in record["episode"]["steps"])):
            codes.add("episode_privacy_not_checked")
        if exchange_state(record["episode"]["steps"][-1]["exchange"])["response_kind"] != "final_text":
            codes.add("episode_final_response_missing")
        allowed = set()
    elif record["schema"] == "nous.trajectory/3":
        # A request snapshot must be complete on its own. The transcript v1
        # exemptions cannot establish which native request context was sent.
        if declared["complete"] is not True or issues or missing:
            codes.add("request_quality_incomplete")
        allowed = set()
    else:
        try:
            _quality(record)  # Reuse the exporter's v1 gate; also enumerate all visible gaps below.
        except InvalidRecord as error:
            codes.add(_code(str(error)))
        except (TypeError, ValueError, KeyError):
            codes.add("invalid_quality_report")
        uses_tools = any(block.get("type") == "tool_use" for message in record["messages"]
                         for block in (message["content"] if isinstance(message.get("content"), list) else [])
                         if isinstance(block, dict))
        allowed = {"system_prompt"} | (set() if uses_tools else {"tool_definitions"})
    blocking_missing = missing - allowed
    codes.update(issues)
    codes.update(blocking_missing)
    if declared["complete"] is not True:
        codes.add("incomplete_trajectory")
    return {"declared": declared, "protocol": protocol, "issues": sorted(issues), "missing": sorted(missing),
            "blocking_missing": sorted(blocking_missing), "informational_missing": sorted(missing & allowed),
            "blocker_codes": sorted(codes)}


def _evidence_facts(record, digest):
    annotations = record.get("evidence", [])
    binding = {"bound_entries": 0, "stale_entries": 0, "invalid_entries": 0}
    if not isinstance(annotations, list):
        binding["invalid_entries"] = 1
        annotations = []
    for entry in annotations:
        if not isinstance(entry, dict):
            binding["invalid_entries"] += 1
        elif entry.get("record_digest") == digest:
            binding["bound_entries"] += 1
        else:
            binding["stale_entries"] += 1
    negative = negative_evidence(record, digest)
    try:
        kinds = _evidence(record, digest)
        outcome = {"status": "satisfied", "code": None, "qualifying_kinds": kinds}
    except InvalidRecord as error:
        code = _code(str(error))
        state = "missing" if code == "verified_outcome_missing" else "blocked_negative_or_conflicting" if code in CONFLICTS else "invalid"
        outcome = {"status": state, "code": code, "qualifying_kinds": []}
    except (TypeError, ValueError, KeyError):
        outcome = {"status": "invalid", "code": "invalid_evidence", "qualifying_kinds": []}
    content_required = record["schema"] in {"nous.trajectory/2", "nous.trajectory/3", "nous.trajectory/4"}
    content = {"required": content_required,
               "required_for_ordered_event_training": record["schema"] == "nous.trajectory/2",
               "required_for_request_training": record["schema"] == "nous.trajectory/3",
               "required_for_episode_training": record["schema"] == "nous.trajectory/4",
               "status": "not_required", "code": None, "qualifying_kinds": []}
    if content_required:
        # training_evidence first checks outcome, so it cannot report both missing
        # dimensions at once. Apply its exact content branch independently, without
        # inserting a synthetic outcome or changing the supplied record.
        expected = {"content_review": ("cleared", "reviewer"), "content_verifier": ("passed", "verifier")}
        valid = set()
        for entry in annotations:
            if not isinstance(entry, dict) or entry.get("record_digest") != digest:
                continue
            kind = entry.get("kind")
            rule = expected.get(kind) if isinstance(kind, str) else None
            if rule and entry.get("result") == rule[0] and isinstance(entry.get(rule[1]), str) and entry[rule[1]].strip():
                valid.add(kind)
        content.update(status="satisfied" if valid else "missing", code=None if valid else "custom_content_review_missing",
                       qualifying_kinds=sorted(valid))
        if outcome["status"] == "invalid":
            content.update(status="invalid", code="invalid_evidence", qualifying_kinds=[])
        if outcome["status"] == "satisfied":
            try:
                profile = (EPISODE_PROFILE if record["schema"] == "nous.trajectory/4" else
                           REQUEST_PROFILE if record["schema"] == "nous.trajectory/3" else CODEMODE_PROFILE)
                both = training_evidence(record, digest, "sft", profile)
                if content["qualifying_kinds"] != sorted(set(both) & set(expected)):
                    raise InvalidRecord("invalid_evidence")
            except InvalidRecord as error:
                code = _code(str(error))
                if code != "custom_content_review_missing":
                    content.update(status="invalid", code=code, qualifying_kinds=[])
            except (TypeError, ValueError, KeyError):
                content.update(status="invalid", code="invalid_evidence", qualifying_kinds=[])
    if negative:
        outcome.update(status="blocked_negative_or_conflicting", code="negative_or_conflicting_evidence", qualifying_kinds=[])
        if content_required:
            content.update(status="blocked_negative_or_conflicting", code="negative_or_conflicting_evidence", qualifying_kinds=[])
    return {"outcome": outcome, "content": content, "digest_binding": binding, "bound_negative_present": negative}


def _actions(record, quality, evidence, rejected, blocked):
    needed = {}
    def add(action, material, basis):
        entry = needed.setdefault(action, {"action": action, "required_materials": set(), "based_on_codes": set(),
                                           "result": "reinspection_required_no_automatic_eligibility"})
        entry["required_materials"].update(material)
        entry["based_on_codes"].update(basis)
    codes = set(quality["blocker_codes"]) | {item["code"] for item in rejected}
    if codes & HISTORICAL_WORKSPACE_GAPS:
        add("recover_historical_workspace", {"authorized_workspace_snapshot_or_verified_relocation",
            "historical_file_exclusion_and_privacy_context", "digest_bound_original_transcript"}, codes & HISTORICAL_WORKSPACE_GAPS)
    if codes & HISTORICAL_WORKSPACE_STATE_GAPS:
        add("verify_historical_workspace_state", {"original_workspace_snapshot_at_session_time",
            "historical_tracked_untracked_and_ignore_state", "digest_bound_original_transcript"}, codes & HISTORICAL_WORKSPACE_STATE_GAPS)
    if codes & HISTORICAL_SOURCE_GAPS:
        add("recover_original_source", {"digest_bound_original_transcript", "actually_retained_tool_payloads_or_native_events",
            "verified_privacy_context_before_reinspection"}, codes & HISTORICAL_SOURCE_GAPS)
    if codes & HISTORY_ANCESTRY_GAPS:
        add("recover_history_ancestry", {"authorized_parent_or_base_history", "verified_native_branch_edges_and_order",
            "documented_history_replay_semantics"}, codes & HISTORY_ANCESTRY_GAPS)
    if codes & TAIL_GAPS:
        if record["schema"] in {"nous.trajectory/3", "nous.trajectory/4"}:
            add("capture_request_context", {"original_final_client_request_response"}, codes & TAIL_GAPS)
        else:
            add("recover_original_source", {"original_authorized_transcript", "actually_observed_missing_tail_or_tool_output"}, codes & TAIL_GAPS)
    if codes & RUNTIME_GAPS:
        add("capture_request_context", {"original_ordered_request_context", "observed_sampling_and_runtime_configuration"}, codes & RUNTIME_GAPS)
    if codes & CONTEXT_GAPS:
        add("capture_request_context", {"actually_observed_original_model_context"}, codes & CONTEXT_GAPS)
    if codes & ADAPTER_GAPS:
        add("implement_runtime_adapter", {"documented_native_semantics", "lossless_adapter_and_synthetic_regression"}, codes & ADAPTER_GAPS)
    tool_gaps = codes & {"tool_definitions", "tool_schema_missing", "codemode_tool_contract_missing"}
    if tool_gaps:
        if record["schema"] == "nous.trajectory/1":
            add("supply_observed_tool_contracts", {"actual_function_schemas_from_same_session", "existing_import_or_recover_context_api"}, tool_gaps)
        else:
            add("capture_request_context", {"original_ordered_tool_catalog"}, tool_gaps)
    if blocked:
        add("resolve_negative_or_conflicting_evidence", {"independent_task_correction_or_new_observed_trajectory",
            "preserve_existing_negative_evidence"}, codes & CONFLICTS or {"negative_or_conflicting_evidence"})
    else:
        if evidence["outcome"]["status"] == "missing":
            add("verify_task_outcome", {"independent_whole_task_evaluation", "digest_bound_truthful_outcome_record"}, {"verified_outcome_missing"})
        if evidence["content"]["status"] == "missing":
            material = ("inspection_of_exact_scrubbed_native_request_and_response" if record["schema"] in {"nous.trajectory/3", "nous.trajectory/4"}
                        else "inspection_of_exact_scrubbed_context_and_custom_code")
            add("review_exact_scrubbed_content", {material, "digest_bound_content_decision"}, {"custom_content_review_missing"})
    handled = TAIL_GAPS | ADAPTER_GAPS | CONTEXT_GAPS | HISTORICAL_WORKSPACE_GAPS | HISTORICAL_WORKSPACE_STATE_GAPS | HISTORICAL_SOURCE_GAPS | HISTORY_ANCESTRY_GAPS | {"tool_definitions", "tool_schema_missing", "codemode_tool_contract_missing"} | CONFLICTS
    generic = {"incomplete_trajectory", "codemode_quality_incomplete", "request_quality_incomplete", "episode_quality_incomplete", "source_quality_issues", "missing_context",
               "verified_outcome_missing", "custom_content_review_missing"} | NONBLOCKING_REJECTIONS
    unexplained = codes - handled - generic
    if unexplained or any(evidence[key]["status"] == "invalid" for key in ("outcome", "content")):
        add("manual_inspection", {"authorized_original_source_and_export_policy"}, unexplained or {"invalid_evidence"})
    # V1 rejections can be shared by equal ID/content with different quality
    # observations. Only this row's declared quality can justify this action.
    if set(quality["blocker_codes"]) & {"incomplete_trajectory", "codemode_quality_incomplete", "request_quality_incomplete", "episode_quality_incomplete"} and not quality["issues"] and not quality["blocking_missing"]:
        add("manual_inspection", {"basis_for_declared_incompleteness"}, {"incomplete_trajectory"})
    return [{**item, "required_materials": sorted(item["required_materials"]), "based_on_codes": sorted(item["based_on_codes"])}
            for _, item in sorted(needed.items())]


def _episode_facts(record, digest, targets):
    from nous_training_episode import _step_review
    from nous_training_request import exchange_state
    steps = []
    for index, step in enumerate(record["episode"]["steps"]):
        state = exchange_state(step["exchange"])
        review = _step_review(record, index, digest)
        steps.append({"step": index, "response_kind": state["response_kind"],
            "quality": _declared_quality(step["quality"]),
            "privacy_checked": step["privacy"].get("scrubbed") is True and step["privacy"].get("tool_history_checked") is True,
            "bound_review_status": review,
            "independent_step_review_required": state["response_kind"] == "tool_calls",
            "action_if_supervising_this_step": ("preserve_negative_step_review" if review == "negative" else
                "independent_step_review_required" if state["response_kind"] == "tool_calls" and review == "unreviewed" else "none"),
            "sft_rows_from_step": len(targets.get(index, set()))})
    return {"steps": steps, "known_facts_scope": "declared_observations_and_digest_bound_step_reviews",
            "reported_conflict_scope": "may_apply_to_individual_target_or_whole_record",
            "training_eligibility_not_reassessed": True}


def _diagnose(snapshot, manifest, raw_manifest):
    # No record ID, source identity, annotation author, body or tool name is output.
    rejections = collections.defaultdict(collections.Counter)
    for _, item in audit._rows(snapshot / "rejected.jsonl"):
        key = (item.get("id"), item.get("record_digest"))
        rejections[key][(audit._bucket(item.get("stage"), audit.STAGES), _code(item.get("reason")))] += 1
    emitted = collections.defaultdict(set)
    emitted_targets = collections.defaultdict(lambda: collections.defaultdict(set))
    for entry in manifest["lineage"]:
        if entry["format"] == "sft":
            emitted[entry["source_line"]].add(entry["line"])
            if isinstance(entry.get("target"), dict) and type(entry["target"].get("step")) is int:
                emitted_targets[entry["source_line"]][entry["target"]["step"]].add(entry["line"])
    records = []
    for ordinal, record in audit._rows(snapshot / "trajectories.jsonl"):
        if ordinal > MAX_DIAGNOSIS_RECORDS:
            raise ValueError("diagnosis_record_limit_exceeded")
        digest = record_digest(record)
        related = [{"stage": stage, "code": code, "count": count}
                   for (stage, code), count in sorted(rejections.get((_safe_id(record["id"]), digest), {}).items())]
        quality, evidence = _quality_facts(record), _evidence_facts(record, digest)
        blocked = evidence["bound_negative_present"] or any(item["code"] in CONFLICTS for item in related)
        known = set(quality["blocker_codes"])
        known.update(evidence[key]["code"] for key in ("outcome", "content") if evidence[key]["code"])
        reported = sorted({item["code"] for item in related} - NONBLOCKING_REJECTIONS)
        records.append({"trajectory_ordinal": ordinal, "record_digest": digest,
                        "schema": audit._bucket(record["schema"], audit.RECORD_SCHEMAS),
                        "agent": audit._bucket(record.get("agent", {}).get("id") if isinstance(record.get("agent"), dict) else None, audit.AGENTS),
                        "existing_export": {"sft_rows_from_this_source": len(emitted[ordinal]),
                            "status": "included" if emitted[ordinal] else "not_included"},
                        "quality": quality, "evidence": evidence, "known_blocker_codes": sorted(known),
                        "reported_rejections": related, "reported_blocker_codes": reported,
                        "blocked_by_negative_or_reported_conflict": blocked,
                        "required_actions": _actions(record, quality, evidence, related, blocked)})
        if record["schema"] == "nous.trajectory/4":
            records[-1]["episode"] = _episode_facts(record, digest, emitted_targets[ordinal])
    aggregate = audit._aggregate(snapshot, manifest)
    report = _base()
    report.update(status="diagnosed", structural_valid=True, diagnosis_available=True,
                  training_rows_present=aggregate["training_rows_present"], dataset_manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
                  counts=aggregate["counts"], aggregate=aggregate, records=records,
                  existing_export_view={"profile": manifest["profile"], "counts": aggregate["counts"],
                                        "basis": "validated_training_files_and_lineage"}, reasons=aggregate["reasons"])
    return report


def diagnose_dataset(path):
    """Describe existing data, never annotate, repair, export, or reapprove it."""
    validated, raw_manifest, manifest = False, None, None
    try:
        source = Path(path)
        raw_manifest = audit._manifest_bytes(source)
        manifest = audit._copy_manifest(raw_manifest)
        if manifest["files"]["trajectories.jsonl"]["records"] > MAX_DIAGNOSIS_RECORDS:
            report = _base(); report["reasons"] = ["diagnosis_record_limit_exceeded"]
            return report
        with tempfile.TemporaryDirectory(prefix="nous-diagnosis-") as temporary:
            snapshot = Path(temporary)
            os.chmod(snapshot, 0o700)
            manifest = audit._snapshot(source, snapshot, raw_manifest)
            result = validate_dataset(snapshot)
            if result.get("valid") is not True:
                safe = audit._invalid(result.get("errors", []), "snapshot_validation_failed")
                report = _base(); report.update(validation_errors=safe["validation_errors"], reasons=safe["reasons"])
                return report
            validated = True
            report = _diagnose(snapshot, manifest, raw_manifest)
            if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode()) > MAX_REPORT_BYTES:
                raise ValueError("diagnosis_report_limit_exceeded")
            return report
    except audit.AuditError as error:
        report = _base()
        report["reasons"] = [str(error) if str(error) in AUDIT_ERRORS else "diagnosis_failed"]
        if isinstance(error.validation_code, str) and error.validation_code in audit.VALIDATION_CODES:
            report["validation_errors"] = [{"code": error.validation_code, "count": 1}]
        return report
    except Exception as error:
        report = _base()
        code = str(error) if type(error) is ValueError and str(error) in {"diagnosis_record_limit_exceeded", "diagnosis_report_limit_exceeded"} else "diagnosis_failed"
        report.update(structural_valid=validated, reasons=[code])
        if validated:
            report["dataset_manifest_sha256"] = hashlib.sha256(raw_manifest).hexdigest()
            report["training_rows_present"] = manifest["files"]["sft.jsonl"]["records"] + manifest["files"]["dpo.jsonl"]["records"] > 0
        return report
