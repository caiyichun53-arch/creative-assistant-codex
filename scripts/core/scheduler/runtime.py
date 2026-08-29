"""Small composition boundary for schedules and external executor launch."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .executor_launch import ExecutorLauncher
from .schedule_registry import ScheduleDefinition, ScheduleRegistry


class CreationAssistantScheduler:
    """Expose scheduling facts without owning business state or a database."""

    def __init__(
        self,
        *,
        registry: ScheduleRegistry | None = None,
        executor_launcher: ExecutorLauncher | None = None,
    ) -> None:
        self.registry = registry or ScheduleRegistry.load()
        self.executor_launcher = executor_launcher or ExecutorLauncher()

    def status(self) -> dict[str, Any]:
        return {
            "schedule_source": str(self.registry.path),
            "schedule_keys": list(self.registry.schedule_keys()),
            "migration_protection": self.registry.migration_protection,
            "default_executor": self.executor_launcher.configuration.default_executor,
            "executor_ids": list(self.executor_launcher.configuration.executor_ids()),
            "business_state_owner": "Creation Assistant Core",
            "scheduler_database": None,
        }

    def due_schedule_keys(self, at: datetime) -> tuple[str, ...]:
        return self.registry.due_keys(at)

    def run_now(
        self,
        *,
        schedule_key: str,
        data_identity: str,
        runner: Callable[[ScheduleDefinition], Any],
    ) -> Any:
        return self.registry.run_now(
            key=schedule_key,
            data_identity=data_identity,
            runner=runner,
        )

    def ensure_external_executor(
        self,
        *,
        is_running: Callable[[str], bool],
        executor_id: str | None = None,
        process_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        return self.executor_launcher.ensure_started(
            executor_id=executor_id,
            is_running=is_running,
            process_factory=process_factory,
        )


__all__ = ["CreationAssistantScheduler"]
