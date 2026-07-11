"""Real tests for scripts/core/experience/goal09_experiments.py's two pieces
of pure business logic that B1 (2026-07-13, 置顶规则总表核对后, BR-EXPERIENCE-004)
fixed: compute_metric_signal()'s three-way support/not_supported/inconclusive
judgment (原文档29.1 安全默认阈值 1.5/0.8), and recompute_experience_state()'s
active/watch/paused/deprecated state machine (原文档"7 推荐状态状态机" §7.2-7.7).

Before this fix, neither function had ANY test coverage anywhere in the repo
(confirmed by a full-repo grep for "goal09_experiments" test imports before
writing this file) -- this is the first real exercise of this module.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from scripts.core.experience.goal09_experiments import (
    ExperienceEvidence,
    ExperienceStateInput,
    ExperimentError,
    ExperimentResultCommand,
    PPlusMetricInput,
    compute_metric_signal,
    recompute_experience_state,
)
from scripts.core.production.goal08_production_chain import VersionRef


def _ref(kind: str = "own_publication") -> VersionRef:
    return VersionRef(
        relation_role="evidence",
        target_object_kind=kind,
        target_stable_id="root-1",
        target_version_id="version-1",
        target_content_hash=None,
        locator={"path": "test-locator"},
    )


def _command(
    *,
    baseline: str = "100",
    observed: str = "100",
    support_ratio: str = "1.5",
    not_supported_ratio: str = "0.8",
    experiment_kind: str = "formal",
    primary_hypothesis_frozen: bool = True,
    actual_use_status: str = "used",
    major_confounder: bool = False,
) -> ExperimentResultCommand:
    return ExperimentResultCommand(
        account_id="acct-1",
        topic_id="topic-1",
        actor="tester",
        idempotency_key="k1",
        experiment_kind=experiment_kind,
        primary_hypothesis_ref=_ref(),
        publication_capture_ref=_ref(),
        metric=PPlusMetricInput(
            metric_name="like_count",
            baseline_value=baseline,
            observed_value=observed,
            support_ratio=support_ratio,
            not_supported_ratio=not_supported_ratio,
        ),
        primary_hypothesis_frozen=primary_hypothesis_frozen,
        actual_use_status=actual_use_status,
        major_confounder=major_confounder,
    )


class ComputeMetricSignalThreeWayTests(unittest.TestCase):
    def test_ratio_at_or_above_support_threshold_is_supported(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="150"))
        self.assertEqual(signal.signal, "supported")
        self.assertTrue(signal.eligible)
        self.assertEqual(signal.ratio, "1.5")

    def test_ratio_above_support_threshold_is_supported(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="200"))
        self.assertEqual(signal.signal, "supported")

    def test_ratio_at_or_below_not_supported_threshold_is_not_supported(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="80"))
        self.assertEqual(signal.signal, "not_supported")
        self.assertEqual(signal.ratio, "0.8")

    def test_ratio_below_not_supported_threshold_is_not_supported(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="20"))
        self.assertEqual(signal.signal, "not_supported")

    def test_ratio_strictly_between_thresholds_is_inconclusive(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="110"))
        self.assertEqual(signal.signal, "inconclusive")
        self.assertTrue(signal.eligible)
        self.assertEqual(signal.ratio, "1.1")

    def test_default_thresholds_match_source_document_1_5_and_0_8(self) -> None:
        command = ExperimentResultCommand(
            account_id="acct-1",
            topic_id="topic-1",
            actor="tester",
            idempotency_key="k1",
            experiment_kind="formal",
            primary_hypothesis_ref=_ref(),
            publication_capture_ref=_ref(),
            metric=PPlusMetricInput(metric_name="like_count", baseline_value="100", observed_value="150"),
            primary_hypothesis_frozen=True,
            actual_use_status="used",
        )
        self.assertEqual(command.metric.support_ratio, Decimal("1.5"))
        self.assertEqual(command.metric.not_supported_ratio, Decimal("0.8"))
        self.assertEqual(compute_metric_signal(command).signal, "supported")

    def test_not_supported_ratio_must_be_lower_than_support_ratio(self) -> None:
        with self.assertRaises(ExperimentError):
            compute_metric_signal(_command(support_ratio="1.0", not_supported_ratio="1.0"))

    def test_not_supported_ratio_must_be_positive(self) -> None:
        with self.assertRaises(ExperimentError):
            compute_metric_signal(_command(not_supported_ratio="0"))

    def test_major_confounder_forces_inconclusive_regardless_of_ratio(self) -> None:
        signal = compute_metric_signal(_command(baseline="100", observed="500", major_confounder=True))
        self.assertEqual(signal.signal, "inconclusive")
        self.assertEqual(signal.reason, "major confounder recorded")

    def test_observational_experiment_is_ineligible(self) -> None:
        signal = compute_metric_signal(_command(experiment_kind="observational"))
        self.assertEqual(signal.signal, "ineligible")
        self.assertFalse(signal.eligible)


def _evidence(
    *,
    evidence_id: str,
    sequence: int,
    kind: str = "formal_p_result",
    signal: str | None = None,
    question: str = "q1",
    eligible: bool = True,
    primary_used: bool = True,
) -> ExperienceEvidence:
    return ExperienceEvidence(
        evidence_id=evidence_id,
        evidence_kind=kind,
        independence_key=evidence_id,
        sequence=sequence,
        eligible=eligible,
        metric_signal=signal,
        primary_used=primary_used,
        core_question_hash=question,
    )


class RecommendationStateMachineTests(unittest.TestCase):
    def test_no_evidence_stays_active(self) -> None:
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=())
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_one_formal_failure_moves_active_to_watch(self) -> None:
        evidence = (_evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),)
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "watch")

    def test_two_independent_external_counterexamples_move_active_to_watch(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, kind="external_counterexample", question="q1"),
            _evidence(evidence_id="e2", sequence=2, kind="external_counterexample", question="q2"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "watch")

    def test_one_external_counterexample_alone_does_not_trigger_watch(self) -> None:
        evidence = (_evidence(evidence_id="e1", sequence=1, kind="external_counterexample", question="q1"),)
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_three_failures_across_three_questions_moves_to_paused(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q2"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q3"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "paused")

    def test_three_failures_on_the_same_question_only_counts_once_stays_watch(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q1"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q1"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="watch", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "watch")

    def test_external_counterexamples_alone_cannot_cause_pause(self) -> None:
        evidence = tuple(
            _evidence(evidence_id=f"e{i}", sequence=i, kind="external_counterexample", question=f"q{i}")
            for i in range(1, 6)
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="watch", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "watch")

    def test_watch_recovers_to_active_on_one_new_support_after_risk_evidence(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="supported", question="q1"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="watch", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_watch_stays_watch_if_support_is_older_than_the_failure(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q1"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="watch", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "watch")

    def test_paused_requires_two_supports_across_two_questions_not_just_one(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q2"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q3"),
            _evidence(evidence_id="e4", sequence=4, signal="supported", question="q1"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="paused", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "paused")

    def test_paused_recovers_to_active_on_two_supports_across_two_questions(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q2"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q3"),
            _evidence(evidence_id="e4", sequence=4, signal="supported", question="q1"),
            _evidence(evidence_id="e5", sequence=5, signal="supported", question="q2"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="paused", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_paused_recovery_count_resets_on_a_new_failure(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q2"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q3"),
            _evidence(evidence_id="e4", sequence=4, signal="supported", question="q1"),
            _evidence(evidence_id="e5", sequence=5, signal="not_supported", question="q1"),
            _evidence(evidence_id="e6", sequence=6, signal="supported", question="q2"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="paused", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "paused")

    def test_deprecated_is_terminal_and_never_auto_restored(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="supported", question="q2"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="deprecated", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "deprecated")

    def test_manual_lock_blocks_automatic_transition(self) -> None:
        evidence = (
            _evidence(evidence_id="e1", sequence=1, signal="not_supported", question="q1"),
            _evidence(evidence_id="e2", sequence=2, signal="not_supported", question="q2"),
            _evidence(evidence_id="e3", sequence=3, signal="not_supported", question="q3"),
        )
        result = recompute_experience_state(
            ExperienceStateInput(
                tactic_key="t1", current_recommendation_status="active", evidence=evidence, manual_lock=True
            )
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_ineligible_evidence_is_never_counted(self) -> None:
        evidence = tuple(
            _evidence(evidence_id=f"e{i}", sequence=i, signal="not_supported", question=f"q{i}", eligible=False)
            for i in range(1, 4)
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "active")

    def test_non_primary_used_formal_result_is_never_counted(self) -> None:
        evidence = tuple(
            _evidence(evidence_id=f"e{i}", sequence=i, signal="not_supported", question=f"q{i}", primary_used=False)
            for i in range(1, 4)
        )
        result = recompute_experience_state(
            ExperienceStateInput(tactic_key="t1", current_recommendation_status="active", evidence=evidence)
        )
        self.assertEqual(result.recommendation_status, "active")


if __name__ == "__main__":
    unittest.main()
