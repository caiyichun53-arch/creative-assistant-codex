import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from scripts.web.read_only_server import create_action_server
from tests.test_core_unified_status import _seed_domain
from tests.test_experience_confirmation_and_usage import _insert_candidate_run, _open_candidate


def _post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    with urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


class PublicationFeedbackP0BTests(unittest.TestCase):
    def test_web_to_p7_validation_chain_and_manual_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publication-p0b.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                domain = "music_entertainment"
                cold_start_id, _ = _seed_domain(core, domain, cold_status="completed")
                activation = core.get_current_domain_activation(domain_label=domain)
                self.assertIsNotNone(activation)
                _insert_candidate_run(core, "p0b-validation-run", domain)
                candidate_id = _open_candidate(
                    core,
                    run_id="p0b-validation-run",
                    domain=domain,
                    prefix="p0b-validation",
                    complete=True,
                    decision="accepted",
                )
                task = core.create_task(
                    topic_payload={
                        "title": "P0-B TEST 内容",
                        "core_question": "验证发布后的经验结果是否可追踪",
                        "domain": domain,
                        "domain_label": domain,
                        "activation_id": activation["activation_id"],
                    },
                    actor="test-user",
                    actor_kind="user",
                    reason="P0-B TEST task",
                    idempotency_key="p0b-task",
                )
                task_id = str(task["task_id"])
                core._insert_artifact_payload(
                    str(task["topic_version_id"]),
                    "formal_topic",
                    {
                        "title": "P0-B TEST 内容",
                        "core_question": "验证发布后的经验结果是否可追踪",
                        "domain": domain,
                        "domain_label": domain,
                        "activation_id": activation["activation_id"],
                        "source_refs": [],
                    },
                )
                review_version_id = "p0b-review-version"
                now = "2026-08-30T00:00:00+00:00"
                core.conn.execute(
                    "INSERT INTO stage0_content_node_version VALUES "
                    "(?, ?, 'review', NULL, ?, NULL, 'approved', ?, 'passed', ?, ?, ?, 1)",
                    (
                        review_version_id,
                        task_id,
                        task["topic_version_id"],
                        "p0b-review-output",
                        "test",
                        "fixture",
                        now,
                    ),
                )
                core._insert_artifact_payload(
                    review_version_id,
                    "review",
                    {
                        "document": {
                            "title": "P0-B TEST 正式内容",
                            "script_text": "这是一段用于验证发布反馈闭环的测试内容。",
                        },
                        "source_boundaries": ["TEST fixture"],
                        "unresolved": [],
                    },
                )
                core._decision(
                    task_id,
                    "review",
                    review_version_id,
                    "approved",
                    "test-user",
                    "user",
                    "P0-B TEST review approved",
                )
                core._set_task(
                    task_id,
                    node="user_final_confirmation",
                    version_id=review_version_id,
                    status="approved",
                    revision=1,
                )
                owned_account_id = f"owned-{domain}"
                core.conn.execute(
                    "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                    (
                        "p0b-audio-delivery",
                        task_id,
                        review_version_id,
                        "test-audio-ref",
                        "test",
                        "fixture",
                        now,
                    ),
                )
                core._audit(
                    task_id,
                    "validation_candidate_used",
                    {
                        "validation_usage_id": "p0b-validation-usage",
                        "task_id": task_id,
                        "content_version_id": review_version_id,
                        "experience_candidate_id": candidate_id,
                        "rationale": "P0-B TEST explicitly selected this candidate for validation",
                        "candidate": {"summary": "P0-B validation candidate"},
                    },
                )
                core.propose_human_decision_carrier(
                    carrier_binding_id="p0b-web-carrier",
                    carrier_kind="web_test",
                    entry_ref="p0b-publication-test",
                    context_strategy="same_test_session",
                    actor="fixture",
                )
                core.validate_human_decision_carrier(
                    carrier_binding_id="p0b-web-carrier",
                    validation_evidence={
                        "inbound_round_trip": True,
                        "outbound_round_trip": True,
                        "same_context_verified": True,
                        "decision_identity_verified": True,
                        "evidence_ref": "p0b-web-publication-test",
                    },
                    actor="fixture",
                    actor_kind="user",
                )
                core.conn.commit()
            finally:
                core.close()

            server = create_action_server(
                "127.0.0.1",
                0,
                data_identity="test",
                database_path=database,
                actor="test-user",
                carrier_binding_id="p0b-web-carrier",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                tasks = _get_json(base_url + "/api/content-tasks")
                self.assertTrue(tasks["ok"])
                self.assertEqual(tasks["tasks"][0]["task_id"], task_id)

                final_confirmation = _post_json(
                    base_url + "/api/action",
                    {
                        "action": "approve_final_content",
                        "task_id": task_id,
                        "reason": "P0-B TEST 最终稿确认",
                    },
                )
                self.assertTrue(final_confirmation["ok"])
                self.assertEqual(final_confirmation["core_result"]["status"], "confirmed")

                publication = _post_json(
                    base_url + "/api/action",
                    {
                        "action": "register_publication",
                        "task_id": task_id,
                        "platform": "test-platform",
                        "external_video_url": "https://example.test/p0b-publication",
                        "published_at": "2026-08-30T10:00:00+08:00",
                        "actual_content_status": "same_as_approved",
                        "actual_content_note": "",
                    },
                )
                self.assertTrue(publication["ok"])
                publication_id = publication["core_result"]["publication_id"]

                missing_before_publication = _post_json(
                    base_url + "/api/action",
                    {
                        "action": "record_publication_observation",
                        "publication_id": "not-registered",
                        "point_code": "P0",
                        "observation_status": "recorded",
                        "metrics": {"value": 1},
                        "source_ref": "test://p0b/P0",
                        "observed_at": "2026-08-30T10:00:00+08:00",
                    },
                )
                self.assertFalse(missing_before_publication["ok"])

                for index in range(8):
                    observation = _post_json(
                        base_url + "/api/action",
                        {
                            "action": "record_publication_observation",
                            "publication_id": publication_id,
                            "point_code": f"P{index}",
                            "observation_status": "recorded",
                            "metrics": {"observed_value": index + 1},
                            "source_ref": f"test://p0b/P{index}",
                            "observed_at": f"2026-08-{30 + index:02d}T10:00:00+08:00",
                        },
                    )
                    self.assertTrue(observation["ok"])

                p7_payload = {
                    "action": "prepare_p7_review",
                    "publication_id": publication_id,
                    "selection_assessment": "P0-B TEST 选题复盘",
                    "narrative_assessment": "P0-B TEST 叙事复盘",
                    "material_assessment": "P0-B TEST 材料复盘",
                    "external_conditions_assessment": "P0-B TEST 外部条件复盘",
                    "feedback_candidate": {
                        "validation_results": [
                            {
                                "experience_candidate_id": candidate_id,
                                "result": "supports",
                                "performance_summary": "TEST 观察结果支持候选假设",
                                "review_basis": "完整 P0-P7 TEST 记录",
                            }
                        ]
                    },
                }
                p7_prepared = _post_json(base_url + "/api/action", p7_payload)
                self.assertTrue(p7_prepared["ok"])
                review_id = p7_prepared["core_result"]["review_id"]

                with self.assertRaises(StateTransitionError):
                    check_core = Stage0ContentProductionCore.open(database, data_identity="test")
                    try:
                        self.assertEqual(
                            check_core.conn.execute(
                                "SELECT COUNT(*) FROM stage0_confirmed_experience"
                            ).fetchone()[0],
                            0,
                        )
                        check_core.promote_experience_candidate(
                            experience_candidate_id=candidate_id,
                            actor="test-user",
                            actor_kind="user",
                            reason="P7 has not been confirmed",
                        )
                    finally:
                        check_core.close()

                p7_confirmed = _post_json(
                    base_url + "/api/action",
                    {
                        "action": "confirm_p7_review",
                        "review_id": review_id,
                        "decision": "confirmed",
                        "reason": "P0-B TEST 确认 P7 复盘",
                    },
                )
                self.assertTrue(p7_confirmed["ok"])

                promotion = _post_json(
                    base_url + "/api/action",
                    {
                        "action": "promote_experience_candidate",
                        "experience_candidate_id": candidate_id,
                        "reason": "P0-B TEST 明确确认支持性真实验证证据",
                    },
                )
                self.assertTrue(promotion["ok"])
                self.assertEqual(promotion["core_result"]["result"]["status"], "promoted")

                final_core = Stage0ContentProductionCore.open(database, data_identity="test")
                try:
                    publication_rows = final_core.list_publication_workbench()
                    self.assertEqual(len(publication_rows), 1)
                    self.assertEqual(
                        {item["point_code"] for item in publication_rows[0]["observations"]},
                        {f"P{index}" for index in range(8)},
                    )
                    review_row = publication_rows[0]["reviews"][0]
                    self.assertEqual(review_row["status"], "confirmed")
                    self.assertEqual(
                        review_row["feedback_candidate_json"]["validation_results"][0]["result"],
                        "supports",
                    )
                    self.assertEqual(
                        final_core.conn.execute(
                            "SELECT COUNT(*) FROM stage0_confirmed_experience"
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    final_core.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
