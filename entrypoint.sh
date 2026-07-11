#!/bin/sh
set -e

# Link the host's Claude credentials into the container's own ~/.claude. The host
# dir is bind-mounted read-only at /host-claude (a DIRECTORY, so the link always
# resolves to the host's current .credentials.json even after Claude's atomic-
# rename token refresh — a single-file mount would detach and "vanish" instead).
# The CLI keeps writing its session data to the real /home/appuser/.claude.
mkdir -p /home/appuser/.claude
if [ -f /host-claude/.credentials.json ]; then
    ln -sf /host-claude/.credentials.json /home/appuser/.claude/.credentials.json
else
    echo "warning: /host-claude/.credentials.json not found — claude will be 'Not logged in'."
fi

# The Agent SDK / CLI expect a ~/.claude.json config file; seed an empty one if
# absent so it doesn't warn/backup on every run. (Persists in the claude-home volume.)
[ -f /home/appuser/.claude.json ] || echo '{}' > /home/appuser/.claude.json

if [ -n "$GITHUB_TOKEN" ] || [ -n "$GH_TOKEN" ]; then
    gh auth setup-git 2>&1 || echo "warning: gh auth setup-git failed; git push to github.com over HTTPS may not authenticate"
fi

exec "$@"
