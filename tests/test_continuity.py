"""Tests for per-cwd continuous sessions, the journal, and /new (claude_handler)."""

import asyncio

import claude_handler
from claude_handler import ClaudeHandler


def _patch_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handler, "CLAUDE_HOME", tmp_path)
    monkeypatch.setattr(claude_handler, "SESSIONS_FILE", tmp_path / "bridge-sessions.json")
    monkeypatch.setattr(claude_handler, "JOURNAL_DIR", tmp_path / "journals")


class TestSessionFor:
    def test_lifecycle_create_recreate_resume_new(self, tmp_path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        h = ClaudeHandler(slack_client=None)
        h._session = {}

        # First use → create (not resume)
        sid1, resume1 = h._session_for("/projects/X", "/projects/X", force_new=False)
        assert resume1 is False

        # Stored, but transcript not on disk yet → recreate same id (never a bad resume)
        sid2, resume2 = h._session_for("/projects/X", "/projects/X", force_new=False)
        assert sid2 == sid1 and resume2 is False

        # Transcript now exists → resume it
        pdir = tmp_path / "projects" / "-projects-X"
        pdir.mkdir(parents=True)
        (pdir / f"{sid1}.jsonl").write_text("{}")
        sid3, resume3 = h._session_for("/projects/X", "/projects/X", force_new=False)
        assert sid3 == sid1 and resume3 is True

        # /new → brand-new id
        sid4, resume4 = h._session_for("/projects/X", "/projects/X", force_new=True)
        assert sid4 != sid1 and resume4 is False

    def test_persists_across_instances(self, tmp_path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        h1 = ClaudeHandler(slack_client=None)
        sid, _ = h1._session_for("/projects/Y", "/projects/Y", force_new=False)
        # A fresh handler (simulating a restart) loads the persisted map
        h2 = ClaudeHandler(slack_client=None)
        assert h2._session.get("/projects/Y") == sid


class TestJournal:
    def test_append_and_tail_roundtrip(self, tmp_path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        claude_handler._append_journal("/projects/X", "do the thing", "did the thing well")
        tail = claude_handler._journal_tail("/projects/X")
        assert "do the thing" in tail and "did the thing well" in tail

    def test_tail_empty_when_no_journal(self, tmp_path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        assert claude_handler._journal_tail("/projects/none") == ""


class TestMemoryAddendum:
    def test_points_at_the_shared_cwd_memory_dir(self):
        a = claude_handler._memory_addendum("/projects/RoxImproved")
        assert "/home/appuser/.claude/projects/-projects-RoxImproved/memory" in a
        assert "MEMORY.md" in a and "SHARED" in a

    def test_empty_when_no_project_dir(self):
        assert claude_handler._memory_addendum(None) == ""


class TestNewCommand:
    def test_bare_new_resets_and_acks(self, tmp_path, monkeypatch):
        _patch_paths(tmp_path, monkeypatch)
        h = ClaudeHandler(slack_client=None)
        h._channel_id_to_project = {}  # unmapped → default cwd key chan:<id>
        h._session = {"chan:C1": "old-session"}

        res = asyncio.run(h.handle_turn("C1", "1782200000.0", "/new"))
        assert "fresh conversation" in res.lower()
        assert "chan:C1" not in h._session  # session was reset
