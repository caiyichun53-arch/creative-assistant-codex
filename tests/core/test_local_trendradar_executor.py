from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.external_adapters import ExternalAdapterCommand, LocalTrendRadarExecutor
from scripts.integrations.trendradar_runtime import (
    approved_runtime_worktree_change,
    changed_crawl_record,
    export_crawl,
    read_original_article,
    verify_official_install,
)


class LocalTrendRadarExecutorTests(unittest.TestCase):
    @staticmethod
    def _command() -> ExternalAdapterCommand:
        return ExternalAdapterCommand(
            adapter_id="collector.trendradar",
            capability="hotspot.daily_snapshot",
            executable="trendradar",
            args=(),
            input_payload={"provider": "trendradar"},
            max_items=100,
            timeout_seconds=30,
        )

    def test_runs_argument_vector_without_shell_and_reads_normalized_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "python.exe").write_text("test", encoding="utf-8")
            (project / "hotspots.json").write_text(
                json.dumps({
                    "items": [{"id": "h1", "title": "社会热点", "url": "https://example.test/h1"}],
                    "provenance": {"raw_database": "real.db", "evidence_dir": "evidence/run-1"},
                }),
                encoding="utf-8",
            )
            executor = LocalTrendRadarExecutor(
                project_dir=project,
                executable=Path("python.exe"),
                command_args=("main.py",),
                normalized_json_path=Path("hotspots.json"),
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with patch("scripts.core.external_adapters.local_trendradar_executor.subprocess.run", return_value=completed) as run:
                result = executor.execute(self._command())
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.payload["items"]), 1)
            self.assertEqual(result.raw_archive_ref, "evidence/run-1")
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.args[0][0], str(project / "python.exe"))

    def test_missing_normalized_export_is_a_failed_result_not_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "python.exe").write_text("test", encoding="utf-8")
            executor = LocalTrendRadarExecutor(
                project_dir=project,
                executable=Path("python.exe"),
                command_args=("main.py",),
                normalized_json_path=Path("missing.json"),
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with patch("scripts.core.external_adapters.local_trendradar_executor.subprocess.run", return_value=completed) as run:
                result = executor.execute(self._command())
            self.assertEqual(result.status, "failed_output_missing")
            self.assertEqual(run.call_count, 1)

    def test_fixture_without_real_database_provenance_cannot_pass_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "python.exe").write_text("test", encoding="utf-8")
            (project / "hotspots.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            executor = LocalTrendRadarExecutor(
                project_dir=project,
                executable=Path("python.exe"),
                command_args=("main.py",),
                normalized_json_path=Path("hotspots.json"),
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with patch("scripts.core.external_adapters.local_trendradar_executor.subprocess.run", return_value=completed):
                result = executor.execute(self._command())
            self.assertEqual(result.status, "failed_output_invalid")

    def test_real_sqlite_crawl_is_converted_without_inventing_missing_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "2026-07-14.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE platforms(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                    CREATE TABLE news_items(
                        id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER,
                        url TEXT, mobile_url TEXT, last_crawl_time TEXT
                    );
                    CREATE TABLE crawl_records(id INTEGER PRIMARY KEY, crawl_time TEXT UNIQUE);
                    CREATE TABLE crawl_source_status(crawl_record_id INTEGER, platform_id TEXT, status TEXT);
                    """
                )
                conn.execute("INSERT INTO platforms VALUES ('weibo', '微博')")
                conn.execute("INSERT INTO news_items VALUES (1, '真实热点', 'weibo', 2, '', '', '12-30')")
                conn.execute("INSERT INTO news_items VALUES (2, '第十一名热点', 'weibo', 11, '', '', '12-30')")
                conn.execute("INSERT INTO crawl_records VALUES (7, '12-30')")
                conn.execute("INSERT INTO crawl_source_status VALUES (7, 'weibo', 'success')")
                conn.commit()
            items, statuses = export_crawl(database, "12-30")
            self.assertEqual(items[0]["id"], "weibo:1")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["url"], "")
            self.assertEqual(items[0]["observed_at"], "2026-07-14T12:30:00+08:00")
            self.assertEqual(statuses, [{"platform_id": "weibo", "status": "success"}])

    def test_real_sqlite_crawl_preserves_all_trendradar_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "2026-07-14.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE platforms(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                    CREATE TABLE news_items(
                        id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER,
                        url TEXT, mobile_url TEXT, last_crawl_time TEXT,
                        event_detail TEXT, extra_metadata TEXT
                    );
                    CREATE TABLE crawl_records(id INTEGER PRIMARY KEY, crawl_time TEXT UNIQUE);
                    CREATE TABLE crawl_source_status(crawl_record_id INTEGER, platform_id TEXT, status TEXT);
                    """
                )
                conn.execute("INSERT INTO platforms VALUES ('weibo', '微博')")
                conn.execute(
                    "INSERT INTO news_items VALUES (1, '真实热点', 'weibo', 2, '', '', '12-30', ?, ?)",
                    ("TrendRadar 已采集的事件详情", '{\"origin\":\"TrendRadar\"}'),
                )
                conn.execute("INSERT INTO crawl_records VALUES (7, '12-30')")
                conn.execute("INSERT INTO crawl_source_status VALUES (7, 'weibo', 'success')")
                conn.commit()
            items, _statuses = export_crawl(database, "12-30")
            record = items[0]["trendradar_record"]
            self.assertEqual(record["event_detail"], "TrendRadar 已采集的事件详情")
            self.assertEqual(record["extra_metadata"], '{\"origin\":\"TrendRadar\"}')

    def test_original_link_reader_uses_trendradar_reader_and_returns_body(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"success": True, "data": {"content": "热点事件正文", "format": "markdown"}}),
            stderr="",
        )
        with patch("scripts.integrations.trendradar_runtime.verify_official_install", return_value={"python_executable": "reader-python"}), patch(
            "scripts.integrations.trendradar_runtime.subprocess.run", return_value=completed
        ) as run:
            result = read_original_article(
                trendradar_dir=Path("vendor/TrendRadar"),
                url="https://example.test/original-event",
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["event_detail"]["content"], "热点事件正文")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_original_link_reader_does_not_search_or_retry_after_an_empty_read(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"success": True, "data": {"content": ""}}), stderr=""
        )
        with patch("scripts.integrations.trendradar_runtime.verify_official_install", return_value={"python_executable": "reader-python"}), patch(
            "scripts.integrations.trendradar_runtime.subprocess.run", return_value=completed
        ) as run:
            result = read_original_article(
                trendradar_dir=Path("vendor/TrendRadar"),
                url="https://example.test/original-event",
            )
        self.assertEqual(result, {"status": "unavailable", "reason": "original_link_reader_returned_empty_content"})
        self.assertEqual(run.call_count, 1)

    def test_original_link_reader_records_a_subprocess_exit_without_a_retry(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=7, stdout="", stderr="reader startup failed")
        with patch("scripts.integrations.trendradar_runtime.verify_official_install", return_value={"python_executable": "reader-python"}), patch(
            "scripts.integrations.trendradar_runtime.subprocess.run", return_value=completed
        ) as run:
            result = read_original_article(
                trendradar_dir=Path("vendor/TrendRadar"),
                url="https://example.test/original-event",
            )
        self.assertEqual(result, {"status": "unavailable", "reason": "original_link_reader_failed_exit_7"})
        self.assertEqual(run.call_count, 1)

    def test_changed_crawl_requires_new_or_updated_real_record(self) -> None:
        old = {("one.db", "10-00"): (10, "2026-07-14 10:00:01")}
        self.assertIsNone(changed_crawl_record(old, dict(old)))
        current = dict(old)
        current[("one.db", "10-01")] = (9, "2026-07-14 10:01:02")
        self.assertEqual(changed_crawl_record(old, current), (Path("one.db"), "10-01"))

    def test_install_check_allows_runtime_outputs_but_rejects_source_changes(self) -> None:
        self.assertTrue(approved_runtime_worktree_change(" M config/config.yaml"))
        self.assertTrue(approved_runtime_worktree_change("M config/config.yaml"))
        self.assertTrue(approved_runtime_worktree_change("?? output/news/2026-07-14.db"))
        self.assertTrue(approved_runtime_worktree_change("?? trendradar/core/__pycache__/runner.pyc"))
        self.assertFalse(approved_runtime_worktree_change(" M trendradar/core/runner.py"))
        self.assertFalse(approved_runtime_worktree_change("?? unexpected.py"))
        self.assertFalse(approved_runtime_worktree_change("R  trendradar/a.py -> output/a.py"))

    def test_current_official_install_remains_valid_after_a_real_output_exists(self) -> None:
        install = Path("vendor/TrendRadar")
        if not install.is_dir():
            self.skipTest("repository-local TrendRadar install is not present")
        identity = verify_official_install(install)
        self.assertEqual(identity["origin"], "https://github.com/sansan0/TrendRadar.git")
        self.assertTrue(identity["python_executable"].endswith("python.exe"))

    def test_execution_baseline_forbids_test_evidence_from_claiming_live_completion(self) -> None:
        baseline = Path("docs/IMPLEMENTATION_EXECUTION_BASELINE.md").read_text(encoding="utf-8")
        self.assertIn("Mock、fixture、fake provider、文件存在或 pytest 通过最高只能达到 `TECH_TESTED`", baseline)
        for level in ("DESIGNED", "SCAFFOLDED", "TECH_TESTED", "ENV_READY", "LIVE_VALIDATED", "PRODUCTION_READY"):
            self.assertIn(f"`{level}`", baseline)


if __name__ == "__main__":
    unittest.main()
