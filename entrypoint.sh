#!/bin/sh
set -e

mkdir -p /home/appuser/.claude
# Authentication. Preferred: a long-lived OAuth token from `claude setup-token`
# (valid ~1 year), passed in as CLAUDE_CODE_OAUTH_TOKEN via .env. It needs no
# host Keychain and never silently expires, so it's the durable path — remove any
# stale host-creds symlink/file so an expiring session can't shadow the token.
# Fallback (no token set): symlink the host's ~/.credentials.json. The host dir is
# bind-mounted read-only at /host-claude (a DIRECTORY, so the link resolves to the
# host's current file even after Claude's atomic-rename refresh — a single-file
# mount would detach and "vanish"). Note: on macOS the live token lives in the
# Keychain, not this file, so the fallback goes stale; prefer the token.
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    rm -f /home/appuser/.claude/.credentials.json
    echo "auth: using CLAUDE_CODE_OAUTH_TOKEN (long-lived; no host Keychain needed)"
elif [ -f /host-claude/.credentials.json ]; then
    ln -sf /host-claude/.credentials.json /home/appuser/.claude/.credentials.json
else
    echo "warning: no CLAUDE_CODE_OAUTH_TOKEN and no /host-claude/.credentials.json — claude will be 'Not logged in'."
fi

# The Agent SDK / CLI expect a ~/.claude.json config file; seed an empty one if
# absent so it doesn't warn/backup on every run. (Persists in the claude-home volume.)
[ -f /home/appuser/.claude.json ] || echo '{}' > /home/appuser/.claude.json

if [ -n "$GITHUB_TOKEN" ] || [ -n "$GH_TOKEN" ]; then
    gh auth setup-git 2>&1 || echo "warning: gh auth setup-git failed; git push to github.com over HTTPS may not authenticate"
fi

exec "$@"
