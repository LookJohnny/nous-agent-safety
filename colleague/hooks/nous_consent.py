#!/usr/bin/env python3
"""Nous 数据计划：谁同意了什么，以及这台电脑上的某个项目能不能上传。

Personal users may share their (masked) agent sessions with Nous to train models, after an explicit, per-purpose opt-in;
nothing is pre-ticked and Nous works the same without it. Company data never leaves through a personal account:
  1. an enterprise-managed device (managed.json from the company)           -> never uploaded
  2. a repository whose git remote belongs to a company (managed or listed)  -> never uploaded
  3. no consent, revoked, or the upload scope not given                       -> never uploaded
  4. a repository the user has not yet said they may share                    -> kept here, pending, until they decide
  5. otherwise                                                                -> uploaded
Every decision says why, and every upload carries the consent record it relied on (the server checks it again).
Design: data-platform/DESIGN.md §2–3.

  python3 nous_consent.py setup                  ask, in the terminal (installer runs this)
  python3 nous_consent.py status | notice
  python3 nous_consent.py grant --scopes upload_raw,train_nous   (non-interactive, after showing the notice elsewhere)
  python3 nous_consent.py revoke
  python3 nous_consent.py delete-my-data
  python3 nous_consent.py repo yes|no [PATH]     may this project's code be shared?
  python3 nous_consent.py work add|remove DOMAIN  e.g. git.mycompany.com or github.com/mycompany (never uploaded)
  python3 nous_consent.py buyers [yes|no BUYER]  who the data is provided to; say no to any one of them
  python3 nous_consent.py region [CODE]          where you are (CN by default in China's time zones)
A new notice does not stretch old consent: a purpose that is new or changed meaning since (SCOPE_SINCE) is not covered
until the person runs setup again.
撤回会先保存同意失效状态，再分别处理训练缓存、服务端请求排队和上传缓存清理。
任一步骤未完成时，revoke/delete-my-data 返回非零；consent.json.local_cleanup 仅保存静态状态码。
明确重试撤回会重新排队已有同意记录的通知；API 计数仍只计算本次新撤回的记录。
请求进入本地队列不代表已送达或远端已删除。手动导出目录需要另外处理。
Standard library only."""
import argparse, hashlib, json, os, re, secrets, subprocess, sys, time

HOME = os.environ.get("NOUS_HOME") or os.path.expanduser("~/.nous-colleague")
STORE = os.path.join(HOME, "consent.json")
MANAGED = [os.environ["NOUS_MANAGED"]] if os.environ.get("NOUS_MANAGED") else \
    ["/Library/Application Support/Nous/managed.json", "/etc/nous/managed.json"]

NOTICE_VERSION = "2026-11-05.1"
NOTICE = """Nous 数据计划（个人版）

Nous 免费开源。你可以选择把 AI 编程助手的使用记录分享给 Nous：打码之后，用来训练和评估 Nous 自己的模型，也会有偿
提供给训练 AI 模型的公司（下面叫"买方"），这是 Nous 的收入来源。分享是自愿的：不分享，Nous 的全部功能照常可用。

会分享什么（打码之后）：
  · 你交给 AI 的任务描述、AI 调用的工具和命令、它读写的代码、它的回复，以及 AI 当时看到的对话记录
  · 你对 AI 操作的批准与拒绝、测试和 CI 结果、所用的模型和用量
  · AI 写的代码过几天还在不在，以及之后这些文件被怎么改了（只看 AI 改过的文件）
  · 只有另外勾选 upload_snapshot 时：会话开始和结束时整个项目的代码（打码后），让买方能重新运行这个任务（"重放"）
不会分享什么：
  · 密钥、密码、令牌（在你的电脑上就被打码）；手机号、身份证号、银行卡号、邮箱等个人信息（打码为占位符）
  · .gitignore 忽略的文件、依赖目录、数据库导出、日志、证书和密钥文件、超过 1 MB 的文件
  · 公司的代码：企业托管的电脑、属于公司代码托管地址的项目、你标记为"不可分享"的项目，永远不会上传
  · 每个项目第一次使用时会问你一次"这是你有权分享的代码吗"，你确认之前，这个项目的记录只留在本机

买方：
  · 名单公开，随时可查（python3 nous_consent.py buyers）；新增买方时会通知你，你可以对任何一家单独说不
  · 买方在合同里承诺：不转给别人；你撤回或删除之后，删除你的原始数据，不再用于之后的训练
  · 目前只提供给中国境内的买方；以后如果要提供给境外买方，会先单独征得你的同意
存在哪里、存多久：数据存放在中国境内；原文默认保存 12 个月。
谁能接触：只有经批准的数据处理任务和你没有拒绝的买方；员工访问需审批，并全程留审计记录。
你的权利：随时查看、导出、更正、删除你的数据，随时撤回同意（python3 nous_consent.py revoke）。
撤回后立即停止上传，已上传的数据在 15 个工作日内删除，并通知拿到过这些数据的买方删除；已经训练完成的模型无法撤销，删除对之后的训练生效。
年龄：需年满 14 周岁。
联系：[privacy@nous.center]

可以分别选择的用途：
  upload_raw      上传打码后的使用记录（下面各项的前提）
  train_nous      用于训练和评估 Nous 自己的模型
  share_partners  有偿提供给买方用于训练模型
  upload_snapshot 上传项目的代码快照（打码后），用于重放任务
"""
NOTICE_SHA = hashlib.sha256(NOTICE.encode("utf-8")).hexdigest()
SCOPES = {"upload_raw": "上传打码后的使用记录", "train_nous": "用于训练 Nous 自己的模型", "share_partners": "有偿提供给买方用于训练模型",
          "upload_snapshot": "上传项目的代码快照，用于重放任务"}
# the notice a scope first meant what it means now: consent given under an earlier notice does not cover it (share_partners
# was "partners, list to follow" until 2026-11-05.1; it is now paid provision to named buyers)
SCOPE_SINCE = {"upload_raw": "2026-10-27.1", "train_nous": "2026-10-27.1", "share_partners": "2026-11-05.1", "upload_snapshot": "2026-11-05.1"}
CN_ZONES = ("Asia/Shanghai", "Asia/Chongqing", "Asia/Chungking", "Asia/Harbin", "Asia/Urumqi", "Asia/Kashgar", "PRC")
_TRAINING_CLEANUP_CODES = frozenset({"health_purge_failed", "records_purge_failed"})
_LAST_LOCAL_CLEANUP = None


# ---------------------------------------------------------------- store
def _load():
    try:
        with open(STORE, encoding="utf-8") as f: s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("subject_pid", "p_" + secrets.token_hex(12))          # a pseudonym: never a phone number or email
    s.setdefault("device_id", "d_" + secrets.token_hex(8))
    s.setdefault("consents", []); s.setdefault("repos", {}); s.setdefault("work", [])
    s.setdefault("buyers", []); s.setdefault("buyer_optout", [])
    return s


def _save(s):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def ids():
    s = _load(); _save(s)
    return s["subject_pid"], s["device_id"]


def active(s=None):
    """The consent in force, or None."""
    s = s or _load()
    # a later notice keeps earlier consent for the purposes it named; a new purpose is a new scope, asked for separately
    return next((c for c in reversed(s["consents"]) if not c.get("revoked_at")), None)


def in_force(c):
    """The scopes this consent really covers: a purpose that is new, or changed meaning, after the notice it was given
    under is not covered until the person agrees again."""
    if not c: return []
    v = c.get("notice_version") or ""
    return [x for x in c.get("scopes") or [] if x in SCOPE_SINCE and v >= SCOPE_SINCE[x]]


def outdated(c=None):
    """The notice changed since this consent was given: ask again (the old consent keeps covering what it covered)."""
    c = c if c is not None else active()
    return bool(c) and c.get("notice_version") != NOTICE_VERSION


def guess_region():
    """Where the person is, for whom the data may go to (cross-border rules). From the system time zone; `region` overrides."""
    tz = os.environ.get("TZ") or ""
    try: tz = tz or os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
    except (IndexError, OSError): pass
    return "CN" if tz in CN_ZONES else "unknown"


def grant(scopes, channel="cli", adult=True):
    """Record an explicit opt-in. upload_raw is required for the others."""
    scopes = [x for x in scopes if x in SCOPES]
    if not adult: raise ValueError("需年满 14 周岁")
    if any(x != "upload_raw" for x in scopes) and "upload_raw" not in scopes: raise ValueError("其他用途需要先选 upload_raw")
    s = _load(); now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    s.setdefault("region", guess_region())
    for c in s["consents"]:
        if not c.get("revoked_at"): c["revoked_at"], c["revoke_reason"] = now, "replaced"
    c = {"consent_id": "c_" + secrets.token_hex(8), "subject_pid": s["subject_pid"], "device_id": s["device_id"],
         "notice_version": NOTICE_VERSION, "notice_sha256": NOTICE_SHA, "scopes": scopes, "granted_at": now,
         "channel": channel, "region": s["region"], "revoked_at": None}
    s["consents"].append(c); _save(s)
    return c


# ---------------------------------------------------------------- buyers
def buyers():
    """-> (the buyer list last fetched from the data center, ids the person said no to)"""
    s = _load()
    return s["buyers"], s["buyer_optout"]


def set_buyers(listed):
    """The data center's current list -> the buyers that are new since last time (to tell the person about)."""
    s = _load(); known = {b.get("buyer_id") for b in s["buyers"]}
    new = [b for b in listed if b.get("buyer_id") not in known]
    s["buyers"] = listed; _save(s)
    return new


def buyer_optout(buyer_id, no=True):
    s = _load()
    s["buyer_optout"] = sorted(set(s["buyer_optout"]) | {buyer_id}) if no else [x for x in s["buyer_optout"] if x != buyer_id]
    _save(s)
    return s["buyer_optout"]


def _tell_server(kind, consent_ids):
    """Queue a revocation / deletion for the data center; the uploader delivers it (also after being offline)."""
    s = _load()
    s.setdefault("to_server", []).append({"kind": kind, "consent_ids": consent_ids, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    _save(s)


def revoke(reason="user", delete=False):
    """Revoke first, then attempt each cleanup independently; return the legacy count.

    ``local_cleanup`` in consent.json contains only a fixed schema, booleans and
    static codes, never exception text or paths. The CLI exits nonzero when any
    local cleanup or server-notice queue operation remains incomplete. A queued
    notice is not confirmation of delivery or remote deletion.
    """
    global _LAST_LOCAL_CLEANUP
    _LAST_LOCAL_CLEANUP = None
    s = _load(); now = time.strftime("%Y-%m-%dT%H:%M:%S%z"); n = 0; ids = []
    for c in s["consents"]:
        # Include already-revoked IDs so an explicit retry can recover a failed
        # queue write. The return count remains newly revoked consents only.
        ids.append(c["consent_id"])
        if not c.get("revoked_at"): c["revoked_at"], c["revoke_reason"] = now, reason; n += 1
    ids = list(dict.fromkeys(ids))
    _save(s)
    errors = set()
    try:
        import nous_training
        report = nous_training.revoke_all(purge=delete)
        if (not isinstance(report, dict) or report.get("status") != "revoked"
                or report.get("purge") is not bool(delete)
                or type(report.get("cleanup_complete")) is not bool
                or not isinstance(report.get("errors"), list)):
            errors.add("training_cleanup_failed")
        else:
            reported = report["errors"]
            if len(reported) > len(_TRAINING_CLEANUP_CODES):
                errors.add("training_cleanup_failed")
            else:
                for code in reported:
                    errors.add(code if isinstance(code, str) and code in _TRAINING_CLEANUP_CODES
                               else "training_cleanup_failed")
            if report["cleanup_complete"] is not True and not reported:
                errors.add("training_cleanup_failed")
    except Exception:
        # A training permission/write error must not prevent the other revocation
        # operations. Source-bearing exception strings are deliberately discarded.
        errors.add("training_cleanup_failed")
    notice = "not_needed"
    if reason != "server_refused" and (ids or delete):
        try:
            _tell_server("delete" if delete else "revoke", ids)
            notice = "queued"
        except Exception:
            errors.add("server_notice_queue_failed")
            notice = "failed"
    try:
        import nous_spool
        nous_spool.purge(all_=True)
    except Exception:
        errors.add("spool_purge_failed")
    cleanup = {"schema": "nous.local-cleanup/1", "operation": "delete" if delete else "revoke",
               "complete": not errors, "server_notice": notice, "errors": sorted(errors)}
    _LAST_LOCAL_CLEANUP = cleanup
    try:
        # Reload after _tell_server so this write cannot erase the queued notice.
        state = _load()
        state["local_cleanup"] = cleanup
        _save(state)
    except Exception:
        # Keep this invocation's report in memory for the CLI even when the status
        # cannot be saved; never fall back to an earlier successful disk report.
        cleanup["complete"] = False
        cleanup["errors"] = sorted(set(cleanup["errors"]) | {"cleanup_status_persist_failed"})
    return n


# ---------------------------------------------------------------- repositories
def _git(cwd, *a):
    try:
        r = subprocess.run(["git", "-C", cwd or ".", *a], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def remote_of(url):
    """git@github.com:Org/Repo.git, https://user:tok@github.com/org/repo -> 'github.com/org/repo' (no credentials)."""
    if not url: return None
    u = url.strip()
    m = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", u)                       # scp-like ssh
    if m: host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^\w+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", u)
        if not m: return u.lower()
        host, path = m.group(1), m.group(2)
    return (host + "/" + re.sub(r"\.git$", "", path.strip("/"))).lower()


def repo_of(cwd):
    """-> (repo_key, remote or None, toplevel or None). The key is what is stored and sent; the remote stays here."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    remote = remote_of(_git(cwd, "remote", "get-url", "origin")) if top else None
    basis = remote or "local:" + os.path.realpath(top or cwd or ".")
    return "r_" + hashlib.sha256(basis.encode()).hexdigest()[:16], remote, top


def set_repo(cwd, shareable):
    key, remote, top = repo_of(cwd)
    s = _load()
    s["repos"][key] = {"shareable": bool(shareable), "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "remote": remote, "path": top or os.path.realpath(cwd or ".")}
    _save(s)
    try:
        import nous_spool
        if shareable: nous_spool.promote(key)         # what waited for this answer may now go
        else: nous_spool.purge(repo_key=key)
    except Exception:
        pass
    return key


def repo_by_key(key):
    return _load()["repos"].get(key)


def managed():
    for p in MANAGED:
        try:
            with open(p, encoding="utf-8") as f: return json.load(f)
        except (OSError, ValueError):
            continue
    return None


def _work_hit(remote, domains):
    r = (remote or "").lower()
    return next((d for d in domains if d and (r == d.lower().strip("/") or r.startswith(d.lower().strip("/") + "/"))), None)


def decide(cwd):
    """-> {upload: True | False | "pending", reason, tenant, consent_id, repo_key, scopes}"""
    m = managed(); s = _load()
    key, remote, _ = repo_of(cwd)
    base = {"repo_key": key, "tenant": (m or {}).get("tenant") or "personal", "consent_id": None, "scopes": []}
    if m and not m.get("upload", False):
        return {**base, "upload": False, "reason": f"企业托管设备（{m.get('tenant') or '企业'}）：数据不离开企业环境"}
    hit = _work_hit(remote, list((m or {}).get("enterprise_repos") or []) + list(s["work"]))
    if hit: return {**base, "upload": False, "reason": f"公司的代码仓库（{hit}）：永不上传"}
    c = active(s)
    if not c or "upload_raw" not in c["scopes"]: return {**base, "upload": False, "reason": "没有同意分享（个人版默认不上传）"}
    base.update(consent_id=c["consent_id"], scopes=in_force(c))
    r = s["repos"].get(key)
    if r is None: return {**base, "upload": "pending", "reason": "这个项目还没确认是否可以分享"}
    if not r["shareable"]: return {**base, "upload": False, "reason": "你把这个项目标记为不可分享"}
    return {**base, "upload": True, "reason": "已同意，且项目可分享"}


def attestation(dec):
    """What travels with an upload so the server can check it again."""
    s = _load(); c = next((c for c in s["consents"] if c["consent_id"] == dec.get("consent_id")), None)
    r = s["repos"].get(dec.get("repo_key")) or {}
    return {"consent": {k: c[k] for k in ("consent_id", "subject_pid", "device_id", "notice_version", "notice_sha256", "scopes",
                                          "granted_at", "revoked_at")} if c else None,
            "repo": {"repo_key": dec.get("repo_key"), "shareable": r.get("shareable"), "decided_at": r.get("decided_at")},
            "tenant": dec.get("tenant"), "decided": dec.get("upload"), "reason": dec.get("reason"),
            "scopes_in_force": in_force(c), "region": (c or {}).get("region") or s.get("region") or "unknown",
            "buyer_optout": s["buyer_optout"]}


# ---------------------------------------------------------------- CLI
def setup(ask=input, out=print):
    if managed(): out("这台电脑由企业管理：使用记录只留在企业环境里，不会上传到 Nous。"); return None
    if outdated():
        out(f"数据计划的告知更新了（{active()['notice_version']} → {NOTICE_VERSION}）。之前的选择对新增或含义变了的用途不生效，请重新选择：\n")
    out(NOTICE)
    if ask("你是否已年满 14 周岁？[y/N] ").strip().lower() not in ("y", "yes", "是"):
        out("未满 14 周岁不能参加数据计划。Nous 的其他功能照常可用。"); return None
    chosen = []
    for k, v in SCOPES.items():
        if k != "upload_raw" and "upload_raw" not in chosen: break
        if ask(f"{v}（{k}）？[y/N] ").strip().lower() in ("y", "yes", "是"): chosen.append(k)
    if not chosen: out("你没有选择分享。Nous 的全部功能照常可用，记录只留在这台电脑上。"); return None
    c = grant(chosen, "installer")
    out(f"已记录你的选择：{'、'.join(SCOPES[x] for x in chosen)}。随时可以撤回：python3 nous_consent.py revoke")
    return c


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nous_consent.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup"); sub.add_parser("status"); sub.add_parser("notice"); sub.add_parser("revoke"); sub.add_parser("delete-my-data")
    g = sub.add_parser("grant"); g.add_argument("--scopes", required=True)
    b = sub.add_parser("buyers"); b.add_argument("answer", nargs="?", choices=("yes", "no")); b.add_argument("buyer", nargs="?")
    rg = sub.add_parser("region"); rg.add_argument("code", nargs="?", help="CN（中国大陆）或其他地区代码")
    r = sub.add_parser("repo"); r.add_argument("answer", choices=("yes", "no")); r.add_argument("path", nargs="?", default=".")
    w = sub.add_parser("work"); w.add_argument("op", choices=("add", "remove")); w.add_argument("domain")
    a = ap.parse_args(argv)
    if a.cmd == "setup": setup(); return 0
    if a.cmd == "notice": print(NOTICE + f"\n（告知版本 {NOTICE_VERSION}）"); return 0
    if a.cmd == "grant":
        c = grant(a.scopes.split(",")); print(f"已记录：{'、'.join(SCOPES[x] for x in c['scopes'])}（{c['consent_id']}）"); return 0
    if a.cmd in ("revoke", "delete-my-data"):
        revoke("user_delete" if a.cmd == "delete-my-data" else "user", delete=a.cmd == "delete-my-data")
        report = _LAST_LOCAL_CLEANUP
        print("已撤回数据分享同意，不再上传。")
        if report and report.get("server_notice") == "queued":
            print("服务端撤回或删除请求已加入本地待发送队列；联网送达后的处理状态需另行确认。")
        if not report or report.get("complete") is not True:
            print("本地清理或请求排队未全部完成，请处理错误后重试；不能确认全部删除。")
            print("错误代码：" + ", ".join(report["errors"] if report else ["cleanup_status_unavailable"]))
            return 1
        print("本次请求的本地清理已完成。手动导出的数据目录需另行处理。")
        return 0
    if a.cmd == "repo":
        set_repo(a.path, a.answer == "yes")
        print("已标记为可分享。" if a.answer == "yes" else "已标记为不可分享：这个项目的记录永不上传，本机等待上传的部分已删除。"); return 0
    if a.cmd == "buyers":
        if a.answer:
            if not a.buyer: print("要说明是哪一家：nous_consent.py buyers no <买方编号>"); return 2
            buyer_optout(a.buyer, a.answer == "no")
            print(f"已记录：{'不提供给' if a.answer == 'no' else '可以提供给'} {a.buyer}（下次上传起生效；已交付的数据按交付记录通知删除）。"); return 0
        listed, no = buyers()
        if not listed: print("还没有买方（或还没有从数据中心取到名单）。"); return 0
        for x in listed:
            print(f"{x.get('buyer_id')}  {x.get('name')}  {x.get('region') or ''}  {x.get('purpose') or ''}" + ("  （你已拒绝）" if x.get("buyer_id") in no else ""))
        print("\n对某一家说不：python3 nous_consent.py buyers no <编号>"); return 0
    if a.cmd == "region":
        s = _load()
        if a.code: s["region"] = a.code.upper(); _save(s)
        print(f"地区：{s.get('region') or guess_region()}（决定数据能提供给哪些买方）"); return 0
    if a.cmd == "work":
        s = _load(); d = a.domain.lower().strip("/")
        s["work"] = sorted(set(s["work"]) | {d}) if a.op == "add" else [x for x in s["work"] if x != d]
        _save(s); print(f"公司代码地址：{', '.join(s['work']) or '无'}（这些地址下的项目永不上传）"); return 0
    if a.cmd == "status":
        s = _load(); c = active(s); m = managed()
        print(f"企业托管：{'是（' + (m.get('tenant') or '') + '）' if m else '否'}")
        print(f"同意：{'、'.join(SCOPES[x] for x in in_force(c)) + '（' + c['granted_at'] + '）' if c else '未同意（不上传）'}")
        if outdated(c): print(f"告知已更新到 {NOTICE_VERSION}：运行 python3 nous_consent.py setup 重新确认（新增的用途在那之前不生效）")
        print(f"当前目录：{decide('.')['reason']}")
        print(f"已确认的项目：{sum(1 for r in s['repos'].values() if r['shareable'])} 个可分享，{sum(1 for r in s['repos'].values() if not r['shareable'])} 个不可分享")
        return 0


if __name__ == "__main__":
    sys.exit(main())
