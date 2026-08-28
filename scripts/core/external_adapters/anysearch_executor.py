"""Formal AnySearch research-material collection adapter."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ANYSEARCH_CLI = PROJECT_ROOT / "vendor" / "AnySearchSkill" / "scripts" / "anysearch_cli.py"
ANYSEARCH_SKILL_VERSION = "3.0.1"
ANYSEARCH_API_KEY_FILE = Path(
    os.environ.get("ANYSEARCH_API_KEY_FILE") or r"I:\api-key.txt"
)
RESULT_BLOCK = re.compile(
    r"^#{2,4}\s+(?!Query\b)(?:\d+[.)]\s*)?(?P<title>.+?)\s*$\n"
    r"(?:-\s*)?(?:\*\*URL\*\*|URL|链接|Link)\s*[:：]\s*(?P<url>https?://\S+)\s*$\n"
    r"(?P<snippet>.*?)(?=^#{2,4}\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
MARKDOWN_LINK = re.compile(
    r"\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)",
    re.IGNORECASE,
)
BARE_URL = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
# A repaired collector must never silently reuse materials retained by the
# earlier incomplete execution.  Old materials remain in Core for provenance,
# but a new formal run starts a clean task-oriented collection batch.
RESEARCH_EXECUTION_VERSION = "gpt_researcher.task_execution.v2"
MIN_SOURCES_PER_TASK = 1
PUBLIC_TASK_TERMS = ("公众", "评价", "记忆", "讨论", "评论", "舆论", "粉丝", "网友", "关注")
FORMAL_BLOCKED_SOURCE_HOSTS = (
    "douyin.com", "tiktok.com", "bilibili.com", "xiaohongshu.com", "kuaishou.com",
)


class AnySearchExecutionError(RuntimeError):
    pass


class AnySearchExecutor:
    """Call the pinned AnySearch skill and retain sources only through Core."""

    def __init__(self, *, core: Stage0ContentProductionCore) -> None:
        self.core = core
        if core.data_identity != "production":
            raise AnySearchExecutionError("AnySearch formal execution requires production data identity")
        if not ANYSEARCH_CLI.is_file():
            raise AnySearchExecutionError("the pinned AnySearch skill is not installed")

    def _call(self, *arguments: str) -> str:
        command = [sys.executable, str(ANYSEARCH_CLI), *arguments]
        environment = os.environ.copy()
        if not str(environment.get("ANYSEARCH_API_KEY") or "").strip():
            try:
                key_text = ANYSEARCH_API_KEY_FILE.read_text(encoding="utf-8-sig").strip()
            except OSError:
                key_text = ""
            if key_text:
                # Accept either a bare key (the current I: document format) or
                # a conventional ANYSEARCH_API_KEY=<key> document without ever
                # placing the credential in the command line or logs.
                for line in key_text.splitlines():
                    candidate = line.strip()
                    if not candidate or candidate.startswith("#"):
                        continue
                    if "=" in candidate:
                        name, value = candidate.split("=", 1)
                        if name.strip() == "ANYSEARCH_API_KEY":
                            candidate = value.strip().strip("\"'")
                    elif ":" in candidate:
                        name, value = candidate.split(":", 1)
                        if name.strip() in {"ANYSEARCH_API_KEY", "api_key", "API_KEY"}:
                            candidate = value.strip().strip("\"'")
                    if candidate:
                        environment["ANYSEARCH_API_KEY"] = candidate
                        break
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
            shell=False,
            env=environment,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "AnySearch returned no error detail").strip()
            raise AnySearchExecutionError(f"AnySearch call failed: {detail[:1000]}")
        return completed.stdout.strip()

    @staticmethod
    def _subject(topic: dict[str, Any]) -> str:
        value = str(topic.get("title") or topic.get("subject") or "").strip()
        if not value:
            raise AnySearchExecutionError("formal topic has no searchable subject")
        return value

    @staticmethod
    def _plan_fingerprint(approved_plan: dict[str, Any]) -> str:
        relevant = {
            key: approved_plan.get(key)
            for key in (
                "research_objective", "research_scope", "research_sequence",
                "research_questions", "source_plan", "required_outputs",
            )
        }
        return hashlib.sha256(
            json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _task_specs(self, *, topic: dict[str, Any], approved_plan: dict[str, Any]) -> list[dict[str, Any]]:
        subject = self._subject(topic)
        questions = [
            str(item).strip()
            for item in (approved_plan.get("research_questions") or [])
            if str(item).strip()
        ]
        sequence = [
            str(item).strip()
            for item in (approved_plan.get("research_sequence") or [])
            if str(item).strip()
        ]
        source_plan = [
            str(item).strip()
            for item in (approved_plan.get("source_plan") or [])
            if str(item).strip()
        ]
        objectives = questions or sequence or ["核查选题对象的基础事实、关键变化和近期公开信息"]
        tasks: list[dict[str, Any]] = []
        for index, objective in enumerate(objectives, start=1):
            sequence_context = sequence[index - 1] if index <= len(sequence) else ""
            source_context = source_plan[index - 1] if index <= len(source_plan) else ""
            task_id = f"research_task_{index:02d}"
            tasks.append({
                "task_id": task_id,
                "task_label": f"研究任务{index}",
                "objective": objective,
                "question": objective if questions else "",
                "sequence_context": sequence_context,
                "source_guidance": source_context,
                "query": f"{subject} {objective}",
            })
        return tasks

    def _task_queries(self, *, subject: str, task: dict[str, Any], social_available: bool) -> list[dict[str, Any]]:
        year = datetime.now(timezone.utc).year
        objective = str(task["objective"]).strip()
        query = str(task["query"]).strip()
        queries: list[dict[str, Any]] = [
            {"query": query, "max_results": 5},
            {"query": f"{query} 官方 正规媒体 采访", "max_results": 5},
        ]
        if any(term in objective for term in ("最新", "近期", "当前", "现在", "近年", "消息", "新闻")):
            queries[0]["query"] = f"{query} 新闻 采访 官方 活动 {year}"
            queries[1]["query"] = f"{query} {year} 正规媒体 最新动态"
        elif any(term in objective for term in ("作品", "专辑", "歌曲", "奖项", "经历", "时间", "出道")):
            queries[1]["query"] = f"{query} 音乐数据库 榜单 奖项 正规媒体"
        elif any(term in objective for term in ("合约", "纠纷", "法律", "低谷", "复出")):
            queries[1]["query"] = f"{query} 新闻 专访 法律 合约 事业变化"
        elif any(term in objective for term in ("行业", "市场", "时代", "女歌手")):
            queries[1]["query"] = f"{query} 行业报告 学术研究 专业媒体"
        if social_available and any(term in objective for term in PUBLIC_TASK_TERMS):
            queries.append({
                "query": f"{subject} {objective} 最新讨论",
                "domain": "social_media",
                "sub_domain": "social_media.social_media",
                "sub_domain_params": {"type": "weibo", "keyword": subject},
                "max_results": 5,
            })
        return queries

    @staticmethod
    def _parse_results(raw: str) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for match in RESULT_BLOCK.finditer(raw):
            url = match.group("url").rstrip(".,;)")
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            results.append(
                {
                    "title": match.group("title").strip(),
                    "source_ref": url,
                    "snippet": match.group("snippet").strip(),
                }
            )
        if not results:
            lines = raw.splitlines()
            current_title = ""
            current_snippet: list[str] = []
            for line in lines:
                stripped = line.strip()
                heading = re.match(r"^#{2,4}\s+(?P<title>.+?)\s*$", stripped)
                url_match = re.search(
                    r"(?:\*\*URL\*\*|URL|链接|Link)\s*[:：]\s*(https?://\S+)",
                    stripped,
                    re.IGNORECASE,
                )
                if heading:
                    current_title = heading.group("title").strip()
                    current_snippet = []
                    continue
                if url_match:
                    url = url_match.group(1).rstrip(".,;)")
                    if urlsplit(url).scheme in {"http", "https"}:
                        results.append({
                            "title": current_title or url,
                            "source_ref": url,
                            "snippet": " ".join(current_snippet).strip(),
                        })
                    current_snippet = []
                    continue
                if stripped and not stripped.startswith("## Query"):
                    current_snippet.append(stripped)

        # Some AnySearch response variants use ordinary Markdown links instead
        # of the documented URL-labelled result blocks.  They are still valid
        # search results and must not make an entire research task look empty.
        if not results:
            for match in MARKDOWN_LINK.finditer(raw):
                url = match.group("url").rstrip(".,;)")
                if urlsplit(url).scheme not in {"http", "https"}:
                    continue
                results.append({
                    "title": match.group("title").strip() or url,
                    "source_ref": url,
                    "snippet": "",
                })

        # Last-resort recovery for plain-text/API wrappers that contain URLs
        # but omit both headings and Markdown link syntax.  The source itself
        # is still traceable; the surrounding line becomes its search snippet.
        if not results:
            for match in BARE_URL.finditer(raw):
                url = match.group(0).rstrip(".,;)")
                if urlsplit(url).scheme not in {"http", "https"}:
                    continue
                line_start = raw.rfind("\n", 0, match.start()) + 1
                line_end = raw.find("\n", match.end())
                if line_end < 0:
                    line_end = len(raw)
                line = raw[line_start:line_end].strip()
                title = re.sub(r"https?://\S+", "", line).strip(" -*#:") or url
                results.append({
                    "title": title[:240],
                    "source_ref": url,
                    "snippet": line[:500],
                })

        if not results:
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None

            def walk(value: Any) -> None:
                if isinstance(value, dict):
                    url = str(
                        value.get("url") or value.get("link") or value.get("source_url") or value.get("href") or ""
                    ).strip()
                    if urlsplit(url).scheme in {"http", "https"}:
                        results.append({
                            "title": str(value.get("title") or value.get("name") or url).strip(),
                            "source_ref": url,
                            "snippet": str(value.get("snippet") or value.get("description") or value.get("content") or "").strip(),
                        })
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
                elif isinstance(value, str) and "http" in value:
                    results.extend(AnySearchExecutor._parse_results(value))

            walk(decoded)

        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in results:
            source_ref = result["source_ref"]
            if source_ref in seen:
                continue
            seen.add(source_ref)
            unique.append(result)
        return unique

    @staticmethod
    def _is_formal_blocked_source(source_ref: str) -> bool:
        host = (urlsplit(source_ref).hostname or "").casefold().removeprefix("www.")
        return any(host == blocked or host.endswith(f".{blocked}") for blocked in FORMAL_BLOCKED_SOURCE_HOSTS)

    @staticmethod
    def _role(task: dict[str, Any]) -> str:
        text = " ".join(str(task.get(key) or "") for key in ("objective", "question", "source_guidance"))
        return "audience_perception" if any(term in text for term in PUBLIC_TASK_TERMS) else "fact_evidence"

    def run(
        self,
        *,
        task_id: str,
        topic: dict[str, Any],
        approved_plan: dict[str, Any],
        user_requirements: str,
    ) -> dict[str, Any]:
        existing = self.core.list_research_materials(task_id=task_id)
        plan_fingerprint = self._plan_fingerprint(approved_plan)
        task_specs = self._task_specs(topic=topic, approved_plan=approved_plan)
        existing_task_counts: dict[str, int] = {}
        for item in existing:
            material = item.get("material") or {}
            task_key = str(material.get("research_task_id") or "")
            if (
                material.get("research_execution") == RESEARCH_EXECUTION_VERSION
                and material.get("research_plan_fingerprint") == plan_fingerprint
                and task_key
            ):
                existing_task_counts[task_key] = existing_task_counts.get(task_key, 0) + 1
        completed_task_ids = {
            task_id for task_id, count in existing_task_counts.items()
            if count >= MIN_SOURCES_PER_TASK
        }
        pending_tasks = [task for task in task_specs if task["task_id"] not in completed_task_ids]
        if not pending_tasks:
            return {
                "task_id": task_id,
                "engine": "AnySearch",
                "skill_version": ANYSEARCH_SKILL_VERSION,
                "framework": "GPT Researcher",
                "source_count": len(existing),
                "task_count": len(task_specs),
                "completed_task_count": len(completed_task_ids),
                "reused": True,
            }
        # AnySearch requires sub-domain discovery before a vertical search.
        domains = self._call("get_sub_domains", "--domains", "social_media")
        social_available = "social_media.social_media" in domains
        collected_at = datetime.now(timezone.utc).isoformat()
        retained = 0
        skipped_blocked_sources = 0
        task_results: list[dict[str, Any]] = []
        seen_by_task: dict[str, set[str]] = {}
        for item in existing:
            material = item.get("material") or {}
            existing_task = str(material.get("research_task_id") or "")
            seen_by_task.setdefault(existing_task, set()).add(str(item.get("source_ref") or ""))
        subject = self._subject(topic)
        for task in pending_tasks:
            query_specs = self._task_queries(subject=subject, task=task, social_available=social_available)
            raw_search = self._call(
                "batch_search",
                "--queries",
                json.dumps(query_specs, ensure_ascii=False),
                "--max_results",
                "5",
            )
            results = self._parse_results(raw_search)
            if not results:
                fallback_results: list[dict[str, str]] = []
                for query_spec in query_specs:
                    fallback_raw = self._call(
                        "search", str(query_spec["query"]), "--max_results", "5"
                    )
                    fallback_results.extend(self._parse_results(fallback_raw))
                results = []
                fallback_seen: set[str] = set()
                for result in fallback_results:
                    if result["source_ref"] in fallback_seen:
                        continue
                    fallback_seen.add(result["source_ref"])
                    results.append(result)
            task_retained = 0
            task_skipped_blocked_sources = 0
            task_seen = seen_by_task.setdefault(task["task_id"], set())
            for result in results:
                source_ref = result["source_ref"]
                if self._is_formal_blocked_source(source_ref):
                    skipped_blocked_sources += 1
                    task_skipped_blocked_sources += 1
                    continue
                if source_ref in task_seen:
                    continue
                task_seen.add(source_ref)
                try:
                    extracted = self._call("extract", source_ref)
                except AnySearchExecutionError:
                    extracted = ""
                material = {
                    "provider": "AnySearch",
                    "subject": subject,
                    "user_requirements": user_requirements,
                    "research_execution": RESEARCH_EXECUTION_VERSION,
                    "research_plan_fingerprint": plan_fingerprint,
                    "research_task_id": task["task_id"],
                    "research_task_label": task["task_label"],
                    "research_task_objective": task["objective"],
                    "research_task_question": task["question"],
                    "research_sequence_context": task["sequence_context"],
                    "research_source_guidance": task["source_guidance"],
                    "search_queries": query_specs,
                    "search_result": result["snippet"],
                    "extracted_content": extracted,
                    "retrieved_at": collected_at,
                }
                try:
                    self.core.record_research_material(
                        task_id=task_id,
                        source_ref=source_ref,
                        title=result["title"],
                        evidence_role=self._role(task),
                        material=material,
                        collected_at=collected_at,
                    )
                except sqlite3.IntegrityError as exc:
                    # A previous interrupted run may already have committed
                    # this exact task/source pair.  Core keeps the first
                    # immutable record; treating this as already retained
                    # makes formal reruns idempotent instead of failing the
                    # whole research batch on a duplicate.
                    if "stage0_research_material.task_id, stage0_research_material.source_ref" not in str(exc):
                        raise
                    continue
                retained += 1
                task_retained += 1
            task_results.append({
                "research_task_id": task["task_id"],
                "task_label": task["task_label"],
                "objective": task["objective"],
                "queries": query_specs,
                "source_count": task_retained,
                "skipped_blocked_sources": task_skipped_blocked_sources,
            })

        final_task_counts = dict(existing_task_counts)
        for result in task_results:
            final_task_counts[result["research_task_id"]] = (
                final_task_counts.get(result["research_task_id"], 0) + int(result["source_count"])
            )
        missing_tasks = [
            task["task_id"] for task in task_specs
            if final_task_counts.get(task["task_id"], 0) < MIN_SOURCES_PER_TASK
        ]
        if missing_tasks:
            raise AnySearchExecutionError(
                "AnySearch did not retain at least one source for research tasks: "
                + ", ".join(missing_tasks)
            )
        return {
            "task_id": task_id,
            "engine": "AnySearch",
            "framework": "GPT Researcher",
            "source_count": retained,
            "task_count": len(task_specs),
            "completed_task_count": len(completed_task_ids) + len(task_results),
            "covered_task_count": len(task_specs) - len(missing_tasks),
            "skipped_blocked_sources": skipped_blocked_sources,
            "reused": bool(existing),
            "tasks": task_results,
        }
