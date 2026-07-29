"""slack_lookup.py — resolve a person to a Slack user id via the bridge's bot token.

For brain sessions that need to @-mention someone (``<@Uxxxx>``) but only hold an
email or a partial name. Run from any host session:

    docker exec claude-slack-bridge python /app/src/slack_lookup.py --email who@fydeos.io
    docker exec claude-slack-bridge python /app/src/slack_lookup.py --name "partial name"

Prints one match per line: ``<id>  <real_name>  <display_name>  <email>``.
Exit 0 when at least one match printed, 1 otherwise.

Reach: the bot's OWN workspace only. Slack-Connect external people (e.g. @phi.cc
seen from a fydeos.io app) are NOT found here — resolve those via Dario's people
directory or ask Dario (consult-dario skill). Requires the ``users:read`` +
``users:read.email`` bot scopes (added 2026-07-29).
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.getenv("SLACK_BOT_TOKEN", "")


def _api(method: str, **params) -> dict:
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    return json.load(urllib.request.urlopen(req))


def _row(u: dict) -> str:
    p = u.get("profile", {})
    return "\t".join([
        u.get("id", "?"),
        p.get("real_name") or u.get("real_name") or "",
        p.get("display_name") or "",
        p.get("email") or "",
    ])


def by_email(email: str) -> int:
    r = _api("users.lookupByEmail", email=email)
    if r.get("ok"):
        print(_row(r["user"]))
        return 0
    print(f"no match ({r.get('error')}) — external-workspace people need the "
          f"Dario directory instead", file=sys.stderr)
    return 1


def by_name(needle: str, max_pages: int = 10) -> int:
    needle_l = needle.lower()
    cursor, found = "", 0
    for _ in range(max_pages):
        kw = {"limit": 200}
        if cursor:
            kw["cursor"] = cursor
        r = _api("users.list", **kw)
        if not r.get("ok"):
            print(f"users.list failed: {r.get('error')}", file=sys.stderr)
            break
        for u in r.get("members", []):
            if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
                continue
            p = u.get("profile", {})
            hay = " ".join(filter(None, [
                u.get("name"), u.get("real_name"), p.get("real_name"),
                p.get("display_name"), p.get("email")])).lower()
            if needle_l in hay:
                print(_row(u))
                found += 1
        cursor = r.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    if not found:
        print("no match in this workspace — try the Dario directory", file=sys.stderr)
    return 0 if found else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", help="exact email lookup (own workspace only)")
    ap.add_argument("--name", help="case-insensitive substring over member names")
    args = ap.parse_args()
    if not TOKEN:
        print("SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1
    if args.email:
        return by_email(args.email)
    if args.name:
        return by_name(args.name)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
