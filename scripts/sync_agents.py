#!/usr/bin/env python3
"""Sync the jev-* agent files with config/agents.json (the single source of truth).

Rewrites each agent's frontmatter so that:
  * `description` is always a double-quoted YAML string (an unquoted ": " makes the whole file
    invalid YAML, and Claude Code then silently skips the agent: this is what hid jev-builder,
    jev-debugger, jev-qa and jev-reviewer),
  * `model` and `effort` come from the config, not from whatever the session happens to pin.

Usage: python scripts/sync_agents.py [--check]   (--check exits 1 if any file would change)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "agents.json").read_text(encoding="utf-8"))


def quote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def rewrite(path: Path) -> tuple[str, str]:
    name = path.stem
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
    if not m:
        raise SystemExit(f"{path}: no frontmatter")
    head, body = m.group(1), m.group(2)
    spec = CFG["agents"][name]
    out = []
    for line in head.splitlines():
        if line.startswith("description:"):
            line = "description: " + quote(line[len("description:"):])
        elif line.startswith("model:"):
            line = "model: " + CFG["models"][spec["model"]]
        elif line.startswith("effort:"):
            line = "effort: " + spec["effort"]
        out.append(line)
    return text, "---\n" + "\n".join(out) + "\n---\n" + body


def main() -> int:
    check = "--check" in sys.argv
    changed = 0
    for path in sorted((ROOT / "agents").glob("jev-*.md")):
        if path.stem not in CFG["agents"]:
            print(f"WARN {path.name}: not in config/agents.json")
            continue
        old, new = rewrite(path)
        if old != new:
            changed += 1
            print(("WOULD CHANGE " if check else "updated ") + path.name)
            if not check:
                path.write_text(new, encoding="utf-8", newline="\n")
    print(f"{changed} file(s) {'need changes' if check else 'updated'}")
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
