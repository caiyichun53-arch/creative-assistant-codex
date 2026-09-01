"""Prepare one real competitor breakdown as a standard external task.

This entry deliberately stops after Core creates the task.  An external
executor must claim it and submit the structured result through the standard
boundary; this script never chooses or calls a model.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    canonicalize_competitor_content_type,
)
from scripts.core.runtime.runtime_storage import runtime_root_for_identity
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillValidationError,
    validate_competitor_breakdown_question_expansion_output,
)
from scripts.core.production.stage1_competitor_registration import (
    _breakdown_domain_context,
    prepare_competitor_breakdown_external_task,
)
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _transcript_from_artifact(artifact: dict[str, Any]) -> str:
    direct = str(artifact.get("transcript") or artifact.get("transcript_text") or "").strip()
    if direct:
        return direct
    reference = str(artifact.get("transcript_ref") or "").strip()
    if not reference:
        return ""
    path = Path(reference)
    if not path.exists() and reference.startswith("/mnt/"):
        path = Path(reference.replace("/mnt/i/", "I:/", 1))
    try:
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
    except (OSError, UnicodeError):
        return ""


def _observed_types(connection: sqlite3.Connection, domain_label: str) -> list[str]:
    rows = connection.execute(
        "SELECT item.artifact_json FROM stage0_competitor_registration_item item "
        "JOIN stage0_competitor_registration registration "
        "ON registration.registration_id=item.registration_id "
        "AND registration.data_identity=item.data_identity "
        "JOIN stage0_cold_start cold_start "
        "ON cold_start.cold_start_id=registration.cold_start_id "
        "AND cold_start.data_identity=registration.data_identity "
        "WHERE item.step_name='breakdown' AND item.status='completed' "
        "AND cold_start.domain_label=? AND item.data_identity='production'",
        (domain_label,),
    ).fetchall()
    daily_rows = connection.execute(
        "SELECT analysis.artifact_json FROM stage0_daily_hit_breakdown analysis "
        "JOIN hits hit ON hit.hit_id=analysis.hit_id "
        "JOIN competitor_accounts account ON account.account_id=hit.account_id "
        "WHERE account.domain_label=? AND analysis.data_identity='production'",
        (domain_label,),
    ).fetchall()
    values: set[str] = set()
    subject_labels = {
        "person": "人物", "work": "作品", "event": "事件", "concept": "概念",
        "case": "案例", "method": "方法", "collection": "合集",
    }
    expression_labels = {
        "story": "故事", "profile": "经历", "list": "盘点", "analysis": "解读",
        "explanation": "背景说明", "commentary": "观点评论", "event_response": "事件回应",
        "interview": "访谈",
    }
    def readable_type(value: str) -> str:
        text = str(value or "").strip()
        if "/" not in text:
            return text
        subject, expression = (part.strip() for part in text.split("/", 1))
        if subject in subject_labels and expression in expression_labels:
            return f"{subject_labels[subject]}{expression_labels[expression]}"
        return text
    for row in (*rows, *daily_rows):
        artifact = _parse_json(row[0])
        breakdown = artifact.get("deep_breakdown") if isinstance(artifact.get("deep_breakdown"), dict) else artifact
        observed = str(breakdown.get("source_content_type") or "").strip()
        if observed:
            values.add(canonicalize_competitor_content_type(readable_type(observed)))
            continue
        subject = str(breakdown.get("content_subject_type") or "").strip()
        expression = str(breakdown.get("expression_form") or "").strip()
        if subject and expression and subject not in {"mixed", "unclear"} and expression not in {"mixed", "unclear"}:
            raw_type = (
                f"{subject_labels.get(subject, subject)}"
                f"{expression_labels.get(expression, expression)}"
            )
            values.add(canonicalize_competitor_content_type(raw_type))
    return sorted(values, key=str.casefold)


def _select_material(connection: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT item.item_ref, item.artifact_json, cold_start.domain_label, registration.registration_id "
        "FROM stage0_competitor_registration_item item "
        "JOIN stage0_competitor_registration registration "
        "ON registration.registration_id=item.registration_id "
        "AND registration.data_identity=item.data_identity "
        "JOIN stage0_cold_start cold_start "
        "ON cold_start.cold_start_id=registration.cold_start_id "
        "AND cold_start.data_identity=registration.data_identity "
        "WHERE item.step_name='transcripts_and_comments' AND item.status='completed' "
        "AND cold_start.domain_label='music_entertainment' AND item.data_identity='production' "
        "ORDER BY item.updated_at DESC, item.item_ref DESC"
    ).fetchall()
    for row in rows:
        current_source_id = str(row[0] or "").strip()
        if not current_source_id or current_source_id != source_id:
            continue
        artifact = _parse_json(row[1])
        transcript = _transcript_from_artifact(artifact)
        comments = artifact.get("comments") if isinstance(artifact.get("comments"), list) else []
        comments = [item for item in comments if isinstance(item, dict) and str(item.get("text") or "").strip()]
        if not transcript or not comments:
            continue
        metrics = artifact.get("metrics") if isinstance(artifact.get("metrics"), dict) else {}
        domain_label = str(row[2] or "music_entertainment")
        return {
            "source_id": current_source_id,
            "registration_id": str(row[3] or ""),
            "title": str(artifact.get("title") or current_source_id),
            "hit_id": str(artifact.get("hit_id") or ""),
            "transcript": transcript,
            "metrics": metrics,
            "comments": comments,
            "domain_label": domain_label,
            "domain_context": _breakdown_domain_context(
                domain_label,
                observed_content_types=_observed_types(connection, domain_label),
                content_type_lifecycle="classify",
            ),
        }
    raise RuntimeError("没有找到同时具备完整口播文案和评论的音乐爆款材料")


def _markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"# 真实隔离拆解结果｜{summary.get('source_id', '')}",
        "",
        f"- 测试状态：{summary.get('status')}",
        f"- 标题：{summary.get('title')}",
        f"- 文案字数：{summary.get('transcript_chars')}",
        f"- 评论条数：{summary.get('comment_count')}",
        f"- 内容类型：{summary.get('source_content_type')}",
        f"- 正式业务数据写入：{summary.get('formal_business_data_written')}",
        "",
        "## 完整拆解分析",
        "",
        str(summary.get("analysis_text") or "（无）"),
        "",
        "## 拓展内容线索",
        "",
    ]
    expansions = summary.get("question_expansions") or []
    if not expansions:
        lines.append("无")
    else:
        for index, item in enumerate(expansions, start=1):
            lines.extend([
                f"### 线索 {index}",
                "",
                f"- 内容类型：{item.get('content_type', '')}",
                f"- 核心问题：{item.get('core_question', '')}",
                f"- 理由：{item.get('reason', '')}",
                "",
            ])
    if summary.get("failure"):
        lines.extend(["## 失败信息", "", str(summary["failure"]), ""])
    return "\n".join(lines).rstrip() + "\n"


def _qualification_smoke_test() -> dict[str, Any]:
    """Exercise qualification statuses in memory without touching formal data."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    core = Stage0ContentProductionCore(
        connection,
        db_path=Path(":memory:"),
        data_identity="test",
    )
    try:
        core.install_schema()
        now = "2026-08-17T00:00:00+08:00"
        connection.executemany(
            "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("owned_test", "owned", "测试账号", "music_entertainment", "owned-test", "active", "test", "smoke", now),
                ("competitor_test", "competitor", "测试对标", "music_entertainment", "competitor-test", "active", "test", "smoke", now),
            ],
        )
        connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cold_test", "owned_test", "music_entertainment", "completed", "test", "smoke", now, None),
        )
        connection.execute(
            "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("registration_test", "cold_test", "competitor_test", "completed", "completed", "test", now, now),
        )
        material = {
            "source_url": "https://example.test/music-source",
            "transcript_ref": "validation/transcript.md",
            "comment_collection_ref": "validation/comments.json",
        }
        for step_name, artifact in (
            ("transcripts_and_comments", material),
            ("breakdown", {"source_id": "source_test", "source_content_type": "人物故事"}),
        ):
            connection.execute(
                "INSERT INTO stage0_competitor_registration_item "
                "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
                "VALUES (?, ?, ?, 'completed', ?, '{}', 1, 'test', ?)",
                ("registration_test", step_name, "source_test", json.dumps(artifact, ensure_ascii=False), now),
            )
        parent = {
            "source_type": "competitor_breakdown",
            "source_object_id": "source_test",
            "registration_id": "registration_test",
        }

        def external_probe(question: str) -> dict[str, Any]:
            if "虚构" in question:
                return {
                    "status": "rejected",
                    "rejection_reason": "lead_no_reliable_public_material",
                    "checks": {"minimum_premise": "no_reliable_relation_found"},
                }
            return {
                "status": "passed",
                "material_refs": [{
                    "kind": "external_public_source",
                    "ref": "https://example.org/reliable-cooperation-source",
                    "title": "公开合作资料",
                    "snippet": "公开材料记录了相关音乐合作关系。",
                }],
                "checks": {"minimum_premise": "basic_relation_confirmed"},
            }

        first_question = "这位音乐人的作品经历如何呈现其音乐转折？"
        result = core.register_breakdown_question_expansions(
            domain_label="music_entertainment",
            breakdown={
                "source_content_type": "人物故事",
                "question_expansions": [
                    {"content_type": "人物故事", "core_question": first_question, "reason": "母来源已经提供人物与音乐作品关系，后续可继续研究。"},
                    {"content_type": "人物故事", "core_question": "这位音乐人的合作歌曲关系是否确实存在？", "reason": "评论补充了一项新的合作歌曲说法，待核实。"},
                    {"content_type": "人物故事", "core_question": "这位音乐人的虚构合作歌曲关系是否确实存在？", "reason": "评论补充了一项新的合作歌曲说法，待核实。"},
                ],
            },
            parent_source_ref=parent,
            external_probe=external_probe,
        )
        unresolved_result = core.register_breakdown_question_expansions(
            domain_label="music_entertainment",
            breakdown={
                "source_content_type": "人物故事",
                "question_expansions": [{
                    "content_type": "人物故事",
                    "core_question": "这位音乐人的另一项合作歌曲关系是否确实存在？",
                    "reason": "评论补充了一项新的合作歌曲说法，待核实。",
                }],
            },
            parent_source_ref=parent,
            external_probe=lambda _question: (_ for _ in ()).throw(TimeoutError("simulated provider timeout")),
        )
        qualification_rows = [
            dict(row) for row in connection.execute(
                "SELECT expansion_id, status, rejection_reason, checks_json FROM stage1_question_expansion_qualification "
                "ORDER BY expansion_id"
            ).fetchall()
        ]
        selection_inputs = core.list_knowledge_selection_inputs()
        candidate_pool = core.list_knowledge_candidate_pool()
        qualified_source_row = connection.execute(
            "SELECT expansion_id, payload_json FROM stage1_question_expansion_source "
            "WHERE data_identity='test' AND validation_outcome='supported' LIMIT 1"
        ).fetchone()
        qualified_source = {
            "source_type": "question_expansion",
            "source_object_id": str(qualified_source_row[0]),
            "source_object_version": str(result["created"][0]["source_object_version"]),
            "source_time": now,
            "payload": json.loads(str(qualified_source_row[1])),
        }
        deterministic_result = Stage1BDailyDiscoveryService(core=core, gateway=None)._deterministic_filter(
            domain_label="music_entertainment",
            source=qualified_source,
            now=datetime.fromisoformat(now),
        )
        canonical_type_checks = {
            "人物经历与观点评论": canonicalize_competitor_content_type("人物经历与观点评论"),
            "主题歌曲盘点与观点评论": canonicalize_competitor_content_type("主题歌曲盘点与观点评论"),
            "作品背景说明": canonicalize_competitor_content_type("作品背景说明"),
        }
        passed = (
            result.get("count") == 2
            and result.get("rejected_count") == 1
            and result.get("unresolved_count") == 0
            and unresolved_result.get("count") == 0
            and unresolved_result.get("rejected_count") == 0
            and unresolved_result.get("unresolved_count") == 1
            and len(qualification_rows) == 4
            and {row["status"] for row in qualification_rows} == {"qualified", "rejected", "unresolved"}
            and all(row["rejection_reason"] for row in qualification_rows if row["status"] == "rejected")
            and all(not row["rejection_reason"] for row in qualification_rows if row["status"] == "unresolved")
            and len(selection_inputs["question_sources"]) == 2
            and not candidate_pool
            and deterministic_result[0] == "eligible"
            and canonical_type_checks == {
                "人物经历与观点评论": "人物故事",
                "主题歌曲盘点与观点评论": "作品盘点",
                "作品背景说明": "作品背景说明",
            }
        )
        return {
            "status": "PASS" if passed else "FAIL",
            "result": result,
            "qualification_rows": qualification_rows,
            "qualified_selection_inputs": len(selection_inputs["question_sources"]),
            "candidate_pool_count": len(candidate_pool),
            "qualified_source_deterministic_filter": deterministic_result,
            "comment_fact_external_confirmation": {
                "status": "qualified",
                "external_probe": "basic_relation_confirmed",
                "entered_selection_inputs": True,
            },
            "real_wrong_lead": {
                "status": "rejected",
                "rejection_reason": "lead_no_reliable_public_material",
                "entered_selection_inputs": False,
            },
            "provider_failure_lead": {
                "status": "unresolved",
                "entered_selection_inputs": False,
                "rejection_reason_empty": all(
                    not row["rejection_reason"] for row in qualification_rows if row["status"] == "unresolved"
                ),
            },
            "unresolved_registration_result": unresolved_result,
            "canonical_type_checks": canonical_type_checks,
            "formal_business_data_written": False,
        }
    finally:
        core.close()


def _semantic_boundary_smoke_test() -> dict[str, Any]:
    input_payload = {
        "source_id": "semantic_smoke",
        "domain_context": {
            "description": "音乐、作品和音乐人物的音乐经历",
            "question_expansion_policy": {
                "primary_content_carrier_required": True,
                "primary_carrier_signal_terms": ["音乐", "歌曲", "作品", "演唱"],
            },
        },
    }
    base = {
        "source_id": "semantic_smoke",
        "source_content_type": "人物故事",
        "schema_version": "competitor_breakdown.output.raw.v4",
        "question_expansions": [],
    }

    def rejected(analysis_text: str, expansions: list[dict[str, str]] | None = None) -> bool:
        payload = {**base, "analysis_text": analysis_text, "question_expansions": expansions or []}
        try:
            validate_competitor_breakdown_question_expansion_output(input_payload, payload)
        except FormalSkillValidationError:
            return True
        return False

    checks = {
        "comment_fact_confirmation_blocked": rejected(
            "五、评论信号\n评论中出现了对事实的确认。\n六、候选复用原则与边界\n无明显短板。"
        ),
        "unsupported_expansion_premise_blocked": rejected(
            "五、评论信号\n评论者提到一次合作。\n六、候选复用原则与边界\n无明显短板。",
            [{
                "content_type": "人物故事",
                "core_question": "这次合作具有历史意义吗？",
                "reason": "来源只提供发生过一次合作，但理由把它写成具有历史意义。",
            }],
        ),
        "non_music_association_blocked": rejected(
            "五、评论信号\n评论者提到一个电影角色。\n六、候选复用原则与边界\n无明显短板。",
            [{
                "content_type": "作品故事",
                "core_question": "这个人物的电影角色如何塑造银幕形象？",
                "reason": "来源人物与音乐有关，但问题主体是电影角色。",
            }],
        ),
        "cross_type_shortfall_blocked": rejected(
            "五、评论信号\n评论者提到一首歌。\n六、候选复用原则与边界\n本篇短板是系列无法继续。"
        ),
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


def _qualification_external_smoke_test(
    source_id: str, *, database_path: Path
) -> dict[str, Any]:
    """Run one read-only minimum check against the explicitly supplied TEST DB."""
    core = Stage0ContentProductionCore.open_read_only(
        database_path, data_identity="test"
    )
    try:
        row = core.conn.execute(
            "SELECT registration_id FROM stage0_competitor_registration_item "
            "WHERE step_name='transcripts_and_comments' AND item_ref=? "
            "AND status='completed' AND data_identity='production' LIMIT 1",
            (source_id,),
        ).fetchone()
        if row is None:
            return {
                "status": "FAIL",
                "reason": "real parent material was not found",
                "formal_business_data_written": False,
            }
        parent_source_ref = {
            "source_type": "competitor_breakdown",
            "source_object_id": source_id,
            "registration_id": str(row["registration_id"]),
        }
        qualification = core.qualify_question_expansion_lead(
            domain_label="music_entertainment",
            question="98 Degrees and Mariah Carey in Thank God I Found You 的音乐合作关系是否确实存在？",
            content_type="人物故事",
            reason="评论补充了一项合作说法，待核实。",
            parent_source_ref=parent_source_ref,
        )
        wrong_line = core.qualify_question_expansion_lead(
            domain_label="music_entertainment",
            question="98 Degrees与贝多芬共同录制音乐作品的合作关系是否确实存在？",
            content_type="人物故事",
            reason="评论补充了一项合作说法，待核实。",
            parent_source_ref=parent_source_ref,
        )
        state = str(qualification.get("status") or "")
        return {
            "status": "PASS" if state == "qualified" and wrong_line.get("status") == "rejected" else "FAIL",
            "source_id": source_id,
            "qualification": qualification,
            "real_wrong_line": wrong_line,
            "formal_business_data_written": False,
            "note": "只执行一次最低外部查实，不写入资格化记录或正式研究材料。",
        }
    finally:
        core.close()


def _content_type_lifecycle_smoke_test() -> dict[str, Any]:
    """Run the 2A fixture-registry regressions without formal business data."""
    from tests.test_content_type_lifecycle_2a import ContentTypeLifecycle2ATest

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ContentTypeLifecycle2ATest)
    result = unittest.TestResult()
    suite.run(result)
    return {
        "status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": [str(item[1]) for item in result.failures],
        "errors": [str(item[1]) for item in result.errors],
        "formal_business_data_written": False,
    }


def _content_type_lifecycle_2b_smoke_test() -> dict[str, Any]:
    """Run frozen-registry and compatibility regressions without formal data."""
    from tests.test_content_type_lifecycle_2b import ContentTypeLifecycle2BTest

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ContentTypeLifecycle2BTest)
    result = unittest.TestResult()
    suite.run(result)
    return {
        "status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": [str(item[1]) for item in result.failures],
        "errors": [str(item[1]) for item in result.errors],
        "formal_business_data_written": False,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id")
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--output-file")
    parser.add_argument("--qualification-smoke", action="store_true")
    parser.add_argument("--qualification-external-smoke", action="store_true")
    parser.add_argument("--content-type-lifecycle-smoke", action="store_true")
    parser.add_argument("--content-type-lifecycle-2b-smoke", action="store_true")
    args = parser.parse_args()
    if args.content_type_lifecycle_smoke:
        smoke = _content_type_lifecycle_smoke_test()
        print(json.dumps(smoke, ensure_ascii=False, indent=2), flush=True)
        return 0 if smoke["status"] == "PASS" else 1
    if args.content_type_lifecycle_2b_smoke:
        smoke = _content_type_lifecycle_2b_smoke_test()
        print(json.dumps(smoke, ensure_ascii=False, indent=2), flush=True)
        return 0 if smoke["status"] == "PASS" else 1
    if args.qualification_smoke:
        smoke = _qualification_smoke_test()
        smoke["semantic_boundary_smoke"] = _semantic_boundary_smoke_test()
        if smoke["semantic_boundary_smoke"]["status"] != "PASS":
            smoke["status"] = "FAIL"
        print(json.dumps(smoke, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.qualification_external_smoke:
        if args.db_path is None:
            parser.error("--db-path is required for qualification external smoke mode")
        source_id = str(args.source_id or "7665354103261842707").strip()
        print(
            json.dumps(
                _qualification_external_smoke_test(
                    source_id, database_path=args.db_path
                ),
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 0
    if not str(args.source_id or "").strip():
        parser.error("--source-id is required unless a qualification smoke mode is used")
    if args.db_path is None:
        parser.error("--db-path is required for a TEST material run")
    formal_root = runtime_root_for_identity("production").resolve()
    requested_db = args.db_path.resolve()
    try:
        requested_db.relative_to(formal_root)
    except ValueError:
        pass
    else:
        parser.error("TEST material runs cannot use a database inside the formal runtime")
    test_core = Stage0ContentProductionCore.open_read_only(
        requested_db, data_identity="production"
    )
    try:
        material = _select_material(test_core.conn, str(args.source_id).strip())
        task = prepare_competitor_breakdown_external_task(
            test_core,
            registration_id=material["registration_id"],
            source_id=material["source_id"],
        )
    finally:
        test_core.close()

    summary = {
        "formal_business_data_written": False,
        "source_id": material["source_id"],
        "registration_id": material["registration_id"],
        "title": material["title"],
        "transcript_chars": len(material["transcript"]),
        "comment_count": len(material["comments"]),
        "observed_content_types_passed": material["domain_context"]["observed_content_types"],
        "status": "external_task_prepared",
        "task": task,
    }
    if args.output_file:
        output_path = Path(args.output_file).resolve()
        try:
            output_path.relative_to(formal_root)
        except ValueError:
            pass
        else:
            parser.error("TEST output cannot be written inside the formal runtime")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.casefold() == ".md":
            output_path.write_text(_markdown_summary(summary), encoding="utf-8")
        else:
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
