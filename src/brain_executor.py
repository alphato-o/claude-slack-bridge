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
import re
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


_MD_BOLD  = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_MD_UBOLD = re.compile(r"__(?=\S)([^\n]+?)(?<=\S)__")
_MD_STRIKE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")

def _md_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn for the NON-streaming reveal path.

    The brain writes outbox replies in Markdown because the streaming path sends them as
    ``markdown_text``. This fallback posts them as ``text=``, which Slack parses as *mrkdwn*,
    where ``*x*`` is bold and ``**x**`` is literal asterisks. Without this, the same reply
    rendered bold when streaming worked and italic (or raw ``**``) when it did not, decided at
    runtime by whether chat.startStream happened to succeed. Observed 2026-07-30: a 3.5k reply
    landed with 20 italic spans and zero bold. Keep in sync with to_markdown() in slack_say.py.
    """
    out = _MD_BOLD.sub(r"*\1*", text or "")
    out = _MD_UBOLD.sub(r"*\1*", out)
    out = _MD_STRIKE.sub(r"~\1~", out)
    return out


class BrainExecutor:
    """Routes a Slack turn to a standing brain session via a host inbox/outbox.

    One instance per brain: the classic single-brain deployment (Dario on Arya,
    DEFAULT_MODE=brain, dir from BRAIN_DIR) and per-channel brains (a projects.json
    entry with ``"mode": "brain"`` plus ``"brain_dir"``/``"brain_name"``, e.g. the
    phicampaign metrics-desk) both go through here — same contract, different dir."""

    def __init__(
        self, slack_client: Any, team_id: str = "",
        brain_dir: str | None = None, name: str | None = None,
        reply_timeout: int | None = None,
    ) -> None:
        self._client = slack_client
        self._team_id = team_id or os.getenv("SLACK_TEAM_ID", "")
        self._dir = Path(brain_dir) if brain_dir else BRAIN_DIR
        self._name = name or os.getenv("BRAIN_NAME", "Dario")
        self._timeout = reply_timeout or REPLY_TIMEOUT
        self._thinking_md = f"🧠 _{self._name} is thinking…_"
        self._thinking_status = f"🧠 {self._name} is thinking…"
        (self._dir / "inbox").mkdir(parents=True, exist_ok=True)
        (self._dir / "outbox").mkdir(parents=True, exist_ok=True)

    async def handle_turn(
        self, channel: str, thread_ts: str, text: str, *,
        user: str = "", user_name: str = "", is_dm: bool = False,
        mentioned: bool = False, message_ts: str = "", files: list | None = None,
    ) -> str:
        evt_id = f"evt_{message_ts or thread_ts or time.time()}".replace(".", "_")
        anchor_ts = thread_ts or message_ts

        # 1) instant ack — 👀 reaction on the source message (no brain round-trip).
        #    WARNING on failure, not debug: a silent ack failure (e.g. the app missing
        #    the reactions:write scope) looks like the bot ignored the user.
        if message_ts:
            try:
                await self._client.reactions_add(channel=channel, timestamp=message_ts, name=ACK_EMOJI)
            except Exception as exc:
                logger.warning("ack reaction failed (missing reactions:write scope?): %s", exc)

        # 1a) SHIMMER — assistant.threads.setStatus animates the app title ("is
        #     working"). Only meaningful in DM/assistant surfaces: on a CHANNEL thread
        #     the API returns ok:true but renders NOTHING (learned 2026-07-29 — 15
        #     silent minutes on #bot-metricsflare), so channels always get the visible
        #     "🧠 thinking…" placeholder instead.
        placeholder_ts = shimmer = None
        status_ok = False
        if is_dm:
            status_ok = await self._set_status(channel, anchor_ts, self._thinking_status)
        if status_ok:
            shimmer = asyncio.ensure_future(self._shimmer(channel, anchor_ts))
        else:
            try:
                r = await self._client.chat_postMessage(
                    channel=channel, thread_ts=anchor_ts, text=self._thinking_md)
                placeholder_ts = r.get("ts")
            except Exception as exc:
                logger.debug("placeholder post failed: %s", exc)

        # 1b) download any image/file attachments into the bind-mounted brain dir so the
        #     native brain can Read them (Slack url_private needs the bot token).
        attachments = slack_files.download(
            files or [], self._dir / "inbox" / f"{evt_id}_files", BOT_TOKEN)

        # 2) write the event to the host inbox (native brain reads it via slack_loop)
        event = {
            "id": evt_id, "channel": channel, "thread_ts": anchor_ts,
            "message_ts": message_ts, "user": user, "user_name": user_name,
            "is_dm": is_dm, "mentioned": mentioned, "text": text,
            "attachments": attachments, "placeholder_ts": placeholder_ts, "t": time.time(),
        }
        inbox_f = self._dir / "inbox" / f"{evt_id}.json"
        tmp = inbox_f.with_suffix(".tmp")
        tmp.write_text(json.dumps(event, ensure_ascii=False))
        tmp.rename(inbox_f)                                   # atomic publish
        logger.info("brain-mode[%s]: queued %s (chan %s, thread %s)",
                    self._name, evt_id, channel, anchor_ts)

        # 3) await the brain's reply file, then reveal the answer into the open stream
        reply_f = self._dir / "outbox" / f"{evt_id}.reply"
        deadline = time.monotonic() + self._timeout
        out = None
        while time.monotonic() < deadline:
            if reply_f.exists():
                out = reply_f.read_text()
                try: reply_f.unlink()
                except FileNotFoundError: pass
                logger.info("brain-mode: got reply for %s (%d chars)", evt_id, len(out))
                break
            await asyncio.sleep(POLL)
        if out is None:
            logger.warning("brain-mode: timeout awaiting reply for %s", evt_id)
            out = "⏳ Still working on this one — I'll follow up in this thread when it's done."

        if shimmer:
            shimmer.cancel()
        await self._clear_status(channel, anchor_ts)       # stop the title shimmer
        await self._finish(channel, anchor_ts, placeholder_ts, message_ts, user, out)
        return ""

    async def _set_status(self, channel, thread_ts, status: str) -> bool:
        """Set the animated 'is working' status on the app title (the shimmer). Returns
        True if the API accepted it (i.e. the shimmer is showing)."""
        try:
            r = await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status)
            return bool(r.get("ok"))
        except Exception as exc:
            logger.debug("setStatus unavailable (%s) — using placeholder", exc)
            return False

    async def _clear_status(self, channel, thread_ts) -> None:
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status="")
        except Exception:
            pass

    async def _shimmer(self, channel, thread_ts):
        """Refresh the status periodically so the shimmer holds through a long think."""
        try:
            while True:
                await asyncio.sleep(9)
                await self._client.assistant_threads_setStatus(
                    channel_id=channel, thread_ts=thread_ts, status=self._thinking_status)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("shimmer refresh stopped: %s", exc)

    async def _finish(self, channel, thread_ts, placeholder_ts, message_ts, user, out: str) -> None:
        """Reveal the brain's reply — text streams in (chat.appendStream animation), or
        morph the fallback placeholder. Then a ✅ done reaction.
        - "__posted__" → brain posted a rich message itself; remove any placeholder.
        - "" (empty)   → no reply; remove any placeholder.
        """
        stripped = out.strip()
        if stripped in ("", "__posted__"):
            if placeholder_ts:
                try: await self._client.chat_delete(channel=channel, ts=placeholder_ts)
                except Exception as exc: logger.debug("placeholder delete failed: %s", exc)
            if message_ts and stripped == "__posted__":
                await self._done_react(channel, message_ts)
            return

        # animate the answer with a streamed reveal (text appears); morph placeholder on fallback
        streamed = False
        if STREAM_REVEAL and not placeholder_ts:
            streamed = await self._stream_reveal(channel, thread_ts, user, out)
        if not streamed:
            if placeholder_ts:
                try:
                    await self._client.chat_update(channel=channel, ts=placeholder_ts, text=_md_to_mrkdwn(out))
                except Exception as exc:
                    logger.debug("placeholder update failed (%s) — posting fresh", exc)
                    await self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_md_to_mrkdwn(out))
            else:
                await self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_md_to_mrkdwn(out))
        if message_ts:
            await self._done_react(channel, message_ts)

    async def _stream_reveal(self, channel, thread_ts, user, out: str) -> bool:
        """Stream the answer text in (the 'text appearing' animation). Returns True on success."""
        try:
            kw = dict(channel=channel, thread_ts=thread_ts)
            if user and self._team_id:
                kw["recipient_user_id"] = user
                kw["recipient_team_id"] = self._team_id
            stream_ts = (await self._client.chat_startStream(**kw))["ts"]
            for chunk in _chunks(out):
                await self._client.chat_appendStream(
                    channel=channel, ts=stream_ts,
                    chunks=[{"type": "markdown_text", "text": chunk}])
            await self._client.chat_stopStream(channel=channel, ts=stream_ts)
            return True
        except Exception as exc:
            logger.debug("stream reveal failed (%s) — will post fresh", exc)
            return False

    async def _done_react(self, channel, message_ts) -> None:
        # ✅ done signal on the user's message (in addition to the 👀 ack)
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
