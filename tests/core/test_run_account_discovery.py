from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema as install_competitor_schema
from scripts.core.business_data.run_account_discovery import (
    ACCOUNT_REVIEW_MIN_VIDEOS,
    find_accounts_due_for_review,
    list_pending_account_reviews,
    resolve_account_review,
    validate_account_discovery_execution_contract,
)
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    install_domain_search_schema(conn)
    return conn


def _insert_tag(conn: sqlite3.Connection, tag_id: str = "tag1", domain_label: str = "fan_kepu_social_life") -> None:
    conn.execute(
        "INSERT INTO domain_search_tags(tag_id, tag, domain_label, status, source, human_review_status) "
        "VALUES (?, ?, ?, 'active', 'sources_yaml', 'approved')",
        (tag_id, tag_id, domain_label),
    )
    conn.commit()


def _insert_discovered_video(
    conn: sqlite3.Connection, *, video_id: str, account_platform_id: str, domain_label: str = "fan_kepu_social_life",
    is_tracked: int = 0, discovered_at: str | None = None, tag_id: str = "tag1",
) -> None:
    conn.execute(
        """
        INSERT INTO discovered_external_videos(
            discovered_video_id, platform, platform_item_id, account_handle, account_platform_id,
            is_tracked_account, tag_id, domain_label, title, raw_json, discovered_at, run_id
        ) VALUES (?, 'douyin', ?, ?, ?, ?, ?, ?, 't', '{}', ?, 'run1')
        """,
        (
            video_id, video_id, account_platform_id, account_platform_id, is_tracked, tag_id, domain_label,
            discovered_at or datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_collect_009(self) -> None:
        contract = validate_account_discovery_execution_contract()
        self.assertIn("BR-COLLECT-009", contract)


class FindAccountsDueForReviewTests(unittest.TestCase):
    def test_three_distinct_videos_triggers_a_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_x")
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0]["account_platform_id"], "acc_x")
                row = conn.execute("SELECT * FROM discovered_account_review WHERE account_platform_id='acc_x'").fetchone()
                self.assertEqual(row["video_count"], ACCOUNT_REVIEW_MIN_VIDEOS)
                self.assertEqual(row["disposition"], "pending")
            finally:
                conn.close()

    def test_two_distinct_videos_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS - 1):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_x")
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(created, [])
            finally:
                conn.close()

    def test_tracked_account_videos_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_tracked", is_tracked=1)
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(created, [], "an already-tracked account must never trigger a discovery review")
            finally:
                conn.close()

    def test_videos_outside_the_30_day_window_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                old_time = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_x", discovered_at=old_time)
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(created, [])
            finally:
                conn.close()

    def test_duplicate_platform_item_id_does_not_count_twice(self) -> None:
        # video_count uses COUNT(DISTINCT platform_item_id) -- the same video
        # discovered via two different tags must not double-count.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn, "tag1")
                _insert_tag(conn, "tag2")
                _insert_discovered_video(conn, video_id="v_shared", account_platform_id="acc_x", tag_id="tag1")
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(created, [])  # only 1 distinct video, below threshold
            finally:
                conn.close()

    def test_running_twice_does_not_create_a_second_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_x")
                find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run2")
                rows = conn.execute("SELECT * FROM discovered_account_review WHERE account_platform_id='acc_x'").fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()


class ResolveAccountReviewTests(unittest.TestCase):
    def _create_review(self, conn: sqlite3.Connection) -> str:
        _insert_tag(conn)
        for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
            _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_x")
        find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
        return conn.execute("SELECT review_id FROM discovered_account_review WHERE account_platform_id='acc_x'").fetchone()["review_id"]

    def test_added_disposition_records_no_ignored_until(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                result = resolve_account_review(conn, review_id=review_id, disposition="added", note="值得追踪")
                self.assertEqual(result["disposition"], "added")
                self.assertIsNone(result["ignored_until"])
                row = conn.execute("SELECT * FROM discovered_account_review WHERE review_id=?", (review_id,)).fetchone()
                self.assertEqual(row["resolved_note"], "值得追踪")
                self.assertIsNotNone(row["resolved_at"])
            finally:
                conn.close()

    def test_ignored_30d_sets_a_real_future_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                now = datetime.now(timezone.utc)
                result = resolve_account_review(conn, review_id=review_id, disposition="ignored_30d", now=now)
                self.assertIsNotNone(result["ignored_until"])
                self.assertGreater(result["ignored_until"], now.isoformat())
            finally:
                conn.close()

    def test_ignored_30d_account_does_not_retrigger_within_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                resolve_account_review(conn, review_id=review_id, disposition="ignored_30d")
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run2")
                self.assertEqual(created, [])
            finally:
                conn.close()

    def test_ignored_30d_account_retriggers_after_the_window_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                past = datetime.now(timezone.utc) - timedelta(days=31)
                resolve_account_review(conn, review_id=review_id, disposition="ignored_30d", now=past)
                # Fresh discoveries within the (new) 30-day window keep the account eligible.
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run2")
                self.assertEqual(len(created), 1)
            finally:
                conn.close()

    def test_ignored_permanently_never_retriggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                resolve_account_review(conn, review_id=review_id, disposition="ignored_permanently")
                # Even brand-new discoveries later must not resurrect this account.
                _insert_discovered_video(conn, video_id="v_new", account_platform_id="acc_x")
                created = find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run2")
                self.assertEqual(created, [])
            finally:
                conn.close()

    def test_unsupported_disposition_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                review_id = self._create_review(conn)
                with self.assertRaises(ValueError):
                    resolve_account_review(conn, review_id=review_id, disposition="not_a_real_disposition")
            finally:
                conn.close()

    def test_unknown_review_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    resolve_account_review(conn, review_id="not_real", disposition="added")
            finally:
                conn.close()


class ListPendingAccountReviewsTests(unittest.TestCase):
    def test_lists_only_pending_dispositions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_tag(conn)
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"v{i}", account_platform_id="acc_pending")
                for i in range(ACCOUNT_REVIEW_MIN_VIDEOS):
                    _insert_discovered_video(conn, video_id=f"w{i}", account_platform_id="acc_resolved")
                find_accounts_due_for_review(conn, domain_label="fan_kepu_social_life", run_id="run1")
                resolved_id = conn.execute("SELECT review_id FROM discovered_account_review WHERE account_platform_id='acc_resolved'").fetchone()["review_id"]
                resolve_account_review(conn, review_id=resolved_id, disposition="added")

                pending = list_pending_account_reviews(conn, domain_label="fan_kepu_social_life")
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["account_platform_id"], "acc_pending")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
