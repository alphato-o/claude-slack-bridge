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
import re
import time
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler
from security import AccessControl, SecurityConfig
from slack_progress import make_reporter

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"

# Dario<->Bran hotline: normally all bot-authored messages are dropped (self-echo guard),
# but an allowlisted sibling bot may consult the live brain via a bot-authored @-mention
# in an allowlisted channel. Default = the Phi campaign fleet's bridge bot in
# #withclaude-phicampaigner. Override via env (comma-separated). Our own bot is never
# listed here, so self-echo stays blocked.
HOTLINE_BOT_CHANNELS = {c for c in os.environ.get("HOTLINE_BOT_CHANNELS", "C0BCJM4DLNQ").split(",") if c}
HOTLINE_BOT_IDS = {b for b in os.environ.get("HOTLINE_BOT_IDS", "B0BCC9KTTRS").split(",") if b}



def _label_by_speaker(queued: list[tuple[str, str]]) -> str:
    """Render queued messages so each one names its author.

    A drained batch can hold messages from SEVERAL people. Collapsing them into
    one blob loses who said what, which makes the brain thank the wrong person
    and address the wrong person as "you". When more than one author is present
    every message gets an explicit ``<@U…>`` prefix; a single-author batch is
    left clean so the common case reads naturally.
    """
    if not queued:
        return ""
    authors = {u for u, _ in queued if u}
    if len(authors) <= 1:
        return "\n\n".join(t for _, t in queued)
    return "\n\n".join(f"<@{u}> said:\n{t}" if u else t for u, t in queued)

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
        # thread_ts → [(user_id, text)]. BUGFIX 2026-08-07: this used to be a
        # bare list[str]. Two people writing into one thread while a run was in
        # flight got their messages concatenated and stamped with the IN-FLIGHT
        # message's author, so the brain credited both to the wrong person and
        # answered one of them with "you" meaning someone else. Keep the author.
        self._queued: dict[str, list[tuple[str, str]]] = {}
        self._seen_ts: dict[str, float] = {}           # event ts → seen-at (dedupe双-fire)
        self._bot_threads: set[str] = set()            # threads the bot belongs to (engage)
        self._non_bot_threads: set[str] = set()        # human-only threads (ignore) — cached
        self._name_cache: dict[str, str] = {}          # user id → display name (context lines)
        self._bot_user_id: str = ""

        self._access_control = AccessControl(SecurityConfig.from_env())
        self._app.event("message")(self._handle_slack_message)
        self._app.event("app_mention")(self._handle_app_mention)
        self._app.event("member_joined_channel")(self._handle_member_joined)

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter bot messages to prevent self-echo loops. EXCEPTION: an allowlisted sibling
        # bot (the Phi campaign fleet's bridge on Bran) may consult the live brain via a
        # bot-authored @-mention of Dario in the hotline channel. Our own bot is never in
        # HOTLINE_BOT_IDS, so self-echo is still dropped.
        bot_id = event.get("bot_id")
        if bot_id:
            hotline = (
                event.get("channel") in HOTLINE_BOT_CHANNELS
                and bot_id in HOTLINE_BOT_IDS
                and f"<@{self._bot_user_id}>" in (event.get("text") or "")
            )
            if not hotline:
                return
            logger.info("hotline: accepting bot mention from %s in %s", bot_id, event.get("channel"))

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
        # The author's HOME team (their own workspace for a Slack Connect user),
        # used as the streaming recipient's team. Slack puts it in user_team /
        # source_team on shared-channel events; falls back to the event team.
        user_team: str = event.get("user_team") or event.get("source_team") or event.get("team", "")

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

        # DM gate: Dario answers DMs ONLY from the owner. Teammate DMs are ignored —
        # Dario's work happens in the open, in channels, not in private DMs.
        # (owner ruling 2026-07-07). DM_OWNER_ID is the owner's Slack user id; empty
        # env disables the gate (session deployments like Bran keep DMs open).
        dm_owner = os.getenv("DM_OWNER_ID", "")
        if channel.startswith("D") and dm_owner and user_id != dm_owner:
            logger.info("Ignoring DM from non-owner %s in %s.", user_id, channel)
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        mention_tag = f"<@{self._bot_user_id}>"
        mentioned = mention_tag in text

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

        # Case 2: Threaded reply with NO pending session. Only engage threads the
        # bot actually BELONGS to — an @-mention in this reply, or a thread it was
        # invited into / has posted in. Otherwise stay out of humans' own threads
        # (the bug: previously ANY thread reply spawned a run, so the bot chimed
        # into colleagues' conversations it was never tagged in).
        if thread_ts:
            if not mentioned:
                # A reply that explicitly @-mentions SOMEONE ELSE (a colleague, another
                # bot like Dario) is addressed to them, not us — never auto-continue on
                # it, even in a 1:1 bot thread. (The metricsflare soup: "@Dario 在不在"
                # in a Bran-rooted thread must not summon Bran.)
                if not channel.startswith("D") and re.search(r"<@[A-Z0-9]+>", text):
                    logger.info(
                        "Reply in %s @-mentions someone else — staying out.", thread_ts)
                    return
                is_bot_thread = thread_ts in self._bot_threads
                if not is_bot_thread:
                    if thread_ts in self._non_bot_threads:
                        return  # already classified as a human-only thread
                    if not await self._is_bot_thread(channel, thread_ts):
                        self._non_bot_threads.add(thread_ts)
                        logger.info("Ignoring reply in non-bot thread %s.", thread_ts)
                        return
                # It IS a bot thread, but only auto-continue WITHOUT a mention while it
                # stays strictly 1:1 (this bot + one human). A 2nd party of ANY kind
                # (another human, or another bot such as Dario) → require an explicit
                # @-mention so the bot does not chime into a group conversation.
                if await self._thread_party_count(channel, thread_ts) > 1:
                    logger.info(
                        "Multi-party thread %s without mention — awaiting explicit @.",
                        thread_ts)
                    return
            # Engaged (an @-mention, or a 1:1 bot thread). Remember it is a bot thread so
            # 1:1 follow-ups continue without a re-mention.
            self._bot_threads.add(thread_ts)
            self._non_bot_threads.discard(thread_ts)
            if mentioned:
                text = text.replace(mention_tag, "").strip()
            if thread_ts in self._active_threads:
                # A run is in flight — interrupt (hard) or queue (soft).
                await self._handle_busy(channel, thread_ts, text)
                return
            self._active_threads.add(thread_ts)  # claim synchronously (close the race)
            asyncio.create_task(self._run_turn(
                channel, thread_ts, text, user_id, is_new=False, user_team=user_team,
                files=event.get("files"), msg_ts=event.get("ts", "")))
            return

        # Case 3: Top-level message — respond if @mentioned, OR if this is a brain-mode
        # channel/DM (a dedicated Dario surface where every message is for the bot).
        if not mentioned:
            _m = self._claude.mode_for(channel)
            if not (_m == "brain" and (channel.startswith("D") or self._brain_answers_all(channel))):
                return

        # Strip the mention from the text so Claude sees clean input.
        text = text.replace(mention_tag, "").strip()

        message_ts: str = event.get("ts", "")
        self._bot_threads.add(message_ts)  # this @-mention opens a bot thread
        if message_ts in self._active_threads:
            await self._handle_busy(channel, message_ts, text)
            return
        self._active_threads.add(message_ts)
        asyncio.create_task(self._run_turn(
            channel, message_ts, text, user_id, is_new=True, user_team=user_team,
            files=event.get("files"), msg_ts=message_ts))

    async def _handle_member_joined(self, event: dict[str, Any]) -> None:
        """When DARIO himself is added to a channel, it auto-works (brain-default mode
        needs no per-channel config) — we just log it and drop a one-time hello so the
        team knows he's live there. No projects.json edit, no restart, no re-discovery."""
        if event.get("user") != self._bot_user_id:
            return  # someone else joined — ignore
        channel = event.get("channel", "")
        logger.info("Dario was added to channel %s — auto-active (brain-default).", channel)
        try:
            await self._app.client.chat_postMessage(
                channel=channel,
                text=("👋 Dario here. @mention me in this channel and I'll pick it up: "
                      "I reply in a thread under your mention. (Casual chatter I stay out of.)"),
            )
        except Exception as exc:
            logger.debug("join-hello to %s failed: %s", channel, exc)

    def _brain_answers_all(self, channel: str) -> bool:
        """Brain-mode channel that answers EVERY message (not just @mentions)?
        Default FALSE — brain channels are @mention-only, so unrelated chatter in a
        shared channel isn't routed to Dario. Opt in per-channel with
        projects.json ``"answer_all": true`` (e.g. a truly Dario-only channel)."""
        cfg = self._claude._channel_id_to_project.get(channel) or self._claude._project_map.get(channel)
        if isinstance(cfg, dict):
            return cfg.get("answer_all", False)
        return False

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

    async def _is_bot_thread(self, channel: str, thread_ts: str) -> bool:
        """Is this a thread the bot belongs to? True if its parent (or any message)
        @-mentions the bot, or the bot has posted in it. Used once per first-seen
        thread to decide whether to engage; the result is cached by the caller.
        On API failure we return False — better to stay quiet than barge into a
        human thread."""
        try:
            resp = await self._app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=100)
            messages = resp.get("messages", []) or []
        except Exception as exc:
            logger.warning("Could not classify thread %s (%s) — staying out.", thread_ts, exc)
            return False
        mention = f"<@{self._bot_user_id}>"
        for m in messages:
            if m.get("user") == self._bot_user_id:
                return True  # the bot has posted here
            if mention in (m.get("text") or ""):
                return True  # someone tagged the bot in this thread
        return False

    async def _thread_party_count(self, channel: str, thread_ts: str) -> int:
        """Count the DISTINCT parties in a thread besides this bot: human participants
        AND other bots/apps that have posted (e.g. Dario sharing a channel with Bran).
        Used to gate no-mention auto-continue: engage without an @-mention only while
        the thread is strictly 1:1 (this bot + exactly one other party, a human). More
        than one party (a 2nd human OR any other bot) → require an explicit @-mention,
        per the >2-parties rule: in a group conversation, address your bots explicitly.
        On API failure we return a large number so the caller stays quiet (require an
        explicit mention) rather than barging in."""
        try:
            resp = await self._app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200)
            messages = resp.get("messages", []) or []
        except Exception as exc:
            logger.warning(
                "party-count fetch failed for %s (%s) — treating as multi-party.",
                thread_ts, exc)
            return 99
        humans: set[str] = set()
        other_bots: set[str] = set()
        for m in messages:
            uid = m.get("user")
            if uid == self._bot_user_id:
                continue  # this bot itself
            bot_id = m.get("bot_id")
            if bot_id:
                other_bots.add(bot_id)  # a DIFFERENT bot is in this thread
            elif uid:
                humans.add(uid)
        return len(humans) + len(other_bots)

    async def _display_name(self, user_id: str) -> str:
        """Resolve a user id to a display name, cached (best-effort; id on failure)."""
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        name = user_id
        try:
            info = await self._app.client.users_info(user=user_id)
            u = info.get("user", {})
            name = u.get("real_name") or u.get("name") or user_id
        except Exception:
            pass
        self._name_cache[user_id] = name
        return name

    async def _slack_context(
        self, channel: str, thread_ts: str, exclude_ts: str, is_new: bool,
    ) -> str | None:
        """The surrounding Slack conversation for one session-mode turn: the thread's
        prior messages (a reply), or the channel's recent messages (a fresh top-level
        mention). This is what fixes 'look at the messages above' — humans converse
        between invocations and expect the bot to have seen it, but a claude turn only
        receives the invoking message. Skipped for brain mode (the desk observes its
        channel through its own tooling). Best-effort: None on failure or nothing new."""
        try:
            if is_new:
                resp = await self._app.client.conversations_history(channel=channel, limit=12)
                msgs = list(reversed(resp.get("messages", []) or []))
            else:
                resp = await self._app.client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=40)
                msgs = resp.get("messages", []) or []
        except Exception as exc:
            logger.debug("context fetch failed for %s: %s", thread_ts, exc)
            return None
        lines: list[str] = []
        for m in msgs:
            ts = m.get("ts", "")
            if ts == exclude_ts or m.get("subtype") == "channel_join":
                continue
            text = " ".join((m.get("text") or "").split())
            if not text:
                continue
            uid = m.get("user", "")
            if uid == self._bot_user_id:
                who = "you (the bot)"
            elif m.get("bot_id") and not uid:
                who = f"[bot {m.get('bot_id')}]"
            else:
                who = await self._display_name(uid) if uid else "?"
            if len(text) > 400:
                text = text[:399] + "…"
            lines.append(f"- {who}: {text}")
        if not lines:
            return None
        return "\n".join(lines[-30:])[-4000:]

    def _make_reporter(
        self, channel: str, thread_ts: str, user_id: str, user_team: str = ""
    ) -> Any:
        """Build a live-progress reporter for one Flow-B run (see slack_progress).

        The streaming recipient must be the user who triggered the run — including
        their HOME team. For a Slack Connect (external-workspace) user that's their
        team, not the bot's; passing the bot's team makes chat.startStream reject
        the recipient (then we fall back to chat.update)."""
        return make_reporter(
            self._app.client, channel, thread_ts,
            user_id=user_id, team_id=user_team or self._claude._team_id,
        )

    async def _run_turn(
        self, channel: str, thread_ts: str, text: str,
        user_id: str = "", is_new: bool = False, user_team: str = "",
        files: list | None = None, msg_ts: str = "",
    ) -> None:
        """Run one Claude turn for *thread_ts*, tracked so it can be interrupted.

        On normal completion or interrupt, any messages queued meanwhile (soft
        interrupts, or the instruction that followed a hard stop) are drained as
        the next turn — so nothing a user sends mid-task is ever lost.
        """
        # mode:brain — route to the single standing brain instead of an isolated claude -p.
        # No streaming reporter (nothing to tee); the brain acks + posts its own reply.
        if self._claude.mode_for(channel) == "brain":
            self._active_threads.add(thread_ts)
            self._run_tasks[thread_ts] = asyncio.current_task()  # type: ignore[assignment]
            try:
                user_name = ""
                try:
                    info = await self._app.client.users_info(user=user_id)
                    u = info.get("user", {})
                    user_name = u.get("real_name") or u.get("name", "")
                except Exception:
                    pass
                reply = await self._claude.brain_for(channel).handle_turn(
                    channel, thread_ts, text,
                    user=user_id, user_name=user_name,
                    is_dm=channel.startswith("D"), mentioned=True,
                    message_ts=msg_ts or thread_ts,   # real per-message ts → unique event id
                    files=files,
                )
                if reply:
                    await self._app.client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts, text=reply,
                        unfurl_links=False, unfurl_media=False)
            except Exception as exc:
                logger.error("brain-mode turn on %s failed: %s", thread_ts, exc)
            finally:
                self._active_threads.discard(thread_ts)
                if self._run_tasks.get(thread_ts) is asyncio.current_task():
                    self._run_tasks.pop(thread_ts, None)
                # BUGFIX 2026-07-13: brain-mode returned here WITHOUT draining
                # self._queued — soft-interrupt messages ("fold into the next
                # turn") were promised to the user but silently lost forever
                # (a real correction from wenrui was dropped this way). Mirror
                # the non-brain drain; synthetic msg_ts keeps the brain-side
                # event id unique (a combined drain has no single message ts,
                # and reusing thread_ts would collide with the done-ledger).
                queued = self._queued.pop(thread_ts, None)
                if queued:
                    combined = _label_by_speaker(queued)
                    user_id = queued[0][0]  # attribute to the first queued speaker, not the finished run
                    logger.info("Draining %d queued msg(s) on %s as the next brain turn.",
                                len(queued), thread_ts)
                    self._active_threads.add(thread_ts)  # claim before the await gap
                    asyncio.create_task(self._run_turn(
                        channel, thread_ts, combined, user_id, is_new=False,
                        user_team=user_team, msg_ts=f"{time.time():.6f}"))
            return

        self._active_threads.add(thread_ts)
        self._run_tasks[thread_ts] = asyncio.current_task()  # type: ignore[assignment]
        reporter = self._make_reporter(channel, thread_ts, user_id, user_team)
        try:
            context = await self._slack_context(channel, thread_ts, msg_ts, is_new)
            await reporter.start()
            response = await self._claude.handle_turn(
                channel, thread_ts, text, reporter, files=files, context=context)
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
                combined = _label_by_speaker(queued)
                user_id = queued[0][0]  # attribute to the first queued speaker, not the finished run
                logger.info("Draining %d queued msg(s) on %s as the next turn.", len(queued), thread_ts)
                self._active_threads.add(thread_ts)  # claim before the await gap
                asyncio.create_task(self._run_turn(
                    channel, thread_ts, combined, user_id, is_new=False, user_team=user_team))

    async def _handle_busy(self, channel: str, thread_ts: str, text: str) -> None:
        """A message arrived while a run is in flight: hard-interrupt (kill the
        current run, then run the new instruction) or soft-interrupt (queue it for
        the next turn)."""
        kind, remainder = self._classify_interrupt(text)
        if kind == "hard":
            logger.info("Hard interrupt on %s (remainder=%r).", thread_ts, remainder[:80])
            if remainder:
                self._queued.setdefault(thread_ts, []).append((user_id, remainder))
            note = "⏹️ _Stopping the current run…_"
            if remainder:
                note += " I'll run your new instruction next."
            await self._post(channel, thread_ts, note)
            task = self._run_tasks.get(thread_ts)
            if task and not task.done():
                task.cancel()  # its finally drains the queue → starts the new turn
            return
        # Soft: queue for the next turn (matches typing while the CLI is working).
        self._queued.setdefault(thread_ts, []).append((user_id, text))
        logger.info("Soft-queued on busy %s: %r", thread_ts, text[:80])
        await self._post(
            channel, thread_ts,
            "📨 _Got it, I'll fold this into the next turn (I'm mid-task). "
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
