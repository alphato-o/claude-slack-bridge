# Handout: #bot-metricsflare → metrics-desk (Slack brain wiring)

**To: the phicampaign control room (main-brain) and the metrics-desk session.**
**From: the Slack bridge (Bran).** Bridge-side wiring is DONE and deployed; this
document is the contract plus the two small steps the desk side owns.

## What changed

Slack channel **#bot-metricsflare (`C0BJ18A9XKQ`)** no longer spawns an isolated
`claude -p`. Every @-mention of the bot there is now delivered to **the LIVE
metrics-desk session** (SESSIONS.md uuid `0bc97093`) through a file handoff, and
the desk's reply goes back into the Slack thread. One desk, one timeline: Slack
questions land in the same session that owns `tools/metrics/`, so answers can
never contradict what the desk actually did.

The bridge acks instantly in Slack (👀 reaction + "metrics-desk is thinking…")
and handles all posting/streaming; the desk only reads events and produces
replies.

## The handoff directory

One dir, two views (bind mount):

| Side | Path |
|---|---|
| Desk / host | `~/Dev/ccplayground/phicampaign/desks/metrics-desk/slack/` |
| Bridge / container | `/brain/metrics-desk/` |

Layout: `inbox/` (bridge → desk, one JSON per event), `outbox/` (desk → bridge),
`done` (desk's processed-id ledger, desk-owned).

## Event format (inbox/evt_<ts>.json)

```json
{
  "id": "evt_1784904534_075949",
  "channel": "C0BJ18A9XKQ",
  "thread_ts": "1784904534.075949",
  "message_ts": "1784904534.075949",
  "user": "U0A7TUK37L3",
  "user_name": "Wood",
  "is_dm": false,
  "mentioned": true,
  "text": "the @-mention text, mention tag stripped",
  "attachments": ["/brain/metrics-desk/inbox/evt_..._files/photo.png"],
  "placeholder_ts": "1784904540.001",
  "t": 1784904534.2
}
```

Notes:
- `attachments` are CONTAINER paths; on the host, replace the prefix
  `/brain/metrics-desk/` with the handoff dir path above.
- Events are atomic (written as `.tmp`, renamed). Never read `.tmp` files.
- The bridge does NOT delete inbox events; the desk appends the id to `done`
  and may archive/delete handled events. Failure direction is redelivery
  (an un-done event stays visible), never loss.

## Reply protocol (outbox/<id>.reply)

Write ONE file named after the event id. Three forms:

1. **Reply text** (the normal case): the bridge posts it into the Slack thread
   with the streaming reveal. Slack markdown is fine.
2. **`__posted__`**: the desk posted its own (richer) message already, e.g. via
   `printf '%s\n' "line1" "line2" | docker exec -i claude-slack-bridge \
   python /app/src/bridge_notify.py --channel <channel> --thread-ts <thread_ts>`
   The bridge then just cleans up its placeholder and ✅-reacts.
3. **Empty file**: no reply wanted (bridge cleans up silently).

Timing: the bridge waits **900 s** per event. If the desk needs longer, reply
fast with a one-liner ("on it, ~10 min") and follow up later in the same thread
via `bridge_notify.py` with the event's `thread_ts`. On timeout the bridge posts
"still working, will follow up" on the desk's behalf, so a late `bridge_notify`
follow-up is expected and correct.

**Pipelining:** the inbox IS the queue. Under load, multiple events accumulate;
process strictly oldest-first, one at a time, each to its own `.reply`. Every
waiting turn keeps its own 900 s clock, so a short ack-reply per event beats one
long silence.

## Desk step 1: the ear (slack_loop.sh)

Create `desks/metrics-desk/slack/slack_loop.sh` (same pattern as Dario's ear on
Arya: singleton lock, oldest-first, done-ledger, exit-to-wake):

```sh
#!/bin/sh
# metrics-desk ear: block until the oldest un-done Slack event, print it, exit.
# Run as a TRACKED BACKGROUND TASK inside the desk session; its exit wakes the
# desk with the event JSON as output. Re-arm after handling each event.
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir "$DIR/.loop.lock" 2>/dev/null || exit 0          # singleton
trap 'rmdir "$DIR/.loop.lock"' EXIT
touch "$DIR/done"
while true; do
  for f in $(ls "$DIR"/inbox/evt_*.json 2>/dev/null | sort); do
    id=$(basename "$f" .json)
    grep -qx "$id" "$DIR/done" || { cat "$f"; exit 0; }
  done
  sleep 2
done
```

## Desk step 2: bootstrap prompt addition (control room owns the edit)

Append to `tools/session-bootstrap/metrics-desk.md`:

> **Slack ear (#bot-metricsflare `C0BJ18A9XKQ`):** you are this channel's brain.
> Keep `desks/metrics-desk/slack/slack_loop.sh` armed as a tracked background
> task at all times. When it wakes you with an event JSON: (1) read it (channel,
> user_name, thread_ts, text, attachments); (2) act with your full desk state;
> (3) reply: write the text to `desks/metrics-desk/slack/outbox/<id>.reply`, or
> post rich content yourself via bridge_notify (then write `__posted__`);
> (4) `echo <id> >> desks/metrics-desk/slack/done`; (5) re-arm slack_loop.sh as
> a fresh tracked background task. Oldest event first when several queue up. If
> a task will run long, reply a one-line ack immediately and follow up in the
> same thread via bridge_notify when done. House style per CORE.md binds (full
> width Chinese punctuation, space before URLs, restrained emoji).

## Control room checklist

- [ ] Add the bootstrap addition above to `tools/session-bootstrap/metrics-desk.md`.
- [ ] Have the metrics-desk create `slack_loop.sh` (step 1) and arm it.
- [ ] Note the Slack ear in `SESSIONS.md` under metrics-desk (surface + re-arm
      policy), so heartbeat checks include "is the ear armed".
- [ ] Smoke test: Alpha @-mentions the bot in #bot-metricsflare; desk sees the
      event, replies; the answer lands in-thread.

## Engagement rules on the channel (bridge-enforced, FYI)

- @-mention of the bot → delivered to the desk. Thread follow-ups keep flowing
  without re-mention only while the thread is strictly 1:1 (bot + one human);
  once a 2nd party joins (another human OR another bot, e.g. Dario), every
  delivery requires an explicit @-mention.
- A message @-mentioning someone else is never delivered to the desk.
- Old behavior note: the channel's previous bridge session (default cwd, no
  codebase) is retired; history for it lives in the bridge journal only.
