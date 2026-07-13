from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema as install_competitor_schema
from scripts.core.business_data.run_domain_search import (
    CONSECUTIVE_CYCLES_BEFORE_PAUSE,
    deterministic_filter,
    extract_hashtags,
    install_schema,
    load_sources_yaml,
    rank_and_select_for_deep_processing,
    record_search_cycle_result,
    search_one_tag,
    seed_active_tags_from_sources_yaml,
    select_tags_due_for_search,
    suggest_tags_from_hit_library,
    validate_domain_search_execution_contract,
)
from scripts.core.experience.review_queue import set_review_status
from scripts.core.external_adapters import ExternalCommandResult


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    install_schema(conn)
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str, *, domain_label: str = "fan_kepu_social_life", sec_uid: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        ) VALUES (?, 'douyin', ?, 'domain', ?, ?, 'https://x', 'cfg', 'active',
                  'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, domain_label, account_id, sec_uid or (account_id + "_sec")),
    )
    conn.commit()


def _insert_video(conn: sqlite3.Connection, video_id: str, account_id: str, *, desc: str) -> None:
    conn.execute(
        "INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json) "
        "VALUES (?, ?, 'douyin', ?, 't', 'https://x', ?)",
        (video_id, account_id, video_id + "_item", json.dumps({"desc": desc})),
    )
    conn.commit()


class LoadSourcesYamlTests(unittest.TestCase):
    def test_parses_a_real_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text("domain_label: fan_kepu_social_life\nactive_tags: [科普, 涨知识]\n", encoding="utf-8")
            data = load_sources_yaml(path)
        self.assertEqual(data["domain_label"], "fan_kepu_social_life")
        self.assertEqual(data["active_tags"], ["科普", "涨知识"])

    def test_unknown_domain_label_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text("domain_label: not_a_real_domain\nactive_tags: []\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_sources_yaml(path)

    def test_missing_active_tags_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.yaml"
            path.write_text("domain_label: fan_kepu_social_life\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_sources_yaml(path)


class SeedActiveTagsTests(unittest.TestCase):
    def test_seeds_real_tags_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                result = seed_active_tags_from_sources_yaml(
                    conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普", "涨知识"]}
                )
                self.assertEqual(result["inserted"], 2)
                rows = conn.execute("SELECT * FROM domain_search_tags WHERE domain_label='fan_kepu_social_life'").fetchall()
                self.assertEqual({r["tag"] for r in rows}, {"科普", "涨知识"})
                self.assertTrue(all(r["status"] == "active" and r["source"] == "sources_yaml" for r in rows))
                cursor_rows = conn.execute("SELECT * FROM domain_search_cursor").fetchall()
                self.assertEqual(len(cursor_rows), 2)
            finally:
                conn.close()

    def test_re_seeding_the_same_tag_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                cfg = {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]}
                seed_active_tags_from_sources_yaml(conn, cfg)
                result = seed_active_tags_from_sources_yaml(conn, cfg)
                self.assertEqual(result["inserted"], 0)
                rows = conn.execute("SELECT * FROM domain_search_tags").fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()


class ExtractHashtagsTests(unittest.TestCase):
    def test_extracts_multiple_glued_tags(self) -> None:
        self.assertEqual(extract_hashtags("有钱人 #商业思维#消费#极氪8X"), ["商业思维", "消费", "极氪8X"])

    def test_no_tags_returns_empty(self) -> None:
        self.assertEqual(extract_hashtags("没有标签的文本"), [])


class SuggestTagsFromHitLibraryTests(unittest.TestCase):
    def test_long_tags_are_filtered_as_campaign_tags(self) -> None:
        # Real-data-validated rule from A2: tags >=6 chars are almost always
        # platform campaign tags, not reusable domain topics.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                _insert_video(conn, "v1", "acc1", desc="标题 #房产中介 #知识前沿派对")
                result = suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                tags = {r["tag"] for r in conn.execute("SELECT tag FROM domain_search_tags").fetchall()}
                self.assertIn("房产中介", tags)
                self.assertNotIn("知识前沿派对", tags)
                self.assertEqual(result["suggested"], 1)
            finally:
                conn.close()

    def test_high_frequency_tags_are_filtered_as_too_generic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                # 20 videos share a generic tag; 1 video has a distinguishing tag.
                for i in range(20):
                    _insert_video(conn, f"v{i}", "acc1", desc="#科普")
                _insert_video(conn, "v_distinct", "acc1", desc="#房产中介")
                suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                tags = {r["tag"] for r in conn.execute("SELECT tag FROM domain_search_tags").fetchall()}
                self.assertNotIn("科普", tags, "high-frequency generic tag must be filtered out")
                self.assertIn("房产中介", tags)
            finally:
                conn.close()

    def test_suggested_tags_are_not_auto_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                _insert_video(conn, "v1", "acc1", desc="#房产中介")
                suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag='房产中介'").fetchone()
                self.assertEqual(row["status"], "suggested")
                self.assertEqual(row["source"], "discovered")
            finally:
                conn.close()

    def test_zero_hits_produces_zero_suggestions_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                result = suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                self.assertEqual(result["suggested"], 0)
            finally:
                conn.close()


class TagConfirmationViaReviewQueueTests(unittest.TestCase):
    """A3's real integration point: review_queue.py --approve tag <id> is
    what promotes a suggested tag to active (domain_search_tags_promote_on_
    approval trigger), not a direct status write anywhere."""

    def test_approving_a_suggested_tag_promotes_it_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                _insert_video(conn, "v1", "acc1", desc="#房产中介")
                suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags WHERE tag='房产中介'").fetchone()["tag_id"]

                set_review_status(conn, stage="tag", item_id=tag_id, status="approved", note="人工确认")

                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag_id=?", (tag_id,)).fetchone()
                self.assertEqual(row["status"], "active")
                self.assertEqual(row["human_review_status"], "approved")
            finally:
                conn.close()

    def test_rejecting_a_suggested_tag_does_not_promote_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                _insert_video(conn, "v1", "acc1", desc="#房产中介")
                suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags WHERE tag='房产中介'").fetchone()["tag_id"]

                set_review_status(conn, stage="tag", item_id=tag_id, status="rejected", note="不相关")

                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag_id=?", (tag_id,)).fetchone()
                self.assertEqual(row["status"], "suggested", "a rejected tag must not be promoted")
                self.assertEqual(row["human_review_status"], "rejected")
            finally:
                conn.close()

    def test_sources_yaml_tags_never_appear_in_the_pending_tag_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                from scripts.core.experience.review_queue import list_pending
                pending = list_pending(conn, stage="tag")
                self.assertEqual(pending, [], "sources.yaml tags are pre-approved, not pending review")
            finally:
                conn.close()


class SelectTagsDueForSearchTests(unittest.TestCase):
    def test_never_searched_tag_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                due = select_tags_due_for_search(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual([r["tag"] for r in due], ["科普"])
            finally:
                conn.close()

    def test_recently_searched_tag_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags").fetchone()["tag_id"]
                record_search_cycle_result(conn, tag_id=tag_id, run_id="run1", produced_validated_topic=False, now=datetime.now(timezone.utc))
                due = select_tags_due_for_search(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual(due, [])
            finally:
                conn.close()

    def test_tag_searched_over_7_days_ago_is_due_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags").fetchone()["tag_id"]
                old_time = datetime.now(timezone.utc) - timedelta(days=8)
                record_search_cycle_result(conn, tag_id=tag_id, run_id="run1", produced_validated_topic=False, now=old_time)
                due = select_tags_due_for_search(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual([r["tag"] for r in due], ["科普"])
            finally:
                conn.close()

    def test_suggested_status_tags_are_never_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "acc1")
                _insert_video(conn, "v1", "acc1", desc="#房产中介")
                suggest_tags_from_hit_library(conn, domain_label="fan_kepu_social_life", run_id="run1")
                due = select_tags_due_for_search(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual(due, [])
            finally:
                conn.close()


class DeterministicFilterTests(unittest.TestCase):
    def test_keeps_items_matching_the_tag_and_not_already_discovered(self) -> None:
        items = [
            {"aweme_id": "1", "desc": "有 #房产中介 的内容"},
            {"aweme_id": "2", "desc": "跟标签无关的内容"},
        ]
        kept = deterministic_filter(items, tag="房产中介", already_discovered_ids=set())
        self.assertEqual([i["aweme_id"] for i in kept], ["1"])

    def test_already_discovered_ids_are_excluded(self) -> None:
        items = [{"aweme_id": "1", "desc": "#房产中介"}]
        kept = deterministic_filter(items, tag="房产中介", already_discovered_ids={"1"})
        self.assertEqual(kept, [])

    def test_duplicate_ids_within_the_same_batch_are_deduped(self) -> None:
        items = [{"aweme_id": "1", "desc": "#房产中介"}, {"aweme_id": "1", "desc": "#房产中介"}]
        kept = deterministic_filter(items, tag="房产中介", already_discovered_ids=set())
        self.assertEqual(len(kept), 1)


class RankAndSelectTests(unittest.TestCase):
    def test_ranks_by_engagement_and_caps_at_limit(self) -> None:
        items = [{"aweme_id": str(i), "liked_count": i, "comment_count": 0} for i in range(10)]
        selected = rank_and_select_for_deep_processing(items, limit=5)
        self.assertEqual(len(selected), 5)
        self.assertEqual([i["aweme_id"] for i in selected], ["9", "8", "7", "6", "5"])


class RecordSearchCycleResultTests(unittest.TestCase):
    def test_producing_a_validated_topic_resets_the_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags").fetchone()["tag_id"]
                record_search_cycle_result(conn, tag_id=tag_id, run_id="run1", produced_validated_topic=False)
                result = record_search_cycle_result(conn, tag_id=tag_id, run_id="run2", produced_validated_topic=True)
                self.assertEqual(result["consecutive_cycles_without_validated_topic"], 0)
                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag_id=?", (tag_id,)).fetchone()
                self.assertEqual(row["status"], "active")
            finally:
                conn.close()

    def test_three_consecutive_cycles_without_a_topic_pauses_the_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags").fetchone()["tag_id"]
                result = None
                for i in range(CONSECUTIVE_CYCLES_BEFORE_PAUSE):
                    result = record_search_cycle_result(conn, tag_id=tag_id, run_id=f"run{i}", produced_validated_topic=False)
                self.assertTrue(result["paused"])
                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag_id=?", (tag_id,)).fetchone()
                self.assertEqual(row["status"], "suggested_pause")
            finally:
                conn.close()

    def test_two_cycles_without_a_topic_does_not_pause_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["科普"]})
                tag_id = conn.execute("SELECT tag_id FROM domain_search_tags").fetchone()["tag_id"]
                result = None
                for i in range(CONSECUTIVE_CYCLES_BEFORE_PAUSE - 1):
                    result = record_search_cycle_result(conn, tag_id=tag_id, run_id=f"run{i}", produced_validated_topic=False)
                self.assertFalse(result["paused"])
                row = conn.execute("SELECT * FROM domain_search_tags WHERE tag_id=?", (tag_id,)).fetchone()
                self.assertEqual(row["status"], "active")
            finally:
                conn.close()


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_effective_baseline_topic_sections(self) -> None:
        contract = validate_domain_search_execution_contract({"live_enabled": True})
        self.assertTrue({"3", "17"}.issubset(contract))

    def test_live_disabled_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_domain_search_execution_contract({"live_enabled": False})

    def test_missing_live_enabled_key_defaults_to_disabled(self) -> None:
        with self.assertRaises(ValueError):
            validate_domain_search_execution_contract({})


class _FakeExecutor:
    def __init__(self, result: ExternalCommandResult):
        self.result = result
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return self.result


class SearchOneTagTests(unittest.TestCase):
    def test_discovers_videos_and_marks_tracked_accounts_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, "tracked_acc", sec_uid="tracked_sec")
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["房产中介"]})
                tag_row = conn.execute("SELECT * FROM domain_search_tags WHERE tag='房产中介'").fetchone()

                executor = _FakeExecutor(
                    ExternalCommandResult(
                        status="succeeded",
                        payload={
                            "items": [
                                {"aweme_id": "v_tracked", "desc": "#房产中介 真实内容", "sec_uid": "tracked_sec", "liked_count": 100},
                                {"aweme_id": "v_untracked", "desc": "#房产中介 另一条", "sec_uid": "untracked_sec", "liked_count": 50},
                            ]
                        },
                        raw_archive_ref="fixture://x",
                        external_side_effect=True,
                    )
                )

                result = search_one_tag(
                    conn, executor, tag_row, domain_label="fan_kepu_social_life", run_id="run_test",
                    domain_search_cfg={"live_enabled": True},
                )

                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["inserted"], 2)
                rows = {r["platform_item_id"]: r for r in conn.execute("SELECT * FROM discovered_external_videos").fetchall()}
                self.assertEqual(rows["v_tracked"]["is_tracked_account"], 1)
                self.assertEqual(rows["v_untracked"]["is_tracked_account"], 0)
            finally:
                conn.close()

    def test_live_disabled_blocks_the_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                seed_active_tags_from_sources_yaml(conn, {"domain_label": "fan_kepu_social_life", "active_tags": ["房产中介"]})
                tag_row = conn.execute("SELECT * FROM domain_search_tags WHERE tag='房产中介'").fetchone()
                executor = _FakeExecutor(ExternalCommandResult(status="succeeded", payload={"items": []}))
                with self.assertRaises(ValueError):
                    search_one_tag(
                        conn, executor, tag_row, domain_label="fan_kepu_social_life", run_id="run_test",
                        domain_search_cfg={"live_enabled": False},
                    )
                self.assertEqual(executor.commands, [], "no real call should have been attempted")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
