"""
slack_progress.py — Live progress reporting for Flow-B (Slack → Claude) runs.

When a human @-mentions the bot, ``claude -p`` can run for minutes while it
thinks, reads files, and runs tools. Previously nothing appeared in Slack until
the final answer. This module surfaces Claude's ``stream-json`` events to Slack
*as they happen*, so the thread feels alive — like the official Claude app.

Three rendering backends, selected by ``SLACK_STREAM_MODE``:

- ``update`` — post a placeholder thread reply, then edit it via ``chat.update``
  on a throttled cadence (Tier 3 rate limit; works with only ``chat:write``).
- ``native`` — use Slack's streaming API (``chat.startStream`` / ``appendStream``
  / ``stopStream``) for true token-by-token output, with ``task_update`` chunks
  for tool calls. Needs nothing extra beyond ``chat:write`` but the API is newer.
- ``auto`` (default) — try ``native``; on any API failure, fall back to
  ``update`` for the rest of the run so the channel is never left dead.
- ``off`` — no live progress (legacy behaviour: only the final answer is posted).

All three share one ``ActivityRenderer`` that turns a stream-json event into a
short, human-readable activity line (``📖 Read claude_handler.py``,
``⚡ Ran pytest -q`` …). The reporter owns *when* to push to Slack (throttling,
heartbeat); the renderer owns *what* the line says.

The reporters are deliberately decoupled from the daemon: they take a Slack
``AsyncWebClient`` plus the target ``channel`` / ``thread_ts`` and expose a
small lifecycle — ``start()`` → ``on_event(event)`` × N → ``finish(text)`` /
``fail(msg)``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Slack hard limits.
SLACK_MAX_MESSAGE_LENGTH = 40000
# chat.update is Tier 3 (~50/min). Editing one message faster than ~1/sec is
# both wasteful and risks message_update_rate_limited, so coalesce updates to
# at most one per this interval. A background heartbeat flushes pending state.
UPDATE_MIN_INTERVAL_S = 1.2
# How often the heartbeat ticks while a run is active — keeps the elapsed timer
# moving during long silent tool calls and refreshes the native "thinking"
# status before its ~2-minute server-side timeout.
HEARTBEAT_INTERVAL_S = 2.0
# Keep the live activity view compact: only the most recent N steps are shown.
MAX_VISIBLE_STEPS = 12
# Per-line snippet caps (rich feed shows command/result snippets — keep tight
# both for readability and to limit how much file/output content leaks into a
# shared channel).
SNIPPET_MAX = 160
STATUS_MAX = 250  # Slack assistant.threads.setStatus is short-form.


# ---------------------------------------------------------------------------
# Activity rendering — stream-json event → human-readable line
# ---------------------------------------------------------------------------

# Emoji per tool family. mcp__<server>__<tool> and unknown tools fall through
# to the default wrench.
_TOOL_EMOJI = {
    "read": "📖", "edit": "✏️", "multiedit": "✏️", "write": "📝",
    "bash": "⚡", "grep": "🔍", "glob": "🔍", "task": "🤖",
    "webfetch": "🌐", "websearch": "🌐", "todowrite": "📋",
    "notebookedit": "✏️",
}


def _first_line(text: str, limit: int = SNIPPET_MAX) -> str:
    """Collapse *text* to a single trimmed line, truncated to *limit* chars."""
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else path


class ActivityRenderer:
    """Translates Claude ``stream-json`` events into short activity strings.

    Stateless and Slack-agnostic so it can be unit-tested in isolation. Each
    method returns either a ready-to-display line or ``None`` when the event
    contributes nothing visible at the current *verbosity*.

    Args:
        verbosity: ``"rich"`` (default) includes thinking and tool-result
            snippets; ``"normal"`` shows actions only; ``"quiet"`` shows just a
            high-level status.
    """

    def __init__(self, verbosity: str = "rich") -> None:
        self.verbosity = verbosity

    # -- tool_use → "✏️ Edited session.py" --------------------------------
    def tool_line(self, name: str, tool_input: dict[str, Any]) -> str:
        key = (name or "").lower()
        emoji = _TOOL_EMOJI.get(key, "🔧")
        ti = tool_input if isinstance(tool_input, dict) else {}

        if key == "read":
            return f"{emoji} Reading {_basename(ti.get('file_path', ''))}"
        if key in ("edit", "multiedit", "notebookedit"):
            return f"{emoji} Editing {_basename(ti.get('file_path', ''))}"
        if key == "write":
            return f"{emoji} Writing {_basename(ti.get('file_path', ''))}"
        if key == "bash":
            return f"{emoji} {_first_line(ti.get('command', ''), 120)}"
        if key in ("grep", "glob"):
            target = ti.get("pattern", "")
            where = ti.get("path") or ti.get("glob") or ""
            return f"{emoji} Searching {_first_line(target, 80)}" + (f" in {_basename(where)}" if where else "")
        if key == "task":
            desc = ti.get("description") or ti.get("subagent_type") or "subtask"
            return f"{emoji} Delegating: {_first_line(desc, 80)}"
        if key in ("webfetch",):
            return f"{emoji} Fetching {_first_line(ti.get('url', ''), 80)}"
        if key in ("websearch",):
            return f"{emoji} Searching the web: {_first_line(ti.get('query', ''), 80)}"
        if key == "todowrite":
            todos = ti.get("todos") or []
            current = next((t.get("content", "") for t in todos
                            if isinstance(t, dict) and t.get("status") == "in_progress"), "")
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
            head = f"{emoji} Plan: {len(todos)} steps ({done} done)"
            return f"{head} — now: {_first_line(current, 80)}" if current else head
        if key.startswith("mcp__"):
            pretty = key.split("__", 2)[-1].replace("_", " ")
            return f"{emoji} {pretty}"
        # Unknown tool — show its name and a hint of the first arg.
        hint = next((str(v) for v in ti.values() if isinstance(v, str)), "")
        return f"{emoji} {name}" + (f": {_first_line(hint, 60)}" if hint else "")

    # -- tool_result → "   ↳ 3 matches" (rich only) ----------------------
    def result_line(self, content: str, is_error: bool) -> str | None:
        if self.verbosity != "rich":
            return None
        snippet = _first_line(content, SNIPPET_MAX)
        if not snippet:
            return None
        return f"   ↳ {'❌ ' if is_error else ''}{snippet}"

    # -- thinking → "🤔 _planning the refactor_" (rich only) -------------
    def thinking_line(self, thought: str) -> str | None:
        if self.verbosity == "quiet":
            return None
        snippet = _first_line(thought, 120)
        return f"🤔 _{snippet}_" if snippet else None

    # -- assistant text → narration line ---------------------------------
    def text_line(self, text: str) -> str | None:
        snippet = _first_line(text, SNIPPET_MAX)
        return f"💬 {snippet}" if snippet else None

    @staticmethod
    def footer(result_event: dict[str, Any], elapsed_s: float) -> str:
        usage = result_event.get("usage") or {}
        turns = result_event.get("num_turns")
        tokens = (usage.get("output_tokens") or 0) + (usage.get("input_tokens") or 0)
        bits = [f"⏱ {_fmt_elapsed(elapsed_s)}"]
        if turns:
            bits.append(f"{turns} turns")
        if tokens:
            bits.append(f"~{tokens // 1000}k tokens" if tokens >= 1000 else f"{tokens} tokens")
        return " · ".join(bits)


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def iter_activity(event: dict[str, Any], renderer: ActivityRenderer) -> list[str]:
    """Yield zero or more activity lines for one stream-json *event*.

    Centralises the event-shape walking so both reporters share identical
    interpretation of Claude's output. Returns a list (a single assistant
    event may carry multiple content blocks).
    """
    lines: list[str] = []
    etype = event.get("type")
    if etype == "assistant":
        for block in event.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                line = renderer.text_line(block.get("text", ""))
            elif bt == "thinking":
                line = renderer.thinking_line(block.get("thinking", ""))
            elif bt == "tool_use":
                line = renderer.tool_line(block.get("name", ""), block.get("input", {}))
            else:
                line = None
            if line:
                lines.append(line)
    elif etype == "user":
        for block in event.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            line = renderer.result_line(str(content), bool(block.get("is_error")))
            if line:
                lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Reporters
# ---------------------------------------------------------------------------

class ProgressReporter:
    """Base lifecycle: ``start`` → ``on_event`` × N → ``finish`` / ``fail``.

    The base class implements the shared view-model (status, rolling step log,
    elapsed time, the heartbeat ticker, and the final-message composition).
    Subclasses implement only the Slack transport: ``_open`` (first paint),
    ``_paint`` (push current state), ``_close`` (final answer).
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str,
        *,
        renderer: ActivityRenderer | None = None,
        set_status: bool = False,
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._renderer = renderer or ActivityRenderer()
        self._set_status = set_status

        self._steps: list[str] = []
        self._status = "🤖 Working…"
        self._started = time.monotonic()
        self._result_event: dict[str, Any] = {}
        self._dirty = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._heartbeat: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Paint the initial 'on it' message and launch the heartbeat."""
        try:
            await self._open()
        except Exception as exc:  # pragma: no cover - network
            logger.warning("progress start failed: %s", exc)
        self._heartbeat = asyncio.create_task(self._tick())

    async def on_event(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        etype = event.get("type")
        if etype == "result":
            self._result_event = event
            return
        new_lines = iter_activity(event, self._renderer)
        if not new_lines:
            return
        async with self._lock:
            for line in new_lines:
                self._steps.append(line)
                # The latest non-narration line doubles as the headline status.
                if not line.startswith(("   ↳", "💬")):
                    self._status = line
            self._dirty = True
        await self._maybe_paint()

    async def finish(self, final_text: str) -> None:
        await self._shutdown()
        try:
            await self._close(final_text)
        except Exception as exc:  # pragma: no cover - network
            logger.warning("progress finish failed: %s", exc)

    async def fail(self, message: str) -> None:
        await self._shutdown()
        try:
            await self._close(message)
        except Exception as exc:  # pragma: no cover - network
            logger.warning("progress fail failed: %s", exc)

    # -- shared helpers --------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _compose_progress(self) -> str:
        """Render the current view-model into a Slack message body."""
        head = f"*{self._status}*  ⏱ {_fmt_elapsed(self.elapsed)}"
        recent = self._steps[-MAX_VISIBLE_STEPS:]
        body = "\n".join(recent)
        more = len(self._steps) - len(recent)
        prefix = f"_…{more} earlier steps_\n" if more > 0 else ""
        return f"{head}\n{prefix}{body}" if body else head

    def _compose_final(self, final_text: str) -> str:
        footer = self._renderer.footer(self._result_event, self.elapsed)
        return f"{final_text}\n\n_{footer}_" if footer else final_text

    async def _maybe_paint(self) -> None:
        if self._closed or not self._dirty:
            return
        if (time.monotonic() - self._last_paint) < UPDATE_MIN_INTERVAL_S:
            return
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._dirty or self._closed:
                return
            self._dirty = False
            self._last_paint = time.monotonic()
        try:
            await self._paint()
            if self._set_status:
                await self._push_status()
        except Exception as exc:  # pragma: no cover - network
            logger.debug("progress paint failed (continuing): %s", exc)

    async def _tick(self) -> None:
        """Heartbeat: keep the timer moving and the native status alive."""
        try:
            while not self._closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                self._dirty = True  # force a repaint so elapsed advances
                await self._flush()
        except asyncio.CancelledError:  # pragma: no cover
            pass

    async def _shutdown(self) -> None:
        self._closed = True
        if self._heartbeat:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):
                pass

    async def _push_status(self) -> None:
        """Best-effort native 'thinking…' status (Tier 2). No-op on failure."""
        try:
            await self._client.assistant_threads_setStatus(
                channel_id=self._channel,
                thread_ts=self._thread_ts,
                status=_first_line(self._status, STATUS_MAX),
            )
        except Exception as exc:  # pragma: no cover - network/feature-gated
            logger.debug("setStatus unavailable: %s", exc)
            self._set_status = False  # stop retrying for this run

    # last_paint defaults to 0 so the first _maybe_paint always flushes.
    _last_paint: float = 0.0

    async def _abort(self) -> None:
        """Tear down an abandoned reporter without posting a final answer.
        Overridden by streaming backends that must close their open stream."""
        return

    # -- transport hooks (subclasses implement) --------------------------
    async def _open(self) -> None: ...
    async def _paint(self) -> None: ...
    async def _close(self, final_text: str) -> None: ...


class ChatUpdateReporter(ProgressReporter):
    """Tier 1: a single placeholder message, edited via ``chat.update``.

    Robust and dependency-free — works with only ``chat:write`` and over Socket
    Mode. The placeholder is reused for the live feed and then overwritten with
    the final answer (split into follow-ups if it exceeds Slack's length cap).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._msg_ts: str | None = None

    async def _open(self) -> None:
        resp = await self._client.chat_postMessage(
            channel=self._channel, thread_ts=self._thread_ts,
            text="🤖 On it…", mrkdwn=True,
        )
        self._msg_ts = resp["ts"]

    async def _paint(self) -> None:
        if not self._msg_ts:
            return
        await self._client.chat_update(
            channel=self._channel, ts=self._msg_ts,
            text=self._compose_progress(), mrkdwn=True,
        )

    async def _close(self, final_text: str) -> None:
        body = self._compose_final(final_text)
        head, *rest = _split_message(body)
        if self._msg_ts:
            await self._client.chat_update(
                channel=self._channel, ts=self._msg_ts, text=head, mrkdwn=True,
            )
        else:  # _open failed — post fresh
            await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=head, mrkdwn=True,
            )
        for chunk in rest:
            await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=chunk, mrkdwn=True,
            )


class NativeStreamReporter(ProgressReporter):
    """Tier 3: Slack native streaming (``chat.startStream`` family).

    Streams Claude's assistant text as ``markdown_text`` and renders tool calls
    as ``task_update`` chunks — the closest match to the official Claude app.
    Channel streams require the recipient's user and team IDs.

    On any streaming API error the reporter raises so the factory's ``auto``
    mode can fall back to :class:`ChatUpdateReporter` for the rest of the run.
    """

    def __init__(
        self, *args: Any,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._user_id = recipient_user_id
        self._team_id = recipient_team_id
        self._stream_ts: str | None = None
        self._text_started = False
        # tool_use id → title, so a tool_result can flip the same task widget
        # from in_progress to complete/error.
        self._titles: dict[str, str] = {}
        self._think_n = 0

    async def _open(self) -> None:
        kwargs: dict[str, Any] = dict(channel=self._channel, thread_ts=self._thread_ts)
        if self._user_id and self._team_id:
            kwargs["recipient_user_id"] = self._user_id
            kwargs["recipient_team_id"] = self._team_id
        resp = await self._client.chat_startStream(**kwargs)
        self._stream_ts = resp["ts"]

    async def on_event(self, event: dict[str, Any]) -> None:
        # Native mode streams assistant *text* into the message body and renders
        # tools/thinking as task_update chunks (separate widgets), so it fully
        # overrides the base accumulation. Any API error bubbles to the
        # FallbackReporter, which stops this stream and switches to chat.update.
        if self._closed:
            return
        etype = event.get("type")
        if etype == "result":
            self._result_event = event
            return

        if etype == "user":  # tool_result(s) — complete/fail the matching task
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id", "")
                content = block.get("content", "")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                chunk = {
                    "type": "task_update",
                    "id": tid or f"r{len(self._titles)}",
                    "title": _first_line(self._titles.get(tid, "tool"), 256),
                    "status": "error" if block.get("is_error") else "complete",
                }
                if self._renderer.verbosity == "rich" and str(content).strip():
                    chunk["details"] = _first_line(str(content), 256)
                await self._append_chunk(chunk)
            return

        for block in _assistant_blocks(event):
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                await self._append_text(block["text"])
                self._text_started = True
            elif bt == "tool_use":
                tid = block.get("id", "") or f"t{len(self._titles)}"
                title = self._renderer.tool_line(block.get("name", ""), block.get("input", {}))
                self._titles[tid] = title
                await self._append_chunk({
                    "type": "task_update", "id": tid,
                    "title": _first_line(title, 256), "status": "in_progress",
                })
            elif bt == "thinking" and self._renderer.verbosity == "rich":
                tl = self._renderer.thinking_line(block.get("thinking", ""))
                if tl:
                    self._think_n += 1
                    await self._append_chunk({
                        "type": "task_update", "id": f"think-{self._think_n}",
                        "title": _first_line(tl, 256), "status": "complete",
                    })

    async def _append_text(self, text: str) -> None:
        if self._stream_ts:
            await self._client.chat_appendStream(
                channel=self._channel, ts=self._stream_ts, markdown_text=text)

    async def _append_chunk(self, chunk: dict[str, Any]) -> None:
        if self._stream_ts:
            await self._client.chat_appendStream(
                channel=self._channel, ts=self._stream_ts, chunks=[chunk])

    async def _paint(self) -> None:
        # Streaming is push-based via on_event; nothing for the heartbeat to do.
        return

    async def _close(self, final_text: str) -> None:
        # Tools-only turn (no streamed text): make sure the answer still lands.
        if not self._text_started and final_text.strip():
            await self._append_text(final_text)
        footer = self._renderer.footer(self._result_event, self.elapsed)
        if footer:
            await self._append_text(f"\n\n_{footer}_")
        if self._stream_ts:
            await self._client.chat_stopStream(channel=self._channel, ts=self._stream_ts)

    async def _abort(self) -> None:
        """Best-effort close of an abandoned stream (used on fallback) so Slack
        doesn't leave the message stuck in the 'streaming' state."""
        if self._stream_ts:
            try:
                await self._client.chat_stopStream(channel=self._channel, ts=self._stream_ts)
            except Exception:
                pass
            self._stream_ts = None


def _assistant_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    if event.get("type") != "assistant":
        return []
    return [b for b in event.get("message", {}).get("content", []) or [] if isinstance(b, dict)]


def _split_message(text: str, limit: int = SLACK_MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class _NullReporter(ProgressReporter):
    """``off`` mode — no live feed; posts only the final answer (legacy behaviour)."""

    async def start(self) -> None:
        return

    async def on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "result":
            self._result_event = event

    async def finish(self, final_text: str) -> None:
        await self._post_final(final_text)

    async def fail(self, message: str) -> None:
        await self._post_final(message)

    async def _post_final(self, text: str) -> None:
        for chunk in _split_message(text):
            await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=chunk, mrkdwn=True,
            )


class FallbackReporter(ProgressReporter):
    """``auto`` mode wrapper: stream natively, fall back to chat.update on error.

    Delegates to a :class:`NativeStreamReporter`. The first time any native call
    raises, it tears down (best-effort) and rebuilds a :class:`ChatUpdateReporter`,
    replaying accumulated steps so the user sees continuity rather than a reset.
    """

    def __init__(self, native: NativeStreamReporter, build_fallback) -> None:
        self._inner: ProgressReporter = native
        self._build_fallback = build_fallback
        self._fellback = False

    async def _switch(self) -> None:
        if self._fellback:
            return
        self._fellback = True
        logger.info("native streaming failed — falling back to chat.update for this run")
        try:
            await self._inner._shutdown()
            await self._inner._abort()  # stop the stuck stream message
        except Exception:
            pass
        self._inner = self._build_fallback()
        await self._inner.start()

    async def start(self) -> None:
        try:
            await self._inner.start()
        except Exception:
            await self._switch()

    async def on_event(self, event: dict[str, Any]) -> None:
        try:
            await self._inner.on_event(event)
        except Exception:
            await self._switch()
            await self._inner.on_event(event)

    async def finish(self, final_text: str) -> None:
        try:
            await self._inner.finish(final_text)
        except Exception:
            await self._switch()
            await self._inner.finish(final_text)

    async def fail(self, message: str) -> None:
        try:
            await self._inner.fail(message)
        except Exception:
            await self._switch()
            await self._inner.fail(message)


def make_reporter(
    client: Any,
    channel: str,
    thread_ts: str,
    *,
    user_id: str | None = None,
    team_id: str | None = None,
    mode: str | None = None,
    verbosity: str | None = None,
) -> ProgressReporter:
    """Construct the progress reporter for one Flow-B run.

    ``mode`` defaults to ``SLACK_STREAM_MODE`` (env), else ``auto``.
    ``verbosity`` defaults to ``SLACK_STREAM_VERBOSITY`` (env), else ``rich``.
    """
    mode = (mode or os.getenv("SLACK_STREAM_MODE", "auto")).lower()
    verbosity = (verbosity or os.getenv("SLACK_STREAM_VERBOSITY", "rich")).lower()
    renderer = ActivityRenderer(verbosity=verbosity)
    use_status = os.getenv("SLACK_STREAM_STATUS", "false").lower() == "true"

    if mode == "off":
        return _NullReporter(client, channel, thread_ts, renderer=renderer)
    if mode == "update":
        return ChatUpdateReporter(client, channel, thread_ts, renderer=renderer, set_status=use_status)

    native = NativeStreamReporter(
        client, channel, thread_ts, renderer=renderer, set_status=use_status,
        recipient_user_id=user_id, recipient_team_id=team_id,
    )
    if mode == "native":
        return native

    # auto (default)
    def _build_fallback() -> ProgressReporter:
        return ChatUpdateReporter(client, channel, thread_ts, renderer=renderer, set_status=use_status)

    return FallbackReporter(native, _build_fallback)
