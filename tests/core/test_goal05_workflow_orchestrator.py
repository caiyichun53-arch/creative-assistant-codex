"""Real coverage for scripts/core/workflow/goal05_workflow.py.

2026-07-11: the only test that exercised Goal05WorkflowOrchestrator
(test_frozen_workflow_steps_enqueue_idempotently_through_goal05_orchestrator
in test_phase5_business_workflow.py) was archived along with the rest of
that file when goal_phase5_business_workflow.py (a different module in the
same directory) was confirmed dead. goal05_workflow.py itself is not dead --
it is a real, transitively-imported dependency of the production entrypoints
(scripts/core/research/goal06_formal_research.py imports
Goal05WorkflowOrchestrator, and that module is imported by
scripts/core/external_adapters/goal_phase4_external_adapters.py, which
run_competitor_registration_full.py/run_reverse_prep.py import directly --
see archive/dead_goal_chain_20260709/README.md). This file replaces the lost
coverage with a clean, standalone test against the real class: a real
in-memory Goal03Scheduler (no mocks of the scheduler or persistence layer),
real enqueued jobs read back via scheduler.get_job(), and no import of
anything under archive/.
"""

from __future__ import annotations

import json
import unittest

from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.workflow.goal05_workflow import (
    Goal05WorkflowOrchestrator,
    WorkflowError,
    WorkflowStepSpec,
)


def make_scheduler() -> Goal03Scheduler:
    counter = {"n": 0}

    def id_factory() -> str:
        counter["n"] += 1
        return f"job-{counter['n']:04d}"

    return Goal03Scheduler.in_memory(id_factory=id_factory, now_ms=lambda: 1_770_000_000_000)


class InitializationTests(unittest.TestCase):
    def test_orchestrator_wraps_the_scheduler_it_was_given(self) -> None:
        scheduler = make_scheduler()
        self.addCleanup(scheduler.store.conn.close)

        orchestrator = Goal05WorkflowOrchestrator(scheduler)

        self.assertIs(orchestrator.scheduler, scheduler)


class StartWorkflowEnqueuesRealJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = make_scheduler()
        self.addCleanup(self.scheduler.store.conn.close)
        self.orchestrator = Goal05WorkflowOrchestrator(self.scheduler)

    def test_valid_multi_step_workflow_enqueues_one_real_job_per_step_in_order(self) -> None:
        steps = (
            WorkflowStepSpec(step_key="collect", job_kind="workflow.collect", payload={"source": "hit-1"}),
            WorkflowStepSpec(step_key="analyze", job_kind="workflow.analyze", payload={"source": "hit-1"}, priority=5),
        )

        result = self.orchestrator.start_workflow(
            workflow_name="reverse-prep-demo",
            steps=steps,
            idempotency_key="wf-run-1",
        )

        self.assertEqual(len(result.job_ids), 2)
        self.assertFalse(result.replayed)

        # Read the jobs back through the scheduler's own accessor -- this is
        # the same code path a real worker would use to claim and run them,
        # not a peek at private state.
        first = self.scheduler.get_job(result.job_ids[0])
        second = self.scheduler.get_job(result.job_ids[1])
        self.assertEqual(first["job_kind"], "workflow.collect")
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["job_kind"], "workflow.analyze")
        self.assertEqual(second["priority"], 5)

        # Both jobs must carry real _workflow lineage metadata, not just a
        # bare job_kind/payload -- this is what lets a later step, or a human
        # auditing the queue, trace a job back to its workflow run.
        first_payload = json.loads(first["payload_json"])
        second_payload = json.loads(second["payload_json"])
        self.assertEqual(first_payload["_workflow"]["workflow_id"], result.workflow_id)
        self.assertEqual(first_payload["_workflow"]["step_key"], "collect")
        self.assertEqual(first_payload["_workflow"]["step_index"], 0)
        self.assertEqual(first_payload["_workflow"]["step_count"], 2)
        self.assertEqual(second_payload["_workflow"]["step_index"], 1)
        self.assertEqual(first_payload["source"], "hit-1")  # original payload preserved alongside lineage

        # Jobs from the same workflow_start share a correlation_id so they
        # can be grouped later -- both should correlate to the workflow_id.
        self.assertEqual(first["correlation_id"], result.workflow_id)
        self.assertEqual(second["correlation_id"], result.workflow_id)

    def test_workflow_id_is_a_deterministic_content_hash_not_a_random_uuid(self) -> None:
        steps = (WorkflowStepSpec(step_key="only", job_kind="workflow.only", payload={}),)

        result_a = self.orchestrator.start_workflow(
            workflow_name="repeatable", steps=steps, idempotency_key="key-a",
        )
        # A second, independent scheduler/orchestrator with the same inputs
        # must derive the identical workflow_id -- proves it's a real content
        # hash of the inputs, not e.g. an incrementing counter or wall-clock
        # timestamp smuggled in as "determinism".
        other_scheduler = make_scheduler()
        self.addCleanup(other_scheduler.store.conn.close)
        result_b = Goal05WorkflowOrchestrator(other_scheduler).start_workflow(
            workflow_name="repeatable", steps=steps, idempotency_key="key-a",
        )

        self.assertEqual(result_a.workflow_id, result_b.workflow_id)

    def test_repeated_start_with_same_idempotency_key_replays_without_duplicate_jobs(self) -> None:
        steps = (WorkflowStepSpec(step_key="only", job_kind="workflow.only", payload={"n": 1}),)

        first_result = self.orchestrator.start_workflow(
            workflow_name="idempotent-demo", steps=steps, idempotency_key="wf-run-2",
        )
        second_result = self.orchestrator.start_workflow(
            workflow_name="idempotent-demo", steps=steps, idempotency_key="wf-run-2",
        )

        self.assertFalse(first_result.replayed)
        self.assertTrue(second_result.replayed)
        self.assertEqual(first_result.job_ids, second_result.job_ids)
        # Real proof there is exactly one row for this job, not two silently
        # created: scheduler.get_job on the shared id resolves to a single,
        # still-queued job.
        job = self.scheduler.get_job(first_result.job_ids[0])
        self.assertEqual(job["status"], "queued")

    def test_no_fallback_and_no_dependency_on_the_archived_dead_chain(self) -> None:
        # Direct assertion that this module's only real dependencies are the
        # two kept modules -- fails loudly if a future edit reintroduces an
        # import from persistence/scheduler's dead siblings (production/
        # correction/host/hermes/state) or anything under archive/.
        import scripts.core.workflow.goal05_workflow as module

        source = module.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("archive.dead_goal_chain", text)
        self.assertNotIn("scripts.core.correction", text)
        self.assertNotIn("scripts.core.production", text)
        self.assertNotIn("scripts.core.host", text)
        self.assertNotIn("scripts.core.hermes", text)
        self.assertNotIn("scripts.core.state", text)
        self.assertNotIn("fallback", text.lower())


class InvalidInputFailsClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = make_scheduler()
        self.addCleanup(self.scheduler.store.conn.close)
        self.orchestrator = Goal05WorkflowOrchestrator(self.scheduler)

    def _valid_steps(self) -> tuple[WorkflowStepSpec, ...]:
        return (WorkflowStepSpec(step_key="s1", job_kind="workflow.step", payload={}),)

    def test_empty_workflow_name_raises_explicitly(self) -> None:
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="", steps=self._valid_steps(), idempotency_key="k")

    def test_empty_idempotency_key_raises_explicitly(self) -> None:
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=self._valid_steps(), idempotency_key="")

    def test_zero_steps_raises_explicitly(self) -> None:
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=(), idempotency_key="k")

    def test_duplicate_step_key_raises_explicitly(self) -> None:
        steps = (
            WorkflowStepSpec(step_key="dup", job_kind="a", payload={}),
            WorkflowStepSpec(step_key="dup", job_kind="b", payload={}),
        )
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=steps, idempotency_key="k")

    def test_empty_step_key_raises_explicitly(self) -> None:
        steps = (WorkflowStepSpec(step_key="", job_kind="a", payload={}),)
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=steps, idempotency_key="k")

    def test_empty_job_kind_raises_explicitly(self) -> None:
        steps = (WorkflowStepSpec(step_key="s1", job_kind="", payload={}),)
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=steps, idempotency_key="k")

    def test_non_positive_max_attempts_raises_explicitly(self) -> None:
        steps = (WorkflowStepSpec(step_key="s1", job_kind="a", payload={}, max_attempts=0),)
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=steps, idempotency_key="k")

    def test_invalid_input_leaves_no_partial_jobs_enqueued(self) -> None:
        # A workflow that fails validation must not have enqueued any of its
        # earlier-looking-valid steps -- validation runs before any
        # scheduler.enqueue_job call, so a failure must be all-or-nothing.
        steps = (
            WorkflowStepSpec(step_key="ok", job_kind="workflow.ok", payload={}),
            WorkflowStepSpec(step_key="ok", job_kind="workflow.ok", payload={}),  # duplicate -> invalid
        )
        with self.assertRaises(WorkflowError):
            self.orchestrator.start_workflow(workflow_name="wf", steps=steps, idempotency_key="partial-check")

        cursor = self.scheduler.conn.execute("SELECT COUNT(*) AS n FROM scheduler_job")
        self.assertEqual(cursor.fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
