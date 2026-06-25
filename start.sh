#!/usr/bin/env bash
# Start Chart Sidekick: server + brain, then open browser.
set -e
cd "$(dirname "$0")/.."
VENV=.venv/bin

pkill -f "chartsidekick/server.py" 2>/dev/null || true
pkill -f "chartsidekick/brain_watchdog.sh" 2>/dev/null || true
pkill -f "chartsidekick/brain.py" 2>/dev/null || true
sleep 1

setsid $VENV/python -u chartsidekick/server.py < /dev/null > /tmp/sidekick_server.log 2>&1 &
# brain runs under a watchdog so a hard crash (OOM/SIGKILL) auto-restarts it.
setsid bash chartsidekick/brain_watchdog.sh < /dev/null >> /tmp/sidekick_brain.log 2>&1 &
sleep 4

echo "Chart Sidekick: http://127.0.0.1:8777/"
echo "server log: /tmp/sidekick_server.log   brain log: /tmp/sidekick_brain.log"
xdg-open http://127.0.0.1:8777/ 2>/dev/null || google-chrome http://127.0.0.1:8777/ 2>/dev/null &
