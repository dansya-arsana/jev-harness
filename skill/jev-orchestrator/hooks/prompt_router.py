#!/usr/bin/env python3
"""UserPromptSubmit hook: suggest at most one skill and inject conditional instructions.

Reads the hook payload on stdin, asks Jev narrow questions, prints
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}
or nothing. Never blocks the prompt: every failure path exits 0 with no output.

Design (mirrors docs.typesafe.ai/cookbooks/skill_suggestion):
  Request 1  one Jev call: Choice over the whole skill roster (+ "none"), the
             "needs_skill" noul, and one noul per conditional instruction.
  Request 2  only when needs_skill passes and the top pick is not "none":
             re-read the top 3 with full description + SKILL.md opening,
             Choice among them (+ "none") and one "fits" noul per candidate.
Policy (thresholds) lives here in code; Jev only answers the questions.

Env:
  JEV_ROUTER=off              disable entirely
  JEV_ROUTER_CONDITIONS=path  alternative conditions file (tests)
  JEV_ROUTER_LOG=name         alternative log name (default "prompts")
"""
import glob
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts"))

HOME = os.path.expanduser("~")
JEV_DIR = os.path.join(HOME, ".claude", "jev")
ROSTER_CACHE = os.path.join(JEV_DIR, "skill_roster.json")
CONDITIONS_FILE = os.environ.get("JEV_ROUTER_CONDITIONS") or os.path.join(JEV_DIR, "conditions.json")
LOG_NAME = os.environ.get("JEV_ROUTER_LOG") or "prompts"

# ---- policy ---------------------------------------------------------------
MIN_PROMPT_CHARS = 15
PROMPT_CHARS = 4000            # user_request sent to Jev
LOG_PROMPT_CHARS = 300
DESC_CHARS = 220               # per-option description in request 1
DESC_CHARS_MIN = 60
CHUNK = 254                    # options per Choice, plus "none" = 255 cap
TOKEN_BUDGET = 24000           # state + longest question, under the 32k hard limit
REQ_TOKEN_BUDGET = 56000       # whole request, under the 64k hard limit
NEEDS_SKILL_MIN = 0.5
NEEDS_SKILL_FLOOR = 0.3         # below this, never suggest a skill
TOP_SKILL_OVERRIDE = 0.6        # request-1 probability that opens the gate when needs_skill is borderline
SHORTLIST = 3
BODY_CHARS = 1200
FINAL_PROB_MIN = 0.5
FITS_MIN = 0.3                 # cookbook: drop a shortlist whose best "fits" noul is below this
COND_THRESHOLD = 0.6
COND_FILE_CHARS = 2500
OUTPUT_MAX = 8000
DEADLINE_S = 8.0
JEV_TIMEOUT = 5.0
MIN_SECOND_CALL_S = 1.5        # skip request 2 if less time than this remains

TIER_RANK = {"project": 0, "user": 1, "plugin": 2, "desktop": 3}
PRUNE_DIRS = {
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "IndexedDB",
    "Local Storage", "Session Storage", "Partitions", "blob_storage", "Crashpad", "sentry",
    "vm_bundles", "claude-code-vm", "fcache", "WebStorage", "Shared Dictionary", "SharedStorage",
    "File System", "node_modules", ".git", "__pycache__", "outputs", "uploads",
}

FILLER_WORDS = set("""
ok okay k kk yes yeah yep yup no nope sure thanks thank you thx ty please pls go ahead do it
that this continue proceed sounds good great nice cool perfect awesome lgtm right got alright
and then again now keep going done fine agreed exactly correct yes! hi hello hey
""".split())

SKILL_LINE = ("<skill_relevance>Relevant to the current request: %s. Load it with the Skill tool if it "
              "fits; ignore this if it does not fit what the user actually asked for.</skill_relevance>")


def est_tokens(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return len(text) // 3 + 1  # conservative: ~3 chars/token


# ---- skip rules -------------------------------------------------------------
def skip_reason(prompt):
    if os.environ.get("JEV_ROUTER", "").strip().lower() in ("off", "0", "false", "no"):
        return "disabled"
    p = (prompt or "").strip()
    if not p:
        return "empty"
    if p.startswith("/"):
        return "slash_command"
    if len(p) < MIN_PROMPT_CHARS:
        return "short"
    words = re.findall(r"[a-z!']+", p.lower())
    if len(words) <= 8 and words and all(w.strip("!'") in FILLER_WORDS for w in words):
        return "conversational"
    return None


# ---- frontmatter ------------------------------------------------------------
def _unquote(value):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1].replace('\\"', '"')
    return v


def split_frontmatter(text):
    """Return (dict of top-level scalar keys, body). Tolerant of odd YAML."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    fm_lines, body = lines[1:end], "\n".join(lines[end + 1:])
    meta = {}
    i = 0
    key_re = re.compile(r"^([A-Za-z0-9_-]+)\s*:(.*)$")
    while i < len(fm_lines):
        line = fm_lines[i]
        m = key_re.match(line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        i += 1
        cont = []
        while i < len(fm_lines) and (fm_lines[i].startswith((" ", "\t")) or not fm_lines[i].strip()):
            cont.append(fm_lines[i])
            i += 1
        while cont and not cont[-1].strip():
            cont.pop()
        if value[:1] in (">", "|") and re.match(r"^[>|][+-]?\d*\s*(#.*)?$", value):
            stripped = [c.strip() for c in cont]
            if value[0] == "|":
                meta[key] = "\n".join(stripped).strip()
            else:
                paras, cur = [], []
                for s in stripped:
                    if s:
                        cur.append(s)
                    elif cur:
                        paras.append(" ".join(cur))
                        cur = []
                if cur:
                    paras.append(" ".join(cur))
                meta[key] = "\n".join(paras).strip()
        elif value == "":
            continue  # nested mapping or list: ignore
        else:
            if value[0] in "'\"" and cont:
                value = " ".join([value] + [c.strip() for c in cont if c.strip()])
            elif cont:
                value = " ".join([value] + [c.strip() for c in cont if c.strip()])
            meta[key] = _unquote(value)
    return meta, body


# ---- roster -----------------------------------------------------------------
def _plugin_name(root):
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json")) as f:
            name = json.load(f).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return None


def _walk_skill_files(base, deadline):
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        if time.time() > deadline:
            break
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        if "SKILL.md" in filenames:
            out.append(os.path.join(dirpath, "SKILL.md"))
    return out


def discover(cwd, deadline):
    """Return list of (path, tier, namespace_or_None)."""
    found = []
    for base in (".claude/skills", ".agents/skills"):
        if cwd:
            for p in sorted(glob.glob(os.path.join(cwd, base, "*", "SKILL.md"))):
                found.append((p, "project", None))
    for base in (os.path.join(HOME, ".claude", "skills"), os.path.join(HOME, ".agents", "skills")):
        for p in sorted(glob.glob(os.path.join(base, "*", "SKILL.md"))):
            found.append((p, "user", None))
    cache = os.path.join(HOME, ".claude", "plugins", "cache")
    # cache/<marketplace>/<plugin>/<version>/{skills/<skill>/SKILL.md | SKILL.md}
    versions = {}
    for vdir in glob.glob(os.path.join(cache, "*", "*", "*")):
        if not os.path.isdir(vdir):
            continue
        key = os.path.dirname(vdir)
        try:
            mt = os.path.getmtime(vdir)
        except OSError:
            continue
        in_use = os.path.exists(os.path.join(vdir, ".in_use"))
        if key not in versions or (in_use, mt) > versions[key][0]:
            versions[key] = ((in_use, mt), vdir)
    for key in sorted(versions):
        vdir = versions[key][1]
        plugin = _plugin_name(vdir) or os.path.basename(key)
        for p in sorted(glob.glob(os.path.join(vdir, "skills", "*", "SKILL.md"))):
            found.append((p, "plugin", plugin))
        root_skill = os.path.join(vdir, "SKILL.md")
        if os.path.isfile(root_skill):
            found.append((root_skill, "plugin", plugin))
    appsup = os.path.join(HOME, "Library", "Application Support")
    for base in sorted(glob.glob(os.path.join(appsup, "Claude*"))):
        for p in sorted(_walk_skill_files(base, deadline)):
            skill_dir = os.path.dirname(p)
            parent = os.path.dirname(skill_dir)
            ns = None
            if os.path.basename(parent) == "skills":
                ns = _plugin_name(os.path.dirname(parent))
            found.append((p, "desktop", ns))
    return found


def signature(found):
    h = hashlib.sha1()
    for path, tier, ns in found:
        try:
            mt = os.path.getmtime(path)
        except OSError:
            mt = 0
        h.update(("%s|%s|%s|%s\n" % (path, tier, ns, mt)).encode())
    return h.hexdigest()


def build_roster(found):
    best = {}
    for path, tier, ns in found:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                meta, _ = split_frontmatter(f.read(20000))
        except OSError:
            continue
        if str(meta.get("disable-model-invocation", "")).lower() == "true":
            continue
        base = (meta.get("name") or os.path.basename(os.path.dirname(path))).strip()
        if not base:
            continue
        name = base
        if ns and not base.startswith(ns + ":"):
            name = "%s:%s" % (ns, base)
        desc = re.sub(r"\s+", " ", str(meta.get("description") or "")).strip()
        rec = {"name": name, "description": desc, "path": path, "tier": tier}
        cur = best.get(name)
        if cur is None or TIER_RANK[tier] < TIER_RANK[cur["tier"]]:
            best[name] = rec
    return sorted(best.values(), key=lambda r: r["name"])


def load_roster(cwd, deadline):
    found = discover(cwd, deadline)
    sig = signature(found)
    cache = {}
    try:
        with open(ROSTER_CACHE) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}
    entry = cache.get(sig)
    if isinstance(entry, dict) and isinstance(entry.get("roster"), list):
        return entry["roster"], True
    roster = build_roster(found)
    cache[sig] = {"built": time.time(), "roster": roster}
    if len(cache) > 8:  # keep the newest few signatures (one per cwd with project skills)
        for old in sorted(cache, key=lambda k: cache[k].get("built", 0))[:-8]:
            cache.pop(old, None)
    try:
        os.makedirs(JEV_DIR, exist_ok=True)
        tmp = ROSTER_CACHE + ".tmp%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, ROSTER_CACHE)
    except Exception:
        pass
    return roster, False


def skill_body(path, chars):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            _, body = split_frontmatter(f.read(40000))
    except OSError:
        return ""
    return re.sub(r"\n{3,}", "\n\n", body).strip()[:chars]


# ---- conditions -------------------------------------------------------------
def load_conditions():
    try:
        with open(CONDITIONS_FILE) as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict) and c.get("id") and c.get("when") and (c.get("inject") or c.get("file")):
            out.append(c)
    return out


def condition_text(c):
    if c.get("inject"):
        text = str(c["inject"])
    else:
        try:
            with open(os.path.expanduser(str(c["file"])), encoding="utf-8", errors="replace") as f:
                _, text = split_frontmatter(f.read(50000))
        except OSError:
            return ""
    text = text.strip()
    limit = int(c.get("max_chars") or COND_FILE_CHARS)
    if len(text) > limit:
        text = text[:limit].rstrip() + " [...]"
    return text


def cwd_match(c, cwd):
    prefix = c.get("cwd_prefix")
    if not prefix or not cwd:
        return False
    prefix = os.path.realpath(os.path.expanduser(prefix)).rstrip("/")
    here = os.path.realpath(cwd)
    return here == prefix or here.startswith(prefix + "/")


# ---- Jev questions ----------------------------------------------------------
CHOICE_INSTR = ("Which of these skills, if any, is the right one to load to help with `user_request`? "
                "Pick none if no listed skill is specifically for what the user is asking.")
NONE_DESC = ("No listed skill fits: the request is conversation, a general question, or a task "
             "none of the listed skills is specifically for.")
RERANK_INSTR = ("Which one of these skills is the right one to load for `user_request`? Read what each "
                "actually does, not just its name. Pick none if none of them does what the user asks.")


def chunk_questions(roster, desc_chars):
    qs = {}
    chunks = [roster[i:i + CHUNK] for i in range(0, len(roster), CHUNK)]
    for n, chunk in enumerate(chunks):
        criteria = {}
        for r in chunk:
            d = r["description"]
            criteria[r["name"]] = (d[:desc_chars].rstrip() + "...") if len(d) > desc_chars else (d or r["name"])
        criteria["none"] = NONE_DESC
        qs["skill" if len(chunks) == 1 else "skill_%d" % n] = {
            "type": "choice", "instructions": CHOICE_INSTR, "criteria": criteria}
    return qs, len(chunks)


def request1_questions(jevlib, roster, conds, state):
    desc_chars = DESC_CHARS
    while True:
        skill_qs, nchunks = chunk_questions(roster, desc_chars) if roster else ({}, 0)
        longest = max([est_tokens(q) for q in skill_qs.values()] or [0])
        total = sum(est_tokens(q) for q in skill_qs.values())
        if (est_tokens(state) + longest <= TOKEN_BUDGET and total <= REQ_TOKEN_BUDGET) \
                or desc_chars <= DESC_CHARS_MIN:
            break
        desc_chars = max(DESC_CHARS_MIN, int(desc_chars * 0.7))
    qs = dict(skill_qs)
    if roster:
        qs["needs_skill"] = jevlib.noul(
            "Would loading a specialized skill (a written playbook with domain-specific steps, "
            "conventions, or tool instructions) materially help the assistant carry out `user_request`, "
            "compared with just giving a normal answer or making an ordinary edit?",
            "Yes: a concrete task in a specialized domain (design, data/SQL, documents, media, deployment, "
            "a named tool or service) where a playbook would improve the result.",
            "No: chat, a quick factual or conceptual question, or a small generic change that general "
            "knowledge fully covers.")
    for c in conds:
        qs["cond::" + c["id"]] = jevlib.noul(
            c["when"],
            c.get("yes") or "Yes, the request involves this.",
            c.get("no") or "No, the request does not involve this.")
    return qs, nchunks, desc_chars


def main():
    t0 = time.time()
    deadline = t0 + DEADLINE_S
    rec = {"errors": [], "latency_ms": {}}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
    except Exception:
        return
    try:
        import jevlib
    except Exception:
        return
    log = jevlib.log
    try:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            return
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
        rec["prompt"] = jevlib.redact(prompt)[:LOG_PROMPT_CHARS]
        rec["session_id"] = payload.get("session_id")
        rec["cwd"] = cwd
        reason = skip_reason(prompt)
        if reason:
            rec["skipped"] = reason
            log(LOG_NAME, rec)
            return
        out = run(jevlib, prompt, cwd, deadline, rec)
        rec["total_ms"] = int((time.time() - t0) * 1000)
        log(LOG_NAME, rec)
        if out:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": out}}))
            sys.stdout.flush()
    except Exception as e:
        rec["errors"].append("fatal: %s" % str(e)[:300])
        try:
            log(LOG_NAME, rec)
        except Exception:
            pass


def run(jevlib, prompt, cwd, deadline, rec):
    t = time.time()
    try:
        roster, cached = load_roster(cwd, deadline - 5.5)
    except Exception as e:
        roster, cached = [], False
        rec["errors"].append("roster: %s" % str(e)[:200])
    rec["roster_size"] = len(roster)
    rec["roster_cached"] = cached
    rec["latency_ms"]["roster"] = int((time.time() - t) * 1000)

    conds = load_conditions()
    fired = {}  # id -> (prob, source)
    ask_conds = []
    for c in conds:
        if cwd_match(c, cwd):
            fired[c["id"]] = (1.0, "cwd")
        else:
            ask_conds.append(c)

    state = {"user_request": jevlib.redact(prompt)[:PROMPT_CHARS]}
    qs, nchunks, desc_chars = request1_questions(jevlib, roster, ask_conds, state)
    rec["chunks"] = nchunks
    if nchunks > 1:
        rec["chunked"] = "roster %d split into %d choice questions" % (len(roster), nchunks)
    if desc_chars != DESC_CHARS:
        rec["desc_chars"] = desc_chars

    suggestion = None
    answers = None
    if qs:
        t = time.time()
        timeout = max(1.0, min(JEV_TIMEOUT, deadline - t - 0.5))
        try:
            resp = jevlib.ask(state, qs, timeout=timeout, retries=1)
            answers = resp.get("answers") or {}
        except Exception as e:  # JevError or anything unexpected: deterministic fallback
            rec["errors"].append("request1: %s" % str(e)[:300])
        rec["latency_ms"]["request1"] = int((time.time() - t) * 1000)

    if answers is not None:
        for c in ask_conds:
            a = answers.get("cond::" + c["id"]) or {}
            p = a.get("noul")
            if isinstance(p, (int, float)):
                rec.setdefault("conditions", {})[c["id"]] = round(p, 3)
                if p >= float(c.get("threshold", COND_THRESHOLD)):
                    fired[c["id"]] = (float(p), "jev")
        suggestion = pick_skill(jevlib, roster, answers, state, deadline, rec)

    return render(conds, fired, suggestion, rec)


def pick_skill(jevlib, roster, answers, state, deadline, rec):
    if not roster:
        return None
    probs = {}
    for key, a in answers.items():
        if key == "skill" or key.startswith("skill_"):
            for name, p in (a.get("probabilities") or {}).items():
                if name != "none" and isinstance(p, (int, float)):
                    probs[name] = max(probs.get(name, 0.0), float(p))
                elif name == "none":
                    probs["none"] = max(probs.get("none", 0.0), float(p))
    if not probs:
        rec["errors"].append("request1: no skill answer")
        return None
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    rec["top_candidates"] = [[n, round(p, 3)] for n, p in ranked[:5]]
    needs = (answers.get("needs_skill") or {}).get("noul")
    rec["needs_skill"] = round(needs, 3) if isinstance(needs, (int, float)) else None
    top_skill_p = next((p for n, p in ranked if n != "none"), 0.0)
    # The needs_skill noul hovers near 0.5 on everyday requests; a clear request-1 winner also passes the gate.
    gate_open = isinstance(needs, (int, float)) and (needs >= NEEDS_SKILL_MIN or (needs >= NEEDS_SKILL_FLOOR and top_skill_p >= TOP_SKILL_OVERRIDE))
    if not gate_open:
        rec["final_skill"] = None
        rec["stop"] = "needs_skill"
        return None
    if ranked[0][0] == "none":
        rec["final_skill"] = None
        rec["stop"] = "top_is_none"
        return None
    by_name = {r["name"]: r for r in roster}
    short = [n for n, _ in ranked if n != "none" and n in by_name][:SHORTLIST]
    if time.time() + MIN_SECOND_CALL_S > deadline:
        rec["final_skill"] = None
        rec["stop"] = "no_time_for_request2"
        return None
    criteria = {}
    qs = {}
    for n in short:
        r = by_name[n]
        body = skill_body(r["path"], BODY_CHARS)
        criteria[n] = (r["description"] or n) + ("\n\n" + body if body else "")
        qs["fits::" + n] = jevlib.noul(
            {"skill": n, "description": (r["description"] or n)[:1500],
             "question": "Does the skill `skill` do the specific thing `user_request` asks for?"},
            "Yes, this skill is specifically for what the user asks.",
            "No, this skill is for something else or only loosely related.")
    criteria["none"] = "None of these skills does what the user asks; answer without loading a skill."
    qs["rerank"] = {"type": "choice", "instructions": RERANK_INSTR, "criteria": criteria}
    t = time.time()
    try:
        resp = jevlib.ask(state, qs, timeout=max(1.0, min(JEV_TIMEOUT, deadline - t - 0.3)), retries=1)
        a2 = resp.get("answers") or {}
    except Exception as e:
        rec["errors"].append("request2: %s" % str(e)[:300])
        rec["latency_ms"]["request2"] = int((time.time() - t) * 1000)
        rec["final_skill"] = None
        rec["stop"] = "request2_failed"
        return None
    rec["latency_ms"]["request2"] = int((time.time() - t) * 1000)
    rr = a2.get("rerank") or {}
    rprobs = rr.get("probabilities") or {}
    choice = rr.get("choice")
    fits = {n: (a2.get("fits::" + n) or {}).get("noul") for n in short}
    rec["rerank"] = {n: round(float(p), 3) for n, p in rprobs.items() if isinstance(p, (int, float))}
    rec["fits"] = {n: (round(v, 3) if isinstance(v, (int, float)) else None) for n, v in fits.items()}
    fit_vals = [v for v in fits.values() if isinstance(v, (int, float))]
    p = rprobs.get(choice) if choice else None
    if not choice or choice == "none" or choice not in by_name:
        rec["stop"] = "rerank_none"
    elif not isinstance(p, (int, float)) or p < FINAL_PROB_MIN:
        rec["stop"] = "rerank_low_prob"
    elif not fit_vals or max(fit_vals) < FITS_MIN:
        rec["stop"] = "fits_low"
    else:
        rec["final_skill"] = choice
        return choice
    rec["final_skill"] = None
    return None


def render(conds, fired, suggestion, rec):
    by_id = {c["id"]: c for c in conds}
    blocks = []
    for cid, (p, src) in sorted(fired.items(), key=lambda kv: -kv[1][0]):
        text = condition_text(by_id[cid])
        if not text:
            rec["errors"].append("condition %s: empty or unreadable" % cid)
            continue
        blocks.append((cid, p, src, '<conditional_instruction id="%s">\n%s\n</conditional_instruction>' % (cid, text)))
    skill_line = SKILL_LINE % suggestion if suggestion else ""
    budget = OUTPUT_MAX - len(skill_line) - 2
    kept, dropped, used = [], [], 0
    for b in blocks:  # highest probability first; drop the lowest when over budget
        if used + len(b[3]) + 2 <= budget:
            kept.append(b)
            used += len(b[3]) + 2
        else:
            dropped.append(b[0])
    rec["fired"] = {b[0]: {"p": round(b[1], 3), "source": b[2]} for b in kept}
    if dropped:
        rec["dropped_for_size"] = dropped
    parts = [b[3] for b in kept] + ([skill_line] if skill_line else [])
    return "\n\n".join(parts)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
