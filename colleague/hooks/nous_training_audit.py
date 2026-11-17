"""Aggregate-only audit of a verified private snapshot; no training or upload."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from nous_training_export import InvalidRecord, record_digest, preference_digest, strict_json
from nous_training_validate import FILES, validate_dataset

SCHEMA = "nous.training.audit/1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DATASET_BYTES = 256 * 1024 * 1024  # Manifest plus the five known JSONL artifacts.
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 1_000_000  # Total physical JSONL rows across the five artifacts.
AGENTS = frozenset({"claude", "codex", "openai-responses", "anthropic-messages"})
SOURCE_KINDS = frozenset({"local_transcript", "claude", "codex", "synthetic_fixture", "live_synthetic_fixture", "request_observer", "request_episode_observer", "historical_transcript", "historical_repair", "derived_standalone_text_task"})
RECORD_SCHEMAS = frozenset({"nous.trajectory/1", "nous.trajectory/2", "nous.trajectory/3", "nous.trajectory/4", "nous.preference/1", "nous.preference/2"})
EVIDENCE_KINDS = frozenset({"human_review", "verifier", "human_preference", "verifier_preference",
                            "content_review", "content_verifier"})
EVIDENCE_RESULTS = frozenset({"accepted", "passed", "chosen", "cleared", "rejected", "failed", "tie", "abstain"})
STAGES = frozenset({"canonical", "trajectory", "preference", "sft", "dpo"})
# These are fixed application codes, never values learned from a dataset.
VALIDATION_CODES = frozenset("""
artifact_missing_or_symlink artifact_read_failed assistant_final_missing
branched_or_compacted_context canonical_lineage_missing codemode_context_missing
codemode_duplicate_call codemode_duplicate_contract codemode_duplicate_item_id
codemode_duplicate_json_key codemode_empty_catalog codemode_empty_content codemode_event_order
codemode_event_type_mismatch codemode_events_empty codemode_final_response_missing
codemode_function_arguments_not_object codemode_hidden_visible_event codemode_incomplete_status
codemode_invalid_base_event codemode_invalid_base_source codemode_invalid_catalog
codemode_invalid_content codemode_invalid_contract codemode_invalid_contract_name
codemode_invalid_error_flag codemode_invalid_event codemode_invalid_event_type
codemode_invalid_fields codemode_invalid_function_strict codemode_invalid_json
codemode_invalid_output codemode_invalid_protocol_shape codemode_invalid_role
codemode_invalid_string codemode_invalid_target codemode_invalid_tool_schema
codemode_invalid_tool_schema_annotation codemode_invalid_tool_schema_bound
codemode_invalid_tool_schema_composition codemode_invalid_tool_schema_enum
codemode_invalid_tool_schema_name codemode_invalid_tool_schema_pattern
codemode_invalid_tool_schema_properties codemode_invalid_tool_schema_required
codemode_invalid_tool_schema_type codemode_invalid_tool_schema_unique_items
codemode_invalid_tool_schemas codemode_late_base_instructions codemode_late_system_context
codemode_no_assistant_target codemode_nonassistant_target codemode_noncanonical_serialization
codemode_pending_tool_call codemode_preferences_not_supported codemode_privacy_not_scrubbed
codemode_protocol_required codemode_protocol_size_limit codemode_quality_incomplete
codemode_runtime_visibility codemode_schema_required codemode_serialization_boundary
codemode_serialization_header codemode_target_not_found codemode_tool_argument_schema_mismatch
codemode_tool_contract_ambiguous codemode_tool_contract_missing codemode_tool_schema_too_deep
codemode_tool_type_mismatch codemode_training_size_limit codemode_unmatched_tool_output
codemode_unresolved_runtime_context codemode_unsupported_configuration
codemode_unsupported_content codemode_unsupported_content_metadata
codemode_unsupported_contract codemode_unsupported_event codemode_unsupported_grammar
codemode_unsupported_phase codemode_unsupported_tool_schema
codemode_unsupported_tool_schema_keyword complete_manifest_missing
conflicting_preference_direction cross_record_evidence_conflict custom_content_review_missing
duplicate_json_key duplicate_preference duplicate_preference_observation duplicate_sft
duplicate_tool_call_id duplicate_tool_schema duplicate_training_sample duplicate_trajectory
eligible_training_sample_missing empty_message_content empty_messages empty_text_message
explicit_preference_missing file_byte_count_mismatch file_line_count_mismatch
file_sha256_mismatch identical_preference_completions incomplete_manifest incomplete_trajectory
invalid_artifact_record invalid_canonical_lineage invalid_content_block invalid_evidence
invalid_file_manifest invalid_json invalid_json_constant invalid_json_value invalid_lineage
invalid_lineage_source invalid_manifest invalid_manifest_files invalid_message_role
invalid_preference_project invalid_preference_provenance invalid_protocol_shape
invalid_quality_report invalid_rejection_record invalid_text_content invalid_tool_call
invalid_tool_parameters invalid_tool_result invalid_tool_result_error_flag invalid_tool_schema
invalid_tool_schema_annotation invalid_tool_schema_bound invalid_tool_schema_composition
invalid_tool_schema_enum invalid_tool_schema_name invalid_tool_schema_pattern
invalid_tool_schema_properties invalid_tool_schema_required invalid_tool_schema_type
invalid_tool_schema_unique_items invalid_tool_schemas invalid_training_content
invalid_training_fields invalid_training_function invalid_training_message
invalid_training_tool_call invalid_training_tool_calls invalid_trajectory_provenance
late_system_message lineage_evidence_mismatch lineage_missing lineage_provenance_mismatch
lineage_record_mismatch lineage_sample_mismatch lineage_source_missing
lineage_training_line_missing manifest_format_mismatch missing_context
negative_or_conflicting_evidence orphan_or_duplicate_tool_result
preference_completion_contains_prompt preference_completion_not_assistant
preference_ordered_source_not_supported preference_prompt_not_ready
preference_source_context_mismatch preference_source_record_missing privacy_not_scrubbed
privacy_version_missing protocol_requires_codemode_profile quality_missing record_id_missing
record_not_object rejection_summary_mismatch source_quality_issues
tool_argument_schema_mismatch tool_arguments_not_json_string tool_arguments_not_object
tool_call_id_missing tool_call_name_missing tool_call_wrong_role tool_result_content_missing
tool_result_id_missing tool_result_name_mismatch tool_result_wrong_role tool_results_missing
tool_schema_missing tool_schema_too_deep training_lineage_missing
training_origin_lineage_missing unbound_protocol_extension unexpected_target_boundary
unexpected_tool_result_fields unsupported_content_block unsupported_custom_tool
unsupported_manifest_schema unsupported_preference_schema unsupported_profile
unsupported_schema unsupported_tool_schema unsupported_tool_schema_keyword user_prompt_missing
validation_failed validation_io_failed verified_outcome_missing
duplicate_request_json_key exchange_requires_request_profile hidden_reasoning_unsupported
invalid_request_body invalid_request_boolean invalid_request_content invalid_request_exchange
invalid_request_input invalid_request_json invalid_request_metadata invalid_request_client_metadata invalid_request_number
invalid_request_sample invalid_request_sample_header invalid_request_sample_target
invalid_request_string invalid_request_tool_arguments invalid_request_tool_catalog
invalid_request_tool_linkage invalid_request_tool_schema invalid_request_target invalid_response_body
request_assistant_prefill_unsupported request_background_unsupported request_content_digest_mismatch
request_final_text_required request_function_contract_missing_or_ambiguous request_input_missing
request_input_incomplete request_item_limit request_message_projection_mismatch request_noncanonical_serialization
request_preferences_not_supported request_privacy_not_scrubbed request_quality_incomplete
request_response_incomplete request_server_context_unsupported request_size_limit
request_structure_too_deep request_tool_arguments_mismatch request_tool_contract_changed
request_tool_history_not_supported request_tool_results_missing request_trajectory_required
request_transport_mismatch request_truncation_unsupported request_unbound_context_extension request_user_input_missing
unbound_request_extension unsupported_request_content unsupported_request_content_metadata
unsupported_request_context unsupported_request_custom_format unsupported_request_field
unsupported_request_include unsupported_request_input_item unsupported_request_provider
unsupported_request_role unsupported_request_schema unsupported_request_serialization
unsupported_request_setting unsupported_request_tool unsupported_request_tool_choice unsupported_request_tool_schema
unsupported_request_transport unsupported_response_context unsupported_response_field
unsupported_response_format unsupported_response_output unsupported_response_usage
unsupported_request_reasoning_field unsupported_request_reasoning_summary
episode_content_digest_mismatch episode_continues_after_final episode_cross_source_step
episode_duplicate_response_identity episode_duplicate_snapshot episode_empty
episode_exchange_schema_or_provider_mismatch episode_final_response_missing
episode_history_list_required episode_history_prefix_changed episode_initial_request_digest_mismatch
episode_initial_tool_history episode_invalid_fields episode_invalid_json episode_invalid_privacy
episode_invalid_quality episode_invalid_record episode_invalid_source_digest episode_invalid_step_evidence
episode_message_projection_mismatch episode_native_exchange_invalid episode_observed_response_missing_or_changed
episode_privacy_not_checked episode_quality_incomplete episode_record_required episode_request_context_changed
episode_response_model_changed episode_reused_call_id episode_sample_invalid episode_schema_or_provider_invalid
episode_size_limit episode_source_identity_missing episode_step_limit episode_step_order_invalid
episode_step_review_missing_or_negative episode_structure_limit episode_supervision_policy_required
episode_target_invalid episode_tool_results_missing_or_duplicate episode_unbound_context_extension
episode_unobserved_context_append episode_preferences_not_supported episode_requires_episode_profile
unbound_episode_extension request_parallel_tool_violation request_tool_batch_interleaved
request_tool_call_limit request_tool_choice_violation request_tool_exchange_required
request_tool_exchange_requires_episode request_tool_response_required request_tool_result_identity_mismatch
request_tool_schema_execution_unsupported request_tool_validation_limit unsupported_request_tool_output
request_excluded_tool_content request_external_file_tool request_file_exclusion_unverified
request_opaque_tool_execution request_privacy_provider_unsupported request_tool_privacy_depth
request_tool_privacy_path_limit
""".split())

GAP_CODES = frozenset("""
agent_mismatch ambiguous_message_update auto branch_context compacted contract_redacted
custom_content_review_required duplicate_item_id duplicate_json_key duplicate_tool_call
duplicate_tool_contract duplicate_tool_result earlier_context event_identity
excluded_file_content final_answer incomplete_final invalid_additional_tools
invalid_call_identity invalid_completed_item invalid_content invalid_custom_format
invalid_function_arguments invalid_function_parameters invalid_function_strict
invalid_history_metadata invalid_item_identity invalid_message invalid_message_content
invalid_offset invalid_payload invalid_record invalid_record_id invalid_record_ordinal
invalid_runtime_execution invalid_runtime_result invalid_session_identity invalid_text
invalid_tool_arguments invalid_tool_call invalid_tool_contract invalid_tool_output
invalid_usage invalid_utf8 invalid_world_state malformed_json malformed_record media_content
media_omitted missing_tool_arguments missing_tool_call_id missing_tool_name missing_tool_output
missing_tool_result_id mixed_agents mixed_sessions no_visible_messages offset_reset
orphan_tool_result partial_tail pending_tool_calls pii_type_changed pre_compaction_context
privacy_exclusion_unverified protocol_size_limit record_ordinal_order_violation
redacted_metadata runtime_configuration runtime_context runtime_context_semantics
runtime_execution runtime_version_changed sampling_context session_context sidechain
source_format source_identity source_order stop_reply_not_flushed streamed_content
system_prompt terminal_response tool_arguments tool_definitions tool_execution tool_linkage
tool_output transcript_unavailable unknown_content_block unknown_event_message
unknown_item_status unknown_message_phase unknown_record_type unknown_response_item
unknown_role unknown_usage_fields unmatched_completion_mirror unmatched_tool_output
unresolved_history_metadata unresolved_thread_settings unresolved_tool_call
unresolved_turn_context unresolved_world_state unsupported_agent unsupported_completion_mirror
unsupported_configuration_update unsupported_content unsupported_content_fields
unsupported_history_metadata_fields unsupported_runtime_execution unsupported_tool_contract
unsupported_world_state_fields visible_content transcript_too_large transcript_line_too_large
transcript_must_be_regular_file transcript_changed_during_read transcript_unreadable
transcript_too_many_lines
request_quality_incomplete request_context request_redacted_or_changed response_redacted
request_response_incomplete request_final_text_required request_tool_history_not_supported
episode_final_response episode_quality_incomplete episode_privacy_not_checked request_tool_content_redacted
request_excluded_tool_content request_external_file_tool request_file_exclusion_unverified request_opaque_tool_execution
history_inherited_context history_ancestry history_nonlegacy_mode history_replay_semantics
history_branch history_branch_context history_missing_parent history_parent_order
history_parent_chain_unavailable history_parent_cycle historical_workspace
historical_workspace_unavailable historical_workspace_state_unverified historical_workspace_state
historical_tool_payloads_omitted historical_native_protocol_unavailable
unsupported_record_fields unsupported_event_fields unsupported_runtime_execution_fields
unsupported_runtime_result_fields unsupported_tool_contract_fields
""".split())


class AuditError(ValueError):
    """Internal static error, never source or filesystem exception text."""
    def __init__(self, reason, validation_code=None):
        super().__init__(reason)
        self.validation_code = validation_code


def _bucket(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _counts(counter):
    return dict(sorted(counter.items()))


def _base():
    return {"schema": SCHEMA, "status": "invalid", "structural_valid": False,
            "training_rows_present": False, "statistics_available": False, "production_readiness": "unverified",
            "model_effectiveness": "unverified", "rights_and_consent": "unverified",
            "validation_errors": [], "reasons": []}


def _invalid(errors, reason="dataset_validation_failed"):
    report = _base()
    safe = collections.Counter()
    for error in errors if isinstance(errors, list) else []:
        value = error.get("code") if isinstance(error, dict) else None
        safe[value if isinstance(value, str) and value in VALIDATION_CODES else "validation_error"] += 1
    report["validation_errors"] = [{"code": code, "count": count} for code, count in sorted(safe.items())]
    if not safe:
        report["validation_errors"] = [{"code": "validation_error", "count": 1}]
    report["reasons"] = [reason]
    return report


def _open_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise AuditError("audit_source_unavailable", "artifact_missing_or_symlink") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise AuditError("audit_source_unavailable", "artifact_missing_or_symlink")
    return os.fdopen(fd, "rb")


def _new_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def _manifest_bytes(root):
    with _open_regular(root / "manifest.json") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > MAX_MANIFEST_BYTES:
            raise AuditError("audit_manifest_too_large")
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise AuditError("audit_manifest_too_large")
        after = os.fstat(handle.fileno())
    if any(getattr(before, field) != getattr(after, field)
           for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        raise AuditError("dataset_changed_during_audit")
    return raw


def _copy_manifest(raw):
    """Only the closed metadata needed for bounded copying; validator follows."""
    try:
        manifest = strict_json(raw.decode("utf-8"))
    except (InvalidRecord, UnicodeError):
        raise AuditError("dataset_validation_failed", "invalid_manifest") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != "nous.training.manifest/2":
        raise AuditError("dataset_validation_failed", "unsupported_manifest_schema")
    if manifest.get("complete") is not True:
        raise AuditError("dataset_validation_failed", "incomplete_manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(FILES):
        raise AuditError("dataset_validation_failed", "invalid_manifest_files")
    total, records = len(raw), 0
    for info in files.values():
        if (not isinstance(info, dict) or set(info) != {"records", "bytes", "sha256"}
                or any(type(info.get(key)) is not int or info[key] < 0 for key in ("records", "bytes"))
                or not isinstance(info.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", info["sha256"]) is None):
            raise AuditError("dataset_validation_failed", "invalid_file_manifest")
        total += info["bytes"]
        records += info["records"]
    if total > MAX_DATASET_BYTES:
        raise AuditError("audit_dataset_too_large")
    if records > MAX_RECORDS:
        raise AuditError("audit_too_many_records")
    return manifest


def _snapshot(source, destination, raw_manifest):
    """Copy only the anchored manifest's files, then validate the copied bytes."""
    if _manifest_bytes(source) != raw_manifest:
        raise AuditError("dataset_changed_during_audit")
    manifest = _copy_manifest(raw_manifest)
    copied_size, copied_records = len(raw_manifest), 0
    for name in FILES:
        expected = manifest["files"][name]
        digest, size = hashlib.sha256(), 0
        with _open_regular(source / name) as incoming, _new_file(destination / name) as outgoing:
            before = os.fstat(incoming.fileno())
            if before.st_size + copied_size > MAX_DATASET_BYTES:
                raise AuditError("audit_dataset_too_large")
            while True:
                raw = incoming.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE_BYTES:
                    raise AuditError("audit_line_too_large")
                copied_records += 1
                if copied_records > MAX_RECORDS:
                    raise AuditError("audit_too_many_records")
                size += len(raw)
                copied_size += len(raw)
                if copied_size > MAX_DATASET_BYTES:
                    raise AuditError("audit_dataset_too_large")
                if size > expected["bytes"]:
                    raise AuditError("dataset_changed_during_audit", "file_byte_count_mismatch")
                outgoing.write(raw)
                digest.update(raw)
            after = os.fstat(incoming.fileno())
        if any(getattr(before, field) != getattr(after, field)
               for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise AuditError("dataset_changed_during_audit")
        if size != expected["bytes"]:
            raise AuditError("dataset_changed_during_audit", "file_byte_count_mismatch")
        if digest.hexdigest() != expected["sha256"]:
            raise AuditError("dataset_changed_during_audit", "file_sha256_mismatch")
    if _manifest_bytes(source) != raw_manifest:
        raise AuditError("dataset_changed_during_audit")
    with _new_file(destination / "manifest.json") as handle:
        handle.write(raw_manifest)
    return manifest


def _rows(path):
    with _open_regular(path) as handle:
        for line, raw in enumerate(handle, 1):
            yield line, strict_json(raw.decode("utf-8"))


def _identity(value):
    return value if isinstance(value, str) and value.strip() else None


def _marker(value):
    return "synthetic" if value is True else "non_synthetic" if value is False else "unknown"


class _Groups:
    """Join same-project native/session/path aliases without emitting identifiers."""
    def __init__(self):
        self.parent = {}
        self.aliases = {}

    def root(self, key):
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def add(self, provenance):
        project = _identity(provenance.get("project_id"))
        session = _identity(provenance.get("session_group_id")) or _identity(provenance.get("session_id"))
        if not project or not session:
            return None
        key = (project, session)
        self.root(key)
        for field in ("session_group_id", "source_transcript_id"):
            value = _identity(provenance.get(field))
            if value is not None:
                alias = (project, field, value)
                previous = self.aliases.setdefault(alias, key)
                self.parent[self.root(key)] = self.root(previous)
        return key


def _aggregate(snapshot, manifest):
    counts = collections.Counter()
    agents, sources, schemas = collections.Counter(), collections.Counter(), collections.Counter()
    quality_states, gaps, missing = collections.Counter(), collections.Counter(), collections.Counter()
    evidence_kinds, bound_kinds, evidence_results = collections.Counter(), collections.Counter(), collections.Counter()
    evidence_binding = collections.Counter()
    episode_step_reviews, episode_step_quality = collections.Counter(), collections.Counter()
    markers = {name: collections.Counter() for name in ("trajectories", "preferences")}
    metadata, trajectories_by_id = {}, {}
    groups = _Groups()
    for name in ("trajectories.jsonl", "preferences.jsonl"):
        family = name[:-6]
        for line, record in _rows(snapshot / name):
            counts[family] += 1
            provenance = record.get("provenance", {})
            provenance = provenance if isinstance(provenance, dict) else {}
            schemas[_bucket(record.get("schema"), RECORD_SCHEMAS)] += 1
            sources[_bucket(provenance.get("source"), SOURCE_KINDS)] += 1
            marker = _marker(provenance.get("synthetic"))
            markers[family][marker] += 1
            key = (name, line)
            metadata[key] = {"project": _identity(provenance.get("project_id")),
                             "group": groups.add(provenance) if name == "trajectories.jsonl" else None, "marker": marker,
                             "source_record_id": _identity(provenance.get("source_record_id"))}
            if name == "trajectories.jsonl":
                trajectories_by_id.setdefault(record["id"], []).append(key)
                agent = record.get("agent")
                agents[_bucket(agent.get("id") if isinstance(agent, dict) else None, AGENTS)] += 1
                quality = record.get("quality")
                quality = quality if isinstance(quality, dict) else {}
                complete = quality.get("complete")
                quality_states["complete" if complete is True else "incomplete" if complete is False else "unknown"] += 1
                for field, counter in (("issues", gaps), ("missing", missing)):
                    values = quality.get(field)
                    if not isinstance(values, list):
                        counter["unknown"] += 1
                    else:
                        # Count affected records, not repeated occurrences of a code.
                        for code in {_bucket(value, GAP_CODES) for value in values}:
                            counter[code] += 1
            digest = record_digest(record) if name == "trajectories.jsonl" else preference_digest(record)
            if record.get("schema") == "nous.trajectory/4":
                from nous_training_episode import _step_review
                for index, step in enumerate(record["episode"]["steps"]):
                    episode_step_reviews[_step_review(record, index, digest)] += 1
                    complete = step["quality"]["complete"]
                    episode_step_quality["complete" if complete is True else "incomplete"] += 1
            annotations = record.get("evidence", [])
            if not isinstance(annotations, list):
                evidence_binding["invalid_evidence_container"] += 1
                annotations = []
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    evidence_kinds["unknown"] += 1
                    evidence_binding["unbound_or_invalid"] += 1
                    continue
                kind = _bucket(annotation.get("kind"), EVIDENCE_KINDS)
                evidence_kinds[kind] += 1
                evidence_results[_bucket(annotation.get("result"), EVIDENCE_RESULTS)] += 1
                if annotation.get("record_digest") == digest:
                    evidence_binding["digest_bound"] += 1
                    bound_kinds[kind] += 1
                else:
                    evidence_binding["unbound_or_invalid"] += 1

    training = {}
    for kind in ("sft", "dpo"):
        for line, _ in _rows(snapshot / (kind + ".jsonl")):
            counts[kind] += 1
            training[(kind, line)] = {"origins": set(), "sources": set(), "evidence": set(), "markers": set(), "unresolved": False}
    rejection_codes, rejection_stages = collections.Counter(), collections.Counter()
    for _, record in _rows(snapshot / "rejected.jsonl"):
        counts["rejected"] += 1
        rejection_codes[_bucket(record.get("reason"), VALIDATION_CODES)] += 1
        rejection_stages[_bucket(record.get("stage"), STAGES)] += 1

    duplicate_entries = 0
    for entry in manifest["lineage"]:
        kind = entry["format"]
        if kind not in ("sft", "dpo"):
            continue
        row = training[(kind, entry["line"])]
        source = (entry["source_file"], entry["source_line"])
        target = entry.get("target")
        origin = (source, json.dumps(target, sort_keys=True, separators=(",", ":")))
        if origin in row["origins"]:
            duplicate_entries += 1
            continue
        row["origins"].add(origin)
        row["evidence"].update(_bucket(kind, EVIDENCE_KINDS) for kind in entry["evidence_kinds"])
        meta = metadata[source]
        row["markers"].add(meta["marker"])
        if kind == "sft":
            row["sources"].add(source)
        else:
            referenced = trajectories_by_id.get(meta["source_record_id"], [])
            row["sources"].update(referenced)
            row["unresolved"] |= not referenced
            row["markers"].update(metadata[key]["marker"] for key in referenced)
            if not referenced:
                row["markers"].add("unknown")

    projects, sessions, used_sources = set(), set(), set()
    group_rows = collections.defaultdict(set)
    rows_evidence, rows_markers = collections.Counter(), collections.Counter()
    multi_origin = origin_links = unresolved = incomplete = any_synthetic = 0
    for row_key, row in training.items():
        origin_links += len(row["origins"])
        multi_origin += len(row["origins"]) > 1
        unresolved += row["unresolved"]
        used_sources.update(row["sources"])
        row_incomplete = row["unresolved"]
        for source in row["sources"]:
            meta = metadata[source]
            if meta["project"]:
                projects.add(meta["project"])
            if meta["group"]:
                group = groups.root(meta["group"])
                sessions.add(group)
                group_rows[group].add(row_key)
            row_incomplete |= not meta["project"] or not meta["group"]
        incomplete += bool(row_incomplete)
        rows_evidence.update(row["evidence"])
        labels = row["markers"]
        any_synthetic += "synthetic" in labels
        if labels == {"synthetic"}:
            rows_markers["all_origins_explicit_synthetic"] += 1
        elif labels == {"non_synthetic"}:
            rows_markers["all_origins_explicit_non_synthetic"] += 1
        elif labels == {"synthetic", "non_synthetic"}:
            rows_markers["mixed_explicit"] += 1
        else:
            rows_markers["unknown_or_partially_marked"] += 1
    total = counts["sft"] + counts["dpo"]
    shared = set().union(*(rows for rows in group_rows.values() if len(rows) > 1)) if group_rows else set()
    report = _base()
    report.update(status="structurally_valid_with_training_rows" if total else "structurally_valid_without_training_rows",
                  structural_valid=True, training_rows_present=total > 0, statistics_available=True, profile=manifest["profile"],
                  counts={key: counts[key] for key in ("trajectories", "preferences", "sft", "dpo", "rejected")},
                  distributions={"trajectory_agents": _counts(agents), "canonical_sources": _counts(sources),
                                 "canonical_schemas": _counts(schemas)},
                  quality={"trajectory_completeness": _counts(quality_states), "trajectory_issue_records": _counts(gaps),
                           "trajectory_missing_context_records": _counts(missing)},
                  rejections={"by_reason": _counts(rejection_codes), "by_stage": _counts(rejection_stages)},
                  evidence={"canonical_annotations_by_kind": _counts(evidence_kinds),
                            "digest_bound_annotations_by_kind": _counts(bound_kinds),
                            "canonical_annotation_results": _counts(evidence_results), "binding": _counts(evidence_binding),
                            "training_rows_by_qualifying_kind": _counts(rows_evidence),
                            "episode_steps_by_bound_review": _counts(episode_step_reviews),
                            "episode_steps_by_declared_quality": _counts(episode_step_quality)},
                  coverage={"training_rows": total, "distinct_source_trajectories": len(used_sources),
                            "distinct_source_projects": len(projects), "distinct_source_session_groups": len(sessions),
                            "training_rows_with_unresolved_sources": unresolved,
                            "training_rows_with_incomplete_source_identity": incomplete,
                            "distinct_origin_links": origin_links, "training_rows_with_multiple_origins": multi_origin,
                            "additional_origin_links_after_deduplication": origin_links - total,
                            "duplicate_lineage_entries": duplicate_entries,
                            "training_rows_sharing_source_session": len(shared),
                            "repeated_session_row_links": sum(len(rows) - 1 for rows in group_rows.values())},
                  synthetic={"canonical_by_explicit_marker": {key: _counts(value) for key, value in markers.items()},
                             "training_rows_by_origin_markers": _counts(rows_markers),
                             "training_rows_with_any_explicit_synthetic_origin": any_synthetic,
                             "all_origins_explicit_synthetic_fraction": rows_markers["all_origins_explicit_synthetic"] / total if total else None})
    if not total:
        report["reasons"] = ["no_training_rows", "inspect_rejections_and_bound_evidence"]
        if not counts["trajectories"] and not counts["preferences"]:
            report["reasons"].append("collect_or_import_explicitly_authorized_sources")
    return report


def audit_dataset(path):
    """Return a privacy-safe aggregate report. Never modify or train on the input."""
    try:
        source = Path(path)
        # Never pass a mutable source path to the validator: create a bounded,
        # regular-file-only snapshot first, then validate the actual copied bytes.
        raw_manifest = _manifest_bytes(source)
        _copy_manifest(raw_manifest)
        with tempfile.TemporaryDirectory(prefix="nous-audit-") as temp:
            snapshot = Path(temp)
            os.chmod(snapshot, 0o700)
            manifest = _snapshot(source, snapshot, raw_manifest)
            validation = validate_dataset(snapshot)
            if validation.get("valid") is not True:
                return _invalid(validation.get("errors", []), "snapshot_validation_failed")
            return _aggregate(snapshot, manifest)
    except AuditError as error:
        # Only our internal static messages are returned; exceptions from parsers,
        # source objects or the OS are never copied into the report.
        report = _base()
        report["reasons"] = [str(error) if str(error) in {"dataset_changed_during_audit", "audit_source_unavailable",
                             "audit_manifest_too_large", "audit_dataset_too_large", "audit_line_too_large",
                             "audit_too_many_records", "dataset_validation_failed"}
                             else "audit_failed"]
        if isinstance(error.validation_code, str) and error.validation_code in VALIDATION_CODES:
            report["validation_errors"] = [{"code": error.validation_code, "count": 1}]
        return report
    except Exception:
        report = _base()
        report["reasons"] = ["audit_failed"]
        return report
