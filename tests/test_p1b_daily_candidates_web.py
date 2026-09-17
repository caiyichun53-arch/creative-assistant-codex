from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.web.read_only_server import create_action_server
from tests.test_core_unified_status import _seed_domain


FORMAL_DATABASE = Path(r"I:\Creation_assistant-runtime\formal\production_activation.sqlite3")
MUSIC_DOMAIN = "music_entertainment"
OTHER_DOMAIN = "domain_1833831517eb"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def _insert_daily_run(
    core: Stage0ContentProductionCore,
    *,
    domain: str,
    daily_run_id: str,
    business_date: str,
    created_at: str,
) -> None:
    core.conn.execute(
        "INSERT INTO stage0_daily_run(" 
        "daily_run_id, domain_label, business_date, lifecycle, created_at, started_at, "
        "finished_at, data_identity, cold_start_id) VALUES (?, ?, ?, 'completed', ?, ?, ?, 'test', ?)",
        (
            daily_run_id,
            domain,
            business_date,
            created_at,
            created_at,
            created_at,
            f"cold-{domain}",
        ),
    )


def _insert_discovery_run(
    core: Stage0ContentProductionCore,
    *,
    domain: str,
    daily_run_id: str,
    run_id: str,
    discovery_date: str,
    completed_at: str,
    candidates: list[tuple[str, str]],
) -> dict[str, str]:
    core.conn.execute(
        "INSERT INTO stage1b_discovery_run(" 
        "run_id, discovery_date, status, data_identity, created_by, created_at, completed_at, failure_reason) "
        "VALUES (?, ?, 'completed', 'test', 'fixture', ?, ?, NULL)",
        (run_id, discovery_date, completed_at, completed_at),
    )
    core.conn.execute(
        "INSERT INTO stage1b_run_execution_context(" 
        "run_id, execution_mode, lifecycle_status, classification_reason, classified_by, "
        "classified_at, data_identity, daily_run_id) VALUES (?, 'production_daily', 'completed', ?, 'fixture', ?, 'test', ?)",
        (run_id, "TEST completed daily candidate fixture", completed_at, daily_run_id),
    )
    core.conn.execute(
        "INSERT INTO stage1b_run_domain_scope(run_id, domain_label, data_identity, recorded_at) "
        "VALUES (?, ?, 'test', ?)",
        (run_id, domain, completed_at),
    )
    result: dict[str, str] = {}
    for position, (candidate_key, title) in enumerate(candidates, start=1):
        source_id = f"source-{candidate_key}"
        assembly_id = f"assembly-{candidate_key}"
        model_run_id = f"model-{candidate_key}"
        candidate_version_id = f"candidate-version-{candidate_key}"
        candidate_id = f"candidate-{candidate_key}"
        source_payload = {
            "title": title,
            "url": f"https://example.test/{candidate_key}",
            "account_name": "TEST source",
            "formal_source": {
                "table": "TEST source fixture",
                "object_id": candidate_key,
                "object_version": "1",
            },
        }
        candidate_payload = {
            "title": title,
            "normalized_title": title.casefold().replace(" ", "-"),
            "core_question": f"{title} 要回答什么问题",
            "topic_angle": "TEST candidate angle",
            "source_reference": {"source_version_id": source_id},
            "why_attention": "TEST candidate evidence",
            "material_readiness": "source recorded",
            "risk_limits": [],
        }
        source_json = _canonical(source_payload)
        candidate_json = _canonical(candidate_payload)
        input_json = _canonical({"source_version_id": source_id, "candidate_key": candidate_key})
        core.conn.execute(
            "INSERT INTO stage1b_source_version(" 
            "source_version_id, run_id, domain_label, source_type, source_object_id, source_object_version, "
            "source_time, expires_at, payload_json, integrity_hash, data_identity, created_at) "
            "VALUES (?, ?, ?, 'saved_user_direction', ?, '1', ?, NULL, ?, ?, 'test', ?)",
            (source_id, run_id, domain, candidate_key, completed_at, source_json, hashlib.sha256(source_json.encode()).hexdigest(), completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_input_assembly(" 
            "assembly_id, run_id, source_version_id, payload_json, integrity_hash, prompt_version, "
            "skill_version, model_config_version, data_identity, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'test.prompt', 'test.skill', 'test.config', 'test', ?)",
            (assembly_id, run_id, source_id, input_json, hashlib.sha256(input_json.encode()).hexdigest(), completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_model_run(" 
            "model_run_id, run_id, source_version_id, input_assembly_id, status, request_id, prompt_version, "
            "skill_version, route_id, route_version, provider_ref, provider_name, model_name, input_integrity_hash, "
            "output_hash, validation_status, error_json, retry_status, usage_json, cost_json, duration_ms, "
            "data_identity, created_at, via_model_gateway) "
            "VALUES (?, ?, ?, ?, 'succeeded', ?, 'test.prompt', 'test.skill', NULL, 'test.route', NULL, 'TEST', 'TEST', ?, NULL, 'passed', '{}', 'not_retried', '{}', '{}', 0, 'test', ?, 0)",
            (model_run_id, run_id, source_id, assembly_id, f"request-{candidate_key}", hashlib.sha256(input_json.encode()).hexdigest(), completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_candidate_version(" 
            "candidate_version_id, candidate_id, run_id, domain_label, source_version_id, parent_candidate_version_id, "
            "model_run_id, payload_json, integrity_hash, status, data_identity, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'awaiting_user_decision', 'test', ?)",
            (candidate_version_id, candidate_id, run_id, domain, source_id, model_run_id, candidate_json, hashlib.sha256(candidate_json.encode()).hexdigest(), completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_candidate_support(" 
            "support_id, candidate_version_id, source_version_id, support_kind, relation_reason, data_identity, created_at) "
            "VALUES (?, ?, ?, 'initial_discovery', 'TEST fixture source', 'test', ?)",
            (f"support-{candidate_key}", candidate_version_id, source_id, completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_candidate_pool_state(" 
            "pool_state_id, candidate_version_id, pool_status, reason, parameter_version, data_identity, effective_at) "
            "VALUES (?, ?, 'current', 'TEST current daily candidate', 'test-v1', 'test', ?)",
            (f"pool-{candidate_key}", candidate_version_id, completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_daily_snapshot(" 
            "snapshot_id, run_id, domain_label, candidate_version_id, display_position, data_identity, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'test', ?)",
            (f"snapshot-{candidate_key}", run_id, domain, candidate_version_id, position, completed_at),
        )
        core.conn.execute(
            "INSERT INTO stage1b_candidate_priority VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id,domain,candidate_version_id,"test-input",_canonical({
                "candidate":candidate_payload,"created_at":completed_at,
                "materials":[{"source_version_id":source_id}]}),
             70,"TEST assessed priority","TEST fixture only",_canonical([source_id]),
             "test-agent",None,"test-execution","test",completed_at),
        )
        result[candidate_key] = candidate_version_id
    return result


class DailyCandidatesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "daily-candidates.sqlite3"
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            _seed_domain(core, MUSIC_DOMAIN, cold_status="completed")
            _seed_domain(core, OTHER_DOMAIN, cold_status="completed")
            _insert_daily_run(
                core,
                domain=MUSIC_DOMAIN,
                daily_run_id="daily-music-old",
                business_date="2026-08-29",
                created_at="2026-08-29T00:00:00+00:00",
            )
            _insert_daily_run(
                core,
                domain=MUSIC_DOMAIN,
                daily_run_id="daily-music-current",
                business_date="2026-08-30",
                created_at="2026-08-30T00:00:00+00:00",
            )
            _insert_daily_run(
                core,
                domain=OTHER_DOMAIN,
                daily_run_id="daily-other-current",
                business_date="2026-08-30",
                created_at="2026-08-30T00:00:00+00:00",
            )
            self.old = _insert_discovery_run(
                core,
                domain=MUSIC_DOMAIN,
                daily_run_id="daily-music-old",
                run_id="discovery-music-old",
                discovery_date="2026-08-29",
                completed_at="2026-08-29T00:01:00+00:00",
                candidates=[("old", "Historical music candidate")],
            )
            self.current = _insert_discovery_run(
                core,
                domain=MUSIC_DOMAIN,
                daily_run_id="daily-music-current",
                run_id="discovery-music-current",
                discovery_date="2026-08-30",
                completed_at="2026-08-30T00:01:00+00:00",
                candidates=[("music-a", "Current music candidate A"), ("music-b", "Current music candidate B")],
            )
            self.other = _insert_discovery_run(
                core,
                domain=OTHER_DOMAIN,
                daily_run_id="daily-other-current",
                run_id="discovery-other-current",
                discovery_date="2026-08-30",
                completed_at="2026-08-30T00:02:00+00:00",
                candidates=[("other-a", "Other domain candidate")],
            )
            core.conn.commit()
        finally:
            core.close()
        self.server = create_action_server(
            port=0,
            data_identity="test",
            database_path=self.database,
            actor="p1b-web-user",
            carrier_binding_id="p1b-web-carrier",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tempdir.cleanup()

    def _candidates(self) -> dict:
        with urlopen(self.base_url + "/api/daily-candidates") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"], payload)
        return {item["domain_label"]: item for item in payload["domains"]}

    def test_priority_top_ten_does_not_limit_user_selection(self) -> None:
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            _insert_daily_run(core,domain=MUSIC_DOMAIN,daily_run_id="daily-twelve",business_date="2026-09-01",created_at="2026-09-01T00:00:00+00:00")
            ids = _insert_discovery_run(core,domain=MUSIC_DOMAIN,daily_run_id="daily-twelve",run_id="discovery-twelve",
                discovery_date="2026-09-01",completed_at="2026-09-01T00:00:00+00:00",
                candidates=[("twelve-"+str(i),"十二条测试候选之"+str(i)) for i in range(12)])
            core.conn.execute("DELETE FROM stage1b_daily_snapshot WHERE run_id='discovery-twelve' AND display_position>10")
            core.conn.commit()
        finally: core.close()
        self.assertEqual(len(self._candidates()[MUSIC_DOMAIN]["candidates"]),10)
        selected = _post_json(self.base_url+"/api/action",{"action":"select_daily_candidate","domain_label":MUSIC_DOMAIN,
            "candidate_version_id":ids["twelve-11"],"reason":"用户选择优先展示以外的候选"})
        self.assertTrue(selected["ok"],selected)
        self.assertEqual(selected["core_result"]["research_plan_status"],"requires_external_intelligence")

    def test_web_page_exposes_daily_candidate_view_and_action(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
        javascript = (
            Path(__file__).resolve().parents[1] / "scripts" / "web" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="daily-candidates"', html)
        self.assertIn('/api/daily-candidates', javascript)
        self.assertIn('data-daily-action="select_daily_candidate"', javascript)
        self.assertNotIn("sqlite", javascript.lower())

    def test_candidates_are_grouped_and_selection_enters_research_plan(self) -> None:
        groups = self._candidates()
        music = groups[MUSIC_DOMAIN]
        other = groups[OTHER_DOMAIN]
        music_ids = {item["candidate_version_id"] for item in music["candidates"]}
        self.assertEqual(music_ids, {self.current["music-a"], self.current["music-b"]})
        self.assertNotIn(self.old["old"], music_ids)
        self.assertEqual(
            {item["candidate_version_id"] for item in other["candidates"]},
            {self.other["other-a"]},
        )
        self.assertTrue(all(item["status"] == "awaiting_user_decision" for item in music["candidates"]))

        selected = _post_json(
            self.base_url + "/api/action",
            {
                "action": "select_daily_candidate",
                "domain_label": MUSIC_DOMAIN,
                "candidate_version_id": self.current["music-a"],
                "reason": "TEST user selected the current music candidate",
            },
        )
        self.assertTrue(selected["ok"], selected)
        self.assertEqual(selected["core_result"]["candidate_version_id"], self.current["music-a"])
        self.assertEqual(selected["core_result"]["research_plan_status"], "requires_external_intelligence")
        self.assertEqual(selected["core_result"]["external_task"]["task_type"], "research_plan")

        with urlopen(self.base_url + "/api/content-tasks") as response:
            tasks = json.loads(response.read().decode("utf-8"))["tasks"]
        task = next(item for item in tasks if item["task_id"] == selected["core_result"]["task_id"])
        self.assertEqual(task["current_node"], "research_plan")
        self.assertEqual(task["current_status"], "processing")

        refreshed = self._candidates()[MUSIC_DOMAIN]["candidates"]
        selected_item = next(item for item in refreshed if item["candidate_version_id"] == self.current["music-a"])
        unselected_item = next(item for item in refreshed if item["candidate_version_id"] == self.current["music-b"])
        self.assertEqual(selected_item["user_decision"], "selected")
        self.assertIsNone(unselected_item["user_decision"])

    def test_historical_cross_domain_and_duplicate_selection_are_rejected(self) -> None:
        historical = _post_json(
            self.base_url + "/api/action",
            {
                "action": "select_daily_candidate",
                "domain_label": MUSIC_DOMAIN,
                "candidate_version_id": self.old["old"],
                "reason": "TEST must reject a historical candidate",
            },
        )
        self.assertFalse(historical["ok"])

        cross_domain = _post_json(
            self.base_url + "/api/action",
            {
                "action": "select_daily_candidate",
                "domain_label": OTHER_DOMAIN,
                "candidate_version_id": self.current["music-b"],
                "reason": "TEST must reject a cross-domain candidate",
            },
        )
        self.assertFalse(cross_domain["ok"])

        selected = _post_json(
            self.base_url + "/api/action",
            {
                "action": "select_daily_candidate",
                "domain_label": MUSIC_DOMAIN,
                "candidate_version_id": self.current["music-b"],
                "reason": "TEST selects the current candidate once",
            },
        )
        self.assertTrue(selected["ok"], selected)
        duplicate = _post_json(
            self.base_url + "/api/action",
            {
                "action": "select_daily_candidate",
                "domain_label": MUSIC_DOMAIN,
                "candidate_version_id": self.current["music-b"],
                "reason": "TEST duplicate selection",
            },
        )
        self.assertFalse(duplicate["ok"])

        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            with self.assertRaises(StateTransitionError):
                core.select_discovery_candidate(
                    candidate_version_id=self.old["old"],
                    actor="p1b-web-user",
                    actor_kind="user",
                    reason="TEST direct historical selection",
                    idempotency_key="p1b-historical-direct",
                )
        finally:
            core.close()

    def test_formal_daily_candidate_read_is_read_only_and_protected(self) -> None:
        before = _sha256(FORMAL_DATABASE)
        server = create_action_server(port=0, data_identity="production")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/daily-candidates") as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"], payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(_sha256(FORMAL_DATABASE), before)


if __name__ == "__main__":
    unittest.main()
