#!/usr/bin/env python3
"""PreToolUse hook (matcher Agent|Task): guard jev-* subagent dispatches, then let Jev re-pick the tier.

1. Guard (vNext, every jev-* dispatch; config/agents.json is the source of truth):
   DENY when the agent file is missing or invalid (the same per-agent check as `jev.py preflight`), when its pinned
   model/effort deviates from config, when the call passes an alias model (opus/sonnet/haiku) or a model that differs
   from config, when a non-opus role would resolve to opus, and when a task routed by `jev.py route` (prompt carries
   `[jev:task=<id>]`) spawns a jev agent that was neither routed nor reached by `jev.py escalate` (no silent substitution).
   WARN (systemMessage) when an opus/max role is outside guardrails. Every jev-* dispatch appends a JSON line to
   ~/.claude/jev/routing.log and prints the `[JEV]` block on stderr. If config/agents.json is unreadable the guard fails
   open, loudly (stderr + routing.log + decisions.jsonl).
2. Re-route (unchanged policy): routes description + prompt with jev.route_task and, when the policy below allows it,
   prints {"hookSpecificOutput": {..., "updatedInput": <tool_input with subagent_type replaced>}}. Skipped for tasks with a
   recorded route (the route is authoritative) and when Jev's pick fails the guard. Every run is logged to decisions.jsonl
   as kind "dispatch" (see `jev.py outcomes`).
Never crashes the tool call: unexpected failures exit 0 with no output.

Env:
  JEV_DISPATCH=apply|shadow|off   re-route: apply (default) rewrites, shadow only logs, off skips it (the guard still runs)
  JEV_GUARD=on|off                guard (default on); off restores the pre-vNext behaviour
  JEV_DISPATCH_LOG=name           alternative decisions log name (default "decisions"; tests)
  JEV_DISPATCH_FAKE=path          JSON file used instead of Jev (tests): a route_task decision, or {"error": "..."}
  JEV_AGENTS_DIR / JEV_CONFIG / JEV_HOME   agents dir, config path, state dir overrides (tests)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts"))

# ---- policy ---------------------------------------------------------------
APPLY_MIN_CONF = 0.6
TIMEOUT_S = 4.0
PROMPT_CHARS = 1500
KEEP_MARKER = "[jev:keep]"
NEVER_APPLY = {"jev-debugger"}  # high effort for code only when stuck (jev.py escalate handles that)


def fake_route_task(path):
    def route(task, context="", timeout=None):
        import jevlib
        with open(path) as f:
            d = json.load(f)
        if "error" in d:
            raise jevlib.JevError(d["error"])
        return d, {}, {}
    return route


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def guard(jev, jevlib, ti, main_type, task_id, cwd, log_name):
    """Returns (result dict or None if failed open, cfg or None)."""
    try:
        cfg = jev.load_config()
    except jev.ConfigError as e:
        msg = "JEV CONFIG UNREADABLE (%s): dispatch guard skipped (fail-open). Fix config/agents.json." % e
        sys.stderr.write(msg + "\n")
        jev.routing_log({"event": "config_unreadable", "error": str(e)[:300], "role": main_type, "task_id": task_id})
        jevlib.log(log_name, {"kind": "dispatch_guard", "fail_open": True, "error": msg, "main_type": main_type})
        return None, None
    routes = jev.load_routes()
    model_param = ti.get("model") if isinstance(ti.get("model"), str) and ti.get("model").strip() else None
    res = jev.check_dispatch(cfg, main_type, model_param=model_param, task_id=task_id, project_dir=cwd, routes=routes)
    attempt = None
    if res["allow"] and res["task_routed"]:
        try:
            attempt = jev.bump_dispatch(task_id)
        except Exception:
            pass
    res["attempt"] = attempt
    fields = {"task_id": task_id, "role": main_type, "model": res["model"], "effort": res["effort"],
              "reason": res["reason"], "attempt": attempt}
    jev.routing_log(dict(fields, event="dispatch", decision="allow" if res["allow"] else "deny",
                         model_param=model_param, errors=res["errors"], warnings=res["warnings"]))
    sys.stderr.write(jev.jev_line(fields) + ("" if res["allow"] else "\ndecision=deny") + "\n")
    return res, cfg


def main():
    mode = (os.environ.get("JEV_DISPATCH") or "apply").strip().lower()
    guard_on = (os.environ.get("JEV_GUARD") or "on").strip().lower() != "off"
    t0 = time.time()
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            return
        import jevlib
        import jev
    except Exception:
        return
    log_name = os.environ.get("JEV_DISPATCH_LOG") or "decisions"
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    main_type = ti.get("subagent_type") or ""
    desc = ti.get("description") if isinstance(ti.get("description"), str) else ""
    prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None

    g, cfg, warnings = None, None, []
    if guard_on and isinstance(main_type, str) and main_type.startswith("jev-"):
        try:
            task_id = jev.task_marker(desc + "\n" + prompt)
            g, cfg = guard(jev, jevlib, ti, main_type, task_id, cwd, log_name)
        except Exception as e:  # a bug in the guard must not block every dispatch, but it must be visible
            sys.stderr.write("JEV GUARD ERROR (fail-open): %s\n" % e)
            g = None
        if g is not None:
            if not g["allow"]:
                emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "\n".join(g["errors"])}})
                try:
                    jevlib.log(log_name, {"kind": "dispatch", "mode": mode, "session_id": payload.get("session_id"),
                                          "tool_use_id": payload.get("tool_use_id"), "main_type": main_type,
                                          "description": jevlib.redact(desc)[:200], "applied": False, "skip": "guard_deny",
                                          "jev_type": None, "errors": g["errors"], "task_id": g.get("task_id")})
                except Exception:
                    pass
                return
            warnings = g["warnings"]
    if mode == "off":
        if warnings:
            emit({"systemMessage": "\n".join(warnings)})
        return

    rec = {"kind": "dispatch", "mode": mode, "errors": [], "applied": False, "skip": None, "jev_type": None}
    out = None
    try:
        rec.update(session_id=payload.get("session_id"), tool_use_id=payload.get("tool_use_id"),
                   description=jevlib.redact(desc)[:200], main_type=main_type or None)
        if g is not None:
            rec.update(task_id=g.get("task_id"), guard_warnings=warnings or None, attempt=g.get("attempt"))
        side = jev.tier_side(main_type)
        if not side:
            rec["skip"] = "tier_not_routable"
            return
        if g is not None and g.get("task_routed"):
            rec["skip"] = "task_routed"  # the recorded route is authoritative; a swap here would be a silent substitution
            return
        fake = os.environ.get("JEV_DISPATCH_FAKE")
        route_task = fake_route_task(fake) if fake else jev.route_task
        try:
            d, _, _ = route_task(desc + "\n\n" + prompt[:PROMPT_CHARS], timeout=TIMEOUT_S)
        except Exception as e:
            rec["errors"].append(str(e)[:300])
            rec["skip"] = "jev_error"
            return
        pick = d.get("subagent_type")
        conf = d.get("depth_confidence") or 0.0
        rec.update(jev_type=pick, depth=d.get("depth"), breadth=d.get("breadth"), depth_confidence=d.get("depth_confidence"),
                   signals=d.get("signals"), reasons=d.get("reasons"), plan_first=d.get("plan_first"))
        if KEEP_MARKER in prompt or KEEP_MARKER in desc:
            skip = "keep_marker"
        elif d.get("via") != "subagent" or not pick:
            skip = "not_single_subagent"
        elif d.get("plan_first"):
            skip = "plan_first"
        elif pick == main_type:
            skip = "agrees"
        elif jev.tier_side(pick) != side:
            skip = "cross_side"
        elif pick in NEVER_APPLY:
            skip = "never_debugger"
        elif conf < APPLY_MIN_CONF:
            skip = "low_confidence"
        elif mode != "apply":
            skip = "shadow"
        else:
            skip = None
        new_input = dict(ti, subagent_type=pick)
        if not skip and cfg is not None:
            if "model" in ti:  # keep an explicit model consistent with the new agent
                new_input["model"] = jev.agent_model(cfg, pick)
            pg = jev.check_dispatch(cfg, pick, model_param=new_input.get("model"), project_dir=cwd)
            if not pg["allow"]:
                skip = "jev_pick_invalid"
                rec["errors"].extend(pg["errors"])
        rec["skip"] = skip
        if skip:
            return
        reason = (d.get("reasons") or ["policy"])[0]
        out = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "Jev routed %s -> %s (%s)" % (main_type, pick, reason),
            "updatedInput": new_input}}
        rec["applied"] = True
        if cfg is not None:
            jev.routing_log({"event": "reroute", "task_id": rec.get("task_id"), "role": pick, "from": main_type,
                             "model": jev.agent_model(cfg, pick), "effort": jev.agent_effort(cfg, pick),
                             "reason": reason, "attempt": None})
    except Exception as e:
        rec["errors"].append("fatal: %s" % str(e)[:300])
    finally:
        if warnings:
            out = dict(out or {}, systemMessage="\n".join(warnings))
        if out:
            emit(out)
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        try:
            jevlib.log(log_name, rec)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
