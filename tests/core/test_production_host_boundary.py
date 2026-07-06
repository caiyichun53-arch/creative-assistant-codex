from __future__ import annotations

import unittest

from scripts.core.host.production_host import (
    ClaudeBindingEvent,
    ClaudeHostBinding,
    CodexBindingEvent,
    CodexHostBinding,
    FeishuBindingEvent,
    FeishuThinBinding,
    HostBindingError,
    PRODUCTION_HOST_ACTOR,
    ProductionHostBridge,
    ProductionHostInboundMessage,
)
from scripts.core.persistence.goal01_store import UUIDv7Generator
from scripts.core.state.goal02_core import CoreMaterializer


def make_core() -> CoreMaterializer:
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    core = CoreMaterializer.in_memory(id_factory=generator.new)
    core.grant_permission(PRODUCTION_HOST_ACTOR, "create_state")
    core.grant_permission(PRODUCTION_HOST_ACTOR, "transition_state")
    return core


class ProductionHostBoundaryTests(unittest.TestCase):
    def test_codex_host_uses_same_core_boundary_as_hermes(self) -> None:
        core = make_core()
        self.addCleanup(core.store.conn.close)
        bridge = ProductionHostBridge(core)

        result = bridge.dispatch(
            CodexHostBinding().to_host_message(
                CodexBindingEvent(
                    event_id="codex-create-topic",
                    thread_id="codex-thread",
                    actor_id="codex-user",
                    command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        command = core.conn.execute("SELECT actor, command_type FROM core_command_envelope").fetchone()
        self.assertEqual(command["actor"], PRODUCTION_HOST_ACTOR)
        self.assertEqual(command["command_type"], "create_state")

    def test_claude_host_uses_same_core_boundary_as_hermes(self) -> None:
        core = make_core()
        self.addCleanup(core.store.conn.close)
        bridge = ProductionHostBridge(core)

        result = bridge.dispatch(
            ClaudeHostBinding().to_host_message(
                ClaudeBindingEvent(
                    event_id="claude-create-topic",
                    thread_id="claude-thread",
                    actor_id="claude-user",
                    command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
                )
            )
        )

        self.assertEqual(result.status, "succeeded")
        command = core.conn.execute("SELECT actor, command_type FROM core_command_envelope").fetchone()
        self.assertEqual(command["actor"], PRODUCTION_HOST_ACTOR)
        self.assertEqual(command["command_type"], "create_state")

    def test_feishu_binding_can_target_codex_without_core_authority(self) -> None:
        binding = FeishuThinBinding()
        message = binding.to_host_message(
            FeishuBindingEvent(
                event_id="feishu-to-codex",
                chat_id="chat-id",
                sender_id="feishu-user",
                command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
            ),
            host="codex",
        )

        self.assertEqual(message.host, "codex")
        self.assertEqual(message.source, "feishu")
        self.assertFalse(hasattr(binding, "core"))
        self.assertFalse(hasattr(binding, "store"))
        self.assertFalse(hasattr(binding, "scheduler"))

    def test_unregistered_host_is_rejected_before_core(self) -> None:
        core = make_core()
        self.addCleanup(core.store.conn.close)
        bridge = ProductionHostBridge(core)
        message = ProductionHostInboundMessage(
            host="random-platform",
            source="web",
            source_event_id="random-create-topic",
            reply_channel_id="reply",
            actor="external-user",
            command_type="create_state",
            object_kind="topic",
            payload={},
        )

        with self.assertRaises(HostBindingError):
            bridge.dispatch(message)
        self.assertEqual(core.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0], 0)
        self.assertEqual(core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
