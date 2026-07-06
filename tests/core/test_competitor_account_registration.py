from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
import unittest

from scripts.core.business_data.register_competitor_accounts import (
    install_schema,
    list_accounts,
    parse_douyin_sec_uid,
    register_from_domain,
)


class CompetitorAccountRegistrationTests(unittest.TestCase):
    def test_parse_douyin_homepage_and_register_idempotently_without_video_collection(self) -> None:
        self.assertEqual(
            parse_douyin_sec_uid("https://www.douyin.com/user/MS4wLjABAAAAabc?from_tab_name=main"),
            "MS4wLjABAAAAabc",
        )
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc?from=seed"},
                {"name": "张脑知", "url": "MS4wLjABAAAAdef"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "accounts.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)

                first = register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                second = register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")

                self.assertEqual(first["inserted_count"], 2)
                self.assertEqual(second["inserted_count"], 0)
                self.assertEqual(second["updated_count"], 2)
                self.assertTrue(second["no_video_collection_started"])
                self.assertTrue(second["no_hit_judgement_started"])
                self.assertEqual(len(list_accounts(conn, "fan_kepu_social_life")), 2)
                video_rows = conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0]
                hit_rows = conn.execute("SELECT count(*) FROM hits").fetchone()[0]
                self.assertEqual(video_rows, 0)
                self.assertEqual(hit_rows, 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
