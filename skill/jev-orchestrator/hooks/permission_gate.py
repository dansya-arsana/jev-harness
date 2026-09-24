#!/usr/bin/env python3
"""PreToolUse permission gate: plain rules first, Jev only for the unclear middle.

Layers (cheapest first):
  1. hard rules   -> deny (secrets + network, download piped to a shell, wiping top-level paths, ...)
                     or ask (force push, reset --hard, sudo, editing secrets files, ...)
  2. fast allow   -> print nothing (every segment is a known read-only or routine dev command)
  3. gray zone    -> one Jev request; reads the script file first when the command runs one

Contract: prints {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"|"ask", ...}}
or nothing. Never prints "allow" (that would auto-approve in non-bypass modes). Never exits 2.
Env: JEV_GATE=off keeps only the hard deny rules.
"""
import json
import os
import re
import shlex
import sys
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts")
HOME = os.path.expanduser("~")

# ---------- patterns ----------

SECRET_REF = re.compile(
    r"(\.ssh(/|\b)|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.pem\b|\.p12\b|\.pfx\b|\.aws/credentials|\.netrc\b"
    r"|\.env(?!\.example|\.sample|\.template)(\.[\w-]+)?\b|\.claude/settings(\.local)?\.json"
    r"|\.config/\S*(key|token|credential|secret)|\.kube/config|\.docker/config\.json|\.gnupg"
    r"|\bsecurity\s+(find-\w*password|dump-keychain))",
    re.I)
NET_SEND = re.compile(
    r"(\b(curl|wget|nc|ncat|netcat|socat|scp|sftp|ftp|telnet|httpie|xh|aria2c)\b|\bssh\s"
    r"|\brsync\b[^;&|\n]*\s[\w.@-]+::?\S*"  # rsync is only network when it names host:path or host::module
    r"|/dev/(tcp|udp)/|\burllib\b|\brequests\.(get|post|put|request)\b|\bhttp\.client\b|\bsocket\b|\bfetch\("
    r"|\bgit\s+push\b|\bgh\s+(gist|api)\b|\bsendmail\b)",
    re.I)
PIPE_TO_SHELL = re.compile(
    r"(\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?((ba|z|da|k)?sh|python3?|perl|ruby|node)\b"
    r"|\bbase64\s+(-d|--decode|-D)\b[^;&]*\|\s*(sudo\s+)?(ba|z)?sh\b"
    r"|\b(ba|z)?sh\s+(-c\s+)?[\"']?\$\((curl|wget)\b"
    r"|\bsource\s+<\((curl|wget)\b|\beval\s+[\"']?\$\((curl|wget)\b)",
    re.I)
TOP_PATH = (r"(/|~|\$HOME|\$\{HOME\}|%s|/\*|~/\*|\$HOME/\*|/Users|/System|/Library|/Applications"
            r"|/usr|/etc|/bin|/sbin|/var|/private|/opt)" % re.escape(HOME))
RM_TOP = re.compile(
    r"\brm\s+((-\w+|--[\w-]+)\s+)*-\w*[rRf]\w*\s+((-\w+|--[\w-]+)\s+)*[\"']?%s/?[\"']?(\s|$|;|&|\|)" % TOP_PATH)
HARD_DENY = [
    (RM_TOP, "recursive delete of a top-level or home path"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bnewfs\w*\b|\bdiskutil\s+(erase\w*|zeroDisk|secureErase|partitionDisk)\b", re.I),
     "formats or erases a disk"),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(r?disk|sd|nvme|hd)", re.I), "writes raw data to a disk device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(chmod|chown)\s+(-\w*R\w*\s+)(\S+\s+)?%s/?(\s|$)" % TOP_PATH), "recursive permission change on a top-level path"),
    (re.compile(r"\bcsrutil\s+(disable|authenticated-root\s+disable)\b|\bspctl\s+--master-disable\b"
                r"|\bspctl\s+--global-disable\b", re.I), "disables macOS security protections"),
]
ASK_RULES = [
    (re.compile(r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)"), "force push rewrites remote history"),
    (re.compile(r"\bgit\s+reset\s+[^;&|]*--hard\b"), "git reset --hard discards uncommitted work"),
    (re.compile(r"\bgit\s+clean\s+-\w*f"), "git clean -f deletes untracked files"),
    (re.compile(r"(^|[;&|]\s*|\s)sudo\s"), "runs as root with sudo"),
    (re.compile(r"(>>?|\btee\s+(-a\s+)?)\s*(/etc/|/usr/|/System/|/Library/|/private/|~/Library/LaunchAgents|%s/Library/LaunchAgents)"
                % re.escape(HOME)), "writes to a system path"),
    (re.compile(r"\b(crontab\s+(-\w+\s+)*\S|launchctl\s+(load|bootstrap|enable))"), "installs a scheduled or background job"),
    (re.compile(r"\brm\s+((-\w+|--[\w-]+)\s+)*-\w*[rR]\w*\s"), None),  # recursive delete: handled below (outside cwd -> ask)
]
SECRET_FILE = re.compile(
    r"(^|/)(\.env(\.[\w-]+)?|\.netrc|credentials|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|[^/]+\.(pem|p12|pfx|key))$"
    r"|/\.ssh/|/\.aws/|/\.gnupg/|/\.claude/settings(\.local)?\.json$", re.I)
SAFE_ENV_TEMPLATE = re.compile(r"\.env\.(example|sample|template)$", re.I)

READ_ONLY_CMDS = {
    "ls", "cat", "head", "tail", "less", "more", "wc", "stat", "file", "tree", "pwd", "echo", "printf", "which",
    "type", "date", "grep", "egrep", "fgrep", "rg", "ag", "sort", "uniq", "cut", "tr", "jq", "basename", "dirname",
    "realpath", "readlink", "du", "df", "true", "false", "diff", "cmp", "cd", "test", "[", "column", "nl", "fold",
    "md5", "shasum", "sha256sum", "uname", "whoami", "id", "hostname", "sw_vers", "xcode-select", "xcrun",
    "ps", "top", "uptime", "env", "printenv", "lsof", "sleep", "open",
}
GIT_READ = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "blame", "ls-files", "describe",
            "shortlog", "reflog", "stash", "fetch", "tag", "config", "worktree", "cat-file", "ls-tree", "grep"}
ROUTINE = [
    re.compile(r"^(python3?|pytest|py\.test)\s+(-m\s+(py_compile|pytest|unittest)\b|-\w|\S+_test\.py|tests?/)"),
    re.compile(r"^pytest\b"),
    re.compile(r"^(npm|pnpm|yarn|bun)\s+(test|run\s+(test|build|lint|typecheck|format)|install|ci|i|ls|outdated)\b"),
    re.compile(r"^npx\s+(tsc|eslint|prettier|vitest|jest)\b"),
    re.compile(r"^(swift\s+(build|test|package\s+(resolve|describe))|xcodegen\s+generate|xcodebuild\b)"),
    re.compile(r"^(go\s+(test|build|vet|fmt|mod\s+tidy)|cargo\s+(test|build|check|clippy|fmt)|make(\s+(test|build|lint))?$)"),
    re.compile(r"^(tsc|eslint|prettier|ruff|black|mypy|swiftlint|swiftformat)\b"),
    re.compile(r"^(mkdir|touch)\s"),
    re.compile(r"^git\s+(add|commit|checkout|switch|restore|merge|rebase|pull|stash|mv|rm\s+--cached)\b"),
]
SCRIPT_RUN = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:python3?|node|bash|sh|zsh|ruby|perl|deno\s+run|bun\s+run|bun|tsx|ts-node)\s+(?:-{1,2}[\w-]+\s+)*"
    r"([^\s;|&<>'\"]+\.(?:py|js|mjs|cjs|ts|sh|bash|zsh|rb|pl))"
    r"|(?:^|[;&|]\s*)(\./[^\s;|&<>]+)")
OBFUSCATION = re.compile(r"\$\(|`|\beval\b|<<|\bbase64\b|\bxxd\s+-r\b|\\x[0-9a-f]{2}|\bprintf\s+['\"]?\\", re.I)


# ---------- helpers ----------

def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))


def deny_reason(why):
    return "Blocked by jev permission gate: %s. If this is really intended, tell the user and let them run it themselves." % why


def log(record):
    try:
        sys.path.insert(0, SCRIPTS)
        import jevlib
        record["command"] = jevlib.redact(record.get("command", ""))[:500]
        jevlib.log("permissions", record)
    except Exception:
        pass


def normalize(cmd):
    """Join line continuations, treat newlines as command separators, collapse whitespace."""
    c = cmd.replace("\\\n", " ").replace("\r", "")
    if "<<" not in c:  # heredoc bodies keep their newlines (and always go to Jev via OBFUSCATION)
        c = c.replace("\n", " ; ")
    c = re.sub(r"[ \t]+", " ", c)
    return c


def segments(cmd):
    """Split on shell operators, respecting quotes. Returns None if the command can't be parsed."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def segment_is_safe(tokens):
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):  # FOO=bar cmd
        tokens = tokens[1:]
    if not tokens:
        return True
    name = os.path.basename(tokens[0])
    args = tokens[1:]
    joined = " ".join(tokens)
    if any(re.match(r"^\d*>>?", a) and a not in (">/dev/null", "2>/dev/null", "2>&1", "&>/dev/null") for a in args):
        return False
    if SECRET_REF.search(joined):
        return False
    if name in READ_ONLY_CMDS:
        if name in ("env", "printenv") and args:
            return False
        if name == "open" and any(a.startswith(("http:", "https:")) for a in args):
            return False
        return True
    if name == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls") for a in args)
    if name == "sed":
        return any(a == "-n" or a.startswith("-n") for a in args) and not any(a.startswith("-i") for a in args) \
            and not re.search(r"(^|;)\s*w\s|\bw\s+/", " ".join(args))
    if name == "awk":
        return not re.search(r"system|print\s*>|\|\s*\"|getline", " ".join(args))
    if name == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in ("stash",) and any(a in ("drop", "clear") for a in args):
            return False
        if sub == "config" and not any(a in ("--get", "--list", "-l", "--get-all") for a in args):
            return False
        if sub == "branch" and any(a in ("-D", "-d", "--delete", "-m", "-M") for a in args):
            return False
        if sub == "tag" and any(a in ("-d", "--delete", "-f") for a in args):
            return False
        if sub == "remote" and any(a in ("add", "remove", "rm", "set-url", "rename") for a in args):
            return False
        return sub in GIT_READ
    return any(p.search(joined) for p in ROUTINE)


def script_contents(cmd, cwd):
    m = SCRIPT_RUN.search(cmd)
    if not m:
        return None, None
    path = m.group(1) or m.group(2)
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        full = os.path.join(cwd or os.getcwd(), full)
    try:
        with open(full, errors="replace") as f:
            return path, f.read(8000)
    except OSError:
        return path, "<script file not found or unreadable>"


def outside_cwd_recursive_rm(cmd, cwd):
    """Recursive rm whose targets resolve outside the project directory."""
    segs = segments(cmd) or []
    for seg in segs:
        if not seg or os.path.basename(seg[0]) != "rm":
            continue
        flags = [a for a in seg[1:] if a.startswith("-")]
        if not any(re.search(r"[rR]", f) for f in flags if not f.startswith("--")) and "--recursive" not in flags:
            continue
        for t in (a for a in seg[1:] if not a.startswith("-")):
            p = os.path.realpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(t)))
            base = os.path.realpath(cwd or os.getcwd())
            if not (p == base or p.startswith(base + os.sep)) and not p.startswith(("/tmp/", "/private/tmp/", "/var/folders/")):
                return t
    return None


# ---------- layers ----------

def check_file_tool(tool_input):
    path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not path or SAFE_ENV_TEMPLATE.search(path):
        return None
    if SECRET_FILE.search(os.path.expanduser(path)):
        return ("ask", "jev gate: this edits a secrets or credentials file (%s); confirm it's intended" % os.path.basename(path))
    return None


def hard_rules(cmd):
    if SECRET_REF.search(cmd) and NET_SEND.search(cmd):
        return ("deny", "reads credentials or keys and sends data over the network in the same command")
    if PIPE_TO_SHELL.search(cmd):
        return ("deny", "runs code downloaded from the network directly in a shell or interpreter")
    code = without_printed_text(cmd)
    for pat, why in HARD_DENY:
        if pat.search(code):
            return ("deny", why)
    return None


def without_printed_text(cmd):
    """Drop echo/printf segments whose text is only printed, so `echo "rm -rf /"` isn't treated as code.
    Keeps everything when the text could be executed (piped into a shell, eval, $(...), backticks, xargs)."""
    if re.search(r"\|\s*(sudo\s+)?((ba|z|da|k)?sh|python3?|perl|ruby|node)\b|\beval\b|\$\(|`|\bxargs\b|\bsource\b|(^|\s)\.\s", cmd):
        return cmd
    segs = segments(cmd)
    printed = ("echo", "printf")
    # Rebuilding from tokens loses shell punctuation (e.g. a fork bomb's ":|:&"), so only do it when there is text to drop.
    if segs is None or not any(s and os.path.basename(s[0]) in printed for s in segs):
        return cmd
    kept = [s for s in segs if s and os.path.basename(s[0]) not in printed]
    return " ; ".join(" ".join(s) for s in kept)


def ask_rules(cmd, cwd):
    for pat, why in ASK_RULES:
        if why and pat.search(cmd):
            return why
    t = outside_cwd_recursive_rm(cmd, cwd)
    if t:
        return "recursive delete outside the project directory (%s)" % t
    return None


def jev_layer(cmd, cwd, mode):
    sys.path.insert(0, SCRIPTS)
    import jevlib
    spath, scontent = script_contents(cmd, cwd)
    state = {"command": jevlib.redact(cmd)[:4000], "cwd": cwd, "permission_mode": mode}
    if spath:
        state["script_path"] = spath
        state["script_contents"] = jevlib.redact(scontent or "")
    q = {
        "decision": {
            "type": "choice",
            "instructions": "A coding agent wants to run `command` in `cwd` without asking the user. If `script_contents` is present, "
                            "judge what that script does, since running the command runs it. Should it run without asking?",
            "criteria": {
                "allow": "Normal development work: building, testing, reading, or editing files inside the project, or harmless local actions",
                "ask": "Plausibly legitimate but has side effects the user should confirm: changes outside the project, installs, deletes, contacts remote services",
                "deny": "Leaks secrets or private data, runs untrusted downloaded code, destroys data, or weakens system security",
            },
        },
        "network_egress": jevlib.noul(
            "Does running `command` (including `script_contents`, if present) send data to, or download code from, a remote host?",
            "It makes network requests to a remote host", "It works only locally"),
        "touches_secrets": jevlib.noul(
            "Does running `command` (including `script_contents`, if present) read, print, or transmit credentials, private keys, tokens, or .env secrets?",
            "It accesses or exposes secrets", "It does not touch secrets"),
        "destructive": jevlib.noul(
            "Does running `command` delete or overwrite data in a way that cannot easily be undone, or affect files outside the project?",
            "It irreversibly deletes or overwrites data", "Nothing is irreversibly lost"),
        "outside_project": jevlib.noul(
            "Does running `command` create, modify, or delete files outside `cwd` (other than temporary directories like /tmp)?",
            "It changes files outside the project", "It only changes files inside the project or temporary directories"),
    }
    t0 = time.time()
    r = jevlib.ask(state, q, timeout=4.0, retries=1)
    a = r["answers"]
    latency = int((time.time() - t0) * 1000)
    ch = a["decision"]["choice"]
    probs = a["decision"]["probabilities"]
    p = {k: a[k]["noul"] for k in ("network_egress", "touches_secrets", "destructive", "outside_project")}
    decision, why = None, None
    if p["touches_secrets"] >= 0.7 and p["network_egress"] >= 0.5:
        decision, why = "deny", "Jev: sends secrets or credentials over the network (secrets %.2f, network %.2f)" % (p["touches_secrets"], p["network_egress"])
    elif ch == "deny" and probs.get("deny", 0) >= 0.8:
        decision, why = "deny", "Jev judged this unsafe (deny %.2f)" % probs["deny"]
    elif ch in ("deny", "ask"):
        decision, why = "ask", "Jev suggests confirming (%s %.2f)" % (ch, probs.get(ch, 0))
    elif p["destructive"] >= 0.7:
        decision, why = "ask", "Jev: may irreversibly delete or overwrite data (%.2f)" % p["destructive"]
    elif p["outside_project"] >= 0.7:
        decision, why = "ask", "Jev: changes files outside the project (%.2f)" % p["outside_project"]
    rec = {"layer": 3, "command": cmd, "cwd": cwd, "decision": decision or "pass", "choice": ch,
           "probabilities": probs, "signals": p, "script": spath, "latency_ms": latency}
    return decision, why, rec


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            return
    except Exception:
        return
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
    mode = data.get("permission_mode") or ""
    gate_off = os.environ.get("JEV_GATE", "").lower() in ("off", "0", "false", "no")

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        if gate_off:
            return
        res = check_file_tool(tool_input)
        if res:
            emit(res[0], res[1])
            log({"layer": 1, "tool": tool, "command": str(tool_input.get("file_path", "")), "decision": res[0], "why": res[1]})
        return
    if tool != "Bash":
        return
    raw = tool_input.get("command")
    if not isinstance(raw, str) or not raw.strip():
        return
    cmd = normalize(raw)

    # Layer 1: hard rules (always on, even with JEV_GATE=off)
    hard = hard_rules(cmd)
    if hard:
        emit("deny", deny_reason(hard[1]))
        log({"layer": 1, "command": cmd, "cwd": cwd, "decision": "deny", "why": hard[1]})
        return
    if gate_off:
        return
    why = ask_rules(cmd, cwd)
    if why:
        emit("ask", "jev gate: " + why)
        log({"layer": 1, "command": cmd, "cwd": cwd, "decision": "ask", "why": why})
        return

    # Layer 2: fast allow
    segs = segments(cmd)
    if segs is not None and not OBFUSCATION.search(cmd) and all(segment_is_safe(s) for s in segs):
        return

    # Layer 3: Jev
    try:
        decision, why, rec = jev_layer(cmd, cwd, mode)
    except Exception as e:  # Jev unavailable: fall back to plain rules, never guess its answer
        risky = NET_SEND.search(cmd) and (SCRIPT_RUN.search(cmd) or SECRET_REF.search(cmd) or OBFUSCATION.search(cmd))
        if risky:
            emit("ask", "jev gate: Jev unavailable and this command uses the network together with a script, secret, or obfuscation")
        log({"layer": 3, "command": cmd, "cwd": cwd, "decision": "ask" if risky else "pass", "error": str(e)[:200]})
        return
    if decision == "deny":
        emit("deny", deny_reason(why))
    elif decision == "ask":
        emit("ask", "jev gate: " + why)
    log(rec)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
