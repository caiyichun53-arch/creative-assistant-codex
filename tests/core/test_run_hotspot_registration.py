from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema as install_competitor_schema
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema
from scripts.core.business_data.run_hotspot_registration import (
    match_domain_policy_terms,
    register_hotspot_event,
    validate_hotspot_registration_execution_contract,
)


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    install_domain_search_schema(conn)
    return conn


def _insert_active_tag(conn: sqlite3.Connection, tag: str, domain_label: str = "fan_kepu_social_life") -> None:
    conn.execute(
        "INSERT INTO domain_search_tags(tag_id, tag, domain_label, status, source, human_review_status) "
        "VALUES (?, ?, ?, 'active', 'sources_yaml', 'approved')",
        (f"{domain_label}_{tag}", tag, domain_label),
    )
    conn.commit()


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_effective_baseline_topic_sections(self) -> None:
        contract = validate_hotspot_registration_execution_contract()
        self.assertTrue({"3", "17"}.issubset(contract))


class MatchDomainPolicyTests(unittest.TestCase):
    def test_matches_the_versioned_domain_policy_without_search_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                matched = match_domain_policy_terms(conn, domain_label="fan_kepu_social_life", raw_text="最近房产中介行业很多新闻")
                self.assertEqual(matched, ["房产"])
            finally:
                conn.close()

    def test_search_tags_never_change_hotspot_domain_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                conn.execute(
                    "INSERT INTO domain_search_tags(tag_id, tag, domain_label, status, source) "
                    "VALUES ('t1', '火星科技', 'fan_kepu_social_life', 'active', 'sources_yaml')"
                )
                conn.commit()
                matched = match_domain_policy_terms(conn, domain_label="fan_kepu_social_life", raw_text="火星科技最新动态")
                self.assertEqual(matched, [])
            finally:
                conn.close()

    def test_no_match_returns_empty_list_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                matched = match_domain_policy_terms(conn, domain_label="fan_kepu_social_life", raw_text="完全无关的内容")
                self.assertEqual(matched, [])
            finally:
                conn.close()


class RegisterHotspotEventTests(unittest.TestCase):
    def test_registers_a_real_hotspot_with_matched_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                result = register_hotspot_event(
                    conn, domain_label="fan_kepu_social_life", raw_text="房产中介行业最近有新政策", created_by="用户本人"
                )
                self.assertEqual(result["matched_tags"], ["房产"])
                row = conn.execute("SELECT * FROM hotspot_events WHERE event_id=?", (result["event_id"],)).fetchone()
                self.assertEqual(row["raw_text"], "房产中介行业最近有新政策")
                self.assertEqual(row["created_by"], "用户本人")
                self.assertEqual(json.loads(row["matched_tags"]), ["房产"])
                self.assertEqual(row["source"], "feishu_manual")
            finally:
                conn.close()

    def test_zero_matched_tags_is_a_valid_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                result = register_hotspot_event(
                    conn, domain_label="fan_kepu_social_life", raw_text="完全不相关的内容", created_by="用户本人"
                )
                self.assertEqual(result["matched_tags"], [])
                self.assertIsNotNone(conn.execute("SELECT * FROM hotspot_events WHERE event_id=?", (result["event_id"],)).fetchone())
            finally:
                conn.close()

    def test_empty_raw_text_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    register_hotspot_event(conn, domain_label="fan_kepu_social_life", raw_text="   ", created_by="x")
            finally:
                conn.close()

    def test_missing_created_by_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    register_hotspot_event(conn, domain_label="fan_kepu_social_life", raw_text="真实文本", created_by="  ")
            finally:
                conn.close()

    def test_unsupported_domain_label_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    register_hotspot_event(conn, domain_label="not_a_real_domain", raw_text="x", created_by="x")
            finally:
                conn.close()

    def test_two_registrations_get_distinct_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                r1 = register_hotspot_event(conn, domain_label="fan_kepu_social_life", raw_text="第一条", created_by="x")
                r2 = register_hotspot_event(conn, domain_label="fan_kepu_social_life", raw_text="第二条", created_by="x")
                self.assertNotEqual(r1["event_id"], r2["event_id"])
                rows = conn.execute("SELECT * FROM hotspot_events").fetchall()
                self.assertEqual(len(rows), 2)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
