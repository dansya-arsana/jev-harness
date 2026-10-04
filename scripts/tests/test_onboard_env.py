#!/usr/bin/env python3
"""Offline tests for scripts/onboard_env.py: fake homes, fake which/run, explicit env, no registry reads."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import onboard_env as oe  # noqa: E402

SECRET = "sk-fake-SECRET-123"
KEY = "tsk-fake-KEYVALUE-999"


def no_which(name):
    return None


def fake_run(argv, timeout):
    return 0, "2.1.240 (Claude Code)\nsecond line\n"


def _fwd(p):
    return str(p).replace("\\", "/")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def write_settings(self, data):
        p = self.home / ".claude" / "settings.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p


class Hosts(Base):
    def rows(self, env=None, which=no_which):
        return {r["host"]: r for r in oe.detect_hosts(self.home, env or {}, which, fake_run)}

    def test_codex_by_config_dir(self):
        (self.home / ".codex").mkdir()
        r = self.rows()["codex"]
        self.assertTrue(r["detected"])
        self.assertIsNone(r["binary"])

    def test_codex_by_env(self):
        d = Path(self.tmp.name) / "cx"
        d.mkdir()
        self.assertTrue(self.rows({"CODEX_HOME": str(d)})["codex"]["detected"])
        self.assertFalse(self.rows()["codex"]["detected"])

    def test_zcode_by_appdata(self):
        ad = Path(self.tmp.name) / "appdata"
        (ad / "ZCode").mkdir(parents=True)
        self.assertTrue(self.rows({"APPDATA": str(ad)})["zcode"]["detected"])

    def test_claude_version(self):
        r = self.rows(which=lambda n: "/bin/claude" if n == "claude" else None)["claude"]
        self.assertTrue(r["detected"])
        self.assertEqual(r["version"], "2.1.240 (Claude Code)")
        self.assertIsNone(self.rows(which=lambda n: "/bin/codex" if n == "codex" else None)["codex"]["version"])

    def test_only_claude_installable(self):
        inst = [h for h, r in self.rows().items() if r["installable"]]
        self.assertEqual(inst, ["claude"])
        self.assertEqual(oe.SUPPORT["codex"]["installs"], [])

    def test_labels_match_research_matrix(self):
        text = (REPO / "docs" / "jev" / "host-research.md").read_text(encoding="utf-8")
        table = {}
        for line in text.splitlines():
            if line.startswith("|") and not line.startswith("|---") and "Gate hook" not in line:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                table[cells[0]] = cells[1:]
        self.assertEqual(len(table), 7)
        for host in oe.HOSTS:
            s = oe.SUPPORT[host]
            self.assertEqual([s[k] for k in oe._FIELDS], table[s["label"]], host)


class Key(Base):
    def test_sources(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        r = oe.key_status(self.home, repo, {}, read_registry=False)
        self.assertFalse(r["present"])
        r = oe.key_status(self.home, repo, {"TYPESAFE_API_KEY": KEY}, read_registry=False)
        self.assertEqual((r["present"], r["source"]), (True, "env"))
        self.assertNotIn(KEY, json.dumps(r))
        f = self.home / ".config" / "typesafe" / ".env"
        f.parent.mkdir(parents=True)
        f.write_text("TYPESAFE_API_KEY=%s\n" % KEY)
        r = oe.key_status(self.home, repo, {}, read_registry=False)
        self.assertEqual(r["source"], "~/.config/typesafe/.env")
        self.assertNotIn(KEY, json.dumps(r))
        f.unlink()
        (repo / ".env").write_text('TYPESAFE_API_KEY="%s"\n' % KEY)
        r = oe.key_status(self.home, repo, {}, read_registry=False)
        self.assertEqual(r["source"], "repo .env")
        self.assertNotIn(KEY, json.dumps(r))


class Settings(Base):
    def fixture(self):
        hooks = self.home / ".claude" / "skills" / "jev-orchestrator" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "permission_gate.py").write_text("# gate")
        py = Path(self.tmp.name) / "py.exe"
        py.write_text("")
        gate_cmd = '"%s" "%s"' % (_fwd(py), _fwd(hooks / "permission_gate.py"))
        s = {"env": {"ANTHROPIC_AUTH_TOKEN": SECRET, "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3",
                     "JEV_GUARD": "1"},
             "skipDangerousModePermissionPrompt": True,
             "hooks": {
                 "PreToolUse": [
                     {"matcher": "Bash", "hooks": [{"type": "command", "command": gate_cmd}]},
                     {"matcher": "Agent", "hooks": [{"type": "command", "command": "other-tool --flag"}]}],
                 "UserPromptSubmit": [{"hooks": [{
                     "type": "command",
                     "command": "python3 ~/.claude/skills/jev-orchestrator/hooks/prompt_router.py"}]}]}}
        self.write_settings(s)
        return s

    def test_owner_like(self):
        self.fixture()
        st = oe.claude_settings(self.home)
        self.assertEqual(sorted(h["ours"] for h in st["hooks"]), [False, True, True])
        gate = [h for h in st["hooks"] if "permission_gate" in h["command"]][0]
        self.assertTrue(gate["ours"] and gate["script_exists"] and gate["python_exists"])
        legacy = [h for h in st["hooks"] if "prompt_router" in h["command"]][0]
        self.assertTrue(legacy["ours"])
        self.assertEqual(legacy["script_path"],
                         _fwd(self.home) + "/.claude/skills/jev-orchestrator/hooks/prompt_router.py")
        self.assertFalse(legacy["script_exists"])
        foreign = [h for h in st["hooks"] if "other-tool" in h["command"]][0]
        self.assertFalse(foreign["ours"])
        self.assertEqual(st["jev_env"], {"JEV_GUARD": "1"})
        self.assertEqual(st["base_url_host"], "api.z.ai")
        self.assertEqual(oe.suggest_preset(st, {}), "zai-glm")
        self.assertEqual(oe.suggest_preset(oe.claude_settings(self.home / "nope"), {}), "balanced")
        rep = oe.doctor(self.home, REPO, {"TYPESAFE_API_KEY": KEY}, which=no_which, run=fake_run,
                        read_registry=False)
        blob = json.dumps(rep, default=str) + oe.format_doctor(rep)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(KEY, blob)
        self.assertTrue(any(i["name"] == "remap" for i in rep["items"]))
        self.assertTrue(rep["critical"])  # the legacy prompt_router script is missing

    def test_dangling_critical(self):
        self.fixture()
        os.remove(self.home / ".claude" / "skills" / "jev-orchestrator" / "hooks" / "permission_gate.py")
        rep = oe.doctor(self.home, REPO, {}, which=no_which, run=fake_run, read_registry=False)
        self.assertTrue(any("permission_gate.py" in c and "blocked" in c for c in rep["critical"]))

    def test_unparsable_critical(self):
        self.write_settings("{ not json")
        rep = oe.doctor(self.home, REPO, {}, which=no_which, run=fake_run, read_registry=False)
        self.assertTrue(rep["critical"])
        self.assertTrue(rep["settings"]["error"])

    def test_mode_classification(self):
        self.assertEqual(oe.current_mode(oe.claude_settings(self.home), None)["mode"], "not-installed")
        self.write_settings({"permissions": {"defaultMode": "acceptEdits"}})
        m = oe.current_mode(oe.claude_settings(self.home), None)
        self.assertEqual((m["mode"], m["default_mode"]), ("not-installed", "acceptEdits"))
        self.write_settings({"permissions": {"defaultMode": "bypassPermissions"}})
        self.assertEqual(oe.current_mode(oe.claude_settings(self.home), None)["mode"], "unprotected-bypass")
        hooks = self.home / ".claude" / "skills" / "jev-orchestrator" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "permission_gate.py").write_text("")
        cmd = 'python "%s"' % _fwd(hooks / "permission_gate.py")
        gate_hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}
        self.write_settings({"permissions": {"defaultMode": "bypassPermissions"}, "hooks": gate_hooks})
        m = oe.current_mode(oe.claude_settings(self.home), None)
        self.assertEqual((m["mode"], m["gate_registered"]), ("bypass", True))
        self.write_settings({"hooks": gate_hooks})
        self.assertEqual(oe.current_mode(oe.claude_settings(self.home), None)["mode"], "safe")

    def test_remap_warnings(self):
        self.fixture()
        st = oe.claude_settings(self.home)
        self.assertEqual(len(oe.remap_warnings(st, {}, {"opus": "claude-opus-5-5"})), 3)
        # agents already pinned to the remap targets (preset zai-glm): only the same-model note remains
        w = oe.remap_warnings(st, {}, {"deep": "glm-5.3", "work": "glm-5.3"})
        self.assertEqual(len(w), 1)
        self.assertIn("same model", w[0])

    def test_config_models_prefers_active(self):
        active = self.home / ".claude" / "jev"
        active.mkdir(parents=True)
        (active / "agents.json").write_text('{"models": {"work": "glm-5.3"}}', encoding="utf-8")
        self.assertEqual(oe._config_models(REPO, self.home), {"work": "glm-5.3"})
        self.assertTrue(any(str(v).startswith("claude-") for v in oe._config_models(REPO).values()))


class Install(Base):
    def test_existing_install_empty(self):
        i = oe.existing_install(self.home, REPO)
        self.assertFalse(i["legacy"])
        self.assertFalse(i["skill_link"]["exists"])
        self.assertEqual(set(i["agents"].values()), {"missing"})
        self.assertEqual(len(i["agents"]), 9)
        self.assertIsNone(i["active_config"])


if __name__ == "__main__":
    unittest.main()
