"""Isolation checks for the one-shot Hermes-native Feishu outbound bridge."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from scripts.agent_platform.hermes_native_feishu_outbound import (
    HERMES_AGENT_ROOT,
    HERMES_HOME,
    HERMES_PYTHON,
    HermesNativeFeishuOutboundError,
    check_hermes_native_feishu_outbound,
    default_hermes_native_feishu_runner_command,
    send_hermes_native_feishu_message,
)
from scripts.agent_platform.hermes_native_feishu_outbound_entry import (
    check_native_feishu_outbound,
    send_native_feishu_outbound,
)


class HermesNativeFeishuOutboundTest(unittest.TestCase):
    @staticmethod
    def _echo_command(*, returncode: int = 0, emit_json: bool = True) -> tuple[str, ...]:
        script = (
            "import json,sys; "
            "payload=json.load(sys.stdin); "
            "print('hermes test log'); "
            + (
                "print(json.dumps({'ok':True,'status':'ready' if '--check' in sys.argv else 'sent','payload':payload,'argv':sys.argv[1:]},ensure_ascii=False)); "
                if emit_json
                else "print('no result'); "
            )
            + f"raise SystemExit({returncode})"
        )
        return (sys.executable, "-c", script)

    def test_default_runner_is_one_shot_current_creator_hermes(self) -> None:
        command = default_hermes_native_feishu_runner_command()
        self.assertEqual(command[:4], ("wsl.exe", "-d", "Ubuntu", "--"))
        self.assertIn(f"HERMES_HOME={HERMES_HOME}", command)
        self.assertIn(f"PYTHONPATH={HERMES_AGENT_ROOT}", command)
        self.assertIn(HERMES_PYTHON, command)
        self.assertTrue(command[-1].endswith("hermes_native_feishu_outbound_entry.py"))

    def test_wrapper_passes_unicode_dynamic_target_and_parses_last_json(self) -> None:
        result = send_hermes_native_feishu_message(
            chat_id="oc_dynamic",
            thread_id="omt_thread",
            message="阶段进度：采集完成",
            runner_command=self._echo_command(),
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["payload"]["chat_id"], "oc_dynamic")
        self.assertEqual(result["payload"]["thread_id"], "omt_thread")
        self.assertEqual(result["payload"]["message"], "阶段进度：采集完成")

    def test_check_adds_check_flag_and_never_requires_a_target(self) -> None:
        result = check_hermes_native_feishu_outbound(
            runner_command=self._echo_command()
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["payload"], {"check": True})
        self.assertIn("--check", result["argv"])

    def test_wrapper_surfaces_json_failure_from_nonzero_process(self) -> None:
        script = (
            "import json,sys; json.load(sys.stdin); "
            "print(json.dumps({'ok':False,'status':'failed','error':'plugin missing'})); "
            "raise SystemExit(7)"
        )
        with self.assertRaisesRegex(
            HermesNativeFeishuOutboundError, "plugin missing"
        ):
            send_hermes_native_feishu_message(
                chat_id="oc_dynamic",
                message="test",
                runner_command=(sys.executable, "-c", script),
            )

    def test_wrapper_rejects_success_without_a_json_result(self) -> None:
        with self.assertRaisesRegex(
            HermesNativeFeishuOutboundError, "final JSON object"
        ):
            send_hermes_native_feishu_message(
                chat_id="oc_dynamic",
                message="test",
                runner_command=self._echo_command(emit_json=False),
            )

    def test_wrapper_requires_dynamic_target_and_message_before_launch(self) -> None:
        with self.assertRaisesRegex(
            HermesNativeFeishuOutboundError, "dynamic chat_id"
        ):
            send_hermes_native_feishu_message(chat_id="", message="test")
        with self.assertRaisesRegex(
            HermesNativeFeishuOutboundError, "non-empty message"
        ):
            send_hermes_native_feishu_message(chat_id="oc_dynamic", message=" ")

    def test_entry_calls_only_registry_sender_with_existing_config(self) -> None:
        calls: list[tuple[object, ...]] = []
        pconfig = object()

        async def sender(*args: object) -> dict[str, object]:
            calls.append(args)
            return {
                "success": True,
                "platform": "feishu",
                "chat_id": "oc_dynamic",
                "message_id": "om_result",
            }

        result = send_native_feishu_outbound(
            {
                "chat_id": "oc_dynamic",
                "message": "仍在运行",
                "thread_id": "omt_thread",
            },
            runtime_loader=lambda: (pconfig, sender),
        )
        self.assertEqual(
            calls,
            [("feishu", pconfig, "oc_dynamic", "仍在运行", "omt_thread")],
        )
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["result"]["message_id"], "om_result")

    def test_entry_check_loads_runtime_but_does_not_call_sender(self) -> None:
        calls: list[str] = []

        async def sender(*_args: object) -> dict[str, object]:
            calls.append("sent")
            return {"success": True}

        result = check_native_feishu_outbound(
            runtime_loader=lambda: (object(), sender)
        )
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["sent"])
        self.assertEqual(calls, [])

    def test_entry_does_not_own_feishu_sdk_or_credentials(self) -> None:
        entry_path = Path(
            sys.modules[
                "scripts.agent_platform.hermes_native_feishu_outbound_entry"
            ].__file__
        )
        source = entry_path.read_text(encoding="utf-8")
        self.assertNotIn("import lark_oapi", source)
        self.assertNotIn("Client.builder", source)
        self.assertNotIn("FEISHU_APP_SECRET", source)
        self.assertNotIn("FEISHU_APP_ID", source)


if __name__ == "__main__":
    unittest.main()
