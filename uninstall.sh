#!/bin/sh
# Jev harness uninstall wrapper: runs scripts/onboard.py --uninstall with Python 3.9+.
#   --legacy   run the old zsh uninstaller (scripts/legacy/uninstall.sh)
# JEV_ONBOARD_PY / JEV_LEGACY_SCRIPT override the targets (tests only).
set -eu
REPO=$(cd "$(dirname "$0")" && pwd)

legacy=0
for a in "$@"; do [ "$a" = "--legacy" ] && legacy=1; done
if [ "$legacy" = 1 ]; then
  command -v zsh >/dev/null 2>&1 || { echo "zsh is required for --legacy"; exit 1; }
  for a in "$@"; do shift; [ "$a" = "--legacy" ] || set -- "$@" "$a"; done
  exec zsh "${JEV_LEGACY_SCRIPT:-$REPO/scripts/legacy/uninstall.sh}" "$@"
fi

for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$c" "${JEV_ONBOARD_PY:-$REPO/scripts/onboard.py}" --uninstall "$@"
  fi
done
echo "jev-harness needs Python 3.9+ (python.org; on Windows the Microsoft Store python3 stub does not count)"
exit 1
