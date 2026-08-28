from __future__ import annotations

import unittest

from scripts.agent_platform.hermes_cold_start_control import (
    parse_control_command,
    resolve_cold_start_id,
)


class _Row(dict):
    __getattr__ = dict.__getitem__


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    def execute(self, _sql: str, params: tuple[str, str]) -> _Row | None:
        status = self.statuses.get(params[0])
        return _Result(_Row(status=status) if status is not None else None)


class _Core:
    data_identity = "production"

    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.conn = _Conn(statuses)

    def list_cold_start_configurations(self):
        return [{"cold_start_id": run_id} for run_id in self.statuses]


class HermesColdStartControlTests(unittest.TestCase):
    def test_exact_control_words_only(self) -> None:
        self.assertEqual(parse_control_command("停止冷启动"), "stop")
        self.assertEqual(parse_control_command(" /cold-start stop "), "stop")
        self.assertIsNone(parse_control_command("为什么刚才停止冷启动失败"))
        self.assertIsNone(parse_control_command("停止冷启动，请说明原因"))

    def test_unique_running_run_is_selected_without_latest_guess(self) -> None:
        run_id, clarification = resolve_cold_start_id(
            _Core({"run-old": "stopped", "run-live": "running"}),
            operation="stop",
        )
        self.assertEqual(run_id, "run-live")
        self.assertIsNone(clarification)

    def test_multiple_running_runs_require_an_exact_id(self) -> None:
        run_id, clarification = resolve_cold_start_id(
            _Core({"run-a": "running", "run-b": "running"}),
            operation="stop",
        )
        self.assertIsNone(run_id)
        self.assertEqual(clarification["status"], "needs_clarification")

    def test_stopped_history_is_not_an_active_stop_target(self) -> None:
        run_id, result = resolve_cold_start_id(
            _Core({"run-old": "stopped"}),
            operation="stop",
        )
        self.assertIsNone(run_id)
        self.assertEqual(result["status"], "no_active_cold_start")


if __name__ == "__main__":
    unittest.main()

