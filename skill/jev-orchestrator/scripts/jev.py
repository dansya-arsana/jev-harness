#!/usr/bin/env python3
"""Jev decision helper for delegating work to subagents.

Commands:
  route  TASK [--context TEXT]    -> delegate TASK? to which tier / effort / ladder, in parallel?
  dedupe SUBGOAL [--ledger PATH]  -> is SUBGOAL already done or in flight? registers it if new
  done   ID [--ledger PATH]       -> mark a subgoal finished
  list   [--ledger PATH]          -> show the subgoal ledger
  stuck  --state TEXT|@FILE [--tier T] -> is the agent stuck? recommends escalation on T's own ladder
  report [--days N] [--json]      -> summarize gate / prompt-router / routing logs for tuning

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

# Tier -> subagent defined in ~/.claude/agents/ (all pin claude-opus-5-5; tiers differ by effort).
# ultracode is not a subagent: the main agent orchestrates the task with the Workflow tool.
TIERS = {
    "scout":     {"subagent_type": "jev-scout",     "effort": "low",    "ladder": "read"},
    "analyst":   {"subagent_type": "jev-analyst",   "effort": "high",   "ladder": "read"},
    "builder":   {"subagent_type": "jev-builder",   "effort": "medium", "ladder": "write"},
    "engineer":  {"subagent_type": "jev-engineer",  "effort": "high",   "ladder": "write"},
    "debugger":  {"subagent_type": "jev-debugger",  "effort": "xhigh",  "ladder": "write"},
    "architect": {"subagent_type": "jev-architect", "effort": "max",    "ladder": "write"},
    "reviewer":  {"subagent_type": "jev-reviewer",  "effort": "medium", "ladder": "read"},
    "advisor":   {"subagent_type": "jev-advisor",   "effort": "max",    "ladder": "read"},
    "ultracode": {"subagent_type": None,            "effort": None,     "ladder": "orchestrate"},
}
# One step up, always on the tier's own ladder (read-only tiers never escalate to a write agent).
ESCALATE = {
    "scout": "analyst", "analyst": "analyst",
    "builder": "engineer", "engineer": "debugger", "debugger": "architect", "architect": "architect",
    "reviewer": "analyst", "advisor": "advisor",
}
# When stuck at the top of the write ladder, a single context is not enough: orchestrate instead.
STUCK_ESCALATE = dict(ESCALATE, architect="ultracode", ultracode="ultracode")

# Thresholds (policy lives in code; Jev only answers narrow questions).
BREADTH_ULTRACODE = 2.5
DEPTH_DEEP_READ = 1.5
DEPTH_ENGINEER = 1.5
DEPTH_DESIGN = 2.0
DEPTH_HARD = 2.5
P_YES = 0.6
STAKES_MAX = 0.85  # only clear-cut high stakes justify architect/max; measured: wrong picks 0.61-0.76, right >= 0.87
P_STRONG = 0.8  # design must be clear-cut to justify max effort; moderate design work goes to engineer
LOW_CONFIDENCE = 0.5
ADVICE_READ_ONLY = 0.85  # design + clearly read-only = advice, not implementation
READ_ONLY = 0.6  # lean read-only: a read agent that needed to write fails safely and gets rerouted


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
        reasons.append("design question that only needs a recommendation -> advisor (read-only, max)")
    elif not writes:
        base = "scout" if depth < DEPTH_DEEP_READ else "analyst"
        if base == "analyst":
            reasons.append("read-only, deeper than a lookup -> analyst")
    elif p["unknown_cause"] >= P_YES:
        # Finding an unknown cause is debugging, whatever the stakes; debugger already runs at xhigh.
        base = "debugger"
        reasons.append("unknown cause -> debugger")
    elif design_task and (p["design"] >= P_STRONG or depth >= DEPTH_HARD):
        base = "architect"
        reasons.append("design decision -> architect")
    elif risky_write and p["high_stakes"] >= STAKES_MAX and depth >= DEPTH_ENGINEER:
        base = "architect"
        reasons.append("high-stakes change with a known approach -> architect")
    elif risky_write:
        base = "engineer"
        reasons.append("moderately high-stakes or small edit -> engineer (high effort)")
    elif depth >= DEPTH_HARD:
        base = "debugger"
        reasons.append("hard edit -> debugger")
    elif depth >= DEPTH_ENGINEER:
        base = "engineer"
        reasons.append("multi-part edit -> engineer")
    else:
        base = "builder"
    if depth_conf < LOW_CONFIDENCE and ESCALATE[base] != base:
        reasons.append("low confidence on depth -> %s escalated to %s" % (base, ESCALATE[base]))
        base = ESCALATE[base]

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
        d.update(delegate=True, via="workflow", effort=TIERS[base]["effort"], worker_tier=base,
                 workflow_shape=shape, workflow_hint=SHAPES[shape],
                 instruction="Delegate via the Workflow tool (load the workflow-authoring skill first); "
                             "pass opts.effort=%r and opts.model='claude-opus-5-5' to every agent() call "
                             "(never an alias: settings can remap opus/sonnet/haiku)." % TIERS[base]["effort"])
        return d, reasons, writes

    if not self_contained:
        reasons.append("not self-contained -> keep in main agent, or pass the needed context in the prompt")
    d = tier_fields(base)
    d.update(delegate=self_contained, via="subagent" if self_contained else "main")
    return d, reasons, writes


# ---------- route ----------

def cmd_route(args):
    state = {"task": jevlib.redact(args.task), "context_from_main_agent": jevlib.redact(args.context or "")}
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
    try:
        r = jevlib.ask(state, questions)
        a = r["answers"]
        depth, depth_conf = a["depth"]["score"], a["depth"]["confidence"]
        breadth = a["breadth"]["score"]
        p = {k: a[k]["noul"] for k in ("self_contained", "read_only", "high_stakes", "unknown_cause", "design", "batchable", "exhaustive")}
    except (jevlib.JevError, KeyError, TypeError) as e:
        fail("Jev unavailable: %s" % e, "route with your own judgment and say Jev was unavailable")

    decision, reasons, writes = decide(args.task, depth, depth_conf, breadth, p)
    decision.update({
        "parallel_safe": not writes,
        "depth": round(depth, 2),
        "breadth": round(breadth, 2),
        "depth_confidence": round(depth_conf, 2),
        "signals": {k: round(v, 2) for k, v in p.items()},
        "reasons": reasons,
        "jev_tokens": r.get("usage", {}).get("input_tokens"),
    })
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
    return {"days": days, "log_dir": log_dir, "gate": gate, "router": router, "orchestrator": orch, "look_at": look}


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
    print("\nWORTH A LOOK")
    for line in rep["look_at"] or ["nothing flagged"]:
        print("  - " + line)


def cmd_report(args):
    rep = build_report(os.path.expanduser(args.log_dir), args.days)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print_report(rep)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("route"); p.add_argument("task"); p.add_argument("--context")
    p.set_defaults(fn=cmd_route)
    p = sub.add_parser("dedupe"); p.add_argument("subgoal"); p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--threshold", type=float, default=0.6); p.set_defaults(fn=cmd_dedupe)
    p = sub.add_parser("done"); p.add_argument("id"); p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.set_defaults(fn=cmd_done)
    p = sub.add_parser("list"); p.add_argument("--ledger", default=DEFAULT_LEDGER); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("report"); p.add_argument("--days", type=float, default=7)
    p.add_argument("--log-dir", default=jevlib.LOG_DIR); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("stuck"); p.add_argument("--state", required=True)
    p.add_argument("--tier", default="builder", choices=list(STUCK_ESCALATE)); p.set_defaults(fn=cmd_stuck)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
