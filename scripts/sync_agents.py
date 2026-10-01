#!/usr/bin/env python3
"""Sync the jev-* agent files with config/agents.json (the single source of truth).

Rewrites each agent's frontmatter so that:
  * `description` is always a double-quoted YAML string (an unquoted ": " makes the whole file
    invalid YAML, and Claude Code then silently skips the agent: this is what hid jev-builder,
    jev-debugger, jev-qa and jev-reviewer),
  * `model` and `effort` come from the config, not from whatever the session happens to pin.

Usage: python scripts/sync_agents.py [--check] [--config PATH] [--out DIR]
  (--check exits 1 if any file would change; --out writes the rendered files to DIR instead of in place)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_cfg(path=None) -> dict:
    return json.loads(Path(path or ROOT / "config" / "agents.json").read_text(encoding="utf-8"))


def quote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(text: str, name: str, cfg: dict) -> str:
    """Pure: return the agent file text (LF) with description quoted and model/effort from cfg."""
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
    if not m:
        raise ValueError(f"{name}: no frontmatter")
    head, body = m.group(1), m.group(2)
    spec = cfg["agents"][name]
    out = []
    for line in head.splitlines():
        if line.startswith("description:"):
            line = "description: " + quote(line[len("description:"):])
        elif line.startswith("model:"):
            line = "model: " + cfg["models"][spec["model"]]
        elif line.startswith("effort:"):
            line = "effort: " + spec["effort"]
        out.append(line)
    return "---\n" + "\n".join(out) + "\n---\n" + body


def rewrite(path: Path, cfg: dict) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    return text, render(text, path.stem, cfg)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    cfg_path = out_dir = None
    for flag in ("--config", "--out"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv):
                print(f"{flag} needs a value")
                return 2
            if flag == "--config":
                cfg_path = argv[i + 1]
            else:
                out_dir = Path(argv[i + 1])
    cfg = load_cfg(cfg_path)
    changed = 0
    for path in sorted((ROOT / "agents").glob("jev-*.md")):
        if path.stem not in cfg["agents"]:
            print(f"WARN {path.name}: not in config/agents.json")
            continue
        try:
            old, new = rewrite(path, cfg)
        except ValueError as e:
            raise SystemExit(f"{path}: no frontmatter") from e
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / path.name).write_text(new, encoding="utf-8", newline="\n")
        if old != new:
            changed += 1
            print(("WOULD CHANGE " if check else "updated ") + path.name)
            if not check and out_dir is None:
                path.write_text(new, encoding="utf-8", newline="\n")
    print(f"{changed} file(s) {'need changes' if check else 'updated'}")
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
