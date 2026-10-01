#!/usr/bin/env python3
"""Labeled evaluations for the Jev harness. Nothing here executes the evaluated commands.

  python3 evals/run_evals.py gate     permission gate on evals/gate_cases.jsonl
  python3 evals/run_evals.py route    jev.py route on evals/route_cases.jsonl
  python3 evals/run_evals.py router   prompt router (conditions + skills) on evals/router_cases.jsonl
  python3 evals/run_evals.py route-fastpath   vNext fast path vs planned route on evals/route_fastpath_cases.jsonl
  python3 evals/run_evals.py all

Results go to evals/results/<name>.json. Gate cases are piped to the hook as JSON only. A script a case
refers to is written into a temp dir so the gate can read it; it is never run.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL = os.path.join(REPO, "skill", "jev-orchestrator")
GATE = os.path.join(SKILL, "hooks", "permission_gate.py")
ROUTER = os.path.join(SKILL, "hooks", "prompt_router.py")
JEV = os.path.join(SKILL, "scripts", "jev.py")
RESULTS = os.path.join(HERE, "results")
WORKERS = 4


def load(name):
    with open(os.path.join(HERE, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))] if v else None


def save(name, data):
    os.makedirs(RESULTS, exist_ok=True)
    data["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(RESULTS, name + ".json"), "w") as f:
        json.dump(data, f, indent=2)


def pmap(fn, items):
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        return list(ex.map(fn, items))


# ---------- gate ----------

SCRIPT_ARG = re.compile(r"(?:python3?|node|bash|sh)\s+(\S+\.(?:py|js|sh))")


def gate_case(case):
    cwd = tempfile.mkdtemp(prefix="jev-eval-gate-")
    if case.get("script"):
        rel = SCRIPT_ARG.search(case["cmd"]).group(1)
        os.makedirs(os.path.join(cwd, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(cwd, rel), "w") as f:
            f.write(case["script"])
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": case["cmd"]},
               "cwd": cwd, "permission_mode": "bypassPermissions"}
    t = time.time()
    p = subprocess.run([sys.executable, GATE], input=json.dumps(payload), capture_output=True, text=True)
    ms = int((time.time() - t) * 1000)
    out = p.stdout.strip()
    got = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "pass"
    return dict(case, got=got, ok=got in case["want"].split("|"), ms=ms, exit=p.returncode,
                jev=ms > 400, script=bool(case.get("script")))


def eval_gate(cases="gate_cases.jsonl", out="gate"):
    rows = pmap(gate_case, load(cases))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"n": 0, "ok": 0})
        k["n"] += 1
        k["ok"] += r["ok"]
    dangerous = [r for r in rows if r["kind"] in ("exfil", "exfil-script", "remote-code", "destructive", "security", "harmful")]
    benign = [r for r in rows if r["kind"] in ("routine", "gray-benign")]
    summary = {
        "cases": len(rows),
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 3),
        "dangerous_blocked": "%d/%d" % (sum(r["got"] in ("deny", "ask") for r in dangerous), len(dangerous)),
        "dangerous_denied": "%d/%d" % (sum(r["got"] == "deny" for r in dangerous), len(dangerous)),
        "benign_false_deny": "%d/%d" % (sum(r["got"] == "deny" for r in benign), len(benign)),
        "routine_silent": "%d/%d" % (sum(r["got"] == "pass" for r in rows if r["kind"] == "routine"),
                                     sum(1 for r in rows if r["kind"] == "routine")),
        "nonzero_exits": sum(r["exit"] != 0 for r in rows),
        "latency_ms_rules": {"p50": pct([r["ms"] for r in rows if not r["jev"]], .5),
                             "p95": pct([r["ms"] for r in rows if not r["jev"]], .95)},
        "latency_ms_jev": {"p50": pct([r["ms"] for r in rows if r["jev"]], .5),
                           "p95": pct([r["ms"] for r in rows if r["jev"]], .95), "n": sum(r["jev"] for r in rows)},
        "by_kind": by_kind,
        "misses": [{"cmd": r["cmd"][:90], "want": r["want"], "got": r["got"]} for r in rows if not r["ok"]],
    }
    save(out, {"summary": summary, "rows": rows})
    return summary


# ---------- route ----------

def route_case(case):
    t = time.time()
    # JEV_HOME keeps eval routes out of the real ~/.claude/jev/last_route.json (the dispatch guard reads it)
    env = dict(os.environ, JEV_HOME=os.path.join(tempfile.gettempdir(), "jev-eval-home"))
    p = subprocess.run([sys.executable, JEV, "route", case["task"]], capture_output=True, text=True, env=env)
    ms = int((time.time() - t) * 1000)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return dict(case, error=p.stdout[:200] + p.stderr[:200], ok=False, ladder_ok=False, ms=ms)
    tier = "main" if d.get("via") == "main" else d.get("tier")
    # advisor is architect-level effort without edit tools: it satisfies an "architect" label for advice questions
    if tier == "advisor" and "architect" in case["ok"]:
        tier = "architect"
    got_ladder = d.get("ladder")
    ok = tier in case["ok"]
    # Tier choice judged on its own: a task kept in the main session still gets a tier and effort.
    tier_ok = tier in case["ok"] or d.get("tier") in case["ok"]
    if case["ladder"] == "any":
        ladder_ok = True
    elif case["ladder"] == "read":
        ladder_ok = got_ladder in ("read", "orchestrate") or tier == "main"
    else:
        ladder_ok = got_ladder in ("write", "orchestrate") or tier == "main"
    row = dict(case, acceptable=case["ok"], got=tier, got_tier=d.get("tier"), via=d.get("via"), effort=d.get("effort"), ladder_got=got_ladder,
               ok=ok, tier_ok=tier_ok, ladder_ok=ladder_ok, ms=ms, depth=d.get("depth"), breadth=d.get("breadth"),
               signals=d.get("signals"), reasons=d.get("reasons"), route=d.get("route"), fast_path_got=d.get("fast_path"),
               next_agent=d.get("next_agent"))
    if "fast_path" in case:  # vNext: fast path (builder directly) vs planned route (architect first)
        row["fast_path_ok"] = d.get("fast_path") == case["fast_path"]
        row["ok"] = row["fast_path_ok"]
    return row


def eval_route(cases="route_cases.jsonl", out="route"):
    rows = pmap(route_case, load(cases))
    read_rows = [r for r in rows if r["ladder"] == "read"]
    summary = {
        "cases": len(rows),
        "tier_acceptable": "%d/%d" % (sum(r["ok"] for r in rows), len(rows)),
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 3),
        "tier_choice_acceptable": "%d/%d" % (sum(r.get("tier_ok", False) for r in rows), len(rows)),
        "kept_in_main": sum(r.get("via") == "main" for r in rows),
        "read_tasks_given_edit_tools": "%d/%d" % (sum(r.get("ladder_got") == "write" and r.get("via") != "main"
                                                      for r in read_rows), len(read_rows)),
        "ladder_violations": sum(not r["ladder_ok"] for r in rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "fast_path": ("%d/%d" % (sum(r.get("fast_path_ok", False) for r in rows), sum("fast_path" in r for r in rows))
                      if any("fast_path" in r for r in rows) else None),
        "fast_path_false_positives": sum(1 for r in rows if r.get("fast_path") is False and r.get("fast_path_got")),
        "latency_ms": {"p50": pct([r["ms"] for r in rows], .5), "p95": pct([r["ms"] for r in rows], .95)},
        "misses": [{"task": r["task"][:80], "want": r["ok"] if isinstance(r["ok"], list) else None,
                    "acceptable": [c for c in load(cases) if c["task"] == r["task"]][0]["ok"],
                    "got": r.get("got"), "effort": r.get("effort")} for r in rows if not r["ok"]],
    }
    save(out, {"summary": summary, "rows": rows})
    return summary


# ---------- router ----------

EVAL_CONDITIONS = [
    {"id": "media-tools", "threshold": 0.6,
     "when": "Does `user_request` ask for AI-generated or AI-edited media: sound (music, sound effects, voice or text-to-speech) or images, video, or 3D assets?",
     "yes": "It asks to create or edit media with a generative AI tool.",
     "no": "No generative media: ordinary code, charts drawn by code, UI work, shaders written in code, or discussing existing assets.",
     "inject": "Media tools: use AudioTool for all sound and VisualTool for images, video and 3D."},
    {"id": "staging-deploy", "threshold": 0.6,
     "when": "Does `user_request` involve deploying to, updating, or debugging the staging server?",
     "yes": "It is about deploying or operating staging.", "no": "It is not about staging deploys.",
     "inject": "Staging runbook: test the proxy config before reloading it."},
]


def router_case(args):
    case, cond_path, cwd = args
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": case["prompt"], "cwd": cwd,
               "session_id": "eval", "transcript_path": "/dev/null"}
    env = dict(os.environ, JEV_ROUTER_CONDITIONS=cond_path, JEV_ROUTER_LOG="prompts_eval")
    env.pop("JEV_ROUTER", None)
    t = time.time()
    p = subprocess.run([sys.executable, ROUTER], input=json.dumps(payload), capture_output=True, text=True, env=env)
    ms = int((time.time() - t) * 1000)
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""
    fired = sorted(re.findall(r'<conditional_instruction id="([^"]+)">', ctx))
    m = re.search(r"Relevant to the current request: ([^\s.]+)\.", ctx)
    skill = m.group(1) if m else None
    row = dict(case, fired=fired, suggested=skill, ms=ms)
    if "fire" in case:
        row["ok"] = fired == sorted(case["fire"])
    if "skill" in case:
        want = case["skill"]
        row["ok"] = (skill is None) if want == "none" else bool(skill and re.search(want, skill)) or \
            (skill is None and "none" in want.split("|"))
    return row


def eval_router():
    tmp = tempfile.mkdtemp(prefix="jev-eval-router-")
    cond_path = os.path.join(tmp, "conditions.json")
    with open(cond_path, "w") as f:
        json.dump(EVAL_CONDITIONS, f)
    cwd = os.path.join(tmp, "workspace")
    os.makedirs(cwd)
    rows = pmap(router_case, [(c, cond_path, cwd) for c in load("router_cases.jsonl")])
    cond = [r for r in rows if "fire" in r]
    tp = sum(len(set(r["fired"]) & set(r["fire"])) for r in cond)
    fp = sum(len(set(r["fired"]) - set(r["fire"])) for r in cond)
    fn = sum(len(set(r["fire"]) - set(r["fired"])) for r in cond)
    skills = [r for r in rows if "skill" in r]
    summary = {
        "condition_prompts": len(cond),
        "condition_exact": "%d/%d" % (sum(r["ok"] for r in cond), len(cond)),
        "condition_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "condition_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "skill_prompts": len(skills),
        "skill_correct": "%d/%d" % (sum(r["ok"] for r in skills), len(skills)),
        "latency_ms": {"p50": pct([r["ms"] for r in rows], .5), "p95": pct([r["ms"] for r in rows], .95),
                       "max": max(r["ms"] for r in rows)},
        "misses": [{"prompt": r["prompt"][:80], "want": r.get("fire", r.get("skill")),
                    "got": r["fired"] if "fire" in r else r["suggested"]} for r in rows if not r["ok"]],
        "skill_rows": [{"prompt": r["prompt"][:70], "want": r["skill"], "suggested": r["suggested"], "ok": r["ok"]}
                       for r in rows if "skill" in r],
    }
    save("router", {"summary": summary, "rows": rows})
    return summary


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    suites = {"gate": eval_gate, "route": eval_route, "router": eval_router,
              "gate-heldout": lambda: eval_gate("gate_cases_heldout.jsonl", "gate_heldout"),
              "gate-heldout2": lambda: eval_gate("gate_cases_heldout2.jsonl", "gate_heldout2"),
              "route-heldout": lambda: eval_route("route_cases_heldout.jsonl", "route_heldout"),
              "route-heldout2": lambda: eval_route("route_cases_heldout2.jsonl", "route_heldout2"),
              "route-heldout3": lambda: eval_route("route_cases_heldout3.jsonl", "route_heldout3"),
              "route-fastpath": lambda: eval_route("route_fastpath_cases.jsonl", "route_fastpath")}
    for name in (["gate", "route", "router"] if which == "all" else [which]):
        t = time.time()
        s = suites[name]()
        print("== %s (%.0f s)" % (name, time.time() - t))
        print(json.dumps(s, indent=2))


if __name__ == "__main__":
    main()
