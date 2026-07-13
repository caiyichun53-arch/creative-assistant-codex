"""接线 goal09_experiments.py(B1, 2026-07-13, 置顶规则总表核对后, BR-EXPERIENCE-004,
原文档"7 推荐状态状态机" + "29 自营实验与P基线")。

这个模块之前(A2)只建了"把真实发布事实存进去"这一层(own_publications.py);
"用发布数据真正评估一条候选方法、推动它在 active/watch/paused 之间流转"这一层
逻辑虽然早就写好(goal09_experiments.py 的 ExperimentMaterializer/
recompute_experience_state),但全仓库搜索确认过从没被真实调用过——这个文件是
第一次真的把两边接起来。

真实评估流程(evaluate_and_record_experiment):
1. 读一条已经用 own_publications.record_own_experiment() 冻结好判断规则的
   own_experiments 行(success_rule/failure_rule 必须在看结果前就定好,这是
   读取时就已经满足的前提,不是这个函数负责的事)。
2. 找它的 Day7 真实快照(own_publication_checks.day_since_publish=7)——没有
   就不能评估,不能提前算,也不能拿其它天数的数据顶替。
3. 找它所属账号在 primary_metric 上最新的基线(own_publication_baselines)——
   sample_count=0 也不能评估(没有基线就没有比较对象)。
4. actual_use_status 从 own_publications.actually_used_tactic_id 是否等于
   own_experiments.primary_hypothesis_tactic_id 真实推导,不是猜的。
5. 调 compute_metric_signal() 拿三态信号,ExperimentMaterializer.
   record_experiment_result() 落一条真实的、内容寻址、幂等的实验结果记录。
6. 结果写回 own_experiments.result/result_computed_at。
7. 如果这条方法已经有 tactic_state 行且不在 candidate(candidate 的晋升不归
   这个函数管),重新拉这条方法名下全部已评估实验(result 不是 NULL 的),组装
   成 ExperienceEvidence 证据流(sequence 用 published_at 真实时间戳换算出的
   unix 秒数,不是猜的顺序;core_question 直接来自 own_experiments.core_question,
   人工登记时就写好的),调 recompute_experience_state();状态真的变了才调
   Goal02StateStore.transition_state() 落一次真实的状态转移,没变就不写。

Usage (as a library, no CLI yet -- matches this session其它绑定脚本的既有节奏):
    from scripts.core.business_data.run_publication_experiment import evaluate_and_record_experiment
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.experience.goal09_experiments import (  # noqa: E402
    ExperienceEvidence,
    ExperienceStateInput,
    ExperimentMaterializer,
    ExperimentResultCommand,
    PPlusMetricInput,
    recompute_experience_state,
)
from scripts.core.persistence.goal01_store import PersistenceStore, content_hash  # noqa: E402
from scripts.core.persistence.goal02_store import Goal02StateStore  # noqa: E402
from scripts.core.production.goal08_production_chain import VersionRef  # noqa: E402

_METRIC_COLUMNS = frozenset({"like_count", "comment_count", "collect_count", "share_count"})


def validate_publication_experiment_execution_contract() -> dict[str, Any]:
    return require_baseline_citations(["9", "18"])


class PublicationExperimentError(RuntimeError):
    pass


def _load_experiment(conn: sqlite3.Connection, experiment_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM own_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
    if row is None:
        raise PublicationExperimentError(f"own_experiments {experiment_id!r} does not exist")
    return row


def _load_publication(conn: sqlite3.Connection, publication_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM own_publications WHERE publication_id=?", (publication_id,)).fetchone()
    if row is None:
        raise PublicationExperimentError(f"own_publications {publication_id!r} does not exist")
    return row


def _load_day7_check(conn: sqlite3.Connection, publication_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM own_publication_checks WHERE publication_id=? AND day_since_publish=7",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise PublicationExperimentError(
            f"no Day7 check recorded for publication {publication_id!r} -- cannot evaluate before the real 7-day mark"
        )
    return row


def _load_latest_baseline(conn: sqlite3.Connection, account_id: str, metric: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM own_publication_baselines
         WHERE account_id=? AND metric=?
         ORDER BY computed_at DESC LIMIT 1
        """,
        (account_id, metric),
    ).fetchone()
    if row is None or row["sample_count"] == 0:
        raise PublicationExperimentError(f"no baseline yet for account {account_id!r} metric {metric!r}")
    return row


def _actual_use_status(publication: sqlite3.Row, primary_hypothesis_tactic_id: str) -> str:
    actually_used = publication["actually_used_tactic_id"]
    if actually_used is None:
        return "unknown"
    return "used" if actually_used == primary_hypothesis_tactic_id else "not_used"


def evaluate_and_record_experiment(
    conn: sqlite3.Connection,
    store: PersistenceStore,
    *,
    experiment_id: str,
    actor: str,
    idempotency_key: str,
    support_ratio: str = "1.5",
    not_supported_ratio: str = "0.8",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    validate_publication_experiment_execution_contract()

    experiment = _load_experiment(conn, experiment_id)
    if experiment["result"] is not None:
        return {
            "experiment_id": experiment_id,
            "already_evaluated": True,
            "result": experiment["result"],
            "recommendation_status_change": None,
        }

    publication = _load_publication(conn, experiment["publication_id"])
    day7_check = _load_day7_check(conn, experiment["publication_id"])
    baseline = _load_latest_baseline(conn, publication["account_id"], experiment["primary_metric"])
    observed_value = day7_check[experiment["primary_metric"]]
    if observed_value is None:
        raise PublicationExperimentError(
            f"Day7 check for publication {publication['publication_id']!r} has no {experiment['primary_metric']} value"
        )

    tactic_id = experiment["primary_hypothesis_tactic_id"]
    actual_use_status = _actual_use_status(publication, tactic_id)
    confounders = _json_list(experiment["confounders"])

    tactic_root = conn.execute("SELECT * FROM trace_root WHERE root_id=?", (tactic_id,)).fetchone()
    if tactic_root is None:
        raise PublicationExperimentError(f"tactic {tactic_id!r} is not a real trace_root")

    command = ExperimentResultCommand(
        account_id=publication["account_id"],
        topic_id=tactic_id,
        actor=actor,
        idempotency_key=idempotency_key,
        experiment_kind="formal",
        primary_hypothesis_ref=VersionRef(
            relation_role="primary_hypothesis",
            target_object_kind="tactic",
            target_stable_id=tactic_id,
            target_version_id=tactic_root["current_version_id"],
            target_content_hash=None,
            locator={"table": "trace_root", "root_id": tactic_id},
        ),
        publication_capture_ref=VersionRef(
            relation_role="publication_capture",
            target_object_kind="own_publication_check",
            target_stable_id=day7_check["check_id"],
            target_version_id=None,
            target_content_hash=_content_hash_of_check(day7_check),
            locator={"table": "own_publication_checks", "check_id": day7_check["check_id"]},
        ),
        metric=PPlusMetricInput(
            metric_name=experiment["primary_metric"],
            baseline_value=baseline["median_value"],
            observed_value=observed_value,
            support_ratio=support_ratio,
            not_supported_ratio=not_supported_ratio,
        ),
        primary_hypothesis_frozen=True,
        actual_use_status=actual_use_status,
        major_confounder=bool(confounders),
        correlation_id=correlation_id,
    )

    with conn:
        result = ExperimentMaterializer(store).record_experiment_result(command)

    signal = result.metric_signal.signal
    computed_at = datetime.now(timezone.utc).isoformat()
    if signal in {"supported", "not_supported", "inconclusive"}:
        conn.execute(
            "UPDATE own_experiments SET result=?, result_computed_at=? WHERE experiment_id=?",
            (signal, computed_at, experiment_id),
        )
        conn.commit()

    status_change = None
    if signal in {"supported", "not_supported"}:
        status_change = _recompute_and_transition_tactic_state(
            conn, store, tactic_id=tactic_id, actor=actor, idempotency_key=f"{idempotency_key}.state"
        )

    return {
        "experiment_id": experiment_id,
        "already_evaluated": False,
        "metric_signal": result.metric_signal.as_payload(),
        "result": signal if signal in {"supported", "not_supported", "inconclusive"} else None,
        "recommendation_status_change": status_change,
    }


def _recompute_and_transition_tactic_state(
    conn: sqlite3.Connection,
    store: PersistenceStore,
    *,
    tactic_id: str,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    tactic_state = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
    if tactic_state is None or tactic_state["state"] == "candidate":
        return None

    evidence = _load_tactic_formal_evidence(conn, tactic_id)
    state_result = recompute_experience_state(
        ExperienceStateInput(
            tactic_key=tactic_id,
            current_recommendation_status=tactic_state["state"],
            evidence=evidence,
        )
    )
    if state_result.recommendation_status == tactic_state["state"]:
        return None

    goal02 = Goal02StateStore(store)
    with conn:
        transition = goal02.transition_state(
            object_kind="tactic",
            object_id=tactic_id,
            new_state=state_result.recommendation_status,
            basis_version_id=conn.execute(
                "SELECT current_version_id FROM trace_root WHERE root_id=?", (tactic_id,)
            ).fetchone()["current_version_id"],
            actor=actor,
            idempotency_key=idempotency_key,
            expected_row_revision=int(tactic_state["row_revision"]),
        )
    return {
        "from_state": tactic_state["state"],
        "to_state": transition.state,
        "reason": state_result.audit_reasons[0] if state_result.audit_reasons else "",
    }


def _load_tactic_formal_evidence(conn: sqlite3.Connection, tactic_id: str) -> tuple[ExperienceEvidence, ...]:
    rows = conn.execute(
        """
        SELECT own_experiments.experiment_id AS experiment_id,
               own_experiments.core_question AS core_question,
               own_experiments.result AS result,
               own_publications.published_at AS published_at,
               own_publications.actually_used_tactic_id AS actually_used_tactic_id
          FROM own_experiments
          JOIN own_publications ON own_publications.publication_id = own_experiments.publication_id
         WHERE own_experiments.primary_hypothesis_tactic_id = ?
           AND own_experiments.result IN ('supported', 'not_supported')
         ORDER BY own_publications.published_at ASC
        """,
        (tactic_id,),
    ).fetchall()
    evidence = []
    for row in rows:
        primary_used = row["actually_used_tactic_id"] == tactic_id
        evidence.append(
            ExperienceEvidence(
                evidence_id=row["experiment_id"],
                evidence_kind="formal_p_result",
                independence_key=row["experiment_id"],
                sequence=int(datetime.fromisoformat(row["published_at"]).timestamp()),
                eligible=True,
                metric_signal=row["result"],
                primary_used=primary_used,
                core_question_hash=row["core_question"],
            )
        )
    return tuple(evidence)


def _json_list(raw: str) -> list[Any]:
    return json.loads(raw) if raw else []


def _content_hash_of_check(check_row: sqlite3.Row) -> str:
    payload = {
        "check_id": check_row["check_id"],
        "publication_id": check_row["publication_id"],
        "day_since_publish": check_row["day_since_publish"],
        "like_count": check_row["like_count"],
        "comment_count": check_row["comment_count"],
        "share_count": check_row["share_count"],
        "collect_count": check_row["collect_count"],
    }
    return content_hash(payload, "own_publication_check.v1")
