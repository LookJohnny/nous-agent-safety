"""Deterministic train/eval partitioning of a validated local training dataset.

Training rows are copied byte-for-byte. Source groups, shared sample origins and
explicit preference source IDs determine connected components; a component can
never be put in both partitions. No model training, uploads or source mutations.
"""
import copy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile

from nous_training_export import (CODEMODE_PROFILE, EPISODE_PROFILE, InvalidRecord,
                                  _digest, _safe_id, convert_messages, strict_json)
from nous_training_validate import FILES, validate_dataset


SCHEMA = "nous.training.split-manifest/1"
ALGORITHM = "sha256-component-threshold-v1"


class SplitError(ValueError):
    """Static diagnostics without source content or filesystem paths."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fail(code):
    raise SplitError(code)


def _positive(value):
    return type(value) is int and value > 0


def _identity(value, code):
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    return value


def _open_regular(path):
    # Never follow a replaced artifact symlink or block opening a FIFO.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        _fail("input_artifact_not_regular")
    return os.fdopen(fd, "rb")


def _new_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def _new_directory(path):
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)


def _verified_snapshot(source, snapshot):
    """Snapshot exact known files, then validate the bytes actually used below."""
    with _open_regular(source / "manifest.json") as handle:
        raw_manifest = handle.read()
    manifest = strict_json(raw_manifest.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != "nous.training.manifest/2" or manifest.get("complete") is not True:
        _fail("invalid_dataset")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(FILES):
        _fail("invalid_dataset_files")
    for name in FILES:
        expected = files[name]
        if (not isinstance(expected, dict) or set(expected) != {"records", "bytes", "sha256"}
                or type(expected["records"]) is not int or expected["records"] < 0
                or type(expected["bytes"]) is not int or expected["bytes"] < 0
                or not isinstance(expected["sha256"], str)):
            _fail("invalid_dataset_files")
        digest, size, rows = hashlib.sha256(), 0, 0
        with _open_regular(source / name) as incoming, _new_file(snapshot / name) as outgoing:
            for raw in incoming:
                if not raw.endswith(b"\n"):
                    _fail("input_jsonl_missing_newline")
                outgoing.write(raw)
                digest.update(raw)
                size += len(raw)
                rows += 1
        if {"records": rows, "bytes": size, "sha256": digest.hexdigest()} != expected:
            _fail("input_changed_or_hash_mismatch")
    with _open_regular(source / "manifest.json") as handle:
        if handle.read() != raw_manifest:
            _fail("input_changed_or_hash_mismatch")
    with _new_file(snapshot / "manifest.json") as handle:
        handle.write(raw_manifest)
    if validate_dataset(snapshot).get("valid") is not True:
        _fail("invalid_snapshot_dataset")
    return manifest, hashlib.sha256(raw_manifest).hexdigest()


class _Components:
    def __init__(self):
        self.parent = {}

    def add(self, value):
        self.parent.setdefault(value, value)

    def find(self, value):
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            previous = self.parent[value]
            self.parent[value] = root
            value = previous
        return root

    def union(self, values):
        values = list(values)
        for value in values:
            self.add(value)
        if not values:
            _fail("training_group_missing")
        first = self.find(values[0])
        for value in values[1:]:
            other = self.find(value)
            if first != other:
                # Deterministic roots make input lineage ordering irrelevant.
                low, high = sorted((first, other))
                self.parent[high] = low
                first = low


def _canonical_metadata(snapshot):
    records, source_ids = {}, {}
    for name in ("trajectories.jsonl", "preferences.jsonl"):
        with _open_regular(snapshot / name) as handle:
            for line, raw in enumerate(handle, 1):
                value = strict_json(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    _fail("invalid_canonical_source")
                ident = _identity(value.get("id"), "canonical_source_id_missing")
                provenance = value.get("provenance", {})
                if not isinstance(provenance, dict):
                    _fail("invalid_source_provenance")
                metadata = {"id": ident, "provenance": provenance, "schema": value.get("schema")}
                # Retain only the target bound, not native messages or bodies.
                # The snapshot validator has already checked target semantics,
                # evidence, and exact sample reconstruction for every origin.
                if value.get("schema") == "nous.trajectory/4":
                    metadata["target_limit"] = len(value["episode"]["steps"])
                elif value.get("schema") == "nous.trajectory/2":
                    metadata["target_limit"] = len(value["protocol"]["events"])
                records[(name, line)] = metadata
                if name == "trajectories.jsonl":
                    source_ids.setdefault(ident, []).append((name, line))
    return records, source_ids


def _group(metadata, group_by):
    provenance = metadata["provenance"]
    project = _identity(provenance.get("project_id"), "project_identity_missing")
    dimensions = {"project_id": project}
    if group_by == "session":
        field = "session_group_id" if "session_group_id" in provenance else "session_id"
        session = _identity(provenance.get(field), "session_identity_missing")
        # The selected identity value matters, not whether an older source used
        # session_id instead of the newer session_group_id key for that value.
        dimensions["session_id"] = session
    return "g_" + _digest(dimensions)


def _safe_provenance(metadata):
    source = metadata["provenance"]
    result = {key: _safe_id(source[key]) for key in
              ("source", "session_id", "session_group_id", "source_transcript_id", "project_id", "source_record_id") if key in source}
    digest = source.get("source_sha256")
    if isinstance(digest, str) and len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
        result["source_sha256"] = digest
    return result


def _preference_prompt_links(snapshot, lineage):
    """Index exact normalized complete prefixes without retaining sample bodies.

    This uses the same TRL normalization as preference_source_matches: native V2
    event trajectories are not DPO sources. Tool context must match exactly.
    Each trajectory is normalized once, not once per preference observation.
    """
    wanted = {entry.get("source_line") for entry in lineage if isinstance(entry, dict)
              and entry.get("format") == "dpo" and type(entry.get("source_line")) is int}
    if not wanted:
        return {}
    queries, matches = {}, {}
    with _open_regular(snapshot / "preferences.jsonl") as handle:
        for line, raw in enumerate(handle, 1):
            if line not in wanted:
                continue
            value = strict_json(raw.decode("utf-8"))
            try:
                prompt = convert_messages(value.get("prompt"), profile="trl")
            except InvalidRecord:
                _fail("invalid_preference_prompt")
            source_key = ("preferences.jsonl", line)
            matches[source_key] = []
            tool_key = _digest(value.get("tools", []))
            queries.setdefault(tool_key, {}).setdefault(len(prompt), {}).setdefault(_digest(prompt), []).append(source_key)
    with _open_regular(snapshot / "trajectories.jsonl") as handle:
        for line, raw in enumerate(handle, 1):
            value = strict_json(raw.decode("utf-8"))
            if value.get("schema") != "nous.trajectory/1":
                continue
            lengths = queries.get(_digest(value.get("tools", [])))
            if not lengths:
                continue
            try:
                observed = convert_messages(value.get("messages"), profile="trl")
            except InvalidRecord:
                continue
            for length, fingerprints in lengths.items():
                if length > len(observed):
                    continue
                for source_key in fingerprints.get(_digest(observed[:length]), []):
                    matches[source_key].append(("trajectories.jsonl", line))
    return matches


def _connect_source_aliases(records, components, group_by):
    if group_by != "session":
        return
    aliases = {}
    for source_key, metadata in sorted(records.items()):
        if source_key[0] != "trajectories.jsonl":
            continue
        try:
            group = _group(metadata, group_by)
        except SplitError:
            # Missing identity still fails for every used source in _plan. An
            # unrelated, untrainable candidate cannot supply a trustworthy alias.
            continue
        provenance = metadata["provenance"]
        for field in ("session_group_id", "source_transcript_id"):
            if field not in provenance:
                continue
            alias = _identity(provenance[field], "source_alias_identity_missing")
            key = (provenance["project_id"], field, alias)
            prior = aliases.setdefault(key, group)
            components.union((prior, group))


def _target(entry, metadata, profile):
    """Check the specific target grammar and canonical bound for this profile."""
    if entry.get("format") == "sft" and profile in (CODEMODE_PROFILE, EPISODE_PROFILE):
        expected_schema = "nous.trajectory/4" if profile == EPISODE_PROFILE else "nous.trajectory/2"
        target, limit = entry.get("target"), metadata.get("target_limit")
        if metadata.get("schema") != expected_schema or type(target) is not dict or not _positive(limit):
            _fail("invalid_target_boundary")
        if profile == EPISODE_PROFILE:
            if set(target) != {"step"} or type(target["step"]) is not int or not 0 <= target["step"] < limit:
                _fail("invalid_target_boundary")
        elif (set(target) != {"start", "end"} or type(target["start"]) is not int
              or type(target["end"]) is not int or not 0 <= target["start"] < target["end"] <= limit):
            _fail("invalid_target_boundary")
        return dict(target)
    if "target" in entry:
        _fail("invalid_target_boundary")
    return None


def _plan(snapshot, manifest, group_by, ratio, seed):
    records, source_ids = _canonical_metadata(snapshot)
    lineage = manifest.get("lineage")
    if not isinstance(lineage, list):
        _fail("lineage_missing")
    prompt_links = _preference_prompt_links(snapshot, lineage)
    samples = {}
    for kind in ("sft", "dpo"):
        with _open_regular(snapshot / (kind + ".jsonl")) as handle:
            for line, raw in enumerate(handle, 1):
                samples[(kind, line)] = {"sample_digest": _digest(strict_json(raw.decode("utf-8"))),
                                         "origins": [], "groups": set()}
    components = _Components()
    warnings = set()
    for entry in lineage:
        if not isinstance(entry, dict):
            _fail("invalid_lineage")
        kind = entry.get("format")
        if kind not in ("sft", "dpo"):
            continue
        line, source_line = entry.get("line"), entry.get("source_line")
        source_file = "trajectories.jsonl" if kind == "sft" else "preferences.jsonl"
        if (not _positive(line) or not _positive(source_line) or entry.get("source_file") != source_file
                or (kind, line) not in samples or (source_file, source_line) not in records):
            _fail("invalid_training_lineage")
        metadata = records[(source_file, source_line)]
        sample = samples[(kind, line)]
        if (entry.get("id") != _safe_id(metadata["id"])
                or entry.get("provenance") != _safe_provenance(metadata)
                or entry.get("sample_digest") != sample["sample_digest"]):
            _fail("lineage_source_mismatch")
        origin = {"source_file": source_file, "source_line": source_line,
                  "id": entry["id"], "record_digest": entry["record_digest"],
                  "provenance": copy.deepcopy(entry["provenance"])}
        target = _target(entry, metadata, manifest.get("profile"))
        if target is not None:
            origin["target"] = target
        if kind == "sft":
            groups = {_group(metadata, group_by)}
        else:
            source_id = _identity(metadata["provenance"].get("source_record_id"), "preference_source_identity_missing")
            sources = source_ids.get(source_id)
            if not sources:
                _fail("preference_source_record_missing")
            groups = {_group(records[key], group_by) for key in sources}
            origin["source_trajectories"] = [
                {"source_file": key[0], "source_line": key[1], "id": _safe_id(records[key]["id"]),
                 "provenance": _safe_provenance(records[key])} for key in sorted(sources)]
            if len(groups) > 1:
                warnings.add("preference_source_id_spans_groups")
            matching = prompt_links.get((source_file, source_line), [])
            if not matching:
                _fail("preference_prompt_source_missing")
            matched_groups = {_group(records[key], group_by) for key in matching}
            if matched_groups - groups:
                warnings.add("cross_source_prompt_groups_connected")
            groups.update(matched_groups)
            origin["prompt_matches"] = [
                {"source_file": key[0], "source_line": key[1], "id": _safe_id(records[key]["id"]),
                 "provenance": _safe_provenance(records[key])} for key in sorted(matching)]
        components.union(groups)
        sample["groups"].update(groups)
        sample["origins"].append(origin)
    for sample in samples.values():
        if not sample["origins"]:
            _fail("training_lineage_missing")
        # A deduplicated sample may be backed by several projects or sessions.
        # Bind all origins rather than choosing whichever lineage appeared first.
        components.union(sample["groups"])
    _connect_source_aliases(records, components, group_by)
    used_components = {components.find(group) for sample in samples.values() for group in sample["groups"]}
    members = {}
    for group in sorted(components.parent):
        parent = components.find(group)
        if parent in used_components:
            members.setdefault(parent, []).append(group)
    component_info, group_info = {}, {}
    fraction = Fraction(str(ratio))
    threshold = fraction.numerator * (1 << 256) // fraction.denominator
    for groups in members.values():
        ident = "c_" + _digest(groups)
        score = int(_digest({"algorithm": ALGORITHM, "seed": seed, "component_id": ident}), 16)
        split = "eval" if score < threshold else "train"
        info = {"component_id": ident, "group_ids": groups, "split": split}
        component_info[ident] = info
        for group in groups:
            group_info[group] = info
    for sample in samples.values():
        info = group_info[next(iter(sample["groups"]))]
        sample.update(split=info["split"], component_id=info["component_id"])
        sample["groups"] = sorted(sample["groups"])
        # Canonical sorted origin metadata gives stable reports independent of
        # incidental manifest lineage entry ordering.
        sample["origins"] = sorted(sample["origins"], key=_json)
    return samples, sorted(component_info.values(), key=lambda v: v["component_id"]), warnings


def _publish(snapshot, out, manifest, manifest_sha, plan, component_info, warnings, group_by, ratio, seed):
    try:
        _new_directory(out)
    except FileExistsError:
        _fail("output_already_exists")
    for split in ("train", "eval"):
        _new_directory(out / split)
    files, line_provenance = {}, []
    counts = {split: {"sft": 0, "dpo": 0, "components": 0, "groups": 0} for split in ("train", "eval")}
    for component in component_info:
        counts[component["split"]]["components"] += 1
        counts[component["split"]]["groups"] += len(component["group_ids"])
    for kind in ("sft", "dpo"):
        handles, digests, sizes = {}, {}, {}
        try:
            for split in ("train", "eval"):
                handles[split] = _new_file(out / split / (kind + ".jsonl"))
                digests[split], sizes[split] = hashlib.sha256(), 0
            with _open_regular(snapshot / (kind + ".jsonl")) as incoming:
                for line, raw in enumerate(incoming, 1):
                    item = plan[(kind, line)]
                    split = item["split"]
                    handles[split].write(raw)
                    digests[split].update(raw)
                    sizes[split] += len(raw)
                    counts[split][kind] += 1
                    line_provenance.append({"file": split + "/" + kind + ".jsonl", "line": counts[split][kind],
                                            "input_file": kind + ".jsonl", "input_line": line,
                                            "sample_digest": item["sample_digest"], "component_id": item["component_id"],
                                            "group_ids": item["groups"], "origins": item["origins"]})
            for split, handle in handles.items():
                handle.flush()
                os.fsync(handle.fileno())
                files[split + "/" + kind + ".jsonl"] = {"records": counts[split][kind], "bytes": sizes[split],
                                                          "sha256": digests[split].hexdigest()}
        finally:
            for handle in handles.values():
                handle.close()
    for split in ("train", "eval"):
        if counts[split]["sft"] + counts[split]["dpo"] == 0:
            warnings.add("empty_" + split + "_split")
    if not plan:
        warnings.add("no_training_samples")
    report = {"schema": SCHEMA, "complete": True, "profile": manifest["profile"],
              "input_manifest_sha256": manifest_sha, "input_files": copy.deepcopy(manifest["files"]),
              "group_by": group_by, "eval_ratio": ratio, "seed": seed, "algorithm": ALGORITHM,
              "files": files, "counts": counts, "components": component_info,
              "line_provenance": line_provenance, "warnings": sorted(warnings),
              "evaluation_validated": False,
              "partition_nonempty": all(counts[s]["sft"] + counts[s]["dpo"] > 0 for s in ("train", "eval"))}
    # Publishing the distinct manifest is the final step. Interrupted output has
    # no complete split-manifest and must not be used as a validated dataset.
    with _new_file(out / "split-manifest.json") as handle:
        handle.write((_json(report) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    return report


def split_dataset(dataset, out, *, group_by="project", eval_ratio=0.1, seed="nous-v1"):
    """Split a completed dataset into new train/eval folders, or raise SplitError.

    Missing group identity is an error. DPO requires an explicit source_record_id
    even when ordinary export allowed a standalone preference. A private verified
    snapshot prevents changes after input validation from entering the split.
    """
    if group_by not in ("project", "session"):
        _fail("unsupported_group_by")
    if (type(eval_ratio) not in (int, float) or not 0 <= eval_ratio <= 1
            or not math.isfinite(eval_ratio)):
        _fail("invalid_eval_ratio")
    if not isinstance(seed, str) or not seed or len(seed) > 1024:
        _fail("invalid_split_seed")
    try:
        source, target = Path(dataset), Path(out)
        if validate_dataset(source).get("valid") is not True:
            _fail("invalid_dataset")
        source_real, target_real = source.resolve(), target.resolve()
        if source_real == target_real or source_real in target_real.parents:
            _fail("output_inside_input_dataset")
        if os.path.lexists(target):
            _fail("output_already_exists")
        with tempfile.TemporaryDirectory(prefix="nous-training-split-") as temporary:
            snapshot = Path(temporary)
            os.chmod(snapshot, 0o700)
            manifest, manifest_sha = _verified_snapshot(source, snapshot)
            plan, components, warnings = _plan(snapshot, manifest, group_by, eval_ratio, seed)
            return _publish(snapshot, target, manifest, manifest_sha, plan, components, warnings,
                            group_by, eval_ratio, seed)
    except SplitError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        _fail("split_failed")
