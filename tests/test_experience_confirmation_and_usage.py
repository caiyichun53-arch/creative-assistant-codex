from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from scripts.core.production.stage0_content_core import InputAssembly, Stage0ContentProductionCore, StateTransitionError
from scripts.core.production.stage1a_research_plan import Stage1AResearchPlanService
from scripts.core.production.stage1c_content_pipeline import Stage1CContentPipelineService
from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.publication_feedback import (
    decide_p7_review,
    prepare_p7_review,
    record_observation,
    register_publication,
)
from scripts.web.read_only_server import create_action_server
from tests.test_core_unified_status import _seed_domain


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


def _source_batch(prefix: str) -> list[dict]:
    return [
        {
            "source_id": f"{prefix}-source-{index}",
            "account_ref": f"{prefix}-account-{index % 2}",
            "account_name": f"{prefix} account {index % 2}",
            "content_subject_type": "person",
            "expression_form": "analysis",
            "structure_assessment": {"statement": "fixture structure evidence"},
            "recurring_evidence_patterns": [
                {
                    "spoken_action": "fixture evidence action",
                    "source_evidence": [{"text": "fixture evidence quote"}],
                }
            ],
        }
        for index in range(3)
    ]


def _insert_candidate_run(core: Stage0ContentProductionCore, run_id: str, domain: str) -> None:
    core.conn.execute(
        "INSERT INTO stage0_experience_candidate_run VALUES (?, ?, 'running', ?, ?, ?, ?, ?, NULL)",
        (run_id, domain, "[]", 0, "test", "fixture", "2026-08-30T00:00:00+00:00"),
    )
    core.conn.commit()


def _open_candidate(
    core: Stage0ContentProductionCore,
    *,
    run_id: str,
    domain: str,
    prefix: str,
    complete: bool,
    decision: str | None = None,
) -> str:
    sources = _source_batch(prefix)
    candidate = core.open_experience_candidate(
        task_id=None,
        domain_label=domain,
        frozen_sources=sources,
        actor="fixture-user",
        experience_candidate_run_id=run_id,
    )
    candidate_id = str(candidate["experience_candidate_id"])
    if complete:
        core.complete_experience_candidate(
            experience_candidate_id=candidate_id,
            proposal={
                "decision": "proposal",
                "candidate": {
                    "summary": f"{prefix} formal experience",
                    "applicable_when": ["music topic"],
                    "method": {"step": f"{prefix} method"},
                    "boundary": {"not_for": f"{prefix} boundary"},
                },
                "source_ids": [item["source_id"] for item in sources],
            },
            model_run_id=f"{prefix}-model-run",
        )
        if decision is not None:
            core.decide_experience_candidate(
                experience_candidate_id=candidate_id,
                decision=decision,
                actor="fixture-user",
                actor_kind="user",
                reason=f"fixture {decision}",
            )
    return candidate_id


def _seed_formal_experience(core: Stage0ContentProductionCore, candidate_id: str) -> str:
    candidate = core.conn.execute(
        "SELECT domain_label, proposal_json FROM stage0_experience_candidate WHERE experience_candidate_id=?",
        (candidate_id,),
    ).fetchone()
    proposal = json.loads(str(candidate["proposal_json"]))
    body = proposal["candidate"]
    experience_id = f"fixture-formal-{candidate_id}"
    core.conn.execute(
        "INSERT INTO stage0_confirmed_experience "
        "(experience_id, experience_candidate_id, domain_label, classification, summary, "
        "applicable_when_json, method_json, source_refs_json, boundary_json, status, "
        "data_identity, confirmed_by, confirmed_at) VALUES (?, ?, ?, 'shared_pattern', ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (
            experience_id,
            candidate_id,
            candidate["domain_label"],
            body["summary"],
            json.dumps(body["applicable_when"]),
            json.dumps(body["method"]),
            json.dumps(proposal["source_ids"]),
            json.dumps(body["boundary"]),
            "test",
            "fixture",
            "2026-08-30T00:00:00+00:00",
        ),
    )
    core.conn.commit()
    return experience_id


class ExperienceCandidateWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "experience-web.sqlite3"
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            _seed_domain(core, "candidate-domain", cold_status="completed")
            _insert_candidate_run(core, "candidate-run", "candidate-domain")
            self.waiting_id = _open_candidate(
                core,
                run_id="candidate-run",
                domain="candidate-domain",
                prefix="waiting",
                complete=True,
            )
            self.preparing_id = _open_candidate(
                core,
                run_id="candidate-run",
                domain="candidate-domain",
                prefix="preparing",
                complete=False,
            )
            core.propose_human_decision_carrier(
                carrier_binding_id="experience-web-carrier",
                carrier_kind="web_test",
                entry_ref="experience-confirmation-test",
                context_strategy="same_test_session",
                actor="fixture-user",
            )
            core.validate_human_decision_carrier(
                carrier_binding_id="experience-web-carrier",
                validation_evidence={
                    "inbound_round_trip": True,
                    "outbound_round_trip": True,
                    "same_context_verified": True,
                    "decision_identity_verified": True,
                    "evidence_ref": "experience-confirmation-test",
                },
                actor="fixture-user",
                actor_kind="user",
            )
            core.conn.commit()
        finally:
            core.close()
        self.server = create_action_server(
            port=0,
            data_identity="test",
            database_path=self.database,
            actor="web-test-user",
            carrier_binding_id="experience-web-carrier",
            config_dir=self.config_dir,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)
        self.tempdir.cleanup()

    def _candidates(self) -> list[dict]:
        with urlopen(self.base_url + "/api/experience-candidates") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "Creation Assistant Core")
        return payload["candidates"]

    def test_web_views_sources_accepts_rejects_and_blocks_preparing(self) -> None:
        candidates = self._candidates()
        self.assertEqual({item["experience_candidate_id"] for item in candidates}, {self.waiting_id, self.preparing_id})
        waiting = next(item for item in candidates if item["experience_candidate_id"] == self.waiting_id)
        preparing = next(item for item in candidates if item["experience_candidate_id"] == self.preparing_id)
        self.assertEqual(waiting["status"], "awaiting_human_decision")
        self.assertEqual(preparing["status"], "preparing")
        self.assertEqual(len(waiting["sources"]), 3)
        self.assertIn("fixture evidence quote", waiting["sources"][0]["evidence_quotes"])

        accepted = _post_json(
            self.base_url + "/api/action",
            {
                "action": "accept_experience_candidate",
                "experience_candidate_id": self.waiting_id,
                "reason": "fixture user accepts this evidence-backed candidate",
            },
        )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["core_result"]["result"]["decision"], "accepted")
        self.assertEqual(accepted["core_result"]["result"]["status"], "validation_ready")

        rejected_id = next(
            item["experience_candidate_id"]
            for item in self._candidates()
            if item["experience_candidate_id"] != self.preparing_id
            and item["status"] == "awaiting_human_decision"
        ) if any(
            item["experience_candidate_id"] != self.preparing_id
            and item["status"] == "awaiting_human_decision"
            for item in self._candidates()
        ) else None
        if rejected_id is None:
            rejected_id = _open_candidate_for_rejection(self.database)
        rejected = _post_json(
            self.base_url + "/api/action",
            {
                "action": "reject_experience_candidate",
                "experience_candidate_id": rejected_id,
                "reason": "fixture user rejects this candidate",
            },
        )
        self.assertTrue(rejected["ok"], rejected)
        self.assertEqual(rejected["core_result"]["result"]["decision"], "rejected")

        blocked = _post_json(
            self.base_url + "/api/action",
            {
                "action": "accept_experience_candidate",
                "experience_candidate_id": self.preparing_id,
                "reason": "must not be accepted while preparing",
            },
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["outcome"], "rejected")

        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            self.assertEqual(
                core.conn.execute(
                    "SELECT status FROM stage0_experience_candidate WHERE experience_candidate_id=?",
                    (self.preparing_id,),
                ).fetchone()[0],
                "preparing",
            )
            self.assertEqual(
                core.conn.execute("SELECT COUNT(*) FROM stage0_confirmed_experience").fetchone()[0],
                0,
            )
        finally:
            core.close()

    def test_page_exposes_minimal_candidate_review_without_database_access(self) -> None:
        with urlopen(self.base_url + "/") as response:
            html = response.read().decode("utf-8")
        javascript = (Path(__file__).resolve().parents[1] / "scripts" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("experience-candidates", html)
        self.assertIn("/api/experience-candidates", javascript)
        self.assertIn("accept_experience_candidate", javascript)
        self.assertIn("reject_experience_candidate", javascript)
        self.assertNotIn("sqlite", javascript.lower())


def _open_candidate_for_rejection(database: Path) -> str:
    core = Stage0ContentProductionCore.open(database, data_identity="test")
    try:
        run_id = "candidate-run-rejection"
        _insert_candidate_run(core, run_id, "candidate-domain")
        return _open_candidate(
            core,
            run_id=run_id,
            domain="candidate-domain",
            prefix="rejection",
            complete=True,
        )
    finally:
        core.close()


class ExperienceUsageTraceTests(unittest.TestCase):
    def test_content_plan_adoption_is_persisted_and_final_run_can_trace_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                _insert_candidate_run(core, "usage-run-1", "music_entertainment")
                experience_one_candidate = _open_candidate(
                    core,
                    run_id="usage-run-1",
                    domain="music_entertainment",
                    prefix="adopted",
                    complete=True,
                    decision="accepted",
                )
                experience_one_id = _seed_formal_experience(core, experience_one_candidate)
                _insert_candidate_run(core, "usage-run-2", "music_entertainment")
                experience_two_candidate = _open_candidate(
                    core,
                    run_id="usage-run-2",
                    domain="music_entertainment",
                    prefix="retrieved-only",
                    complete=True,
                    decision="accepted",
                )
                experience_ids = [experience_one_id]
                experience_two_id = _seed_formal_experience(core, experience_two_candidate)
                experience_ids.append(experience_two_id)
                task = core.create_task(
                    topic_payload={
                        "title": "music topic experience trace",
                        "core_question": "which evidence-backed method applies",
                        "domain": "music_entertainment",
                        "domain_label": "music_entertainment",
                    },
                    actor="fixture-user",
                    actor_kind="user",
                    reason="fixture content task",
                    idempotency_key="usage-task",
                )
                task_id = str(task["task_id"])
                core._insert_artifact_payload(
                    str(task["topic_version_id"]),
                    "formal_topic",
                    {
                        "title": "music topic experience trace",
                        "core_question": "which evidence-backed method applies",
                        "domain": "music_entertainment",
                        "domain_label": "music_entertainment",
                        "source_refs": [],
                    },
                )

                def executor(external_task: dict) -> dict:
                    node = str(external_task["business_context"]["node"])
                    if node == "research_plan":
                        output = {
                            "research_objective": "prepare the approved research basis",
                            "research_scope": {"included": ["fixture"], "excluded": ["outside scope"]},
                            "research_sequence": ["read supplied material"],
                            "research_questions": ["what is supported"],
                            "source_plan": ["use supplied material"],
                            "required_outputs": ["research conclusion"],
                        }
                        return {
                            "execution_id": "usage-research-execution",
                            "executor_id": "test-executor",
                            "model_ref": "executor-current-model",
                            "output": output,
                        }
                    input_assembly = external_task["input"]["input_assembly"]
                    supplied_ids = [
                        str(item["experience_id"])
                        for item in input_assembly["considered_experience"]
                    ]
                    self.assertEqual(set(supplied_ids), set(experience_ids))
                    return {
                        "execution_id": "usage-content-plan-execution",
                        "executor_id": "test-executor",
                        "model_ref": "executor-current-model",
                        "output": {
                            "node": "content_plan",
                            "document": {"outline": "use one applicable method"},
                            "source_boundaries": ["fixture material only"],
                            "unresolved": ["none"],
                        },
                        "experience_usage": {
                            "adopted_experience_ids": [experience_ids[0]],
                            "not_adopted_experience_ids": [experience_ids[1]],
                            "rationale": "the first experience matches this topic; the second was retrieved but not adopted",
                        },
                    }

                research_service = Stage1AResearchPlanService(
                    core=core,
                    external_executor=executor,
                )
                research_result = research_service.generate_research_plan(
                    task_id=task_id,
                    user_requirements="prepare research",
                    actor="fixture-user",
                    idempotency_key="usage-research",
                )
                research_service.approve_research_plan(
                    task_id=task_id,
                    research_plan_version_id=str(research_result["node_version_id"]),
                    actor="fixture-user",
                    reason="fixture research approval",
                    idempotency_key="usage-research-approve",
                )
                current = core.get_task(task_id)
                deep_assembly = InputAssembly(
                    task_id=task_id,
                    node="deep_research",
                    upstream_version_id=str(current["current_version_id"]),
                    user_requirements="fixture deep research",
                    material_refs=(),
                    research_refs=(),
                    content_plan_ref=None,
                    considered_experience=(),
                    adopted_experience=(),
                    rejected_experience=(),
                    omitted_materials=(),
                    prompt_version="fixture.deep_research.prompt",
                    skill_version="fixture.deep_research.skill",
                    model_config_version="fixture.model_config",
                )
                deep_assembly_result = core.create_input_assembly(
                    deep_assembly,
                    idempotency_key="usage-deep-research-assembly",
                )
                deep_request = core.create_node_request(
                    task_id=task_id,
                    node="deep_research",
                    input_assembly_id=deep_assembly_result["assembly_id"],
                    actor="fixture-user",
                    idempotency_key="usage-deep-research-request",
                )
                deep_output = {"subject": "fixture", "source_map": []}
                deep_run_id = core.record_external_node_execution(
                    task_id=task_id,
                    node_version_id=deep_request["node_version_id"],
                    execution_id="usage-deep-research-execution",
                    executor_id="test-executor",
                    model_ref="executor-current-model",
                    submitted_at=None,
                    output_payload=deep_output,
                )
                deep_result = core.complete_node_from_external_result(
                    task_id=task_id,
                    node_version_id=deep_request["node_version_id"],
                    model_run_id=deep_run_id,
                    output_ref="fixture-deep-research",
                    validation_status="passed",
                    actor="fixture-user",
                    expected_task_revision=int(deep_request["task_revision"]),
                    idempotency_key="usage-deep-research-complete",
                    artifact_payload=deep_output,
                )
                core.approve_current_node(
                    task_id=task_id,
                    version_id=str(deep_result["node_version_id"]),
                    actor="fixture-user",
                    actor_kind="user",
                    reason="fixture deep research approval",
                    idempotency_key="usage-deep-research-approve",
                )
                content_service = Stage1CContentPipelineService(
                    core=core,
                    external_executor=executor,
                )
                output = content_service.generate(
                    task_id=task_id,
                    actor="fixture-user",
                    user_requirements="prepare content plan",
                    idempotency_key="usage-content-plan",
                )
                content_plan_version_id = str(output["node_version_id"])
                content_service.approve(
                    task_id=task_id,
                    version_id=content_plan_version_id,
                    actor="fixture-user",
                    reason="fixture content plan approval",
                    idempotency_key="usage-content-plan-approve",
                )
                current = core.get_task(task_id)
                usage_by_task = core.list_content_experience_usage(task_id=task_id)
                usage_by_final_version = core.list_content_experience_usage(
                    content_version_id=str(current["current_version_id"])
                )
                self.assertEqual([item["experience_id"] for item in usage_by_task], [experience_ids[0]])
                self.assertEqual(
                    [item["experience_id"] for item in usage_by_final_version],
                    [experience_ids[0]],
                )
                self.assertEqual(usage_by_task[0]["content_version_id"], content_plan_version_id)
                self.assertEqual(usage_by_task[0]["experience"]["source_ids"], _source_ids("adopted"))
                self.assertIn(
                    "fixture evidence quote",
                    usage_by_task[0]["source_cards"][0]["evidence_quotes"],
                )
                self.assertNotIn(experience_ids[1], {item["experience_id"] for item in usage_by_task})
                workbench = core.list_content_workbench()
                self.assertEqual(workbench[0]["experience_usage"][0]["experience_id"], experience_ids[0])
            finally:
                core.close()


class ExperienceValidationChainTests(unittest.TestCase):
    def test_validation_candidate_requires_p7_support_before_manual_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "validation.sqlite3"
            core = Stage0ContentProductionCore.open(database, data_identity="test")
            try:
                _insert_candidate_run(core, "validation-run", "music_entertainment")
                support_id = _open_candidate(
                    core,
                    run_id="validation-run",
                    domain="music_entertainment",
                    prefix="validation-support",
                    complete=True,
                    decision="accepted",
                )
                contradict_id = _open_candidate(
                    core,
                    run_id="validation-run",
                    domain="music_entertainment",
                    prefix="validation-contradict",
                    complete=True,
                    decision="accepted",
                )
                inconclusive_id = _open_candidate(
                    core,
                    run_id="validation-run",
                    domain="music_entertainment",
                    prefix="validation-inconclusive",
                    complete=True,
                    decision="accepted",
                )
                retrieved_only_id = _open_candidate(
                    core,
                    run_id="validation-run",
                    domain="music_entertainment",
                    prefix="validation-retrieved-only",
                    complete=True,
                    decision="accepted",
                )
                adopted_candidate_ids = [support_id, contradict_id, inconclusive_id]
                all_candidate_ids = [*adopted_candidate_ids, retrieved_only_id]

                self.assertEqual(
                    {
                        item["status"]
                        for item in core.list_pre_topic_experience_candidates(
                            domain_label="music_entertainment"
                        )
                    },
                    {"validation_ready"},
                )
                self.assertEqual(
                    core.conn.execute("SELECT COUNT(*) FROM stage0_confirmed_experience").fetchone()[0],
                    0,
                )

                task = core.create_task(
                    topic_payload={
                        "title": "music topic validation trace",
                        "core_question": "which experience candidate is supported by production results",
                        "domain": "music_entertainment",
                        "domain_label": "music_entertainment",
                    },
                    actor="fixture-user",
                    actor_kind="user",
                    reason="fixture validation task",
                    idempotency_key="validation-task",
                )
                task_id = str(task["task_id"])
                core._insert_artifact_payload(
                    str(task["topic_version_id"]),
                    "formal_topic",
                    {
                        "title": "music topic validation trace",
                        "core_question": "which experience candidate is supported by production results",
                        "domain": "music_entertainment",
                        "domain_label": "music_entertainment",
                        "source_refs": [],
                    },
                )

                def executor(external_task: dict) -> dict:
                    node = str(external_task["business_context"]["node"])
                    if node == "research_plan":
                        return {
                            "execution_id": "validation-research-execution",
                            "executor_id": "test-executor",
                            "model_ref": "executor-current-model",
                            "output": {
                                "research_objective": "prepare the approved research basis",
                                "research_scope": {"included": ["fixture"], "excluded": ["outside scope"]},
                                "research_sequence": ["read supplied material"],
                                "research_questions": ["what is supported"],
                                "source_plan": ["use supplied material"],
                                "required_outputs": ["research conclusion"],
                            },
                        }
                    input_assembly = external_task["input"]["input_assembly"]
                    supplied_ids = {
                        str(item["experience_candidate_id"])
                        for item in input_assembly["considered_validation_candidates"]
                    }
                    self.assertEqual(supplied_ids, set(all_candidate_ids))
                    return {
                        "execution_id": "validation-content-plan-execution",
                        "executor_id": "test-executor",
                        "model_ref": "executor-current-model",
                        "output": {
                            "node": "content_plan",
                            "document": {"outline": "validate selected candidate hypotheses"},
                            "source_boundaries": ["fixture material only"],
                            "unresolved": ["none"],
                        },
                        "experience_usage": {
                            "adopted_experience_ids": [],
                            "not_adopted_experience_ids": [],
                            "rationale": "no formal experience exists in this isolated TEST",
                        },
                        "validation_usage": {
                            "adopted_candidate_ids": adopted_candidate_ids,
                            "not_adopted_candidate_ids": [retrieved_only_id],
                            "rationale": "only three candidates were explicitly selected as validation inputs",
                        },
                    }

                research_service = Stage1AResearchPlanService(
                    core=core,
                    external_executor=executor,
                )
                research_result = research_service.generate_research_plan(
                    task_id=task_id,
                    user_requirements="prepare research",
                    actor="fixture-user",
                    idempotency_key="validation-research",
                )
                research_service.approve_research_plan(
                    task_id=task_id,
                    research_plan_version_id=str(research_result["node_version_id"]),
                    actor="fixture-user",
                    reason="fixture research approval",
                    idempotency_key="validation-research-approve",
                )
                current = core.get_task(task_id)
                deep_assembly = InputAssembly(
                    task_id=task_id,
                    node="deep_research",
                    upstream_version_id=str(current["current_version_id"]),
                    user_requirements="fixture deep research",
                    material_refs=(),
                    research_refs=(),
                    content_plan_ref=None,
                    considered_experience=(),
                    adopted_experience=(),
                    rejected_experience=(),
                    omitted_materials=(),
                    prompt_version="fixture.deep_research.prompt",
                    skill_version="fixture.deep_research.skill",
                    model_config_version="fixture.model_config",
                )
                deep_assembly_result = core.create_input_assembly(
                    deep_assembly,
                    idempotency_key="validation-deep-research-assembly",
                )
                deep_request = core.create_node_request(
                    task_id=task_id,
                    node="deep_research",
                    input_assembly_id=deep_assembly_result["assembly_id"],
                    actor="fixture-user",
                    idempotency_key="validation-deep-research-request",
                )
                deep_run_id = core.record_external_node_execution(
                    task_id=task_id,
                    node_version_id=deep_request["node_version_id"],
                    execution_id="validation-deep-research-execution",
                    executor_id="test-executor",
                    model_ref="executor-current-model",
                    submitted_at=None,
                    output_payload={"subject": "fixture", "source_map": []},
                )
                deep_result = core.complete_node_from_external_result(
                    task_id=task_id,
                    node_version_id=deep_request["node_version_id"],
                    model_run_id=deep_run_id,
                    output_ref="fixture-deep-research",
                    validation_status="passed",
                    actor="fixture-user",
                    expected_task_revision=int(deep_request["task_revision"]),
                    idempotency_key="validation-deep-research-complete",
                    artifact_payload={"subject": "fixture", "source_map": []},
                )
                core.approve_current_node(
                    task_id=task_id,
                    version_id=str(deep_result["node_version_id"]),
                    actor="fixture-user",
                    actor_kind="user",
                    reason="fixture deep research approval",
                    idempotency_key="validation-deep-research-approve",
                )

                content_service = Stage1CContentPipelineService(
                    core=core,
                    external_executor=executor,
                )
                content_plan = content_service.generate(
                    task_id=task_id,
                    actor="fixture-user",
                    user_requirements="prepare content plan",
                    idempotency_key="validation-content-plan",
                )
                content_plan_version_id = str(content_plan["node_version_id"])
                content_service.approve(
                    task_id=task_id,
                    version_id=content_plan_version_id,
                    actor="fixture-user",
                    reason="fixture content plan approval",
                    idempotency_key="validation-content-plan-approve",
                )

                usage = core.list_content_validation_usage(task_id=task_id)
                self.assertEqual(
                    {item["experience_candidate_id"] for item in usage},
                    set(adopted_candidate_ids),
                )
                self.assertNotIn(retrieved_only_id, {item["experience_candidate_id"] for item in usage})
                self.assertTrue(all(item["source_cards"] for item in usage))

                with self.assertRaises(StateTransitionError):
                    core.promote_experience_candidate(
                        experience_candidate_id=support_id,
                        actor="fixture-user",
                        actor_kind="user",
                        reason="must wait for P7 evidence",
                    )
                self.assertEqual(
                    core.conn.execute("SELECT COUNT(*) FROM stage0_confirmed_experience").fetchone()[0],
                    0,
                )

                core.conn.execute(
                    "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        "validation-owned-account",
                        "TEST owned account",
                        "music_entertainment",
                        "validation-account-ref",
                        "test",
                        "fixture-user",
                        "2026-08-30T00:00:00+00:00",
                    ),
                )
                core.conn.execute(
                    "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                    (
                        "validation-audio-delivery",
                        task_id,
                        content_plan_version_id,
                        "test-audio-ref",
                        "test",
                        "fixture-user",
                        "2026-08-30T00:00:00+00:00",
                    ),
                )
                core.conn.commit()
                publication = register_publication(
                    core.conn,
                    publication_id="validation-publication",
                    content_account_id="validation-owned-account",
                    domain_label="music_entertainment",
                    task_id=task_id,
                    audio_delivery_id="validation-audio-delivery",
                    approved_content_version_id=content_plan_version_id,
                    platform="test-platform",
                    external_video_url="https://example.test/validation-publication",
                    published_at="2026-08-30T00:00:00+00:00",
                    actual_content_status="same_as_approved",
                    actual_content_note="",
                    confirmed_by="fixture-user",
                    data_identity="test",
                    created_by="fixture-user",
                )
                self.assertEqual(publication["status"], "registered")
                for index in range(8):
                    record_observation(
                        core.conn,
                        observation_id=f"validation-observation-{index}",
                        publication_id="validation-publication",
                        point_code=f"P{index}",
                        observation_status="recorded",
                        metrics={"observed_value": 100 + index},
                        missing_reason="",
                        source_ref=f"test://validation/P{index}",
                        observed_at=f"2026-08-{30 + index:02d}T00:00:00+00:00",
                        recorded_by="fixture-user",
                        data_identity="test",
                    )
                validation_results = [
                    {
                        "experience_candidate_id": support_id,
                        "result": "supports",
                        "performance_summary": "the observed production result is consistent with the candidate",
                        "review_basis": "P0-P7 TEST observations and the human review record",
                    },
                    {
                        "experience_candidate_id": contradict_id,
                        "result": "contradicts",
                        "performance_summary": "the observed production result conflicts with the candidate",
                        "review_basis": "P0-P7 TEST observations and the human review record",
                    },
                    {
                        "experience_candidate_id": inconclusive_id,
                        "result": "inconclusive",
                        "performance_summary": "the observed production result is not decisive",
                        "review_basis": "P0-P7 TEST observations and the human review record",
                    },
                ]
                review = prepare_p7_review(
                    core.conn,
                    review_id="validation-p7-review",
                    publication_id="validation-publication",
                    selection_assessment="fixture selection assessment",
                    narrative_assessment="fixture narrative assessment",
                    material_assessment="fixture material assessment",
                    external_conditions_assessment="fixture external conditions assessment",
                    feedback_candidate={"validation_results": validation_results},
                    created_by="fixture-user",
                    data_identity="test",
                )
                self.assertEqual(review["status"], "awaiting_user_confirmation")
                with self.assertRaises(StateTransitionError):
                    core.promote_experience_candidate(
                        experience_candidate_id=support_id,
                        actor="fixture-user",
                        actor_kind="user",
                        reason="P7 review still needs human confirmation",
                    )
                decided = decide_p7_review(
                    core.conn,
                    review_id="validation-p7-review",
                    decision="confirmed",
                    actor="fixture-user",
                    reason="fixture confirms the P7 review record",
                    data_identity="test",
                )
                self.assertEqual(decided["status"], "confirmed")

                review_row = core.conn.execute(
                    "SELECT feedback_candidate_json FROM stage0_publication_p7_review WHERE review_id=?",
                    ("validation-p7-review",),
                ).fetchone()
                stored_results = json.loads(str(review_row["feedback_candidate_json"]))["validation_results"]
                self.assertEqual(
                    {item["result"] for item in stored_results},
                    {"supports", "contradicts", "inconclusive"},
                )
                self.assertTrue(all(item["validation_usage_ids"] for item in stored_results))

                promoted = core.promote_experience_candidate(
                    experience_candidate_id=support_id,
                    actor="fixture-user",
                    actor_kind="user",
                    reason="fixture human confirms supportive production evidence",
                )
                self.assertEqual(promoted["status"], "promoted")
                self.assertEqual(
                    core.conn.execute("SELECT COUNT(*) FROM stage0_confirmed_experience").fetchone()[0],
                    1,
                )
                promoted_view = next(
                    item
                    for item in core.list_all_experience_candidates()
                    if item["experience_candidate_id"] == support_id
                )
                self.assertEqual(promoted_view["status"], "promoted")
                self.assertEqual(
                    len(core.list_content_validation_usage(task_id=task_id)),
                    3,
                )
            finally:
                core.close()


def _source_ids(prefix: str) -> list[str]:
    return [f"{prefix}-source-{index}" for index in range(3)]


if __name__ == "__main__":
    unittest.main()
