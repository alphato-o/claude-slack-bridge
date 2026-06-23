"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. container restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.

Project detection: reads ``projects.json`` at the repo root to map Slack
channels to project directories.  When a message arrives, the handler resolves
the channel to a project path and runs ``claude -p`` from that directory so
Claude sees the project's CLAUDE.md and codebase.

Each entry in ``projects.json`` can be a plain path string (legacy) or a dict
with ``path`` and optional ``plugin_dir`` / ``worktrees`` fields. When
``plugin_dir`` is set, ``--plugin-dir <dir>`` is prepended to the
``claude -p`` invocation so project-specific skills are loaded automatically.

When ``worktrees`` is a ``{label: path}`` map, users can route a top-level
Slack message to a specific worktree by prefixing the message with
``[label]`` (e.g. ``@Bot [feature-x] refactor session.py``). The label
prefix is stripped before the prompt is sent to Claude. Replies inside the
resulting thread stay in that worktree without re-tagging.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Flow-B runs are real work that can legitimately take 30–60+ min, so we do NOT
# cap total wall-clock blindly. Instead an inactivity watchdog kills the run only
# when it produces NO output for IDLE_TIMEOUT seconds (a genuinely stuck process),
# resetting on every stream event. MAX_RUNTIME is a generous hard backstop against
# a runaway that keeps emitting forever. Both overridable via env.
IDLE_TIMEOUT = int(os.getenv("FLOW_B_IDLE_TIMEOUT", "1200"))   # 20 min of silence
MAX_RUNTIME = int(os.getenv("FLOW_B_MAX_RUNTIME", "14400"))    # 4 h hard cap
WATCHDOG_INTERVAL = 15  # how often the watchdog checks (seconds)
# Claude CLI in stream-json mode emits one JSON event per line. A single
# event can embed large tool inputs/results (file reads, MCP responses,
# task outputs), easily exceeding asyncio's default 64 KB StreamReader
# buffer and raising ``LimitOverrunError`` ("Separator is found, but chunk
# is longer than limit"). Bump the limit so we can ingest realistic events.
STREAM_BUFFER_LIMIT = 100 * 1024 * 1024  # 100 MB
PROJECTS_CONFIG = Path(__file__).parent.parent / "projects.json"

# Allow Slack's leading bold/italic/strike markers (``*``, ``_``, ``~``)
# before the tag — Slack delivers ``*[label] msg*`` when the user bolds
# the whole line.
_WORKTREE_TAG_RE = re.compile(r"^[\s*_~]*\[([^\]]+)\]\s*")
# Labels become directory names; restrict to a safe alphabet to block
# path-traversal attempts like ``[../etc]``.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _parse_worktree_tag(text: str) -> tuple[str | None, str]:
    """Strip a leading ``[label]`` tag from *text*.

    Returns ``(label, remaining_text)``. ``label`` is ``None`` when no tag
    is present or when the label contains unsafe characters. The label is
    what users type in Slack to route a Flow-B message to a specific
    worktree (e.g. ``[claude-slack-test] hi``).
    """
    match = _WORKTREE_TAG_RE.match(text)
    if not match:
        return None, text
    label = match.group(1).strip()
    if not _SAFE_LABEL_RE.match(label):
        return None, text
    remaining = text[match.end() :]
    return label, remaining


def _resolve_dynamic_worktree(default_path: str, label: str) -> str | None:
    """Resolve *label* to a sibling worktree directory of *default_path*.

    Worktrees are typically created with ``git worktree add ../<name>`` so
    they live next to the main checkout. This lets users add/remove
    worktrees without editing ``projects.json``: the daemon checks whether
    a sibling directory named *label* exists and looks like a git checkout
    (has a ``.git`` file or directory).

    Returns the resolved path or ``None`` if no matching directory exists.
    """
    parent = os.path.dirname(default_path)
    candidate = os.path.join(parent, label)
    git_marker = os.path.join(candidate, ".git")
    if os.path.isdir(candidate) and os.path.exists(git_marker):
        return candidate
    return None


def _load_project_map() -> dict[str, Any]:
    """Load channel → project config mapping from projects.json.

    Values may be a plain path string (legacy) or a dict with ``path`` and
    optional ``plugin_dir`` keys (extended format).
    """
    if not PROJECTS_CONFIG.exists():
        logger.warning("No projects.json at %s — project detection disabled.", PROJECTS_CONFIG)
        return {}
    with open(PROJECTS_CONFIG) as f:
        mapping = json.load(f)
    logger.info("Loaded project map with %d entries.", len(mapping))
    return mapping


# --- Cross-session continuity state (persisted in the ~/.claude volume) ---------
CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
SESSIONS_FILE = CLAUDE_HOME / "bridge-sessions.json"  # cwd → continuous session id
JOURNAL_DIR = CLAUDE_HOME / "bridge-journals"         # per-cwd durable work log
JOURNAL_INJECT_CHARS = 3500   # how much journal tail to seed a fresh session with
JOURNAL_ASK_CHARS = 300
JOURNAL_DID_CHARS = 700


def _trim(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def _save_sessions(mapping: dict[str, str]) -> None:
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(mapping, indent=2))
    except Exception as exc:
        logger.warning("Could not persist bridge session map: %s", exc)


def _session_file_exists(project_dir: str | None, session_id: str) -> bool:
    """True if Claude's on-disk transcript for this session exists, so we can
    safely ``--resume`` it (vs re-creating it with ``--session-id``)."""
    if not project_dir:
        return False
    slug = project_dir.replace("/", "-")
    return (CLAUDE_HOME / "projects" / slug / f"{session_id}.jsonl").exists()


def _journal_file(cwd_key: str) -> Path:
    return JOURNAL_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", cwd_key) + ".md")


def _journal_tail(cwd_key: str) -> str:
    try:
        return _journal_file(cwd_key).read_text()[-JOURNAL_INJECT_CHARS:].strip()
    except Exception:
        return ""


def _append_journal(cwd_key: str, request: str, result: str) -> None:
    """Append a compact entry to the project's durable work log. This survives
    session compaction, ``/new``, and (via the volume) restarts — the durable
    backbone behind the verbatim session."""
    try:
        f = _journal_file(cwd_key)
        f.parent.mkdir(parents=True, exist_ok=True)
        new = not f.exists()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(f, "a") as fh:
            if new:
                fh.write(
                    "# Bridge journal\n\n_Durable log of Slack-driven work in this "
                    "project, kept by claude-slack-bridge. Seeded into a fresh session "
                    "so a new conversation knows the recent history._\n")
            fh.write(
                f"\n## {stamp}\n"
                f"**Asked:** {_trim(request, JOURNAL_ASK_CHARS)}\n\n"
                f"**Did:** {_trim(result, JOURNAL_DID_CHARS)}\n")
    except Exception as exc:
        logger.debug("journal append failed: %s", exc)


def _anchor_addendum(channel: str, thread_ts: str) -> str:
    """System-prompt addendum telling Claude its Slack location anchor, and how a
    job that outlives the turn can post its result back to THIS thread."""
    return (
        f" SLACK ANCHOR — you are in channel {channel}, thread_ts {thread_ts}; "
        "this turn's replies land in this thread. If you must run work that "
        "OUTLIVES this turn (a long battery/build/script you can't reasonably wait "
        "out), do NOT say you'll 'report later' — a detached job can post its OWN "
        "result back to this thread when it finishes by piping output to "
        f"`python /app/src/bridge_notify.py --channel {channel} --thread-ts {thread_ts}` "
        "(no token needed — the daemon posts it). For example: "
        f"`nohup sh -c 'run_the_battery; summarize | python /app/src/bridge_notify.py "
        f"--channel {channel} --thread-ts {thread_ts}' >/tmp/job.log 2>&1 &`. "
        "Prefer finishing within the turn and reporting inline (you have ~20 min of "
        "idle headroom); use the detached-notify pattern only for genuinely long jobs."
    )


class ClaudeHandler:
    """
    Manages Claude Code CLI invocations for Slack messages.

    Args:
        slack_client: An async Slack WebClient (``self._app.client``).
    """

    def __init__(self, slack_client: Any) -> None:
        self._slack_client = slack_client
        self._bot_user_id: str = ""
        self._team_id: str = ""
        self._project_map: dict[str, Any] = _load_project_map()
        # Resolved at startup: channel ID → {"path": str|None, "plugin_dir": str|None,
        #                                    "worktrees": dict[str, str]}
        self._channel_id_to_project: dict[str, dict] = {}
        # ONE continuous Claude session PER WORKING DIRECTORY (cwd key → session id),
        # so a new @mention resumes the project's ongoing conversation instead of
        # starting cold. Persisted to the volume so it survives restarts.
        self._session: dict[str, str] = _load_sessions()
        # Per-cwd lock: only one run may use a given session at a time (you can't
        # --resume the same session concurrently). Same-cwd runs serialize here.
        self._cwd_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """Cache the bot's own user ID and resolve channel names to IDs."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        self._team_id = resp.get("team_id", "")
        logger.info(
            "ClaudeHandler initialized, bot_user_id=%s (%d persisted project sessions)",
            self._bot_user_id, len(self._session),
        )

        if self._project_map:
            await self._resolve_channel_ids()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Both Slack entry points (new mention, thread reply) run one continuous
    # per-project conversation, so these are thin wrappers over handle_turn.
    async def handle_message(
        self, channel: str, message_ts: str, text: str, reporter: Any = None
    ) -> str:
        return await self.handle_turn(channel, message_ts, text, reporter)

    async def handle_thread_reply(
        self, channel: str, thread_ts: str, text: str, reporter: Any = None
    ) -> str:
        return await self.handle_turn(channel, thread_ts, text, reporter)

    async def handle_turn(
        self, channel: str, thread_ts: str, text: str, reporter: Any = None
    ) -> str:
        """Run one turn, resuming the project's continuous Claude session.

        Continuity is keyed by working directory: every @mention/reply for a
        project resumes the same session, so a new request inherits the prior
        conversation instead of starting cold. ``/new`` (optionally followed by
        an instruction) starts a fresh session; a fresh session is seeded with
        the project's durable journal so it still knows recent history.
        """
        force_new = False
        stripped = text.lstrip()
        if stripped == "/new" or stripped[:5] in ("/new ", "/new\n"):
            force_new = True
            text = stripped[4:].strip()

        label, text = _parse_worktree_tag(text)
        project_dir, plugin_dir = self._get_project_config(channel, label)
        cwd_key = project_dir or f"chan:{channel}"

        if force_new and not text:
            self._session.pop(cwd_key, None)
            _save_sessions(self._session)
            return "🆕 Started a fresh conversation for this project. What would you like me to do?"

        # Tell Claude its Slack anchor so any work that outlives this turn can
        # report back to THIS thread (token-safely, via bridge_notify).
        system_prompt = self._FLOW_B_SYSTEM_PROMPT + _anchor_addendum(channel, thread_ts)

        session_id, resume = self._session_for(cwd_key, project_dir, force_new)
        prompt = text
        if resume:
            logger.info("Resuming session %s for %s (thread %s)", session_id, cwd_key, thread_ts)
            cmd = self._build_cmd(resume=session_id, plugin_dir=plugin_dir,
                                  system_prompt=system_prompt)
        else:
            logger.info("New session %s for %s%s (thread %s)", session_id, cwd_key,
                        " (/new)" if force_new else "", thread_ts)
            tail = _journal_tail(cwd_key)
            if tail:
                prompt = (
                    "## Earlier work in this project (prior Slack sessions — context only)\n"
                    f"{tail}\n\n---\n\n## Current request\n{text}"
                )
            cmd = self._build_cmd(session_id=session_id, plugin_dir=plugin_dir,
                                  system_prompt=system_prompt)

        async with self._lock_for(cwd_key):
            result = await self._run_claude(cmd, prompt, cwd=project_dir, reporter=reporter)
        _append_journal(cwd_key, text, result)
        return result

    def _lock_for(self, cwd_key: str) -> asyncio.Lock:
        lock = self._cwd_locks.get(cwd_key)
        if lock is None:
            lock = asyncio.Lock()
            self._cwd_locks[cwd_key] = lock
        return lock

    def _session_for(
        self, cwd_key: str, project_dir: str | None, force_new: bool
    ) -> tuple[str, bool]:
        """Return (session_id, resume?) for a cwd. Creates+persists a new id on
        first use or ``/new``; otherwise resumes the stored id when its transcript
        exists on disk (else re-creates it with that id — never a failed resume)."""
        if force_new or cwd_key not in self._session:
            sid = str(uuid.uuid4())
            self._session[cwd_key] = sid
            _save_sessions(self._session)
            return sid, False
        sid = self._session[cwd_key]
        return sid, _session_file_exists(project_dir, sid)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_project_config(
        self, channel_id: str, label: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (project_dir, plugin_dir) for a Slack channel ID.

        When *label* is provided and matches a registered worktree for the
        channel, the worktree path is returned instead of the default. An
        unknown label falls back to the default with a warning so messages
        aren't silently dropped.

        Both values are ``None`` when no mapping exists for the channel.
        """
        config = self._channel_id_to_project.get(channel_id)
        if not config:
            logger.info("No project mapping for channel %s — using default cwd.", channel_id)
            return None, None

        plugin_dir = config["plugin_dir"]
        worktrees: dict[str, str] = config.get("worktrees", {})
        default_path = config["path"]

        if label and label in worktrees:
            return worktrees[label], plugin_dir

        if label and default_path:
            dynamic = _resolve_dynamic_worktree(default_path, label)
            if dynamic:
                return dynamic, plugin_dir

        path = default_path
        logger.info(
            "Channel %s → project %s%s",
            channel_id, path,
            f" (plugin_dir={plugin_dir})" if plugin_dir else "",
        )
        return path, plugin_dir

    async def _resolve_channel_ids(self) -> None:
        """Resolve channel names from project_map to Slack channel IDs."""
        try:
            result = await self._slack_client.conversations_list(
                types="public_channel,private_channel", limit=1000,
            )
            channels = result.get("channels", [])

            name_to_id: dict[str, str] = {}
            for ch in channels:
                name_to_id[f"#{ch['name']}"] = ch["id"]
                name_to_id[ch["name"]] = ch["id"]
                name_to_id[ch["id"]] = ch["id"]  # allow raw IDs in config

            for channel_key, value in self._project_map.items():
                # Normalise the legacy string format and the dict format.
                if isinstance(value, str):
                    config = {"path": value, "plugin_dir": None, "worktrees": {}}
                else:
                    config = {
                        "path": value.get("path"),
                        "plugin_dir": value.get("plugin_dir"),
                        "worktrees": value.get("worktrees") or {},
                    }

                # DM channel IDs (D...) and raw channel IDs (C...) are not
                # returned by conversations_list — register them directly.
                if channel_key.startswith(("C", "D")) and channel_key not in name_to_id:
                    self._channel_id_to_project[channel_key] = config
                    logger.info(
                        "Mapped %s (raw ID) → %s%s",
                        channel_key, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                    continue

                channel_id = name_to_id.get(channel_key)
                if channel_id:
                    self._channel_id_to_project[channel_id] = config
                    logger.info(
                        "Mapped %s (ID: %s) → %s%s",
                        channel_key, channel_id, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                else:
                    logger.warning("Channel %s not found in workspace — skipping.", channel_key)

        except Exception as exc:
            logger.error("Failed to resolve channel IDs: %s", exc)

    # Flow-B Claude runs inside the bridge container; it has no docker CLI,
    # so the ``claude-slack-bridge`` entry in the project's .mcp.json (which
    # spawns ``session.py`` via ``docker exec``) fails to start. Other MCP
    # servers in .mcp.json (e.g. Notion) load normally. The system-prompt
    # addendum tells Claude not to mention the failed bridge server in its
    # reply.
    _FLOW_B_SYSTEM_PROMPT = (
        "You are replying to a Slack message; your response is posted directly "
        "into the Slack thread, and the user's next message resumes this session "
        "as your next prompt. This means your reply text IS your "
        "channel to the user — to ask a question, end your turn with the "
        "question as your final reply; the user's reply arrives as the next "
        "prompt. Never call mcp__claude-slack-bridge__ask_on_slack — it is not "
        "available in this mode, and any skill or command that instructs you "
        "to use it should be reinterpreted as 'end your turn with that "
        "message as your reply'. Do not mention MCP, tool availability, "
        "Docker, or the claude-slack-bridge server in your reply. "
        "IMPORTANT — you get ONE turn per message and CANNOT report back on your "
        "own later: when you stop, the run ends and any background process you "
        "started is killed. So NEVER say you'll 'report when it completes' as if "
        "you'll return unprompted. For long verification (tests, regression "
        "batteries, builds, deploys): run it to completion WITHIN this turn and "
        "report the actual result — you have generous time, minutes of work are "
        "fine, prefer waiting synchronously over backgrounding. Only if it is "
        "genuinely too long, end with a clear 'reply <something> to get the "
        "results' so the user's next message resumes you to fetch them."
    )

    @staticmethod
    def _build_cmd(
        session_id: str | None = None,
        resume: str | None = None,
        plugin_dir: str | None = None,
        system_prompt: str | None = None,
    ) -> list[str]:
        # stream-json + --verbose makes the CLI emit one event per line on
        # stdout (system/init, assistant text, thinking, tool_use,
        # tool_result, result). We log each event as it arrives so Docker
        # captures Claude's full trace, not just the final reply.
        cmd = [
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--append-system-prompt", system_prompt or ClaudeHandler._FLOW_B_SYSTEM_PROMPT,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if plugin_dir:
            cmd.extend(["--plugin-dir", plugin_dir])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if resume:
            cmd.extend(["--resume", resume])
        return cmd

    async def _run_claude(
        self, cmd: list[str], prompt: str, cwd: str | None = None, reporter: Any = None
    ) -> str:
        """Spawn a ``claude -p`` subprocess, stream-log its events, and return the final reply.

        When *reporter* is provided, every parsed stream-json event is also
        forwarded to it (``reporter.on_event``) so the run's progress is
        surfaced live to Slack. Reporter errors are swallowed — a flaky progress
        update must never break the actual Claude run.
        """
        env = os.environ.copy()
        # Strip tokens that must never be reachable by the Claude subprocess.
        # A prompt-injection attack could otherwise instruct Claude to exfiltrate them.
        for _key in ("CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(_key, None)

        logger.debug("claude spawn: cwd=%s cmd=%s prompt=%r", cwd, cmd, prompt[:500])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=STREAM_BUFFER_LIMIT,
            )
        except FileNotFoundError:
            logger.error("claude CLI not found — is it installed and in PATH?")
            return "Sorry, the Claude CLI is not available."

        # Send prompt and close stdin so claude can begin work.
        assert process.stdin is not None
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        final_result: str | None = None
        start = time.monotonic()
        last_activity = start

        async def consume_stdout() -> None:
            nonlocal final_result, last_activity
            assert process.stdout is not None
            async for raw_line in process.stdout:
                last_activity = time.monotonic()  # any output = alive, reset idle
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("claude stdout (non-json): %s", line[:1000])
                    continue
                self._log_stream_event(event)
                if reporter is not None and isinstance(event, dict):
                    try:
                        await reporter.on_event(event)
                    except Exception as exc:
                        logger.debug("reporter.on_event failed (ignored): %s", exc)
                if (
                    isinstance(event, dict)
                    and event.get("type") == "result"
                    and "result" in event
                ):
                    final_result = event["result"]

        async def consume_stderr() -> None:
            nonlocal last_activity
            assert process.stderr is not None
            async for raw_line in process.stderr:
                last_activity = time.monotonic()
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("claude stderr: %s", line[:1000])

        stdout_task = asyncio.create_task(consume_stdout())
        stderr_task = asyncio.create_task(consume_stderr())

        # Inactivity watchdog: kill only on genuine silence or the hard backstop,
        # never on total elapsed while output keeps flowing.
        async def watchdog() -> str:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                now = time.monotonic()
                if now - last_activity > IDLE_TIMEOUT:
                    return "idle"
                if now - start > MAX_RUNTIME:
                    return "max"

        work = asyncio.ensure_future(
            asyncio.gather(stdout_task, stderr_task, process.wait())
        )
        wd = asyncio.ensure_future(watchdog())
        try:
            done, _ = await asyncio.wait({work, wd}, return_when=asyncio.FIRST_COMPLETED)

            if work not in done:
                # Watchdog fired first — the run stalled (the finally kills it).
                reason = wd.result()
                elapsed = int(time.monotonic() - start)
                idle = int(time.monotonic() - last_activity)
                logger.error(
                    "Claude subprocess stalled (%s) after %ds elapsed / %ds idle (last result=%r)",
                    reason, elapsed, idle, (final_result or "")[:200],
                )
                if final_result:
                    return final_result  # got a result right before the cap — use it
                mins = IDLE_TIMEOUT // 60 if reason == "idle" else MAX_RUNTIME // 60
                why = (f"went quiet for {mins} min (looked stuck)" if reason == "idle"
                       else f"hit the {mins // 60} h max-runtime cap")
                return f"_(I stopped — the run {why}. Reply in this thread to continue where I left off.)_"

            await work  # surface any consumer/process exception

            if process.returncode != 0:
                logger.error(
                    "Claude CLI failed (rc=%d) cmd=%s prompt=%r",
                    process.returncode, cmd, prompt[:200],
                )
                return "Sorry, I encountered an error processing your request."

            if final_result is None:
                logger.warning("Claude stream ended with no result event.")
                return "Sorry, I couldn't parse the response."
            return final_result
        finally:
            # Guarantee no orphaned subprocess or tasks — covers the watchdog stall
            # AND a hard interrupt (the turn task is cancelled, raising CancelledError
            # here; we must still kill claude -p so it doesn't run on detached).
            wd.cancel()
            if process.returncode is None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except Exception:
                    pass
            for t in (stdout_task, stderr_task, work):
                if not t.done():
                    t.cancel()

    @staticmethod
    def _log_stream_event(event: Any) -> None:
        """Log a single stream-json event from ``claude -p`` in human-readable form.

        All per-event logs are at DEBUG so the default INFO level matches the
        pre-stream-json behaviour (lifecycle only). Set ``LOG_LEVEL=DEBUG`` to
        see the full trace of Claude's tool calls and reasoning.
        """
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype == "system":
            logger.debug(
                "claude stream: system/%s session=%s cwd=%s tools=%s",
                event.get("subtype", ""),
                event.get("session_id", ""),
                event.get("cwd", ""),
                event.get("tools", ""),
            )
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        logger.debug("claude text: %s", text[:2000])
                elif btype == "thinking":
                    thought = (block.get("thinking") or "").strip()
                    if thought:
                        logger.debug("claude thinking: %s", thought[:2000])
                elif btype == "tool_use":
                    logger.debug(
                        "claude tool_use: %s id=%s input=%s",
                        block.get("name", ""),
                        block.get("id", ""),
                        json.dumps(block.get("input", {}), ensure_ascii=False)[:2000],
                    )
        elif etype == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                content = block.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                logger.debug(
                    "claude tool_result%s id=%s: %s",
                    " (error)" if block.get("is_error") else "",
                    block.get("tool_use_id", ""),
                    str(content)[:2000],
                )
        elif etype == "result":
            logger.debug(
                "claude stream: result subtype=%s duration_ms=%s num_turns=%s usage=%s",
                event.get("subtype", ""),
                event.get("duration_ms", ""),
                event.get("num_turns", ""),
                event.get("usage", ""),
            )
        else:
            logger.debug("claude stream: %s %s", etype, json.dumps(event, ensure_ascii=False)[:500])

    async def _build_thread_prompt(self, channel: str, thread_ts: str) -> str:
        """Fetch Slack thread history and format as a conversation prompt."""
        resp = await self._slack_client.conversations_replies(
            channel=channel, ts=thread_ts
        )
        messages = resp.get("messages", [])

        lines = ["The following is a Slack conversation. Continue assisting the user.\n"]
        for msg in messages:
            is_bot = (
                msg.get("user") == self._bot_user_id
                or msg.get("bot_id")
            )
            label = "[Assistant]" if is_bot else "[Human]"
            text = msg.get("text", "")
            lines.append(f"{label}: {text}")

        return "\n".join(lines)
