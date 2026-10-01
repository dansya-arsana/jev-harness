---
name: jev-orchestrator
description: "Use Jev (TypeSafe) to split and delegate work to JEV subagents with deterministic model routing. Use when a task has several independent parts, when about to spawn a subagent, when running a goal-driven or multi-round loop, or when an attempt keeps failing. Runs a preflight of the jev-* agents, routes each subtask (fast path straight to the builder, or architect plan first), persists plans, validates structured handoffs, and escalates by failure category. Triggers include /jev-orchestrator, delegate with jev, route this with jev, split this into subagents."
---

# Jev orchestrator (vNext)

You (the main agent) plan and split the work. **Jev answers the small questions, code holds the policy, and `config/agents.json` holds the routing**: model, effort, write capability, fallbacks (all `null`), context policy, escalation table and guardrails. `scripts/sync_agents.py` writes the agent files from it.

Helper: `python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py <command>`. Each command prints one JSON object (preflight prints a table unless `--json`). A route call is ~800 Jev input tokens and ~1.3 s.

## 0. Preflight first, every task

```bash
python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py preflight        # required agents; --all for all nine
```

It checks each agent file in `~/.claude/agents` (project `.claude/agents` shadows it): file exists, frontmatter parses (strict: an unquoted `: ` or ` #` in a value fails, because Claude Code then silently skips the agent), `name` matches the file, `model` and `effort` equal the config, tools match the write flag, the body is not empty, and no fallback is configured.

- **FAIL (exit 1): stop.** Show the user the table and the `JEV PREFLIGHT FAILED` message. Do not start the task and **never spawn a substitute** (no engineer for a missing builder, no analyst for a missing reviewer).
- **PASS is about files, not the live registry.** Claude Code registers agents when the session starts. If a file was missing or invalid at session start, the agent stays unregistered until the user restarts Claude Code, even if preflight passes now. If the Agent tool says "Agent type 'jev-x' not found", stop and tell the user to restart. Don't substitute.

## Roles (from config/agents.json)

| Agent | Model | Effort | Capability | Purpose |
|---|---|---:|---|---|
| `jev-architect` | Opus 5.5 | max | read-only | architecture, planning, replan. Returns the plan as text and never writes files |
| `jev-advisor` | Sonnet 5.5 | high | read-only | design alternatives, a recommendation |
| `jev-analyst` | Sonnet 5.5 | high | read-only | investigation, code analysis. **Not a reviewer** |
| `jev-builder` | Sonnet 5.5 | low | write | default implementation: the persisted plan, or a fast-path task |
| `jev-engineer` | Sonnet 5.5 | medium | write | complex implementation, builder escalation |
| `jev-debugger` | Opus 5.5 | high | write | hard debugging, last-resort rescue |
| `jev-reviewer` | Sonnet 5.5 | medium | read-only | independent review (review contract) |
| `jev-qa` | Sonnet 5.5 | low | browser | QA and verification via `jevqa.py` |
| `jev-scout` | Sonnet 5.5 | low | read-only | repo, file and symbol lookup |

SONNET executes, inspects and verifies; OPUS decides and rescues. Model IDs are `claude-sonnet-5-5` / `claude-opus-5-5`.

## Spawning: the model is never ambient

- **Agent tool:** pass `subagent_type` only. The agent file pins the model from config. If you pass `model`, it must be exactly the config ID (`claude-sonnet-5-5` for a Sonnet role). **Never pass an alias** (`opus`/`sonnet`/`haiku`). Aliases follow settings remaps (`ANTHROPIC_DEFAULT_*_MODEL`), and the dispatch guard denies them.
- **Workflow `agent()` calls:** always pass `opts.model` set to the config model ID of the role the agent plays (`route` returns `worker_model` for ultracode), plus `opts.effort` from the route. Workflow agents otherwise inherit the session model.
- Put `[jev:task=<task_id>]` (route returns it as `task_marker`) in every prompt of a routed task. The dispatch guard uses it to enforce the route.
- Prompts carry everything the subagent needs, because it doesn't see this conversation: the task, the persisted plan path, the context block, and earlier BLOCKED reports. Don't tell agents to "report terse" or "use graphify first": their files already say so.

## Loop

1. **Split** the request into subtasks that stand alone: what to do, where, and how to know it's done.
2. **Dedupe:** `jev.py dedupe "<subtask>"`. If `duplicate_of` is set, reuse that result or wait for it. The ledger is `./.jev/subgoals.jsonl`.
3. **Route:** `jev.py route "<subtask>" [--context "<facts>"] [--task-id T] [--files a.ts]`. The output adds `task_id`, `task_marker`, `fast_path` (+ `fast_path_checks`), `route`, `sequence` (agent / model / effort / purpose per step), `next_agent`, and `model`/`effort` from config. The route is recorded in `~/.claude/jev/last_route.json`.
   - `route: fast`: only when every check holds (one file expected; no architecture, schema, persistence, public-API or concurrency change; requirements clear; low stakes; small). Spawn **`jev-builder`** directly. No architect, no Opus. Then optionally `jev-reviewer`, and `jev-qa` for UI. If the builder reports the task is not trivial (BLOCKED), escalate (step 6).
   - `route: planned`: (a) spawn `plan_first.planner` (normally `jev-architect`). (b) **You persist its plan:** `jev.py plan save --task T --stdin` (or `--from-file`), which writes `.jev/plans/T.md` and warns on missing sections. Never spawn a builder or engineer just to save markdown; the architect is read-only and never writes. (c) Spawn `plan_first.implementer` (`jev-builder`, low) in a fresh context with the task, the plan path and the context block. (d) Then `jev-reviewer`, then `jev-qa` when there is something runnable.
   - `route: direct`: a read-only task. Spawn `subagent_type` (scout / analyst / reviewer / advisor). `parallel_safe: true` subtasks can launch together. Writers run **one at a time**, never two on the same files.
   - `route: workflow` (tier `ultracode`): load the `workflow-authoring` skill and author a Workflow (`workflow_shape`, `workflow_hint`), with `opts.model` set to `worker_model` and `opts.effort` set to `effort` in every `agent()`. Read-only shapes have no writer agents.
   - `via: main`: do it yourself, or rewrite the subtask with the missing context and route again.
   - Jev unavailable (`{"error", "fallback"}`, exit 1): don't guess Jev's answer. Follow `fallback`: write work takes the planned route unless it is a trivial one-file change, and you tell the user Jev was unavailable.
4. **Context (graph first):** before spawning, `jev.py context prepare --task-id T --role <agent> --task "<subtask>" [--files a,b] [--refresh]`. It returns `{pack_path, context_block, graph_status, fallback, reused}`. Paste `context_block` into the prompt (or hand over `pack_path`). Packs are reused across the route (the builder reuses the architect's pack); use `--refresh` only when the repo changed. If `graph_status` is `unavailable`, the wrapper failed soft: give the agent the file list and say so. A `[JEV CONTEXT]` line is logged whenever a fallback ran.
5. **Handoffs are artifacts, not memory.** Contracts live in `config/schemas/`. Validate them with `jev.py handoff validate --kind plan|completion|failure|review|qa FILE` (JSON, or YAML with PyYAML); `jev.py handoff template --kind K` prints a skeleton. Agents report in the terse protocol (`STATUS: DONE` with CHANGED / WHY / TEST / RISK / NEXT, or `STATUS: BLOCKED` with CATEGORY / FOUND / EVIDENCE / NEXT). `jev.py report lint FILE` checks one. Persisted docs stay normal prose.
6. **Escalate by failure category, never by substitution.** When an agent reports BLOCKED, or `jev.py stuck --state @<file> --tier <tier>` says `escalate: true`, classify the failure and run
   `jev.py escalate --task T --from <role> --category <category> [--evidence path ...]`
   | category | next |
   |---|---|
   | `implementation_complexity` | `jev-engineer` (engineer -> debugger -> architect up the ladder) |
   | `hard_debugging` | `jev-debugger` directly |
   | `invalid_plan` | `jev-architect` **replan directly**, with no engineer or debugger attempt |
   | `requirement_ambiguity` | `orchestrator`: stop coding. Use analyst/advisor only if it can be inferred safely, else ask the user |
   | `environment_failure` | `orchestrator`: fix the environment or stop |
   | `test_failure` | the same coder, twice, then one step up |
   Guardrails from config: `max_replans` (2) and `max_debugger_attempts` (2). When one is hit, `next_agent: orchestrator` and you stop and report to the user. State lives in `.jev/state/T.json`. The escalation is recorded, so the dispatch guard now allows the new agent. Give it the BLOCKED report and what already failed.
7. **Review:** non-trivial changes always end with **`jev-reviewer`** (never `jev-analyst` as a stand-in). Give it the task, the plan path, the acceptance criteria, `git diff`, the test results and a context pack, and nothing of the builder's reasoning. It returns the review contract. `changes_required` goes back to the builder (major/critical issues may need the engineer). `pass` goes to `jev-qa`.
8. **Mark done:** `jev.py done <id>`. **Verify with real checks**: "done" means the tests pass or the build succeeds.

## Dispatch guard (hooks/dispatch_router.py, PreToolUse `Agent|Task`)

For every `jev-*` dispatch it **denies**:
- an agent that fails the per-agent preflight (missing, invalid frontmatter, model/effort off config);
- a call with an alias `model` or a model that differs from config;
- a non-Opus role resolving to Opus (guardrail);
- a different jev agent than the task's recorded route without a recorded escalation ("recorded escalation required").

It **warns** (systemMessage) when an Opus or max-effort role is outside `guardrails`. Every jev-* dispatch appends `{task_id, role, model, effort, reason, attempt}` to `~/.claude/jev/routing.log` and prints the `[JEV]` block on stderr. If `config/agents.json` can't be read, the guard fails open with a loud stderr/log message. `JEV_GUARD=off` disables the guard.

After the guard, the older re-route still runs for dispatches without a recorded route: Jev may swap the tier on the same read/write side (depth confidence >= 0.6, never to debugger), and only if the new pick also passes the guard. `[jev:keep]` keeps your pick. `JEV_DISPATCH=apply|shadow|off` controls the re-route only. `jev.py outcomes` / `jev.py label <id|last> ok|too_low|too_high` measure it.

## Context packs (jevpack.py)

`jev.py context` (jevctx: graphify -> graphq -> pack) is the default. `jevpack.py` still works for manual packs: `jevpack.py build --name <n> --task "<task>" --files-from <list>`, then `jevpack.py slice <n> --subtask "<subtask>" > .jev/packs/<n>-<k>.md`, and hand the slice over by path ("First Read `<path>`; open other files only if something is missing"). Don't run both for the same agent: one context system per handoff.

## Browser QA (jev-ultrafast + jev-qa)

`scripts/jevqa.py` pairs browser-use's jev-ultrafast with the `jev-qa` reviewer (Sonnet 5.5, low effort), which reads the screenshots and the report. One-time setup:

```bash
git clone https://github.com/browser-use/jev-ultrafast.git ~/Documents/Tools/jev-ultrafast
cd ~/Documents/Tools/jev-ultrafast && uv sync
```

Run: `uv run --project ~/Documents/Tools/jev-ultrafast python scripts/jevqa.py run <scenario.json> [--out DIR] [--headful]`. The default out dir is `./.jev/qa/<timestamp>/`.

- The QA Chrome is always a throwaway profile on its own debugging port, headless unless `--headful`. It never attaches to your normal Chrome, and it is killed and its profile deleted on exit.
- Typed text comes only from the flow's `values` map (`{"<label regex>": "<value>"}`). Anything unmatched types nothing. Values whose labels look secret are masked in the report.
- Flows whose host isn't in `allow_hosts` (default: localhost only) are refused. A run that navigates off those hosts stops with `left_allowed_hosts`.
- Scenario: `{"base_url", "allow_hosts", "viewports": [{name, width, height, mobile?}], "flows": [{name, path, goal?, values?, max_steps? (25), expect_text?, expect_url?, viewports?, max_slices? (10), wait_ms? (0), init_script?, wait_for?, wait_for_loader? (true), loader_timeout_ms? (10000), scroll_to?}], "init_script"?, "wait_for_loader"?}`. A flow with no `goal` is capture-only.
- Intro loaders: jevqa waits until the page stops looking busy (still loading, `aria-busy`, all controls inert, or a full-screen overlay), up to `loader_timeout_ms`, and records `loader` (`detected`, `reason`, `waited_ms`, `cleared`). `wait_for` polls a CSS selector (up to 15 s), then `wait_ms` sleeps. `init_script` (scenario or flow; `""` turns it off) runs before navigation. Use it only for loaders that never clear.
- Goal flows run at the flow's first viewport, scroll about 0.8 screens per step, and give one JS `click()` to a disclosure that ignored a real click (logged in `fallback_clicks`; report each one as a bug). Use `scroll_to` unless finding the section is itself the test.

  ```json
  {"base_url": "http://localhost:3002",
   "viewports": [{"name": "desktop", "width": 1440, "height": 900}, {"name": "mobile", "width": 390, "height": 844, "mobile": true}],
   "flows": [{"name": "home", "path": "/", "wait_for": "h1", "max_slices": 4},
             {"name": "faq", "path": "/", "viewports": ["desktop"], "scroll_to": "#faq", "max_steps": 8,
              "goal": "Expand the FAQ question 'How are prompts checked?'. Do not submit any form."}]}
  ```
- Output: `<flow>-<viewport>-NN.png` slices, a `<flow>-<viewport>-sheet.jpg` contact sheet per flow and viewport, `report.json` and `report.md`. The checks cover overflow, alt text, unnamed controls, text under 11px, `expect_text`/`expect_url`, console errors and the loader. Goal flows add `untouched_selects`, `agent_viewport`, `scroll_to`, `fallback_clicks` and `initial_labels`/`final_labels` (read `initial_labels` first when a flow is BLOCKED at 0 steps).

## Rules

- Don't route trivial one-step requests that you can do yourself. This skill is for multi-part work.
- Show the user the routing table (subtask -> route, agent, model/effort, and why) before spawning more than 3 subagents or starting a Workflow.
- No silent substitution, ever. A missing or unusable agent stops the task. The only exception is a fallback explicitly configured in `config/agents.json`, and preflight rejects those today.
- `jev.py report [--days 7] [--json]` summarizes the gate, prompt-router and routing logs. `~/.claude/jev/routing.log` shows the model and effort of every jev dispatch, which makes accidental Opus use visible.
- After a routed agent completes, label it: `jev.py label <id|last> ok|too_low|too_high`.
- The API key is read from `TYPESAFE_API_KEY`, then `~/.config/typesafe/.env`, then `.env` at the repo root. Never print it.

## Rollback

`config/agents.pre-vnext.json` is the routing from before vNext: every agent on `claude-opus-5-5`, advisor at max. To roll back the model split: copy it over `config/agents.json`, run `python scripts/sync_agents.py`, re-run `./install.sh`, then restart Claude Code. **Keep** the registry fix (quoted descriptions), preflight, the dispatch guard and routing.log: they read whichever config is active, so they keep validating and logging after a rollback. `JEV_GUARD=off` is the emergency switch for the guard alone.
