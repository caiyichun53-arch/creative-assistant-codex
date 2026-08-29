"""The only project-owned source for time-based business schedules.

This module deliberately does not open a database and does not execute a
business operation.  It only reads the current schedule declaration and
guards automatic callers before they can open the formal runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "schedule_registry.json"
REGISTRY_VERSION = "creation_assistant_schedule_registry_v1"
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ScheduleRegistryError(ValueError):
    """The schedule declaration is missing or inconsistent."""


class ScheduleDisabledError(ScheduleRegistryError):
    """An automatic caller attempted to run a disabled schedule."""


@dataclass(frozen=True)
class ScheduleDefinition:
    key: str
    enabled: bool
    time: str
    timezone: str
    trigger_type: str
    domain_scope: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScheduleRegistryError(f"duplicate schedule configuration key: {key}")
        result[key] = value
    return result


class ScheduleRegistry:
    """Read-only access to the project's current business schedule registry."""

    def __init__(self, *, path: Path = DEFAULT_REGISTRY_PATH, payload: dict[str, Any]) -> None:
        self.path = path
        self._payload = payload
        self.migration_protection = bool(payload["migration_protection"])

        schedules = payload["schedules"]
        self._schedules = {
            key: self._definition(key, value) for key, value in schedules.items()
        }

    @classmethod
    def load(cls, path: Path = DEFAULT_REGISTRY_PATH) -> "ScheduleRegistry":
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except FileNotFoundError as exc:
            raise ScheduleRegistryError(f"schedule registry not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ScheduleRegistryError(f"invalid schedule registry JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise ScheduleRegistryError("schedule registry root must be an object")
        cls._validate_payload(payload)
        return cls(path=path, payload=payload)

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        if payload.get("registry_version") != REGISTRY_VERSION:
            raise ScheduleRegistryError("unsupported schedule registry version")
        if not isinstance(payload.get("migration_protection"), bool):
            raise ScheduleRegistryError("migration_protection must be boolean")
        if payload.get("business_event_triggers_are_not_schedules") is not True:
            raise ScheduleRegistryError(
                "business event triggers must remain outside the schedule registry"
            )
        schedules = payload.get("schedules")
        if not isinstance(schedules, dict) or "daily" not in schedules:
            raise ScheduleRegistryError("the single current daily schedule is required")
        if not isinstance(schedules["daily"], dict):
            raise ScheduleRegistryError("daily schedule must be an object")

    @staticmethod
    def _definition(key: str, value: Any) -> ScheduleDefinition:
        if not isinstance(value, dict):
            raise ScheduleRegistryError(f"schedule {key} must be an object")
        enabled = value.get("enabled")
        schedule_time = value.get("time")
        timezone = value.get("timezone")
        trigger_type = value.get("trigger_type")
        domain_scope = value.get("domain_scope")
        if not isinstance(enabled, bool):
            raise ScheduleRegistryError(f"schedule {key}.enabled must be boolean")
        if not isinstance(schedule_time, str) or not _TIME_PATTERN.fullmatch(schedule_time):
            raise ScheduleRegistryError(f"schedule {key}.time must be HH:MM")
        if not isinstance(timezone, str) or not timezone.strip():
            raise ScheduleRegistryError(f"schedule {key}.timezone is required")
        if trigger_type != "absolute_time":
            raise ScheduleRegistryError(f"schedule {key} must use an absolute time")
        if domain_scope != "core_managed":
            raise ScheduleRegistryError(f"schedule {key} must use Core-managed domains")
        return ScheduleDefinition(
            key=key,
            enabled=enabled,
            time=schedule_time,
            timezone=timezone,
            trigger_type=trigger_type,
            domain_scope=domain_scope,
        )

    def schedule_keys(self) -> tuple[str, ...]:
        return tuple(self._schedules)

    def get(self, key: str) -> ScheduleDefinition:
        try:
            return self._schedules[key]
        except KeyError as exc:
            raise ScheduleRegistryError(f"unknown schedule: {key}") from exc

    def due_keys(self, at: datetime) -> tuple[str, ...]:
        current_time = at.strftime("%H:%M")
        return tuple(
            key
            for key, schedule in self._schedules.items()
            if schedule.enabled and schedule.time == current_time
        )

    def run_now(
        self,
        *,
        key: str,
        data_identity: str,
        runner: Callable[[ScheduleDefinition], Any],
    ) -> Any:
        schedule = self.get(key)
        if data_identity != "test" and (
            self.migration_protection or not schedule.enabled
        ):
            raise ScheduleDisabledError(
                f"schedule {key} is disabled for automatic/formal execution during migration protection"
            )
        return runner(schedule)


_AUTOMATIC_DAILY_TRIGGERS = frozenset(
    {
        "automatic",
        "daily_scheduled",
        "daily_missed_batch_catch_up",
        "hermes_daily",
    }
)


def assert_automatic_schedule_allowed(
    *,
    schedule_key: str,
    trigger: str,
    data_identity: str,
    registry: ScheduleRegistry | None = None,
) -> None:
    """Reject old automatic callers before they can open formal storage.

    Test identities remain usable for isolated tests.  Manual/user-resume
    calls are not reclassified as automatic here; their existing Core rules
    remain responsible for their meaning.
    """

    if data_identity == "test" or trigger not in _AUTOMATIC_DAILY_TRIGGERS:
        return
    active_registry = registry or ScheduleRegistry.load()
    schedule = active_registry.get(schedule_key)
    if active_registry.migration_protection or not schedule.enabled:
        raise ScheduleDisabledError(
            f"automatic schedule {schedule_key} is disabled during migration protection"
        )


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "ScheduleDefinition",
    "ScheduleDisabledError",
    "ScheduleRegistry",
    "ScheduleRegistryError",
    "assert_automatic_schedule_allowed",
]
