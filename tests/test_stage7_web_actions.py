from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.core.core_entry import build_status
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.web.read_only_server import create_action_server
from tests._cold_start_test_model import test_task_model_resolver as resolve_test_model
from tests.test_core_unified_status import _seed_domain


FORMAL_DATABASE = Path(r"I:\Creation_assistant-runtime\formal\production_activation.sqlite3")
FORMAL_SHA256 = "B8D5A61E656798C485DC51DF64623F3985617C9B9A1BB1ECB2D3C38FDBA174F8"
APP_JS = Path(__file__).resolve().parents[1] / "scripts" / "web" / "static" / "app.js"


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


def _browser_daily_payload(raw_date: str) -> dict:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const appPath = process.argv[1];
const rawDate = process.argv[2];
const listeners = new Map();
const elements = new Map();
const requests = [];
let domReady = null;

function element(selector) {
  if (!elements.has(selector)) {
    elements.set(selector, {
      value: "",
      hidden: false,
      textContent: "",
      innerHTML: "",
      addEventListener: (event, handler) => listeners.set(selector + ":" + event, handler),
    });
  }
  return elements.get(selector);
}

const document = {
  addEventListener: (event, handler) => {
    if (event === "DOMContentLoaded") domReady = handler;
  },
  querySelector: element,
};
const context = {
  document,
  window: { prompt: () => "" },
  fetch: (url, options) => {
    requests.push({ url, options });
    return new Promise(() => {});
  },
};
vm.runInNewContext(fs.readFileSync(appPath, "utf8"), context, { filename: appPath });
domReady();
element("#daily-domain").value = "action-domain";
element("#daily-date").value = rawDate;
listeners.get("#daily-start:click")();
const request = requests.find((item) => item.url === "/api/action");
if (!request) throw new Error("daily action request was not sent");
process.stdout.write(request.options.body);
"""
    result = subprocess.run(
        ["node", "-e", script, str(APP_JS), raw_date],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class Stage7WebActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "test.sqlite3"
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            _seed_domain(
                core,
                "action-domain",
                cold_status="running",
                waiting_registration=True,
            )
            now = "2026-08-30T00:00:00+00:00"
            registration_id = "registration-action-domain"
            core.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step='completed', "
                "status='completed', completed_at=? WHERE registration_id=?",
                (now, registration_id),
            )
            core.conn.execute(
                "INSERT INTO competitor_accounts VALUES (?, 'douyin', ?, ?, ?, ?, ?, ?, "
                "'active', 'standard', 'standard', ?, ?)",
                (
                    "competitor-action-domain",
                    "action-domain",
                    "action-domain",
                    "registered competitor",
                    "sec-action-domain",
                    "https://douyin.com/user/action-domain",
                    f"stage0_competitor_registration:{registration_id}",
                    now,
                    now,
                ),
            )
            core.conn.execute(
                "INSERT INTO stage0_cold_start_run_contract("
                "cold_start_id, domain_label, owned_account_id, competitor_account_ids_json, "
                "input_snapshot_json, cold_start_contract_version, created_at, data_identity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cold-action-domain",
                    "action-domain",
                    "owned-action-domain",
                    "[]",
                    json.dumps({"run_model": {"model_name": "isolated/model-a"}}),
                    "cold_start_guard_v14",
                    now,
                    "test",
                ),
            )
            core.propose_human_decision_carrier(
                carrier_binding_id="web-test-carrier",
                carrier_kind="web_test",
                entry_ref="stage7-web-test",
                context_strategy="same_test_session",
                actor="stage7-test",
            )
            core.validate_human_decision_carrier(
                carrier_binding_id="web-test-carrier",
                validation_evidence={
                    "inbound_round_trip": True,
                    "outbound_round_trip": True,
                    "same_context_verified": True,
                    "decision_identity_verified": True,
                    "evidence_ref": "stage7-web-round-trip",
                },
                actor="stage7-test",
                actor_kind="user",
            )
            core.conn.commit()
        finally:
            core.close()

        self.server = create_action_server(
            port=0,
            data_identity="test",
            database_path=self.database,
            actor="stage7-web-user",
            carrier_binding_id="web-test-carrier",
            config_dir=self.config_dir,
            task_model_resolver=resolve_test_model,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tempdir.cleanup()

    def status(self) -> dict:
        with urlopen(self.base_url + "/api/status") as response:
            return json.loads(response.read().decode("utf-8"))["status"]

    def test_action_server_page_is_reachable_and_exposes_only_fixed_action_route(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
        javascript = (Path(__file__).resolve().parents[1] / "scripts" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="action-panel"', html)
        self.assertIn('fetch("/api/action"', javascript)
        self.assertNotIn("sqlite", javascript.lower())

    def test_stop_and_resume_use_the_same_current_run(self) -> None:
        formal_before = _sha256(FORMAL_DATABASE)
        before = self.status()
        domain = next(item for item in before["domains"] if item["domain_identity"] == "action-domain")
        run_id = domain["current_cold_start"]["cold_start_id"]

        stopped = _post_json(
            self.base_url + "/api/action",
            {"action": "cold_start_stop", "domain_label": "action-domain", "reason": "web test stop"},
        )
        self.assertTrue(stopped["ok"])
        stopped_domain = next(item for item in stopped["status"]["domains"] if item["domain_identity"] == "action-domain")
        self.assertEqual(stopped_domain["current_cold_start"]["cold_start_id"], run_id)
        self.assertEqual(stopped_domain["current_cold_start"]["status"], "stopped")

        resumed = _post_json(
            self.base_url + "/api/action",
            {"action": "cold_start_resume", "domain_label": "action-domain"},
        )
        self.assertTrue(resumed["ok"], resumed)
        resumed_domain = next(item for item in resumed["status"]["domains"] if item["domain_identity"] == "action-domain")
        self.assertEqual(resumed_domain["current_cold_start"]["cold_start_id"], run_id)
        self.assertEqual(resumed_domain["current_cold_start"]["status"], "running")

        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            self.assertEqual(core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 1)
        finally:
            core.close()
        self.assertEqual(_sha256(FORMAL_DATABASE), formal_before)

    def test_daily_start_and_resume_use_core_lifecycle(self) -> None:
        started = _post_json(
            self.base_url + "/api/action",
            {
                "action": "daily_start",
                "domain_label": "action-domain",
            },
        )
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["core_result"]["action"], "started")
        daily_id = started["status"]["domains"][0]["current_daily"]["daily_run_id"]
        self.assertEqual(started["status"]["domains"][0]["current_daily"]["lifecycle"], "running")

        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            core.finish_daily_run(
                daily_run_id=daily_id,
                lifecycle="failed",
                actor="stage7-fixture",
                reason="fixture failure",
            )
        finally:
            core.close()

        resumed = _post_json(
            self.base_url + "/api/action",
            {"action": "daily_resume", "domain_label": "action-domain"},
        )
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["core_result"]["action"], "resumed")
        self.assertEqual(resumed["core_result"]["daily_run"]["daily_run_id"], daily_id)
        self.assertEqual(resumed["status"]["domains"][0]["current_daily"]["lifecycle"], "running")

    def test_daily_date_input_preserves_core_parameter_boundary(self) -> None:
        empty = _browser_daily_payload("")
        self.assertNotIn("business_date", empty)

        explicit = _browser_daily_payload("2026-08-30")
        self.assertEqual(explicit["business_date"], "2026-08-30")

        invalid = _browser_daily_payload("not-a-date")
        self.assertEqual(invalid["business_date"], "not-a-date")

        rejected = _post_json(
            self.base_url + "/api/action",
            {
                "action": "daily_start",
                "domain_label": "action-domain",
                "business_date": "not-a-date",
            },
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["outcome"], "rejected")
        self.assertIn("YYYY-MM-DD", rejected["error"])

    def test_formal_mutating_action_is_rejected_without_formal_write(self) -> None:
        before = _sha256(FORMAL_DATABASE)
        server = create_action_server(port=0, data_identity="production")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/api/action"
            payload = _post_json(
                url,
                {
                    "action": "daily_start",
                    "domain_label": "music_entertainment",
                    "business_date": "2026-08-30",
                },
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        after = _sha256(FORMAL_DATABASE)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "rejected")
        self.assertIn("migration protection", payload["error"])
        self.assertEqual(payload["status"]["system"]["runtime_identity"], "FORMAL")
        self.assertEqual(before, FORMAL_SHA256)
        self.assertEqual(after, before)

    def test_http_cannot_switch_identity_or_database(self) -> None:
        response = _post_json(
            self.base_url + "/api/action",
            {"action": "daily_start", "data_identity": "production"},
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["outcome"], "rejected")
        self.assertNotIn("status", response)

    def test_completed_or_non_current_runs_cannot_be_operated_as_current(self) -> None:
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            core.conn.execute(
                "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id=?",
                ("cold-action-domain",),
            )
            core.conn.commit()
        finally:
            core.close()
        completed = _post_json(
            self.base_url + "/api/action",
            {"action": "cold_start_resume", "domain_label": "action-domain"},
        )
        self.assertFalse(completed["ok"])
        self.assertEqual(completed["outcome"], "rejected")
        self.assertEqual(
            completed["status"]["domains"][0]["current_cold_start"]["status"],
            "completed",
        )

        non_current = _post_json(
            self.base_url + "/api/action",
            {"action": "cold_start_stop", "domain_label": "missing-domain", "reason": "must reject"},
        )
        self.assertFalse(non_current["ok"])
        self.assertEqual(non_current["outcome"], "rejected")

    def test_cold_start_preview_and_confirm_create_one_test_run(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database = Path(tempdir) / "cold-start.sqlite3"
            config_dir = Path(tempdir) / "domain_packs"
            config_dir.mkdir()
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            core.close()
            server = create_action_server(
                port=0,
                data_identity="test",
                database_path=database,
                actor="stage7-web-user",
                carrier_binding_id="web-test-carrier",
                config_dir=config_dir,
                task_model_resolver=resolve_test_model,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/action"
                configuration = {
                    "domain_name": "web-created-domain",
                    "owned_account": {
                        "display_name": "web owned",
                        "external_account_ref": "douyin:web-owned",
                    },
                    "competitor_accounts": [
                        {
                            "display_name": f"web competitor {index}",
                            "external_account_ref": f"douyin:web-competitor-{index}",
                        }
                        for index in range(20)
                    ],
                }
                preview = _post_json(url, {"action": "cold_start_preview", "configuration": configuration})
                self.assertTrue(preview["ok"])
                self.assertTrue(preview["core_result"]["ready_to_confirm"], preview)
                confirmed = _post_json(url, {"action": "cold_start_confirm"})
                self.assertTrue(confirmed["ok"])
                self.assertTrue(confirmed["core_result"]["automatic_start"])
                self.assertTrue(confirmed["status"]["domains"], confirmed)
                created = next(
                    item for item in confirmed["status"]["domains"]
                    if item["domain_identity"] == confirmed["core_result"].get("domain_label")
                )
                self.assertTrue(created["current_activation"]["exists"])
                self.assertEqual(created["current_cold_start"]["status"], "running")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    @staticmethod
    def _write_domain_pack(config_dir: Path, domain_label: str) -> None:
        (config_dir / f"{domain_label}.yaml").write_text(
            "\n".join(
                (
                    f"name: {domain_label}",
                    f"formal_domain_label: {domain_label}",
                    "activation_status: approved",
                    "workflow_mode: manual_guard",
                    "runtime_requirements: []",
                    "discovery:",
                    "  hotspot_match_terms: []",
                    "  risk_block_terms: []",
                    "  exclude_terms: []",
                    "  topic_search: {}",
                )
            ),
            encoding="utf-8",
        )

    def _prepare_waiting_review(self, review_kind: str) -> tuple[tempfile.TemporaryDirectory, Path, Path, list[dict]]:
        tempdir = tempfile.TemporaryDirectory()
        root = Path(tempdir.name)
        database = root / "review.sqlite3"
        config_dir = root / "domain_packs"
        config_dir.mkdir()
        domain = f"web-{review_kind}-domain"
        self._write_domain_pack(config_dir, domain)
        core = Stage0ContentProductionCore.open(database, data_identity="test")
        try:
            _seed_domain(core, domain, cold_status="running", waiting_registration=True)
            now = "2026-08-30T00:00:00+00:00"
            registration_id = f"registration-{domain}"
            core.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step='completed', status='completed', completed_at=? "
                "WHERE registration_id=?",
                (now, registration_id),
            )
            core.conn.execute(
                "INSERT INTO stage0_cold_start_run_contract("
                "cold_start_id, domain_label, owned_account_id, competitor_account_ids_json, "
                "input_snapshot_json, cold_start_contract_version, created_at, data_identity) "
                "VALUES (?, ?, ?, '[]', ?, ?, ?, 'test')",
                (
                    f"cold-{domain}",
                    domain,
                    f"owned-{domain}",
                    json.dumps({"run_model": {"model_name": "isolated/model-a"}}),
                    "cold_start_guard_v14",
                    now,
                ),
            )
            core.propose_human_decision_carrier(
                carrier_binding_id="web-test-carrier",
                carrier_kind="web_test",
                entry_ref="stage7-web-test",
                context_strategy="same_test_session",
                actor="stage7-test",
            )
            core.validate_human_decision_carrier(
                carrier_binding_id="web-test-carrier",
                validation_evidence={
                    "inbound_round_trip": True,
                    "outbound_round_trip": True,
                    "same_context_verified": True,
                    "decision_identity_verified": True,
                    "evidence_ref": "stage7-web-review-round-trip",
                },
                actor="stage7-test",
                actor_kind="user",
            )
            decisions: list[dict] = []
            if review_kind == "tags":
                tag_id = f"tag-{domain}"
                core.conn.execute(
                    "INSERT INTO domain_search_tags(tag_id, tag, domain_label, status, source, source_video_id, human_review_status) "
                    "VALUES (?, 'webtag', ?, 'suggested', 'discovered', 'source-1', 'pending_review')",
                    (tag_id, domain),
                )
                candidate_set = {
                    "retained": [{
                        "tag_id": tag_id,
                        "tag": "webtag",
                        "source_ids": ["source-1"],
                        "registration_ids": [registration_id],
                        "source_count": 1,
                        "account_count": 1,
                    }],
                    "filtered": [],
                }
                core.conn.execute(
                    "INSERT INTO stage0_cold_start_tag_library("
                    "tag_library_id, cold_start_id, domain_label, extraction_method, source_item_count, "
                    "minimum_source_support, candidate_set_json, tag_ids_json, status, data_identity, "
                    "created_by, created_at, reviewed_by, reviewed_at, review_reason) "
                    "VALUES (?, ?, ?, 'fixture', 1, 1, ?, ?, 'awaiting_human_review', 'test', 'fixture', ?, NULL, NULL, NULL)",
                    (
                        f"library-{domain}",
                        f"cold-{domain}",
                        domain,
                        json.dumps(candidate_set),
                        json.dumps([tag_id]),
                        now,
                    ),
                )
                decisions = [{"tag_id": tag_id, "decision": "accepted", "edited_tag": "webtag"}]
            elif review_kind == "content_types":
                artifact = {
                    "deep_breakdown": {
                        "source_id": "source-1",
                        "source_content_type": "人物故事",
                        "content_subject_type": "person",
                        "expression_form": "story",
                        "content_type_evidence": ["source-1-evidence"],
                    }
                }
                core.conn.execute(
                    "INSERT INTO stage0_competitor_registration_item "
                    "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
                    "VALUES (?, 'breakdown', 'source-1', 'completed', ?, '{}', 1, 'test', ?)",
                    (registration_id, json.dumps(artifact, ensure_ascii=False), now),
                )
                candidate = core.build_cold_start_content_type_candidates(
                    cold_start_id=f"cold-{domain}", actor="fixture"
                )
                decisions = [
                    {"candidate_id": item["candidate_id"], "decision": "accepted"}
                    for item in candidate["candidates"]
                ]
            else:
                artifact = {
                    "deep_breakdown": {
                        "source_id": "source-1",
                        "boundary_observation": "具体关系变化及影响的边界证据",
                    }
                }
                core.conn.execute(
                    "INSERT INTO stage0_competitor_registration_item "
                    "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
                    "VALUES (?, 'breakdown', 'source-1', 'completed', ?, '{}', 1, 'test', ?)",
                    (registration_id, json.dumps(artifact, ensure_ascii=False), now),
                )
                candidate = core.build_cold_start_domain_boundary_candidates(
                    cold_start_id=f"cold-{domain}",
                    actor="fixture",
                    proposal_generator=lambda snapshot: {
                        "in_boundary_principles": [{
                            "boundary_id": "in-1",
                            "principle": "具体关系变化或影响属于生产范围",
                            "rationale": "来自当前运行证据",
                            "evidence_refs": [snapshot["valid_observations"][0]["source_id"]],
                        }],
                        "out_boundary_principles": [{
                            "boundary_id": "out-1",
                            "principle": "只有名称关联但没有具体关系不属于生产范围",
                            "rationale": "避免只按名称判断",
                            "evidence_refs": [snapshot["valid_observations"][0]["source_id"]],
                        }],
                        "unknown_topic_rule": {
                            "rule": "未知题材按问题性质判断",
                            "uncertain_action": "等待人工确认",
                        },
                    },
                )
                decisions = [
                    {"boundary_id": item["boundary_id"], "decision": "accepted"}
                    for item in (
                        candidate["proposal"]["in_boundary_principles"]
                        + candidate["proposal"]["out_boundary_principles"]
                    )
                ]
            core.conn.execute(
                "UPDATE stage0_cold_start SET status='waiting_human' WHERE cold_start_id=?",
                (f"cold-{domain}",),
            )
            core.conn.commit()
        finally:
            core.close()
        return tempdir, database, config_dir, decisions

    def test_three_waiting_human_reviews_go_through_core_over_http(self) -> None:
        for review_kind, action in (
            ("tags", "review_tags"),
            ("content_types", "review_content_types"),
            ("domain_boundary", "review_domain_boundary"),
        ):
            tempdir, database, config_dir, decisions = self._prepare_waiting_review(review_kind)
            try:
                domain = f"web-{review_kind}-domain"
                server = create_action_server(
                    port=0,
                    data_identity="test",
                    database_path=database,
                    actor="stage7-web-user",
                    carrier_binding_id="web-test-carrier",
                    config_dir=config_dir,
                    task_model_resolver=resolve_test_model,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    result = _post_json(
                        f"http://127.0.0.1:{server.server_address[1]}/api/action",
                        {
                            "action": action,
                            "domain_label": domain,
                            "decisions": decisions,
                            "reason": f"web {review_kind} review",
                        },
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["core_result"]["status"], "frozen" if review_kind != "tags" else "accepted")
                self.assertEqual(result["status"]["domains"][0]["current_cold_start"]["status"], "waiting_human")
            finally:
                tempdir.cleanup()


if __name__ == "__main__":
    unittest.main()
