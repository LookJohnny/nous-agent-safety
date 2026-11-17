#!/usr/bin/env python3
"""Mask secret values in text read by an AI agent, keeping the structure it needs (variable names, sections, comments).
  KEY=value            -> KEY=●●●（已隐藏 N 位）
  "key": "value"       -> "key": "●●●（已隐藏 N 位）"   (only for secret-looking key names)
  sk-… / xox… / ghp_… / Bearer … / private key blocks anywhere -> ●●●
Used as a filter (stdin -> stdout) and as a module. Standard library only."""
import json, re, sys

MASK_VERSION = "m2-contextual-secrets"

SECRETISH = re.compile(r"(?i)(pass(word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|private|credential|auth|cookie|session|dsn|conn(ection)?_?str|database_url|webhook|密码|密钥|口令|令牌)")
PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|(AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{16,})\b"),
    re.compile(r"\bapify_api_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@"),            # user:password@ in URLs
]
ENV_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*[=:]\s*)(.+?)\s*$")
JSON_PAIR = re.compile(r'("((?:\\.|[^"\\])+)"\s*:\s*")((?:\\.|[^"\\])*)(")')
REDACTED = re.compile(r"(?:●●●（已隐藏 \d+ 位）|<REDACTED_SECRET>|<SECRET>|<PII_[0-9a-f]+>)\Z")
# A bot webhook's UUID path is itself a bearer credential. Keep its public
# endpoint visible, but never retain the bearer segment. Placeholder/documentation
# URLs without a real-shaped UUID are deliberately not guessed to be credentials.
WEBHOOK = re.compile(
    r"(?i)(https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/)"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-z_-])")
_VALUE = r'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|`[^`\r\n]*`|[A-Za-z0-9_!@#$%^&*+/?~-][A-Za-z0-9_!@#$%^&*+./?~=-]*)'''
# Inline assignments use narrower key matching than legacy ENV_LINE. In
# particular, identifiers such as session_id, tokenizer and max_tokens stay
# intact, as do JSON Schema property names (which have no scalar assignment).
INLINE_PAIR = re.compile(
    r"(?<![\w.-])(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>[ \t]*=[ \t]*)(?P<value>" + _VALUE + r")")
INLINE_SECRETISH = re.compile(
    r"(?i)(?:[A-Za-z0-9_.-]*[_-])?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?(?:key|token)|private[_-]?key|credential|authorization|cookie|webhook)\Z")
NATURAL_PAIR = re.compile(
    r"(?P<label>(?<![\w.-])(?:password|passwd|pwd|secret|token|api[ _-]?key|access[ _-]?token)\b|密码|密钥|口令|令牌)"
    r"(?P<sep>[ \t]*(?:[:：=]|(?:is|was)[ \t]+|就是|设置为|设为|是|为)[ \t]*)"
    r"(?P<value>" + _VALUE + r")", re.I)
_PROSE = frozenset("a an the not required optional missing invalid incorrect correct empty unknown unset null none true false string boolean used stored encrypted hashed protected strong weak secretless redacted set available unavailable expired revoked rejected accepted passed supplied provided generated configured defined named".split())


def hide(v):
    if REDACTED.fullmatch(v):
        return v
    return f"●●●（已隐藏 {len(v)} 位）"


def _assigned_value(value, *, natural=False):
    if value[:1] in {'"', "'", "`"} and value[-1:] == value[:1]:
        return value[0] + hide(value[1:-1]) + value[-1]
    # Do not swallow sentence punctuation after an unquoted value.
    core = value.rstrip(".")
    tail = value[len(core):]
    if not core or (natural and core.lower() in _PROSE):
        return value
    return hide(core) + tail


def _json_pair(match):
    # Decode only the key/value fragments; retain the original JSON spelling and
    # delimiters. Escaped keys must not bypass secret-name matching, and escaped
    # quotes inside a value must not leave a suffix outside the replacement.
    try:
        key = json.loads('"' + match.group(2) + '"')
        value = json.loads('"' + match.group(3) + '"')
    except ValueError:
        key, value = match.group(2), match.group(3)
    if not SECRETISH.search(key) or not value or REDACTED.fullmatch(value):
        return match.group(0)
    return match.group(1) + hide(value) + match.group(4)


def mask(text, env_file=True):
    """env_file=True: every KEY=value line is masked (the whole file is secrets). Otherwise only secret-looking keys."""
    out = []
    for line in text.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[:-1] if nl else line
        m = ENV_LINE.match(body)
        if m and not body.lstrip().startswith("#") and (env_file or SECRETISH.search(m.group(1))):
            v = m.group(2).strip().strip('"').strip("'")
            body = m.group(1) + (hide(v) if v else "")
        else:
            body = JSON_PAIR.sub(_json_pair, body)
            body = INLINE_PAIR.sub(lambda j: j.group("key") + j.group("sep") +
                (_assigned_value(j.group("value")) if INLINE_SECRETISH.fullmatch(j.group("key")) else j.group("value")), body)
        out.append(body + nl)
    s = "".join(out)
    for p in PATTERNS:
        s = p.sub(lambda m: hide(m.group(0)), s)
    s = WEBHOOK.sub(lambda m: m.group(1) + hide(m.group(2)), s)
    s = NATURAL_PAIR.sub(lambda m: m.group("label") + m.group("sep") +
                         _assigned_value(m.group("value"), natural=bool(re.search(r"\b(?:is|was)\b", m.group("sep"), re.I))), s)
    return s


if __name__ == "__main__":
    data = sys.stdin.read()
    sys.stdout.write(mask(data, env_file="--all" not in sys.argv))
    sys.stdout.write("\n[Nous 读心：密钥值已自动隐藏，只保留名称和结构。需要真实值时，请让程序从环境变量读取。]\n")
