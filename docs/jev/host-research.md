# Host support research (2026-10-01)

Research brief for the onboarding installer. Sources are each project's official documentation; anything not confirmed is marked UNVERIFIED.
Re-check before relying on a detail, because these tools change quickly.

## Local machine observations

- `claude --version` reports 2.1.240. `codex`, `gemini` and `zcode` are not on the shell PATH.
- `~/.codex/config.toml` exists (provider, model and reasoning effort set), so Codex must be detected by its config directory as well as by binary: the desktop app uses the same directory.
- `~/.zcode/` and `%APPDATA%\ZCode` exist; `~/.zcode/cli/config.json` currently only has `plugins` and `mcp` keys. There is no `~/.gemini`.

## Claude Code

- Detect with `claude --version`; user config lives in `~/.claude/` (`settings.json`, `agents/`, `skills/`, `CLAUDE.md`).
- Supports PreToolUse, PermissionRequest and UserPromptSubmit command hooks, subagents, skills, CLAUDE.md and MCP.
- Subagent frontmatter `model` accepts `sonnet`, `opus`, `haiku`, `fable`, a full model ID or `inherit`; `effort`, `tools` and `permissionMode` are also accepted.
- Permission modes: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`; the default is set with `permissions.defaultMode`.
- Bypass: the CLI never persists `bypassPermissions` as `defaultMode`, but a hand-written value in user or managed settings is honoured. `skipDangerousModePermissionPrompt: true` suppresses the warning dialog. Managed settings can disable bypass entirely.
- Model remapping: the `model` setting and `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` environment variables. A subagent alias such as `opus` can resolve to the main conversation's model, so agent files should use full model IDs.
- Gate semantics: the docs say deny rules block in every mode including bypass, and that PreToolUse hooks run before every tool call whether or not it needs permission. No sentence states outright that a hook deny survives bypass, so the installer's self-test must confirm it live (the project's own end-to-end test already did this for the desktop app and headless `claude -p`).
- Listing models programmatically: UNVERIFIED.

## OpenAI Codex

- Detect the `codex` binary and `~/.codex` (or `$CODEX_HOME`); the Windows desktop app shares that directory.
- Config: `~/.codex/config.toml` plus `<repo>/.codex/config.toml`.
- Hooks are supported (`~/.codex/hooks.json` or an inline `[hooks]` table; events include PreToolUse, PermissionRequest, PostToolUse, UserPromptSubmit, Stop, SessionStart, SubagentStart, SubagentStop). The decision format is Claude-style (`permissionDecision: "deny"`). Hooks must be reviewed and trusted through `/hooks` before they run, so an installer cannot activate them on its own. Enable with `[features] hooks = true`.
- Subagents are standalone TOML files in `~/.codex/agents/` (settings include `model`, `model_reasoning_effort`, `sandbox_mode`): a different format from Claude's markdown frontmatter.
- Rules: `~/.codex/AGENTS.md`, with a default 32 KiB size cap. Skills: `SKILL.md` directories (the user-level path is UNVERIFIED).
- Approval and sandbox: `approval_policy` (`on-request`, `never`, or a granular table) and `sandbox_mode` (`read-only`, `workspace-write`, `danger-full-access`); profiles via `--profile`. The exact yolo flag name is UNVERIFIED.
- Models: `model`, `model_provider`, `[model_providers.X]`, `model_reasoning_effort`.

## Gemini CLI

- Detect `gemini --version` and `~/.gemini/`.
- Hooks live in `settings.json`: pre-tool event `BeforeTool`, prompt event `BeforeAgent`; deny via `decision: "deny"` or exit code 2.
- Subagents: `~/.gemini/agents/*.md` (frontmatter `tools`, `model`). Skills: `~/.gemini/skills/` or `~/.agents/skills/`. Rules: `GEMINI.md`.
- Approval modes: `default`, `auto_edit`, `plan`; YOLO can only be enabled by flag, never from settings, and `security.disableYoloMode` blocks it.

## Cursor, Windsurf, Cline, Roo

- Cursor: `~/.cursor/hooks.json` (`version: 1`) with `preToolUse`, `beforeShellExecution`, `beforeMCPExecution`, `beforeSubmitPrompt`; exit code 2 or `permission: "deny"` blocks, other failures fail open unless `failClosed: true`. Everything else UNVERIFIED.
- Windsurf: `~/.codeium/windsurf/hooks.json` with `pre_run_command`, `pre_write_code`, `pre_user_prompt`; exit code 2 blocks; the stdin schema is not Claude-compatible. Everything else UNVERIFIED.
- Cline and Roo: nothing verified.

## ZCode

- Hooks in `~/.zcode/cli/config.json` under `hooks.events.<Event>`; Claude-compatible `permissionDecision` deny and exit code 2; only user-level hooks and plugin hooks run (project-level hooks are ignored); the config is snapshotted per session, so a new session is required.
- Skills: `~/.zcode/skills/<name>/SKILL.md`. Subagents: `~/.zcode/agents/*.md` in its own format (do not copy Claude agent files). Rules: AGENTS.md. Permission modes: UNVERIFIED.

## Support matrix

| Host | Gate hook | Prompt hook | Subagents | Skills | Rules file | Permission/model config |
|---|---|---|---|---|---|---|
| Claude Code | full | full | full | full | full | full |
| Codex | partial (trust review) | partial | partial (TOML) | partial | full | full |
| Gemini CLI | partial (other schema) | partial | partial | full | full | partial |
| Cursor | partial (other schema) | partial | unverified | unverified | unverified | unverified |
| Windsurf | partial (other schema) | partial | unverified | unverified | unverified | unverified |
| Cline / Roo | unverified | unverified | unverified | unverified | unverified | unverified |
| ZCode | partial (user-level) | partial | partial (own format) | full | full | unverified |

## Installer principles that follow

1. Detect by binary and by config directory; show what each host can honestly support; install only after confirmation.
2. Claude Code gets the full harness. Never write `bypassPermissions` or `skipDangerousModePermissionPrompt` without an explicit, separately confirmed choice, and back up `settings.json` first.
3. Other hosts get only what is verified to work, labelled partial or experimental; never claim a gate is active when the host requires the user to trust it first (Codex) or restart (ZCode).
4. Do not install anything for hosts whose formats are unverified.
