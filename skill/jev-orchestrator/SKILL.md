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

Every tier runs `claude-opus-5-5`, pinned in the agent file.

| tier | subagent_type | effort | ladder | for |
|---|---|---|---|---|
| scout | `jev-scout` | low | read | lookups, quick read-only answers |
| analyst | `jev-analyst` | high | read | investigation, root-cause finding (decision thinking, no edits) |
| advisor | `jev-advisor` | max | read | design questions that only need a recommendation |
| architect | `jev-architect` | max | plan | the implementation plan for design, hard or high-stakes work; never edits |
| reviewer | `jev-reviewer` | medium | read | review and checks of finished work |
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
   - `via: subagent`: spawn `subagent_type` **without a `model` parameter**. The agent files pin `claude-opus-5-5`. Passing an alias (`opus`/`sonnet`/`haiku`) would override that, and if your settings remap aliases (for example `ANTHROPIC_DEFAULT_OPUS_MODEL`) the subagent would run on whatever the alias points to. Put everything the subagent needs in the prompt, because it doesn't see this conversation. Ask for a short result: what changed, where, and anything uncertain.
   - `via: workflow` (tier `ultracode`, `subagent_type: null`): don't spawn one subagent. Load the `workflow-authoring` skill, then author and run a Workflow script that fans out agents and adversarially verifies their results. `workflow_shape` (understand / investigate / review read-only; audit / migrate write) and `workflow_hint` suggest a structure. In the script's `agent()` calls, **always pass `opts.model: 'claude-opus-5-5'`**: Workflow agents otherwise inherit the session's model alias, which follows any alias remapping in your settings. Never pass an alias. Pass `opts.effort` set to the returned `effort` (the worker tier's effort, see `worker_tier`). `workflow_shape` `investigate` and `understand` are read-only: no writer agents. Writers must never share files.
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

## Rules

- Don't route trivial one-step requests. Just do them. This skill is for multi-part work.
- Show the user the routing table (subtask -> tier/effort, and why) before spawning more than 3 subagents or starting an ultracode Workflow.
- `jev.py report [--days 7] [--json]` summarizes the gate, prompt-router and routing logs, with a "worth a look" list (Jev errors, slow prompts, commands the gate keeps asking about). Run it when the user asks how the Jev hooks are doing or wants to tune thresholds.
- Every decision is logged to `~/.claude/jev/decisions.jsonl`. When a routing turns out wrong, say so, because that log is how the thresholds get tuned.
- The API key is read from `TYPESAFE_API_KEY`, then `~/.config/typesafe/.env`, then `.env` at the root of this repo. Never print it. Task text is redacted before it is sent or logged.
- If the helper returns `{"error": ..., "fallback": ...}` (exit 1), don't guess Jev's answer. Follow `fallback`, use your own judgment, and tell the user Jev was unavailable.
