"""Readable experience-first Obsidian mirror.

The vault is a browsing surface for confirmed experience, not an export of
internal IDs, crawler traces, model calls or the tag library.  Formal data
remains in Core; Markdown never writes business state back.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)


def _page_name(value: str, *, position: int) -> str:
    text = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]+', "-", value).strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)[:48].strip(" .-") or "领域经验"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"经验-{position:03d}-{text}-{digest}.md"


class ObsidianKnowledgeMirrorService:
    def __init__(self, *, core: Stage0ContentProductionCore):
        self.core = core

    def export(
        self,
        *,
        vault_directory: Path | str,
        actor: str,
        cold_start_id: str | None = None,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise StateTransitionError("knowledge mirror export requires an explicit actor")
        requested = Path(vault_directory).expanduser().resolve()
        if not requested.name:
            raise StateTransitionError("knowledge mirror export requires a concrete local vault directory")
        requested.mkdir(parents=True, exist_ok=True)
        mirror_root = requested / "creation-assistant-mirror"
        staging_root = requested / "creation-assistant-mirror.next"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True)

        experiences = self._accepted_experiences(cold_start_id=cold_start_id)
        relationship_count, record_count = self._write_experience_library(
            staging_root=staging_root,
            experiences=experiences,
        )
        if mirror_root.exists():
            shutil.rmtree(mirror_root)
        staging_root.replace(mirror_root)
        receipt = self.core.record_knowledge_mirror_run(
            cold_start_id=cold_start_id,
            mirror_root=str(mirror_root),
            record_count=record_count,
            relationship_count=relationship_count,
            actor=actor,
        )
        return {
            **receipt,
            "mirror_root": str(mirror_root),
            "table_count": 1,
            "record_count": record_count,
            "relationship_count": relationship_count,
            "bidirectional_relationships": True,
            "experience_first": True,
            "includes_tag_library": False,
            "formal_write_entry": False,
        }

    def _accepted_experiences(
        self, *, cold_start_id: str | None
    ) -> list[dict[str, Any]]:
        # The former generic experience library was retired.  Evidence cards
        # will be introduced only after the user approves their new design.
        del cold_start_id
        return []

    def _write_experience_library(
        self,
        *,
        staging_root: Path,
        experiences: list[dict[str, Any]],
    ) -> tuple[int, int]:
        library_dir = staging_root / "领域经验"
        library_dir.mkdir(exist_ok=True)
        by_domain: dict[str, list[dict[str, Any]]] = {}
        for experience in experiences:
            by_domain.setdefault(experience["domain_label"], []).append(experience)

        domain_links: list[str] = []
        relationship_count = 0
        record_count = 1
        for domain_label, items in by_domain.items():
            domain_file = library_dir / f"{domain_label}.md"
            links: list[str] = []
            for position, item in enumerate(items, start=1):
                filename = _page_name(item["claim"], position=position)
                page = library_dir / filename
                links.append(f"- [[{filename[:-3]}]]")
                page.write_text(
                    "\n".join(
                        [
                            f"# {item['claim']}",
                            "",
                            f"所属领域：[[{domain_label}]]",
                            f"经验类型：{item['classification']}",
                            f"正式接受时间：{item['accepted_at']}",
                            "",
                            "## 可复用经验",
                            "",
                            item["claim"],
                            "",
                            "## 证据说明",
                            "",
                            f"- 本条经验有 {item['evidence_count']} 条正式拆解依据。",
                            "- 详细原始材料保留在系统内，用于必要追溯；不在经验库展示内部编号。",
                            "",
                            "## 所属经验库",
                            "",
                            f"- [[{domain_label}]]",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                relationship_count += 2
                record_count += 1
            domain_file.write_text(
                "\n".join(
                    [
                        f"# {domain_label}领域经验",
                        "",
                        "这里只保留已经正式接受、可复用的经验。标签库、采集记录、模型日志和内部编号不进入这里。",
                        "",
                        "## 已确认经验",
                        "",
                        *(links or ["- 暂无已确认经验"]),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            domain_links.append(f"- [[领域经验/{domain_label}]]")
            record_count += 1
        (staging_root / "index.md").write_text(
            "\n".join(
                [
                    "# 创作经验库",
                    "",
                    "这里是可直接阅读和复用的正式经验库，不是内部运行追溯库。",
                    "",
                    "## 领域经验",
                    "",
                    *(domain_links or ["- 当前还没有已正式接受的领域经验。"]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return relationship_count, record_count
