#!/usr/bin/env python3
"""
slack_notify.py — fire-and-forget post to a Slack channel from inside the bridge
container, WITH delivery guarantees.

    echo "🟡 SILVER — replied @foo …" \\
        | docker exec -i claude-slack-bridge python slack_notify.py --channel C0BCJM4DLNQ

    docker exec claude-slack-bridge python slack_notify.py \\
        --channel C0BCJM4DLNQ --message "⏭️ SKIP — dry scan"

The bot token stays inside the container (SLACK_BOT_TOKEN); callers never see
it. Message text comes from --message or stdin. Posted via markdown_text so
Slack renders real markdown, with no @channel ping.

Delivery guarantees (added 2026-07-31 after two silent failures):

- Every ``<@U…>`` mention in the text is validated against the TARGET CHANNEL's
  member list. Mentioning a non-member notifies NOBODY (and renders as dead
  text for Connect-external users), so that post is REFUSED (exit 4) with the
  working options listed. Override with --force.
- A DM target (``--channel U…`` or ``--channel D…``) is checked for the
  external-workspace trap: an app-initiated Slack-Connect DM to a user from
  another org who has never messaged this bot lands in acceptance limbo (the
  recipient sees nothing, the API reports success). That send is REFUSED
  (exit 4) unless the DM already has messages FROM the user, or --force.

Exit codes: 0 ok, 1 empty message, 2 no token, 3 Slack API error, 4 refused.
"""

import argparse
import os
import re
import sys

from slack_sdk import WebClient


def _channel_members(client: WebClient, channel: str) -> set[str]:
    members: set[str] = set()
    cursor = None
    while True:
        r = client.conversations_members(channel=channel, limit=200, cursor=cursor)
        members.update(r.get("members", []))
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return members


def _who(client: WebClient, uid: str) -> str:
    try:
        u = client.users_info(user=uid)["user"]
        name = u.get("real_name") or u.get("name") or uid
        team = u.get("team_id", "")
        return f"{uid} ({name}, team {team})"
    except Exception:
        return uid


def main() -> int:
    ap = argparse.ArgumentParser(description="Post a one-shot message to a Slack channel.")
    ap.add_argument("--channel", required=True,
                    help="Channel ID (C…/G…), DM channel (D…), or user ID (U…) to DM")
    ap.add_argument("--message", default=None, help="Message text (default: read from stdin)")
    ap.add_argument("--thread-ts", default=None, help="Optional parent ts to reply in a thread")
    ap.add_argument("--force", action="store_true",
                    help="Send despite failed delivery validation (you accept nobody may see it)")
    args = ap.parse_args()

    text = (args.message if args.message is not None else sys.stdin.read()).strip()
    if not text:
        print("slack_notify: empty message, nothing sent", file=sys.stderr)
        return 1

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("slack_notify: SLACK_BOT_TOKEN not set in the container", file=sys.stderr)
        return 2

    client = WebClient(token=token)
    channel = args.channel
    mentions = set(re.findall(r"<@(U[A-Z0-9]+)>", text))

    try:
        my_team = client.auth_test().get("team_id", "")

        # --- refuse DELETED users anywhere (mention or DM target) ----------------
        # The trap that burned us on 2026-07-31: a stale directory pointed at
        # DELETED ghost accounts (old guest users), so correct-looking sends went
        # to corpses. users:read makes this cheap to catch.
        targets = set(mentions)
        if channel.startswith("U"):
            targets.add(channel)
        if not args.force:
            for uid in sorted(targets):
                try:
                    u = client.users_info(user=uid)["user"]
                except Exception:
                    continue
                if u.get("deleted"):
                    print(f"slack_notify: REFUSED — {uid} ({u.get('real_name') or u.get('name')}) "
                          "is a DELETED account (a stale directory id?). Nothing sent to it can "
                          "ever be seen. Find the person's LIVE id: "
                          "python /app/src/slack_lookup.py --channel <the-channel> --name <name>",
                          file=sys.stderr)
                    return 4

        # --- DM path: user id, or D channel -------------------------------------
        dm_target = channel if channel.startswith("U") else ""
        if channel.startswith("U"):
            channel = client.conversations_open(users=channel)["channel"]["id"]
        if channel.startswith("D") and not args.force:
            hist = client.conversations_history(channel=channel, limit=20)
            other = dm_target or next(
                (m.get("user") for m in hist.get("messages", [])
                 if m.get("user") and not m.get("bot_id")), "")
            them_spoke = any(m.get("user") == other for m in hist.get("messages", []))
            external = False
            if other:
                try:
                    external = client.users_info(user=other)["user"].get("team_id", my_team) != my_team
                except Exception:
                    external = True  # can't verify → treat as external
            if external and not them_spoke:
                print(
                    "slack_notify: REFUSED — this DM targets an EXTERNAL-workspace user "
                    f"({_who(client, other)}) who has never messaged this bot. App-initiated "
                    "Slack-Connect DMs to such users land in acceptance limbo: the API reports "
                    "success but the person sees NOTHING. Working options: (1) reach them on "
                    "Feishu (lark-cli, the Dario identity), (2) @-mention them in a shared "
                    "channel they are a member of, (3) ask Alpha to DM them, (4) --force if you "
                    "truly accept the limbo risk.", file=sys.stderr)
                return 4

        # --- channel path: validate mentions against membership ------------------
        if channel.startswith(("C", "G")) and mentions and not args.force:
            members = _channel_members(client, channel)
            ghosts = sorted(mentions - members)
            if ghosts:
                for g in ghosts:
                    print(f"slack_notify: REFUSED — {_who(client, g)} is NOT a member of "
                          f"{channel}; the mention would notify NOBODY (dead text for "
                          "Connect-external viewers).", file=sys.stderr)
                print(
                    "slack_notify: options: (1) have them invited to the channel first, "
                    "(2) post in a shared channel where they ARE a member, (3) reach them on "
                    "Feishu (lark-cli), (4) --force to post anyway without notifying them.",
                    file=sys.stderr)
                return 4

        resp = client.chat_postMessage(
            channel=channel, markdown_text=text, thread_ts=args.thread_ts,
        )
    except Exception as exc:
        print(f"slack_notify: Slack API error: {exc}", file=sys.stderr)
        return 3

    print(resp.get("ts", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
