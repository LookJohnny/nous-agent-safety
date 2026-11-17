"""What is taken out of a record before it may leave this computer (Nous 数据计划, data-platform/DESIGN.md §3.3).

  scrub(text)        secrets (nous_mask) and personal information -> placeholders. A placeholder is stable for the same
                     value on this computer (<PHONE_3f2a>), so a model still learns the structure, but it cannot be
                     reversed without this computer's key. Returns the text and what was replaced, by type.
  excluded(path)     files never shared: secrets, keys and certificates, database dumps, logs, dependency and build
                     folders, binaries, files over 1 MB, and anything .gitignore ignores.
SCRUBBER_VERSION goes with every record, so data can be scanned again when the rules improve. Standard library only."""
import hashlib, hmac, os, re, subprocess

SCRUBBER_VERSION = "p1.2-m2"
MAX_BYTES = 1_000_000

PHONE = re.compile(r"(?<![\d+])(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")
IDCARD = re.compile(r"(?<![\dXx])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dXx])")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
SAFE_EMAIL = re.compile(r"@(example\.(com|org|net)|test|localhost|users\.noreply\.github\.com)$|^(noreply|no-reply|git)@", re.I)

EXCLUDE_NAMES = re.compile(r"""(?ix)(^|/)(
    \.env(\..*)? | .*\.(pem|key|p12|pfx|jks|keystore|crt|cer|der|kdbx|ovpn) | id_(rsa|dsa|ecdsa|ed25519)(\.pub)? | \.netrc | \.npmrc | \.pypirc |
    credentials(\.json)? | .*secrets?\.(json|ya?ml|toml|env) | .*\.(sql|dump|bak|sqlite3?|db|mdb|log)(\.gz)? | .*\.(xlsx?|numbers|csv|tsv|parquet)
)$""")
EXCLUDE_DIRS = re.compile(r"(^|/)(node_modules|vendor|\.git|dist|build|out|target|__pycache__|\.venv|venv|\.next|\.cache|Pods|DerivedData)(/|$)")


def _key():
    try:
        import nous_ledger
        return nous_ledger._key()
    except Exception:
        return b"nous-local"


def _tag(kind, value, key):
    return f"<{kind}_{hmac.new(key, (kind + ':' + value).encode(), hashlib.sha256).hexdigest()[:4]}>"


def _id_ok(s):
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    try: return "10X98765432"[sum(int(a) * b for a, b in zip(s[:17], w)) % 11] == s[17].upper()
    except (ValueError, IndexError): return False


def _luhn(digits):
    t, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt: d = d * 2 - 9 if d * 2 > 9 else d * 2
        t += d; alt = not alt
    return t % 10 == 0


def scrub(text, key=None):
    """-> (scrubbed text, {type: count})"""
    if not text: return text or "", {}
    key = key or _key(); counts = {}
    def bump(k): counts[k] = counts.get(k, 0) + 1
    try:
        from nous_mask import mask
        before = text.count("●●●"); text = mask(text, env_file=False); n = text.count("●●●") - before
        if n: counts["SECRET"] = n
    except Exception:
        pass
    def ids(m):
        if not _id_ok(m.group(0)): return m.group(0)
        bump("IDCARD"); return _tag("IDCARD", m.group(0), key)
    text = IDCARD.sub(ids, text)
    def cards(m):
        d = re.sub(r"\D", "", m.group(0))
        if not (16 <= len(d) <= 19 and _luhn(d)): return m.group(0)
        bump("CARD"); return _tag("CARD", d, key)
    text = CARD.sub(cards, text)
    def phones(m):
        bump("PHONE"); return _tag("PHONE", re.sub(r"\D", "", m.group(0))[-11:], key)
    text = PHONE.sub(phones, text)
    def emails(m):
        if SAFE_EMAIL.search(m.group(0)): return m.group(0)
        bump("EMAIL"); return _tag("EMAIL", m.group(0).lower(), key)
    text = EMAIL.sub(emails, text)
    return text, counts


def excluded_name(rel):
    """The part of excluded() that needs only the path (snapshots: the file is a git blob, not on disk)."""
    if EXCLUDE_DIRS.search(rel): return "依赖、构建或版本库目录"
    if EXCLUDE_NAMES.search(rel): return "密钥、证书、数据文件或日志"
    return None


def excluded(path, cwd=None, *, strict=False):
    """Return an exclusion reason, or None after the applicable checks pass.

    Legacy callers retain best-effort git-ignore checking. Training collection
    uses strict=True: unavailable Git, timeouts and any status other than Git's
    documented 0 (ignored) / 1 (not ignored) exclude the file conservatively.
    A non-repository directory normally returns 128 and is unverified too. Never
    put subprocess output or exception text in an exclusion reason.
    """
    p = str(path or "")
    if not p: return None
    rel = os.path.relpath(p, cwd) if cwd and os.path.isabs(p) else p
    if EXCLUDE_DIRS.search(rel) or EXCLUDE_DIRS.search(p): return "依赖、构建或版本库目录"
    if EXCLUDE_NAMES.search(p): return "密钥、证书、数据文件或日志"
    try:
        st = os.stat(p)
        if st.st_size > MAX_BYTES: return "超过 1 MB"
        with open(p, "rb") as f:
            if b"\0" in f.read(8192): return "二进制文件"
    except OSError:
        pass
    top = cwd if cwd and os.path.isdir(cwd) else os.path.dirname(p) or "."
    try:
        r = subprocess.run(["git", "-C", top, "check-ignore", "-q", p], capture_output=True, timeout=3)
        if strict and (type(getattr(r, "returncode", None)) is not int or r.returncode not in (0, 1)):
            return "privacy_exclusion_unverified"
        if r.returncode == 0: return ".gitignore 忽略的文件"
    except (OSError, subprocess.TimeoutExpired):
        if strict: return "privacy_exclusion_unverified"
    return None
