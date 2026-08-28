"""Minimal one-run/one-process control for detached cold-start execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
import uuid

import psutil

from scripts.core.runtime.runtime_storage import runtime_path


HEARTBEAT_INTERVAL_SECONDS = 1.0
STARTING_RESERVATION_SECONDS = 30.0
DEFAULT_NOTIFICATION_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_SECONDS


_LOCAL_RECORD_LOCKS_GUARD = threading.Lock()
_LOCAL_RECORD_LOCKS: dict[str, threading.Lock] = {}
_TERMINAL_EXECUTOR_STATES = {"completed", "failed", "launch_failed", "stopped"}


class ColdStartExecutorControlError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def executor_root(startup_root: Path | None = None) -> Path:
    return (
        Path(startup_root).resolve()
        if startup_root is not None
        else runtime_path("agent_platform", "cold_start_background").resolve()
    )


def executor_record_path(
    cold_start_id: str,
    *,
    startup_root: Path | None = None,
) -> Path:
    run_id = str(cold_start_id or "").strip()
    if not run_id:
        raise ColdStartExecutorControlError("executor inspection requires a run identity")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
    return executor_root(startup_root) / f"{digest}.executor.json"


def _local_record_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(Path(path).resolve()))
    with _LOCAL_RECORD_LOCKS_GUARD:
        return _LOCAL_RECORD_LOCKS.setdefault(key, threading.Lock())


def _lock_record_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_record_file(handle: Any) -> None:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Closing the handle also releases the operating-system lock.
        pass


@contextmanager
def _executor_record_lock(
    path: Path,
) -> Any:
    """Hold the local and cross-process lock for one record mutation."""

    local_lock = _local_record_lock(path)
    local_lock.acquire()
    handle = None
    locked = False
    try:
        lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _lock_record_file(handle)
        locked = True
        yield
    finally:
        if handle is not None:
            if locked:
                _unlock_record_file(handle)
            handle.close()
        local_lock.release()


def _write_executor_record_unlocked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_executor_record(path: Path, payload: dict[str, Any]) -> None:
    with _executor_record_lock(path):
        _write_executor_record_unlocked(path, payload)


def read_executor_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _normalize_notification_target(
    value: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ColdStartExecutorControlError("notification target must be an object")
    platform = str(value.get("platform") or "").strip().lower()
    chat_id = str(value.get("chat_id") or "").strip()
    thread_id = str(value.get("thread_id") or "").strip()
    if platform != "feishu" or not chat_id:
        raise ColdStartExecutorControlError(
            "notification target requires a dynamic Feishu chat identity"
        )
    target = {"platform": "feishu", "chat_id": chat_id}
    if thread_id:
        target["thread_id"] = thread_id
    return target


def _record_notification_target(
    record: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if not isinstance(record, Mapping) or record.get("notification_target") is None:
        return None
    target = record.get("notification_target")
    if not isinstance(target, Mapping):
        raise ColdStartExecutorControlError("stored notification target is invalid")
    return _normalize_notification_target(target)


def get_cold_start_notification_target(
    *,
    cold_start_id: str,
    startup_root: Path | None = None,
) -> dict[str, str] | None:
    """Read the internal run-bound target without exposing it in public status."""

    path = executor_record_path(cold_start_id, startup_root=startup_root)
    record = read_executor_record(path)
    if record is None or str(record.get("cold_start_id") or "") != str(cold_start_id):
        return None
    target = _record_notification_target(record)
    return dict(target) if target is not None else None


def _matching_process(record: dict[str, Any]) -> psutil.Process | None:
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    token = str(record.get("worker_token") or "").strip()
    if pid <= 0 or not token:
        return None
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        expected_created = float(record.get("process_create_time") or 0.0)
        if expected_created and abs(process.create_time() - expected_created) > 0.01:
            return None
        if token not in process.cmdline():
            return None
        return process
    except (psutil.Error, OSError, ValueError):
        return None


def inspect_cold_start_background(
    *,
    cold_start_id: str,
    startup_root: Path | None = None,
) -> dict[str, Any]:
    path = executor_record_path(cold_start_id, startup_root=startup_root)
    record = read_executor_record(path)
    if record is None or str(record.get("cold_start_id") or "") != str(cold_start_id):
        return {
            "managed": True,
            "active": False,
            "process_exists": False,
            "responsive": False,
            "reserved": False,
            "state": "not_registered",
            "pid": None,
            "record_path": str(path),
        }
    state = str(record.get("state") or "unknown")
    process = _matching_process(record)
    now = time.time()
    started_at = _timestamp(record.get("started_at") or record.get("reserved_at"))
    heartbeat_at = _timestamp(record.get("heartbeat_at"))
    reserved = (
        state == "starting"
        and process is None
        and started_at is not None
        and now - started_at <= STARTING_RESERVATION_SECONDS
    )
    process_exists = process is not None
    active = bool(reserved or (process_exists and state in {"starting", "running", "stop_requested"}))
    responsive = bool(
        process_exists
        and heartbeat_at is not None
        and now - heartbeat_at <= HEARTBEAT_INTERVAL_SECONDS * 4
    )
    return {
        "managed": True,
        "active": active,
        "process_exists": process_exists,
        "responsive": responsive,
        "reserved": reserved,
        "state": state,
        "pid": int(record.get("pid") or 0) or None,
        "worker_token": str(record.get("worker_token") or ""),
        "run_model": str(record.get("run_model") or ""),
        "started_at": record.get("started_at"),
        "heartbeat_at": record.get("heartbeat_at"),
        "record_path": str(path),
    }


def reserve_cold_start_background(
    *,
    configuration_id: str,
    cold_start_id: str,
    startup_root: Path | None = None,
    notification_target: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    path = executor_record_path(cold_start_id, startup_root=startup_root)
    requested_target = _normalize_notification_target(notification_target)
    with _executor_record_lock(path):
        previous_record = read_executor_record(path)
        if (
            previous_record is not None
            and str(previous_record.get("cold_start_id") or "") != str(cold_start_id)
        ):
            raise ColdStartExecutorControlError(
                "executor record belongs to a different cold-start run"
            )
        bound_target = _record_notification_target(previous_record)
        if (
            bound_target is not None
            and requested_target is not None
            and requested_target != bound_target
        ):
            raise ColdStartExecutorControlError(
                "the cold-start notification target is already bound and cannot be changed"
            )
        if bound_target is None:
            bound_target = requested_target
        current = inspect_cold_start_background(
            cold_start_id=cold_start_id,
            startup_root=startup_root,
        )
        previous_state = str((previous_record or {}).get("state") or "")
        if (
            previous_state == "stop_requested"
            or current["active"]
            or current["process_exists"]
        ):
            raise ColdStartExecutorControlError(
                "this cold-start run already has an executor"
            )
        worker_token = uuid.uuid4().hex
        record = {
            "state": "starting",
            "configuration_id": str(configuration_id),
            "cold_start_id": str(cold_start_id),
            "worker_token": worker_token,
            "pid": None,
            "reserved_at": _now(),
            "heartbeat_at": _now(),
        }
        if bound_target is not None:
            record["notification_target"] = dict(bound_target)
        _write_executor_record_unlocked(path, record)
    return path, worker_token


def activate_cold_start_background(
    *,
    record_path: Path,
    worker_token: str,
    run_model: str,
) -> dict[str, Any]:
    with _executor_record_lock(record_path):
        record = read_executor_record(record_path)
        if record is None or str(record.get("worker_token") or "") != worker_token:
            raise ColdStartExecutorControlError(
                "executor reservation is missing or changed"
            )
        if str(record.get("state") or "") != "starting":
            raise ColdStartExecutorControlError(
                "executor reservation is no longer starting"
            )
        process = psutil.Process(os.getpid())
        active = {
            **record,
            "state": "running",
            "pid": os.getpid(),
            "process_create_time": process.create_time(),
            "run_model": str(run_model or ""),
            "started_at": _now(),
            "heartbeat_at": _now(),
        }
        _write_executor_record_unlocked(record_path, active)
    return active


def _refresh_running_heartbeat(
    *,
    record_path: Path,
    worker_token: str,
) -> dict[str, Any] | None:
    with _executor_record_lock(record_path):
        record = read_executor_record(record_path)
        if (
            record is None
            or str(record.get("worker_token") or "") != worker_token
            or str(record.get("state") or "") != "running"
        ):
            return None
        updated = {**record, "heartbeat_at": _now()}
        _write_executor_record_unlocked(record_path, updated)
        return updated


def start_cold_start_heartbeat(
    *,
    record_path: Path,
    worker_token: str,
    notification_callback: Callable[[dict[str, Any]], None] | None = None,
    notification_interval_seconds: float = DEFAULT_NOTIFICATION_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    if notification_callback is not None and not callable(notification_callback):
        raise ColdStartExecutorControlError("notification callback must be callable")
    try:
        notification_interval = float(notification_interval_seconds)
    except (TypeError, ValueError) as exc:
        raise ColdStartExecutorControlError(
            "notification interval must be a positive number"
        ) from exc
    if notification_interval <= 0:
        raise ColdStartExecutorControlError(
            "notification interval must be a positive number"
        )

    stop_event = threading.Event()
    notification_lock = threading.Lock()
    notification_in_flight = False
    last_notification_at = time.monotonic()

    def notify(payload: dict[str, Any]) -> None:
        nonlocal notification_in_flight
        try:
            if stop_event.is_set() or notification_callback is None:
                return
            current = read_executor_record(record_path)
            if (
                current is None
                or str(current.get("worker_token") or "") != worker_token
                or str(current.get("state") or "") != "running"
            ):
                return
            target = _record_notification_target(current)
            if target is None:
                return
            notification_callback({**payload, "notification_target": dict(target)})
        except Exception:
            # Outbound notification is best-effort and must never stop the
            # executor heartbeat or alter the business lifecycle.
            return
        finally:
            with notification_lock:
                notification_in_flight = False

    def heartbeat() -> None:
        nonlocal last_notification_at, notification_in_flight
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            record = _refresh_running_heartbeat(
                record_path=record_path,
                worker_token=worker_token,
            )
            if record is None:
                return
            heartbeat_at = str(record.get("heartbeat_at") or "")
            if stop_event.is_set():
                return
            if notification_callback is None or _record_notification_target(record) is None:
                continue
            current_time = time.monotonic()
            if current_time - last_notification_at < notification_interval:
                continue
            with notification_lock:
                if notification_in_flight:
                    continue
                notification_in_flight = True
                last_notification_at = current_time
            threading.Thread(
                target=notify,
                args=({
                    "event": "cold_start_heartbeat",
                    "cold_start_id": str(record.get("cold_start_id") or ""),
                    "configuration_id": str(record.get("configuration_id") or ""),
                    "heartbeat_at": heartbeat_at,
                },),
                name=f"cold-start-notification-{worker_token[:8]}",
                daemon=True,
            ).start()

    thread = threading.Thread(
        target=heartbeat,
        name=f"cold-start-heartbeat-{worker_token[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def finish_cold_start_background(
    *,
    record_path: Path,
    worker_token: str,
    state: str,
    lifecycle_status: str,
    reason: str = "",
) -> None:
    requested_state = str(state)
    with _executor_record_lock(record_path):
        record = read_executor_record(record_path)
        if record is None or str(record.get("worker_token") or "") != worker_token:
            return
        current_state = str(record.get("state") or "")
        if current_state == "stop_requested" and requested_state != "stopped":
            return
        if (
            current_state in _TERMINAL_EXECUTOR_STATES
            and current_state != requested_state
        ):
            return
        _write_executor_record_unlocked(
            record_path,
            {
                **record,
                "state": requested_state,
                "lifecycle_status": str(lifecycle_status),
                "reason": str(reason or ""),
                "heartbeat_at": _now(),
                "exited_at": _now(),
            },
        )


def fail_cold_start_background_reservation(
    *,
    record_path: Path,
    worker_token: str,
    reason: str,
) -> None:
    finish_cold_start_background(
        record_path=record_path,
        worker_token=worker_token,
        state="launch_failed",
        lifecycle_status="failed",
        reason=reason,
    )


def stop_cold_start_background(
    *,
    cold_start_id: str,
    startup_root: Path | None = None,
    wait_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    path = executor_record_path(cold_start_id, startup_root=startup_root)
    inspection = inspect_cold_start_background(
        cold_start_id=cold_start_id,
        startup_root=startup_root,
    )
    if inspection["reserved"] and not inspection["process_exists"]:
        deadline = time.monotonic() + min(max(wait_timeout_seconds, 0.1), 15.0)
        while time.monotonic() < deadline:
            time.sleep(0.05)
            inspection = inspect_cold_start_background(
                cold_start_id=cold_start_id,
                startup_root=startup_root,
            )
            if inspection["process_exists"] or not inspection["active"]:
                break

    stop_recorded = False
    with _executor_record_lock(path):
        record = read_executor_record(path)
        if record is not None:
            current_state = str(record.get("state") or "")
            if current_state not in _TERMINAL_EXECUTOR_STATES:
                if current_state != "stop_requested":
                    record = {
                        **record,
                        "state": "stop_requested",
                        "stop_requested_at": _now(),
                    }
                    _write_executor_record_unlocked(path, record)
                stop_recorded = True

    process = _matching_process(record or {})
    terminated_pids: list[int] = []
    if process is not None:
        processes = process.children(recursive=True)
        processes.append(process)
        terminated_pids = [item.pid for item in processes]
        for item in reversed(processes):
            try:
                item.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(processes, timeout=max(wait_timeout_seconds / 2, 0.1))
        for item in alive:
            try:
                item.kill()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(alive, timeout=max(wait_timeout_seconds / 2, 0.1))
        if alive:
            raise ColdStartExecutorControlError(
                "cold-start executor process tree did not stop"
            )
    if record is not None and stop_recorded:
        finish_cold_start_background(
            record_path=path,
            worker_token=str(record.get("worker_token") or ""),
            state="stopped",
            lifecycle_status="stopped",
            reason="user requested stop",
        )
    after = inspect_cold_start_background(
        cold_start_id=cold_start_id,
        startup_root=startup_root,
    )
    return {
        "status": "executor_stopped",
        "cold_start_id": str(cold_start_id),
        "terminated": bool(terminated_pids),
        "terminated_pids": terminated_pids,
        "active": bool(after["active"]),
        "process_exists": bool(after["process_exists"]),
        "record_path": str(path),
    }
