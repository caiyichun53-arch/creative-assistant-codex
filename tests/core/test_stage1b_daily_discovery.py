from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError, ModelProviderResult, ModelRoute, ModelUsage
from scripts.core.production.stage0_content_core import (
    CoreDiscoveryModelRunMaterializer,
    DataIdentityError,
    FORMAL_DB_PATH,
    ModelGatewayRequiredError,
    Stage0ContentProductionCore,
    StateTransitionError,
    StaleResultError,
)
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService, validate_candidate_judgement


VALID_JUDGEMENT = {
    "outcome": "candidate",
    "title": "被忽略的日常问题",
    "core_question": "这个日常现象为什么容易被误解？",
    "why_attention": "它直接关联普通人的日常选择。",
    "new_angle": "从常见误解与可核对机制之间的差距切入。",
    "material_readiness": "已有真实来源线索；正式研究仍需独立材料。",
    "risk_limits": "来源仅作问题线索，不作为事实证据。",
    "originality_relation": "problem_expansion",
}


class FakeDiscoveryProvider:
    """Explicit test-only provider; it never represents Mimo or a live source."""

    def __init__(self, *, provider_name: str = "test-mimo", output: object = VALID_JUDGEMENT, fail: bool = False) -> None:
        self.provider_name = provider_name
        self.output = output
        self.fail = fail
        self.calls = 0

    def complete(self, request, route):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise RuntimeError("test-only provider failure")
        return ModelProviderResult(
            output_text=self.output if isinstance(self.output, str) else json.dumps(self.output),
            usage=ModelUsage(prompt_tokens=7, completion_tokens=9, total_tokens=16),
            cost={"status": "test-only"},
            metadata={"test_identity": True},
        )


class FakeRouter:
    def __init__(self, route: ModelRoute) -> None:
        self.route = route
        self.routes = {"business_analysis": type("Definition", (), {"fallback": "none", "allowed_task_types": ("topic_screening",)})()}

    def resolve_bound_route(self, route_id: str, *, route_name: str):  # type: ignore[no-untyped-def]
        if route_id != "business_analysis" or route_name != "stage1b.daily_discovery":
            raise RuntimeError("unexpected test route")
        return self.route


class Stage1BDailyDiscoveryTests(unittest.TestCase):
    NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "daily_discovery_test.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        self._install_formal_source_fixture_tables()
        self.provider = FakeDiscoveryProvider()
        self.route = ModelRoute(
            route_name="stage1b.daily_discovery", provider_name=self.provider.provider_name,
            model_name="test-mimo-v1", config_version="test.v1", config_hash="test-config",
            route_id="business_analysis", provider_ref="test_mimo",
        )
        self.router_patch = patch("scripts.core.production.stage0_content_core.ModelRouter.from_file", return_value=FakeRouter(self.route))
        self.router_patch.start()
        self.addCleanup(self.router_patch.stop)
        self.gateway = ModelGateway(
            routes={self.route.route_name: self.route},
            providers={self.provider.provider_name: self.provider},
            materializer=CoreDiscoveryModelRunMaterializer(self.core),
        )
        self.service = Stage1BDailyDiscoveryService(core=self.core, gateway=self.gateway)

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    def _install_formal_source_fixture_tables(self) -> None:
        self.core.conn.executescript(
            """
            CREATE TABLE competitor_accounts (
                account_id TEXT PRIMARY KEY, domain_label TEXT NOT NULL, registration_status TEXT NOT NULL,
                account_name TEXT NOT NULL, source_config_ref TEXT NOT NULL
            );
            CREATE TABLE competitor_videos (
                video_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, title TEXT, url TEXT, publish_time TEXT,
                last_checked_at TEXT NOT NULL, raw_json TEXT NOT NULL, raw_archive_ref TEXT, excluded_reason TEXT
            );
            CREATE TABLE hits (
                hit_id TEXT PRIMARY KEY, video_id TEXT NOT NULL, account_id TEXT NOT NULL, title TEXT, url TEXT,
                publish_time TEXT, promoted_at TEXT NOT NULL, hit_channel TEXT NOT NULL, judgment_confidence TEXT NOT NULL
            );
            """
        )
        self.core.conn.execute("INSERT INTO competitor_accounts VALUES ('social', 'fan_kepu_social_life', 'active', '测试社会生活账号', 'test-source-config')")
        self.core.conn.commit()

    def _insert_video(
        self,
        *,
        video_id: str = "video-1",
        title: str = "常见生活现象为什么容易被误解",
        publish_time: str = "2026-07-13T10:00:00+00:00",
        last_checked_at: str = "2026-07-14T11:00:00+00:00",
    ) -> None:
        self.core.conn.execute(
            "INSERT INTO competitor_videos VALUES (?, 'social', ?, ?, ?, ?, ?, 'test://raw-archive', NULL)",
            (video_id, title, f"https://example.test/{video_id}", publish_time, last_checked_at, json.dumps({"id": video_id, "title": title})),
        )
        self.core.conn.commit()

    def _insert_hit(self, *, hit_id: str, video_id: str, title: str, promoted_at: str = "2026-07-14T11:00:00+00:00") -> None:
        self.core.conn.execute(
            "INSERT INTO hits VALUES (?, ?, 'social', ?, ?, '2026-07-01T10:00:00+00:00', ?, 'formal-trigger', 'formal')",
            (hit_id, video_id, title, f"https://example.test/{video_id}", promoted_at),
        )
        self.core.conn.commit()

    def _run(self, key: str = "daily-1", **kwargs: object) -> dict:
        return self.service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="test-worker",
            idempotency_key=key,
            execution_mode="test_isolated",
            now=self.NOW,
            **kwargs,
        )

    def _first_candidate(self, result: dict) -> dict:
        return self.service.view_daily_snapshot(run_id=result["run_id"])["fan_kepu_social_life"][0]

    def test_no_qualified_source_returns_zero_candidates_with_a_recorded_reason(self) -> None:
        result = self._run()
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
        self.assertEqual(result["summary"]["source_readiness"]["fan_kepu_social_life"]["reason"], "no_qualified_formal_source")
        self.assertEqual(self.provider.calls, 0)

    def test_explicit_single_domain_never_reads_or_snapshots_music_entertainment(self) -> None:
        self._insert_video()
        result = self.service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="test-worker",
            idempotency_key="social-only",
            execution_mode="test_isolated",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
        )
        self.assertEqual(set(result["summary"]["domains"]), {"fan_kepu_social_life"})
        self.assertEqual(set(result["summary"]["source_readiness"]), {"fan_kepu_social_life"})
        snapshot_domains = [
            row[0]
            for row in self.core.conn.execute(
                "SELECT DISTINCT domain_label FROM stage1b_daily_snapshot WHERE run_id=?",
                (result["run_id"],),
            ).fetchall()
        ]
        self.assertEqual(snapshot_domains, ["fan_kepu_social_life"])

    def test_unregistered_or_stale_source_cannot_be_recorded(self) -> None:
        self._insert_video()
        run = self.core.create_discovery_run(discovery_date="2026-07-14", actor="test-worker", execution_mode="test_isolated", idempotency_key="source-run")
        source = self.core.load_real_discovery_sources(domain_label="fan_kepu_social_life", daily_since="2026-07-11T12:00:00+00:00", per_source_limit=1)[0]
        missing = {**source, "source_object_id": "not-registered", "payload": {**source["payload"], "formal_source": {**source["payload"]["formal_source"], "object_id": "not-registered"}}}
        with self.assertRaisesRegex(StateTransitionError, "not registered"):
            self.core.record_discovery_source(run_id=run["run_id"], domain_label="fan_kepu_social_life", idempotency_key="missing", **missing)
        stale = {**source, "source_object_version": "wrong-version", "payload": {**source["payload"], "formal_source": {**source["payload"]["formal_source"], "object_version": "wrong-version"}}}
        with self.assertRaises(StaleResultError):
            self.core.record_discovery_source(run_id=run["run_id"], domain_label="fan_kepu_social_life", idempotency_key="stale", **stale)

    def test_model_route_mismatch_fails_closed(self) -> None:
        self._insert_video()
        wrong_provider = FakeDiscoveryProvider(provider_name="wrong-provider")
        wrong_route = ModelRoute("stage1b.daily_discovery", "wrong-provider", "wrong-model", "wrong.v1", "wrong-config", route_id="business_analysis", provider_ref="wrong")
        service = Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=ModelGateway(routes={wrong_route.route_name: wrong_route}, providers={wrong_provider.provider_name: wrong_provider}, materializer=CoreDiscoveryModelRunMaterializer(self.core)),
        )
        with self.assertRaises(ModelGatewayRequiredError):
            service.run_daily_discovery(discovery_date="2026-07-14", actor="test-worker", idempotency_key="wrong-route", execution_mode="test_isolated", now=self.NOW)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage1b_candidate_version").fetchone()[0], 0)

    def test_model_failure_records_zero_candidate_without_reusing_a_result(self) -> None:
        self._insert_video()
        self.provider.fail = True
        result = self._run("provider-failure")
        self.assertEqual(result["summary"]["filtered"]["fan_kepu_social_life"]["model_gateway_failed"], 1)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage1b_candidate_version").fetchone()[0], 0)
        self.assertEqual(self.core.conn.execute("SELECT reason_code FROM stage1b_candidate_absence").fetchone()[0], "model_gateway_failed")
        self.assertEqual(self.core.conn.execute("SELECT status FROM stage1b_model_run").fetchone()[0], "failed")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(result["status"], "completed_with_failures")

    def test_zero_candidate_output_is_a_valid_recorded_outcome(self) -> None:
        self._insert_video()
        self.provider.output = {"outcome": "no_candidate", "reason": "测试来源不足以形成具体问题"}
        result = self._run("zero-candidate")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
        self.assertEqual(self.core.conn.execute("SELECT reason_code FROM stage1b_candidate_absence").fetchone()[0], "model_returned_no_candidate")

    def test_test_isolated_candidate_is_never_a_formal_candidate_pool_entry(self) -> None:
        self._insert_video()
        candidate = self._first_candidate(self._run())
        self.assertEqual(self.core.get_discovery_candidate(candidate["candidate_version_id"])["status"], "awaiting_user_decision")
        self.assertEqual(self.core.get_discovery_candidate(candidate["candidate_version_id"])["data_identity"], "test")
        self.assertEqual(candidate["execution_mode"], "test_isolated")
        self.assertFalse(candidate["formal_candidate_pool"])
        with self.assertRaisesRegex(StateTransitionError, "production_daily"):
            self.service.handoff_selected_candidate(candidate_version_id=candidate["candidate_version_id"], actor="user", reason="test selection", idempotency_key="test-select")
        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="fixture")

    def test_cooldown_applies_only_to_ordinary_unselected_candidates(self) -> None:
        self._insert_video(video_id="historic-one", title="同一个历史问题", publish_time="2026-07-01T10:00:00+00:00")
        self._insert_hit(hit_id="hit-one", video_id="historic-one", title="同一个历史问题")
        first = self._run("historic-one")
        self.assertEqual(first["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 1)
        self._insert_video(video_id="historic-two", title="同一个历史问题", publish_time="2026-07-01T10:00:00+00:00")
        self._insert_hit(hit_id="hit-two", video_id="historic-two", title="同一个历史问题")
        second = self._run("historic-two")
        self.assertEqual(second["summary"]["filtered"]["fan_kepu_social_life"]["cooldown_active"], 1)
        candidate = self._first_candidate(first)
        self.service.record_user_decision(candidate_version_id=candidate["candidate_version_id"], decision="rejected", actor="user", reason="test user rejection", idempotency_key="reject")
        self._insert_video(video_id="historic-three", title="同一个历史问题", publish_time="2026-07-01T10:00:00+00:00")
        self._insert_hit(hit_id="hit-three", video_id="historic-three", title="同一个历史问题")
        third = self._run("historic-three")
        self.assertEqual(third["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 1)

    def test_test_isolated_selection_cannot_cross_into_stage1a(self) -> None:
        self._insert_video()
        candidate = self._first_candidate(self._run())
        with self.assertRaisesRegex(StateTransitionError, "select through"):
            self.service.record_user_decision(candidate_version_id=candidate["candidate_version_id"], decision="selected", actor="user", reason="select", idempotency_key="wrong-select")
        with self.assertRaisesRegex(StateTransitionError, "production_daily"):
            self.service.handoff_selected_candidate(candidate_version_id=candidate["candidate_version_id"], actor="user", reason="I choose this", idempotency_key="select")

    def test_test_identity_cannot_masquerade_as_production_daily(self) -> None:
        self._insert_video()
        with self.assertRaises(DataIdentityError):
            self.service.run_daily_discovery(
                discovery_date="2026-07-14", actor="test-worker", idempotency_key="wrong-mode",
                execution_mode="production_daily", now=self.NOW, domains=("fan_kepu_social_life",),
            )

    def test_core_rejects_business_ranking_fields(self) -> None:
        self._insert_video()
        run = self.core.create_discovery_run(discovery_date="2026-07-14", actor="test-worker", execution_mode="test_isolated", idempotency_key="rank-run")
        source = self.core.load_real_discovery_sources(domain_label="fan_kepu_social_life", daily_since="2026-07-11T12:00:00+00:00", per_source_limit=1)[0]
        stored = self.core.record_discovery_source(run_id=run["run_id"], domain_label="fan_kepu_social_life", idempotency_key="rank-source", **source)
        self.core.record_discovery_filter(source_version_id=stored["source_version_id"], outcome="eligible", reason_code="eligible", detail={}, idempotency_key="rank-filter")
        assembly = self.core.create_discovery_input_assembly(run_id=run["run_id"], source_version_id=stored["source_version_id"], payload={"source": source["payload"]}, prompt_version="test", skill_version="test", idempotency_key="rank-assembly")
        model_result = self.gateway.complete(self.core.prepare_discovery_model_request(run_id=run["run_id"], source_version_id=stored["source_version_id"], assembly_id=assembly["assembly_id"], prompt="test prompt"))
        with self.assertRaisesRegex(StateTransitionError, "business-ranking"):
            self.core.create_discovery_candidate(run_id=run["run_id"], source_version_id=stored["source_version_id"], model_run_id=model_result.envelope_version_id, candidate_id="rank-candidate", payload={"title": "test", "score": 99}, idempotency_key="rank-candidate")

    def test_user_facing_fields_must_be_natural_chinese(self) -> None:
        english = {**VALID_JUDGEMENT, "title": "A daily problem"}
        with self.assertRaisesRegex(Exception, "natural Chinese"):
            validate_candidate_judgement(english)

    def test_social_life_filter_excludes_programming_engineering_rockets_materials_and_music(self) -> None:
        for index, title in enumerate(("Python 编程入门", "火箭发动机工程技术", "新型材料技术突破", "歌手音乐专辑盘点")):
            with self.subTest(title=title):
                self._insert_video(video_id=f"excluded-{index}", title=title)
                result = self._run(f"excluded-{index}")
                self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
                self.assertEqual(result["summary"]["filtered"]["fan_kepu_social_life"]["outside_social_life_domain"], index + 1)

    def test_zero_candidate_remains_valid_when_batch_times_out_before_any_request(self) -> None:
        self._insert_video()
        result = self._run("batch-timeout", batch_timeout_seconds=0)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.core.conn.execute("SELECT lifecycle_status FROM stage1b_run_execution_context").fetchone()[0], "timed_out")


if __name__ == "__main__":
    unittest.main()
