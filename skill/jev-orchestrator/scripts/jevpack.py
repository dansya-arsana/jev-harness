#!/usr/bin/env python3
"""Context packs: gather code once, give every subagent a Jev-scored slice of it.

The paper's shared-retrieval idea plus its visibility ladder, in two steps:
  build  gather files into function/class-sized chunks (no model, instant)
  slice  Jev scores every chunk against ONE subtask; essential chunks are shown in full,
         background chunks as a one-line outline, the rest hidden, within a token budget

Commands:
  build  --task TEXT [--name N] [PATH ...] [--files-from FILE] [--grep REGEX ...] [--root DIR]
  slice  NAME --subtask TEXT [--budget TOKENS] [--json]
  info   NAME
Packs live in ./.jev/packs/<name>.json (inside the project, gitignored by convention).
"""
import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import jevlib  # noqa: E402

PACK_DIR = os.path.join(".jev", "packs")
MAX_FILE_BYTES = 400_000
MAX_CHUNKS = 2000  # slicing batches requests, so this only guards against huge inputs
CHUNK_MAX_LINES = 120
WINDOW_LINES = 80
REQUEST_TOKEN_BUDGET = 48_000   # stay well inside Jev's 64k per-request limit
PARALLEL_REQUESTS = 4
SKIP_DIRS = {".git", "node_modules", ".build", "build", "DerivedData", "dist", ".next", "__pycache__", ".venv",
             "venv", "Pods", ".jev", "graphify-out", "coverage", ".turbo"}
TEXT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".swift", ".go", ".rs", ".rb", ".java", ".kt",
            ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".cs", ".php", ".sh", ".zsh", ".bash", ".sql", ".md",
            ".json", ".yml", ".yaml", ".toml", ".css", ".scss", ".html", ".vue", ".svelte", ".txt", ".gradle",
            ".xml", ".plist", ".graphql", ".proto", ".tf", ".ini", ".cfg", ".env.example"}
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[\w-]+)?|\.netrc|credentials|id_(rsa|dsa|ecdsa|ed25519)|[^/]+\.(pem|p12|pfx|key))$", re.I)
SAFE_TEMPLATE = re.compile(r"\.env\.(example|sample|template)$", re.I)
# A line that starts a top-level-ish definition, across common languages.
DEF_START = re.compile(
    r"^(\s{0,4})(export\s+)?(default\s+)?(async\s+)?(public\s+|private\s+|internal\s+|fileprivate\s+|open\s+|static\s+|final\s+|@\w+\s+)*"
    r"(def|class|func|function|struct|enum|protocol|extension|interface|type|impl|fn|trait|module|actor|const\s+\w+\s*=\s*(\(|async|function)|let\s+\w+\s*=\s*(\(|async))\b")
HEADING = re.compile(r"^#{1,4}\s")
LEVELS = [
    "Irrelevant: unrelated to `subtask`",
    "Background: knowing this exists helps, but its code is not needed",
    "Relevant: needed to understand how to do `subtask`",
    "Essential: must be read or changed to do `subtask`, or directly answers it",
]
FULL_AT = 1.5      # score >= this -> show in full (budget permitting)
OUTLINE_AT = 0.7   # score >= this -> one-line outline


def est_tokens(text):
    return len(text) // 4 + 1


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "pack"


# ---------- build ----------

def iter_files(root, paths):
    seen = set()
    for p in paths:
        full = os.path.normpath(os.path.join(root, os.path.expanduser(p)))
        if any(ch in p for ch in "*?["):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root)
                    if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(fn, p):
                        seen.add(os.path.join(dirpath, fn))
        elif os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    seen.add(os.path.join(dirpath, fn))
        elif os.path.isfile(full):
            seen.add(full)
    for f in sorted(seen):
        rel = os.path.relpath(f, root)
        ext = os.path.splitext(f)[1].lower()
        if SECRET_FILE.search(rel) and not SAFE_TEMPLATE.search(rel):
            continue
        if ext not in TEXT_EXT and not SAFE_TEMPLATE.search(rel):
            continue
        try:
            if os.path.getsize(f) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield f, rel


def split_chunks(lines):
    """Split a file into (start, end) 1-based line ranges at definition or heading boundaries."""
    starts = [0]
    for i, line in enumerate(lines):
        if i and (DEF_START.match(line) or HEADING.match(line)):
            starts.append(i)
    ranges = []
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        # merge tiny leading pieces (imports, headers) into the next chunk
        if ranges and (ranges[-1][1] - ranges[-1][0]) < 6:
            ranges[-1] = (ranges[-1][0], b)
            continue
        ranges.append((a, b))
    out = []
    for a, b in ranges:
        while b - a > CHUNK_MAX_LINES:
            out.append((a, a + WINDOW_LINES))
            a += WINDOW_LINES
        if b > a:
            out.append((a, b))
    return [(a + 1, b) for a, b in out]


def outline_of(lines, start, end):
    for line in lines[start - 1:end]:
        t = line.strip()
        if t and not t.startswith(("import ", "from ", "#include", "using ", "//", "#!", "/*", "*")):
            return t[:140]
    return (lines[start - 1].strip() if start - 1 < len(lines) else "")[:140]


def grep_files(root, patterns):
    hits = set()
    for pat in patterns:
        try:
            r = subprocess.run(["grep", "-rIlE", "--exclude-dir=" + ",".join(SKIP_DIRS), pat, "."],
                               cwd=root, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in r.stdout.splitlines():
            hits.add(line[2:] if line.startswith("./") else line)
    return sorted(hits)


def cmd_build(args):
    root = os.path.abspath(args.root)
    paths = list(args.paths)
    if args.files_from:
        with open(args.files_from) as f:
            paths += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if args.grep:
        paths += grep_files(root, args.grep)
    if not paths:
        sys.exit(json.dumps({"error": "no paths: pass PATHs, --files-from, or --grep"}))
    chunks, files = [], []
    for full, rel in iter_files(root, paths):
        try:
            with open(full, errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        files.append(rel)
        for start, end in split_chunks(lines):
            text = "\n".join(lines[start - 1:end])
            if not text.strip():
                continue
            chunks.append({"id": "c%d" % len(chunks), "path": rel, "start": start, "end": end,
                           "outline": outline_of(lines, start, end), "text": jevlib.redact(text)})
    dropped = max(0, len(chunks) - MAX_CHUNKS)
    kept_paths = {c["path"] for c in chunks[:MAX_CHUNKS]}
    dropped_files = sorted({c["path"] for c in chunks[MAX_CHUNKS:]} - kept_paths)
    chunks = chunks[:MAX_CHUNKS]
    name = args.name or slug(args.task)
    pack = {"name": name, "task": args.task, "root": root, "created": time.time(), "files": files,
            "chunks": chunks, "dropped_chunks": dropped, "dropped_files": dropped_files}
    os.makedirs(PACK_DIR, exist_ok=True)
    path = os.path.join(PACK_DIR, name + ".json")
    with open(path, "w") as f:
        json.dump(pack, f)
    total = sum(est_tokens(c["text"]) for c in chunks)
    print(json.dumps({"pack": path, "name": name, "files": len(files), "chunks": len(chunks),
                      "dropped_chunks": dropped, "dropped_files": dropped_files, "approx_tokens": total,
                      "warning": ("over %d chunks: narrow the paths" % MAX_CHUNKS) if dropped else None}, indent=2))


# ---------- slice ----------

def load_pack(name):
    path = name if name.endswith(".json") else os.path.join(PACK_DIR, name + ".json")
    if not os.path.isfile(path):
        sys.exit(json.dumps({"error": "no pack at %s (run build first, from the same directory)" % path}))
    with open(path) as f:
        return json.load(f)


def batches(chunks, subtask):
    base = est_tokens(subtask) + 200
    cur, size = [], base
    for c in chunks:
        t = est_tokens(c["text"]) + 120
        if cur and size + t > REQUEST_TOKEN_BUDGET:
            yield cur
            cur, size = [], base
        cur.append(c)
        size += t
    if cur:
        yield cur


def score_batch(subtask, task, batch):
    state = {"subtask": jevlib.redact(subtask), "overall_task": jevlib.redact(task)}
    questions = {}
    for c in batch:
        questions[c["id"]] = {
            "type": "score",
            "instructions": {
                "chunk": {"path": c["path"], "lines": "%d-%d" % (c["start"], c["end"]), "code": c["text"][:12000]},
                "question": "How useful is `chunk` to an engineer doing `subtask` (part of `overall_task`)?",
            },
            "criteria": LEVELS,
        }
    r = jevlib.ask(state, questions, timeout=30, retries=2)
    return {cid: a["score"] for cid, a in r["answers"].items()}, r.get("usage", {}).get("input_tokens", 0)


def fence_lang(path):
    ext = os.path.splitext(path)[1].lstrip(".")
    return {"py": "python", "js": "javascript", "ts": "typescript", "tsx": "tsx", "swift": "swift", "rb": "ruby",
            "sh": "bash", "zsh": "bash", "md": "markdown", "yml": "yaml", "rs": "rust", "kt": "kotlin"}.get(ext, ext)


def cmd_slice(args):
    pack = load_pack(args.name)
    chunks = pack["chunks"]
    t0 = time.time()
    scores, jev_tokens, errors = {}, 0, []
    with concurrent.futures.ThreadPoolExecutor(PARALLEL_REQUESTS) as ex:
        futs = [ex.submit(score_batch, args.subtask, pack["task"], b) for b in batches(chunks, args.subtask)]
        for fut in futs:
            try:
                s, n = fut.result()
                scores.update(s)
                jev_tokens += n
            except (jevlib.JevError, KeyError, TypeError) as e:
                errors.append(str(e)[:200])
    if not scores:
        sys.exit(json.dumps({"error": "Jev unavailable: %s" % "; ".join(errors),
                             "fallback": "give the subagent the pack's file list (jevpack info) instead of a slice"}))
    ranked = sorted(chunks, key=lambda c: -scores.get(c["id"], 0))
    full, outline, used = [], [], 0
    for c in ranked:
        s = scores.get(c["id"], 0)
        t = est_tokens(c["text"])
        if s >= FULL_AT and used + t <= args.budget:
            full.append(c)
            used += t
        elif s >= OUTLINE_AT:
            outline.append(c)
    hidden = len(chunks) - len(full) - len(outline)
    latency = int((time.time() - t0) * 1000)
    jevlib.log("packs", {"kind": "slice", "pack": pack["name"], "subtask": jevlib.redact(args.subtask)[:300],
                         "chunks": len(chunks), "full": len(full), "outline": len(outline), "hidden": hidden,
                         "slice_tokens": used, "pack_tokens": sum(est_tokens(c["text"]) for c in chunks),
                         "jev_tokens": jev_tokens, "latency_ms": latency, "errors": errors})
    if args.json:
        print(json.dumps({"full": [c["id"] for c in full], "outline": [c["id"] for c in outline], "hidden": hidden,
                          "scores": {k: round(v, 2) for k, v in scores.items()}, "slice_tokens": used,
                          "jev_tokens": jev_tokens, "latency_ms": latency, "errors": errors}, indent=2))
        return
    order = {c["id"]: i for i, c in enumerate(chunks)}
    out = ["<context_pack name=\"%s\">" % pack["name"],
           "Gathered once for: %s" % pack["task"],
           "Scored for this subtask: %s" % args.subtask,
           "Root: %s. %d chunks in full, %d outlined, %d hidden (of %d from %d files)." % (
               pack["root"], len(full), len(outline), hidden, len(chunks), len(pack["files"])),
           "Start from this context. Open other files only if something you need is missing here.",
           "Each code line starts with its real line number in that file: cite those numbers.", ""]
    # Every line carries its real file line number, so citations never depend on counting from a header.
    for c in sorted(full, key=lambda c: order[c["id"]]):
        width = len(str(c["end"]))
        numbered = "\n".join("%*d  %s" % (width, c["start"] + i, line) for i, line in enumerate(c["text"].split("\n")))
        out += ["### %s:%d-%d" % (c["path"], c["start"], c["end"]), "```" + fence_lang(c["path"]), numbered, "```", ""]
    if outline:
        out.append("### Also relevant (outline only; open if needed)")
        for c in sorted(outline, key=lambda c: order[c["id"]]):
            out.append("- %s:%d-%d  %s" % (c["path"], c["start"], c["end"], c["outline"]))
        out.append("")
    out.append("</context_pack>")
    print("\n".join(out))
    if errors:
        print("\n(jevpack: %d scoring batch(es) failed; those chunks are hidden)" % len(errors), file=sys.stderr)


def cmd_info(args):
    pack = load_pack(args.name)
    by_file = {}
    for c in pack["chunks"]:
        by_file[c["path"]] = by_file.get(c["path"], 0) + 1
    print(json.dumps({"name": pack["name"], "task": pack["task"], "root": pack["root"], "files": len(pack["files"]),
                      "chunks": len(pack["chunks"]), "dropped_chunks": pack.get("dropped_chunks", 0),
                      "approx_tokens": sum(est_tokens(c["text"]) for c in pack["chunks"]),
                      "chunks_per_file": by_file}, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build"); p.add_argument("--task", required=True); p.add_argument("--name")
    p.add_argument("paths", nargs="*"); p.add_argument("--files-from"); p.add_argument("--grep", action="append")
    p.add_argument("--root", default="."); p.set_defaults(fn=cmd_build)
    p = sub.add_parser("slice"); p.add_argument("name"); p.add_argument("--subtask", required=True)
    p.add_argument("--budget", type=int, default=8000); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_slice)
    p = sub.add_parser("info"); p.add_argument("name"); p.set_defaults(fn=cmd_info)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
