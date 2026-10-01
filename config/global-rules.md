<!-- jev-harness:begin (managed by jev-harness scripts/onboard.py; edit config/global-rules.md in the repo) -->
# Required stack on this machine: Jev + caveman + graphify + jev-ultrafast (bora)

Use all four together in every project. They are not optional.

1. **Jev decides, Claude does the work.** The jev-harness hooks already gate every tool call and
   route every prompt. For any multi-step or non-trivial task, load the `jev-orchestrator` skill and
   run `jev.py route` before choosing a subagent tier or effort; use `jev.py stuck` / `dedupe` when a
   subagent stalls or subgoals overlap. Do not pick tiers by hand when Jev can answer.
2. **caveman** stays on (SessionStart hook). Keep replies terse; code, commits, PRs and docs stay normal prose.
3. **graphify first for code questions.** In a repo with `graphify-out/`, query the graph before
   reading raw files. In a new or unfamiliar codebase, build the graph (`/graphify`) before broad exploration.
4. **Browser work goes through jev-ultrafast (bora)**, not screenshot-driven clicking, because it is
   faster and uses fewer tokens:
   - Public-site research/tasks: `powershell -File ~/Documents/Tools/jev-ultrafast/scripts/jev-run.ps1 --url URL --goal 'GOAL' [--text 'field=value']`
     (macOS/Linux: `scripts/jev-run.sh`). Public sites only; never personal accounts.
   - Local/staging QA: the `jev-qa` agent with `jevqa.py run`.
   - Fall back to the built-in browser only for what bora cannot do (logged-in sessions the user
     drives, visual checks bora cannot capture), and say why.

**Improve and share.** When you find a fix or improvement for jev-harness, jev-ultrafast-bora, or
these rules, commit it on a branch in that repo and push to GitHub (dansya-arsana) so others get it.
Ask before pushing to `main` directly.
<!-- jev-harness:end -->
