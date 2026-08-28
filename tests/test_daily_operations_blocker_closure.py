from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sqlite3
import os
import unittest
from unittest.mock import patch

from scripts.agent_platform.daily_operations_runtime import DailyOperationsCoordinator
from scripts.core.production.business_runtime_guard import enforce_runtime_startup_guard
from scripts.core.production.stage1_competitor_registration import (
    configured_first_registration_item_limit,
)
from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService
from scripts.core.production.stage1_competitor_registration import build_production_daily_hit_gateway
from scripts.core.production.stage1b_daily_discovery import (
    Stage1BDailyDiscoveryService,
    build_production_daily_discovery_gateway,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelRoute
from scripts.core.model_gateway.model_router import ModelRouterError
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class _Cursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows

    def fetchone(self) -> dict | None:
        return self.rows[0] if self.rows else None


class _DailyConnection:
    def __init__(self, *, hit: dict | None = None, cold_start_ready: bool = False) -> None:
        self.hit = hit
        self.cold_start_ready = cold_start_ready

    def execute(self, sql: str, _params: tuple | list = ()) -> _Cursor:
        if "SELECT account.account_id" in sql:
            return _Cursor([{
                "account_id": "account-1",
                "platform": "douyin",
                "account_name": "测试对标账号",
                "homepage_url": "https://example.test/account-1",
            }])
        if "SELECT DISTINCT hit.*" in sql:
            return _Cursor([self.hit] if self.hit is not None else [])
        if "FROM hit_transcripts" in sql:
            return _Cursor([{
                "cleaned_transcript_text": "已经保存的逐字稿",
                "raw_transcript_text": "",
            }])
        if "FROM hit_comments" in sql:
            return _Cursor([])
        if "SELECT hit.hit_id FROM hits" in sql:
            return _Cursor([])
        if "FROM stage0_cold_start_configuration" in sql:
            return _Cursor([{"ready": 1}] if self.cold_start_ready else [])
        raise AssertionError(f"unexpected test query: {sql}")


class _DailyCore:
    data_identity = "production"

    def __init__(self, *, hit: dict | None = None, cold_start_ready: bool = False) -> None:
        self.conn = _DailyConnection(hit=hit, cold_start_ready=cold_start_ready)
        self.snapshot_calls = 0
        self.hit_judgement_calls = 0

    def record_daily_competitor_snapshot(self, **_kwargs: object) -> dict:
        self.snapshot_calls += 1
        return {"source_refs": [{"video_id": "video-1"}]}

    def judge_daily_competitor_hits(self, **_kwargs: object) -> dict:
        self.hit_judgement_calls += 1
        return {"new_hit_ids": ["hit-1"] if self.conn.hit is not None else []}


class _Collector:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def collect_video_snapshot(self, **_kwargs: object) -> SimpleNamespace:
        self.snapshot_calls += 1
        return SimpleNamespace(
            payload={"items": [{"video_id": "video-1"}]},
            raw_archive_ref="archive://daily-test",
        )


class DailyRuntimeGuardScopeTests(unittest.TestCase):
    def test_daily_scope_ignores_unrelated_checks_and_first_crawl_environment(self) -> None:
        with TemporaryDirectory() as tempdir:
            with (
                patch(
                    "scripts.core.production.business_runtime_guard.assert_formal_runtime_storage",
                    return_value={"validated": True},
                ),
                patch(
                    "scripts.core.production.business_runtime_guard._public_setting_binding_errors",
                    side_effect=AssertionError("daily scope must not scan public bindings"),
                ),
                patch.dict(os.environ, {"COMPETITOR_FIRST_CRAWL_MAX_ITEMS": "7"}, clear=False),
            ):
                result = enforce_runtime_startup_guard(
                    entrypoint="test_daily_scope",
                    scope="daily",
                    event_log_path=Path(tempdir) / "guard-events.jsonl",
                )
        self.assertTrue(result["valid"])
        self.assertEqual(result["runtime_scope"], "daily")

    def test_first_registration_still_keeps_its_fifty_item_rule(self) -> None:
        with patch.dict(os.environ, {"COMPETITOR_FIRST_CRAWL_MAX_ITEMS": "7"}, clear=False):
            with self.assertRaisesRegex(Exception, "exactly 50"):
                configured_first_registration_item_limit()


class DailyStoppedBoundaryTests(unittest.TestCase):
    def _daily_guards(self):
        return (
            patch(
                "scripts.core.production.stage1_daily_operations.enforce_runtime_startup_guard",
                return_value={"valid": True},
            ),
            patch(
                "scripts.core.production.stage1_daily_operations.enforce_daily_operations_runtime_guard",
                return_value=None,
            ),
        )

    def test_unfrozen_content_type_allows_collection_and_hit_save(self) -> None:
        core = _DailyCore()
        collector = _Collector()
        service = ProductionDailyOperationsService(core=core, collector=collector)
        with self._daily_guards()[0], self._daily_guards()[1], patch(
            "scripts.core.production.stage1_daily_operations.require_frozen_content_type_registry",
            side_effect=ValueError("registry is not frozen"),
        ) as frozen_check:
            result = service.run(
                domain_label="music_entertainment",
                discovery_date="2026-08-28",
                actor="daily-test",
                attempt_ref="attempt-1",
                daily_run_id="daily-1",
                effective_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(collector.snapshot_calls, 1)
        self.assertEqual(core.snapshot_calls, 1)
        self.assertEqual(core.hit_judgement_calls, 1)
        frozen_check.assert_not_called()

    def test_unfrozen_content_type_stops_at_breakdown_and_keeps_material_boundary(self) -> None:
        hit = {
            "hit_id": "hit-1",
            "account_id": "account-1",
            "domain_label": "music_entertainment",
            "platform": "douyin",
            "url": "https://example.test/video-1",
            "platform_item_id": "video-1",
            "like_count": 10,
            "comment_count": 1,
            "share_count": 0,
            "collect_count": 0,
            "promoted_at": "2026-08-28T01:00:00+00:00",
        }
        core = _DailyCore(hit=hit)
        collector = _Collector()
        service = ProductionDailyOperationsService(core=core, collector=collector)
        with (
            self._daily_guards()[0],
            self._daily_guards()[1],
            patch(
                "scripts.core.production.stage1_daily_operations.require_frozen_content_type_registry",
                side_effect=ValueError("registry is not frozen"),
            ),
            patch(
                "scripts.core.production.stage1_daily_operations.build_production_daily_hit_gateway",
                side_effect=AssertionError("Breakdown model must not execute while stopped"),
            ),
        ):
            result = service.run(
                domain_label="music_entertainment",
                discovery_date="2026-08-28",
                actor="daily-test",
                attempt_ref="attempt-1",
                daily_run_id="daily-1",
                effective_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "content_type_registry_not_frozen")
        self.assertEqual(result["hit_processing"][0]["status"], "stopped")
        self.assertEqual(result["upstream_failures"], [])

    def test_cold_start_wait_stops_candidate_discovery_without_running_it(self) -> None:
        core = _DailyCore(cold_start_ready=False)
        service = ProductionDailyOperationsService(core=core)
        with self._daily_guards()[0], self._daily_guards()[1], patch(
            "scripts.core.production.stage1_daily_operations.Stage1BDailyDiscoveryService",
            side_effect=AssertionError("candidate discovery must not execute while waiting"),
        ):
            result = service.run_candidate_discovery(
                domain_label="music_entertainment",
                discovery_date="2026-08-28",
                actor="daily-test",
                attempt_ref="attempt-1",
                daily_run_id="daily-1",
            )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["stop_reason"], "domain_cold_start_not_completed")
        self.assertEqual(result["candidate_count"], 0)

    def test_candidate_stop_is_not_relabelled_as_failure(self) -> None:
        core = SimpleNamespace(data_identity="production", close=lambda: None)
        service = SimpleNamespace(
            run=lambda **_kwargs: {"status": "completed", "upstream_failures": []},
            run_candidate_discovery=lambda **_kwargs: {
                "status": "stopped",
                "stop_reason": "domain_cold_start_not_completed",
                "candidate_count": 0,
            },
        )
        with (
            patch(
                "scripts.agent_platform.daily_operations_runtime.Stage0ContentProductionCore.open",
                return_value=core,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.ProductionDailyOperationsService",
                return_value=service,
            ),
        ):
            result = DailyOperationsCoordinator(
                db_path=Path("unused.sqlite3"),
                data_identity="production",
            )._run_production(
                domain_label="music_entertainment",
                business_date="2026-08-28",
                daily_run_id="daily-1",
                resume=False,
                attempt_ref="attempt-1",
            )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["candidate_status"], "stopped")

    def test_real_collection_failure_wins_over_candidate_stop(self) -> None:
        core = SimpleNamespace(data_identity="production", close=lambda: None)
        service = SimpleNamespace(
            run=lambda **_kwargs: {
                "status": "completed_with_failures",
                "upstream_failures": [{"source": "collector"}],
            },
            run_candidate_discovery=lambda **_kwargs: {
                "status": "stopped",
                "stop_reason": "domain_cold_start_not_completed",
                "candidate_count": 0,
            },
        )
        with (
            patch(
                "scripts.agent_platform.daily_operations_runtime.Stage0ContentProductionCore.open",
                return_value=core,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.ProductionDailyOperationsService",
                return_value=service,
            ),
        ):
            result = DailyOperationsCoordinator(
                db_path=Path("unused.sqlite3"),
                data_identity="production",
            )._run_production(
                domain_label="music_entertainment",
                business_date="2026-08-28",
                daily_run_id="daily-1",
                resume=False,
                attempt_ref="attempt-1",
            )
        self.assertEqual(result["status"], "failed")

    def test_execution_binding_is_shared_with_daily_service(self) -> None:
        core = SimpleNamespace(data_identity="production", close=lambda: None)
        service = SimpleNamespace(
            run=lambda **_kwargs: {"status": "stopped", "upstream_failures": []},
        )
        binding = {
            "route_id": "business_analysis",
            "provider_ref": "mimo_main",
            "provider_name": "hermes",
            "provider_type": "mimo",
            "model_name": "model-from-hermes",
            "endpoint": "https://mimo.example/v1",
            "source": "hermes_current_session",
            "explicit_override": False,
        }
        with (
            patch(
                "scripts.agent_platform.daily_operations_runtime.Stage0ContentProductionCore.open",
                return_value=core,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.ProductionDailyOperationsService",
                return_value=service,
            ) as service_factory,
        ):
            result = DailyOperationsCoordinator(
                db_path=Path("unused.sqlite3"),
                data_identity="production",
            )._run_production(
                domain_label="music_entertainment",
                business_date="2026-08-28",
                daily_run_id="daily-1",
                resume=False,
                attempt_ref="attempt-1",
                task_model_binding=binding,
            )
        self.assertEqual(result["status"], "stopped")
        service_factory.assert_called_once_with(
            core=core,
            task_model_binding=binding,
        )

    def test_breakdown_and_source_to_topic_gateways_use_same_frozen_binding(self) -> None:
        router = SimpleNamespace()
        router.routes = {
            "business_analysis": SimpleNamespace(
                fallback="none",
                allowed_task_types=("benchmark_analysis", "topic_screening"),
            )
        }
        binding = {
            "route_id": "business_analysis",
            "provider_ref": "mimo_main",
            "provider_name": "hermes",
            "provider_type": "mimo",
            "model_name": "model-from-hermes",
            "endpoint": "https://mimo.example/v1",
            "source": "hermes_current_session",
            "explicit_override": False,
        }
        seen: list[dict] = []
        route = ModelRoute(
            route_name="unused",
            provider_name="hermes",
            model_name="model-from-hermes",
            config_version="hermes_task_binding.v1",
            config_hash="hash",
            route_id="business_analysis",
            provider_ref="mimo_main",
        )
        provider = SimpleNamespace(provider_name="hermes")
        router.resolve_frozen_task_route = lambda received, **_kwargs: (
            seen.append(received) or route
        )
        router.resolve_bound_provider = lambda _route: SimpleNamespace(
            provider_ref="mimo_main",
            provider_name="hermes",
        )
        core = SimpleNamespace(data_identity="production")
        with (
            patch(
                "scripts.core.production.stage1_competitor_registration.ModelRouter.from_file",
                return_value=router,
            ),
            patch(
                "scripts.core.production.stage1_competitor_registration.build_configured_model_provider",
                return_value=provider,
            ),
            patch(
                "scripts.core.production.stage1b_daily_discovery.ModelRouter.from_file",
                return_value=router,
            ),
            patch(
                "scripts.core.production.stage1b_daily_discovery.build_configured_model_provider",
                return_value=provider,
            ),
        ):
            build_production_daily_hit_gateway(core, task_model_binding=binding)
            build_production_daily_discovery_gateway(core, task_model_binding=binding)
        self.assertEqual(seen, [binding, binding])

    def test_source_to_topic_uses_the_gateway_route_when_no_route_is_injected(self) -> None:
        route = ModelRoute(
            route_name="business.source_to_topic",
            provider_name="hermes",
            model_name="model-from-hermes",
            config_version="hermes_task_binding.v1",
            config_hash="hash",
            route_id="business_analysis",
            provider_ref="mimo_main",
        )
        gateway = SimpleNamespace(routes={route.route_name: route})
        service = Stage1BDailyDiscoveryService(
            core=SimpleNamespace(data_identity="test"),
            gateway=gateway,
        )
        self.assertEqual(service.model_route, route)

    def test_source_to_topic_rejects_a_route_different_from_the_gateway(self) -> None:
        gateway_route = ModelRoute(
            route_name="business.source_to_topic",
            provider_name="hermes",
            model_name="model-from-hermes",
            config_version="hermes_task_binding.v1",
            config_hash="hash",
            route_id="business_analysis",
            provider_ref="mimo_main",
        )
        stale_route = ModelRoute(
            route_name="business.source_to_topic",
            provider_name="hermes",
            model_name="stale-model",
            config_version="model_routes.v1",
            config_hash="stale-hash",
            route_id="business_analysis",
            provider_ref="relay_main",
        )
        with self.assertRaisesRegex(ModelRouterError, "does not match"):
            Stage1BDailyDiscoveryService(
                core=SimpleNamespace(data_identity="test"),
                gateway=SimpleNamespace(routes={gateway_route.route_name: gateway_route}),
                model_route=stale_route,
            )

    def test_unexpected_discovery_failure_is_finalized_as_failed(self) -> None:
        route = ModelRoute(
            route_name="business.source_to_topic",
            provider_name="hermes",
            model_name="model-from-hermes",
            config_version="hermes_task_binding.v1",
            config_hash="hash",
            route_id="business_analysis",
            provider_ref="mimo_main",
        )
        source = {
            "source_type": "historical_high_signal",
            "source_object_id": "source-1",
            "source_object_version": "source-version-1",
            "source_time": "2026-08-27T00:00:00+00:00",
            "payload": {"title": "一条足够长的正式来源标题", "url": "https://example.test/source-1"},
        }

        class Core:
            data_identity = "test"

            def find_command_replay(self, *_args, **_kwargs):
                return None

            def create_discovery_run(self, **_kwargs):
                return {"run_id": "discovery-1"}

            def discovery_source_readiness(self, **_kwargs):
                return {}

            def load_real_discovery_sources(self, **_kwargs):
                return [source]

            def discovery_source_seen(self, **_kwargs):
                return False

            def formal_topic_title_seen(self, **_kwargs):
                return False

            def record_discovery_source(self, **_kwargs):
                return {"source_version_id": "source-version-1"}

            def record_discovery_filter(self, **_kwargs):
                return None

            def list_active_experiences(self, **_kwargs):
                return []

            def create_discovery_input_assembly(self, **_kwargs):
                raise RuntimeError("envelope binding mismatch")

            def complete_discovery_run(self, **kwargs):
                self.finalized = kwargs
                return {"status": "failed"}

            def record_completed_command(self, **_kwargs):
                return None

        core = Core()
        service = Stage1BDailyDiscoveryService(
            core=core,
            gateway=SimpleNamespace(routes={route.route_name: route}),
        )
        with patch.object(service, "_assembly_payload", return_value={}):
            result = service.run_daily_discovery(
                discovery_date="2026-08-28",
                actor="test",
                idempotency_key="discovery-failure-test",
                execution_mode="test_isolated",
                domains=("music_entertainment",),
                source_types=("historical_high_signal",),
                now=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(core.finalized["lifecycle_status"], "failed")
        self.assertIn("envelope binding mismatch", core.finalized["failure_reason"])


class DailyResumeDiscoveryReconciliationTests(unittest.TestCase):
    @staticmethod
    def _core(path: Path) -> Stage0ContentProductionCore:
        core = Stage0ContentProductionCore(
            sqlite3.connect(str(path)), db_path=path, data_identity="production"
        )
        core.install_schema()
        return core

    def _prepare_failed_daily(
        self,
        core: Stage0ContentProductionCore,
        *,
        business_date: str,
        lifecycle: str = "failed",
        reason: str = "source_to_topic / ModelGateway envelope contract mismatch",
    ) -> tuple[dict, dict]:
        daily_run = core.get_or_create_daily_run(
            domain_label="music_entertainment",
            business_date=business_date,
            actor="test",
        )
        core.start_daily_run(
            daily_run_id=daily_run["daily_run_id"],
            resume=False,
            actor="test",
        )
        core.finish_daily_run(
            daily_run_id=daily_run["daily_run_id"],
            lifecycle=lifecycle,
            actor="test",
            reason=reason,
        )
        discovery = core.create_discovery_run(
            discovery_date=business_date,
            actor="previous-execution",
            execution_mode="production_daily",
            domains=("music_entertainment",),
            idempotency_key=f"previous-discovery:{business_date}",
            daily_run_id=daily_run["daily_run_id"],
        )
        return daily_run, discovery

    def test_failed_parent_closes_only_prior_processing_child(self) -> None:
        with TemporaryDirectory() as tempdir:
            core = self._core(Path(tempdir) / "daily.sqlite3")
            try:
                daily_run, prior = self._prepare_failed_daily(
                    core, business_date="2026-08-28"
                )
                resumed = core.start_daily_run(
                    daily_run_id=daily_run["daily_run_id"],
                    resume=True,
                    actor="daily_operations_user_resume",
                )
                result = core.reconcile_prior_daily_discovery_for_resume(
                    daily_run_id=daily_run["daily_run_id"],
                    prior_daily_lifecycle="failed",
                    resume_started_at=str(resumed["started_at"]),
                    execution_attempt_ref="resume-1",
                    idempotency_key="resume-reconcile-1",
                )
                diagnostic = core.discovery_run_diagnostics(run_id=prior["run_id"])
            finally:
                core.close()
        self.assertEqual(result["finalized_run_ids"], [prior["run_id"]])
        self.assertEqual(diagnostic["status"], "failed")
        self.assertEqual(diagnostic["lifecycle_status"], "failed")
        self.assertIn("source_to_topic / ModelGateway envelope contract mismatch", diagnostic["failure_reason"])

    def test_stopped_parent_also_closes_prior_processing_child(self) -> None:
        with TemporaryDirectory() as tempdir:
            core = self._core(Path(tempdir) / "daily.sqlite3")
            try:
                daily_run, prior = self._prepare_failed_daily(
                    core, business_date="2026-08-29", lifecycle="stopped", reason="content type gate"
                )
                resumed = core.start_daily_run(
                    daily_run_id=daily_run["daily_run_id"],
                    resume=True,
                    actor="daily_operations_user_resume",
                )
                result = core.reconcile_prior_daily_discovery_for_resume(
                    daily_run_id=daily_run["daily_run_id"],
                    prior_daily_lifecycle="stopped",
                    resume_started_at=str(resumed["started_at"]),
                    execution_attempt_ref="resume-stopped-1",
                    idempotency_key="resume-reconcile-stopped-1",
                )
                diagnostic = core.discovery_run_diagnostics(run_id=prior["run_id"])
            finally:
                core.close()
        self.assertEqual(result["finalized_count"], 1)
        self.assertEqual(diagnostic["lifecycle_status"], "failed")
        self.assertEqual(diagnostic["failure_reason"], "content type gate")

    def test_current_resume_child_and_other_daily_child_are_not_touched(self) -> None:
        with TemporaryDirectory() as tempdir:
            core = self._core(Path(tempdir) / "daily.sqlite3")
            try:
                first_daily, first_prior = self._prepare_failed_daily(
                    core, business_date="2026-08-30"
                )
                other_daily, other_prior = self._prepare_failed_daily(
                    core, business_date="2026-08-31"
                )
                resumed = core.start_daily_run(
                    daily_run_id=first_daily["daily_run_id"],
                    resume=True,
                    actor="daily_operations_user_resume",
                )
                first_result = core.reconcile_prior_daily_discovery_for_resume(
                    daily_run_id=first_daily["daily_run_id"],
                    prior_daily_lifecycle="failed",
                    resume_started_at=str(resumed["started_at"]),
                    execution_attempt_ref="resume-isolation-1",
                    idempotency_key="resume-reconcile-isolation-1",
                )
                current = core.create_discovery_run(
                    discovery_date="2026-08-30",
                    actor="current-execution",
                    execution_mode="production_daily",
                    domains=("music_entertainment",),
                    idempotency_key="current-discovery:2026-08-30",
                    daily_run_id=first_daily["daily_run_id"],
                )
                second_result = core.reconcile_prior_daily_discovery_for_resume(
                    daily_run_id=first_daily["daily_run_id"],
                    prior_daily_lifecycle="failed",
                    resume_started_at=str(resumed["started_at"]),
                    execution_attempt_ref="resume-isolation-1",
                    idempotency_key="resume-reconcile-isolation-2",
                )
                current_diagnostic = core.discovery_run_diagnostics(run_id=current["run_id"])
                other_diagnostic = core.discovery_run_diagnostics(run_id=other_prior["run_id"])
            finally:
                core.close()
        self.assertEqual(first_result["finalized_run_ids"], [first_prior["run_id"]])
        self.assertEqual(second_result["finalized_count"], 0)
        self.assertEqual(current_diagnostic["status"], "processing")
        self.assertEqual(other_diagnostic["status"], "processing")

    def test_normal_daily_does_not_scan_prior_processing_children(self) -> None:
        with TemporaryDirectory() as tempdir:
            with (
                patch(
                    "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
                    return_value=None,
                ),
                patch.object(
                    Stage0ContentProductionCore,
                    "reconcile_prior_daily_discovery_for_resume",
                    side_effect=AssertionError("normal daily must not reconcile prior discovery"),
                ) as reconcile,
            ):
                result = DailyOperationsCoordinator(
                    db_path=Path(tempdir) / "daily.sqlite3",
                    data_identity="test",
                    runner=lambda **_kwargs: {"status": "completed"},
                    domain_provider=lambda: ["music_entertainment"],
                    now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
                ).schedule_all(
                    domain_labels=("music_entertainment",),
                    trigger="automatic",
                    resume=False,
                    attempt_ref="normal-1",
                )
        self.assertEqual(result["domain_results"][0]["daily_run_status"], "completed")
        reconcile.assert_not_called()

    def test_explicit_resume_reconciles_before_runner_inside_execution_path(self) -> None:
        calls: list[dict] = []
        core = SimpleNamespace(
            get_daily_run_for_domain_date=lambda **_kwargs: {
                "daily_run_id": "daily-1",
                "lifecycle": "failed",
            },
            start_daily_run=lambda **_kwargs: {
                "daily_run_id": "daily-1",
                "lifecycle": "running",
                "started_at": "2026-08-28T01:00:00+00:00",
            },
            reconcile_prior_daily_discovery_for_resume=lambda **kwargs: calls.append(kwargs) or {
                "finalized_run_ids": ["discovery-old"],
                "finalized_count": 1,
            },
            finish_daily_run=lambda **_kwargs: {
                "daily_run_id": "daily-1",
                "lifecycle": "completed",
            },
            close=lambda: None,
        )

        with (
            patch(
                "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
                return_value=None,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.Stage0ContentProductionCore.open",
                return_value=core,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.blocking_process_mutex",
                return_value=nullcontext(),
            ),
        ):
            result = DailyOperationsCoordinator(
                db_path=Path("unused.sqlite3"),
                data_identity="production",
                runner=lambda **_kwargs: {"status": "completed"},
                domain_provider=lambda: ["music_entertainment"],
                now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            ).schedule_all(
                domain_labels=("music_entertainment",),
                trigger="user_resume",
                resume=True,
                attempt_ref="resume-execution-1",
            )

        self.assertEqual(result["domain_results"][0]["daily_run_status"], "completed")
        self.assertEqual(calls[0]["daily_run_id"], "daily-1")
        self.assertEqual(calls[0]["prior_daily_lifecycle"], "failed")
        self.assertEqual(calls[0]["execution_attempt_ref"], "resume-execution-1")
        self.assertEqual(
            result["domain_results"][0]["prior_discovery_resume_reconciliation"]["finalized_count"],
            1,
        )

    def test_completed_daily_skips_resume_reconciliation(self) -> None:
        core = SimpleNamespace(
            get_daily_run_for_domain_date=lambda **_kwargs: {
                "daily_run_id": "daily-completed",
                "lifecycle": "completed",
            },
            close=lambda: None,
        )
        with (
            patch(
                "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
                return_value=None,
            ),
            patch(
                "scripts.agent_platform.daily_operations_runtime.Stage0ContentProductionCore.open",
                return_value=core,
            ),
            patch.object(
                core,
                "reconcile_prior_daily_discovery_for_resume",
                create=True,
                side_effect=AssertionError("completed daily must not reconcile prior discovery"),
            ) as reconcile_mock,
        ):
            result = DailyOperationsCoordinator(
                db_path=Path("unused.sqlite3"),
                data_identity="production",
                runner=lambda **_kwargs: {"status": "failed"},
                domain_provider=lambda: ["music_entertainment"],
                now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            ).schedule_all(
                domain_labels=("music_entertainment",),
                trigger="user_resume",
                resume=True,
                attempt_ref="resume-completed-1",
            )
        self.assertEqual(result["domain_results"][0]["action"], "skipped_completed")
        reconcile_mock.assert_not_called()


class DailyRunStoppedLifecycleTests(unittest.TestCase):
    def test_stopped_run_requires_resume_and_reuses_same_daily_run(self) -> None:
        with TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "daily.sqlite3"
            calls: list[dict] = []

            def stopped_runner(**kwargs: object) -> dict:
                calls.append(dict(kwargs))
                return {"status": "stopped", "stop_reason": "content_type_registry_not_frozen"}

            with patch(
                "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
                return_value=None,
            ):
                first = DailyOperationsCoordinator(
                    db_path=db_path,
                    data_identity="test",
                    runner=stopped_runner,
                    domain_provider=lambda: ["music_entertainment"],
                    now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
                )
                first_result = first.schedule_all(
                    domain_labels=("music_entertainment",),
                    trigger="automatic",
                    attempt_ref="attempt-1",
                )
                automatic_again = first.schedule_all(
                    domain_labels=("music_entertainment",),
                    trigger="automatic",
                    attempt_ref="attempt-2",
                )

                def completed_runner(**kwargs: object) -> dict:
                    calls.append(dict(kwargs))
                    return {"status": "completed"}

                resumed = DailyOperationsCoordinator(
                    db_path=db_path,
                    data_identity="test",
                    runner=completed_runner,
                    domain_provider=lambda: ["music_entertainment"],
                    now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
                )
                resumed_result = resumed.schedule_all(
                    domain_labels=("music_entertainment",),
                    trigger="user_resume",
                    resume=True,
                    attempt_ref="attempt-3",
                )

        self.assertEqual(first_result["domain_results"][0]["daily_run_status"], "stopped")
        self.assertEqual(
            automatic_again["domain_results"][0]["action"],
            "awaiting_user_resume",
        )
        self.assertEqual(resumed_result["domain_results"][0]["daily_run_status"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["daily_run_id"], calls[1]["daily_run_id"])

    def test_provider_404_remains_failed(self) -> None:
        with TemporaryDirectory() as tempdir:
            coordinator = DailyOperationsCoordinator(
                db_path=Path(tempdir) / "daily.sqlite3",
                data_identity="test",
                runner=lambda **_kwargs: {
                    "status": "failed",
                    "error": {"type": "ProviderError", "message": "404"},
                },
                domain_provider=lambda: ["music_entertainment"],
                now_provider=lambda: datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            )
            with patch(
                "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
                return_value=None,
            ):
                result = coordinator.schedule_all(
                    domain_labels=("music_entertainment",),
                    trigger="automatic",
                    attempt_ref="attempt-404",
                )
        self.assertEqual(result["domain_results"][0]["daily_run_status"], "failed")


if __name__ == "__main__":
    unittest.main()
