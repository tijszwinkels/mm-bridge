"""Bridge dispatch tests — use fake MM/VD clients and drive events in."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field

from mm_bridge.bridge import Bridge
from mm_bridge.config import Config


# ───────────────────── Fakes ──────────────────────────────────────────────


@dataclass
class _Post:
    channel_id: str
    message: str
    file_ids: list[str] | None = None
    root_id: str | None = None


@dataclass
class FakeMattermostClient:
    bot_user_id: str = "bot-user"
    bot_username: str = "claude"
    team_id: str = "team-1"
    channels: dict = field(default_factory=dict)
    posted: list[_Post] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    typing: list[tuple[str, str | None]] = field(default_factory=list)
    uploaded: list[tuple[str, str]] = field(default_factory=list)
    users: dict = field(default_factory=dict)
    posts_by_channel: dict = field(default_factory=dict)
    posts_by_id: dict = field(default_factory=dict)
    files_by_id: dict = field(default_factory=dict)
    download_failures: set = field(default_factory=set)

    def login(self) -> None:
        pass

    async def listen_websocket(self, handlers) -> None:
        # Not driven in unit tests.
        return

    def post_message(self, channel_id: str, message: str) -> dict:
        self.posted.append(_Post(channel_id, message))
        return {"id": "p"}

    def post(self, channel_id: str, message: str, *, file_ids=None, root_id=None):
        self.posted.append(_Post(channel_id, message, file_ids, root_id))
        return {"id": "p"}

    def create_channel(self, name: str, display_name: str, purpose: str = "") -> dict:
        cid = f"c-{name}"
        self.channels[cid] = {
            "id": cid, "name": name, "display_name": display_name, "purpose": purpose,
        }
        return {"id": cid}

    def rename_channel(self, channel_id: str, display_name: str) -> None:
        self.renames.append((channel_id, display_name))

    def set_channel_header(self, channel_id: str, header: str) -> None:
        pass

    def get_channel(self, channel_id: str) -> dict:
        return self.channels.get(channel_id, {"id": channel_id, "purpose": ""})

    def remove_self_from_channel(self, channel_id: str) -> None:
        self.removed.append(channel_id)

    def get_user(self, user_id: str) -> dict:
        return self.users.get(user_id, {"id": user_id, "username": f"u-{user_id[:4]}"})

    def publish_user_typing(self, channel_id: str, parent_id=None) -> None:
        self.typing.append((channel_id, parent_id))

    def upload_file(self, channel_id: str, path) -> str:
        fid = f"f-{len(self.uploaded)}"
        self.uploaded.append((channel_id, str(path)))
        return fid

    def download_file(self, file_id: str) -> bytes:
        if file_id in self.download_failures:
            raise RuntimeError(f"simulated download failure for {file_id}")
        return self.files_by_id.get(file_id, b"")

    def get_max_file_size(self) -> int:
        return 50 * 1024 * 1024

    def get_posts(self, channel_id: str, limit: int) -> list[dict]:
        return self.posts_by_channel.get(channel_id, [])[:limit]

    def get_post(self, post_id: str) -> dict:
        return self.posts_by_id.get(post_id, {"message": ""})


@dataclass
class FakeVibeDeckClient:
    created: list[dict] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    forks: list[tuple[str, str]] = field(default_factory=list)
    titles: list[tuple[str, str | None]] = field(default_factory=list)
    next_session_id: str = "session-next"
    fork_response: dict | None = None
    sessions_meta: list[dict] = field(default_factory=list)
    models_by_backend: dict = field(default_factory=lambda: {
        "claude": ["opus", "sonnet"],
        "codex": ["gpt-5.4"],
        "pi": ["pi-v1"],
        "opencode": [],
    })

    async def close(self) -> None:
        pass

    async def health(self) -> dict:
        return {"ok": True}

    async def create_session(self, message, cwd, backend=None, model_index=None,
                             source_session_id=None) -> dict:
        self.created.append({
            "message": message, "cwd": cwd, "backend": backend,
            "model_index": model_index, "source_session_id": source_session_id,
        })
        return {"status": "started", "cwd": cwd}

    async def send_message(self, session_id, message) -> dict:
        self.sent.append((session_id, message))
        return {"status": "sent"}

    async def fork_session(self, session_id, message) -> dict:
        self.forks.append((session_id, message))
        if self.fork_response is not None:
            return self.fork_response
        return {"status": "forking", "session_id": session_id}

    async def list_sessions(self) -> list[dict]:
        return self.sessions_meta

    async def get_session_meta(self, session_id):
        for s in self.sessions_meta:
            if s.get("id") == session_id:
                return s
        return {}

    async def set_session_title(self, session_id, title) -> None:
        self.titles.append((session_id, title))

    async def list_models(self, backend) -> list[str]:
        return self.models_by_backend.get(backend, [])

    async def stream_events(self, on_event) -> None:
        return


# ───────────────────── Test fixtures ──────────────────────────────────────


class _BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):  # type: ignore[override]
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            mm_bot_token="t",
            default_cwd="/tmp/proj",
            state_file=f"{self.tmp.name}/state.json",
            default_backend="claude",
            default_model="opus",
        )
        self.bridge = Bridge(self.config)
        self.bridge.mm = FakeMattermostClient()
        self.bridge.vd = FakeVibeDeckClient()
        from mm_bridge.typing_indicator import TypingIndicator
        self.bridge.typing = TypingIndicator(self.bridge.mm, refresh_s=0.01)

    async def asyncTearDown(self):  # type: ignore[override]
        self.tmp.cleanup()


# ───────────────────── Tests ──────────────────────────────────────────────


class InviteFlowTests(_BridgeTestCase):
    async def test_bot_invited_to_unmapped_channel_creates_session(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/tmp/proj")
        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")
        self.assertIn("c1", self.bridge.pending_mm_sessions)
        # A welcome post (plus no warnings since purpose was empty).
        self.assertTrue(any("Session started" in p.message for p in self.bridge.mm.posted))

    async def test_bot_invited_to_mapped_channel_is_noop(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self.bridge.vd.created), 0)

    async def test_purpose_with_model_resolves_model_index(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, sonnet"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")
        self.assertEqual(self.bridge.vd.created[0]["model_index"], 1)

    async def test_unknown_purpose_token_posts_warning_and_uses_defaults(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "opusz"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertTrue(any(":warning:" in p.message for p in self.bridge.mm.posted))
        self.assertEqual(self.bridge.vd.created[0]["backend"], "claude")

    async def test_mention_only_token_is_cached_on_channel(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, mention-only"}

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)


class ForwardingTests(_BridgeTestCase):
    async def test_posted_in_mapped_channel_forwards_to_session(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hi")])

    async def test_posted_in_unmapped_channel_is_dropped(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_queues_while_session_pending(self):
        self.bridge.pending_mm_sessions["c1"] = type(self.bridge.pending_mm_sessions.get("c1") or self.bridge)  # placeholder
        # Use the real dataclass:
        from mm_bridge.bridge import PendingMattermostSession
        import time as _time
        self.bridge.pending_mm_sessions["c1"] = PendingMattermostSession(
            channel_id="c1", cwd="/tmp/proj", backend="claude",
            initial_message="placeholder", requested_at=_time.monotonic(),
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "waiting msg", "user_id": "u1", "type": "",
        })

        self.assertIn("waiting msg",
                      self.bridge.pending_mm_sessions["c1"].queued_messages)
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_attribution_kicks_in_on_second_user(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "first", "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "second", "user_id": "u2", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent[0], ("s1", "first"))
        self.assertTrue(self.bridge.vd.sent[1][1].startswith("u-u2: second"))

    async def test_mention_only_filters_non_mentions(self):
        self.bridge.mapping.link("c1", "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "just chatting", "user_id": "u1", "type": "",
        })
        self.assertEqual(self.bridge.vd.sent, [])

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude help please",
            "user_id": "u1", "type": "",
        })
        self.assertEqual(self.bridge.vd.sent, [("s1", "help please")])


class InboundAttachmentTests(_BridgeTestCase):
    """User attaches files in MM → bridge downloads them into the session cwd."""

    def _set_session_cwd(self, session_id: str, cwd: str) -> None:
        self.bridge.vd.sessions_meta.append(
            {"id": session_id, "projectPath": cwd, "backend": "claude"},
        )

    def _post_with_attachment(
        self, channel_id: str, file_id: str, name: str, body: bytes, *,
        message: str = "", root_id: str | None = None,
    ) -> dict:
        self.bridge.mm.files_by_id[file_id] = body
        return {
            "channel_id": channel_id,
            "message": message,
            "user_id": "u1",
            "type": "",
            "root_id": root_id or "",
            "file_ids": [file_id],
            "metadata": {"files": [{"id": file_id, "name": name, "size": len(body)}]},
        }

    async def test_attachment_saved_to_inbox_and_note_prepended(self):
        from pathlib import Path
        self.bridge.mapping.link("c1", "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment(
            "c1", "fid-1", "notes.md", b"# hello\n", message="please look at this",
        )
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "notes.md"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"# hello\n")
        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent_body = self.bridge.vd.sent[0][1]
        self.assertIn(f"[User attached file: {saved}]", sent_body)
        self.assertIn("please look at this", sent_body)
        self.assertTrue(sent_body.startswith("[User attached file: "))

    async def test_attachment_only_post_still_forwards(self):
        from pathlib import Path
        self.bridge.mapping.link("c1", "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment("c1", "fid-1", "photo.png", b"\x89PNG...")
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "photo.png"
        self.assertTrue(saved.exists())
        self.assertEqual(len(self.bridge.vd.sent), 1)
        self.assertIn(f"[User attached file: {saved}]", self.bridge.vd.sent[0][1])

    async def test_filename_conflict_gets_suffix(self):
        from pathlib import Path
        self.bridge.mapping.link("c1", "s1")
        self._set_session_cwd("s1", self.tmp.name)
        existing = Path(self.tmp.name) / ".mattermost-inbox"
        existing.mkdir()
        (existing / "data.txt").write_bytes(b"original")

        post = self._post_with_attachment("c1", "fid-1", "data.txt", b"second")
        await self.bridge._on_mm_posted(post)

        self.assertEqual((existing / "data.txt").read_bytes(), b"original")
        self.assertEqual((existing / "data-1.txt").read_bytes(), b"second")
        self.assertIn("data-1.txt", self.bridge.vd.sent[0][1])

    async def test_traversal_filename_is_sanitized(self):
        from pathlib import Path
        self.bridge.mapping.link("c1", "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment(
            "c1", "fid-1", "../../etc/passwd", b"evil",
        )
        await self.bridge._on_mm_posted(post)

        inbox = Path(self.tmp.name) / ".mattermost-inbox"
        saved = list(inbox.iterdir())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].parent, inbox)
        self.assertEqual(saved[0].read_bytes(), b"evil")

    async def test_download_failure_yields_skipped_note(self):
        self.bridge.mapping.link("c1", "s1")
        self._set_session_cwd("s1", self.tmp.name)
        self.bridge.mm.download_failures.add("fid-bad")

        post = self._post_with_attachment(
            "c1", "fid-bad", "doc.pdf", b"unused", message="hi",
        )
        await self.bridge._on_mm_posted(post)

        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent_body = self.bridge.vd.sent[0][1]
        self.assertIn("[MM attachment skipped: `doc.pdf` download failed]", sent_body)
        self.assertIn("hi", sent_body)

    async def test_thread_fork_downloads_to_parent_cwd(self):
        from pathlib import Path
        self.bridge.mapping.link("c1", "s-parent")
        self._set_session_cwd("s-parent", self.tmp.name)
        self.bridge.mm.posts_by_id["root-post"] = {"message": "original msg"}

        post = self._post_with_attachment(
            "c1", "fid-t", "thread.txt", b"fork payload",
            message="replying", root_id="root-post",
        )
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "thread.txt"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"fork payload")

        self.assertEqual(len(self.bridge.vd.forks), 1)
        fork_body = self.bridge.vd.forks[0][1]
        self.assertIn(f"[User attached file: {saved}]", fork_body)
        self.assertIn("replying", fork_body)
        self.assertIn("Mattermost thread context", fork_body)


class CatchUpTests(_BridgeTestCase):
    async def test_catch_up_command_sends_block_to_session(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.mm.posts_by_channel["c1"] = [
            {"user_id": "u1", "message": "m1", "type": ""},
            {"user_id": self.bridge.mm.bot_user_id, "message": "bot echo", "type": ""},
            {"user_id": "u2", "message": "m2", "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude catch up 50",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        block = self.bridge.vd.sent[0][1]
        self.assertIn("m1", block)
        self.assertIn("m2", block)
        self.assertNotIn("bot echo", block)


class LeaveTests(_BridgeTestCase):
    async def test_leave_command_removes_bot_and_unlinks(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude leave done",
            "user_id": "u1", "type": "",
        })

        self.assertIn("c1", self.bridge.mm.removed)
        self.assertIsNone(self.bridge.mapping.get_session("c1"))

    async def test_user_removed_unlinks_mapping(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertIsNone(self.bridge.mapping.get_session("c1"))


class SessionAddedClaimTests(_BridgeTestCase):
    async def test_session_added_claims_pending_invite(self):
        from mm_bridge.bridge import PendingMattermostSession
        import time as _time
        self.bridge.pending_mm_sessions["c1"] = PendingMattermostSession(
            channel_id="c1", cwd="/tmp/proj", backend="claude",
            initial_message="placeholder", requested_at=_time.monotonic(),
        )

        await self.bridge._on_vd_event("session_added", {
            "id": "sess-new", "projectPath": "/tmp/proj",
            "firstMessage": "placeholder", "backend": "claude",
        })

        self.assertEqual(self.bridge.mapping.get_session("c1"), "sess-new")
        self.assertNotIn("c1", self.bridge.pending_mm_sessions)

    async def test_session_added_without_pending_creates_channel(self):
        await self.bridge._on_vd_event("session_added", {
            "id": "sess-cli", "projectPath": "/tmp/proj",
            "projectName": "my-project", "firstMessage": "hi from CLI",
        })

        self.assertTrue(self.bridge.mapping.get_channel("sess-cli"))
        self.assertTrue(self.bridge.mm.channels)


class ThreadForkTests(_BridgeTestCase):
    async def test_thread_post_in_mapped_channel_calls_fork(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.vd.sessions_meta = [
            {"id": "s1", "projectPath": "/tmp/proj", "backend": "claude"},
        ]
        self.bridge.mm.posts_by_id["r1"] = {"message": "root post body"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "r1", "message": "thread starter",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.forks), 1)
        fork_sid, fork_msg = self.bridge.vd.forks[0]
        self.assertEqual(fork_sid, "s1")
        self.assertIn("Mattermost thread context", fork_msg)
        self.assertIn("> root post body", fork_msg)
        self.assertTrue(fork_msg.endswith("thread starter"))
        self.assertEqual(len(self.bridge.pending_forks), 1)

    async def test_thread_fork_unavailable_marks_dead(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.vd.fork_response = {
            "status": "fork_unavailable", "reason": "opencode", "http_status": 501,
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": "r1", "message": "thread starter",
            "user_id": "u1", "type": "",
        })

        self.assertIn(("c1", "r1"), self.bridge.dead_threads)
        self.assertEqual(len(self.bridge.pending_forks), 0)

    async def test_session_added_claims_fork_then_posts_disclaimer(self):
        from mm_bridge.bridge import PendingMattermostSession
        import time as _time
        self.bridge.mapping.link("c1", "s1")
        self.bridge.pending_forks.append(PendingMattermostSession(
            channel_id="c1", cwd="/tmp/proj", backend="claude",
            initial_message="thread starter", requested_at=_time.monotonic(),
            is_fork=True, fork_parent_session="s1",
            fork_thread_channel="c1", fork_thread_root="r1",
        ))

        # On Claude Code, a fork's firstMessage is the parent's
        # context-continuation summary — NOT the user's thread message.
        # The claim must succeed anyway (matched by cwd).
        await self.bridge._on_vd_event("session_added", {
            "id": "sess-fork", "projectPath": "/tmp/proj",
            "firstMessage": "This session is being continued from a previous conversation...",
            "backend": "claude",
        })

        self.assertEqual(
            self.bridge.mapping.get_thread_session("c1", "r1"), "sess-fork",
        )
        disclaimer = [p for p in self.bridge.mm.posted if "Forked conversation" in p.message]
        self.assertEqual(len(disclaimer), 1)
        self.assertEqual(disclaimer[0].root_id, "r1")


class AssistantMessageTests(_BridgeTestCase):
    async def test_plain_text_posts_to_channel(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [{"type": "text", "text": "hello there"}],
            },
        })

        self.assertTrue(any(p.message == "hello there" for p in self.bridge.mm.posted))

    async def test_leave_channel_directive_removes_bot(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "text", "text": 'goodbye <leaveChannel reason="done" />'},
                ],
            },
        })

        self.assertIn("c1", self.bridge.mm.removed)
        self.assertIsNone(self.bridge.mapping.get_session("c1"))


class NameSyncTests(_BridgeTestCase):
    async def test_channel_renamed_syncs_title_to_vibedeck(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "old", "purpose": "",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "new-name", "purpose": "",
        })

        self.assertEqual(self.bridge.vd.titles, [("s1", "new-name")])

    async def test_summary_updated_renames_channel(self):
        self.bridge.mapping.link("c1", "s1")

        await self.bridge._on_vd_event("session_summary_updated", {
            "session_id": "s1", "summaryTitle": "Great Session",
        })

        self.assertEqual(self.bridge.mm.renames, [("c1", "Great Session")])

    async def test_name_sync_prevents_ping_pong(self):
        self.bridge.mapping.link("c1", "s1")
        self.bridge.name_sync.note_remote_update("mm", "c1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "old", "purpose": "",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "new-name", "purpose": "",
        })

        self.assertEqual(self.bridge.vd.titles, [])


if __name__ == "__main__":
    unittest.main()
