#!/usr/bin/env sh
# Agent Duet installer for macOS / Linux. Extra arguments go to scripts/install.py,
# for example:  sh install.sh --default-mode work
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    exec "$PY" -X utf8 "$DIR/scripts/install.py" "$@"
  fi
done
echo "Agent Duet needs Python 3.10+ on PATH (python3). Install it, then rerun: sh install.sh" >&2
exit 1
