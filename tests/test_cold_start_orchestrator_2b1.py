"""Isolation checks for the 2B-1 cold-start orchestrator boundary."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.runtime.runtime_storage import runtime_path
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.cold_start_orchestrator import ColdStartExecutionOrchestrator
from scripts.core.production.high_signal_policy import (
    build_historical_collection_artifact,
    build_high_signal_artifact,
)
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
    CompetitorRegistrationService,
)


class FakeContentBranchExecutor:
    def __init__(
        self,
        *,
        fail_high_signal_once_for: set[str] | None = None,
        fail_breakdown_once_for: set[str] | None = None,
        core: Stage0ContentProductionCore | None = None,
    ) -> None:
        self.fail_high_signal_once_for = set(fail_high_signal_once_for or set())
        self.fail_breakdown_once_for = set(fail_breakdown_once_for or set())
        self.core = core
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, step_name, registration, completed_artifacts):
        registration_id = str(registration["registration_id"])
        self.calls.append((registration_id, step_name))
        if step_name == "historical_material":
            evaluated_at = int(time.time())
            items = [
                {
                    "source_id": f"{registration_id}-video-{index}",
                    "platform": "douyin",
                    "url": f"https://www.douyin.com/video/{registration_id}-{index}",
                    "title": f"#领域话题{index % 2} 第{index}条内容",
                    "author": registration_id,
                    "published_at": evaluated_at - (8 * 86400) - index * 3600,
                    "duration_seconds": 30,
                    "metrics": {
                        "like_count": 10,
                        "comment_count": 2,
                        "share_count": 0,
                        "collect_count": 0,
                    },
                }
                for index in range(20)
            ]
            return (
                build_historical_collection_artifact(
                    platform="douyin",
                    account_source_ref=str(registration["external_account_ref"]),
                    items=items,
                    raw_archive_ref=f"isolated://{registration_id}/history",
                    command_hash="history-command",
                    output_hash="history-output",
                    evaluated_at=evaluated_at,
                ),
            )
        if step_name == "high_signal_identification":
            if registration_id in self.fail_high_signal_once_for:
                self.fail_high_signal_once_for.remove(registration_id)
                raise RuntimeError("isolated high-signal failure")
            historical = next(
                item["artifact_refs"][0]
                for item in completed_artifacts
                if item["step_name"] == "historical_material"
            )
            return (build_high_signal_artifact(historical["items"], evaluated_at=historical["evaluated_at"]),)
        if step_name == "transcripts_and_comments":
            if self.core is not None:
                high_signal = next(
                    item["artifact_refs"][0]
                    for item in completed_artifacts
                    if item["step_name"] == "high_signal_identification"
                )
                selected_items = [
                    item for item in high_signal.get("selected_items", [])
                    if isinstance(item, dict) and str(item.get("source_id") or "").strip()
                ]
                for item in selected_items:
                    source_id = str(item["source_id"])
                    transcript = "test retained transcript evidence"
                    transcript_path = runtime_path(
                        "cold_start_test_materials",
                        registration_id,
                        f"{source_id}.txt",
                        data_identity="test",
                    )
                    transcript_path.parent.mkdir(parents=True, exist_ok=True)
                    transcript_path.write_text(transcript, encoding="utf-8")
                    self.core.record_competitor_registration_item(
                        registration_id=registration_id,
                        step_name="transcripts_and_comments",
                        item_ref=source_id,
                        status="completed",
                        artifact={
                            "artifact_kind": "transcript_and_comments",
                            "source_id": source_id,
                            "transcript_ref": str(transcript_path),
                            "transcript": transcript,
                            "metrics": dict(item.get("metrics") or {}),
                            "comments": [],
                        },
                        error=None,
                    )
            return ({"artifact_kind": "isolated_prepared_materials", "selected_count": 1},)
        if step_name == "breakdown":
            if registration_id in self.fail_breakdown_once_for:
                self.fail_breakdown_once_for.remove(registration_id)
                raise RuntimeError("isolated breakdown failure")
            if self.core is not None:
                materials = [
                    item for item in self.core.list_competitor_registration_items(
                        registration_id=registration_id,
                        step_name="transcripts_and_comments",
                    )
                    if item["status"] == "completed"
                ]
                for item in materials:
                    source_id = str(item["item_ref"])
                    deep_breakdown = {
                        "source_id": source_id,
                        "source_content_type": "人物经历故事",
                        "analysis_text": "WHAT\n测试材料的核心对象和命题。\nHOW\n测试材料中的推进动作及其关系。\nSO WHAT\n无有效复用参考。",
                        "boundary_observation": "test evidence shows a concrete subject and its change or impact",
                        "schema_version": "competitor_breakdown.output.raw.v5",
                    }
                    raw_output = json.dumps(deep_breakdown, ensure_ascii=False, sort_keys=True)
                    self.core.record_competitor_breakdown_attempt(
                        registration_id=registration_id,
                        source_id=source_id,
                        attempt_kind="initial",
                        outcome="completed",
                        raw_model_output=raw_output,
                        raw_model_output_status="available",
                        model_run_id=f"test-breakdown-{registration_id}-{source_id}",
                    )
                    self.core.record_competitor_registration_item(
                        registration_id=registration_id,
                        step_name="breakdown",
                        item_ref=source_id,
                        status="completed",
                        artifact={
                            "artifact_kind": "deep_breakdown",
                            "source_id": source_id,
                            "model_run_id": f"test-breakdown-{registration_id}-{source_id}",
                            "raw_model_output": raw_output,
                            "deep_breakdown": deep_breakdown,
                        },
                        error=None,
                    )
                return ({"artifact_kind": "isolated_breakdown", "selected_count": len(materials)},)
            return ({
                "artifact_kind": "isolated_breakdown",
                "selected_count": 1,
                "forbidden_tag_from_breakdown": "#拆解不应生成标签",
            },)
        raise AssertionError(f"2B-1 must not execute the tag branch: {step_name}")


class FirstDeepFailureExecutor(FakeContentBranchExecutor):
    """Fail the first deep step after every account has finished selection."""

    def __init__(self) -> None:
        super().__init__()
        self.first_deep_failure_seen = False
        self.basic_accounts_before_failure: set[str] = set()

    def execute(self, *, step_name, registration, completed_artifacts):
        registration_id = str(registration["registration_id"])
        if step_name == "breakdown" and not self.first_deep_failure_seen:
            self.first_deep_failure_seen = True
            self.calls.append((registration_id, step_name))
            self.basic_accounts_before_failure = {
                current_id
                for current_id, current_step in self.calls
                if current_step == "high_signal_identification"
            }
            raise RuntimeError("isolated first deep step is still waiting")
        return super().execute(
            step_name=step_name,
            registration=registration,
            completed_artifacts=completed_artifacts,
        )


class NoHitDeepGateExecutor(FakeContentBranchExecutor):
    """Make one account produce no hits and record the deep payload sizes."""

    def __init__(self, no_hit_registration_id: str) -> None:
        super().__init__()
        self.no_hit_registration_id = no_hit_registration_id
        self.transcript_selected_counts: dict[str, int] = {}
        self.breakdown_input_counts: dict[str, int] = {}

    def execute(self, *, step_name, registration, completed_artifacts):
        registration_id = str(registration["registration_id"])
        if step_name == "historical_material" and registration_id == self.no_hit_registration_id:
            history = super().execute(
                step_name=step_name,
                registration=registration,
                completed_artifacts=completed_artifacts,
            )[0]
            zero_items = []
            for item in history["items"]:
                zero_item = dict(item)
                zero_item["metrics"] = {
                    metric: 0 for metric in (item.get("metrics") or {})
                }
                zero_items.append(zero_item)
            return (
                build_historical_collection_artifact(
                    platform=str(history["platform"]),
                    account_source_ref=str(history["account_source_ref"]),
                    items=zero_items,
                    raw_archive_ref=str(history["raw_archive_ref"]),
                    command_hash=str(history["command_hash"]),
                    output_hash=str(history["output_hash"]),
                    evaluated_at=int(history["evaluated_at"]),
                ),
            )
        if step_name == "transcripts_and_comments":
            high_signal = next(
                item["artifact_refs"][0]
                for item in completed_artifacts
                if item["step_name"] == "high_signal_identification"
            )
            selected = high_signal["selected_items"]
            self.transcript_selected_counts[registration_id] = len(selected)
            return ({
                "artifact_kind": "isolated_prepared_materials",
                "selected_count": len(selected),
            },)
        if step_name == "breakdown":
            self.breakdown_input_counts[registration_id] = self.transcript_selected_counts.get(
                registration_id, -1
            )
            return ({
                "artifact_kind": "isolated_breakdown",
                "selected_count": self.breakdown_input_counts[registration_id],
            },)
        return super().execute(
            step_name=step_name,
            registration=registration,
            completed_artifacts=completed_artifacts,
        )


class ColdStartOrchestrator2B1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection, db_path=Path(":memory:"), data_identity="test"
        )
        self.core.install_schema()
        self.onboarding = ColdStartOnboardingService(
            core=self.core, config_dir=self.config_dir,
        )
        self.adapter = ColdStartHumanDecisionAdapter(
            core=self.core, config_dir=self.config_dir,
        )
        self.core.propose_human_decision_carrier(
            carrier_binding_id="isolated-2b2-carrier",
            carrier_kind="isolated_test",
            entry_ref="2b2_test",
            context_strategy="same_test_session",
            actor="隔离测试",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id="isolated-2b2-carrier",
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "isolated-2b2-round-trip",
            },
            actor="隔离测试",
            actor_kind="user",
        )
        payload = {
            "domain_mode": "create",
            "domain_name": "2B-1隔离领域",
            "platform": "douyin",
            "owned_account": {
                "display_name": "自营账号",
                "external_account_ref": "douyin:2b1-owned",
            },
            "competitor_accounts": [
                {
                    "display_name": f"对标账号{index}",
                    "external_account_ref": f"douyin:2b1-competitor-{index}",
                }
                for index in range(20)
            ],
            "actor": "隔离测试",
        }
        preview = self.onboarding.preview(payload)
        result = self.onboarding.confirm(payload)
        self.cold_start_id = str(result["cold_start_id"])

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    def _run(
        self,
        executor: FakeContentBranchExecutor,
        *,
        events: list[dict] | None = None,
    ) -> dict:
        service = CompetitorRegistrationService(core=self.core, executor=executor)
        with patch("scripts.core.production.stage0_content_core.record_runtime_guard_event"):
            return ColdStartExecutionOrchestrator(
                core=self.core,
                registration_service=service,
                progress_callback=(events.append if events is not None else None),
            ).run(
                cold_start_id=self.cold_start_id,
                actor="隔离测试",
                idempotency_key="isolated-2b1-orchestrator",
            )

    def test_each_phase_emits_one_aggregate_summary_from_formal_results(self) -> None:
        events: list[dict] = []
        self._run(FakeContentBranchExecutor(), events=events)
        summaries = [event for event in events if event.get("event") == "phase_summary"]
        self.assertEqual(
            [event["phase"] for event in summaries],
            ["historical_collection", "baseline_high_signal", "preparation", "breakdown"],
        )
        historical, baseline, preparation, breakdown = [
            event["summary"] for event in summaries
        ]
        self.assertEqual(historical["account_total"], 20)
        self.assertEqual(historical["success_accounts"], 20)
        self.assertEqual(historical["historical_items"], 400)
        self.assertEqual(baseline["historical_items"], 400)
        self.assertGreaterEqual(baseline["high_signal_items"], 0)
        self.assertEqual(
            preparation["prepared_items"], preparation["transcript_items"]
        )
        self.assertGreaterEqual(breakdown["success_items"], 0)

    def test_resume_emits_recovery_summary_without_replaying_completed_phases(self) -> None:
        self._run(FakeContentBranchExecutor())
        self.core.conn.execute(
            "UPDATE stage0_cold_start SET status='running' WHERE cold_start_id=?",
            (self.cold_start_id,),
        )
        events: list[dict] = []
        self._run(FakeContentBranchExecutor(), events=events)
        self.assertEqual(
            [event.get("event") for event in events if event.get("event") == "resume_summary"],
            ["resume_summary"],
        )
        self.assertEqual(
            [event for event in events if event.get("event") == "phase_summary"],
            [],
        )


    def _prepare_breakdown_subject(self) -> tuple[dict, dict, ConfiguredCompetitorRegistrationExecutor]:
        registration_id = str(
            self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? ORDER BY registration_id LIMIT 1",
                (self.cold_start_id,),
            ).fetchone()["registration_id"]
        )
        service = CompetitorRegistrationService(
            core=self.core,
            executor=FakeContentBranchExecutor(),
        )
        with patch("scripts.core.production.stage0_content_core.record_runtime_guard_event"):
            for index, step in enumerate(
                ("historical_material", "high_signal_identification", "transcripts_and_comments"),
                start=1,
            ):
                service.run_competitor_registration_step(
                    registration_id=registration_id,
                    actor="隔离测试",
                    idempotency_key=f"step6-subject:{index}:{step}",
                )
        registration = self.core.get_competitor_registration(registration_id=registration_id)
        source_id = f"{registration_id}-step6-source"
        transcript_path = Path(self.temp_dir.name) / f"{source_id}.txt"
        transcript_path.write_text("完整口播文案", encoding="utf-8")
        material = {
            "artifact_kind": "transcript_and_comments",
            "source_id": source_id,
            "transcript_ref": str(transcript_path),
            "metrics": {"like_count": 10},
            "comments": [],
        }
        self.core.conn.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'transcripts_and_comments', ?, 'completed', ?, '{}', 1, ?, ?)",
            (
                registration_id,
                source_id,
                json.dumps(material, ensure_ascii=False),
                self.core.data_identity,
                "2026-08-23T00:00:00+08:00",
            ),
        )
        self.core.conn.commit()
        executor = object.__new__(ConfiguredCompetitorRegistrationExecutor)
        executor.core = self.core
        executor.progress_callback = None
        executor.on_material_change = None
        return registration, material, executor

    @staticmethod
    def _breakdown_value(source_id: str, *, valid_core: bool = True) -> dict:
        return {
            "source_id": source_id,
            "source_content_type": "person/story",
            "analysis_text": (
                "WHAT\n完整核心拆解的对象和命题。\n"
                "HOW\n完整核心拆解的推进动作及其关系。\n"
                "SO WHAT\n无有效复用参考。"
                if valid_core else ""
            ),
            "schema_version": "competitor_breakdown.output.raw.v4",
            "question_expansions": [{"core_question": "", "content_type": "", "reason": ""}],
            "expansion_signals": {},
        }

    def test_step6_core_contract_and_failed_resume_semantics(self) -> None:
        registration, material, _ = self._prepare_breakdown_subject()
        registration = {**registration, "status": "processing"}
        source_id = str(material["source_id"])

        invalid = self._breakdown_value(source_id, valid_core=False)
        valid = self._breakdown_value(source_id, valid_core=True)
        valid["question_expansions"] = []
        valid.pop("expansion_signals", None)
        submissions = [
            {
                "execution_id": "external-invalid-1",
                "executor_id": "isolated-executor",
                "model_ref": "reported-model",
                "output": invalid,
            },
            {
                "execution_id": "external-valid-1",
                "executor_id": "isolated-executor",
                "model_ref": "reported-model",
                "output": valid,
            },
            {
                "execution_id": "external-invalid-2",
                "executor_id": "isolated-executor",
                "model_ref": "reported-model",
                "output": invalid,
            },
            {
                "execution_id": "external-valid-2",
                "executor_id": "isolated-executor",
                "model_ref": "reported-model",
                "output": valid,
            },
        ]
        submitted_tasks: list[dict] = []

        def submit(task: dict) -> dict:
            submitted_tasks.append(task)
            return submissions.pop(0)

        executor = ConfiguredCompetitorRegistrationExecutor(
            core=self.core,
            collector=None,
            transcriber=None,
            media_materializer=None,
            external_executor=submit,
        )

        with self.assertRaises(Exception):
            executor.process_prepared_breakdown(
                registration=registration,
                material=material,
            )
        failed = next(
            item for item in self.core.list_competitor_registration_items(
                registration_id=str(registration["registration_id"]), step_name="breakdown"
            ) if item["item_ref"] == source_id
        )
        self.assertEqual(failed["status"], "failed")

        completed = executor.process_prepared_breakdown(
            registration=registration,
            material=material,
        )
        self.assertEqual(completed["deep_breakdown"]["source_id"], source_id)
        stored = next(
            item for item in self.core.list_competitor_registration_items(
                registration_id=str(registration["registration_id"]), step_name="breakdown"
            ) if item["item_ref"] == source_id
        )
        self.assertEqual(stored["status"], "completed")

        with self.assertRaises(Exception):
            executor.process_prepared_breakdown(
                registration=registration,
                material=material,
            )
        failed_again = next(
            item for item in self.core.list_competitor_registration_items(
                registration_id=str(registration["registration_id"]), step_name="breakdown"
            ) if item["item_ref"] == source_id
        )
        self.assertEqual(failed_again["status"], "failed")

        resumed = executor._breakdown(
            registration,
            ({"step_name": "transcripts_and_comments", "artifact_refs": [material]},),
        )
        self.assertEqual(len(resumed), 1)
        retried = next(
            item for item in self.core.list_competitor_registration_items(
                registration_id=str(registration["registration_id"]), step_name="breakdown"
            ) if item["item_ref"] == source_id
        )
        self.assertEqual(retried["status"], "completed")

        submitted_count = len(submitted_tasks)
        executor._breakdown(
            registration,
            ({"step_name": "transcripts_and_comments", "artifact_refs": [material]},),
        )
        self.assertEqual(len(submitted_tasks), submitted_count)
        self.assertEqual(submissions, [])

    def _review_current_library(self, *, command_id: str) -> dict:
        library = self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)
        self.assertIsNotNone(library)
        candidates = list(library["candidates"])
        command = FormalHumanDecisionCommand(
            command_id=command_id,
            carrier_binding_id="isolated-2b2-carrier",
            session_ref="isolated-2b2-session",
            action="review_competitor_tag_library",
            target_ref=f"cold_start:{self.cold_start_id}",
            payload={
                "cold_start_id": self.cold_start_id,
                "decisions": [
                    {
                        "tag_id": item["tag_id"],
                        "decision": "accepted",
                        "edited_tag": item["tag"],
                    }
                    for item in candidates
                ],
                "reason": "隔离审核",
            },
            actor="隔离测试",
        )
        return self.adapter.review_tag_library(command=command)

    def test_no_hit_account_sends_no_material_into_deep_work(self) -> None:
        target_id = str(
            self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? ORDER BY registration_id LIMIT 1",
                (self.cold_start_id,),
            ).fetchone()["registration_id"]
        )
        executor = NoHitDeepGateExecutor(target_id)
        result = self._run(executor)

        self.assertTrue(result["tag_input_ready"])
        self.assertEqual(executor.transcript_selected_counts[target_id], 0)
        self.assertEqual(executor.breakdown_input_counts[target_id], 0)

    def test_all_stage_boundaries_are_strict_before_breakdown(self) -> None:
        executor = FirstDeepFailureExecutor()
        result = self._run(executor)

        self.assertTrue(executor.first_deep_failure_seen)
        self.assertEqual(len(executor.basic_accounts_before_failure), 20)
        self.assertTrue(result["tag_input_ready"])
        self.assertEqual(result["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(result["breakdown_progress"], {"completed": 19, "total": 20})
        self.assertEqual(len(result["failures"]), 1)
        self.assertTrue(result["tag_branch"]["generation_started"])

        first_deep_index = min(
            index for index, (_, step) in enumerate(executor.calls)
            if step in {"transcripts_and_comments", "breakdown"}
        )
        last_basic_index = max(
            index for index, (_, step) in enumerate(executor.calls)
            if step == "high_signal_identification"
        )
        self.assertLess(last_basic_index, first_deep_index)

        history_indices = [
            index for index, (_, step) in enumerate(executor.calls)
            if step == "historical_material"
        ]
        screening_indices = [
            index for index, (_, step) in enumerate(executor.calls)
            if step == "high_signal_identification"
        ]
        preparation_indices = [
            index for index, (_, step) in enumerate(executor.calls)
            if step == "transcripts_and_comments"
        ]
        breakdown_indices = [
            index for index, (_, step) in enumerate(executor.calls)
            if step == "breakdown"
        ]
        self.assertLess(max(history_indices), min(screening_indices))
        self.assertLess(max(screening_indices), min(preparation_indices))
        self.assertLess(max(preparation_indices), min(breakdown_indices))

        registration_ids = [
            str(row["registration_id"])
            for row in self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? ORDER BY created_at, registration_id",
                (self.cold_start_id,),
            ).fetchall()
        ]
        for registration_id in registration_ids:
            account_steps = [
                step for current_id, step in executor.calls if current_id == registration_id
            ]
            self.assertEqual(
                account_steps,
                [
                    "historical_material",
                    "high_signal_identification",
                    "transcripts_and_comments",
                    "breakdown",
                ],
            )

    def test_tag_handoff_is_ready_at_twenty_of_twenty_before_tag_step(self) -> None:
        executor = FakeContentBranchExecutor()
        result = self._run(executor)

        self.assertTrue(result["tag_input_ready"])
        self.assertEqual(result["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(result["breakdown_progress"], {"completed": 20, "total": 20})
        self.assertTrue(result["tag_branch"]["generation_started"])
        self.assertFalse(result["tag_branch"]["human_review_started"])
        self.assertEqual(result["tag_branch"]["accounts_waiting_at_existing_tag_step"], 0)
        self.assertNotIn("tag_candidates", [step for _, step in executor.calls])
        status = self.core.get_cold_start_orchestration_status(cold_start_id=self.cold_start_id)
        self.assertTrue(status["tag_input_ready"])
        self.assertFalse(status["tag_branch_waits_for_breakdown"])
        self.assertEqual(status["tag_candidate_step_completed_count"], 0)
        library = self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)
        self.assertIsNotNone(library)
        self.assertEqual(library["status"], "awaiting_human_review")
        self.assertEqual(library["retained_count"], 2)
        self.assertEqual(library["candidates"][0]["source_count"], 200)
        self.assertNotIn(
            "拆解不应生成标签",
            [item["tag"] for item in library["candidates"]],
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (self.cold_start_id,),
            ).fetchone()["status"],
            "running",
        )

        restarted = self._run(FakeContentBranchExecutor())
        self.assertTrue(restarted["tag_input_ready"])
        self.assertTrue(restarted["tag_branch"]["generation_started"])
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) AS count FROM stage0_cold_start_tag_library "
                "WHERE cold_start_id=? AND data_identity=?",
                (self.cold_start_id, self.core.data_identity),
            ).fetchone()["count"]),
            1,
        )

    def test_nineteen_of_twenty_does_not_build_library(self) -> None:
        registration_id = str(
            self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? ORDER BY registration_id LIMIT 1",
                (self.cold_start_id,),
            ).fetchone()["registration_id"]
        )
        result = self._run(FakeContentBranchExecutor(fail_high_signal_once_for={registration_id}))
        self.assertFalse(result["tag_input_ready"])
        self.assertEqual(result["selection_progress"], {"completed": 19, "total": 20})
        self.assertFalse(result["tag_branch"]["generation_started"])
        self.assertIsNone(self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id))

    def test_breakdown_failure_does_not_remove_ready_tag_input_and_can_resume(self) -> None:
        registration_id = str(
            self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? ORDER BY registration_id LIMIT 1",
                (self.cold_start_id,),
            ).fetchone()["registration_id"]
        )
        first_executor = FakeContentBranchExecutor(fail_breakdown_once_for={registration_id})
        first = self._run(first_executor)
        self.assertTrue(first["tag_input_ready"])
        self.assertEqual(first["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(first["breakdown_progress"], {"completed": 19, "total": 20})
        self.assertEqual(len(first["failures"]), 1)
        self.assertEqual(first["failures"][0]["registration_id"], registration_id)
        self.assertTrue(first["tag_branch"]["generation_started"])

        reviewed = self._review_current_library(command_id="isolated-2b2-parallel-review")
        self.assertEqual(reviewed["status"], "accepted")

        second = self._run(FakeContentBranchExecutor())
        self.assertTrue(second["tag_input_ready"])
        self.assertEqual(second["breakdown_progress"], {"completed": 20, "total": 20})
        self.assertNotIn("tag_candidates", [step for _, step in first_executor.calls])
        failure_audit = self.core.conn.execute(
            "SELECT COUNT(*) AS count FROM stage0_audit_event "
            "WHERE action='cold_start_registration_step_failed'"
        ).fetchone()
        self.assertEqual(int(failure_audit["count"]), 1)

    def test_existing_human_entry_reviews_one_whole_library_without_completion(self) -> None:
        result = self._run(FakeContentBranchExecutor())
        library = self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)
        self.assertTrue(result["tag_input_ready"])
        self.assertIsNotNone(library)
        candidates = list(library["candidates"])
        decisions = [
            {
                "tag_id": item["tag_id"],
                "decision": "rejected" if index == 0 else "accepted",
                "edited_tag": "" if index == 0 else item["tag"],
            }
            for index, item in enumerate(candidates)
        ]
        command = FormalHumanDecisionCommand(
            command_id="isolated-2b2-tag-review",
            carrier_binding_id="isolated-2b2-carrier",
            session_ref="isolated-2b2-session",
            action="review_competitor_tag_library",
            target_ref=f"cold_start:{self.cold_start_id}",
            payload={
                "cold_start_id": self.cold_start_id,
                "decisions": decisions,
                "reason": "隔离审核",
            },
            actor="隔离测试",
        )
        reviewed = self.adapter.review_tag_library(command=command)
        self.assertEqual(reviewed["status"], "accepted")
        self.assertEqual(reviewed["reviewed_count"], len(candidates))
        self.assertEqual(reviewed["rejected_count"], 1)
        self.assertEqual(
            self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)["status"],
            "accepted",
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (self.cold_start_id,),
            ).fetchone()["status"],
            "running",
        )
        command_status = self.core.conn.execute(
            "SELECT status FROM stage0_human_decision_command WHERE command_id=?",
            (command.command_id,),
        ).fetchone()["status"]
        self.assertEqual(command_status, "completed")
        replay = self.adapter.review_tag_library(command=command)
        self.assertEqual(replay["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
