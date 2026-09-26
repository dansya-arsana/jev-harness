#!/usr/bin/env python3
"""Tests for hooks/prompt_router.py. Stdlib unittest; runs the hook as a subprocess with crafted stdin.

Run:  python3 ~/.claude/skills/jev-orchestrator/hooks/tests/test_prompt_router.py -v
Jev-dependent tests skip when Jev is unreachable. Test runs log to ~/.claude/jev/prompts_test.jsonl.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "prompt_router.py")
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts")
LOG_NAME = "prompts_test"
LOG_PATH = os.path.expanduser("~/.claude/jev/%s.jsonl" % LOG_NAME)
# Portable fixtures: a neutral working dir, a fake project dir, and a test-only conditions file.
TMP = tempfile.mkdtemp(prefix="jev-router-test-")
DEFAULT_CWD = os.path.join(TMP, "workspace")
PROJECT_CWD = os.path.join(TMP, "demo-game")
ALIAS_DIR = os.path.join(TMP, "demo-alias")
CONDITIONS = os.path.join(TMP, "conditions.json")
for _d in (DEFAULT_CWD, PROJECT_CWD):
    os.makedirs(_d, exist_ok=True)
with open(CONDITIONS, "w") as _f:
    json.dump([
        {"id": "media-tools", "threshold": 0.6,
         "when": "Does `user_request` ask for AI-generated or AI-edited media: sound (music, sound effects, voice) or images, video, or 3D assets?",
         "yes": "It asks to create or edit media with a generative AI tool.",
         "no": "No generative media: ordinary code, charts drawn by code, UI work, or discussing existing assets.",
         "inject": "Media rule: use AudioTool for all sound (music, SFX, voice) and VisualTool for images, video and 3D."},
        {"id": "staging-deploy", "threshold": 0.6,
         "when": "Does `user_request` involve deploying to, updating, or debugging the shared staging server?",
         "yes": "It is about deploying or operating staging.", "no": "It is not about staging deploys.",
         "inject": "Staging rule: the staging box is shared; always test the proxy config before reloading it."},
        {"id": "demo-game", "threshold": 0.6, "cwd_prefix": PROJECT_CWD, "paths": [ALIAS_DIR],
         "when": "Is `user_request` about the Demo Game project?",
         "yes": "It is about the Demo Game.", "no": "It is not about the Demo Game.",
         "inject": "Demo Game notes: Swift 6 + RealityKit; run xcodegen after adding files."},
    ], _f)
DEAD_URL = "http://127.0.0.1:9"  # nothing listens: any Jev call fails fast

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.dirname(HERE))

LATENCIES = []  # wall ms of live (Jev) cases
_JEV_OK = None


def jev_reachable():
    global _JEV_OK
    if _JEV_OK is None:
        try:
            import jevlib
            jevlib.ask("ping", {"q": jevlib.noul("Is this a ping?", "yes", "no")}, timeout=6, retries=1)
            _JEV_OK = True
        except Exception:
            _JEV_OK = False
    return _JEV_OK


def run_hook(prompt=None, cwd=DEFAULT_CWD, raw=None, env_extra=None, transcript_path="/tmp/none.jsonl"):
    sid = "test-" + uuid.uuid4().hex[:10]
    if raw is None:
        raw = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": cwd,
                          "session_id": sid, "transcript_path": transcript_path})
    env = dict(os.environ, JEV_ROUTER_LOG=LOG_NAME, JEV_ROUTER_CONDITIONS=CONDITIONS)
    env.pop("JEV_ROUTER", None)
    env.update(env_extra or {})
    t = time.time()
    p = subprocess.run([sys.executable, HOOK], input=raw.encode(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, timeout=30)
    ms = int((time.time() - t) * 1000)
    ctx = None
    out = p.stdout.decode()
    if out.strip():
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = data["hookSpecificOutput"]["additionalContext"]
    return {"code": p.returncode, "stdout": out, "stderr": p.stderr.decode(), "ctx": ctx,
            "ms": ms, "log": find_log(sid)}


def find_log(sid):
    try:
        with open(LOG_PATH) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines[-200:]):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("session_id") == sid:
            return rec
    return None


def fired_ids(ctx):
    return re.findall(r'<conditional_instruction id="([^"]+)">', ctx or "")


def suggested(ctx):
    m = re.search(r"Relevant to the current request: ([^\s.]+)\.", ctx or "")
    return m.group(1) if m else None


class NoJev(unittest.TestCase):
    """Cases that must not call Jev at all (Jev URL points at a dead port)."""

    def assert_silent_skip(self, r, reason):
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["stdout"], "")
        self.assertIsNotNone(r["log"], "run was not logged")
        self.assertEqual(r["log"].get("skipped"), reason)
        self.assertNotIn("request1", r["log"].get("latency_ms", {}))

    def test_slash_command(self):
        r = run_hook("/loop 5m check deploy", env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
        self.assert_silent_skip(r, "slash_command")

    def test_ok_thanks(self):
        r = run_hook("ok thanks", env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
        self.assert_silent_skip(r, "short")

    def test_conversational_long(self):
        r = run_hook("yes please go ahead and do it", env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
        self.assert_silent_skip(r, "conversational")

    def test_what_is_2_plus_2(self):
        r = run_hook("what is 2+2", env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
        self.assert_silent_skip(r, "short")
        self.assertIsNone(suggested(r["ctx"]))
        self.assertEqual(fired_ids(r["ctx"]), [])

    def test_router_off(self):
        r = run_hook("make a landing page for my coffee brand with a premium editorial feel",
                     env_extra={"JEV_ROUTER": "off", "TYPESAFE_BASE_URL": DEAD_URL})
        self.assert_silent_skip(r, "disabled")

    def test_malformed_stdin(self):
        for raw in ["{not json", "", "[1,2,3]", '{"prompt": 42}', "\x00\xff"]:
            r = run_hook(raw=raw, env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
            self.assertEqual(r["code"], 0, raw)
            self.assertEqual(r["stdout"], "", raw)

    def test_project_cwd_without_jev(self):
        """cwd_prefix fires deterministically even when Jev is down; no skill is guessed."""
        r = run_hook("refactor this function so it is easier to read", cwd=PROJECT_CWD,
                     env_extra={"TYPESAFE_BASE_URL": DEAD_URL})
        self.assertEqual(r["code"], 0)
        self.assertEqual(fired_ids(r["ctx"]), ["demo-game"])
        self.assertIsNone(suggested(r["ctx"]))
        self.assertTrue(any("request1" in e for e in r["log"]["errors"]))
        self.assertLess(r["ms"], 8000)


NOTE = ("<task-notification>\n<task-id>abc123</task-id>\n<status>completed</status>\n"
        "<summary>Agent finished building the landing page and ran all the tests</summary>\n</task-notification>")
OFF = {"TYPESAFE_BASE_URL": DEAD_URL}


class Harness(unittest.TestCase):
    assert_silent_skip = NoJev.assert_silent_skip

    def test_notification_only(self):
        r = run_hook(NOTE, env_extra=OFF)
        self.assert_silent_skip(r, "harness_message")
        self.assertEqual(r["log"]["harness_tags"], ["task-notification"])

    def test_bash_input(self):
        self.assert_silent_skip(run_hook("<bash-input>ls -la</bash-input>", env_extra=OFF), "harness_message")

    def test_notification_plus_text(self):
        r = run_hook(NOTE + "\nplease also make the hero video autoplay muted on mobile", env_extra=OFF)
        self.assertEqual(r["code"], 0)
        self.assertIsNone(r["log"].get("skipped"))
        self.assertEqual(r["log"]["harness_tags"], ["task-notification"])
        self.assertTrue(any("request1" in e for e in r["log"]["errors"]))


class Slash(unittest.TestCase):
    assert_silent_skip = NoJev.assert_silent_skip

    def test_goal_routed(self):
        r = run_hook("/goal build the checkout flow with Stripe test mode and add tests", env_extra=OFF)
        self.assertEqual(r["code"], 0)
        self.assertIsNone(r["log"].get("skipped"))
        self.assertEqual(r["log"]["slash"], "goal")

    def test_goal_short(self):
        self.assert_silent_skip(run_hook("/goal ok", env_extra=OFF), "short")

    def test_loop_skipped(self):
        self.assert_silent_skip(run_hook("/loop 5m check the deploy status on staging every five minutes",
                                         env_extra=OFF), "slash_command")

    def test_env_extends(self):
        r = run_hook("/loop 5m check the deploy status on staging every five minutes",
                     env_extra=dict(OFF, JEV_ROUTER_SLASH="Loop"))
        self.assertIsNone(r["log"].get("skipped"))
        self.assertEqual(r["log"]["slash"], "loop")


def write_transcript(entries, garbage=False):
    path = os.path.join(TMP, "t-%s.jsonl" % uuid.uuid4().hex[:8])
    with open(path, "w") as f:
        if garbage:
            f.write("{not json\n" + "x" * 50000 + "\n\x00\n")
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def tool_use(path):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "reading"},
        {"type": "tool_use", "id": uuid.uuid4().hex, "name": "Read", "input": {"file_path": path + "/main.swift"}}]}}


class Recent(unittest.TestCase):
    PROMPT = "refactor this function so it is easier to read"

    def run_t(self, tp):
        r = run_hook(self.PROMPT, cwd=DEFAULT_CWD, env_extra=OFF, transcript_path=tp)
        self.assertEqual(r["code"], 0)
        return r

    def test_three_hits_fire(self):
        r = self.run_t(write_transcript([tool_use(PROJECT_CWD) for _ in range(3)]))
        self.assertEqual(fired_ids(r["ctx"]), ["demo-game"])
        self.assertEqual(r["log"]["fired"]["demo-game"]["source"], "recent")
        self.assertGreaterEqual(r["log"]["recent_hits"]["demo-game"], 3)
        self.assertIn("recent", r["log"]["latency_ms"])

    def test_two_hits_nothing(self):
        r = self.run_t(write_transcript([tool_use(PROJECT_CWD) for _ in range(2)]))
        self.assertEqual(fired_ids(r["ctx"]), [])
        self.assertEqual(r["log"]["recent_hits"]["demo-game"], 2)

    def test_non_tool_use_ignored(self):
        entries = []
        for _ in range(6):
            entries.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "see " + PROJECT_CWD}]}})
            entries.append({"type": "user", "message": {"role": "user", "content": "work in " + PROJECT_CWD}})
            entries.append({"type": "attachment", "content": PROJECT_CWD})
            entries.append({"type": "assistant", "message": {"content": [{"type": "text", "text": PROJECT_CWD}]}})
        r = self.run_t(write_transcript(entries))
        self.assertEqual(fired_ids(r["ctx"]), [])
        self.assertNotIn("recent_hits", r["log"])

    def test_missing_and_garbage(self):
        for tp in (os.path.join(TMP, "missing.jsonl"), write_transcript([], garbage=True), None):
            r = self.run_t(tp)
            self.assertEqual(fired_ids(r["ctx"]), [])
            self.assertFalse(any(e.startswith("recent") for e in r["log"]["errors"]))

    def test_paths_alias(self):
        r = self.run_t(write_transcript([tool_use(ALIAS_DIR) for _ in range(4)]))
        self.assertEqual(fired_ids(r["ctx"]), ["demo-game"])
        self.assertEqual(r["log"]["fired"]["demo-game"]["source"], "recent")


class Parser(unittest.TestCase):
    def test_frontmatter_variants(self):
        import prompt_router as pr
        text = ("---\nname: demo\ndescription: >-\n  folded line one\n  line two\nmetadata:\n  type: x\n"
                "other: 'it''s quoted'\nlong: plain start\n  continued here\n---\nBody text\n")
        meta, body = pr.split_frontmatter(text)
        self.assertEqual(meta["name"], "demo")
        self.assertEqual(meta["description"], "folded line one line two")
        self.assertEqual(meta["other"], "it's quoted")
        self.assertEqual(meta["long"], "plain start continued here")
        self.assertNotIn("type", meta)
        self.assertEqual(body.strip(), "Body text")
        meta, _ = pr.split_frontmatter('﻿---\r\nname: x\r\ndescription: "a \\"b\\" c"\r\n---\r\n')
        self.assertEqual(meta["description"], 'a "b" c')
        meta, _ = pr.split_frontmatter("---\ndescription: |\n  a\n  b\n---\n")
        self.assertEqual(meta["description"], "a\nb")

    def test_render_size_cap_drops_lowest(self):
        import prompt_router as pr
        conds = [{"id": "a", "when": "?", "inject": "A" * 2400, "max_chars": 3000},
                 {"id": "b", "when": "?", "inject": "B" * 2400, "max_chars": 3000},
                 {"id": "c", "when": "?", "inject": "C" * 2400, "max_chars": 3000},
                 {"id": "d", "when": "?", "inject": "D" * 2400, "max_chars": 3000}]
        rec = {"errors": []}
        out = pr.render(conds, {"a": (0.9, "jev"), "b": (0.7, "jev"), "c": (0.95, "jev"), "d": (0.65, "jev")},
                        "some-skill", rec)
        self.assertLessEqual(len(out), 8000)
        self.assertEqual(fired_ids(out), ["c", "a", "b"])
        self.assertEqual(rec["dropped_for_size"], ["d"])
        self.assertEqual(suggested(out), "some-skill")

    def test_roster(self):
        import prompt_router as pr
        roster, _ = pr.load_roster(DEFAULT_CWD, time.time() + 5)
        names = {r["name"] for r in roster}
        print("\n  roster size (cwd=%s): %d" % (DEFAULT_CWD, len(roster)))
        self.assertGreater(len(roster), 0, "no SKILL.md files found in any roster location")
        self.assertEqual(len(names), len(roster))
        roster2, cached = pr.load_roster(DEFAULT_CWD, time.time() + 5)
        self.assertTrue(cached)
        self.assertEqual(len(roster2), len(roster))


class Live(unittest.TestCase):
    """Cases that need Jev; skipped when it is unreachable."""

    def setUp(self):
        if not jev_reachable():
            self.skipTest("Jev unreachable")

    def live(self, prompt, cwd=DEFAULT_CWD):
        r = run_hook(prompt, cwd=cwd)
        LATENCIES.append(r["ms"])
        self.assertEqual(r["code"], 0)
        self.assertLess(len(r["ctx"] or ""), 8000)
        rec = r["log"] or {}
        print("\n  [%d ms] %s\n    skill=%s conditions=%s top=%s needs=%s stop=%s jev_ms=%s errors=%s" % (
            r["ms"], prompt[:70], suggested(r["ctx"]), fired_ids(r["ctx"]),
            rec.get("top_candidates", [])[:3], rec.get("needs_skill"), rec.get("stop"),
            rec.get("latency_ms"), rec.get("errors")))
        self.assertEqual(rec.get("errors"), [], "Jev errors: %s" % rec.get("errors"))
        return r

    def roster_has(self, pattern):
        import prompt_router as pr
        roster, _ = pr.load_roster(DEFAULT_CWD, time.time() + 5)
        if not any(re.search(pattern, r["name"]) for r in roster):
            self.skipTest("no installed skill matches /%s/" % pattern)

    def test_landing_page_design_skill(self):
        self.roster_has(r"design|frontend|taste|ui|visual|web")
        r = self.live("make a landing page for my coffee brand with a premium editorial feel")
        skill = suggested(r["ctx"])
        self.assertIsNotNone(skill)
        self.assertRegex(skill, r"design|frontend|taste|ui|visual|getlayers|web|redesign")
        self.assertEqual(fired_ids(r["ctx"]), [])

    def test_music_sfx_media_condition(self):
        r = self.live("compose a 30 second tense music cue and footsteps sound effects for the wolf attack scene")
        self.assertIn("media-tools", fired_ids(r["ctx"]))
        self.assertIn("AudioTool", r["ctx"])

    def test_chart_does_not_fire_media_condition(self):
        r = self.live("plot a bar chart of weekly signups from this csv with matplotlib")
        self.assertNotIn("media-tools", fired_ids(r["ctx"]))

    def test_staging_condition(self):
        r = self.live("deploy the latest build to the staging server")
        self.assertIn("staging-deploy", fired_ids(r["ctx"]))

    def test_plain_question_nothing(self):
        r = self.live("what is the capital of France, briefly?")
        self.assertIsNone(r["ctx"])

    def test_sql_data_skill(self):
        self.roster_has(r"sql|query|data")
        r = self.live("write a SQL query for monthly active users by country in BigQuery")
        skill = suggested(r["ctx"])
        self.assertIsNotNone(skill)
        self.assertRegex(skill, r"sql|query|data")

    def test_project_cwd_generic(self):
        r = self.live("refactor this function so it is easier to read", cwd=PROJECT_CWD)
        self.assertIn("demo-game", fired_ids(r["ctx"]))
        self.assertEqual(r["log"]["fired"]["demo-game"]["source"], "cwd")


def tearDownModule():
    if LATENCIES:
        s = sorted(LATENCIES)
        p50 = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0
        print("\nlive hook wall latency ms: %s  p50=%s  max=%s" % (LATENCIES, p50, s[-1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
