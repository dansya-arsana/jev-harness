#!/usr/bin/env python3
"""jev-harness onboarding installer (Python 3.9+, standard library only).

    python scripts/onboard.py            interactive install or update (Claude Code)
    python scripts/onboard.py --check    read-only health check
    python scripts/onboard.py --dry-run  show the plan, change nothing
    python scripts/onboard.py --uninstall

Exit codes: 0 ok, 1 check or verification failed, 2 refused or bad usage (nothing written), 3 apply error.
Against the real home only --check, --dry-run, --list-presets and --show-routing are safe to try first.
Secrets are never printed; the TypeSafe key is entered hidden and never put on a command line.
"""
import argparse
import datetime
import getpass
import inspect
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import onboard_apply as oa  # noqa: E402
import onboard_env as oe  # noqa: E402
import onboard_presets as op  # noqa: E402
import onboard_verify as ov  # noqa: E402

REPO = HERE.parent
HOST_CHOICES = ("auto", "claude", "codex", "gemini", "cursor", "windsurf", "zcode")

BYPASS_WARNING = """\
BYPASS MODE: read this before you continue.
Claude Code will run every tool call without asking you.
Still blocked by the Jev permission gate (hard deny): piping downloaded code into a shell or interpreter, reading
secrets and then sending data over the network, recursive delete or chown of your home, / or system folders, disk
formatting and raw disk writes, fork bombs, disabling macOS protections, and anything Jev denies (needs a key and
network).
No longer asked (these run silently and are logged): force push, reset --hard, clean -f, sudo, writes to system
paths, cron/launchd, terraform/pulumi/kubectl/helm, recursive delete outside the project, edits to .env and key
files, and edits to ~/.claude/settings.json (an agent could remove the gate for future sessions; --check reports
it).
Only Bash and file edits are inspected by the gate: not PowerShell, MCP or web tools.
Managed settings can disable bypass mode.
This changes permissions.defaultMode in ~/.claude/settings.json (a backup is made first).
Undo it any time with: python scripts/onboard.py --mode safe"""


class _Exit(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


def build_parser():
    p = argparse.ArgumentParser(
        prog="onboard.py", description="jev-harness onboarding: check, install, update and uninstall (Claude Code).",
        epilog="Exit codes: 0 ok, 1 check/verification failed, 2 refused or bad usage, 3 apply error.")
    p.add_argument("--check", action="store_true", help="read-only health check (environment, preflight, staleness, "
                   "gate self-test)")
    p.add_argument("--uninstall", action="store_true", help="undo what the manifest recorded")
    p.add_argument("--list-presets", action="store_true", help="list the model presets and exit")
    p.add_argument("--show-routing", action="store_true", help="show the routing table (active config, or what "
                   "--preset/--set would produce)")
    p.add_argument("--host", choices=HOST_CHOICES, default="auto", help="host to install for (only claude is installable)")
    p.add_argument("--preset", metavar="NAME", help="model preset (see --list-presets)")
    p.add_argument("--set", dest="set", action="append", default=[], metavar="ROLE=KEY[:EFFORT]",
                   help="override one role's model key and/or effort (repeatable)")
    p.add_argument("--model-id", dest="model_id", action="append", default=[], metavar="KEY=ID",
                   help="define or change a model ID (repeatable)")
    p.add_argument("--allow-opus", dest="allow_opus", action="append", default=[], metavar="ROLE|all",
                   help="allow Opus for a role beyond architect and debugger (repeatable)")
    p.add_argument("--allow-max", dest="allow_max", action="append", default=[], metavar="ROLE|all",
                   help="allow max effort for a role beyond the architect (repeatable)")
    p.add_argument("--mode", choices=("safe", "bypass"), help="permission mode (default safe)")
    p.add_argument("--confirm-bypass", action="store_true", help="non-interactive confirmation for --mode bypass")
    p.add_argument("--skip-bypass-prompt", action="store_true",
                   help="also set skipDangerousModePermissionPrompt (bypass mode only)")
    p.add_argument("--components", metavar="LIST",
                   help="comma list of: " + ", ".join(oa.COMPONENTS))
    p.add_argument("--python", metavar="PATH", help="interpreter the hooks should use")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask; accept the plan")
    p.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--online", action="store_true", help="--check: make one Jev call to test connectivity")
    p.add_argument("--offline", action="store_true", help="skip every network call (sample route)")
    p.add_argument("--no-tests", action="store_true", help="skip the offline test suites after installing")
    p.add_argument("--e2e", action="store_true", help="run the live end-to-end gate check (one real model call; "
                   "real home only)")
    p.add_argument("--force", action="store_true", help="--uninstall: remove the skill link even if Claude Code runs")
    p.add_argument("--home", metavar="DIR", help="home directory to operate on (default: JEV_ONBOARD_HOME or ~)")
    return p


def _confirm_kw(fn, confirmed):
    """confirmed_bypass is passed only when the apply engine knows the parameter (it is additive there)."""
    try:
        if "confirmed_bypass" in inspect.signature(fn).parameters:
            return {"confirmed_bypass": bool(confirmed)}
    except (TypeError, ValueError):
        pass
    return {}


def _ascii(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def _real_path(p):
    return os.path.normcase(os.path.realpath(str(p)))


class _App(object):
    def __init__(self, args, input_fn, getpass_fn, out, interactive, which, run, now):
        self.args = args
        self.input_fn = input_fn
        self.getpass_fn = getpass_fn
        self.out = out
        self.which = which
        self.run = run or oe._default_run
        self._now = now
        self.env = dict(os.environ)
        h = args.home or self.env.get("JEV_ONBOARD_HOME")
        if h and str(h).replace(chr(92), "/").startswith("//"):
            sys.stderr.write("onboard.py: refusing a UNC or device path for --home / JEV_ONBOARD_HOME\n")
            raise SystemExit(2)
        self.home = Path(os.path.abspath(os.path.expanduser(h))) if h else Path.home()
        self.repo = REPO
        if interactive is None:
            try:
                interactive = bool(sys.stdin.isatty())
            except (AttributeError, ValueError):
                interactive = False
        self.interactive = bool(interactive) and not args.yes

    # ---------- small helpers ----------

    def say(self, text=""):
        self.out.write(_ascii(text) + "\n")

    def now(self):
        n = self._now
        if callable(n):
            n = n()
        return n or datetime.datetime.now(datetime.timezone.utc)

    def refuse(self, msg, code=2):
        self.say("ERROR: " + msg)
        raise _Exit(code)

    def ask(self, prompt):
        return str(self.input_fn(prompt)).strip()

    def yn(self, prompt, default):
        suffix = " [Y/n] " if default else " [y/N] "
        for _ in range(3):
            a = self.ask(prompt + suffix).lower()
            if a == "":
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            self.say("Please answer y or n.")
        return False

    def is_real_home(self):
        return _real_path(self.home) == _real_path(Path.home())

    def doctor(self):
        return oe.doctor(self.home, self.repo, self.env, online=bool(self.args.online and not self.args.offline),
                         which=self.which, run=self.run)

    # ---------- flows without writes ----------

    def list_presets(self):
        presets = op.list_presets(self.repo)
        if self.args.json:
            self.say(json.dumps(presets, indent=2))
        else:
            self.say(op.format_presets(presets))
            self.say("")
            self.say("Use --preset NAME. Change single roles with --set ROLE=KEY[:EFFORT], --model-id KEY=ID, "
                     "--allow-opus ROLE|all and --allow-max ROLE|all.")
        return 0

    def _has_overrides(self):
        a = self.args
        return bool(a.preset or a.set or a.model_id or a.allow_opus or a.allow_max)

    def show_routing(self):
        a = self.args
        if self._has_overrides():
            settings = oe.claude_settings(self.home, self.which)
            remaps = oe.remap_env(settings, self.env)["remaps"]
            try:
                roles = sorted(op.shipped_config(self.repo)["agents"])
                cfg, warns = op.build_config(
                    self.repo, a.preset or "balanced", sets=op.parse_sets(a.set, roles),
                    model_ids=op.parse_model_ids(a.model_id), allow_opus=a.allow_opus, allow_max=a.allow_max,
                    remap_env=remaps)
            except op.PresetError as e:
                for err in e.errors:
                    self.say("ERROR: " + err)
                return 2
            source = "preview of preset %s (nothing is written)" % (a.preset or "balanced")
        else:
            warns = []
            active = self.home / oa.CONFIG_REL
            path = active if active.is_file() else self.repo / "config" / "agents.json"
            try:
                cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as e:
                self.say("ERROR: cannot read %s: %s" % (path, e))
                return 2
            source = "active config %s" % oa.norm(path)
        if a.json:
            self.say(json.dumps({"source": source, "models": cfg.get("models"),
                                 "agents": {r: {"model": s.get("model"), "effort": s.get("effort"),
                                                "write": bool(s.get("write"))} for r, s in cfg["agents"].items()},
                                 "warnings": warns}, indent=2))
            return 0
        self.say("Routing: " + source)
        self.say(op.routing_table(cfg))
        for w in warns:
            self.say("WARNING: " + w)
        return 0

    # ---------- --check ----------

    def check(self):
        a = self.args
        report = self.doctor()
        settings = report["settings"]
        manifest = report["install"]["manifest"]
        # A manifest-recorded interpreter is never executed (the manifest is user-writable data).
        python = sys.executable
        pf = ov.preflight(self.home, self.repo, python, self.env)
        remaps = oe.remap_env(settings, self.env)["remaps"]
        stale = ov.staleness(self.home, self.repo, remaps)
        mode = report["mode"]["mode"]
        st = ov.gate_selftest(settings, "bypass" if mode in ("bypass", "unprotected-bypass") else "safe")
        e2e = None
        if a.e2e:
            e2e = ov.e2e_run(self.home, "bypassPermissions" if mode.endswith("bypass") else "default",
                             which=self.which)
        gate_ok = ov.registered_gate(settings) is not None
        ok = (not report["critical"] and gate_ok and pf["ok"] and stale["ok"] and st["status"] != "fail"
              and not (e2e and e2e["status"] == "FAIL"))
        suggested = oe.suggest_preset(settings, self.env)
        if a.json:
            self.say(json.dumps({"ok": ok, "doctor": report, "suggested_preset": suggested, "preflight": pf,
                                 "gate_registered": gate_ok, "staleness": stale, "selftest": st, "e2e": e2e}, indent=2, default=str))
            return 0 if ok else 1
        self.say(oe.format_doctor(report))
        self.say("")
        self.say("Verification")
        self.say("  [%s] preflight: %s" % ("OK" if pf["ok"] else "FAIL",
                                          "all configured agents pass" if pf["ok"] else pf["detail"]))
        self.say("  [%s] gate hook: %s" % ("OK" if gate_ok else "FAIL", "registered and its script exists" if gate_ok
                                           else "no working permission gate is registered (re-run onboard.py to add it)"))
        self.say("  [%s] staleness: %s" % ("OK" if stale["ok"] else "FAIL", "; ".join(stale["details"])))
        for r in st["results"]:
            for pr in r.get("probes", []):
                self.say("  [%s] gate self-test (%s): %s -> %s" % ("OK" if pr["ok"] else "FAIL", r["claude_mode"],
                                                                  pr["name"], pr["got"]))
            if r.get("error"):
                self.say("  [FAIL] gate self-test (%s): %s" % (r["claude_mode"], r["error"]))
        if not st["results"]:
            self.say("  [SKIP] gate self-test: %s" % st["detail"])
        if e2e:
            self.say("  [%s] e2e gate check: %s" % (e2e["status"], e2e["detail"]))
        self.say("")
        self.say("Check %s." % ("passed" if ok else "FAILED"))
        return 0 if ok else 1

    # ---------- install ----------

    def _roles(self):
        return sorted(op.shipped_config(self.repo)["agents"])

    def _default_components(self, report, manifest, key_found):
        if manifest and (manifest.get("choice") or {}).get("components"):
            return list(manifest["choice"]["components"])
        if report["install"]["legacy"]:
            comps = ["skill", "agents", "gate"]
            scripts = {oe.hook_script(h["command"]) for h in report["settings"]["hooks"] if h["ours"]}
            if "dispatch_router.py" in scripts:
                comps.append("dispatch")
            if "prompt_router.py" in scripts:
                comps.append("router")
            return comps
        comps = ["skill", "agents", "gate", "dispatch"]
        if not key_found:
            comps.append("keyfile")
        return comps

    def _parse_components(self, text):
        comps = [c.strip() for c in text.split(",") if c.strip()]
        bad = [c for c in comps if c not in oa.COMPONENTS]
        if bad:
            self.refuse("unknown component(s): %s (valid: %s)" % (", ".join(bad), ", ".join(oa.COMPONENTS)))
        missing = [c for c in oa.REQUIRED if c not in comps]
        if missing:
            self.refuse("required component(s) missing: %s (the gate, the agents and the skill link belong together)"
                        % ", ".join(missing))
        return [c for c in oa.COMPONENTS if c in comps]

    def _ask_components(self, defaults, key_found, keyfile_exists):
        comps = ["skill", "agents", "gate"]
        if self.yn("Register the dispatch guard (checks every subagent launch)?", "dispatch" in defaults):
            comps.append("dispatch")
        if self.yn("Register the prompt router (one Jev call and extra tokens per prompt)?", "router" in defaults):
            comps.append("router")
        rp = self.repo / "config" / "global-rules.md"
        if rp.is_file():
            self.say("Optional rules block for ~/.claude/CLAUDE.md starts with:")
            for ln in rp_head(self.repo):
                self.say("  " + ln)
            if self.yn("Add the rules block to ~/.claude/CLAUDE.md?", "rules" in defaults):
                comps.append("rules")
        if not key_found and not keyfile_exists:
            if self.yn("Create ~/.config/typesafe/.env for your TypeSafe key?", True):
                comps.append("keyfile")
        elif "keyfile" in defaults:
            comps.append("keyfile")
        return [c for c in oa.COMPONENTS if c in comps]

    def _ask_preset(self, default):
        presets = op.list_presets(self.repo)
        names = [p["name"] for p in presets]
        self.say("Model presets:")
        for i, p in enumerate(presets, 1):
            self.say("  %d. %-16s %s%s" % (i, p["name"], p["summary"], "   <- suggested" if p["name"] == default else ""))
        for _ in range(3):
            ans = self.ask("Preset (number or name, Enter = %s): " % default)
            if not ans:
                return default
            if ans.isdigit() and 1 <= int(ans) <= len(names):
                return names[int(ans) - 1]
            if ans in names:
                return ans
            self.say("Not a preset: %r" % ans)
        raise _Exit(2)

    def _ask_custom(self, remaps, sets, model_ids):
        """Per-role model key and effort. Returns (sets, model_ids)."""
        try:
            base, _w = op.build_config(self.repo, "balanced", sets=sets, model_ids=model_ids,
                                       allow_opus=["all"], allow_max=["all"], remap_env=remaps)
        except op.PresetError as e:
            self.refuse("; ".join(e.errors))
        sets = {r: dict(s) for r, s in sets.items()}
        model_ids = dict(model_ids)
        for role in sorted(base["agents"]):
            spec = base["agents"][role]
            keys = sorted(base["models"])
            self.say("%s: now %s (%s) at %s effort" % (role, spec["model"], base["models"][spec["model"]], spec["effort"]))
            k = self.ask("  model key (%s, 'add' for a new key; Enter keeps %s): " % (", ".join(keys), spec["model"]))
            new = {}
            if k == "add":
                nk = self.ask("  new key name: ")
                nid = self.ask("  full model ID for %s: " % nk)
                if nk and nid:
                    model_ids[nk] = nid
                    base["models"][nk] = nid
                    new["model"] = nk
            elif k and k in base["models"]:
                new["model"] = k
            elif k:
                self.say("  unknown key %r; kept %s" % (k, spec["model"]))
            e = self.ask("  effort (%s; Enter keeps %s): " % ("/".join(op.EFFORTS), spec["effort"]))
            if e in op.EFFORTS:
                new["effort"] = e
            elif e:
                self.say("  unknown effort %r; kept %s" % (e, spec["effort"]))
            if new:
                sets.setdefault(role, {}).update(new)
        return sets, model_ids

    def _build(self, preset, sets, model_ids, allow_opus, allow_max, remaps):
        """(cfg, warnings, allow_opus, allow_max). Interactive runs may accept Opus/max overrides with a typed yes."""
        for attempt in range(2):
            try:
                cfg, warns = op.build_config(self.repo, preset, sets=sets, model_ids=model_ids, allow_opus=allow_opus,
                                             allow_max=allow_max, remap_env=remaps)
                return cfg, warns, allow_opus, allow_max
            except op.PresetError as e:
                pat = re.compile(r"needs --allow-(opus|max) (\S+)")
                found = [pat.search(x) for x in e.errors]
                if self.interactive and attempt == 0 and found and all(found):
                    for x in e.errors:
                        self.say("WARNING: " + x)
                    if self.ask("These overrides cost more (Opus or max effort outside the defaults). "
                                "Type yes to allow them: ").lower() == "yes":
                        allow_opus = list(allow_opus) + [m.group(2) for m in found if m.group(1) == "opus"]
                        allow_max = list(allow_max) + [m.group(2) for m in found if m.group(1) == "max"]
                        continue
                for x in e.errors:
                    self.say("ERROR: " + x)
                raise _Exit(2)
        raise _Exit(2)

    def _bypass_keys(self, settings, skip):
        def fmt(present, v):
            return json.dumps(v) if present else "(absent)"
        dm = settings["default_mode"]
        lines = ["permissions.defaultMode: %s -> \"bypassPermissions\"" % fmt(dm is not None, dm)]
        if skip:
            sk = settings["skip_prompt"]
            lines.append("skipDangerousModePermissionPrompt: %s -> true" % fmt(sk is not None, sk))
        return lines

    def install(self):
        a = self.args
        env, home, repo = self.env, self.home, self.repo
        report = self.doctor()
        settings = report["settings"]
        manifest = report["install"]["manifest"]
        mch = (manifest or {}).get("choice") or {}
        key = report["key"]

        self.say("jev-harness onboarding (repo %s, home %s)" % (oa.norm(repo), oa.norm(home)))
        detected = [h["label"] + (" " + h["version"] if h["version"] else "") for h in report["hosts"] if h["detected"]]
        self.say("Hosts found: %s" % (", ".join(detected) or "none"))
        self.say("Mode now: %s" % report["mode"]["mode"])
        self.say("TypeSafe key: %s" % ("present (source: %s)" % key["source"] if key["present"] else "not found"))
        for c in report["critical"]:
            self.say("CRITICAL: " + c)

        # (2) host
        rows = {r["host"]: r for r in report["hosts"]}
        host = a.host
        if host == "auto":
            if rows["claude"]["detected"]:
                host = "claude"
            else:
                self.say("Claude Code was not found (no claude binary and no ~/.claude). Hosts:")
                for h in report["hosts"]:
                    if h["host"] != "cline-roo":
                        self.say("  %-12s %s; install: %s" % (h["label"], "detected" if h["detected"] else "not found",
                                                             "full harness" if h["installable"] else "nothing yet"))
                self.say("Install Claude Code first, or pass --host claude to prepare anyway.")
                raise _Exit(2)
        if host != "claude":
            row = rows[host]
            self.say("%s: %s" % (row["label"], "detected" if row["detected"] else "not found"))
            for k, v in row["support"].items():
                self.say("  %-10s %s" % (k, v))
            self.say("  why nothing is installed: %s" % row["why_not"])
            self.say("Nothing was installed for %s." % row["label"])
            return 0

        # (3) defaults, overrides
        mode_only = bool(a.mode and manifest and not (a.preset or a.set or a.model_id or a.allow_opus or a.allow_max
                                                      or a.components or a.python))
        roles = self._roles()
        try:
            flag_sets = op.parse_sets(a.set, roles)
            flag_ids = op.parse_model_ids(a.model_id)
            op.expand_roles(a.allow_opus, roles)
            op.expand_roles(a.allow_max, roles)
        except op.PresetError as e:
            for x in e.errors:
                self.say("ERROR: " + x)
            raise _Exit(2)
        reset = bool(a.preset)
        sets = {} if reset else {r: dict(s) for r, s in (mch.get("sets") or {}).items()}
        for r, s in flag_sets.items():
            sets.setdefault(r, {}).update(s)
        model_ids = {} if reset else dict(mch.get("model_ids") or {})
        model_ids.update(flag_ids)
        allow_opus = ([] if reset else list(mch.get("allow_opus") or [])) + list(a.allow_opus)
        allow_max = ([] if reset else list(mch.get("allow_max") or [])) + list(a.allow_max)

        key_found = key["present"]
        keyfile_exists = oa.lexists(home / oa.KEYFILE_REL)
        defaults = self._default_components(report, manifest, key_found)
        if a.components:
            comps = self._parse_components(a.components)
        elif self.interactive and not mode_only:
            comps = self._ask_components(defaults, key_found, keyfile_exists)
        else:
            comps = [c for c in oa.COMPONENTS if c in defaults]

        suggestion = oe.suggest_preset(settings, env)
        preset = a.preset or mch.get("preset")
        if not preset:
            preset = suggestion
        if self.interactive and not a.preset and not mode_only:
            preset = self._ask_preset(preset)
        remaps = oe.remap_env(settings, env)["remaps"]
        if self.interactive and preset == "custom" and not mode_only:
            sets, model_ids = self._ask_custom(remaps, sets, model_ids)

        mode = a.mode or mch.get("mode") or "safe"
        if self.interactive and not a.mode and not mode_only:
            self.say("Permission mode: 1 = safe (Claude asks, the gate protects), 2 = bypass (no prompts, hard-deny "
                     "list still enforced)")
            ans = self.ask("Mode (1/2, Enter = %s): " % ("2" if mode == "bypass" else "1"))
            if ans in ("1", "2"):
                mode = "bypass" if ans == "2" else "safe"
        skip = bool(a.skip_bypass_prompt)
        if skip and mode != "bypass":
            self.refuse("--skip-bypass-prompt is only valid together with bypass mode (--mode bypass)")
        if mode == "bypass" and not a.skip_bypass_prompt and mch.get("skip_bypass_prompt") and mch.get("mode") == "bypass":
            skip = True
        if mode == "bypass" and "gate" not in comps:
            self.refuse("bypass mode needs the permission gate component")

        try:
            python, pwarn = oa.pick_python(a.python, mch.get("python"))
        except oa.ApplyError as e:
            self.refuse(str(e))

        # keyfile (hidden paste; Enter = fill in later)
        new_key = None
        if self.interactive and "keyfile" in comps and not key_found and not keyfile_exists:
            new_key = self.getpass_fn("Paste your TypeSafe key (hidden; Enter = fill in later): ").strip() or None
        rules_block = None
        if "rules" in comps:
            rp = repo / "config" / "global-rules.md"
            try:
                rules_block = rp.read_text(encoding="utf-8")
            except OSError as e:
                self.refuse("cannot read %s: %s" % (rp, e))

        # (5) build and render
        cfg, bwarn, allow_opus, allow_max = self._build(preset, sets, model_ids, allow_opus, allow_max, remaps)
        try:
            agents = op.render_agents(repo, cfg)
        except op.PresetError as e:
            for x in e.errors:
                self.say("ERROR: " + x)
            raise _Exit(2)
        allow_opus = op.expand_roles(allow_opus, roles)
        allow_max = op.expand_roles(allow_max, roles)

        # (6) bypass gating
        need_confirm = mode == "bypass" and (settings["default_mode"] != "bypassPermissions" or
                                             (skip and not settings["skip_prompt"]))
        if need_confirm:
            if not self.interactive:
                if not a.confirm_bypass:
                    self.say("Bypass mode is refused without --confirm-bypass (it removes the permission prompts).")
                    self.say("Run with --mode bypass --confirm-bypass --yes after reading what it does: "
                             "python scripts/onboard.py --help")
                    raise _Exit(2)
            else:
                self.say(BYPASS_WARNING)
                if self.ask("Type the word bypass to continue: ") != "bypass":
                    self.say("That was not the word 'bypass'. Nothing was written.")
                    raise _Exit(2)
                self.say("These keys will change in settings.json:")
                for ln in self._bypass_keys(settings, skip):
                    self.say("  " + ln)
                if not self.yn("Write these keys?", False):
                    self.say("Nothing was written.")
                    raise _Exit(2)
                if skip and not self.yn("Also hide Claude Code's own bypass warning (skipDangerousModePermissionPrompt)?",
                                        False):
                    skip = False

        # every path that reaches this point has passed the confirmations above (or bypass was already on)
        confirmed = mode == "bypass"
        choice = {"preset": preset, "sets": sets, "model_ids": model_ids, "allow_opus": allow_opus,
                  "allow_max": allow_max, "components": comps, "mode": mode, "skip_bypass_prompt": skip,
                  "python": python}
        plan_kw = dict(components=comps, agents=agents, active_config=cfg, mode=mode, skip_bypass_prompt=skip,
                       python=python, rules_block=rules_block, choice=choice, platform=sys.platform,
                       key_found=key_found, key=new_key)

        # (7) plan
        try:
            actions, pwarn2 = oa.plan_install(home, repo, **dict(plan_kw, **_confirm_kw(oa.plan_install, confirmed)))
        except oa.ApplyError as e:
            self.refuse(str(e), 3)
        warnings = list(pwarn) + list(pwarn2) + [w for w in bwarn]
        warnings += oe.remap_warnings(settings, env, cfg.get("models"))
        if key.get("warning"):
            warnings.append(key["warning"])
        if not actions:
            for w in warnings:
                self.say("NOTE: " + w)
            self.say("Nothing to change.")
            return 0
        self.say("")
        self.say("Plan (preset %s, mode %s, components %s):" % (preset, mode, ", ".join(comps)))
        self.say(oa.format_plan(actions, warnings, dry_run=a.dry_run))
        if a.dry_run:
            return 0

        # (8) confirm
        if self.interactive:
            if not self.yn("Proceed?", False):
                self.say("Nothing was changed.")
                return 2
        elif not a.yes:
            self.say("Refusing to change anything without confirmation: pass --yes (or run in a terminal).")
            return 2

        # (9) apply
        try:
            res = oa.apply(actions, home, repo, mode=mode, choice=choice, now=self.now(), platform=sys.platform,
                           **_confirm_kw(oa.apply, confirmed))
        except oa.ApplyError as e:
            self.say("ERROR: " + str(e))
            self.say("Earlier steps stay recorded in the manifest; `python scripts/onboard.py --uninstall` cleans them up.")
            return 3
        except OSError as e:
            self.say("ERROR: %s: %s" % (type(e).__name__, e))
            return 3
        self.say("Applied: %s" % ", ".join(res["applied"]))
        if res["backup_dir"]:
            self.say("Backup: %s" % res["backup_dir"])

        # (10) verify
        want_e2e = bool(a.e2e)
        if (not want_e2e and self.interactive and mode == "bypass" and self.is_real_home() and self.which("claude")
                and self.yn("Run the live end-to-end gate check now (makes one real Claude model call)?", True)):
            want_e2e = True
        v = ov.verify(home, repo, python, mode=mode, settings=oe.claude_settings(home, self.which), key=key,
                      offline=a.offline, run_tests=not a.no_tests, e2e=want_e2e, env=env, which=self.which)
        self.say("")
        self.say("Verification")
        for s in v["steps"]:
            self.say("  [%s] %s: %s" % (s["status"].upper(), s["name"], s["detail"]))
        if v.get("e2e") == "FAIL" and mode == "bypass":
            self.say("CRITICAL: the gate did not stop the canary in bypass mode; switching back to safe mode now.")
            kw = dict(plan_kw, mode="safe", skip_bypass_prompt=False, choice=dict(choice, mode="safe",
                                                                                skip_bypass_prompt=False))
            try:
                back, _w = oa.plan_install(home, repo, **dict(kw, **_confirm_kw(oa.plan_install, False)))
                oa.apply(back, home, repo, mode="safe", choice=kw["choice"], now=self.now(), platform=sys.platform,
                         **_confirm_kw(oa.apply, False))
                self.say("Mode restored to safe.")
            except (oa.ApplyError, OSError) as e:
                self.say("CRITICAL: could not switch back automatically (%s); run: python scripts/onboard.py --mode safe" % e)
            return 1

        # (11) next steps
        self.say("")
        self.say("Restart Claude Code (close every session) so it loads agents and hooks. Hooks are registered, not "
                 "yet active in running sessions.")
        self.say("Check any time:   python scripts/onboard.py --check")
        self.say("Switch mode:      python scripts/onboard.py --mode safe   (or --mode bypass --confirm-bypass)")
        self.say("Undo everything:  python scripts/onboard.py --uninstall")
        return 0 if v["ok"] else 1

    # ---------- uninstall ----------

    def uninstall(self):
        a = self.args
        try:
            actions, warnings = oa.plan_uninstall(self.home, self.repo, force=a.force)
        except (oa.ApplyError, OSError) as e:
            self.refuse(str(e), 3)
        if not actions:
            self.say("Nothing to uninstall.")
            for w in warnings:
                self.say("NOTE: " + w)
            return 0
        self.say("Uninstall plan:")
        for n, act in enumerate(actions, 1):
            self.say("%d. %s" % (n, act.summary))
            for d in act.details:
                self.say("     - %s" % d)
        for w in warnings:
            self.say("NOTE: " + w)
        self.say("A backup copy of every file it changes goes to ~/.claude/backups/jev-onboard-<timestamp>/.")
        if a.dry_run:
            self.say("DRY RUN: nothing was changed.")
            return 0
        self.say("Nothing has changed yet.")
        if self.interactive:
            if not self.yn("Proceed?", False):
                self.say("Nothing was changed.")
                return 2
        elif not a.yes:
            self.say("Refusing to change anything without confirmation: pass --yes (or run in a terminal).")
            return 2
        try:
            res = oa.apply_uninstall(actions, self.home, self.repo, now=self.now())
        except (oa.ApplyError, OSError) as e:
            self.say("ERROR: %s" % e)
            return 3
        self.say("Done: %s" % ", ".join(res["applied"]))
        if res["backup_dir"]:
            self.say("Backup: %s" % res["backup_dir"])
        for w in res["warnings"]:
            self.say("NOTE: " + w)
        self.say("Restart Claude Code so it stops loading the removed hooks and agents.")
        return 0

    # ---------- dispatch ----------

    def run_all(self):
        a = self.args
        if a.online and a.offline:
            self.refuse("--online and --offline cannot be used together")
        if a.e2e and not self.is_real_home():
            self.refuse("--e2e makes a live model call against the real Claude Code and needs the real home; "
                        "--home %s is not it" % oa.norm(self.home))
        if a.list_presets:
            return self.list_presets()
        if a.show_routing:
            return self.show_routing()
        if a.check:
            return self.check()
        if a.uninstall:
            return self.uninstall()
        return self.install()


def rp_head(repo):
    try:
        text = (Path(repo) / "config" / "global-rules.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")[:2]]


def main(argv=None, *, input_fn=input, getpass_fn=getpass.getpass, out=None, interactive=None, which=shutil.which,
         run=None, now=None):
    out = out or sys.stdout
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    buf = io.StringIO() if (args.json and not (args.check or args.list_presets or args.show_routing)) else None
    app = _App(args, input_fn, getpass_fn, buf if buf is not None else out, interactive, which, run, now)
    try:
        code = app.run_all()
    except _Exit as e:
        code = e.code
    except (EOFError, KeyboardInterrupt):
        app.say("Aborted. If nothing was reported as applied, nothing was changed.")
        code = 2
    if buf is not None:
        out.write(json.dumps({"exit": code, "log": buf.getvalue().splitlines()}, indent=2) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
