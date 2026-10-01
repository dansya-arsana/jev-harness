"""Sandbox helpers for the onboarding tests: a throwaway home plus a copy of the repo, never the real home.

Nothing here (or in the tests built on it) may write to ~/.claude, ~/.codex, ~/.zcode or ~/.config.
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import onboard_apply  # noqa: E402

FAKE_TOKEN = "sk-fake-SECRET-123"

# Captured at import, before any test patches HOME or USERPROFILE.
_REAL_HOMES = set()
for _v in (os.path.expanduser("~"), os.environ.get("USERPROFILE", ""), os.environ.get("HOME", "")):
    if _v:
        _REAL_HOMES.add(os.path.normcase(os.path.realpath(_v)))
_PROTECTED = (".claude", ".codex", ".zcode", ".config")
_ROOTS = []


def _under(path, base):
    path, base = os.path.normcase(os.path.realpath(str(path))), os.path.normcase(os.path.realpath(str(base)))
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def assert_not_real_home(path):
    """Fail unless path is inside the system temp dir and not the real home or one of its host config dirs."""
    p = os.path.realpath(str(path))
    if not _under(p, tempfile.gettempdir()):
        raise AssertionError("%s is not inside the temp dir" % p)
    for real in _REAL_HOMES:
        if os.path.normcase(p) == real or _under(real, p):
            raise AssertionError("%s is (or contains) the real home" % p)
        for d in _PROTECTED:
            if _under(p, os.path.join(real, d)):
                raise AssertionError("%s is inside the real %s" % (p, d))


def _copy_repo(dst):
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache")
    for name in ("skill", "agents", "config", "scripts"):
        shutil.copytree(str(REPO / name), str(dst / name), ignore=ignore)


def make_sandbox():
    """(root, home, repo_copy, env). Call cleanup(root) when done."""
    root = Path(tempfile.mkdtemp(prefix="jev-onboard-")).resolve()
    assert_not_real_home(root)
    _ROOTS.append(root)
    home, repo_copy, bin_dir = root / "home", root / "repo", root / "bin"
    for d in (home, repo_copy, bin_dir, root / "appdata", root / "localappdata"):
        d.mkdir(parents=True, exist_ok=True)
    _copy_repo(repo_copy)
    env = {}
    for k in ("SYSTEMROOT", "COMSPEC", "WINDIR", "TEMP", "TMP", "PATHEXT", "LANG"):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update({"PATH": str(bin_dir), "HOME": str(home), "USERPROFILE": str(home),
                "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
                "JEV_ONBOARD_NO_REGISTRY": "1", "TYPESAFE_BASE_URL": "https://127.0.0.1:9"})
    return root, home, repo_copy, env


def snapshot(directory):
    """{relative path: sha256} for every file under directory; links are recorded, never followed."""
    out = {}
    base = Path(directory)
    if not base.exists():
        return out

    def walk(d):
        for name in sorted(os.listdir(str(d))):
            p = d / name
            rel = os.path.relpath(str(p), str(base)).replace("\\", "/")
            if onboard_apply.is_link(p):
                out[rel] = "link->%s" % onboard_apply.link_target(p)
            elif p.is_dir():
                out[rel + "/"] = "dir"
                walk(p)
            else:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()

    walk(base)
    return out


def _unlink_links(d):
    for name in os.listdir(str(d)):
        p = d / name
        if onboard_apply.is_link(p):
            onboard_apply.remove_link(p)
        elif p.is_dir():
            _unlink_links(p)


def _on_error(func, path, exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def cleanup(root=None):
    roots = [Path(root)] if root else list(_ROOTS)
    for r in roots:
        assert_not_real_home(r)
        if r.exists():
            _unlink_links(r)
            shutil.rmtree(str(r), onerror=_on_error)
        if r in _ROOTS:
            _ROOTS.remove(r)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def legacy_owner_home(home, repo_copy):
    """Recreate the owner's pre-onboarding state: z.ai remaps, a planted fake token, two legacy jev hooks,
    one foreign hook, a skill link to the repo copy and copied agents."""
    home, repo_copy = Path(home), Path(repo_copy)
    assert_not_real_home(home)
    claude = home / ".claude"
    (claude / "skills").mkdir(parents=True, exist_ok=True)
    target = repo_copy / "skill" / "jev-orchestrator"
    onboard_apply.make_link(str(target), str(claude / "skills" / "jev-orchestrator"))
    py = sys.executable.replace("\\", "/")
    hook_dir_win = str(claude / "skills" / "jev-orchestrator" / "hooks")
    settings = {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN,
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.5-air",
        },
        "model": "opus",
        "skipDangerousModePermissionPrompt": True,
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash|Write|Edit", "hooks": [
                    {"type": "command", "command": '"%s" "%s\\permission_gate.py"' % (py, hook_dir_win), "timeout": 10}]},
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "node /opt/foreign/guard.js", "timeout": 5}]},
                {"matcher": "Agent|Task", "hooks": [
                    {"type": "command", "command": "python3 ~/.claude/skills/jev-orchestrator/hooks/dispatch_router.py",
                     "timeout": 10}]},
            ],
        },
    }
    write_json(claude / "settings.json", settings)
    (claude / "agents").mkdir(parents=True, exist_ok=True)
    for f in sorted((repo_copy / "agents").glob("jev-*.md")):
        shutil.copyfile(str(f), str(claude / "agents" / f.name))
    return settings
