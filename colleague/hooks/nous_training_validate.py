#!/usr/bin/env python3
"""Offline verification of a completed Nous training dataset; never prints content.

validate_dataset(path) returns {valid, errors, counters}. Each error contains a
static code and optional known artifact name/line number. No network/dependencies.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from nous_training_export import (
    InvalidRecord, PROFILES, _SHA, _digest, _envelope, _evidence, _lineage, _safe_id,
    negative_evidence, negative_samples_for, preference_digest, preference_key, record_digest, sample_for,
    strict_json, validate_canonical_messages, validate_sample, samples_for, training_evidence, formats_for,
    validate_preference_source, _json,
)

FILES = ("trajectories.jsonl", "preferences.jsonl", "sft.jsonl", "dpo.jsonl", "rejected.jsonl")
FORMATS = {"trajectory": "trajectories.jsonl", "preference": "preferences.jsonl",
           "sft": "sft.jsonl", "dpo": "dpo.jsonl"}


def _validate_dataset(path):
    errors, counters = [], collections.Counter()

    def error(code, file=None, line=None):
        counters["errors"] += 1
        if len(errors) < 1000:
            item = {"code": code}
            if file:
                item["file"] = file
            if line is not None:
                item["line"] = line
            errors.append(item)

    def result():
        return {"valid": counters["errors"] == 0, "errors": errors,
                "counters": {k: counters[k] for k in ("trajectories", "preferences", "sft", "dpo", "rejected", "errors")}}

    root = Path(path)
    manifest_path = root / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        error("complete_manifest_missing")
        return result()
    try:
        manifest = strict_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, InvalidRecord):
        error("invalid_manifest")
        return result()
    if not isinstance(manifest, dict) or manifest.get("schema") != "nous.training.manifest/2":
        error("unsupported_manifest_schema")
        return result()
    if manifest.get("complete") is not True:
        error("incomplete_manifest")
    profile = manifest.get("profile")
    if profile not in PROFILES:
        error("unsupported_profile")
        return result()
    expected_formats = formats_for(profile)
    if manifest.get("formats") != expected_formats:
        error("manifest_format_mismatch")
    declared = manifest.get("files")
    if not isinstance(declared, dict) or set(declared) != set(FILES):
        error("invalid_manifest_files")
        return result()

    with tempfile.TemporaryDirectory(prefix="nous-validation-") as temp:
        db = sqlite3.connect(os.path.join(temp, "index.sqlite"))
        os.chmod(os.path.join(temp, "index.sqlite"), 0o600)
        try:
            db.executescript("""
                CREATE TABLE sources(file TEXT,line INTEGER,digest TEXT,record_id TEXT,data TEXT,PRIMARY KEY(file,line));
                CREATE INDEX source_ids ON sources(record_id);
                CREATE TABLE samples(kind TEXT,line INTEGER,digest TEXT,data TEXT,PRIMARY KEY(kind,line));
                CREATE INDEX sample_digest ON samples(kind,digest);
                CREATE TABLE blocked(kind TEXT,digest TEXT,PRIMARY KEY(kind,digest));
                CREATE TABLE blocked_samples(kind TEXT,digest TEXT,PRIMARY KEY(kind,digest));
                CREATE TABLE directions(pair_key TEXT,orientation INTEGER,PRIMARY KEY(pair_key,orientation));
                CREATE TABLE covered(kind TEXT,line INTEGER,PRIMARY KEY(kind,line));
                CREATE TABLE covered_origins(kind TEXT,line INTEGER,source_file TEXT,source_line INTEGER,target TEXT,
                    PRIMARY KEY(kind,line,source_file,source_line,target));
            """)

            def check_preference_source(record):
                provenance = record.get("provenance", {})
                rid = provenance.get("source_record_id") if isinstance(provenance, dict) else None
                rows = db.execute("SELECT data FROM sources WHERE file='trajectories.jsonl' AND record_id=?", (rid,)) if isinstance(rid, str) else ()
                validate_preference_source(record, (strict_json(row[0]) for row in rows))

            rejected_counts = collections.Counter()
            for name in FILES:
                artifact = root / name
                info = declared[name]
                if (not isinstance(info, dict) or set(info) != {"records", "bytes", "sha256"}
                        or any(type(info.get(k)) is not int or info[k] < 0 for k in ("records", "bytes"))
                        or not isinstance(info.get("sha256"), str) or not _SHA.fullmatch(info["sha256"])):
                    error("invalid_file_manifest", name)
                    continue
                if not artifact.is_file() or artifact.is_symlink():
                    error("artifact_missing_or_symlink", name)
                    continue
                sha, size, rows = hashlib.sha256(), 0, 0
                try:
                    with artifact.open("rb") as stream:
                        for line, raw in enumerate(stream, 1):
                            sha.update(raw)
                            size += len(raw)
                            rows += 1
                            counters[name.removesuffix(".jsonl")] += 1
                            try:
                                obj = strict_json(raw.decode("utf-8"))
                                if name in ("trajectories.jsonl", "preferences.jsonl"):
                                    is_pref = name == "preferences.jsonl"
                                    kind = "dpo" if is_pref else "sft"
                                    _envelope(obj, preference=is_pref)
                                    if is_pref and obj.get("schema") not in ("nous.preference/1", "nous.preference/2"):
                                        raise InvalidRecord("unsupported_preference_schema")
                                    for key in (("prompt", "chosen", "rejected") if is_pref else ("messages",)):
                                        validate_canonical_messages(obj.get(key))
                                    if not isinstance(obj.get("tools", []), list):
                                        raise InvalidRecord("invalid_tool_schemas")
                                    digest = preference_digest(obj) if is_pref else record_digest(obj)
                                    db.execute("INSERT INTO sources VALUES (?,?,?,?,?)", (name, line, digest, obj["id"], raw.decode("utf-8")))
                                    if negative_evidence(obj, digest):
                                        db.execute("INSERT OR IGNORE INTO blocked VALUES (?,?)", (kind, digest))
                                    try:
                                        for negative_sample in negative_samples_for(obj, kind, profile, digest):
                                            db.execute("INSERT OR IGNORE INTO blocked_samples VALUES (?,?)", (kind, _digest(negative_sample)))
                                    except InvalidRecord:
                                        pass
                                    if is_pref:
                                        try:
                                            sample = sample_for(obj, "dpo", profile)
                                            _evidence(obj, digest, preference=True)
                                            check_preference_source(obj)
                                            pair, orientation = preference_key(sample)
                                            db.execute("INSERT OR IGNORE INTO directions VALUES (?,?)", (pair, orientation))
                                        except InvalidRecord:
                                            pass  # A canonical observation need not be trainable.
                                elif name in ("sft.jsonl", "dpo.jsonl"):
                                    kind = name[:-6]
                                    validate_sample(obj, kind, profile)
                                    digest = _digest(obj)
                                    if db.execute("SELECT 1 FROM samples WHERE kind=? AND digest=?", (kind, digest)).fetchone():
                                        error("duplicate_training_sample", name, line)
                                    db.execute("INSERT INTO samples VALUES (?,?,?,?)", (kind, line, digest, raw.decode("utf-8")))
                                else:
                                    if (not isinstance(obj, dict) or not {"stage", "reason"} <= set(obj)
                                            or set(obj) - {"stage", "reason", "id", "record_digest"}
                                            or obj.get("stage") not in ("canonical", "trajectory", "preference", "sft", "dpo")
                                            or not isinstance(obj.get("reason"), str)
                                            or re.fullmatch(r"[a-z][a-z0-9_]*", obj["reason"]) is None):
                                        raise InvalidRecord("invalid_rejection_record")
                                    if "id" in obj and (not isinstance(obj["id"], str) or _safe_id(obj["id"]) != obj["id"]):
                                        raise InvalidRecord("invalid_rejection_record")
                                    if "record_digest" in obj and (not isinstance(obj["record_digest"], str) or not _SHA.fullmatch(obj["record_digest"])):
                                        raise InvalidRecord("invalid_rejection_record")
                                    rejected_counts[obj["stage"] + ":" + obj["reason"]] += 1
                            except InvalidRecord as exc:
                                error(str(exc), name, line)
                            except (UnicodeError, TypeError, ValueError, KeyError, RecursionError):
                                error("invalid_artifact_record", name, line)
                except OSError:
                    error("artifact_read_failed", name)
                    continue
                if sha.hexdigest() != info["sha256"]:
                    error("file_sha256_mismatch", name)
                if size != info["bytes"]:
                    error("file_byte_count_mismatch", name)
                if rows != info["records"]:
                    error("file_line_count_mismatch", name)
            if dict(rejected_counts) != manifest.get("rejections"):
                error("rejection_summary_mismatch")
            lineage = manifest.get("lineage")
            if not isinstance(lineage, list):
                error("lineage_missing")
                lineage = []
            for entry in lineage:
                try:
                    if not isinstance(entry, dict) or entry.get("format") not in FORMATS or type(entry.get("line")) is not int or entry["line"] < 1:
                        raise InvalidRecord("invalid_lineage")
                    kind, line = entry["format"], entry["line"]
                    if kind in ("trajectory", "preference"):
                        source_file, source_line = FORMATS[kind], line
                    else:
                        source_file = "trajectories.jsonl" if kind == "sft" else "preferences.jsonl"
                        source_line = entry.get("source_line")
                        if entry.get("source_file") != source_file or type(source_line) is not int or source_line < 1:
                            raise InvalidRecord("invalid_lineage_source")
                    source_row = db.execute("SELECT digest,data FROM sources WHERE file=? AND line=?", (source_file, source_line)).fetchone()
                    if source_row is None:
                        raise InvalidRecord("lineage_source_missing")
                    digest, raw = source_row
                    source = strict_json(raw)
                    if entry.get("record_digest") != digest or entry.get("id") != _safe_id(source["id"]):
                        raise InvalidRecord("lineage_record_mismatch")
                    expected_meta = _lineage(source, digest, line, kind, [])
                    if entry.get("provenance") != expected_meta["provenance"]:
                        raise InvalidRecord("lineage_provenance_mismatch")
                    if kind in ("sft", "dpo"):
                        stored = db.execute("SELECT digest FROM samples WHERE kind=? AND line=?", (kind, line)).fetchone()
                        if stored is None:
                            raise InvalidRecord("lineage_training_line_missing")
                        sample = sample_for(source, kind, profile, target=entry.get("target"))
                        sample_digest = _digest(sample)
                        if entry.get("sample_digest") != sample_digest or stored[0] != sample_digest:
                            raise InvalidRecord("lineage_sample_mismatch")
                        evidence = training_evidence(source, digest, kind, profile)
                        if entry.get("evidence_kinds") != evidence:
                            raise InvalidRecord("lineage_evidence_mismatch")
                        if (db.execute("SELECT 1 FROM blocked WHERE kind=? AND digest=?", (kind, digest)).fetchone()
                                or db.execute("SELECT 1 FROM blocked_samples WHERE kind=? AND digest=?", (kind, sample_digest)).fetchone()):
                            raise InvalidRecord("cross_record_evidence_conflict")
                        if kind == "dpo":
                            pair, orientation = preference_key(sample)
                            if db.execute("SELECT 1 FROM directions WHERE pair_key=? AND orientation!=?", (pair, orientation)).fetchone():
                                raise InvalidRecord("conflicting_preference_direction")
                            check_preference_source(source)
                        db.execute("INSERT OR IGNORE INTO covered_origins VALUES (?,?,?,?,?)",
                                   (kind, line, source_file, source_line, _json(entry.get("target"))))
                    elif entry.get("evidence_kinds") != []:
                        raise InvalidRecord("invalid_canonical_lineage")
                    db.execute("INSERT OR IGNORE INTO covered VALUES (?,?)", (kind, line))
                except InvalidRecord as exc:
                    error(str(exc), "manifest.json")
                except (TypeError, ValueError, KeyError, RecursionError):
                    error("invalid_lineage", "manifest.json")
            for kind, line in db.execute("SELECT kind,line FROM samples"):
                if not db.execute("SELECT 1 FROM covered WHERE kind=? AND line=?", (kind, line)).fetchone():
                    error("training_lineage_missing", FORMATS[kind], line)
            for file, line in db.execute("SELECT file,line FROM sources"):
                kind = "trajectory" if file == "trajectories.jsonl" else "preference"
                if not db.execute("SELECT 1 FROM covered WHERE kind=? AND line=?", (kind, line)).fetchone():
                    error("canonical_lineage_missing", file, line)
            # A surviving training row may have several accepted canonical
            # origins, including several CodeMode targets. Checking only one
            # lineage per row would let a clipped dedup origin split a project
            # across train/eval. Reconstruct every eligible source -> target map.
            for source_file, source_line, digest, raw in db.execute("SELECT file,line,digest,data FROM sources"):
                kind = "sft" if source_file == "trajectories.jsonl" else "dpo"
                try:
                    source = strict_json(raw)
                    candidates = samples_for(source, kind, profile)
                    training_evidence(source, digest, kind, profile)
                    if kind == "dpo":
                        check_preference_source(source)
                except InvalidRecord:
                    continue  # Incomplete/unreviewed canonical observations are retained.
                for sample, target in candidates:
                    sample_digest = _digest(sample)
                    if (db.execute("SELECT 1 FROM blocked WHERE kind=? AND digest=?", (kind, digest)).fetchone()
                            or db.execute("SELECT 1 FROM blocked_samples WHERE kind=? AND digest=?", (kind, sample_digest)).fetchone()):
                        continue
                    if kind == "dpo":
                        pair, orientation = preference_key(sample)
                        if db.execute("SELECT 1 FROM directions WHERE pair_key=? AND orientation!=?", (pair, orientation)).fetchone():
                            continue
                    stored = db.execute("SELECT line FROM samples WHERE kind=? AND digest=?", (kind, sample_digest)).fetchall()
                    if not stored:
                        error("eligible_training_sample_missing", source_file, source_line)
                    for (line,) in stored:
                        if not db.execute("SELECT 1 FROM covered_origins WHERE kind=? AND line=? AND source_file=? AND source_line=? AND target=?",
                                          (kind, line, source_file, source_line, _json(target))).fetchone():
                            error("training_origin_lineage_missing", source_file, source_line)
        except (OSError, sqlite3.Error):
            error("validation_io_failed")
        finally:
            db.close()
    return result()


def validate_dataset(path):
    """Content-free result even for malformed paths or filesystem failures."""
    try:
        return _validate_dataset(path)
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, RecursionError, OverflowError):
        return {"valid": False, "errors": [{"code": "validation_failed"}],
                "counters": {"trajectories": 0, "preferences": 0, "sft": 0, "dpo": 0, "rejected": 0, "errors": 1}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="completed dataset directory")
    args = parser.parse_args(argv)
    report = validate_dataset(args.dataset)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
