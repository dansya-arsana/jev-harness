#!/bin/zsh
# Install the Jev harness into ~/.claude by linking it from this repo.
#
#   ./install.sh           link the skill, the jev-* agents, and seed ~/.claude/jev/conditions.json
#   ./install.sh --hooks   also register the permission gate and prompt router in ~/.claude/settings.json
#
# Safe to re-run: links are refreshed, real files it would replace are moved to ~/.claude/backups/ (never deleted),
# settings.json is backed up before any change, and hook entries are only added once.
set -euo pipefail

REPO="${0:A:h}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/.claude/backups/jev-$STAMP"

link() {  # link <repo path> <target path>
  local src="$REPO/$1" dst="$2"
  mkdir -p "${dst:h}"
  if [[ -L "$dst" ]]; then
    rm "$dst"
  elif [[ -e "$dst" ]]; then
    mkdir -p "$BACKUP"
    mv "$dst" "$BACKUP/"
    echo "backed up $dst -> $BACKUP/"
  fi
  ln -s "$src" "$dst"
  echo "linked    $dst"
}

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

link skill/jev-orchestrator "$HOME/.claude/skills/jev-orchestrator"
for f in "$REPO"/agents/jev-*.md; do
  link "agents/${f:t}" "$HOME/.claude/agents/${f:t}"
done

mkdir -p "$HOME/.claude/jev"
if [[ ! -e "$HOME/.claude/jev/conditions.json" ]]; then
  cp "$REPO/config/conditions.example.json" "$HOME/.claude/jev/conditions.json"
  echo "created   ~/.claude/jev/conditions.json from the example (edit it: these are your own rules)"
else
  echo "kept      ~/.claude/jev/conditions.json (yours)"
fi

if [[ ! -e "$REPO/.env" && -z "${TYPESAFE_API_KEY:-}" && ! -e "$HOME/.config/typesafe/.env" ]]; then
  echo
  echo "No TypeSafe key found. Put it in $REPO/.env (see .env.example) or export TYPESAFE_API_KEY."
fi

if [[ "${1:-}" == "--hooks" ]]; then
  SETTINGS="$HOME/.claude/settings.json"
  [[ -e "$SETTINGS" ]] || echo '{}' > "$SETTINGS"
  cp -p "$SETTINGS" "$SETTINGS.bak-jev-$STAMP"
  python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
hooks = d.setdefault("hooks", {})
entries = {
    "PreToolUse": {"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command",
                   "command": "python3 ~/.claude/skills/jev-orchestrator/hooks/permission_gate.py", "timeout": 10}]},
    "UserPromptSubmit": {"hooks": [{"type": "command",
                         "command": "python3 ~/.claude/skills/jev-orchestrator/hooks/prompt_router.py", "timeout": 15}]},
}
for event, entry in entries.items():
    lst = hooks.setdefault(event, [])
    cmd = entry["hooks"][0]["command"]
    if any(h.get("command") == cmd for g in lst for h in g.get("hooks", [])):
        print("hook      %s already registered" % event)
    else:
        lst.append(entry)
        print("hook      %s registered" % event)
text = json.dumps(d, indent=2) + "\n"
json.loads(text)
open(path, "w").write(text)
PY
  echo "settings backup: $SETTINGS.bak-jev-$STAMP"
fi

echo
echo "done. Start a new Claude Code session so it loads the agents and hooks."
