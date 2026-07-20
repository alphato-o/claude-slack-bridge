#!/bin/sh
# Visible long-running refresher for the bridge container's Claude creds.
#
# Unlike an in-agent cron (invisible to you) or a LaunchAgent (out of sight),
# this is a plain background process you can see and control:
#
#   Start:  nohup sh scripts/refresh-bridge-creds-loop.sh >/dev/null 2>&1 &
#   Watch:  tail -f ~/.claude-bridge-creds.log
#   Check:  ps aux | grep -v grep | grep refresh-bridge-creds-loop
#   Stop:   pkill -f refresh-bridge-creds-loop
#
# Each cycle runs refresh-bridge-creds.sh (exports the macOS Keychain token into
# the host file + container) then sleeps INTERVAL seconds. No token is logged.
INTERVAL="${INTERVAL:-3600}"
LOG="${LOG:-$HOME/.claude-bridge-creds.log}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "$(date '+%F %T') === loop up (pid $$, every ${INTERVAL}s, script=$HERE) ===" >> "$LOG"
while true; do
    sh "$HERE/refresh-bridge-creds.sh" >> "$LOG" 2>&1
    sleep "$INTERVAL"
done
