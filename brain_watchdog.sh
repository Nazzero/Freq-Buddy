#!/usr/bin/env bash
# Keep brain.py alive. The brain's own loop now catches per-turn errors, but a
# hard death (OOM kill, SIGKILL, segfault in a child) can still take the process
# down. This respawns it so the chat never gets permanently stuck with no brain
# polling. Run under setsid from start.sh.
cd "$(dirname "$0")/.."
VENV=.venv/bin
LOG=/tmp/sidekick_brain.log

while true; do
  echo "[watchdog] starting brain.py at $(date -Is)" >> "$LOG"
  $VENV/python -u chartsidekick/brain.py < /dev/null >> "$LOG" 2>&1
  code=$?
  echo "[watchdog] brain.py exited (code=$code) at $(date -Is); restarting in 2s" >> "$LOG"
  sleep 2
done
