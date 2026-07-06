from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.hermes.goal11_host_binding import (
    GOAL11_HOST_MESSAGE_SCOPE,
    GOAL11_RESPONSE_SEND_SCOPE,
    GOAL11_RESPONSE_TOPIC,
    CodexBindingEvent,
    CodexHostBinding,
    FeishuBindingEvent,
    FeishuResponseDispatcher,
    FeishuThinBinding,
    HermesCoreBridge,
    HermesHostBindingError,
)
from scripts.core.host.production_host import PRODUCTION_HOST_ACTOR
from scripts.core.persistence.goal01_store import IdempotencyConflict, UUIDv7Generator
from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.state.goal02_core import CoreMaterializer


class FakeClock:
    def __init__(self, start_ms: int):
        self.current_ms = start_ms

    def now_ms(self) -> int:
        return self.current_ms

    def advance_ms(self, delta_ms: int) -> None:
        self.current_ms += delta_ms


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
    core.grant_permission(PRODUCTION_HOST_ACTOR, "create_state")
    core.grant_permission(PRODUCTION_HOST_ACTOR, "transition_state")
    return core


def make_scheduler(core: CoreMaterializer, clock: FakeClock | None = None) -> Goal03Scheduler:
    active_clock = clock or FakeClock(1_725_100_000_000)
    return Goal03Scheduler(core.store, id_factory=core.id_factory, now_ms=active_clock.now_ms)


def make_runtime(scheduler: Goal03Scheduler, dispatcher: FeishuResponseDispatcher) -> RuntimeHost:
    runtime = RuntimeHost(scheduler, worker_id="goal11-worker")
    runtime.register_handler(
        "outbox.dispatch",
        dispatcher.handler(),
        contract=RuntimeHandlerContract(
            job_kind="outbox.dispatch",
            required_payload_keys=("outbox_id", "topic", "payload"),
            required_result_keys=("outbox_id", "reply_channel_id", "receipt_id"),
        ),
    )
    return runtime


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


def test_feishu_binding_only_maps_to_host_message() -> None:
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
    print("PASS Feishu binding maps only to production Host message")


def test_hermes_dispatches_formal_state_through_core() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())

    result = bridge.dispatch(message)

    assert result.status == "succeeded"
    assert result.receipt_id != result.core_receipt_id
    assert result.host_message_version_id is not None
    assert result.response_outbox_id is not None
    assert result.object_id is not None
    assert result.state == "candidate"
    assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM command_receipt WHERE command_scope='goal02.core'").fetchone()[0] == 1
    assert core.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope=?",
        (GOAL11_HOST_MESSAGE_SCOPE,),
    ).fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM trace_root WHERE object_kind='goal11_host_message'").fetchone()[0] == 1
    outbox = core.conn.execute("SELECT topic, status FROM outbox_message WHERE outbox_id=?", (result.response_outbox_id,)).fetchone()
    assert outbox["topic"] == GOAL11_RESPONSE_TOPIC
    assert outbox["status"] == "pending"
    command = core.conn.execute("SELECT actor, command_type FROM core_command_envelope").fetchone()
    assert command["actor"] == PRODUCTION_HOST_ACTOR
    assert command["command_type"] == "create_state"
    print("PASS production Host records receipt and dispatches formal state through Core")


def test_duplicate_feishu_event_replays_without_duplicate_state() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())

    first = bridge.dispatch(message)
    replay = bridge.dispatch(message)

    assert first.receipt_id == replay.receipt_id
    assert first.core_receipt_id == replay.core_receipt_id
    assert first.response_outbox_id == replay.response_outbox_id
    assert replay.replayed
    assert core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM command_receipt WHERE command_scope='goal02.core'").fetchone()[0] == 1
    assert core.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope=?",
        (GOAL11_HOST_MESSAGE_SCOPE,),
    ).fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM outbox_message WHERE topic=?", (GOAL11_RESPONSE_TOPIC,)).fetchone()[0] == 1
    print("PASS duplicate Feishu event replays without duplicate state or response")


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
        assert core.conn.execute(
            "SELECT count(*) FROM command_receipt WHERE command_scope=?",
            (GOAL11_HOST_MESSAGE_SCOPE,),
        ).fetchone()[0] == 1
        print("PASS changed payload with same event key rejected")
        return
    raise AssertionError("expected idempotency conflict")


def test_codex_host_dispatches_through_same_production_boundary() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = CodexHostBinding().to_host_message(
        CodexBindingEvent(
            event_id="codex-topic-create",
            thread_id="codex-thread-fixture",
            actor_id="codex-user-fixture",
            command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
        )
    )

    result = bridge.dispatch(message)

    assert result.status == "succeeded"
    assert result.object_id is not None
    command = core.conn.execute("SELECT actor, command_type FROM core_command_envelope").fetchone()
    assert command["actor"] == PRODUCTION_HOST_ACTOR
    assert command["command_type"] == "create_state"
    print("PASS Codex host dispatches through the same production Host boundary")


def test_unregistered_host_rejected_before_core() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    message = FeishuThinBinding().to_hermes_message(make_create_topic_event())
    bad_message = type(message)(
        host="unregistered-host",
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
        print("PASS unregistered host rejected before Core")
        return
    raise AssertionError("expected unregistered host rejection")


def test_response_outbox_enqueues_and_worker_sends_without_hermes() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    dispatch = bridge.dispatch(FeishuThinBinding().to_hermes_message(make_create_topic_event("evt-response")))
    scheduler = make_scheduler(core)
    job_ids = bridge.enqueue_response_jobs(scheduler)
    replay_job_ids = bridge.enqueue_response_jobs(scheduler)
    dispatcher = FeishuResponseDispatcher(core.store)
    runtime = make_runtime(scheduler, dispatcher)

    assert job_ids == replay_job_ids
    assert len(job_ids) == 1
    result = runtime.run_once()

    assert result.status == "succeeded"
    assert core.conn.execute("SELECT status FROM outbox_message WHERE outbox_id=?", (dispatch.response_outbox_id,)).fetchone()[
        "status"
    ] == "sent"
    assert len(dispatcher.sent) == 1
    assert core.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope=?",
        (GOAL11_RESPONSE_SEND_SCOPE,),
    ).fetchone()[0] == 1
    assert core.conn.execute("SELECT count(*) FROM scheduler_job WHERE status='succeeded'").fetchone()[0] == 1
    print("PASS response outbox worker sends without Hermes")


def test_scheduler_recovers_response_job_after_hermes_offline_lease() -> None:
    clock = FakeClock(1_725_100_000_000)
    core = make_core()
    bridge = HermesCoreBridge(core)
    dispatch = bridge.dispatch(FeishuThinBinding().to_hermes_message(make_create_topic_event("evt-lease-recover")))
    scheduler = make_scheduler(core, clock)
    (job_id,) = bridge.enqueue_response_jobs(scheduler)
    claim = scheduler.claim_next(worker_id="offline-hermes", lease_seconds=1)

    assert claim.job_id == job_id
    clock.advance_ms(2_000)
    recovered = scheduler.recover_expired_leases(actor="goal11.scheduler.recovery")
    dispatcher = FeishuResponseDispatcher(core.store)
    runtime = make_runtime(scheduler, dispatcher)
    result = runtime.run_once()

    assert recovered == 1
    assert result.status == "succeeded"
    job = scheduler.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["attempt_count"] == 2
    assert core.conn.execute("SELECT status FROM outbox_message WHERE outbox_id=?", (dispatch.response_outbox_id,)).fetchone()[
        "status"
    ] == "sent"
    print("PASS scheduler recovers response job after Hermes offline lease")


def test_fake_feishu_failure_retries_without_duplicate_send() -> None:
    core = make_core()
    bridge = HermesCoreBridge(core)
    dispatch = bridge.dispatch(FeishuThinBinding().to_hermes_message(make_create_topic_event("evt-send-retry")))
    assert dispatch.response_outbox_id is not None
    scheduler = make_scheduler(core)
    (job_id,) = bridge.enqueue_response_jobs(scheduler)
    dispatcher = FeishuResponseDispatcher(core.store, fail_once_outbox_ids={dispatch.response_outbox_id})
    runtime = make_runtime(scheduler, dispatcher)

    first = runtime.run_once()
    second = runtime.run_once()
    job = scheduler.get_job(job_id)
    payload = json.loads(job["payload_json"])
    replay = dispatcher.dispatch(payload)

    assert first.status == "failed"
    assert first.reason == "handler_error"
    assert second.status == "succeeded"
    assert job["status"] == "succeeded"
    assert job["attempt_count"] == 2
    assert len(dispatcher.sent) == 1
    assert replay.replayed
    assert core.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope=?",
        (GOAL11_RESPONSE_SEND_SCOPE,),
    ).fetchone()[0] == 1
    assert core.conn.execute("SELECT status FROM outbox_message WHERE outbox_id=?", (dispatch.response_outbox_id,)).fetchone()[
        "status"
    ] == "sent"
    print("PASS fake Feishu failure retries without duplicate send")


def main() -> int:
    test_feishu_binding_only_maps_to_host_message()
    test_hermes_dispatches_formal_state_through_core()
    test_duplicate_feishu_event_replays_without_duplicate_state()
    test_changed_payload_with_same_event_key_is_rejected()
    test_codex_host_dispatches_through_same_production_boundary()
    test_unregistered_host_rejected_before_core()
    test_response_outbox_enqueues_and_worker_sends_without_hermes()
    test_scheduler_recovers_response_job_after_hermes_offline_lease()
    test_fake_feishu_failure_retries_without_duplicate_send()
    print("GOAL-11 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
