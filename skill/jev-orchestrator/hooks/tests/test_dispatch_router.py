#!/usr/bin/env python3
"""Tests for hooks/dispatch_router.py and `jev.py outcomes` / `label`. Stdlib unittest, offline.

Run:  python3 ~/.claude/skills/jev-orchestrator/hooks/tests/test_dispatch_router.py -v
Jev is replaced by JEV_DISPATCH_FAKE; hook runs log to ~/.claude/jev/decisions_test.jsonl.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "dispatch_router.py")
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
sys.path.insert(0, SCRIPTS)
import jev  # noqa: E402

LOG_NAME = "decisions_test"
LOG_PATH = os.path.expanduser("~/.claude/jev/%s.jsonl" % LOG_NAME)
TMP = tempfile.mkdtemp(prefix="jev-dispatch-test-")


def decision(sub="jev-scout", via="subagent", conf=0.8, **kw):
    d = {"tier": "x", "subagent_type": sub, "via": via, "depth": 1.0, "breadth": 1.0, "depth_confidence": conf,
         "signals": {}, "reasons": ["because"]}
    d.update(kw)
    return d


def run_hook(main_type, dec, mode=None, prompt="Look at the code.", desc="Find the router"):
    fake = os.path.join(TMP, uuid.uuid4().hex + ".json")
    with open(fake, "w") as f:
        json.dump(dec, f)
    tid = "toolu_" + uuid.uuid4().hex
    env = dict(os.environ, JEV_DISPATCH_FAKE=fake, JEV_DISPATCH_LOG=LOG_NAME)
    env.pop("JEV_DISPATCH", None)
    if mode:
        env["JEV_DISPATCH"] = mode
    payload = {"tool_name": "Agent", "tool_use_id": tid, "session_id": "s1", "cwd": TMP,
               "tool_input": {"description": desc, "prompt": prompt, "subagent_type": main_type, "run_in_background": True}}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
    rec = None
    if os.path.isfile(LOG_PATH):
        with open(LOG_PATH) as f:
            for line in f:
                r = json.loads(line)
                if r.get("tool_use_id") == tid:
                    rec = r
    return p, rec


class HookTests(unittest.TestCase):
    def assertSilent(self, p):
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_non_routable_skipped(self):
        for t in ("jev-qa", "Explore", "general-purpose", ""):
            p, rec = run_hook(t, decision())
            self.assertSilent(p)
            self.assertEqual(rec["skip"], "tier_not_routable")

    def test_keep_marker(self):
        p, rec = run_hook("jev-analyst", decision("jev-scout"), prompt="Do it [jev:keep]")
        self.assertSilent(p)
        self.assertEqual(rec["skip"], "keep_marker")
        self.assertEqual(rec["jev_type"], "jev-scout")

    def test_off_logs_nothing(self):
        p, rec = run_hook("jev-analyst", decision("jev-scout"), mode="off")
        self.assertSilent(p)
        self.assertIsNone(rec)

    def test_shadow(self):
        p, rec = run_hook("jev-analyst", decision("jev-scout"), mode="shadow")
        self.assertSilent(p)
        self.assertEqual(rec["skip"], "shadow")
        self.assertFalse(rec["applied"])

    def test_applies_same_side(self):
        p, rec = run_hook("jev-analyst", decision("jev-scout", conf=0.6))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["permissionDecisionReason"], "Jev routed jev-analyst -> jev-scout (because)")
        self.assertEqual(out["updatedInput"], {"description": "Find the router", "prompt": "Look at the code.",
                                               "subagent_type": "jev-scout", "run_in_background": True})
        self.assertTrue(rec["applied"])
        self.assertIsNone(rec["skip"])
        for k in ("depth", "breadth", "depth_confidence", "signals", "reasons", "plan_first", "latency_ms", "mode", "errors"):
            self.assertIn(k, rec)

    def test_write_side_applies(self):
        p, _ = run_hook("jev-engineer", decision("jev-builder"))
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["updatedInput"]["subagent_type"], "jev-builder")

    def test_guards(self):
        cases = [
            ("jev-analyst", decision("jev-builder"), "cross_side"),
            ("jev-builder", decision("jev-architect"), "cross_side"),
            ("jev-analyst", decision("jev-scout", conf=0.59), "low_confidence"),
            ("jev-builder", decision("jev-debugger"), "never_debugger"),
            ("jev-engineer", decision("jev-builder", plan_first={"planner": "jev-architect"}), "plan_first"),
            ("jev-builder", decision(None, via="workflow"), "not_single_subagent"),
            ("jev-analyst", decision("jev-scout", via="main"), "not_single_subagent"),
            ("jev-scout", decision("jev-scout"), "agrees"),
        ]
        for main_type, dec, skip in cases:
            p, rec = run_hook(main_type, dec)
            self.assertSilent(p)
            self.assertEqual(rec["skip"], skip, (main_type, dec))

    def test_jev_error_silent(self):
        p, rec = run_hook("jev-analyst", {"error": "HTTP 500"})
        self.assertSilent(p)
        self.assertEqual(rec["skip"], "jev_error")
        self.assertTrue(rec["errors"])

    def test_bad_stdin(self):
        p = subprocess.run([sys.executable, HOOK], input="not json", capture_output=True, text=True, timeout=20)
        self.assertSilent(p)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=TMP)
        self.logs = os.path.join(self.dir, "logs")
        self.projects = os.path.join(self.dir, "projects")
        os.makedirs(self.logs)
        self.now = time.time()
        self.records = []

    def dispatch(self, tid, main, jev_type, applied=False, desc="find the router code", session="s1", ts=None):
        self.records.append({"kind": "dispatch", "tool_use_id": tid, "session_id": session, "main_type": main,
                             "jev_type": jev_type, "applied": applied, "skip": None, "description": desc,
                             "ts": ts or self.now - 100 + len(self.records)})

    def label(self, tid, label):
        self.records.append({"kind": "dispatch_label", "tool_use_id": tid, "label": label, "note": None,
                             "ts": self.now - 50 + len(self.records)})

    def transcript(self, tid, agent_type, entries, sess="sess"):
        d = os.path.join(self.projects, "-proj", sess, "subagents")
        os.makedirs(d, exist_ok=True)
        name = "agent-" + uuid.uuid4().hex[:8]
        with open(os.path.join(d, name + ".meta.json"), "w") as f:
            json.dump({"agentType": agent_type, "description": "x", "toolUseId": tid}, f)
        with open(os.path.join(d, name + ".jsonl"), "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def outcomes(self):
        with open(os.path.join(self.logs, "decisions.jsonl"), "w") as f:
            for r in self.records:
                f.write(json.dumps(r) + "\n")
        return jev.build_outcomes(self.logs, 7, self.projects)

    def test_meta_match_duration_tokens(self):
        t = self.now - 600
        self.dispatch("toolu_a", "jev-analyst", "jev-scout", applied=True)
        self.transcript("toolu_other", "jev-builder", [{"type": "user", "timestamp": iso(t)}])
        self.transcript("toolu_a", "jev-scout", [
            {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": iso(t + 30), "message": {"id": "m1", "usage": {"output_tokens": 100},
             "content": [{"type": "tool_use", "id": "x"}]}},
            {"type": "assistant", "timestamp": iso(t + 31), "message": {"id": "m1", "usage": {"output_tokens": 100},
             "content": [{"type": "text", "text": "hm"}]}},
            {"type": "user", "timestamp": iso(t + 40), "message": {"content": [{"type": "tool_result", "is_error": True}]}},
            {"type": "assistant", "timestamp": iso(t + 120), "message": {"id": "m2", "usage": {"output_tokens": 50},
             "content": [{"type": "text", "text": "I was unable to find it; blocked."}]}},
        ])
        r = self.outcomes()["rows"][0]
        self.assertEqual(r["ran_type"], "jev-scout")
        self.assertTrue(r["transcript"])
        self.assertEqual(r["minutes"], 2.0)
        self.assertEqual(r["output_tokens"], 150)
        self.assertEqual((r["turns"], r["tool_uses"], r["tool_errors"]), (3, 1, 1))
        self.assertEqual(r["final_flags"], ["blocked", "unable"])
        self.assertEqual(r["auto_label"], "too_low")

    def test_escalated(self):
        self.dispatch("t1", "jev-builder", "jev-builder", desc="fix the login redirect bug")
        self.dispatch("t2", "jev-engineer", "jev-engineer", desc="fix the login redirect bug again")  # 5/6 words
        self.dispatch("t3", "jev-scout", "jev-scout", desc="find the router")
        self.dispatch("t4", "jev-analyst", "jev-analyst", desc="find the router", session="s2")  # other session
        self.dispatch("t5", "jev-scout", "jev-scout", desc="list the tests")
        self.dispatch("t6", "jev-engineer", "jev-engineer", desc="list the tests")  # other side
        rows = {r["tool_use_id"]: r for r in self.outcomes()["rows"]}
        self.assertTrue(rows["t1"]["escalated"])
        self.assertEqual(rows["t1"]["auto_label"], "too_low")
        self.assertFalse(rows["t2"]["escalated"])
        self.assertFalse(rows["t3"]["escalated"])
        self.assertFalse(rows["t5"]["escalated"])

    def test_label_latest_wins(self):
        self.dispatch("t1", "jev-analyst", "jev-analyst")
        self.label("t1", "too_low")
        self.label("t1", "ok")
        r = self.outcomes()["rows"][0]
        self.assertEqual(r["label"], "ok")

    def test_label_command_last(self):
        self.dispatch("t1", "jev-analyst", "jev-analyst")
        self.dispatch("t2", "jev-analyst", "jev-scout")
        self.outcomes()
        env = dict(os.environ, HOME=self.dir)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "jev.py"), "label", "last", "too_low", "--note", "n",
                            "--log-dir", self.logs], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["tool_use_id"], "t2")

    def test_accuracy(self):
        self.dispatch("a", "jev-scout", "jev-scout")                      # agree, ok
        self.dispatch("b", "jev-analyst", "jev-scout", applied=True)      # jev ran, ok -> jev right
        self.dispatch("c", "jev-analyst", "jev-scout", applied=True)      # jev ran, too_low -> main (higher) right
        self.dispatch("d", "jev-scout", "jev-analyst")                    # main ran, too_low -> jev (higher) right
        self.dispatch("e", "jev-analyst", "jev-scout")                    # main ran, too_low -> jev lower: unclear
        self.dispatch("f", "jev-engineer", "jev-builder")                 # main ran, too_high -> jev right
        self.dispatch("g", "jev-analyst", "jev-scout")                    # unlabeled
        for tid, lab in (("a", "ok"), ("b", "ok"), ("c", "too_low"), ("d", "too_low"), ("e", "too_low"), ("f", "too_high")):
            self.label(tid, lab)
        out = self.outcomes()["summary"]
        m = out["manual"]
        self.assertEqual(m["labeled"], 6)
        self.assertEqual(m["accuracy"], round(2 / 6, 2))
        self.assertEqual(m["jev_pick_ran"], {"n": 3, "ok": 2, "accuracy": 0.67})   # a, b, c
        self.assertEqual(m["main_pick_ran"], {"n": 4, "ok": 1, "accuracy": 0.25})  # a, d, e, f
        self.assertEqual(m["disagreements"], {"jev_right": 3, "main_right": 1, "unclear": 1})
        self.assertEqual(out["agreement"], round(1 / 7, 2))
        self.assertEqual(out["applied_rate"], round(2 / 7, 2))
        self.assertEqual(out["auto"]["labeled"], 0)

    def test_empty(self):
        out = self.outcomes()
        self.assertEqual(out["rows"], [])
        self.assertIsNone(out["summary"]["agreement"])


if __name__ == "__main__":
    unittest.main()
