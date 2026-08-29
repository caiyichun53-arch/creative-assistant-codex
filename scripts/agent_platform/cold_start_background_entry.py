"""Detached one-shot executor for one already-created cold-start run."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import uuid

from scripts.agent_platform.cold_start_background_control import (
    activate_cold_start_background,
    executor_root,
    fail_cold_start_background_reservation,
    finish_cold_start_background,
    read_executor_record,
    reserve_cold_start_background,
    start_cold_start_heartbeat,
)
from scripts.agent_platform.hermes_cold_start_notifications import (
    ColdStartBackgroundNotifier,
)
from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.agent_platform.hermes_native_feishu_outbound import (
    send_hermes_native_feishu_message,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENTRY_MODULE = "scripts.agent_platform.cold_start_background_entry"
STARTUP_TIMEOUT_SECONDS = 15.0


class ColdStartBackgroundLaunchError(RuntimeError):
    pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_stdout_json_utf8(payload: Mapping[str, Any]) -> None:
    """Write one JSON result without depending on the Windows console codec."""
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is None:
        raise RuntimeError("background JSON output requires binary stdout")
    stdout_buffer.write(encoded)
    stdout_buffer.flush()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ColdStartBackgroundLaunchError(
            "cold-start background startup receipt is not an object"
        )
    return value


def _failure_notification_reason(
    core: Any | None,
    *,
    cold_start_id: str,
    fallback: str,
) -> str:
    """Build a user-facing failure summary from existing stage records."""
    fallback_text = str(fallback or "").strip()
    if core is None:
        return fallback_text
    try:
        def candidate_failure_summary() -> str:
            candidates = (
                (
                    "stage0_cold_start_domain_boundary_candidate",
                    "生产边界候选生成失败",
                ),
                (
                    "stage0_cold_start_content_type_candidate",
                    "内容类型候选生成失败",
                ),
            )
            failures: list[tuple[str, str, str]] = []
            for table, label in candidates:
                row = core.conn.execute(
                    f"SELECT status, failure_json, created_at FROM {table} "
                    "WHERE cold_start_id=? AND data_identity=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (cold_start_id, core.data_identity),
                ).fetchone()
                if row is None or str(row["status"] or "") != "failed":
                    continue
                try:
                    failure = json.loads(str(row["failure_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    failure = {}
                reason = (
                    str(failure.get("reason") or "").strip()
                    if isinstance(failure, dict)
                    else ""
                )
                failures.append((str(row["created_at"] or ""), label, reason))
            if not failures:
                return ""
            _, label, reason = max(failures, key=lambda item: item[0])
            detail = reason or fallback_text or "未记录失败原因"
            return f"【{label}】\n主要原因：{detail}\n状态：可恢复"

        rows = core.conn.execute(
            "SELECT item.status, item.error_json "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "AND item.step_name='breakdown'",
            (cold_start_id, core.data_identity),
        ).fetchall()
        if not rows:
            return candidate_failure_summary() or fallback_text
        reasons: dict[str, int] = {}
        completed = failed = 0
        for row in rows:
            status = str(row["status"] or "")
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
                try:
                    error = json.loads(str(row["error_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    error = {}
                reason = str(error.get("reason") or "").strip() if isinstance(error, dict) else ""
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
        total = len(rows)
        if not failed:
            return candidate_failure_summary() or fallback_text
        primary_reason = max(reasons, key=reasons.get) if reasons else fallback_text
        lines = [
            "【内容拆解中断】",
            f"总计：{total}条",
            f"已完成：{completed}条",
            f"剩余：{max(total - completed, 0)}条",
            f"主要原因：{primary_reason or '未记录失败原因'}",
            "状态：可恢复",
        ]
        return "\n".join(lines)
    except Exception:
        return fallback_text


def _detached_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
                | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            ),
            "startupinfo": startupinfo,
        }
    return {"start_new_session": True}

def _reap_background_process(process: Any) -> None:
    """Reap a detached child later without making the tool call wait for it."""

    threading.Thread(
        target=process.wait,
        name=f"cold-start-reaper-{process.pid}",
        daemon=True,
    ).start()




def launch_cold_start_background(
    *,
    configuration_id: str,
    cold_start_id: str,
    actor: str,
    python_executable: str | None = None,
    entry_module: str = DEFAULT_ENTRY_MODULE,
    startup_root: Path | None = None,
    startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
    popen_factory: Callable[..., Any] | None = None,
    notification_target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start one detached worker and wait only for its startup handshake."""

    configuration = str(configuration_id or "").strip()
    run_id = str(cold_start_id or "").strip()
    actor_value = str(actor or "").strip()
    if not configuration or not run_id or not actor_value:
        raise ColdStartBackgroundLaunchError(
            "background launch requires configuration, run and actor identities"
        )
    root = executor_root(startup_root, data_identity="production")
    root.mkdir(parents=True, exist_ok=True)
    launch_id = uuid.uuid4().hex
    executor_record, worker_token = reserve_cold_start_background(
        configuration_id=configuration,
        cold_start_id=run_id,
        startup_root=startup_root,
        notification_target=notification_target,
    )
    startup_receipt = root / f"{run_id}.{launch_id}.startup.json"
    log_path = root / f"{run_id}.{launch_id}.log"
    command = [
        str(python_executable or sys.executable),
        "-m",
        str(entry_module),
        "--configuration-id",
        configuration,
        "--cold-start-id",
        run_id,
        "--actor",
        actor_value,
        "--startup-receipt",
        str(startup_receipt),
        "--executor-record",
        str(executor_record),
        "--worker-token",
        worker_token,
    ]
    popen = popen_factory or subprocess.Popen
    process: Any | None = None
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        process = popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_detached_process_kwargs(),
        )
    except Exception as exc:
        fail_cold_start_background_reservation(
            record_path=executor_record,
            worker_token=worker_token,
            reason=str(exc),
        )
        raise ColdStartBackgroundLaunchError(
            f"could not create the cold-start background process: {exc}"
        ) from exc
    finally:
        log_handle.close()

    worker_reported_failure_reason: str | None = None
    deadline = time.monotonic() + max(float(startup_timeout_seconds), 0.1)
    try:
        while time.monotonic() < deadline:
            if startup_receipt.exists():
                receipt = _read_json(startup_receipt)
                receipt_status = str(receipt.get("status") or "")
                if receipt_status == "ready":
                    if str(receipt.get("cold_start_id") or "") != run_id:
                        raise ColdStartBackgroundLaunchError(
                            "background startup receipt belongs to a different run"
                        )
                    _reap_background_process(process)
                    return {
                        "status": "background_started",
                        "started": True,
                        "cold_start_id": run_id,
                        "configuration_id": configuration,
                        "pid": int(process.pid),
                        "run_model": str(receipt.get("run_model") or ""),
                        "worker_token": worker_token,
                        "log_path": str(log_path),
                        "executor_record": str(executor_record),
                    }
                if receipt_status == "failed":
                    receipt_run_id = str(receipt.get("cold_start_id") or "").strip()
                    receipt_configuration_id = str(
                        receipt.get("configuration_id") or ""
                    ).strip()
                    receipt_reason = str(receipt.get("reason") or "").strip()
                    if (
                        receipt_run_id != run_id
                        or receipt_configuration_id != configuration
                        or not receipt_reason
                    ):
                        raise ColdStartBackgroundLaunchError(
                            "background startup failure receipt is invalid"
                        )
                    worker_reported_failure_reason = receipt_reason
                    _reap_background_process(process)
                    raise ColdStartBackgroundLaunchError(receipt_reason)
                raise ColdStartBackgroundLaunchError(
                    str(receipt.get("reason") or "background worker startup failed")
                )
            return_code = process.poll()
            if return_code is not None:
                raise ColdStartBackgroundLaunchError(
                    f"cold-start background process exited before startup handshake ({return_code})"
                )
            time.sleep(0.05)
        raise ColdStartBackgroundLaunchError(
            "cold-start background process did not become ready before the startup timeout"
        )
    except Exception:
        if worker_reported_failure_reason is None:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5.0)
                except Exception:
                    pass
            fail_cold_start_background_reservation(
                record_path=executor_record,
                worker_token=worker_token,
                reason="background startup handshake failed",
            )
        else:
            try:
                fail_cold_start_background_reservation(
                    record_path=executor_record,
                    worker_token=worker_token,
                    reason=worker_reported_failure_reason,
                )
            except Exception:
                # A bookkeeping failure must not hide the worker's real reason.
                pass
        raise
    finally:
        try:
            startup_receipt.unlink()
        except FileNotFoundError:
            pass


def _configuration_and_run(
    core: Any,
    *,
    configuration_id: str,
    cold_start_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return CreationAssistantFormalBusinessCore(core=core).validate_cold_start_background(
        configuration_id=configuration_id,
        cold_start_id=cold_start_id,
    )


def prepare_cold_start_background_execution(
    core: Any,
    *,
    configuration_id: str,
    cold_start_id: str,
    executor_builder: Callable[..., Any] | None = None,
    registration_service_factory: Callable[..., Any] | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Validate one existing run and build its executor from its execution model."""

    _configuration_and_run(
        core,
        configuration_id=configuration_id,
        cold_start_id=cold_start_id,
    )
    run_model_binding = core.get_cold_start_run_model_binding(
        cold_start_id=cold_start_id
    )
    if executor_builder is None:
        from scripts.core.production.stage1_competitor_registration import (
            build_configured_competitor_registration_executor,
        )

        executor_builder = build_configured_competitor_registration_executor
    executor_kwargs: dict[str, Any] = {
        "task_model_binding": dict(run_model_binding),
    }
    if progress_callback is not None:
        executor_kwargs["progress_callback"] = progress_callback
    executor = executor_builder(core, **executor_kwargs)
    if registration_service_factory is None:
        from scripts.core.production.stage1_competitor_registration import (
            CompetitorRegistrationService,
        )

        registration_service_factory = CompetitorRegistrationService
    registration_service = registration_service_factory(
        core=core,
        executor=executor,
    )
    return registration_service, dict(run_model_binding)


def run_cold_start_background_execution(
    core: Any,
    *,
    configuration_id: str,
    cold_start_id: str,
    actor: str,
    registration_service: Any,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the existing orchestrator through the Core business boundary."""

    return CreationAssistantFormalBusinessCore(core=core).execute_cold_start_background(
        configuration_id=configuration_id,
        cold_start_id=cold_start_id,
        actor=actor,
        registration_service=registration_service,
        progress_callback=progress_callback,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one already-created cold start in a detached process"
    )
    parser.add_argument("--configuration-id", required=True)
    parser.add_argument("--cold-start-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--startup-receipt", type=Path, required=True)
    parser.add_argument("--executor-record", type=Path, required=True)
    parser.add_argument("--worker-token", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    core: Any | None = None
    business: CreationAssistantFormalBusinessCore | None = None
    ready_written = False
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    terminal_reason = ""
    notifier: ColdStartBackgroundNotifier | None = None
    try:
        executor_record = read_executor_record(arguments.executor_record) or {}
        raw_notification_target = executor_record.get("notification_target")
        notification_target = (
            dict(raw_notification_target)
            if isinstance(raw_notification_target, Mapping)
            else None
        )

        def notification_sender(
            *,
            target: Mapping[str, Any],
            message: str,
            **_metadata: Any,
        ) -> dict[str, Any]:
            return send_hermes_native_feishu_message(
                chat_id=str(target.get("chat_id") or ""),
                message=message,
                thread_id=str(target.get("thread_id") or "") or None,
                timeout_seconds=8.0,
            )

        notifier = ColdStartBackgroundNotifier(
            cold_start_id=arguments.cold_start_id,
            record_path=arguments.executor_record,
            worker_token=arguments.worker_token,
            notification_target=notification_target,
            sender=notification_sender,
        )

        def executor_progress(phase: str, detail: str) -> None:
            notifier.progress({"phase": phase, "detail": detail})

        def orchestrator_progress(payload: dict[str, Any]) -> None:
            notifier.progress(payload)

        def heartbeat_notification(_payload: dict[str, Any]) -> None:
            notifier.heartbeat()

        from scripts.core.production.stage0_content_core import (
            FORMAL_DB_PATH,
            Stage0ContentProductionCore,
        )

        core = Stage0ContentProductionCore.open(
            FORMAL_DB_PATH,
            data_identity="production",
        )
        business = CreationAssistantFormalBusinessCore(core=core)
        registration_service, run_model_binding = (
            prepare_cold_start_background_execution(
                core,
                configuration_id=arguments.configuration_id,
                cold_start_id=arguments.cold_start_id,
                progress_callback=executor_progress,
            )
        )

        activate_cold_start_background(
            record_path=arguments.executor_record,
            worker_token=arguments.worker_token,
            run_model=str(run_model_binding.get("model_name") or ""),
        )
        heartbeat_stop, heartbeat_thread = start_cold_start_heartbeat(
            record_path=arguments.executor_record,
            worker_token=arguments.worker_token,
            notification_callback=heartbeat_notification,
        )
        _write_json(
            arguments.startup_receipt,
            {
                "status": "ready",
                "cold_start_id": arguments.cold_start_id,
                "configuration_id": arguments.configuration_id,
                "run_model": str(run_model_binding.get("model_name") or ""),
                "pid": os.getpid(),
                "ready_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        ready_written = True
        result = run_cold_start_background_execution(
            core,
            configuration_id=arguments.configuration_id,
            cold_start_id=arguments.cold_start_id,
            actor=arguments.actor,
            registration_service=registration_service,
            progress_callback=orchestrator_progress,
        )
        _write_stdout_json_utf8(result)
        lifecycle_status = str(
            business.read_cold_start_lifecycle(
                cold_start_id=arguments.cold_start_id
            )["status"]
        )
        if lifecycle_status == "running":
            raise RuntimeError(
                "background executor exited while automatic run lifecycle remained running"
            )
        if lifecycle_status == "completed":
            notifier.final("completed")
        elif lifecycle_status == "failed":
            notifier.final(
                "failed",
                _failure_notification_reason(
                    core,
                    cold_start_id=arguments.cold_start_id,
                    fallback=str(result.get("reason") or result.get("error") or ""),
                ),
            )
        return 1 if lifecycle_status == "failed" else 0
    except Exception as exc:
        terminal_reason = str(exc)
        try:
            if business is not None:
                business.fail_cold_start_background_if_running(
                    configuration_id=arguments.configuration_id,
                    cold_start_id=arguments.cold_start_id,
                    actor=arguments.actor,
                    reason=f"background execution failed: {exc}",
                )
        except Exception:
            pass
        if not ready_written:
            _write_json(
                arguments.startup_receipt,
                {
                    "status": "failed",
                    "cold_start_id": arguments.cold_start_id,
                    "configuration_id": arguments.configuration_id,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        if notifier is not None:
            notifier.final(
                "failed",
                _failure_notification_reason(
                    core,
                    cold_start_id=arguments.cold_start_id,
                    fallback=terminal_reason,
                ),
            )
        _write_stdout_json_utf8(
            {
                "status": "failed",
                "cold_start_id": arguments.cold_start_id,
                "error_type": type(exc).__name__,
                "reason": str(exc),
            }
        )
        return 1
    finally:

        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)
        if ready_written and core is not None:
            try:
                if business is None:
                    raise RuntimeError("cold-start Core boundary is unavailable")
                lifecycle_status = str(
                    business.read_cold_start_lifecycle(
                        cold_start_id=arguments.cold_start_id
                    )["status"]
                )
                finish_cold_start_background(
                    record_path=arguments.executor_record,
                    worker_token=arguments.worker_token,
                    state=lifecycle_status,
                    lifecycle_status=lifecycle_status,
                    reason=terminal_reason,
                )
            except Exception:
                pass
        if core is not None:
            core.close()

if __name__ == "__main__":
    raise SystemExit(main())
