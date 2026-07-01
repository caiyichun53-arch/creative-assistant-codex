from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.hermes.goal11_host_binding import (
    FeishuBindingEvent,
    FeishuThinBinding,
    HermesCoreBridge,
    HermesHostBindingError,
)
from scripts.core.persistence.goal01_store import IdempotencyConflict, UUIDv7Generator
from scripts.core.state.goal02_core import CoreMaterializer


class FakeClock:
    def __init__(self, start_ms: int):
        self.current_ms = start_ms

    def now_ms(self) -> int:
        return self.current_ms


class DeterministicBits:
    def __init__(self):
        self.value = 0

    def __call__(self, bit_count: int) -> int:
        self.value += 1
        return self.value & ((1 << bit_count) - 1)


def make_core() -> CoreMaterializer:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    core = CoreMaterializer.in_memory(id_factory=generator.new)
    core.grant_permission("hermes", "create_state")
    core.grant_permission("hermes", "transition_state")
    return core


def make_create_topic_event(event_id: str = "evt-topic-create") -> FeishuBindingEvent:
    return FeishuBindingEvent(
        event_id=event_id,
        chat_id="chat-fixture",
        sender_id="user-fixture",
        command={
            "command_type": "create_state",
            "object_kind": "topic",
            "payload": {},
        },
    )


def test_feishu_binding_only_maps_to_hermes_message() -> None:
    binding = FeishuThinBinding()
    message = binding.to_hermes_message(make_create_topic_event())

    assert message.host == "hermes"
    assert message.source == "feishu"
    assert message.reply_channel_id == "chat-fixture"
    assert message.actor == "user-fixture"
    assert message.command_type == "create_state"
    assert message.object_kind == "topic"
    assert not hasattr(binding, "core")
    assert not hasattr(binding, "store")
    assert not hasattr(binding, "scheduler")
    print("PASS Feishu binding maps only to Hermes message")


def test_hermes_dispatches_formal_state_through_core() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())

    result = bridge.dispatch(message)

    assert result.status == "succeeded"
    assert result.object_id is not None
    assert result.state == "candidate"
    assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM command_receipt WHERE command_scope='goal02.core'").fetchone()[0] == 1
    command = core.conn.execute("SELECT actor, command_type FROM core_command_envelope").fetchone()
    assert command["actor"] == "hermes"
    assert command["command_type"] == "create_state"
    print("PASS Hermes dispatches formal state through Core")


def test_duplicate_feishu_event_replays_without_duplicate_state() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())

    first = bridge.dispatch(message)
    replay = bridge.dispatch(message)

    assert first.receipt_id == replay.receipt_id
    assert replay.replayed
    assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM command_receipt WHERE command_scope='goal02.core'").fetchone()[0] == 1
    print("PASS duplicate Feishu event replays without duplicate state")


def test_changed_payload_with_same_event_key_is_rejected() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    binding = FeishuThinBinding()
    bridge.dispatch(binding.to_hermes_message(make_create_topic_event("evt-conflict")))

    changed = FeishuBindingEvent(
        event_id="evt-conflict",
        chat_id="chat-fixture",
        sender_id="user-fixture",
        command={
            "command_type": "create_state",
            "object_kind": "topic",
            "payload": {"state": "selected"},
        },
    )
    try:
        bridge.dispatch(binding.to_hermes_message(changed))
    except IdempotencyConflict:
        assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 1
        print("PASS changed payload with same event key rejected")
        return
    raise AssertionError("expected idempotency conflict")


def test_non_hermes_host_rejected_before_core() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())
    bad_message = type(message)(
        host="codex",
        source=message.source,
        source_event_id=message.source_event_id,
        reply_channel_id=message.reply_channel_id,
        actor=message.actor,
        command_type=message.command_type,
        object_kind=message.object_kind,
        payload=message.payload,
    )

    try:
        bridge.dispatch(bad_message)
    except HermesHostBindingError:
        assert core.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0] == 0
        assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 0
        print("PASS non-Hermes host rejected before Core")
        return
    raise AssertionError("expected non-Hermes host rejection")


def main() -> int:
    test_feishu_binding_only_maps_to_hermes_message()
    test_hermes_dispatches_formal_state_through_core()
    test_duplicate_feishu_event_replays_without_duplicate_state()
    test_changed_payload_with_same_event_key_is_rejected()
    test_non_hermes_host_rejected_before_core()
    print("GOAL-11 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
