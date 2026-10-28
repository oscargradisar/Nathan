#!/usr/bin/env bash
# Launch Claude Code in full-auto mode on the Nathan roadmap.
# It runs every command and edit without asking for approval, so only run it inside this project folder.
#
#   ./autopilot.sh            interactive: you can watch, and type to steer at any time
#   ./autopilot.sh --headless runs unattended in the background, logging to autopilot.log
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env
[ -d .git ] || { git init -q && git add . && git commit -qm "Nathan scaffold"; }

if [ "${1:-}" = "--headless" ]; then
  nohup claude -p "/autopilot" --dangerously-skip-permissions > autopilot.log 2>&1 &
  echo "Autopilot running in the background (pid $!). Follow along: tail -f autopilot.log"
else
  exec claude --dangerously-skip-permissions "/autopilot"
fi
