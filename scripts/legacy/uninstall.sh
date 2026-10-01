#!/bin/zsh
# LEGACY: superseded by scripts/onboard.py; writes no manifest
# Remove the Jev harness links and hook entries from ~/.claude. Leaves your conditions.json and logs in place.
set -euo pipefail

REPO="${0:A:h:h:h}"

unlink_if_ours() {
  local dst="$1"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$REPO"/* ]]; then
    rm "$dst" && echo "removed   $dst"
  fi
}

unlink_if_ours "$HOME/.claude/skills/jev-orchestrator"
for f in "$REPO"/agents/jev-*.md; do
  unlink_if_ours "$HOME/.claude/agents/${f:t}"
done

SETTINGS="$HOME/.claude/settings.json"
if [[ -e "$SETTINGS" ]]; then
  cp -p "$SETTINGS" "$SETTINGS.bak-jev-uninstall-$(date +%Y%m%d-%H%M%S)"
  python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
d = json.load(open(path))
ours = ("jev-orchestrator/hooks/permission_gate.py", "jev-orchestrator/hooks/dispatch_router.py", "jev-orchestrator/hooks/prompt_router.py")
for event, groups in list(d.get("hooks", {}).items()):
    kept = []
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not any(o in h.get("command", "") for o in ours)]
        if g["hooks"]:
            kept.append(g)
    d["hooks"][event] = kept
open(path, "w").write(json.dumps(d, indent=2) + "\n")
print("hooks     removed from settings.json")
PY
fi
echo "done. ~/.claude/jev/ (your conditions and logs) was left in place."
