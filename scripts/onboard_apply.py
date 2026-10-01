#!/usr/bin/env python3
"""Apply engine for the jev-harness onboarding installer (Python 3.9+, standard library only).

Plans and applies the changes inside <home>/.claude (skill link, agents, routing config, settings.json hooks and
mode keys, CLAUDE.md rules block) plus the optional TypeSafe key file, and undoes them from a manifest.
Every pre-existing file it changes is backed up first, settings.json is only edited atomically and only
when it parses, and nothing here ever prints a secret value.

Backups are taken lazily, just before each file's own write (the backup dir is created at the first one and
mirrors the file's path under home), not all up front. A run that fails part-way therefore keeps the backups of
the files it had already replaced; completed actions stay in the manifest so --uninstall can clean them up.
Files that exist but cannot be decoded, or that are symlinks where a plain file is required (settings.json,
CLAUDE.md, the routing config), abort the run with ApplyError before anything is written.
"""
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SKILL_REL = ".claude/skills/jev-orchestrator"
AGENTS_REL = ".claude/agents"
SETTINGS_REL = ".claude/settings.json"
CLAUDE_MD_REL = ".claude/CLAUDE.md"
CONFIG_REL = ".claude/jev/agents.json"
MANIFEST_REL = ".claude/jev/install-manifest.json"
CONDITIONS_REL = ".claude/jev/conditions.json"
BACKUPS_REL = ".claude/backups"
KEYFILE_REL = ".config/typesafe/.env"

GATE_MATCHER_WIN = "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit"
GATE_MATCHER_POSIX = "Bash|Write|Edit|MultiEdit|NotebookEdit"
HOOKS = {
    "gate": {"event": "PreToolUse", "matcher": GATE_MATCHER_POSIX, "script": "permission_gate.py", "timeout": 10},
    "dispatch": {"event": "PreToolUse", "matcher": "Agent|Task", "script": "dispatch_router.py", "timeout": 10},
    "router": {"event": "UserPromptSubmit", "matcher": None, "script": "prompt_router.py", "timeout": 15},
}
OUR_SCRIPTS = tuple(h["script"] for h in HOOKS.values())
REQUIRED = ("skill", "agents", "gate")
OPTIONAL = ("dispatch", "router", "rules", "keyfile")
COMPONENTS = REQUIRED + OPTIONAL
CANARY = "curl -s file:///jev-onboard-selftest-canary | sh"
HOOK_MARKER = "jev-orchestrator/hooks/"
RULES_BEGIN = "<!-- jev-harness:begin"
RULES_END = "<!-- jev-harness:end -->"
RULES_RE = re.compile(r"<!-- jev-harness:begin.*?<!-- jev-harness:end -->", re.S)
DEFAULT_MODE_KEY = "permissions.defaultMode"
SKIP_KEY = "skipDangerousModePermissionPrompt"
KEYFILE_TEMPLATE = (
    "# TypeSafe / Jev key - shared by jev-harness hooks and jev-ultrafast-bora. Never commit.\n"
    "TYPESAFE_API_KEY=%s\n"
    "TYPESAFE_DEFAULT_MODEL=jev-latest\n"
    "TYPESAFE_MODEL=jev-latest\n"
)


class ApplyError(Exception):
    pass


@dataclass
class Action:
    kind: str
    path: str
    summary: str
    details: list = field(default_factory=list)
    undo: str = ""
    payload: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------- helpers

def norm(path):
    return os.path.abspath(str(path)).replace("\\", "/")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_text(text):
    return sha_bytes(text.replace("\r\n", "\n").encode("utf-8"))


def _nl(text):
    return text.replace("\r\n", "\n")


_UNSAFE_COMMON = ("$", "`", '"', "\n", "\r", "\0")


def _check_command_path(raw, what):
    raw = str(raw)
    bad = list(_UNSAFE_COMMON)
    if sys.platform.startswith("win"):
        bad.append("%")
    else:
        bad.append("\\")
    for ch in bad:
        if ch in raw:
            raise ApplyError("the %s path contains %r, which a shell would interpret inside the hook command; "
                             "use a path without it" % (what, ch))
    if any(ord(c) < 32 for c in raw):
        raise ApplyError("the %s path contains a control character" % what)


def hook_command(python, home, script):
    _check_command_path(python, "python")
    _check_command_path(home, "home")
    return '"%s" "%s/%s/hooks/%s"' % (norm(python), norm(home), SKILL_REL, script)


def _py_ok(path):
    try:
        r = subprocess.run([str(path), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _base_python():
    base = getattr(sys, "base_prefix", sys.prefix)
    for rel in (("python.exe",), ("bin", "python3"), ("bin", "python")):
        p = os.path.join(base, *rel)
        if os.path.isfile(p):
            return p
    return None


def pick_python(override=None, manifest_python=None):
    """(path, warnings). Only an interpreter the user passed explicitly (--python, re-validated), the current
    sys.executable or the sys.base_prefix interpreter is ever run or registered. A manifest-recorded python is
    only compared (to report that the interpreter changed) and is never executed."""
    warnings = []
    if override:
        o = str(override)
        if _is_special_path(o) or not os.path.isabs(o) or not os.path.isfile(o):
            raise ApplyError("--python %s must be an absolute path to an existing local interpreter" % o)
        if not _py_ok(o):
            raise ApplyError("--python %s does not run or is older than Python 3.9" % o)
        cand = o
    else:
        cand = sys.executable
        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            base = _base_python()
            if base and _py_ok(base):
                cand = base
            else:
                warnings.append("running inside a virtual environment; hooks will call %s which may disappear "
                                "(use --python to pick a stable interpreter)" % norm(cand))
        if not cand or not _py_ok(cand):
            raise ApplyError("no working Python 3.9+ interpreter found; pass --python PATH")
    if "windowsapps" in norm(cand).lower():
        warnings.append("%s is a Microsoft Store python; hooks can fail to start (use --python with a python.org install)"
                        % norm(cand))
    if isinstance(manifest_python, str) and manifest_python and norm(manifest_python) != norm(cand):
        warnings.append("the interpreter changed since the last install (was %s); hooks will be re-registered with %s"
                        % (manifest_python.replace("\\", "/"), norm(cand)))
    return norm(cand), warnings


def is_ours(command):
    """The script name when the hook command points into jev-orchestrator/hooks/, else None."""
    if not isinstance(command, str):
        return None
    c = command.replace("\\", "/")
    for s in OUR_SCRIPTS:
        if HOOK_MARKER + s in c:
            return s
    return None


def read_json(path):
    """(data, sha256 of the raw bytes | None when the file is missing). Raises ApplyError on bad JSON."""
    p = str(path)
    if not lexists(p):
        return {}, None
    if is_link(p):
        raise ApplyError("%s is a symlink or link; onboarding will not edit it (replace it with a real file first)" % norm(p))
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise ApplyError("%s cannot be read (%s); nothing written" % (norm(p), type(e).__name__))
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ApplyError("%s is not valid JSON (%s); fix or move it, onboarding never overwrites it" % (norm(p), type(e).__name__))
    if not isinstance(data, dict):
        raise ApplyError("%s is not a JSON object; onboarding never overwrites it" % norm(p))
    return data, sha_bytes(raw)


def _file_sha(path):
    try:
        with open(str(path), "rb") as f:
            return sha_bytes(f.read())
    except OSError:
        return None


def write_bytes_atomic(path, data, mode=None):
    p = str(path)
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".jev-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(p):
            try:
                shutil.copymode(p, tmp)
            except OSError:
                pass
        elif mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha_bytes(data)


def write_json_atomic(path, data, expected_sha):
    """Re-hash the file first; on mismatch raise. Returns the sha256 of what was written."""
    cur = _file_sha(path)
    if cur != expected_sha:
        raise ApplyError("%s changed while onboarding ran; nothing written" % norm(path))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return write_bytes_atomic(path, text.encode("utf-8"), mode=0o644)


# ---------------------------------------------------------------- links

def is_link(p):
    p = str(p)
    if os.path.islink(p):
        return True
    if sys.platform.startswith("win"):
        try:
            st = os.lstat(p)
        except OSError:
            return False
        if not getattr(st, "st_file_attributes", 0) & 0x400:
            return False
        tag = getattr(st, "st_reparse_tag", None)
        return tag is None or tag in (0xA0000003, 0xA000000C)
    return False


def lexists(p):
    try:
        os.lstat(str(p))
        return True
    except OSError:
        return False


def link_target(p):
    try:
        t = os.readlink(str(p))
    except OSError:
        return None
    if t.startswith("\\\\?\\"):
        t = t[4:]
    return norm(t)


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def make_link(target, link):
    target, link = str(target), str(link)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if not sys.platform.startswith("win"):
        os.symlink(target, link, target_is_directory=True)
        return
    errs = []
    try:
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
        return
    except Exception as e:
        errs.append("CreateJunction: %s" % e)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.abspath(link), os.path.abspath(target)],
                       capture_output=True, text=True)
    if r.returncode == 0 and is_link(link):
        return
    errs.append("mklink /J: %s" % (r.stdout + r.stderr).strip()[:200])
    raise ApplyError("cannot create a link to %s (%s); the skill folder is never copied silently" % (norm(target), "; ".join(errs)))


def remove_link(p):
    p = str(p)
    if not is_link(p):
        raise ApplyError("%s is not a link; refusing to remove it" % norm(p))
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)
    if lexists(p):
        raise ApplyError("could not remove the link %s" % norm(p))


# ---------------------------------------------------------------- manifest

def load_manifest(home):
    p = Path(home) / MANIFEST_REL
    if not p.exists():
        return None
    data, _ = read_json(p)
    return data


_AGENT_NAME_RE = re.compile(r"^jev-[A-Za-z0-9._-]+\.md$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _lexical_rel(rel, what):
    """A manifest path must be a plain relative path: no absolute form, no drive, no '..' parts."""
    if not isinstance(rel, str) or not rel:
        raise ApplyError("manifest: %s is not a path" % what)
    r = rel.replace("\\", "/")
    if r.startswith("/") or _DRIVE_RE.match(r) or os.path.isabs(rel):
        raise ApplyError("manifest: %s is an absolute path (%s); refusing to use it" % (what, rel))
    if any(part == ".." for part in r.split("/")):
        raise ApplyError("manifest: %s contains '..' (%s); refusing to use it" % (what, rel))
    return r


def _contained(home, rel, base_rel, what, follow_final):
    """home/rel must stay inside home/base_rel after resolving links (the final component is only resolved
    when follow_final, because install paths such as the skill link are links by design)."""
    r = _lexical_rel(rel, what)
    base = os.path.normcase(os.path.realpath(str(Path(home) / base_rel)))
    full = Path(home) / r
    probe = os.path.realpath(str(full)) if follow_final else os.path.join(os.path.realpath(str(full.parent)), full.name)
    probe = os.path.normcase(probe)
    if not (probe == base or probe.startswith(base.rstrip(os.sep) + os.sep)):
        raise ApplyError("manifest: %s resolves outside %s (%s); refusing to use it" % (what, base_rel, rel))
    return r


def _backup_ref(home, rel, what):
    if rel is None:
        return None
    r = _lexical_rel(rel, what)
    if not r.startswith(BACKUPS_REL + "/"):
        raise ApplyError("manifest: %s is not under %s (%s); refusing to use it" % (what, BACKUPS_REL, rel))
    return _contained(home, r, BACKUPS_REL, what, True)


def _is_special_path(p):
    """UNC, long-path and device prefixes (anything starting with two slashes): never handed to a filesystem call."""
    return isinstance(p, str) and p.replace("\\", "/").startswith("//")


def _no_special(p, what):
    if _is_special_path(p):
        raise ApplyError("manifest: %s is a UNC or device path (%s); refusing to touch it" % (what, p))


def _need(it, field, types, what, optional=False):
    v = it.get(field)
    if v is None and optional:
        return v
    if isinstance(v, bool) and bool not in types:
        raise ApplyError("manifest: %s.%s has the wrong type" % (what, field))
    if not isinstance(v, types):
        raise ApplyError("manifest: %s.%s has the wrong type" % (what, field))
    return v


def _under_dir(path, base):
    path, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(base))
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _relink_target_ok(repo, target, sub):
    """A previous link may only be re-created when it points at (or inside) the RUNNING repo's <sub> folder.
    manifest['repo'] and anything else the manifest says is never trusted."""
    if repo is None or not isinstance(target, str) or _is_special_path(target) or not os.path.isabs(target):
        return False
    base = os.path.join(str(repo), *sub)
    return os.path.isdir(base) and _under_dir(target, base) and os.path.exists(target)


def _check_previous(home, repo, manifest, it, what, sub):
    prev = it.get("previous")
    if prev is None:
        return
    if not isinstance(prev, dict):
        raise ApplyError("manifest: %s.previous is not an object" % what)
    t = prev.get("type")
    if t not in (None, "none", "link", "file", "dir"):
        raise ApplyError("manifest: %s.previous has an unknown type" % what)
    if t == "link":
        tgt = prev.get("target")
        if not _relink_target_ok(repo, tgt, sub):
            shown = tgt.replace("\\", "/") if isinstance(tgt, str) else "?"
            manifest.setdefault("_warnings", []).append(
                "%s will not be re-linked; previous target was %s" % (what, shown))
            it["previous"] = {"type": "none", "target": None, "backup": None}
            return
    if t == "dir":
        _backup_ref(home, prev.get("backup"), what + " previous folder backup")
    elif prev.get("backup") is not None:
        _backup_ref(home, prev.get("backup"), what + " previous backup")


def validate_manifest(home, manifest, repo=None):
    """Schema- and path-check the manifest before any code uses it. Every path must be one of the fixed install
    files under home/.claude, restore sources must sit in home/.claude/backups, setting items may never cause a
    write of bypassPermissions or true, and UNC/device paths are rejected before any filesystem call.
    A previous link is only ever re-created inside the running repo (the repo argument); manifest['repo'] is
    checked for shape but never trusted. Raises ApplyError (nothing is touched) otherwise."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items", []), list):
        raise ApplyError("manifest: unexpected shape; refusing to use it")
    for k in ("repo",):
        if manifest.get(k) is not None:
            _need(manifest, k, (str,), "manifest")
            _no_special(manifest[k], "repo")
    if manifest.get("choice") is not None:
        ch = _need(manifest, "choice", (dict,), "manifest")
        sv = ch.get("sets")
        if sv is not None and not (isinstance(sv, list) and all(isinstance(x, str) for x in sv)) and not (
                isinstance(sv, dict) and all(isinstance(k, str) and isinstance(v, (str, dict)) for k, v in sv.items())):
            raise ApplyError("manifest: choice.sets has the wrong type")
        for k in ("components", "allow_opus", "allow_max"):
            v = ch.get(k)
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ApplyError("manifest: choice.%s must be a list of strings" % k)
        mi = ch.get("model_ids")
        if mi is not None and not (isinstance(mi, list) and all(isinstance(x, str) for x in mi)) and not (
                isinstance(mi, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in mi.items())):
            raise ApplyError("manifest: choice.model_ids has the wrong type")
        for k in ("preset", "mode", "python"):
            if ch.get(k) is not None and not isinstance(ch[k], str):
                raise ApplyError("manifest: choice.%s must be a string" % k)
        if ch.get("skip_bypass_prompt") is not None and not isinstance(ch["skip_bypass_prompt"], bool):
            raise ApplyError("manifest: choice.skip_bypass_prompt must be true or false")
    fixed = {"link": (SKILL_REL,), "settings_file": (SETTINGS_REL,), "rules_block": (CLAUDE_MD_REL,),
             "seed": (CONDITIONS_REL, KEYFILE_REL)}
    for it in manifest.get("items", []):
        if not isinstance(it, dict):
            raise ApplyError("manifest: an item is not an object")
        kind = it.get("kind")
        what = str(kind)
        if kind == "file":
            path = _lexical_rel(it.get("path"), "file path")
            if path != CONFIG_REL and not (path.startswith(AGENTS_REL + "/") and _AGENT_NAME_RE.match(path[len(AGENTS_REL) + 1:])):
                raise ApplyError("manifest: file path %s is not a jev agent or the routing config" % path)
            _contained(home, path, ".claude", "file path", False)
            _need(it, "sha256", (str,), what, True)
            _need(it, "created", (bool,), what, True)
            _backup_ref(home, _need(it, "backup", (str,), what, True), "file backup")
            _check_previous(home, repo, manifest, it, path, ("agents",))
        elif kind in fixed:
            path = _lexical_rel(it.get("path"), kind + " path")
            if path not in fixed[kind]:
                raise ApplyError("manifest: %s path %s is not allowed" % (kind, path))
            _contained(home, path, ".claude" if path.startswith(".claude") else ".config", kind + " path", False)
            if kind in ("settings_file", "rules_block"):
                _need(it, "sha_after", (str,), what, True)
                _backup_ref(home, _need(it, "original_backup", (str,), what, True), kind + " backup")
            if kind == "rules_block":
                _need(it, "sha256", (str,), what, True)
            if kind == "link":
                _need(it, "target", (str,), what, True)
                _no_special(it.get("target"), "link target")
                _check_previous(home, repo, manifest, it, path, ("skill", "jev-orchestrator"))
        elif kind == "setting":
            key = it.get("key")
            if key not in (DEFAULT_MODE_KEY, SKIP_KEY):
                raise ApplyError("manifest: setting key %r is not one onboarding manages" % (key,))
            old = _need(it, "old", (dict,), what)
            if not isinstance(old.get("present"), bool):
                raise ApplyError("manifest: setting.old.present must be true or false")
            want = "bypassPermissions" if key == DEFAULT_MODE_KEY else True
            if it.get("new") != want or isinstance(it.get("new"), bool) != isinstance(want, bool):
                raise ApplyError("manifest: setting.new for %s is not the value onboarding sets" % key)
            ov = old.get("value")
            if old["present"] and (ov == "bypassPermissions" or ov is True) or (
                    old["present"] and not isinstance(ov, (str, bool, int, float, type(None)))):
                raise ApplyError("manifest: setting.old for %s would restore a bypass value; refusing" % key)
            _need(it, "created_parent", (bool,), what, True)
        elif kind == "hook":
            pass
        else:
            raise ApplyError("manifest: unknown item kind %r" % (kind,))
    return manifest


def load_manifest_checked(home, repo=None):
    m = load_manifest(home)
    return validate_manifest(home, m, repo) if m is not None else None


def _git_commit(repo):
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def _iso(now):
    return now.isoformat(timespec="seconds")


def _rel(home, path):
    return os.path.relpath(str(path), str(home)).replace("\\", "/")


def _items(manifest, kind):
    return [i for i in (manifest or {}).get("items", []) if i.get("kind") == kind]


def _find_item(manifest, kind, path):
    for i in _items(manifest, kind):
        if i.get("path") == path:
            return i
    return None


def _set_items(manifest, kind, new_items, keep=lambda i: False):
    """Replace every item of this kind (except those where keep(item) is true) with new_items."""
    manifest["items"] = [i for i in manifest.get("items", []) if i.get("kind") != kind or keep(i)] + list(new_items)


def _upsert(manifest, kind, item, key="path"):
    manifest["items"] = [i for i in manifest.get("items", []) if not (i.get("kind") == kind and i.get(key) == item.get(key))]
    manifest["items"].append(item)


class Ctx(object):
    """One apply run: the lazily created backup dir and the manifest saved after every action."""

    def __init__(self, home, repo, now, choice=None):
        self.home = Path(home)
        self.repo = Path(repo)
        self.now = now
        self.backup_dir = None
        self.manifest = load_manifest_checked(home, repo)
        fresh = self.manifest is None
        if fresh:
            self.manifest = {"schema": 1, "repo": norm(repo), "harness_commit": _git_commit(repo),
                             "installed_at": _iso(now), "updated_at": _iso(now), "host": "claude",
                             "choice": choice or {}, "items": [], "backups": []}
        self.manifest.setdefault("items", [])
        self.manifest.setdefault("backups", [])
        if choice is not None:
            self.manifest["choice"] = choice
        self.manifest["repo"] = norm(repo)
        self.manifest["updated_at"] = _iso(now)

    def ensure_backup_dir(self):
        if self.backup_dir is None:
            root = self.home / BACKUPS_REL
            stamp = "jev-onboard-" + self.now.strftime("%Y%m%d-%H%M%S")
            n = 0
            while True:
                cand = root / (stamp if n == 0 else "%s-%d" % (stamp, n))
                try:
                    os.makedirs(str(cand))
                    break
                except FileExistsError:
                    n += 1
            self.backup_dir = cand
            self.manifest["backups"].append(_rel(self.home, cand))
        return self.backup_dir

    def backup_target(self, path):
        return self.ensure_backup_dir() / _rel(self.home, path)

    def backup_file(self, path):
        """Copy path into the backup dir (mirroring its place under home). Returns the home-relative backup path."""
        dst = self.backup_target(path)
        if not dst.exists():
            os.makedirs(str(dst.parent), exist_ok=True)
            shutil.copy2(str(path), str(dst))
        return _rel(self.home, dst)

    def move_to_backup(self, path):
        dst = self.backup_target(path)
        os.makedirs(str(dst.parent), exist_ok=True)
        if dst.exists():
            n = 1
            while Path("%s.%d" % (dst, n)).exists():
                n += 1
            dst = Path("%s.%d" % (dst, n))
        os.rename(str(path), str(dst))
        return _rel(self.home, dst)

    def save(self):
        self.manifest.pop("_warnings", None)
        p = self.home / MANIFEST_REL
        write_bytes_atomic(p, (json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), mode=0o644)


# ---------------------------------------------------------------- pure settings edits

def _hooks_shape(settings):
    hooks = settings.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise ApplyError("settings.json 'hooks' is not an object; refusing to edit it")
    for ev, lst in hooks.items():
        if not isinstance(lst, list):
            raise ApplyError("settings.json hooks.%s is not a list; refusing to edit it" % ev)
    return hooks


def _group_hooks(group):
    return group.get("hooks") if isinstance(group, dict) and isinstance(group.get("hooks"), list) else None


def _strip_ours(settings):
    """(new settings without any hook object of ours, number removed)."""
    new = copy.deepcopy(settings)
    hooks = _hooks_shape(new)
    removed = 0
    for ev in list(hooks.keys()):
        lst = hooks[ev]
        out = []
        touched = False
        for g in lst:
            gh = _group_hooks(g)
            if gh is None:
                out.append(g)
                continue
            keep = [h for h in gh if not (isinstance(h, dict) and is_ours(h.get("command")))]
            n = len(gh) - len(keep)
            if n:
                touched = True
                removed += n
                if keep:
                    g["hooks"] = keep
                    out.append(g)
            else:
                out.append(g)
        if touched:
            if out:
                hooks[ev] = out
            else:
                del hooks[ev]
    return new, removed


def remove_our_hooks(settings):
    """(settings without our hooks, number of hook objects removed)."""
    return _strip_ours(settings)


def _wanted_hook(w):
    return {"type": "command", "command": w["command"], "timeout": w["timeout"]}


def _is_exact(settings, wanted):
    hooks = _hooks_shape(settings)
    found = {}
    for ev, lst in hooks.items():
        for g in lst:
            gh = _group_hooks(g)
            if gh is None:
                continue
            for h in gh:
                s = is_ours(h.get("command")) if isinstance(h, dict) else None
                if s is None:
                    continue
                if s in found or s not in wanted or len(gh) != 1:
                    return False
                w = wanted[s]
                if ev != w["event"] or h != _wanted_hook(w):
                    return False
                if g.get("matcher") != w.get("matcher") or set(g.keys()) - {"matcher", "hooks"}:
                    return False
                found[s] = True
    return set(found) == set(wanted)


def merge_hooks(settings, wanted):
    """Replace every hook of ours with the wanted ones; foreign hooks and other keys are never touched.
    wanted: {script: {event, matcher|None, command, timeout}}. Returns (new settings, notes)."""
    if _is_exact(settings, wanted):
        return copy.deepcopy(settings), []
    new, removed = _strip_ours(settings)
    hooks = new.get("hooks")
    if hooks is None:
        hooks = new["hooks"] = {}
    notes = []
    if removed:
        notes.append("%d older jev hook entr%s replaced" % (removed, "y" if removed == 1 else "ies"))
    for script, w in wanted.items():
        group = {}
        if w.get("matcher"):
            group["matcher"] = w["matcher"]
        group["hooks"] = [_wanted_hook(w)]
        hooks.setdefault(w["event"], []).append(group)
        notes.append("%s registered under %s" % (script, w["event"]))
    if not hooks and not settings.get("hooks"):
        new.pop("hooks", None)
    return new, notes


def _get_key(settings, key):
    if key == DEFAULT_MODE_KEY:
        perms = settings.get("permissions")
        if isinstance(perms, dict) and "defaultMode" in perms:
            return True, perms["defaultMode"]
        return False, None
    return (key in settings), settings.get(key)


def _set_key(settings, key, value):
    if key == DEFAULT_MODE_KEY:
        perms = settings.get("permissions")
        if perms is None:
            perms = settings["permissions"] = {}
        if not isinstance(perms, dict):
            raise ApplyError("settings.json 'permissions' is not an object; refusing to edit it")
        perms["defaultMode"] = value
    else:
        settings[key] = value


def _del_key(settings, key, created_parent):
    if key == DEFAULT_MODE_KEY:
        perms = settings.get("permissions")
        if isinstance(perms, dict):
            perms.pop("defaultMode", None)
            if created_parent and not perms:
                del settings["permissions"]
    else:
        settings.pop(key, None)


def apply_mode(settings, mode, skip_prompt, manifest_items, path=SETTINGS_REL):
    """(new settings, setting items, warnings). manifest_items: the manifest's current kind=setting items."""
    new = copy.deepcopy(settings)
    owned = {}
    for i in manifest_items or []:
        if i.get("kind", "setting") == "setting":
            owned[i["key"]] = dict(i)
    warnings = []
    if mode == "bypass":
        wants = [(DEFAULT_MODE_KEY, "bypassPermissions")]
        if skip_prompt:
            wants.append((SKIP_KEY, True))
        for key, want in wants:
            present, cur = _get_key(new, key)
            if present and cur == want:
                continue
            owned[key] = {"kind": "setting", "path": path, "key": key, "old": {"present": present, "value": cur},
                          "new": want, "created_parent": key == DEFAULT_MODE_KEY and "permissions" not in new}
            _set_key(new, key, want)
    elif mode == "safe":
        for key in list(owned):
            item = owned.pop(key)
            present, cur = _get_key(new, key)
            if present and cur == item["new"]:
                old = item["old"]
                if old["present"] and (old["value"] == "bypassPermissions" or old["value"] is True):
                    warnings.append("%s: refusing to restore a bypass value from the manifest; left as it is" % key)
                elif old["present"]:
                    _set_key(new, key, old["value"])
                else:
                    _del_key(new, key, item.get("created_parent", False))
            else:
                warnings.append("%s was changed after onboarding set it; left as it is now" % key)
        present, cur = _get_key(new, DEFAULT_MODE_KEY)
        if present and cur == "bypassPermissions":
            warnings.append("permissions.defaultMode is bypassPermissions but onboarding did not set it; left alone")
    else:
        raise ApplyError("unknown mode %r (use safe or bypass)" % (mode,))
    return new, list(owned.values()), warnings


# ---------------------------------------------------------------- self-test

def _shell_argv(command, platform):
    if platform.startswith("win"):
        return None
    return ["/bin/sh", "-c", command]


def _default_runner(command, stdin_text, env, timeout, platform=None):
    platform = platform or sys.platform
    argv = _shell_argv(command, platform)
    try:
        if argv is None:
            r = subprocess.run(command, shell=True, input=stdin_text, capture_output=True, text=True, env=env,
                               timeout=timeout)
        else:
            r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout or "", r.stderr or ""


_JEV_PATH_VARS = ("JEV_HOME", "JEV_CONFIG", "JEV_AGENTS_DIR", "JEV_ROUTER_CONDITIONS", "JEV_DISPATCH_FAKE")


def _hook_env(settings_env, tmp):
    """Probe environment: inherited env plus the settings' JEV_* switches, with every path or log override
    scrubbed and JEV_HOME/HOME/USERPROFILE pointing at the temp dir so probes never write the real ~/.claude/jev."""
    env = dict(os.environ)
    for k, v in (settings_env or {}).items():
        if str(k).startswith("JEV_") and isinstance(v, str):
            env[k] = v
    for k in _JEV_PATH_VARS:
        env.pop(k, None)
    env["JEV_HOME"] = tmp
    env["HOME"] = tmp
    env["USERPROFILE"] = tmp
    return env


def _decision(stdout):
    s = stdout.strip()
    if not s:
        return "", None
    try:
        obj = json.loads(s)
    except ValueError:
        return None, "output is not JSON"
    if not isinstance(obj, dict):
        return None, "output is not a JSON object"
    return (obj.get("hookSpecificOutput") or {}).get("permissionDecision", ""), None


def selftest_gate(command, claude_mode, *, settings_env=None, platform=None, runner=None, timeout=20):
    """Run the exact registered gate command with three probes. Never touches the real home."""
    platform = platform or sys.platform
    runner = runner or (lambda c, i, e, t: _default_runner(c, i, e, t, platform))
    res = {"ok": False, "shell": "cmd" if platform.startswith("win") else "sh", "claude_mode": claude_mode,
           "probes": [], "error": None}
    tmp = tempfile.mkdtemp(prefix="jev-selftest-")
    try:
        env = _hook_env(settings_env, tmp)
        probes = [("canary: download piped to a shell", CANARY, "deny"),
                  ("read-only command", "git status", ""),
                  ("git reset --hard", "git reset --hard HEAD", "" if claude_mode == "bypassPermissions" else "ask")]
        all_ok = True
        for name, cmd, expected in probes:
            payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd},
                                  "cwd": tmp, "permission_mode": claude_mode, "session_id": "jev-onboard-selftest"})
            code, out, err = runner(command, payload, env, timeout)
            got, perr = _decision(out)
            ok = code == 0 and perr is None and got == expected
            probe = {"name": name, "command": cmd, "expected": expected or "(no output)",
                     "got": ("(no output)" if got == "" else got) if perr is None else perr, "ok": ok}
            if code != 0:
                probe["got"] = "exit code %s" % code
            res["probes"].append(probe)
            all_ok = all_ok and ok
        res["ok"] = all_ok
        if not all_ok:
            bad = [p for p in res["probes"] if not p["ok"]][0]
            res["error"] = "gate probe '%s' expected %s, got %s" % (bad["name"], bad["expected"], bad["got"])
    except Exception as e:
        res["error"] = "self-test failed to run: %s" % type(e).__name__
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def smoke_hook(command, *, settings_env=None, platform=None, runner=None, timeout=20):
    platform = platform or sys.platform
    runner = runner or (lambda c, i, e, t: _default_runner(c, i, e, t, platform))
    tmp = tempfile.mkdtemp(prefix="jev-smoke-")
    try:
        code, out, err = runner(command, "{}", _hook_env(settings_env, tmp), timeout)
        ok = code == 0
        return {"ok": ok, "command": command, "error": None if ok else "exit code %s" % code}
    except Exception as e:
        return {"ok": False, "command": command, "error": "failed to run: %s" % type(e).__name__}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- planning

def _config_text(active_config):
    if isinstance(active_config, str):
        return _nl(active_config)
    return json.dumps(active_config, indent=2, ensure_ascii=False) + "\n"


def _read_text(path):
    """Text of an existing regular file, None when it is absent. Raises ApplyError when it exists but cannot be
    read or decoded, or is a link: an unreadable file is never treated as absent (it would be overwritten)."""
    p = str(path)
    if not lexists(p):
        return None
    if is_link(p) or os.path.islink(p):
        raise ApplyError("%s is a symlink or link; onboarding will not replace it" % norm(p))
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ApplyError("%s exists but cannot be read as UTF-8 text (%s); nothing written" % (norm(p), type(e).__name__))


def _read_text_soft(path):
    """Like _read_text but None for anything unreadable (comparison only; callers must not treat None as absent)."""
    try:
        return _read_text(path)
    except ApplyError:
        return None


def _wanted_hooks(components, python, home, platform):
    wanted = {}
    for comp in ("gate", "dispatch", "router"):
        if comp in components:
            h = dict(HOOKS[comp])
            if comp == "gate" and platform.startswith("win"):
                h["matcher"] = GATE_MATCHER_WIN
            wanted[h["script"]] = {"event": h["event"], "matcher": h["matcher"], "timeout": h["timeout"],
                                   "command": hook_command(python, home, h["script"])}
    return wanted


def _rules_text(text, block, nl):
    """New CLAUDE.md text with the block added or replaced (keeps the file's newline style)."""
    block = _nl(block).strip("\n").replace("\n", nl)
    if RULES_RE.search(text):
        return RULES_RE.sub(lambda m: block, text, count=1)
    if not text.strip():
        return block + nl
    return text.rstrip() + nl + nl + block + nl


def _rules_removed(text, nl):
    out = re.sub(r"(?:\r?\n){0,2}" + RULES_RE.pattern, "", text, count=1, flags=re.S)
    out = out.lstrip("\r\n") if not out.strip() else out
    if out.strip() and not out.endswith("\n"):
        out += nl
    return out


def plan_install(home, repo, *, components, agents, active_config, mode, skip_bypass_prompt, python, rules_block,
                 choice, platform=None, key_found=False, key=None, confirmed_bypass=False):
    """(actions, warnings). Pure: reads the home, writes nothing.
    confirmed_bypass must be True (the caller has taken both confirmations) for mode="bypass" or skip_bypass_prompt."""
    home, repo = Path(home), Path(repo)
    platform = platform or sys.platform
    comps = list(components)
    for c in comps:
        if c not in COMPONENTS:
            raise ApplyError("unknown component %r (valid: %s)" % (c, ", ".join(COMPONENTS)))
    if mode not in ("safe", "bypass"):
        raise ApplyError("unknown mode %r (use safe or bypass)" % (mode,))
    if mode == "bypass" and "gate" not in comps:
        raise ApplyError("bypass mode needs the permission gate component; refusing to plan it without")
    if skip_bypass_prompt and mode != "bypass":
        raise ApplyError("--skip-bypass-prompt is only valid in bypass mode")
    if (mode == "bypass" or skip_bypass_prompt) and not confirmed_bypass:
        raise ApplyError("bypass mode and --skip-bypass-prompt need the explicit confirmations (confirmed_bypass)")
    manifest = load_manifest_checked(home, repo)
    actions, warnings = [], []
    if manifest:
        warnings.extend(manifest.pop("_warnings", []))
    if key is not None:
        if not isinstance(key, str) or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ApplyError("the key contains a newline or control character; paste only the key")
        if any(ord(c) > 126 for c in key):
            warnings.append("the key contains non-ASCII characters; it is written as UTF-8 exactly as entered (check it)")

    # skill link
    skill_link = home / SKILL_REL
    skill_target = norm(repo / "skill" / "jev-orchestrator")
    if "skill" in comps:
        cur_is_link = is_link(skill_link)
        if cur_is_link and same_path(link_target(skill_link), skill_target) and os.path.isdir(str(skill_link)):
            pass
        else:
            prev = {"type": "none", "target": None, "backup": None}
            if cur_is_link:
                prev = {"type": "link", "target": link_target(skill_link), "backup": None}
                what = "re-point the skill link (was -> %s)" % prev["target"]
            elif lexists(skill_link):
                prev["type"] = "dir"
                what = "move the existing skill folder to the backup dir, then link the skill"
            else:
                what = "link the skill"
            actions.append(Action("link_skill", norm(skill_link), "%s: %s -> %s" % (what, SKILL_REL, skill_target),
                                  [], "remove the link, restore the previous one" if prev["type"] != "none" else "remove the link",
                                  {"target": skill_target, "previous": prev}))

    # agents
    if "agents" in comps:
        for name in sorted(agents):
            path = home / AGENTS_REL / name
            text = _nl(agents[name])
            data = text.encode("utf-8")
            item = _find_item(manifest, "file", _rel(home, path))
            if is_link(path) or os.path.islink(str(path)):
                raise ApplyError("%s is a symlink or link; onboarding will not replace it "
                                 "(remove it yourself, or run --uninstall to clear legacy links, then retry)" % norm(path))
            cur = _read_text(path)
            if cur is not None and _nl(cur) == text:
                continue
            note = []
            if lexists(path):
                if item is None or _file_sha(path) != item.get("sha256"):
                    note.append("replaces a copy you edited (backup kept)")
            actions.append(Action("write_agent", norm(path), "write %s/%s" % (AGENTS_REL, name), note,
                                  "restore the backup or delete the file", {"text": text, "sha256": sha_bytes(data)}))

    # active config
    if active_config is not None:
        path = home / CONFIG_REL
        text = _config_text(active_config)
        cur = _read_text(path)
        if cur is None or _nl(cur) != text:
            note = []
            item = _find_item(manifest, "file", _rel(home, path))
            if cur is not None and (item is None or _file_sha(path) != item.get("sha256")):
                note.append("replaces a routing config you edited (backup kept)")
            actions.append(Action("write_config", norm(path), "write the routing config %s" % CONFIG_REL, note,
                                  "restore the backup or delete the file", {"text": text}))

    # conditions seed
    cond = home / CONDITIONS_REL
    if "router" in comps and not lexists(cond):
        actions.append(Action("seed_conditions", norm(cond), "create %s as [] (your own rules; never removed)" % CONDITIONS_REL,
                              [], "left in place", {"text": "[]\n"}))

    # key file
    keyfile = home / KEYFILE_REL
    if "keyfile" in comps and not lexists(keyfile) and not key_found:
        actions.append(Action("create_keyfile", norm(keyfile),
                              "create %s (%s)" % (KEYFILE_REL, "with the key you entered" if key else "empty template, fill in later"),
                              [], "left in place (holds your key)", {"key": key}))

    # rules block
    claude_md = home / CLAUDE_MD_REL
    cur_md = _read_text(claude_md)  # raises ApplyError when it exists but cannot be decoded or is a link
    rules_item = _find_item(manifest, "rules_block", _rel(home, claude_md))
    if "rules" in comps:
        if rules_block is None:
            raise ApplyError("the rules component needs the rules block text")
        base = cur_md or ""
        nl = "\r\n" if "\r\n" in base else "\n"
        new_md = _rules_text(base, rules_block, nl)
        if cur_md is None or new_md != cur_md:
            verb = "update" if cur_md and RULES_RE.search(cur_md) else "add"
            actions.append(Action("rules", norm(claude_md), "%s the jev rules block in %s" % (verb, CLAUDE_MD_REL), [],
                                  "restore the backup or remove the block",
                                  {"mode": verb, "block": _nl(rules_block).strip("\n"), "sha_before": _file_sha(claude_md)}))
    elif rules_item and cur_md and RULES_RE.search(cur_md):
        actions.append(Action("rules", norm(claude_md), "remove the jev rules block from %s" % CLAUDE_MD_REL, [],
                              "restore the backup", {"mode": "remove", "block": None, "sha_before": _file_sha(claude_md)}))

    # settings.json: hook merge + mode keys in one action
    spath = home / SETTINGS_REL
    data, ssha = read_json(spath)
    wanted = _wanted_hooks(comps, python, home, platform)
    new, notes = merge_hooks(data, wanted)
    new, items, mwarn = apply_mode(new, mode, skip_bypass_prompt, _items(manifest, "setting"))
    if "gate" not in comps and any(
            isinstance(h, dict) and is_ours(h.get("command")) == "permission_gate.py"
            for lst in (data.get("hooks") or {}).values() if isinstance(lst, list)
            for g in lst for h in (_group_hooks(g) or [])):
        warnings.append("the gate component is not selected: the existing jev permission gate hook will be REMOVED "
                        "from settings.json (tool calls will no longer be checked)")
    if new != data:
        details = []
        for script, w in wanted.items():
            details.append("hook %s %s%s: %s" % (w["event"], script,
                           " (matcher %s)" % w["matcher"] if w["matcher"] else "",
                           "unchanged" if _is_exact(data, {script: w}) else "registered with the exact command"))
        for n in notes:
            if "older" in n:
                details.append(n)
        for key in (DEFAULT_MODE_KEY, SKIP_KEY):
            op, np_ = _get_key(data, key), _get_key(new, key)
            if op != np_:
                fmt = lambda t: json.dumps(t[1]) if t[0] else "(absent)"
                details.append("%s: %s -> %s" % (key, fmt(op), fmt(np_)))
        actions.append(Action("settings", norm(spath), "edit %s (backup first, atomic write, only our hooks and mode keys)" % SETTINGS_REL,
                              details, "restore the backup byte for byte, or run --uninstall",
                              {"sha": ssha, "wanted": wanted, "mode": mode, "skip": bool(skip_bypass_prompt),
                               "python": norm(python), "components": comps}))
    warnings.extend(mwarn)
    return actions, warnings


def format_plan(actions, warnings, *, dry_run):
    lines = []
    if not actions:
        lines.append("Nothing to change.")
    for n, a in enumerate(actions, 1):
        lines.append("%d. %s" % (n, a.summary))
        for d in a.details:
            lines.append("     - %s" % d)
        if a.undo:
            lines.append("     undo: %s" % a.undo)
    for w in warnings:
        lines.append("WARNING: %s" % w)
    lines.append("Backups go to ~/.claude/backups/jev-onboard-<timestamp>/ (created only when a file is replaced).")
    lines.append("Undo: python scripts/onboard.py --uninstall")
    lines.append("DRY RUN: nothing was changed." if dry_run else "Nothing has changed yet.")
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------- apply

def _setting_env(data):
    env = data.get("env") if isinstance(data, dict) else None
    return {k: v for k, v in (env or {}).items() if str(k).startswith("JEV_") and isinstance(v, str)}


def _clear_empty_dir(p):
    """CreateJunction can leave an empty real directory behind when it fails part-way; remove only that."""
    try:
        if lexists(p) and not is_link(p) and os.path.isdir(str(p)) and not os.listdir(str(p)):
            os.rmdir(str(p))
    except OSError:
        pass


def _do_link_skill(ctx, a):
    link, target, prev = Path(a.path), a.payload["target"], a.payload["previous"]
    if not os.path.isdir(target):
        raise ApplyError("the skill folder %s does not exist" % target)
    rel = _rel(ctx.home, link)
    old = _find_item(ctx.manifest, "link", rel)
    previous = dict(prev)
    moved_backup = None
    if prev["type"] == "link":
        if old and old.get("target") and same_path(old["target"], prev["target"]):
            previous = old["previous"]
        remove_link(link)
    elif prev["type"] == "dir":
        moved_backup = ctx.move_to_backup(link)
        previous["backup"] = moved_backup
    try:
        make_link(target, link)
        if not (is_link(link) and os.path.isdir(str(link / "hooks"))):
            raise ApplyError("the skill link was created but %s/hooks is not reachable" % norm(link))
    except BaseException as e:
        # Put the previous link or folder back BEFORE failing: existing hooks may point through this path.
        problems = []
        try:
            if is_link(link):
                remove_link(link)
            else:
                _clear_empty_dir(link)
            if prev["type"] == "link" and prev.get("target"):
                make_link(prev["target"], link)
            elif prev["type"] == "dir" and moved_backup:
                os.rename(str(ctx.home / moved_backup), str(link))
        except Exception as e2:
            problems.append("could not restore the previous skill path (%s); it is in %s" % (
                type(e2).__name__, moved_backup or prev.get("target")))
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        msg = str(e) if isinstance(e, ApplyError) else "linking the skill failed (%s)" % type(e).__name__
        raise ApplyError(msg + ("; previous skill path restored" if not problems else "; " + "; ".join(problems)))
    _upsert(ctx.manifest, "link", {"kind": "link", "path": rel, "target": target, "previous": previous})


def _record_file(ctx, a, kind, data, mode=0o644):
    path = Path(a.path)
    rel = _rel(ctx.home, path)
    old = _find_item(ctx.manifest, kind, rel)
    cur_sha = _file_sha(path) if not os.path.islink(str(path)) else None
    previous = {"type": "none", "target": None}
    backup = None
    if os.path.islink(str(path)):
        previous = {"type": "link", "target": os.readlink(str(path))}
        os.unlink(str(path))
    elif lexists(path):
        if cur_sha != sha_bytes(data):
            backup = ctx.backup_file(path)
        previous = {"type": "file", "target": None}
    sha = write_bytes_atomic(path, data, mode=mode)
    item = {"kind": kind, "path": rel, "sha256": sha, "created": not lexists_before(previous), "backup": backup,
            "previous": previous}
    if old and cur_sha == old.get("sha256"):
        item["created"], item["backup"], item["previous"] = old.get("created", False), old.get("backup"), old.get("previous") or previous
    _upsert(ctx.manifest, "file", dict(item, kind="file"))


def lexists_before(previous):
    return previous["type"] != "none"


def _do_write_agent(ctx, a):
    _record_file(ctx, a, "file", a.payload["text"].encode("utf-8"))


def _do_write_config(ctx, a):
    _record_file(ctx, a, "file", a.payload["text"].encode("utf-8"))


def _do_seed(ctx, a):
    sha = write_bytes_atomic(a.path, a.payload["text"].encode("utf-8"), mode=0o644)
    _upsert(ctx.manifest, "seed", {"kind": "seed", "path": _rel(ctx.home, a.path), "sha256": sha})


def _do_keyfile(ctx, a):
    key = a.payload.get("key")
    text = KEYFILE_TEMPLATE % (key or "")
    path = Path(a.path)
    if lexists(path):
        return
    write_bytes_atomic(path, text.encode("utf-8"), mode=0o600)
    _upsert(ctx.manifest, "seed", {"kind": "seed", "path": _rel(ctx.home, path), "created": True})


def _do_rules(ctx, a):
    path = Path(a.path)
    rel = _rel(ctx.home, path)
    cur = _read_text(path)
    if _file_sha(path) != a.payload["sha_before"]:
        raise ApplyError("%s changed while onboarding ran; nothing written" % CLAUDE_MD_REL)
    old = _find_item(ctx.manifest, "rules_block", rel)
    cur_sha = _file_sha(path)
    nl = "\r\n" if cur and "\r\n" in cur else "\n"
    if a.payload["mode"] == "remove":
        new = _rules_removed(cur, nl)
    else:
        new = _rules_text(cur or "", a.payload["block"], nl)
    backup = ctx.backup_file(path) if lexists(path) else None
    sha_after = write_bytes_atomic(path, new.encode("utf-8"), mode=0o644)
    if a.payload["mode"] == "remove":
        ctx.manifest["items"] = [i for i in ctx.manifest["items"] if not (i.get("kind") == "rules_block" and i.get("path") == rel)]
        return
    item = {"kind": "rules_block", "path": rel, "sha256": sha_text(a.payload["block"]), "created_file": cur is None,
            "original_backup": backup, "sha_after": sha_after}
    if old and cur_sha == old.get("sha_after"):
        item["created_file"], item["original_backup"] = old["created_file"], old.get("original_backup")
    elif old:
        item["original_backup"] = None if cur is not None else item["original_backup"]
    _upsert(ctx.manifest, "rules_block", item)


def _registered_gate(data):
    for lst in (data.get("hooks") or {}).values():
        for g in lst if isinstance(lst, list) else []:
            for h in (_group_hooks(g) or []):
                if isinstance(h, dict) and is_ours(h.get("command")) == "permission_gate.py":
                    return h.get("command")
    return None


def _do_settings(ctx, a, mode, selftest, smoke, platform, results):
    p = a.payload
    spath = Path(a.path)
    rel = _rel(ctx.home, spath)
    data, sha0 = read_json(spath)
    if sha0 != p["sha"]:
        raise ApplyError("%s changed while onboarding ran; nothing written" % SETTINGS_REL)
    hook_dir = ctx.home / SKILL_REL / "hooks"
    wanted = p["wanted"]
    for script in wanted:
        if not os.path.isfile(str(hook_dir / script)):
            raise ApplyError("hook script %s does not exist under the skill link; settings.json untouched" % norm(hook_dir / script))
    senv = _setting_env(data)
    if "permission_gate.py" in wanted:
        cmd = wanted["permission_gate.py"]["command"]
        modes = ["default"] + (["bypassPermissions"] if mode == "bypass" else [])
        for m in modes:
            r = selftest(cmd, m, **({"settings_env": senv} if selftest is selftest_gate else {}))
            results[m] = r
            if not r.get("ok"):
                raise ApplyError("gate self-test failed in %s mode (%s); settings.json untouched" % (m, r.get("error") or "no detail"))
    elif mode == "bypass":
        raise ApplyError("bypass mode needs the permission gate; settings.json untouched")
    for script in ("dispatch_router.py", "prompt_router.py"):
        if script in wanted:
            r = smoke(wanted[script]["command"], **({"settings_env": senv} if smoke is smoke_hook else {}))
            if not r.get("ok"):
                raise ApplyError("%s failed its smoke run (%s); settings.json untouched" % (script, r.get("error")))

    old_sf = _find_item(ctx.manifest, "settings_file", rel)
    new, notes = merge_hooks(data, wanted)
    new, items, _w = apply_mode(new, mode, p["skip"], _items(ctx.manifest, "setting"))
    existed = sha0 is not None
    backup = ctx.backup_file(spath) if existed else None
    sha_after = write_json_atomic(spath, new, sha0)

    def revert_file():
        """Undo our write. Returns a short status text; never raises."""
        try:
            if _file_sha(spath) != sha_after:
                return "settings.json changed again after our write, so it was NOT restored (backup: %s)" % (backup or "none")
            if existed:
                write_bytes_atomic(spath, (ctx.home / backup).read_bytes())
                return "settings.json restored from backup"
            os.unlink(str(spath))
            return "settings.json removed (it did not exist before)"
        except Exception as e2:
            return "COULD NOT restore settings.json (%s); restore it by hand from %s" % (type(e2).__name__, backup or "your own copy")

    if mode == "bypass":
        try:
            again, _s = read_json(spath)
            reg = _registered_gate(again)
            _g, dm = _get_key(again, DEFAULT_MODE_KEY)
            if not reg:
                raise ApplyError("no gate hook found after writing")
            r = selftest(reg, "bypassPermissions", **({"settings_env": senv} if selftest is selftest_gate else {}))
            results["bypassPermissions-registered"] = r
            if not r.get("ok"):
                raise ApplyError(r.get("error") or "gate self-test failed")
        except ApplyError as e:
            raise ApplyError("bypass mode was NOT enabled: %s (%s)" % (e, revert_file()))
        except BaseException as e:
            status = revert_file()
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            raise ApplyError("bypass mode was NOT enabled: %s (%s)" % (type(e).__name__, status))

    sf = {"kind": "settings_file", "path": rel, "created": not existed, "original_backup": backup, "sha_after": sha_after,
          "had_hooks_key": "hooks" in data, "had_permissions_key": "permissions" in data}
    if old_sf:
        if sha0 == old_sf.get("sha_after"):
            sf.update(created=old_sf["created"], original_backup=old_sf.get("original_backup"))
        else:
            sf.update(created=old_sf["created"], original_backup=None)
        sf["had_hooks_key"], sf["had_permissions_key"] = old_sf["had_hooks_key"], old_sf["had_permissions_key"]
    _upsert(ctx.manifest, "settings_file", sf)
    _set_items(ctx.manifest, "hook", [dict(kind="hook", event=w["event"], matcher=w["matcher"], command=w["command"],
                                           timeout=w["timeout"], script=s) for s, w in wanted.items()])
    _set_items(ctx.manifest, "setting", items)
    ctx.manifest.setdefault("choice", {})["python"] = p["python"]


def apply(actions, home, repo, *, mode, choice, now, selftest=selftest_gate, smoke=smoke_hook, platform=None,
          confirmed_bypass=False):
    """Apply planned actions in order. Raises ApplyError; completed actions stay in the manifest.
    mode="bypass" (or a planned skip-prompt key) needs confirmed_bypass=True, else nothing is touched."""
    platform = platform or sys.platform
    result = {"ok": True, "backup_dir": None, "applied": [], "selftest": {}, "warnings": []}
    if not actions:
        return result
    if (mode == "bypass" or any(a.payload.get("skip") for a in actions)) and not confirmed_bypass:
        raise ApplyError("bypass mode and --skip-bypass-prompt need the explicit confirmations (confirmed_bypass); nothing written")
    ctx = Ctx(home, repo, now, choice)
    handlers = {"link_skill": _do_link_skill, "write_agent": _do_write_agent, "write_config": _do_write_config,
                "seed_conditions": _do_seed, "create_keyfile": _do_keyfile, "rules": _do_rules}
    try:
        for a in actions:
            if a.kind == "settings":
                _do_settings(ctx, a, mode, selftest, smoke, platform, result["selftest"])
            elif a.kind in handlers:
                handlers[a.kind](ctx, a)
            else:
                raise ApplyError("unknown action kind %r" % a.kind)
            result["applied"].append(a.kind)
            ctx.save()
    except BaseException:
        if ctx.backup_dir is not None or ctx.manifest["items"]:
            try:
                ctx.save()
            except Exception:
                pass
        raise
    finally:
        result["backup_dir"] = norm(ctx.backup_dir) if ctx.backup_dir else None
    return result


# ---------------------------------------------------------------- uninstall

def claude_running(platform=None, run=None):
    platform = platform or sys.platform

    def default_run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout

    run = run or default_run
    try:
        if platform.startswith("win"):
            out = run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"])
            return "claude.exe" in (out or "").lower()
        out = run(["ps", "-A", "-o", "comm="])
        return any(l.strip().endswith("claude") for l in (out or "").splitlines())
    except Exception:
        return False


def _un_settings(home, manifest):
    """(op, new data|None, notes) with op in None | restore | delete | edit."""
    spath = home / SETTINGS_REL
    if not spath.exists():
        return None, None, []
    rel = _rel(home, spath)
    sf = _find_item(manifest, "settings_file", rel)
    cur = _file_sha(spath)
    if sf and cur == sf.get("sha_after"):
        ob = sf.get("original_backup")
        if ob and (home / ob).is_file():
            return "restore", None, ["restore settings.json byte for byte from %s" % ob]
        if sf.get("created"):
            return "delete", None, ["delete settings.json (onboarding created it; a copy goes to the backup dir)"]
    data, _ = read_json(spath)
    new, removed = _strip_ours(data)
    new, _items_left, warns = apply_mode(new, "safe", False, _items(manifest, "setting"))
    if sf:
        if not sf.get("had_hooks_key") and new.get("hooks") == {}:
            del new["hooks"]
        if not sf.get("had_permissions_key") and new.get("permissions") == {}:
            del new["permissions"]
    if new == data:
        return None, None, warns
    notes = []
    if removed:
        notes.append("remove %d jev hook entr%s" % (removed, "y" if removed == 1 else "ies"))
    for key in (DEFAULT_MODE_KEY, SKIP_KEY):
        if _get_key(data, key) != _get_key(new, key):
            notes.append("restore %s" % key)
    return "edit", new, notes + ["WARNING: " + w for w in warns]


def _un_rules(home, manifest):
    """(op, new text|None, notes) op in None | restore | delete | remove | left."""
    path = home / CLAUDE_MD_REL
    rel = _rel(home, path)
    item = _find_item(manifest, "rules_block", rel)
    if not item or not path.exists():
        return None, None, []
    if _file_sha(path) == item.get("sha_after"):
        ob = item.get("original_backup")
        if ob and (home / ob).is_file():
            return "restore", None, []
        if item.get("created_file"):
            return "delete", None, []
    text = _read_text(path) or ""
    m = RULES_RE.search(text)
    if not m:
        return None, None, []
    if sha_text(m.group(0)) == item.get("sha256"):
        nl = "\r\n" if "\r\n" in text else "\n"
        return "remove", _rules_removed(text, nl), []
    return "left", None, ["the jev rules block in CLAUDE.md was edited; left in place"]


def plan_uninstall(home, repo, *, force=False, running=claude_running):
    """(actions, warnings)."""
    home, repo = Path(home), Path(repo)
    manifest = load_manifest_checked(home, repo)
    actions, warnings = [], []
    m = manifest or {}
    if manifest:
        warnings.extend(manifest.pop("_warnings", []))

    op, new, notes = _un_settings(home, m)
    if op:
        actions.append(Action("un_settings", norm(home / SETTINGS_REL), "settings.json: " + "; ".join(n for n in notes if not n.startswith("WARNING")),
                              [n for n in notes if n.startswith("WARNING")], "", {"op": op}))
    elif notes:
        warnings.extend(n.replace("WARNING: ", "") for n in notes)

    op, new, notes = _un_rules(home, m)
    if op == "left":
        warnings.extend(notes)
    elif op:
        actions.append(Action("un_rules", norm(home / CLAUDE_MD_REL), "CLAUDE.md: %s the jev rules block" % op, [], "", {"op": op}))

    if manifest:
        for item in _items(manifest, "file"):
            path = home / item["path"]
            if not lexists(path):
                continue
            if _file_sha(path) == item.get("sha256"):
                actions.append(Action("un_file", norm(path), "%s: restore the previous state or remove our copy" % item["path"], [], "",
                                      {"item": item}))
            else:
                warnings.append("%s was edited after onboarding wrote it; left in place" % item["path"])
    else:
        agent_dir = home / AGENTS_REL
        for tpl in sorted((repo / "agents").glob("jev-*.md")):
            path = agent_dir / tpl.name
            if not lexists(path):
                continue
            if os.path.islink(str(path)):
                if same_path(os.path.realpath(str(path)), tpl):
                    actions.append(Action("un_legacy_agent", norm(path), "%s/%s: remove the link into this repo" % (AGENTS_REL, tpl.name),
                                          [], "", {"op": "unlink"}))
                else:
                    warnings.append("%s links elsewhere; left in place" % path.name)
                continue
            cur, ref = _read_text(path), _read_text(tpl)
            if cur is not None and ref is not None and _nl(cur) == _nl(ref):
                actions.append(Action("un_legacy_agent", norm(path), "%s/%s: remove the copy of this repo's agent" % (AGENTS_REL, tpl.name),
                                      [], "", {"op": "delete"}))
            else:
                actions.append(Action("un_legacy_agent", norm(path), "%s/%s: not a plain copy; move it to the backup dir" % (AGENTS_REL, tpl.name),
                                      [], "", {"op": "move"}))

    link = home / SKILL_REL
    keep_link = False
    if is_link(link):
        if same_path(link_target(link), repo / "skill" / "jev-orchestrator"):
            if not force and running():
                keep_link = True
                warnings.append("Claude Code is running: the skill link was kept; restart Claude Code, then run --uninstall again")
            else:
                actions.append(Action("un_link", norm(link), "remove the skill link %s" % SKILL_REL, [], "",
                                      {"item": _find_item(manifest, "link", _rel(home, link)) if manifest else None}))
        else:
            warnings.append("the skill link points to another checkout; left in place")
    if manifest and not keep_link:
        actions.append(Action("un_manifest", norm(home / MANIFEST_REL), "remove the install manifest (a copy goes to the backup dir)", [], "", {}))
    warnings.append("left in place: %s, %s, jev logs under ~/.claude/jev, ~/.claude/backups, and the key file %s"
                    % (CONDITIONS_REL, CONFIG_REL if not manifest else "(seeds)", KEYFILE_REL))
    return actions, warnings


def _restore_bytes_from(home, rel_backup, path):
    write_bytes_atomic(path, (home / rel_backup).read_bytes(), mode=0o644)


def apply_uninstall(actions, home, repo, *, now):
    home, repo = Path(home), Path(repo)
    ctx = Ctx(home, repo, now)
    manifest = load_manifest_checked(home, repo) or {}
    result = {"ok": True, "backup_dir": None, "applied": [], "warnings": list(manifest.pop("_warnings", []))}
    removed_files = []
    try:
        for a in actions:
            path = Path(a.path)
            if a.kind == "un_settings":
                op, new, notes = _un_settings(home, manifest)
                if op is None:
                    continue
                if op == "restore":
                    sf = _find_item(manifest, "settings_file", _rel(home, path))
                    ctx.backup_file(path)
                    _restore_bytes_from(home, sf["original_backup"], path)
                elif op == "delete":
                    ctx.backup_file(path)
                    os.unlink(str(path))
                else:
                    sha = _file_sha(path)
                    ctx.backup_file(path)
                    write_json_atomic(path, new, sha)
            elif a.kind == "un_rules":
                op, new, notes = _un_rules(home, manifest)
                item = _find_item(manifest, "rules_block", _rel(home, path))
                if op == "restore":
                    ctx.backup_file(path)
                    _restore_bytes_from(home, item["original_backup"], path)
                elif op == "delete":
                    ctx.backup_file(path)
                    os.unlink(str(path))
                elif op == "remove":
                    ctx.backup_file(path)
                    write_bytes_atomic(path, new.encode("utf-8"))
            elif a.kind == "un_file":
                item = a.payload["item"]
                if _file_sha(path) != item.get("sha256"):
                    result["warnings"].append("%s changed since planning; left in place" % item["path"])
                    continue
                prev = item.get("previous") or {"type": "none"}
                ctx.backup_file(path)
                if item.get("backup") and (home / item["backup"]).is_file():
                    _restore_bytes_from(home, item["backup"], path)
                elif prev.get("type") == "link" and prev.get("target"):
                    os.unlink(str(path))
                    try:
                        os.symlink(prev["target"], str(path))
                    except OSError:
                        result["warnings"].append("could not recreate the old link at %s" % item["path"])
                else:
                    os.unlink(str(path))
            elif a.kind == "un_legacy_agent":
                op = a.payload["op"]
                if op == "unlink":
                    os.unlink(str(path))
                elif op == "delete":
                    ctx.backup_file(path)
                    os.unlink(str(path))
                else:
                    ctx.move_to_backup(path)
            elif a.kind == "un_link":
                remove_link(path)
                item = a.payload.get("item")
                prev = (item or {}).get("previous") or {"type": "none"}
                if prev.get("type") == "link" and prev.get("target"):
                    try:
                        make_link(prev["target"], path)
                    except (ApplyError, OSError):
                        _clear_empty_dir(path)
                        result["warnings"].append("could not recreate the previous skill link")
                elif prev.get("type") == "dir" and prev.get("backup") and (home / prev["backup"]).exists():
                    os.rename(str(home / prev["backup"]), str(path))
            elif a.kind == "un_manifest":
                ctx.backup_file(path)
                os.unlink(str(path))
            else:
                raise ApplyError("unknown action kind %r" % a.kind)
            result["applied"].append(a.kind)
            if a.kind == "un_settings":
                drop = lambda i: i.get("kind") in ("settings_file", "hook", "setting")
            elif a.kind == "un_rules":
                drop = lambda i: i.get("kind") == "rules_block"
            elif a.kind == "un_file":
                drop = lambda i, p=_rel(home, path): i.get("kind") == "file" and i.get("path") == p
            else:
                drop = None
            if drop and manifest.get("items") is not None:
                manifest["items"] = [i for i in manifest["items"] if not drop(i)]
                if not any(x.kind == "un_manifest" for x in actions):
                    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
                    write_bytes_atomic(home / MANIFEST_REL, text.encode("utf-8"))
    finally:
        result["backup_dir"] = norm(ctx.backup_dir) if ctx.backup_dir else None
    return result
