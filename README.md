# jev-harness

**A Claude Code harness where [Jev](https://docs.typesafe.ai) makes the small per-turn decisions.** A tested proof of concept of the ideas in *Jev Engineering for Coding Agents* (a September 2026 synthesis of design notes by TypeSafe's founder): permissions, skill routing, conditional instructions, effort-tiered subagents, and shared retrieval.

Jev is TypeSafe's decision model. It doesn't write code or text. It returns typed answers (a choice, a score, or a yes/no probability) to narrow questions. This repo uses it as the decision layer next to Claude: **code owns the policy, Jev answers the questions, Claude does the work.**

> Full write-up with method, numbers, and limitations: **[docs/REPORT.md](docs/REPORT.md)**

## What's in it

| Component | Hook / entry point | What it does |
|---|---|---|
| **Permission gate** | `PreToolUse` | Hard rules deny or ask instantly. Routine dev commands pass silently in about 45 ms. Only the unclear middle goes to Jev (about 1.1 s), which reads a script's contents before it runs. Never auto-approves. |
| **Prompt router** | `UserPromptSubmit` | Suggests at most one fitting skill (the two-request design from TypeSafe's skill-suggestion cookbook), and injects your own instructions only when their condition holds. Skips short replies, harness notices (agent-finished messages, bash input) and slash commands, except `/goal`, whose text it routes. |
| **Effort tiers** | `agents/jev-*.md` | Nine subagents on one model (`claude-opus-5-5`). **Effort rule:** high / xhigh / max / ultracode only for decisions, architecture and orchestration; medium for review and checks; code at low by default. Planners (read-only): analyst (high), advisor (max), architect (max, plans only). Reviewer: medium. QA (browser, read-only): low. Coders: builder (**low**, default), engineer (medium, big multi-file work), debugger (high, only when stuck). Hard work runs as plan first, then implement at low. |
| **Dispatch router** | `PreToolUse` (`Agent\|Task`) | Re-routes jev-* subagent dispatches with the same policy as `jev.py route`; swaps the tier only on the same read/write side at depth confidence >= 0.6, never to debugger. `[jev:keep]` opts out, `JEV_DISPATCH=shadow\|off`. `jev.py outcomes` / `label` measure how the dispatches went. |
| **Router CLI** | `jev.py route / stuck / dedupe` | Jev picks the tier, effort, and whether a task can run in parallel. It escalates when an agent is stuck (never across the read/write boundary) and catches duplicate subgoals. |
| **Context packs** | `jevpack.py build / slice` | Gathers code once as function/class chunks. Jev scores every chunk per subtask: full, outline, or hidden. |
| **Browser QA** | `jevqa.py run` + `agents/jev-qa.md` | Drives local or staging pages with jev-ultrafast in a throwaway Chrome profile, fills forms only with fake scenario values, and saves viewport screenshot slices plus DOM and console checks. The `jev-qa` agent (low effort) reviews them. Needs `git clone https://github.com/browser-use/jev-ultrafast.git ~/Documents/Tools/jev-ultrafast && cd ~/Documents/Tools/jev-ultrafast && uv sync`. |
| **Report** | `jev.py report` | Summarizes the logs: what the gate flagged, slow prompts, Jev errors, routing decisions. |

## Headline results

| | Result |
|---|---|
| Gate, fresh held-out set (64 commands, never tuned on) | **64/64**: 12/12 harmful denied, **0/40** false denies, 30/30 routine silent |
| Gate, end to end in `bypassPermissions` mode | deny honored (desktop app and headless `claude -p`) |
| Routing, untouched final set (50 tasks) | **47/50** acceptable (94%); **0/49** read-only tasks given edit tools across all sets |
| Conditional instructions (24 prompts) | precision 1.00, recall 0.92 |
| Skill suggestion (12 prompts, 95 installed skills) | 12/12 (depends on the skills you have installed) |
| Context packs on Click 8.1.7 (3 questions) | 16–32% faster, equal answer quality (blind-graded), **+7–25% tokens** with outline-first slices |
| Jev cost | about $0.00002 per gate or route call, about $0.007 per 480-chunk slice |

The report also documents an overfitting episode: a routing fix that scored 90% on a reused test set scored 62% on a fresh one. It covers how that was fixed and re-tested ([section 5.6](docs/REPORT.md#56-round-2-fixing-the-gaps-and-re-testing)).

## Install

Requires Claude Code, Python 3.9+, and a [TypeSafe API key](https://console.typesafe.ai).

```bash
git clone https://github.com/dansya-arsana/jev-harness.git && cd jev-harness
cp .env.example .env            # put your TYPESAFE_API_KEY in it
./install.sh --hooks            # links the skill + agents into ~/.claude and registers both hooks
```

**Windows:** put the key in `~/.config/typesafe/.env` (the installer creates it), then run
`powershell -ExecutionPolicy Bypass -File install.ps1`. It junctions the skill, copies the agents, registers the
hooks with your full `python.exe` path (so no `python3` alias is needed), and adds the required-stack rules from
[`config/global-rules.md`](config/global-rules.md) to `~/.claude/CLAUDE.md` between markers. `jevqa.py` finds
Chrome or Edge on its own; set `JEVQA_CHROME` to override.

`install.sh` links everything from this repo into `~/.claude`. It moves any real files it would replace into `~/.claude/backups/` and backs up `settings.json` before editing it. It's safe to re-run. `./uninstall.sh` removes the links and hook entries and leaves your rules and logs alone.

Then:

- **Your own rules:** edit `~/.claude/jev/conditions.json` (seeded from `config/conditions.example.json`). Each rule is a yes/no question about the prompt, plus the text or file to inject when it holds. A rule with `cwd_prefix` (plus optional `paths` aliases) also fires without asking Jev when the session runs inside that folder, or when at least 3 of the session's last 30 tool calls touched one of those paths.
- **Use the orchestrator:** type `/jev-orchestrator` or "split this into subagents" in Claude Code.
- **Check on it:** `python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py report`.
- **Switches:** `JEV_GATE=off` keeps only the hard denies; `JEV_ROUTER=off` disables the router; `JEV_ROUTER_SLASH=loop,...` routes the text of more slash commands besides `/goal`.

If your `settings.json` remaps the model aliases (`ANTHROPIC_DEFAULT_OPUS_MODEL`, etc.), keep the agent files on a full model ID, as they ship.

## Test and evaluate

```bash
python3 skill/jev-orchestrator/hooks/tests/test_permission_gate.py   # 24 unit tests
python3 skill/jev-orchestrator/scripts/tests/test_route_policy.py    # 20 offline policy tests
python3 skill/jev-orchestrator/hooks/tests/test_prompt_router.py     # 29 unit tests
python3 skill/jev-orchestrator/scripts/tests/test_jevqa.py           # 36 offline browser-QA tests
python3 skill/jev-orchestrator/hooks/tests/test_dispatch_router.py   # 15 offline dispatch-router and outcomes tests
python3 evals/run_evals.py all            # labeled evals -> evals/results/*.json
python3 evals/run_evals.py gate-heldout2   # fresh held-out gate set
python3 evals/run_evals.py route-heldout3  # untouched final routing set
```

Gate tests and evals only pipe JSON describing a command into the hook; no evaluated command is ever executed. Jev-dependent tests skip when Jev is unreachable.

## Layout

```
skill/jev-orchestrator/   SKILL.md, scripts/ (jev.py, jevpack.py, jevlib.py), hooks/ (+ tests/)
agents/                   jev-scout, -analyst, -advisor, -reviewer, -builder, -engineer, -debugger, -architect
config/                   conditions.example.json
evals/                    labeled cases, held-out cases, run_evals.py, results/
docs/REPORT.md            research write-up
```

## Credits

The design ideas come from *Jev Engineering for Coding Agents*, an independent synthesis of design notes by Diogo Almeida (TypeSafe). This project is independent and not affiliated with or endorsed by TypeSafe or Anthropic. The skill-suggestion design follows [TypeSafe's cookbook](https://docs.typesafe.ai/cookbooks/skill_suggestion).

MIT License.
