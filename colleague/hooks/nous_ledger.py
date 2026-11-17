"""Nous 证据账本: what the AI tools on this computer tried, what Nous saw in their replies, and what Nous did about it.
The answer to an insurer's or regulator's first question after an incident: "what did you know, and what did you do?"

- Append-only JSONL, one record per event, each carrying the hash of the one before it (a hash chain): editing,
  deleting or reordering any record breaks every hash after it. `verify()` checks the chain.
- No content is stored. A command or reply is kept only as an HMAC under a key that stays on this computer, so the
  ledger can be shared without revealing what was said, and the owner can later prove a given text is the one recorded.
- The chain alone does not stop someone with full control of this computer rewriting it from the start. `anchor()`
  gives the current head; sending it somewhere the owner does not control (the daily Slack/Feishu digest, the insurer)
  makes everything before it fixed from that moment.
Standard library only: the hooks import it too, and several processes append at once (serialised with flock).
  python3 nous_ledger.py verify | head | stamp [TSA_URL] | stamps | label NAME | export OUT.tar.gz | profile [days] | report OUT.html [days] | evidence SEQ [TEXTFILE]"""
import fcntl, glob, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, time, urllib.request

HOME = os.environ.get("NOUS_LEDGER_HOME", os.path.expanduser("~/.nous-colleague/ledger"))
PATH, KEYPATH = os.path.join(HOME, "ledger.jsonl"), os.path.join(HOME, "ledger.key")
GENESIS = "0" * 64
VERSION = 1

# what an underwriter asks about, in the words of the AI-agent insurance market (see e.g. mount.insure's categories)
CATEGORY = {
    "data_exposure": "数据与密钥泄露",
    "unauthorized_action": "越权或不可撤销的操作",
    "risky_command": "高风险命令（放行并记录）",
    "prompt_injection": "提示注入（外部内容里夹带指令）",
    "hallucination": "编造（错误信息）",
    "harmful_advice": "危险建议",
    "hostility": "敌意 / 贬低用户",
    "sycophancy": "附和（顺着用户说）",
    "claim_mismatch": "说做不一（声称完成但没有做到）",
    "cross_border": "数据出境（发往境外的服务）",
}
# mitigated: Nous stopped it or made it safe. residual: it happened, Nous recorded it (and told someone).
OUTCOME = {
    "blocked": "已拦下", "masked": "已打码后放行", "vaulted": "已收进保险箱", "denied": "管理员拒绝", "timeout": "无人确认，自动拒绝",
    "held": "等待管理员批准，暂不执行", "approved": "管理员批准后执行", "asked_in_tool": "连不上管理员，交由使用者确认", "noted": "放行并记录",
    "flagged": "已标注并提醒", "clean": "未见异常",
}
MITIGATED = {"blocked", "masked", "vaulted", "held", "denied", "timeout"}
READOUT_VALIDATED = {"hostility": True, "hallucination": False, "sycophancy": False, "harmful_advice": False}


def _key():
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    try: return open(KEYPATH, "rb").read()
    except FileNotFoundError:
        try:
            fd = os.open(KEYPATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f: f.write(secrets.token_bytes(32))
        except FileExistsError: pass
        return open(KEYPATH, "rb").read()


def digest(text):
    return hmac.new(_key(), (text or "").encode("utf-8"), hashlib.sha256).hexdigest()


def _hash(rec):
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _last(f):
    """Last record, reading only the tail. Bytes, not text: the tail may start inside a multi-byte character."""
    f.seek(0, 2); end = f.tell()
    if not end: return None
    n = 4096
    while True:
        f.seek(max(0, end - n)); tail = f.read()
        lines = [l for l in tail.splitlines() if l.strip()]
        if len(lines) >= 2 or n >= end: return json.loads(lines[-1].decode("utf-8"))
        n *= 4


def append(category, outcome, source, title="", text=None, scores=None, kind="guard"):
    """Record one event. Never raises: Nous must not break the session it protects."""
    try:
        os.makedirs(HOME, mode=0o700, exist_ok=True)
        fd = os.open(PATH, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "rb+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            last = _last(f)
            rec = {"v": VERSION, "seq": (last["seq"] + 1) if last else 0, "ts": round(time.time(), 3), "kind": kind,
                   "category": category, "outcome": outcome, "source": str(source)[:80], "title": str(title)[:80],
                   "digest": digest(text) if text else None,
                   "scores": {k: round(float(v), 4) for k, v in (scores or {}).items()} or None,
                   "prev": last["hash"] if last else GENESIS}
            rec["hash"] = _hash(rec)
            f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")); f.flush(); os.fsync(f.fileno())
            return rec
    except Exception:
        return None


def records():
    try: return [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
    except FileNotFoundError: return []


def verify(recs=None):
    """-> (ok, n, problem). Checks sequence numbers, each record's own hash, and the link to the record before."""
    recs = records() if recs is None else recs; prev = GENESIS
    for i, r in enumerate(recs):
        if r.get("seq") != i: return False, i, f"第 {i} 条的序号是 {r.get('seq')}（记录被删除或插入）"
        if r.get("prev") != prev: return False, i, f"第 {i} 条与前一条接不上（前面的记录被改动）"
        if _hash(r) != r.get("hash"): return False, i, f"第 {i} 条的内容与它的哈希不符（这一条被改动）"
        prev = r["hash"]
    return True, len(recs), ""


def anchor():
    recs = records()
    return {"seq": recs[-1]["seq"], "hash": recs[-1]["hash"], "ts": recs[-1]["ts"]} if recs else {"seq": -1, "hash": GENESIS, "ts": None}


def profile(days=30, recs=None, now=None):
    """Risk profile over the last `days`: per category how often it happened and how much of it Nous stopped,
    readout rates per AI reply read, and which protections were seen working. Every number is a plain count or ratio."""
    recs = records() if recs is None else recs; now = now or time.time()
    w = [r for r in recs if r["ts"] >= now - days * 86400]
    reads = [r for r in w if r["kind"] == "reading"]
    cats = {}
    for r in w:
        if r["kind"] == "reading": continue                       # replies: counted under readouts (flagged, not stopped)
        c = cats.setdefault(r["category"], {"events": 0, "mitigated": 0, "outcomes": {}})
        c["events"] += 1; c["mitigated"] += r["outcome"] in MITIGATED; c["outcomes"][r["outcome"]] = c["outcomes"].get(r["outcome"], 0) + 1
    readouts = {}
    for k in READOUT_VALIDATED:
        n = sum(1 for r in reads if r["category"] == k and r["outcome"] == "flagged")
        if n or k in ("hostility", "hallucination", "sycophancy"):
            readouts[k] = {"flagged": n, "per_1000_replies": round(1000 * n / len(reads), 1) if reads else None, "validated": READOUT_VALIDATED[k]}
    asks = [r for r in w if r["category"] == "unauthorized_action"]
    sources = sorted({r["source"].split(" · ")[0] for r in w})
    controls = {"发送前拦截密钥": any(r["category"] == "data_exposure" and r["outcome"] in ("blocked", "vaulted") for r in w),
                "读取密钥文件自动打码": any(r["outcome"] == "masked" for r in w),
                "不可撤销操作需管理员批准": any(r["outcome"] in ("approved", "denied", "timeout") for r in w),
                "逐条读心（模型内部状态）": bool(reads)}
    return {"window_days": days, "from": min((r["ts"] for r in w), default=None), "to": max((r["ts"] for r in w), default=None),
            "records": len(w), "events": sum(c["events"] for c in cats.values()), "ai_replies_read": len(reads), "sources": sources, "categories": cats, "readouts": readouts,
            "oversight": {"asked": len(asks), "approved": sum(r["outcome"] == "approved" for r in asks),
                          "refused": sum(r["outcome"] in ("denied", "timeout", "held", "blocked") for r in asks),
                          "no_admin": sum(r["outcome"] == "asked_in_tool" for r in asks)},
            "controls_seen": controls}


# ---- third-party time stamps (RFC 3161): what makes the chain hold even against someone in full control of this computer ----
TSA = os.environ.get("NOUS_TSA", "http://timestamp.digicert.com")
ANCHORS = os.path.join(HOME, "anchors")


def _openssl():
    for c in ("/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", shutil.which("openssl")):   # macOS's LibreSSL has no `ts`
        if c and os.path.exists(c):
            r = subprocess.run([c, "ts", "-help"], capture_output=True)
            if b"Usage: ts" in r.stdout + r.stderr: return c
    raise RuntimeError("需要支持 ts 的 OpenSSL（brew install openssl）")


def _cafile():
    for c in (os.environ.get("NOUS_TSA_CAFILE"), "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem", "/etc/ssl/cert.pem"):
        if c and os.path.exists(c): return c
    raise RuntimeError("找不到根证书文件（设置 NOUS_TSA_CAFILE）")


def stamp(tsa=None):
    """Send the ledger head's hash (nothing else) to a time-stamping authority and keep its signed reply.
    Afterwards nobody, including whoever controls this computer, can change records up to the head without it showing."""
    h = anchor()
    if h["seq"] < 0: return None
    tsa = tsa or TSA; ossl = _openssl(); os.makedirs(ANCHORS, mode=0o700, exist_ok=True)
    base = os.path.join(ANCHORS, f"{h['seq']:08d}")
    q = subprocess.run([ossl, "ts", "-query", "-digest", h["hash"], "-sha256", "-cert"], capture_output=True, check=True).stdout
    req = urllib.request.Request(tsa, data=q, headers={"Content-Type": "application/timestamp-query"})
    with urllib.request.urlopen(req, timeout=30) as r: tsr = r.read()
    open(base + ".tsr", "wb").write(tsr)
    txt = subprocess.run([ossl, "ts", "-reply", "-in", base + ".tsr", "-text"], capture_output=True, text=True).stdout
    if "Granted" not in txt: raise RuntimeError("时间戳机构没有签发：" + (re.search(r"Status:.*", txt) or [""])[0])
    meta = {"seq": h["seq"], "hash": h["hash"], "tsa": tsa, "time": (re.search(r"Time stamp: (.*)", txt) or [None, None])[1], "stamped_at": time.time()}
    json.dump(meta, open(base + ".json", "w"), ensure_ascii=False, indent=1)
    return meta


def stamps():
    return [json.load(open(f)) for f in sorted(glob.glob(os.path.join(ANCHORS, "*.json")))]


def check_stamp(meta, recs=None, tsr=None):
    """The TSA's signature is valid for that hash, and that hash is still the record at that position in this ledger.
    tsr: the signed reply's bytes (from an exported bundle); default: this computer's anchors folder."""
    recs = records() if recs is None else recs
    seq, hx = meta.get("seq"), str(meta.get("hash", ""))
    if not (isinstance(seq, int) and re.fullmatch(r"[0-9a-f]{64}", hx)): return {"signature_ok": False, "in_chain": False, "ok": False}
    in_chain = 0 <= seq < len(recs) and recs[seq].get("hash") == hx
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tsr") as tf:
            if tsr is not None: tf.write(tsr); tf.flush()
            path = tf.name if tsr is not None else os.path.join(ANCHORS, f"{seq:08d}.tsr")
            r = subprocess.run([_openssl(), "ts", "-verify", "-digest", hx, "-in", path, "-CAfile", _cafile()], capture_output=True, text=True)
        sig = "Verification: OK" in r.stdout + r.stderr
    except Exception:
        sig = False
    return {"signature_ok": sig, "in_chain": in_chain, "ok": sig and in_chain}


def covering_stamp(seq, recs=None):
    """The earliest time stamp taken at or after record `seq`: it fixes that record."""
    for m in stamps():
        if m["seq"] >= seq: return {**m, **check_stamp(m, recs)}
    return None


# ---- export for a team report: everything a reviewer needs to check this computer's record, and nothing it said ----
def machine():
    """A random id for this computer's ledger (not the hostname), plus an editable label."""
    p = os.path.join(HOME, "machine.json")
    try: return json.load(open(p))
    except (OSError, ValueError):
        os.makedirs(HOME, mode=0o700, exist_ok=True)
        m = {"id": secrets.token_hex(8), "label": "", "created": time.time()}
        json.dump(m, open(p, "w"), ensure_ascii=False); return m


def set_label(label):
    m = machine(); m["label"] = str(label)[:60]; json.dump(m, open(os.path.join(HOME, "machine.json"), "w"), ensure_ascii=False); return m


def export(out):
    """-> a .tar.gz with manifest.json, ledger.jsonl and anchors/*. The HMAC key is never included."""
    import io, tarfile
    m = machine(); recs = records(); ok, n, why = verify(recs)
    man = {"format": "nous-ledger-bundle/1", "machine": m["id"], "label": m["label"], "exported_at": time.time(),
           "head": anchor(), "records": n, "chain_ok_at_export": ok}
    def add(tf, name, data):
        ti = tarfile.TarInfo(name); ti.size = len(data); ti.mtime = int(time.time()); ti.mode = 0o600; tf.addfile(ti, io.BytesIO(data))
    with tarfile.open(out, "w:gz") as tf:
        add(tf, "manifest.json", json.dumps(man, ensure_ascii=False, indent=1).encode())
        add(tf, "ledger.jsonl", open(PATH, "rb").read() if os.path.exists(PATH) else b"")
        for f in sorted(glob.glob(os.path.join(ANCHORS, "*"))):
            if f.endswith((".json", ".tsr")): add(tf, "anchors/" + os.path.basename(f), open(f, "rb").read())
    return man


def evidence(seq, text=None):
    """Claim pack for one event: the record, proof that it sits unaltered in the chain, and (optionally) proof that a
    given text is the one recorded. Contains no content unless the owner supplies it."""
    recs = records(); ok, n, why = verify(recs)
    if not 0 <= seq < len(recs): raise SystemExit(f"没有第 {seq} 条记录")
    r = recs[seq]
    out = {"record": r, "chain": {"intact": ok, "length": n, "problem": why or None, "head": anchor()},
           "how_to_check": "逐条重算 sha256(去掉 hash 字段、键排序的紧凑 JSON)，每条的 prev 必须等于上一条的 hash；"
                           "time_stamp 是第三方时间戳机构（RFC 3161）对某条记录哈希的签名：openssl ts -verify -digest <hash> -in <seq>.tsr -CAfile <根证书> 通过，"
                           "且该位置的记录哈希相同，就说明这条记录在那个时间之前就已存在、之后没有被改写。"}
    out["time_stamp"] = covering_stamp(seq, recs)
    if text is not None: out["text_matches_record"] = r.get("digest") is not None and hmac.compare_digest(digest(text), r["digest"])
    return out


def _t(ts): return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


try:                                       # the shared Nous UI tokens (design/tokens.json -> nous_tokens.py), next to this file
    if os.path.dirname(os.path.abspath(__file__)) not in sys.path: sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import nous_tokens as _tok
    _ROOT = _tok.ROOT_CSS
except ImportError:
    _ROOT = ""
STYLE = _ROOT + """:root{--cyb:rgba(51,207,106,.45);--ambb:rgba(224,154,60,.5);--redb:rgba(255,90,74,.55)}
*{box-sizing:border-box;margin:0}
html,body{background:var(--bg);color:var(--ink);font:14px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.top{display:flex;align-items:center;flex-wrap:wrap;gap:8px 18px;min-height:46px;padding:8px 18px;border-bottom:1px solid var(--line)}
.mark{font:14px/1 var(--wide);letter-spacing:.4em;color:var(--hi)}.sp{flex:1}
.lbl{font:10px/1.3 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}.lbl b{color:var(--mid);font-weight:400}
.tag{display:inline-block;font:9.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;padding:4px 6px;border:1px solid var(--line2);color:var(--mid);white-space:nowrap}
.tag.ok{color:var(--cy);border-color:var(--cyb)}.tag.warn{color:var(--amb);border-color:var(--ambb)}.tag.bad{color:var(--red);border-color:var(--redb)}
main{max-width:960px;margin:auto;padding:28px 16px 40px}
h1{font:600 22px/1.25 var(--sans);color:var(--hi);margin:6px 0 4px}
h2{display:flex;align-items:baseline;gap:12px;font:500 14px/1.4 var(--sans);color:var(--hi);margin:30px 0 10px;padding-top:12px;border-top:1px solid var(--line)}
h2::before{content:"";flex:none;width:6px;height:6px;background:var(--cy);transform:translateY(-2px)}
h2 small{font:10px var(--mono);letter-spacing:.14em;color:var(--dim)}
h3{font:9.5px/1 var(--wide);letter-spacing:.18em;text-transform:uppercase;color:var(--cy);margin:0 0 10px}
.mut{color:var(--mid);font-size:13px}b{font-weight:600}
table{border-collapse:collapse;width:100%;font:12px/1.4 var(--mono);color:var(--ink)}
th{text-align:left;font:10px/1.3 var(--mono);color:var(--dim);letter-spacing:.12em;text-transform:uppercase;padding:0 10px 7px 0;border-bottom:1px solid var(--line)}
td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top}
.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.box{position:relative;border:1px solid var(--line);padding:12px 14px}
.frame::before,.frame::after{content:"";position:absolute;width:9px;height:9px;border:0 solid var(--mid)}
.frame::before{left:-1px;top:-1px;border-left-width:1px;border-top-width:1px}.frame::after{right:-1px;bottom:-1px;border-right-width:1px;border-bottom-width:1px}
.ok{color:var(--cy)}.bad{color:var(--red)}.warn{color:var(--amb)}
code{font:12px var(--mono);color:var(--hi);word-break:break-all}
.lbl code.hx{font-size:10px;letter-spacing:.04em;text-transform:none;color:var(--mid)}
ul{padding-left:0;list-style:none}li{padding:2px 0}li.off{color:var(--dim)}
.cells{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));border-top:1px solid var(--line);border-left:1px solid var(--line)}
.cells>div{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:12px 14px;min-width:0}
.kpi{font:500 20px/1.2 var(--mono);color:var(--hi);margin-top:6px;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;font-size:13px}.kv>dt{font:10px/2 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}.kv>dd{margin:0;min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:4px}.chips code{font-size:11px;color:var(--ink);border:1px solid var(--line);padding:1px 5px}
.sub{margin-top:14px}.notes{margin-top:8px;font-size:13px;color:var(--mid)}.notes li{border-left:2px solid var(--line2);padding:3px 9px;margin-top:4px}
.scroll{overflow-x:auto}.scroll table{min-width:640px}
@media (max-width:600px){.kv{grid-template-columns:1fr}.top{padding:8px 16px}}
@media print{:root{--bg:#fff;--line:#bbb;--line2:#999;--rule:#ddd;--dim:#666;--mid:#444;--ink:#1a1a1a;--hi:#000;--cy:#1d7a3e;--amb:#8a5a10;--red:#b0291c;--cyb:#1d7a3e;--ambb:#8a5a10;--redb:#b0291c}
html,body{background:#fff}main{padding:16px 0;max-width:none}.top{padding:8px 0}h2,.box,tr{break-inside:avoid}.scroll{overflow:visible}.scroll table{min-width:0}}"""
METHOD = """<h2>方法与局限 <small>METHOD &amp; LIMITS</small></h2><ul class=mut>
<li>所有数字都是计数或比例，没有加权总分。在有足够的赔付数据之前，我们不给不透明的评分。</li>
<li>“已化解”指拦下、打码、收进保险箱、等待批准期间暂不执行，或管理员拒绝 / 超时自动拒绝。标注提醒、批准后执行和放行记录不算化解。</li>
<li>敌意读数已通过预注册验证；编造、附和、危险建议读数仍是实验性的，只作风险信号，不作定责依据。</li>
<li>只覆盖已接入 Nous 的工具（见“覆盖范围”）。没有接入 Nous 的电脑和工具、接入前发生的事件都不在统计内。</li>
</ul>"""


def page(title, subtitle, body, status=""):
    """status: optional HTML for the right side of the top bar (chain check, head hash)."""
    from html import escape as e
    return (f"<!doctype html><html lang=zh-CN><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
            f"<title>{e(title)}</title><style>{STYLE}</style></head><body><header class=top><span class=mark>NOUS</span>"
            f"<span class=lbl>Evidence ledger</span><span class=sp></span>{status}</header>"
            f"<main><div class=lbl>AI risk attestation</div><h1>{e(title)}</h1><div class=mut>{subtitle}</div>{body}{METHOD}</main></body></html>")


def profile_html(p):
    """The parts of a report that come from a profile: events and what Nous did, readouts, oversight, controls."""
    from html import escape as e
    rows = "".join(
        f"<tr><td>{e(CATEGORY.get(k, k))}</td><td class=n>{c['events']}</td><td class=n>{c['mitigated']}</td>"
        f"<td class=n>{round(100 * c['mitigated'] / c['events']) if c['events'] else 0}%</td>"
        f"<td>{e('；'.join(f'{OUTCOME.get(o, o)} {v}' for o, v in sorted(c['outcomes'].items(), key=lambda x: -x[1])))}</td></tr>"
        for k, c in sorted(p["categories"].items(), key=lambda x: -x[1]["events"])) or "<tr><td colspan=5>这段时间没有记录到风险事件。</td></tr>"
    ro = "".join(f"<tr><td>{e(CATEGORY[k])}</td><td class=n>{v['flagged']}</td><td class=n>{'—' if v['per_1000_replies'] is None else v['per_1000_replies']}</td>"
                 f"<td>{'已验证' if v['validated'] else '实验性'}</td></tr>" for k, v in p["readouts"].items())
    ctl = "".join(f"<li class={'on' if v else 'off'}>{'●' if v else '○'} {e(k)}{'' if v else '（这段时间没有触发记录）'}</li>" for k, v in p["controls_seen"].items())
    o = p["oversight"]
    return f"""<h2>覆盖范围 <small>COVERAGE</small></h2><div>受保护的 AI 工具：{e('、'.join(p['sources']) or '—')}；记录到需要处置的操作 {p['events']} 次，逐条读心读了 {p['ai_replies_read']} 条 AI 回复。</div>
<h2>AI 的操作：风险事件与处置 <small>EVENTS</small></h2><div class=scroll><table><tr><th>类别</th><th class=n>次数</th><th class=n>Nous 已化解</th><th class=n>化解率</th><th>处置结果</th></tr>{rows}</table></div>
<h2>AI 的回复：读数（模型内部状态） <small>READOUTS</small></h2><div class=mut>回复已经到达使用者，Nous 做的是标注和提醒，不是拦截。</div><table><tr><th>读数</th><th class=n>标注</th><th class=n>每千条回复</th><th>状态</th></tr>{ro}</table>
<h2>人工监督 <small>OVERSIGHT</small></h2><div>不可撤销的操作共申请 {o['asked']} 次：批准 {o['approved']}，拒绝或超时 {o['refused']}，连不上管理员时交由使用者确认 {o['no_admin']}。</div>
<h2>这段时间实际起作用的防护 <small>CONTROLS</small></h2><ul>{ctl}</ul>"""


def policy_html(days=30, recs=None, now=None):
    """Agent policy section: the declarative policy in force (nous_policy), its adaptive tightening, the policy decisions
    recorded in this ledger, and what an OpenShell export of it would contain. Never raises: a report must still render."""
    from html import escape as e
    head = "<h2>Agent 策略 <small>AGENT POLICY</small></h2>"
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import nous_policy
        pol = nous_policy.load(); st = nous_policy._state()
    except Exception as x:
        return head + f"<div class=box><span class=warn>无法读取策略引擎：</span><code>{e(repr(x)[:120])}</code></div>"
    if pol is None:
        return head + ("<div class=box><span class=mut>这台电脑没有配置 Agent 策略（AI 工具按 Nous 内置规则处理）。要配置，运行</span> "
                       "<code>python3 hooks/nous_policy.py init</code><span class=mut>，然后编辑</span> <code>"
                       + e(nous_policy.POLICY) + "</code>。</div>")
    if "_error" in pol:
        return head + f"<div class=box><span class=bad>策略文件无法解析，当前不生效：</span><code>{e(pol['_error'][:200])}</code></div>"
    now = now or time.time()
    flagged = sum(1 for v in (st.get("flagged") or {}).values() if now - v.get("ts", 0) < nous_policy.FLAG_TTL)
    mode, lock = pol.get("mode", "enforce"), bool(st.get("lockdown"))
    fs, net, ex, tl = (pol.get(k, {}) or {} for k in ("filesystem", "network", "exec", "tools"))

    def chips(xs): return "<span class=chips>" + "".join(f"<code>{e(str(x))}</code>" for x in xs) + "</span>" if xs else "<span class=mut>—</span>"
    def row(k, v): return f"<dt>{e(k)}</dt><dd>{v}</dd>"
    cells = (f"<div><div class=lbl>Mode</div><div class=kpi>{e(str(mode)).upper()}</div><div class=mut>{'按规则拦截或询问' if mode == 'enforce' else '只记录本来会怎么处理，不拦'}</div></div>"
             f"<div><div class=lbl>Lockdown</div><div class=kpi>{'<span class=bad>ON</span>' if lock else 'OFF'}</div><div class=mut>{'所有会话改用封锁配置' if lock else '未开启封锁'}</div></div>"
             f"<div><div class=lbl>Flagged sessions</div><div class=kpi>{'<span class=warn>%d</span>' % flagged if flagged else 0}</div><div class=mut>当前因风险收紧的会话（{nous_policy.FLAG_TTL // 60} 分钟内）</div></div>")
    rules = ("<dl class=kv>"
             + row("文件 · 可写", chips(fs.get("write_allow"))) + row("文件 · 禁止写", chips(fs.get("write_deny")))
             + row("文件 · 禁止读", chips(fs.get("read_deny"))) + row("范围外写入", f"<code>{e(str(fs.get('outside_write_allow', 'ask')))}</code>")
             + row("网络 · 默认", f"<code>{e(str(net.get('default', 'allow')))}</code> <span class=mut>允许 {len(net.get('allow') or [])} 个 · 禁止 {len(net.get('deny') or [])} 个</span>")
             + row("网络 · 允许", chips(net.get("allow"))) + row("网络 · 禁止", chips(net.get("deny")))
             + row("命令 · 禁止", chips(ex.get("deny"))) + row("命令 · 先问人", chips(ex.get("ask")))
             + row("工具 · 禁止", chips(tl.get("deny"))) + row("工具 · 先问人", chips(tl.get("ask"))) + "</dl>")
    ad, profs = pol.get("adaptive", {}) or {}, pol.get("profiles", {}) or {}
    adrows = "".join(
        f"<tr><td>{k}</td><td>{e(w)}</td><td>{e(str(ad[k]))}{'' if ad[k] in profs else ' <span class=warn>（未定义）</span>'}</td></tr>"
        for k, w in (("on_injection", "会话读到疑似提示注入的外部内容"), ("on_flag", "Nous 把会话里 AI 的回复标为有风险"), ("on_lockdown", "管理员开启封锁"))
        if ad.get(k)) or "<tr><td colspan=3 class=mut>没有配置动态收紧：风险出现时策略不变。</td></tr>"

    recs = records() if recs is None else recs
    dec = [r for r in recs if str(r.get("title", "")).startswith("策略") and r.get("ts", 0) >= now - days * 86400][::-1]
    word = lambda r: ("audit", "") if "只记录" in r["title"][:8] else ("deny", "bad") if r["outcome"] in MITIGATED else (r["outcome"], "warn")
    drows = "".join(
        f"<tr><td>{_t(r['ts'])}</td><td>{e(r['source'])}</td><td><span class='tag {word(r)[1]}'>{word(r)[0]}</span> {e(OUTCOME.get(r['outcome'], r['outcome']))}</td><td>{e(r['title'])}</td></tr>"
        for r in dec[:50]) or "<tr><td colspan=4 class=mut>这段时间策略没有拦下或记录任何操作。</td></tr>"
    more = f"<div class=mut>共 {len(dec)} 条，只列出最近 50 条。</div>" if len(dec) > 50 else ""

    try:
        import nous_openshell
        osp, notes = nous_openshell.translate(pol)
        rw = osp["filesystem_policy"]["read_write"]
        hosts = [x["host"] for x in osp.get("network_policies", {}).get("nous_allow", {}).get("endpoints", [])]
        osh = ("<dl class=kv>" + row("read_write", f"<span class=n>{len(rw)}</span> " + chips(rw))
               + row("include_workdir", "<code>true</code>") + row("allowed hosts", f"<span class=n>{len(hosts)}</span> " + chips(hosts)) + "</dl>"
               + ("<ul class=notes>" + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul>" if notes else ""))
    except Exception as x:
        osh = f"<span class=warn>无法生成导出：</span><code>{e(repr(x)[:120])}</code>"

    return f"""{head}<div class=mut>策略文件 <code>{e(nous_policy.POLICY)}</code>：AI 编程助手每次调用工具前都按它检查，Nous 在会话里看到风险时自动收紧，每次决定都写进本账本。</div>
<div class=cells style="margin-top:12px">{cells}</div>
<div class="box sub"><h3>Base rules</h3>{rules}</div>
<div class="box sub"><h3>Adaptive</h3><table><tr><th>触发</th><th>情形</th><th>改用配置</th></tr>{adrows}</table></div>
<div class=sub><h3>Recent decisions</h3><div class=scroll><table><tr><th>时间</th><th>来源</th><th>决定</th><th>内容</th></tr>{drows}</table></div>{more}</div>
<div class="box sub"><h3>OpenShell export</h3><div class=mut style="margin-bottom:8px">同一份策略导出到 NVIDIA OpenShell 沙箱（<code>nous_openshell.py export</code>）后的内容：</div>{osh}</div>"""


def report_html(days=30):
    """Self-contained page for an underwriter: period, chain check, time stamps, what happened, what Nous did, method and limits."""
    from html import escape as e
    recs = records(); ok, n, why = verify(recs); p = profile(days, recs); h = anchor(); st = stamps()
    if st:
        m = st[-1]; c = check_stamp(m, recs); fixed = sum(1 for r in recs if r["seq"] <= m["seq"])
        stamp_html = (f"<h2>第三方时间戳 <small>TIME STAMP</small></h2><div class=box><b class={'ok' if c['ok'] else 'bad'}>{'签名有效，且与账本一致' if c['ok'] else '时间戳无法验证'}</b>：前 {fixed} 条记录"
                      f"（到第 {m['seq']} 条）已由 {e(m['tsa'].split('//')[-1])} 于 {e(m['time'] or '—')} 签发时间戳。<br><span class=mut>时间戳机构只收到账本指纹，没有收到任何记录内容。"
                      f"在那之前的记录，任何人（包括这台电脑的管理员）改动都会被发现。共签发 {len(st)} 次。</span></div>")
    else:
        stamp_html = "<h2>第三方时间戳 <small>TIME STAMP</small></h2><div class=box><span class=bad>尚未签发。</span><span class=mut>没有第三方时间戳时，完整性只能证明到最近一次发出的账本指纹为止。</span></div>"
    body = f"""<h2>账本完整性 <small>CHAIN</small></h2><div class="box frame"><b class={'ok' if ok else 'bad'}>{'哈希链完整' if ok else '哈希链断裂：' + e(why)}</b>，共 {n} 条记录。<br>
账本指纹（第 {h['seq']} 条）：<code>{h['hash']}</code><br><span class=mut>这个指纹每天随汇总发到管理员的 Slack / 飞书。指纹对得上，说明那天以前的记录没有被改写过。
账本里不存任何对话或命令原文，只存本机密钥下的 HMAC 摘要；需要举证时，由使用方提供原文来核对。</span></div>
{stamp_html}{profile_html(p)}{policy_html(days, recs)}"""
    status = (f"<span class=lbl>Head <b>#{h['seq']}</b> <code class=hx>{h['hash'][:16]}…</code></span>"
              f"<span class='tag {'ok' if ok else 'bad'}'>{'Chain verified' if ok else 'Chain broken'}</span>")
    return page("AI 使用风险证明", f"由 Nous 读心生成 · {_t(time.time())} · 统计最近 {days} 天（{_t(p['from'])} 至 {_t(p['to'])}）", body, status)


if __name__ == "__main__":
    a = sys.argv[1:] or ["verify"]
    if a[0] == "verify": ok, n, why = verify(); print(("账本完整，" if ok else "账本有问题：" + why + "；") + f"共 {n} 条。"); sys.exit(0 if ok else 1)
    elif a[0] == "head": print(json.dumps(anchor()))
    elif a[0] == "stamp": print(json.dumps(stamp(a[1] if len(a) > 1 else None), ensure_ascii=False))
    elif a[0] == "export": print(json.dumps(export(a[1]), ensure_ascii=False, indent=1))
    elif a[0] == "label": print(json.dumps(set_label(" ".join(a[1:])), ensure_ascii=False))
    elif a[0] == "stamps": print(json.dumps([{**m, **check_stamp(m)} for m in stamps()], ensure_ascii=False, indent=1))
    elif a[0] == "profile": print(json.dumps(profile(int(a[1]) if len(a) > 1 else 30), ensure_ascii=False, indent=1))
    elif a[0] == "report":
        open(a[1], "w", encoding="utf-8").write(report_html(int(a[2]) if len(a) > 2 else 30)); print("已生成", a[1])
    elif a[0] == "evidence":
        print(json.dumps(evidence(int(a[1]), open(a[2], encoding="utf-8").read() if len(a) > 2 else None), ensure_ascii=False, indent=1))
    else: sys.exit(__doc__)
