# JEV routing baseline (before vNext)

Captured 2026-10-01 from a live session that was running the Agent Ranch project.

## What the session saw

The Agent tool listed only five JEV agents: `jev-advisor`, `jev-analyst`, `jev-architect`, `jev-engineer`, `jev-scout`.
Spawning `jev-builder` failed with "Agent type 'jev-builder' not found", and `jev-reviewer`, `jev-debugger` and `jev-qa` were
missing too. All nine definition files existed in `~/.claude/agents/` and were identical to `agents/` in this repository.

## Root cause

It is not a registry problem. Four definition files have invalid YAML frontmatter, so Claude Code skips them when it loads agents.
Their `description:` values contain an unquoted colon followed by a space, for example
`description: Default coder for Jev-routed work (low effort): small and ordinary edits`. YAML reads the second colon as a nested
mapping and fails ("mapping values are not allowed here"). The five agents that did register contain no such colon.

| Agent | Definition exists | Frontmatter parses | Registered in the session |
|---|---|---|---|
| jev-advisor | yes | yes | yes |
| jev-analyst | yes | yes | yes |
| jev-architect | yes | yes | yes |
| jev-builder | yes | no | no |
| jev-debugger | yes | no | no |
| jev-engineer | yes | yes | yes |
| jev-qa | yes | no | no |
| jev-reviewer | yes | no | no |
| jev-scout | yes | yes | yes |

## Model and effort before the fix

Every agent file pinned `model: claude-opus-5-5`, so every worker ran on Opus. Efforts were: architect max, advisor max,
analyst high, debugger high, engineer medium, reviewer medium, builder low, qa low, scout low. The orchestrator skill told
callers to spawn without a `model` argument so that the pinned Opus applied.

## Consequences observed

- The orchestrator substituted `jev-engineer` for the missing builder and `jev-analyst` for the missing reviewer, silently.
- The architect is read-only, so its plan had to be saved by hand (or by spawning a write-capable agent).
- A reviewer attempt through a different plugin agent failed because that agent used the `haiku` alias, which the user's
  provider setup mapped to a model the plan did not include.
- Graph-first retrieval and terse reporting only happened because each prompt repeated the instruction.

## Fix applied in this repository

`config/agents.json` is now the single source of truth for model, effort, write capability, fallbacks, context policy and the
escalation table. `scripts/sync_agents.py` rewrites the agent files from it (descriptions are always quoted), and a lint test
fails if any agent file does not parse. Further phases are tracked in `docs/jev/vnext-plan.md`.

## vNext implementation notes and deviations from the plan

- **Registered / Callable in the preflight table** are inferred from the file check. Nothing outside the session can query
  the live Agent registry, and Claude Code loads agents only at session start. Preflight therefore validates the files and
  tells the user to restart Claude Code after any fix. The dispatch guard repeats the same per-agent check on every jev-*
  dispatch.
- **Project agents shadow user agents.** Preflight and the guard check `<project>/.claude/agents/<name>.md` first when it
  exists, because Claude Code prefers it.
- **The substitution check needs a task marker.** The guard only knows which task a dispatch belongs to when the prompt carries
  `[jev:task=<id>]` (`route` returns it as `task_marker`). Dispatches without a marker are still validated (file, model,
  guardrails) and logged, but they are not compared with a recorded route.
- **The `[JEV]` block goes to stderr.** A PreToolUse hook's stdout must be JSON, so the block is printed on stderr (visible in
  verbose/transcript mode), and the full record goes to `~/.claude/jev/routing.log` (JSON lines). Guard warnings are shown
  to the user as a `systemMessage`.
- **Planned route implementer.** Every non-fast-path write task gets `plan_first` (architect plans, builder implements). The
  `tier` field still reports the policy's effort class (builder / engineer / architect), so the existing route evals stay
  comparable. Unknown-root-cause bugs keep the analyst as planner (investigation, not design).
- **Fast-path classifier.** All plan criteria apply, plus two conservative extras: low stakes (no auth, secrets, payments or
  network keywords, `high_stakes` < 0.4) and small depth (< 1.5). The architecture / schema / persistence / public-API /
  concurrency checks combine Jev's signals with keyword patterns on the task text, so a false "not fast" only costs one
  architect call.
- **Escalation ladder details.** `test_failure` lets the same coder retry twice, then steps up one rung.
  `implementation_complexity` from the engineer goes to the debugger, and from the debugger to an architect replan (the old
  builder -> engineer -> debugger -> replan ladder). Guardrail hits return `next_agent: orchestrator` (stop and report).
- **QA contract.** The plan names a QA handoff but gives no shape. `config/schemas/qa.schema.json` defines verdict, checks and
  findings.
- **Phase 7 (graph-first context)** is `jevctx.py`, built separately. `jev.py context` is only a thin wrapper that fails soft
  (`graph_status: unavailable`).

## Verification trace (2026-10-01)

After the fix, a live session registered all nine agents without a restart (the four previously skipped agents appeared once
their files parsed). One real task went through the new cheap-first path:

1. `jev.py route` classified a one-file `uninstall.sh` fix as the fast path: no architect, no Opus.
2. `jev-builder` (claude-sonnet-5-5, low) made the change in 8 s on about 13k tokens and reported in the terse protocol.
3. `jev-reviewer` (claude-sonnet-5-5, medium) reviewed it independently and returned pass with three minor notes.
4. `~/.claude/jev/routing.log` shows role, model, effort and attempt for each dispatch.
