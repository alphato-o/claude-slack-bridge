#!/usr/bin/env python3
"""
slack_notify.py — fire-and-forget post to a Slack channel from inside the bridge
container.

Unlike ``ask_on_slack`` (which posts *and blocks* waiting for a human reply),
this is a one-shot notification: post a message and exit. It's meant to be called
by project sessions / session-managed crons that want to push a result to Slack
without holding the bot token themselves and without waiting for a reply:

    echo "🟡 SILVER — replied @foo …" \\
        | docker exec -i claude-slack-bridge python slack_notify.py --channel C0BCJM4DLNQ

    docker exec claude-slack-bridge python slack_notify.py \\
        --channel C0BCJM4DLNQ --message "⏭️ SKIP — dry scan"

The bot token stays inside the container (read from SLACK_BOT_TOKEN); callers
never see it. Message text comes from --message or stdin. Posted via the
markdown_text param so Slack renders real markdown, with no @channel ping.
Exit codes: 0 ok, 1 empty message, 2 no token, 3 Slack API error.
"""

import argparse
import os
import sys

from slack_sdk import WebClient


def main() -> int:
    ap = argparse.ArgumentParser(description="Post a one-shot message to a Slack channel.")
    ap.add_argument("--channel", required=True, help="Channel ID, e.g. C0BCJM4DLNQ")
    ap.add_argument("--message", default=None, help="Message text (default: read from stdin)")
    ap.add_argument("--thread-ts", default=None, help="Optional parent ts to reply in a thread")
    args = ap.parse_args()

    text = (args.message if args.message is not None else sys.stdin.read()).strip()
    if not text:
        print("slack_notify: empty message, nothing sent", file=sys.stderr)
        return 1

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("slack_notify: SLACK_BOT_TOKEN not set in the container", file=sys.stderr)
        return 2

    try:
        resp = WebClient(token=token).chat_postMessage(
            channel=args.channel, markdown_text=text, thread_ts=args.thread_ts,
        )
    except Exception as exc:
        print(f"slack_notify: Slack API error: {exc}", file=sys.stderr)
        return 3

    print(resp.get("ts", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
