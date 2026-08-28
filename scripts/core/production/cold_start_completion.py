"""One idempotent completion judgment for the current cold-start run.

This module does not add a new state machine. It reads the existing current-run
records and promotes the existing cold-start row to completed only when all
formal prerequisites are true for the same data identity and cold-start id.
"""

from __future__ import annotations

from typing import Any


def _missing_breakdowns(self: Any, cold_start_id: str) -> int:
    row = self.conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM stage0_competitor_registration_item material
        JOIN stage0_competitor_registration registration
          ON registration.registration_id=material.registration_id
        LEFT JOIN stage0_competitor_registration_item breakdown
          ON breakdown.registration_id=material.registration_id
         AND breakdown.item_ref=material.item_ref
         AND breakdown.data_identity=material.data_identity
         AND breakdown.step_name='breakdown'
        WHERE registration.cold_start_id=?
          AND registration.data_identity=?
          AND material.data_identity=?
          AND material.step_name='transcripts_and_comments'
          AND material.status='completed'
          AND (breakdown.status IS NULL OR breakdown.status NOT IN ('completed', 'excluded'))
        """,
        (cold_start_id, self.data_identity, self.data_identity),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def _step_count(self: Any, cold_start_id: str, step_name: str) -> int:
    row = self.conn.execute(
        """
        SELECT COUNT(DISTINCT step.registration_id) AS count
        FROM stage0_competitor_registration_step step
        JOIN stage0_competitor_registration registration
          ON registration.registration_id=step.registration_id
        WHERE registration.cold_start_id=?
          AND registration.data_identity=?
          AND step.data_identity=?
          AND step.step_name=?
        """,
        (cold_start_id, self.data_identity, self.data_identity, step_name),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def _current_run_conditions(self: Any, *, cold_start_id: str) -> dict[str, Any]:
    row = self.conn.execute(
        "SELECT cold_start_id, status FROM stage0_cold_start "
        "WHERE cold_start_id=? AND data_identity=?",
        (cold_start_id, self.data_identity),
    ).fetchone()
    if row is None:
        raise self.StateTransitionError(
            "cold-start completion requires a current run in this data identity"
        )

    if str(row["status"]) == "completed":
        return {
            "current_run_valid": True,
            "registrations_20": True,
            "registrations_completed": True,
            "historical_material_complete": True,
            "high_signal_identification_complete": True,
            "tag_library_accepted": True,
            "preparation_complete": True,
            "necessary_breakdown_complete": True,
            "content_types_frozen_for_current_run": True,
            "production_boundary_frozen_for_current_run": True,
            "already_completed": True,
        }

    registration_row = self.conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count
        FROM stage0_competitor_registration
        WHERE cold_start_id=? AND data_identity=?
        """,
        (cold_start_id, self.data_identity),
    ).fetchone()
    total = int(registration_row["total"] if registration_row else 0)
    completed_count = int(registration_row["completed_count"] or 0) if registration_row else 0
    expected = 20

    tag_row = self.conn.execute(
        "SELECT status FROM stage0_cold_start_tag_library "
        "WHERE cold_start_id=? AND data_identity=?",
        (cold_start_id, self.data_identity),
    ).fetchone()

    return {
        "current_run_valid": str(row["status"]) in {"running", "waiting_human"},
        "registrations_20": total == expected,
        "registrations_completed": completed_count == expected,
        "historical_material_complete": _step_count(self, cold_start_id, "historical_material") == expected,
        "high_signal_identification_complete": _step_count(self, cold_start_id, "high_signal_identification") == expected,
        "tag_library_accepted": tag_row is not None and str(tag_row["status"]) == "accepted",
        "preparation_complete": _step_count(self, cold_start_id, "transcripts_and_comments") == expected,
        "necessary_breakdown_complete": _missing_breakdowns(self, cold_start_id) == 0,
        "content_types_frozen_for_current_run": bool(
            self.cold_start_content_types_are_frozen(cold_start_id=cold_start_id)
        ),
        "production_boundary_frozen_for_current_run": bool(
            self.cold_start_domain_boundary_is_frozen(cold_start_id=cold_start_id)
        ),
        "already_completed": False,
    }


def try_complete_cold_start(
    self: Any,
    *,
    cold_start_id: str,
    trigger: str,
    actor: str = "system",
) -> dict[str, Any]:
    """Attempt to complete exactly one current cold-start run.

    A false result is not a new waiting state. It is a read-only report of
    missing prerequisites. A true result performs the one existing status
    transition and is safe to call repeatedly.
    """
    run_id = str(cold_start_id or "").strip()
    trigger_value = str(trigger or "").strip()
    actor_value = str(actor or "").strip() or "system"
    if not run_id or not trigger_value:
        raise self.StateTransitionError(
            "cold-start completion requires the run identity and a trigger"
        )

    conditions = _current_run_conditions(self, cold_start_id=run_id)
    if conditions.get("already_completed"):
        return {
            "cold_start_id": run_id,
            "status": "completed",
            "completed": True,
            "missing_conditions": [],
            "conditions": conditions,
            "trigger": trigger_value,
            "idempotent": True,
        }

    missing = [
        key for key, value in conditions.items()
        if key != "already_completed" and value is not True
    ]
    if missing:
        current = self.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        return {
            "cold_start_id": run_id,
            "status": str(current["status"]) if current is not None else "unknown",
            "completed": False,
            "missing_conditions": missing,
            "conditions": conditions,
            "trigger": trigger_value,
            "idempotent": True,
        }

    now = self._now()
    with self.conn:
        updated = self.conn.execute(
            """
            UPDATE stage0_cold_start
               SET status='completed', completed_at=?
             WHERE cold_start_id=?
               AND data_identity=?
               AND status IN ('running', 'waiting_human')
            """,
            (now, run_id, self.data_identity),
        ).rowcount
        if updated:
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='completed' "
                "WHERE cold_start_id=? AND data_identity=? "
                "AND status IN ('confirmed', 'started')",
                (run_id, self.data_identity),
            )
            self._audit(
                None,
                "cold_start_completed_by_unified_judgment",
                {
                    "cold_start_id": run_id,
                    "trigger": trigger_value,
                    "actor": actor_value,
                    "conditions": conditions,
                },
            )

    current = self.conn.execute(
        "SELECT status FROM stage0_cold_start "
        "WHERE cold_start_id=? AND data_identity=?",
        (run_id, self.data_identity),
    ).fetchone()
    completed = current is not None and str(current["status"]) == "completed"
    return {
        "cold_start_id": run_id,
        "status": str(current["status"]) if current is not None else "unknown",
        "completed": completed,
        "missing_conditions": [] if completed else missing,
        "conditions": conditions,
        "trigger": trigger_value,
        "idempotent": True,
    }


def attach_core_methods(core_cls: type[Any]) -> None:
    core_cls.try_complete_cold_start = try_complete_cold_start
