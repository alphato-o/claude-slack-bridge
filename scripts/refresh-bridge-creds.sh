#!/bin/sh
# Keep the claude-slack-bridge container authenticated on a macOS host.
#
# Claude Code stores its live OAuth token in the macOS Keychain, which the Linux
# container cannot read. The container instead reads ~/.claude/.credentials.json,
# a file that goes stale because it is not the live store. When the container's
# token expires it fails every turn with "OAuth session expired and could not be
# refreshed" (surfaced by the Agent SDK as the opaque "error result: success").
#
# Run hourly by a LaunchAgent, this script keeps the container fresh:
#   (1) nudge claude to refresh the Keychain token if it is near expiry
#       (the host Keychain stays the single refresher -> no dual-refresh
#        rotation conflict between host and container),
#   (2) export the fresh creds into the host file (read on container restart)
#       and into the running container's live creds.
# No token is ever printed or logged.
set -u

CLAUDE="${CLAUDE_BIN:-/Users/fydeos/.local/bin/claude}"
SECURITY=/usr/bin/security
DOCKER="${DOCKER_BIN:-/usr/local/bin/docker}"
SVC="Claude Code-credentials"
HOST_CREDS="$HOME/.claude/.credentials.json"
CONTAINER="${BRIDGE_CONTAINER:-claude-slack-bridge}"

# (1) refresh the Keychain token (claude refreshes on start when near expiry).
"$CLAUDE" -p "ok" >/dev/null 2>&1 || true

# (2a) export Keychain -> host file (atomic). The container reads this on restart.
if "$SECURITY" find-generic-password -s "$SVC" -w > "$HOST_CREDS.tmp" 2>/dev/null; then
    chmod 600 "$HOST_CREDS.tmp" && mv "$HOST_CREDS.tmp" "$HOST_CREDS"
    echo "$(date '+%Y-%m-%d %H:%M:%S') host file refreshed"
else
    rm -f "$HOST_CREDS.tmp"
    echo "$(date '+%Y-%m-%d %H:%M:%S') WARN could not read Keychain creds"
fi

# (2b) push Keychain -> container live creds (atomic within the container).
"$SECURITY" find-generic-password -s "$SVC" -w 2>/dev/null | \
  "$DOCKER" exec -i "$CONTAINER" sh -c \
  'cat > /home/appuser/.claude/.credentials.json.tmp && chmod 600 /home/appuser/.claude/.credentials.json.tmp && mv /home/appuser/.claude/.credentials.json.tmp /home/appuser/.claude/.credentials.json' \
  >/dev/null 2>&1 \
  && echo "$(date '+%Y-%m-%d %H:%M:%S') container creds refreshed" \
  || echo "$(date '+%Y-%m-%d %H:%M:%S') WARN container not updated (down?)"
