"""Shared TypeSafe/Jev client for the jev-orchestrator skill and its hooks.

Stdlib only (Python 3.9+). Import with:
    sys.path.insert(0, "<repo>/skill/jev-orchestrator/scripts")
    import jevlib
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"
MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
LOG_DIR = os.path.expanduser("~/.claude/jev")
# Repo root (skill/jev-orchestrator/scripts/ -> ../../..), resolved through the ~/.claude symlink.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
KEY_FILES = [
    os.environ.get("TYPESAFE_ENV_FILE", ""),
    os.path.expanduser("~/.config/typesafe/.env"),
    os.path.join(REPO_ROOT, ".env"),
]


class JevError(Exception):
    """Jev could not answer (no key, HTTP error, timeout). Callers must fall back, never guess."""


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    for path in KEY_FILES:
        if path and os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    if line.startswith("TYPESAFE_API_KEY="):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if value:
                            return value
    # Windows: a key set with setx lives in HKCU\Environment and may predate the running shell.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value = winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]
            if value:
                return value
    except Exception:
        pass
    raise JevError("TYPESAFE_API_KEY not found (env var or " + ", ".join(p for p in KEY_FILES if p) + ")")


def ask(state, questions, timeout=30.0, retries=3):
    """One /v1/systemone request. Returns the parsed response dict. Raises JevError."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
    })
    last = "unknown error"
    for attempt in range(max(1, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last = "HTTP %d: %s" % (e.code, e.read().decode(errors="replace")[:300])
            if e.code == 429 or e.code >= 500:
                if attempt + 1 < retries:
                    time.sleep(min(float(e.headers.get("retry-after") or 2 ** attempt), 5))
                continue
            raise JevError(last)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = "connection failed: %s" % (getattr(e, "reason", None) or e)
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 5))
    raise JevError(last)


def noul(instructions, yes, no):
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(sk|pk|rk|ghp|gho|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|auth)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&]{4,}"), r"\1<redacted>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"), "<redacted private key>"),
]


def redact(text):
    """Strip obvious credentials before text is logged or sent to Jev."""
    if not isinstance(text, str):
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def log(name, record):
    """Append one JSON line to ~/.claude/jev/<name>.jsonl. Never raises."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        record = dict(record, ts=time.time())
        with open(os.path.join(LOG_DIR, name + ".jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
