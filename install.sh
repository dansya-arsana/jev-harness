#!/bin/sh
# Jev harness installer wrapper: finds Python 3.9+ and runs scripts/onboard.py.
#   --legacy   run the old zsh installer (scripts/legacy/install.sh)
#   --hooks    shorthand for --components skill,agents,gate,dispatch,router
# JEV_ONBOARD_PY / JEV_LEGACY_SCRIPT override the targets (tests only).
set -eu
REPO=$(cd "$(dirname "$0")" && pwd)

legacy=0
for a in "$@"; do [ "$a" = "--legacy" ] && legacy=1; done
if [ "$legacy" = 1 ]; then
  command -v zsh >/dev/null 2>&1 || { echo "zsh is required for --legacy"; exit 1; }
  # drop --legacy and rewrite nothing else
  for a in "$@"; do shift; [ "$a" = "--legacy" ] || set -- "$@" "$a"; done
  exec zsh "${JEV_LEGACY_SCRIPT:-$REPO/scripts/legacy/install.sh}" "$@"
fi

# rewrite --hooks
for a in "$@"; do
  shift
  if [ "$a" = "--hooks" ]; then set -- "$@" --components skill,agents,gate,dispatch,router; else set -- "$@" "$a"; fi
done

for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$c" "${JEV_ONBOARD_PY:-$REPO/scripts/onboard.py}" "$@"
  fi
done
echo "jev-harness needs Python 3.9+ (python.org; on Windows the Microsoft Store python3 stub does not count)"
exit 1
