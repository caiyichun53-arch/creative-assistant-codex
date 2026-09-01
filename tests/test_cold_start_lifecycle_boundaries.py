"""Isolation checks for the cold-start initialization/execution boundary."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.external_adapters.goal_phase4_external_adapters import ExternalAdapterRunResult
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.live_music_cold_start_preflight import (
    LiveColdStartPreflight,
    LiveColdStartRequest,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
)
from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir


def _payload(domain: str = "生命周期测试") -> dict:
    return {
        "domain_mode": "create",
        "domain_name": domain,
        "platform": "douyin",
        "owned_account": {
            "display_name": "自营账号",
            "external_account_ref": "douyin:owned-lifecycle",
        },
        "competitor_accounts": [
            {
                "display_name": f"对标账号{i}",
                "external_account_ref": f"douyin:competitor-lifecycle-{i}",
            }
            for i in range(20)
        ],
        "actor": "生命周期测试用户",
    }


class _HistoryCore:
    def __init__(self) -> None:
        self.progress = {
            "production-tracking-registration": {
                "registration_id": "production-tracking-registration",
                "status": "completed",
                "raw_archive_refs": ["old://production-tracking"],
            }
        }
        self.requested: list[str] = []
        self.recorded: list[dict] = []

    def get_competitor_historical_page_progress(self, *, registration_id: str):
        self.requested.append(registration_id)
        return self.progress.get(registration_id)

    def record_competitor_historical_page_progress(self, **payload):
        value = dict(payload)
        self.recorded.append(value)
        self.progress[str(payload["registration_id"])] = value
        return value


class _OnePageCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def collect_video_snapshot_page(
        self, *, platform, source_url, continuation_cursor, published_after
    ):
        del platform, source_url, published_after
        self.calls.append(("collect", str(continuation_cursor or "")))
        now = int(time.time())
        items = [
            {
                "source_id": f"fresh-{index}",
                "platform": "douyin",
                "url": f"https://example.invalid/fresh-{index}",
                "title": f"fresh-{index}",
                "author": "fresh-account",
                "published_at": now - 10 * 86400,
                "duration_seconds": 30,
                "metrics": {
                    "like_count": 100,
                    "comment_count": 10,
                    "share_count": 3,
                    "collect_count": 2,
                },
            }
            for index in range(20)
        ]
        return ExternalAdapterRunResult(
            adapter_id="isolated-collector",
            capability="platform.video_snapshot",
            item_count=len(items),
            payload={
                "items": items,
                "pagination": {
                    "next_cursor": "",
                    "has_more": False,
                    "reached_time_boundary": False,
                    "stop_reason": "platform_exhausted",
                },
            },
            command_hash="fresh-command",
            output_hash="fresh-output",
            raw_archive_ref="fresh://archive",
            external_side_effect=False,
        )

    def read_video_snapshot_archive(self, *, raw_archive_ref, platform):
        del raw_archive_ref, platform
        return []


class _UnavailableRegistrationService:
    def run_competitor_registration_step(self, **_kwargs):
        raise RuntimeError("shared MediaCrawler unavailable")
class ColdStartLifecycleBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection, db_path=Path(":memory:"), data_identity="production"
        )
        self.core.install_schema()
        self.service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            execution_registration_service=_UnavailableRegistrationService(),
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    def test_missing_execution_runtime_is_resumable_and_does_not_delete_run(self) -> None:
        payload = _payload()
        preview = self.service.preview(payload)
        report = {
            "domain_label": preview["normalized"]["domain_label"],
            "ready": True,
            "checks": [
                {
                    "code": "mediacrawler_runtime",
                    "passed": False,
                    "required_for_cold_start": False,
                    "detail": "collector unavailable",
                }
            ],
        }
        with patch.object(self.service, "inspect_configuration", return_value=report):
            result = self.service.confirm(payload)

        self.assertEqual(result["execution"]["status"], "execution_failed")
        self.assertTrue(result["execution"]["failures"])
        self.assertNotIn("account login", str(result["execution"]["failures"][0]).lower())
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_competitor_registration").fetchone()[0],
            20,
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start_configuration"
            ).fetchone()["status"],
            "started",
        )

    def test_executor_setup_failure_is_not_creation_failure(self) -> None:
        payload = _payload(domain="执行器初始化失败")
        preview = self.service.preview(payload)
        self.service.execution_registration_service = None

        def fail_background(**_: object) -> dict[str, object]:
            raise RuntimeError("background startup unavailable")

        self.service.background_execution_launcher = fail_background
        report = {
            "domain_label": preview["normalized"]["domain_label"],
            "ready": True,
            "checks": [],
        }
        with (
            patch.object(self.service, "inspect_configuration", return_value=report),
            patch(
                "scripts.core.production.stage1_competitor_registration.build_configured_competitor_registration_executor",
                side_effect=RuntimeError("analysis model unavailable"),
            ),
        ):
            result = self.service.confirm(payload)

        self.assertEqual(result["execution"]["status"], "background_launch_failed")
        self.assertFalse(result["automatic_start"])
        self.assertIn("background startup unavailable", result["execution"]["reason"])
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start_configuration"
            ).fetchone()["status"],
            "started",
        )
    def test_cold_start_preflight_only_resolves_analysis_model(self) -> None:
        now = "2026-08-22T00:00:00+00:00"
        with self.connection:
            self.connection.execute(
                "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("owned-lifecycle", "owned", "自营", "domain-lifecycle", "douyin:owned", "active", "production", "test", now),
            )
            for index in range(20):
                self.connection.execute(
                    "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"competitor-{index}", "competitor", f"对标{index}", "domain-lifecycle", f"douyin:competitor-{index}", "active", "production", "test", now),
                )

        class Route:
            provider_ref = "analysis-provider"

        class Router:
            def __init__(self) -> None:
                self.route_ids: list[str] = []

            def resolve_hermes_task_binding(self, *, route_id, **_):
                self.route_ids.append(route_id)
                if route_id != "business_analysis":
                    raise AssertionError("writing generation must not be a cold-start preflight dependency")
                return Route()

            def resolve_frozen_task_route(self, binding, **_):
                return binding

            def validate_bound_provider_configuration(self, *_args, **_kwargs):
                return None

        router = Router()
        preflight = LiveColdStartPreflight(
            core=self.core,
            repo_root=Path(self.temp_dir.name),
            environment={},
        )
        request = LiveColdStartRequest(
            domain_label="domain-lifecycle",
            owned_account_id="owned-lifecycle",
            competitor_account_ids=tuple(f"competitor-{index}" for index in range(20)),
        )
        with (
            patch("scripts.core.production.live_music_cold_start_preflight.get_domain_pack", return_value={"activation_status": "approved", "runtime_requirements": []}),
            patch.object(LocalMediaCrawlerExecutor, "readiness_problems", return_value=[]),
            patch("scripts.core.production.live_music_cold_start_preflight.retained_douyin_collector_browser_status", return_value={"status": "ready"}),
            patch.object(preflight, "_env", return_value=("C:\fake-runtime", None)),
            patch("scripts.core.production.live_music_cold_start_preflight.Path.is_file", return_value=True),
        ):
            report = preflight.inspect(request)

        self.assertEqual(router.route_ids, [])

    def test_cold_start_session_check_uses_shared_collector_not_owned_account_login(self) -> None:
        class Route:
            provider_ref = "analysis-provider"

        class Router:
            def resolve_hermes_task_binding(self, *, route_id, **_):
                self.route_id = route_id
                return Route()

            def resolve_frozen_task_route(self, binding, **_):
                return binding

            def validate_bound_provider_configuration(self, *_args, **_kwargs):
                return None

        preflight = LiveColdStartPreflight(
            core=self.core,
            repo_root=Path(self.temp_dir.name),
            environment={},
        )
        request = LiveColdStartRequest(
            domain_label="domain-lifecycle",
            owned_account_id="owned-account-without-login-profile",
            competitor_account_ids=tuple(f"competitor-{index}" for index in range(20)),
        )
        with (
            patch("scripts.core.production.live_music_cold_start_preflight.get_domain_pack", return_value={"activation_status": "approved", "runtime_requirements": []}),
            patch.object(LocalMediaCrawlerExecutor, "readiness_problems", return_value=[]),
            patch("scripts.core.production.live_music_cold_start_preflight.retained_douyin_collector_browser_status", return_value={"status": "ready"}),
            patch.object(preflight, "_env", return_value=("C:\fake-runtime", None)),
            patch("scripts.core.production.live_music_cold_start_preflight.Path.is_file", return_value=True),
        ):
            report = preflight.inspect(request)

        session_check = next(item for item in report["checks"] if item["code"] == "mediacrawler_account_session")
        self.assertTrue(session_check["passed"])
        self.assertIn("shared MediaCrawler collector session", session_check["detail"])
        self.assertNotIn("own-account", session_check["detail"])

    def test_cold_start_session_check_reports_shared_session_failure(self) -> None:
        class Route:
            provider_ref = "analysis-provider"

        class Router:
            def resolve_hermes_task_binding(self, *, route_id, **_):
                return Route()

            def resolve_frozen_task_route(self, binding, **_):
                return binding

            def validate_bound_provider_configuration(self, *_args, **_kwargs):
                return None

        preflight = LiveColdStartPreflight(
            core=self.core,
            repo_root=Path(self.temp_dir.name),
            environment={},
        )
        request = LiveColdStartRequest(
            domain_label="domain-lifecycle",
            owned_account_id="owned-account-without-login-profile",
            competitor_account_ids=tuple(f"competitor-{index}" for index in range(20)),
        )
        with (
            patch("scripts.core.production.live_music_cold_start_preflight.get_domain_pack", return_value={"activation_status": "approved", "runtime_requirements": []}),
            patch.object(LocalMediaCrawlerExecutor, "readiness_problems", return_value=[]),
            patch("scripts.core.production.live_music_cold_start_preflight.retained_douyin_collector_browser_status", return_value={"status": "not_ready"}),
            patch.object(preflight, "_env", return_value=("C:\fake-runtime", None)),
            patch("scripts.core.production.live_music_cold_start_preflight.Path.is_file", return_value=True),
        ):
            report = preflight.inspect(request)

        session_check = next(item for item in report["checks"] if item["code"] == "mediacrawler_account_session")
        self.assertFalse(session_check["passed"])
        self.assertFalse(session_check["required_for_cold_start"])
        self.assertIn("shared MediaCrawler collector session is not ready", session_check["detail"])
        self.assertNotIn("own-account", session_check["detail"])

    def test_first_collection_ignores_unrelated_production_tracking_history(self) -> None:
        core = _HistoryCore()
        collector = _OnePageCollector()
        executor = ConfiguredCompetitorRegistrationExecutor(
            core=core,
            collector=collector,
            transcriber=object(),
            media_materializer=object(),
        )
        registration = {
            "registration_id": "new-cold-start-registration",
            "cold_start_id": "new-cold-start",
            "external_account_ref": "douyin:fresh-account",
        }

        artifact = executor._historical_material(registration, ())[0]

        self.assertEqual(core.requested, ["new-cold-start-registration"])
        self.assertEqual(artifact["collection_status"], "target_reached")
        self.assertEqual(artifact["item_count"], 20)
        self.assertEqual(collector.calls, [("collect", "")])


if __name__ == "__main__":
    unittest.main()
