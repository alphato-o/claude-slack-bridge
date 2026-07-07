"""
brain_executor.py — the ``mode: brain`` execution path.

Where ``SessionExecutor`` (the existing ``claude_handler`` logic) spawns an isolated
``claude -p`` per channel, ``BrainExecutor`` routes the turn to a SINGLE, already-running
Claude session (Dario's brain, native on the host). It does NOT spawn a model.

Flow:
  1. instant 👀 ack in-thread (no brain round-trip)
  2. write the event as JSON to  <brain_dir>/inbox/evt_<ts>.json   (bind-mounted host dir)
  3. block until the brain writes <brain_dir>/outbox/evt_<ts>.reply  (or times out)
  4. return the reply text — the daemon posts it as the thread reply

This keeps ONE brain across every brain-mode channel: shared memory, glossary, Linear,
meeting archive — the whole accumulated Dario identity, not N amnesiac sandboxes.

Config: a channel is brain-mode when its projects.json entry has ``"mode": "brain"``,
or when DEFAULT_MODE=brain and the entry doesn't override. BRAIN_DIR env points at the
bind-mounted host dir (default /brain, mapped to projectDario/dario/slack).
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.getenv("BRAIN_DIR", "/brain"))
ACK_EMOJI = os.getenv("BRAIN_ACK_EMOJI", "eyes")
REPLY_TIMEOUT = int(os.getenv("BRAIN_REPLY_TIMEOUT", "600"))   # seconds to await the brain
POLL = 0.4


class BrainExecutor:
    """Routes a Slack turn to the single standing Dario brain via a host inbox/outbox."""

    def __init__(self, slack_client: Any) -> None:
        self._client = slack_client
        (BRAIN_DIR / "inbox").mkdir(parents=True, exist_ok=True)
        (BRAIN_DIR / "outbox").mkdir(parents=True, exist_ok=True)

    async def handle_turn(
        self, channel: str, thread_ts: str, text: str, *,
        user: str = "", user_name: str = "", is_dm: bool = False,
        mentioned: bool = False, message_ts: str = "",
    ) -> str:
        evt_id = f"evt_{message_ts or thread_ts or time.time()}".replace(".", "_")
        anchor_ts = thread_ts or message_ts

        # 1) instant ack — a reaction on the source message (no brain round-trip)
        if message_ts:
            try:
                await self._client.reactions_add(channel=channel, timestamp=message_ts, name=ACK_EMOJI)
            except Exception as exc:
                logger.debug("ack reaction failed: %s", exc)

        # 2) write the event to the host inbox
        event = {
            "id": evt_id, "channel": channel, "thread_ts": anchor_ts,
            "message_ts": message_ts, "user": user, "user_name": user_name,
            "is_dm": is_dm, "mentioned": mentioned, "text": text, "t": time.time(),
        }
        inbox_f = BRAIN_DIR / "inbox" / f"{evt_id}.json"
        tmp = inbox_f.with_suffix(".tmp")
        tmp.write_text(json.dumps(event, ensure_ascii=False))
        tmp.rename(inbox_f)                                   # atomic publish
        logger.info("brain-mode: queued %s (chan %s, thread %s)", evt_id, channel, anchor_ts)

        # 3) await the brain's reply file
        reply_f = BRAIN_DIR / "outbox" / f"{evt_id}.reply"
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            if reply_f.exists():
                text_out = reply_f.read_text()
                try: reply_f.unlink()
                except FileNotFoundError: pass
                logger.info("brain-mode: got reply for %s (%d chars)", evt_id, len(text_out))
                # The brain typically posts its own richly-formatted reply via slack_say and
                # writes a sentinel here; an empty/"__posted__" reply means "already posted,
                # don't double-post". Non-empty → daemon posts it as the thread reply.
                return "" if text_out.strip() in ("", "__posted__") else text_out
            await asyncio.sleep(POLL)

        logger.warning("brain-mode: timeout awaiting reply for %s", evt_id)
        return ("⏳ I've got your message and I'm on it — this one's taking longer than usual. "
                "I'll follow up in this thread when it's done.")


def channel_mode(config: Any, default_mode: str) -> str:
    """Resolve a channel's mode from its projects.json entry (str legacy → session)."""
    if isinstance(config, dict):
        return config.get("mode", default_mode)
    return default_mode
