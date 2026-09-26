#!/usr/bin/env python3
"""PreToolUse hook (matcher Agent|Task): let Jev re-pick the tier of a jev-* subagent dispatch.

Reads the hook payload on stdin, routes description + prompt with jev.route_task, and when the policy
below allows it prints {"hookSpecificOutput": {..., "updatedInput": <tool_input with subagent_type replaced>}}.
Otherwise prints nothing. Never blocks: every failure path exits 0 with no output. Every run is logged to
decisions.jsonl as kind "dispatch" (see `jev.py outcomes`).

Env:
  JEV_DISPATCH=apply|shadow|off   apply (default) rewrites, shadow only logs, off does nothing
  JEV_DISPATCH_LOG=name           alternative log name (default "decisions"; tests)
  JEV_DISPATCH_FAKE=path          JSON file used instead of Jev (tests): a route_task decision, or {"error": "..."}
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
NEVER_APPLY = {"jev-debugger"}  # high effort for code only when stuck (jev.py stuck handles that)


def fake_route_task(path):
    def route(task, context="", timeout=None):
        import jevlib
        with open(path) as f:
            d = json.load(f)
        if "error" in d:
            raise jevlib.JevError(d["error"])
        return d, {}, {}
    return route


def main():
    mode = (os.environ.get("JEV_DISPATCH") or "apply").strip().lower()
    if mode == "off":
        return
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
    rec = {"kind": "dispatch", "mode": mode, "errors": [], "applied": False, "skip": None, "jev_type": None}
    try:
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        main_type = ti.get("subagent_type") or ""
        desc = ti.get("description") if isinstance(ti.get("description"), str) else ""
        prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
        rec.update(session_id=payload.get("session_id"), tool_use_id=payload.get("tool_use_id"),
                   description=jevlib.redact(desc)[:200], main_type=main_type or None)
        side = jev.tier_side(main_type)
        if not side:
            rec["skip"] = "tier_not_routable"
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
        rec["skip"] = skip
        if skip:
            return
        reason = (d.get("reasons") or ["policy"])[0]
        out = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "Jev routed %s -> %s (%s)" % (main_type, pick, reason),
            "updatedInput": dict(ti, subagent_type=pick)}}
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
        rec["applied"] = True
    except Exception as e:
        rec["errors"].append("fatal: %s" % str(e)[:300])
    finally:
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
