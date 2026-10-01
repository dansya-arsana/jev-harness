"""Offline tests for scripts/onboard_presets.py and the sync_agents refactor. Writes only to temp dirs."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import onboard_presets as op  # noqa: E402

jev = op._jev(REPO)
ROLES = sorted(op.shipped_config(REPO)["agents"])
GLM_ENV = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3"}


def build(preset, **kw):
    if preset == "zai-glm":
        kw.setdefault("remap_env", GLM_ENV)
    if preset == "legacy-all-opus":
        kw.setdefault("allow_opus", ["all"])
        kw.setdefault("allow_max", ["jev-advisor"])
    return op.build_config(REPO, preset, **kw)


class Loading(unittest.TestCase):
    def test_every_preset_loads_with_nine_roles(self):
        for name in op.PRESET_ORDER:
            data = op.load_preset(REPO, name)
            self.assertEqual(data["name"], name)
            self.assertTrue(data["summary"])
            if data.get("base") == "shipped":
                continue
            self.assertEqual(sorted(data["agents"]), ROLES, name)

    def test_list_presets_has_custom(self):
        names = [p["name"] for p in op.list_presets(REPO)]
        self.assertEqual(names, list(op.PRESET_ORDER) + ["custom"])
        self.assertIn("balanced", op.format_presets(op.list_presets(REPO)))

    def test_unknown_preset_lists_valid(self):
        with self.assertRaises(op.PresetError) as cm:
            op.load_preset(REPO, "nope")
        self.assertIn("zai-glm", str(cm.exception))

    def test_parsers(self):
        self.assertEqual(op.normalize_role("builder"), "jev-builder")
        self.assertEqual(op.parse_sets(["builder=sonnet:low"], ROLES), {"jev-builder": {"model": "sonnet", "effort": "low"}})
        self.assertEqual(op.parse_model_ids(["work=glm-5.3"]), {"work": "glm-5.3"})
        self.assertEqual(op.expand_roles(["all"], ROLES), ROLES)
        self.assertEqual(op.expand_roles(["qa", "jev-qa"], ROLES), ["jev-qa"])
        with self.assertRaises(op.PresetError):
            op.expand_roles(["zzz"], ROLES)


class Presets(unittest.TestCase):
    def test_balanced_equals_shipped(self):
        cfg, _ = build("balanced")
        ship = op.shipped_config(REPO)
        for k in ("models", "agents", "guardrails", "fallbacks", "required_agents", "context_policy", "escalation"):
            self.assertEqual(cfg[k], ship[k], k)
        self.assertEqual(cfg["_preset"]["name"], "balanced")

    def test_custom_is_balanced(self):
        cfg, _ = build("custom")
        self.assertEqual(cfg["agents"], op.shipped_config(REPO)["agents"])

    def test_economy_has_no_opus(self):
        cfg, _ = build("economy")
        self.assertTrue(all(s["model"] == "sonnet" for s in cfg["agents"].values()))

    def test_max_quality_limits(self):
        cfg, _ = build("max-quality")
        self.assertEqual(sorted(r for r, s in cfg["agents"].items() if s["model"] == "opus"), list(op.OPUS_ROLES))
        self.assertEqual([r for r, s in cfg["agents"].items() if s["effort"] == "max"], ["jev-architect"])

    def test_zai_glm_maps_to_remaps(self):
        cfg, _ = build("zai-glm")
        self.assertEqual(cfg["models"], {"deep": "glm-5.3", "work": "glm-5.3"})
        self.assertEqual(cfg["agents"]["jev-architect"]["model"], "deep")
        self.assertEqual(cfg["agents"]["jev-builder"]["model"], "work")
        self.assertEqual(cfg["agents"]["jev-builder"]["effort"], "low")

    def test_zai_glm_without_remaps_errors(self):
        with self.assertRaises(op.PresetError) as cm:
            op.build_config(REPO, "zai-glm", remap_env={})
        self.assertIn("--model-id deep=", str(cm.exception))
        cfg, _ = op.build_config(REPO, "zai-glm", model_ids=["deep=glm-5.3", "work=glm-4.7"], remap_env={})
        self.assertEqual(cfg["models"]["work"], "glm-4.7")

    def test_all_presets_render_preflight_and_dispatch(self):
        for name in op.PRESET_ORDER:
            cfg, _ = build(name)
            with tempfile.TemporaryDirectory() as td:
                for fn, text in op.render_agents(REPO, cfg).items():
                    Path(td, fn).write_text(text, encoding="utf-8", newline="\n")
                self.assertTrue(jev.preflight(cfg, td, all_agents=True)["ok"], name)
                for role in ROLES:
                    d = jev.check_dispatch(cfg, role, agents_dir=td)
                    self.assertTrue(d["allow"], "%s %s %s" % (name, role, d["errors"]))

    def test_build_is_deterministic(self):
        a, _ = build("max-quality", sets=["qa=sonnet:high"])
        b, _ = build("max-quality", sets=["qa=sonnet:high"])
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertNotIn("20", json.dumps(a["_preset"]))

    def test_legacy_needs_flags_and_matches_pre_vnext(self):
        with self.assertRaises(op.PresetError):
            op.build_config(REPO, "legacy-all-opus")
        cfg, warns = build("legacy-all-opus")
        pre = json.loads((REPO / "config" / "agents.pre-vnext.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["models"], pre["models"])
        for role in ROLES:
            self.assertEqual((cfg["agents"][role]["model"], cfg["agents"][role]["effort"]),
                             (pre["agents"][role]["model"], pre["agents"][role]["effort"]))
        for k in ("opus_allowed_roles", "max_effort_allowed_roles"):
            self.assertEqual(set(cfg["guardrails"][k]), set(pre["guardrails"][k]))
        self.assertTrue(warns)


class Guardrails(unittest.TestCase):
    def rejects(self, text, **kw):
        with self.assertRaises(op.PresetError) as cm:
            op.build_config(REPO, "balanced", **kw)
        self.assertIn(text, str(cm.exception))

    def test_builder_on_opus(self):
        self.rejects("--allow-opus builder", sets=["builder=opus"])
        cfg, warns = op.build_config(REPO, "balanced", sets=["builder=opus"], allow_opus=["builder"])
        self.assertIn("jev-builder", cfg["guardrails"]["opus_allowed_roles"])
        self.assertIn("jev-architect", cfg["guardrails"]["opus_allowed_roles"])
        self.assertTrue(any("Opus" in w for w in warns))

    def test_reviewer_at_max(self):
        self.rejects("--allow-max reviewer", sets=["reviewer=sonnet:max"])
        cfg, _ = op.build_config(REPO, "balanced", sets=["reviewer=sonnet:max"], allow_max=["reviewer"])
        self.assertEqual(cfg["guardrails"]["max_effort_allowed_roles"], ["jev-architect", "jev-reviewer"])

    def test_dispatch_collision(self):
        self.rejects("check_dispatch", model_ids=["sonnet=claude-opus-5-5"])

    def test_alias_ids(self):
        for bad in ("sonnet", "fable", "opus[1m]"):
            self.rejects("alias", model_ids=["work=" + bad])

    def test_bad_ids(self):
        self.rejects("valid model ID", model_ids=["work=glm 5"])
        self.rejects("valid model ID", model_ids=["work=a: b"])

    def test_bad_effort_and_key(self):
        self.rejects("bad effort", sets=["builder=sonnet:ultra"])
        self.rejects("does not exist", sets=["builder=nokey"])
        self.rejects("unknown role", sets=["zzz=sonnet"])

    def test_validate_config_flags_write_and_fallback_changes(self):
        cfg = op.shipped_config(REPO)
        cfg["agents"]["jev-scout"]["write"] = True
        cfg["fallbacks"]["jev-scout"] = "jev-builder"
        errs, _ = op.validate_config(cfg, repo=REPO)
        self.assertTrue(any("write flag" in e for e in errs))
        self.assertTrue(any("fallback" in e for e in errs))


class Rendering(unittest.TestCase):
    def test_routing_table_ascii(self):
        cfg, _ = build("zai-glm")
        t = op.routing_table(cfg)
        t.encode("ascii")
        self.assertIn("glm-5.3", t)
        self.assertIn("Model key", t)

    def test_render_has_full_ids_and_no_alias(self):
        cfg, _ = build("economy")
        texts = op.render_agents(REPO, cfg)
        self.assertEqual(len(texts), 9)
        self.assertIn("model: claude-sonnet-5-5", texts["jev-architect.md"])
        self.assertNotIn("\r", texts["jev-architect.md"])


class SyncAgentsCli(unittest.TestCase):
    def test_check_exits_zero(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "sync_agents.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "0 file(s) need changes")

    def test_out_dir_and_config(self):
        import sync_agents
        cfg, _ = build("economy")
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td, "c.json")
            cfgp.write_text(json.dumps(cfg), encoding="utf-8")
            out = Path(td, "out")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sync_agents.main(["--config", str(cfgp), "--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual(len(list(out.glob("jev-*.md"))), 9)
        self.assertEqual(sync_agents.load_cfg()["agents"]["jev-architect"]["model"], "opus")
        with self.assertRaises(ValueError):
            sync_agents.render("no frontmatter", "jev-builder", op.shipped_config(REPO))


if __name__ == "__main__":
    unittest.main()
