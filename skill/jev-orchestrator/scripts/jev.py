#!/usr/bin/env python3
"""Jev decision helper for delegating work to subagents.

Commands:
  route  TASK [--context TEXT] [--task-id T] [--files a,b] -> fast path or planned route, model / effort per step
  dedupe SUBGOAL [--ledger PATH]  -> is SUBGOAL already done or in flight? registers it if new
  done   ID [--ledger PATH]       -> mark a subgoal finished
  list   [--ledger PATH]          -> show the subgoal ledger
  stuck  --state TEXT|@FILE [--tier T] -> is the agent stuck? (then classify the failure and run `escalate`)
  report [--days N] [--json]      -> summarize gate / prompt-router / routing logs for tuning
  report lint FILE                -> check a terse STATUS/... agent report (also: report-lint FILE)
  outcomes [--days N] [--json]    -> how routed subagent dispatches went (from their transcripts + labels)
  label  ID|last ok|too_low|too_high [--note T] -> record whether the tier that ran was right

vNext (config/agents.json is the single source of truth for model / effort / write / fallbacks / escalation):
  preflight [--json] [--all] [--agents-dir DIR]          -> validate the jev-* agent files; exit 1 on failure
  escalate --task T --from ROLE --category CAT [--evidence E ...] -> stuck ladder v2 (failure-aware)
  plan save --task T [--from-file F|--stdin] / plan show --task T  -> the orchestrator persists the architect's plan
  handoff validate --kind plan|completion|failure|review|qa FILE / handoff template --kind K
  context prepare --task-id T --role R --task TEXT [...] -> graph-first context pack via jevctx.py (fails soft)

Every command prints one JSON object on stdout. Stdlib only (Python 3.9+).
Each decision is appended to ~/.claude/jev/decisions.jsonl for threshold tuning.
If Jev is unavailable, commands print {"error": ..., "fallback": ...} and exit 1; they never guess Jev's answer.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import jevlib  # noqa: E402

DEFAULT_LEDGER = os.path.join(".jev", "subgoals.jsonl")

# Tier -> subagent defined in ~/.claude/agents/. Model and effort come from config/agents.json (_sync_tiers_from_config);
# the efforts below are only the fallback when the config can't be read.
# ultracode is not a subagent: the main agent orchestrates the task with the Workflow tool.
TIERS = {
    "scout":     {"subagent_type": "jev-scout",     "effort": "low",    "ladder": "read"},
    "analyst":   {"subagent_type": "jev-analyst",   "effort": "high",   "ladder": "read"},
    "builder":   {"subagent_type": "jev-builder",   "effort": "low",    "ladder": "write"},
    "engineer":  {"subagent_type": "jev-engineer",  "effort": "medium", "ladder": "write"},
    "debugger":  {"subagent_type": "jev-debugger",  "effort": "high",   "ladder": "write"},
    "architect": {"subagent_type": "jev-architect", "effort": "max",    "ladder": "plan"},
    "reviewer":  {"subagent_type": "jev-reviewer",  "effort": "medium", "ladder": "read"},
    "advisor":   {"subagent_type": "jev-advisor",   "effort": "high",   "ladder": "read"},
    "ultracode": {"subagent_type": None,            "effort": None,     "ladder": "orchestrate"},
}
# One step up, always on the tier's own ladder (read-only tiers never escalate to a write agent).
ESCALATE = {
    "scout": "analyst", "analyst": "analyst",
    "builder": "engineer", "engineer": "debugger", "debugger": "architect", "architect": "architect",  # code: low -> medium -> high -> replan
    "reviewer": "reviewer", "advisor": "advisor",  # review stays medium
}
# When stuck at the top of the write ladder, a single context is not enough: orchestrate instead.
STUCK_ESCALATE = dict(ESCALATE, architect="ultracode", ultracode="ultracode")

# ---------- config (config/agents.json: single source of truth for routing) ----------

CONFIG_PATH = os.path.join(jevlib.REPO_ROOT, "config", "agents.json")
SCHEMA_DIR = os.path.join(jevlib.REPO_ROOT, "config", "schemas")


class ConfigError(Exception):
    """config/agents.json is missing or malformed."""


def jev_home():
    """Where last_route.json and routing.log live (default ~/.claude/jev; JEV_HOME overrides, for tests)."""
    return os.environ.get("JEV_HOME") or jevlib.LOG_DIR


ACTIVE_CONFIG_NAME = "agents.json"


def config_path():
    """JEV_CONFIG, else <jev_home()>/agents.json when present (written by onboarding), else the shipped config."""
    env = os.environ.get("JEV_CONFIG")
    if env:
        return env
    active = os.path.join(jev_home(), ACTIVE_CONFIG_NAME)
    if os.path.isfile(active):
        return active
    return CONFIG_PATH


def load_config(path=None):
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("agents"), dict) or not isinstance(cfg.get("models"), dict):
        raise ConfigError("%s: needs 'agents' and 'models' objects" % path)
    return cfg


def agent_model(cfg, agent):
    """Runtime model id for `agent` (e.g. claude-sonnet-5-5), or None if the agent isn't configured."""
    spec = cfg["agents"].get(agent)
    if not spec:
        return None
    return cfg["models"].get(spec.get("model"), spec.get("model"))


def agent_effort(cfg, agent):
    return (cfg["agents"].get(agent) or {}).get("effort")


def _sync_tiers_from_config():
    try:
        cfg = load_config()
    except ConfigError:
        return None
    for t in TIERS.values():
        spec = cfg["agents"].get(t["subagent_type"] or "")
        if spec and spec.get("effort"):
            t["effort"] = spec["effort"]
    return cfg


_CFG = _sync_tiers_from_config()


def tier_model(tier):
    """Config model id for a tier's agent (None for ultracode or when the config is unreadable)."""
    st = TIERS.get(tier, {}).get("subagent_type")
    return agent_model(_CFG, st) if (_CFG and st) else None


# Thresholds (policy lives in code; Jev only answers narrow questions).
BREADTH_ULTRACODE = 2.5
DEPTH_DEEP_READ = 1.5
DEPTH_ENGINEER = 1.5
DEPTH_DESIGN = 2.0
DEPTH_HARD = 2.5
DEPTH_CODE_MEDIUM = 2.0  # below this, code runs at low (builder)
P_YES = 0.6
STAKES_MAX = 0.85  # only clear-cut high stakes justify architect/max; measured: wrong picks 0.61-0.76, right >= 0.87
P_STRONG = 0.8  # design must be clear-cut to justify max effort; moderate design work goes to engineer
LOW_CONFIDENCE = 0.5
ADVICE_READ_ONLY = 0.85  # design + clearly read-only = advice, not implementation
READ_ONLY = 0.6  # lean read-only: a read agent that needed to write fails safely and gets rerouted


REVIEW = re.compile(r"\b(review|check|verify|double-check|sanity[- ]check|proofread|audit this (diff|change|pr))\b", re.I)

NO_EDIT = re.compile(
    r"\b(don'?t|do not|without|no need to|never)\s+(fix|chang|edit|modif|touch|commit|writ(e|ing) (any )?code)\w*"
    r"|\bno (code )?changes\b|\bread[- ]only\b|\breport only\b|\bjust (explain|investigate|report|look|review)\b",
    re.I)


def fail(message, fallback):
    print(json.dumps({"error": message, "fallback": fallback}, indent=2))
    sys.exit(1)


def tier_fields(tier):
    t = TIERS[tier]
    return {"tier": tier, "subagent_type": t["subagent_type"], "effort": t["effort"], "ladder": t["ladder"]}


def workflow_shape(task, p):
    """Cheap hint for the ultracode Workflow shape, from answers we already have (no extra Jev call)."""
    if p["read_only"] >= READ_ONLY:  # high stakes is ignored for reads (Jev flags any auth reading as high stakes)
        if re.search(r"\breview\b", task, re.I):
            return "review"
        return "investigate" if p["unknown_cause"] >= P_YES else "understand"
    return "audit" if p["high_stakes"] >= P_YES or p["unknown_cause"] >= P_YES else "migrate"


SHAPES = {
    "understand": "fan out read-only agents per subsystem, then synthesize one explanation",
    "investigate": "fan out read-only finders per area, adversarially verify each finding, report only (no edits)",
    "review": "fan out reviewers per area, then adversarially verify each finding before reporting",
    "audit": "fan out finders per area, adversarially verify each finding, then fix confirmed ones one writer at a time",
    "migrate": "plan the change once, fan out per-file/per-module edits in non-overlapping batches, then verify with tests",
}


def decide(task, depth, depth_conf, breadth, p):
    """Pure routing policy. p holds noul probabilities: self_contained, read_only, high_stakes, unknown_cause, design,
    batchable, exhaustive."""
    reasons = []
    # Designing produces interfaces/code, so a design task is a write task even if Jev rates it mostly read-only.
    design_task = p["design"] >= P_YES and depth >= DEPTH_DESIGN
    # A design question that only asks for a recommendation needs max effort but no edit tools.
    advice_only = design_task and p["read_only"] >= ADVICE_READ_ONLY
    writes = (p["read_only"] < READ_ONLY or design_task) and not advice_only
    if NO_EDIT.search(task):
        # The user said not to change anything: that's a rule, not a judgment call.
        writes, design_task = False, False
        reasons.append("task says not to edit -> read ladder")
    risky_write = writes and p["high_stakes"] >= P_YES  # high stakes only matters for writes

    # Tier on the task's own ladder, ignoring breadth (also used as the worker effort for ultracode).
    if advice_only:
        base = "advisor"
        reasons.append("design question that only needs a recommendation -> advisor (read-only, %s)" % TIERS["advisor"]["effort"])
    elif not writes:
        base = "scout" if depth < DEPTH_DEEP_READ else "analyst"
        if base == "analyst" and REVIEW.search(task):
            # Owner rule: review and checks run at medium; high is for investigation and decisions.
            base = "reviewer"
            reasons.append("review / check -> reviewer (medium)")
        elif base == "analyst":
            reasons.append("read-only investigation -> analyst (high)")
    elif p["unknown_cause"] >= P_YES:
        base = "analyst_then_builder"
    elif design_task and (p["design"] >= P_STRONG or depth >= DEPTH_HARD):
        base = "architect"
        reasons.append("design decision -> architect plans")
    elif risky_write and p["high_stakes"] >= STAKES_MAX and depth >= DEPTH_ENGINEER:
        base = "architect"
        reasons.append("high-stakes change -> architect plans")
    elif depth >= DEPTH_HARD:
        base = "architect"
        reasons.append("hard change -> architect plans")
    elif depth >= DEPTH_CODE_MEDIUM:
        base = "engineer"
        reasons.append("substantial multi-file change -> engineer (medium)")
    else:
        base = "builder"
        reasons.append("code defaults to builder (low)")
    # Low depth confidence only escalates READ tiers; code effort is never raised pre-emptively
    # (it goes up only when an attempt gets stuck: builder low -> engineer medium -> debugger high).
    if depth_conf < LOW_CONFIDENCE and TIERS.get(base, {}).get("ladder") == "read" and ESCALATE[base] != base:
        reasons.append("low confidence on depth -> %s escalated to %s" % (base, ESCALATE[base]))
        base = ESCALATE[base]

    # Owner rule: high / xhigh / max / ultracode are for decisions, architecture and orchestration only.
    # Review and checks run at medium. Code runs at low by default; medium/high only when needed.
    # Hard or unclear write work therefore runs in two steps: a read-only planner, then a low-effort implementer.
    plan = None
    if base == "architect":
        plan = {"planner": "jev-architect", "planner_effort": TIERS["architect"]["effort"], "implementer": "jev-builder",
                "implementer_effort": TIERS["builder"]["effort"],
                "escalate_if_stuck": ["jev-engineer (medium)", "jev-debugger (high)"]}
        reasons.append("plan with architect (%s, read-only), implement with builder (%s)" % (
            TIERS["architect"]["effort"], TIERS["builder"]["effort"]))
    elif base == "analyst_then_builder":
        plan = {"planner": "jev-analyst", "planner_effort": TIERS["analyst"]["effort"], "implementer": "jev-builder",
                "implementer_effort": TIERS["builder"]["effort"],
                "escalate_if_stuck": ["jev-engineer (medium)", "jev-debugger (high)"]}
        reasons.append("unknown cause -> analyst (high, read-only) finds it, builder (low) fixes it")
        base = "builder"

    self_contained = p["self_contained"] >= 0.5
    mechanical = depth < DEPTH_ENGINEER and base in ("scout", "builder")
    # Wide is not enough for a Workflow: one agent can batch a repeated change however many files it touches.
    batchable = p.get("batchable", 0.0) >= P_YES
    exhaustive = p.get("exhaustive", 0.0) >= P_YES
    single_agent = mechanical or batchable or not exhaustive
    if breadth >= BREADTH_ULTRACODE and single_agent:
        reasons.append("broad but %s -> one %s instead of a Workflow" % (
            "mechanical" if mechanical else "one repeated change" if batchable else "one flow or question", base))
    if breadth >= BREADTH_ULTRACODE and not single_agent:
        shape = workflow_shape(task, p)
        reasons.append("breadth %.2f >= %.1f -> too broad for one context, orchestrate with Workflow" % (breadth, BREADTH_ULTRACODE))
        if not self_contained:
            reasons.append("not self-contained -> put the missing context into every workflow agent prompt")
        d = tier_fields("ultracode")
        worker_model = tier_model(base)
        d.update(delegate=True, via="workflow", effort=TIERS[base]["effort"], worker_tier=base, worker_model=worker_model,
                 workflow_shape=shape, workflow_hint=SHAPES[shape],
                 instruction="Delegate via the Workflow tool (load the workflow-authoring skill first); "
                             "planning/review agents may use high+; reviewers/checkers use opts.effort='medium'; every agent() that writes code passes opts.effort='low' (medium/high only when a coder got stuck); "
                             "pass opts.model to every agent() call: the config/agents.json model id of the role it plays (%s for the %s worker); "
                             "never an alias: settings can remap opus/sonnet/haiku." % (worker_model or "see config/agents.json", base))
        return d, reasons, writes

    if not self_contained:
        reasons.append("not self-contained -> keep in main agent, or pass the needed context in the prompt")
    d = tier_fields(base)
    d.update(delegate=self_contained, via="subagent" if self_contained else "main")
    if plan:
        d["plan_first"] = plan
    return d, reasons, writes


# ---------- route ----------

def route_task(task, context="", timeout=None):
    """Ask Jev about `task` and apply the routing policy. Returns (decision, answers, state).
    Raises jevlib.JevError if Jev can't answer. `timeout` (seconds) caps a single Jev attempt with no retries."""
    state = {"task": jevlib.redact(task), "context_from_main_agent": jevlib.redact(context or "")}
    questions = {
        "depth": {
            "type": "score",
            "instructions": "How much careful reasoning does `task` need to be done correctly?",
            "criteria": [
                "Mechanical: a lookup, search, rename, formatting, or version bump with no judgment",
                "Clear change: the fix or edit is known and touches one or two files",
                "Multi-part: coordinated changes across several files, or a bug whose cause is already known",
                "Hard: unknown root cause, design decisions, concurrency, security, or subtle correctness",
            ],
        },
        "breadth": {
            "type": "score",
            "instructions": "How much of the codebase does `task` span, i.e. how many files or areas must be read or changed to do it?",
            "criteria": [
                "One file",
                "A few related files",
                "One subsystem or module",
                "Many subsystems or the whole codebase",
            ],
        },
        "self_contained": jevlib.noul(
            "Could a fresh assistant do `task` well given only the task text and the repository, without the main agent's conversation history?",
            "The task names what to do and where; the repository is enough",
            "It depends on decisions, findings, or preferences only present in the main conversation"),
        "read_only": jevlib.noul(
            "Can `task` be completed purely by reading, searching, or running read-only commands, without editing any file?",
            "Only reads, searches, or reports", "Needs to create, edit, or delete files or change state"),
        "high_stakes": jevlib.noul(
            "If `task` were done slightly wrong, could the result be a security hole, leaked secrets, lost or corrupted data, "
            "wrong money amounts, or a production outage? Judge the consequences of a mistake, not the topic: a UI tweak on a "
            "login screen or a flag that only prints output is not high stakes.",
            "A plausible mistake would cause a security, data, money, or outage problem",
            "A plausible mistake would be a visible bug that is cheap to notice and fix"),
        "unknown_cause": jevlib.noul(
            "Is `task` debugging or investigating a problem whose cause is not yet known?",
            "The cause must be found", "The cause or the needed change is already known"),
        "design": jevlib.noul(
            "Does `task` require choosing between approaches or designing architecture or interfaces?",
            "An approach must be chosen or an architecture/interface designed",
            "The approach is already decided; it only needs to be carried out"),
        "exhaustive": jevlib.noul(
            "Does `task` require covering every instance or area exhaustively (an audit, a sweep, or a migration of many "
            "separate components), rather than explaining or changing one flow, feature, or change set?",
            "It must find or change every instance across many separate areas",
            "It is about one flow, feature, question, or change set, even if that spans many files"),
        "batchable": jevlib.noul(
            "Could a single engineer do all of `task` in one pass by applying the same known change or reading pattern "
            "repeatedly (e.g. a rename, threading one field through layers, a consistent API update), without separate "
            "investigation or design work in each area?",
            "One consistent change or pass, repeated across files; no per-area investigation",
            "Different areas need their own investigation, findings, or decisions"),
    }
    kw = {"timeout": timeout, "retries": 1} if timeout else {}
    try:
        r = jevlib.ask(state, questions, **kw)
        a = r["answers"]
        depth, depth_conf = a["depth"]["score"], a["depth"]["confidence"]
        breadth = a["breadth"]["score"]
        p = {k: a[k]["noul"] for k in ("self_contained", "read_only", "high_stakes", "unknown_cause", "design", "batchable", "exhaustive")}
    except (KeyError, TypeError) as e:
        raise jevlib.JevError("bad Jev answer: %r" % e)

    decision, reasons, writes = decide(task, depth, depth_conf, breadth, p)
    decision.update({
        "parallel_safe": not writes,
        "depth": round(depth, 2),
        "breadth": round(breadth, 2),
        "depth_confidence": round(depth_conf, 2),
        "signals": {k: round(v, 2) for k, v in p.items()},
        "reasons": reasons,
        "jev_tokens": r.get("usage", {}).get("input_tokens"),
    })
    return decision, a, state


def cmd_route(args):
    task_id = check_task_id(args.task_id) if args.task_id else None
    try:
        decision, a, state = route_task(args.task, args.context or "")
    except jevlib.JevError as e:
        fail("Jev unavailable: %s" % e, "route with your own judgment and say Jev was unavailable; without Jev signals treat "
             "write work as the planned route (architect plans, orchestrator persists, builder implements) unless it is a "
             "trivial one-file change")
    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    decision = finalize_route(args.task, decision, files, task_id)
    try:
        record_route(decision)
    except Exception as e:  # the route is still valid; only the substitution check loses its record
        decision["route_record_error"] = str(e)[:200]
    routing_log({"event": "route", "task_id": decision["task_id"], "route": decision["route"], "fast_path": decision["fast_path"],
                 "role": decision.get("next_agent"), "model": agent_model(_CFG, decision["next_agent"]) if (_CFG and decision.get("next_agent")) else None,
                 "effort": agent_effort(_CFG, decision["next_agent"]) if (_CFG and decision.get("next_agent")) else None,
                 "reason": (decision.get("reasons") or [None])[-1], "attempt": None})
    jevlib.log("decisions", {"cwd": os.getcwd(), "kind": "route", "inputs": state, "answers": a, "decision": decision})
    print(json.dumps(decision, indent=2))


# ---------- subgoal ledger ----------

def read_ledger(path):
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_ledger(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def cmd_dedupe(args):
    rows = read_ledger(args.ledger)
    active = [row for row in rows if row["status"] in ("in_flight", "done")][-254:]  # 255-option cap incl. "none"
    decision = {"duplicate_of": None, "status": None}
    if active:
        criteria = {row["id"]: jevlib.redact("[%s] %s" % (row["status"], row["text"])) for row in active}
        criteria["none"] = "The new subgoal is different work from every listed subgoal"
        state = {"new_subgoal": jevlib.redact(args.subgoal)}
        q = {"match": {
            "type": "choice",
            "instructions": "Which existing subgoal would do the same work as `new_subgoal`, so that running `new_subgoal` would repeat it? Pick none if it is genuinely different work, even if related.",
            "criteria": criteria,
        }}
        try:
            r = jevlib.ask(state, q)
            m = r["answers"]["match"]
        except (jevlib.JevError, KeyError, TypeError) as e:
            fail("Jev unavailable: %s" % e, "not registered; compare against `jev.py list` yourself")
        decision["match_confidence"] = round(m["confidence"], 2)
        if m["choice"] != "none" and m["probabilities"][m["choice"]] >= args.threshold:
            hit = next(row for row in active if row["id"] == m["choice"])
            decision.update(duplicate_of=hit["id"], status=hit["status"], text=hit["text"])
        jevlib.log("decisions", {"cwd": os.getcwd(), "kind": "dedupe", "inputs": state, "answers": r["answers"], "decision": decision})
    if decision["duplicate_of"] is None:
        new_id = "g%d" % (len(rows) + 1)
        rows.append({"id": new_id, "text": jevlib.redact(args.subgoal), "status": "in_flight", "ts": time.time()})
        write_ledger(args.ledger, rows)
        decision.update(registered=new_id)
    print(json.dumps(decision, indent=2))


def cmd_done(args):
    rows = read_ledger(args.ledger)
    for row in rows:
        if row["id"] == args.id:
            row["status"] = "done"
            write_ledger(args.ledger, rows)
            print(json.dumps({"ok": True, "id": args.id}))
            return
    print(json.dumps({"error": "no subgoal %s in %s" % (args.id, args.ledger)}))
    sys.exit(1)


def cmd_list(args):
    print(json.dumps(read_ledger(args.ledger), indent=2))


# ---------- stuck ----------

def cmd_stuck(args):
    text = args.state
    if text.startswith("@"):
        with open(text[1:]) as f:
            text = f.read()[-60000:]  # stay well inside Jev's 32k-token state budget
    text = jevlib.redact(text)
    state = {"recent_agent_activity": text, "current_tier": args.tier}
    q = {"stuck": jevlib.noul(
        "Based on `recent_agent_activity`, is the agent stuck: repeating the same failing approach, re-editing the same code without progress, or tests failing again for the same reason?",
        "No real progress across the last attempts", "Making progress or trying meaningfully different approaches")}
    nxt = STUCK_ESCALATE[args.tier]
    try:
        r = jevlib.ask(state, q)
        s = r["answers"]["stuck"]["noul"]
    except (jevlib.JevError, KeyError, TypeError) as e:
        fail("Jev unavailable: %s" % e, "judge stuck yourself; if escalating, the next tier on this ladder is %s" % nxt)
    escalate = s >= P_YES
    decision = {"stuck": round(s, 2), "escalate": escalate, "current_tier": args.tier}
    if escalate:
        decision["classify"] = ("classify the failure, then run: jev.py escalate --task <task_id> --from %s --category "
                                "<%s>; it routes by category and records the escalation the dispatch router requires" % (
                                    TIERS[args.tier]["subagent_type"] or args.tier, "|".join(CATEGORIES)))
    decision.update({"next_" + k: v for k, v in tier_fields(nxt if escalate else args.tier).items()})
    decision["subagent_type"] = decision.pop("next_subagent_type")  # keep the old top-level key
    if escalate and args.tier == "ultracode":
        decision["note"] = "the Workflow itself is stuck: re-scope or split the task, or hand back to the user"
    elif escalate and args.tier == "architect":
        decision["note"] = "architect is stuck: orchestrate with the Workflow tool (ultracode) instead of one subagent"
    elif escalate and nxt == args.tier:
        decision["note"] = "already at the top of the %s ladder; re-scope the task or orchestrate it (ultracode)" % TIERS[args.tier]["ladder"]
    jevlib.log("decisions", {"cwd": os.getcwd(), "kind": "stuck", "inputs": {"current_tier": args.tier, "chars": len(text)},
                             "answers": r["answers"], "decision": decision})
    print(json.dumps(decision, indent=2))


# ---------- dispatch outcomes / labels ----------

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "max": 3}
LABELS = ("ok", "too_low", "too_high")
ESCALATE_JACCARD = 0.6
FINAL_FLAGS = re.compile(r"\b(blocked|stuck|could not|couldn't|unable|failing)\b", re.I)
_TYPE_TIER = {t["subagent_type"]: name for name, t in TIERS.items() if t["subagent_type"]}


def tier_side(subagent_type):
    """'read' (read + plan ladders), 'write', or None for types Jev doesn't route."""
    tier = _TYPE_TIER.get(subagent_type or "")
    if not tier:
        return None
    return "write" if TIERS[tier]["ladder"] == "write" else "read"


def tier_effort(subagent_type):
    tier = _TYPE_TIER.get(subagent_type or "")
    return EFFORT_RANK.get(TIERS[tier]["effort"]) if tier else None


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a, b):
    a, b = _words(a), _words(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def find_transcripts(projects_dir, ids):
    """tool_use_id -> (meta dict, jsonl path) for subagent transcripts under projects_dir."""
    import glob
    out = {}
    for meta_path in glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "agent-*.meta.json")):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        tid = meta.get("toolUseId") if isinstance(meta, dict) else None
        if tid in ids:
            out[tid] = (meta, meta_path[:-len(".meta.json")] + ".jsonl")
    return out


def _parse_ts(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_stats(path):
    stats = {"minutes": None, "turns": 0, "tool_uses": 0, "tool_errors": 0, "output_tokens": 0, "final_flags": []}
    stamps, tokens, last_text = [], {}, ""
    try:
        f = open(path)
    except OSError:
        return stats
    with f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            t = _parse_ts(e.get("timestamp")) if e.get("timestamp") else None
            if t is not None:
                stamps.append(t)
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            if e.get("type") == "assistant":
                stats["turns"] += 1
                usage = msg.get("usage") or {}
                mid = msg.get("id") or id(e)
                tokens[mid] = max(tokens.get(mid, 0), usage.get("output_tokens") or 0)
                text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if text:
                    last_text = text
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    stats["tool_uses"] += 1
                elif c.get("type") == "tool_result" and c.get("is_error"):
                    stats["tool_errors"] += 1
    if stamps:
        stats["minutes"] = round((max(stamps) - min(stamps)) / 60.0, 2)
    stats["output_tokens"] = sum(tokens.values())
    stats["final_flags"] = sorted(set(m.lower() for m in FINAL_FLAGS.findall(last_text)))
    return stats


def _labels(decisions):
    out = {}
    for r in decisions:  # log order: the latest label wins
        if r.get("kind") == "dispatch_label" and r.get("label") in LABELS:
            out[r.get("tool_use_id")] = r
    return out


def _accuracy(rows, key):
    """Accuracy of what ran, split by whose pick ran, and who was right on disagreements."""
    acc = {"labeled": 0, "ok": 0, "jev_pick_ran": [0, 0], "main_pick_ran": [0, 0],
           "disagreements": {"jev_right": 0, "main_right": 0, "unclear": 0}}
    for r in rows:
        label = r.get(key)
        if label not in LABELS:
            continue
        acc["labeled"] += 1
        ok = label == "ok"
        acc["ok"] += ok
        ran, jev, main = r["ran_type"], r["jev_type"], r["main_type"]
        for who, pick in (("jev_pick_ran", jev), ("main_pick_ran", main)):
            if pick and ran == pick:
                acc[who][0] += ok
                acc[who][1] += 1
        if not jev or jev == main or ran not in (jev, main):
            continue
        other = main if ran == jev else jev
        ran_winner, other_winner = ("jev_right", "main_right") if ran == jev else ("main_right", "jev_right")
        er, eo = tier_effort(ran), tier_effort(other)
        if ok:
            acc["disagreements"][ran_winner] += 1
        elif er is not None and eo is not None and ((label == "too_low" and eo > er) or (label == "too_high" and eo < er)):
            acc["disagreements"][other_winner] += 1
        else:
            acc["disagreements"]["unclear"] += 1
    for who in ("jev_pick_ran", "main_pick_ran"):
        n_ok, n = acc[who]
        acc[who] = {"n": n, "ok": n_ok, "accuracy": round(n_ok / n, 2) if n else None}
    acc["accuracy"] = round(acc["ok"] / acc["labeled"], 2) if acc["labeled"] else None
    return acc


def _gt(a, b):
    return a is not None and b is not None and a > b


def _median(values):
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def build_outcomes(log_dir, days, projects_dir=None):
    since = time.time() - days * 86400
    decisions = _read_log(log_dir, "decisions", since)
    labels = _labels(_read_log(log_dir, "decisions", 0))
    dispatches = [r for r in decisions if r.get("kind") == "dispatch" and r.get("tool_use_id")]
    found = find_transcripts(projects_dir or PROJECTS_DIR, {r["tool_use_id"] for r in dispatches})
    rows = []
    for r in dispatches:
        tid = r["tool_use_id"]
        meta, path = found.get(tid, (None, None))
        stats = transcript_stats(path) if path else {}
        fallback = r.get("jev_type") if r.get("applied") else r.get("main_type")
        lab = labels.get(tid) or {}
        rows.append(dict({
            "tool_use_id": tid, "session_id": r.get("session_id"), "ts": r.get("ts", 0),
            "description": r.get("description") or "", "main_type": r.get("main_type"), "jev_type": r.get("jev_type"),
            "applied": bool(r.get("applied")), "skip": r.get("skip"),
            "ran_type": (meta or {}).get("agentType") or fallback, "transcript": bool(path),
            "label": lab.get("label"), "note": lab.get("note"),
        }, **stats))
    for i, r in enumerate(rows):
        side, eff = tier_side(r["ran_type"]), tier_effort(r["ran_type"])
        r["escalated"] = bool(side) and any(
            later["session_id"] == r["session_id"] and tier_side(later["ran_type"]) == side
            and _gt(tier_effort(later["ran_type"]), eff)
            and _jaccard(later["description"], r["description"]) >= ESCALATE_JACCARD
            for later in rows[i + 1:])
        r["stuck"] = bool(r.get("final_flags"))
        r["auto_label"] = "too_low" if (r["escalated"] or r["stuck"]) else None
    routable = [r for r in rows if r["skip"] != "tier_not_routable" and r["jev_type"]]
    per_tier = {}
    for t in sorted(set(r["ran_type"] for r in rows if r["ran_type"])):
        g = [r for r in rows if r["ran_type"] == t]
        per_tier[t] = {"n": len(g), "median_min": _median([r.get("minutes") for r in g]),
                       "median_out_tokens": _median([r.get("output_tokens") for r in g if r["transcript"]]),
                       "errors": sum(1 for r in g if r.get("tool_errors")), "stuck": sum(r["stuck"] for r in g),
                       "escalated": sum(r["escalated"] for r in g)}
    summary = {
        "dispatches": len(rows), "routable": len(routable), "with_transcript": sum(r["transcript"] for r in rows),
        "agreement": round(sum(r["jev_type"] == r["main_type"] for r in routable) / len(routable), 2) if routable else None,
        "applied_rate": round(sum(r["applied"] for r in routable) / len(routable), 2) if routable else None,
        "per_tier": per_tier,
        "manual": _accuracy(rows, "label"),
        "auto": _accuracy([r for r in rows if not r["label"]], "auto_label"),
    }
    return {"days": days, "rows": rows, "summary": summary}


def _print_outcomes(out):
    rows, s = out["rows"], out["summary"]
    if not rows:
        print("No dispatch records in the last %g day(s). The dispatch_router hook logs them once registered." % out["days"])
        return
    print("%-12s %-14s %-14s %-14s %3s %6s %7s %4s %-9s %s" % ("tool_use", "main", "jev", "ran", "app", "min", "outtok", "err", "label", "description"))
    for r in rows:
        print("%-12s %-14s %-14s %-14s %3s %6s %7s %4s %-9s %s" % (
            r["tool_use_id"][-12:], r["main_type"] or "-", r["jev_type"] or "-", r["ran_type"] or "-",
            "y" if r["applied"] else "", r.get("minutes") if r.get("minutes") is not None else "-",
            r.get("output_tokens", "-"), r.get("tool_errors", "-"),
            r["label"] or ("~" + r["auto_label"] if r["auto_label"] else "-"), r["description"][:50]))
    print("\ndispatches %d (routable %d, transcripts %d)  agreement %s  applied %s" % (
        s["dispatches"], s["routable"], s["with_transcript"], s["agreement"], s["applied_rate"]))
    for t, v in s["per_tier"].items():
        print("  %-14s n %d  median %s min  %s out tok  errors %d  stuck %d  escalated %d" % (
            t, v["n"], v["median_min"], v["median_out_tokens"], v["errors"], v["stuck"], v["escalated"]))
    for kind in ("manual", "auto"):
        a = s[kind]
        if a["labeled"]:
            print("  %s labels %d: accuracy %s | jev pick ran %s | main pick ran %s | disagreements %s" % (
                kind, a["labeled"], a["accuracy"], a["jev_pick_ran"], a["main_pick_ran"], a["disagreements"]))
        else:
            print("  %s labels: none" % kind)


def cmd_outcomes(args):
    out = build_outcomes(os.path.expanduser(args.log_dir), args.days, args.projects_dir)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _print_outcomes(out)


def cmd_label(args):
    tid = args.id
    if tid == "last":
        rows = [r for r in _read_log(os.path.expanduser(args.log_dir), "decisions", 0) if r.get("kind") == "dispatch" and r.get("tool_use_id")]
        if not rows:
            print(json.dumps({"error": "no dispatch records to label"}))
            sys.exit(1)
        tid = rows[-1]["tool_use_id"]
    rec = {"kind": "dispatch_label", "tool_use_id": tid, "label": args.label, "note": args.note}
    jevlib.log("decisions", rec)
    print(json.dumps(dict(rec, ok=True)))


# ---------- report ----------

def _read_log(log_dir, name, since):
    rows = []
    path = os.path.join(log_dir, name + ".jsonl")
    if not os.path.isfile(path):
        return rows
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) >= since:
                rows.append(r)
    return rows


def _pct(values, q):
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    return int(v[min(len(v) - 1, int(round(q * (len(v) - 1))))])


def _count(items):
    out = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def build_report(log_dir, days):
    since = time.time() - days * 86400
    perms = _read_log(log_dir, "permissions", since)
    prompts = _read_log(log_dir, "prompts", since)
    decisions = _read_log(log_dir, "decisions", since)

    # Permission gate (fast-allow commands are not logged, only rule hits and Jev calls)
    jev_perm = [r for r in perms if r.get("layer") == 3]
    flagged = [r for r in perms if r.get("decision") in ("deny", "ask")]
    repeat_asks = _count(re.sub(r"\s+", " ", r.get("command", ""))[:60] for r in flagged if r.get("decision") == "ask")
    gate = {
        "logged": len(perms),
        "by_layer_decision": _count("L%s %s" % (r.get("layer"), r.get("decision")) for r in perms),
        "jev_calls": len(jev_perm),
        "jev_errors": sum(1 for r in jev_perm if r.get("error")),
        "jev_latency_ms": {"p50": _pct([r.get("latency_ms") for r in jev_perm], 0.5),
                           "p95": _pct([r.get("latency_ms") for r in jev_perm], 0.95)},
        "recent_flagged": [{"decision": r.get("decision"), "layer": r.get("layer"),
                            "command": r.get("command", "")[:120], "why": r.get("why") or r.get("choice")}
                           for r in flagged[-15:]],
        "repeated_asks": {k: v for k, v in repeat_asks.items() if v >= 3},
    }

    # Prompt router
    ran = [r for r in prompts if not r.get("skipped")]
    totals = [r.get("total_ms") for r in ran]
    errors = [e for r in ran for e in (r.get("errors") or [])]
    router = {
        "prompts": len(prompts),
        "skipped": _count(r["skipped"] for r in prompts if r.get("skipped")),
        "ran_jev": len(ran),
        "errors": len(errors),
        "error_samples": sorted(set(e[:120] for e in errors))[:5],
        "total_ms": {"p50": _pct(totals, 0.5), "p95": _pct(totals, 0.95), "max": _pct(totals, 1.0)},
        "skills_suggested": _count(r["final_skill"] for r in ran if r.get("final_skill")),
        "no_skill_reasons": _count(r.get("stop") or "none" for r in ran if not r.get("final_skill")),
        "conditions_fired": _count(c for r in ran for c in (r.get("fired") or {})),
        "slowest": [{"ms": r.get("total_ms"), "prompt": (r.get("prompt") or "")[:80], "errors": r.get("errors")}
                    for r in sorted(ran, key=lambda r: -(r.get("total_ms") or 0))[:3]],
    }

    # Orchestrator decisions
    routes = [r for r in decisions if r.get("kind") == "route"]
    stuck = [r for r in decisions if r.get("kind") == "stuck"]
    dedupe = [r for r in decisions if r.get("kind") == "dedupe"]
    orch = {
        "routes": len(routes),
        "by_tier": _count((r.get("decision") or {}).get("tier") for r in routes),
        "kept_in_main": sum(1 for r in routes if (r.get("decision") or {}).get("via") == "main"),
        "stuck_checks": len(stuck),
        "escalations": _count("%s->%s" % ((r.get("decision") or {}).get("current_tier"), (r.get("decision") or {}).get("next_tier"))
                              for r in stuck if (r.get("decision") or {}).get("escalate")),
        "dedupe_checks": len(dedupe),
        "duplicates_caught": sum(1 for r in dedupe if (r.get("decision") or {}).get("duplicate_of")),
    }

    # Live dispatch routing (dispatch_router hook)
    disp = [r for r in decisions if r.get("kind") == "dispatch"]
    routable = [r for r in disp if r.get("skip") != "tier_not_routable" and r.get("jev_type")]
    labels = _labels(decisions)
    dispatch = {
        "dispatches": len(disp), "routable": len(routable),
        "skips": _count(r.get("skip") for r in disp if r.get("skip")),
        "agreement": round(sum(r.get("jev_type") == r.get("main_type") for r in routable) / len(routable), 2) if routable else None,
        "applied_rate": round(sum(bool(r.get("applied")) for r in routable) / len(routable), 2) if routable else None,
        "errors": sum(1 for r in disp if r.get("errors")),
        "labeled_accuracy": None,
    }
    if labels:
        lab_rows = [{"label": (labels.get(r.get("tool_use_id")) or {}).get("label"), "main_type": r.get("main_type"),
                     "jev_type": r.get("jev_type"),
                     "ran_type": r.get("jev_type") if r.get("applied") else r.get("main_type")} for r in disp]
        dispatch["labeled_accuracy"] = _accuracy(lab_rows, "label")

    # Things worth a look (plain rules; thresholds are starting points)
    look = []
    if router["errors"]:
        look.append("router: %d Jev error(s); each one can stall a prompt up to ~5 s" % router["errors"])
    if (router["total_ms"]["p95"] or 0) > 4000:
        look.append("router: p95 %d ms is slow; consider skipping more follow-up prompts" % router["total_ms"]["p95"])
    if gate["jev_errors"]:
        look.append("gate: %d Jev error(s) fell back to plain rules" % gate["jev_errors"])
    for cmd, n in gate["repeated_asks"].items():
        look.append("gate: asked %dx about '%s' - allow it in rules if it's routine" % (n, cmd))
    if len(ran) >= 10 and not router["skills_suggested"]:
        look.append("router: no skill suggested in %d prompts; check needs_skill thresholds" % len(ran))
    if routes and orch["kept_in_main"] > len(routes) / 2:
        look.append("routing: most tasks judged not self-contained; pass more context with --context")
    return {"days": days, "log_dir": log_dir, "gate": gate, "router": router, "orchestrator": orch, "dispatch": dispatch, "look_at": look}


def _print_report(rep):
    def kv(d):
        return ", ".join("%s %s" % (k, v) for k, v in d.items()) or "-"
    g, r, o = rep["gate"], rep["router"], rep["orchestrator"]
    print("Jev report: last %d day(s)  (%s)\n" % (rep["days"], rep["log_dir"]))
    print("PERMISSION GATE  (routine commands pass silently and aren't logged)")
    print("  logged: %d  |  %s" % (g["logged"], kv(g["by_layer_decision"])))
    print("  Jev calls: %d, errors %d, latency p50 %s / p95 %s ms" % (g["jev_calls"], g["jev_errors"], g["jev_latency_ms"]["p50"], g["jev_latency_ms"]["p95"]))
    for f in g["recent_flagged"]:
        print("    %-4s L%s  %-70s  %s" % (f["decision"], f["layer"], f["command"][:70], (f["why"] or "")[:70]))
    print("\nPROMPT ROUTER")
    print("  prompts: %d, ran Jev: %d, skipped: %s" % (r["prompts"], r["ran_jev"], kv(r["skipped"])))
    print("  time p50 %s / p95 %s / max %s ms, errors %d" % (r["total_ms"]["p50"], r["total_ms"]["p95"], r["total_ms"]["max"], r["errors"]))
    print("  skills suggested: %s" % kv(r["skills_suggested"]))
    print("  no skill because: %s" % kv(r["no_skill_reasons"]))
    print("  conditions fired: %s" % kv(r["conditions_fired"]))
    for s in r["slowest"]:
        print("    slow %s ms: %s%s" % (s["ms"], s["prompt"], ("  [" + s["errors"][0][:60] + "]") if s["errors"] else ""))
    print("\nORCHESTRATOR")
    print("  routes: %d  by tier: %s  kept in main: %d" % (o["routes"], kv(o["by_tier"]), o["kept_in_main"]))
    print("  stuck checks: %d  escalations: %s" % (o["stuck_checks"], kv(o["escalations"])))
    print("  dedupe checks: %d  duplicates caught: %d" % (o["dedupe_checks"], o["duplicates_caught"]))
    d = rep["dispatch"]
    print("\nDISPATCH ROUTING")
    print("  dispatches: %d  routable: %d  skips: %s  errors: %d" % (d["dispatches"], d["routable"], kv(d["skips"]), d["errors"]))
    print("  agreement: %s  applied: %s" % (d["agreement"], d["applied_rate"]))
    if d["labeled_accuracy"] and d["labeled_accuracy"]["labeled"]:
        a = d["labeled_accuracy"]
        print("  labeled %d: accuracy %s  disagreements %s  (details: jev.py outcomes)" % (a["labeled"], a["accuracy"], a["disagreements"]))
    print("\nWORTH A LOOK")
    for line in rep["look_at"] or ["nothing flagged"]:
        print("  - " + line)


def cmd_report(args):
    if args.action == "lint":
        if not args.file:
            _err_exit({"error": "usage: jev.py report lint FILE (or - for stdin)"})
        return cmd_report_lint(args)
    if args.action:
        _err_exit({"error": "unknown report action %r (only 'lint')" % args.action})
    rep = build_report(os.path.expanduser(args.log_dir), args.days)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print_report(rep)


# ====================================================================================================
# vNext: preflight, dispatch checks, fast path, stuck ladder v2, plan persistence, handoffs, reports.
# Everything below reads config/agents.json; nothing here calls Jev.
# ====================================================================================================

try:
    import yaml as _yaml  # optional: only used as a second opinion on frontmatter and to read YAML handoffs
except Exception:  # pragma: no cover
    _yaml = None

MODEL_ALIASES = {"opus", "sonnet", "haiku", "inherit", "default", "best", "opusplan"}
WRITE_TOOLS = ("Edit", "Write")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
TASK_MARKER = re.compile(r"\[jev:task=([A-Za-z0-9][A-Za-z0-9._-]{0,80})\]")
REGISTRY_NOTE = ("File checks cannot see the live session's agent registry: Claude Code loads agents when a session starts. "
                 "An agent whose file was missing or invalid when this session started stays unregistered until you restart "
                 "Claude Code, even if the file is valid now.")
RESTART_HINT = ("Fix: re-run python scripts/onboard.py (re-renders ~/.claude/agents from your routing config; "
                "python scripts/sync_agents.py fixes the repo templates), run `jev.py preflight` again, then restart Claude Code.")


def default_agents_dir():
    return os.environ.get("JEV_AGENTS_DIR") or os.path.expanduser("~/.claude/agents")


def is_model_alias(model):
    m = re.sub(r"\[.*\]$", "", str(model or "").strip().lower())
    return m in MODEL_ALIASES


def _err_exit(obj, code=1):
    print(json.dumps(obj, indent=2))
    sys.exit(code)


def check_task_id(task_id):
    if not task_id or not TASK_ID_RE.match(task_id):
        _err_exit({"error": "bad task id %r: use letters, digits, '.', '_' or '-' (max 81 chars)" % task_id})
    return task_id


# ---------- frontmatter (strict, works without PyYAML) ----------

def _plain_scalar_error(raw):
    if ": " in raw or raw.endswith(":"):
        return "unquoted ': ' in a plain value (YAML reads it as a nested mapping and Claude Code skips the agent); quote the value"
    if " #" in raw or "\t#" in raw:
        return "unquoted ' #' in a plain value (YAML treats the rest as a comment); quote the value"
    if raw[0] in "@`%":
        return "plain value starts with reserved character %r; quote the value" % raw[0]
    if raw[0] in "&*!":
        return "plain value starts with %r (anchor/alias/tag); quote the value" % raw[0]
    return None


def _scalar(raw):
    """Parse one frontmatter value. Returns (value, error)."""
    if raw == "":
        return None, None
    q = raw[0]
    if q == '"':
        i, out = 1, []
        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(nxt, "\\" + nxt))
                i += 2
                continue
            if c == '"':
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    return None, "text after the closing quote: %r" % rest[:30]
                return "".join(out), None
            out.append(c)
            i += 1
        return None, "unterminated double-quoted value"
    if q == "'":
        i, out = 1, []
        while i < len(raw):
            if raw[i] == "'":
                if raw[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                rest = raw[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    return None, "text after the closing quote: %r" % rest[:30]
                return "".join(out), None
            out.append(raw[i])
            i += 1
        return None, "unterminated single-quoted value"
    if q == "[":
        if not raw.rstrip().endswith("]"):
            return None, "unterminated [ list ]"
        inner = raw.strip()[1:-1]
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()], None
    if q in "|>":
        return "", None  # block scalar: content is on the indented lines below
    err = _plain_scalar_error(raw)
    return (None, err) if err else (raw.strip(), None)


def parse_frontmatter(text, use_yaml=True):
    """Strict frontmatter check. Returns (fields or None, body, errors). Rejects unquoted plain values containing ': ' or
    ' #' without needing PyYAML; when PyYAML is installed its parse must also succeed."""
    text = text.lstrip("\ufeff")
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)", text, re.S)
    if not m:
        return None, text, ["no YAML frontmatter (the file must start with a --- line and close it with another ---)"]
    head, body = m.group(1), m.group(2)
    fields, errors, last = {}, [], None
    for n, line in enumerate(head.splitlines(), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t" or line.startswith("- "):
            if last is None:
                errors.append("line %d: indented line before any key" % n)
            elif line.strip().startswith("- ") and isinstance(fields.get(last), (list, type(None))):
                fields[last] = (fields.get(last) or []) + [line.strip()[2:].strip().strip("'\"")]
            continue
        km = re.match(r"([A-Za-z_][\w-]*)[ \t]*:(?:[ \t]+(.*))?$", line)
        if not km:
            errors.append("line %d: expected 'key: value', got %r" % (n, line[:60]))
            continue
        key, raw = km.group(1), (km.group(2) or "").strip()
        if key in fields:
            errors.append("line %d: duplicate key %r" % (n, key))
        value, err = _scalar(raw)
        if err:
            errors.append("line %d (%s): %s" % (n, key, err))
        fields[key], last = value, key
    if use_yaml and _yaml is not None:
        try:
            data = _yaml.safe_load(head)
            if not isinstance(data, dict):
                errors.append("PyYAML: frontmatter is not a mapping")
            elif not errors:
                fields = data
        except Exception as e:
            errors.append("PyYAML: %s" % str(e).replace("\n", " ")[:200])
    return fields, body, errors


def parse_tools(value):
    if value is None:
        return None
    items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value))
    return [str(i).strip() for i in items if str(i).strip()]


# ---------- preflight ----------

def resolve_agent_file(name, agents_dir=None, project_dir=None):
    """Project agents (<project>/.claude/agents) shadow user agents in Claude Code, so check those first."""
    if project_dir:
        p = os.path.join(project_dir, ".claude", "agents", name + ".md")
        if os.path.isfile(p):
            return p, "project"
    return os.path.join(agents_dir or default_agents_dir(), name + ".md"), "user"


def check_agent(name, cfg, agents_dir=None, project_dir=None, check_fallback=True, use_yaml=True):
    spec = cfg["agents"].get(name)
    path, scope = resolve_agent_file(name, agents_dir, project_dir)
    row = {"agent": name, "path": path, "scope": scope, "exists": False, "frontmatter_ok": False, "model": None,
           "effort": None, "expected_model": agent_model(cfg, name), "expected_effort": agent_effort(cfg, name),
           "write": (spec or {}).get("write"), "errors": [], "ok": False}
    err = row["errors"]
    if not spec:
        err.append("%s is not in config/agents.json" % name)
        return row
    if check_fallback and (cfg.get("fallbacks") or {}).get(name):
        err.append("config fallback %s -> %s: silent substitution is not allowed (set it to null)" % (name, cfg["fallbacks"][name]))
    if not os.path.isfile(path):
        err.append("definition file missing: %s" % path)
        return row
    row["exists"] = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        err.append("cannot read %s: %s" % (path, e))
        return row
    fields, body, ferr = parse_frontmatter(text, use_yaml=use_yaml)
    err.extend("frontmatter %s" % e for e in ferr)
    if fields is None:
        return row
    row["frontmatter_ok"] = not ferr
    row["model"], row["effort"] = fields.get("model"), fields.get("effort")
    if fields.get("name") != name:
        err.append("name %r does not match the file name %r" % (fields.get("name"), name))
    if not fields.get("description"):
        err.append("description is empty")
    if row["model"] != row["expected_model"]:
        err.append("model %r != config %r%s" % (row["model"], row["expected_model"],
                                                 " (an alias follows settings remaps)" if is_model_alias(row["model"]) else ""))
    if str(row["effort"]) != str(row["expected_effort"]):
        err.append("effort %r != config %r" % (row["effort"], row["expected_effort"]))
    tools = parse_tools(fields.get("tools"))
    row["tools"] = tools
    if spec.get("write"):
        if tools is not None and not all(t in tools for t in WRITE_TOOLS):
            err.append("write: true in config but tools lack %s" % " and ".join(t for t in WRITE_TOOLS if t not in tools))
    elif tools is None:
        err.append("write: false in config but no tools list (the agent inherits every tool, including Edit and Write)")
    elif any(t in tools for t in WRITE_TOOLS + ("MultiEdit", "NotebookEdit")):
        err.append("write: false in config but tools include %s" % ", ".join(
            t for t in WRITE_TOOLS + ("MultiEdit", "NotebookEdit") if t in tools))
    if not body.strip():
        err.append("empty body (no instructions)")
    row["ok"] = not err
    return row


def _short_model(m):
    return (m or "-").replace("claude-", "")


def preflight_table(rows):
    lines = ["| Agent | Definition Exists | Frontmatter Valid | Registered | Callable | Model | Effort |",
             "|---|---:|---:|---:|---:|---|---:|"]
    for r in rows:
        reg = "yes*" if r["ok"] else ("no" if (not r["exists"] or not r["frontmatter_ok"]) else "unusable")
        model = _short_model(r["model"]) if r["model"] == r["expected_model"] else "%s (want %s)" % (
            _short_model(r["model"]), _short_model(r["expected_model"]))
        effort = str(r["effort"]) if str(r["effort"]) == str(r["expected_effort"]) else "%s (want %s)" % (
            r["effort"], r["expected_effort"])
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["agent"], "yes" if r["exists"] else "NO", "yes" if r["frontmatter_ok"] else ("NO" if r["exists"] else "-"),
            reg, reg, model, effort))
    lines.append("")
    lines.append("* yes = the file is valid, so Claude Code registers it at session start. " + REGISTRY_NOTE)
    return "\n".join(lines)


def preflight(cfg, agents_dir=None, names=None, all_agents=False, project_dir=None, use_yaml=True):
    agents_dir = agents_dir or default_agents_dir()
    required = list(cfg.get("required_agents") or [])
    names = list(names or (sorted(cfg["agents"]) if all_agents else required))
    config_errors, warnings = [], []
    for r in required:
        if r not in cfg["agents"]:
            config_errors.append("required agent %s is not in config/agents.json" % r)
    for k, v in sorted((cfg.get("fallbacks") or {}).items()):
        if v:
            config_errors.append("fallback %s -> %s: silent substitution is not allowed (set it to null)" % (k, v))
    g = cfg.get("guardrails") or {}
    for name, spec in sorted(cfg["agents"].items()):
        if spec.get("model") == "opus" and name not in (g.get("opus_allowed_roles") or [name]):
            warnings.append("%s is pinned to opus but is not in guardrails.opus_allowed_roles" % name)
        if spec.get("effort") == "max" and name not in (g.get("max_effort_allowed_roles") or [name]):
            warnings.append("%s runs at max effort but is not in guardrails.max_effort_allowed_roles" % name)
    rows = [check_agent(n, cfg, agents_dir, project_dir, check_fallback=False, use_yaml=use_yaml) for n in names]
    ok = not config_errors and all(r["ok"] for r in rows)
    return {"ok": ok, "agents_dir": agents_dir, "checked": names, "agents": rows, "config_errors": config_errors,
            "warnings": warnings, "table": preflight_table(rows), "message": preflight_message(ok, rows, config_errors, cfg),
            "registry_note": REGISTRY_NOTE}


def preflight_message(ok, rows, config_errors, cfg):
    if ok:
        return ("JEV PREFLIGHT PASSED (%d agents: file present, frontmatter valid, model/effort/tools match config/agents.json)."
                "\nIf you changed any agent file during this session, restart Claude Code so the registry reloads it." % len(rows))
    missing = [r for r in rows if not r["exists"]]
    invalid = [r for r in rows if r["exists"] and not r["ok"]]
    out = ["JEV PREFLIGHT FAILED", ""]
    if missing:
        out.append("Missing agents:")
        out.extend("- %s (%s)" % (r["agent"], r["path"]) for r in missing)
        out.append("")
    if invalid:
        out.append("Invalid agents:")
        for r in invalid:
            out.append("- %s: %s" % (r["agent"], "; ".join(r["errors"])))
        out.append("")
    if config_errors:
        out.append("Config errors:")
        out.extend("- " + e for e in config_errors)
        out.append("")
    bad = missing + invalid
    if bad:
        out.append("Expected routes:")
        width = max(len(r["agent"]) for r in bad)
        out.extend("%s -> %s / %s" % (r["agent"].ljust(width), r["expected_model"], r["expected_effort"]) for r in bad)
        out.append("")
    out += ["Task execution stopped.", "No fallback agent was spawned.", "", RESTART_HINT]
    return "\n".join(out)


def cmd_preflight(args):
    try:
        cfg = load_config()
    except ConfigError as e:
        _err_exit({"ok": False, "error": str(e), "message": "JEV PREFLIGHT FAILED\n\nconfig/agents.json unreadable: %s\n\n"
                   "Task execution stopped.\nNo fallback agent was spawned." % e})
    project_dir = None if (args.agents_dir or os.environ.get("JEV_AGENTS_DIR")) else os.getcwd()
    res = preflight(cfg, args.agents_dir, args.agent, args.all, project_dir)
    res["config"] = config_path()
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("Config: %s" % res["config"])
        print(res["table"])
        print()
        for w in res["warnings"]:
            print("WARNING: " + w)
        print(res["message"])
    if not res["ok"]:
        sys.exit(1)


# ---------- routing state: last_route.json + routing.log ----------

def routes_path():
    return os.path.join(jev_home(), "last_route.json")


def load_routes():
    try:
        with open(routes_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def update_route(task_id, fn, keep=300):
    """Load last_route.json, apply fn(record) -> record for task_id, save. Returns the new record."""
    routes = load_routes()
    rec = fn(dict(routes.get(task_id) or {"task_id": task_id}))
    rec["updated"] = time.time()
    routes[task_id] = rec
    if len(routes) > keep:
        for k in sorted(routes, key=lambda k: routes[k].get("updated", 0))[:len(routes) - keep]:
            routes.pop(k, None)
    _atomic_write_json(routes_path(), routes)
    return rec


def routing_log(record):
    """Append one JSON line to ~/.claude/jev/routing.log. Never raises."""
    try:
        os.makedirs(jev_home(), exist_ok=True)
        with open(os.path.join(jev_home(), "routing.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(record, ts=time.time())) + "\n")
    except Exception:
        pass


def jev_line(fields, tag="[JEV]"):
    """The plan's routing log block: [JEV] then one key=value per line (None values skipped)."""
    return "\n".join([tag] + ["%s=%s" % (k, v) for k, v in fields.items() if v is not None])


# ---------- dispatch checks (used by hooks/dispatch_router.py) ----------

def task_marker(text):
    m = TASK_MARKER.search(text or "")
    return m.group(1) if m else None


def allowed_for(rec):
    """Agents a task may spawn: its routed sequence plus every recorded escalation target."""
    allowed = set(rec.get("allowed") or [])
    allowed |= {e.get("to") for e in rec.get("escalations") or [] if e.get("to")}
    if not rec.get("route"):  # escalation-only record (route was never recorded): the agents it escalated from
        allowed |= {e.get("from") for e in rec.get("escalations") or [] if e.get("from")}
    return allowed


def check_dispatch(cfg, agent, model_param=None, task_id=None, agents_dir=None, project_dir=None, routes=None):
    """Decide whether a jev-* Agent/Task call may run. Returns a dict with allow, errors, warnings, model, effort, reason."""
    out = {"allow": True, "agent": agent, "task_id": task_id, "errors": [], "warnings": [], "model": None,
           "effort": agent_effort(cfg, agent), "reason": None, "task_routed": False}
    err = out["errors"]
    spec = cfg["agents"].get(agent)
    if not spec:
        err.append("JEV DISPATCH DENIED: %s is not a configured JEV agent (config/agents.json). No fallback agent was spawned." % agent)
        out["allow"] = False
        return out
    expected = agent_model(cfg, agent)
    row = check_agent(agent, cfg, agents_dir, project_dir)
    if not row["ok"]:
        err.append("JEV DISPATCH DENIED: %s failed preflight: %s. No fallback agent was spawned. %s %s" % (
            agent, "; ".join(row["errors"]), RESTART_HINT, REGISTRY_NOTE))
    resolved = model_param or row["model"] or expected
    out["model"] = resolved
    if model_param:
        if is_model_alias(model_param):
            err.append("JEV DISPATCH DENIED: model=%r is an alias; aliases follow settings remaps (ANTHROPIC_DEFAULT_*_MODEL). "
                       "Omit `model` (the agent file pins %s) or pass exactly %s." % (model_param, expected, expected))
        elif model_param != expected:
            err.append("JEV DISPATCH DENIED: model=%r differs from config/agents.json (%s -> %s)." % (model_param, agent, expected))
    g = cfg.get("guardrails") or {}
    opus_id = cfg["models"].get("opus")
    if resolved and opus_id and (resolved == opus_id or "opus" in str(resolved).lower()):
        if spec.get("model") != "opus":
            err.append("JEV GUARDRAIL: expected %s -> %s, resolved %s -> %s. Abort before execution." % (
                agent, expected, agent, resolved))
        elif agent not in (g.get("opus_allowed_roles") or [agent]):
            out["warnings"].append("JEV WARNING: %s runs on opus (%s) but is not in guardrails.opus_allowed_roles" % (agent, resolved))
    if out["effort"] == "max" and agent not in (g.get("max_effort_allowed_roles") or [agent]):
        out["warnings"].append("JEV WARNING: %s runs at max effort but is not in guardrails.max_effort_allowed_roles" % agent)
    if task_id and routes is not None and task_id in routes:
        rec = routes[task_id]
        out["task_routed"] = True
        allowed = allowed_for(rec)
        esc = [e for e in rec.get("escalations") or [] if e.get("to") == agent]
        if agent not in allowed:
            err.append("JEV DISPATCH DENIED: task %s was routed to %s; spawning %s instead needs a recorded escalation "
                       "(recorded escalation required). Classify the failure and run `jev.py escalate --task %s --from <role> "
                       "--category <category>` first. No silent substitution." % (
                           task_id, ", ".join(sorted(rec.get("allowed") or [])) or "nothing", agent, task_id))
        elif esc:
            out["reason"] = "escalation %s from %s" % (esc[-1].get("category"), esc[-1].get("from"))
        else:
            out["reason"] = rec.get("reason") or "routed"
    elif task_id:
        out["reason"] = "task %s has no recorded route" % task_id
    else:
        out["reason"] = "dispatch without a [jev:task=...] marker (substitution check skipped)"
    out["allow"] = not err
    return out


def bump_dispatch(task_id):
    rec = update_route(task_id, lambda r: dict(r, dispatches=int(r.get("dispatches") or 0) + 1))
    return rec["dispatches"]


# ---------- route: fast path vs planned ----------

FAST_BREADTH_MAX = 0.75  # breadth score: 0 = "One file", 1 = "A few related files"
FAST_SIGNAL_MAX = 0.4    # design / high_stakes / unknown_cause must be clearly low (conservative)
FAST_CLEAR_MIN = P_YES   # self_contained: the task text is enough
FAST_BLOCKERS = {
    "architecture_change": re.compile(r"\b(architect\w*|redesign|re-architect|new (module|service|subsystem|layer)|module boundar\w*|"
                                      r"cross[- ]module|state machine|framework|dependency injection|plugin system)\b", re.I),
    "schema_change": re.compile(r"\b(schema|migrations?|migrate|database|db|sql|tables?|columns?|index(es)?|orm|proto(buf)?)\b", re.I),
    "persistence_change": re.compile(r"\b(persist\w*|storage|store[sd]?|save (format|file|data)|serializ\w*|deserializ\w*|cache|caching|"
                                     r"disk|file format|local ?storage|session storage|cookies?)\b", re.I),
    "public_api_change": re.compile(r"\b(public api|api|endpoints?|signatures?|interfaces?|exported|exports?|breaking|sdk|"
                                    r"contract|protocol|webhooks?|graphql|rest)\b", re.I),
    "concurrency_change": re.compile(r"\b(concurren\w*|threads?|threading|race|races|locks?|locking|mutex\w*|async\w*|await|"
                                     r"parallel\w*|deadlocks?|atomic\w*|coroutines?|workers?|queues?)\b", re.I),
    "security_or_network": re.compile(r"\b(auth\w*|security|secrets?|tokens?|passwords?|crypto\w*|permissions?|payments?|"
                                      r"network\w*|sockets?|http client|retry|retries|production|prod deploy)\b", re.I),
}
AMBIGUITY = re.compile(r"\b(maybe|somehow|not sure|unclear|figure out|tbd|decide|which (approach|way|option)|or should|"
                       r"something like|improve|better|clean ?up|whatever)\b|\?", re.I)


def fast_path_check(task, decision, files=None, writes=True):
    """Plan phase 10: fast path only when ALL hold. Returns (eligible, checks, reasons)."""
    sig = decision.get("signals") or {}
    depth = decision.get("depth") if decision.get("depth") is not None else 9
    breadth = decision.get("breadth") if decision.get("breadth") is not None else 9
    files = [f for f in (files or []) if f]
    single = (len(files) == 1 and breadth < 1.0) if files else breadth < FAST_BREADTH_MAX
    hits = {k: bool(rx.search(task or "")) for k, rx in FAST_BLOCKERS.items()}
    checks = {
        "single_file_expected": single,
        "architecture_change": hits["architecture_change"] or sig.get("design", 1) >= FAST_SIGNAL_MAX,
        "schema_change": hits["schema_change"],
        "persistence_change": hits["persistence_change"],
        "public_api_change": hits["public_api_change"],
        "concurrency_change": hits["concurrency_change"],
        "requirements_clear": (sig.get("self_contained", 0) >= FAST_CLEAR_MIN and sig.get("unknown_cause", 1) < FAST_SIGNAL_MAX
                               and not AMBIGUITY.search(task or "")),
        "low_stakes": sig.get("high_stakes", 1) < FAST_SIGNAL_MAX and not hits["security_or_network"],
        "small": depth < DEPTH_ENGINEER,
        "writes": bool(writes),
    }
    want = {"single_file_expected": True, "architecture_change": False, "schema_change": False, "persistence_change": False,
            "public_api_change": False, "concurrency_change": False, "requirements_clear": True, "low_stakes": True,
            "small": True, "writes": True}
    failed = [k for k, v in want.items() if checks[k] != v]
    if decision.get("tier") != "builder" or decision.get("plan_first") or decision.get("via") == "workflow":
        failed.append("policy_tier_not_builder")
    return not failed, checks, failed


def make_task_id(task):
    import hashlib
    return "T-" + hashlib.sha1((task or "").strip().lower().encode("utf-8")).hexdigest()[:8]


def _step(cfg, agent, purpose, optional=False):
    return {"agent": agent, "model": agent_model(cfg, agent) if cfg else None, "effort": agent_effort(cfg, agent) if cfg else None,
            "purpose": purpose, "optional": optional}


def finalize_route(task, decision, files=None, task_id=None, cfg=None, writes=None):
    """Add the vNext route on top of decide(): task_id, fast_path, route, sequence (agent/model/effort per step), and the
    model/effort of subagent_type from config/agents.json. Pure (no I/O)."""
    cfg = cfg or _CFG
    d = dict(decision)
    reasons = list(d.get("reasons") or [])
    if writes is None:
        writes = not d.get("parallel_safe", True)
    d["task_id"] = task_id or make_task_id(task)
    fast, checks, failed = fast_path_check(task, d, files, writes)
    d["fast_path"], d["fast_path_checks"] = fast, checks
    plan_path = os.path.join(".jev", "plans", d["task_id"] + ".md")
    if d.get("via") == "workflow":
        d["route"] = "workflow"
        seq = []
    elif fast:
        d["route"] = "fast"
        d.update(tier_fields("builder"))
        d.pop("plan_first", None)
        reasons.append("fast path: single file, no architecture/schema/persistence/public-API/concurrency change, "
                       "requirements clear -> jev-builder directly (no architect)")
        seq = [_step(cfg, "jev-builder", "implement; stop and report invalid_plan / implementation_complexity if not trivial"),
               _step(cfg, "jev-reviewer", "independent review of the diff", optional=True),
               _step(cfg, "jev-qa", "browser QA, only for UI changes", optional=True)]
    elif writes:
        d["route"] = "planned"
        if not d.get("plan_first"):
            d["plan_first"] = {"planner": "jev-architect", "planner_effort": TIERS["architect"]["effort"],
                               "implementer": "jev-builder", "implementer_effort": TIERS["builder"]["effort"],
                               "escalate_if_stuck": ["jev-engineer (medium)", "jev-debugger (high)"]}
        pf = d["plan_first"]
        reasons.append("planned route (fast path failed: %s) -> %s plans, orchestrator persists the plan, %s implements" % (
            ", ".join(failed) or "-", pf["planner"], pf["implementer"]))
        seq = [_step(cfg, pf["planner"], "structured plan (read-only; returns text, never writes files)"),
               {"agent": None, "model": None, "effort": None, "optional": False,
                "purpose": "orchestrator persists the plan: jev.py plan save --task %s --stdin  (-> %s)" % (d["task_id"], plan_path)},
               _step(cfg, pf["implementer"], "implement the persisted plan (fresh context: task + plan + context pack)"),
               _step(cfg, "jev-reviewer", "independent review against the plan's acceptance criteria"),
               _step(cfg, "jev-qa", "verification, when there is a runnable surface", optional=True)]
        d["plan_path"] = plan_path
    else:
        d["route"] = "direct"
        seq = [_step(cfg, d["subagent_type"], "read-only task")] if d.get("subagent_type") else []
    d["sequence"] = seq
    d["allowed_agents"] = sorted({s["agent"] for s in seq if s["agent"]})
    first = next((s for s in seq if s["agent"]), None)
    d["next_agent"] = first["agent"] if first else None
    if cfg and d.get("subagent_type"):
        d["model"] = agent_model(cfg, d["subagent_type"])
        d["effort"] = agent_effort(cfg, d["subagent_type"]) or d.get("effort")
    else:
        d["model"] = d.get("worker_model")
    d["reasons"] = reasons
    d["task_marker"] = "[jev:task=%s]" % d["task_id"]
    return d


def record_route(d):
    """Remember the route per task for the dispatch router (substitution check)."""
    reason = (d.get("reasons") or ["routed"])[-1]
    return update_route(d["task_id"], lambda r: dict(
        r, task_id=d["task_id"], route=d["route"], fast_path=d["fast_path"], tier=d.get("tier"), cwd=os.getcwd(),
        allowed=d["allowed_agents"], sequence=[s["agent"] for s in d["sequence"]], reason=reason[:200],
        escalations=r.get("escalations") or [], dispatches=r.get("dispatches") or 0, routed_at=time.time()))


# ---------- escalate: stuck ladder v2 ----------

CATEGORIES = ("implementation_complexity", "hard_debugging", "invalid_plan", "requirement_ambiguity",
              "environment_failure", "test_failure")
LADDER = ["jev-builder", "jev-engineer", "jev-debugger", "jev-architect"]
CODERS = ("jev-builder", "jev-engineer", "jev-debugger")
DEFAULT_STATE_DIR = os.path.join(".jev", "state")


def norm_role(role):
    role = (role or "").strip()
    if not role or role in ("orchestrator", "stop") or role.startswith("jev-"):
        return role
    return "jev-" + role


def new_state(task_id):
    return {"task_id": task_id, "attempts": 1, "replans": 0, "debugger_attempts": 0, "test_failures": {}, "history": []}


def _step_up(role):
    i = LADDER.index(role) if role in LADDER else -1
    return LADDER[i + 1] if i + 1 < len(LADDER) else "orchestrator"


def escalate_decision(cfg, state, from_role, category, evidence=None):
    """Pure stuck ladder v2. Returns (decision, new_state). Never burns engineer/debugger on an invalid plan."""
    g = cfg.get("guardrails") or {}
    max_replans, max_dbg = int(g.get("max_replans", 2)), int(g.get("max_debugger_attempts", 2))
    table = cfg.get("escalation") or {}
    st = json.loads(json.dumps(state))
    from_role = norm_role(from_role)
    notes, guardrail = [], None
    target = table.get(category)
    action = None
    if category == "requirement_ambiguity":
        nxt, action = "orchestrator", "stop_coding"
        notes.append("stop coding: let jev-analyst / jev-advisor infer the answer only if it is safe, otherwise ask the user")
    elif category == "environment_failure":
        nxt, action = "orchestrator", "fix_environment_or_stop"
        notes.append("fix the environment (deps, services, credentials) or stop; a different agent will not help")
    elif category == "invalid_plan":
        nxt, action = "jev-architect", "replan"
        notes.append("invalid plan / assumption: replan directly, no engineer or debugger attempt")
    elif category == "hard_debugging":
        nxt, action = "jev-debugger", "debug"
    elif category == "implementation_complexity":
        nxt, action = target or "jev-engineer", "implement"
        if from_role in LADDER and LADDER.index(from_role) >= LADDER.index(nxt):
            nxt = _step_up(from_role)
            notes.append("%s already at or above %s on the ladder -> %s" % (from_role, target, nxt))
    elif category == "test_failure":
        coder = from_role if from_role in CODERS else (target or "jev-builder")
        seen = int(st["test_failures"].get(coder, 0))
        if seen >= 2:
            nxt = _step_up(coder)
            notes.append("%s failed tests %d times -> step up the ladder to %s" % (coder, seen + 1, nxt))
        else:
            nxt = coder
            notes.append("simple test failure: %s fixes it (attempt %d of 2 before stepping up)" % (coder, seen + 1))
        st["test_failures"][coder] = seen + 1
        action = "fix_tests"
    else:
        raise ValueError("unknown category %r (use one of %s)" % (category, ", ".join(CATEGORIES)))
    if nxt == "jev-debugger" and st["debugger_attempts"] >= max_dbg:
        guardrail = "max_debugger_attempts (%d) reached" % max_dbg
        nxt, action = "jev-architect", "replan"
    if nxt == "jev-architect" and st["replans"] >= max_replans:
        guardrail = ((guardrail + "; ") if guardrail else "") + "max_replans (%d) reached" % max_replans
        nxt, action = "orchestrator", "stop_report_to_user"
        notes.append("guardrail hit: stop and report to the user with the failure history instead of another attempt")
    if action == "implement" and nxt == "jev-architect":
        action = "replan"
    if nxt == "orchestrator" and action == "implement":
        action = "stop_report_to_user"
    is_agent = nxt in cfg["agents"]
    if is_agent:
        st["attempts"] += 1
    if nxt == "jev-architect":
        st["replans"] += 1
    if nxt == "jev-debugger":
        st["debugger_attempts"] += 1
    rec = {"from": from_role, "to": nxt, "category": category, "evidence": list(evidence or []), "ts": time.time(),
           "guardrail": guardrail}
    st["history"].append(rec)
    d = {"task_id": st["task_id"], "from": from_role, "category": category, "next_agent": nxt,
         "subagent_type": nxt if is_agent else None, "model": agent_model(cfg, nxt) if is_agent else None,
         "effort": agent_effort(cfg, nxt) if is_agent else None, "action": action,
         "attempt": st["attempts"] if is_agent else None, "replans": st["replans"], "debugger_attempts": st["debugger_attempts"],
         "guardrail": guardrail, "notes": notes, "evidence": rec["evidence"]}
    return d, st


def state_path(task_id, state_dir=None):
    return os.path.join(state_dir or DEFAULT_STATE_DIR, task_id + ".json")


def load_state(task_id, state_dir=None):
    try:
        with open(state_path(task_id, state_dir), encoding="utf-8") as f:
            st = json.load(f)
        base = new_state(task_id)
        base.update(st if isinstance(st, dict) else {})
        return base
    except (OSError, ValueError):
        return new_state(task_id)


def cmd_escalate(args):
    try:
        cfg = load_config()
    except ConfigError as e:
        _err_exit({"error": str(e), "fallback": "stop: without config/agents.json the escalation table is unknown"})
    task_id = check_task_id(args.task)
    st = load_state(task_id, args.state_dir)
    d, st = escalate_decision(cfg, st, args.from_role, args.category, args.evidence)
    _atomic_write_json(state_path(task_id, args.state_dir), st)
    d["state_path"] = os.path.abspath(state_path(task_id, args.state_dir))
    if d["subagent_type"]:
        update_route(task_id, lambda r: dict(r, escalations=(r.get("escalations") or []) + [
            {"from": d["from"], "to": d["subagent_type"], "category": d["category"], "ts": time.time()}]))
    fields = {"task_id": task_id, "from": d["from"], "to": d["next_agent"], "failure": d["category"], "model": d["model"],
              "effort": d["effort"], "attempt": d["attempt"], "guardrail": d["guardrail"]}
    d["log"] = jev_line(fields)
    routing_log(dict(fields, event="escalation", action=d["action"], cwd=os.getcwd()))
    print(json.dumps(d, indent=2))


# ---------- plan persistence (the orchestrator writes; the read-only architect never does) ----------

DEFAULT_PLANS_DIR = os.path.join(".jev", "plans")
PLAN_SECTIONS = {
    "objective": r"objective",
    "assumptions": r"assumptions?",
    "constraints": r"constraints?",
    "files": r"(files_to_inspect|likely_files_to_modify|files?\b)",
    "steps": r"(implementation[_ ]steps|steps)",
    "invariants": r"invariants?",
    "acceptance": r"acceptance([_ ]criteria)?",
    "escalation": r"escalation([_ ]conditions)?",
}


def plan_missing_sections(text):
    missing = []
    for name, pat in PLAN_SECTIONS.items():
        if not re.search(r"(?im)^[ \t>]*(?:#{1,6}[ \t]*|[-*][ \t]+|\*\*)?(?:\d+[.)][ \t]*)?" + pat + r"\b", text or ""):
            missing.append(name)
    return missing


def cmd_plan(args):
    task_id = check_task_id(args.task)
    path = os.path.join(args.plans_dir, task_id + ".md")
    if args.plan_cmd == "show":
        if not os.path.isfile(path):
            _err_exit({"error": "no plan for %s at %s" % (task_id, path)})
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if args.raw:
            sys.stdout.write(text)
            return
        print(json.dumps({"task_id": task_id, "path": os.path.abspath(path), "missing_sections": plan_missing_sections(text),
                          "text": text}, indent=2))
        return
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            text = f.read()
    elif args.stdin:
        text = sys.stdin.read()
    else:
        _err_exit({"error": "plan save needs --from-file F or --stdin"})
    if not text.strip():
        _err_exit({"error": "empty plan text; nothing saved"})
    os.makedirs(args.plans_dir, exist_ok=True)
    archived = None
    if os.path.isfile(path):  # a replan: keep the previous version next to it
        n = 1
        while os.path.exists(os.path.join(args.plans_dir, "%s.v%d.md" % (task_id, n))):
            n += 1
        archived = os.path.join(args.plans_dir, "%s.v%d.md" % (task_id, n))
        os.replace(path, archived)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    missing = plan_missing_sections(text)
    warnings = ["plan is missing section: %s" % m for m in missing]
    out = {"ok": True, "task_id": task_id, "path": os.path.abspath(path), "archived_previous": archived and os.path.abspath(archived),
           "missing_sections": missing, "warnings": warnings,
           "next": "hand the builder this path (fresh context: task + plan + context pack); never spawn a coder to save a plan"}
    routing_log({"event": "plan_saved", "task_id": task_id, "path": out["path"], "missing_sections": missing})
    print(json.dumps(out, indent=2))


# ---------- structured handoffs (config/schemas/*.schema.json) ----------

HANDOFF_KINDS = {"plan": ("jev_plan", "jev_plan"), "completion": ("jev_handoff", "jev_handoff"),
                 "failure": ("jev_failure", "jev_failure"), "review": ("review", "review"), "qa": ("qa", "qa")}
_JSON_TYPES = {"string": str, "array": list, "object": dict, "boolean": bool, "null": type(None)}


def _type_ok(value, t):
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[t])


def schema_errors(value, schema, path="$"):
    """Validate the JSON Schema subset our schemas use (type, required, properties, additionalProperties, items, enum,
    const, minItems, minLength, pattern). Stdlib only."""
    errs = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return ["%s: expected %s, got %s" % (path, " or ".join(types), type(value).__name__)]
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r is not one of %s" % (path, value, schema["enum"]))
    if "const" in schema and value != schema["const"]:
        errs.append("%s: must be %r" % (path, schema["const"]))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errs.append("%s: must not be empty" % path)
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append("%s: %r does not match %s" % (path, value, schema["pattern"]))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append("%s: needs at least %d item(s)" % (path, schema["minItems"]))
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs += schema_errors(item, schema["items"], "%s[%d]" % (path, i))
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append("%s: missing required field %r" % (path, k))
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                errs.append("%s: unexpected field %r" % (path, k))
    return errs


def load_schema(kind):
    name = HANDOFF_KINDS[kind][0]
    with open(os.path.join(SCHEMA_DIR, name + ".schema.json"), encoding="utf-8") as f:
        return json.load(f)


def load_artifact(text):
    """JSON, or YAML when PyYAML is installed."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    if _yaml is None:
        raise ValueError("not JSON, and PyYAML is not installed to read YAML (pip install pyyaml, or write JSON)")
    try:
        return _yaml.safe_load(text)
    except Exception as e:
        raise ValueError("not valid JSON or YAML: %s" % str(e).replace("\n", " ")[:200])


def validate_handoff(kind, doc):
    """Returns (errors, warnings, unwrapped document)."""
    root = HANDOFF_KINDS[kind][1]
    if isinstance(doc, dict) and len(doc) == 1 and root in doc:
        doc = doc[root]
    errors = schema_errors(doc, load_schema(kind))
    warnings = []
    if not errors and kind == "review":
        f = doc["findings"]
        if doc["verdict"] == "pass" and (f.get("critical") or f.get("major")):
            errors.append("$.verdict: pass is not allowed with critical or major findings")
        if doc["verdict"] == "pass" and any(a.get("status") == "fail" for a in doc.get("acceptance_criteria") or []):
            errors.append("$.verdict: pass is not allowed while an acceptance criterion fails")
        if doc["verdict"] == "changes_required" and not doc.get("required_changes"):
            warnings.append("changes_required without required_changes: the worker won't know what to fix")
    if not errors and kind == "failure" and doc["category"] == "invalid_plan" and doc["recommended_route"]["agent"] != "jev-architect":
        warnings.append("invalid_plan should route to jev-architect (replan), not %s" % doc["recommended_route"]["agent"])
    return errors, warnings, doc


HANDOFF_TEMPLATES = {
    "plan": {"jev_plan": {
        "task_id": "TASK-123", "objective": "One sentence: what is true when this is done.",
        "assumptions": ["Fact about the repo the plan relies on (verified or marked as unverified)."],
        "constraints": ["No save format break.", "No new runtime dependency."],
        "files_to_inspect": ["src/module/File.kt"], "likely_files_to_modify": ["src/module/File.kt"],
        "implementation_steps": ["Step 1: concrete change, with the function/type it touches."],
        "invariants": ["Property that must hold before and after the change."],
        "acceptance_criteria": ["Observable check the reviewer and QA can verify."],
        "escalation_conditions": ["Situation in which the builder must stop and report invalid_plan."]}},
    "completion": {"jev_handoff": {
        "task_id": "TASK-123", "role": "jev-builder", "status": "completed", "files_changed": ["src/module/File.kt"],
        "implementation_summary": ["What changed, one line each."], "decisions": ["Choice made inside the plan's scope."],
        "tests_run": ["./gradlew test"], "test_results": ["PASS"], "unresolved": [], "risks": ["Remaining risk."],
        "next_recommended_agent": "jev-reviewer"}},
    "failure": {"jev_failure": {
        "task_id": "TASK-123", "agent": "jev-builder", "category": "implementation_complexity",
        "completed": ["What is already done."], "blocked_by": ["The concrete blocker."],
        "recommended_route": {"agent": "jev-engineer", "reason": "Why that agent."}, "evidence": ["src/module/File.kt:42"]}},
    "review": {"review": {
        "verdict": "changes_required", "findings": {"critical": [], "major": ["path:line - what breaks and when"], "minor": []},
        "acceptance_criteria": [{"criterion": "Criterion from the plan.", "status": "pass"}],
        "regression_risks": ["Dependent that may break."], "required_changes": ["What the worker must change."]}},
    "qa": {"qa": {
        "task_id": "TASK-123", "verdict": "pass", "target": "http://localhost:3000",
        "checks": [{"name": "home renders", "status": "pass", "evidence": ".jev/qa/run/home-desktop-sheet.jpg"}],
        "findings": [], "out_dir": ".jev/qa/run"}},
}


def _to_yaml(value, indent=0):
    """Tiny YAML emitter for templates (dicts, lists, strings), so templates work without PyYAML."""
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append("%s%s:" % (pad, k))
                lines.append(_to_yaml(v, indent + 1))
            else:
                lines.append("%s%s: %s" % (pad, k, json.dumps(v) if not isinstance(v, (dict, list)) else ("{}" if isinstance(v, dict) else "[]")))
    else:
        for item in value:
            if isinstance(item, dict):
                sub = _to_yaml(item, indent + 1).split("\n")
                lines.append("%s- %s" % (pad, sub[0].strip()))
                lines.extend(sub[1:])
            else:
                lines.append("%s- %s" % (pad, json.dumps(item)))
    return "\n".join(lines)


def cmd_handoff(args):
    if args.handoff_cmd == "template":
        t = HANDOFF_TEMPLATES[args.kind]
        print(json.dumps(t, indent=2) if args.format == "json" else _to_yaml(t))
        return
    try:
        with open(args.file, encoding="utf-8") as f:
            doc = load_artifact(f.read())
        errors, warnings, _ = validate_handoff(args.kind, doc)
    except (OSError, ValueError) as e:
        errors, warnings = [str(e)], []
    out = {"ok": not errors, "kind": args.kind, "file": args.file, "schema": HANDOFF_KINDS[args.kind][0] + ".schema.json",
           "errors": errors, "warnings": warnings}
    print(json.dumps(out, indent=2))
    if errors:
        sys.exit(1)


# ---------- terse report protocol ----------

REPORT_SECTIONS = {"DONE": ["STATUS", "CHANGED", "WHY", "TEST", "RISK", "NEXT"],
                   "BLOCKED": ["STATUS", "CATEGORY", "FOUND", "EVIDENCE", "NEXT"]}
REPORT_HEADER = re.compile(r"^\s*([A-Z][A-Z_]+):\s*(.*?)\s*$")
REPORT_MAX_LINES = 60
REPORT_MAX_ITEM = 240


def lint_report(text):
    sections, order, cur = {}, [], None
    for line in (text or "").splitlines():
        m = REPORT_HEADER.match(line)
        if m and m.group(1) in ("STATUS", "CHANGED", "WHY", "TEST", "RISK", "NEXT", "CATEGORY", "FOUND", "EVIDENCE"):
            cur = m.group(1)
            order.append(cur)
            sections.setdefault(cur, [])
            if m.group(2):
                sections[cur].append(m.group(2))
        elif cur and line.strip():
            sections[cur].append(re.sub(r"^\s*[-*]\s*", "", line).strip())
    errors, warnings = [], []
    status = (sections.get("STATUS") or [""])[0].strip().upper()
    if status not in REPORT_SECTIONS:
        errors.append("STATUS must be DONE or BLOCKED, got %r" % (status or None))
        need = []
    else:
        need = REPORT_SECTIONS[status]
    for s in need:
        if s not in sections:
            errors.append("missing %s:" % s)
        elif not [x for x in sections[s] if x]:
            errors.append("%s: is empty (write '- none' if there is nothing)" % s)
    dup = sorted({s for s in order if order.count(s) > 1})
    if dup:
        errors.append("repeated section(s): %s" % ", ".join(dup))
    if status == "BLOCKED":
        cats = [c.strip().lower() for c in sections.get("CATEGORY") or []]
        bad = [c for c in cats if c not in CATEGORIES]
        if bad:
            errors.append("CATEGORY %s not one of %s" % (", ".join(bad), ", ".join(CATEGORIES)))
    extra = [s for s in sections if need and s not in need]
    if extra:
        warnings.append("sections not used by a %s report: %s" % (status, ", ".join(extra)))
    n_lines = len([l for l in (text or "").splitlines() if l.strip()])
    if n_lines > REPORT_MAX_LINES:
        warnings.append("%d lines: the report protocol is terse (aim for under %d)" % (n_lines, REPORT_MAX_LINES))
    long_items = [x for v in sections.values() for x in v if len(x) > REPORT_MAX_ITEM]
    if long_items:
        warnings.append("%d item(s) longer than %d chars: one fact per line" % (len(long_items), REPORT_MAX_ITEM))
    return {"ok": not errors, "status": status or None, "errors": errors, "warnings": warnings,
            "sections": {k: v for k, v in sections.items()}}


def cmd_report_lint(args):
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    res = lint_report(text)
    print(json.dumps(res, indent=2))
    if not res["ok"]:
        sys.exit(1)


# ---------- context: thin wrapper around jevctx.py (graph-first context packs) ----------

def jevctx_path():
    return os.environ.get("JEV_CTX") or os.path.join(os.path.dirname(os.path.realpath(__file__)), "jevctx.py")


def run_context(argv, timeout=180):
    """Run `jevctx.py <argv> --json`. Fails soft: any problem returns graph_status=unavailable (never raises)."""
    argv = list(argv)
    if "--json" not in argv:
        argv.append("--json")
    soft = {"pack_path": None, "context_block": "", "graph_status": "unavailable", "fallback": "targeted_file_search",
            "reused": False}
    path = jevctx_path()
    if not os.path.isfile(path):
        return dict(soft, error="jevctx.py not found at %s" % path)
    try:
        import subprocess
        p = subprocess.run([sys.executable, path] + argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return dict(soft, error="jevctx.py failed to run: %s" % e)
    try:
        out = json.loads(p.stdout)
        if not isinstance(out, dict):
            raise ValueError("not an object")
    except ValueError:
        return dict(soft, error="jevctx.py exit %d, no JSON output: %s" % (p.returncode, (p.stderr or p.stdout)[-300:].strip()))
    if p.returncode != 0 and "graph_status" not in out:
        return dict(soft, error="jevctx.py exit %d: %s" % (p.returncode, out.get("error") or (p.stderr or "")[-300:]))
    return out


def _argv_value(argv, flag):
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def cmd_context(args):
    argv = [args.action] + list(args.rest or [])
    out = run_context(argv)
    fields = {"task_id": _argv_value(argv, "--task-id"), "role": _argv_value(argv, "--role"),
              "graph_status": out.get("graph_status"), "fallback": out.get("fallback"), "reused": out.get("reused"),
              "reason": out.get("error") or out.get("reason")}
    routing_log(dict(fields, event="context", pack_path=out.get("pack_path")))
    if (out.get("fallback") or "none") != "none" or out.get("error"):
        sys.stderr.write(jev_line(fields, "[JEV CONTEXT]") + "\n")
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("route"); p.add_argument("task"); p.add_argument("--context")
    p.add_argument("--task-id", help="stable id for this task (default: T-<hash of the task text>)")
    p.add_argument("--files", help="comma-separated files the task is expected to touch (helps the fast-path check)")
    p.set_defaults(fn=cmd_route)
    p = sub.add_parser("dedupe"); p.add_argument("subgoal"); p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--threshold", type=float, default=0.6); p.set_defaults(fn=cmd_dedupe)
    p = sub.add_parser("done"); p.add_argument("id"); p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.set_defaults(fn=cmd_done)
    p = sub.add_parser("list"); p.add_argument("--ledger", default=DEFAULT_LEDGER); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("report"); p.add_argument("action", nargs="?", help="'lint' to check a terse agent report")
    p.add_argument("file", nargs="?"); p.add_argument("--days", type=float, default=7)
    p.add_argument("--log-dir", default=jevlib.LOG_DIR); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("outcomes"); p.add_argument("--days", type=float, default=7)
    p.add_argument("--log-dir", default=jevlib.LOG_DIR); p.add_argument("--projects-dir", default=PROJECTS_DIR)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_outcomes)
    p = sub.add_parser("label"); p.add_argument("id", help="tool_use_id or 'last'"); p.add_argument("label", choices=LABELS)
    p.add_argument("--note"); p.add_argument("--log-dir", default=jevlib.LOG_DIR); p.set_defaults(fn=cmd_label)
    p = sub.add_parser("stuck"); p.add_argument("--state", required=True)
    p.add_argument("--tier", default="builder", choices=list(STUCK_ESCALATE)); p.set_defaults(fn=cmd_stuck)
    p = sub.add_parser("report-lint"); p.add_argument("file"); p.set_defaults(fn=cmd_report_lint)
    p = sub.add_parser("preflight"); p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="check every configured agent, not only required_agents")
    p.add_argument("--agent", action="append", help="check only this agent (repeatable)")
    p.add_argument("--agents-dir", help="default ~/.claude/agents (env JEV_AGENTS_DIR)"); p.set_defaults(fn=cmd_preflight)
    p = sub.add_parser("escalate"); p.add_argument("--task", required=True)
    p.add_argument("--from", dest="from_role", required=True, help="role that failed, e.g. jev-builder or builder")
    p.add_argument("--category", required=True, choices=CATEGORIES)
    p.add_argument("--evidence", nargs="*", default=[]); p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.set_defaults(fn=cmd_escalate)
    p = sub.add_parser("plan"); psub = p.add_subparsers(dest="plan_cmd", required=True)
    for name in ("save", "show"):
        q = psub.add_parser(name); q.add_argument("--task", required=True); q.add_argument("--plans-dir", default=DEFAULT_PLANS_DIR)
        if name == "save":
            g = q.add_mutually_exclusive_group(); g.add_argument("--from-file"); g.add_argument("--stdin", action="store_true")
        else:
            q.add_argument("--raw", action="store_true", help="print the markdown instead of JSON")
        q.set_defaults(fn=cmd_plan)
    p = sub.add_parser("handoff"); hsub = p.add_subparsers(dest="handoff_cmd", required=True)
    q = hsub.add_parser("validate"); q.add_argument("--kind", required=True, choices=list(HANDOFF_KINDS)); q.add_argument("file")
    q.set_defaults(fn=cmd_handoff)
    q = hsub.add_parser("template"); q.add_argument("--kind", required=True, choices=list(HANDOFF_KINDS))
    q.add_argument("--format", choices=("yaml", "json"), default="yaml"); q.set_defaults(fn=cmd_handoff)
    p = sub.add_parser("context", help="graph-first context via jevctx.py, e.g. context prepare --task-id T --role R --task TEXT")
    p.add_argument("action"); p.add_argument("rest", nargs=argparse.REMAINDER); p.set_defaults(fn=cmd_context)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
