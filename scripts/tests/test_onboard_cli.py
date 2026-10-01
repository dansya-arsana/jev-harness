"""Offline tests for scripts/onboard.py and scripts/onboard_verify.py. Every run uses a sandbox home plus a copy of the
repo (never the real home), and the e2e check is only tested with a fake runner.

Run: export TYPESAFE_BASE_URL=https://127.0.0.1:9; python -m unittest discover -s scripts/tests -p "test_onboard_cli.py" -v
"""
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboard_sandbox as sb  # noqa: E402
from onboard_sandbox import FAKE_TOKEN  # noqa: E402

import onboard  # noqa: E402
import onboard_env as oe  # noqa: E402
import onboard_verify as ov  # noqa: E402

oa = sb.onboard_apply
NOW = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
BASE = ["--offline", "--no-tests", "--force"]


class Scripted(object):
    """input_fn answering by prompt substring; anything unmatched gets Enter."""

    def __init__(self, rules):
        self.rules = list(rules)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        for sub, ans in self.rules:
            if sub in prompt:
                return ans
        return ""


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.root, self.home, self.repo, self.env = sb.make_sandbox()
        self.addCleanup(sb.cleanup, self.root)
        sb.assert_not_real_home(self.home)

    def cli(self, *args, home=None, extra_env=None):
        """Run the sandbox repo copy's onboard.py in a subprocess. Returns (exit code, stdout+stderr)."""
        env = dict(self.env)
        env.update(extra_env or {})
        argv = [sys.executable, str(self.repo / "scripts" / "onboard.py"), "--home", str(home or self.home)] + list(args)
        p = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(self.root), timeout=600)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def run_main(self, args, input_fn=None, interactive=False, getpass_fn=None):
        """In-process main() with the sandbox environment; returns (code, output)."""
        out = io.StringIO()
        fn = input_fn or Scripted([])
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(onboard, "REPO", self.repo):
            code = onboard.main(["--home", str(self.home)] + list(args), input_fn=fn,
                                getpass_fn=getpass_fn or (lambda p="": ""), out=out, interactive=interactive, now=NOW)
        return code, out.getvalue()

    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def install(self, *extra):
        return self.cli("--host", "claude", "--yes", *(BASE + list(extra)))

    def backups(self):
        d = self.home / ".claude" / "backups"
        return sorted(os.listdir(str(d))) if d.exists() else []


class ParserTests(unittest.TestCase):
    def test_flags_exist(self):
        p = onboard.build_parser()
        a = p.parse_args(["--check", "--preset", "economy", "--set", "builder=sonnet:low", "--set", "qa=sonnet",
                          "--model-id", "k=v", "--allow-opus", "all", "--allow-max", "advisor", "--mode", "bypass",
                          "--confirm-bypass", "--skip-bypass-prompt", "--components", "skill", "--python", "x", "-y",
                          "--dry-run", "--json", "--online", "--no-tests", "--e2e", "--force", "--home", "h",
                          "--host", "codex", "--list-presets", "--show-routing", "--uninstall", "--offline"])
        self.assertEqual(a.set, ["builder=sonnet:low", "qa=sonnet"])
        self.assertTrue(a.yes and a.dry_run and a.e2e)
        with self.assertRaises(SystemExit):
            p.parse_args(["--hooks"])

    def test_our_scripts_match_apply_hooks(self):
        self.assertEqual(tuple(oe.OUR_SCRIPTS), tuple(h["script"] for h in oa.HOOKS.values()))
        self.assertEqual(tuple(oe.OUR_SCRIPTS), tuple(oa.OUR_SCRIPTS))

    def test_bad_usage_exit_codes(self):
        out = io.StringIO()
        self.assertEqual(onboard.main(["--no-such-flag"], out=out), 2)
        self.assertEqual(onboard.main(["--online", "--offline", "--list-presets"], out=out), 2)

    def test_confirm_kw_only_when_engine_knows_it(self):
        self.assertEqual(onboard._confirm_kw(lambda a, confirmed_bypass=False: 0, True), {"confirmed_bypass": True})
        self.assertEqual(onboard._confirm_kw(lambda a: 0, True), {})


class ReadOnlyFlows(SandboxCase):
    def test_list_presets(self):
        code, out = self.cli("--list-presets")
        self.assertEqual(code, 0, out)
        for name in ("balanced", "economy", "max-quality", "zai-glm", "legacy-all-opus", "custom"):
            self.assertIn(name, out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_show_routing_zai_glm_uses_remaps(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.home)
        code, out = self.cli("--show-routing", "--preset", "zai-glm")
        self.assertEqual(code, 0, out)
        self.assertIn("glm-5.3", out)
        self.assertIn("jev-architect", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_show_routing_active_and_errors(self):
        code, out = self.cli("--show-routing")
        self.assertEqual(code, 0, out)
        self.assertIn("claude-opus", out)
        code, out = self.cli("--show-routing", "--preset", "zai-glm")  # no remaps in a fresh home
        self.assertEqual(code, 2)
        self.assertIn("--model-id", out)
        code, out = self.cli("--show-routing", "--preset", "zai-glm", "--model-id", "deep=glm-5.3",
                             "--model-id", "work=glm-5.3")
        self.assertEqual(code, 0, out)

    def test_check_json_on_legacy_owner_home(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.home)
        code, out = self.cli("--check", "--json", "--offline")
        self.assertNotIn(FAKE_TOKEN, out)
        data = json.loads(out)
        self.assertIs(data["doctor"]["install"]["legacy"], True)
        self.assertEqual(data["doctor"]["mode"]["mode"], "safe")
        self.assertEqual(data["suggested_preset"], "zai-glm")
        self.assertIn("gate_registered", data)
        self.assertEqual(code, 0 if data["ok"] else 1)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_check_text_fresh_home_fails_without_traceback(self):
        code, out = self.cli("--check")
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("Check FAILED", out)
        self.assertEqual(sb.snapshot(self.home), {})


class InstallFlows(SandboxCase):
    def test_dry_run_changes_nothing(self):
        code, out = self.cli("--host", "claude", "--dry-run", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN: nothing was changed.", out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_dry_run_json(self):
        code, out = self.cli("--host", "claude", "--dry-run", "--yes", "--json", *BASE)
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["exit"], 0)
        self.assertTrue(any("DRY RUN" in ln for ln in data["log"]))

    def test_auto_host_without_claude_refuses(self):
        code, out = self.cli("--yes", *BASE)
        self.assertEqual(code, 2, out)
        self.assertIn("Claude Code was not found", out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_non_claude_host_is_reported_only(self):
        code, out = self.cli("--host", "codex", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing was installed for Codex", out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_install_then_repeat_is_a_noop(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        claude = self.home / ".claude"
        self.assertTrue((claude / "jev" / "install-manifest.json").is_file())
        self.assertTrue((claude / "jev" / "agents.json").is_file())
        self.assertEqual(len(list((claude / "agents").glob("jev-*.md"))), 9)
        hooks = json.dumps(self.settings()["hooks"])
        self.assertIn("permission_gate.py", hooks)
        self.assertIn("dispatch_router.py", hooks)
        self.assertNotIn("prompt_router.py", hooks)
        self.assertIn("[OK] preflight", out)
        self.assertIn("Restart Claude Code", out)
        snap = sb.snapshot(self.home)
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change.", out)
        self.assertEqual(sb.snapshot(self.home), snap)

    def test_bypass_needs_confirmation_then_round_trip(self):
        code, out = self.cli("--host", "claude", "--mode", "bypass", "--yes", *BASE)
        self.assertEqual(code, 2, out)
        self.assertIn("--confirm-bypass", out)
        self.assertEqual(sb.snapshot(self.home), {})
        code, out = self.cli("--host", "claude", "--mode", "bypass", "--confirm-bypass", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        s = self.settings()
        self.assertEqual(s["permissions"]["defaultMode"], "bypassPermissions")
        self.assertNotIn("skipDangerousModePermissionPrompt", s)
        code, out = self.cli("--mode", "safe", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertNotIn("defaultMode", json.dumps(self.settings()))

    def test_skip_prompt_only_with_bypass(self):
        code, out = self.cli("--host", "claude", "--skip-bypass-prompt", "--yes", *BASE)
        self.assertEqual(code, 2, out)
        self.assertIn("--skip-bypass-prompt", out)
        self.assertEqual(sb.snapshot(self.home), {})
        code, out = self.cli("--host", "claude", "--mode", "bypass", "--confirm-bypass", "--skip-bypass-prompt",
                             "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertIs(self.settings()["skipDangerousModePermissionPrompt"], True)

    def test_opus_override_needs_allow_flag(self):
        code, out = self.cli("--host", "claude", "--set", "builder=opus", "--yes", *BASE)
        self.assertEqual(code, 2, out)
        self.assertIn("--allow-opus", out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_unknown_component_and_required_component(self):
        code, out = self.cli("--host", "claude", "--components", "skill,agents,gate,nope", "--yes", *BASE)
        self.assertEqual(code, 2, out)
        code, out = self.cli("--host", "claude", "--components", "skill,agents", "--yes", *BASE)
        self.assertEqual(code, 2, out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_confirmation_required_without_yes(self):
        code, out = self.cli("--host", "claude", *BASE)  # stdin is not a TTY and no --yes
        self.assertEqual(code, 2, out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_unparsable_settings_is_an_apply_error_not_a_traceback(self):
        p = self.home / ".claude" / "settings.json"
        p.parent.mkdir(parents=True)
        p.write_text("{not json", encoding="utf-8")
        before = sb.snapshot(self.home)
        code, out = self.cli("--host", "claude", "--yes", *BASE)
        self.assertEqual(code, 3, out)
        self.assertIn("not valid JSON", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_legacy_owner_install_dry_run_zai_glm(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.home)
        code, out = self.cli("--host", "claude", "--dry-run", "--yes", "--preset", "zai-glm", *BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("2 older jev hook entries replaced", out)
        self.assertIn("DRY RUN: nothing was changed.", out)
        self.assertNotIn("defaultMode", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_uninstall_restores_settings_byte_exact(self):
        sb.write_json(self.home / ".claude" / "settings.json", {"theme": "dark"})
        orig = (self.home / ".claude" / "settings.json").read_bytes()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertNotEqual((self.home / ".claude" / "settings.json").read_bytes(), orig)
        code, out = self.cli("--uninstall", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.home / ".claude" / "settings.json").read_bytes(), orig)
        self.assertFalse((self.home / ".claude" / "jev" / "install-manifest.json").exists())
        self.assertFalse(oa.lexists(self.home / ".claude" / "skills" / "jev-orchestrator"))

    def test_uninstall_dry_run_and_empty(self):
        code, out = self.cli("--uninstall", "--yes", *BASE)
        self.assertEqual(code, 0, out)
        self.assertEqual(sb.snapshot(self.home), {})
        self.install()
        snap = sb.snapshot(self.home)
        code, out = self.cli("--uninstall", "--dry-run", *BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN: nothing was changed.", out)
        self.assertEqual(sb.snapshot(self.home), snap)
        code, out = self.cli("--uninstall", *BASE)  # no TTY, no --yes
        self.assertEqual(code, 2, out)
        self.assertEqual(sb.snapshot(self.home), snap)


class InteractiveFlows(SandboxCase):
    def test_interactive_zai_glm_safe_install(self):
        sb.legacy_owner_home(self.home, self.repo)
        fn = Scripted([("Preset", "zai-glm"), ("Mode", "1"), ("Proceed?", "y"), ("Create ~/.config", "n")])
        code, out = self.run_main(["--host", "claude"] + BASE, input_fn=fn, interactive=True)
        self.assertEqual(code, 0, out)
        cfg = json.loads((self.home / ".claude" / "jev" / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["models"]["deep"], "glm-5.3")
        self.assertEqual(cfg["models"]["work"], "glm-5.3")
        s = self.settings()
        self.assertEqual(s["env"]["ANTHROPIC_AUTH_TOKEN"], FAKE_TOKEN)  # untouched
        self.assertNotIn("defaultMode", json.dumps(s))
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertTrue(any("Proceed?" in p for p in fn.prompts))

    def test_wrong_bypass_word_writes_nothing(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.home)
        fn = Scripted([("Preset", "balanced"), ("Mode", "2"), ("bypass to continue", "nope"), ("Proceed?", "y")])
        code, out = self.run_main(["--host", "claude"] + BASE, input_fn=fn, interactive=True)
        self.assertEqual(code, 2, out)
        self.assertIn("BYPASS MODE", out)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_bypass_declined_at_key_confirmation_writes_nothing(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.home)
        fn = Scripted([("Preset", "balanced"), ("Mode", "2"), ("bypass to continue", "bypass"),
                       ("Write these keys?", "n")])
        code, out = self.run_main(["--host", "claude"] + BASE, input_fn=fn, interactive=True)
        self.assertEqual(code, 2, out)
        self.assertIn("permissions.defaultMode", out)
        self.assertEqual(sb.snapshot(self.home), before)

    def test_bypass_with_both_confirmations_installs(self):
        sb.legacy_owner_home(self.home, self.repo)
        fn = Scripted([("Preset", "balanced"), ("Mode", "2"), ("bypass to continue", "bypass"),
                       ("Write these keys?", "y"), ("Proceed?", "y"), ("Create ~/.config", "n")])
        code, out = self.run_main(["--host", "claude"] + BASE, input_fn=fn, interactive=True)
        self.assertEqual(code, 0, out)
        s = self.settings()
        self.assertEqual(s["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIs(s["skipDangerousModePermissionPrompt"], True)  # the owner's own pre-existing value

    def test_interactive_opus_override_needs_typed_yes(self):
        fn = Scripted([("Type yes", "no")])
        code, out = self.run_main(["--host", "claude", "--preset", "balanced", "--set", "builder=opus"] + BASE,
                                  input_fn=fn, interactive=True)
        self.assertEqual(code, 2, out)
        self.assertIn("WARNING", out)
        self.assertEqual(sb.snapshot(self.home), {})

    def test_hidden_key_never_echoed(self):
        asked = []
        fn = Scripted([("Mode", "1"), ("Preset", "balanced"), ("Proceed?", "y"), ("Create ~/.config", "y")])
        code, out = self.run_main(["--host", "claude"] + BASE, input_fn=fn, interactive=True,
                                  getpass_fn=lambda p="": asked.append(p) or "tsk-fake-HIDDEN-KEY-1")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(asked), 1)
        self.assertNotIn("tsk-fake-HIDDEN-KEY-1", out)
        self.assertIn("tsk-fake-HIDDEN-KEY-1", (self.home / ".config" / "typesafe" / ".env").read_text())
        self.assertNotIn("tsk-fake-HIDDEN-KEY-1", (self.home / ".claude" / "jev" / "install-manifest.json").read_text())

    def test_e2e_with_non_real_home_exits_2(self):
        other = self.root / "otherhome"
        other.mkdir()
        env = dict(self.env, HOME=str(other), USERPROFILE=str(other))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(onboard, "REPO", self.repo):
            code = onboard.main(["--home", str(self.home), "--host", "claude", "--yes", "--e2e"] + BASE, out=out,
                                interactive=False)
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("real home", out.getvalue())
        self.assertEqual(sb.snapshot(self.home), {})


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_offline_tests_environment(self):
        calls = []

        def fake_run(argv, env=None, timeout=120, cwd=None, stdin=None):
            calls.append((argv, dict(env), timeout))
            return 0, "ok", ""

        base = {"JEV_CONFIG": "/should/go", "PATH": "x", "HOME": "/real/home"}
        res = ov.offline_tests(sb.REPO, sys.executable, base, fake_run)
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 3)
        for argv, env, timeout in calls:
            self.assertNotIn("JEV_CONFIG", env)
            self.assertEqual(env["TYPESAFE_BASE_URL"], "https://127.0.0.1:9")
            self.assertEqual(timeout, 600)
            self.assertNotEqual(env["HOME"], "/real/home")
            self.assertEqual(env["HOME"], env["USERPROFILE"])
        self.assertIn("--check", calls[2][0])

    def test_preflight_sets_active_config(self):
        cfg = self.home / ".claude" / "jev" / "agents.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{}", encoding="utf-8")
        seen = {}

        def fake_run(argv, env=None, timeout=120, cwd=None, stdin=None):
            seen.update(env=env, argv=argv)
            return 0, json.dumps({"ok": True, "config": str(cfg), "warnings": [], "message": "ok"}), ""

        res = ov.preflight(self.home, sb.REPO, sys.executable, {"JEV_CONFIG": "/other"}, fake_run)
        self.assertTrue(res["ok"])
        self.assertEqual(seen["env"]["JEV_CONFIG"], str(cfg))
        self.assertIn("--all", seen["argv"])
        self.assertIn(str(self.home / ".claude" / "agents"), seen["argv"])

    def test_sample_route_skips(self):
        self.assertEqual(ov.sample_route(self.home, sb.REPO, sys.executable, {"present": True}, offline=True)["status"],
                         "skipped")
        self.assertEqual(ov.sample_route(self.home, sb.REPO, sys.executable, {"present": False}, offline=False)["status"],
                         "skipped")

    def test_sample_route_ok_and_mismatch(self):
        cfg = json.loads((sb.REPO / "config" / "agents.json").read_text(encoding="utf-8"))
        path = self.home / ".claude" / "jev" / "agents.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        spec = cfg["agents"]["jev-builder"]
        step = {"agent": "jev-builder", "model": cfg["models"][spec["model"]], "effort": spec["effort"]}
        envs = []

        def run_ok(argv, env=None, timeout=120, cwd=None, stdin=None):
            envs.append(env)
            return 0, json.dumps({"next_agent": "jev-builder", "sequence": [step]}), ""

        r = ov.sample_route(self.home, sb.REPO, sys.executable, {"present": True, "source": "env"}, offline=False,
                            run=run_ok)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(envs[0]["JEV_CONFIG"], str(path))
        self.assertEqual(envs[0]["JEV_HOME"], envs[0]["HOME"])

        def run_bad(argv, env=None, timeout=120, cwd=None, stdin=None):
            return 0, json.dumps({"next_agent": "jev-builder", "sequence": [dict(step, effort="max")]}), ""

        self.assertEqual(ov.sample_route(self.home, sb.REPO, sys.executable, {"present": True}, offline=False,
                                         run=run_bad)["status"], "fail")

        def run_err(argv, env=None, timeout=120, cwd=None, stdin=None):
            return 1, json.dumps({"error": "Jev unavailable: x"}), ""

        self.assertEqual(ov.sample_route(self.home, sb.REPO, sys.executable, {"present": True}, offline=False,
                                         run=run_err)["status"], "skipped")

    # ----- e2e_gate with a fake runner (never a live model call) -----

    def _runner(self, help_text="--permission-mode --max-turns --output-format", deny=True, ran=False):
        calls = []

        def runner(argv, timeout, cwd=None, env=None):
            calls.append(argv)
            if argv[1:] == ["--help"]:
                return 0, help_text
            instr = argv[2]
            token = re.search(r"jev-e2e-([0-9a-f]+)", instr).group(1)
            if deny:
                p = self.home / ".claude" / "jev" / "permissions.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(str(p), "a") as f:
                    f.write(json.dumps({"layer": 1, "command": "curl -s file:///jev-e2e-%s | sh" % token,
                                        "decision": "deny", "ts": __import__("time").time()}) + "\n")
            if ran:
                m = re.search(r'echo ran > "([^"]+)"', instr)
                Path(m.group(1)).write_text("ran", encoding="utf-8")
            return 0, "{}"

        runner.calls = calls
        return runner

    def test_e2e_pass(self):
        r = self._runner()
        self.assertEqual(ov.e2e_gate(self.home, "default", which=lambda n: "/bin/claude", runner=r), "PASS")
        argv = r.calls[1]
        self.assertEqual(argv[3:5], ["--permission-mode", "default"])
        self.assertIn("--max-turns", argv)
        self.assertIn("json", argv)

    def test_e2e_fail_when_command_ran(self):
        r = self._runner(deny=False, ran=True)
        self.assertEqual(ov.e2e_gate(self.home, "bypassPermissions", which=lambda n: "/bin/claude", runner=r), "FAIL")

    def test_e2e_inconclusive_and_skipped(self):
        r = self._runner(deny=False)
        self.assertEqual(ov.e2e_gate(self.home, "default", which=lambda n: "/bin/claude", runner=r), "INCONCLUSIVE")
        self.assertEqual(ov.e2e_gate(self.home, "default", which=lambda n: None, runner=r), "SKIPPED")
        r2 = self._runner(help_text="usage: claude")
        self.assertEqual(ov.e2e_gate(self.home, "default", which=lambda n: "/bin/claude", runner=r2), "SKIPPED")

    def test_verify_ok_rules(self):
        def run(argv, env=None, timeout=120, cwd=None, stdin=None):
            return 0, json.dumps({"ok": False, "message": "bad"}), ""

        v = ov.verify(self.home, sb.REPO, sys.executable, run_tests=False, offline=True, run=run,
                      settings={"hooks": [], "jev_env": {}})
        self.assertFalse(v["ok"])
        self.assertEqual({s["name"]: s["status"] for s in v["steps"]}["offline tests"], "skipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
