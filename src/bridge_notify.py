#!/usr/bin/env python3
"""
bridge_notify.py — post a message to a Slack thread from INSIDE a Flow-B run,
without holding the bot token.

Flow-B Claude runs with the Slack tokens stripped from its environment (so a
prompt-injection can't exfiltrate them), which also means it can't post to Slack
directly. This helper instead asks the daemon to post: it connects to the
daemon's local Unix socket and sends a one-line ``NOTIFY`` command; the daemon
(which holds the token) does the posting.

Its purpose is to let a job that OUTLIVES a turn return its result to the thread
that started it. Claude backgrounds the job and wires its tail to this CLI:

    nohup sh -c 'python3 tests.py cases.json out.jsonl; \\
        summarize out.jsonl | python /app/src/bridge_notify.py \\
        --channel C0XXXX --thread-ts 1782.456' >/tmp/job.log 2>&1 &

Message text comes from --message or stdin. Exit: 0 ok, 1 empty, 2 socket error,
3 daemon error.
"""

import argparse
import json
import socket
import sys

SOCKET_PATH = "/tmp/slack-bridge.sock"


def main() -> int:
    ap = argparse.ArgumentParser(description="Post to a Slack thread via the bridge daemon.")
    ap.add_argument("--channel", required=True, help="Channel ID, e.g. C0XXXX")
    ap.add_argument("--thread-ts", default=None, help="Thread to post into (the anchor)")
    ap.add_argument("--message", default=None, help="Message text (default: read stdin)")
    args = ap.parse_args()

    text = (args.message if args.message is not None else sys.stdin.read()).strip()
    if not text:
        print("bridge_notify: empty message, nothing sent", file=sys.stderr)
        return 1

    payload = json.dumps({"channel": args.channel, "thread_ts": args.thread_ts, "text": text})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(20)
            s.connect(SOCKET_PATH)
            s.sendall(b"NOTIFY " + payload.encode("utf-8") + b"\n")
            resp = s.recv(16)
    except Exception as exc:
        print(f"bridge_notify: socket error talking to the daemon: {exc}", file=sys.stderr)
        return 2

    if resp.strip() != b"OK":
        print(f"bridge_notify: daemon reported an error: {resp!r}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
