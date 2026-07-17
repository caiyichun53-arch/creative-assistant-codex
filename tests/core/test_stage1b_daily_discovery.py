from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.run_domain_search import seed_active_tags_from_sources_yaml
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
from scripts.core.production.stage1b_daily_discovery import (
    DailyDiscoveryValidationError,
    Stage1BDailyDiscoveryService,
    parse_candidate_judgement_output,
    validate_candidate_judgement,
)


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


VALID_SOURCE_TO_TOPIC_OUTPUT = {
    "topic_status": "generated_good_candidate",
    "candidate_topic": "养老服务为什么影响普通家庭的日常选择",
    "topic_angle": "生活机制解释",
    "core_question": "养老服务为什么影响普通家庭的日常选择",
    "audience_relation": "它直接关系到普通家庭的照护安排和消费判断。",
    "content_increment": "从服务供给、家庭决策和现实约束之间的关系切入。",
    "supporting_evidence": ["养老服务为什么引发普通家庭讨论"],
    "source_constraints": ["来源仅作发现线索，不作为事实证据。"],
    "no_result_reason": "none",
    "confidence": "high",
    "angle_discovery": {
        "problem_angle": {"found": True, "direction": "养老服务影响家庭选择", "reason": "输入材料包含具体生活问题。"},
        "audience_relevance_angle": {"found": True, "direction": "普通家庭照护压力", "reason": "与目标受众生活经验相关。"},
        "content_increment_angle": {"found": True, "direction": "解释服务与家庭决策关系", "reason": "不只复述热点标题。"},
        "tension_angle": {"found": True, "direction": "需求增长与服务理解不足", "reason": "存在现实张力。"},
        "distinct_angle": {"found": True, "direction": "从家庭选择机制切入", "reason": "不同于纯新闻复述。"},
        "producible_angle": {"found": True, "direction": "可做单条口播", "reason": "有核心问题。"},
        "durable_value_angle": {"found": True, "direction": "热点后仍有生活解释价值", "reason": "不只依赖实时热度。"},
    },
    "candidate_selection": {
        "selected_direction": "养老服务影响家庭选择",
        "why_selected": "受众关系和内容增量最清楚。",
        "rejected_directions": [],
    },
    "risks": ["不得声称未研究过的政策事实。"],
    "material_gaps": ["正式研究仍需补充政策与案例材料。"],
    "user_review_required": False,
    "user_review_reasons": [],
    "execution_review": {
        "used_only_supplied_material": True,
        "did_not_search_by_itself": True,
        "did_not_invent_facts": True,
        "respected_domain_boundary": True,
        "respected_risk_boundary": True,
        "did_not_force_candidate": True,
        "no_score_rank_weight": True,
    },
    "experience_usage": {"used_experience_ids": [], "unused_experience_ids": [], "rationale": "测试不引用经验。"},
    "schema_version": "source_to_topic.output.v1",
}


class FakeDiscoveryProvider:
    """Explicit test-only provider; it never represents Mimo or a live source."""

    def __init__(self, *, provider_name: str = "test-mimo", output: object = VALID_SOURCE_TO_TOPIC_OUTPUT, fail: bool = False) -> None:
        self.provider_name = provider_name
        self.output = output
        self.fail = fail
        self.calls = 0

    def complete(self, request, route):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise RuntimeError("test-only provider failure")
        output = self.output
        if isinstance(output, dict):
            output = dict(output)
            evidence = list(request.input_payload.get("source_evidence_refs") or [])
            if output.get("topic_status") != "no_result" and evidence:
                output["supporting_evidence"] = evidence[:2]
        return ModelProviderResult(
            output_text=output if isinstance(output, str) else json.dumps(output),
            usage=ModelUsage(prompt_tokens=7, completion_tokens=9, total_tokens=16),
            cost={"status": "test-only"},
            metadata={"test_identity": True},
        )


class FakeRouter:
    def __init__(self, route: ModelRoute) -> None:
        self.route = route
        self.routes = {"business_analysis": type("Definition", (), {"fallback": "none", "allowed_task_types": ("topic_screening",)})()}

    def resolve(self, route_id: str, *, route_name: str | None = None, **kwargs: object):  # type: ignore[no-untyped-def]
        if route_id != "business_analysis" or route_name != "business.source_to_topic":
            raise RuntimeError("unexpected test route")
        return self.route

    def resolve_bound_route(self, route_id: str, *, route_name: str):  # type: ignore[no-untyped-def]
        if route_id != "business_analysis" or route_name != "business.source_to_topic":
            raise RuntimeError("unexpected test route")
        return self.route


class NoopProductionAcquirer:
    def collect_hotspots(self, *, discovery_run_id: str, now: datetime, deadline_monotonic: float | None = None) -> dict:
        return {"status": "completed", "item_count": 0}

    def search_tags(self, *, discovery_run_id: str, domain: str, now: datetime, deadline_monotonic: float | None = None) -> dict:
        return {"status": "completed", "selected_tag_count": 0, "results": [], "failed": 0}


class Stage1BDailyDiscoveryTests(unittest.TestCase):
    NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "daily_discovery_test.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        self._install_formal_source_fixture_tables()
        self.provider = FakeDiscoveryProvider()
        self.route = ModelRoute(
            route_name="business.source_to_topic", provider_name=self.provider.provider_name,
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
        if hasattr(self, "production_core"):
            self.production_core.close()
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

    def _insert_hotspot(
        self,
        *,
        observation_id: str = "hotspot-1",
        title: str = "养老服务为什么引发普通家庭讨论",
        seed_search_tag: bool = True,
    ) -> None:
        if seed_search_tag:
            seed_active_tags_from_sources_yaml(
                self.core.conn,
                {"domain_label": "fan_kepu_social_life", "active_tags": ["养老"]},
            )
        self.core.conn.execute(
            """
            INSERT INTO trendradar_collection_run(
                collection_run_id, discovery_run_id, status, item_count, command_hash, started_at, completed_at
            ) VALUES ('tr-run', 'upstream-run', 'completed', 1, 'hash', ?, ?)
            """,
            (self.NOW.isoformat(), self.NOW.isoformat()),
        )
        self.core.conn.execute(
            """
            INSERT INTO trendradar_hotspot_observation(
                observation_id, provider_item_id, title, url, source_channel, source_rank,
                observed_at, raw_json, collection_run_id
            ) VALUES (?, 'provider-1', ?, 'https://example.test/hotspot', 'news', 1, ?, ?, 'tr-run')
            """,
            (observation_id, title, self.NOW.isoformat(), json.dumps({"id": "provider-1", "title": title})),
        )
        self.core.conn.commit()

    def _insert_hotspots(self, count: int) -> None:
        seed_active_tags_from_sources_yaml(
            self.core.conn,
            {"domain_label": "fan_kepu_social_life", "active_tags": ["养老"]},
        )
        self.core.conn.execute(
            """
            INSERT INTO trendradar_collection_run(
                collection_run_id, discovery_run_id, status, item_count, command_hash, started_at, completed_at
            ) VALUES ('tr-run-many', 'upstream-run-many', 'completed', ?, 'hash-many', ?, ?)
            """,
            (count, self.NOW.isoformat(), self.NOW.isoformat()),
        )
        for index in range(count):
            title = f"养老服务为什么影响普通家庭日常选择 {index}"
            self.core.conn.execute(
                """
                INSERT INTO trendradar_hotspot_observation(
                    observation_id, provider_item_id, title, url, source_channel, source_rank,
                    observed_at, raw_json, collection_run_id
                ) VALUES (?, ?, ?, ?, 'news', ?, ?, ?, 'tr-run-many')
                """,
                (
                    f"hotspot-many-{index}",
                    f"provider-many-{index}",
                    title,
                    f"https://example.test/hotspot-many-{index}",
                    index + 1,
                    self.NOW.isoformat(),
                    json.dumps({"id": f"provider-many-{index}", "title": title}),
                ),
            )
        self.core.conn.commit()

    def _insert_tag_source(self) -> None:
        seed_active_tags_from_sources_yaml(
            self.core.conn,
            {"domain_label": "fan_kepu_social_life", "active_tags": ["消费规则"]},
        )
        tag_id = self.core.conn.execute("SELECT tag_id FROM domain_search_tags WHERE tag='消费规则'").fetchone()[0]
        self.core.conn.execute(
            """
            INSERT INTO discovered_external_videos(
                discovered_video_id, platform, platform_item_id, tag_id, domain_label,
                title, url, raw_json, discovered_at, run_id
            ) VALUES ('tag-video-1', 'douyin', 'item-1', ?, 'fan_kepu_social_life',
                      '消费规则为什么让普通人困惑', 'https://example.test/tag-video', ?, ?, 'search-run')
            """,
            (tag_id, json.dumps({"aweme_id": "item-1", "desc": "消费规则为什么让普通人困惑"}), self.NOW.isoformat()),
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

    def test_hotspot_and_tag_discovery_are_formal_sources_not_direct_candidates(self) -> None:
        self._insert_hotspot()
        self._insert_tag_source()
        sources = self.core.load_real_discovery_sources(
            domain_label="fan_kepu_social_life",
            daily_since="2026-07-11T12:00:00+00:00",
            per_source_limit=6,
        )
        self.assertEqual([source["source_type"] for source in sources], ["hotspot", "tag_discovery"])
        result = self._run("hotspot-tag")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["sources_read"], 2)
        self.assertEqual(self.provider.calls, 2)
        recorded_types = {
            row[0] for row in self.core.conn.execute("SELECT source_type FROM stage1b_source_version").fetchall()
        }
        self.assertEqual(recorded_types, {"hotspot", "tag_discovery"})

    def test_hotspot_domain_matching_does_not_depend_on_the_topic_search_library(self) -> None:
        self._insert_hotspot(seed_search_tag=False)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM domain_search_tags").fetchone()[0], 0)
        sources = self.core.load_real_discovery_sources(
            domain_label="fan_kepu_social_life",
            daily_since="2026-07-11T12:00:00+00:00",
            per_source_limit=6,
        )
        self.assertEqual([source["source_type"] for source in sources], ["hotspot"])

    def test_completed_expansion_is_daily_report_source_but_saved_user_direction_is_not(self) -> None:
        self.core.register_question_expansion_source(
            expansion_id="expansion-1",
            domain_label="fan_kepu_social_life",
            core_question="为什么养老服务会改变普通家庭的日常选择？",
            parent_source_ref={"source_type": "hotspot", "source_id": "parent-1"},
            actor="test-user",
        )
        self.core.register_saved_user_direction_source(
            direction_id="direction-1",
            domain_label="fan_kepu_social_life",
            core_question="为什么预付消费退款总让普通人陷入被动？",
            submitted_by="test-user",
        )
        sources = self.core.load_real_discovery_sources(
            domain_label="fan_kepu_social_life",
            daily_since="2026-07-11T12:00:00+00:00",
            per_source_limit=6,
        )
        self.assertEqual(
            [source["source_type"] for source in sources],
            ["question_expansion", "saved_user_direction"],
        )
        result = self._run("internal-sources")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 1)
        self.assertEqual(self.provider.calls, 1)

    def test_runtime_collects_hotspot_then_converts_it_before_starting_tag_search(self) -> None:
        test_case = self

        class OrderedAcquirer:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def collect_hotspots(self, *, discovery_run_id: str, now: datetime, deadline_monotonic: float | None = None) -> dict:
                self.calls.append("trendradar")
                test_case._insert_hotspot()
                return {"status": "completed", "item_count": 1}

            def search_tags(self, *, discovery_run_id: str, domain: str, now: datetime, deadline_monotonic: float | None = None) -> dict:
                self.calls.append("tag_search")
                test_case.assertEqual(test_case.provider.calls, 1, "hotspot conversion must finish before tag search starts")
                return {"selected_tag_count": 0, "results": [], "failed": 0}

        acquirer = OrderedAcquirer()
        service = Stage1BDailyDiscoveryService(core=self.core, gateway=self.gateway, source_acquirer=acquirer)  # type: ignore[arg-type]
        result = service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="test-worker",
            idempotency_key="ordered-runtime",
            execution_mode="test_isolated",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
        )
        self.assertEqual(acquirer.calls, ["trendradar", "tag_search"])
        self.assertEqual(result["summary"]["acquisition"]["execution_order"][:2], ["trendradar_hotspot", "hotspot_conversion"])

    def test_stage1_daily_report_has_only_five_reportable_source_types(self) -> None:
        production_db = Path(self.tempdir.name) / "formal-production-daily-sources.sqlite3"
        with patch("scripts.core.production.stage0_content_core.FORMAL_DB_PATH", production_db):
            production_core = Stage0ContentProductionCore.open(production_db, data_identity="production")
        self.production_core = production_core
        production_provider = FakeDiscoveryProvider()
        production_gateway = ModelGateway(
            routes={self.route.route_name: self.route},
            providers={production_provider.provider_name: production_provider},
            materializer=CoreDiscoveryModelRunMaterializer(production_core),
        )
        production_service = Stage1BDailyDiscoveryService(
            core=production_core,
            gateway=production_gateway,
            source_acquirer=NoopProductionAcquirer(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(DailyDiscoveryValidationError, "manual|daily report|人工|日报"):
            production_service.run_daily_discovery(
                discovery_date="2026-07-14",
                actor="daily-worker",
                idempotency_key="manual-source-not-daily",
                execution_mode="production_daily",
                now=self.NOW,
                source_types=(
                    "hotspot",
                    "daily_competitor_content",
                    "historical_high_signal",
                    "tag_discovery",
                    "question_expansion",
                    "saved_user_direction",
                ),
            )

    def test_hotspot_processing_is_limited_to_three_daily_items(self) -> None:
        self._insert_hotspots(4)
        result = self._run("hotspot-daily-limit", source_types=("hotspot",))
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["sources_read"], 3)
        self.assertLessEqual(self.provider.calls, 3)

    def test_validation_live_stops_after_one_eligible_model_attempt(self) -> None:
        production_db = Path(self.tempdir.name) / "formal-validation-live-limit.sqlite3"
        with patch("scripts.core.production.stage0_content_core.FORMAL_DB_PATH", production_db):
            production_core = Stage0ContentProductionCore.open(production_db, data_identity="production")
        self.production_core = production_core
        production_provider = FakeDiscoveryProvider()
        production_gateway = ModelGateway(
            routes={self.route.route_name: self.route},
            providers={production_provider.provider_name: production_provider},
            materializer=CoreDiscoveryModelRunMaterializer(production_core),
        )
        production_core.conn.execute(
            """
            INSERT INTO trendradar_collection_run(
                collection_run_id, discovery_run_id, status, item_count, command_hash, started_at, completed_at
            ) VALUES ('tr-run-validation-limit', 'upstream-validation-limit', 'completed', 4, 'hash-validation-limit', ?, ?)
            """,
            (self.NOW.isoformat(), self.NOW.isoformat()),
        )
        for index in range(4):
            title = f"validation live hotspot source {index}"
            production_core.conn.execute(
                """
                INSERT INTO trendradar_hotspot_observation(
                    observation_id, provider_item_id, title, url, source_channel, source_rank,
                    observed_at, raw_json, collection_run_id
                ) VALUES (?, ?, ?, ?, 'news', ?, ?, ?, 'tr-run-validation-limit')
                """,
                (
                    f"validation-hotspot-{index}",
                    f"validation-provider-{index}",
                    title,
                    f"https://example.test/validation-hotspot-{index}",
                    index + 1,
                    self.NOW.isoformat(),
                    json.dumps({"id": f"validation-provider-{index}", "title": title}),
                ),
            )
        production_core.conn.commit()
        service = Stage1BDailyDiscoveryService(
            core=production_core,
            gateway=production_gateway,
            source_acquirer=NoopProductionAcquirer(),  # type: ignore[arg-type]
        )

        result = service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="validator",
            idempotency_key="validation-live-one-attempt",
            execution_mode="validation_live",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
            source_types=("hotspot",),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["sources_read"], 1)
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 1)
        self.assertEqual(production_provider.calls, 1)

    def test_hotspots_are_clustered_before_daily_processing_limit(self) -> None:
        self.core.conn.execute(
            """
            INSERT INTO trendradar_collection_run(
                collection_run_id, discovery_run_id, status, item_count, command_hash, started_at, completed_at
            ) VALUES ('tr-run-cluster', 'upstream-run-cluster', 'completed', 5, 'hash-cluster', ?, ?)
            """,
            (self.NOW.isoformat(), self.NOW.isoformat()),
        )
        rows = [
            ("hotspot-cluster-1", "provider-cluster-1", "shared housing policy debate", "weibo", 1),
            ("hotspot-cluster-2", "provider-cluster-2", "shared housing policy debate", "toutiao", 2),
            ("hotspot-cluster-3", "provider-cluster-3", "salary holiday policy route", "baidu", 3),
            ("hotspot-cluster-4", "provider-cluster-4", "family service pressure", "zhihu", 4),
            ("hotspot-cluster-5", "provider-cluster-5", "consumer refund dispute", "douyin", 5),
        ]
        for observation_id, provider_id, title, channel, rank in rows:
            self.core.conn.execute(
                """
                INSERT INTO trendradar_hotspot_observation(
                    observation_id, provider_item_id, title, url, source_channel, source_rank,
                    observed_at, raw_json, collection_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'tr-run-cluster')
                """,
                (
                    observation_id,
                    provider_id,
                    title,
                    f"https://example.test/{provider_id}",
                    channel,
                    rank,
                    self.NOW.isoformat(),
                    json.dumps({"id": provider_id, "title": title}),
                ),
            )
        self.core.conn.commit()

        sources = self.core.load_real_discovery_sources(
            domain_label="fan_kepu_social_life",
            daily_since="2026-07-11T12:00:00+00:00",
            per_source_limit=6,
        )
        hotspot_sources = [source for source in sources if source["source_type"] == "hotspot"]
        self.assertEqual(len(hotspot_sources), 4)
        first_cluster = hotspot_sources[0]["payload"]["hotspot_event_cluster"]
        self.assertEqual(hotspot_sources[0]["source_object_id"], "hotspot-cluster-1")
        self.assertTrue(first_cluster["cluster_id"].startswith("hotspot_cluster_"))
        self.assertEqual(first_cluster["representative_source"], "shared housing policy debate")
        self.assertEqual(len(first_cluster["merged_sources"]), 2)

    def test_each_daily_report_source_keeps_evidence_for_has_or_has_not(self) -> None:
        result = self._run(
            "daily-source-evidence",
            source_types=(
                "hotspot",
                "daily_competitor_content",
                "historical_high_signal",
                "tag_discovery",
                "question_expansion",
            ),
        )
        source_evidence = result["summary"].get("source_evidence", {}).get("fan_kepu_social_life")
        self.assertEqual(
            set(source_evidence or {}),
            {"hotspot", "daily_competitor_content", "historical_high_signal", "tag_discovery", "question_expansion"},
        )
        for source_type, evidence in source_evidence.items():
            with self.subTest(source_type=source_type):
                self.assertIn(evidence.get("status"), {"has_candidate", "no_candidate", "not_available"})
                self.assertTrue(evidence.get("reason") or evidence.get("source_refs"))

    def test_daily_candidate_snapshot_contains_stage1a_handoff_packet(self) -> None:
        self._insert_video()
        candidate = self._first_candidate(self._run("handoff-packet"))
        packet = candidate.get("stage1a_handoff_packet")
        self.assertIsInstance(packet, dict)
        self.assertEqual(packet.get("candidate_version_id"), candidate["candidate_version_id"])
        for field in (
            "source_type",
            "core_question",
            "domain_label",
            "recommendation_reason",
            "source_evidence_refs",
            "risk_limits",
            "material_gap",
            "timeliness_limits",
            "capacity_consumption",
        ):
            self.assertTrue(packet.get(field), field)

    def test_batch_interruption_is_finalized_without_retry(self) -> None:
        class InterruptingAcquirer:
            def collect_hotspots(self, **kwargs: object) -> dict:
                raise KeyboardInterrupt

        service = Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=self.gateway,
            source_acquirer=InterruptingAcquirer(),  # type: ignore[arg-type]
        )
        result = service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="test-worker",
            idempotency_key="interrupted-runtime",
            execution_mode="test_isolated",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
        )
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.core.conn.execute(
            "SELECT lifecycle_status FROM stage1b_run_execution_context"
        ).fetchone()[0], "interrupted")

    def test_partial_source_failure_is_not_reported_as_full_success(self) -> None:
        class PartialAcquirer:
            def collect_hotspots(self, **kwargs: object) -> dict:
                return {"status": "completed_with_failures", "item_count": 1, "invalid_items": 1}

            def search_tags(self, **kwargs: object) -> dict:
                return {"status": "completed_with_failures", "failed": 1, "results": []}

        service = Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=self.gateway,
            source_acquirer=PartialAcquirer(),  # type: ignore[arg-type]
        )
        result = service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="test-worker",
            idempotency_key="partial-runtime",
            execution_mode="test_isolated",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
        )
        self.assertEqual(result["status"], "completed_with_failures")
        self.assertGreaterEqual(result["summary"]["technical_failures"], 2)

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

    def test_validation_live_requires_exactly_one_source_type(self) -> None:
        with self.assertRaisesRegex(StateTransitionError, "exactly one source type"):
            self.service.run_daily_discovery(
                discovery_date="2026-07-14",
                actor="validator",
                idempotency_key="multi-source-validation",
                execution_mode="validation_live",
                now=self.NOW,
                domains=("fan_kepu_social_life",),
                source_types=("daily_competitor_content", "historical_high_signal"),
            )

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
        wrong_route = ModelRoute("business.source_to_topic", "wrong-provider", "wrong-model", "wrong.v1", "wrong-config", route_id="business_analysis", provider_ref="wrong")
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
        self.provider.output = {
            **VALID_SOURCE_TO_TOPIC_OUTPUT,
            "topic_status": "no_result",
            "candidate_topic": "",
            "topic_angle": "",
            "core_question": "",
            "audience_relation": "",
            "content_increment": "",
            "supporting_evidence": [],
            "source_constraints": ["测试来源不足以形成具体问题"],
            "no_result_reason": "insufficient_source_evidence",
            "confidence": "none",
            "candidate_selection": {**VALID_SOURCE_TO_TOPIC_OUTPUT["candidate_selection"], "selected_direction": "", "why_selected": ""},
            "material_gaps": ["测试来源不足以形成具体问题"],
        }
        result = self._run("zero-candidate")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
        self.assertEqual(self.core.conn.execute("SELECT reason_code FROM stage1b_candidate_absence").fetchone()[0], "model_returned_no_candidate")

    def test_single_json_code_fence_is_normalized_but_extra_text_is_rejected(self) -> None:
        fenced = f"```json\n{json.dumps(VALID_JUDGEMENT, ensure_ascii=False)}\n```"
        self.assertEqual(parse_candidate_judgement_output(fenced), validate_candidate_judgement(VALID_JUDGEMENT))
        with self.assertRaises(json.JSONDecodeError):
            parse_candidate_judgement_output(f"下面是结果：\n{json.dumps(VALID_JUDGEMENT, ensure_ascii=False)}")

    def test_fenced_json_can_complete_without_becoming_a_format_failure(self) -> None:
        self._insert_video()
        output = {
            **VALID_SOURCE_TO_TOPIC_OUTPUT,
            "supporting_evidence": ["常见生活现象为什么容易被误解"],
        }
        self.provider.output = f"```json\n{json.dumps(output, ensure_ascii=False)}\n```"
        result = self._run("fenced-json")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 1)
        self.assertEqual(self.core.conn.execute("SELECT validation_status FROM stage1b_model_run").fetchone()[0], "passed")

    def test_entertainment_angle_cannot_be_repackaged_as_social_life_candidate(self) -> None:
        self._insert_video(title="住房申购为什么引发公众讨论")
        self.provider.output = {
            **VALID_SOURCE_TO_TOPIC_OUTPUT,
            "candidate_topic": "住房申购争议背后的社会讨论",
            "core_question": "住房申购争议背后的社会讨论",
            "topic_angle": "从偶像粉丝社群反应切入讨论公众情绪。",
            "content_increment": "偶像粉丝社群反应",
        }
        result = self._run("entertainment-angle")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
        self.assertEqual(result["summary"]["filtered"]["fan_kepu_social_life"]["candidate_outside_domain_policy"], 1)
        absence = self.core.conn.execute("SELECT reason_code FROM stage1b_candidate_absence").fetchone()[0]
        self.assertEqual(absence, "candidate_outside_domain_policy")
        self.assertEqual(self.core.conn.execute("SELECT validation_status FROM stage1b_model_run").fetchone()[0], "passed")

    def test_self_declared_severe_material_gap_becomes_zero_candidate(self) -> None:
        self._insert_video(title="养老服务为什么引发普通家庭讨论")
        self.provider.output = {
            **VALID_SOURCE_TO_TOPIC_OUTPUT,
            "material_gaps": ["材料严重不足，仅有标题线索，无法确认事实准确性。"],
        }
        result = self._run("severe-material-gap")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["domains"]["fan_kepu_social_life"]["candidates"], 0)
        self.assertEqual(result["summary"]["filtered"]["fan_kepu_social_life"]["candidate_material_insufficient"], 1)
        self.assertEqual(self.core.conn.execute("SELECT reason_code FROM stage1b_candidate_absence").fetchone()[0], "candidate_material_insufficient")

    def test_candidate_input_contains_the_versioned_domain_policy(self) -> None:
        self._insert_video()
        self._run("domain-policy-input")
        payload = json.loads(self.core.conn.execute("SELECT payload_json FROM stage1b_input_assembly").fetchone()[0])
        self.assertEqual(payload["schema_version"], "source_to_topic.input.v1")
        self.assertIn("粉丝", payload["domain_rule_summary"])
        self.assertIn("住房", payload["domain_rule_summary"])

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
                self.assertEqual(result["summary"]["filtered"]["fan_kepu_social_life"]["outside_domain_policy"], index + 1)

    def test_physically_purges_only_the_confirmed_validation_run(self) -> None:
        production_db = Path(self.tempdir.name) / "formal-production.sqlite3"
        with patch("scripts.core.production.stage0_content_core.FORMAL_DB_PATH", production_db):
            production_core = Stage0ContentProductionCore.open(production_db, data_identity="production")
        self.production_core = production_core
        production_provider = FakeDiscoveryProvider()
        production_gateway = ModelGateway(
            routes={self.route.route_name: self.route},
            providers={production_provider.provider_name: production_provider},
            materializer=CoreDiscoveryModelRunMaterializer(production_core),
        )
        production_core.register_saved_user_direction_source(
            direction_id="bad-direction",
            domain_label="fan_kepu_social_life",
            core_question="为什么这个错误验证问题不应继续保留？",
            submitted_by="user",
        )
        service = Stage1BDailyDiscoveryService(core=production_core, gateway=production_gateway)
        result = service.run_daily_discovery(
            discovery_date="2026-07-14",
            actor="validator",
            idempotency_key="bad-validation",
            execution_mode="validation_live",
            now=self.NOW,
            domains=("fan_kepu_social_life",),
            source_types=("saved_user_direction",),
        )
        snapshot = service.view_daily_snapshot(
            run_id=result["run_id"], domains=("fan_kepu_social_life",)
        )["fan_kepu_social_life"]
        self.assertFalse(snapshot[0]["formal_candidate_pool"])
        with self.assertRaisesRegex(StateTransitionError, "production_daily"):
            service.handoff_selected_candidate(
                candidate_version_id=snapshot[0]["candidate_version_id"],
                actor="user",
                reason="validation must not be promoted",
                idempotency_key="validation-handoff",
            )
        unrelated = production_core.create_discovery_run(
            discovery_date="2026-07-14",
            actor="validator",
            execution_mode="validation_live",
            idempotency_key="unrelated-validation",
        )
        purge = production_core.purge_validation_live_run(
            run_id=result["run_id"],
            actor="user",
            reason="explicitly approved erroneous validation result deletion",
            expected_candidate_count=1,
            confirmation=f"DELETE_VALIDATION_LIVE_RUN:{result['run_id']}",
        )
        self.assertEqual(purge["result"], "physically_deleted")
        self.assertEqual(purge["deleted"]["stage1b_candidate_version"], 1)
        self.assertIsNone(production_core.conn.execute(
            "SELECT 1 FROM stage1b_discovery_run WHERE run_id=?", (result["run_id"],)
        ).fetchone())
        self.assertIsNotNone(production_core.conn.execute(
            "SELECT 1 FROM stage1b_discovery_run WHERE run_id=?", (unrelated["run_id"],)
        ).fetchone())
        for table in ("stage0_audit_event", "stage0_command_receipt"):
            column = "payload_json" if table == "stage0_audit_event" else "result_json"
            self.assertEqual(production_core.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (f"%{result['run_id']}%",)
            ).fetchone()[0], 0)

    def test_zero_candidate_remains_valid_when_batch_times_out_before_any_request(self) -> None:
        self._insert_video()
        result = self._run("batch-timeout", batch_timeout_seconds=0)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.core.conn.execute("SELECT lifecycle_status FROM stage1b_run_execution_context").fetchone()[0], "timed_out")


if __name__ == "__main__":
    unittest.main()
