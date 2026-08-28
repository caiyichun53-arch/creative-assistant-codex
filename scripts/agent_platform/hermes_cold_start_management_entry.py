"""CLI host for the registered Hermes cold-start management operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.agent_platform.hermes_cold_start_action import HermesColdStartAction
from scripts.agent_platform.cold_start_background_control import (
    get_cold_start_notification_target,
    inspect_cold_start_background,
    stop_cold_start_background,
)
from scripts.agent_platform.cold_start_background_entry import (
    launch_cold_start_background,
)
from scripts.agent_platform.hermes_tool_registry import HermesToolRouter
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import (
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--operation",
        required=True,
        choices=("status", "stop", "resume", "verify-zero-state"),
    )
    parser.add_argument("--actor", default="single_user", help="legacy transport context; not a business operator")
    parser.add_argument("--carrier-binding-id", required=True)
    parser.add_argument("--session-ref", required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--cold-start-id")
    parser.add_argument("--domain-label")
    parser.add_argument("--configuration-id")
    arguments = parser.parse_args()

    core = Stage0ContentProductionCore.open(
        Path(FORMAL_DB_PATH),
        data_identity="production",
    )
    try:
        adapter = ColdStartHumanDecisionAdapter(
            core=core,
            background_execution_launcher=launch_cold_start_background,
            background_execution_inspector=inspect_cold_start_background,
            background_execution_stopper=stop_cold_start_background,
            background_notification_target_reader=get_cold_start_notification_target,
        )
        if arguments.operation == "verify-zero-state":
            domain_label = str(arguments.domain_label or "").strip()
            cold_start_id = str(arguments.cold_start_id or "").strip()
            configuration_id = str(arguments.configuration_id or "").strip()
            if not domain_label or not cold_start_id or not configuration_id:
                raise SystemExit(
                    "zero-state verification requires domain, run and configuration"
                )
            state = core.domain_business_state(domain_label=domain_label)
            result = {
                "status": "completed",
                "operation": "verify-zero-state",
                "result": {
                    **state,
                    "target_run_rows": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_cold_start "
                        "WHERE cold_start_id=? AND data_identity=?",
                        (cold_start_id, core.data_identity),
                    ).fetchone()[0],
                    "target_configuration_rows": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_cold_start_configuration "
                        "WHERE configuration_id=? AND data_identity=?",
                        (configuration_id, core.data_identity),
                    ).fetchone()[0],
                    "domain_account_rows": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_content_account "
                        "WHERE domain_label=? AND data_identity=?",
                        (domain_label, core.data_identity),
                    ).fetchone()[0],
                    "preserved_onboarding_failures": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_cold_start_onboarding_failure "
                        "WHERE configuration_id=? AND data_identity=?",
                        (configuration_id, core.data_identity),
                    ).fetchone()[0],
                    "preserved_audit_events": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_audit_event "
                        "WHERE payload_json LIKE ? AND data_identity=?",
                        (f"%{cold_start_id}%", core.data_identity),
                    ).fetchone()[0],
                    "preserved_human_commands": core.conn.execute(
                        "SELECT COUNT(*) FROM stage0_human_decision_command "
                        "WHERE (target_ref=? OR payload_json LIKE ?) "
                        "AND data_identity=?",
                        (
                            cold_start_id,
                            f"%{cold_start_id}%",
                            core.data_identity,
                        ),
                    ).fetchone()[0],
                    "created_new_run": False,
                    "formal_business_data_written": False,
                },
            }
        else:
            action = HermesColdStartAction(
                adapter=adapter,
                carrier_binding_id=arguments.carrier_binding_id,
            )
            result = HermesToolRouter(cold_start_action=action).dispatch(
                tool_name=HermesColdStartAction.ACTION_NAME,
                arguments={
                    "operation": arguments.operation,
                    "cold_start_id": str(arguments.cold_start_id or "").strip() or None,
                    "explicit_user_confirmation": arguments.operation == "stop",
                    **(
                        {"reason": arguments.reason}
                        if arguments.reason
                        else {}
                    ),
                },
                context={
                    "user_identity": arguments.actor,
                    "hermes_carrier_binding_id": arguments.carrier_binding_id,
                    "session_identity": arguments.session_ref,
                    "command_identity": arguments.command_id,
                },
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        core.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

