#!/usr/bin/env python3
"""Offline tests for the JEV vNext pieces of jev.py: preflight, fast path vs planned route, stuck ladder v2 (escalate),
plan persistence, structured handoffs, the terse report protocol and the jevctx wrapper. No Jev calls.

Run:  python3 skill/jev-orchestrator/scripts/tests/test_vnext.py -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPTS)))
REPO_AGENTS = os.path.join(REPO, "agents")
JEV = os.path.join(SCRIPTS, "jev.py")
sys.path.insert(0, SCRIPTS)
import jev  # noqa: E402

CFG = jev.load_config()
TMP = tempfile.mkdtemp(prefix="jev-vnext-test-")


def run(args, cwd=None, env=None, stdin=None):
    e = dict(os.environ, JEV_HOME=os.path.join(TMP, "home"))
    e.pop("JEV_AGENTS_DIR", None)
    e.update(env or {})
    p = subprocess.run([sys.executable, JEV] + args, capture_output=True, text=True, cwd=cwd or TMP, env=e, input=stdin,
                       timeout=60)
    return p


def agents_copy(**edits):
    """Copy the repo agents into a temp dir; edits: name -> fn(text) -> text (None deletes the file)."""
    d = tempfile.mkdtemp(dir=TMP)
    for f in os.listdir(REPO_AGENTS):
        if f.endswith(".md"):
            shutil.copy(os.path.join(REPO_AGENTS, f), d)
    for name, fn in edits.items():
        path = os.path.join(d, name.replace("_", "-") + ".md")
        if fn is None:
            os.remove(path)
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(fn(text))
    return d


def set_line(key, value):
    def fn(text):
        lines = text.split("\n")
        return "\n".join(("%s: %s" % (key, value)) if l.startswith(key + ":") else l for l in lines)
    return fn


class Frontmatter(unittest.TestCase):
    def test_unquoted_colon_rejected_without_yaml(self):
        text = "---\nname: jev-x\ndescription: Default coder (low effort): small edits\nmodel: m\n---\nbody\n"
        fields, body, errs = jev.parse_frontmatter(text, use_yaml=False)
        self.assertTrue(any("': '" in e for e in errs), errs)

    def test_unquoted_hash_rejected(self):
        _, _, errs = jev.parse_frontmatter("---\nname: a\ndescription: fix #1 issue #2\n---\nb\n", use_yaml=False)
        self.assertTrue(any("' #'" in e for e in errs), errs)

    def test_quoted_ok_and_unescaped(self):
        fields, body, errs = jev.parse_frontmatter('---\nname: a\ndescription: "x: y \\"q\\" # z"\ntools: Read, Edit\n---\nbody\n',
                                                   use_yaml=False)
        self.assertEqual(errs, [])
        self.assertEqual(fields["description"], 'x: y "q" # z')
        self.assertEqual(jev.parse_tools(fields["tools"]), ["Read", "Edit"])

    def test_yaml_second_opinion(self):
        if jev._yaml is None:
            self.skipTest("PyYAML not installed")
        _, _, errs = jev.parse_frontmatter("---\nname: a\ndescription: \"unterminated\n---\nb\n")
        self.assertTrue(errs)

    def test_no_frontmatter(self):
        fields, _, errs = jev.parse_frontmatter("just text")
        self.assertIsNone(fields)
        self.assertTrue(errs)


class Preflight(unittest.TestCase):
    def test_repo_agents_valid(self):
        # Also the lint test for agents/: every file must parse and match config/agents.json.
        res = jev.preflight(CFG, REPO_AGENTS, all_agents=True)
        self.assertTrue(res["ok"], res["message"])
        self.assertEqual(sorted(res["checked"]), sorted(CFG["agents"]))
        self.assertIn("JEV PREFLIGHT PASSED", res["message"])

    def test_repo_agents_valid_without_yaml(self):
        res = jev.preflight(CFG, REPO_AGENTS, all_agents=True, use_yaml=False)
        self.assertTrue(res["ok"], res["message"])

    def test_model_resolution(self):
        want = {"jev-builder": "claude-sonnet-5-5", "jev-engineer": "claude-sonnet-5-5", "jev-reviewer": "claude-sonnet-5-5",
                "jev-architect": "claude-opus-5-5", "jev-debugger": "claude-opus-5-5"}
        for a, m in want.items():
            self.assertEqual(jev.agent_model(CFG, a), m)

    def test_missing_builder_aborts_without_fallback(self):
        d = agents_copy(jev_builder=None)
        res = jev.preflight(CFG, d)
        self.assertFalse(res["ok"])
        msg = res["message"]
        for s in ("JEV PREFLIGHT FAILED", "Missing agents:", "- jev-builder", "jev-builder -> claude-sonnet-5-5 / low",
                  "Task execution stopped.", "No fallback agent was spawned.", "restart Claude Code"):
            self.assertIn(s, msg)
        self.assertIsNone(CFG["fallbacks"]["jev-builder"])
        p = run(["preflight", "--agents-dir", d])
        self.assertEqual(p.returncode, 1)
        self.assertIn("No fallback agent was spawned.", p.stdout)
        self.assertIn("| jev-builder | NO |", p.stdout)
        p = run(["preflight", "--agents-dir", d, "--json"])
        self.assertEqual(p.returncode, 1)
        out = json.loads(p.stdout)
        self.assertFalse(out["ok"])
        self.assertIn("registry_note", out)

    def test_bad_yaml(self):
        d = agents_copy(jev_reviewer=set_line("description", "Reviewer (medium effort): reviews diffs"))
        res = jev.preflight(CFG, d)
        self.assertFalse(res["ok"])
        row = [r for r in res["agents"] if r["agent"] == "jev-reviewer"][0]
        self.assertTrue(row["exists"])
        self.assertFalse(row["frontmatter_ok"])
        self.assertIn("Invalid agents:", res["message"])

    def test_wrong_model_and_effort(self):
        d = agents_copy(jev_builder=lambda t: set_line("effort", "high")(set_line("model", "claude-opus-5-5")(t)))
        row = [r for r in jev.preflight(CFG, d)["agents"] if r["agent"] == "jev-builder"][0]
        self.assertFalse(row["ok"])
        self.assertTrue(any("model" in e for e in row["errors"]))
        self.assertTrue(any("effort" in e for e in row["errors"]))

    def test_alias_model_flagged(self):
        d = agents_copy(jev_scout=set_line("model", "sonnet"))
        row = jev.check_agent("jev-scout", CFG, d)
        self.assertTrue(any("alias" in e for e in row["errors"]), row["errors"])

    def test_tools_vs_write_flag(self):
        d = agents_copy(jev_builder=set_line("tools", "Read, Grep, Glob, Bash, Edit"),
                        jev_scout=set_line("tools", "Read, Grep, Edit"))
        self.assertTrue(any("Write" in e for e in jev.check_agent("jev-builder", CFG, d)["errors"]))
        self.assertTrue(any("include Edit" in e for e in jev.check_agent("jev-scout", CFG, d)["errors"]))
        d = agents_copy(jev_reviewer=lambda t: "\n".join(l for l in t.split("\n") if not l.startswith("tools:")))
        self.assertTrue(any("inherits every tool" in e for e in jev.check_agent("jev-reviewer", CFG, d)["errors"]))

    def test_name_mismatch_and_empty_body(self):
        d = agents_copy(jev_qa=lambda t: set_line("name", "qa")(t.split("\n---\n")[0] + "\n---\n\n"))
        errs = jev.check_agent("jev-qa", CFG, d)["errors"]
        self.assertTrue(any("does not match" in e for e in errs), errs)
        self.assertTrue(any("empty body" in e for e in errs), errs)

    def test_configured_fallback_fails(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["fallbacks"]["jev-reviewer"] = "jev-analyst"
        res = jev.preflight(cfg, REPO_AGENTS)
        self.assertFalse(res["ok"])
        self.assertTrue(any("silent substitution" in e for e in res["config_errors"]))

    def test_project_agent_shadows_user_agent(self):
        proj = tempfile.mkdtemp(dir=TMP)
        os.makedirs(os.path.join(proj, ".claude", "agents"))
        with open(os.path.join(proj, ".claude", "agents", "jev-builder.md"), "w") as f:
            f.write("---\nname: jev-builder\ndescription: broken: yes\n---\nx\n")
        row = jev.check_agent("jev-builder", CFG, REPO_AGENTS, project_dir=proj)
        self.assertEqual(row["scope"], "project")
        self.assertFalse(row["ok"])


def sig(**kw):
    base = dict(self_contained=0.9, read_only=0.05, high_stakes=0.05, unknown_cause=0.05, design=0.05, batchable=0.2, exhaustive=0.1)
    base.update(kw)
    return base


def route(task, depth=0.3, breadth=0.2, conf=0.9, files=None, task_id=None, **signals):
    s = sig(**signals)
    d, reasons, writes = jev.decide(task, depth, conf, breadth, s)
    d.update(depth=depth, breadth=breadth, depth_confidence=conf, signals=s, reasons=reasons, parallel_safe=not writes)
    return jev.finalize_route(task, d, files, task_id, CFG)


class FastPath(unittest.TestCase):
    def test_simple_copy_change_skips_architect_and_opus(self):
        d = route("Change the Save button label to Submit in SettingsPage.tsx")
        self.assertTrue(d["fast_path"], d["fast_path_checks"])
        self.assertEqual((d["route"], d["next_agent"], d["model"], d["effort"]), ("fast", "jev-builder", "claude-sonnet-5-5", "low"))
        agents = [s["agent"] for s in d["sequence"]]
        self.assertNotIn("jev-architect", agents)
        self.assertFalse(any("opus" in (s["model"] or "") for s in d["sequence"]))
        self.assertNotIn("plan_first", d)
        self.assertEqual(d["allowed_agents"], ["jev-builder", "jev-qa", "jev-reviewer"])

    def test_complex_task_is_planned_with_architect_first(self):
        d = route("Add persistence for user settings across the settings and storage modules", depth=2.2, breadth=1.8)
        self.assertFalse(d["fast_path"])
        self.assertEqual(d["route"], "planned")
        seq = d["sequence"]
        self.assertEqual((seq[0]["agent"], seq[0]["model"], seq[0]["effort"]), ("jev-architect", "claude-opus-5-5", "max"))
        self.assertIsNone(seq[1]["agent"])
        self.assertIn("jev.py plan save --task %s" % d["task_id"], seq[1]["purpose"])
        self.assertEqual((seq[2]["agent"], seq[2]["effort"]), ("jev-builder", "low"))
        self.assertEqual(seq[3]["agent"], "jev-reviewer")
        self.assertEqual(d["plan_path"], os.path.join(".jev", "plans", d["task_id"] + ".md"))

    def test_each_blocker_disables_fast_path(self):
        base = "Rename the label in Header.tsx"
        cases = {
            "schema": ("Add a column to the users table in models.py", {}),
            "persistence": ("Persist the toggle state in prefs.ts", {}),
            "public api": ("Change the signature of the exported parse() in api.ts", {}),
            "concurrency": ("Add a lock around the cache refresh in worker.py", {}),
            "architecture": ("Redesign the module boundary in app.ts", {}),
            "ambiguity": ("Maybe improve the header somehow?", {}),
            "unknown cause": (base, {"unknown_cause": 0.8}),
            "design signal": (base, {"design": 0.7}),
            "stakes": (base, {"high_stakes": 0.7}),
            "not self contained": (base, {"self_contained": 0.3}),
        }
        self.assertTrue(route(base)["fast_path"])
        for name, (task, s) in cases.items():
            self.assertFalse(route(task, **s)["fast_path"], name)

    def test_files_and_breadth(self):
        t = "Fix the typo in the footer text"
        self.assertTrue(route(t, files=["Footer.tsx"])["fast_path"])
        self.assertFalse(route(t, files=["Footer.tsx", "Header.tsx"])["fast_path"])
        self.assertFalse(route(t, breadth=1.0)["fast_path"])
        self.assertFalse(route(t, depth=1.6)["fast_path"])

    def test_read_only_is_direct(self):
        d = route("Find where the login route is defined", read_only=0.95)
        self.assertEqual((d["route"], d["fast_path"], d["next_agent"]), ("direct", False, "jev-scout"))
        self.assertEqual(d["model"], "claude-sonnet-5-5")

    def test_task_id(self):
        a, b = route("Fix the typo"), route("fix the typo ")
        self.assertEqual(a["task_id"], b["task_id"])
        self.assertTrue(a["task_id"].startswith("T-"))
        self.assertEqual(route("Fix the typo", task_id="TASK-9")["task_marker"], "[jev:task=TASK-9]")

    def test_record_route(self):
        os.environ["JEV_HOME"] = os.path.join(TMP, "rec-home")
        try:
            d = route("Fix the typo in Footer.tsx", task_id="TASK-REC")
            jev.record_route(d)
            rec = jev.load_routes()["TASK-REC"]
            self.assertEqual(rec["allowed"], d["allowed_agents"])
            self.assertEqual(rec["route"], "fast")
        finally:
            os.environ.pop("JEV_HOME")


class Escalation(unittest.TestCase):
    def esc(self, st, frm, cat):
        return jev.escalate_decision(CFG, st, frm, cat)

    def test_table(self):
        st = jev.new_state("T")
        cases = {"implementation_complexity": "jev-engineer", "hard_debugging": "jev-debugger", "invalid_plan": "jev-architect",
                 "requirement_ambiguity": "orchestrator", "environment_failure": "orchestrator", "test_failure": "jev-builder"}
        for cat, want in cases.items():
            d, _ = self.esc(st, "jev-builder", cat)
            self.assertEqual(d["next_agent"], want, cat)

    def test_invalid_plan_goes_straight_to_architect(self):
        st = jev.new_state("T")
        d, st = self.esc(st, "builder", "invalid_plan")
        self.assertEqual((d["next_agent"], d["action"], d["model"], d["effort"]), ("jev-architect", "replan", "claude-opus-5-5", "max"))
        self.assertEqual((st["replans"], st["debugger_attempts"]), (1, 0))
        self.assertEqual([h["to"] for h in st["history"]], ["jev-architect"])  # no engineer, no debugger burned

    def test_requirement_ambiguity_stops_coding(self):
        d, _ = self.esc(jev.new_state("T"), "jev-engineer", "requirement_ambiguity")
        self.assertEqual((d["next_agent"], d["action"], d["subagent_type"]), ("orchestrator", "stop_coding", None))

    def test_ladder_steps(self):
        st = jev.new_state("T")
        d, st = self.esc(st, "jev-builder", "implementation_complexity")
        self.assertEqual((d["next_agent"], d["attempt"], d["effort"]), ("jev-engineer", 2, "medium"))
        d, st = self.esc(st, "jev-engineer", "implementation_complexity")
        self.assertEqual((d["next_agent"], d["attempt"], d["effort"]), ("jev-debugger", 3, "high"))
        d, st = self.esc(st, "jev-debugger", "implementation_complexity")
        self.assertEqual((d["next_agent"], d["action"]), ("jev-architect", "replan"))

    def test_debugger_guardrail(self):
        st = jev.new_state("T")
        for _ in range(CFG["guardrails"]["max_debugger_attempts"]):
            d, st = self.esc(st, "jev-engineer", "hard_debugging")
            self.assertEqual(d["next_agent"], "jev-debugger")
        d, st = self.esc(st, "jev-debugger", "hard_debugging")
        self.assertEqual(d["next_agent"], "jev-architect")
        self.assertIn("max_debugger_attempts", d["guardrail"])

    def test_replan_guardrail(self):
        st = jev.new_state("T")
        for _ in range(CFG["guardrails"]["max_replans"]):
            d, st = self.esc(st, "jev-builder", "invalid_plan")
            self.assertEqual(d["next_agent"], "jev-architect")
        d, st = self.esc(st, "jev-builder", "invalid_plan")
        self.assertEqual((d["next_agent"], d["action"]), ("orchestrator", "stop_report_to_user"))
        self.assertIn("max_replans", d["guardrail"])

    def test_test_failure_retries_then_steps_up(self):
        st = jev.new_state("T")
        d, st = self.esc(st, "jev-builder", "test_failure")
        self.assertEqual(d["next_agent"], "jev-builder")
        d, st = self.esc(st, "jev-builder", "test_failure")
        self.assertEqual(d["next_agent"], "jev-builder")
        d, st = self.esc(st, "jev-builder", "test_failure")
        self.assertEqual(d["next_agent"], "jev-engineer")

    def test_cli_writes_state_and_route_record(self):
        cwd = tempfile.mkdtemp(dir=TMP)
        home = os.path.join(cwd, "home")
        p = run(["escalate", "--task", "TASK-7", "--from", "builder", "--category", "invalid_plan", "--evidence", "src/a.kt"],
                cwd=cwd, env={"JEV_HOME": home})
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["next_agent"], "jev-architect")
        self.assertIn("[JEV]", d["log"])
        self.assertIn("failure=invalid_plan", d["log"])
        with open(os.path.join(cwd, ".jev", "state", "TASK-7.json")) as f:
            st = json.load(f)
        self.assertEqual((st["replans"], st["history"][0]["evidence"]), (1, ["src/a.kt"]))
        with open(os.path.join(home, "last_route.json")) as f:
            rec = json.load(f)["TASK-7"]
        self.assertEqual(rec["escalations"][0]["to"], "jev-architect")
        with open(os.path.join(home, "routing.log")) as f:
            self.assertEqual(json.loads(f.readlines()[-1])["event"], "escalation")
        p = run(["escalate", "--task", "../x", "--from", "builder", "--category", "invalid_plan"], cwd=cwd, env={"JEV_HOME": home})
        self.assertEqual(p.returncode, 1)


PLAN_OK = """# Plan TASK-1
## Objective
Do it.
## Assumptions
- a
## Constraints
- c
## Files
- src/a.py
## Implementation steps
1. x
## Invariants
- i
## Acceptance criteria
- ok
## Escalation conditions
- stop if y
"""


class PlanPersistence(unittest.TestCase):
    def test_save_show_and_replan_archive(self):
        cwd = tempfile.mkdtemp(dir=TMP)
        src = os.path.join(cwd, "plan.md")
        with open(src, "w") as f:
            f.write(PLAN_OK)
        p = run(["plan", "save", "--task", "TASK-1", "--from-file", src], cwd=cwd)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["missing_sections"], [])
        self.assertTrue(os.path.isfile(os.path.join(cwd, ".jev", "plans", "TASK-1.md")))
        p = run(["plan", "save", "--task", "TASK-1", "--stdin"], cwd=cwd, stdin="## Objective\nonly this\n")
        out = json.loads(p.stdout)
        self.assertTrue(out["ok"])
        self.assertIn("acceptance", out["missing_sections"])
        self.assertTrue(out["warnings"])
        self.assertTrue(out["archived_previous"].endswith("TASK-1.v1.md"))
        p = run(["plan", "show", "--task", "TASK-1"], cwd=cwd)
        self.assertIn("only this", json.loads(p.stdout)["text"])
        p = run(["plan", "show", "--task", "TASK-1", "--raw"], cwd=cwd)
        self.assertEqual(p.stdout, "## Objective\nonly this\n")

    def test_yaml_plan_sections_recognized(self):
        text = jev._to_yaml(jev.HANDOFF_TEMPLATES["plan"])
        self.assertEqual(jev.plan_missing_sections(text), [])

    def test_errors(self):
        cwd = tempfile.mkdtemp(dir=TMP)
        self.assertEqual(run(["plan", "show", "--task", "NOPE"], cwd=cwd).returncode, 1)
        self.assertEqual(run(["plan", "save", "--task", "X", "--stdin"], cwd=cwd, stdin="  ").returncode, 1)
        self.assertEqual(run(["plan", "save", "--task", "a/b", "--stdin"], cwd=cwd, stdin="x").returncode, 1)


class Handoffs(unittest.TestCase):
    def write(self, obj, ext=".json"):
        fd, path = tempfile.mkstemp(suffix=ext, dir=TMP)
        with os.fdopen(fd, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        return path

    def test_templates_validate(self):
        for kind in jev.HANDOFF_KINDS:
            for fmt in ("yaml", "json"):
                if fmt == "yaml" and jev._yaml is None:
                    continue
                p = run(["handoff", "template", "--kind", kind, "--format", fmt])
                path = self.write(p.stdout, "." + fmt)
                v = run(["handoff", "validate", "--kind", kind, path])
                self.assertEqual(v.returncode, 0, (kind, fmt, v.stdout))

    def test_invalid_documents(self):
        plan = json.loads(json.dumps(jev.HANDOFF_TEMPLATES["plan"]["jev_plan"]))
        del plan["acceptance_criteria"]
        errs, _, _ = jev.validate_handoff("plan", plan)
        self.assertTrue(any("acceptance_criteria" in e for e in errs))
        fail = dict(jev.HANDOFF_TEMPLATES["failure"]["jev_failure"], category="tired")
        self.assertTrue(jev.validate_handoff("failure", fail)[0])
        rev = json.loads(json.dumps(jev.HANDOFF_TEMPLATES["review"]["review"]))
        rev["verdict"] = "pass"
        errs, _, _ = jev.validate_handoff("review", rev)
        self.assertTrue(any("critical or major" in e for e in errs))
        rev["findings"]["major"] = []
        rev["acceptance_criteria"][0]["status"] = "maybe"
        self.assertTrue(jev.validate_handoff("review", rev)[0])
        done = dict(jev.HANDOFF_TEMPLATES["completion"]["jev_handoff"], status="done", role="builder")
        errs = jev.validate_handoff("completion", done)[0]
        self.assertEqual(len(errs), 2, errs)
        p = run(["handoff", "validate", "--kind", "review", self.write({"review": rev})])
        self.assertEqual(p.returncode, 1)
        self.assertFalse(json.loads(p.stdout)["ok"])

    def test_invalid_plan_failure_should_route_to_architect(self):
        f = dict(jev.HANDOFF_TEMPLATES["failure"]["jev_failure"], category="invalid_plan")
        errs, warns, _ = jev.validate_handoff("failure", f)
        self.assertEqual(errs, [])
        self.assertTrue(warns)

    def test_schema_files_are_valid_json_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        for kind, (name, root) in jev.HANDOFF_KINDS.items():
            schema = jev.load_schema(kind)
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(jev.HANDOFF_TEMPLATES[kind][root], schema)  # the stdlib validator agrees with jsonschema


DONE_REPORT = """STATUS: DONE

CHANGED:
- ScheduleAdherence.kt

WHY:
- unified status source

TEST:
- +15s = ON_TIME

RISK:
- none

NEXT:
- jev-reviewer
"""

BLOCKED_REPORT = """STATUS: BLOCKED
CATEGORY:
- invalid_plan
FOUND:
- planned API does not exist
EVIDENCE:
- src/foo/Bar.kt
NEXT:
- jev-architect replan
"""


class ReportLint(unittest.TestCase):
    def test_good_reports(self):
        self.assertTrue(jev.lint_report(DONE_REPORT)["ok"])
        r = jev.lint_report(BLOCKED_REPORT)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "BLOCKED")

    def test_bad_reports(self):
        r = jev.lint_report(DONE_REPORT.replace("RISK:\n- none\n", ""))
        self.assertIn("missing RISK:", r["errors"])
        r = jev.lint_report(BLOCKED_REPORT.replace("invalid_plan", "confused"))
        self.assertFalse(r["ok"])
        self.assertFalse(jev.lint_report("I changed some stuff, all good.")["ok"])
        self.assertFalse(jev.lint_report(DONE_REPORT.replace("- jev-reviewer", ""))["ok"])

    def test_cli(self):
        path = os.path.join(TMP, "r.txt")
        with open(path, "w") as f:
            f.write(BLOCKED_REPORT)
        self.assertEqual(run(["report", "lint", path]).returncode, 0)
        self.assertEqual(run(["report-lint", "-"], stdin="STATUS: DONE\n").returncode, 1)


class ContextWrapper(unittest.TestCase):
    def test_missing_jevctx_fails_soft(self):
        p = run(["context", "prepare", "--task-id", "T1", "--role", "jev-builder", "--task", "x"],
                env={"JEV_CTX": os.path.join(TMP, "nope.py")})
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual((out["graph_status"], out["pack_path"], out["reused"]), ("unavailable", None, False))
        self.assertIn("[JEV CONTEXT]", p.stderr)

    def test_passthrough_and_crash(self):
        ok = os.path.join(TMP, "ctx_ok.py")
        with open(ok, "w") as f:
            f.write("import json,sys\nprint(json.dumps({'pack_path':'p','context_block':'B','graph_status':'ok',"
                    "'fallback':'none','reused':True,'argv':sys.argv[1:]}))\n")
        out = json.loads(run(["context", "prepare", "--task-id", "T1", "--role", "jev-builder", "--task", "x y", "--files", "a,b"],
                             env={"JEV_CTX": ok}).stdout)
        self.assertEqual(out["graph_status"], "ok")
        self.assertEqual(out["argv"], ["prepare", "--task-id", "T1", "--role", "jev-builder", "--task", "x y", "--files", "a,b", "--json"])
        bad = os.path.join(TMP, "ctx_bad.py")
        with open(bad, "w") as f:
            f.write("raise SystemExit('boom')\n")
        p = run(["context", "prepare", "--task-id", "T1", "--role", "r", "--task", "x"], env={"JEV_CTX": bad})
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(out["graph_status"], "unavailable")
        self.assertIn("boom", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
