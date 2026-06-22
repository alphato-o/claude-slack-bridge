"""Unit tests for src/slack_progress.py — renderer + reporter view-model.

No real Slack: a FakeAsyncClient records the Web API calls each reporter makes,
so we can assert the lifecycle (placeholder → live edits → final answer) without
network or timing flakiness.
"""

import asyncio

import pytest

from slack_progress import (
    ActivityRenderer,
    ChatUpdateReporter,
    NativeStreamReporter,
    _split_message,
    iter_activity,
    make_reporter,
)


# ---------- fakes ----------

class FakeAsyncClient:
    """Records calls; returns plausible Slack responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ts = 1700000000.0

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("postMessage", kwargs))
        self._ts += 1
        return {"ok": True, "ts": f"{self._ts:.6f}"}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"ok": True, "ts": kwargs.get("ts")}

    async def chat_startStream(self, **kwargs):
        self.calls.append(("startStream", kwargs))
        self._ts += 1
        return {"ok": True, "ts": f"{self._ts:.6f}"}

    async def chat_appendStream(self, **kwargs):
        self.calls.append(("appendStream", kwargs))
        return {"ok": True}

    async def chat_stopStream(self, **kwargs):
        self.calls.append(("stopStream", kwargs))
        return {"ok": True}

    async def assistant_threads_setStatus(self, **kwargs):
        self.calls.append(("setStatus", kwargs))
        return {"ok": True}

    def named(self, name: str) -> list[dict]:
        return [kw for (n, kw) in self.calls if n == name]


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _tool_result(content: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": content, "is_error": is_error}]},
    }


# ---------- ActivityRenderer ----------

class TestRenderer:
    def test_read_uses_basename(self):
        r = ActivityRenderer()
        assert r.tool_line("Read", {"file_path": "/a/b/session.py"}) == "📖 Reading session.py"

    def test_bash_shows_command(self):
        r = ActivityRenderer()
        assert r.tool_line("Bash", {"command": "pytest -q"}).startswith("⚡ pytest -q")

    def test_todowrite_summarises_progress(self):
        r = ActivityRenderer()
        todos = [
            {"status": "completed", "content": "a"},
            {"status": "in_progress", "content": "wire daemon"},
            {"status": "pending", "content": "c"},
        ]
        line = r.tool_line("TodoWrite", {"todos": todos})
        assert "3 steps" in line and "1 done" in line and "wire daemon" in line

    def test_unknown_tool_falls_back_to_name(self):
        r = ActivityRenderer()
        assert r.tool_line("Frobnicate", {"x": "y"}).startswith("🔧 Frobnicate")

    def test_long_command_is_truncated(self):
        r = ActivityRenderer()
        line = r.tool_line("Bash", {"command": "x" * 500})
        assert line.endswith("…") and len(line) < 200

    def test_result_line_only_in_rich(self):
        assert ActivityRenderer("rich").result_line("3 matches", False) == "   ↳ 3 matches"
        assert ActivityRenderer("normal").result_line("3 matches", False) is None

    def test_error_result_flagged(self):
        assert ActivityRenderer("rich").result_line("boom", True).startswith("   ↳ ❌")

    def test_thinking_suppressed_when_quiet(self):
        assert ActivityRenderer("quiet").thinking_line("hmm") is None
        assert ActivityRenderer("rich").thinking_line("hmm") == "🤔 _hmm_"


class TestIterActivity:
    def test_assistant_blocks_yield_lines(self):
        r = ActivityRenderer()
        ev = _assistant(
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/h.py"}},
            {"type": "text", "text": "Refactoring now."},
        )
        assert iter_activity(ev, r) == ["✏️ Editing h.py", "💬 Refactoring now."]

    def test_tool_result_event(self):
        assert iter_activity(_tool_result("done"), ActivityRenderer("rich")) == ["   ↳ done"]

    def test_result_event_yields_nothing(self):
        assert iter_activity({"type": "result", "result": "x"}, ActivityRenderer()) == []


class TestSplit:
    def test_short_message_not_split(self):
        assert _split_message("hello") == ["hello"]

    def test_long_message_split(self):
        chunks = _split_message("a" * 50, limit=20)
        assert len(chunks) == 3 and "".join(chunks) == "a" * 50


# ---------- ChatUpdateReporter lifecycle ----------

class TestChatUpdateReporter:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_start_posts_placeholder(self):
        client = FakeAsyncClient()
        rep = ChatUpdateReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep._shutdown()  # stop heartbeat cleanly

        self._run(go())
        posts = client.named("postMessage")
        assert len(posts) == 1 and posts[0]["thread_ts"] == "100.0"
        assert "On it" in posts[0]["text"]

    def test_event_triggers_update_with_activity(self):
        client = FakeAsyncClient()
        rep = ChatUpdateReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep.on_event(_assistant(
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/p/x.py"}}))
            await rep._shutdown()

        self._run(go())
        updates = client.named("update")
        assert updates and "Reading x.py" in updates[-1]["text"]

    def test_finish_writes_final_answer_with_footer(self):
        client = FakeAsyncClient()
        rep = ChatUpdateReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep.on_event({"type": "result", "result": "ignored",
                                "num_turns": 3, "usage": {"input_tokens": 10, "output_tokens": 2000}})
            await rep.finish("Here is the answer.")

        self._run(go())
        # Final answer goes via markdown_text (Slack renders Claude's markdown),
        # not the mrkdwn `text` param.
        update = client.named("update")[-1]
        assert "text" not in update
        final = update["markdown_text"]
        assert "Here is the answer." in final
        assert "3 turns" in final and "~2k tokens" in final

    def test_finish_splits_overlong_answer(self):
        client = FakeAsyncClient()
        rep = ChatUpdateReporter(client, "C1", "100.0")
        huge = "z" * 45000

        async def go():
            await rep.start()
            await rep.finish(huge)

        self._run(go())
        # First chunk edits the placeholder; remainder posts as follow-ups.
        assert client.named("update")
        assert len(client.named("postMessage")) >= 2  # placeholder + at least one overflow


# ---------- NativeStreamReporter lifecycle ----------

class TestNativeStreamReporter:
    def test_streams_text_and_tool_widgets(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(
            client, "C1", "100.0", recipient_user_id="U1", recipient_team_id="T1")

        async def go():
            await rep.start()
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}}))
            # tool_result flips the same task id to complete
            await rep.on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tool_1", "content": "a\nb"}]}})
            await rep.on_event(_assistant({"type": "text", "text": "All done."}))
            await rep.finish("All done.")

        asyncio.run(go())
        starts = client.named("startStream")[0]
        assert starts["recipient_user_id"] == "U1" and starts["recipient_team_id"] == "T1"
        appends = client.named("appendStream")
        # EVERYTHING goes through chunks (mixing top-level markdown_text with
        # chunks triggers streaming_mode_mismatch), so no append uses the
        # top-level markdown_text param.
        assert all("markdown_text" not in a for a in appends)
        chunks = [a["chunks"][0] for a in appends if "chunks" in a]
        # text streamed as a markdown_text chunk
        assert any(c["type"] == "markdown_text" for c in chunks)
        # tool rendered as a task_update chunk, then completed
        assert any(c["type"] == "task_update" and c["status"] == "in_progress" for c in chunks)
        assert any(c["type"] == "task_update" and c["status"] == "complete"
                   and c["id"] == "tool_1" for c in chunks)
        assert client.named("stopStream")

    def test_tool_result_error_marks_task_error(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "boom"}}))
            await rep.on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t9", "content": "nope", "is_error": True}]}})
            await rep.finish("done")

        asyncio.run(go())
        chunks = [a["chunks"][0] for a in client.named("appendStream") if "chunks" in a]
        assert any(c["id"] == "t9" and c["status"] == "error" for c in chunks)

    def test_abort_stops_stream(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep._abort()

        asyncio.run(go())
        assert client.named("stopStream")

    def test_todowrite_renders_live_plan(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(client, "C1", "100.0")

        def todos(*statuses):
            return {"todos": [{"content": f"step {i}", "status": s}
                              for i, s in enumerate(statuses)]}

        async def go():
            await rep.start()
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "tw1", "name": "TodoWrite",
                 "input": todos("completed", "in_progress", "pending")}))
            # second call updates the SAME widgets (same todo-* ids), no new banner
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "tw2", "name": "TodoWrite",
                 "input": todos("completed", "completed", "in_progress")}))
            await rep.finish("done")

        asyncio.run(go())
        all_chunks = [c for a in client.named("appendStream") if "chunks" in a for c in a["chunks"]]
        # exactly one plan banner across both TodoWrite calls
        assert sum(1 for c in all_chunks if c["type"] == "plan_update") == 1
        # todo-1 goes in_progress (call 1) then complete (call 2) — same stable id
        todo1 = [c for c in all_chunks if c.get("id") == "todo-1"]
        assert any(c["status"] == "in_progress" for c in todo1)
        assert any(c["status"] == "complete" for c in todo1)
        # the TodoWrite tool_use itself is not rendered as a generic task widget
        assert all(c.get("id") not in ("tw1", "tw2") for c in all_chunks)

    def test_taskfamily_renders_live_plan(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            # TaskCreate: the id is only known once the result comes back
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "u1", "name": "TaskCreate", "input": {"subject": "Build X"}}))
            await rep.on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1",
                 "content": "Task #1 created successfully: Build X"}]}})
            # TaskUpdate flips the same widget in_progress → completed
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "u2", "name": "TaskUpdate",
                 "input": {"taskId": "1", "status": "in_progress"}}))
            await rep.on_event(_assistant(
                {"type": "tool_use", "id": "u3", "name": "TaskUpdate",
                 "input": {"taskId": "1", "status": "completed"}}))
            await rep.finish("done")

        asyncio.run(go())
        all_chunks = [c for a in client.named("appendStream") if "chunks" in a for c in a["chunks"]]
        assert sum(1 for c in all_chunks if c["type"] == "plan_update") == 1
        task1 = [c for c in all_chunks if c.get("id") == "task-1"]
        assert any(c["status"] == "pending" for c in task1)
        assert any(c["status"] == "in_progress" for c in task1)
        assert any(c["status"] == "complete" for c in task1)
        assert all(c["title"] == "Build X" for c in task1)  # subject carried across updates
        # raw TaskCreate/TaskUpdate tool_use ids are not generic widgets
        assert all(c.get("id") not in ("u1", "u2", "u3") for c in all_chunks)

    def test_tool_result_without_widget_is_skipped(self):
        client = FakeAsyncClient()
        rep = NativeStreamReporter(client, "C1", "100.0")

        async def go():
            await rep.start()
            await rep.on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]}})
            await rep.finish("done")

        asyncio.run(go())
        all_chunks = [c for a in client.named("appendStream") if "chunks" in a for c in a["chunks"]]
        assert all(c.get("id") != "ghost" for c in all_chunks)


# ---------- factory ----------

class TestFactory:
    def test_off_posts_only_final(self):
        client = FakeAsyncClient()
        rep = make_reporter(client, "C1", "100.0", mode="off")

        async def go():
            await rep.start()
            await rep.on_event(_assistant({"type": "tool_use", "name": "Read", "input": {}}))
            await rep.finish("answer")

        asyncio.run(go())
        assert client.named("postMessage")[-1]["markdown_text"] == "answer"
        assert not client.named("update")  # no live edits in off mode

    def test_mode_selection(self):
        c = FakeAsyncClient()
        from slack_progress import FallbackReporter, _NullReporter
        assert isinstance(make_reporter(c, "C", "1", mode="off"), _NullReporter)
        assert isinstance(make_reporter(c, "C", "1", mode="update"), ChatUpdateReporter)
        assert isinstance(make_reporter(c, "C", "1", mode="native"), NativeStreamReporter)
        assert isinstance(make_reporter(c, "C", "1", mode="auto"), FallbackReporter)
