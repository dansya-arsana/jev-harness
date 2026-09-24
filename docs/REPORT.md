# Jev as the decision layer of a coding agent: a tested proof of concept

*September 2026. Proof of concept built and evaluated in Claude Code with `claude-opus-5-5` and `jev-1.13.0` (TypeSafe). Independent work; not affiliated with TypeSafe or Anthropic.*

## 1. Summary

*Jev Engineering for Coding Agents* argues that a coding agent's leverage isn't in its loop or its model. It's in the many small decisions made around every turn: what the model sees, which tool or model handles a subtask, and whether a command may run. It proposes handing those decisions to Jev, a model that returns typed, calibrated answers (a choice, a score, a yes/no probability) instead of text.

We built the parts of that design that can run today as a sidecar to Claude Code, then measured them:

| Idea from the paper | What we built | Result |
|---|---|---|
| Programmable permissions | `PreToolUse` gate: plain rules, then Jev for the unclear middle, reading scripts before they run | Fresh held-out after fixes: **64/64**; 12/12 harmful denied, **0/40** false denies, 30/30 routine commands silent |
| Tool / skill routing | `UserPromptSubmit` two-request skill suggestion | **12/12** on the author's 95-skill install |
| Conditional instructions | Rules injected only when a Jev yes/no about the prompt holds | Precision **1.00**, recall **0.92** (24 prompts) |
| Routing work to the right model / effort | 8 effort tiers on one model; Jev picks tier, ladder, parallelism | Untouched final set: **47/50** (94%); **0** read-only tasks given edit tools across all 160 tasks after round 2 |
| Shared retrieval + visibility ladder | Context packs: chunk once, Jev scores chunks per subtask | Outline-first slices: 16–32% faster, fewer tool calls, same answer quality, **+7–25% tokens** (was +42–59%) on a small, well-known codebase |

The permission gate, conditional instructions and read/write-safe routing work well enough to use every day. Round 2 (section 5.6) fixed the gaps round 1 found and re-tested them on fresh sets. That round also shows how easy it is to overfit a router to a test set, and how we caught it. Context packs still don't save tokens on a small, familiar codebase, but they now cost far less and still save time (sections 5.4 and 5.6). The central idea, rebuilding the context window per turn and pricing cache reuse, can't be done inside Claude Code and remains untested (section 8).

## 2. Background: what the paper proposes

The paper's organizing question is what a coding agent would look like if language models had no KV cache. Today's agents keep an append-only transcript because reusing a cached prefix is cheap and changing anything early is expensive. The paper traces six common behaviors back to that one economic fact:

1. **Routing loses money.** Handing work from a frontier model to a cheaper one, then back, reloads the context twice. With its example list prices ($5/$25 per million input/output tokens for Opus, $3/$15 for Sonnet) and a plausible session shape, pure Opus costs about 4.15 units against 6.19 for the routed path. The routed path costs more because of the context rebuilds, even though the cheaper model is cheaper per token.
2. **Tool schemas crowd the context,** whether or not they are relevant to the turn.
3. **Compaction is query-blind:** it compresses before it knows what the next question will need.
4. **Sub-agents are rare,** because deciding what context to pass in and merge back is hard.
5. **Restarts throw away good state with bad.**
6. **Every built-in "battery" costs context permanently.**

Its answer is to make state explicit and typed, and to ask Jev at each decision point: how visible each chunk should be for this query, whether to reuse the cache, whether a subtask can leave the frontier model, which tool fits, and whether a command should run. The paper also notes that reading and searching dominate token use (roughly two thirds in its estimate), so retrieval is where the biggest savings are.

## 3. What we built

```
 user prompt ──► UserPromptSubmit: prompt_router.py ──► + <skill_relevance>, + <conditional_instruction>s
                     (1-2 Jev requests: skill choice over the roster, needs_skill, one yes/no per rule)

 Claude (main, claude-opus-5-5)
   │  jev.py route "<subtask>"  ──► Jev: depth, breadth, read_only, high_stakes, unknown_cause, design, self_contained
   │        └─► tier + effort + ladder + parallel_safe   (policy in plain code)
   │  jevpack.py build / slice  ──► Jev: one Score per chunk ──► full / outline / hidden slice
   ├──► jev-scout (low) / jev-analyst (high)                         read ladder (Read, Grep, Glob, Bash)
   ├──► jev-builder (medium) / -engineer (high) / -debugger (xhigh) / -architect (max)   write ladder
   └──► ultracode: a Workflow of many agents
        every Bash/Write/Edit ──► PreToolUse: permission_gate.py
                                   1 hard rules (deny/ask) → 2 routine fast path (silent) → 3 Jev (reads scripts)
```

**Design rules we held to:**

- **Code owns the policy; Jev answers narrow questions.** Every threshold lives in Python and can be tuned without re-asking Jev. If Jev errors, every caller falls back to plain rules and never guesses its answer.
- **Independent questions go in one request,** so each decision costs one round trip (Jev bills per input token only, about $0.042 per million).
- **The gate never emits "allow".** Only deny or ask; everything else prints nothing, so it can't weaken Claude Code's own permission modes.
- **Credentials are redacted** before anything is sent to Jev or written to a log.

**Tiers.** All tiers run the same model and differ only by effort. That removes the paper's routing penalty *within* a model: there's no rebuild cost for switching effort between separate subagents, and no quality cliff between tiers. Tasks sit on two ladders. Read-only tasks go to agents that have no edit tools, and escalation never crosses from one ladder to the other.

## 4. Method

All numbers come from `evals/` and can be regenerated with `python3 evals/run_evals.py all`. Result files are in `evals/results/`.

- **Self-written sets:** 65 gate commands, 30 routing tasks, 24 condition prompts and 12 skill prompts, written by the author of the rules. These measure regressions, not generalization.
- **Held-out sets:** each was written by a separate agent told not to read any file in the repo, and given only the label definitions. Round 1 used gate set 1 (63 commands) and routing set 1 (40 tasks), with **no tuning after seeing them**. Round 2 (section 5.6) tuned only on sets already seen, and tested on fresh ones: gate set 2 (64) and routing sets 2 (40) and 3 (50).
- **Gate safety:** commands are only piped to the hook as JSON. Scripts a case refers to are written to a temp directory so the gate can read them, and are never executed.
- **End to end:** the gate was also tested live in `bypassPermissions` mode, both in the Claude Code desktop app and in headless `claude -p`, using a command that would have been harmless if it ran (a missing file sent to a closed local port).
- **Context-pack benchmark:** `jev-analyst` subagents answered the same read-only questions with and without a slice. Token, tool-call and time figures are the Agent tool's own reported numbers. Answer quality was graded **blind**: a separate reviewer saw answers labeled A/B (order swapped on one question) and checked every citation against the source.
- **Environment:** macOS, Python 3.9, Claude Code desktop, `claude-opus-5-5`, `jev-1.13.0`, September 2026.

## 5. Results

### 5.1 Permission gate

| Set | Cases | Accuracy | Harmful blocked | Harmful denied | Benign false deny | Routine silent |
|---|---|---|---|---|---|---|
| Self-written | 65 | 100% | 23/23 | 23/23 | 0/30 | 20/20 |
| **Held-out** | 63 | **88.9%** | **12/12** | **12/12** | **0/38** | 26/29 |

**Latency:** plain rules decide in 31–43 ms at p50 (57 ms at p95). The Jev path is 1.13 s at p50 (1.21 s at p95). On the held-out set, 27 of 63 commands reached Jev.

**Held-out misses** were all on the cautious side:
- Six everyday commands got an unnecessary "ask": `pip install -r requirements.txt`, `docker compose up -d`, `docker build`, a normal `git push`, `rm -rf node_modules dist .next`, and `git stash && git pull --rebase && git stash pop`.
- One command was denied where "ask" was right: `terraform apply -auto-approve`.

That's friction, not a safety failure. The fixes, and a re-test on a fresh set, are in section 5.6.

**End to end:** in `bypassPermissions` mode, the hook's deny blocked the command in both the desktop app and headless `claude -p`, and the model reported the gate's reason back to the user. The gate also blocked the author's own shell commands several times during development when they contained test patterns. Once, Jev judged a Python snippet that *built* an exfiltration-shaped command string unsafe, at 0.81 probability. That's the correct call, and it's inconvenient when writing tests.

**Bugs found while building** (each now has a regression test):
1. **Newlines weren't command separators,** so `ls` followed by a second line was treated as just `ls`.
2. **A text-stripping step for `echo "rm -rf /"`** rebuilt commands from tokens and broke shell punctuation. As a result, the fork-bomb rule silently never matched, and those commands were caught only when Jev happened to say deny. This showed up as a flaky test.
3. **Local `rsync` counted as network traffic,** which caused a false deny on an ordinary file copy.

### 5.2 Routing: tier, effort and ladder

| Set | Tasks | Acceptable tier | Tier choice acceptable¹ | Read-only tasks given edit tools | Ladder violations |
|---|---|---|---|---|---|
| Self-written | 30 | 27/30 (90%) | 29/30 | **0/8** | 0 |
| **Held-out** | 40 | **34/40 (85%)** | 35/40 | **0/12** | 0 |

¹ Counting the tier Jev chose even when the task was kept in the main session.

**Latency** is 1.16 s at p50 per `route` call, about 800 Jev input tokens.

**The safety property held on every case:** no read-only task was ever handed an agent with Edit/Write tools. That was a real bug in the first version, found by a cross-model review and fixed before these evals.

**Miss patterns:**
- **Too eager to orchestrate (3 held-out cases).** Wide, known-approach changes went to *ultracode* where one *engineer* was expected: renaming a Django model everywhere, threading a new proto field through a stack, replacing session auth with OAuth on two clients. The breadth score alone decides ultracode; it should also require that the work can't be batched by one agent.
- **Design tasks kept in the main session (2 self-written, 1 held-out).** Jev chose architect at max effort but judged them "not self-contained". That's defensible, since design choices usually depend on the user's preferences, but it lowers the delegation score.
- **Money makes it high-stakes (1 held-out).** A cents-level rounding bug in a revenue rollup went to architect (max) instead of debugger (xhigh).
- **Over-escalation (1 self-written).** "Write unit tests for the invoice module" went to debugger. That's too much effort.

### 5.3 Prompt router

**Conditional instructions**, using two neutral example rules (AI media generation; staging deploys):

| Prompts | Exact | Precision | Recall |
|---|---|---|---|
| 24 | 23/24 | **1.00** | **0.92** |

- **Negatives it got right:** charts drawn with matplotlib, particle effects and shaders written in code, and an audio *crash* fix. None of these triggered the media rule. An earlier wording of that rule did fire on charts. That was caught in review and narrowed to *AI-generated* media, which is exactly the kind of tuning the paper expects conditional instructions to need.
- **The one miss:** a prompt needing two rules at once ("after deploying to staging, generate an image") fired only one.

**Skill suggestion:** **12/12** on the author's install (95 skills gathered from user, plugin and desktop-app folders, many of them near-duplicates):
- `design-taste-frontend` for a landing page, and `redesign-existing-projects` (not its near-duplicates) for a redesign.
- `data:write-query` for SQL and `data:create-viz` for a chart.
- `pptx`, `pdf` and `engineering:code-review` for decks, forms and reviews.
- Nothing for the five prompts that needed no skill.

This depends on the installed skills and isn't portable. Anyone reproducing it gets results for their own roster.

**Latency** is 1.6 s at p50 and 2.7 s at p95 per prompt (3.6 s max). Slash commands and short replies skip Jev. One real prompt waited 5.6 s when a Jev TLS handshake timed out. The router gave up and passed the prompt through unchanged, as designed.

### 5.4 Context packs

`jevpack build` split Click 8.1.7 (16 files, about 10.1k lines) into 481 chunks, about 87k tokens, in 0.1 s. A `slice` makes one Jev request per ~48k tokens of chunks, taking 2.7 s and about 160k Jev tokens (about $0.007). Each slice showed 17–41 chunks in full and 23–60 as outlines, and hid the other 395–417.

| Codebase | Question | Tokens (without → with) | Tool calls | Time | Blind score (without / with) |
|---|---|---|---|---|---|
| Click 8.1.7 | Value resolution order | 15,462 → 23,101 (+49%) | 3 → 1 | 17.1 → 12.8 s | 9 / 8 |
| Click 8.1.7 | Group dispatch and chaining | 16,994 → 24,064 (+42%) | 5 → 2 | 22.8 → 14.8 s | 9 / 8 |
| Click 8.1.7 | Shell completion | 17,319 → 27,618 (+59%) | 4 → 2 | 27.6 → 17.0 s | 8 / 9 |
| Private Swift game* | Enemy AI | 21,385 → 14,627 (−32%) | 5 → 2 | 21.0 → 16.0 s | not graded |
| Private Swift game* | Wall damage | 25,289 → 14,600 (−42%) | 5 → 2 | 25.6 → 13.2 s | not graded |

\* An early pilot. The slice was **hand-trimmed and pasted** into the prompt, so it was much smaller than a real slice. These rows overstate the savings and are shown only to explain the gap.

**Quality:** all six Click answers were judged equivalent (within 1 point), with **0 factual errors and 92/92 citations correct**. Before the fix, one pilot answer did cite wrong lines, because the agent counted lines from a chunk's header. Slices now print the real line number on every line.

**Why tokens went up:** a real slice is 31–40 KB (about 8–10k tokens with line numbers), and the agent reads all of it. Without a slice, the agent needs only 3–5 targeted searches and reads on a small, well-organized library it likely already knows. So the paper's "retrieval dominates" point holds, but a *static* 8k-token slice isn't cheaper than a smart agent's own retrieval when that retrieval is already cheap.

**When packs do help:**
- **Speed:** about half the tool calls and 25–38% less wall time.
- **Shared slices:** one slice's Jev cost is split across every agent that reads it.
- **Unfamiliar or large code,** where searching is slow.

The token result points to the fix: smaller budgets (3–5k) and outlines by default, with full chunks only on request. That's closer to the paper's visibility ladder applied *per turn* rather than once per subtask.

### 5.5 Cost and overhead

| Operation | Jev input tokens | Cost | Latency |
|---|---|---|---|
| Gate, Jev path | about 500 | about $0.00002 | 1.1 s |
| `route` | about 800 | about $0.00003 | 1.2 s |
| Prompt router (1–2 requests, 95-skill roster) | about 10–20k | under $0.001 | 1.6–2.7 s |
| Context slice (481 chunks) | about 160k | about $0.007 | 2.7 s |

Jev's cost is negligible next to the frontier model it serves. The real cost is **latency on the critical path**: about 2 s added to every non-trivial prompt, and about 1 s to every gray-zone command.

### 5.6 Round 2: fixing the gaps and re-testing

Round 1 left five known problems: gate friction on everyday commands, over-eager Workflow routing, expensive context slices, a false deny when a search *pattern* merely named a secrets file (it blocked our own repo-listing check), and the unclear status of held-out data once you tune on it. For round 2, every fix was tuned only on sets we had already seen, then tested on **newly written sets that no rule was tuned against**.

**Permission gate fixes:**
- Routine rules now cover a project's own `pip install -r`/`-e .`, `uv sync`/`poetry install`, `docker build` and `docker compose`, a plain `git push`, and `rm -rf` of known build folders inside the repo.
- `terraform apply`/`destroy` and `kubectl`/`helm` changes now ask.
- A real bug is fixed: `git` commands only checked the read-only list and returned early, so routine rules like `git pull` never applied.
- Search patterns no longer count as reading a secret, but the search's *file* arguments still do. So searching inside a secrets file and piping the result to the network is still denied.

| Gate set | Round 1 | Round 2 |
|---|---|---|
| Self-written (65) | 65/65 | 65/65 |
| Held-out 1 (63; now used for tuning) | 56/63 (88.9%) | 63/63 |
| **Held-out 2 (64; fresh, never tuned on)** | — | **64/64**: 12/12 harmful denied, 0/40 false denies, 30/30 routine silent |

**Routing: an overfitting episode, stated plainly.** The first round-2 fix added one Jev question ("could one agent do this as a single repeated change?"). It raised held-out 1 from 34/40 to 36/40, but on a **fresh** set it scored only **25/40 (62%)**. The router had been fitted to the first held-out set. The fresh set's misses showed three real problems:
- **Read-only explanations sent to Workflows.** "Explain how a refund flows…" went to ultracode.
- **A stakes question that judged the topic, not the consequences.** A red logout button was scored high-stakes because it "touches auth".
- **Rules in the wrong order.** Unknown-cause bugs that mentioned production went to architect instead of debugger.

We then changed principles, not thresholds for individual cases, tuning only on the 110 tasks already seen:
1. **Unknown cause wins.** A bug with an unknown cause goes to debugger whatever the stakes.
2. **Stakes are judged by consequence.** The question now asks "would a plausible mistake cause a security, data, money or outage problem?". Only clear-cut stakes (≥ 0.85) reach architect; moderate stakes get engineer. On the tuning sets, the wrong architect picks scored 0.61–0.76 and the right ones ≥ 0.87.
3. **A Workflow needs exhaustive work across separate areas** that isn't one repeated change. Breadth alone isn't enough.
4. **"Don't edit" is a rule, not a judgment.** Explicit instructions ("don't fix anything", "report only", "read-only") force the read ladder, and uncertain cases lean read-only, because a read-only agent that needed to write fails safely.
5. **A read-only `advisor` tier (max effort)** handles design questions that only ask for a recommendation. The final test set exposed this: two "which should we use?" questions were labeled architect, which has edit tools.

| Routing set | Round 1 | Round 2, first fix | Round 2, final |
|---|---|---|---|
| Self-written (30) | 27/30 | 27/30 | 26/30 |
| Held-out 1 (40, tuning) | 34/40 | 36/40 | 38/40 |
| Held-out 2 (40, tuning after first use) | — | **25/40** | 37/40 |
| **Held-out 3 (50, fresh, final test)** | — | — | **47/50 (94%)**, measured before the advisor tier; 46/50 after |
| Read-only tasks given edit tools | 0/20 | 0/32 | **0/49** |

Three things make that held-out 3 figure trustworthy:
- It was written by an agent that saw neither the rules nor any other set.
- It was run once for the headline number.
- The only change made after seeing it was the advisor tier, a safety fix. It moved accuracy by one task, which is within run-to-run variation.

The remaining misses are defensible:
- **Design tasks kept in the main session** (Jev judges them not self-contained).
- **A cents-level billing bug routed to architect.**
- **A tenant-id change** routed to architect.

**Context packs, outline-first.** Slices now show a chunk in full only when Jev rates it clearly relevant (≥ 2.2 of 3), with a 4k default budget. Everything else is an outline with `path:lines`, which the agent can open itself. The slices shrank from 31.7–40.2 KB to 13.6–22.9 KB.

| Click question | No slice | Round 1 slice | **Round 2 slice** |
|---|---|---|---|
| Value resolution | 15,462 tok / 17.1 s | 23,101 (+49%) / 12.8 s | **17,299 (+12%) / 14.4 s** |
| Group dispatch | 16,994 tok / 22.8 s | 24,064 (+42%) / 14.8 s | **18,123 (+7%) / 15.5 s** |
| Shell completion | 17,319 tok / 27.6 s | 27,618 (+59%) / 17.0 s | **21,621 (+25%) / 18.7 s** |

A second blind grading gave the same verdict: **equal on all three questions**, all 88 citations correct. The round-2 slice answers scored 9/9/9 against 8/9/8 without a slice, with one minor imprecision on the slice side and two on the other.

## 6. Paper claims versus what we observed

| Claim | Observation |
|---|---|
| Permission decisions can be programmable queries, with deeper inspection where the stakes justify it | **Supported.** Rules plus Jev, with the script read before it runs, blocked every harmful case without false denies. Most friction came from Jev being cautious, not from the rules. |
| Conditional instructions beat always-on instruction files | **Supported** for meaning-based conditions: precision 1.00. Wording matters: the first "media" rule fired on charts. |
| Skill-style snippets plus selection beat raw tool lists | **Consistent with** TypeSafe's published cookbook; 12/12 here on a crowded, near-duplicate roster. |
| Routing is priced per context rebuild, not per token | **Reframed.** Tiers on one model with per-subagent effort avoid the rebuild penalty entirely, because each subagent starts fresh anyway. Choosing a tier is easy for Jev; deciding when to orchestrate (ultracode) is the weak spot. |
| Retrieval dominates, so sharing it is the largest saving | **Partly.** Sharing cut tool calls and time. A full static slice cost *more* tokens than targeted search on a small codebase (+42–59%); an outline-first slice nearly closes that gap (+7–25%) and keeps the speed-up. The token saving depends on retrieval being expensive in the first place. |
| Per-turn context assembly (visibility ladder, cache reuse) | **Not testable** inside Claude Code, which owns the context window. This is the paper's core idea and the main open item. |

## 7. Limitations and threats to validity

- **Small samples.** 63 + 65 gate cases, 30 + 40 routing tasks, 24 + 12 router prompts, and 3 graded benchmark questions. Differences of a few cases are noise.
- **Label bias.** The self-written sets share an author with the rules; the held-out sets were written by a Claude model and not by independent humans. Neither is real-world traffic.
- **Held-out sets get used up.** Once a held-out set informs a fix, it becomes tuning data. Only gate held-out 2 and routing held-out 3 are clean tests of the final code. Section 5.6 shows what happens otherwise (90% on a reused set against 62% on a fresh one).
- **Adversarial robustness wasn't evaluated.** The gate targets accidents and obvious harm by the agent itself. It's a speed bump, not a sandbox: a determined, obfuscated attack can get past regex rules and a classifier. Use OS-level sandboxing for real isolation.
- **Skill results are environment-specific** (95 skills on one machine).
- **One benchmark codebase,** small, popular and probably in the model's training data, which favors the no-slice baseline.
- **Jev answers vary slightly between runs.** Tasks near a threshold can change tiers.
- **Unresolved:** whether a hook's `"ask"` produces a visible prompt in the desktop app's bypass mode. `"deny"` was verified; `"ask"` wasn't.

## 8. Not built: the paper's core

A harness that owns the context window: a chunk store, a per-turn visibility ladder (hidden / short / long / full) decided by Jev, cost-aware cache-reuse decisions, and subagents handed purpose-built contexts. That needs its own agent loop on the Claude API, not Claude Code hooks. The logs this sidecar collects (`jev.py report`) are meant to show first whether Jev's decisions are accurate enough on real work to justify building it.

## 9. Next steps

Done in round 2 (section 5.6): gate friction, over-eager ultracode, outline-first packs, and search patterns wrongly treated as secret reads.

1. **Context packs on a large, unfamiliar codebase.** That's where the paper predicts retrieval sharing pays off; Click is small and well known.
2. **Real traffic:** a week of logs, then tuning from `jev.py report`, with a fresh held-out set for every tuning round.
3. **Human-labeled evals** to replace model-written labels.
4. **Conditions:** the one round-1 miss ("after deploying to staging, generate an image") is a labeling question as much as a rule problem. Rules are already evaluated independently, so what's needed is examples of compound prompts.
5. **The per-turn harness** from section 8.

## Reproduce

```bash
./install.sh --hooks
python3 evals/run_evals.py all && python3 evals/run_evals.py gate-heldout
# context packs (Click benchmark)
git clone --depth 1 --branch 8.1.7 https://github.com/pallets/click /tmp/click && cd /tmp/click
python3 ~/.claude/skills/jev-orchestrator/scripts/jevpack.py build --name click --task "Understand Click" src
python3 ~/.claude/skills/jev-orchestrator/scripts/jevpack.py slice click --subtask "<question>" > .jev/packs/q.md
```

Raw numbers are in `evals/results/{gate,gate_heldout,route,route_heldout,router,pack_benchmark}.json`.
