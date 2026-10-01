# jev-harness

**A Claude Code harness where [Jev](https://docs.typesafe.ai), TypeSafe's decision model, gates tool calls and routes work to nine `jev-*` subagents.** Jev does not write code or text: it returns typed answers (a choice, a score, a yes/no probability) to narrow questions. Claude does the work and code holds the policy. It needs Claude Code, Python 3.9+ and a [TypeSafe API key](https://console.typesafe.ai).

It is a tested proof of concept of the ideas in *Jev Engineering for Coding Agents* (a September 2026 synthesis of design notes by TypeSafe's founder): permissions, skill routing, conditional instructions, effort-tiered subagents, and shared retrieval. Full write-up with method, numbers and limitations: **[docs/REPORT.md](docs/REPORT.md)**.

## Quick start

```bash
git clone https://github.com/dansya-arsana/jev-harness.git && cd jev-harness
python3 scripts/onboard.py
```

Windows: `py scripts\onboard.py`, or `powershell -ExecutionPolicy Bypass -File install.ps1` (the wrapper finds a real Python 3.9+ and skips the Microsoft Store `python3` stub). Then **restart Claude Code** (close every session): hooks and agents are loaded at session start, so until then they are registered but not active.

`python3 scripts/onboard.py --dry-run` shows the plan; nothing changes before you confirm. Every file it would replace is backed up first under `~/.claude/backups/jev-onboard-<timestamp>/`, and a manifest at `~/.claude/jev/install-manifest.json` records what it did so `--uninstall` can undo it.

## Which hosts are supported

Onboarding installs the full harness for Claude Code only. For the other hosts it detects them, shows the row below, and installs nothing, because their hook input and agent formats are not verified against this harness. Source for every cell: [docs/jev/host-research.md](docs/jev/host-research.md) (2026-10-01; re-check before relying on a detail).

| Host | Gate hook | Prompt hook | Subagents | Skills | Rules file | Permission/model config | What onboarding installs | Why |
|---|---|---|---|---|---|---|---|---|
| Claude Code | full | full | full | full | full | full | skill, agents, gate, dispatch guard; router and rules block opt-in | |
| Codex | partial (trust review) | partial | partial (TOML) | partial | full | full | nothing yet | hooks must be trusted in `/hooks` before they run; hook input and TOML agent schemas not verified |
| Gemini CLI | partial (other schema) | partial | partial | full | full | partial | nothing yet | hook input schema differs from Claude's; not verified |
| Cursor | partial (other schema) | partial | unverified | unverified | unverified | unverified | nothing yet | hook input schema differs from Claude's; not verified |
| Windsurf | partial (other schema) | partial | unverified | unverified | unverified | unverified | nothing yet | hook input schema differs from Claude's; not verified |
| Cline / Roo | unverified | unverified | unverified | unverified | unverified | unverified | nothing yet | nothing verified |
| ZCode | partial (user-level) | partial | partial (own format) | full | full | unverified | nothing yet | own agent format; hooks need a new session; hook input schema not verified |

`python3 scripts/onboard.py --check` reports which hosts it detects and what each can honestly support. `--host` selects one explicitly.

## Model presets

The routing (model and effort per role) is a choice, not a fixed file. Your choice is stored in `~/.claude/jev/agents.json` and rendered into `~/.claude/agents/jev-*.md`; `config/agents.json` holds only the shipped defaults and is never written by onboarding.

| Preset | Models | Summary |
|---|---|---|
| `balanced` | Opus 5.5 + Sonnet 5.5 | Default. Opus plans and rescues (architect max, debugger high); Sonnet builds, reviews and looks things up. |
| `economy` | Sonnet 5.5 for every role | Cheapest. No Opus. Architect and debugger run on Sonnet at high effort. |
| `max-quality` | Opus 5.5 + Sonnet 5.5 | Same split as balanced with higher efforts on the Sonnet roles. |
| `zai-glm` | whatever your `ANTHROPIC_DEFAULT_OPUS_MODEL` and `..._SONNET_MODEL` remaps point to | For Z.ai (or another third-party base URL). Uses the model IDs from your settings, never aliases. |
| `legacy-all-opus` | Opus 5.5 for every role | The old pre-vNext routing. Needs `--allow-opus all --allow-max jev-advisor`. |
| `custom` | your choice | Starts from balanced; asks per role for a model key and effort. |

Overrides, all repeatable:

- `--set ROLE=KEY[:EFFORT]`, for example `--set builder=sonnet:low`
- `--model-id KEY=ID`, for example `--model-id work=glm-5.3`
- `--allow-opus ROLE|all` and `--allow-max ROLE|all` lift the guardrails: Opus is reserved for architect and debugger, max effort for architect. The warning states the cost.

`--list-presets` prints the presets; `--show-routing` prints the active routing, or what `--preset` and `--set` would produce, without writing anything. Full model IDs only: aliases such as `opus`, `sonnet`, `haiku`, `fable` are rejected, because an alias follows your settings remaps.

## Safe mode and bypass mode

| | safe (default) | bypass |
|---|---|---|
| What Claude asks | Claude Code's normal prompts | Nothing: every tool call runs without asking |
| What the gate does | Hard rules deny, risky commands "ask", routine commands pass, unclear ones go to Jev | Hard rules still deny; every "ask" becomes a silent pass |
| Changes in `settings.json` | only our hook entries | also `permissions.defaultMode = "bypassPermissions"` |
| Switch | `python3 scripts/onboard.py --mode safe` | `python3 scripts/onboard.py --mode bypass` |

Which to pick:

| Situation | Choice |
|---|---|
| Anthropic account, normal use | `balanced` + safe |
| Cheapest | `economy` |
| Best output | `max-quality` |
| Z.ai through `ANTHROPIC_BASE_URL` | `zai-glm` |
| Unattended runs | bypass (the hard-deny list stays enforced) |

Bypass needs two confirmations: typing the word `bypass`, then a separate "Write these keys?" answer (non-interactive runs need `--confirm-bypass` and `--yes`). `skipDangerousModePermissionPrompt` is written only with `--skip-bypass-prompt`, which has its own answer. `bypassPermissions` is written only after the installed gate command denies a harmless canary in bypass mode, and `settings.json` is restored if that fails.

Honest gaps in bypass mode:

- Everything the gate would "ask" about (force push, `reset --hard`, `clean -f`, `sudo`, system-path writes, cron/launchd, terraform/kubectl/helm, recursive delete outside the project, edits to `.env`, key files and `~/.claude/settings.json`) runs silently and is only logged.
- Only Bash and file edits are inspected. PowerShell, MCP and web tools are not.
- An agent can edit `~/.claude/settings.json` to drop the gate for later sessions. `--check` reports that state as unprotected bypass.
- Managed settings can disable bypass entirely.

What still gets denied in bypass: piping downloaded code into a shell or interpreter, reading secrets plus sending data over the network, recursive delete or chown of home, `/` or system folders, disk formatting and raw writes, fork bombs, disabling macOS protections, and anything Jev denies (that needs a key and network).

## Verify

```bash
python3 scripts/onboard.py --check                 # environment, hosts, settings, gate self-test, staleness
python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py preflight --all
python3 scripts/onboard.py --e2e                   # optional: asks real Claude to run a harmless canary
python -m unittest discover -s scripts/tests -p "test_*.py"
```

- `--check` writes nothing. Its gate self-test pipes three commands into the registered gate command: a download-and-run canary must be denied, `git status` must pass silently, `git reset --hard HEAD` must ask (or pass silently under bypass).
- `jev.py preflight` checks the agent files only (see Components); the live agent registry loads at session start.
- `--e2e` runs `claude -p` against a canary and reports PASS, FAIL, INCONCLUSIVE or SKIPPED. It needs the real home.
- `--offline` skips network calls, `--online` adds one minimal Jev call, `--no-tests` skips the offline test run after installing.

## Troubleshooting

- **Agents missing or "Agent type not found".** An unquoted `: ` or ` #` inside a frontmatter value makes Claude Code silently skip the agent. Run `jev.py preflight`, then restart Claude Code.
- **Alias remaps or a third-party base URL** (`ANTHROPIC_DEFAULT_*_MODEL`, `ANTHROPIC_BASE_URL` pointing at Z.ai): use `--preset zai-glm`. Never pass aliases; they follow your remaps.
- **Hooks not firing.** Restart Claude Code, run `/hooks` and check the registered commands, and check that the interpreter path in each command exists (`python3 scripts/onboard.py --check` lists them).
- **Every Bash/Edit call is blocked with "can't open file ... permission_gate.py".** A registered hook points at a script that no longer exists (for example after moving the clone). Re-run onboard.py from the new clone, or run `--uninstall`, or restore the backup at `~/.claude/backups/jev-onboard-*/.claude/settings.json`.
- **Codex.** Its hooks must be trusted through `/hooks` before they run. Onboarding installs nothing for Codex.
- **Windows.** `python3` is usually the Microsoft Store stub: use `py` or the `install.ps1` wrapper. Avoid running from a venv, or pass `--python PATH` with a full interpreter path.

## Uninstall

```bash
python3 scripts/onboard.py --uninstall
```

It reverts only what the manifest records and what has not changed since: our hook entries and the `defaultMode` or skip-prompt keys it set, the rules block, the agent files and `~/.claude/jev/agents.json`, then the skill link, then the manifest (copied to the backup folder first). If Claude Code is running, the skill link is kept until you restart and run it again (`--force` overrides). Left in place: `conditions.json`, logs, backups, and your TypeSafe key file. Anything you edited afterwards is left alone or moved to the backup folder, never deleted.

The pre-onboarding scripts remain in `scripts/legacy/` and are run through `install.sh --legacy` (or `install.ps1 -Legacy`); they write no manifest. `install.sh --hooks` is accepted and maps to the components `skill,agents,gate,dispatch,router`.

## What's in it

| Component | Hook / entry point | What it does |
|---|---|---|
| **Permission gate** | `PreToolUse` | Hard rules deny or ask instantly. Routine dev commands pass silently in about 45 ms. Only the unclear middle goes to Jev (about 1.1 s), which reads a script's contents before it runs. Never auto-approves. |
| **Prompt router** (opt-in) | `UserPromptSubmit` | Suggests at most one fitting skill (the two-request design from TypeSafe's skill-suggestion cookbook), and injects your own instructions only when their condition holds. One Jev call and extra tokens per prompt. Skips short replies, harness notices (agent-finished messages, bash input) and slash commands, except `/goal`, whose text it routes. |
| **Rules block** (opt-in) | `~/.claude/CLAUDE.md` | The required-stack rules from [`config/global-rules.md`](config/global-rules.md), added between `jev-harness:begin` and `jev-harness:end` markers. |
| **Agents + routing config** | `agents/jev-*.md`, `config/agents.json` | Nine subagents. `config/agents.json` holds the shipped defaults for model, effort, write capability, fallbacks (all `null`: no silent substitution), context policy, escalation table and guardrails; `scripts/sync_agents.py` writes the agent frontmatter from a config, and onboarding renders your preset the same way. Shipped (balanced): **Opus 5.5** decides and rescues: architect (max, read-only, plans only), debugger (high). **Sonnet 5.5** executes, inspects and verifies: builder (**low**, default coder), engineer (medium), reviewer (medium), analyst (high), advisor (high), QA (low), scout (low). |
| **Preflight** | `jev.py preflight` | Before any task: every required agent file exists, its frontmatter parses (strict, no PyYAML needed), name/model/effort match the config, tools match the write flag, no fallbacks. Fails with `JEV PREFLIGHT FAILED ... No fallback agent was spawned.` (Files only: the live registry loads at session start, so restart Claude Code after fixes.) |
| **Dispatch guard + router** | `PreToolUse` (`Agent\|Task`) | Denies a jev-* dispatch whose agent fails preflight, that passes an alias or off-config `model`, that would run a Sonnet role on Opus, or that swaps a routed agent without a recorded escalation. Logs `{task_id, role, model, effort, reason, attempt}` to `~/.claude/jev/routing.log`. Then the older Jev re-route may swap the tier on the same read/write side (depth confidence >= 0.6, never to debugger) for unrouted dispatches. `JEV_GUARD=off`, `JEV_DISPATCH=shadow\|off`, `[jev:keep]`. |
| **Router CLI** | `jev.py route / escalate / plan / handoff / context` | `route` picks the fast path (one-file, clear, no architecture/schema/persistence/API/concurrency change: builder directly) or the planned route (architect plans, orchestrator persists with `plan save`, builder implements), with model and effort per step from config. `escalate` routes by failure category (invalid plan replans directly; guardrails cap replans and debugger attempts). `handoff validate` checks plan / completion / failure / review / QA artifacts against `config/schemas/`. `report lint` checks the terse STATUS report. `context` wraps `jevctx.py` (graph-first packs, fails soft). `stuck` and `dedupe` work as before. |
| **Context packs** | `jevpack.py build / slice` | Gathers code once as function/class chunks. Jev scores every chunk per subtask: full, outline, or hidden. |
| **Browser QA** | `jevqa.py run` + `agents/jev-qa.md` | Drives local or staging pages with jev-ultrafast in a throwaway Chrome profile, fills forms only with fake scenario values, and saves viewport screenshot slices plus DOM and console checks. The `jev-qa` agent (low effort) reviews them. Needs `git clone https://github.com/browser-use/jev-ultrafast.git ~/Documents/Tools/jev-ultrafast && cd ~/Documents/Tools/jev-ultrafast && uv sync`. `jevqa.py` finds Chrome or Edge on its own; set `JEVQA_CHROME` to override. |
| **Report** | `jev.py report` | Summarizes the logs: what the gate flagged, slow prompts, Jev errors, routing decisions. |

Default components are the skill, agents, gate and dispatch guard. After installing:

- **Your own rules:** edit `~/.claude/jev/conditions.json` (created empty when you choose the router; an example is in `config/conditions.example.json`). Each rule is a yes/no question about the prompt, plus the text or file to inject when it holds. A rule with `cwd_prefix` (plus optional `paths` aliases) also fires without asking Jev when the session runs inside that folder, or when at least 3 of the session's last 30 tool calls touched one of those paths.
- **Use the orchestrator:** type `/jev-orchestrator` or "split this into subagents" in Claude Code.
- **Check on it:** `python3 ~/.claude/skills/jev-orchestrator/scripts/jev.py report`.
- **Switches:** `JEV_GATE=off` keeps only the hard denies; `JEV_ROUTER=off` disables the router; `JEV_ROUTER_SLASH=loop,...` routes the text of more slash commands besides `/goal`.

To go back to the old all-Opus routing, run `python3 scripts/onboard.py --preset legacy-all-opus --allow-opus all --allow-max jev-advisor`. Preflight, the dispatch guard and routing.log stay on and validate against whichever config is active.

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

## Test and evaluate

```bash
python -m unittest discover -s scripts/tests -p "test_*.py"          # onboarding tests, offline, temporary home
python3 skill/jev-orchestrator/hooks/tests/test_permission_gate.py   # 24 unit tests
python3 skill/jev-orchestrator/scripts/tests/test_route_policy.py    # 20 offline policy tests
python3 skill/jev-orchestrator/hooks/tests/test_prompt_router.py     # 29 unit tests
python3 skill/jev-orchestrator/scripts/tests/test_jevqa.py           # 36 offline browser-QA tests
python3 skill/jev-orchestrator/hooks/tests/test_dispatch_router.py   # 31 offline dispatch guard, re-route and outcomes tests
python3 skill/jev-orchestrator/scripts/tests/test_vnext.py           # 43 offline vNext tests (preflight, fast path, escalate, plan, handoffs)
python3 scripts/sync_agents.py --check                               # agent frontmatter matches config/agents.json
python3 evals/run_evals.py all            # labeled evals -> evals/results/*.json
python3 evals/run_evals.py gate-heldout2   # fresh held-out gate set
python3 evals/run_evals.py route-heldout3  # untouched final routing set
python3 evals/run_evals.py route-fastpath  # vNext fast path vs planned route (12 cases)
```

Gate tests and evals only pipe JSON describing a command into the hook; no evaluated command is ever executed. Jev-dependent tests skip when Jev is unreachable. The onboarding tests never touch your real `~/.claude`.

## Layout

```
install.sh, install.ps1, uninstall.sh   thin wrappers that find Python 3.9+ and run scripts/onboard.py
skill/jev-orchestrator/   SKILL.md, scripts/ (jev.py, jevpack.py, jevlib.py), hooks/ (+ tests/)
agents/                   jev-scout, -analyst, -advisor, -reviewer, -qa, -builder, -engineer, -debugger, -architect
config/                   agents.json (shipped routing defaults), agents.pre-vnext.json (old all-Opus routing),
                          presets/ (model presets), schemas/ (handoff contracts), conditions.example.json,
                          global-rules.md (opt-in rules block)
scripts/onboard.py        the installer: check, install, modes, uninstall
scripts/onboard_*.py      env detection, presets, apply engine, verification
scripts/sync_agents.py    writes agent frontmatter from a routing config
scripts/legacy/           the pre-onboarding shell and PowerShell installers
scripts/tests/            onboarding tests
docs/jev/                 vNext plan, onboarding plan, host research, routing baseline
evals/                    labeled cases, held-out cases, run_evals.py, results/
docs/REPORT.md            research write-up
```

## Credits

The design ideas come from *Jev Engineering for Coding Agents*, an independent synthesis of design notes by Diogo Almeida (TypeSafe). This project is independent and not affiliated with or endorsed by TypeSafe or Anthropic. The skill-suggestion design follows [TypeSafe's cookbook](https://docs.typesafe.ai/cookbooks/skill_suggestion).

MIT License.
