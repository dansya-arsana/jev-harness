# jev-harness

**A Claude Code harness where [Jev](https://docs.typesafe.ai) makes the small per-turn decisions.** A tested proof of concept of the ideas in *Jev Engineering for Coding Agents* (a September 2026 synthesis of design notes by TypeSafe's founder): permissions, skill routing, conditional instructions, effort-tiered subagents, and shared retrieval.

Jev is TypeSafe's decision model. It doesn't write code or text. It returns typed answers (a choice, a score, or a yes/no probability) to narrow questions. This repo uses it as the decision layer next to Claude: **code owns the policy, Jev answers the questions, Claude does the work.**

> Full write-up with method, numbers, and limitations: **[docs/REPORT.md](docs/REPORT.md)**

## What's in it

| Component | Hook / entry point | What it does |
|---|---|---|
| **Permission gate** | `PreToolUse` | Hard rules deny or ask instantly. Routine dev commands pass silently in about 45 ms. Only the unclear middle goes to Jev (about 1.1 s), which reads a script's contents before it runs. Never auto-approves. |
| **Prompt router** | `UserPromptSubmit` | Suggests at most one fitting skill (the two-request design from TypeSafe's skill-suggestion cookbook), and injects your own instructions only when their condition holds. Skips slash commands and short replies. |
| **Effort tiers** | `agents/jev-*.md` | Seven subagents on one model (`claude-opus-5-5`) that differ by reasoning effort. Read-only: scout (low), analyst (high), reviewer (medium). Write: builder (medium), engineer (high), debugger (xhigh), architect (max). Plus *ultracode*: orchestrate with a Workflow. |
| **Router CLI** | `jev.py route / stuck / dedupe` | Jev picks the tier, effort, and whether a task can run in parallel. It escalates when an agent is stuck (never across the read/write boundary) and catches duplicate subgoals. |
| **Context packs** | `jevpack.py build / slice` | Gathers code once as function/class chunks. Jev scores every chunk per subtask: full, outline, or hidden. |
| **Report** | `jev.py report` | Summarizes the logs: what the gate flagged, slow prompts, Jev errors, routing decisions. |

## Headline results

| | Result |
|---|---|
| Gate, held-out set (63 commands written independently) | 12/12 harmful commands denied, **0/38** false denies, 6 benign commands got an unnecessary "ask" |
| Gate, end to end in `bypassPermissions` mode | deny honored (desktop app and headless `claude -p`) |
| Routing, held-out set (40 tasks) | 34/40 acceptable tier, **0/12** read-only tasks given edit tools |
| Conditional instructions (24 prompts) | precision 1.00, recall 0.92 |
| Skill suggestion (12 prompts, 95 installed skills) | 12/12 (depends on the skills you have installed) |
| Context packs on Click 8.1.7 (3 questions) | about half the tool calls, 25–38% faster, but **+42–59% tokens** |
| Jev cost | about $0.00002 per gate or route call, about $0.007 per 480-chunk slice |

The context-pack result is mixed on purpose; [the report](docs/REPORT.md#54-context-packs) explains when it helps and when it doesn't.

## Install

Requires Claude Code, Python 3.9+, and a [TypeSafe API key](https://console.typesafe.ai).

```bash
git clone https://github.com/dansya-arsana/jev-harness.git && cd jev-harness
cp .env.example .env            # put your TYPESAFE_API_KEY in it
./install.sh --hooks            # links the skill + agents into ~/.claude and registers both hooks
```

`install.sh` links everything from this repo into `~/.claude`. It moves any real files it would replace into `~/.claude/backups/` and backs up `settings.json` before editing it. It's safe to re-run. `./uninstall.sh` removes the links and hook entries and leaves your rules and logs alone.

Then:

- **Your own rules:** edit `~/.claude/jev/conditions.json` (seeded from `config/conditions.example.json`). Each rule is a yes/no question about the prompt, plus the text or file to inject when it holds.
- **Use the orchestrator:** type `/jev-orchestrator` or "split this into subagents" in Claude Code.
- **Check on it:** `python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py report`.
- **Switches:** `JEV_GATE=off` keeps only the hard denies; `JEV_ROUTER=off` disables the router.

If your `settings.json` remaps the model aliases (`ANTHROPIC_DEFAULT_OPUS_MODEL`, etc.), keep the agent files on a full model ID, as they ship.

## Test and evaluate

```bash
python3 skill/jev-orchestrator/hooks/tests/test_permission_gate.py   # 20 unit tests
python3 skill/jev-orchestrator/hooks/tests/test_prompt_router.py     # 17 unit tests
python3 evals/run_evals.py all            # labeled evals -> evals/results/*.json
python3 evals/run_evals.py gate-heldout   # held-out gate set
```

Gate tests and evals only pipe JSON describing a command into the hook; no evaluated command is ever executed. Jev-dependent tests skip when Jev is unreachable.

## Layout

```
skill/jev-orchestrator/   SKILL.md, scripts/ (jev.py, jevpack.py, jevlib.py), hooks/ (+ tests/)
agents/                   jev-scout, -analyst, -reviewer, -builder, -engineer, -debugger, -architect
config/                   conditions.example.json
evals/                    labeled cases, held-out cases, run_evals.py, results/
docs/REPORT.md            research write-up
```

## Credits

The design ideas come from *Jev Engineering for Coding Agents*, an independent synthesis of design notes by Diogo Almeida (TypeSafe). This project is independent and not affiliated with or endorsed by TypeSafe or Anthropic. The skill-suggestion design follows [TypeSafe's cookbook](https://docs.typesafe.ai/cookbooks/skill_suggestion).

MIT License.
