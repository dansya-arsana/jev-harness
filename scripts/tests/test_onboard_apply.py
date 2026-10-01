"""Offline tests for scripts/onboard_apply.py. Every test runs in a temporary sandbox (never the real home).

Run: export TYPESAFE_BASE_URL=https://127.0.0.1:9; python -m unittest discover -s scripts/tests -p "test_onboard_apply.py" -v
"""
import copy
import datetime
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onboard_sandbox as sb  # noqa: E402
from onboard_sandbox import FAKE_TOKEN  # noqa: E402

oa = sb.onboard_apply
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
PY = sys.executable.replace("\\", "/")


def at(n):
    return NOW + datetime.timedelta(seconds=n)


def stub_ok(command, claude_mode, **kw):
    return {"ok": True, "claude_mode": claude_mode, "probes": [], "error": None}


def smoke_ok(command, **kw):
    return {"ok": True, "command": command, "error": None}


def wanted_for(home, comps=("gate", "dispatch", "router"), platform="linux"):
    return oa._wanted_hooks(list(comps), PY, home, platform)


FOREIGN = {"type": "command", "command": "node /opt/foreign/guard.js", "timeout": 5}


def ours(script, event="PreToolUse", matcher="Bash", style="old"):
    cmd = {"old": "python3 ~/.claude/skills/jev-orchestrator/hooks/%s",
           "win": '"C:\\Py\\python.exe" "C:\\Users\\x\\.claude\\skills\\jev-orchestrator\\hooks\\%s"'}[style] % script
    g = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
    if matcher:
        g["matcher"] = matcher
    return g


class PureTests(unittest.TestCase):
    def test_is_ours(self):
        self.assertEqual(oa.is_ours('"C:\\a\\jev-orchestrator\\hooks\\permission_gate.py"'), "permission_gate.py")
        self.assertEqual(oa.is_ours("python3 ~/.claude/skills/jev-orchestrator/hooks/prompt_router.py"), "prompt_router.py")
        self.assertIsNone(oa.is_ours("node /opt/foreign/guard.js"))
        self.assertIsNone(oa.is_ours(None))

    def test_hook_command_is_absolute(self):
        c = oa.hook_command("C:\\Python\\python.exe", "/h", "permission_gate.py")
        self.assertTrue(c.startswith('"C:/Python/python.exe" "'))
        self.assertNotIn("~", c)
        self.assertNotIn("\\", c)
        self.assertTrue(c.endswith('/.claude/skills/jev-orchestrator/hooks/permission_gate.py"'))

    def test_pick_python(self):
        path, warns = oa.pick_python()
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(oa.pick_python(override=sys.executable)[0], oa.norm(sys.executable))
        with self.assertRaises(oa.ApplyError):
            oa.pick_python(override=os.path.join(os.path.dirname(sys.executable), "nope-python"))

    def test_claude_running(self):
        win = lambda argv: '"claude.exe","123","Console","1","100 K"\n'
        self.assertTrue(oa.claude_running("win32", win))
        self.assertFalse(oa.claude_running("win32", lambda argv: "INFO: No tasks are running\n"))
        self.assertTrue(oa.claude_running("linux", lambda argv: "bash\nclaude\n"))
        self.assertFalse(oa.claude_running("linux", lambda argv: "bash\nclaudette\n"))
        self.assertFalse(oa.claude_running("linux", mock.Mock(side_effect=OSError("boom"))))


class MergeHooksTests(unittest.TestCase):
    def setUp(self):
        self.wanted = wanted_for("/h")

    def test_legacy_entries_replaced_and_foreign_kept(self):
        s = {"theme": "dark", "hooks": {"PreToolUse": [
            ours("permission_gate.py"), {"matcher": "Bash", "hooks": [FOREIGN]}, ours("dispatch_router.py", matcher="Agent|Task", style="win")],
            "Stop": [{"hooks": [FOREIGN]}]}}
        new, notes = oa.merge_hooks(s, self.wanted)
        cmds = [h["command"] for g in new["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(sum(1 for c in cmds if "permission_gate.py" in c), 1)
        self.assertEqual(sum(1 for c in cmds if "dispatch_router.py" in c), 1)
        self.assertIn(FOREIGN["command"], cmds)
        self.assertTrue(all(c.startswith('"') for c in cmds if oa.is_ours(c)))
        self.assertEqual(new["theme"], "dark")
        self.assertEqual(new["hooks"]["Stop"], [{"hooks": [FOREIGN]}])
        self.assertIn("2 older jev hook entries replaced", notes)
        self.assertEqual(new["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"], 15)
        self.assertNotIn("matcher", new["hooks"]["UserPromptSubmit"][0])

    def test_duplicates_collapse(self):
        s = {"hooks": {"PreToolUse": [ours("permission_gate.py"), ours("permission_gate.py", style="win")]}}
        new, _ = oa.merge_hooks(s, {"permission_gate.py": self.wanted["permission_gate.py"]})
        self.assertEqual(len(new["hooks"]["PreToolUse"]), 1)

    def test_shared_group_keeps_foreign_hook(self):
        g = {"matcher": "Bash", "hooks": [ours("permission_gate.py")["hooks"][0], FOREIGN]}
        s = {"hooks": {"PreToolUse": [g]}}
        new, _ = oa.merge_hooks(s, {"permission_gate.py": self.wanted["permission_gate.py"]})
        shared = new["hooks"]["PreToolUse"][0]
        self.assertEqual(shared["hooks"], [FOREIGN])
        self.assertEqual(shared["matcher"], "Bash")
        self.assertEqual(len(new["hooks"]["PreToolUse"]), 2)

    def test_deselected_router_removed_and_empty_list_dropped(self):
        s, _ = oa.merge_hooks({}, self.wanted)
        self.assertIn("UserPromptSubmit", s["hooks"])
        w2 = {k: v for k, v in self.wanted.items() if k != "prompt_router.py"}
        new, _ = oa.merge_hooks(s, w2)
        self.assertNotIn("UserPromptSubmit", new["hooks"])
        self.assertIn("PreToolUse", new["hooks"])

    def test_second_merge_changes_nothing(self):
        once, _ = oa.merge_hooks({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN]}]}}, self.wanted)
        twice, notes = oa.merge_hooks(once, self.wanted)
        self.assertEqual(once, twice)
        self.assertEqual(notes, [])
        self.assertEqual(list(json.dumps(once)), list(json.dumps(twice)))

    def test_remove_our_hooks(self):
        s, _ = oa.merge_hooks({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN]}]}}, self.wanted)
        new, n = oa.remove_our_hooks(s)
        self.assertEqual(n, 3)
        self.assertEqual(new, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN]}]}})

    def test_bad_shape_refused(self):
        with self.assertRaises(oa.ApplyError):
            oa.merge_hooks({"hooks": []}, self.wanted)

    def test_platform_matchers(self):
        w_win = wanted_for("/h", ("gate",), "win32")["permission_gate.py"]
        w_nix = wanted_for("/h", ("gate",), "linux")["permission_gate.py"]
        self.assertIn("PowerShell", w_win["matcher"])
        self.assertNotIn("PowerShell", w_nix["matcher"])


class ApplyModeTests(unittest.TestCase):
    def test_bypass_absent_roundtrip(self):
        s = {"theme": "dark"}
        new, items, w = oa.apply_mode(s, "bypass", False, [])
        self.assertEqual(new["permissions"]["defaultMode"], "bypassPermissions")
        self.assertNotIn("skipDangerousModePermissionPrompt", new)
        self.assertEqual(items[0]["old"], {"present": False, "value": None})
        back, items2, _ = oa.apply_mode(new, "safe", False, items)
        self.assertEqual(back, s)
        self.assertEqual(items2, [])

    def test_accept_edits_restored(self):
        s = {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(ls)"]}}
        new, items, _ = oa.apply_mode(s, "bypass", False, [])
        self.assertEqual(items[0]["old"], {"present": True, "value": "acceptEdits"})
        back, _, _ = oa.apply_mode(new, "safe", False, items)
        self.assertEqual(back, s)

    def test_bypass_set_by_user_left_alone(self):
        s = {"permissions": {"defaultMode": "bypassPermissions"}}
        new, items, w = oa.apply_mode(s, "bypass", False, [])
        self.assertEqual(new, s)
        self.assertEqual(items, [])
        back, items2, warns = oa.apply_mode(s, "safe", False, [])
        self.assertEqual(back, s)
        self.assertTrue(any("did not set it" in x for x in warns))

    def test_user_changed_value_not_clobbered(self):
        s = {}
        new, items, _ = oa.apply_mode(s, "bypass", False, [])
        new["permissions"]["defaultMode"] = "plan"
        back, _, warns = oa.apply_mode(new, "safe", False, items)
        self.assertEqual(back["permissions"]["defaultMode"], "plan")
        self.assertTrue(warns)

    def test_skip_prompt_only_when_asked(self):
        s = {"skipDangerousModePermissionPrompt": False}
        new, items, _ = oa.apply_mode(s, "bypass", True, [])
        self.assertIs(new["skipDangerousModePermissionPrompt"], True)
        back, _, _ = oa.apply_mode(new, "safe", True, items)
        self.assertEqual(back, s)
        new2, items2, _ = oa.apply_mode({}, "bypass", False, [])
        self.assertNotIn("skipDangerousModePermissionPrompt", new2)

    def test_unknown_mode(self):
        with self.assertRaises(oa.ApplyError):
            oa.apply_mode({}, "yolo", False, [])


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.root, self.home, self.repo, self.env = sb.make_sandbox()
        self.addCleanup(sb.cleanup, self.root)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                         "TYPESAFE_BASE_URL": "https://127.0.0.1:9"})
        p.start()
        self.addCleanup(p.stop)
        sb.assert_not_real_home(self.home)
        self.agents = {f.name: f.read_text(encoding="utf-8") for f in sorted((self.repo / "agents").glob("jev-*.md"))}
        self.config = json.loads((self.repo / "config" / "agents.json").read_text(encoding="utf-8"))
        self.rules = (self.repo / "config" / "global-rules.md").read_text(encoding="utf-8")
        self.settings = self.home / ".claude" / "settings.json"
        self.n = 0

    def plan(self, **over):
        kw = dict(confirmed_bypass=True, components=["skill", "agents", "gate", "dispatch"], agents=self.agents, active_config=self.config,
                  mode="safe", skip_bypass_prompt=False, python=PY, rules_block=self.rules,
                  choice={"preset": "balanced", "components": []}, platform=sys.platform)
        kw.update(over)
        return oa.plan_install(self.home, self.repo, **kw)

    def apply(self, actions, **over):
        self.n += 1
        kw = dict(mode="safe", choice={"preset": "balanced"}, now=at(self.n * 10), selftest=stub_ok, smoke=smoke_ok, confirmed_bypass=True)
        kw.update(over)
        return oa.apply(actions, self.home, self.repo, **kw)

    def backups(self):
        d = self.home / ".claude" / "backups"
        return sorted(os.listdir(str(d))) if d.exists() else []

    def sj(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def write_settings(self, data):
        sb.write_json(self.settings, data)
        return self.settings.read_bytes()


class LinkTests(SandboxCase):
    def test_link_roundtrip_and_remove_refuses_real_dir(self):
        a, b, link = self.root / "a", self.root / "b", self.root / "l"
        a.mkdir()
        b.mkdir()
        (a / "f.txt").write_text("a")
        oa.make_link(str(a), str(link))
        self.assertTrue(oa.is_link(link))
        self.assertTrue((link / "f.txt").exists())
        oa.remove_link(link)
        self.assertFalse(oa.lexists(link))
        self.assertTrue((a / "f.txt").exists())
        with self.assertRaises(oa.ApplyError):
            oa.remove_link(a)
        self.assertTrue((a / "f.txt").exists())

    def test_link_to_other_target_replaced_then_restored(self):
        other = self.repo / "skill" / "jev-orchestrator" / "hooks"  # inside the running repo: may be re-linked
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(other), str(link))
        actions, _ = self.plan(components=["skill"], active_config=None)
        self.assertEqual([a.kind for a in actions], ["link_skill"])
        self.apply(actions)
        self.assertTrue(oa.same_path(oa.link_target(link), self.repo / "skill" / "jev-orchestrator"))
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(500))
        self.assertTrue(oa.is_link(link))
        self.assertTrue(oa.same_path(oa.link_target(link), other))
        self.assertTrue(other.exists())

    def test_link_to_foreign_target_replaced_but_not_restored(self):
        other = self.home / "other"
        (other / "hooks").mkdir(parents=True)
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(other), str(link))
        actions, _ = self.plan(components=["skill"], active_config=None)
        self.apply(actions)
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertTrue(any("will not be re-linked; previous target was" in w for w in uw))
        oa.apply_uninstall(ua, self.home, self.repo, now=at(500))
        self.assertFalse(oa.lexists(link))
        self.assertTrue((other / "hooks").exists())

    def test_real_dir_moved_to_backup_intact(self):
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        (link / "mine").mkdir(parents=True)
        (link / "mine" / "note.txt").write_text("keep me")
        actions, _ = self.plan(components=["skill"], active_config=None)
        res = self.apply(actions)
        self.assertTrue(oa.is_link(link))
        bk = Path(res["backup_dir"]) / ".claude" / "skills" / "jev-orchestrator" / "mine" / "note.txt"
        self.assertEqual(bk.read_text(), "keep me")
        ua, _ = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(500))
        self.assertFalse(oa.is_link(link))
        self.assertEqual((link / "mine" / "note.txt").read_text(), "keep me")


class PlanTests(SandboxCase):
    def test_planning_leaves_snapshot_unchanged(self):
        sb.legacy_owner_home(self.home, self.repo)
        before = sb.snapshot(self.root)
        self.plan(components=list(oa.COMPONENTS))
        oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertEqual(before, sb.snapshot(self.root))

    def test_clean_install_kinds_in_order(self):
        actions, warns = self.plan(components=list(oa.COMPONENTS))
        kinds = [a.kind for a in actions]
        self.assertEqual(kinds, ["link_skill"] + ["write_agent"] * 9 + ["write_config", "seed_conditions", "create_keyfile",
                                                                       "rules", "settings"])
        self.assertEqual(len(self.agents), 9)

    def test_minimal_kinds(self):
        actions, _ = self.plan()
        self.assertEqual([a.kind for a in actions], ["link_skill"] + ["write_agent"] * 9 + ["write_config", "settings"])

    def test_bypass_without_gate_raises(self):
        with self.assertRaises(oa.ApplyError):
            self.plan(components=["skill", "agents"], mode="bypass")
        with self.assertRaises(oa.ApplyError):
            self.plan(skip_bypass_prompt=True)

    def test_invalid_settings_json_never_overwritten(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{not json", encoding="utf-8")
        with self.assertRaises(oa.ApplyError):
            self.plan()
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_zero_actions_create_nothing(self):
        before = sb.snapshot(self.root)
        res = oa.apply([], self.home, self.repo, mode="safe", choice={}, now=NOW)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["backup_dir"])
        self.assertEqual(before, sb.snapshot(self.root))

    def test_format_plan(self):
        actions, warns = self.plan()
        txt = oa.format_plan(actions, warns, dry_run=True)
        self.assertIn("1. ", txt)
        self.assertIn("Undo: python scripts/onboard.py --uninstall", txt)
        self.assertIn("DRY RUN: nothing was changed.", txt)
        self.assertIn("Nothing has changed yet.", oa.format_plan(actions, warns, dry_run=False))
        txt.encode("ascii")


class InstallTests(SandboxCase):
    def test_full_install_with_real_selftest_then_repeat_is_noop(self):
        existing = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(components=["skill", "agents", "gate", "dispatch", "router", "rules", "keyfile"])
        res = oa.apply(actions, self.home, self.repo, mode="safe", choice={"preset": "balanced"}, now=NOW)
        self.assertTrue(res["ok"])
        self.assertTrue(res["selftest"]["default"]["ok"])
        s = self.sj()
        self.assertEqual(s["theme"], "dark")
        scripts = sorted(oa.is_ours(h["command"]) for ev in s["hooks"].values() for g in ev for h in g["hooks"])
        self.assertEqual(scripts, ["dispatch_router.py", "permission_gate.py", "prompt_router.py"])
        for ev in s["hooks"].values():
            for g in ev:
                for h in g["hooks"]:
                    self.assertTrue(h["command"].startswith('"'))
                    self.assertNotIn("~", h["command"])
        manifest = oa.load_manifest(self.home)
        self.assertEqual(manifest["schema"], 1)
        kinds = {i["kind"] for i in manifest["items"]}
        self.assertTrue({"link", "file", "seed", "hook", "settings_file", "rules_block"} <= kinds)
        self.assertEqual(len(list((self.home / ".claude" / "agents").glob("jev-*.md"))), 9)
        self.assertTrue((self.home / ".claude" / "jev" / "agents.json").exists())
        self.assertEqual((self.home / ".claude" / "jev" / "conditions.json").read_text(), "[]\n")
        self.assertIn("TYPESAFE_API_KEY=", (self.home / ".config" / "typesafe" / ".env").read_text())
        self.assertIn("jev-harness:begin", (self.home / ".claude" / "CLAUDE.md").read_text())
        self.assertEqual(len(res["backup_dir"] and self.backups()), 1)
        # second run: nothing planned and no new backup dir
        before = self.backups()
        again, _ = self.plan(components=["skill", "agents", "gate", "dispatch", "router", "rules", "keyfile"])
        self.assertEqual(again, [])
        self.assertEqual(self.backups(), before)
        # the backup of settings.json holds the original bytes
        bk = Path(res["backup_dir"]) / ".claude" / "settings.json"
        self.assertEqual(bk.read_bytes(), existing)

    def test_legacy_owner_home(self):
        orig = sb.legacy_owner_home(self.home, self.repo)
        raw = self.settings.read_bytes()
        actions, warns = self.plan(components=["skill", "agents", "gate", "dispatch"])
        self.assertEqual([a.kind for a in actions], ["write_config", "settings"])
        settings_action = actions[-1]
        self.assertIn("2 older jev hook entries replaced", settings_action.details)
        self.assertFalse(any("defaultMode" in d for d in settings_action.details))
        res = oa.apply(actions, self.home, self.repo, mode="safe", choice={"preset": "zai-glm"}, now=NOW)
        s = self.sj()
        self.assertEqual(s["env"], orig["env"])
        self.assertEqual(s["env"]["ANTHROPIC_AUTH_TOKEN"], FAKE_TOKEN)
        self.assertIs(s["skipDangerousModePermissionPrompt"], True)
        self.assertEqual(s["model"], "opus")
        cmds = [h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("node /opt/foreign/guard.js", cmds)
        self.assertEqual(sum(1 for c in cmds if oa.is_ours(c)), 2)
        self.assertNotIn("permissions", s)
        self.assertTrue(all(c.startswith('"') for c in cmds if oa.is_ours(c)))
        # uninstall restores settings.json byte for byte
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(100))
        self.assertEqual(self.settings.read_bytes(), raw)
        self.assertFalse((self.home / ".claude" / "jev" / "install-manifest.json").exists())

    def test_install_then_uninstall_is_clean_with_external_edit(self):
        self.write_settings({"theme": "dark", "env": {"X": "1"}})
        actions, _ = self.plan(components=["skill", "agents", "gate", "dispatch", "router", "rules"])
        self.apply(actions)
        s = self.sj()
        s["extra"] = {"added": "later"}
        self.write_settings(s)
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertTrue(any("left in place" in w for w in uw))
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertEqual(self.sj(), {"theme": "dark", "env": {"X": "1"}, "extra": {"added": "later"}})
        self.assertFalse(oa.is_link(self.home / ".claude" / "skills" / "jev-orchestrator"))
        self.assertEqual(list((self.home / ".claude" / "agents").glob("jev-*.md")), [])
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((self.home / ".claude" / "jev" / "agents.json").exists())

    def test_uninstall_created_settings_file_deleted(self):
        actions, _ = self.plan()
        self.apply(actions)
        ua, _ = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertFalse(self.settings.exists())

    def test_edited_agent_gets_note_backup_and_survives_uninstall(self):
        actions, _ = self.plan()
        self.apply(actions)
        agent = self.home / ".claude" / "agents" / "jev-builder.md"
        agent.write_text("my own edit\n", encoding="utf-8")
        cfg2 = copy.deepcopy(self.config)
        actions2, _ = self.plan(agents=dict(self.agents, **{"jev-builder.md": self.agents["jev-builder.md"] + "\nnew line\n"}))
        a = [x for x in actions2 if x.kind == "write_agent"]
        self.assertEqual(len(a), 1)
        self.assertIn("replaces a copy you edited (backup kept)", a[0].details)
        res = self.apply(actions2)
        saved = Path(res["backup_dir"]) / ".claude" / "agents" / "jev-builder.md"
        self.assertEqual(saved.read_text(encoding="utf-8"), "my own edit\n")
        agent.write_text("edited again\n", encoding="utf-8")
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertEqual(agent.read_text(encoding="utf-8"), "edited again\n")

    def test_rules_block_keeps_crlf_and_restores(self):
        md = self.home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True)
        original = b"# mine\r\n\r\nline two\r\n"
        md.write_bytes(original)
        actions, _ = self.plan(components=["skill", "agents", "gate", "rules"])
        self.apply(actions)
        data = md.read_bytes()
        self.assertIn(b"jev-harness:begin", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        # rerun with a different block updates it
        a2, _ = self.plan(components=["skill", "agents", "gate", "rules"], rules_block=self.rules + "\nextra line\n")
        self.assertEqual([a.kind for a in a2], ["rules"])
        self.assertIn("update", a2[0].summary)
        ua, _ = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertEqual(md.read_bytes(), original)

    def test_rules_block_deselected_is_removed(self):
        a, _ = self.plan(components=["skill", "agents", "gate", "rules"])
        self.apply(a)
        a2, _ = self.plan(components=["skill", "agents", "gate"])
        self.assertEqual([x.kind for x in a2], ["rules"])
        self.apply(a2)
        self.assertNotIn("jev-harness", (self.home / ".claude" / "CLAUDE.md").read_text())

    def test_keyfile_with_key_never_printed(self):
        actions, warns = self.plan(components=["skill", "agents", "gate", "keyfile"], key="tsk-fake-KEY-999")
        k = [a for a in actions if a.kind == "create_keyfile"][0]
        self.assertNotIn("tsk-fake-KEY-999", oa.format_plan(actions, warns, dry_run=True))
        self.assertNotIn("tsk-fake-KEY-999", repr(actions))
        self.apply(actions)
        self.assertIn("TYPESAFE_API_KEY=tsk-fake-KEY-999", Path(k.path).read_text())
        none, _ = self.plan(components=["skill", "agents", "gate", "keyfile"], key_found=True)
        self.assertFalse([a for a in none if a.kind == "create_keyfile"])

    def test_no_secret_in_plan_result_or_manifest(self):
        sb.legacy_owner_home(self.home, self.repo)
        actions, warns = self.plan(components=list(oa.COMPONENTS))
        txt = oa.format_plan(actions, warns, dry_run=False)
        self.assertNotIn(FAKE_TOKEN, txt)
        self.assertNotIn(FAKE_TOKEN, repr(actions))
        res = self.apply(actions)
        self.assertNotIn(FAKE_TOKEN, json.dumps(res, default=str))
        self.assertNotIn(FAKE_TOKEN, (self.home / ".claude" / "jev" / "install-manifest.json").read_text())
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertNotIn(FAKE_TOKEN, oa.format_plan(ua, uw, dry_run=True))


class ModeAndSafetyTests(SandboxCase):
    def test_bypass_install_then_safe(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        self.assertTrue(any("permissions.defaultMode" in d for d in actions[-1].details))
        res = self.apply(actions, mode="bypass")
        s = self.sj()
        self.assertEqual(s["permissions"]["defaultMode"], "bypassPermissions")
        self.assertNotIn("skipDangerousModePermissionPrompt", s)
        back, _ = self.plan(mode="safe")
        self.assertEqual([a.kind for a in back], ["settings"])
        self.apply(back, mode="safe")
        s2 = self.sj()
        self.assertNotIn("permissions", s2)
        self.assertIn("hooks", s2)
        again, _ = self.plan(mode="safe")
        self.assertEqual(again, [])
        # skip prompt only with the flag
        sk, _ = self.plan(mode="bypass", skip_bypass_prompt=True)
        self.apply(sk, mode="bypass")
        self.assertIs(self.sj()["skipDangerousModePermissionPrompt"], True)

    def test_failing_selftest_before_write_leaves_settings_untouched(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        bad = lambda c, m, **kw: {"ok": False, "error": "no deny", "probes": []}
        with self.assertRaises(oa.ApplyError):
            self.apply(actions, mode="bypass", selftest=bad)
        self.assertEqual(self.settings.read_bytes(), orig)

    def test_failing_selftest_after_write_restores_settings(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        calls = []

        def flaky(command, mode, **kw):
            calls.append((command, mode))
            return {"ok": len(calls) < 3, "error": "canary not denied", "probes": []}

        with self.assertRaises(oa.ApplyError) as cm:
            self.apply(actions, mode="bypass", selftest=flaky)
        self.assertIn("bypass mode was NOT enabled", str(cm.exception))
        self.assertEqual(self.settings.read_bytes(), orig)
        self.assertEqual(calls[0][1], "default")
        self.assertEqual(calls[1][1], "bypassPermissions")
        self.assertEqual(calls[2][1], "bypassPermissions")
        self.assertEqual(calls[2][0], calls[0][0])
        self.assertTrue(calls[0][0].startswith('"'))
        # earlier actions stay in the manifest so --uninstall can clean them up
        m = oa.load_manifest(self.home)
        self.assertTrue(any(i["kind"] == "link" for i in m["items"]))
        self.assertFalse(any(i["kind"] == "hook" for i in m["items"]))

    def test_failing_smoke_blocks_registration(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(components=["skill", "agents", "gate", "dispatch"])
        bad = lambda c, **kw: {"ok": False, "error": "exit code 1"}
        with self.assertRaises(oa.ApplyError):
            self.apply(actions, smoke=bad)
        self.assertEqual(self.settings.read_bytes(), orig)

    def test_missing_script_blocks_registration(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(components=["skill", "agents", "gate"])
        os.unlink(str(self.repo / "skill" / "jev-orchestrator" / "hooks" / "permission_gate.py"))
        with self.assertRaises(oa.ApplyError):
            self.apply(actions)
        self.assertEqual(self.settings.read_bytes(), orig)

    def test_concurrent_edit_detected(self):
        self.write_settings({"theme": "dark"})
        actions, _ = self.plan()
        edited = self.write_settings({"theme": "light"})
        with self.assertRaises(oa.ApplyError) as cm:
            self.apply(actions)
        self.assertIn("changed while onboarding ran", str(cm.exception))
        self.assertEqual(self.settings.read_bytes(), edited)

    def test_write_json_atomic_rechecks_hash(self):
        self.write_settings({"a": 1})
        _, sha = oa.read_json(self.settings)
        self.settings.write_text('{"a": 2}', encoding="utf-8")
        with self.assertRaises(oa.ApplyError) as cm:
            oa.write_json_atomic(self.settings, {"a": 3}, sha)
        self.assertIn("changed while onboarding ran; nothing written", str(cm.exception))
        self.assertEqual(json.loads(self.settings.read_text()), {"a": 2})
        self.assertEqual([p for p in os.listdir(str(self.settings.parent)) if p.startswith(".jev-tmp")], [])

    def test_running_claude_keeps_link(self):
        actions, _ = self.plan(components=["skill", "agents", "gate", "dispatch"])
        self.apply(actions)
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: True)
        self.assertTrue(any("restart Claude Code, then run --uninstall again" in w for w in uw))
        self.assertNotIn("un_link", [a.kind for a in ua])
        self.assertNotIn("un_manifest", [a.kind for a in ua])
        oa.apply_uninstall(ua, self.home, self.repo, now=at(700))
        self.assertTrue(oa.is_link(link))
        self.assertTrue((self.home / ".claude" / "jev" / "install-manifest.json").exists())
        s = self.sj() if self.settings.exists() else {}
        self.assertFalse(any(oa.is_ours(h.get("command")) for ev in s.get("hooks", {}).values() for g in ev for h in g["hooks"]))
        # claude closed: second uninstall finishes the job
        ua2, _ = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertIn("un_link", [a.kind for a in ua2])
        oa.apply_uninstall(ua2, self.home, self.repo, now=at(800))
        self.assertFalse(oa.is_link(link))
        self.assertFalse((self.home / ".claude" / "jev" / "install-manifest.json").exists())

    def test_force_removes_link_while_running(self):
        actions, _ = self.plan(components=["skill", "agents", "gate"])
        self.apply(actions)
        ua, _ = oa.plan_uninstall(self.home, self.repo, force=True, running=lambda: True)
        self.assertIn("un_link", [a.kind for a in ua])

    def test_legacy_agents_without_manifest(self):
        sb.legacy_owner_home(self.home, self.repo)
        agents = self.home / ".claude" / "agents"
        (agents / "jev-qa.md").write_text("customised", encoding="utf-8")
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        res = oa.apply_uninstall(ua, self.home, self.repo, now=at(5))
        self.assertEqual(list(agents.glob("jev-*.md")), [])
        self.assertEqual((Path(res["backup_dir"]) / ".claude" / "agents" / "jev-qa.md").read_text(), "customised")
        self.assertFalse(oa.is_link(self.home / ".claude" / "skills" / "jev-orchestrator"))
        s = self.sj()
        self.assertEqual(s["env"]["ANTHROPIC_AUTH_TOKEN"], FAKE_TOKEN)
        cmds = [h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(cmds, ["node /opt/foreign/guard.js"])


class SelftestTests(SandboxCase):
    def gate_command(self):
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(self.repo / "skill" / "jev-orchestrator"), str(link))
        return oa.hook_command(PY, self.home, "permission_gate.py")

    def test_gate_default_and_bypass(self):
        cmd = self.gate_command()
        d = oa.selftest_gate(cmd, "default")
        self.assertTrue(d["ok"], d)
        self.assertEqual([p["expected"] for p in d["probes"]], ["deny", "(no output)", "ask"])
        self.assertEqual([p["got"] for p in d["probes"]], ["deny", "(no output)", "ask"])
        b = oa.selftest_gate(cmd, "bypassPermissions")
        self.assertTrue(b["ok"], b)
        self.assertEqual([p["got"] for p in b["probes"]], ["deny", "(no output)", "(no output)"])
        self.assertIn(d["shell"], ("cmd", "sh"))
        self.assertIsNone(d["error"])
        self.assertEqual(d["probes"][0]["command"], oa.CANARY)

    def test_selftest_does_not_touch_the_sandbox_home(self):
        cmd = self.gate_command()
        before = sb.snapshot(self.home)
        oa.selftest_gate(cmd, "default")
        self.assertEqual(before, sb.snapshot(self.home))

    def test_missing_script_fails_selftest(self):
        cmd = oa.hook_command(PY, self.home, "permission_gate.py")
        r = oa.selftest_gate(cmd, "default")
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_runner_decisions(self):
        def runner_for(outputs):
            it = iter(outputs)
            return lambda c, i, e, t: next(it)
        deny = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}})
        ask = json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}})
        ok = oa.selftest_gate("x", "default", runner=runner_for([(0, deny, ""), (0, "", ""), (0, ask, "")]))
        self.assertTrue(ok["ok"])
        no_deny = oa.selftest_gate("x", "default", runner=runner_for([(0, "", ""), (0, "", ""), (0, ask, "")]))
        self.assertFalse(no_deny["ok"])
        self.assertIn("canary", no_deny["error"])
        bad_exit = oa.selftest_gate("x", "default", runner=runner_for([(2, deny, ""), (0, "", ""), (0, ask, "")]))
        self.assertFalse(bad_exit["ok"])
        junk = oa.selftest_gate("x", "default", runner=runner_for([(0, "not json", ""), (0, "", ""), (0, ask, "")]))
        self.assertFalse(junk["ok"])

    def test_runner_gets_exact_payload_and_temp_home(self):
        seen = []

        def runner(c, i, e, t):
            seen.append((c, json.loads(i), e))
            return 0, "", ""
        oa.selftest_gate("CMD", "bypassPermissions", settings_env={"JEV_GATE": "off", "OTHER": "x"}, runner=runner)
        c, payload, env = seen[0]
        self.assertEqual(c, "CMD")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertEqual(payload["permission_mode"], "bypassPermissions")
        self.assertEqual(payload["session_id"], "jev-onboard-selftest")
        self.assertEqual(payload["tool_input"]["command"], oa.CANARY)
        self.assertEqual(env["JEV_GATE"], "off")
        self.assertNotIn("OTHER", env)
        self.assertEqual(env["HOME"], env["USERPROFILE"])
        self.assertFalse(sb._under(env["HOME"], self.home) and env["HOME"] == str(self.home))

    def test_smoke_hook(self):
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(self.repo / "skill" / "jev-orchestrator"), str(link))
        r = oa.smoke_hook(oa.hook_command(PY, self.home, "dispatch_router.py"))
        self.assertTrue(r["ok"], r)
        bad = oa.smoke_hook(oa.hook_command(PY, self.home, "missing_script.py"))
        self.assertFalse(bad["ok"])


class ReviewFixTests(SandboxCase):
    def raw_manifest(self, items, **extra):
        m = {"schema": 1, "repo": oa.norm(self.repo), "items": items, "backups": []}
        m.update(extra)
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", m)

    # (1) undecodable files are never treated as absent
    def test_undecodable_claude_md_aborts_and_is_untouched(self):
        md = self.home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True)
        md.write_bytes(b"caf\xe9 \xff\xfe not utf8\n")
        before = sb.snapshot(self.root)
        with self.assertRaises(oa.ApplyError):
            self.plan(components=["skill", "agents", "gate", "rules"])
        with self.assertRaises(oa.ApplyError):
            self.plan(components=["skill", "agents", "gate"])
        self.assertEqual(before, sb.snapshot(self.root))

    def test_undecodable_settings_agent_and_config_abort(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b"\xff\xfe\x00{")
        with self.assertRaises(oa.ApplyError):
            self.plan()
        self.settings.unlink()
        agent = self.home / ".claude" / "agents" / "jev-qa.md"
        agent.parent.mkdir(parents=True)
        agent.write_bytes(b"\xe9\xe9")
        with self.assertRaises(oa.ApplyError):
            self.plan()
        agent.unlink()
        cfg = self.home / ".claude" / "jev" / "agents.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_bytes(b"\xe9{")
        with self.assertRaises(oa.ApplyError):
            self.plan()

    def test_existing_claude_md_is_always_backed_up(self):
        md = self.home / ".claude" / "CLAUDE.md"
        md.parent.mkdir(parents=True)
        md.write_bytes(b"# mine\n")
        actions, _ = self.plan(components=["skill", "agents", "gate", "rules"])
        res = self.apply(actions)
        self.assertEqual((Path(res["backup_dir"]) / ".claude" / "CLAUDE.md").read_bytes(), b"# mine\n")

    # (2) the skill path is restored when linking fails
    def test_make_link_failure_restores_previous_link(self):
        other = self.root / "other"
        (other / "hooks").mkdir(parents=True)
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(other), str(link))
        real = oa.make_link
        target = oa.norm(self.repo / "skill" / "jev-orchestrator")

        def flaky(t, l):
            if oa.norm(t) == target:
                raise oa.ApplyError("forced failure")
            return real(t, l)

        actions, _ = self.plan(components=["skill"], active_config=None)
        with mock.patch.object(oa, "make_link", flaky):
            with self.assertRaises(oa.ApplyError) as cm:
                self.apply(actions)
        self.assertIn("previous skill path restored", str(cm.exception))
        self.assertTrue(oa.is_link(link))
        self.assertTrue(oa.same_path(oa.link_target(link), other))

    def test_make_link_failure_restores_previous_dir(self):
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        (link / "hooks").mkdir(parents=True)
        (link / "hooks" / "permission_gate.py").write_text("legacy")
        actions, _ = self.plan(components=["skill"], active_config=None)
        with mock.patch.object(oa, "make_link", side_effect=OSError("nope")):
            with self.assertRaises(oa.ApplyError):
                self.apply(actions)
        self.assertFalse(oa.is_link(link))
        self.assertEqual((link / "hooks" / "permission_gate.py").read_text(), "legacy")

    # (3) crafted manifests
    def test_manifest_traversal_and_absolute_paths_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("precious")
        bad_items = [
            [{"kind": "file", "path": ".claude/agents/../../outside.txt", "sha256": "x", "created": True, "backup": None}],
            [{"kind": "file", "path": oa.norm(outside), "sha256": "x", "created": True, "backup": None}],
            [{"kind": "file", "path": ".claude/agents/jev-x.md", "sha256": "x", "created": False,
              "backup": ".claude/backups/../../outside.txt"}],
            [{"kind": "file", "path": ".claude/agents/jev-x.md", "sha256": "x", "created": False, "backup": ".claude/settings.json"}],
            [{"kind": "settings_file", "path": ".claude/settings.json", "created": False, "sha_after": "x",
              "original_backup": ".claude/backups/../../outside.txt"}],
            [{"kind": "settings_file", "path": "../outside.txt", "created": True, "sha_after": "x", "original_backup": None}],
            [{"kind": "link", "path": ".claude/skills/jev-orchestrator", "target": "x",
              "previous": {"type": "dir", "backup": "../../outside.txt"}}],
            [{"kind": "link", "path": ".claude/skills/jev-orchestrator", "target": "x",
              "previous": {"type": "dir", "backup": oa.norm(outside)}}],
            [{"kind": "rules_block", "path": ".claude/CLAUDE.md", "sha256": "x", "created_file": False,
              "sha_after": "x", "original_backup": ".claude/backups/..\\..\\outside.txt"}],
            [{"kind": "file", "path": ".claude/agents/evil.md", "sha256": "x", "created": True, "backup": None}],
            [{"kind": "setting", "path": ".claude/settings.json", "key": "env", "old": {"present": False}, "new": 1}],
        ]
        for items in bad_items:
            self.raw_manifest(items)
            with self.assertRaises(oa.ApplyError, msg=str(items)):
                oa.plan_uninstall(self.home, self.repo, running=lambda: False)
            with self.assertRaises(oa.ApplyError, msg=str(items)):
                oa.apply_uninstall([], self.home, self.repo, now=NOW)
        self.assertEqual(outside.read_text(), "precious")

    def test_manifest_path_through_escaping_link_rejected(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.home / ".claude").mkdir(parents=True)
        oa.make_link(str(outside), str(self.home / ".claude" / "agents"))
        self.raw_manifest([{"kind": "file", "path": ".claude/agents/jev-x.md", "sha256": "x", "created": True, "backup": None}])
        with self.assertRaises(oa.ApplyError):
            oa.plan_uninstall(self.home, self.repo, running=lambda: False)

    def test_manifest_python_is_not_run_unless_it_looks_like_python(self):
        fake = self.root / "tool.exe"
        fake.write_bytes(b"MZ")
        txt = self.root / "python.txt"
        txt.write_text("x")
        self.raw_manifest([], choice={"python": oa.norm(fake)})
        calls = []
        real = oa._py_ok

        def spy(p):
            calls.append(oa.norm(p))
            return real(p)

        with mock.patch.object(oa, "_py_ok", spy):
            for cand in (oa.norm(fake), oa.norm(txt), "C:/does/not/exist/python.exe", "relative/python", None):
                path, _ = oa.pick_python(manifest_python=cand)
                self.assertEqual(path, oa.norm(sys.executable))
        self.assertNotIn(oa.norm(fake), calls)
        self.assertNotIn(oa.norm(txt), calls)
        # a real python recorded in the manifest is accepted
        with mock.patch.object(oa, "_py_ok", spy):
            self.assertEqual(oa.pick_python(manifest_python=sys.executable)[0], oa.norm(sys.executable))

    # minor: links as settings / CLAUDE.md
    def test_settings_symlink_refused_even_when_dangling(self):
        self.settings.parent.mkdir(parents=True)
        real_target = self.root / "dotfiles-settings.json"
        try:
            os.symlink(str(real_target), str(self.settings))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create file symlinks here")
        with self.assertRaises(oa.ApplyError):
            self.plan()
        self.assertFalse(real_target.exists())

    def test_link_flag_refuses_settings_and_claude_md(self):
        self.write_settings({"theme": "dark"})
        md = self.home / ".claude" / "CLAUDE.md"
        md.write_text("x")
        names = ("settings.json", "CLAUDE.md")
        with mock.patch.object(oa, "is_link", lambda p: str(p).endswith(names)):
            with self.assertRaises(oa.ApplyError):
                self.plan()
            with self.assertRaises(oa.ApplyError):
                self.plan(components=["skill", "agents", "gate", "rules"])

    # minor: probes never write the real jev dir
    def test_hook_env_scrubs_inherited_jev_paths(self):
        keep = self.root / "realish-jev"
        keep.mkdir()
        with mock.patch.dict(os.environ, {"JEV_HOME": str(keep), "JEV_CONFIG": str(keep / "c.json"),
                                          "JEV_DISPATCH_FAKE": str(keep / "f.json")}):
            env = oa._hook_env({"JEV_HOME": str(keep), "JEV_GATE": "on", "JEV_AGENTS_DIR": "x"}, "TMPDIR")
        self.assertEqual(env["JEV_HOME"], "TMPDIR")
        self.assertNotIn("JEV_CONFIG", env)
        self.assertNotIn("JEV_DISPATCH_FAKE", env)
        self.assertNotIn("JEV_AGENTS_DIR", env)
        self.assertEqual(env["JEV_GATE"], "on")
        self.assertEqual(env["HOME"], "TMPDIR")

    def test_probes_do_not_write_inherited_jev_home(self):
        keep = self.root / "realish-jev"
        keep.mkdir()
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(self.repo / "skill" / "jev-orchestrator"), str(link))
        with mock.patch.dict(os.environ, {"JEV_HOME": str(keep)}):
            r = oa.selftest_gate(oa.hook_command(PY, self.home, "permission_gate.py"), "default")
            s = oa.smoke_hook(oa.hook_command(PY, self.home, "dispatch_router.py"))
        self.assertTrue(r["ok"] and s["ok"])
        self.assertEqual(os.listdir(str(keep)), [])
        self.assertEqual(os.listdir(str(self.home)), [".claude"])

    # minor: command quoting
    def test_hook_command_rejects_shell_active_characters(self):
        bad = ["$", "`", '"', "\n"] + (["%"] if sys.platform.startswith("win") else ["\\"])
        for ch in bad:
            with self.assertRaises(oa.ApplyError, msg=repr(ch)):
                oa.hook_command(PY, "/h/a%sb" % ch, "permission_gate.py")
            with self.assertRaises(oa.ApplyError, msg=repr(ch)):
                oa.hook_command("/usr/bin/py%sthon" % ch, "/h", "permission_gate.py")
        oa.hook_command("/usr/bin/python3", "/h/with space/it's", "permission_gate.py")

    # minor: key payload
    def test_key_payload_rules(self):
        comps = ["skill", "agents", "gate", "keyfile"]
        for bad in ("abc\ndef", "abc\r", "a\x00b", "a\tb"):
            with self.assertRaises(oa.ApplyError):
                self.plan(components=comps, key=bad)
        actions, warns = self.plan(components=comps, key="k\u00e9y")
        self.assertTrue(any("non-ASCII" in w for w in warns))
        self.apply(actions)
        kf = self.home / ".config" / "typesafe" / ".env"
        self.assertIn("TYPESAFE_API_KEY=k\u00e9y", kf.read_text(encoding="utf-8"))
        seed = [i for i in oa.load_manifest(self.home)["items"] if i["kind"] == "seed" and i["path"].endswith("typesafe/.env")][0]
        self.assertNotIn("sha256", seed)
        self.assertTrue(seed["created"])

    # minor: removing an existing gate must be loud
    def test_deselecting_gate_warns_about_removal(self):
        a, _ = self.plan()
        self.apply(a)
        a2, warns = self.plan(components=["skill", "agents", "dispatch"])
        self.assertTrue(any("REMOVED" in w and "gate" in w for w in warns), warns)
        _, quiet = self.plan()
        self.assertFalse(any("REMOVED" in w for w in quiet))

    # minor: bypass needs the confirmations
    def test_bypass_needs_confirmed_bypass(self):
        orig = self.write_settings({"theme": "dark"})
        with self.assertRaises(oa.ApplyError):
            self.plan(mode="bypass", confirmed_bypass=False)
        with self.assertRaises(oa.ApplyError):
            self.plan(mode="bypass", skip_bypass_prompt=True, confirmed_bypass=False)
        safe_actions, _ = self.plan(confirmed_bypass=False)
        self.assertTrue(safe_actions)
        byp, _ = self.plan(mode="bypass")
        before = sb.snapshot(self.root)
        with self.assertRaises(oa.ApplyError):
            oa.apply(byp, self.home, self.repo, mode="bypass", choice={}, now=NOW, selftest=stub_ok, smoke=smoke_ok)
        self.assertEqual(before, sb.snapshot(self.root))
        sk, _ = self.plan(mode="bypass", skip_bypass_prompt=True)
        with self.assertRaises(oa.ApplyError):
            oa.apply(sk, self.home, self.repo, mode="safe", choice={}, now=NOW, selftest=stub_ok, smoke=smoke_ok)
        self.assertEqual(self.settings.read_bytes(), orig)

    # minor: revert re-checks the file before restoring
    def test_revert_does_not_clobber_a_newer_edit(self):
        self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        calls = []

        def edits_then_fails(command, mode, **kw):
            calls.append(mode)
            if len(calls) == 3:
                sb.write_json(self.settings, {"theme": "someone else"})
                return {"ok": False, "error": "canary not denied", "probes": []}
            return {"ok": True, "probes": [], "error": None}

        with self.assertRaises(oa.ApplyError) as cm:
            self.apply(actions, mode="bypass", selftest=edits_then_fails)
        self.assertIn("NOT restored", str(cm.exception))
        self.assertEqual(self.sj(), {"theme": "someone else"})

    def test_revert_failure_is_reported_not_raised_raw(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        calls = []

        def fail_third(command, mode, **kw):
            calls.append(mode)
            return {"ok": len(calls) < 3, "error": "no deny", "probes": []}

        real = oa.write_bytes_atomic

        def boom(path, data, mode=None):
            if str(path).endswith("settings.json") and data == orig:
                raise PermissionError("locked")
            return real(path, data, mode)

        with mock.patch.object(oa, "write_bytes_atomic", boom):
            with self.assertRaises(oa.ApplyError) as cm:
                self.apply(actions, mode="bypass", selftest=fail_third)
        self.assertIn("COULD NOT restore settings.json", str(cm.exception))
        self.assertIn("bypass mode was NOT enabled", str(cm.exception))


class ManifestHardeningTests(SandboxCase):
    def raw_manifest(self, items, **extra):
        m = {"schema": 1, "repo": oa.norm(self.repo), "items": items, "backups": []}
        m.update(extra)
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", m)

    def bad_everywhere(self, items, **extra):
        """A crafted manifest must stop plan_install, plan_uninstall and apply_uninstall with a clean ApplyError."""
        self.raw_manifest(items, **extra)
        before = sb.snapshot(self.root)
        with self.assertRaises(oa.ApplyError, msg=str(items)):
            oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        with self.assertRaises(oa.ApplyError, msg=str(items)):
            oa.apply_uninstall([], self.home, self.repo, now=NOW)
        with self.assertRaises(oa.ApplyError, msg=str(items)):
            self.plan()
        with self.assertRaises(oa.ApplyError, msg=str(items)):
            oa.Ctx(self.home, self.repo, NOW)
        self.assertEqual(before, sb.snapshot(self.root))

    # (1) the manifest's python is never run or registered
    def test_manifest_python_is_never_executed(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        evil = downloads / "python.exe"
        evil.write_bytes(b"MZ")
        ran = []
        real = oa._py_ok

        def spy(p):
            ran.append(oa.norm(p))
            return real(p)

        cands = [oa.norm(evil), "//evil-host/share/python.exe", "\\\\evil-host\\share\\python.exe",
                 "\\\\?\\C:\\x\\python.exe", sys.executable]
        with mock.patch.object(oa, "_py_ok", spy):
            for c in cands:
                path, warns = oa.pick_python(manifest_python=c)
                self.assertEqual(path, oa.norm(sys.executable))
        self.assertNotIn(oa.norm(evil), ran)
        self.assertFalse(any("evil-host" in r or "\\\\" in r for r in ran))
        self.assertTrue(any("interpreter changed" in w for w in warns) is False)  # same interpreter: no warning

    def test_manifest_python_change_is_only_reported(self):
        evil = self.root / "python.exe"
        evil.write_bytes(b"MZ")
        path, warns = oa.pick_python(manifest_python=oa.norm(evil))
        self.assertTrue(any("interpreter changed" in w for w in warns))

    def test_explicit_python_override_is_revalidated(self):
        for bad in ("//host/share/python.exe", "relative/python", str(self.root / "missing" / "python.exe")):
            with self.assertRaises(oa.ApplyError):
                oa.pick_python(override=bad)
        junk = self.root / "python.exe"
        junk.write_bytes(b"MZ")
        with self.assertRaises(oa.ApplyError):
            oa.pick_python(override=str(junk))
        self.assertEqual(oa.pick_python(override=sys.executable)[0], oa.norm(sys.executable))

    # (2) setting items can never cause a bypass write
    def test_crafted_setting_items_rejected(self):
        base = {"kind": "setting", "path": ".claude/settings.json", "created_parent": False}
        crafted = [
            dict(base, key=oa.DEFAULT_MODE_KEY, old={"present": True, "value": "bypassPermissions"}, new="acceptEdits"),
            dict(base, key=oa.DEFAULT_MODE_KEY, old={"present": True, "value": "bypassPermissions"}, new="bypassPermissions"),
            dict(base, key=oa.SKIP_KEY, old={"present": True, "value": True}, new=True),
            dict(base, key=oa.SKIP_KEY, old={"present": True, "value": True}, new=False),
            dict(base, key=oa.SKIP_KEY, old={"present": True, "value": 1}, new=1),
            dict(base, key=oa.DEFAULT_MODE_KEY, old={"present": True, "value": {"x": 1}}, new="bypassPermissions"),
            dict(base, key=oa.DEFAULT_MODE_KEY, old="not a dict", new="bypassPermissions"),
            dict(base, key=oa.DEFAULT_MODE_KEY, old={"present": "yes"}, new="bypassPermissions"),
            dict(base, key=oa.DEFAULT_MODE_KEY, old={"present": False}, new="plan"),
        ]
        for item in crafted:
            self.bad_everywhere([item])

    def test_apply_mode_itself_refuses_bypass_restore(self):
        evil = [{"kind": "setting", "path": "x", "key": oa.DEFAULT_MODE_KEY, "new": "acceptEdits",
                 "old": {"present": True, "value": "bypassPermissions"}}]
        new, items, warns = oa.apply_mode({"permissions": {"defaultMode": "acceptEdits"}}, "safe", False, evil)
        self.assertEqual(new["permissions"]["defaultMode"], "acceptEdits")
        self.assertTrue(any("refusing" in w for w in warns))
        evil2 = [{"kind": "setting", "path": "x", "key": oa.SKIP_KEY, "new": False, "old": {"present": True, "value": True}}]
        new2, _, _ = oa.apply_mode({oa.SKIP_KEY: False}, "safe", False, evil2)
        self.assertIs(new2[oa.SKIP_KEY], False)

    def test_every_bypass_writer_is_gated(self):
        src = Path(oa.__file__).read_text(encoding="utf-8")
        writers = [m.start() for m in __import__("re").finditer(r"_set_key\(new,", src)]
        self.assertEqual(len(writers), 2)  # the bypass branch and the safe-mode restore of an old value
        for pos in writers:
            window = src[max(0, pos - 900):pos]
            self.assertTrue('mode == "bypass"' in window or 'old["value"] == "bypassPermissions"' in window)
        # behaviour: nothing but a confirmed bypass plan can ever add the bypass values
        self.write_settings({"theme": "dark"})
        for kw in (dict(), dict(mode="safe"), dict(components=["skill", "agents", "gate"])):
            actions, _ = self.plan(**kw)
            self.apply(actions)
            s = self.sj()
            self.assertNotIn("permissions", s)
            self.assertNotIn(oa.SKIP_KEY, s)
        ua, _ = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        s = self.sj()
        self.assertNotIn("permissions", s)
        self.assertNotIn(oa.SKIP_KEY, s)
        with self.assertRaises(oa.ApplyError):
            self.plan(mode="bypass", confirmed_bypass=False)

    def test_non_dict_previous_and_bad_types_raise_cleanly(self):
        link = {"kind": "link", "path": ".claude/skills/jev-orchestrator", "target": "x"}
        for prev in ("oops", ["a"], 5):
            self.bad_everywhere([dict(link, previous=prev)])
        fl = {"kind": "file", "path": ".claude/agents/jev-x.md", "sha256": "x", "created": True, "backup": None}
        self.bad_everywhere([dict(fl, previous="oops")])
        self.bad_everywhere([dict(fl, backup=5)])
        self.bad_everywhere([dict(fl, created="yes")])
        self.bad_everywhere([{"kind": "settings_file", "path": ".claude/settings.json", "sha_after": 5, "original_backup": None}])
        self.bad_everywhere(["not an object"])
        self.raw_manifest([], choice="notadict")
        with self.assertRaises(oa.ApplyError):
            oa.load_manifest_checked(self.home)
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", {"items": "x"})
        with self.assertRaises(oa.ApplyError):
            oa.load_manifest_checked(self.home)

    # (3) UNC / device paths never reach the filesystem
    def test_unc_paths_rejected_before_any_filesystem_call(self):
        seen = []
        names = ("exists", "isdir", "isfile", "realpath", "islink", "lexists")
        reals = {n: getattr(os.path, n) for n in names}

        def spy(name):
            def f(p, *a, **k):
                seen.append(str(p))
                return reals[name](p, *a, **k)
            return f

        uncs = ["\\\\evil-host\\share\\x", "//evil-host/share/x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x"]
        link = {"kind": "link", "path": ".claude/skills/jev-orchestrator", "target": "x"}
        cases = []
        for u in uncs:
            cases.append(([dict(link, target=u)], {}))
            cases.append(([], {"repo": u}))
        patches = [mock.patch.object(os.path, n, spy(n)) for n in names]
        for p in patches:
            p.start()
        try:
            for items, extra in cases:
                m = {"schema": 1, "repo": oa.norm(self.repo), "items": items}
                m.update(extra)
                with self.assertRaises(oa.ApplyError):
                    oa.validate_manifest(self.home, m, self.repo)
            # previous link targets: refused (not raised), never touched
            for u in uncs:
                m = {"schema": 1, "repo": oa.norm(self.repo), "items": [
                    dict(link, previous={"type": "link", "target": u, "backup": None})]}
                out = oa.validate_manifest(self.home, m, self.repo)
                self.assertEqual(out["items"][0]["previous"]["type"], "none")
                self.assertTrue(out["_warnings"])
        finally:
            for p in patches:
                p.stop()
        self.assertFalse([x for x in seen if x.replace("\\", "/").startswith("//")], seen)

    def test_relink_target_only_inside_running_repo(self):
        link = {"kind": "link", "path": ".claude/skills/jev-orchestrator", "target": "x"}
        outside = self.root / "arbitrary"
        (outside / "hooks").mkdir(parents=True)
        attacker = self.root / "attacker-repo"
        (attacker / "skill" / "jev-orchestrator" / "hooks").mkdir(parents=True)
        inside_home = self.home / "checkout"
        (inside_home / "hooks").mkdir(parents=True)
        repo_skill = self.repo / "skill" / "jev-orchestrator"

        def check(target, repo_field, running=None):
            m = {"schema": 1, "repo": oa.norm(repo_field), "items": [
                dict(link, previous={"type": "link", "target": oa.norm(target), "backup": None})]}
            return oa.validate_manifest(self.home, m, running or self.repo)["items"][0]["previous"]["type"]

        self.assertEqual(check(repo_skill, self.repo), "link")
        self.assertEqual(check(repo_skill / "hooks", self.repo), "link")
        # the manifest's own repo claim changes nothing
        self.assertEqual(check(repo_skill, attacker), "link")
        self.assertEqual(check(repo_skill, outside), "link")
        self.assertEqual(check(attacker / "skill" / "jev-orchestrator" / "hooks", attacker), "none")
        self.assertEqual(check(attacker / "skill" / "jev-orchestrator" / "hooks", self.repo), "none")
        self.assertEqual(check(outside, self.repo), "none")
        self.assertEqual(check(inside_home, self.repo), "none")  # no "anything under home" fallback
        self.assertEqual(check(self.home / "missing", self.repo), "none")
        self.assertEqual(check(repo_skill.parent.parent / "agents", self.repo), "none")  # skill link: skill folder only
        self.assertEqual(check(repo_skill, self.repo, running=attacker), "none")
        m = {"schema": 1, "items": [dict(link, previous={"type": "link", "target": oa.norm(repo_skill), "backup": None})]}
        self.assertEqual(oa.validate_manifest(self.home, m)["items"][0]["previous"]["type"], "none")  # no repo: no relink

    def test_attacker_repo_in_manifest_cannot_make_uninstall_relink(self):
        attacker = self.root / "attacker-repo"
        evil = attacker / "skill" / "jev-orchestrator"
        (evil / "hooks").mkdir(parents=True)
        (evil / "hooks" / "permission_gate.py").write_text("raise SystemExit(0)")
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        actions, _ = self.plan(components=["skill"], active_config=None)
        self.apply(actions)
        m = oa.load_manifest(self.home)
        m["repo"] = oa.norm(attacker)
        for it in m["items"]:
            if it["kind"] == "link":
                it["previous"] = {"type": "link", "target": oa.norm(evil / "hooks"), "backup": None}
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", m)
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertTrue(any("will not be re-linked; previous target was" in w for w in uw), uw)
        res = oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertFalse(oa.lexists(link))
        self.assertTrue(any("will not be re-linked" in w for w in res["warnings"]))

    def test_agent_previous_link_only_into_running_repo_agents(self):
        fl = {"kind": "file", "path": ".claude/agents/jev-qa.md", "sha256": "x", "created": False, "backup": None}
        attacker = self.root / "attacker-repo"
        (attacker / "skill" / "jev-orchestrator").mkdir(parents=True)
        (attacker / "agents").mkdir()
        (attacker / "agents" / "jev-qa.md").write_text("evil")

        def kind_for(target):
            m = {"schema": 1, "repo": oa.norm(attacker), "items": [dict(fl, previous={"type": "link", "target": oa.norm(target)})]}
            return oa.validate_manifest(self.home, m, self.repo)["items"][0]["previous"]["type"]

        self.assertEqual(kind_for(attacker / "agents" / "jev-qa.md"), "none")
        self.assertEqual(kind_for(self.repo / "agents" / "jev-qa.md"), "link")
        self.assertEqual(kind_for(self.repo / "skill" / "jev-orchestrator" / "SKILL.md"), "none")

    def test_crafted_choice_types_refused_cleanly(self):
        for ch in ({"components": "gate"}, {"components": [1]}, {"sets": {"a": 1}}, {"sets": 7}, {"allow_opus": [None]},
                   {"model_ids": 5}, {"model_ids": {"a": 1}}, {"preset": 3}, {"skip_bypass_prompt": "yes"}, {"python": []}):
            self.raw_manifest([], choice=ch)
            with self.assertRaises(oa.ApplyError, msg=str(ch)):
                oa.load_manifest_checked(self.home, self.repo)
        self.raw_manifest([], choice={"components": ["gate"], "sets": {"jev-builder": {"model": "sonnet", "effort": "low"}}, "model_ids": ["x=y"], "preset": "balanced",
                                      "skip_bypass_prompt": False, "python": "C:/x/python.exe", "allow_opus": [], "allow_max": []})
        oa.load_manifest_checked(self.home, self.repo)

    def test_refused_relink_is_reported_and_uninstall_still_works(self):
        outside = self.root / "arbitrary"
        (outside / "hooks").mkdir(parents=True)
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        actions, _ = self.plan(components=["skill"], active_config=None)
        self.apply(actions)
        m = oa.load_manifest(self.home)
        for it in m["items"]:
            if it["kind"] == "link":
                it["previous"] = {"type": "link", "target": oa.norm(outside), "backup": None}
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", m)
        ua, uw = oa.plan_uninstall(self.home, self.repo, running=lambda: False)
        self.assertTrue(any("will not be re-linked; previous target was" in w for w in uw))
        oa.apply_uninstall(ua, self.home, self.repo, now=at(900))
        self.assertFalse(oa.lexists(link))
        self.assertTrue((outside / "hooks").exists())

    # minor items
    def test_agent_symlink_aborts_plan(self):
        agents = self.home / ".claude" / "agents"
        agents.mkdir(parents=True)
        target = self.root / "somewhere.md"
        target.write_text("precious")
        try:
            os.symlink(str(target), str(agents / "jev-qa.md"))
        except (OSError, NotImplementedError):
            self.skipTest("cannot create file symlinks here")
        before = sb.snapshot(self.root)
        with self.assertRaises(oa.ApplyError):
            self.plan()
        self.assertEqual(before, sb.snapshot(self.root))
        self.assertEqual(target.read_text(), "precious")

    def test_spoofed_manifest_sha_does_not_suppress_backup(self):
        actions, _ = self.plan(components=["skill", "agents", "gate"])
        self.apply(actions)
        agent = self.home / ".claude" / "agents" / "jev-qa.md"
        agent.write_text("my own edit\n", encoding="utf-8")
        m = oa.load_manifest(self.home)
        for it in m["items"]:
            if it["kind"] == "file" and it["path"].endswith("jev-qa.md"):
                it["sha256"] = oa.sha_bytes(b"my own edit\n")  # spoof: claims the edit is what we wrote
        sb.write_json(self.home / ".claude" / "jev" / "install-manifest.json", m)
        a2, _ = self.plan(components=["skill", "agents", "gate"])
        res = self.apply(a2)
        saved = Path(res["backup_dir"]) / ".claude" / "agents" / "jev-qa.md"
        self.assertEqual(saved.read_text(encoding="utf-8"), "my own edit\n")

    def test_keyboard_interrupt_during_link_still_restores(self):
        other = self.home / "othercheckout"
        (other / "hooks").mkdir(parents=True)
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        oa.make_link(str(other), str(link))
        real = oa.make_link
        target = oa.norm(self.repo / "skill" / "jev-orchestrator")

        def interrupt(t, l):
            if oa.norm(t) == target:
                raise KeyboardInterrupt()
            return real(t, l)

        actions, _ = self.plan(components=["skill"], active_config=None)
        with mock.patch.object(oa, "make_link", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(actions)
        self.assertTrue(oa.same_path(oa.link_target(link), other))

    def test_failed_junction_leaves_no_empty_dir(self):
        link = self.home / ".claude" / "skills" / "jev-orchestrator"
        actions, _ = self.plan(components=["skill"], active_config=None)

        def half_made(t, l):
            os.makedirs(str(l))
            raise OSError("half made")

        with mock.patch.object(oa, "make_link", half_made):
            with self.assertRaises(oa.ApplyError):
                self.apply(actions)
        self.assertFalse(oa.lexists(link))

    def test_keyboard_interrupt_in_bypass_verify_restores_settings(self):
        orig = self.write_settings({"theme": "dark"})
        actions, _ = self.plan(mode="bypass")
        calls = []

        def interrupt(command, mode, **kw):
            calls.append(mode)
            if len(calls) == 3:
                raise KeyboardInterrupt()
            return {"ok": True, "probes": [], "error": None}

        with self.assertRaises(KeyboardInterrupt):
            self.apply(actions, mode="bypass", selftest=interrupt)
        self.assertEqual(self.settings.read_bytes(), orig)


class SandboxGuardTests(unittest.TestCase):
    def test_real_home_is_refused(self):
        real = Path(os.path.expanduser("~"))
        with self.assertRaises(AssertionError):
            sb.assert_not_real_home(real)
        with self.assertRaises(AssertionError):
            sb.assert_not_real_home(real / ".claude")
        with self.assertRaises(AssertionError):
            sb.assert_not_real_home(real / ".config" / "typesafe")

    def test_sandbox_env(self):
        root, home, repo, env = sb.make_sandbox()
        try:
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["USERPROFILE"], str(home))
            self.assertEqual(env["PATH"], str(root / "bin"))
            self.assertEqual(env["JEV_ONBOARD_NO_REGISTRY"], "1")
            self.assertTrue((repo / "skill" / "jev-orchestrator" / "hooks" / "permission_gate.py").exists())
            self.assertFalse((repo / "docs").exists())
            self.assertFalse((repo / ".git").exists())
        finally:
            sb.cleanup(root)
        self.assertFalse(root.exists())

    def test_cleanup_removes_links_without_touching_targets(self):
        root, home, repo, env = sb.make_sandbox()
        sb.legacy_owner_home(home, repo)
        skill_file = repo / "skill" / "jev-orchestrator" / "SKILL.md"
        self.assertTrue(skill_file.exists())
        sb.cleanup(root)
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
