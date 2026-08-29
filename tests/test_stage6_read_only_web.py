from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from scripts.core.core_entry import CoreStartupError, build_status
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.web import read_only_server
from scripts.web.read_only_server import ReadOnlyWebApplication, create_server
from tests.test_core_unified_status import _seed_domain


FORMAL_DATABASE = Path(r"I:\Creation_assistant-runtime\formal\production_activation.sqlite3")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _get_json(url: str) -> tuple[int, dict]:
    try:
        response = urlopen(url)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    with response:
        return response.status, json.loads(response.read().decode("utf-8"))


class Stage6ReadOnlyWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(port=0, data_identity="production")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_local_page_and_api_are_reachable_and_match_core(self) -> None:
        page_status, page_payload = _get_json(self.base_url + "/missing")
        self.assertEqual(page_status, 404)
        self.assertFalse(page_payload["ok"])

        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Creation Assistant", html)
            self.assertNotIn("/api/status", html)

        expected = build_status()
        status, payload = _get_json(self.base_url + "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], expected)

    def test_formal_page_data_has_current_history_boundary(self) -> None:
        _, payload = _get_json(self.base_url + "/api/status")
        status = payload["status"]
        self.assertEqual(status["system"]["runtime_identity"], "FORMAL")
        self.assertEqual(status["business_runs"]["in_progress"], [])
        self.assertEqual(status["history"]["unfinished_business_runs_total"], 9)
        self.assertIn("historical/non-current", status["human_summary"])
        for domain in status["domains"]:
            self.assertFalse(domain["current_activation"]["exists"])
            self.assertEqual(domain["current_cold_start"]["status"], "\u672acold-start")
            self.assertFalse(domain["current_daily"]["exists"])
            self.assertFalse(domain["waiting_human"])
            self.assertEqual(domain["in_progress_business_runs"], [])

    def test_waiting_human_and_daily_lifecycles_are_returned_unchanged_from_core(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "web-status.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                _seed_domain(core, "waiting", cold_status="waiting_human", waiting_registration=True)
                for lifecycle in ("running", "stopped", "failed", "completed"):
                    _seed_domain(core, lifecycle, daily_lifecycle=lifecycle)
                core.conn.commit()
            finally:
                core.close()

            expected = build_status(data_identity="test", database_path=database)
            server = create_server(port=0, data_identity="test", database_path=database)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/status"
                status, payload = _get_json(url)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], expected)
        by_domain = {item["domain_identity"]: item for item in payload["status"]["domains"]}
        self.assertTrue(by_domain["waiting"]["waiting_human"])
        self.assertEqual(by_domain["waiting"]["waiting_for"][0]["kind"], "competitor_registration_review")
        for lifecycle in ("running", "stopped", "failed", "completed"):
            self.assertEqual(by_domain[lifecycle]["current_daily"]["lifecycle"], lifecycle)

    def test_test_identity_is_fixed_and_cannot_read_formal(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "test-only.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            core.close()
            app = ReadOnlyWebApplication(data_identity="test", database_path=database)
            status = app.read_status()
            self.assertEqual(status["system"]["runtime_identity"], "TEST")
            self.assertIsNone(status["formal_database"])

        isolated_app = ReadOnlyWebApplication(data_identity="test", database_path=FORMAL_DATABASE)
        with self.assertRaises(CoreStartupError):
            isolated_app.read_status()

    def test_core_failure_is_reported_without_database_fallback(self) -> None:
        with patch.object(read_only_server, "build_status", side_effect=RuntimeError("Core unavailable")):
            status, payload = _get_json(self.base_url + "/api/status")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source"], "Creation Assistant Core")
        self.assertIn("\u72b6\u6001\u8bfb\u53d6\u5931\u8d25", payload["error"])

    def test_page_has_no_business_operations_and_write_methods_are_rejected(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
        self.assertNotIn("<button", html)
        self.assertNotIn("<form", html)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = Request(self.base_url + "/api/status", method=method)
            try:
                urlopen(request)
            except HTTPError as exc:
                self.assertEqual(exc.code, 405)
            else:
                self.fail(f"{method} must be rejected")

    def test_reading_page_and_status_does_not_modify_formal_database(self) -> None:
        before = _sha256(FORMAL_DATABASE)
        with urlopen(self.base_url + "/"):
            pass
        _get_json(self.base_url + "/api/status")
        after = _sha256(FORMAL_DATABASE)
        self.assertEqual(before, "B8D5A61E656798C485DC51DF64623F3985617C9B9A1BB1ECB2D3C38FDBA174F8")
        self.assertEqual(after, before)

    def test_frontend_is_a_static_core_status_renderer(self) -> None:
        html = (read_only_server.STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (read_only_server.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('/static/app.js', html)
        self.assertIn('fetch("/api/status"', javascript)
        for field in (
            "runtime_identity",
            "migration_protection",
            "daily_schedule",
            "default_executor",
            "current_activation",
            "current_cold_start",
            "waiting_human",
            "current_daily",
            "in_progress_business_runs",
            "unfinished_business_runs",
        ):
            self.assertIn(field, javascript)
        self.assertNotIn("sqlite", javascript.lower())


if __name__ == "__main__":
    unittest.main()
