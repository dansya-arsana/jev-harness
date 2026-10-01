#!/usr/bin/env python3
"""jevctx: harness-owned, graph-first context preparation for a spawned JEV agent (vnext-plan 8, Phase 7).

    python jevctx.py prepare --task-id T --role R --task "<text>" [--files a,b] [--refresh] [--json] [--root DIR]

Pipeline (each tool keeps one job):
    graphify (structure) -> graphq (retrieval, run as a subprocess) -> jevpack (packaging) -> agent (reasoning)

1. Context policy for the role comes from config/agents.json `context_policy` (required|conditional|optional, scope).
2. Graph queries are built from the task text (1 broad + at most 2 targeted) and run through `graphq`
   (env JEV_GRAPHQ, else `graphq` on PATH) with a timeout. Identical (task-id, query) pairs are never re-run
   within one repo-state epoch: the existing pack is reused.
3. When the graph is unavailable, stale, cites nothing, or misses explicitly named files/symbols, a controlled
   fallback runs: targeted file search bounded by the role scope (narrow/targeted <= 20 files, expanded <= 60,
   broad <= 200). A `[JEV CONTEXT] ...` line is printed to stderr and logged.
4. The pack is written to .jev/context/<T>.json (8.5 contract) and a compact `context_block`
   (bounded by a char budget, default 6000) is returned for injection into the agent prompt.
5. Metrics go to .jev/logs/context.jsonl. Secrets (.env*, keys) are never read; text is redacted.

Stdout with --json: {pack_path, context_block, graph_status, fallback, reused, queries, reason}.
Without --json: the context_block only (a summary goes to stderr).
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import jevlib  # noqa: E402
import jevpack  # noqa: E402

CONFIG_PATH = os.environ.get("JEV_AGENTS_CONFIG") or os.path.join(jevlib.REPO_ROOT, "config", "agents.json")
DEFAULT_POLICY = {"graph": "conditional", "scope": "targeted"}
DEFAULT_BLOCK_CHARS = 6000
GRAPHQ_TIMEOUT_S = float(os.environ.get("JEV_GRAPHQ_TIMEOUT", "45"))
MAX_QUERIES = 3            # one broad + at most two targeted
MAX_TARGETED = 2
MAX_SYMBOLS = 30
MAX_DEPENDENCIES = 15
MAX_PACK_FILES = 200
SCOPE_FILE_CAP = {"narrow": 20, "targeted": 20, "expanded": 60, "changed_files_plus_dependents": 60,
                  "feature_surface": 60, "broad": 200}
SCOPE_PRIMARY_CAP = {"narrow": 6, "targeted": 8, "expanded": 12, "changed_files_plus_dependents": 12,
                     "feature_surface": 10, "broad": 25}
SCOPE_GRAPH_BUDGET = {"narrow": 2500, "targeted": 2500, "expanded": 4000, "changed_files_plus_dependents": 4000,
                      "feature_surface": 3000, "broad": 6000}
# Files the bounded Python walk may visit before giving up (rg is term-restricted and output-capped instead).
SCOPE_VISIT_CAP = {"narrow": 3000, "targeted": 3000, "expanded": 10000, "broad": 50000}
NARROW_SCOPES = {"narrow", "targeted"}
STOP_SYMBOLS = {"TODO", "FIXME", "README", "JSON", "HTTP", "HTTPS", "API", "URL", "UI", "CLI", "JEV"}

SRC_RE = re.compile(r"\bsrc=([^\s\]]+)\s+loc=L(\d+)")
AT_RE = re.compile(r"\bat=([^\s\]]+?):L(\d+)")
NODE_RE = re.compile(r"^NODE\s+(.+?)\s+\[src=")
EDGE_RE = re.compile(r"^EDGE\s+(.+?)\s+--(\w+)\s.*?-->\s+(.+?)(?:\s+at=\S+)?\s*$")
EXT_ALT = "|".join(sorted({e.lstrip(".") for e in jevpack.TEXT_EXT if e.count(".") == 1}, key=len, reverse=True))
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:%s))(?![\w])" % EXT_ALT)
DIR_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+)")
BACKTICK_RE = re.compile(r"`([^`\s]{2,80})`")
CALL_RE = re.compile(r"\b([A-Za-z_][\w]*)\s*\(")
IDENT_RES = [re.compile(r"\b([a-z][a-z0-9]*[A-Z]\w*)\b"),            # camelCase
             re.compile(r"\b([A-Z][a-z0-9]+[A-Z]\w*)\b"),            # PascalCase with 2+ humps
             re.compile(r"\b([a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9])\b"),  # snake_case
             re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")]          # SCREAMING_CASE


# ---------- small helpers ----------

def norm(path):
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_secret(rel):
    rel = norm(rel)
    return bool(jevpack.SECRET_FILE.search(rel)) and not jevpack.SAFE_TEMPLATE.search(rel)


def in_skip_dir(rel):
    return any(part in jevpack.SKIP_DIRS for part in norm(rel).split("/")[:-1])


def usable_file(root, rel):
    rel = norm(rel)
    if not rel or rel.startswith("../") or os.path.isabs(rel) or is_secret(rel) or in_skip_dir(rel):
        return False
    return os.path.isfile(os.path.join(root, rel))


def uniq(items):
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def safe_id(task_id):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-.")
    return s[:80] or "task"


def short_role(role):
    return role[4:] if role.startswith("jev-") else role


def full_role(role):
    return role if role.startswith("jev-") else "jev-" + role


def load_config(path=None):
    try:
        with open(path or CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def role_policy(config, role):
    pol = dict(DEFAULT_POLICY)
    pol.update((config.get("context_policy") or {}).get(full_role(role), {}))
    return pol


def block_budget(config, policy):
    for v in (policy.get("context_chars"), (config.get("context") or {}).get("budget_chars")):
        if isinstance(v, int) and v > 500:
            return v
    return DEFAULT_BLOCK_CHARS


# ---------- task term extraction ----------

def extract_terms(text):
    """(paths, symbols) explicitly referenced by the task text."""
    text = text or ""
    paths = uniq([norm(m.group(1)) for m in PATH_RE.finditer(text)] +
                 [norm(m.group(1)) for m in DIR_PATH_RE.finditer(text) if "://" not in m.group(0)])
    path_set = set(paths) | {os.path.splitext(os.path.basename(p))[0] for p in paths}
    cands = [m.group(1) for m in BACKTICK_RE.finditer(text)]
    cands += [m.group(1) for m in CALL_RE.finditer(text)]
    for rx in IDENT_RES:
        cands += [m.group(1) for m in rx.finditer(text)]
    symbols = []
    for c in cands:
        c = c.strip("().,:;")
        if (not c or c in path_set or "/" in c or PATH_RE.fullmatch(c) or c.upper() in STOP_SYMBOLS
                or not re.fullmatch(r"[A-Za-z_][\w.]*", c) or len(c) < 3):
            continue
        symbols.append(c)
    return paths, uniq(symbols)[:12]


def build_queries(task, paths, symbols, broad=True):
    """One broad query (the task itself) + at most two targeted ones (symbols, then path stems)."""
    targeted = []
    if symbols:
        targeted.append(" ".join(symbols[:4]))
    stems = uniq([os.path.splitext(os.path.basename(p))[0] for p in paths])
    if stems:
        targeted.append(" ".join(stems[:3]))
    head = re.sub(r"\s+", " ", jevlib.redact(task or "")).strip()[:200] if broad else ""
    return uniq(([head] if head else []) + targeted[:MAX_TARGETED])[:MAX_QUERIES]


# ---------- repo state ----------

def git_head(root):
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def repo_state(root, files):
    mt = {}
    for rel in files[:MAX_PACK_FILES]:
        try:
            mt[rel] = round(os.path.getmtime(os.path.join(root, rel)), 3)
        except OSError:
            mt[rel] = None
    return {"head": git_head(root), "mtimes": mt}


def stale_reason(root, pack):
    st = pack.get("repo_state") or {}
    if st.get("head") and git_head(root) != st["head"]:
        return "repository HEAD changed since the pack was built"
    for rel, m in (st.get("mtimes") or {}).items():
        try:
            cur = round(os.path.getmtime(os.path.join(root, rel)), 3)
        except OSError:
            cur = None
        if cur != m:
            return "file changed since the pack was built: %s" % rel
    return None


# ---------- plan / handoff context ----------

def read_text(path, limit=200_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def list_items(text, key):
    """Collect `- item` lines under a `key:` (yaml) or `## Key` (markdown) heading."""
    out, on = [], False
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^(#+\s*)?%s\s*:?\s*$" % key, s, re.I):
            on = True
            continue
        if on:
            m = re.match(r"^[-*]\s+(.*)$", s)
            if m:
                out.append(m.group(1).strip().strip('"\''))
            elif s:
                on = False
    return out


def prior_artifacts(root, task_id, policy):
    """Plan + handoffs for the task: files they name, constraints, invariants, refs."""
    sid = safe_id(task_id)
    refs, texts = [], []
    plan = os.path.join(root, ".jev", "plans", sid + ".md")
    if os.path.isfile(plan):
        refs.append(".jev/plans/%s.md" % sid)
        texts.append(read_text(plan))
    hdir = os.path.join(root, ".jev", "handoffs")
    if policy.get("prior_handoff_first") or policy.get("plan_context_first"):
        try:
            for fn in sorted(os.listdir(hdir)):
                if fn.startswith(sid) and os.path.splitext(fn)[1] in (".yaml", ".yml", ".md", ".json"):
                    refs.append(".jev/handoffs/" + fn)
                    texts.append(read_text(os.path.join(hdir, fn)))
        except OSError:
            pass
    blob = "\n".join(texts)
    paths, symbols = extract_terms(blob)
    files = [p for p in paths if usable_file(root, p)]
    return {"refs": refs, "files": files, "symbols": symbols,
            "constraints": list_items(blob, "constraints"), "invariants": list_items(blob, "invariants")}


# ---------- graphq ----------

def graphq_argv():
    spec = os.environ.get("JEV_GRAPHQ", "").strip()
    if spec:
        if os.path.isfile(spec):
            return _script_argv(spec)
        parts = shlex.split(spec, posix=os.name != "nt")
        if parts and os.path.isfile(parts[0]):
            return _script_argv(parts[0]) + parts[1:]
        found = shutil.which(parts[0]) if parts else None
        return ([found] + parts[1:]) if found else None
    found = shutil.which("graphq") or _which_exact("graphq")
    return _script_argv(found) if found else None


def _which_exact(name):
    """PATH lookup for an extensionless shim (shutil.which ignores those on Windows)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d.strip('"'), name)
        if d and os.path.isfile(cand):
            return cand
    return None


def _script_argv(path):
    if path.endswith(".py"):
        return [sys.executable, path]
    if os.name == "nt" and not path.lower().endswith((".exe", ".cmd", ".bat", ".com")):
        first = read_text(path, 200).splitlines()[:1]
        if first and first[0].startswith("#!"):
            if "python" in first[0]:
                return [sys.executable, path]
            sh = shutil.which("bash") or shutil.which("sh")
            return [sh, path] if sh else None
    return [path]


def run_graphq(argv, root, query, intent, budget, timeout):
    """-> (ok, text, error). Never raises."""
    try:
        r = subprocess.run(argv + [query, "--intent", intent, "--budget", str(budget)], cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", "graphq timed out after %ss" % timeout
    except OSError as e:
        return False, "", "graphq could not start: %s" % type(e).__name__
    if r.returncode != 0:
        return False, r.stdout or "", "graphq exited %d" % r.returncode
    return True, r.stdout or "", None


def parse_graphq(text):
    """Citations from graphify/graphq output -> {files: {path: hits}, symbols: [...], edges: [(a, rel, b)]}."""
    files, symbols, edges = {}, [], []
    for line in text.splitlines():
        line = line.strip()
        for rx in (SRC_RE, AT_RE):
            for m in rx.finditer(line):
                p = norm(m.group(1))
                files[p] = files.get(p, 0) + 1
        m = NODE_RE.match(line)
        if m:
            name = m.group(1).strip()
            bare = name[:-2] if name.endswith("()") else name
            if not PATH_RE.fullmatch(bare) and "/" not in bare and re.fullmatch(r"[A-Za-z_][\w.$]*", bare):
                symbols.append(bare)
        m = EDGE_RE.match(line)
        if m:
            edges.append((m.group(1).strip(), m.group(2), m.group(3).strip()))
    return {"files": files, "symbols": uniq(symbols), "edges": edges}


# ---------- controlled fallback search ----------

def targeted_search(root, terms, names, scope, anchors=()):
    """Files containing any term or named like any name, bounded by the role scope. Never reads secrets."""
    cap = SCOPE_FILE_CAP.get(scope, 20)
    visit_cap = SCOPE_VISIT_CAP.get(scope, SCOPE_VISIT_CAP["expanded"] if cap >= 60 else 3000)
    roots = [a for a in uniq(anchors) if os.path.isdir(os.path.join(root, a))] if scope in NARROW_SCOPES else []
    if not roots:
        roots = ["."]
    terms = [t for t in uniq(terms) if len(t) >= 3][:12]
    names = uniq([os.path.basename(n) for n in names if n])[:12]
    hits = []
    rg = None if os.environ.get("JEV_CTX_NO_RG") else shutil.which("rg")
    if rg:
        hits = _rg_search(rg, root, terms, names, roots, cap)
    else:
        hits = _walk_search(root, terms, names, roots, cap, visit_cap)
    return [h for h in uniq(hits) if usable_file(root, h)][:cap]


def _rg_globs():
    out = []
    for d in sorted(jevpack.SKIP_DIRS):
        out += ["-g", "!%s/" % d]
    for g in (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*", "id_ecdsa*",
              "id_ed25519*", ".netrc", "credentials"):
        out += ["-g", "!" + g]
    return out


def _rg_search(rg, root, terms, names, roots, cap):
    found = []
    base = [rg, "--no-messages", "--max-filesize", "400K"] + _rg_globs()
    try:
        if names:
            r = subprocess.run(base + ["--files"] + sum((["--iglob", "**/" + n] for n in names), []) + roots,
                               cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20)
            found += sorted(norm(l.strip()) for l in r.stdout.splitlines() if l.strip())
        if terms and len(found) < cap:
            r = subprocess.run(base + ["-l", "-F"] + sum((["-e", t] for t in terms), []) + ["--"] + roots,
                               cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20)
            found += sorted(norm(l.strip()) for l in r.stdout.splitlines() if l.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return [f[2:] if f.startswith("./") else f for f in found][:cap * 3]


def _walk_search(root, terms, names, roots, cap, visit_cap):
    found, visited = [], 0
    lnames = {n.lower() for n in names}
    for start in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, start)):
            dirnames[:] = sorted(d for d in dirnames if d not in jevpack.SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                visited += 1
                if visited > visit_cap or len(found) >= cap:
                    return found
                rel = norm(os.path.relpath(os.path.join(dirpath, fn), root))
                if is_secret(rel):
                    continue
                if fn.lower() in lnames:
                    found.append(rel)
                    continue
                if not terms or os.path.splitext(fn)[1].lower() not in jevpack.TEXT_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > jevpack.MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                text = read_text(full, jevpack.MAX_FILE_BYTES)
                if any(t in text for t in terms):
                    found.append(rel)
    return found


# ---------- pack ----------

def pack_path(root, task_id):
    return os.path.join(root, ".jev", "context", safe_id(task_id) + ".json")


def load_pack(root, task_id):
    try:
        with open(pack_path(root, task_id), encoding="utf-8") as f:
            pack = json.load(f)
        return pack if isinstance(pack, dict) else None
    except (OSError, ValueError):
        return None


def new_pack(task_id, task):
    return {"jev_context_pack": 1, "task_id": task_id, "task": jevlib.redact(task),
            "retrieval": {"source": "graphify", "selector": "graphq", "graph_status": "skipped",
                          "fallback": "none", "reason": None, "epoch": 0, "queries": []},
            "primary_files": [], "related_files": [], "symbols": [], "dependencies": [],
            "constraints": [], "invariants": [], "context_budget": {"bounded": True},
            "plan_refs": [], "repo_state": {}, "history": [],
            "metrics": {"graph_queries": 0, "graph_fallbacks": 0, "context_pack_reuse": 0,
                        "context_pack_refreshes": 0}}


def file_texts(root, files, limit=60):
    for rel in files[:limit]:
        if usable_file(root, rel):
            yield rel, read_text(os.path.join(root, rel), jevpack.MAX_FILE_BYTES)


def missing_symbols(root, pack, symbols):
    have = set(pack.get("symbols") or [])
    left = [s for s in symbols if s not in have]
    if not left:
        return []
    for _, text in file_texts(root, (pack.get("primary_files") or []) + (pack.get("related_files") or [])):
        left = [s for s in left if s not in text]
        if not left:
            break
    return left


def module_of(rel):
    parts = norm(rel).split("/")
    return "/".join(parts[:-1]) or "."


def conditional_triggers(root, pack, files, paths, symbols, refresh):
    """8.3 triggers -> list of (code, reason) for a role that may reuse the existing pack."""
    if pack is None:
        return [("no_prior_pack", "no context pack for this task yet")]
    out = []
    if refresh:
        out.append(("refresh", "refresh requested"))
    st = stale_reason(root, pack)
    if st:
        out.append(("stale", st))
    known = set(pack.get("primary_files") or []) | set(pack.get("related_files") or [])
    mods = {module_of(f) for f in known}
    for f in uniq(files + [p for p in paths if usable_file(root, p)]):
        if f not in known:
            if module_of(f) not in mods:
                out.append(("unplanned_module", "implementation touches unplanned module: %s" % module_of(f)))
            else:
                out.append(("scope_mismatch", "file outside the selected scope: %s" % f))
            break
    miss = missing_symbols(root, pack, symbols)
    if miss:
        out.append(("missing_symbol", "referenced symbol not present in selected context: %s" % ", ".join(miss[:3])))
    if not pack.get("primary_files"):
        out.append(("insufficient", "provided context is insufficient (no primary files)"))
    return out


def rank_files(cited, paths, symbols, root, scope):
    """Split cited files into primary (named/matching task terms, then most cited) and related."""
    pcap = SCOPE_PRIMARY_CAP.get(scope, 8)
    ok = {f: n for f, n in cited.items() if usable_file(root, f)}
    named = [p for p in paths if p in ok] + [f for f in ok if any(f.endswith("/" + p) for p in paths)]
    stems = {os.path.splitext(os.path.basename(p))[0].lower() for p in paths}
    lsyms = [s.lower() for s in symbols]
    def score(f):
        b = os.path.basename(f).lower()
        return (-(os.path.splitext(b)[0] in stems or any(s in b for s in lsyms)), -ok[f], f)
    ordered = uniq(named + sorted(ok, key=score))
    return ordered[:pcap], ordered[pcap:]


def dependencies(edges, primary, symbols):
    keys = {os.path.basename(f) for f in primary} | set(symbols)
    out = []
    for a, rel, b in edges:
        a_, b_ = a.rstrip("()"), b.rstrip("()")
        if keys and not ({a, b, a_, b_} & keys):
            continue
        out.append("%s %s %s" % (a, rel, b))
    return uniq(out)[:MAX_DEPENDENCIES]


def fallback_log_line(task_id, role, status, fallback, reason):
    return ("[JEV CONTEXT] task_id=%s role=%s graph_status=%s fallback=%s reason=%s"
            % (task_id, full_role(role), status, fallback, reason or "-"))


# ---------- context block (packaging via jevpack) ----------

def render_block(root, pack, role, budget):
    """Compact, char-bounded prompt block. Chunking/redaction/secret skipping come from jevpack."""
    r = pack["retrieval"]
    head = ['<jev_context task_id="%s" role="%s" graph_status="%s" fallback="%s">'
            % (pack["task_id"], full_role(role), r.get("graph_status"), r.get("fallback")),
            "Task: %s" % pack.get("task", "")[:400],
            "Primary files: %s" % (", ".join(pack["primary_files"]) or "(none)")]
    if pack["related_files"]:
        head.append("Related files: %s" % ", ".join(pack["related_files"][:30]))
    if pack["symbols"]:
        head.append("Symbols: %s" % ", ".join(pack["symbols"][:20]))
    for key in ("dependencies", "constraints", "invariants"):
        if pack.get(key):
            head.append("%s:" % key.capitalize())
            head += ["- %s" % jevlib.redact(x)[:200] for x in pack[key][:10]]
    if pack.get("plan_refs"):
        head.append("Read first: %s" % ", ".join(pack["plan_refs"]))
    head.append("Start from this context. Use targeted Read/grep only for gaps; do not scan the whole repository.")
    tail = "</jev_context>"
    out = "\n".join(head)
    if len(out) + len(tail) + 1 > budget:
        return out[:max(0, budget - len(tail) - 5)] + "\n...\n" + tail
    syms = [s for s in pack["symbols"] if len(s) >= 3][:20]
    _, chunks = jevpack.gather_chunks(root, pack["primary_files"])
    hot = [c for c in chunks if any(s in c["text"] for s in syms)]
    cold = [c for c in chunks if c not in hot]
    parts = []
    room = budget - len(out) - len(tail) - 2
    for c in hot:
        width = len(str(c["end"]))
        body = "\n".join("%*d  %s" % (width, c["start"] + i, l) for i, l in enumerate(c["text"].split("\n")))
        piece = "### %s:%d-%d\n```%s\n%s\n```" % (c["path"], c["start"], c["end"], jevpack.fence_lang(c["path"]), body)
        if len(piece) + 1 <= room:
            parts.append(piece)
            room -= len(piece) + 1
    outline = []
    for c in cold + [c for c in hot if not any(p.startswith("### %s:%d-" % (c["path"], c["start"])) for p in parts)]:
        line = "- %s:%d-%d  %s" % (c["path"], c["start"], c["end"], c["outline"])
        if len(line) + 1 + (len("Outline:") + 1 if not outline else 0) > room:
            break
        if not outline:
            outline.append("Outline:")
            room -= len("Outline:") + 1
        outline.append(line)
        room -= len(line) + 1
    block = "\n".join([out] + parts + outline + [tail])
    return block[:budget]


# ---------- main entry ----------

def prepare(task_id, role, task, files=None, refresh=False, root=".", config=None, timeout=None):
    """Prepare (or reuse) the task's context pack for `role`. Returns the CLI result dict. Never raises on graph errors."""
    root = os.path.abspath(root)
    config = load_config() if config is None else config
    policy = role_policy(config, role)
    scope = policy.get("scope", "targeted")
    mode = policy.get("graph", "conditional")
    files = [norm(f) for f in (files or []) if f and not is_secret(f)]
    timeout = GRAPHQ_TIMEOUT_S if timeout is None else timeout
    paths, symbols = extract_terms(task)
    prior = load_pack(root, task_id)
    pack = prior or new_pack(task_id, task)
    r = pack["retrieval"]
    arts = prior_artifacts(root, task_id, policy)
    if arts["refs"]:
        pack["plan_refs"] = uniq(pack.get("plan_refs", []) + arts["refs"])
        pack["primary_files"] = uniq(pack["primary_files"] + arts["files"])[:MAX_PACK_FILES]
        pack["constraints"] = uniq(pack["constraints"] + arts["constraints"])
        pack["invariants"] = uniq(pack["invariants"] + arts["invariants"])
    # Explicitly named, existing files always belong to the primary scope.
    named = uniq(files + [p for p in paths if usable_file(root, p)])
    named = [f for f in named if usable_file(root, f)]

    # ---- decide whether to retrieve ----
    triggers = conditional_triggers(root, prior, files, paths, symbols, refresh)
    codes = {c for c, _ in triggers}
    if prior is not None and ("stale" in codes or "refresh" in codes):
        r["epoch"] = int(r.get("epoch", 0)) + 1
    if mode == "required":
        want_graph, broad = True, True
    elif mode == "optional":
        want_graph, broad = bool(refresh and not files), True
    else:
        want_graph = bool(triggers)
        broad = bool(codes & {"no_prior_pack", "stale", "refresh", "insufficient"})
    if mode == "optional" and not want_graph:
        triggers = []

    queries_run, dedupe_hits, status, fallback, reason = 0, 0, None, "none", None
    fresh = True  # False when status is inherited from the reused pack (its fallback already ran)
    cited, gsyms, edges = {}, [], []
    if want_graph:
        qs = build_queries(task, paths, symbols, broad=broad) or build_queries(task, paths, symbols, broad=True)
        done = {(q.get("query"), q.get("epoch", 0)) for q in r.get("queries", [])}
        todo = []
        for q in qs:
            if (q, r.get("epoch", 0)) in done:
                dedupe_hits += 1
            else:
                todo.append(q)
        graph_json = os.path.join(root, "graphify-out", "graph.json")
        argv = graphq_argv()
        if todo and not os.path.isfile(graph_json):
            status, reason = "unavailable", "graphify-out/graph.json missing"
        elif todo and not argv:
            status, reason = "unavailable", "graphq not found (set JEV_GRAPHQ or put graphq on PATH)"
        elif todo:
            intent = "%s for %s" % (policy.get("purpose") or scope, full_role(role))
            errors, missing_cited = [], 0
            for q in todo:
                ok, text, err = run_graphq(argv, root, q, intent, SCOPE_GRAPH_BUDGET.get(scope, 3000), timeout)
                queries_run += 1
                parsed = parse_graphq(text) if ok else {"files": {}, "symbols": [], "edges": []}
                for f, n in parsed["files"].items():
                    if is_secret(f):
                        continue
                    if not os.path.isfile(os.path.join(root, f)):
                        missing_cited += 1
                        continue
                    cited[f] = cited.get(f, 0) + n
                gsyms += parsed["symbols"]
                edges += parsed["edges"]
                r.setdefault("queries", []).append({"query": q, "epoch": r.get("epoch", 0), "role": full_role(role),
                                                    "ok": ok, "files_cited": len(parsed["files"]), "ts": time.time()})
                if err:
                    errors.append(err)
                    if "timed out" in err or "could not start" in err:
                        break  # do not burn the timeout again on the next query
            pack["metrics"]["graph_queries"] = pack["metrics"].get("graph_queries", 0) + queries_run
            if errors and not cited:
                status, reason = "unavailable", errors[0]
            elif missing_cited and not cited:
                status, reason = "stale", "graph cites files that no longer exist (rebuild graphify-out)"
            elif not cited:
                status, reason = "insufficient", "graph result cites no files"
            else:
                missed = [f for f in named if f not in cited]
                gstale = _graph_older_than(graph_json, root, named)
                if missing_cited:
                    status, reason = "stale", "graph cites %d missing file(s)" % missing_cited
                elif gstale:
                    status, reason = "stale", "graph older than named file %s" % gstale
                elif missed:
                    status, reason = "insufficient", "graph result misses named file %s" % missed[0]
                else:
                    status = "ok"
        else:
            status, fresh = (r.get("graph_status") if prior else "skipped"), False
    else:
        status, fresh = (r.get("graph_status") if prior and prior["retrieval"].get("queries") else "skipped"), False

    # merge graph results
    if cited:
        prim, rel = rank_files(cited, paths, symbols, root, scope)
        pack["primary_files"] = uniq(named + prim + pack["primary_files"])
        pack["related_files"] = [f for f in uniq(pack["related_files"] + rel) if f not in pack["primary_files"]]
        task_syms = [s for s in gsyms if s in symbols]
        pack["symbols"] = uniq(task_syms + pack["symbols"] + gsyms)[:MAX_SYMBOLS]
        pack["dependencies"] = uniq(pack["dependencies"] + dependencies(edges, pack["primary_files"], pack["symbols"]))[:MAX_DEPENDENCIES]

    # ---- controlled fallback ----
    need_fb = fresh and status in ("unavailable", "stale", "insufficient")
    if not need_fb and want_graph and not queries_run and (codes & {"missing_symbol", "unplanned_module", "scope_mismatch"}):
        # The graph queries for this task were already run (deduped); expand by targeted search instead.
        need_fb, status = True, "insufficient"
        reason = next(t for c, t in triggers if c in ("missing_symbol", "unplanned_module", "scope_mismatch"))
    if need_fb:
        miss = missing_symbols(root, pack, symbols) or symbols
        if not miss and not paths and not named:
            words = sorted(set(re.findall(r"[A-Za-z_]\w{4,}", task or "")), key=lambda w: (-len(w), w))
            miss = words[:4]
        anchors = [module_of(f) for f in pack["primary_files"] + named]
        hits = targeted_search(root, miss, paths + named, scope, anchors)
        cap = SCOPE_FILE_CAP.get(scope, 20)
        name_hits = [h for h in hits if any(h == p or h.endswith("/" + os.path.basename(p)) for p in paths)]
        pack["primary_files"] = uniq(pack["primary_files"] + named + name_hits)
        if not pack["primary_files"]:  # nothing graph-selected or named: best search hits become the scope
            pack["primary_files"] = hits[:SCOPE_PRIMARY_CAP.get(scope, 8)]
        pack["related_files"] = [f for f in uniq(pack["related_files"] + hits) if f not in pack["primary_files"]]
        found_syms = [s for s in symbols if any(s in t for _, t in file_texts(root, hits, cap))]
        pack["symbols"] = uniq(pack["symbols"] + found_syms)[:MAX_SYMBOLS]
        fallback = "targeted_file_search"
        pack["metrics"]["graph_fallbacks"] = pack["metrics"].get("graph_fallbacks", 0) + 1
        pack["fallback_files"] = len(hits)
    elif named:
        pack["primary_files"] = uniq(named + pack["primary_files"])
        pack["related_files"] = [f for f in pack["related_files"] if f not in pack["primary_files"]]

    # bound the pack by the widest scope that has written to it
    cap = max(SCOPE_FILE_CAP.get(scope, 20), pack.get("context_budget", {}).get("max_files", 0))
    pack["primary_files"] = pack["primary_files"][:cap]
    pack["related_files"] = pack["related_files"][:max(0, cap - len(pack["primary_files"]))]

    retrieved = bool(queries_run or fallback != "none")
    reused = prior is not None and (not retrieved or dedupe_hits > 0)
    refreshed = prior is not None and retrieved
    if reason is None and triggers and want_graph:
        reason = triggers[0][1]
    r.update({"graph_status": status or "skipped", "fallback": fallback, "reason": reason})
    pack["metrics"]["context_pack_reuse"] = pack["metrics"].get("context_pack_reuse", 0) + int(reused)
    pack["metrics"]["context_pack_refreshes"] = pack["metrics"].get("context_pack_refreshes", 0) + int(refreshed)
    if retrieved or prior is None:
        pack["repo_state"] = repo_state(root, pack["primary_files"] + pack["related_files"])
    budget = block_budget(config, policy)
    pack["context_budget"] = {"bounded": True, "chars": budget, "scope": scope, "max_files": cap}
    pack.setdefault("history", []).append({"role": full_role(role), "ts": time.time(), "queries": queries_run,
                                           "dedupe_hits": dedupe_hits, "fallback": fallback, "reused": reused,
                                           "triggers": [c for c, _ in triggers]})
    pack["history"] = pack["history"][-30:]
    block = render_block(root, pack, role, budget)
    pack["context_block"] = block

    path = pack_path(root, task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.dumps(pack, indent=2)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)

    log_line = fallback_log_line(task_id, role, r["graph_status"], fallback, reason) if fallback != "none" else None
    if log_line:
        print(log_line, file=sys.stderr)
    _log_metrics(root, {"task_id": task_id, "role": full_role(role), "graph_status": r["graph_status"],
                        "fallback": fallback, "reason": reason, "graph_queries": queries_run,
                        "graph_fallbacks": int(fallback != "none"), "context_pack_reuse": int(reused),
                        "context_pack_refreshes": int(refreshed), "dedupe_hits": dedupe_hits,
                        "pack_chars": len(data), "block_chars": len(block), "log_line": log_line})
    return {"pack_path": path, "context_block": block, "graph_status": r["graph_status"], "fallback": fallback,
            "reused": reused, "queries": queries_run, "reason": reason}


def _graph_older_than(graph_json, root, named):
    try:
        g = os.path.getmtime(graph_json)
    except OSError:
        return None
    for f in named:
        try:
            if os.path.getmtime(os.path.join(root, f)) > g + 1:
                return f
        except OSError:
            continue
    return None


def _log_metrics(root, record):
    try:
        d = os.path.join(root, ".jev", "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "context.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(record, ts=time.time())) + "\n")
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="build or reuse the task context pack for a role")
    p.add_argument("--task-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--files", default="", help="comma-separated paths the role will touch")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", default=".")
    p.add_argument("--timeout", type=float, default=None, help="graphq timeout seconds (default 45)")
    a = ap.parse_args(argv)
    res = prepare(a.task_id, a.role, a.task, files=[f.strip() for f in a.files.split(",") if f.strip()],
                  refresh=a.refresh, root=a.root, timeout=a.timeout)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(res["context_block"])
        print("jevctx: pack=%s graph_status=%s fallback=%s reused=%s queries=%d"
              % (res["pack_path"], res["graph_status"], res["fallback"], res["reused"], res["queries"]),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
