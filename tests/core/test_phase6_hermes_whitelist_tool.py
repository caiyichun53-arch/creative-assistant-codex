from __future__ import annotations

import unittest

from scripts.core.hermes.goal_phase6_whitelist_tool import (
    HermesWhitelistTool,
    HermesWhitelistToolError,
    HermesWhitelistToolRequest,
)
from scripts.core.model_gateway.formal_skill_adapter import (
    DeterministicResearchEvidenceExtractModelPort,
    sample_research_evidence_extract_input,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRoute, ModelRunMaterializer
from scripts.core.persistence.goal01_store import UUIDv7Generator, content_hash
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer
from scripts.core.workflow.goal_phase5_business_workflow import (
    BusinessWorkflowMaterializer,
    BusinessWorkflowWorker,
    FormalSkillDispatcher,
)


def make_core_and_scheduler():
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    core = CoreMaterializer.in_memory(id_factory=generator.new)
    core.grant_permission("hermes", "create_state")
    core.grant_permission("hermes", "transition_state")
    scheduler = Goal03Scheduler(core.store, id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
    return core, scheduler, generator


def make_research_gateway(store, *, include_route: bool = True):
    provider = DeterministicResearchEvidenceExtractModelPort()
    routes = {}
    if include_route:
        routes["business.research_evidence_extract"] = ModelRoute(
            route_name="business.research_evidence_extract",
            provider_name=provider.provider_name,
            model_name="deterministic-research-evidence-extract",
            config_version="GOAL-V0.6.2-PRODUCTION-COMPLETION-01.phase6.test.v1",
            config_hash=content_hash({"route": "business.research_evidence_extract"}),
            timeout_ms=1000,
        )
    return ModelGateway(
        routes=routes,
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
    )


def create_research_request(request_id: str = "phase6-create"):
    return HermesWhitelistToolRequest(
        action="create_controlled_task",
        actor="feishu-user",
        request_id=request_id,
        payload={
            "workflow_id": "business.research",
            "workflow_instance_id": f"workflow-{request_id}",
            "domain": "fan_kepu_social_life",
            "content_form": "short_video_script",
            "conditions": ["ordinary_life_problem"],
            "input_payloads": {
                "research_evidence_extract": sample_research_evidence_extract_input(
                    request_id=f"{request_id}-research-evidence"
                )
            },
        },
    )


class Phase6HermesWhitelistToolTests(unittest.TestCase):
    def test_create_controlled_task_enqueues_only_first_formal_workflow_step(self) -> None:
        core, scheduler, _generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)

        result = tool.execute(create_research_request())

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["task_state"], "queued")
        self.assertEqual(result.payload["workflow_id"], "business.research")
        self.assertEqual(result.payload["first_step_key"], "research_evidence_extract")
        self.assertEqual(len(result.payload["job_ids"]), 1)
        self.assertEqual(core.conn.execute("SELECT count(*) FROM production_task_state").fetchone()[0], 1)
        self.assertEqual(scheduler.conn.execute("SELECT count(*) FROM scheduler_job").fetchone()[0], 1)
        self.assertEqual(
            scheduler.conn.execute("SELECT job_kind FROM scheduler_job").fetchone()["job_kind"],
            "formal_skill.execute",
        )

    def test_query_status_and_result_after_worker_runs(self) -> None:
        core, scheduler, generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)
        created = tool.execute(create_research_request("phase6-result"))
        job_id = created.payload["job_ids"][0]
        dispatcher = FormalSkillDispatcher(
            store=core.store,
            gateway=make_research_gateway(core.store),
            id_factory=generator.new,
        )
        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(core.store),
            skill_executor=dispatcher,
            worker_id="phase6-worker",
        )

        step = worker.run_once()
        status = tool.execute(
            HermesWhitelistToolRequest(
                action="query_task_status",
                actor="feishu-user",
                request_id="phase6-status",
                payload={"task_id": created.payload["task_id"], "job_id": job_id},
            )
        )
        result = tool.execute(
            HermesWhitelistToolRequest(
                action="query_task_result",
                actor="feishu-user",
                request_id="phase6-result-query",
                payload={"job_id": job_id},
            )
        )

        self.assertEqual(step.status, "succeeded")
        self.assertEqual(status.payload["task"]["state"], "queued")
        self.assertEqual(status.payload["job"]["status"], "succeeded")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["formal_skill_id"], "research_evidence_extract")
        self.assertTrue(result.payload["result_version_id"])

    def test_query_failure_reason_after_missing_route_failure(self) -> None:
        core, scheduler, generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)
        created = tool.execute(create_research_request("phase6-failure"))
        job_id = created.payload["job_ids"][0]
        dispatcher = FormalSkillDispatcher(
            store=core.store,
            gateway=make_research_gateway(core.store, include_route=False),
            id_factory=generator.new,
        )
        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(core.store),
            skill_executor=dispatcher,
            worker_id="phase6-worker",
        )

        step = worker.run_once()
        reason = tool.execute(
            HermesWhitelistToolRequest(
                action="query_failure_reason",
                actor="feishu-user",
                request_id="phase6-failure-reason",
                payload={"job_id": job_id},
            )
        )

        self.assertEqual(step.status, "failed")
        self.assertEqual(reason.status, "succeeded")
        self.assertEqual(reason.payload["error"]["code"], "BusinessWorkflowError")
        self.assertIn("missing approved model route", reason.payload["error"]["message"])

    def test_cancel_task_cancels_scheduler_job_and_core_task(self) -> None:
        core, scheduler, _generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)
        created = tool.execute(create_research_request("phase6-cancel"))
        job_id = created.payload["job_ids"][0]

        cancelled = tool.execute(
            HermesWhitelistToolRequest(
                action="cancel_task",
                actor="feishu-user",
                request_id="phase6-cancel-request",
                payload={"task_id": created.payload["task_id"], "job_id": job_id},
            )
        )

        self.assertEqual(cancelled.status, "succeeded")
        self.assertEqual(scheduler.get_job(job_id)["status"], "cancelled")
        self.assertEqual(core.get_state("production_task", created.payload["task_id"])["state"], "cancelled")

    def test_query_human_confirmation_items(self) -> None:
        core, scheduler, _generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)
        created = core.execute(
            CoreCommandEnvelope(
                command_type="create_state",
                actor="hermes",
                object_kind="experiment",
                idempotency_key="phase6-experiment-create",
                payload={},
            )
        )
        core.execute(
            CoreCommandEnvelope(
                command_type="transition_state",
                actor="hermes",
                object_kind="experiment",
                object_id=created.object_id,
                expected_basis_version_id=created.basis_version_id,
                idempotency_key="phase6-experiment-confirmation",
                payload={"to_state": "active"},
                confirmed=False,
            )
        )

        result = tool.execute(
            HermesWhitelistToolRequest(
                action="query_human_confirmation_items",
                actor="feishu-user",
                request_id="phase6-confirmation-list",
                payload={},
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(result.payload["items"]), 1)
        self.assertEqual(result.payload["items"][0]["reason"], "confirmation_required")

    def test_business_model_binding_summary_is_non_sensitive_and_single_active_binding(self) -> None:
        core, scheduler, _generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)

        result = tool.execute(
            HermesWhitelistToolRequest(
                action="query_business_model_binding_summary",
                actor="feishu-user",
                request_id="phase6-model-summary",
                payload={},
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["binding_id"], "business.primary")
        self.assertEqual(result.payload["active_binding_count"], 1)
        self.assertFalse(result.payload["fallback_enabled"])
        self.assertTrue(result.payload["actual_model_value_redacted"])

    def test_non_whitelisted_actions_and_direct_execution_payloads_are_rejected(self) -> None:
        core, scheduler, _generator = make_core_and_scheduler()
        self.addCleanup(core.store.conn.close)
        tool = HermesWhitelistTool(core=core, scheduler=scheduler)

        with self.assertRaises(HermesWhitelistToolError):
            tool.execute(
                HermesWhitelistToolRequest(
                    action="shell",
                    actor="feishu-user",
                    request_id="phase6-shell",
                    payload={},
                )
            )
        with self.assertRaises(HermesWhitelistToolError):
            tool.execute(
                HermesWhitelistToolRequest(
                    action="create_controlled_task",
                    actor="feishu-user",
                    request_id="phase6-forbidden-payload",
                    payload={"workflow_id": "business.research", "shell_command": "python old_script.py"},
                )
            )


if __name__ == "__main__":
    unittest.main()
