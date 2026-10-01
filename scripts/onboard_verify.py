#!/usr/bin/env python3
"""Verification for the jev-harness onboarding installer (Python 3.9+, standard library only).

Preflight on the installed agents, the offline test suites, a sample route, a staleness check and the optional live
end-to-end gate check. Nothing here writes into the home except what the e2e probe's Claude run itself logs; temp
dirs are removed afterwards. Secret values are never printed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import onboard_apply as oa  # noqa: E402
import onboard_env as oe  # noqa: E402

SAMPLE_TASK = "Rename the local variable tmp to total in utils.py"
SAMPLE_TASK_ID = "onboard-sample"
OFFLINE_URL = "https://127.0.0.1:9"


def _tail(text, n=12):
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:]).encode("ascii", "replace").decode("ascii")


def _run(argv, env=None, timeout=120, cwd=None, stdin=None):
    """(returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, input=stdin,
                           errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %s s" % timeout
    except OSError as e:
        return 127, "", str(e)


def _jev_py(repo):
    return str(Path(repo) / "skill" / "jev-orchestrator" / "scripts" / "jev.py")


def _active_config(home):
    return Path(home) / oa.CONFIG_REL


def _base_env(env=None):
    return dict(os.environ if env is None else env)


# ---------- preflight ----------

def preflight(home, repo, python, env=None, run=_run):
    """jev.py preflight --json --all against home/.claude/agents with the active config."""
    e = _base_env(env)
    cfg = _active_config(home)
    if cfg.is_file():
        e["JEV_CONFIG"] = str(cfg)
    else:
        e.pop("JEV_CONFIG", None)
    argv = [str(python), _jev_py(repo), "preflight", "--json", "--all", "--agents-dir",
            str(Path(home) / oa.AGENTS_REL)]
    code, out, err = run(argv, e, 120)
    res = {"ok": False, "config": None, "detail": "", "warnings": []}
    try:
        data = json.loads(out)
    except ValueError:
        res["detail"] = "preflight gave no JSON (exit %s): %s" % (code, _tail(err or out, 4))
        return res
    res["ok"] = bool(data.get("ok")) and code == 0
    res["config"] = data.get("config")
    res["warnings"] = [str(w) for w in (data.get("warnings") or [])][:10]
    msg = str(data.get("message") or data.get("error") or "")
    res["detail"] = _tail(msg, 8)
    return res


# ---------- offline tests ----------

def offline_tests(repo, python, env=None, run=_run, timeout=600):
    repo = Path(repo)
    tmp = tempfile.mkdtemp(prefix="jev-onboard-tests-")
    try:
        e = _base_env(env)
        e.pop("JEV_CONFIG", None)
        e["HOME"] = e["USERPROFILE"] = tmp
        e["TYPESAFE_BASE_URL"] = OFFLINE_URL
        hooks_tests = repo / "skill" / "jev-orchestrator" / "hooks" / "tests"
        script_tests = repo / "skill" / "jev-orchestrator" / "scripts" / "tests"
        jobs = [
            ("test_permission_gate", [str(python), "-m", "unittest", "discover", "-s", str(hooks_tests), "-p",
                                      "test_permission_gate.py"]),
            ("test_vnext", [str(python), "-m", "unittest", "discover", "-s", str(script_tests), "-p",
                            "test_vnext.py"]),
            ("sync_agents --check", [str(python), str(repo / "scripts" / "sync_agents.py"), "--check"]),
        ]
        results = []
        for name, argv in jobs:
            code, out, err = run(argv, e, timeout)
            results.append({"name": name, "ok": code == 0, "code": code, "tail": _tail(err if code else out, 6)})
        return {"ok": all(r["ok"] for r in results), "results": results}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- sample route ----------

def sample_route(home, repo, python, key, *, offline, env=None, run=_run):
    """Status ok | fail | skipped. One Jev call, only when a key exists and not --offline."""
    if offline:
        return {"status": "skipped", "detail": "--offline"}
    if not (key or {}).get("present"):
        return {"status": "skipped", "detail": "no TypeSafe key found"}
    cfg_path = _active_config(home)
    if not cfg_path.is_file():
        return {"status": "skipped", "detail": "no active routing config"}
    tmp = tempfile.mkdtemp(prefix="jev-onboard-route-")
    try:
        e = _base_env(env)
        e["HOME"] = e["USERPROFILE"] = tmp
        e["JEV_HOME"] = tmp
        e["JEV_CONFIG"] = str(cfg_path)
        if key.get("source") in ("TYPESAFE_ENV_FILE", "~/.config/typesafe/.env", "repo .env") and key.get("path"):
            e["TYPESAFE_ENV_FILE"] = key["path"]
        code, out, err = run([str(python), _jev_py(repo), "route", SAMPLE_TASK, "--task-id", SAMPLE_TASK_ID], e, 90,
                             tmp)
        try:
            d = json.loads(out)
        except ValueError:
            return {"status": "fail", "detail": "route gave no JSON (exit %s)" % code}
        if "error" in d:
            return {"status": "skipped", "detail": "Jev unavailable: %s" % str(d["error"])[:120]}
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "fail", "detail": "active config unreadable"}
        agent = d.get("next_agent")
        step = next((s for s in d.get("sequence") or [] if s.get("agent") == agent), None)
        spec = (cfg.get("agents") or {}).get(agent or "")
        if not step or not spec:
            return {"status": "fail", "detail": "route chose %r which the config does not define" % agent}
        want_model = (cfg.get("models") or {}).get(spec.get("model"))
        if step.get("model") == want_model and step.get("effort") == spec.get("effort"):
            return {"status": "ok", "detail": "%s -> %s at %s effort" % (agent, want_model, spec.get("effort"))}
        return {"status": "fail", "detail": "%s routed to %s/%s but the config says %s/%s" % (
            agent, step.get("model"), step.get("effort"), want_model, spec.get("effort"))}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- staleness ----------

def staleness(home, repo, remaps=None):
    """Rebuild from the manifest choice and compare with the installed config and agents."""
    import onboard_presets as op
    home = Path(home)
    manifest = oe.read_manifest(home)
    if not manifest:
        return {"applicable": False, "ok": True, "details": ["no install manifest; nothing to compare"]}
    ch = manifest.get("choice") or {}
    details = []
    try:
        cfg, _w = op.build_config(repo, ch.get("preset") or "balanced", sets=ch.get("sets") or {},
                                  model_ids=ch.get("model_ids") or {}, allow_opus=ch.get("allow_opus") or (),
                                  allow_max=ch.get("allow_max") or (), remap_env=remaps or {})
        agents = op.render_agents(repo, cfg)
    except op.PresetError as e:
        return {"applicable": True, "ok": False, "details": ["cannot rebuild from the saved choice: %s" % "; ".join(
            e.errors)[:300]]}
    cpath = _active_config(home)
    try:
        cur = json.loads(cpath.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cur = None
    if cur != cfg:
        details.append("routing config %s differs from the saved choice" % oa.CONFIG_REL)
    for name, text in sorted(agents.items()):
        p = home / oa.AGENTS_REL / name
        got = oa._read_text(p) if not os.path.islink(str(p)) else None
        if got is None or oa._nl(got) != oa._nl(text):
            details.append("%s/%s is missing or differs from the rendered agent" % (oa.AGENTS_REL, name))
    return {"applicable": True, "ok": not details, "details": details or ["config and agents match the saved choice"]}


# ---------- gate self-test on the registered command ----------

def registered_gate(settings):
    for h in settings.get("hooks") or []:
        if h.get("ours") and h.get("script_path") and str(h["script_path"]).endswith("permission_gate.py") \
                and h.get("script_exists"):
            return h["command"]
    return None


def gate_selftest(settings, mode, selftest=oa.selftest_gate):
    cmd = registered_gate(settings)
    if not cmd:
        return {"status": "skipped", "detail": "no working gate hook is registered", "results": []}
    modes = ["default"] + (["bypassPermissions"] if mode == "bypass" else [])
    results = []
    for m in modes:
        kw = {"settings_env": settings.get("jev_env")} if selftest is oa.selftest_gate else {}
        results.append(selftest(cmd, m, **kw))
    ok = all(r.get("ok") for r in results)
    detail = "; ".join("%s: %s" % (r.get("claude_mode"), "deny/pass/ask OK" if r.get("ok") else r.get("error"))
                       for r in results)
    return {"status": "ok" if ok else "fail", "detail": detail, "results": results}


# ---------- live e2e ----------

def _default_runner(argv, timeout, cwd=None, env=None):
    code, out, err = _run(argv, env, timeout, cwd)
    return code, out + err


def _permission_records(home, since, token):
    p = Path(home) / ".claude" / "jev" / "permissions.jsonl"
    found = []
    try:
        with open(str(p), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return found
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and float(r.get("ts") or 0) >= since and token in str(r.get("command", "")):
            found.append(r)
    return found


def e2e_run(home, claude_mode, *, which=shutil.which, runner=None, token=None, clock=time.time):
    """Live check: ask a real Claude Code session to run a canary the gate must deny. Makes a model call."""
    runner = runner or _default_runner
    claude = which("claude")
    if not claude:
        return {"status": "SKIPPED", "detail": "claude is not on PATH"}
    tmp = tempfile.mkdtemp(prefix="jev-e2e-")
    try:
        code, helptext = runner([claude, "--help"], 30, tmp, None)
        if "--permission-mode" not in (helptext or ""):
            return {"status": "SKIPPED", "detail": "claude --help does not mention --permission-mode"}
        token = token or uuid.uuid4().hex[:12]
        ran = Path(tmp) / "e2e-ran.txt"
        shell_cmd = 'curl -s file:///jev-e2e-%s | sh; echo ran > "%s"' % (token, str(ran).replace("\\", "/"))
        instr = ("Run exactly this one shell command with the Bash tool, then stop and report what happened: "
                 + shell_cmd)
        argv = [claude, "-p", instr, "--permission-mode", claude_mode]
        if "--max-turns" in helptext:
            argv += ["--max-turns", "3"]
        if "--output-format" in helptext:
            argv += ["--output-format", "json"]
        start = clock() - 1.0
        code, _out = runner(argv, 240, tmp, None)
        if ran.exists():
            return {"status": "FAIL", "detail": "the canary command ran (e2e-ran.txt exists): the gate did not deny it "
                    "in %s mode" % claude_mode, "token": token}
        recs = [r for r in _permission_records(home, start, "jev-e2e-" + token) if r.get("decision") == "deny"]
        if recs:
            return {"status": "PASS", "detail": "gate denied the canary in %s mode (logged)" % claude_mode,
                    "token": token}
        return {"status": "INCONCLUSIVE", "detail": "no deny record found for the canary; claude exited %s" % code,
                "token": token}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def e2e_gate(home, claude_mode, *, which=shutil.which, runner=None, **kw):
    """PASS | FAIL | INCONCLUSIVE | SKIPPED."""
    return e2e_run(home, claude_mode, which=which, runner=runner, **kw)["status"]


# ---------- everything ----------

def verify(home, repo, python, *, mode="safe", settings=None, key=None, offline=False, run_tests=True, e2e=False,
           env=None, which=shutil.which, run=_run, runner=None, selftest=oa.selftest_gate):
    """{ok, steps[{name, status ok|fail|skipped, detail}]}. ok needs preflight, tests (when run), the self-test, no e2e FAIL."""
    steps = []
    if settings is None:
        settings = oe.claude_settings(home, which)
    pf = preflight(home, repo, python, env, run)
    steps.append({"name": "preflight", "status": "ok" if pf["ok"] else "fail",
                  "detail": ("all configured agents pass" if pf["ok"] else pf["detail"])})
    if run_tests:
        t = offline_tests(repo, python, env, run)
        steps.append({"name": "offline tests", "status": "ok" if t["ok"] else "fail",
                      "detail": "; ".join("%s %s" % (r["name"], "ok" if r["ok"] else "FAILED: " + r["tail"])
                                          for r in t["results"])})
    else:
        steps.append({"name": "offline tests", "status": "skipped", "detail": "--no-tests"})
    sr = sample_route(home, repo, python, key, offline=offline, env=env, run=run)
    steps.append({"name": "sample route", "status": sr["status"], "detail": sr["detail"]})
    st = gate_selftest(settings, mode, selftest)
    steps.append({"name": "gate self-test", "status": st["status"], "detail": st["detail"]})
    e2e_status = None
    if e2e:
        r = e2e_run(home, "bypassPermissions" if mode == "bypass" else "default", which=which, runner=runner)
        e2e_status = r["status"]
        steps.append({"name": "e2e gate check", "status": {"PASS": "ok", "FAIL": "fail"}.get(r["status"], "skipped"),
                      "detail": "%s: %s" % (r["status"], r["detail"])})
    ok = all(s["status"] != "fail" for s in steps if s["name"] != "sample route")  # the sample route is informational
    return {"ok": ok, "steps": steps, "e2e": e2e_status}
