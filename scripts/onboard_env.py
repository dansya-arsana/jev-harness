#!/usr/bin/env python3
"""Environment and host checks for the jev-harness onboarding installer (read-only; Python 3.9+, stdlib only).

Every function takes `home` (a Path) and `env` (a Mapping) explicitly, and `which` / `run` are injectable, so tests
run against fake homes. Nothing here writes anything. Secret values are never returned: only allow-listed settings
values (model ids, base-URL host, JEV_* keys) and the *source* of a TypeSafe key.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

OUR_SCRIPTS = ("permission_gate.py", "dispatch_router.py", "prompt_router.py")
HOOK_MARKER = "jev-orchestrator/hooks/"
REMAP_KEYS = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL")
DEFAULT_ROLES = ("jev-architect", "jev-advisor", "jev-analyst", "jev-builder", "jev-engineer", "jev-debugger",
                 "jev-reviewer", "jev-qa", "jev-scout")
CRITICAL_MSG = ("every Bash/Edit/Write call will be blocked; re-run onboard.py from the repo or --uninstall")

HOSTS = ("claude", "codex", "gemini", "cursor", "windsurf", "zcode", "cline-roo")

# label, gate, prompt, subagents, skills, rules, config: copied verbatim from docs/jev/host-research.md matrix
_FIELDS = ("gate", "prompt", "subagents", "skills", "rules", "config")


def _row(label, *vals):
    d = {"label": label}
    d.update(dict(zip(_FIELDS, vals)))
    return d


SUPPORT = {
    "claude": _row("Claude Code", "full", "full", "full", "full", "full", "full"),
    "codex": _row("Codex", "partial (trust review)", "partial", "partial (TOML)", "partial", "full", "full"),
    "gemini": _row("Gemini CLI", "partial (other schema)", "partial", "partial", "full", "full", "partial"),
    "cursor": _row("Cursor", "partial (other schema)", "partial", "unverified", "unverified", "unverified", "unverified"),
    "windsurf": _row("Windsurf", "partial (other schema)", "partial", "unverified", "unverified", "unverified",
                     "unverified"),
    "zcode": _row("ZCode", "partial (user-level)", "partial", "partial (own format)", "full", "full", "unverified"),
    "cline-roo": _row("Cline / Roo", "unverified", "unverified", "unverified", "unverified", "unverified",
                      "unverified"),
}
SUPPORT["claude"]["installs"] = ["skill", "agents", "gate", "dispatch", "router", "rules", "keyfile"]
for _h in HOSTS[1:]:
    SUPPORT[_h]["installs"] = []
SUPPORT["claude"]["why_not"] = ""
SUPPORT["codex"]["why_not"] = ("hooks must be trusted in /hooks before they run; hook input and TOML agent schemas "
                               "not verified")
for _h in ("gemini", "cursor", "windsurf"):
    SUPPORT[_h]["why_not"] = "hook input schema differs from Claude's; not verified"
SUPPORT["zcode"]["why_not"] = "own agent format; hooks need a new session; hook input schema not verified"
SUPPORT["cline-roo"]["why_not"] = "nothing verified"

# host -> (binaries, config dir specs, version args or None). Dir spec: ("home", rel) | ("env", VAR) | ("appdata", rel)
PROBES = {
    "claude": (["claude"], [("home", ".claude")], ["--version"]),
    "codex": (["codex"], [("env", "CODEX_HOME"), ("home", ".codex")], None),
    "gemini": (["gemini"], [("home", ".gemini")], ["--version"]),
    "cursor": (["cursor"], [("home", ".cursor")], None),
    "windsurf": (["windsurf"], [("home", ".codeium/windsurf")], None),
    "zcode": (["zcode"], [("home", ".zcode"), ("appdata", "ZCode")], None),
}


def _default_run(argv, timeout):
    """Run argv; return (returncode, stdout). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "" if not p.stdout else "")
    except Exception as e:  # noqa: BLE001 - best effort probe
        return 1, str(e)


def _fwd(s):
    return str(s).replace("\\", "/")


def _config_dirs(spec, home, env):
    out = []
    for kind, val in spec:
        if kind == "home":
            out.append(Path(home) / val)
        elif kind == "env":
            v = env.get(val)
            if v:
                out.append(Path(v))
        elif kind == "appdata":
            base = env.get("APPDATA")
            if base:
                out.append(Path(base) / val)
    return out


def detect_hosts(home, env, which=shutil.which, run=_default_run):
    """One row per probed host (cline-roo is not probed)."""
    rows = []
    for host in HOSTS:
        sup = SUPPORT[host]
        binary = version = None
        dirs = []
        detected = False
        if host in PROBES:
            bins, dspecs, vargs = PROBES[host]
            for b in bins:
                found = which(b)
                if found:
                    binary = found
                    break
            dirs = [str(d) for d in _config_dirs(dspecs, home, env) if d.exists()]
            detected = bool(binary) or bool(dirs)
            if binary and vargs:
                code, out = run([binary] + vargs, 10)
                lines = (out or "").strip().splitlines()
                if code == 0 and lines:
                    version = lines[0].strip()
        rows.append({"host": host, "label": sup["label"], "binary": binary, "version": version,
                     "config_dirs": dirs, "detected": detected,
                     "support": {k: sup[k] for k in _FIELDS},
                     "installable": bool(sup["installs"]), "why_not": sup["why_not"]})
    return rows


# ---------- TypeSafe key ----------

def _file_has_key(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.startswith("TYPESAFE_API_KEY="):
                    if line.split("=", 1)[1].strip().strip('"').strip("'"):
                        return True
    except OSError:
        pass
    return False


def key_status(home, repo, env, read_registry=True):
    """{present, source, path, warning}. Never returns the key value."""
    res = {"present": False, "source": None, "path": None, "warning": None}
    if env.get("TYPESAFE_API_KEY"):
        res.update(present=True, source="env")
        return res
    cands = []
    if env.get("TYPESAFE_ENV_FILE"):
        cands.append(("TYPESAFE_ENV_FILE", Path(env["TYPESAFE_ENV_FILE"])))
    cands.append(("~/.config/typesafe/.env", Path(home) / ".config" / "typesafe" / ".env"))
    cands.append(("repo .env", Path(repo) / ".env"))
    for src, p in cands:
        if p.is_file() and _file_has_key(p):
            res.update(present=True, source=src, path=str(p))
            if os.name == "posix":
                try:
                    if p.stat().st_mode & 0o077:
                        res["warning"] = "key file %s is readable by others (chmod 600)" % p
                except OSError:
                    pass
            return res
    if read_registry and os.name == "nt" and not env.get("JEV_ONBOARD_NO_REGISTRY"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                if winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]:
                    res.update(present=True, source="registry")
        except Exception:  # noqa: BLE001
            pass
    return res


# ---------- Claude settings ----------

def settings_path(home):
    return Path(home) / ".claude" / "settings.json"


def manifest_path(home):
    return Path(home) / ".claude" / "jev" / "install-manifest.json"


def _expand_home(token, home):
    h = _fwd(home)
    for pre in ("${HOME}", "$HOME", "~"):
        if token.startswith(pre):
            return h + token[len(pre):]
    return token


def _split(cmd):
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def hook_script(command):
    """Return the OUR_SCRIPTS name when `command` is one of our hooks, else None."""
    c = _fwd(command)
    for s in OUR_SCRIPTS:
        if HOOK_MARKER + s in c:
            return s
    return None


def _hook_info(event, matcher, command, home, which):
    script = hook_script(command)
    ours = script is not None
    info = {"event": event, "matcher": matcher, "command": command, "ours": ours, "script_path": None,
            "script_exists": None, "python": None, "python_exists": None}
    if not ours:
        return info
    toks = _split(_fwd(command))
    marker = HOOK_MARKER + script
    for t in toks:
        if marker in t:
            info["script_path"] = _expand_home(t, home)
            break
    if info["script_path"]:
        info["script_exists"] = os.path.isfile(info["script_path"])
    if toks:
        py = _expand_home(toks[0], home)
        info["python"] = py
        info["python_exists"] = os.path.isfile(py) or bool(which(py))
    return info


def claude_settings(home, which=shutil.which):
    res = {"exists": False, "error": None, "hooks": [], "default_mode": None, "skip_prompt": None, "model": None,
           "remaps": {}, "base_url_host": None, "jev_env": {}}
    p = settings_path(home)
    if not p.is_file():
        return res
    res["exists"] = True
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except (OSError, ValueError) as e:
        res["error"] = "settings.json does not parse: %s" % e
        return res
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for h in g.get("hooks") or []:
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        res["hooks"].append(_hook_info(event, g.get("matcher"), h["command"], home, which))
    perms = data.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("defaultMode"), str):
        res["default_mode"] = perms["defaultMode"]
    sk = data.get("skipDangerousModePermissionPrompt")
    res["skip_prompt"] = sk if isinstance(sk, bool) else None
    if isinstance(data.get("model"), str):
        res["model"] = data["model"]
    senv = data.get("env")
    if isinstance(senv, dict):
        for k in REMAP_KEYS:
            if isinstance(senv.get(k), str):
                res["remaps"][k] = senv[k]
        base = senv.get("ANTHROPIC_BASE_URL")
        if isinstance(base, str):
            res["base_url_host"] = urlparse(base).hostname or None
        for k, v in senv.items():
            if k.startswith("JEV_") and isinstance(v, (str, int, float, bool)):
                res["jev_env"][k] = v
    return res


def remap_env(settings, env):
    """Remaps and base host: settings env first, then the process env."""
    out = {"remaps": {}, "base_url_host": settings.get("base_url_host")}
    for k in REMAP_KEYS:
        v = (settings.get("remaps") or {}).get(k) or env.get(k)
        if v:
            out["remaps"][k] = v
    if not out["base_url_host"] and env.get("ANTHROPIC_BASE_URL"):
        out["base_url_host"] = urlparse(env["ANTHROPIC_BASE_URL"]).hostname or None
    return out


def _third_party(host):
    return bool(host) and not (host == "anthropic.com" or host.endswith(".anthropic.com"))


def suggest_preset(settings, env):
    r = remap_env(settings, env)
    host = (r["base_url_host"] or "").lower()
    if (host.endswith("z.ai") or host.endswith("bigmodel.cn")) and \
            "ANTHROPIC_DEFAULT_OPUS_MODEL" in r["remaps"] and "ANTHROPIC_DEFAULT_SONNET_MODEL" in r["remaps"]:
        return "zai-glm"
    return "balanced"


def remap_warnings(settings, env, model_ids):
    """model_ids: the config's model ids (e.g. {'opus': 'claude-opus-5-5'})."""
    r = remap_env(settings, env)
    out = []
    host = r["base_url_host"]
    ids = [str(v) for v in (model_ids or {}).values()]
    if _third_party(host) and any(i.startswith("claude-") for i in ids):
        out.append("ANTHROPIC_BASE_URL points at %s but the routing config uses claude- model ids; "
                   "suggest --preset zai-glm (or --model-id KEY=ID)" % host)
    # Agent files pin full ids that the remap does not rewrite: only worth a warning while some pinned id is not a remap target.
    if r["remaps"] and not (ids and set(ids) <= set(str(v) for v in r["remaps"].values())):
        out.append("Model remaps are set (%s); agent files pin full model IDs, which the remap does not rewrite"
                   % ", ".join(sorted(r["remaps"])))
    o = r["remaps"].get("ANTHROPIC_DEFAULT_OPUS_MODEL")
    s = r["remaps"].get("ANTHROPIC_DEFAULT_SONNET_MODEL")
    if o and s and o == s:
        out.append("OPUS and SONNET remaps are the same model (%s)" % o)
    return out


def read_manifest(home):
    try:
        with open(manifest_path(home), encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def current_mode(settings, manifest):
    hooks = settings.get("hooks") or []
    gate = any(h["ours"] and h["script_path"] and h["script_path"].endswith("permission_gate.py")
               and h["script_exists"] for h in hooks)
    dm = settings.get("default_mode")
    ours_any = any(h["ours"] for h in hooks)
    set_by = False
    if manifest:
        for it in manifest.get("items") or []:
            if isinstance(it, dict) and it.get("kind") == "setting" and it.get("key") == "permissions.defaultMode":
                set_by = True
        if (manifest.get("choice") or {}).get("mode") == "bypass":
            set_by = True
    if dm == "bypassPermissions":
        mode = "bypass" if gate else "unprotected-bypass"
        explain = ("bypassPermissions with the Jev gate registered" if gate else
                   "bypassPermissions and no working Jev gate hook: nothing is protecting tool calls")
    elif manifest or ours_any:
        mode = "safe"
        explain = "permission mode %s with the gate %s" % (dm or "default", "registered" if gate else "not registered")
    else:
        mode = "not-installed"
        explain = "no jev hooks or manifest found"
    return {"mode": mode, "default_mode": dm, "gate_registered": gate, "set_by_onboard": set_by,
            "explain": explain}


# ---------- tools ----------

def _first_line(out):
    lines = (out or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _item(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _default_chrome_guarded(scripts_dir):
    try:
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import jevqa  # noqa: WPS433
        return jevqa._default_chrome()
    except Exception:  # noqa: BLE001
        return None


def tool_checks(home, repo, env, which=shutil.which, run=_default_run):
    items = []
    v = sys.version_info
    exe = _fwd(sys.executable)
    if (v.major, v.minor) < (3, 9):
        items.append(_item("python", "fail", "Python %d.%d is too old; need 3.9+" % (v.major, v.minor)))
    else:
        notes = []
        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            notes.append("running inside a venv; hooks should use a system interpreter (--python)")
        if "WindowsApps" in exe:
            notes.append("WindowsApps (Store stub) path")
        items.append(_item("python", "warn" if notes else "ok",
                           "%d.%d.%d at %s%s" % (v.major, v.minor, v.micro, exe,
                                                 "; " + "; ".join(notes) if notes else "")))
    items.append(_item("os", "ok", "%s (%s)" % (sys.platform, os.name)))
    git = which("git")
    if git:
        code, out = run([git, "-C", str(repo), "rev-parse", "--short", "HEAD"], 10)
        commit = _first_line(out) if code == 0 else None
        dirty = None
        if commit:
            c2, o2 = run([git, "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], 10)
            if c2 == 0:
                dirty = len([ln for ln in o2.splitlines() if ln.strip()])
        if commit:
            items.append(_item("git", "ok" if not dirty else "warn",
                               "commit %s, %s dirty tracked file(s)" % (commit, dirty if dirty is not None else "?")))
        else:
            items.append(_item("git", "info", "git found but %s is not a repository" % repo))
    else:
        items.append(_item("git", "info", "not found (optional)"))
    for name in ("node", "graphify"):
        p = which(name)
        items.append(_item(name, "ok" if p else "info", p or "not found (optional)"))
    gq = env.get("JEV_GRAPHQ") or which("graphq")
    items.append(_item("graphq", "ok" if gq else "info", gq or "not found (optional)"))
    chrome = env.get("JEVQA_CHROME") or _default_chrome_guarded(os.path.join(str(repo), "skill", "jev-orchestrator",
                                                                           "scripts"))
    items.append(_item("chrome", "ok" if chrome else "info", chrome or "not found (optional, for jevqa)"))
    ultra = Path(home) / "Documents" / "Tools" / "jev-ultrafast"
    items.append(_item("jev-ultrafast", "ok" if ultra.exists() else "info",
                       str(ultra) if ultra.exists() else "not found (optional)"))
    if env.get("CLAUDE_CONFIG_DIR"):
        items.append(_item("CLAUDE_CONFIG_DIR", "warn",
                           "set to %s; Claude Code reads that instead of ~/.claude, onboarding writes ~/.claude"
                           % env["CLAUDE_CONFIG_DIR"]))
    return items


def _is_link(p):
    if os.path.islink(p):
        return True
    if os.name == "nt":
        try:
            return bool(os.lstat(p).st_file_attributes & 0x400)
        except (OSError, AttributeError):
            return False
    return False


def _roles(repo):
    try:
        with open(Path(repo) / "config" / "agents.json", encoding="utf-8") as f:
            return list((json.load(f).get("agents") or {}).keys()) or list(DEFAULT_ROLES)
    except (OSError, ValueError):
        return list(DEFAULT_ROLES)


def existing_install(home, repo):
    home = Path(home)
    manifest = read_manifest(home)
    settings = claude_settings(home)
    legacy = manifest is None and any(h["ours"] for h in settings["hooks"])
    skill = home / ".claude" / "skills" / "jev-orchestrator"
    link = {"exists": os.path.lexists(skill), "is_link": False, "target": None, "points_to_repo": False,
            "dangling": False}
    if link["exists"]:
        link["is_link"] = _is_link(skill)
        if link["is_link"]:
            try:
                tgt = os.path.realpath(skill)
                link["target"] = _fwd(tgt)
                repo_skill = os.path.realpath(Path(repo) / "skill" / "jev-orchestrator")
                link["points_to_repo"] = os.path.normcase(tgt) == os.path.normcase(repo_skill)
            except OSError:
                pass
            link["dangling"] = not os.path.exists(skill)
    agents = {}
    for role in _roles(repo):
        a = home / ".claude" / "agents" / (role + ".md")
        if not os.path.lexists(a):
            agents[role] = "missing"
        elif os.path.islink(a):
            agents[role] = "symlink"
        else:
            agents[role] = "copy"
    active = home / ".claude" / "jev" / "agents.json"
    return {"manifest": manifest, "legacy": legacy, "skill_link": link, "agents": agents,
            "active_config": str(active) if active.is_file() else None}


def jev_online(repo):
    """One minimal Jev call (only with --online). Returns {ok, detail}; never prints the key."""
    scripts = os.path.join(str(repo), "skill", "jev-orchestrator", "scripts")
    try:
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import jevlib
        jevlib.ask({}, [jevlib.noul("Is this a connectivity check?", "yes", "no")], timeout=15.0, retries=1)
        return {"ok": True, "detail": "Jev answered"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": jevlib_redact(str(e))}


def jevlib_redact(text):
    try:
        import jevlib
        return jevlib.redact(text)
    except Exception:  # noqa: BLE001
        return "error (details withheld)"


def doctor(home, repo, env, *, online=False, which=shutil.which, run=_default_run, read_registry=True):
    home = Path(home)
    repo = Path(repo)
    hosts = detect_hosts(home, env, which, run)
    settings = claude_settings(home, which)
    manifest = read_manifest(home)
    mode = current_mode(settings, manifest)
    install = existing_install(home, repo)
    items = tool_checks(home, repo, env, which, run)
    ks = key_status(home, repo, env, read_registry)
    if ks["present"]:
        items.append(_item("typesafe key", "warn" if ks["warning"] else "ok",
                           "present (source: %s)%s" % (ks["source"], "; " + ks["warning"] if ks["warning"] else "")))
    else:
        items.append(_item("typesafe key", "warn", "not found; Jev calls fall back until one is set"))
    if online:
        r = jev_online(repo)
        items.append(_item("jev online", "ok" if r["ok"] else "warn", r["detail"]))
    critical = []
    if settings["error"]:
        critical.append(settings["error"] + ": " + CRITICAL_MSG)
    if mode["mode"] == "unprotected-bypass":
        critical.append("unprotected bypass: " + mode["explain"])
    for h in settings["hooks"]:
        if h["ours"] and (h["script_exists"] is False or h["python_exists"] is False):
            what = "script %s" % h["script_path"] if h["script_exists"] is False else "interpreter %s" % h["python"]
            critical.append("hook %s: missing %s: %s" % (hook_script(h["command"]), what, CRITICAL_MSG))
    for w in remap_warnings(settings, env, _config_models(repo, home)):
        items.append(_item("remap", "warn", w))
    items.append(_item("suggested preset", "info", suggest_preset(settings, env)))
    items.append(_item("mode", "info", "%s (%s)" % (mode["mode"], mode["explain"])))
    return {"items": items, "hosts": hosts, "settings": _public_settings(settings), "mode": mode,
            "install": install, "critical": critical, "key": ks}


def _public_settings(settings):
    """Settings summary without hook commands' arguments beyond what is needed (commands are not secret)."""
    return dict(settings)


def _config_models(repo, home=None):
    """Model ids of the routing config in use: the active ~/.claude/jev/agents.json when one is installed, else the shipped one."""
    paths = ([Path(home) / ".claude" / "jev" / "agents.json"] if home else []) + [Path(repo) / "config" / "agents.json"]
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                return dict(json.load(f).get("models") or {})
        except (OSError, ValueError):
            continue
    return {}


def format_doctor(report):
    lines = ["Hosts"]
    for h in report["hosts"]:
        if h["host"] == "cline-roo":
            continue
        state = "detected" if h["detected"] else "not found"
        extra = []
        if h["version"]:
            extra.append(h["version"])
        if h["config_dirs"]:
            extra.append("config: " + ", ".join(_fwd(d) for d in h["config_dirs"]))
        s = h["support"]
        lines.append("  %-12s %-9s gate=%s; install: %s%s" % (
            h["label"], state, s["gate"], "full harness" if h["installable"] else "nothing yet",
            ("  (" + "; ".join(extra) + ")") if extra else ""))
    lines.append("")
    lines.append("Checks")
    for it in report["items"]:
        lines.append("  [%s] %s: %s" % (it["status"].upper(), it["name"], it["detail"]))
    inst = report["install"]
    lines.append("")
    lines.append("Install state")
    lines.append("  manifest: %s; legacy hooks: %s" % ("yes" if inst["manifest"] else "no",
                                                       "yes" if inst["legacy"] else "no"))
    sl = inst["skill_link"]
    lines.append("  skill link: %s" % ("missing" if not sl["exists"] else
                                      ("link -> %s%s" % (sl["target"], " (DANGLING)" if sl["dangling"] else
                                                         "" if sl["points_to_repo"] else " (other target)")
                                       if sl["is_link"] else "real directory")))
    lines.append("  agents: %s" % ", ".join("%s=%s" % (k, v) for k, v in inst["agents"].items()))
    lines.append("  active config: %s" % (inst["active_config"] or "none (shipped config in use)"))
    lines.append("  mode: %s" % report["mode"]["mode"])
    if report["critical"]:
        lines.append("")
        for c in report["critical"]:
            lines.append("CRITICAL: " + c)
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")
