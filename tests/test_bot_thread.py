"""Unit tests for SlackDaemon._is_bot_thread — the gate that keeps the bot out of
humans' own threads (only engage threads it was tagged into / has posted in)."""

import asyncio

from slack_daemon import SlackDaemon


class FakeClient:
    def __init__(self, messages=None, boom=False):
        self.messages = messages or []
        self.boom = boom
        self.calls = 0

    async def conversations_replies(self, channel, ts, limit=100):
        self.calls += 1
        if self.boom:
            raise RuntimeError("rate_limited")
        return {"messages": self.messages}


class FakeApp:
    def __init__(self, client):
        self.client = client


def _daemon(messages=None, boom=False):
    d = SlackDaemon.__new__(SlackDaemon)  # skip AsyncApp construction
    d._bot_user_id = "UBOT"
    d._app = FakeApp(FakeClient(messages, boom))
    return d


class TestIsBotThread:
    def test_true_when_parent_mentions_bot(self):
        d = _daemon([{"user": "UHUMAN", "text": "<@UBOT> do x"},
                     {"user": "UHUMAN2", "text": "a reply"}])
        assert asyncio.run(d._is_bot_thread("C1", "1.0")) is True

    def test_true_when_bot_has_posted(self):
        d = _daemon([{"user": "UHUMAN", "text": "bragging"},
                     {"user": "UBOT", "text": "the bot chimed in earlier"}])
        assert asyncio.run(d._is_bot_thread("C1", "1.0")) is True

    def test_false_for_human_only_thread(self):
        d = _daemon([{"user": "UHUMAN", "text": "isn't this just claude tag?"},
                     {"user": "UHUMAN2", "text": "lol I thought so too"}])
        assert asyncio.run(d._is_bot_thread("C1", "1.0")) is False

    def test_false_and_safe_on_api_error(self):
        # If we can't classify, stay out (don't barge into a human thread).
        d = _daemon(boom=True)
        assert asyncio.run(d._is_bot_thread("C1", "1.0")) is False
