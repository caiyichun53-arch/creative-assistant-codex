from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from tests.test_core_unified_status import _seed_domain


class FinalContentConfirmationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "final-content-confirmation.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        _seed_domain(self.core, "music_entertainment", cold_status="completed")
        self.business = CreationAssistantFormalBusinessCore(core=self.core)

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    def _create_current_final_task(
        self,
        *,
        label: str,
        current_version_id: str | None = None,
        old_version_id: str | None = None,
    ) -> tuple[str, str, str | None]:
        activation = self.core.get_current_domain_activation(
            domain_label="music_entertainment"
        )
        assert activation is not None
        created = self.core.create_task(
            topic_payload={
                "title": f"{label}正式主题",
                "core_question": "验证最终确认门禁",
                "domain": "music_entertainment",
                "domain_label": "music_entertainment",
                "activation_id": activation["activation_id"],
            },
            actor="test-user",
            actor_kind="user",
            reason=f"创建{label}任务",
            idempotency_key=f"create-{label}",
        )
        task_id = str(created["task_id"])
        topic_version_id = str(created["topic_version_id"])
        self.core._insert_artifact_payload(
            topic_version_id,
            "formal_topic",
            {
                "title": f"{label}正式主题",
                "core_question": "验证最终确认门禁",
                "domain": "music_entertainment",
                "domain_label": "music_entertainment",
                "activation_id": activation["activation_id"],
                "source_refs": [],
            },
        )

        old_version = old_version_id
        if old_version is not None:
            self._insert_review_version(
                task_id=task_id,
                version_id=old_version,
                upstream_version_id=topic_version_id,
                revision=1,
            )

        current_version = current_version_id or f"{label}-current-review"
        self._insert_review_version(
            task_id=task_id,
            version_id=current_version,
            upstream_version_id=old_version or topic_version_id,
            revision=2 if old_version is not None else 1,
        )
        self.core._set_task(
            task_id,
            node="user_final_confirmation",
            version_id=current_version,
            status="approved",
            revision=2 if old_version is not None else 1,
        )
        self.core.conn.commit()
        return task_id, current_version, old_version

    def _insert_review_version(
        self,
        *,
        task_id: str,
        version_id: str,
        upstream_version_id: str,
        revision: int,
    ) -> None:
        now = "2026-08-31T00:00:00+08:00"
        self.core.conn.execute(
            "INSERT INTO stage0_content_node_version VALUES "
            "(?, ?, 'review', NULL, ?, NULL, 'approved', ?, 'passed', ?, ?, ?, ?)",
            (
                version_id,
                task_id,
                upstream_version_id,
                f"{version_id}-output",
                "test",
                "fixture",
                now,
                revision,
            ),
        )
        self.core._insert_artifact_payload(
            version_id,
            "review",
            {
                "node": "review",
                "document": {
                    "title": "测试最终内容",
                    "script_text": "测试正文",
                },
                "source_boundaries": ["TEST fixture"],
                "unresolved": [],
            },
        )

    def _approve_review(self, task_id: str, version_id: str) -> None:
        self.core._decision(
            task_id,
            "review",
            version_id,
            "approved",
            "test-user",
            "user",
            "用户批准审核结果",
        )
        self.core.conn.commit()

    def _confirm_final(self, task_id: str, version_id: str) -> dict[str, str]:
        return self.business.approve_final_content(
            task_id=task_id,
            version_id=version_id,
            actor="test-user",
            reason="用户确认最终内容",
            idempotency_key=f"confirm-{task_id}-{version_id}",
        )

    def test_current_review_approval_allows_final_confirmation_once(self) -> None:
        task_id, version_id, _ = self._create_current_final_task(label="current")
        self._approve_review(task_id, version_id)

        result = self._confirm_final(task_id, version_id)

        self.assertEqual(result["status"], "confirmed")
        decisions = self.core.conn.execute(
            "SELECT node, decision, actor_kind FROM stage0_content_decision "
            "WHERE task_id=? AND version_id=? ORDER BY rowid",
            (task_id, version_id),
        ).fetchall()
        self.assertEqual(
            [(row["node"], row["decision"], row["actor_kind"]) for row in decisions],
            [("review", "approved", "user"), ("user_final_confirmation", "approved", "user")],
        )
        replayed = self._confirm_final(task_id, version_id)
        self.assertEqual(replayed, result)
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_content_decision "
                "WHERE task_id=? AND version_id=? AND node='user_final_confirmation'",
                (task_id, version_id),
            ).fetchone()[0],
            1,
        )

    def test_model_completed_without_human_review_approval_is_rejected(self) -> None:
        task_id, version_id, _ = self._create_current_final_task(label="unapproved")

        with self.assertRaisesRegex(StateTransitionError, "approved review decision"):
            self._confirm_final(task_id, version_id)

    def test_historical_review_approval_does_not_approve_current_version(self) -> None:
        task_id, current_version_id, old_version_id = self._create_current_final_task(
            label="history",
            old_version_id="history-old-review",
        )
        assert old_version_id is not None
        self._approve_review(task_id, old_version_id)

        with self.assertRaisesRegex(StateTransitionError, "approved review decision"):
            self._confirm_final(task_id, current_version_id)

    def test_review_approval_from_another_task_does_not_cross_task_boundary(self) -> None:
        approved_task_id, approved_version_id, _ = self._create_current_final_task(
            label="approved-task"
        )
        self._approve_review(approved_task_id, approved_version_id)
        other_task_id, other_version_id, _ = self._create_current_final_task(
            label="other-task"
        )

        with self.assertRaisesRegex(StateTransitionError, "approved review decision"):
            self._confirm_final(other_task_id, other_version_id)

    def test_current_version_change_does_not_reuse_old_review_approval(self) -> None:
        task_id, current_version_id, old_version_id = self._create_current_final_task(
            label="version",
            old_version_id="version-old-review",
        )
        assert old_version_id is not None
        self._approve_review(task_id, old_version_id)

        with self.assertRaisesRegex(StateTransitionError, "approved review decision"):
            self._confirm_final(task_id, current_version_id)


if __name__ == "__main__":
    unittest.main()
