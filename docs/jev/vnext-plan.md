# JEV vNext — Agent Registry, Model Routing & Handoff Fix Plan

> Status: Implementation Plan  
> Goal: Fix JEV so its intended cheap-first escalation ladder actually runs, remove accidental Opus inheritance, make model routing deterministic, and make Opus ↔ Sonnet handoffs independent from hidden model context.

---

## 1. Executive Summary

JEV's current orchestration concept is already correct:

```text
Architect plan
    ↓
Builder
    ↓ if stuck
Engineer
    ↓ if stuck
Debugger
    ↓ if still stuck / plan invalid
Architect replan
```

The current problem is runtime behavior:

1. `jev-builder` is not registered in the active Agent tool session.
2. `jev-reviewer` is not registered in the active Agent tool session.
3. Missing agents are being silently substituted:
   - `jev-builder` → `jev-engineer`
   - `jev-reviewer` → `jev-analyst`
4. The orchestrator currently spawns agents without an explicit `model`, causing the session-pinned Opus 5.5 to apply.
5. The architect is read-only but parts of the workflow expect it to write plan files.
6. Opus and Sonnet should not depend on continuation of hidden reasoning/context between agents.
7. Expensive agents are being used for tasks that should run on cheaper lanes.
8. `graphify` / `graphq` usage is currently prompt-driven instead of enforced by the orchestrator.
9. `jevpack.py`, `graphq`, and direct/manual file reads can overlap and duplicate context.
10. Terse/Caveman-style agent reporting is currently a repeated prompt convention instead of a standard inter-agent report contract.

This plan fixes all of the above without replacing JEV's core architecture.

The final architecture should be layered:

```text
graphify
   ↓
graphq
   ↓
jevpack
   ↓
bounded task context
   ↓
JEV model/effort routing
   ↓
agent execution
   ↓
terse structured handoff
```

---

# 2. Target Architecture

## 2.1 Final Role Matrix

| Agent | Model | Effort | Capability | Main Purpose |
|---|---|---:|---|---|
| `jev-architect` | Opus 5.5 | max | read-only | architecture, planning, replan |
| `jev-advisor` | Sonnet 5.5 | high | read-only | design alternatives / advice |
| `jev-analyst` | Sonnet 5.5 | high | read-only | investigation / code analysis |
| `jev-builder` | Sonnet 5.5 | low | write | default implementation |
| `jev-engineer` | Sonnet 5.5 | medium | write | complex implementation / builder escalation |
| `jev-debugger` | Opus 5.5 | high | write/read as required | hard debugging / last-resort rescue |
| `jev-reviewer` | Sonnet 5.5 | medium or high | read-only | independent review |
| `jev-qa` | Sonnet 5.5 | low | browser/test | QA and verification |
| `jev-scout` | Sonnet 5.5 | low | read-only | repo/file/symbol lookup |

Core rule:

```text
SONNET = execute / inspect / verify
OPUS   = decide / rescue
```

Context rule:

```text
GRAPHIFY = structural source graph
GRAPHQ   = task-specific retrieval / relevance selection
JEVPACK  = context packaging / handoff
AGENT    = reasoning / execution
```

Reporting rule:

```text
internal reasoning  = normal for the selected effort
agent report        = terse structured protocol
persisted docs      = normal prose
```

Target high-level flow:

```text
                      JEV
                       │
               ┌───────┴────────┐
               │                │
            ROUTING          CONTEXT
               │                │
      model + effort        graphify
               │                │
               │             graphq
               │                │
               └───────┬────────┘
                       │
                    jevpack
                       │
                bounded context
                       │
           ┌───────────┴───────────┐
           │                       │
        SONNET                    OPUS
     execution layer         intelligence layer
           │                       │
    builder / engineer         architect
    reviewer / analyst         debugger
    scout / QA
           │                       │
           └──────────┬────────────┘
                      │
               TERSE JEV REPORT
                      │
              explicit handoff
```

New invariant:

```text
NO AGENT SHOULD NEED TO DISCOVER THE WHOLE REPOSITORY
UNLESS ITS ROLE EXPLICITLY REQUIRES BROAD DISCOVERY.
```

---

# 3. Required Behavioral Changes

## 3.1 No Silent Agent Substitution

Current behavior must stop:

```text
jev-builder missing
→ silently use jev-engineer
```

or:

```text
jev-reviewer missing
→ silently use jev-analyst
```

Replace with:

```text
preflight validation
→ fail fast
→ explain missing registration
→ do not start task
```

Exception: substitution is allowed only when explicitly defined in configuration.

Example:

```yaml
fallbacks:
  jev-builder: null
  jev-reviewer: null
```

This means no automatic fallback.

---

## 3.2 Deterministic Model Routing

Do not rely on session model inheritance.

Bad:

```text
spawn jev-builder
(no model parameter)
→ pinned session model decides model
```

Target:

```text
agent definition / orchestrator routing
explicitly determines:
- role
- model
- effort
```

Model routing must be visible and inspectable before execution.

Target metadata:

```yaml
agent:
  role: jev-builder
  model: sonnet-5.5
  effort: low
```

If the agent framework supports model configuration inside agent files, prefer that.

If not, the orchestrator must explicitly pass the model during spawn.

Do not use an ambient session model as the source of truth.

---

# 4. Agent Registration Fix

## 4.1 First Investigation Task

Before changing files, inspect how agents become available to the Agent tool.

Find:

- agent definition directory/directories
- registry or manifest
- session bootstrap code
- agent loader
- filtering logic
- tool registration logic
- naming rules
- enabled/disabled flags
- role capability metadata
- whether registration happens at process start or dynamically

Search for references to:

```text
jev-architect
jev-engineer
jev-builder
jev-reviewer
Agent tool
registerAgent
agents
subagents
agent registry
```

Do not assume that the existence of an agent file means the agent is registered.

---

## 4.2 Compare Registered vs Existing Agents

Produce this diagnostic table:

| Agent | Definition Exists | Registered | Callable | Model | Effort |
|---|---:|---:|---:|---|---:|
| jev-architect | | | | | |
| jev-advisor | | | | | |
| jev-analyst | | | | | |
| jev-builder | | | | | |
| jev-engineer | | | | | |
| jev-debugger | | | | | |
| jev-reviewer | | | | | |
| jev-qa | | | | | |
| jev-scout | | | | | |

Required result:

```text
ALL required JEV agents:
definition exists
AND
registered
AND
callable
```

---

## 4.3 Fix Registration

Ensure these roles are registered:

```text
jev-architect
jev-advisor
jev-analyst
jev-builder
jev-engineer
jev-debugger
jev-reviewer
jev-qa
jev-scout
```

Registration names must match exactly.

Avoid alias-only registration such as:

```text
builder
reviewer
```

if the orchestrator calls:

```text
jev-builder
jev-reviewer
```

---

# 5. Add JEV Startup Preflight

Before any orchestration begins, run preflight.

Pseudo-flow:

```text
start JEV task
    ↓
load expected agents
    ↓
query active registry
    ↓
validate required roles
    ↓
validate model configuration
    ↓
validate effort configuration
    ↓
validate capabilities
    ↓
PASS → continue
FAIL → abort
```

Required checks:

```yaml
preflight:
  required_agents:
    - jev-architect
    - jev-builder
    - jev-engineer
    - jev-debugger
    - jev-reviewer
    - jev-qa
    - jev-scout

  checks:
    registration: true
    callable: true
    model_resolved: true
    effort_resolved: true
    capability_resolved: true

  on_failure:
    abort_task: true
    allow_silent_fallback: false
```

Example failure:

```text
JEV PREFLIGHT FAILED

Missing agents:
- jev-builder
- jev-reviewer

Expected routes:
jev-builder  -> Sonnet 5.5 / low
jev-reviewer -> Sonnet 5.5 / medium

Task execution stopped.
No fallback agent was spawned.
```

---

# 6. New Routing Policy

## 6.1 Normal Planned Route

Use for:

- multi-file changes
- cross-module changes
- architecture changes
- persistence changes
- state machine changes
- unclear requirements
- concurrency
- networking
- database/schema work
- security-sensitive changes

Flow:

```text
jev-architect
Opus 5.5 / max
        ↓
structured plan
        ↓
jev-builder
Sonnet 5.5 / low
        ↓
success?
  ├─ yes → review
  └─ no  → classify failure
```

---

## 6.2 Fast Path

Do not automatically spend Opus MAX on trivial tasks.

Eligible examples:

- copy/text change
- simple rename
- constant change
- isolated null guard
- simple one-file UI adjustment
- obvious small bug with known acceptance criteria
- mechanical refactor with no API/schema effect

Criteria:

```yaml
fast_path:
  max_files_expected: 1
  architecture_change: false
  public_api_change: false
  schema_change: false
  persistence_change: false
  concurrency_change: false
  requirement_ambiguity: low
```

Route:

```text
jev-builder
Sonnet 5.5 / low
      ↓
optional jev-reviewer
      ↓
jev-qa if relevant
```

If builder determines the task is not actually trivial:

```text
STOP
→ request planned route
→ architect
```

Do not let builder improvise architecture.

---

# 7. Stuck Ladder v2

Current ladder:

```text
builder
→ engineer
→ debugger
→ replan
```

Keep it, but classify failure first.

## 7.1 Failure Categories

### A. Implementation Complexity

Examples:

- more files than expected
- refactor larger than planned
- unfamiliar framework integration
- builder cannot complete implementation cleanly

Route:

```text
jev-builder
→ jev-engineer
```

---

### B. Hard Debugging

Examples:

- nondeterministic issue
- state corruption
- race condition
- lifecycle bug
- repeated failing tests without obvious cause
- issue crosses unrelated modules
- builder/engineer cannot identify root cause

Route:

```text
jev-debugger
Opus 5.5 / high
```

Do not force Engineer first if the failure is clearly a debugging problem.

---

### C. Invalid Plan / Invalid Assumption

Examples:

- architect expected API does not exist
- architecture conflicts with repo reality
- required invariant cannot hold
- implementation exposes missing requirement
- planned module boundary is wrong

Route immediately to:

```text
jev-architect
Opus 5.5 / max
REPLAN
```

Do not waste an Engineer attempt when the plan itself is wrong.

---

### D. Requirement Ambiguity

Examples:

- multiple valid product interpretations
- missing acceptance criteria
- requested behavior contradicts existing behavior

Route:

```text
stop coding
→ analyst/advisor if clarification can be inferred safely
→ otherwise report ambiguity to orchestrator/user
```

Do not let implementation agents invent product decisions.

---

# 8. Graph-First Context Policy

Current reality:

```text
jev.py route
→ does not automatically read graphify

jevpack.py
→ touches graph/context pack files

graphq
→ runs graphify query and filters relevant chunks

agents
→ use graphify/graphq only because prompts tell them to
```

This must become automatic behavior instead of repeated prompt boilerplate.

## 8.1 Harness-Level Enforcement

The orchestrator/harness should own graph-first context preparation.

Preferred sequence:

```text
task
  ↓
task classification
  ↓
context policy lookup
  ↓
graphq query when required
  ↓
graphify-backed relevant context
  ↓
jevpack package
  ↓
spawn agent with bounded context
```

Agent files should still contain a short graph-first instruction as defense-in-depth, but the system must not rely on the agent remembering to do it.

---

## 8.2 Context Policy by Role

Use a role-specific policy rather than blindly making every agent query the graph again.

```yaml
context_policy:

  jev-architect:
    graph: required
    scope: broad
    purpose: architecture_and_dependencies

  jev-advisor:
    graph: conditional
    scope: targeted
    purpose: design_advice

  jev-analyst:
    graph: required
    scope: broad
    purpose: investigation

  jev-scout:
    graph: required
    scope: targeted
    purpose: lookup

  jev-builder:
    graph: conditional
    scope: narrow
    use_plan_context_first: true
    purpose: implementation

  jev-engineer:
    graph: conditional
    scope: expanded
    use_prior_handoff_first: true
    purpose: implementation_escalation

  jev-debugger:
    graph: required
    scope: expanded
    purpose: root_cause

  jev-reviewer:
    graph: required
    scope: changed_files_plus_dependents
    purpose: regression_review

  jev-qa:
    graph: optional
    scope: feature_surface
    purpose: verification
```

---

## 8.3 Avoid Duplicate Retrieval

Do not allow this pattern by default:

```text
Architect graphq
Builder graphq same task
Engineer graphq same task
Reviewer graphq same task
QA graphq same task
```

The architect's plan/context pack should carry enough information for the builder to start without rediscovering the repository.

The builder should query graphq again only when one of these occurs:

```text
scope mismatch
unknown dependency
plan references missing symbol
implementation touches unplanned module
repository state changed
provided context is insufficient
```

The Engineer should start from Builder handoff + existing pack and expand retrieval only when escalation requires it.

Reviewer retrieval should focus on:

```text
changed files
direct dependents
relevant invariants
acceptance criteria
likely regression surface
```

QA should use graph retrieval only if code/repository context is actually needed for verification.

---

## 8.4 Define Responsibilities Clearly

Prevent `jevpack.py`, `graphq`, and agent file reads from becoming three competing context systems.

Target separation:

```text
graphify
= structural repository knowledge

graphq
= task-specific graph retrieval / relevance filtering

jevpack
= package selected context for a task or handoff

agent
= reason, implement, review, or test
```

Preferred direction:

```text
graphify
    ↓
graphq
    ↓
jevpack
    ↓
agent
```

Not:

```text
jevpack independently discovers context
+
graphq independently discovers context
+
agent independently scans whole repository
```

---

## 8.5 Context Pack Contract

Recommended pack shape:

```yaml
jev_context_pack:
  task_id: TASK-123

  task:
    "Fix next-run calculation"

  retrieval:
    source: graphify
    selector: graphq

  primary_files:
    - main/utils/ScheduleAdherence.kt
    - main/map/ScheduleStatusManager.kt

  related_files:
    - main/data/FullScheduleData.kt

  symbols:
    - calculateScheduleStatus
    - activeTripId

  dependencies:
    - "ScheduleStatusManager consumes ScheduleAdherence"
    - "Next-run selection depends on activeTripId ordering"

  constraints:
    - "Preserve existing schedule threshold semantics"

  invariants:
    - "All UI and internal status calculations use the same source"

  context_budget:
    bounded: true
```

The exact schema can follow existing JEV conventions, but the pack must make the selected scope explicit.

---

## 8.6 Graph Failure / Low-Confidence Fallback

Graph-first must not become graph-only.

If graph retrieval fails, is stale, or clearly misses relevant files:

```text
graph retrieval fails
        ↓
controlled fallback
        ↓
targeted repo/file search
        ↓
update context pack
```

Do not silently fall back to an unrestricted full-repository scan unless the role explicitly allows broad discovery.

Log when graph fallback occurs.

Example:

```text
[JEV CONTEXT]
task_id=TASK-123
role=jev-builder
graph_status=insufficient
fallback=targeted_file_search
reason=referenced symbol not present in selected context
```

---

# 9. Terse Agent Report Protocol

Caveman-style reporting should become a JEV boundary protocol, not a requirement that all persisted writing be terse.

Use:

```text
internal reasoning
→ normal for role/effort

inter-agent report
→ terse + structured

README / architecture docs / migration docs
→ normal prose
```

Recommended default report:

```text
STATUS: DONE

CHANGED:
- ScheduleAdherence.kt
- ScheduleStatusManager.kt

WHY:
- unified status source
- fixed next-run selection

TEST:
- +15s = ON_TIME
- -301s = VERY_BEHIND

RISK:
- fallback when activeTripId absent

NEXT:
- jev-reviewer
```

For failures:

```text
STATUS: BLOCKED

CATEGORY:
- invalid_plan

FOUND:
- planned API does not exist

EVIDENCE:
- src/foo/Bar.kt

NEXT:
- jev-architect replan
```

Persisted files remain in normal prose unless the file itself is intended to be a machine-oriented handoff artifact.

---

# 10. Artifact-Based Handoff

Agents must not depend on hidden chain-of-thought from another model.

All handoffs should be explicit.

## 8.1 Architect Output Contract

Architect returns structured output.

Example:

```yaml
jev_plan:
  task_id: TASK-123

  objective:
    "Implement streaming world chunks around player."

  assumptions:
    - "World coordinates are deterministic."
    - "Chunk state can be serialized independently."

  constraints:
    - "No save format break."
    - "No new runtime dependency."

  files_to_inspect:
    - src/world/ChunkManager.kt
    - src/world/WorldState.kt

  likely_files_to_modify:
    - src/world/ChunkManager.kt

  implementation_steps:
    - "Compute player chunk coordinate."
    - "Maintain active chunk radius."
    - "Load entering chunks."
    - "Persist and unload leaving chunks."

  invariants:
    - "Never unload current player chunk."
    - "Same seed reproduces same terrain."
    - "Entity state survives unload/reload."

  acceptance_criteria:
    - "Crossing chunk boundary loads next chunk."
    - "Memory does not grow indefinitely."
    - "Returning restores persisted state."

  escalation_conditions:
    - "Save architecture conflicts with chunk lifecycle."
```

---

## 8.2 Builder / Engineer Completion Contract

```yaml
jev_handoff:
  task_id: TASK-123
  role: jev-builder
  status: completed

  files_changed:
    - src/world/ChunkManager.kt

  implementation_summary:
    - "Added active chunk radius management."
    - "Added unload persistence hook."

  decisions:
    - "Used current save serializer."
    - "Kept radius configurable."

  tests_run:
    - "./gradlew test"

  test_results:
    - "PASS"

  unresolved:
    - []

  risks:
    - "Large radius may spike load times."

  next_recommended_agent:
    - jev-reviewer
```

---

## 8.3 Failure Contract

```yaml
jev_failure:
  task_id: TASK-123

  agent: jev-builder

  category: implementation_complexity

  completed:
    - "Mapped affected files."
    - "Implemented chunk coordinate detection."

  blocked_by:
    - "Current cache ownership spans 4 modules."

  recommended_route:
    agent: jev-engineer
    reason: "Cross-module implementation exceeds builder scope."

  evidence:
    - src/world/ChunkManager.kt
    - src/cache/WorldCache.kt
```

The next agent receives this artifact plus relevant repo state.

---

# 11. Plan Persistence Fix

`jev-architect` is read-only.

Therefore:

```text
architect should NOT be responsible
for writing its own plan file
```

Correct flow:

```text
Architect returns plan text/structured output
        ↓
Orchestrator receives plan
        ↓
Orchestrator persists plan
        ↓
Builder receives persisted plan
```

Recommended directory:

```text
.jev/
  plans/
    TASK-123.md
  handoffs/
    TASK-123-builder.yaml
    TASK-123-review.yaml
  logs/
```

If `.jev` is not appropriate for this repository, use the project's existing internal-workflow directory.

Do not spawn `jev-engineer` merely to save markdown.

---

# 12. Reviewer Restoration

`jev-reviewer` must exist as its own role.

Do not permanently replace it with `jev-analyst`.

Reviewer input should include:

```text
original task
architect plan
acceptance criteria
git diff
changed files
test results
relevant repository context
```

Reviewer should not need the builder's hidden reasoning.

Reviewer contract:

```yaml
review:
  verdict: pass | changes_required

  findings:
    critical: []
    major: []
    minor: []

  acceptance_criteria:
    - criterion: "..."
      status: pass | fail | unclear

  regression_risks: []

  required_changes: []
```

Recommended route:

```text
implementation
    ↓
jev-reviewer
Sonnet 5.5 / medium
    ↓
PASS → QA
FAIL → Builder/Engineer based on severity
```

Use `high` effort only for:

- security-sensitive code
- state machines
- concurrency
- persistence
- high blast-radius refactor
- public API changes

---

# 13. Model Configuration

## 11.1 Preferred Configuration

```yaml
agents:

  jev-architect:
    model: opus-5.5
    effort: max
    write: false

  jev-advisor:
    model: sonnet-5.5
    effort: high
    write: false

  jev-analyst:
    model: sonnet-5.5
    effort: high
    write: false

  jev-builder:
    model: sonnet-5.5
    effort: low
    write: true

  jev-engineer:
    model: sonnet-5.5
    effort: medium
    write: true

  jev-debugger:
    model: opus-5.5
    effort: high

  jev-reviewer:
    model: sonnet-5.5
    effort: medium
    write: false

  jev-qa:
    model: sonnet-5.5
    effort: low

  jev-scout:
    model: sonnet-5.5
    effort: low
    write: false
```

Adapt exact model IDs to the actual provider/runtime names.

Do not guess IDs.

Inspect currently supported model identifiers before changing configuration.

---

# 14. Routing Metadata

Every spawn should be logged.

Example:

```text
[JEV]
task_id=TASK-123
role=jev-builder
model=sonnet-5.5
effort=low
reason=default implementation route
attempt=1
```

Escalation:

```text
[JEV]
task_id=TASK-123
from=jev-builder
to=jev-engineer
failure=implementation_complexity
model=sonnet-5.5
effort=medium
attempt=2
```

Debugger:

```text
[JEV]
task_id=TASK-123
from=jev-engineer
to=jev-debugger
failure=hard_debugging
model=opus-5.5
effort=high
attempt=3
```

This must make accidental Opus usage easy to detect.

---

# 15. Cost Guardrails

Add optional counters:

```yaml
usage_guardrails:
  opus:
    allowed_roles:
      - jev-architect
      - jev-debugger

    warn_on_unexpected_role: true

  max_effort:
    allowed_roles:
      - jev-architect

  task:
    max_replans: 2
    max_debugger_attempts: 2
```

If Sonnet worker unexpectedly resolves to Opus:

```text
WARNING / FAIL

Expected:
jev-builder → Sonnet 5.5

Resolved:
jev-builder → Opus 5.5

Abort before execution.
```

Prefer failure over hidden cost drift.

---

# 16. Suggested Orchestrator Decision Flow

```text
NEW TASK
   │
   ▼
PREFLIGHT
   │
   ├── FAIL → ABORT
   │
   ▼
CLASSIFY TASK
   │
   ├── trivial / isolated
   │       ↓
   │   Builder LOW
   │
   └── non-trivial
           ↓
      Architect MAX
           ↓
      Persist Plan
           ↓
      Builder LOW
           │
           ▼
     RESULT CLASSIFY
           │
   ┌───────┼───────────┐
   │       │           │
success  impl issue  plan invalid
   │       │           │
   │       ▼           ▼
   │   Engineer MED   Replan MAX
   │       │
   │       ▼
   │   still stuck?
   │       │
   │       ▼
   │   Debugger HIGH
   │
   ▼
Reviewer MED
   │
   ├── changes → worker
   │
   ▼
QA LOW
   │
   ▼
DONE
```

---

# 17. Implementation Phases

## Phase 0 — Baseline Snapshot

Before changes:

1. Record current registered agents.
2. Record current resolved model for each JEV agent.
3. Record current effort for each JEV agent.
4. Save one example task trace.
5. Confirm exact current bug:
   - `jev-builder` not found
   - `jev-reviewer` not found
6. Identify where pinned Opus is inherited.

Deliverable:

```text
docs/jev/current-routing-baseline.md
```

or equivalent.

---

## Phase 1 — Registry Repair

Tasks:

- locate registration mechanism
- register `jev-builder`
- register `jev-reviewer`
- verify all nine JEV roles
- remove broken stale aliases if needed
- ensure exact names are callable

Acceptance:

```text
Agent tool successfully resolves:
jev-builder
jev-reviewer
```

No substitute agents involved.

---

## Phase 2 — Preflight

Implement:

- expected role list
- runtime registry validation
- model validation
- effort validation
- capability validation
- clear failure message

Acceptance:

Delete/disable `jev-builder` temporarily.

Expected:

```text
task aborts before architect/builder execution
```

Restore afterwards.

---

## Phase 3 — Model Split

Change routing from:

```text
all JEV agents
→ pinned Opus
```

to:

```text
Architect → Opus
Debugger  → Opus

Builder   → Sonnet
Engineer  → Sonnet
Reviewer  → Sonnet
Analyst   → Sonnet
Advisor   → Sonnet
QA        → Sonnet
Scout     → Sonnet
```

Acceptance:

Run one task and inspect logs.

No worker role should use Opus.

---

## Phase 4 — Plan Persistence

Remove architecture where a write-capable coding agent is used merely to save the architect's plan.

Implement orchestrator persistence.

Acceptance:

```text
Architect remains read-only.
Plan file exists.
No Engineer/Builder spawned to write the plan.
```

---

## Phase 5 — Structured Handoff

Add standard schemas for:

- plan
- implementation completion
- implementation failure
- review
- QA

Ensure cross-model handoff relies only on explicit artifacts.

Acceptance:

A plan generated by Opus can be implemented by Sonnet in a fresh context without requiring architect conversation history.

---

## Phase 6 — Failure-Aware Escalation

Implement categories:

```text
implementation_complexity
hard_debugging
invalid_plan
requirement_ambiguity
environment_failure
test_failure
```

Routing table:

| Failure | Next |
|---|---|
| implementation_complexity | `jev-engineer` |
| hard_debugging | `jev-debugger` |
| invalid_plan | `jev-architect` |
| requirement_ambiguity | orchestrator/user clarification |
| environment_failure | fix environment / stop |
| simple test failure | builder or engineer |

Acceptance:

An invalid architecture assumption must replan directly rather than automatically burn Engineer + Debugger attempts.

---

## Phase 7 — Graph-First Context Automation

Implement harness-level context policy:

- detect whether the role requires graph retrieval
- run `graphq` automatically when required
- use graphify-backed results as the primary retrieval source
- package selected context through `jevpack`
- inject bounded context into the spawned agent
- avoid repeated identical graph queries across agents
- log graph retrieval and fallback behavior

Acceptance:

```text
A complex task can invoke Architect without manually telling it "use graphify first".
```

And:

```text
Builder receives plan + bounded context pack
without independently scanning the whole repository.
```

---

## Phase 8 — Terse Report Protocol

Standardize inter-agent reporting.

Implement:

- terse completion report
- terse blocked/failure report
- explicit next-agent recommendation
- normal prose retained for persisted docs

Acceptance:

```text
No orchestrator prompt needs to repeat "report terse".
```

---

## Phase 9 — Reviewer Restoration

Restore actual:

```text
jev-reviewer
```

Do not use:

```text
jev-analyst
```

as a normal review substitute.

Acceptance:

Implementation task trace shows:

```text
builder/engineer
→ reviewer
→ QA
```

---

## Phase 10 — Fast Path

Implement trivial-task classifier.

Start conservative.

Initially only allow fast path when all are true:

```yaml
single_file_expected: true
architecture_change: false
schema_change: false
persistence_change: false
public_api_change: false
concurrency_change: false
requirements_clear: true
```

Acceptance:

Simple copy change does not invoke Opus Architect.

Complex task still does.

---

# 18. Tests

## 16.1 Registry Test

```text
Given all definitions exist
When JEV initializes
Then every required agent is callable.
```

---

## 16.2 Missing Agent Test

Temporarily unregister builder.

Expected:

```text
preflight fails
task does not run
engineer is NOT substituted
```

---

## 16.3 Model Resolution Test

Expected:

```text
jev-builder  = Sonnet 5.5
jev-engineer = Sonnet 5.5
jev-reviewer = Sonnet 5.5
jev-architect = Opus 5.5
jev-debugger  = Opus 5.5
```

---

## 16.4 Simple Task Routing Test

Task:

```text
Change one UI string.
```

Expected:

```text
Builder
→ optional Reviewer
→ Done
```

No Architect.

No Opus.

---

## 16.5 Planned Task Test

Task:

```text
Cross-module feature requiring persistence.
```

Expected:

```text
Architect MAX
→ persisted plan
→ Builder LOW
→ Reviewer
→ QA
```

---

## 16.6 Builder Escalation Test

Force builder to report implementation complexity.

Expected:

```text
Builder
→ Engineer
```

---

## 16.7 Debugger Escalation Test

Use a deliberately difficult failing-state scenario.

Expected:

```text
Builder/Engineer
→ Debugger Opus HIGH
```

---

## 16.8 Invalid Plan Test

Mock an architect assumption that is demonstrably false.

Expected:

```text
Builder detects invalid plan
→ Architect REPLAN
```

Not:

```text
Builder
→ Engineer
→ Debugger
```

---

## 16.9 Cross-Model Handoff Test

1. Architect produces plan with Opus.
2. Start a fresh Sonnet builder context.
3. Give only:
   - task
   - persisted plan
   - repo context
4. Builder should implement correctly.

Pass condition:

No hidden architect conversation is required.

---

# 19. Telemetry

Track per task:

```yaml
task_metrics:
  task_id:
  route:
  agents_used:
  model_per_agent:
  effort_per_agent:
  attempts:
  escalations:
  replans:
  review_cycles:
  qa_cycles:
  fast_path:
  failure_categories:
  graph_queries:
  graph_fallbacks:
  context_pack_reuse:
  context_pack_refreshes:
```

Optional future metrics:

```text
% tasks completed by Builder
% tasks escalated to Engineer
% tasks requiring Debugger
% tasks requiring Replan
% tasks invoking Opus
average Opus calls per task
% tasks using graph context automatically
% builder runs reusing architect context without broad rediscovery
graph fallback rate
average context pack size
```

The desired trend after this migration:

```text
most implementation work → Builder
some → Engineer
few → Debugger
few → Replan
```

---

# 20. Guardrails

Do not:

- silently substitute missing roles
- inherit model from ambient session configuration
- use Engineer to write markdown plan files
- make Builder perform architecture decisions
- make Analyst act as permanent Reviewer
- invoke Opus for trivial mechanical tasks
- depend on hidden reasoning across Opus/Sonnet
- retry the same failed approach repeatedly without changing route
- continue coding when the plan is invalid
- rely on prompt repetition to enforce graph-first behavior
- make every agent repeat the same graph query
- let `jevpack`, `graphq`, and the agent independently discover the same context
- force terse style onto persisted human-facing documentation

Do:

- validate agents before execution
- make routing deterministic
- persist explicit plans
- classify failure
- keep handoffs structured
- log model + effort + reason
- keep expensive intelligence exceptional
- enforce graph-first at the harness/orchestrator layer
- reuse context packs across the route when still valid
- keep inter-agent reports terse and persisted docs readable

---

# 21. Migration Safety

Do not migrate all routing in one unreviewed change.

Recommended order:

```text
1. fix registry
2. add preflight
3. add routing logs
4. split models
5. fix plan persistence
6. automate graph-first context preparation
7. standardize terse report protocol
8. restore reviewer
9. add structured handoff
10. add failure-aware escalation
11. add fast path
```

After each step:

```text
run one real task
inspect route
inspect model
inspect output
inspect cost behavior
```

---

# 22. Rollback

Keep previous routing config available during migration.

If a regression occurs:

```text
revert routing config
keep registry fix
keep preflight
keep logging
```

Registry validation and telemetry should remain even if model split is temporarily rolled back.

---

# 23. Definition of Done

This project is complete when all are true:

- [ ] `jev-builder` is registered and callable.
- [ ] `jev-reviewer` is registered and callable.
- [ ] Preflight validates all required JEV roles.
- [ ] Missing agents never trigger silent substitution.
- [ ] Agent model selection is deterministic.
- [ ] Session-pinned Opus no longer controls all subagents.
- [ ] Builder runs Sonnet 5.5 low.
- [ ] Engineer runs Sonnet 5.5 medium.
- [ ] Reviewer runs Sonnet 5.5 medium/high.
- [ ] Architect runs Opus 5.5 max.
- [ ] Debugger runs Opus 5.5 high.
- [ ] Architect remains read-only.
- [ ] Orchestrator persists architect plan.
- [ ] No coding agent is used only to save markdown.
- [ ] Handoff works across fresh Opus/Sonnet contexts.
- [ ] Failure categories route to the correct next agent.
- [ ] Simple tasks can bypass Architect.
- [ ] Complex tasks still use Architect first.
- [ ] Routing logs show model, effort, role, attempt and reason.
- [ ] Reviewer is independent from Analyst.
- [ ] Graph-first retrieval is enforced by the harness where policy requires it.
- [ ] Agents no longer need repeated prompt text telling them to use graphify/graphq.
- [ ] `graphify`, `graphq`, and `jevpack` have distinct responsibilities.
- [ ] Builder can reuse Architect context without repeating broad discovery.
- [ ] Graph retrieval has a controlled fallback path.
- [ ] Inter-agent reports are terse by default.
- [ ] Persisted human-facing files remain normal prose.
- [ ] At least one end-to-end task completes on the intended cheap-first path.

---

# 24. Recommended Agent Prompt

Use this when handing the plan to the implementation agent:

```text
Implement the JEV routing migration described in this document.

Important rules:

1. Do not assume how agent registration works. Inspect the repository/runtime first.
2. First reproduce and document why `jev-builder` and `jev-reviewer` exist as files but are not registered in the active Agent tool.
3. Fix registry before touching model routing.
4. Add startup preflight before allowing silent fallbacks.
5. Preserve JEV's existing stuck ladder concept:
   builder → engineer → debugger → replan.
6. Improve the ladder by classifying failure before escalation.
7. Make model selection deterministic:
   - Opus 5.5: architect, debugger
   - Sonnet 5.5: builder, engineer, reviewer, analyst, advisor, QA, scout
8. Do not depend on ambient/pinned session model inheritance.
9. Do not make the read-only architect write files.
10. Persist architect output through the orchestrator.
11. Restore `jev-reviewer`; do not use `jev-analyst` as its permanent replacement.
12. Use explicit structured artifacts for cross-agent handoff.
13. Do not depend on hidden chain-of-thought/context between Opus and Sonnet.
14. Add logging showing role, model, effort, attempt, escalation reason.
15. Add tests covering missing-agent failure, routing, escalation, replan, reviewer, and cross-model handoff.
16. Do not introduce a broad rewrite if the existing architecture can be repaired incrementally.
17. After every phase, run the narrowest relevant tests before continuing.
18. If actual repository architecture differs from this plan, preserve the intent and document the deviation.

Before coding, return:
- discovered registration mechanism
- current routing mechanism
- exact files likely to change
- migration order
- risks

Then implement phase-by-phase.
```

---

# 25. Desired Final Behavior Example

```text
User task
   ↓
JEV preflight PASS
   ↓
task classified as complex
   ↓
graphq / graphify context preparation
   ↓
jevpack bounded context
   ↓
jev-architect
Opus MAX
   ↓
orchestrator saves plan + context pack
   ↓
jev-builder
Sonnet LOW
(reuses plan/context first)
   ↓
implementation succeeds
   ↓
jev-reviewer
Sonnet MED
   ↓
changes requested
   ↓
jev-builder
Sonnet LOW
   ↓
review PASS
   ↓
jev-qa
Sonnet LOW
   ↓
DONE
```

Opus usage:

```text
1 call
```

Instead of:

```text
architect Opus
engineer Opus
analyst Opus
review Opus
misc Opus
```

That is the intended JEV vNext architecture.
