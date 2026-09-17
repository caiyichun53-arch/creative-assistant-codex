from copy import deepcopy
import unittest

from scripts.core.production.hotspot_report import prepare, render
from scripts.mcp.creation_assistant_mcp_server import tool_definitions


def snapshot():
    return {
        "collected_at": "2026-09-08T09:59:30+08:00",
        "platforms": [{"id": "a", "name": "平台甲", "updated_at": "2026-09-08T09:59:00+08:00", "status": "success"}],
        "items": [{"id": str(n), "platform_id": "a", "title": title, "url": "https://example.com/" + str(n), "ranks": [n], "count": 1}
                  for n, title in enumerate(["LGD战胜NIP", "为什么没人喝猪奶", "AI偷看成人内容被抓包", "为什么没人喝猪奶"], 1)],
    }


def output():
    return {"schema_version": "hotspot_report.output.v1", "events": [
        {"title": "LGD战胜NIP", "member_ids": ["1"], "merge_reason": "单条比赛结果", "decision": "exclude", "rule_ids": ["esports"], "reason": "电竞比赛胜负", "uncertain": False},
        {"title": "为什么没人喝猪奶", "member_ids": ["2", "4"], "merge_reason": "相同问题", "decision": "keep", "rule_ids": [], "reason": "日常科学问题，不在排除范围", "uncertain": False},
        {"title": "AI偷看成人内容被抓包", "member_ids": ["3"], "merge_reason": "对象不明，单列", "decision": "keep", "rule_ids": [], "reason": "无法确认AI所指，不猜为电竞", "uncertain": True},
    ]}


class HotspotReportTests(unittest.TestCase):
    def test_skill_is_delivered_without_model_and_all_sources_reach_agent(self):
        task = prepare(snapshot())["task"]
        self.assertEqual(task["skill"]["formal_skill_id"], "hotspot_report")
        self.assertEqual(len(task["input"]["snapshot"]["items"]), 4)
        self.assertFalse(task["constraints"]["topic_generation"])
        self.assertNotIn("model_ref", task)
        self.assertIn("creation_assistant_hotspot_report", {t["name"] for t in tool_definitions()})

    def test_native_scoring_and_three_disjoint_lists(self):
        result = render(snapshot(), output(), 1)
        self.assertEqual(result["top"][0]["score"], 67 + 55 * 0.5)
        self.assertEqual(result["below_top"][0]["member_ids"], ["3"])
        self.assertTrue(result["below_top"][0]["uncertain"])
        self.assertEqual(result["excluded_by_preference"][0]["member_ids"], ["1"])
        self.assertFalse(result["formal_business_data_written"])
        self.assertEqual(result["topic_generation"], "not_triggered")

    def test_rejects_real_failure_modes_before_scoring(self):
        variants = []
        missing = output(); missing["events"].pop(); variants.append(missing)
        duplicate = output(); duplicate["events"][2]["member_ids"].append("2"); variants.append(duplicate)
        invented = output(); invented["events"][2]["member_ids"] = ["unknown"]; variants.append(invented)
        no_rule = output(); no_rule["events"][0]["rule_ids"] = []; variants.append(no_rule)
        guessed = output(); guessed["events"][0]["uncertain"] = True; variants.append(guessed)
        new_rule = output(); new_rule["events"][0]["rule_ids"] = ["all_games"]; variants.append(new_rule)
        score = output(); score["events"][1]["score"] = 999; variants.append(score)
        bad_decision = output(); bad_decision["events"][1]["decision"] = "skip"; variants.append(bad_decision)
        split = output(); split["events"][1]["member_ids"] = ["2"]
        other = deepcopy(split["events"][1]); other["member_ids"] = ["4"]; split["events"].append(other); variants.append(split)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises((ValueError, RuntimeError)):
                render(snapshot(), variant, 20)

    def test_no_default_cap_and_ties(self):
        data = snapshot(); data["items"][2]["ranks"] = [2]
        data["items"].pop()
        decisions = output(); decisions["events"][1]["member_ids"] = ["2"]
        result = render(data, decisions, None)
        self.assertEqual(len(result["top"]), 2)
        self.assertEqual([e["rank"] for e in result["top"]], [1, 1])
        self.assertEqual(result["below_top"], [])


if __name__ == "__main__":
    unittest.main()
