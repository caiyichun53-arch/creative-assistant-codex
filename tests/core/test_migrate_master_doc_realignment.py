from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.migrate_master_doc_realignment import migrate, reset_judgement_state, wipe_video_data

# Mirrors the pre-2026-07-07 schema shape well enough to exercise the migration
# (is_pinned/status columns, no first_contact_category/video_checks.discovery_batch_index).
OLD_SCHEMA = """
CREATE TABLE competitor_accounts(
    account_id TEXT PRIMARY KEY, platform TEXT, domain_label TEXT, domain_name TEXT,
    account_name TEXT, sec_uid TEXT, homepage_url TEXT, source_config_ref TEXT,
    registration_status TEXT, first_crawl_policy TEXT, comments_policy TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE competitor_videos(
    video_id TEXT PRIMARY KEY, account_id TEXT, platform TEXT, platform_item_id TEXT,
    title TEXT, url TEXT, publish_time TEXT, duration_sec INTEGER,
    like_count INTEGER, comment_count INTEGER, share_count INTEGER, collect_count INTEGER,
    is_pinned INTEGER, excluded_reason TEXT, status TEXT, registration_run_id TEXT,
    raw_archive_ref TEXT, raw_json TEXT, first_seen_at TEXT, last_checked_at TEXT, check_count INTEGER
);
CREATE TABLE baselines(baseline_id TEXT PRIMARY KEY, account_id TEXT);
CREATE TABLE hits(hit_id TEXT PRIMARY KEY, account_id TEXT);
CREATE TABLE video_checks(check_id TEXT PRIMARY KEY, video_id TEXT);
"""


class MigrateMasterDocRealignmentTests(unittest.TestCase):
    def test_reclassifies_existing_videos_and_backs_up_first(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "formal" / "old.sqlite3"
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(db_path)
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO competitor_accounts VALUES('acc1','douyin','d','d','a','s','h','c','active','p','p', ?, ?)",
                (now.isoformat(), now.isoformat()),
            )
            # one historical_mature (30 days old), one transition (3 days old), one missing publish_time
            conn.execute(
                "INSERT INTO competitor_videos VALUES('v1','acc1','douyin','p1','t','u',?,NULL,10,1,1,1,0,NULL,'archived','r',NULL,'{}',?,?,0)",
                ((now - timedelta(days=30)).isoformat(), now.isoformat(), now.isoformat()),
            )
            conn.execute(
                "INSERT INTO competitor_videos VALUES('v2','acc1','douyin','p2','t','u',?,NULL,20,2,2,2,0,NULL,'watching','r',NULL,'{}',?,?,0)",
                ((now - timedelta(days=3)).isoformat(), now.isoformat(), now.isoformat()),
            )
            conn.execute(
                "INSERT INTO competitor_videos VALUES('v3','acc1','douyin','p3','t','u',NULL,NULL,5,0,0,0,0,NULL,'archived','r',NULL,'{}',?,?,0)",
                (now.isoformat(), now.isoformat()),
            )
            conn.commit()
            conn.close()

            # _safe_db_path only allows paths under the real repo's data/formal --
            # this test deliberately runs in a temp dir, so bypass just that guard;
            # the guard's own behavior is exercised by run_competitor_registration_full's
            # own tests, not re-tested here.
            with patch("scripts.core.business_data.migrate_master_doc_realignment._safe_db_path", side_effect=lambda p: p):
                report = migrate(db_path)

            self.assertTrue(Path(report["backup_path"]).exists())
            self.assertEqual(report["old_row_count"], 3)
            self.assertEqual(report["migrated_video_count"], 3)
            self.assertEqual(report["category_counts"], {"historical_mature": 1, "transition": 1})
            self.assertEqual(report["missing_publish_time"], 1)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute("PRAGMA table_info(competitor_videos)").fetchall()}
            self.assertIn("first_contact_category", columns)
            self.assertNotIn("is_pinned", columns)
            self.assertNotIn("status", columns)
            rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM competitor_videos").fetchall()}
            self.assertEqual(rows["v1"]["first_contact_category"], "historical_mature")
            self.assertEqual(rows["v2"]["first_contact_category"], "transition")
            self.assertIsNone(rows["v3"]["first_contact_category"])
            self.assertEqual(rows["v3"]["excluded_reason"], "missing_publish_time")
            self.assertEqual(conn.execute("SELECT count(*) FROM video_checks").fetchone()[0], 2)
            conn.close()

    def test_wipe_video_data_empties_videos_but_keeps_accounts_and_backs_up(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "formal" / "old.sqlite3"
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(db_path)
            conn.executescript(OLD_SCHEMA)
            conn.execute(
                "INSERT INTO competitor_accounts VALUES('acc1','douyin','d','d','a','s','h','c','active','p','p', ?, ?)",
                (now.isoformat(), now.isoformat()),
            )
            conn.execute(
                "INSERT INTO competitor_videos VALUES('v1','acc1','douyin','p1','t','u',?,NULL,10,1,1,1,0,NULL,'archived','r',NULL,'{}',?,?,0)",
                ((now - timedelta(days=30)).isoformat(), now.isoformat(), now.isoformat()),
            )
            conn.execute("INSERT INTO baselines VALUES('bl1','acc1')")
            conn.execute("INSERT INTO hits VALUES('h1','acc1')")
            conn.commit()
            conn.close()

            with patch("scripts.core.business_data.migrate_master_doc_realignment._safe_db_path", side_effect=lambda p: p):
                report = wipe_video_data(db_path)

            self.assertTrue(Path(report["backup_path"]).exists())
            self.assertEqual(report["wiped_video_count"], 1)
            self.assertEqual(report["wiped_baseline_count"], 1)
            self.assertEqual(report["wiped_hit_count"], 1)

            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM baselines").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM hits").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM competitor_accounts").fetchone()[0], 1)
            conn.close()

    def test_reset_judgement_state_clears_hits_but_keeps_raw_video_data(self) -> None:
        # 2026-07-08: a genuine threshold/rule change invalidates hits computed
        # under the old rule -- this clears hits/baselines/candidate fields but
        # must NOT touch raw crawl fields (like_count, first_contact_category,
        # etc.), so a subsequent --rejudge-only can recompute cleanly without a
        # re-crawl.
        from scripts.core.business_data.register_competitor_accounts import install_schema

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "formal" / "new.sqlite3"
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(db_path)
            install_schema(conn)
            conn.execute(
                """
                INSERT INTO competitor_accounts(
                    account_id, platform, domain_label, domain_name, account_name, sec_uid,
                    homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
                ) VALUES('acc1','douyin','d','d','a','s','h','c','active','p','p')
                """
            )
            conn.execute(
                """
                INSERT INTO competitor_videos(
                    video_id, account_id, platform, platform_item_id, title, url, publish_time,
                    like_count, comment_count, first_contact_category, first_trigger_at,
                    first_trigger_observation, trigger_rules, baseline_mode, judgment_confidence, raw_json
                ) VALUES('v1','acc1','douyin','p1','t','u',?,500,100,'historical_mature',?,
                         'historical_mature','["x"]','mature_history','rough','{}')
                """,
                ((now - timedelta(days=30)).isoformat(), now.isoformat()),
            )
            conn.execute("INSERT INTO baselines VALUES('bl1','acc1','mature_history','like_count',NULL,20,100.0,'run1',?)", (now.isoformat(),))
            conn.execute(
                "INSERT INTO hits VALUES('h1','v1','acc1','douyin','p1','t','u',?,500,100,NULL,NULL,'like_anomaly:5.00x','rough',NULL,'run1','none',?)",
                ((now - timedelta(days=30)).isoformat(), now.isoformat()),
            )
            conn.commit()
            conn.close()

            with patch("scripts.core.business_data.migrate_master_doc_realignment._safe_db_path", side_effect=lambda p: p):
                report = reset_judgement_state(db_path)

            self.assertTrue(Path(report["backup_path"]).exists())
            self.assertEqual(report["cleared_hit_count"], 1)
            self.assertEqual(report["cleared_baseline_count"], 1)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT count(*) FROM hits").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM baselines").fetchone()[0], 0)
            row = conn.execute("SELECT * FROM competitor_videos WHERE video_id='v1'").fetchone()
            self.assertIsNone(row["first_trigger_at"])
            self.assertIsNone(row["trigger_rules"])
            self.assertIsNone(row["judgment_confidence"])
            # Raw crawl fields survive untouched.
            self.assertEqual(row["like_count"], 500)
            self.assertEqual(row["comment_count"], 100)
            self.assertEqual(row["first_contact_category"], "historical_mature")
            conn.close()


if __name__ == "__main__":
    unittest.main()
