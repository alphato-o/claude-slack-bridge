"""
slack_daemon.py — Slack Socket Mode listener + Unix domain socket server.

The daemon holds exactly one Socket Mode WebSocket connection to Slack and
accepts local connections from session processes (started via docker exec).

Each session connects, sends ``REGISTER {thread_ts}\n``, and blocks. When a
Slack reply arrives for that thread_ts the daemon forwards it over the socket,
unblocking the waiting session with zero polling.

Additionally, the daemon handles Human→Claude messages: top-level Slack
messages (and threaded replies with no pending MCP session) are forwarded to
the Claude Code CLI, and the response is posted back as a thread reply.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler
from security import AccessControl, SecurityConfig
from slack_progress import make_reporter

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"


class SlackDaemon:
    """
    Bridges Slack Socket Mode events to waiting session processes via a
    Unix domain socket, and handles Human→Claude messages via the Claude
    Code CLI.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()
        self._claude = ClaudeHandler(slack_client=self._app.client)
        self._active_threads: set[str] = set()
        self._run_tasks: dict[str, asyncio.Task] = {}  # thread_ts → in-flight run task
        self._queued: dict[str, list[str]] = {}        # thread_ts → messages to apply next turn
        self._seen_ts: dict[str, float] = {}           # event ts → seen-at (dedupe双-fire)
        self._bot_user_id: str = ""

        self._access_control = AccessControl(SecurityConfig.from_env())
        self._app.event("message")(self._handle_slack_message)
        self._app.event("app_mention")(self._handle_app_mention)

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        # Dedupe: a mention can arrive as BOTH a `message` and an `app_mention`
        # event with the same ts; process each user message exactly once.
        evt_ts = event.get("ts", "")
        if evt_ts:
            now = time.monotonic()
            self._seen_ts = {k: v for k, v in self._seen_ts.items() if now - v < 120}
            if evt_ts in self._seen_ts:
                return
            self._seen_ts[evt_ts] = now

        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")

        # Access control: reject unauthorized users/channels before any processing.
        if not self._access_control.is_allowed(user_id=user_id, channel_id=channel):
            thread_ts = event.get("thread_ts") or event.get("ts", "")
            try:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=self._access_control.rejection_message(),
                )
            except Exception as exc:
                logger.warning("Failed to send rejection message to %s: %s", channel, exc)
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")

        # Case 1: Threaded reply WITH a pending MCP session — forward to session.
        if thread_ts:
            async with self._lock:
                writer = self._pending.pop(thread_ts, None)

            if writer is not None:
                logger.info("Slack reply in thread %s: %r", thread_ts, text)
                try:
                    writer.write(text.encode() + b"\n")
                    await writer.drain()
                    logger.info("Reply forwarded to session for thread %s.", thread_ts)
                except Exception as exc:
                    logger.warning("Failed to forward reply for %s: %s", thread_ts, exc)
                finally:
                    writer.close()
                return

        # Case 2: Threaded reply with NO pending session.
        if thread_ts:
            if thread_ts in self._active_threads:
                # A run is in flight — interrupt (hard) or queue (soft) instead of
                # silently dropping the message.
                await self._handle_busy(channel, thread_ts, text)
                return
            self._active_threads.add(thread_ts)  # claim synchronously (close the race)
            asyncio.create_task(self._run_turn(channel, thread_ts, text, user_id, is_new=False))
            return

        # Case 3: Top-level message — only respond if the bot is mentioned.
        mention_tag = f"<@{self._bot_user_id}>"
        if mention_tag not in text:
            return

        # Strip the mention from the text so Claude sees clean input.
        text = text.replace(mention_tag, "").strip()

        message_ts: str = event.get("ts", "")
        if message_ts in self._active_threads:
            await self._handle_busy(channel, message_ts, text)
            return
        self._active_threads.add(message_ts)
        asyncio.create_task(self._run_turn(channel, message_ts, text, user_id, is_new=True))

    async def _handle_app_mention(self, event: dict[str, Any]) -> None:
        """Handle app_mention events (bot @mentioned in any channel)."""
        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")

        if not self._access_control.is_allowed(user_id=user_id, channel_id=channel):
            thread_ts = event.get("thread_ts") or event.get("ts", "")
            try:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=self._access_control.rejection_message(),
                )
            except Exception as exc:
                logger.warning("Failed to send rejection message to %s: %s", channel, exc)
            return

        # Delegate to the normal message handler for authorized mentions.
        await self._handle_slack_message(event)

    def _make_reporter(self, channel: str, thread_ts: str, user_id: str) -> Any:
        """Build a live-progress reporter for one Flow-B run (see slack_progress)."""
        return make_reporter(
            self._app.client, channel, thread_ts,
            user_id=user_id, team_id=self._claude._team_id,
        )

    async def _run_turn(
        self, channel: str, thread_ts: str, text: str,
        user_id: str = "", is_new: bool = False,
    ) -> None:
        """Run one Claude turn for *thread_ts*, tracked so it can be interrupted.

        On normal completion or interrupt, any messages queued meanwhile (soft
        interrupts, or the instruction that followed a hard stop) are drained as
        the next turn — so nothing a user sends mid-task is ever lost.
        """
        self._active_threads.add(thread_ts)
        self._run_tasks[thread_ts] = asyncio.current_task()  # type: ignore[assignment]
        reporter = self._make_reporter(channel, thread_ts, user_id)
        try:
            await reporter.start()
            response = await self._claude.handle_turn(channel, thread_ts, text, reporter)
            await reporter.finish(response)
        except asyncio.CancelledError:
            # Intentional hard interrupt — finalize the stream, don't treat as error.
            logger.info("Turn on %s hard-interrupted.", thread_ts)
            try:
                await reporter.fail("⏹️ Stopped.")
            except Exception:
                pass
        except Exception as exc:
            logger.error("Error in turn on %s: %s", thread_ts, exc)
            try:
                await reporter.fail("Sorry, I encountered an error processing your request.")
            except Exception:
                pass
        finally:
            self._active_threads.discard(thread_ts)
            if self._run_tasks.get(thread_ts) is asyncio.current_task():
                self._run_tasks.pop(thread_ts, None)
            queued = self._queued.pop(thread_ts, None)
            if queued:
                combined = "\n\n".join(queued)
                logger.info("Draining %d queued msg(s) on %s as the next turn.", len(queued), thread_ts)
                self._active_threads.add(thread_ts)  # claim before the await gap
                asyncio.create_task(
                    self._run_turn(channel, thread_ts, combined, user_id, is_new=False))

    async def _handle_busy(self, channel: str, thread_ts: str, text: str) -> None:
        """A message arrived while a run is in flight: hard-interrupt (kill the
        current run, then run the new instruction) or soft-interrupt (queue it for
        the next turn)."""
        kind, remainder = self._classify_interrupt(text)
        if kind == "hard":
            logger.info("Hard interrupt on %s (remainder=%r).", thread_ts, remainder[:80])
            if remainder:
                self._queued.setdefault(thread_ts, []).append(remainder)
            note = "⏹️ _Stopping the current run…_"
            if remainder:
                note += " I'll run your new instruction next."
            await self._post(channel, thread_ts, note)
            task = self._run_tasks.get(thread_ts)
            if task and not task.done():
                task.cancel()  # its finally drains the queue → starts the new turn
            return
        # Soft: queue for the next turn (matches typing while the CLI is working).
        self._queued.setdefault(thread_ts, []).append(text)
        logger.info("Soft-queued on busy %s: %r", thread_ts, text[:80])
        await self._post(
            channel, thread_ts,
            "📨 _Got it — I'll fold this into the next turn (I'm mid-task). "
            "Send `!` (or `停`/`stop`) first to interrupt now instead._",
        )

    @staticmethod
    def _classify_interrupt(text: str) -> tuple[str, str]:
        """Classify a mid-task message. Returns ``("hard", remainder)`` for a stop
        request (leading ``!`` or a stop-word; *remainder* becomes the next
        instruction) or ``("soft", text)`` to queue it."""
        s = text.strip()
        if s.startswith("!"):
            return "hard", s[1:].strip()
        low = s.lower()
        stop_exact = {"stop", "停", "停止", "停下", "打断", "中断", "abort", "cancel"}
        if low in stop_exact or s in stop_exact:
            return "hard", ""
        for w in ("停止", "停下", "中断", "打断", "停", "stop", "abort", "cancel"):
            if s.startswith(w + " ") or low.startswith(w + " "):
                return "hard", s[len(w):].lstrip(" :,，、").strip()
        return "soft", s

    async def _post(self, channel: str, thread_ts: str, text: str) -> None:
        """Best-effort tiny acknowledgement in the thread."""
        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True)
        except Exception as exc:
            logger.debug("ack post failed: %s", exc)

    async def _handle_notify(self, payload: str, writer: asyncio.StreamWriter) -> None:
        """Post a message to Slack for a token-less helper. Payload is one JSON
        line: {"channel":..., "thread_ts":..., "text":...}."""
        try:
            data = json.loads(payload)
            await self._app.client.chat_postMessage(
                channel=data["channel"],
                thread_ts=data.get("thread_ts") or None,
                markdown_text=data["text"],
            )
            logger.info(
                "bridge_notify → channel=%s thread=%s (%d chars)",
                data.get("channel"), data.get("thread_ts"), len(data.get("text", "")),
            )
            writer.write(b"OK\n")
        except Exception as exc:
            logger.warning("bridge_notify failed: %s", exc)
            try:
                writer.write(b"ERR\n")
            except Exception:
                pass
        try:
            await writer.drain()
        except Exception:
            pass

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        thread_ts: str | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().rstrip("\n").split(" ", 1)
            cmd = parts[0] if parts else ""

            # Fire-and-forget notify: a token-less Flow-B helper (bridge_notify.py)
            # asks the daemon to post a message back to a thread — used so a
            # backgrounded script can return its result to the thread that started
            # it after the Claude turn has ended. The bot token never leaves here.
            if cmd == "NOTIFY" and len(parts) == 2:
                await self._handle_notify(parts[1], writer)
                return

            if cmd != "REGISTER" or len(parts) != 2:
                logger.warning("Bad socket command: %r", line)
                return

            thread_ts = parts[1]
            async with self._lock:
                self._pending[thread_ts] = writer

            logger.info("Session registered for thread %s.", thread_ts)

            # Block until the session disconnects (reader.read returns b"" on close).
            # This ensures _pending is cleaned up if the session exits before a reply arrives.
            await reader.read(1)

        except Exception as exc:
            logger.error("Session connection error: %s", exc)
        finally:
            if thread_ts:
                async with self._lock:
                    self._pending.pop(thread_ts, None)
            if not writer.is_closing():
                writer.close()

    async def start(self) -> None:
        """Start the Unix socket server and Slack Socket Mode handler concurrently."""
        await self._claude.initialize()
        self._bot_user_id = self._claude._bot_user_id

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        server = await asyncio.start_unix_server(
            self._handle_session_connection, path=SOCKET_PATH
        )
        logger.info("Unix socket server listening at %s.", SOCKET_PATH)

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self._handler.start_async(),
            )
