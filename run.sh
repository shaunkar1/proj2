#!/bin/bash
# Machina De-ID: run one attempt end to end on this machine (streaming, resumable).
#   bash code/run.sh <attempt-number> [extra args for "deid.cli run"]
# Needs: state/dest.txt  (gs://builders-machina-submissions-1120/teams/<team-id>/)
#        state/token.txt (1-hour upload token).  While this script runs it also watches the
#        clipboard: copying the track page's "upload commands" renews the token file.
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

# token watcher: pick up a fresh token whenever the upload commands are copied
if command -v pbpaste >/dev/null 2>&1; then
  (
    while kill -0 $$ 2>/dev/null; do
      c="$(pbpaste 2>/dev/null | sed -n "s/.*MACHINA_UPLOAD_TOKEN=['\"]\{0,1\}\([^'\" ]*\).*/\1/p" | head -1)"
      if [ "${#c}" -gt 200 ] && [ "$c" != "$(cat state/token.txt 2>/dev/null)" ]; then
        printf '%s' "$c" > state/token.txt
        echo "[token] new upload token taken from the clipboard at $(date +%H:%M:%S)" >> "$LOG"
      fi
      sleep 5
    done
  ) &
fi

cd code || exit 1
export PYTHONUNBUFFERED=1
if [ ! -f "../state/go-$ATTEMPT" ]; then
  echo "=== trial: 120 files, saved locally only" | tee -a "../$LOG"
  ../.venv/bin/python -m deid.cli run --attempt "trial-$ATTEMPT" --state ../state --dry "../state/dryout-$ATTEMPT" \
      --sample 120 2>&1 | tee -a "../$LOG"
  echo "=== trial done $(date +%H:%M:%S); waiting for state/go-$ATTEMPT (continues by itself after 20 min)" | tee -a "../$LOG"
  for i in $(seq 1 240); do [ -f "../state/go-$ATTEMPT" ] && break; sleep 5; done
fi
echo "=== full run -> $DEST $(date +%H:%M:%S)" | tee -a "../$LOG"
../.venv/bin/python -m deid.cli run --attempt "$ATTEMPT" --state ../state --dest "$DEST" \
    --token-file ../state/token.txt "$@" 2>&1 | tee -a "../$LOG"
../.venv/bin/python -m deid.cli finish --attempt "$ATTEMPT" --state ../state --dest "$DEST" \
    --token-file ../state/token.txt --code-dir . 2>&1 | tee -a "../$LOG"
echo "=== run.sh finished $(date); submit: $DEST" | tee -a "../$LOG"
