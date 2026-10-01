#!/usr/bin/env python3
"""Model presets, per-role overrides, guardrails and agent rendering for the onboarding installer.

Presets change only models, agents.*.model, agents.*.effort and the guardrail role lists.
Python 3.9+, standard library only. Pure functions: nothing here writes outside a directory you pass in.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

EFFORTS = ("low", "medium", "high", "max")
ALIASES = ("opus", "sonnet", "haiku", "fable", "inherit", "default", "best", "opusplan")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")
OPUS_ROLES = ("jev-architect", "jev-debugger")
MAX_ROLES = ("jev-architect",)
PRESET_ORDER = ("balanced", "economy", "max-quality", "zai-glm", "legacy-all-opus")
CUSTOM_NAME = "custom"
CUSTOM_SUMMARY = "Start from balanced and change roles with --set, --model-id, --allow-opus and --allow-max."
COPIED_KEYS = ("write", "role")


class PresetError(ValueError):
    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        ValueError.__init__(self, "; ".join(self.errors))


def _repo(repo):
    return Path(repo) if repo else REPO


# ---------- imports of sibling modules ----------

def _jev(repo=None):
    """Import jev with JEV_CONFIG pinned to the shipped config only during the import, then restore os.environ."""
    mod = sys.modules.get("jev")
    if mod is not None:
        return mod
    r = _repo(repo)
    scripts = str(r / "skill" / "jev-orchestrator" / "scripts")
    old = os.environ.get("JEV_CONFIG")
    os.environ["JEV_CONFIG"] = str(r / "config" / "agents.json")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import jev  # noqa: WPS433
    finally:
        if old is None:
            os.environ.pop("JEV_CONFIG", None)
        else:
            os.environ["JEV_CONFIG"] = old
    return jev


def _sync():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import sync_agents  # noqa: WPS433
    return sync_agents


# ---------- loading ----------

def shipped_config(repo=None):
    return json.loads((_repo(repo) / "config" / "agents.json").read_text(encoding="utf-8"))


def _preset_dir(repo):
    return _repo(repo) / "config" / "presets"


def _valid_names(repo):
    return list(PRESET_ORDER) + [CUSTOM_NAME]


def list_presets(repo=None):
    out = []
    for name in PRESET_ORDER:
        data = load_preset(repo, name)
        out.append({"name": name, "summary": data.get("summary", ""),
                    "requires_env": list(data.get("requires_env") or []), "base": data.get("base")})
    out.append({"name": CUSTOM_NAME, "summary": CUSTOM_SUMMARY, "requires_env": [], "base": "shipped",
                "alias_of": "balanced"})
    return out


def load_preset(repo, name):
    if name == CUSTOM_NAME:
        name = "balanced"
    if name not in PRESET_ORDER:
        raise PresetError("unknown preset %r (valid: %s)" % (name, ", ".join(_valid_names(repo))))
    path = _preset_dir(repo) / (name + ".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PresetError("cannot read preset %s: %s" % (name, e))


def normalize_role(name, roles=None):
    name = str(name).strip()
    if name.startswith("jev-"):
        return name
    return "jev-" + name


def parse_sets(values, roles=None):
    """["builder=sonnet:low", "qa=work"] -> {"jev-builder": {"model": "sonnet", "effort": "low"}, ...}"""
    out, errors = {}, []
    for v in values or []:
        if "=" not in str(v):
            errors.append("--set %r: expected ROLE=KEY[:EFFORT]" % v)
            continue
        role, rest = str(v).split("=", 1)
        role = normalize_role(role)
        if roles is not None and role not in roles:
            errors.append("--set %r: unknown role %s (valid: %s)" % (v, role, ", ".join(sorted(roles))))
            continue
        key, _, effort = rest.partition(":")
        key, effort = key.strip(), effort.strip()
        if not key and not effort:
            errors.append("--set %r: expected ROLE=KEY[:EFFORT]" % v)
            continue
        spec = out.setdefault(role, {})
        if key:
            spec["model"] = key
        if effort:
            spec["effort"] = effort
    if errors:
        raise PresetError(errors)
    return out


def parse_model_ids(values):
    """["work=glm-5.3"] -> {"work": "glm-5.3"}"""
    out, errors = {}, []
    for v in values or []:
        if "=" not in str(v):
            errors.append("--model-id %r: expected KEY=ID" % v)
            continue
        key, mid = str(v).split("=", 1)
        key, mid = key.strip(), mid.strip()
        if not key or not mid:
            errors.append("--model-id %r: expected KEY=ID" % v)
            continue
        out[key] = mid
    if errors:
        raise PresetError(errors)
    return out


def expand_roles(values, roles):
    roles = list(roles)
    picked, errors = set(), []
    for v in values or []:
        if str(v).strip() == "all":
            picked.update(roles)
            continue
        role = normalize_role(v)
        if role not in roles:
            errors.append("unknown role %r (valid: all, %s)" % (v, ", ".join(r[4:] for r in roles)))
        else:
            picked.add(role)
    if errors:
        raise PresetError(errors)
    return [r for r in roles if r in picked]


# ---------- validation ----------

def _is_alias(model_id):
    s = re.sub(r"\[[^\]]*\]\s*$", "", str(model_id)).strip().lower()
    return s in ALIASES


def _is_opus_id(model_id):
    return "opus" in str(model_id).lower()


def validate_config(cfg, allow_opus=(), allow_max=(), repo=None):
    errors, warnings = [], []
    shipped = shipped_config(repo)
    models = cfg.get("models") or {}
    agents = cfg.get("agents") or {}
    opus_ok = set(OPUS_ROLES) | set(allow_opus)
    max_ok = set(MAX_ROLES) | set(allow_max)
    for key, mid in sorted(models.items()):
        if not isinstance(mid, str) or not MODEL_ID_RE.match(mid):
            errors.append("model %s: ID %r is not a valid model ID" % (key, mid))
        elif _is_alias(mid):
            errors.append("model %s: %r is an alias; use a full model ID (aliases follow settings remaps)" % (key, mid))
    opus_id = models.get("opus")
    for role in sorted(shipped["agents"]):
        if role not in agents:
            errors.append("%s: missing role" % role)
            continue
        spec = agents[role]
        key, effort = spec.get("model"), spec.get("effort")
        if key not in models:
            errors.append("%s: unknown model key %r (known: %s)" % (role, key, ", ".join(sorted(models))))
            continue
        if effort not in EFFORTS:
            errors.append("%s: bad effort %r (valid: %s)" % (role, effort, ", ".join(EFFORTS)))
        mid = models[key]
        uses_opus = key == "opus" or _is_opus_id(mid)
        if uses_opus and role not in opus_ok:
            errors.append("%s on Opus needs --allow-opus %s (Opus is reserved for architect and debugger)" % (
                role, role[4:]))
        elif uses_opus and role not in OPUS_ROLES:
            warnings.append("%s runs on Opus (--allow-opus): higher cost and rate-limit use than Sonnet" % role)
        if effort == "max" and role not in max_ok:
            errors.append("%s at max effort needs --allow-max %s (max is reserved for the architect)" % (role, role[4:]))
        elif effort == "max" and role not in MAX_ROLES:
            warnings.append("%s runs at max effort (--allow-max): slowest and most expensive setting" % role)
        if opus_id and key != "opus" and (mid == opus_id or _is_opus_id(mid)):
            errors.append("%s: model key %r resolves to the Opus ID %s; jev.py check_dispatch would deny it "
                          "(use a key other than opus/sonnet for non-Opus IDs, or keep Opus IDs on the opus key)" % (
                              role, key, mid))
        ship = shipped["agents"][role]
        if bool(spec.get("write")) != bool(ship.get("write")):
            errors.append("%s: write flag differs from the shipped config" % role)
    for role in sorted(agents):
        if role not in shipped["agents"]:
            errors.append("%s: not a shipped role" % role)
    for role, val in sorted((cfg.get("fallbacks") or {}).items()):
        if val is not None:
            errors.append("fallback %s -> %s: silent substitution is not allowed (must be null)" % (role, val))
    if cfg.get("fallbacks") != shipped.get("fallbacks"):
        errors.append("fallbacks differ from the shipped config")
    return errors, warnings


# ---------- building ----------

def _resolve_models(models, remap_env, overridden, name):
    out, errors = {}, []
    env = remap_env or {}
    for key, val in models.items():
        if isinstance(val, str) and val.startswith("$"):
            var = val[1:]
            if key in overridden:
                continue
            got = env.get(var)
            if not got:
                errors.append("preset %s needs %s for model key %r: set it in settings.json env or pass "
                              "`--model-id %s=ID`" % (name, var, key, key))
                continue
            out[key] = got
        else:
            out[key] = val
    return out, errors


def build_config(repo, preset, *, sets=None, model_ids=None, allow_opus=(), allow_max=(), remap_env=None):
    errors = []
    data = load_preset(repo, preset)
    name = data.get("name", preset)
    shipped = shipped_config(repo)
    roles = sorted(shipped["agents"])
    try:
        mids = model_ids if isinstance(model_ids, dict) else parse_model_ids(model_ids)
    except PresetError as e:
        errors.extend(e.errors)
        mids = {}
    try:
        sets_d = sets if isinstance(sets, dict) else parse_sets(sets, roles)
    except PresetError as e:
        errors.extend(e.errors)
        sets_d = {}
    try:
        opus_roles = expand_roles(allow_opus, roles)
        max_roles = expand_roles(allow_max, roles)
    except PresetError as e:
        errors.extend(e.errors)
        opus_roles, max_roles = [], []

    cfg = copy.deepcopy(shipped)
    if data.get("base") != "shipped":
        models, errs = _resolve_models(data.get("models") or {}, remap_env, set(mids), name)
        errors.extend(errs)
        cfg["models"] = models
        pagents = data.get("agents") or {}
        for role in roles:
            if role not in pagents:
                errors.append("preset %s: missing role %s" % (name, role))
                continue
            cfg["agents"][role]["model"] = pagents[role]["model"]
            cfg["agents"][role]["effort"] = pagents[role]["effort"]
    cfg["models"].update(mids)
    for role, spec in sets_d.items():
        if role not in cfg["agents"]:
            errors.append("--set: unknown role %s" % role)
            continue
        if "model" in spec:
            if spec["model"] not in cfg["models"]:
                errors.append("--set %s: model key %r does not exist (known: %s); add it with --model-id KEY=ID" % (
                    role, spec["model"], ", ".join(sorted(cfg["models"]))))
            else:
                cfg["agents"][role]["model"] = spec["model"]
        if "effort" in spec:
            if spec["effort"] not in EFFORTS:
                errors.append("--set %s: bad effort %r (valid: %s)" % (role, spec["effort"], ", ".join(EFFORTS)))
            else:
                cfg["agents"][role]["effort"] = spec["effort"]

    g = cfg.setdefault("guardrails", {})
    g["opus_allowed_roles"] = list(shipped["guardrails"]["opus_allowed_roles"]) + [
        r for r in opus_roles if r not in shipped["guardrails"]["opus_allowed_roles"]]
    g["max_effort_allowed_roles"] = list(shipped["guardrails"]["max_effort_allowed_roles"]) + [
        r for r in max_roles if r not in shipped["guardrails"]["max_effort_allowed_roles"]]

    cfg["_comment"] = ("Generated by scripts/onboard.py from preset %r. Edit by re-running onboarding; "
                       "config/agents.json holds the shipped defaults." % name)
    cfg["_preset"] = {"name": name, "sets": {r: dict(s) for r, s in sorted(sets_d.items())},
                      "model_ids": dict(sorted(mids.items())), "allow_opus": list(opus_roles),
                      "allow_max": list(max_roles)}
    if errors:
        raise PresetError(errors)
    verrs, warnings = validate_config(cfg, opus_roles, max_roles, repo=repo)
    if verrs:
        raise PresetError(verrs)
    return cfg, warnings


# ---------- rendering ----------

def render_agents(repo, cfg):
    repo = _repo(repo)
    jev = _jev(repo)
    sync = _sync()
    out, errors = {}, []
    for role in sorted(cfg["agents"]):
        tpl = repo / "agents" / (role + ".md")
        if not tpl.is_file():
            errors.append("%s: no agent template at agents/%s.md" % (role, role))
            continue
        try:
            text = sync.render(tpl.read_text(encoding="utf-8"), role, cfg)
        except (ValueError, KeyError) as e:
            errors.append("%s: %s" % (role, e))
            continue
        _, _, ferrs = jev.parse_frontmatter(text, use_yaml=False)
        if ferrs:
            errors.append("%s: invalid frontmatter: %s" % (role, "; ".join(ferrs)))
            continue
        out[role + ".md"] = text
    if errors:
        raise PresetError(errors)
    return out


# ---------- tables ----------

def _table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = "| " + " | ".join("%%-%ds" % w for w in widths) + " |"
    out = [line, fmt % tuple(headers), line]
    out.extend(fmt % tuple(r) for r in rows)
    out.append(line)
    return "\n".join(out)


def routing_table(cfg):
    g = cfg.get("guardrails") or {}
    rows = []
    for role in sorted(cfg["agents"]):
        spec = cfg["agents"][role]
        notes = []
        if role in (g.get("opus_allowed_roles") or []) and role not in OPUS_ROLES:
            notes.append("opus override")
        if role in (g.get("max_effort_allowed_roles") or []) and role not in MAX_ROLES:
            notes.append("max override")
        rows.append([role, "yes" if spec.get("write") else "no", spec["model"],
                     cfg["models"].get(spec["model"], "?"), spec["effort"],
                     "; ".join(notes) or spec.get("role", "")])
    return _table(["Role", "Writes", "Model key", "Model ID", "Effort", "Note"], rows)


def format_presets(presets):
    rows = [[p["name"], p.get("summary", ""), ", ".join(p.get("requires_env") or []) or "-"] for p in presets]
    return _table(["Preset", "Summary", "Needs env"], rows)
