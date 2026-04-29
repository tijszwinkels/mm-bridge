"""Bridge dispatch tests — use fake MM/VD clients and drive events in."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field

from mm_bridge.bridge import Bridge
from mm_bridge.config import Anchor, Config


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
    edits: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[tuple[str, bool]] = field(default_factory=list)  # (post_id, permanent)
    renames: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    typing: list[tuple[str, str | None]] = field(default_factory=list)
    uploaded: list[tuple[str, str]] = field(default_factory=list)
    users: dict = field(default_factory=dict)
    posts_by_channel: dict = field(default_factory=dict)
    posts_by_id: dict = field(default_factory=dict)
    files_by_id: dict = field(default_factory=dict)
    download_failures: set = field(default_factory=set)
    # Auto-join support:
    bot_channel_ids: set = field(default_factory=set)
    public_channels: list = field(default_factory=list)
    joined: list = field(default_factory=list)
    # First-message preamble support:
    channel_members: dict = field(default_factory=dict)
    # Permanent-delete failure simulation:
    permanent_delete_disabled: bool = False
    _post_counter: int = 0
    # Mirror of MattermostClient's own-post tracking — populated
    # whenever the fake creates or edits a post.
    own_post_ids: set = field(default_factory=set)

    def _next_post_id(self) -> str:
        self._post_counter += 1
        return f"p{self._post_counter}"

    def login(self) -> None:
        pass

    async def listen_websocket(self, handlers) -> None:
        # Not driven in unit tests.
        return

    def is_own_post(self, post_id: str) -> bool:
        return bool(post_id) and post_id in self.own_post_ids

    def post_message(self, channel_id: str, message: str) -> dict:
        pid = self._next_post_id()
        self.posted.append(_Post(channel_id, message))
        self.own_post_ids.add(pid)
        return {"id": pid}

    def post(self, channel_id: str, message: str, *, file_ids=None, root_id=None):
        pid = self._next_post_id()
        self.posted.append(_Post(channel_id, message, file_ids, root_id))
        self.own_post_ids.add(pid)
        return {"id": pid}

    def update_post(self, post_id: str, message: str) -> dict:
        self.edits.append((post_id, message))
        self.own_post_ids.add(post_id)
        return {"id": post_id}

    def delete_post(self, post_id: str, *, permanent: bool = False) -> None:
        if permanent and self.permanent_delete_disabled:
            raise RuntimeError(
                "Cannot delete post, ServiceSettings.EnableAPIPostDeletion is not enabled."
            )
        self.deletes.append((post_id, permanent))

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

    def set_channel_purpose(self, channel_id: str, purpose: str) -> None:
        self.channels.setdefault(channel_id, {"id": channel_id})["purpose"] = purpose

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

    def get_bot_channel_ids(self) -> set:
        return set(self.bot_channel_ids)

    def list_public_team_channels(self) -> list:
        return list(self.public_channels)

    def join_channel(self, channel_id: str) -> None:
        self.joined.append(channel_id)
        self.bot_channel_ids.add(channel_id)

    def join_all_public_team_channels(self) -> list:
        newly = [
            ch["id"] for ch in self.public_channels
            if ch["id"] not in self.bot_channel_ids
        ]
        for cid in newly:
            self.join_channel(cid)
        return newly

    def get_channel_members(self, channel_id: str) -> list:
        return list(self.channel_members.get(channel_id, []))


@dataclass
class FakeVibeDeckClient:
    created: list[dict] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    forks: list[tuple[str, str]] = field(default_factory=list)
    titles: list[tuple[str, str | None]] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
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

    async def interrupt_session(self, session_id) -> dict:
        self.interrupted.append(session_id)
        return {"status": "interrupted", "session_id": session_id}

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
            sidecar_dir=f"{self.tmp.name}/sidecar",
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

    async def test_invite_claim_normalises_backend_name_variants(self):
        """VD echoes the display name ('Claude Code', 'Codex') in session_added
        while pending stores the purpose token ('claude', 'codex'). The claim
        must still recognise them as the same backend."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "codex"}

        orig_create = self.bridge.vd.create_session

        async def create_that_renames_backend(**kwargs):
            await self.bridge._on_vd_session_added({
                "id": "s-new",
                "projectPath": kwargs["cwd"],
                "backend": "Codex",  # VD's SSE-side display form
                "firstMessage": "Hello! I've just been added",
            })
            return await orig_create(**kwargs)

        self.bridge.vd.create_session = create_that_renames_backend

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-new")

    async def test_invite_claim_normalises_claude_to_claude_code(self):
        """Purpose token 'claude' and VD's 'Claude Code' must be treated as the
        same backend by the claim path."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude"}

        orig_create = self.bridge.vd.create_session

        async def create_that_renames_backend(**kwargs):
            await self.bridge._on_vd_session_added({
                "id": "s-claude",
                "projectPath": kwargs["cwd"],
                "backend": "Claude Code",
                "firstMessage": "Hello! I've just been added",
            })
            return await orig_create(**kwargs)

        self.bridge.vd.create_session = create_that_renames_backend

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-claude")

    async def test_invite_claimed_when_session_added_fires_during_create(self):
        """Regression: VD emits session_added SSE before create_session HTTP
        returns, so the pending entry must be registered BEFORE the await."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        orig_create = self.bridge.vd.create_session

        async def racing_create(**kwargs):
            # SSE arrives mid-await, before create_session returns
            await self.bridge._on_vd_session_added({
                "id": "s-new",
                "projectPath": kwargs["cwd"],
                "backend": kwargs.get("backend") or "claude",
                "firstMessage": "Hello! I've just been added",
            })
            return await orig_create(**kwargs)

        self.bridge.vd.create_session = racing_create

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "s-new")
        # No orphan auto-channel created by _create_channel_for_session
        self.assertEqual(self.bridge.mm.channels, {"c1": {"id": "c1", "purpose": ""}})

    async def test_invite_sets_vd_session_title_from_mm_display_name(self):
        """On claim, the VD session title mirrors the MM channel's display_name."""
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "", "display_name": "My Project Discussion",
        }

        orig_create = self.bridge.vd.create_session

        async def racing_create(**kwargs):
            await self.bridge._on_vd_session_added({
                "id": "s-new",
                "projectPath": kwargs["cwd"],
                "backend": kwargs.get("backend") or "claude",
                "firstMessage": "Hello! I've just been added",
            })
            return await orig_create(**kwargs)

        self.bridge.vd.create_session = racing_create

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertIn(("s-new", "My Project Discussion"), self.bridge.vd.titles)

    async def test_invite_claim_skips_title_when_display_name_empty(self):
        """Blank display_name → no title set (leave VD's default)."""
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "", "display_name": "",
        }

        orig_create = self.bridge.vd.create_session

        async def racing_create(**kwargs):
            await self.bridge._on_vd_session_added({
                "id": "s-new",
                "projectPath": kwargs["cwd"],
                "backend": kwargs.get("backend") or "claude",
                "firstMessage": "Hello! I've just been added",
            })
            return await orig_create(**kwargs)

        self.bridge.vd.create_session = racing_create

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.titles, [])

    async def test_bot_invited_to_mapped_channel_is_noop(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
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

    async def test_purpose_cwd_inside_allowed_roots_is_applied(self):
        self.config.allowed_attachment_roots = [self.tmp.name]
        project = f"{self.tmp.name}/myproj"
        import os; os.makedirs(project, exist_ok=True)
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": f"claude, cwd={project}",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.created[0]["cwd"], project)
        # No warning about the cwd itself (welcome post is the only notice).
        self.assertFalse(
            any(":warning:" in p.message and "cwd" in p.message
                for p in self.bridge.mm.posted)
        )

    async def test_purpose_cwd_outside_allowed_roots_warns_and_uses_default(self):
        self.config.allowed_attachment_roots = [self.tmp.name]
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, cwd=/etc/passwd-dir",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/tmp/proj")
        self.assertTrue(
            any(":warning:" in p.message and "cwd" in p.message
                for p in self.bridge.mm.posted),
            f"expected a cwd rejection warning; got {[p.message for p in self.bridge.mm.posted]}",
        )

    async def test_purpose_cwd_trusted_when_no_allowed_roots(self):
        self.config.allowed_attachment_roots = []
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, cwd=/opt/whatever",
        }

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(self.bridge.vd.created[0]["cwd"], "/opt/whatever")


class ForwardingTests(_BridgeTestCase):
    async def test_posted_in_mapped_channel_forwards_to_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hi")])

    async def test_posted_in_unmapped_channel_is_dropped(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": "hi", "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_with_from_bridge_cli_prop_is_skipped(self):
        """Posts authored by the bridge CLI (e.g. ``mm-bridge spawn``'s
        parent-channel announcement) carry a ``props.from_bridge_cli``
        marker so the daemon can recognise them on the WS echo and
        avoid forwarding them to the linked session as a user turn.
        Without this, the parent session reads its own spawn
        announcement back as if a human had typed it."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": ":thread: Spawned ...",
            "user_id": "u1", "type": "",
            "props": {"from_bridge_cli": "spawn-announcement"},
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_posted_without_from_bridge_cli_prop_forwards_normally(self):
        """Regression guard: posts whose ``props`` dict is missing or
        lacks the marker still forward to the linked session."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "human msg",
            "user_id": "u1", "type": "",
            "props": {"unrelated": "value"},
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "human msg")])

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
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "first", "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "second", "user_id": "u2", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent[0], ("s1", "first"))
        self.assertTrue(self.bridge.vd.sent[1][1].startswith("u-u2: second"))

    async def test_mention_only_drops_are_replayed_on_next_mention(self):
        """Non-mentions are held in an in-memory queue and prepended to
        the next forwarded message as a catch-up block — the session
        should see the conversation it missed while mention-only was
        silencing it."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "just chatting",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "more chatter",
            "user_id": "u2", "type": "",
        })
        self.assertEqual(self.bridge.vd.sent, [])

        await self.bridge._on_mm_posted({
            "id": "p3", "channel_id": "c1", "message": "@claude help please",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        session_id, body = self.bridge.vd.sent[0]
        self.assertEqual(session_id, "s1")
        self.assertIn("Catch-up context", body)
        self.assertIn("u-u1: just chatting", body)
        self.assertIn("u-u2: more chatter", body)
        self.assertTrue(body.rstrip().endswith("help please"))

    async def test_mention_only_drops_cleared_after_replay(self):
        """The silent-drop queue is drained on replay — a second mention
        after a quiet gap should not re-include earlier drops."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "earlier",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "@claude first",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p3", "channel_id": "c1", "message": "@claude second",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        self.assertIn("earlier", self.bridge.vd.sent[0][1])
        self.assertNotIn("earlier", self.bridge.vd.sent[1][1])
        self.assertNotIn("Catch-up context", self.bridge.vd.sent[1][1])

    async def test_mention_only_drops_capped_by_initial_catch_up_n(self):
        """If more than ``initial_catch_up_n`` messages pile up, only
        the most recent N survive."""
        self.config.initial_catch_up_n = 3
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        for i in range(5):
            await self.bridge._on_mm_posted({
                "id": f"d{i}", "channel_id": "c1", "message": f"drop{i}",
                "user_id": "u1", "type": "",
            })
        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude ping",
            "user_id": "u1", "type": "",
        })

        body = self.bridge.vd.sent[0][1]
        self.assertNotIn("drop0", body)
        self.assertNotIn("drop1", body)
        self.assertIn("drop2", body)
        self.assertIn("drop3", body)
        self.assertIn("drop4", body)

    async def test_mention_only_drops_disabled_when_catch_up_is_zero(self):
        """``initial_catch_up_n = 0`` disables auto-replay too — matches
        the knob that disables initial session catch-up."""
        self.config.initial_catch_up_n = 0
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "p1", "channel_id": "c1", "message": "silent",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "p2", "channel_id": "c1", "message": "@claude hi",
            "user_id": "u1", "type": "",
        })

        body = self.bridge.vd.sent[0][1]
        self.assertNotIn("silent", body)
        self.assertNotIn("Catch-up context", body)

    async def test_explicit_catch_up_drains_silent_drop_queue(self):
        """`@claude catch up` already surfaces the missed conversation
        from MM history; silent-drop entries that appear in the
        catch-up block get cleared so the next mention doesn't replay
        the same context twice."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        # The MM history used by `_run_catch_up` mirrors the incoming
        # post ids so `_drain_silent_drops_matching` can align them.
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "d1", "user_id": "u1", "message": "ambient chatter",
             "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "ambient chatter",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_posted({
            "id": "cu", "channel_id": "c1", "message": "@claude catch up",
            "user_id": "u1", "type": "",
        })

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

        # Next mention must not prepend another catch-up block —
        # history has already been delivered by the catch-up command.
        await self.bridge._on_mm_posted({
            "id": "next", "channel_id": "c1", "message": "@claude what next",
            "user_id": "u1", "type": "",
        })

        last_body = self.bridge.vd.sent[-1][1]
        self.assertNotIn("Catch-up context", last_body)
        self.assertNotIn("ambient chatter", last_body)

    async def test_partial_catch_up_preserves_unsurfaced_drops(self):
        """``@claude catch up 1`` surfaces only the most recent message;
        earlier queued drops that weren't in the block must remain so
        the next mention still replays them."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        # `get_posts` is served in `oldest-first` order and the
        # collector scans as-is, taking up to n entries.
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "older", "user_id": "u1", "message": "older chatter",
             "type": ""},
            {"id": "newer", "user_id": "u1", "message": "newer chatter",
             "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "id": "older", "channel_id": "c1", "message": "older chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "newer", "channel_id": "c1", "message": "newer chatter",
            "user_id": "u1", "type": "",
        })
        self.assertEqual(len(self.bridge._silent_drops[("c1", None)]), 2)

        await self.bridge._on_mm_posted({
            "id": "cu", "channel_id": "c1", "message": "@claude catch up 1",
            "user_id": "u1", "type": "",
        })

        # Only the single surfaced post ("older", first by get_posts
        # order) is removed; "newer" stays queued.
        remaining = self.bridge._silent_drops.get(("c1", None))
        self.assertIsNotNone(remaining)
        remaining_ids = [p.get("id") for p in remaining]
        self.assertEqual(remaining_ids, ["newer"])

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude what now",
            "user_id": "u1", "type": "",
        })

        last_body = self.bridge.vd.sent[-1][1]
        self.assertIn("newer chatter", last_body)
        self.assertNotIn("older chatter", last_body)

    async def test_mention_only_drops_survive_send_failure(self):
        """If the VD send fails while replaying silent drops, the queue
        must not be lost AND the failed trigger itself is re-queued —
        otherwise a transient outage would eat the user's actual
        request."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "drop-1", "channel_id": "c1", "message": "dropped once",
            "user_id": "u1", "type": "",
        })

        # First mention: VD raises. Queue must preserve the old drop
        # and also enqueue the failed trigger.
        async def failing_send(session_id, message):
            raise RuntimeError("simulated VD outage")

        self.bridge.vd.send_message = failing_send  # type: ignore[assignment]
        await self.bridge._on_mm_posted({
            "id": "trigger-1", "channel_id": "c1", "message": "@claude first try",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(any(
            ":warning:" in p.message for p in self.bridge.mm.posted
        ))
        self.assertIn(("c1", None), self.bridge._silent_drops)
        self.assertEqual(len(self.bridge._silent_drops[("c1", None)]), 2)

        # Second mention: VD back online. Catch-up block carries BOTH
        # the original drop and the failed trigger; queue clears only
        # after a successful send.
        captured: list[tuple[str, str]] = []

        async def ok_send(session_id, message):
            captured.append((session_id, message))
            return {"status": "sent"}

        self.bridge.vd.send_message = ok_send  # type: ignore[assignment]
        await self.bridge._on_mm_posted({
            "id": "trigger-2", "channel_id": "c1", "message": "@claude second try",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(captured), 1)
        _, body = captured[0]
        self.assertIn("dropped once", body)
        self.assertIn("first try", body)
        self.assertTrue(body.rstrip().endswith("second try"))
        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_cleared_on_user_removed(self):
        """Silent drops accumulated under the old session must not leak
        into the next session that takes over the same channel — clear
        on ``user_removed`` (the bot being kicked out / leaving)."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "stale",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_cleared_on_leave(self):
        """``@claude leave`` clears any queued drops in that channel."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "d1", "channel_id": "c1", "message": "stale",
            "user_id": "u1", "type": "",
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        await self.bridge._on_mm_posted({
            "id": "d2", "channel_id": "c1", "message": "@claude leave done",
            "user_id": "u1", "type": "",
        })

        self.assertNotIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_drops_thread_cleared_on_thread_leave(self):
        """Leaving a thread clears that thread's queue without touching
        the channel-root queue."""
        self.bridge.mapping.link(Anchor("c1"), "s-root")
        self.bridge.mapping.link(Anchor("c1", "t1"), "s-thread")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "dr", "channel_id": "c1", "message": "root chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "dt", "channel_id": "c1", "message": "thread chatter",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        await self.bridge._on_mm_posted({
            "id": "lv", "channel_id": "c1", "message": "@claude leave",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        self.assertNotIn(("c1", "t1"), self.bridge._silent_drops)
        self.assertIn(("c1", None), self.bridge._silent_drops)

    async def test_mention_only_replay_downloads_queued_attachments(self):
        """File uploads silently dropped before a mention must be
        downloaded on replay so the session sees the file it's being
        asked about, not just the message text."""
        from pathlib import Path

        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.vd.sessions_meta.append(
            {"id": "s1", "projectPath": self.tmp.name, "backend": "claude"},
        )
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )
        self.bridge.mm.files_by_id["fid-silent"] = b"# uploaded before mention\n"

        # Drop: file upload with no @claude mention.
        await self.bridge._on_mm_posted({
            "id": "drop-1", "channel_id": "c1", "message": "",
            "user_id": "u1", "type": "",
            "file_ids": ["fid-silent"],
            "metadata": {"files": [{"id": "fid-silent", "name": "silent.md",
                                     "size": 25}]},
        })
        self.assertIn(("c1", None), self.bridge._silent_drops)

        # Mention triggers replay — attachment should be saved and
        # the catch-up block should reference the saved path.
        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude please look",
            "user_id": "u1", "type": "",
        })

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "silent.md"
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"# uploaded before mention\n")

        self.assertEqual(len(self.bridge.vd.sent), 1)
        body = self.bridge.vd.sent[0][1]
        self.assertIn("Catch-up context", body)
        self.assertIn(f"[User attached file: {saved}]", body)

    async def test_mention_only_drops_keyed_per_thread(self):
        """Silent drops in a thread do not leak into the channel root
        (or vice versa) — each anchor has its own queue."""
        self.bridge.mapping.link(Anchor("c1"), "s-root")
        self.bridge.mapping.link(Anchor("c1", "t1"), "s-thread")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "id": "pr1", "channel_id": "c1", "message": "root chatter",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "pt1", "channel_id": "c1", "message": "thread chatter",
            "user_id": "u1", "type": "", "root_id": "t1",
        })
        await self.bridge._on_mm_posted({
            "id": "pr2", "channel_id": "c1", "message": "@claude root",
            "user_id": "u1", "type": "",
        })
        await self.bridge._on_mm_posted({
            "id": "pt2", "channel_id": "c1", "message": "@claude thread",
            "user_id": "u1", "type": "", "root_id": "t1",
        })

        by_session = {sid: body for sid, body in self.bridge.vd.sent}
        self.assertIn("root chatter", by_session["s-root"])
        self.assertNotIn("thread chatter", by_session["s-root"])
        self.assertIn("thread chatter", by_session["s-thread"])
        self.assertNotIn("root chatter", by_session["s-thread"])


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
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self._set_session_cwd("s1", self.tmp.name)

        post = self._post_with_attachment("c1", "fid-1", "photo.png", b"\x89PNG...")
        await self.bridge._on_mm_posted(post)

        saved = Path(self.tmp.name) / ".mattermost-inbox" / "photo.png"
        self.assertTrue(saved.exists())
        self.assertEqual(len(self.bridge.vd.sent), 1)
        self.assertIn(f"[User attached file: {saved}]", self.bridge.vd.sent[0][1])

    async def test_filename_conflict_gets_suffix(self):
        from pathlib import Path
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s-parent")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
        # All bot-identity posts are filtered from history replay —
        # restart-safe (is_own_post() is empty on a fresh process, so we
        # can't rely on it for catch-up).
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


class InitialCatchUpTests(_BridgeTestCase):
    """On first session creation, prepend the last N channel messages as context."""

    async def test_invite_session_prepends_catch_up_block(self):
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "earlier chat", "type": ""},
            {"id": "p2", "user_id": "u2", "message": "more chat", "type": ""},
        ]

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        self.assertEqual(len(self.bridge.vd.created), 1)
        first_msg = self.bridge.vd.created[0]["message"]
        self.assertIn("Catch-up context", first_msg)
        self.assertIn("earlier chat", first_msg)
        self.assertIn("more chat", first_msg)
        # Original placeholder still appended after the block.
        self.assertIn("I'll wait for the user", first_msg)

    async def test_engagement_excludes_triggering_post(self):
        self.config.auto_join_public_channels = True
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "old message", "type": ""},
            {"id": "trigger", "user_id": "u1", "message": "@claude hi", "type": ""},
        ]

        await self.bridge._on_mm_posted({
            "id": "trigger", "channel_id": "c1", "message": "@claude hi",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        first_msg = self.bridge.vd.created[0]["message"]
        self.assertIn("old message", first_msg)
        self.assertNotIn("@claude hi\n[End of catch-up]", first_msg)
        # The engagement msg itself is still the post-catch-up payload.
        self.assertTrue(first_msg.rstrip().endswith("hi"))

    async def test_zero_disables_auto_catch_up(self):
        self.config.initial_catch_up_n = 0
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = [
            {"id": "p1", "user_id": "u1", "message": "history", "type": ""},
        ]

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        first_msg = self.bridge.vd.created[0]["message"]
        self.assertNotIn("Catch-up context", first_msg)

    async def test_empty_channel_skips_block(self):
        self.config.initial_catch_up_n = 50
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}
        self.bridge.mm.posts_by_channel["c1"] = []

        await self.bridge._on_mm_user_added("c1", self.bridge.mm.bot_user_id)

        first_msg = self.bridge.vd.created[0]["message"]
        self.assertNotIn("Catch-up context", first_msg)


class LeaveTests(_BridgeTestCase):
    async def test_leave_command_removes_bot_and_unlinks(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude leave done",
            "user_id": "u1", "type": "",
        })

        self.assertIn("c1", self.bridge.mm.removed)
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))

    async def test_user_removed_unlinks_mapping(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_user_removed("c1", self.bridge.mm.bot_user_id)

        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))


class StopCommandTests(_BridgeTestCase):
    async def test_stop_command_in_channel_interrupts_session(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, ["s1"])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_stop_command_case_insensitive(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@Claude STOP",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, ["s1"])

    async def test_stop_command_in_thread_interrupts_thread_session(self):
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertEqual(self.bridge.vd.interrupted, ["fork-s"])

    async def test_stop_with_trailing_text_is_regular_message(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude stop doing that",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])
        self.assertEqual(len(self.bridge.vd.sent), 1)

    async def test_stop_in_unmapped_channel_is_ignored(self):
        await self.bridge._on_mm_posted({
            "channel_id": "c-unmapped", "message": "@claude stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])

    async def test_bare_stop_in_autorespond_mode_interrupts(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, ["s1"])

    async def test_bare_stop_in_mention_only_mode_is_not_interrupt(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=True,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.interrupted, [])

    async def test_bare_stop_in_thread_autorespond_interrupts_thread_session(self):
        from mm_bridge.purpose import PurposeConfig
        self.bridge.mapping.link(Anchor("c1"), "parent-s")
        self.bridge.mapping.link(Anchor("c1", "r1"), "fork-s")
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model=None, mention_only=False,
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stop",
            "user_id": "u1", "type": "", "root_id": "r1",
        })

        self.assertEqual(self.bridge.vd.interrupted, ["fork-s"])


class FirstMessageConfigTests(_BridgeTestCase):
    """First user message after invite may re-configure the channel via tokens.

    If it parses cleanly as config, we apply + persist + confirm. If it changes
    backend/model/cwd we restart the session; if only the mention flag changes,
    we update in-place.
    """

    async def _prime_channel(self, channel_id: str, purpose: str = "") -> None:
        """Simulate a successful invite: session mapped, awaiting first message."""
        self.bridge.mm.channels[channel_id] = {"id": channel_id, "purpose": purpose}
        await self.bridge._on_mm_user_added(channel_id, self.bridge.mm.bot_user_id)
        # Pretend the SSE claim succeeded with the session we pretend-created.
        pending = self.bridge.pending_mm_sessions.pop(channel_id, None)
        assert pending is not None
        session_id = "s-primed"
        self.bridge.mapping.link(Anchor(channel_id), session_id)
        self.bridge.awaiting_first_message.add(channel_id)
        return session_id

    async def test_non_config_first_message_forwards_normally(self):
        # Prime with autorespond so the mention-only filter is out of the way —
        # this test is about the first-message-config gate, not the mention rule.
        session_id = await self._prime_channel("c1", purpose="autorespond")
        # Clear bot posts from the welcome message.
        self.bridge.vd.created.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hello world",
            "user_id": "u1", "type": "",
        })

        # Forwarded — message body may be prefixed with a first-message
        # preamble; we only assert the tail is correct here.
        self.assertEqual(len(self.bridge.vd.sent), 1)
        self.assertEqual(self.bridge.vd.sent[0][0], session_id)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("hello world"))
        self.assertNotIn("c1", self.bridge.awaiting_first_message)

    async def test_flag_only_config_updates_in_place_no_session_restart(self):
        session_id = await self._prime_channel("c1")
        self.bridge.vd.created.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond",
            "user_id": "u1", "type": "",
        })

        # Session is NOT restarted.
        self.assertEqual(self.bridge.vd.created, [])
        # Still mapped.
        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), session_id)
        # Config updated: mention_only now False.
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        # Persisted back to Channel Purpose.
        self.assertIn("autorespond", self.bridge.mm.channels["c1"]["purpose"])
        # Confirmation posted, message not forwarded.
        self.assertEqual(self.bridge.vd.sent, [])
        self.assertNotIn("c1", self.bridge.awaiting_first_message)

    async def test_config_with_model_change_restarts_session(self):
        session_id = await self._prime_channel("c1", purpose="claude, opus")
        self.bridge.vd.created.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "claude, sonnet",
            "user_id": "u1", "type": "",
        })

        # Old session unmapped, a new session created.
        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(self.bridge.vd.created[0]["model_index"], 1)  # sonnet=1
        # Original session no longer linked.
        self.assertNotEqual(self.bridge.mapping.get_session(Anchor("c1")), session_id)
        # Persisted.
        self.assertIn("sonnet", self.bridge.mm.channels["c1"]["purpose"])

    async def test_bot_mention_prefix_treats_as_normal_message(self):
        """Commands like `@claude stop` or a plain '@claude hi' must NOT parse
        as config — they contain the bot mention which isn't a known token."""
        session_id = await self._prime_channel("c1")
        self.bridge.vd.created.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude hi",
            "user_id": "u1", "type": "",
        })

        # Forwarded (with mention stripped). A first-message preamble may
        # prefix the body; assert only that the tail is "hi".
        self.assertEqual(len(self.bridge.vd.sent), 1)
        self.assertEqual(self.bridge.vd.sent[0][0], session_id)
        self.assertTrue(self.bridge.vd.sent[0][1].endswith("hi"))

    async def test_only_first_message_is_checked_for_config(self):
        """Once the channel is no longer awaiting, tokens-that-look-like-config
        should be forwarded verbatim — not swallowed."""
        session_id = await self._prime_channel("c1", purpose="autorespond")
        self.bridge.vd.created.clear()

        # First message: normal → removes awaiting flag.
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hello", "user_id": "u1", "type": "",
        })
        # Second message: looks like config but must forward.
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "claude, sonnet",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        self.assertEqual(self.bridge.vd.sent[1][1], "claude, sonnet")


class RuntimeToggleTests(_BridgeTestCase):
    """After the first message, only the literal words `autorespond` and
    `noautorespond` toggle the mention_only flag — nothing else."""

    async def test_literal_noautorespond_toggles_mention_only_on(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=False,
        )
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "claude, opus"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "noautorespond",
            "user_id": "u1", "type": "",
        })

        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)
        # Persisted + not forwarded.
        self.assertIn("mention-only", self.bridge.mm.channels["c1"]["purpose"])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_literal_autorespond_toggles_mention_only_off(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        from mm_bridge.purpose import PurposeConfig
        self.bridge.purpose_by_channel["c1"] = PurposeConfig(
            backend="claude", model="opus", mention_only=True,
        )
        self.bridge.mm.channels["c1"] = {
            "id": "c1", "purpose": "claude, opus, mention-only",
        }

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond",
            "user_id": "u1", "type": "",
        })

        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        self.assertNotIn("mention-only", self.bridge.mm.channels["c1"]["purpose"])
        self.assertEqual(self.bridge.vd.sent, [])

    async def test_autorespond_with_trailing_text_is_regular_message(self):
        """`autorespond now` must NOT toggle — only the literal word alone does."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond now",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "autorespond now")])


class PurposeUpdateNoticeTests(_BridgeTestCase):
    async def test_self_triggered_purpose_change_suppresses_notice(self):
        """When the bridge writes the purpose (e.g. after a runtime toggle),
        the incoming `channel_updated` event must not spawn a user notice."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }
        self.bridge._note_self_wrote_purpose("c1", "claude, opus, mention-only")

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "", "purpose": "claude, opus, mention-only",
        })

        self.assertFalse(
            any("takes effect only for new sessions" in p.message
                for p in self.bridge.mm.posted),
            "self-written purpose must not trigger the change notice",
        )

    async def test_external_purpose_change_still_posts_notice(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "", "purpose": "claude, opus",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "", "purpose": "claude, opus, mention-only",
        })

        self.assertTrue(
            any("takes effect only for new sessions" in p.message
                for p in self.bridge.mm.posted),
        )


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

        self.assertEqual(self.bridge.mapping.get_session(Anchor("c1")), "sess-new")
        self.assertNotIn("c1", self.bridge.pending_mm_sessions)

    async def test_session_added_without_pending_creates_channel(self):
        await self.bridge._on_vd_event("session_added", {
            "id": "sess-cli", "projectPath": "/tmp/proj",
            "projectName": "my-project", "firstMessage": "hi from CLI",
        })

        self.assertTrue(self.bridge.mapping.get_anchor("sess-cli"))
        self.assertTrue(self.bridge.mm.channels)


class ThreadForkTests(_BridgeTestCase):
    async def test_thread_post_in_mapped_channel_calls_fork(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
        self.bridge.mapping.link(Anchor("c1"), "s1")
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
            self.bridge.mapping.get_session(Anchor("c1", "r1")), "sess-fork",
        )
        disclaimer = [p for p in self.bridge.mm.posted if "Forked conversation" in p.message]
        self.assertEqual(len(disclaimer), 1)
        self.assertEqual(disclaimer[0].root_id, "r1")


class AssistantMessageTests(_BridgeTestCase):
    async def test_plain_text_posts_to_channel(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [{"type": "text", "text": "hello there"}],
            },
        })

        self.assertTrue(any(p.message == "hello there" for p in self.bridge.mm.posted))

    async def test_leave_channel_directive_removes_bot(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

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
        self.assertIsNone(self.bridge.mapping.get_session(Anchor("c1")))


class DirectUserMessageMirrorTests(_BridgeTestCase):
    """User turns that arrive via the coding agent's UI/CLI (role=user from
    SSE) are mirrored back into the bound MM channel so MM watchers see the
    full conversation. Bridge-originated sends, tool results, and synthetic
    fork preambles are all suppressed.
    """

    USER_PREFIX = "_via coding agent:_"

    async def _direct_user_text(self, session_id: str, text: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "user",
                "blocks": [{"type": "text", "text": text}],
            },
        })

    async def test_direct_user_text_block_is_mirrored(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._direct_user_text("s1", "what's the build status?")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertIn("what's the build status?", mirrored[0].message)
        self.assertEqual(mirrored[0].channel_id, "c1")
        self.assertIsNone(mirrored[0].root_id)

    async def test_tool_result_role_user_is_not_mirrored(self):
        """Claude/Codex represent tool results as role=user with a
        tool_result block. These must not leak into MM."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "user",
                "blocks": [{
                    "type": "tool_result",
                    "content": "drwxr-xr-x  4 user  staff  128 ...",
                    "is_error": False,
                }],
            },
        })

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_recent_mm_forwarded_post_does_not_echo_back(self):
        """When MM forwards a user post to VD via send_message, VD's
        transcript echoes it back as role=user. The bridge must dedup
        against its recent-send window so the channel doesn't double up.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": None, "message": "hello agent",
            "user_id": "u1", "type": "", "id": "p-mm-1",
        })

        self.assertEqual(self.bridge.vd.sent, [("s1", "hello agent")])

        # VD echoes the same body back as role=user. Must not produce
        # an extra MM post.
        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s1", "hello agent")
        after = len(self.bridge.mm.posted)
        self.assertEqual(
            after, before,
            "MM-originated message must not be re-mirrored as a direct turn",
        )

    async def test_first_message_after_invite_claim_is_swallowed(self):
        """The first user-role event after an invite-claim is the
        firstMessage we shipped via create_session — not direct input."""
        from mm_bridge.bridge import PendingMattermostSession
        import time as _time

        self.bridge.pending_mm_sessions["c1"] = PendingMattermostSession(
            channel_id="c1", cwd="/tmp/proj", backend="claude",
            initial_message="kick-off prompt", requested_at=_time.monotonic(),
        )
        await self.bridge._on_vd_event("session_added", {
            "id": "s-inv", "projectPath": "/tmp/proj", "backend": "claude",
            "firstMessage": "kick-off prompt",
        })

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-inv", "kick-off prompt")
        self.assertEqual(len(self.bridge.mm.posted), before)

    async def test_fork_continuation_preamble_is_swallowed(self):
        """Claude Code forks emit a synthetic continuation summary as
        firstMessage. It is NOT user-typed input — must be suppressed."""
        from mm_bridge.bridge import PendingMattermostSession
        import time as _time

        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.pending_forks.append(PendingMattermostSession(
            channel_id="c1", cwd="/tmp/proj", backend="claude",
            initial_message="thread starter", requested_at=_time.monotonic(),
            is_fork=True, fork_parent_session="s1",
            fork_thread_channel="c1", fork_thread_root="r1",
        ))
        synth = "This session is being continued from a previous conversation..."
        await self.bridge._on_vd_event("session_added", {
            "id": "s-fork", "projectPath": "/tmp/proj", "backend": "claude",
            "firstMessage": synth,
        })

        before = len(self.bridge.mm.posted)
        await self._direct_user_text("s-fork", synth)
        self.assertEqual(len(self.bridge.mm.posted), before)

    async def test_autonomous_session_first_message_is_mirrored(self):
        """A session VD created on its own (no MM origin) gets a fresh
        channel and the firstMessage IS direct input — must surface in MM.
        """
        await self.bridge._on_vd_event("session_added", {
            "id": "s-cli", "projectPath": "/tmp/proj", "backend": "claude",
            "firstMessage": "started a session locally",
            "summaryTitle": "Local",
        })

        anchor = self.bridge.mapping.get_anchor("s-cli")
        self.assertIsNotNone(anchor)

        await self._direct_user_text("s-cli", "started a session locally")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertIn("started a session locally", mirrored[0].message)

    async def test_thread_anchor_routes_to_thread_root(self):
        self.bridge.mapping.link(Anchor("c1", "r1"), "s-thr")

        await self._direct_user_text("s-thr", "thread-only thought")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)
        self.assertEqual(mirrored[0].channel_id, "c1")
        self.assertEqual(mirrored[0].root_id, "r1")

    async def test_disabled_via_config(self):
        self.bridge.config.mirror_direct_user_messages = False
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._direct_user_text("s1", "should not show")

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_unbound_session_silently_dropped(self):
        await self._direct_user_text("s-orphan", "no anchor here")

        self.assertEqual(self.bridge.mm.posted, [])

    async def test_dedup_window_expires(self):
        """An echo arriving long after the send window must be mirrored.
        We model this by directly draining the recent-send entry, then
        firing the same body as a direct turn."""
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "root_id": None, "message": "stale body",
            "user_id": "u1", "type": "", "id": "p-mm-1",
        })
        self.bridge.config.direct_user_message_dedup_window_seconds = 0.0
        # After zeroing the window, the recent-send entry is past — a
        # direct echo of the same body must now mirror.
        await self._direct_user_text("s1", "stale body")

        mirrored = [p for p in self.bridge.mm.posted if self.USER_PREFIX in p.message]
        self.assertEqual(len(mirrored), 1)


class ToolUseCoalescingTests(_BridgeTestCase):
    """Verify tool-use blocks coalesce into a single per-turn placeholder
    post that gets hard-deleted when the turn ends.
    """

    async def _tool_use(self, session_id: str, tool: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "blocks": [{"type": "tool_use", "tool_name": tool}],
            },
        })

    async def _text(self, session_id: str, text: str) -> None:
        await self.bridge._on_vd_event("message", {
            "session_id": session_id,
            "message": {
                "role": "assistant",
                "blocks": [{"type": "text", "text": text}],
            },
        })

    async def test_single_tool_use_creates_placeholder(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1)
        self.assertEqual(tool_posts[0].message, "_Using tool: Bash_")

    async def test_repeat_tool_bumps_counter_via_edit(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        for _ in range(3):
            await self._tool_use("s1", "Bash")

        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1, "only one placeholder post should be created")
        self.assertEqual(self.bridge.mm.edits[-1][1], "_Using tool: Bash (x3)_")

    async def test_different_tool_adds_new_line(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Read")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")

        final = self.bridge.mm.edits[-1][1]
        self.assertEqual(
            final,
            "_Using tool: Bash (x2)_\n_Using tool: Read_\n_Using tool: Bash (x2)_",
        )
        # Still one underlying post.
        tool_posts = [p for p in self.bridge.mm.posted if "Using tool" in p.message]
        self.assertEqual(len(tool_posts), 1)

    async def test_real_text_ends_run_without_deleting(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Bash")

        await self._text("s1", "here is the answer")

        # Placeholder is NOT deleted — it stays in the channel as a
        # compact record of the tools used this turn.
        self.assertEqual(self.bridge.mm.deletes, [])
        # But state is dropped so the next turn starts fresh.
        self.assertNotIn("s1", self.bridge.tool_use_runs)
        self.assertTrue(
            any(p.message == "here is the answer" for p in self.bridge.mm.posted),
        )

    async def test_next_turn_creates_fresh_placeholder(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")
        first_placeholder_id = self.bridge.tool_use_runs["s1"].post_id
        await self._text("s1", "ok done")
        await self._tool_use("s1", "Read")

        # Different post id — a new placeholder, not an edit of the old.
        self.assertNotEqual(
            self.bridge.tool_use_runs["s1"].post_id, first_placeholder_id,
        )

    async def test_mixed_block_event_text_then_tool(self):
        """A single event with [text, tool_use] posts the text, then starts
        a fresh tool-use run for the trailing tool.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "text", "text": "planning next step"},
                    {"type": "tool_use", "tool_name": "Bash"},
                ],
            },
        })

        self.assertTrue(any(p.message == "planning next step" for p in self.bridge.mm.posted))
        self.assertIn("s1", self.bridge.tool_use_runs)
        self.assertEqual(
            self.bridge.tool_use_runs["s1"].lines, [["Bash", 1]],
        )

    async def test_tool_error_ends_run_without_deleting(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        await self.bridge._on_vd_event("message", {
            "session_id": "s1",
            "message": {
                "role": "assistant",
                "blocks": [
                    {"type": "tool_result", "is_error": True, "content": "boom"},
                ],
            },
        })

        self.assertEqual(self.bridge.mm.deletes, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)
        self.assertTrue(any("boom" in p.message for p in self.bridge.mm.posted))

    async def test_session_status_stopped_ends_run(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        await self._tool_use("s1", "Bash")

        await self.bridge._on_vd_event("session_status", {
            "session_id": "s1", "running": False,
        })

        self.assertEqual(self.bridge.mm.deletes, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)

    async def test_placeholder_is_never_deleted(self):
        """Safety net: regardless of turn end-reason, the placeholder post
        is preserved in the channel as a permanent record.
        """
        self.bridge.mapping.link(Anchor("c1"), "s1")
        for _ in range(3):
            await self._tool_use("s1", "Bash")
        await self._text("s1", "done")

        self.assertEqual(self.bridge.mm.deletes, [])

    async def test_show_tool_use_false_suppresses_placeholder(self):
        """When `show_tool_use=False`, tool_use blocks are silently
        dropped — no post, no edit, no run state. Real text still posts.
        """
        self.bridge.config.show_tool_use = False
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self._tool_use("s1", "Bash")
        await self._tool_use("s1", "Read")

        self.assertFalse(
            any("Using tool" in p.message for p in self.bridge.mm.posted),
        )
        self.assertEqual(self.bridge.mm.edits, [])
        self.assertNotIn("s1", self.bridge.tool_use_runs)

        await self._text("s1", "here's the answer")
        self.assertTrue(
            any(p.message == "here's the answer" for p in self.bridge.mm.posted),
        )

    async def test_sessions_isolated(self):
        """Two live sessions must not share placeholder state."""
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.mapping.link(Anchor("c2"), "s2")

        await self._tool_use("s1", "Bash")
        await self._tool_use("s2", "Read")
        await self._tool_use("s1", "Bash")

        self.assertEqual(self.bridge.tool_use_runs["s1"].lines, [["Bash", 2]])
        self.assertEqual(self.bridge.tool_use_runs["s2"].lines, [["Read", 1]])


class NameSyncTests(_BridgeTestCase):
    async def test_channel_renamed_syncs_title_to_vibedeck(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "old", "purpose": "",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "new-name", "purpose": "",
        })

        self.assertEqual(self.bridge.vd.titles, [("s1", "new-name")])

    async def test_summary_updated_renames_channel(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_event("session_summary_updated", {
            "session_id": "s1", "summaryTitle": "Great Session",
        })

        self.assertEqual(self.bridge.mm.renames, [("c1", "Great Session")])

    async def test_name_sync_prevents_ping_pong(self):
        self.bridge.mapping.link(Anchor("c1"), "s1")
        self.bridge.name_sync.note_remote_update("mm", "c1")
        self.bridge.last_channel_state["c1"] = {
            "display_name": "old", "purpose": "",
        }

        await self.bridge._on_mm_channel_updated({
            "id": "c1", "display_name": "new-name", "purpose": "",
        })

        self.assertEqual(self.bridge.vd.titles, [])


class AutoJoinTests(_BridgeTestCase):
    """`auto_join_public_channels` opts the bot into team-wide presence.

    Joining is silent — no VD session until a user engages (by @mention
    under mention-only, or any message under autorespond). Self-joins must
    not be treated as external invites.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config.auto_join_public_channels = True

    async def test_channel_created_event_joins_when_enabled(self):
        await self.bridge._on_mm_channel_created("c-new")
        self.assertIn("c-new", self.bridge.mm.joined)
        # Session-less silent presence; the resulting user_added must not
        # spawn an invite flow.
        await self.bridge._on_mm_user_added("c-new", self.bridge.mm.bot_user_id)
        self.assertIsNone(self.bridge.mapping.get_session("c-new"))
        self.assertEqual(self.bridge.vd.created, [])

    async def test_channel_created_event_ignored_when_disabled(self):
        self.config.auto_join_public_channels = False
        await self.bridge._on_mm_channel_created("c-new")
        self.assertEqual(self.bridge.mm.joined, [])

    async def test_mention_triggers_engagement_session(self):
        # Channel exists, bot joined silently; no mapping yet.
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude hello there",
            "user_id": "u1", "type": "",
        })

        # VD session created with the engagement message (mention stripped).
        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(self.bridge.vd.created[0]["message"], "hello there")
        # Pending registered, not yet mapped (awaits SSE claim).
        self.assertIn("c1", self.bridge.pending_mm_sessions)
        pending = self.bridge.pending_mm_sessions["c1"]
        self.assertFalse(
            pending.allow_first_message_config,
            "engagement sessions must not re-enter the first-message config gate",
        )
        # No welcome message for engagement — the response is the welcome.
        self.assertEqual(self.bridge.mm.posted, [])

    async def test_non_mention_ignored_under_mention_only_default(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "just chatting with my team",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.created, [])
        self.assertNotIn("c1", self.bridge.pending_mm_sessions)

    async def test_autorespond_purpose_engages_on_any_message(self):
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "no mention at all",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(
            self.bridge.vd.created[0]["message"], "no mention at all",
        )

    async def test_mention_with_pure_config_applies_without_session(self):
        """`@claude autorespond` on an auto-joined channel should configure, not engage."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude autorespond",
            "user_id": "u1", "type": "",
        })

        # No VD session created — message was pure config.
        self.assertEqual(self.bridge.vd.created, [])
        self.assertNotIn("c1", self.bridge.pending_mm_sessions)
        # Purpose cached + persisted.
        self.assertIn("c1", self.bridge.purpose_by_channel)
        self.assertFalse(self.bridge.purpose_by_channel["c1"].mention_only)
        # Confirmation posted.
        self.assertEqual(len(self.bridge.mm.posted), 1)
        self.assertIn("Config applied", self.bridge.mm.posted[0].message)

    async def test_mention_with_config_alias_spelling(self):
        """Accept `autoresponse` / `noautoresponse` spelling variants."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude noautoresponse",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.created, [])
        self.assertIn("c1", self.bridge.purpose_by_channel)
        self.assertTrue(self.bridge.purpose_by_channel["c1"].mention_only)

    async def test_mention_with_chat_still_engages(self):
        """Chat text with unknown tokens should still start a session."""
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": ""}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "@claude hello there",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.created), 1)
        self.assertEqual(self.bridge.vd.created[0]["message"], "hello there")

    async def test_engagement_disabled_when_auto_join_disabled(self):
        self.config.auto_join_public_channels = False
        self.bridge.mm.channels["c1"] = {"id": "c1", "purpose": "autorespond"}

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "no mention at all",
            "user_id": "u1", "type": "",
        })

        self.assertEqual(self.bridge.vd.created, [])


class FirstMessagePreambleTests(_BridgeTestCase):
    """When a user posts the first message after invite, prepend a short
    MM-context preamble to the forwarded VD message."""

    async def _prime_channel_with_members(
        self,
        channel_id: str,
        *,
        display_name: str,
        members: list[dict],
    ) -> str:
        """Set up a mapped channel with `awaiting_first_message` set and the
        given member roster. Returns the session_id."""
        self.bridge.mm.channels[channel_id] = {
            "id": channel_id, "purpose": "", "display_name": display_name,
        }
        for m in members:
            uid = m["user_id"]
            self.bridge.mm.users[uid] = {
                "id": uid,
                "username": m.get("username") or f"u-{uid[:4]}",
                "is_bot": bool(m.get("is_bot")),
            }
        self.bridge.mm.channel_members[channel_id] = [
            {"user_id": m["user_id"]} for m in members
        ]
        session_id = "s-primed"
        self.bridge.mapping.link(Anchor(channel_id), session_id)
        self.bridge.awaiting_first_message.add(channel_id)
        return session_id

    async def test_single_user_preamble_prepended(self) -> None:
        session_id = await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "hi there",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 1)
        sent = self.bridge.vd.sent[0][1]
        self.assertTrue(
            sent.startswith(
                '[Running inside Mattermost channel "Bug Bash". '
                "You're talking to @alice. "
                "@-mention to keep their attention.]"
            ),
            f"preamble missing or malformed; got: {sent!r}",
        )
        self.assertIn("hi there", sent)

    async def test_multi_user_preamble_lists_humans_only(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="team-room",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
                {"user_id": "u-bob", "username": "bob"},
                {"user_id": "u-gpt", "username": "gpt", "is_bot": True},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "ping",
            "user_id": "u-alice", "type": "",
        })

        sent = self.bridge.vd.sent[0][1]
        self.assertIn(
            "Multiple users in this channel (`alice`, `bob`) "
            "— messages are prefixed with `username:`",
            sent,
        )
        self.assertNotIn("gpt", sent)  # bot excluded

    async def test_zero_humans_omits_user_sentence(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="bots-only",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
            ],
        )
        # A human posts even though they're not (yet) in the member list.
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "stranger here",
            "user_id": "u-stranger", "type": "",
        })

        sent = self.bridge.vd.sent[0][1]
        self.assertTrue(sent.startswith(
            '[Running inside Mattermost channel "bots-only". '
            "@-mention to keep their attention.]"
        ))

    async def test_preamble_not_added_on_second_message(self) -> None:
        await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[
                {"user_id": self.bridge.mm.bot_user_id, "is_bot": True},
                {"user_id": "u-alice", "username": "alice"},
            ],
        )

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "first",
            "user_id": "u-alice", "type": "",
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "second",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(len(self.bridge.vd.sent), 2)
        self.assertIn("Running inside Mattermost channel", self.bridge.vd.sent[0][1])
        self.assertNotIn("Running inside Mattermost channel", self.bridge.vd.sent[1][1])

    async def test_config_token_first_message_does_not_forward_preamble(self) -> None:
        """First-message purpose tokens are consumed as config — nothing goes
        to VD, so there's no preamble either."""
        await self._prime_channel_with_members(
            "c1",
            display_name="Bug Bash",
            members=[{"user_id": "u-alice", "username": "alice"}],
        )
        # Clear sent from any prior wiring.
        self.bridge.vd.sent.clear()

        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "autorespond",
            "user_id": "u-alice", "type": "",
        })

        self.assertEqual(self.bridge.vd.sent, [])

    async def test_preamble_format_helper_single_user(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", ["alice"])
        self.assertEqual(
            out,
            '[Running inside Mattermost channel "My Chan". '
            "You're talking to @alice. "
            "@-mention to keep their attention.]",
        )

    async def test_preamble_format_helper_multi_user(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", ["alice", "bob", "carol"])
        self.assertIn("Multiple users in this channel (`alice`, `bob`, `carol`)", out)
        self.assertIn("messages are prefixed with `username:`", out)

    async def test_preamble_format_helper_empty(self) -> None:
        from mm_bridge.bridge import _format_first_message_preamble
        out = _format_first_message_preamble("My Chan", [])
        self.assertEqual(
            out,
            '[Running inside Mattermost channel "My Chan". '
            "@-mention to keep their attention.]",
        )


class MentionUserWhenDoneTests(_BridgeTestCase):
    """`mention_user_when_done` — post `@<username>` in the session's anchor
    when the VD run ends, targeted at the user whose MM post triggered it."""

    async def _trigger_run(self, channel_id: str, user_id: str, session_id: str) -> None:
        self.bridge.mapping.link(Anchor(channel_id), session_id)
        await self.bridge._on_mm_posted({
            "channel_id": channel_id, "message": "do a thing",
            "user_id": user_id, "type": "",
        })

    async def test_posts_mention_to_channel_on_run_end(self) -> None:
        await self._trigger_run("c1", "u1", "s1")

        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].channel_id, "c1")
        self.assertEqual(mentions[0].message, "@u-u1")
        self.assertIsNone(mentions[0].root_id)

    async def test_posts_mention_into_thread_when_anchor_is_thread(self) -> None:
        self.bridge.mapping.link(Anchor("c1", "root-post"), "s-thread")
        # Feed a forwarded user post directly via the low-level path, since
        # `_on_mm_posted` has its own thread dispatch logic.
        await self.bridge._forward_user_post(
            "c1", "s-thread",
            {"user_id": "u2", "message": "hi", "type": ""},
            "hi", "root-post", first_message=False,
        )

        await self.bridge._on_vd_session_status({
            "session_id": "s-thread", "running": False,
        })

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].root_id, "root-post")
        self.assertEqual(mentions[0].message, "@u-u2")

    async def test_no_mention_when_no_triggerer_tracked(self) -> None:
        # Session exists but no user post was forwarded (e.g. autorespond loop).
        self.bridge.mapping.link(Anchor("c1"), "s1")

        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })

        self.assertFalse(any(p.message.startswith("@") for p in self.bridge.mm.posted))

    async def test_triggerer_consumed_on_use(self) -> None:
        await self._trigger_run("c1", "u1", "s1")

        # First completion event pings.
        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })
        # A second running=false (e.g. spurious duplicate) must NOT re-ping.
        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })

        mentions = [p for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(len(mentions), 1)

    async def test_disabled_by_config(self) -> None:
        self.bridge.config.mention_user_when_done = False
        await self._trigger_run("c1", "u1", "s1")

        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })

        self.assertFalse(any(p.message.startswith("@") for p in self.bridge.mm.posted))

    async def test_second_run_pings_most_recent_triggerer(self) -> None:
        # Alice triggers → run ends → ping @alice. Then Bob triggers → run
        # ends → ping @bob, not @alice.
        await self._trigger_run("c1", "u-alice", "s1")
        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })
        await self.bridge._on_mm_posted({
            "channel_id": "c1", "message": "my turn",
            "user_id": "u-bob", "type": "",
        })
        await self.bridge._on_vd_session_status({
            "session_id": "s1", "running": False,
        })

        mentions = [p.message for p in self.bridge.mm.posted if p.message.startswith("@")]
        self.assertEqual(mentions, ["@u-u-al", "@u-u-bo"])


if __name__ == "__main__":
    unittest.main()
