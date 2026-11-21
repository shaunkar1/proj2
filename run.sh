#!/bin/bash
# Machina De-ID: run one attempt end to end on this machine (streaming, resumable).
#   bash code/run.sh <attempt-number> [extra args for "deid.cli run"]
# Needs: state/dest.txt  (gs://builders-machina-submissions-1120/teams/<team-id>/)
#        state/token.txt (1-hour upload token; replace the file content to renew)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
ATTEMPT="${1:-1}"
[ $# -gt 0 ] && shift
cd "$ROOT" || exit 1
mkdir -p state logs
LOG="logs/attempt-$ATTEMPT.log"
{
echo "=== run.sh attempt $ATTEMPT $(date)"
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv || exit 1; fi
.venv/bin/python -m pip -q install --upgrade pip >/dev/null 2>&1
for pkg in "pikepdf>=8,<11" "pyahocorasick>=2.0" "py7zr>=0.20"; do
  .venv/bin/python -m pip -q install "$pkg" >/dev/null 2>&1 || echo "WARN: could not install $pkg"
done
.venv/bin/python -c "import sys; print('python', sys.version.split()[0])"
.venv/bin/python -c "import pikepdf; print('pikepdf', pikepdf.__version__)" 2>&1 | tail -1
.venv/bin/python -c "import ahocorasick; print('pyahocorasick ok')" 2>&1 | tail -1
.venv/bin/python -c "import py7zr; print('py7zr ok')" 2>&1 | tail -1
} 2>&1 | tee -a "$LOG"
DEST_BASE="$(tr -d ' \r\n' < state/dest.txt 2>/dev/null)"
if [ -z "$DEST_BASE" ]; then echo "state/dest.txt is missing (gs://.../teams/<team-id>/)" | tee -a "$LOG"; exit 1; fi
DEST="${DEST_BASE%/}/attempt-$ATTEMPT/"
if command -v caffeinate >/dev/null 2>&1; then caffeinate -dims -w $$ & fi
cd code || exit 1
export PYTHONUNBUFFERED=1
../.venv/bin/python -m deid.cli run --attempt "$ATTEMPT" --state ../state --dest "$DEST" \
    --token-file ../state/token.txt "$@" 2>&1 | tee -a "../$LOG"
../.venv/bin/python -m deid.cli finish --attempt "$ATTEMPT" --state ../state --dest "$DEST" \
    --token-file ../state/token.txt --code-dir . 2>&1 | tee -a "../$LOG"
echo "=== run.sh finished $(date); submit: $DEST" | tee -a "../$LOG"
