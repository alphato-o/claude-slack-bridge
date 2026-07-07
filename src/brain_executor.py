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

import slack_files

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.getenv("BRAIN_DIR", "/brain"))
ACK_EMOJI = os.getenv("BRAIN_ACK_EMOJI", "eyes")
REPLY_TIMEOUT = int(os.getenv("BRAIN_REPLY_TIMEOUT", "600"))   # seconds to await the brain
POLL = 0.4
BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
# Animate the reply via Slack's streaming API (native 'text appearing' effect). On by default.
STREAM_REVEAL = os.getenv("BRAIN_STREAM_REVEAL", "true").lower() == "true"
CHUNK_CHARS = int(os.getenv("BRAIN_STREAM_CHUNK", "28"))       # ~chars per animated append


def _chunks(text: str):
    """Split text into streaming chunks at word boundaries (~CHUNK_CHARS each) so the
    reveal animates smoothly without one giant append or hundreds of tiny ones."""
    words, buf, n = text.split(" "), [], 0
    for w in words:
        buf.append(w); n += len(w) + 1
        if n >= CHUNK_CHARS:
            yield " ".join(buf) + " "; buf, n = [], 0
    if buf:
        yield " ".join(buf)


class BrainExecutor:
    """Routes a Slack turn to the single standing Dario brain via a host inbox/outbox."""

    def __init__(self, slack_client: Any, team_id: str = "") -> None:
        self._client = slack_client
        self._team_id = team_id or os.getenv("SLACK_TEAM_ID", "")
        (BRAIN_DIR / "inbox").mkdir(parents=True, exist_ok=True)
        (BRAIN_DIR / "outbox").mkdir(parents=True, exist_ok=True)

    THINKING = "🧠 _Dario is thinking…_"

    async def handle_turn(
        self, channel: str, thread_ts: str, text: str, *,
        user: str = "", user_name: str = "", is_dm: bool = False,
        mentioned: bool = False, message_ts: str = "", files: list | None = None,
    ) -> str:
        evt_id = f"evt_{message_ts or thread_ts or time.time()}".replace(".", "_")
        anchor_ts = thread_ts or message_ts

        # 1) instant ack — 👀 reaction on the source message (no brain round-trip)
        if message_ts:
            try:
                await self._client.reactions_add(channel=channel, timestamp=message_ts, name=ACK_EMOJI)
            except Exception as exc:
                logger.debug("ack reaction failed: %s", exc)

        # 1a) post a "thinking…" placeholder that will MORPH into the answer — makes the
        #     wait feel like a live agent working, not a silent gap.
        placeholder_ts = None
        try:
            r = await self._client.chat_postMessage(
                channel=channel, thread_ts=anchor_ts, text=self.THINKING)
            placeholder_ts = r.get("ts")
        except Exception as exc:
            logger.debug("placeholder post failed: %s", exc)

        # 1b) download any image/file attachments into the bind-mounted brain dir so the
        #     native brain can Read them (Slack url_private needs the bot token).
        attachments = slack_files.download(
            files or [], BRAIN_DIR / "inbox" / f"{evt_id}_files", BOT_TOKEN)

        # 2) write the event to the host inbox (native brain reads it via slack_loop)
        event = {
            "id": evt_id, "channel": channel, "thread_ts": anchor_ts,
            "message_ts": message_ts, "user": user, "user_name": user_name,
            "is_dm": is_dm, "mentioned": mentioned, "text": text,
            "attachments": attachments, "placeholder_ts": placeholder_ts, "t": time.time(),
        }
        inbox_f = BRAIN_DIR / "inbox" / f"{evt_id}.json"
        tmp = inbox_f.with_suffix(".tmp")
        tmp.write_text(json.dumps(event, ensure_ascii=False))
        tmp.rename(inbox_f)                                   # atomic publish
        logger.info("brain-mode: queued %s (chan %s, thread %s)", evt_id, channel, anchor_ts)

        # 3) await the brain's reply file, then MORPH the placeholder into the answer
        reply_f = BRAIN_DIR / "outbox" / f"{evt_id}.reply"
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            if reply_f.exists():
                out = reply_f.read_text()
                try: reply_f.unlink()
                except FileNotFoundError: pass
                logger.info("brain-mode: got reply for %s (%d chars)", evt_id, len(out))
                await self._finish(channel, anchor_ts, placeholder_ts, message_ts, user, out)
                return ""
            await asyncio.sleep(POLL)

        logger.warning("brain-mode: timeout awaiting reply for %s", evt_id)
        await self._finish(channel, anchor_ts, placeholder_ts, message_ts, user,
                           "⏳ Still working on this one — I'll follow up in this thread when it's done.")
        return ""

    async def _finish(self, channel, thread_ts, placeholder_ts, message_ts, user, out: str) -> None:
        """Resolve the placeholder based on the brain's outbox reply.
        - "__posted__"  → brain already posted a rich message itself; remove the placeholder.
        - "" (empty)    → no reply; remove the placeholder.
        - any text      → reveal it. If STREAM_REVEAL, animate via Slack's streaming API
          (native cursor + smooth text-appearing, like the official app); else edit the
          placeholder in place (the morph). Then ✅ done reaction.
        """
        stripped = out.strip()
        if stripped in ("", "__posted__"):
            if placeholder_ts:
                try: await self._client.chat_delete(channel=channel, ts=placeholder_ts)
                except Exception as exc: logger.debug("placeholder delete failed: %s", exc)
            if message_ts and stripped == "__posted__":
                await self._done_react(channel, message_ts)
            return

        revealed = False
        if STREAM_REVEAL:
            revealed = await self._stream_reveal(channel, thread_ts, placeholder_ts, user, out)
        if not revealed:                                   # fallback: single in-place morph
            if placeholder_ts:
                try:
                    await self._client.chat_update(channel=channel, ts=placeholder_ts, text=out)
                except Exception as exc:
                    logger.debug("placeholder update failed (%s) — posting fresh", exc)
                    await self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=out)
            else:
                await self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=out)
        if message_ts:
            await self._done_react(channel, message_ts)

    async def _stream_reveal(self, channel, thread_ts, placeholder_ts, user, out: str) -> bool:
        """Animate the reply using Slack's streaming API — the native 'text appearing'
        effect. Content is pre-computed (brain mode has no live token stream), but the
        animation matches the official Claude app. Returns True on success."""
        # The stream is a NEW message, so drop the static placeholder first.
        if placeholder_ts:
            try: await self._client.chat_delete(channel=channel, ts=placeholder_ts)
            except Exception: pass
        try:
            kw = dict(channel=channel, thread_ts=thread_ts)
            if user and self._team_id:
                kw["recipient_user_id"] = user
                kw["recipient_team_id"] = self._team_id
            resp = await self._client.chat_startStream(**kw)
            stream_ts = resp["ts"]
        except Exception as exc:
            logger.debug("startStream failed (%s) — will fall back to morph", exc)
            return False
        try:
            for chunk in _chunks(out):
                await self._client.chat_appendStream(
                    channel=channel, ts=stream_ts,
                    chunks=[{"type": "markdown_text", "text": chunk}])
            await self._client.chat_stopStream(channel=channel, ts=stream_ts)
            return True
        except Exception as exc:
            logger.debug("stream append/stop failed (%s)", exc)
            try: await self._client.chat_stopStream(channel=channel, ts=stream_ts)
            except Exception: pass
            return False

    async def _done_react(self, channel, message_ts) -> None:
        try:
            await self._client.reactions_add(
                channel=channel, timestamp=message_ts, name="white_check_mark")
        except Exception as exc:
            logger.debug("done reaction failed: %s", exc)
        # ✅ done signal on the user's message (in addition to the 👀 ack)
        if message_ts and stripped not in ("",):
            try:
                await self._client.reactions_add(
                    channel=channel, timestamp=message_ts, name="white_check_mark")
            except Exception as exc:
                logger.debug("done reaction failed: %s", exc)


def channel_mode(config: Any, default_mode: str) -> str:
    """Resolve a channel's mode from its projects.json entry (str legacy → session)."""
    if isinstance(config, dict):
        return config.get("mode", default_mode)
    return default_mode
