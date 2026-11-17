"""The upload spool for the Nous data program: events in schema nous.step/2 and their masked content, waiting on this
computer until the uploader sends them (data-platform/DESIGN.md §4–5).

Only sessions nous_consent.decide() allows (or holds as pending) get here; everything is scrubbed (nous_privacy) first.
  ~/.nous-colleague/spool/ready/<repo_key>/<session>.jsonl     may be uploaded
  ~/.nous-colleague/spool/pending/<repo_key>/<session>.jsonl   waits until the person says the project may be shared
  ~/.nous-colleague/spool/blobs/<aa>/<sha256>.gz               content, addressed by the sha256 of the masked text
Events carry only blob hashes; a blob is stored once however often it is referenced. Besides the steps themselves:
label events learned later (reading, rework, undo, human_edit, ci_status) that point back at trace steps, the turn's
transcript on Stop, and project snapshots (snapshot_events, called by the uploader). Standard library only."""
import glob, gzip, hashlib, json, os, re, secrets, shutil, time

HOME = os.environ.get("NOUS_HOME") or os.path.expanduser("~/.nous-colleague")
SPOOL = os.path.join(HOME, "spool")
SCHEMA = "nous.step/2"
KINDS = {"UserPromptSubmit": "prompt", "PreToolUse": "decision", "PostToolUse": "tool_result", "PostToolUseFailure": "tool_result",
         "Stop": "reply",
         # labels: what was learned later about earlier steps (they point back with session, turn and trace steps)
         "Reading": "reading", "Rework": "rework", "Undo": "undo", "HumanEdit": "human_edit", "CI": "ci_status"}
LABELS = {"reading", "rework", "undo", "human_edit", "ci_status", "snapshot"}
TRANSCRIPT_MAX = 20_000_000                        # characters of one turn's transcript; beyond this only the size is kept
_DECISIONS = {}                                    # cwd -> decision, for this process


def _dir(*p):
    d = os.path.join(SPOOL, *p); os.makedirs(d, mode=0o700, exist_ok=True); return d


def put_blob(text, kind):
    """Masked text -> sha256 (stored once, gzip, 0600). -> (sha, {type: count})"""
    import nous_privacy
    clean, counts = nous_privacy.scrub(text)
    raw = clean.encode("utf-8"); sha = hashlib.sha256(raw).hexdigest()
    path = os.path.join(_dir("blobs", sha[:2]), sha + ".gz")
    if not os.path.exists(path):
        tmp = path + "." + secrets.token_hex(4)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f: f.write(gzip.compress(raw))
        os.replace(tmp, path)
        with open(path + ".meta", "w") as f: json.dump({"sha256": sha, "size": len(raw), "kind": kind,
                                                        "scrubber_version": nous_privacy.SCRUBBER_VERSION}, f)
    return sha, counts


def blob_path(sha):
    return os.path.join(SPOOL, "blobs", sha[:2], sha + ".gz")


def _strings(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, str): out.append(obj)
    elif isinstance(obj, dict): [_strings(v, out) for v in obj.values()]
    elif isinstance(obj, list): [_strings(v, out) for v in obj]
    return out


def _decision(cwd):
    if cwd not in _DECISIONS:
        import nous_consent
        _DECISIONS[cwd] = nous_consent.decide(cwd)
    return _DECISIONS[cwd]


def _turn(session, ev):
    p = os.path.join(_dir("state"), (session or "none")[:80] + ".json")
    try:
        with open(p) as f: st = json.load(f)
    except (OSError, ValueError):
        st = {"turn": 0, "seq": 0}
    if ev == "UserPromptSubmit": st["turn"] += 1
    st["seq"] += 1
    with open(p, "w") as f: json.dump(st, f)
    return st["turn"], st["seq"]


def path_sha(path):
    """A file's stable, non-reversible id in events (the same for a write, a read, a later check and a human edit)."""
    return hashlib.sha256(os.path.realpath(str(path)).encode()).hexdigest()[:16]


def rel(path, cwd):
    """The path inside the project (what a model sees as the file name); never the person's home folder."""
    p, c = os.path.realpath(str(path)), os.path.realpath(cwd or ".")
    return os.path.relpath(p, c) if p.startswith(c.rstrip("/") + "/") else os.path.basename(p)


def _files(per_path, cwd, **fields):
    """{path: value} -> [{path_sha, rel, ...}]; files that are never shared keep only their id."""
    import nous_privacy
    out = []
    for p, v in sorted(per_path.items()):
        f = {"path_sha": path_sha(p), **({"rel": rel(p, cwd)} if not nous_privacy.excluded(p, cwd) else {"excluded": True})}
        f.update({k: fn(v) for k, fn in fields.items()})
        out.append(f)
    return out[:500]


def base_event(rec, dec, kind):
    import nous_consent
    subject, device = nous_consent.ids()
    e = {"event_id": f"{int(time.time() * 1000):013x}{secrets.token_hex(8)}", "schema": SCHEMA,
         "tenant_id": dec["tenant"], "subject_pid": subject, "device_id": device, "consent_id": dec["consent_id"],
         "session_id": rec.get("session"), "ts": rec.get("ts"), "kind": kind,
         "agent": {"name": rec.get("agent") or "claude", "app": rec.get("app")},
         "model": {"id": rec.get("model"), "endpoint_host": rec.get("endpoint"), "endpoint_region": rec.get("endpoint_region")},
         "env": {"repo_key": dec["repo_key"], "base_commit": rec.get("base")},
         "context": {"completeness": "partial"}, "sampling": {"reason": "all", "rate": 1.0}}
    if rec.get("step") is not None: e["trace_step"] = rec["step"]      # the step in this computer's trajectory log
    return e


def write_event(dec, session, e):
    where = "ready" if dec["upload"] is True else "pending"
    p = os.path.join(_dir(where, dec["repo_key"]), (session or "none")[:80] + ".jsonl")
    with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def _transcript(msgs, cwd, blob, excluded):
    """One turn of the agent's own transcript (nous_trace.transcript_slice) -> blob, with files that are never shared
    taken out of tool calls and their results. -> context fields"""
    import nous_privacy
    drop = {}
    for m in msgs:
        for b in m.get("content") or []:
            if b.get("type") != "tool_use" or not isinstance(b.get("input"), dict): continue
            path = b["input"].get("file_path") or b["input"].get("notebook_path")
            why = nous_privacy.excluded(path, cwd) if path else None
            if why:
                drop[b.get("id")] = why; excluded.append(why)
                b["input"] = {k: v for k, v in b["input"].items() if k not in ("content", "new_string", "old_string", "edits", "new_source")}
    for m in msgs:
        for b in m.get("content") or []:
            if b.get("type") == "tool_result" and b.get("tool_use_id") in drop: b["content"] = f"[不分享：{drop[b['tool_use_id']]}]"
    text = json.dumps(msgs, ensure_ascii=False)
    if len(text) > TRANSCRIPT_MAX: return {"completeness": "partial", "transcript_chars": len(text), "transcript_too_large": True}
    return {"completeness": "transcript", "transcript_ref": blob(text, "transcript"), "messages": len(msgs),
            "missing": ["system_prompt", "tool_definitions"]}            # only an API gateway sees those (DESIGN P6)


def capture(rec, d):
    """One trace record (nous_trace) -> one nous.step/2 event in the spool, if this project may be shared. Never raises."""
    try:
        ev = rec.get("event")
        if ev not in KINDS: return None
        if ev == "Reading" and rec.get("readings") is None: return None    # nothing was read (the reader was not running)
        dec = _decision(rec.get("cwd") or "")
        if dec["upload"] is False: return None
        import nous_privacy
        counts, excluded = {}, []
        def blob(text, kind):
            if not text: return None
            sha, c = put_blob(text, kind)
            for k, v in c.items(): counts[k] = counts.get(k, 0) + v
            return sha
        e = base_event(rec, dec, KINDS[ev])
        e["turn_id"], e["step_seq"] = _turn(rec.get("session"), ev)
        cwd = rec.get("cwd") or None
        # HMAC under this computer's key: pairs a call's decision and result, never reversible. 32 hex characters, because
        # every 64-hex string in an event is a blob reference to the uploader and the server.
        if rec.get("digest"): e["digest"] = rec["digest"][:32]
        if ev == "UserPromptSubmit":
            e["prompt_ref"] = blob(str(d.get("user_input") or d.get("prompt") or ""), "prompt")
        elif ev == "Stop":
            e["reply_ref"] = blob(str(d.get("last_assistant_message") or ""), "output")
            if rec.get("claims"): e["claims"] = [{"kind": c["kind"], "backed": not c.get("why")} for c in rec["claims"]]
            if rec.get("usage"): e["usage"] = rec["usage"]
            if (rec.get("settled") or {}).get("dropped"):
                e["settled_dropped"] = _files(rec["settled"]["dropped"], cwd, lines=len)
            msgs, version = (d["_transcript"]() if callable(d.get("_transcript")) else (None, None))
            if msgs: e["context"] = _transcript(msgs, cwd, blob, excluded)
            if version: e["agent"]["version"] = version
        elif ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            ti = dict(d.get("tool_input") or {})
            path = ti.get("file_path") or ti.get("notebook_path")
            why = nous_privacy.excluded(path, cwd) if path else None
            if why:                                       # the file itself is never shared, not even what was written to it
                excluded.append(why)
                for k in ("content", "new_string", "old_string", "edits", "new_source"): ti.pop(k, None)
            e["tool"] = {"name": d.get("_tool") or d.get("tool_name"), "canonical": rec.get("tool"), "kind": rec.get("kind"),
                         "input_ref": blob(json.dumps(ti, ensure_ascii=False, sort_keys=True), "input")}
            for k in ("shape", "test_cmd"):
                if rec.get(k): e["tool"][k] = rec[k]
            if ev != "PreToolUse":
                out = rec.get("outcome") or {}
                e["tool"].update(ok=out.get("ok"), exit_code=out.get("exit"), tests=out.get("tests"), error_kind=out.get("error_kind"))
                if rec.get("added"): e["tool"]["added"] = _files(rec["added"], cwd, lines=len)    # what a later check counts
                if rec.get("commit"): e["tool"]["commit"] = rec["commit"]
                if not why:
                    e["tool"]["output_ref"] = blob("\n".join(_strings(d.get("tool_response"))) or str(d.get("error") or ""), "output")
                    if rec.get("tool") == "Read" and path: e["context"]["files_read"] = [{"path_sha": path_sha(path),
                                                                                          "blob": e["tool"]["output_ref"]}]
            if rec.get("decision"): e["decision"] = {k: v for k, v in rec["decision"].items() if k not in ("by",)}
        else:
            e.update(_label(rec, d, cwd, blob))
        e["privacy"] = {"scrubber_version": nous_privacy.SCRUBBER_VERSION, "redactions": counts, "excluded_files": len(excluded)}
        if dec["upload"] == "pending": remember(dec["repo_key"], rec.get("cwd"))
        write_event(dec, rec.get("session"), e)
        return e
    except Exception:
        return None


def _label(rec, d, cwd, blob):
    """The label events: they carry no prompt or code of their own, except a person's later edit (as a diff)."""
    ev = rec["event"]
    if ev == "Reading":
        return {"readings": {"findings": list((rec.get("readings") or {}).get("findings") or [])}}
    if ev == "Rework":
        writers = rec.get("writers") or {}
        return {"rework": {"milestone": rec.get("milestone"), "reverted": rec.get("reverted") or [],
                           "files": _files(rec.get("files") or {}, cwd, added=lambda v: v["added"], alive=lambda v: v["alive"]),
                           "writers": {path_sha(p): s for p, s in writers.items()}}}
    if ev == "Undo":
        return {"undo": rec.get("undo") or {}}
    if ev == "HumanEdit":
        he = rec.get("human_edit") or {}
        out = {"human_edit": {"milestone": he.get("milestone"), "days": he.get("days"),
                              "files": list(he.get("files") or []),
                              "writers": he.get("writers") or {}}}
        diff = d.get("_diff")() if callable(d.get("_diff")) else d.get("_diff")
        if diff: out["human_edit"]["diff_ref"] = blob(diff, "diff")
        return out
    if ev == "CI":
        return {"ci": {k: rec.get(k) for k in ("state", "commit", "source")}}
    return {}


def remember(repo_key, cwd):
    """Which folder a pending project is, so the person can be asked about it. Local only: never uploaded."""
    p = os.path.join(_dir("state"), "_paths.json")
    try:
        with open(p) as f: m = json.load(f)
    except (OSError, ValueError):
        m = {}
    if m.get(repo_key) != cwd:
        m[repo_key] = cwd
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: json.dump(m, f, ensure_ascii=False)


def path_of(repo_key):
    try:
        with open(os.path.join(SPOOL, "state", "_paths.json")) as f: return json.load(f).get(repo_key)
    except (OSError, ValueError):
        return None


def promote(repo_key):
    """The person said this project may be shared: its pending events become ready."""
    src = os.path.join(SPOOL, "pending", repo_key)
    if not os.path.isdir(src): return 0
    dst = _dir("ready", repo_key); n = 0
    for f in glob.glob(os.path.join(src, "*.jsonl")):
        with open(f, encoding="utf-8") as a, open(os.path.join(dst, os.path.basename(f)), "a", encoding="utf-8") as b: b.write(a.read())
        os.remove(f); n += 1
    shutil.rmtree(src, ignore_errors=True)
    return n


def pending_repos():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(SPOOL, "pending", "*")) if os.path.isdir(p))


def purge(all_=False, repo_key=None, older_than=None):
    """Delete waiting events: everything (consent withdrawn), one project (marked not shareable), or pending events older
    than `older_than` seconds (never decided). Blobs nothing references any more go too. -> files removed"""
    n, now = 0, time.time()
    for where in ("ready", "pending"):
        for d in glob.glob(os.path.join(SPOOL, where, "*")):
            for f in glob.glob(os.path.join(d, "*.jsonl")):
                stale = older_than and where == "pending" and now - os.path.getmtime(f) > older_than
                if all_ or os.path.basename(d) == repo_key or stale: os.remove(f); n += 1
            if os.path.isdir(d) and not os.listdir(d): os.rmdir(d)
    if all_:
        for f in glob.glob(os.path.join(SPOOL, "outbox", "*")): os.remove(f); n += 1
    gc()
    return n


def referenced():
    refs = set()
    for f in glob.glob(os.path.join(SPOOL, "*", "*", "*.jsonl")) + glob.glob(os.path.join(SPOOL, "outbox", "*.json*")):
        try:
            with open(f, "rb") as fh: raw = fh.read()
            txt = (gzip.decompress(raw) if f.endswith(".gz") else raw).decode("utf-8", "replace")
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        refs.update(re.findall(r"[0-9a-f]{64}", txt))
    return refs


def gc():
    keep = referenced(); n = 0
    for p in glob.glob(os.path.join(SPOOL, "blobs", "*", "*.gz")):
        if os.path.basename(p)[:-3] not in keep:
            os.remove(p); n += 1
            if os.path.exists(p + ".meta"): os.remove(p + ".meta")
    return n


# ---------------------------------------------------------------- project snapshots (replay; only with upload_snapshot)
SNAPSHOT_MAX = 50 * 2 ** 20                        # bytes of shareable files in one snapshot; beyond this: listed, not stored
LOCKFILES = re.compile(r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|uv\.lock|poetry\.lock|Pipfile\.lock|"
                       r"requirements[\w.-]*\.txt|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock|\.python-version|\.nvmrc|\.tool-versions)$")


def _git_out(top, *args, data=None, timeout=120):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", top, *args], input=data, capture_output=True, timeout=timeout,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _cat(top, shas):
    """{sha: bytes} through one `git cat-file --batch`."""
    raw = _git_out(top, "cat-file", "--batch", data=b"".join(s.encode() + b"\n" for s in shas))
    out, i = {}, 0
    while raw and i < len(raw):
        nl = raw.index(b"\n", i); head = raw[i:nl].split()
        if len(head) < 3: break                       # "<sha> missing"
        size = int(head[2]); out[head[0].decode()] = raw[nl + 1:nl + 1 + size]; i = nl + 1 + size + 1
    return out


def runtime():
    """What the snapshot ran on (seen when it is uploaded, not when the session ran)."""
    import platform, subprocess
    out = {"os": platform.system(), "os_release": platform.release(), "machine": platform.machine()}
    for name, cmd in (("python", ["python3", "--version"]), ("node", ["node", "--version"])):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            v = (r.stdout or r.stderr).strip().split()
            out[name] = v[-1] if r.returncode == 0 and v else None
        except (OSError, subprocess.TimeoutExpired):
            out[name] = None
    return out


def snapshot_manifest(top, tree):
    """A git tree (nous_undo's snapshot of the whole working tree, .gitignore respected) -> manifest, with every shareable
    text file stored as a masked blob. Never-shared files are listed by path and reason only. -> (manifest, counts) or None"""
    import nous_privacy
    ls = _git_out(top, "ls-tree", "-r", "-z", "--long", tree)
    if ls is None: return None
    keep, skipped, total = [], [], 0
    for item in ls.split(b"\0"):
        if not item or b"\t" not in item: continue
        meta, path = item.split(b"\t", 1)
        mode, typ, sha, size = meta.decode().split()
        path = os.fsdecode(path)
        if typ != "blob": skipped.append({"path": path, "why": "子模块"}); continue
        why = nous_privacy.excluded_name(path) or ("超过 1 MB" if size.isdigit() and int(size) > nous_privacy.MAX_BYTES else None)
        if why: skipped.append({"path": path, "why": why}); continue
        keep.append((mode, sha, path)); total += int(size) if size.isdigit() else 0
    man = {"git_tree": tree, "files": [], "skipped": skipped, "lockfiles": sorted(p for _, _, p in keep if LOCKFILES.search(p))}
    if total > SNAPSHOT_MAX:
        man.update(too_large=True, bytes=total, n_files=len(keep)); return man, {}
    data, counts = _cat(top, sorted({s for _, s, _ in keep})), {}
    for mode, sha, path in keep:
        b = data.get(sha)
        if b is None: skipped.append({"path": path, "why": "读不到"}); continue
        if mode == "120000": man["files"].append({"path": path, "mode": mode, "link": os.fsdecode(b)}); continue
        if b"\0" in b[:8192]: skipped.append({"path": path, "why": "二进制文件"}); continue
        try: text = b.decode("utf-8")
        except UnicodeDecodeError: skipped.append({"path": path, "why": "不是 UTF-8 文本"}); continue
        ref, c = put_blob(text, "file")
        for k, v in c.items(): counts[k] = counts.get(k, 0) + v
        man["files"].append({"path": path, "mode": mode, "blob": ref, "bytes": len(b), **({"masked": True} if c else {})})
    man["bytes"] = sum(f.get("bytes", 0) for f in man["files"])
    return man, counts


def snapshot_events(session, repo_key, dec, meta, sent):
    """The session's start and end snapshots (nous_undo) of the project this spool file is for -> snapshot events
    (not yet written). `sent`: git trees already sent for this session (updated in place)."""
    import nous_undo, nous_consent, nous_privacy
    idx = nous_undo._load(nous_undo._index_path(session)) or {}
    out = []
    for top in dict.fromkeys(s["repo"] for s in idx.get("snaps") or [] if s["kind"] == "git"):
        if not os.path.isdir(top) or nous_consent.repo_of(top)[0] != repo_key: continue
        gs = [s for s in idx["snaps"] if s["kind"] == "git" and s["repo"] == top]
        ends = [s for s in gs if s["reason"] == "end"]
        for which, snap in (("start", gs[0]), ("end", ends[-1] if ends else None)):
            if not snap or snap["tree"] in sent: continue
            got = snapshot_manifest(top, snap["tree"])
            if not got: continue
            man, counts = got
            ref, c2 = put_blob(json.dumps(man, ensure_ascii=False), "manifest")
            e = base_event({**meta, "session": session, "ts": snap["ts"], "base": snap.get("head")}, dec, "snapshot")
            e["turn_id"], e["step_seq"] = _turn(session, "Snapshot")
            e["snapshot"] = {"which": which, "manifest_ref": ref, "git_tree": snap["tree"], "files": len(man["files"]),
                             "skipped": len(man["skipped"]), "bytes": man.get("bytes", 0), "too_large": bool(man.get("too_large")),
                             "lockfiles": man["lockfiles"], "runtime": runtime(),
                             # listed here too, so the uploader sends them, the server checks them and gc() keeps them
                             "blobs": sorted({f["blob"] for f in man["files"] if f.get("blob")})}
            red = dict(counts)
            for k, v in c2.items(): red[k] = red.get(k, 0) + v
            e["privacy"] = {"scrubber_version": nous_privacy.SCRUBBER_VERSION, "redactions": red, "excluded_files": len(man["skipped"])}
            out.append(e); sent.append(snap["tree"])
    return out
