from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.core_entry import CoreReadOnlySession, CoreStartupError, build_status
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.mcp.creation_assistant_mcp_server import CreationAssistantMcpApplication


FORMAL_DATABASE = Path(r"I:\Creation_assistant-runtime\formal\production_activation.sqlite3")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _seed_domain(
    core: Stage0ContentProductionCore,
    domain: str,
    *,
    cold_status: str | None = "running",
    daily_lifecycle: str | None = None,
    waiting_registration: bool = False,
    current: bool = True,
) -> tuple[str, str]:
    now = "2026-08-30T00:00:00+00:00"
    owned_id = f"owned-{domain}"
    cold_id = f"cold-{domain}"
    config_id = f"config-{domain}"
    core.conn.execute(
        "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', 'test', 'fixture', ?)",
        (owned_id, domain, domain, f"douyin:{owned_id}", now),
    )
    core.conn.execute(
        "INSERT INTO stage0_cold_start(cold_start_id, owned_account_id, domain_label, status, "
        "data_identity, created_by, created_at, completed_at) VALUES (?, ?, ?, ?, 'test', 'fixture', ?, NULL)",
        (cold_id, owned_id, domain, cold_status or "completed", now),
    )
    core.conn.execute(
        "INSERT INTO stage0_cold_start_configuration("
        "configuration_id, domain_mode, domain_label, domain_name, domain_boundary, platform, "
        "owned_account_id, competitor_account_ids_json, status, data_identity, confirmed_by, confirmed_at, cold_start_id"
        ") VALUES (?, 'create', ?, ?, '', 'douyin', ?, '[]', 'started', 'test', 'fixture', ?, ?)",
        (config_id, domain, domain, owned_id, now, cold_id),
    )
    if current:
        core._ensure_current_domain_activation(
            domain_label=domain,
            cold_start_id=cold_id,
            configuration_id=config_id,
        )
    if waiting_registration:
        competitor_id = f"competitor-{domain}"
        core.conn.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'competitor', ?, ?, ?, 'active', 'test', 'fixture', ?)",
            (competitor_id, competitor_id, domain, f"douyin:{competitor_id}", now),
        )
        core.conn.execute(
            "INSERT INTO stage0_competitor_registration(" 
            "registration_id, cold_start_id, competitor_account_id, current_step, status, "
            "data_identity, created_at, completed_at) VALUES (?, ?, ?, 'awaiting_human_review', "
            "'awaiting_human_review', 'test', ?, NULL)",
            (f"registration-{domain}", cold_id, competitor_id, now),
        )
    if daily_lifecycle is not None:
        core.conn.execute(
            "INSERT INTO stage0_daily_run(" 
            "daily_run_id, domain_label, business_date, lifecycle, created_at, started_at, "
            "finished_at, data_identity, cold_start_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'test', ?)",
            (
                f"daily-{domain}", domain, "2026-08-30", daily_lifecycle,
                now, now, now if daily_lifecycle != "running" else None, cold_id,
            ),
        )
    return cold_id, config_id


class CoreUnifiedStatusTests(unittest.TestCase):
    def test_formal_current_is_separate_from_history_and_is_read_only(self) -> None:
        before = _sha256(FORMAL_DATABASE)
        status = build_status()
        after = _sha256(FORMAL_DATABASE)

        self.assertEqual(before, "B8D5A61E656798C485DC51DF64623F3985617C9B9A1BB1ECB2D3C38FDBA174F8")
        self.assertEqual(after, before)
        self.assertEqual(status["system"]["runtime_identity"], "FORMAL")
        self.assertTrue(status["system"]["database"]["readable"])
        self.assertEqual({item["domain_identity"] for item in status["domains"]}, {"domain_1833831517eb", "music_entertainment"})
        self.assertEqual(status["business_runs"]["in_progress"], [])
        self.assertEqual(status["history"]["unfinished_business_runs_total"], 9)
        self.assertIn("historical/non-current", status["human_summary"])
        for domain in status["domains"]:
            self.assertFalse(domain["current_activation"]["exists"])
            self.assertEqual(domain["current_cold_start"]["status"], "未cold-start")
            self.assertFalse(domain["current_daily"]["exists"])
            self.assertGreater(domain["history"]["cold_starts"]["total"], 0)
            self.assertGreater(domain["history"]["daily_runs"]["total"], 0)
        music = next(item for item in status["domains"] if item["domain_identity"] == "music_entertainment")
        self.assertEqual(music["in_progress_business_runs"], [])
        self.assertEqual(music["history"]["unfinished_business_runs"]["total"], 9)
        for record in music["history"]["unfinished_business_runs"]["records"]:
            self.assertEqual(record["classification"], "historical/non-current")
            self.assertFalse(record["has_current_business_control"])
            self.assertFalse(record["will_resume"])
            self.assertFalse(record["will_continue"])
            self.assertFalse(record["blocks_new_cold_start"])

    def test_test_status_reports_waiting_daily_lifecycles_and_domain_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "status.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                _seed_domain(core, "waiting", cold_status="waiting_human", waiting_registration=True)
                _seed_domain(core, "running", daily_lifecycle="running")
                _seed_domain(core, "stopped", daily_lifecycle="stopped")
                _seed_domain(core, "failed", daily_lifecycle="failed")
                _seed_domain(core, "completed", daily_lifecycle="completed")
                _seed_domain(core, "new-domain", current=False, cold_status="failed", daily_lifecycle="completed")
                core.conn.commit()
            finally:
                core.close()

            before = _sha256(database)
            status = build_status(data_identity="test", database_path=database)
            after = _sha256(database)

        self.assertEqual(after, before)
        by_domain = {item["domain_identity"]: item for item in status["domains"]}
        self.assertTrue(by_domain["waiting"]["waiting_human"])
        self.assertEqual(by_domain["waiting"]["current_cold_start"]["status"], "waiting_human")
        self.assertEqual(by_domain["waiting"]["waiting_for"][0]["kind"], "competitor_registration_review")
        self.assertFalse(by_domain["running"]["waiting_human"])
        self.assertEqual(by_domain["running"]["current_daily"]["lifecycle"], "running")
        self.assertEqual(by_domain["stopped"]["current_daily"]["lifecycle"], "stopped")
        self.assertEqual(by_domain["failed"]["current_daily"]["lifecycle"], "failed")
        self.assertEqual(by_domain["completed"]["current_daily"]["lifecycle"], "completed")
        self.assertFalse(by_domain["new-domain"]["current_activation"]["exists"])
        self.assertEqual(by_domain["new-domain"]["current_cold_start"]["status"], "未cold-start")
        self.assertFalse(by_domain["new-domain"]["current_daily"]["exists"])
        self.assertGreater(by_domain["new-domain"]["history"]["daily_runs"]["total"], 0)
        self.assertTrue(by_domain["new-domain"]["can_start_new_cold_start"])

    def test_test_identity_cannot_read_formal_and_does_not_see_formal_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "test-status.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                _seed_domain(core, "test-only", current=False)
                core.conn.commit()
            finally:
                core.close()
            status = build_status(data_identity="test", database_path=database)

        self.assertEqual({item["domain_identity"] for item in status["domains"]}, {"test-only"})
        with self.assertRaises(CoreStartupError):
            CoreReadOnlySession.open(data_identity="test", database_path=FORMAL_DATABASE)

    def test_mcp_status_reuses_core_status(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "mcp-status.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            app = CreationAssistantMcpApplication(core)
            try:
                from_core = build_status(data_identity="test", database_path=database)
                from_mcp = app.status()
                self.assertEqual(from_mcp["status_schema_version"], from_core["status_schema_version"])
                self.assertEqual(from_mcp["system"], from_core["system"])
                self.assertEqual(from_mcp["domains"], from_core["domains"])
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
