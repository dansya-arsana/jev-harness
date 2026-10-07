---
name: jev-orchestrator
description: Use Jev (TypeSafe) to decide how to split and delegate work to subagents. Use when a task has several independent parts, when about to spawn a subagent, when running a goal-driven or multi-round loop, or when an attempt keeps failing. Jev decides whether each subtask should be delegated, which tier (scout/analyst read-only; builder/engineer/debugger/architect for edits; ultracode for Workflow orchestration), whether it can run in parallel, whether it duplicates earlier work, and whether the agent is stuck. Trigger: "/jev-orchestrator", "delegate with jev", "route this with jev", "split this into subagents".
---

# Jev orchestrator

You (the main agent) plan and split the work. **Jev makes the small decisions about each piece.** Subagents do the work and return short results. Code, not Jev, holds the policy: the thresholds live in `scripts/jev.py`.

Helper: `python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py <command>`. Each command prints one JSON object. A route call is ~800 Jev input tokens and ~1.3 s, so ask freely.

## Tiers

**Owner rule for effort:**
- **high, xhigh ("extra"), max and ultracode:** only for decisions, architecture thinking and orchestration.
- **medium:** review and checks.
- **code:** low by default. Medium or high only when it's really needed, never as the starting point.

Every tier runs `claude-sonnet-5-5`, pinned in the agent file.

| tier | subagent_type | effort | ladder | for |
|---|---|---|---|---|
| scout | `jev-scout` | low | read | lookups, quick read-only answers |
| analyst | `jev-analyst` | high | read | investigation, root-cause finding (decision thinking, no edits) |
| advisor | `jev-advisor` | max | read | design questions that only need a recommendation |
| architect | `jev-architect` | max | plan | the implementation plan for design, hard or high-stakes work; never edits |
| reviewer | `jev-reviewer` | medium | read | review and checks of finished work |
| qa | `jev-qa` | low | read | browser QA of local/staging pages: screenshots plus DOM checks via `jevqa.py` |
| builder | `jev-builder` | **low** | write | **default coder**: ordinary edits, and implementing a plan |
| engineer | `jev-engineer` | medium | write | substantial multi-file changes, or a stuck builder |
| debugger | `jev-debugger` | high | write | only when builder and engineer got stuck on the same problem |
| ultracode | Workflow | orchestration | orchestrate | planning and review fan-out; coding agents inside it pass `opts.effort: 'low'`, and reviewers `'medium'` |

**Two-step routing:** when `route` returns `plan_first`, run the planner first (read-only, high or above), then give its plan verbatim to the implementer, which is `jev-builder` at low effort. Only raise coding effort through a stuck check: builder (low) → engineer (medium) → debugger (high) → replan with the architect.

## Loop

1. **Split** the request into subtasks. Write each one so it stands alone: what to do, where, and how to know it's done.
2. **Dedupe** each subtask before starting it: `jev.py dedupe "<subtask>"`. If `duplicate_of` is set, don't start it. Reuse the finished result (`status: done`) or wait for the in-flight one. Otherwise it is registered with an id (`registered`). The ledger is `./.jev/subgoals.jsonl` in the current project.
3. **Route** it: `jev.py route "<subtask>" --context "<only the facts the subagent needs>"`. The output has `tier`, `subagent_type`, `effort`, `ladder`, `delegate`, `via`, `parallel_safe`, `depth`, `breadth`, `signals`, `reasons`.
   - `via: main` (`delegate: false`): do it yourself, or rewrite the subtask with the missing context and route again.
   - `via: subagent`: spawn `subagent_type` **without a `model` parameter**. The agent files pin `claude-sonnet-5-5`. Passing an alias (`opus`/`sonnet`/`haiku`) would override that, and if your settings remap aliases (for example `ANTHROPIC_DEFAULT_OPUS_MODEL`) the subagent would run on whatever the alias points to. Put everything the subagent needs in the prompt, because it doesn't see this conversation. Ask for a short result: what changed, where, and anything uncertain.
   - `via: workflow` (tier `ultracode`, `subagent_type: null`): don't spawn one subagent. Load the `workflow-authoring` skill, then author and run a Workflow script that fans out agents and adversarially verifies their results. `workflow_shape` (understand / investigate / review read-only; audit / migrate write) and `workflow_hint` suggest a structure. In the script's `agent()` calls, **always pass `opts.model: 'claude-sonnet-5-5'`**: Workflow agents otherwise inherit the session's model alias, which follows any alias remapping in your settings. Never pass an alias. Pass `opts.effort` set to the returned `effort` (the worker tier's effort, see `worker_tier`). `workflow_shape` `investigate` and `understand` are read-only: no writer agents. Writers must never share files.
   - `parallel_safe: true` means the subtask is read-only, so launch it together with other parallel-safe subtasks in one message. Subtasks that edit files run **one at a time**, never two writers on the same files.
4. **Mark done**: `jev.py done <id>` once the result is back and checked.
5. **Stuck check**: if a subtask failed twice, or tests keep failing for the same reason, save the recent output to a file and run `jev.py stuck --state @<file> --tier <tier>`. If `escalate: true`, re-run it with `next_tier` / `subagent_type` and tell the new agent what already failed. Escalation stays on the task's own ladder: scout -> analyst -> analyst, and code builder (low) -> engineer (medium) -> debugger (high) -> architect replans. A stuck architect returns `next_tier: ultracode`, meaning orchestrate it with a Workflow.
6. **Review**: for non-trivial changes, finish with `jev-reviewer` on the diff.
7. **Verify with real checks.** Jev's answers are always well-formed but can still be wrong. "Done" means tests pass or the build succeeds, not that Jev thinks it's done.

## Context packs: search once, share with every subagent

When two or more subagents (including the final reviewer) will work in the same area, gather the code once instead of letting each one re-explore. Measured effect (see the repo's `docs/REPORT.md`): slices cut tool calls by about half and wall time by 25–38%, but a full 8k-token slice can cost *more* tokens than letting an agent grep a small, well-organized codebase (+42–59% on Click). Use packs when speed matters, when several agents share one slice, or on large or unfamiliar code. The default slice is outline-first (4k budget, only clearly relevant chunks in full); raise `--budget` when speed matters more than tokens.

Helper: `python3 ~/.claude/skills/jev-orchestrator/scripts/jevpack.py <command>` (run from the project root; packs go to `./.jev/packs/`).

1. **Find the files once.** Grep/Glob yourself, or one `jev-scout`, then write the paths to a file.
2. **Build:** `jevpack.py build --name <n> --task "<overall task>" --files-from <list>` (or pass paths/globs, or `--grep REGEX`). No model is used; it chunks the files by function/class and takes about 0.1 s. Secrets files are skipped.
3. **Slice per subtask:** `jevpack.py slice <n> --subtask "<subtask>" > .jev/packs/<n>-<k>.md` (default budget 4000 tokens). Jev scores every chunk for this subtask: essential chunks in full with real line numbers, background chunks as one-line outlines, the rest hidden. That's about 3 s and under a cent per slice.
4. **Hand it over by path, not by pasting:** in the subagent prompt write "First Read `<abs path to slice>`; start from it and open other files only if something is missing." That keeps the slice out of your own context.
5. Give the reviewer a slice too (subtask = "review this change" plus the diff summary).

Skip packs for a single small task, or when the files are already known and few. If `slice` errors, fall back to giving the subagent the file list from `jevpack.py info <n>`.

## Browser QA (jev-ultrafast + jev-qa)

`scripts/jevqa.py` pairs browser-use's jev-ultrafast (Jev picks each browser action) with the `jev-qa` reviewer (Opus 5.5, low effort), which reads every screenshot and the report. One-time setup:

```bash
git clone https://github.com/browser-use/jev-ultrafast.git ~/Documents/Tools/jev-ultrafast
cd ~/Documents/Tools/jev-ultrafast && uv sync
```

Run: `uv run --project ~/Documents/Tools/jev-ultrafast python scripts/jevqa.py run <scenario.json> [--out DIR] [--headful]`. The default out dir is `./.jev/qa/<timestamp>/`.

- The QA Chrome is always a throwaway profile: a fresh temp `--user-data-dir` on its own debugging port, headless unless `--headful`. It never attaches to your normal Chrome. It is killed and its profile deleted on exit, including on errors and Ctrl-C.
- Typed text comes from a local shim that answers only from the flow's `values` map (`{"<label regex>": "<value>"}`). Anything it doesn't match gets `{"text": null}`, and nothing is typed. Values whose labels look secret (`password|card|cvv|token|secret`) are masked in the report.
- Flows whose host isn't in `allow_hosts` (default: localhost only) are refused. A run that navigates off those hosts stops with `left_allowed_hosts`.
- Scenario: `{"base_url", "allow_hosts", "viewports": [{name, width, height, mobile?}], "flows": [{name, path, goal?, values?, max_steps? (25), expect_text?, expect_url?, viewports?, max_slices? (10), wait_ms? (0), init_script?, wait_for?, wait_for_loader? (true), loader_timeout_ms? (10000), scroll_to?}], "init_script"?, "wait_for_loader"?}`. A flow with no `goal` is capture-only.
- Intro loaders: after load, jevqa waits for the page to stop looking busy. Busy means still loading, `aria-busy`, every visible control inside `[inert]`/`[aria-hidden]`, or a full-screen fixed overlay with no controls. It waits up to `loader_timeout_ms`, then goes on either way. The result is recorded as `loader` (`detected`, `reason`, `waited_ms`, `cleared`), and a capture flow whose loader never cleared fails `checks.loader`. `wait_for_loader: false` (scenario or flow) turns it off. `wait_for` (a CSS selector) then polls until that element is visible, for up to 15 s; a timeout is a failed `wait_for` check. After that, jevqa sleeps `wait_ms` (0-15000). All of this happens before capture, and for goal flows before the agent's first decision; the agent then re-observes the page. `init_script` (a string, at scenario level or per flow; a flow's own value overrides the scenario one and `""` turns it off) is injected with `Page.addScriptToEvaluateOnNewDocument` before navigation. Use it only for loaders that never clear on their own.
- Goal flows: jevqa swaps jev-ultrafast's `Browser` for a subclass, inside the jevqa process only, while the `Agent` is being constructed. The subclass does four things:
  - It runs the agent at the flow's first listed viewport (else the scenario's first). jev-ultrafast on its own always uses 1120×780 desktop.
  - It scrolls about 0.8 of a screen per step, with the wheel at the viewport centre.
  - It registers the `init_script` just before the first navigation.
  - It gives one JS `click()` to a disclosure (`<summary>` or `aria-expanded`) whose state didn't change after a real click. These are logged in `fallback_clicks`; report each one as a bug.

  `scroll_to` (a CSS selector) starts a goal flow at that section. Use it unless finding the section is itself the test, because jev-ultrafast rarely scrolls to look for something it can't see.

  ```json
  {"base_url": "http://localhost:3002",
   "viewports": [{"name": "desktop", "width": 1440, "height": 900}, {"name": "mobile", "width": 390, "height": 844, "mobile": true}],
   "flows": [{"name": "home", "path": "/", "wait_for": "h1", "max_slices": 4},
             {"name": "faq", "path": "/", "viewports": ["desktop"], "scroll_to": "#faq", "max_steps": 8,
              "goal": "Expand the FAQ question 'How are prompts checked?'. Do not submit any form."},
             {"name": "menu", "path": "/", "viewports": ["mobile"], "max_steps": 6,
              "goal": "Open the site menu using the Menu button. Do not submit any form."}]}
  ```
- Output: `<flow>-<viewport>-NN.png` slices, one `<flow>-<viewport>-sheet.jpg` contact sheet per flow and viewport (the slices in reading order, 3 columns, 480px tiles, 6px black gutter; listed first in `report.md`), plus `report.json` and `report.md`. The checks cover horizontal overflow, missing image alt text, unnamed buttons and links, text under 11px, `expect_text`, `expect_url`, console errors and the loader. Goal flows also report:
  - `untouched_selects`: `<select>`s still on their first option (a hint, not a failure);
  - `agent_viewport`, `loader`, `scroll_to` and `fallback_clicks`;
  - `initial_labels` / `final_labels`: what the agent could click before and after the run. Read `initial_labels` first when a flow is BLOCKED at 0 steps.

## Live dispatch routing

`hooks/dispatch_router.py` (PreToolUse, matcher `Agent|Task`) re-routes each jev-* dispatch through `jev.py`'s policy, so the tier you pick may be swapped before the agent starts.
- Only picks with a routable tier take part (scout, analyst, builder, engineer, debugger, architect, reviewer, advisor). Others (jev-qa, Explore, general-purpose, ...) are just logged.
- Jev's pick replaces yours only if it is a single subagent (not main, ultracode or plan_first), on the same side (read/plan vs write), `depth_confidence >= 0.6`, and not jev-debugger.
- Put `[jev:keep]` in the prompt to keep your pick (it is still logged).
- `JEV_DISPATCH=apply` (default), `shadow` (log only) or `off`.
- `jev.py outcomes [--days 7] [--json]` joins dispatch logs with subagent transcripts: duration, tokens, errors, stuck/escalated signals, agreement and labeled accuracy.
- `jev.py label <tool_use_id|last> ok|too_low|too_high [--note TEXT]` records whether the tier that ran was right; the latest label wins.

## Rules

- Don't route trivial one-step requests. Just do them. This skill is for multi-part work.
- Show the user the routing table (subtask -> tier/effort, and why) before spawning more than 3 subagents or starting an ultracode Workflow.
- `jev.py report [--days 7] [--json]` summarizes the gate, prompt-router and routing logs, with a "worth a look" list (Jev errors, slow prompts, commands the gate keeps asking about). Run it when the user asks how the Jev hooks are doing or wants to tune thresholds.
- After a routed agent completes, label it: `jev.py label <id> ok|too_low|too_high` (`last` for the latest dispatch). Those labels are the accuracy numbers.
- Every decision is logged to `~/.claude/jev/decisions.jsonl`. When a routing turns out wrong, say so, because that log is how the thresholds get tuned.
- The API key is read from `TYPESAFE_API_KEY`, then `~/.config/typesafe/.env`, then `.env` at the root of this repo. Never print it. Task text is redacted before it is sent or logged.
- If the helper returns `{"error": ..., "fallback": ...}` (exit 1), don't guess Jev's answer. Follow `fallback`, use your own judgment, and tell the user Jev was unavailable.
