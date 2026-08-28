"""Readable self-media content creation knowledge mirror.

The vault is a browsing surface for project foundation, source material,
processing, topics, delivery, feedback and confirmed experience.  Formal
data remains in Core; Markdown never writes business state back.
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
from scripts.core.business_data.domain_labels import configured_domain_packs, get_domain_pack

KNOWLEDGE_BASE_NAME = "自媒体内容创作知识库"
OBSIDIAN_MIRROR_ROOT = f"创作助手/经验库/{KNOWLEDGE_BASE_NAME}"
OBSIDIAN_EXPERIENCE_ROOT = OBSIDIAN_MIRROR_ROOT

# Obsidian sorts names alphabetically.  The numeric prefixes make the
# creation flow stable and keep the visible names understandable.
FOLDER_NAMES = {
    "首页": "00 导航",
    "导航": "00 导航",
    "项目基础": "01 账号与领域",
    "账号与领域": "01 账号与领域",
    "原始素材": "02 素材",
    "素材": "02 素材",
    "整理材料": "02 素材",
    "内容整理": "02 素材",
    "选题研究": "03 选题",
    "选题": "03 选题",
    "文稿与音频": "04 自营内容",
    "自营内容": "04 自营内容",
    "发布与复盘": "发布与复盘",
    "风格与偏好": "05 风格与偏好",
    "经验": "06 经验",
    "可复用经验": "06 经验",
    "系统记录": "07 系统记录",
    "归档": "08 归档",
    "来源与基础信息": "01 来源与数据",
    "来源与数据": "01 来源与数据",
    "文案与评论": "02 文案与评论",
    "用户提供": "04 用户提供",
    "采集状态": "01 运行状态",
    "内容拆解": "03 内容拆解",
    "参考案例": "02 参考案例",
    "依据材料": "依据材料",
    "研究材料": "03 研究材料",
    "选题输入": "01 选题输入",
    "候选选题": "02 候选选题",
    "正式选题": "03 正式选题",
    "研究材料": "03 研究材料",
    "待确认经验": "01 待确认",
    "已确认经验": "02 已确认",
    "历史候选": "03 历史候选",
    "通用": "00 通用",
    "跨领域": "99 跨领域",
    "领域设置": "03 领域",
    "领域": "03 领域",
    "自营账号": "01 自营账号",
    "对标账号": "02 对标账号",
    "账号信息": "账号信息",
    "系统生成文稿": "01 文稿",
    "文稿": "01 文稿",
    "人工改稿": "03 改稿样本",
    "改稿样本": "03 改稿样本",
    "音频": "02 音频",
    "个人通用风格": "01 个人通用风格",
    "通用风格": "01 个人通用风格",
    "领域风格": "02 领域风格",
    "运行状态": "01 运行状态",
    "创作参数": "02 创作参数",
    "人工确认": "03 人工确认",
}


def _folder_name(value: str) -> str:
    return FOLDER_NAMES.get(value, value)


def _folder_path(root: Path, *parts: str) -> Path:
    return root.joinpath(*(_folder_name(part) for part in parts))


def _mirror_alias(parts: tuple[str, ...]) -> str:
    """Return a short, readable label for an internal Obsidian link."""
    if not parts:
        return KNOWLEDGE_BASE_NAME
    final = str(parts[-1])
    if final == "index":
        return _folder_name(parts[-2]) if len(parts) > 1 else KNOWLEDGE_BASE_NAME
    if final.lower().endswith(".md"):
        final = final[:-3]
    return final or KNOWLEDGE_BASE_NAME


def _mirror_link(*parts: str, label: str | None = None) -> str:
    path = f"{OBSIDIAN_MIRROR_ROOT}/{'/'.join(_folder_name(part) for part in parts)}"
    return f"[[{path}|{label or _mirror_alias(parts)}]]"


def _mirror_page_link(*parts: str) -> str:
    """Link to a page while keeping the final page name unchanged."""
    if not parts:
        return f"[[{OBSIDIAN_MIRROR_ROOT}|{KNOWLEDGE_BASE_NAME}]]"
    folders = [_folder_name(part) for part in parts[:-1]]
    folders.append(parts[-1])
    return f"[[{OBSIDIAN_MIRROR_ROOT}/{'/'.join(folders)}|{_mirror_alias(parts)}]]"


def _experience_link(*parts: str) -> str:
    return f"[[{OBSIDIAN_EXPERIENCE_ROOT}/{'/'.join(parts)}]]"


def _page_name(value: str, *, position: int, prefix: str = "资料") -> str:
    text = re.sub(r'[<>:"/\\\\|?#\x00-\x1f]+', "-", value).strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)[:48].strip(" .-") or "未命名"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    clean_prefix = _directory_name(prefix, fallback="资料")
    return f"{clean_prefix}-{text}-{digest}.md"


def _directory_name(value: str, *, fallback: str) -> str:
    text = re.sub(r'[<>:"/\\|?#\x00-\x1f]+', "-", value).strip()
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text[:80] or fallback


def _domain_name(value: str) -> str:
    """Return the user-facing Chinese domain name without changing its internal label."""
    label = str(value or "").strip()
    if not label:
        return "未分类"
    try:
        pack = get_domain_pack(label)
    except (KeyError, ValueError):
        return _directory_name(label, fallback="未分类")
    return _directory_name(str(pack.get("name") or label), fallback="未分类")


def _scope_name(value: Any) -> str:
    """Return the visible applicability scope for a stored record.

    Formal records historically carry one domain label.  Empty scope is now
    treated as genuinely general material, while explicit multi-domain values
    are kept in a separate cross-domain area instead of being hidden as
    unclassified content.
    """
    if isinstance(value, (list, tuple, set)):
        labels = [str(item).strip() for item in value if str(item).strip()]
        if len(labels) > 1:
            return "跨领域"
        value = labels[0] if labels else ""
    label = str(value or "").strip()
    normalized = label.lower().replace("-", "_").replace(" ", "")
    if not label or normalized in {"common", "general", "global", "通用"}:
        return "通用"
    if normalized in {"cross_domain", "crossdomain", "multiple", "跨领域"}:
        return "跨领域"
    if any(separator in label for separator in (",", "，", "/", "、", ";", "；")):
        parts = [part.strip() for part in re.split(r"[,，/、;；]", label) if part.strip()]
        if len(parts) > 1:
            return "跨领域"
    return _domain_name(label)


def _scope_for(item: dict[str, Any], *fallback_keys: str) -> str:
    """Read explicit scope first, then fall back to the historical domain field."""
    for key in ("scope", "scope_type", "applicability_scope", "domain_scope"):
        if item.get(key) not in (None, "", [], {}):
            return _scope_name(item.get(key))
    for key in ("domain_labels", "domains"):
        if item.get(key) not in (None, "", [], {}):
            return _scope_name(item.get(key))
    for key in fallback_keys or ("domain_label", "domain"):
        if item.get(key) not in (None, "", [], {}):
            return _scope_name(item.get(key))
    return "通用"


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
        mirror_root = requested / KNOWLEDGE_BASE_NAME
        staging_root = requested / f".{KNOWLEDGE_BASE_NAME}.next"
        if staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True)

        experiences = self._accepted_experiences(cold_start_id=cold_start_id)
        review_candidates = self._pending_experience_candidates()
        source_cards_by_candidate = self.core.list_experience_candidate_source_cards(
            candidate_ids=[str(item["experience_candidate_id"]) for item in review_candidates]
        )
        source_records = self.core.list_knowledge_source_records()
        account_registry = self.core.list_knowledge_account_registry()
        processing_status = self.core.list_knowledge_processing_status()
        selection_inputs = self.core.list_knowledge_selection_inputs()
        candidate_pool = self.core.list_knowledge_candidate_pool()
        manual_sources = self.core.list_manual_source_workbench()
        human_decisions = self.core.list_knowledge_human_decisions()
        publications = self.core.list_publication_workbench()
        content_workbench = self.core.list_content_workbench()
        (
            source_pages,
            breakdown_pages,
            source_record_count,
            breakdown_record_count,
        ) = self._write_clean_source_library(
            staging_root=staging_root,
            records=source_records,
        )
        reference_pages, reference_record_count = self._write_clean_reference_cases(
            staging_root=staging_root,
            source_cards_by_candidate=source_cards_by_candidate,
            source_domains={
                key: _scope_for(record)
                for record in source_records
                for key in (
                    str(record.get("source_id") or ""),
                    str(record.get("platform_item_id") or ""),
                )
                if key
            },
        )
        experience_record_count, experience_relationship_count = self._write_clean_experience_library(
            staging_root=staging_root,
            experiences=experiences,
            review_candidates=review_candidates,
            source_cards_by_candidate=source_cards_by_candidate,
            reference_pages=reference_pages,
        )
        self._write_account_domain_system(
            staging_root=staging_root,
            cold_start_configurations=self.core.list_cold_start_configurations(),
            voice_profiles=self.core.list_voice_profiles(),
            account_registry=account_registry,
            processing_status=processing_status,
            human_decisions=human_decisions,
            content_workbench=content_workbench,
            publications=publications,
            search_tags=selection_inputs.get("tags") or [],
        )
        selection_counts = self._write_selection_inputs(staging_root=staging_root, inputs=selection_inputs)
        self._write_candidate_pool(staging_root=staging_root, candidates=candidate_pool)
        self._write_manual_sources(staging_root=staging_root, sources=manual_sources)
        self._write_reorganized_content_workbench(
            staging_root=staging_root,
            workbench=content_workbench,
            publications=publications,
            account_registry=account_registry,
        )
        self._write_style_and_preferences(
            staging_root=staging_root,
            account_registry=account_registry,
        )
        self._write_navigation(staging_root=staging_root)
        record_count = (
            source_record_count
            + breakdown_record_count
            + reference_record_count
            + experience_record_count
            + len(candidate_pool)
            + len(content_workbench)
            + len(publications)
        )
        relationship_count = (
            source_record_count * 2
            + breakdown_record_count
            + reference_record_count
            + experience_relationship_count
        )
        source_card_count = reference_record_count
        breakdown_card_count = breakdown_record_count
        material_card_count = source_record_count
        self._write_knowledge_catalog(
            staging_root=staging_root,
            counts={
                "来源记录": len(source_records),
                "转写记录": sum(len(item.get("transcripts") or []) for item in source_records),
                "评论记录": sum(len(item.get("comments") or []) for item in source_records),
                "内容分析": sum(len(item.get("deep_analysis") or []) for item in source_records),
                "热点记录": selection_counts["hotspots"],
                "问题拓展": selection_counts["questions"],
                "检索标签": len(selection_inputs.get("tags") or []),
                "用户保存方向": selection_counts["directions"],
                "候选选题": len(candidate_pool),
                "采集处理明细": len(processing_status.get("items") or []),
                "处理尝试": len(processing_status.get("attempts") or []),
                "补采检查": len(processing_status.get("checkpoints") or []),
                "拆解任务": len(processing_status.get("backlog") or []),
                "待确认经验": len(review_candidates),
                "已确认经验": len(experiences),
                "用户提供来源": len(manual_sources),
                "人工确认记录": len(human_decisions),
                "正式选题任务": len(content_workbench),
                "发布记录": len(publications),
            },
        )
        self._install_mirror(staging_root=staging_root, mirror_root=mirror_root)
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
            "knowledge_base_name": KNOWLEDGE_BASE_NAME,
            "experience_first": False,
            "includes_tag_library": False,
            "formal_write_entry": False,
            "review_candidate_count": len(review_candidates),
            "accepted_experience_count": len(experiences),
            "source_card_count": source_card_count,
            "breakdown_card_count": breakdown_card_count,
            "prepared_material_count": material_card_count,
        }

    @staticmethod
    def _install_mirror(*, staging_root: Path, mirror_root: Path) -> None:
        """Replace generated pages while preserving only current style material."""
        if mirror_root.exists():
            new_style_root = staging_root / "05 风格与偏好"
            new_style_root.mkdir(parents=True, exist_ok=True)

            def copy_user_files(source_root: Path, target_root: Path) -> None:
                if not source_root.exists():
                    return
                for item in source_root.rglob("*"):
                    if not item.is_file() or item.name == "index.md":
                        continue
                    try:
                        existing_text = item.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        existing_text = ""
                    if existing_text.rstrip().endswith("- 当前还没有记录人工修改内容。"):
                        continue
                    relative = item.relative_to(source_root)
                    target = target_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)

            # Preserve style pages already created by a user in the new layout.
            copy_user_files(mirror_root / "05 风格与偏好", new_style_root)

            try:
                shutil.rmtree(mirror_root)
            except OSError as exc:
                failures: list[str] = []
                for child in list(mirror_root.iterdir()):
                    try:
                        if child.is_dir():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                    except OSError:
                        failures.append(str(child))
                if failures:
                    raise StateTransitionError(
                        "知识库仍被其他程序占用，旧目录没有清干净，请先关闭 Obsidian 后再同步。"
                    ) from exc
        try:
            staging_root.replace(mirror_root)
            return
        except PermissionError:
            # Windows may refuse the directory rename while Obsidian is
            # watching the vault. The old tree has already been removed, so a
            # merge copy cannot resurrect obsolete folders.
            shutil.copytree(staging_root, mirror_root, dirs_exist_ok=True)
            shutil.rmtree(staging_root, ignore_errors=True)

    def _accepted_experiences(
        self, *, cold_start_id: str | None
    ) -> list[dict[str, Any]]:
        del cold_start_id
        domains = {
            str(item.get("domain_label") or "").strip()
            for item in (self.core.list_knowledge_account_registry().get("formal_accounts") or [])
            if isinstance(item, dict) and str(item.get("domain_label") or "").strip()
        }
        experiences: list[dict[str, Any]] = []
        for domain in sorted(domains):
            experiences.extend(self.core.list_active_experiences(domain_label=domain))
        return experiences

    def _pending_experience_candidates(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.core.list_pre_topic_experience_candidates()
            if item["status"] == "awaiting_human_decision"
        ]

    def _write_experience_library(
        self,
        *,
        staging_root: Path,
        experiences: list[dict[str, Any]],
        review_candidates: list[dict[str, Any]],
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
        prepared_materials: list[dict[str, Any]],
    ) -> tuple[int, int, int, int, int]:
        library_dir = _folder_path(staging_root, "可复用经验", "已确认经验")
        library_dir.mkdir(parents=True, exist_ok=True)
        by_domain: dict[str, list[dict[str, Any]]] = {}
        for experience in experiences:
            by_domain.setdefault(experience["domain_label"], []).append(experience)

        domain_links: list[str] = []
        relationship_count = 0
        record_count = 1
        for domain_label, items in by_domain.items():
            domain = _domain_name(domain_label)
            domain_file = library_dir / f"{domain}.md"
            domain_dir = library_dir / domain
            domain_dir.mkdir(parents=True, exist_ok=True)
            links_by_layer: dict[str, list[str]] = {}
            for position, item in enumerate(items, start=1):
                layer_label = self._experience_layer_label(item.get("experience_layer"))
                layer_dir = domain_dir / layer_label
                layer_dir.mkdir(parents=True, exist_ok=True)
                filename = _page_name(item["claim"], position=position)
                page = layer_dir / filename
                links_by_layer.setdefault(layer_label, []).append(
                    f"- {_mirror_link('可复用经验', '已确认经验', domain, layer_label, filename[:-3])}"
                )
                page.write_text(
                    "\n".join(
                        [
                            f"# {item['claim']}",
                            "",
                            f"所属领域：{_mirror_link('可复用经验', '已确认经验', domain)}",
                            f"经验层级：{layer_label}",
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
                            f"- {_mirror_link('可复用经验', '已确认经验', domain, layer_label, 'index')}",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                relationship_count += 2
                record_count += 1
            layer_links: list[str] = []
            for layer_label, links in links_by_layer.items():
                layer_index = domain_dir / layer_label / "index.md"
                layer_index.write_text(
                    "\n".join(
                        [
                            f"# {domain} · {layer_label}",
                            "",
                            "这一页只列出该领域、该层级下已经正式接受的经验。",
                            "",
                            *(links or ["- 暂无已确认经验"]),
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                layer_links.append(
                    f"- {_mirror_link('可复用经验', '已确认经验', domain, layer_label, 'index')}"
                )
            domain_file.write_text(
                "\n".join(
                    [
                        f"# {domain}经验",
                        "",
                        "这里只保留已经正式接受、可复用的经验。标签库、采集记录、模型日志和内部编号不进入这里。",
                        "",
                        "## 按经验层级查看",
                        "",
                        *(layer_links or ["- 暂无已确认经验"]),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            domain_links.append(f"- {_mirror_link('可复用经验', '已确认经验', domain)}")
            record_count += 1
        material_page_names, material_record_count, material_relationship_count = self._write_prepared_material_cards(
            staging_root=staging_root,
            materials=prepared_materials,
        )
        source_page_names, source_record_count = self._write_source_cards(
            staging_root=staging_root,
            source_cards_by_candidate=source_cards_by_candidate,
        )
        breakdown_page_names, breakdown_record_count = self._write_breakdown_cards(
            staging_root=staging_root,
            source_cards_by_candidate=source_cards_by_candidate,
        )
        source_link_count, breakdown_link_count, material_link_count = self._write_experience_review_queue(
            staging_root=staging_root,
            candidates=review_candidates,
            source_cards_by_candidate=source_cards_by_candidate,
            source_page_names=source_page_names,
            breakdown_page_names=breakdown_page_names,
            material_page_names=material_page_names,
        )
        record_count += material_record_count + source_record_count + breakdown_record_count
        relationship_count += (
            material_relationship_count
            + source_link_count
            + breakdown_link_count
            + material_link_count
        )
        self._write_source_boundary_page(staging_root=staging_root)
        index_links = [
            f"- 先了解知识库怎么运行：{_mirror_link('项目基础', '使用说明')}",
            f"- 查看账号、内容领域和采集来源：{_mirror_link('项目基础', '账号资料')}、{_mirror_link('项目基础', '内容领域')}、{_mirror_link('项目基础', '采集来源')}",
            f"- 查看采集处理状态和人工确认：{_mirror_link('项目基础', '采集与处理状态')}、{_mirror_link('项目基础', '人工确认记录')}",
            f"- 查看知识库实际保存了哪些数据：{_mirror_link('项目基础', '知识库内容清单')}",
            f"- 查看原始素材：{_mirror_link('原始素材', 'index')}",
            f"- 查看全部来源记录：{_mirror_link('原始素材', '全部来源', 'index')}",
            f"- 查看用户提供的来源：{_mirror_link('原始素材', '用户提供', 'index')}",
            f"- 查看整理后的材料：{_mirror_link('整理材料', 'index')}",
            f"- 查看选题和研究：{_mirror_link('选题研究', 'index')}",
            f"- 查看选题输入、候选选题和正式选题：{_mirror_link('选题研究', 'index')}",
            f"- 查看文稿和音频：{_mirror_link('文稿与音频', 'index')}",
            f"- 查看发布与复盘：{_mirror_link('发布与复盘', 'index')}",
            f"- 查看待确认经验：{_mirror_link('可复用经验', '待确认经验.md')}",
            f"- 查看已确认经验：{_mirror_link('可复用经验', '已确认经验.md')}",
        ]
        _folder_path(staging_root, "可复用经验", "已确认经验").mkdir(parents=True, exist_ok=True)
        _folder_path(staging_root, "可复用经验", "已确认经验", "暂无已确认经验.md").write_text(
            "# 暂无已确认经验\n\n当前没有已经人工确认的经验。待确认内容只能从‘待确认经验’入口查看，不能直接用于后续创作。\n",
            encoding="utf-8",
        )
        _folder_path(staging_root, "可复用经验", "已确认经验.md").write_text(
            "\n".join(
                [
                    "# 已确认经验",
                    "",
                    "这里只收已经人工确认、可以复用的经验。",
                    "",
                    *(domain_links or ["- 当前还没有已确认经验。"]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        home_root = _folder_path(staging_root, "首页")
        home_root.mkdir(parents=True, exist_ok=True)
        (home_root / "首页.md").write_text(
            "\n".join(
                [
                    f"# {KNOWLEDGE_BASE_NAME}",
                    "",
                    "这是自媒体内容创作知识库的总入口。",
                    "这里可以查看项目运行所需的基础资料、创作过程中的素材和产出，以及已经确认的可复用经验。",
                    "正式业务数据仍由系统保存；这里主要用于阅读、检索和人工确认。",
                    "",
                    "## 按你的需要进入",
                    "",
                    *index_links,
                    "",
                    "## 当前状态",
                    "",
                    f"- 待确认经验：{len(review_candidates)} 条。",
                    f"- 已确认经验：{len(experiences)} 条。",
                    "- 待确认经验不会自动用于后续创作，只有人工确认后才会进入已确认经验。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return (
            relationship_count,
            record_count + len(review_candidates) + 4,
            len(source_page_names),
            len(breakdown_page_names),
            len(material_page_names),
        )

    @staticmethod
    def _payload(record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            return {}
        payload = record.get("payload")
        return payload if isinstance(payload, dict) else record

    @classmethod
    def _render_payload(cls, payload: dict[str, Any]) -> list[str]:
        labels = {
            "title": "标题", "domain": "领域", "domain_label": "领域",
            "core_question": "核心问题", "topic_angle": "讲述角度",
            "scope_or_requirement": "范围和要求", "audience": "目标人群",
            "value": "内容价值", "material_gaps": "材料缺口", "risks": "风险",
            "timeliness": "时效要求", "claims_to_verify": "需要核实的说法",
            "research_stop_condition": "研究停止条件", "conclusion": "研究结论",
            "key_conclusion": "核心结论", "supporting_evidence": "支持依据",
            "outline": "内容安排", "hook": "开头", "body": "正文", "ending": "结尾",
            "review": "审核结果", "change_request": "修改意见", "script": "口播正文",
            "content_promise": "内容承诺", "research_questions": "研究问题",
            "must_include": "必须包含", "must_avoid": "必须避免",
        }
        lines: list[str] = []
        for key, value in payload.items():
            label = labels.get(str(key))
            if not label or value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                lines.append(f"### {label}")
                for item in value:
                    lines.extend([f"- {item}", ""] if not isinstance(item, dict) else cls._render_payload(item))
            elif isinstance(value, dict):
                lines.append(f"### {label}")
                lines.extend(cls._render_payload(value))
            else:
                lines.extend([f"- {label}：{value}", ""])
        return lines

    @staticmethod
    def _node_label(node: Any) -> str:
        return {
            "research_plan": "研究方案", "deep_research": "研究结果",
            "content_plan": "内容计划", "formal_draft": "初稿",
            "copy_optimization": "优化稿", "de_ai_revision": "自然表达修改",
            "review": "最终审核",
        }.get(str(node or ""), "尚未进入内容环节")

    @staticmethod
    def _status_label(status: Any) -> str:
        return {
            "not_started": "尚未开始", "awaiting_human_review": "等待人工确认",
            "approved": "已确认", "completed": "已完成", "cancelled": "已取消",
            "failed": "处理失败",
        }.get(str(status or ""), str(status or "未说明"))

    def _write_account_domain_system(
        self,
        *,
        staging_root: Path,
        cold_start_configurations: list[dict[str, Any]],
        voice_profiles: list[dict[str, Any]],
        account_registry: dict[str, list[dict[str, Any]]],
        processing_status: dict[str, list[dict[str, Any]]],
        human_decisions: list[dict[str, Any]],
        content_workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
        search_tags: list[dict[str, Any]],
    ) -> None:
        """Write the compact account/domain boundary and runtime records.

        Account ownership, domain scope and system runtime data are separate
        concepts.  They must not be rendered as repeated copies of one
        another under a generic project-foundation folder.
        """
        del cold_start_configurations, content_workbench, publications
        root = _folder_path(staging_root, "账号与领域")
        owned_root = _folder_path(staging_root, "账号与领域", "自营账号")
        competitor_root = _folder_path(staging_root, "账号与领域", "对标账号")
        domain_root = _folder_path(staging_root, "账号与领域", "领域")
        system_root = _folder_path(staging_root, "系统记录")
        for folder in (root, owned_root, competitor_root, domain_root, system_root):
            folder.mkdir(parents=True, exist_ok=True)

        formal_accounts = [
            item for item in (account_registry.get("formal_accounts") or [])
            if isinstance(item, dict)
        ]
        collection_accounts = {
            str(item.get("account_id") or ""): item
            for item in (account_registry.get("collection_accounts") or [])
            if isinstance(item, dict)
        }
        domains: dict[str, dict[str, Any]] = {}
        for scope, pack in configured_domain_packs().items():
            if str(pack.get("activation_status") or "") == "fixture_only":
                continue
            domains[scope] = {
                "name": str(pack.get("name") or scope),
                "boundary": str(pack.get("description") or "未记录"),
                "status": str(pack.get("activation_status") or "未说明"),
            }
        for account in formal_accounts:
            scope = _scope_for(account)
            domains.setdefault(scope, {"name": scope, "boundary": "未记录", "status": "未说明"})
        tags_by_domain: dict[str, list[str]] = {}
        for tag in search_tags:
            scope = _scope_for(tag)
            if scope != "通用":
                value = str(tag.get("tag") or "").strip()
                if value:
                    tags_by_domain.setdefault(scope, []).append(value)

        owned_accounts = [
            item for item in formal_accounts if item.get("account_role") == "owned"
        ]
        competitor_accounts = [
            item for item in formal_accounts if item.get("account_role") != "owned"
        ]

        def account_name(item: dict[str, Any]) -> str:
            return _directory_name(str(item.get("display_name") or "未命名账号"), fallback="未命名账号")

        def account_scope(item: dict[str, Any]) -> str:
            return _scope_for(item, "domain_label", "domain")

        owned_links: list[str] = []
        for item in sorted(owned_accounts, key=lambda value: account_name(value)):
            name = account_name(item)
            account_dir = owned_root / name
            account_dir.mkdir(parents=True, exist_ok=True)
            account_id = str(item.get("content_account_id") or "")
            collection = collection_accounts.get(account_id, {})
            scope = account_scope(item)
            page_lines = [
                f"# 自营账号｜{name}", "",
                f"账号地址：{item.get('external_account_ref') or '未记录'}",
                f"账号状态：{item.get('status') or '未说明'}",
                f"平台：{collection.get('platform') or '未记录'}",
                f"采集状态：{collection.get('registration_status') or '未记录'}", "",
                "## 所属领域", "",
                f"- {_mirror_link('账号与领域', '领域', scope, 'index')}｜{scope}", "",
                "## 相关内容", "",
                f"- {_mirror_link('自营内容', name, 'index')}｜该账号的自营内容",
                f"- {_mirror_link('风格与偏好', name, 'index')}｜该账号的风格与偏好", "",
            ]
            (account_dir / "账号信息.md").write_text("\n".join(page_lines), encoding="utf-8")
            (account_dir / "index.md").write_text(
                "\n".join([f"# 自营账号｜{name}", "", f"- {_mirror_link('账号与领域', '自营账号', name, '账号信息')}", ""]),
                encoding="utf-8",
            )
            owned_links.append(f"- {_mirror_link('账号与领域', '自营账号', name, '账号信息')}｜{name}")
        (owned_root / "index.md").write_text(
            "\n".join(["# 自营账号", "", *(owned_links or ["- 当前没有已登记的自营账号。"]), ""]),
            encoding="utf-8",
        )

        competitor_links_by_domain: dict[str, list[str]] = {}
        for item in sorted(competitor_accounts, key=lambda value: (account_scope(value), account_name(value))):
            scope = account_scope(item)
            name = account_name(item)
            account_dir = competitor_root / _folder_name(scope)
            account_dir.mkdir(parents=True, exist_ok=True)
            account_id = str(item.get("content_account_id") or "")
            collection = collection_accounts.get(account_id, {})
            page = account_dir / f"{name}.md"
            page.write_text(
                "\n".join([
                    f"# 对标账号｜{name}", "",
                    f"所属领域：{scope}",
                    f"账号地址：{item.get('external_account_ref') or '未记录'}",
                    f"账号状态：{item.get('status') or '未说明'}",
                    f"平台：{collection.get('platform') or '未记录'}",
                    f"采集状态：{collection.get('registration_status') or '未记录'}", "",
                    "对标账号只用于该领域的素材观察和内容拆解，不进入自营内容和个人风格。", "",
                ]),
                encoding="utf-8",
            )
            link = f"- {_mirror_link('账号与领域', '对标账号', scope, name)}｜{name}"
            competitor_links_by_domain.setdefault(scope, []).append(link)
        competitor_index_links = [
            f"- {_mirror_link('账号与领域', '对标账号', scope, 'index')}｜{scope}"
            for scope in sorted(competitor_links_by_domain)
        ]
        for scope, links in competitor_links_by_domain.items():
            scope_dir = competitor_root / _folder_name(scope)
            (scope_dir / "index.md").write_text(
                "\n".join([f"# 对标账号｜{scope}", "", *links, ""]),
                encoding="utf-8",
            )
        (competitor_root / "index.md").write_text(
            "\n".join(["# 对标账号", "", *(competitor_index_links or ["- 当前没有已登记的对标账号。"]), ""]),
            encoding="utf-8",
        )

        domain_links: list[str] = []
        owned_by_domain: dict[str, list[str]] = {}
        for item in owned_accounts:
            owned_by_domain.setdefault(account_scope(item), []).append(account_name(item))
        for scope in sorted(domains):
            item = domains[scope]
            page = domain_root / _folder_name(scope) / "领域.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            tag_text = "、".join(sorted(set(tags_by_domain.get(scope, [])))) or "当前没有登记检索标签"
            page.write_text(
                "\n".join([
                    f"# 领域｜{scope}", "",
                    f"领域名称：{item['name']}",
                    f"状态：{item['status']}",
                    f"内容边界：{item['boundary']}",
                    f"选题检索标签：{tag_text}", "",
                    "## 自营账号", "",
                    *(
                        f"- {_mirror_link('账号与领域', '自营账号', name, '账号信息')}"
                        for name in sorted(owned_by_domain.get(scope, []))
                    ),
                    *( ["- 当前没有归属该领域的自营账号。"] if not owned_by_domain.get(scope) else [] ),
                    "",
                    "## 对标账号", "",
                    *(competitor_links_by_domain.get(scope) or ["- 当前没有该领域的对标账号。"]),
                    "",
                ]),
                encoding="utf-8",
            )
            domain_links.append(f"- {_mirror_link('账号与领域', '领域', scope, 'index')}｜{scope}")
            (page.parent / "index.md").write_text(
                "\n".join([f"# 领域｜{scope}", "", f"- {_mirror_page_link('账号与领域', '领域', scope, '领域')}", ""]),
                encoding="utf-8",
            )
        (domain_root / "index.md").write_text(
            "\n".join(["# 领域", "", *(domain_links or ["- 当前没有已确认领域。"]), ""]),
            encoding="utf-8",
        )
        (root / "index.md").write_text(
            "\n".join([
                "# 账号与领域", "",
                "账号决定内容归谁，领域决定内容讲什么。两者不互相替代，也不重复保存同一份资料。", "",
                f"- {_mirror_link('账号与领域', '自营账号', 'index')}｜自营账号",
                f"- {_mirror_link('账号与领域', '对标账号', 'index')}｜对标账号",
                f"- {_mirror_link('账号与领域', '领域', 'index')}｜领域边界和归属", "",
            ]),
            encoding="utf-8",
        )

        status = processing_status or {}
        (system_root / "01 运行状态.md").write_text(
            "\n".join([
                "# 运行状态", "",
                f"采集处理记录：{len(status.get('items') or [])} 条",
                f"处理尝试：{len(status.get('attempts') or [])} 条",
                f"补采检查：{len(status.get('checkpoints') or [])} 条",
                f"待处理任务：{len(status.get('backlog') or [])} 条", "",
                "这里仅保留会影响业务判断的运行状态；底层日志和技术追踪继续保留在正式系统中。", "",
            ]),
            encoding="utf-8",
        )
        parameter_lines = ["# 创作参数", "", "这里记录系统实际使用的声音和生成配置。", ""]
        parameter_lines.extend(
            f"- {profile.get('profile_label') or '未命名声音'}｜状态：{self._status_label(profile.get('status'))}｜模式：{profile.get('mode') or '未说明'}"
            for profile in voice_profiles
        )
        if not voice_profiles:
            parameter_lines.append("- 当前没有已登记的创作参数。")
        (system_root / "02 创作参数.md").write_text("\n".join(parameter_lines + [""]), encoding="utf-8")
        decision_lines = ["# 人工确认", ""]
        for decision in human_decisions:
            decision_lines.append(
                f"- 时间：{decision.get('received_at') or '未记录'}｜结果：{decision.get('decision') or decision.get('status') or '未说明'}｜对象：{decision.get('target_ref') or decision.get('command_id') or '未记录'}"
            )
        if not human_decisions:
            decision_lines.append("- 当前没有人工确认记录。")
        (system_root / "03 人工确认.md").write_text("\n".join(decision_lines + [""]), encoding="utf-8")
        (system_root / "index.md").write_text(
            "\n".join([
                "# 系统记录", "",
                "这里保存系统运行必须的数据，不放日常创作内容。", "",
                f"- {_mirror_link('系统记录', '运行状态')}｜运行状态",
                f"- {_mirror_link('系统记录', '创作参数')}｜创作参数",
                f"- {_mirror_link('系统记录', '人工确认')}｜人工确认", "",
            ]),
            encoding="utf-8",
        )

    def _write_project_foundation(
        self,
        *,
        staging_root: Path,
        cold_start_configurations: list[dict[str, Any]],
        voice_profiles: list[dict[str, Any]],
        account_registry: dict[str, list[dict[str, Any]]],
        processing_status: dict[str, list[dict[str, Any]]],
        human_decisions: list[dict[str, Any]],
        content_workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
        search_tags: list[dict[str, Any]],
    ) -> None:
        foundation_root = _folder_path(staging_root, "项目基础")
        foundation_root.mkdir(parents=True, exist_ok=True)
        common_root = foundation_root / _folder_name("通用")
        common_root.mkdir(parents=True, exist_ok=True)
        domain_settings_root = foundation_root / _folder_name("领域设置")
        domain_settings_root.mkdir(parents=True, exist_ok=True)
        accounts: dict[str, dict[str, Any]] = {}
        domains: dict[str, dict[str, Any]] = {}
        tags_by_scope: dict[str, list[dict[str, Any]]] = {}
        for tag in search_tags:
            scope = _scope_for(tag)
            if scope != "通用":
                tags_by_scope.setdefault(scope, []).append(tag)
        for configuration in cold_start_configurations:
            scope = _scope_for(configuration)
            domains[scope] = {
                "name": str(configuration.get("domain_name") or scope),
                "boundary": str(configuration.get("domain_boundary") or "未记录"),
                "status": str(configuration.get("status") or "未说明"),
            }
            for account in configuration.get("accounts") or []:
                if not isinstance(account, dict):
                    continue
                account_id = str(account.get("content_account_id") or account.get("display_name") or "").strip()
                if account_id:
                    accounts[account_id] = {
                        "name": str(account.get("display_name") or "未命名账号"),
                        "role": "自有账号" if account.get("account_role") == "owned" else "对标账号",
                        "scope": scope,
                        "source": str(account.get("external_account_ref") or "未记录"),
                        "status": str(account.get("status") or "未说明"),
                    }
        account_lines = [
            f"- {item['name']}｜{item['role']}｜适用范围：{item['scope']}｜状态：{item['status']}｜来源：{item['source']}"
            for item in sorted(accounts.values(), key=lambda value: (value["scope"], value["name"]))
        ]
        (common_root / "账号资料.md").write_text(
            "\n".join([
                "# 账号资料", "",
                "这里展示当前正式配置中的自有账号和对标账号。账号身份由系统保存，知识库只做阅读展示。", "",
                *(account_lines or ["- 当前没有已登记账号。"]), "",
            ]),
            encoding="utf-8",
        )
        collection_accounts = account_registry.get("collection_accounts") or []
        collection_lines = [
            "# 账号与采集配置", "",
            "这里保存实际采集账号的运行配置。账号资料回答‘是谁’，这里回答‘系统怎样采集它’。", "",
        ]
        for account in collection_accounts:
            collection_lines.extend([
                f"## {account.get('account_name') or '未命名账号'}",
                f"- 平台：{account.get('platform') or '未记录'}",
                f"- 适用范围：{_scope_for(account)}",
                f"- 主页：{account.get('homepage_url') or '未记录'}",
                f"- 采集状态：{account.get('registration_status') or '未记录'}",
                f"- 首次采集策略：{account.get('first_crawl_policy') or '未记录'}",
                f"- 评论采集策略：{account.get('comments_policy') or '未记录'}",
                f"- 来源配置：{account.get('source_config_ref') or '未记录'}", "",
            ])
        if not collection_accounts:
            collection_lines.append("- 当前没有采集账号配置。")
        (common_root / "账号与采集配置.md").write_text("\n".join(collection_lines), encoding="utf-8")

        baseline_lines = ["# 账号基线", "", "这里保存账号和内容表现的比较基线，供后续判断内容是否达到高信号。", ""]
        for baseline in account_registry.get("baselines") or []:
            baseline_lines.append(
                f"- 账号：{baseline.get('account_id') or '未记录'}｜指标：{baseline.get('metric') or '未记录'}｜观察点：{baseline.get('observation_point') or '未记录'}｜样本数：{baseline.get('sample_count') or 0}｜中位数：{baseline.get('median_value') or '未记录'}｜计算时间：{baseline.get('computed_at') or '未记录'}"
            )
        if not account_registry.get("baselines"):
            baseline_lines.append("- 当前没有账号基线。")
        (common_root / "账号基线.md").write_text("\n".join(baseline_lines + [""]), encoding="utf-8")
        domain_lines: list[str] = ["# 内容领域", ""]
        for name, item in sorted(domains.items()):
            domain_lines.extend([
                f"## {name}", "", f"- 领域名称：{item['name']}",
                f"- 当前状态：{item['status']}", f"- 内容边界：{item['boundary']}", "",
            ])
        if not domains:
            domain_lines.append("- 当前没有已确认领域。")
        (common_root / "内容领域.md").write_text("\n".join(domain_lines), encoding="utf-8")
        domain_links: list[str] = []
        for scope, item in sorted(domains.items()):
            scope_root = domain_settings_root / _folder_name(scope)
            scope_root.mkdir(parents=True, exist_ok=True)
            (scope_root / "领域说明.md").write_text(
                "\n".join([
                    f"# 领域说明｜{scope}", "",
                    f"适用范围：{scope}",
                    f"领域名称：{item['name']}",
                    f"当前状态：{item['status']}",
                    f"内容边界：{item['boundary']}", "",
                ]),
                encoding="utf-8",
            )
            domain_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '领域说明')}｜{scope}")
        (domain_settings_root / "index.md").write_text(
            "\n".join(["# 领域设置", "", *(domain_links or ["- 当前没有已确认领域。"]), ""]),
            encoding="utf-8",
        )
        accounts_by_scope: dict[str, list[dict[str, Any]]] = {}
        for account in accounts.values():
            accounts_by_scope.setdefault(str(account["scope"]), []).append(account)
        collection_by_scope: dict[str, list[dict[str, Any]]] = {}
        for account in collection_accounts:
            collection_by_scope.setdefault(_scope_for(account), []).append(account)
        account_scope_by_id = {str(account_id): str(item["scope"]) for account_id, item in accounts.items()}
        baselines_by_scope: dict[str, list[dict[str, Any]]] = {}
        for baseline in account_registry.get("baselines") or []:
            baseline_scope = account_scope_by_id.get(str(baseline.get("account_id") or ""), "通用")
            baselines_by_scope.setdefault(baseline_scope, []).append(baseline)
        scoped_settings = set(domains) | set(accounts_by_scope) | set(collection_by_scope) | set(baselines_by_scope) | set(tags_by_scope)
        domain_links = []
        for scope in sorted(scoped_settings):
            scope_root = domain_settings_root / _folder_name(scope)
            scope_root.mkdir(parents=True, exist_ok=True)
            scope_item = domains.get(scope)
            scope_index_links: list[str] = []
            if scope_item:
                (scope_root / "领域说明.md").write_text(
                    "\n".join([
                        f"# 领域说明｜{scope}", "",
                        f"适用范围：{scope}",
                        f"领域名称：{scope_item['name']}",
                        f"当前状态：{scope_item['status']}",
                        f"内容边界：{scope_item['boundary']}", "",
                    ]),
                    encoding="utf-8",
                )
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '领域说明')}")
            scoped_accounts = sorted(accounts_by_scope.get(scope, []), key=lambda value: (value["role"], value["name"]))
            owned_accounts = [item for item in scoped_accounts if item["role"] == "自有账号"]
            competitor_accounts = [item for item in scoped_accounts if item["role"] == "对标账号"]
            def _account_lines(items: list[dict[str, Any]]) -> list[str]:
                return [
                    f"- {item['name']}｜适用范围：{item['scope']}｜状态：{item['status']}｜账号地址：{item['source']}"
                    for item in items
                ]
            if scoped_accounts:
                (scope_root / "账号资料.md").write_text(
                    "\n".join([
                        f"# 账号资料｜{scope}", "",
                        "这里明确区分自营账号和对标账号，不能把两类账号混在一起理解。", "",
                        "## 自营账号", "",
                        *(_account_lines(owned_accounts) or ["- 当前没有已登记的自营账号。"]), "",
                        "## 对标账号", "",
                        *(_account_lines(competitor_accounts) or ["- 当前没有已登记的对标账号。"]), "",
                    ]),
                    encoding="utf-8",
                )
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '账号资料')}")
                source_lines = [
                    f"- {item['name']}｜{item['role']}｜适用范围：{item['scope']}｜来源：{item['source']}"
                    for item in scoped_accounts
                ]
                (scope_root / "采集来源.md").write_text(
                    "\n".join([
                        f"# 采集来源｜{scope}", "",
                        "这里只展示已经进入正式配置的来源。登录信息、密钥和内部运行日志不写入知识库。", "",
                        *source_lines, "",
                    ]),
                    encoding="utf-8",
                )
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '采集来源')}")
            scoped_collection = collection_by_scope.get(scope, [])
            if scoped_collection:
                collection_lines = [f"# 账号与采集配置｜{scope}", "", f"适用范围：{scope}", ""]
                for account in scoped_collection:
                    collection_lines.extend([
                        f"## {account.get('account_name') or '未命名账号'}",
                        f"- 平台：{account.get('platform') or '未记录'}",
                        f"- 主页：{account.get('homepage_url') or '未记录'}",
                        f"- 采集状态：{account.get('registration_status') or '未记录'}",
                        f"- 首次采集策略：{account.get('first_crawl_policy') or '未记录'}",
                        f"- 评论采集策略：{account.get('comments_policy') or '未记录'}",
                        f"- 来源配置：{account.get('source_config_ref') or '未记录'}", "",
                    ])
                (scope_root / "账号与采集配置.md").write_text("\n".join(collection_lines), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '账号与采集配置')}")
            scoped_baselines = baselines_by_scope.get(scope, [])
            if scoped_baselines:
                baseline_lines = [f"# 账号基线｜{scope}", "", f"适用范围：{scope}", ""]
                baseline_lines.extend(
                    f"- 账号：{baseline.get('account_id') or '未记录'}｜指标：{baseline.get('metric') or '未记录'}｜观察点：{baseline.get('observation_point') or '未记录'}｜样本数：{baseline.get('sample_count') or 0}｜中位数：{baseline.get('median_value') or '未记录'}｜计算时间：{baseline.get('computed_at') or '未记录'}"
                    for baseline in scoped_baselines
                )
                (scope_root / "账号基线.md").write_text("\n".join(baseline_lines + [""]), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '账号基线')}")
            scoped_tags = sorted(tags_by_scope.get(scope, []), key=lambda value: str(value.get("tag") or ""))
            if scoped_tags:
                tag_lines = [
                    f"# 选题检索标签｜{scope}", "",
                    "这些是系统用于发现选题素材的检索配置，不是候选选题。", "",
                ]
                tag_lines.extend(
                    f"- {tag.get('tag') or '未命名标签'}｜状态：{tag.get('status') or '未说明'}｜人工审核：{tag.get('human_review_status') or '未说明'}"
                    for tag in scoped_tags
                )
                (scope_root / "选题检索标签.md").write_text("\n".join(tag_lines + [""]), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '选题检索标签')}")
            owned_content_lines = self._owned_content_lines(
                scope=scope,
                workbench=content_workbench,
                publications=publications,
            )
            (scope_root / "自营内容.md").write_text(
                "\n".join([
                    f"# 自营内容｜{scope}", "",
                    "这里保存自营账号自己的选题、文稿、音频、发布和复盘入口。对标账号的内容不放在这里。", "",
                    *owned_content_lines, "",
                ]),
                encoding="utf-8",
            )
            scope_index_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, '自营内容')}")
            if scope_index_links:
                (scope_root / "index.md").write_text("\n".join([f"# 领域设置｜{scope}", "", *scope_index_links, ""]), encoding="utf-8")
                domain_links.append(f"- {_mirror_link('项目基础', '领域设置', scope, 'index')}｜{scope}")
        (domain_settings_root / "index.md").write_text(
            "\n".join(["# 领域设置", "", *(domain_links or ["- 当前没有领域专属设置。"]), ""]),
            encoding="utf-8",
        )
        for common_name in ("账号资料", "账号与采集配置", "账号基线", "采集来源"):
            (common_root / f"{common_name}.md").unlink(missing_ok=True)
        voice_lines = [
            "# 使用的模型与声音设置", "",
            "这里展示已经登记的声音配置及其状态；正式模型路线仍以系统当前配置为准。", "",
        ]
        voice_lines.extend(
            f"- {profile.get('profile_label') or '未命名声音'}｜状态：{self._status_label(profile.get('status'))}｜模式：{profile.get('mode') or '未说明'}"
            for profile in voice_profiles
        )
        if not voice_profiles:
            voice_lines.append("- 当前没有已登记声音配置。")
        voice_lines.append("")
        (common_root / "使用的模型与声音设置.md").write_text("\n".join(voice_lines), encoding="utf-8")
        (common_root / "使用说明.md").write_text(
            "\n".join([
                "# 使用说明", "",
                "正式业务数据和状态由系统保存，知识库负责阅读、检索和查看结果。", "",
                "## 写入原则", "",
                "- 系统根据正式数据自动生成页面，不根据页面标题猜测归属。",
                "- 同一份材料只保存一份，其他位置使用链接。",
                "- 状态变化由系统更新，Obsidian页面不直接改变正式状态。",
                "- 无法确定归属的内容必须停在待补充状态，不能自动归类。", "",
            ]),
            encoding="utf-8",
        )
        registrations = processing_status.get("registrations") or []
        items = processing_status.get("items") or []
        attempts = processing_status.get("attempts") or []
        backlog = processing_status.get("backlog") or []
        status_lines = [
            "# 采集与处理状态", "",
            "这里保存会影响后续运行的采集、转写、评论整理、内容拆解和失败状态。", "",
            f"- 采集登记：{len(registrations)} 条",
            f"- 处理项目：{len(items)} 条",
            f"- 处理尝试：{len(attempts)} 条",
            f"- 拆解任务：{len(backlog)} 条", "",
            "## 采集登记", "",
        ]
        for registration in registrations:
            status_lines.append(
                f"- {registration.get('display_name') or '未命名账号'}｜适用范围：{_scope_for(registration)}｜{registration.get('status') or '未说明'}｜当前步骤：{registration.get('current_step') or '未说明'}"
            )
        if not registrations:
            status_lines.append("- 当前没有采集登记。")
        status_lines.extend(["", "## 处理明细", ""])
        for item in items:
            error = item.get("error")
            suffix = f"｜问题：{error}" if error not in (None, "", {}, []) else ""
            status_lines.append(
                f"- {item.get('item_ref') or '未命名材料'}｜{item.get('step_name') or '未说明'}｜{item.get('status') or '未说明'}｜更新时间：{item.get('updated_at') or '未记录'}{suffix}"
            )
        if not items:
            status_lines.append("- 当前没有处理明细。")
        status_lines.extend(["", "## 失败和补充记录", ""])
        for attempt in attempts:
            status_lines.append(
                f"- {attempt.get('source_id') or '未命名来源'}｜{attempt.get('attempt_kind') or '未说明'}｜{attempt.get('outcome') or '未说明'}｜{attempt.get('reason') or '未记录'}"
            )
        if not attempts:
            status_lines.append("- 当前没有处理失败或补充记录。")
        status_lines.append("")
        (common_root / "采集与处理状态.md").write_text("\n".join(status_lines), encoding="utf-8")

        decision_lines = [
            "# 人工确认记录", "",
            "这里保存会改变正式业务状态的人工确认、退回和拒绝记录。", "",
        ]
        for decision in human_decisions:
            decision_lines.append(
                f"- {decision.get('received_at') or '未记录'}｜动作：{decision.get('action') or '未说明'}｜对象：{decision.get('target_ref') or '未说明'}｜状态：{decision.get('status') or '未说明'}｜结果：{decision.get('result_json') or '未记录'}"
            )
        if not human_decisions:
            decision_lines.append("- 当前没有人工确认记录。")
        decision_lines.append("")
        (common_root / "人工确认记录.md").write_text("\n".join(decision_lines), encoding="utf-8")
        common_links = [
            "内容领域", "使用的模型与声音设置", "采集与处理状态", "人工确认记录",
            "知识库内容清单", "分层和写入说明",
        ]
        (common_root / "index.md").write_text(
            "\n".join(["# 通用设置", "", "这里保存不属于某一个具体领域、但会影响整个知识库运行的内容。", "", *(f"- {_mirror_link('项目基础', '通用', name)}" for name in common_links), ""]),
            encoding="utf-8",
        )
        (foundation_root / "index.md").write_text(
            "\n".join([
                "# 项目基础", "",
                f"- {_mirror_link('项目基础', '通用', 'index')}",
                f"- {_mirror_link('项目基础', '领域设置', 'index')}", "",
            ]),
            encoding="utf-8",
        )

    def _owned_content_lines(
        self,
        *,
        scope: str,
        workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
    ) -> list[str]:
        writing_nodes = {
            "content_plan", "formal_draft", "copy_optimization",
            "de_ai_revision", "review",
        }
        lines: list[str] = []
        for position, work in enumerate(workbench, start=1):
            topic = self._payload(work.get("topic"))
            work_scope = _scope_for(topic, "domain_label", "domain")
            if work_scope != scope:
                continue
            title = str(topic.get("title") or topic.get("core_question") or f"未命名选题 {position}")
            task_id = str(work.get("task_id") or position)
            folder = f"{_directory_name(title, fallback='未命名选题')[:60]}｜{hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:8]}"
            versions = work.get("artifact_versions") if isinstance(work.get("artifact_versions"), list) else []
            has_manuscript = any(
                version.get("node") in writing_nodes and version.get("artifact")
                for version in versions
            )
            lines.extend([
                f"## {title}",
                f"- 当前阶段：{self._node_label(work.get('current_node'))}",
                f"- 当前状态：{self._status_label(work.get('current_status'))}",
                f"- 选题与研究：{_mirror_link('选题研究', '正式选题', scope, folder, '选题与研究')}",
            ])
            if has_manuscript:
                lines.extend([
                    f"- 系统生成文稿：{_mirror_page_link('文稿与音频', '系统生成文稿', scope, folder, '系统生成文稿')}",
                    f"- 人工改稿：{_mirror_page_link('文稿与音频', '人工改稿', scope, folder, '人工改稿')}",
                ])
            if work.get("audio_attempts"):
                lines.append(
                    f"- 音频：{_mirror_page_link('文稿与音频', '音频', scope, folder, '音频')}"
                )
            lines.append("")

        for position, item in enumerate(publications, start=1):
            publication = item.get("publication") if isinstance(item.get("publication"), dict) else {}
            publication_scope = _scope_for(publication)
            if publication_scope != scope:
                continue
            publication_id = str(publication.get("publication_id") or f"发布记录-{position}")
            folder = f"{_directory_name(publication_id, fallback=f'发布记录-{position}')}｜{hashlib.sha256(publication_id.encode('utf-8')).hexdigest()[:8]}"
            lines.extend([
                f"## 已发布内容｜{publication_id}",
                f"- 发布与复盘：{_mirror_link('发布与复盘', scope, folder, '发布与复盘')}",
                "",
            ])
        return lines or ["- 当前还没有自营内容记录。"]

    @staticmethod
    def _display_value(value: Any) -> str:
        if value in (None, "", [], {}):
            return "未记录"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _metric_label(key: str) -> str:
        return {
            "like_count": "点赞数",
            "comment_count": "评论数",
            "share_count": "分享数",
            "collect_count": "收藏数",
            "play_count": "播放数",
        }.get(str(key), str(key))

    @staticmethod
    def _source_stem(record: dict[str, Any]) -> str:
        source_id = str(record.get("source_id") or "未命名来源")
        account = _directory_name(
            str(record.get("account_name") or "未登记账号"),
            fallback="未登记账号",
        )
        title = _directory_name(
            str(record.get("title") or source_id),
            fallback="未命名来源",
        )
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
        return f"{account}｜{title[:48]}｜{digest}"

    @staticmethod
    def _source_file(prefix: str, stem: str) -> str:
        return f"{prefix}｜{stem}.md"

    def _write_clean_source_library(
        self,
        *,
        staging_root: Path,
        records: list[dict[str, Any]],
    ) -> tuple[dict[str, str], dict[str, str], int, int]:
        """Write source identity, source metrics, transcript and comments.

        The first two are one page because they answer the same question:
        "what is this source and what do we know about it?"  Transcript and
        comments are another page because they are both text material from the
        same source.  A source keeps one stable identity throughout.
        """
        raw_root = _folder_path(staging_root, "原始素材")
        source_root = _folder_path(raw_root, "来源与基础信息")
        copy_root = _folder_path(raw_root, "文案与评论")
        breakdown_root = _folder_path(staging_root, "内容整理", "内容拆解")
        for root in (source_root, copy_root, breakdown_root):
            root.mkdir(parents=True, exist_ok=True)

        source_pages: dict[str, str] = {}
        breakdown_pages: dict[str, str] = {}
        source_links: list[str] = []
        copy_links: list[str] = []
        breakdown_links: list[str] = []
        source_by_scope: dict[str, list[str]] = {}
        copy_by_scope: dict[str, list[str]] = {}
        breakdown_by_scope: dict[str, list[str]] = {}

        for record in records:
            source_id = str(record.get("source_id") or "未命名来源")
            stem = self._source_stem(record)
            source_filename = self._source_file("来源与基础信息", stem)
            copy_filename = self._source_file("文案与评论", stem)
            source_pages[source_id] = source_filename[:-3]

            scope = _scope_for(record)
            account = str(record.get("account_name") or "未登记账号")
            title = str(record.get("title") or source_id)
            url = str(record.get("url") or "").strip()
            source_link = _mirror_link(
                "原始素材", "来源与基础信息", scope, source_filename[:-3], label="来源与基础信息"
            )
            copy_link = _mirror_link(
                "原始素材", "文案与评论", scope, copy_filename[:-3], label="文案与评论"
            )
            source_index_link = _mirror_link(
                "原始素材", "来源与基础信息", scope, source_filename[:-3], label=f"{title}｜{account}"
            )
            copy_index_link = _mirror_link(
                "原始素材", "文案与评论", scope, copy_filename[:-3], label=f"{title}｜{account}"
            )
            source_links.append(f"- {source_index_link}｜适用范围：{scope}")
            copy_links.append(
                f"- {copy_index_link}｜适用范围：{scope}｜转写：{len(record.get('transcripts') or [])} 条｜评论：{len(record.get('comments') or [])} 条"
            )
            source_by_scope.setdefault(scope, []).append(source_links[-1])
            copy_by_scope.setdefault(scope, []).append(copy_links[-1])

            analyses = record.get("deep_analysis") if isinstance(record.get("deep_analysis"), list) else []
            breakdown_filename = self._source_file("拆解", stem)
            breakdown_link = ""
            if analyses:
                breakdown_pages[source_id] = breakdown_filename[:-3]
                breakdown_link = _mirror_link(
                    "内容整理", "内容拆解", scope, breakdown_filename[:-3], label="内容拆解"
                )
                breakdown_links.append(
                    f"- {_mirror_link('内容整理', '内容拆解', scope, breakdown_filename[:-3], label=f'{title}｜{account}')}｜适用范围：{scope}"
                )
                breakdown_by_scope.setdefault(scope, []).append(breakdown_links[-1])

            source_lines = [
                f"# 来源与基础信息｜{title}", "",
                f"适用范围：{scope}",
                f"账号：{account}",
                f"平台：{self._platform_label(record.get('platform'))}",
                f"发布时间：{record.get('publish_time') or '未记录'}",
                f"系统内部编号：{source_id}",
                f"平台内容编号：{record.get('platform_item_id') or '未记录'}",
                f"是否排除：{record.get('excluded_reason') or '否'}",
                f"是否完成持续跟踪：{'是' if record.get('tracking_completed') else '否'}", "",
                f"## {self._source_link_label(record)}", "",
                f"- [打开{self._source_link_label(record).replace('链接', '')}]({url})" if url else f"- 当前没有可点击的{self._source_link_label(record)}", "",
                "## 基础数据", "",
            ]
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
            if metrics:
                source_lines.extend(
                    f"- {self._metric_label(key)}：{value if value not in (None, '') else '未记录'}"
                    for key, value in metrics.items()
                )
            else:
                source_lines.append("- 当前没有基础数据")
            source_lines.extend(["", "## 持续采集数据", ""])
            checks = record.get("checks") if isinstance(record.get("checks"), list) else []
            if checks:
                for check in checks:
                    check_metrics = check.get("metrics") if isinstance(check.get("metrics"), dict) else {}
                    rendered = "；".join(
                        f"{self._metric_label(key)}：{value}" for key, value in check_metrics.items()
                    ) or "没有记录指标"
                    source_lines.append(
                        f"- 第 {check.get('day_since_publish') or '?'} 天｜{check.get('checked_at') or '未记录'}｜{rendered}"
                    )
            else:
                source_lines.append("- 当前没有持续采集数据")
            source_lines.extend(["", "## 高信号记录", ""])
            hits = record.get("hits") if isinstance(record.get("hits"), list) else []
            if hits:
                for hit in hits:
                    source_lines.append(
                        f"- 命中渠道：{hit.get('hit_channel') or '未记录'}｜状态：{hit.get('preparation_status') or '未记录'}｜进入时间：{hit.get('promoted_at') or '未记录'}"
                    )
            else:
                source_lines.append("- 当前没有高信号记录")
            source_lines.extend([
                "", "## 同一来源的其他内容", "",
                f"- {copy_link}",
            ])
            if source_id in breakdown_pages:
                source_lines.append(f"- {breakdown_link}")
            source_lines.append("")
            scope_folder = _folder_name(scope)
            (source_root / scope_folder / source_filename).parent.mkdir(parents=True, exist_ok=True)
            (copy_root / scope_folder).mkdir(parents=True, exist_ok=True)
            if analyses:
                (breakdown_root / scope_folder).mkdir(parents=True, exist_ok=True)
            (source_root / scope_folder / source_filename).write_text("\n".join(source_lines), encoding="utf-8")

            transcripts = record.get("transcripts") if isinstance(record.get("transcripts"), list) else []
            transcript_lines = [
                f"# 文案与评论｜{title}", "",
                f"账号：{account}",
                f"转写记录数：{len(transcripts)}",
                f"评论记录数：{len(record.get('comments') or [])}", "",
                "## 关联内容", "",
                f"- {source_link}",
                *( [f"- {breakdown_link}"] if breakdown_link else [] ),
                "",
                "## 转写文案", "",
            ]
            if transcripts:
                for transcript in transcripts:
                    transcript_lines.extend([
                        f"## 第 {transcript.get('version') or '?'} 版",
                        f"处理状态：{transcript.get('processing_status') or '未记录'}",
                        f"处理时间：{transcript.get('created_at') or '未记录'}", "",
                        "### 文案内容", "",
                        transcript.get("cleaned_text") or transcript.get("raw_text") or "- 这一版没有可展示的文字", "",
                    ])
            else:
                transcript_lines.extend([
                    "## 当前状态", "",
                    "当前没有转写文案。这里明确表示正式数据中没有转写结果。", "",
                ])
            transcript_lines.extend(["## 评论", ""])

            comments = record.get("comments") if isinstance(record.get("comments"), list) else []
            if comments:
                for comment in comments:
                    text = str(comment.get("text") or "").strip()
                    if text:
                        transcript_lines.append(
                            f"> {text}（点赞：{comment.get('like_count') or 0}｜样本序号：{comment.get('sample_rank') or '未记录'}）"
                        )
            else:
                transcript_lines.extend(["当前没有评论材料。", ""])
            (copy_root / scope_folder / copy_filename).write_text("\n".join(transcript_lines), encoding="utf-8")

            if analyses:
                breakdown_lines = [
                    f"# 内容拆解｜{title}", "",
                    f"账号：{account}",
                    "## 关联内容", "",
                    f"- {source_link}",
                    f"- {copy_link}", "",
                ]
                for analysis in analyses:
                    breakdown_lines.extend([
                        f"## 第 {analysis.get('version') or '?'} 版拆解",
                        f"分析时间：{analysis.get('created_at') or '未记录'}",
                        f"分析方式：{analysis.get('model_name') or '未记录'}", "",
                        "### 主题判断", self._display_value(analysis.get("topic_pattern")),
                        "", "### 开头判断", self._display_value(analysis.get("hook_pattern")),
                        "", "### 结构判断", self._display_value(analysis.get("structure_pattern")), "",
                    ])
                (breakdown_root / scope_folder / breakdown_filename).write_text("\n".join(breakdown_lines), encoding="utf-8")

        self._write_source_index(source_root, "来源与基础信息", source_links, "这里把来源身份、视频或文章链接、基础数据和持续采集数据放在一起。", domain_links=source_by_scope, path_parts=("原始素材", "来源与基础信息"))
        self._write_source_index(copy_root, "文案与评论", copy_links, "这里把同一来源的转写文案和评论放在一起；没有数据时会明确写出。", domain_links=copy_by_scope, path_parts=("原始素材", "文案与评论"))
        self._write_source_index(breakdown_root, "内容拆解", breakdown_links, "这里只放已经形成内容拆解的来源，没有拆解的来源不会伪装成有结果。", domain_links=breakdown_by_scope, path_parts=("内容整理", "内容拆解"))
        (raw_root / "index.md").write_text(
            "\n".join([
                "# 原始素材", "",
                "同一条来源只认一个身份。来源与基础信息放在一起，转写文案与评论放在一起，避免一条来源被拆成几处。", "",
                f"- 来源总数：{len(records)} 条",
                f"- 有转写记录：{sum(1 for item in records if item.get('transcripts'))} 条",
                f"- 有评论材料：{sum(1 for item in records if item.get('comments'))} 条",
                "", "## 按资料类型查看", "",
                f"- {_mirror_link('原始素材', '来源与基础信息', 'index')}",
                f"- {_mirror_link('原始素材', '文案与评论', 'index')}",
                f"- {_mirror_link('原始素材', '用户提供', 'index')}", "",
            ]),
            encoding="utf-8",
        )
        (breakdown_root.parent / "index.md").write_text(
            "\n".join([
                "# 素材", "",
                "来源、文案、评论、内容拆解和用户提供的原始资料都归在这里；每条资料保留自己的领域。", "",
                f"- {_mirror_link('素材', '来源与数据', 'index')}｜来源与数据",
                f"- {_mirror_link('素材', '文案与评论', 'index')}｜文案与评论",
                f"- {_mirror_link('素材', '内容拆解', 'index')}｜内容拆解",
                f"- {_mirror_link('素材', '用户提供', 'index')}｜用户提供", "",
            ]),
            encoding="utf-8",
        )
        return source_pages, breakdown_pages, len(records), len(breakdown_pages)

    def _write_source_index(
        self,
        root: Path,
        title: str,
        links: list[str],
        description: str,
        *,
        domain_links: dict[str, list[str]] | None = None,
        path_parts: tuple[str, ...] = (),
    ) -> None:
        root_links = links
        if domain_links:
            root_links = []
            for scope, scope_items in sorted(domain_links.items()):
                scope_root = root / _folder_name(scope)
                scope_root.mkdir(parents=True, exist_ok=True)
                (scope_root / "index.md").write_text(
                    "\n".join([
                        f"# {title}｜{scope}", "", description, "", *(scope_items or ["- 当前没有记录。"]), "",
                    ]),
                    encoding="utf-8",
                )
                root_links.append(
                    f"- {_mirror_link(*path_parts, scope, 'index')}｜{scope}"
                )
        (root / "index.md").write_text(
            "\n".join([f"# {title}", "", description, "", *(root_links or ["- 当前没有记录。"]), ""]),
            encoding="utf-8",
        )

    @classmethod
    def _source_link_label(cls, record: dict[str, Any]) -> str:
        source_kind = str(record.get("source_kind") or "").lower()
        platform = str(record.get("platform") or "").lower()
        url = str(record.get("url") or "").lower()
        if (
            "视频" in source_kind
            or platform in {"douyin", "bilibili", "xiaohongshu", "youtube"}
            or any(host in url for host in ("douyin.com", "bilibili.com", "xiaohongshu.com", "youtube.com", "youtu.be"))
        ):
            return "视频链接"
        if (
            "文章" in source_kind
            or platform in {"article", "news", "baidu", "zhihu", "weibo"}
            or any(marker in url for marker in ("/article/", "/news/", "mp.weixin.qq.com"))
        ):
            return "文章链接"
        return "原文链接"

    @staticmethod
    def _platform_label(value: Any) -> str:
        return {
            "douyin": "抖音",
            "bilibili": "哔哩哔哩",
            "xiaohongshu": "小红书",
            "youtube": "YouTube",
        }.get(str(value or ""), str(value or "未记录"))

    def _write_clean_reference_cases(
        self,
        *,
        staging_root: Path,
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
        source_domains: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], int]:
        root = _folder_path(staging_root, "可复用经验", "待确认经验", "依据材料")
        root.mkdir(parents=True, exist_ok=True)
        unique_cards: dict[str, dict[str, Any]] = {}
        for cards in source_cards_by_candidate.values():
            for card in cards:
                key = str(card.get("source_key") or "").strip()
                if key and key not in unique_cards:
                    unique_cards[key] = card
        page_names: dict[str, str] = {}
        links: list[str] = []
        links_by_domain: dict[str, list[str]] = {}
        for position, (source_key, card) in enumerate(sorted(unique_cards.items()), start=1):
            title = str(card.get("title") or "未找到标题")
            account = str(card.get("account_name") or "未找到账号")
            scope = _scope_name(
                card.get("scope")
                or card.get("scope_type")
                or card.get("domain_labels")
                or card.get("domain_label")
                or (source_domains or {}).get(source_key)
                or ""
            )
            filename = _page_name(
                f"{account}｜{title}｜{source_key}",
                position=position,
                prefix="案例",
            )
            page_names[source_key] = f"{scope}/{filename[:-3]}"
            links.append(f"- {_mirror_link('可复用经验', '待确认经验', '依据材料', scope, filename[:-3])}｜{account}｜{title}")
            links_by_domain.setdefault(scope, []).append(links[-1])
            actions = card.get("evidence_actions") or []
            quotes = card.get("evidence_quotes") or []
            scope_folder = _folder_name(scope)
            (root / scope_folder).mkdir(parents=True, exist_ok=True)
            (root / scope_folder / filename).write_text(
                "\n".join([
                    f"# 经验候选依据｜{title}", "",
                    f"账号：{account}",
                    f"案例来源编号：{source_key}",
                    f"发布时间：{card.get('publish_time') or '未记录'}", "",
                    f"## {self._source_link_label(card)}", "",
                    f"- [打开{self._source_link_label(card).replace('链接', '')}]({card.get('url')})" if card.get("url") else f"- 当前没有可点击的{self._source_link_label(card)}", "",
                    "## 这份材料是做什么的", "",
                    "这不是要研究的爆款，也不是正式经验。它只是系统提出待确认经验时引用的来源依据，方便你判断这条经验是否值得保留。", "",
                    "## 从来源中看到的做法", "",
                    *(f"- {item}" for item in actions or ["当前没有提取到具体做法。"]),
                    "", "## 原文证据", "",
                    *(f"> {item}" for item in quotes or ["当前没有保留可展示的证据摘录。"]),
                    "", "## 使用边界", "",
                    "这是一条经验候选依据，不是正式经验，也不是事实结论。", "",
                ]),
                encoding="utf-8",
            )
        self._write_source_index(root, "经验候选依据", links, "这里只放被待确认经验引用的来源依据。它不属于内容整理，也不属于正式研究材料。", domain_links=links_by_domain, path_parts=("可复用经验", "待确认经验", "依据材料"))
        return page_names, len(page_names)

    def _write_clean_experience_library(
        self,
        *,
        staging_root: Path,
        experiences: list[dict[str, Any]],
        review_candidates: list[dict[str, Any]],
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
        reference_pages: dict[str, str],
    ) -> tuple[int, int]:
        root = _folder_path(staging_root, "可复用经验")
        confirmed_root = _folder_path(staging_root, "可复用经验", "已确认经验")
        review_root = _folder_path(staging_root, "可复用经验", "待确认经验")
        confirmed_root.mkdir(parents=True, exist_ok=True)
        review_root.mkdir(parents=True, exist_ok=True)
        confirmed_links: list[str] = []
        review_links: list[str] = []
        confirmed_by_domain: dict[str, list[str]] = {}
        review_by_domain: dict[str, list[str]] = {}
        relationship_count = 0
        for position, item in enumerate(experiences, start=1):
            claim = str(item.get("claim") or "未命名经验")
            scope = _scope_for(item)
            filename = _page_name(claim, position=position, prefix="经验")
            confirmed_links.append(f"- {_mirror_link('可复用经验', '已确认经验', scope, filename[:-3])}")
            confirmed_by_domain.setdefault(scope, []).append(confirmed_links[-1])
            scope_folder = _folder_name(scope)
            (confirmed_root / scope_folder).mkdir(parents=True, exist_ok=True)
            (confirmed_root / scope_folder / filename).write_text(
                "\n".join([
                    f"# 已确认经验｜{claim}", "",
                    f"适用范围：{scope}",
                    f"经验类型：{item.get('classification') or '未说明'}",
                    f"正式确认时间：{item.get('accepted_at') or '未记录'}", "",
                    "## 经验内容", "", claim, "",
                    "## 证据情况", "",
                    f"- 正式拆解依据：{item.get('evidence_count') or 0} 条",
                    "- 详细来源仍保留在原始素材和内容拆解中。", "",
                ]),
                encoding="utf-8",
            )
            relationship_count += 1
        for position, candidate in enumerate(review_candidates, start=1):
            proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
            content = proposal.get("candidate") if isinstance(proposal.get("candidate"), dict) else proposal
            summary = str(content.get("summary") or candidate.get("experience_candidate_id") or "未命名候选")
            scope = _scope_for(candidate)
            filename = _page_name(summary, position=position, prefix="待确认")
            candidate_id = str(candidate.get("experience_candidate_id") or "")
            links: list[str] = []
            for card in source_cards_by_candidate.get(candidate_id, []):
                source_key = str(card.get("source_key") or "")
                if source_key in reference_pages:
                    links.append(
                        f"- {_mirror_link('可复用经验', '待确认经验', '依据材料', *reference_pages[source_key].split('/'))}"
                    )
                    relationship_count += 1
            review_links.append(f"- {_mirror_link('可复用经验', '待确认经验', scope, filename[:-3])}｜{summary}")
            review_by_domain.setdefault(scope, []).append(review_links[-1])
            scope_folder = _folder_name(scope)
            (review_root / scope_folder).mkdir(parents=True, exist_ok=True)
            (review_root / scope_folder / filename).write_text(
                "\n".join([
                    f"# 待确认经验｜{summary}", "",
                    f"当前状态：{candidate.get('status') or '等待人工确认'}",
                    f"来源数量：{candidate.get('source_count') or 0}", "",
                    "## 候选内容", "", self._display_value(content), "",
                    "## 依据材料", "", *(links or ["- 当前没有解析到依据材料，不能直接确认。"]), "",
                    "## 说明", "", "这只是系统提出的候选，必须经过人工确认后才会进入已确认经验。", "",
                ]),
                encoding="utf-8",
            )
        confirmed_index_links: list[str] = []
        for scope in sorted(confirmed_by_domain):
            scope_folder = _folder_name(scope)
            (confirmed_root / scope_folder / "index.md").write_text("\n".join([f"# 已确认经验｜{scope}", "", *confirmed_by_domain[scope], ""]), encoding="utf-8")
            confirmed_index_links.append(f"- {_mirror_link('可复用经验', '已确认经验', scope, 'index')}｜{scope}")
        (confirmed_root / "index.md").write_text("\n".join(["# 已确认经验", "", *(confirmed_index_links or ["- 当前没有已确认经验。"]), ""]), encoding="utf-8")
        if not experiences:
            (confirmed_root / "暂无已确认经验.md").write_text("# 暂无已确认经验\n\n当前没有已经人工确认的经验。\n", encoding="utf-8")
        review_index_links: list[str] = []
        for scope in sorted(review_by_domain):
            scope_folder = _folder_name(scope)
            (review_root / scope_folder / "index.md").write_text("\n".join([f"# 待确认经验｜{scope}", "", *review_by_domain[scope], ""]), encoding="utf-8")
            review_index_links.append(f"- {_mirror_link('可复用经验', '待确认经验', scope, 'index')}｜{scope}")
        (review_root / "index.md").write_text("\n".join(["# 待确认经验", "", "这些内容还没有被用户确认，不能直接作为创作规则使用。依据材料只是帮助你审核候选，不是要专门研究的爆款。", "", f"- {_mirror_link('可复用经验', '待确认经验', '依据材料', 'index')}", *(review_index_links or ["- 当前没有待确认经验。"]), ""]), encoding="utf-8")
        (root / "index.md").write_text("\n".join(["# 可复用经验", "", "只有人工确认后的内容，才会进入已确认经验；待确认内容只能用于人工审核。", "", f"- {_mirror_link('可复用经验', '待确认经验', 'index')}", f"- {_mirror_link('可复用经验', '已确认经验', 'index')}", ""]), encoding="utf-8")
        return len(experiences) + len(review_candidates), relationship_count

    def _write_reorganized_content_workbench(
        self,
        *,
        staging_root: Path,
        workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
        account_registry: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Keep self-owned output under account first, then strict domain scope."""
        topic_root = _folder_path(staging_root, "选题", "正式选题")
        research_root = _folder_path(staging_root, "选题")
        content_root = _folder_path(staging_root, "自营内容")
        archive_root = _folder_path(staging_root, "归档")
        for folder in (research_root, topic_root, content_root, archive_root):
            folder.mkdir(parents=True, exist_ok=True)

        formal_accounts = [
            item for item in (account_registry.get("formal_accounts") or [])
            if isinstance(item, dict) and item.get("account_role") == "owned" and item.get("status") == "active"
        ]

        def visible_account(item: dict[str, Any]) -> str:
            return _directory_name(str(item.get("display_name") or "未命名自营账号"), fallback="未命名自营账号")

        def resolve_account(payload: dict[str, Any]) -> str:
            refs = {
                str(payload.get(key) or "").strip()
                for key in ("account_ref", "account_id", "content_account_id", "account_name")
                if str(payload.get(key) or "").strip()
            }
            for item in formal_accounts:
                values = {
                    str(item.get(key) or "").strip()
                    for key in ("content_account_id", "display_name", "external_account_ref")
                    if str(item.get(key) or "").strip()
                }
                if refs & values:
                    return visible_account(item)
            return "待补账号"

        def strict_scope(payload: dict[str, Any]) -> str:
            scope = _scope_for(payload, "domain_label", "domain")
            return "待补领域" if scope == "通用" else scope

        topic_links: list[str] = []
        content_links_by_account: dict[str, list[str]] = {}
        topic_content_links: dict[str, list[str]] = {}
        writing_nodes = {"content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"}

        for position, work in enumerate(workbench, start=1):
            topic = self._payload(work.get("topic"))
            artifact = self._payload(work.get("current_artifact"))
            title = str(topic.get("title") or topic.get("core_question") or f"未命名选题 {position}").strip()
            scope = strict_scope(topic)
            account = resolve_account(topic)
            task_id = str(work.get("task_id") or position)
            folder = f"{_directory_name(title, fallback='未命名选题')[:60]}｜{hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:8]}"
            topic_dir = topic_root / _folder_name(scope) / folder
            topic_dir.mkdir(parents=True, exist_ok=True)
            topic_link = _mirror_page_link("选题", "正式选题", scope, folder, "选题与研究")
            topic_links.append(f"- {topic_link}｜{title}")
            versions = work.get("artifact_versions") if isinstance(work.get("artifact_versions"), list) else []
            research_materials = [item for item in (work.get("research_materials") or []) if isinstance(item, dict)]
            research_lines = [
                f"- {item.get('title') or '未命名材料'}｜{item.get('evidence_role') or '未说明'}｜{item.get('source_ref') or '无来源链接'}"
                for item in research_materials
            ]
            if research_materials:
                (topic_dir / "研究材料.md").write_text(
                    "\n".join([f"# 研究材料｜{title}", "", *research_lines, ""]),
                    encoding="utf-8",
                )
            topic_page_lines = [
                f"# 正式选题｜{title}", "",
                f"账号：{account}", f"领域：{scope}",
                f"当前阶段：{self._node_label(work.get('current_node'))}",
                f"当前状态：{self._status_label(work.get('current_status'))}", "",
                "## 选题内容", "", *self._render_payload(topic),
                "", "## 研究材料", "", *(research_lines or ["- 当前没有正式研究材料。"]), "",
            ]
            (topic_dir / "选题与研究.md").write_text("\n".join(topic_page_lines), encoding="utf-8")

            has_manuscript = work.get("current_node") in writing_nodes and bool(artifact)
            has_audio = any(
                isinstance(attempt, dict)
                and str(attempt.get("status") or "") in {"processing", "awaiting_human_review", "approved", "returned"}
                for attempt in (work.get("audio_attempts") or [])
            )
            if not (has_manuscript or has_audio):
                continue

            content_dir = content_root / account / _folder_name(scope) / folder
            content_dir.mkdir(parents=True, exist_ok=True)
            content_page_links: list[str] = []
            if has_manuscript:
                current_writing = artifact if work.get("current_node") in writing_nodes else {}
                lines = [
                    f"# 文稿｜{title}", "", f"账号：{account}", f"领域：{scope}", "",
                    "## 当前版本", "", *(self._render_payload(current_writing) or ["- 当前没有正式文稿。"]),
                    "", "## 说明", "", "这里只展示当前正式文稿，不把研究方案或旧版本混进来。",
                ]
                (content_dir / "文稿.md").write_text("\n".join(lines), encoding="utf-8")
                content_page_links.append(f"- {_mirror_page_link('自营内容', account, scope, folder, '文稿')}｜文稿")
            if has_audio:
                audio_lines = [f"# 音频｜{title}", "", f"账号：{account}", f"领域：{scope}", "", "## 音频记录", ""]
                for attempt in work.get("audio_attempts") or []:
                    if isinstance(attempt, dict):
                        audio_lines.append(
                            f"- 第 {attempt.get('attempt_number') or '?'} 次｜状态：{self._status_label(attempt.get('status'))}｜位置：{attempt.get('audio_ref') or '未记录'}"
                        )
                (content_dir / "音频.md").write_text("\n".join(audio_lines + [""]), encoding="utf-8")
                content_page_links.append(f"- {_mirror_page_link('自营内容', account, scope, folder, '音频')}｜音频")
            (content_dir / "index.md").write_text(
                "\n".join([
                    f"# 自营内容｜{title}", "", f"账号：{account}", f"领域：{scope}", "",
                    *content_page_links,
                    f"- {_mirror_link('风格与偏好', account, 'index')}｜该账号的风格与偏好", "",
                ]),
                encoding="utf-8",
            )
            content_link = _mirror_link("自营内容", account, scope, folder, "index")
            content_links_by_account.setdefault(account, []).append(f"- {content_link}｜{title}｜{scope}")
            topic_content_links.setdefault(str(work.get("task_id") or ""), []).append(f"- {content_link}｜自营内容")

        for position, item in enumerate(publications, start=1):
            publication = item.get("publication") if isinstance(item.get("publication"), dict) else {}
            scope = strict_scope(publication)
            account = resolve_account(publication)
            publication_id = str(publication.get("publication_id") or f"发布记录-{position}")
            folder = f"{_directory_name(publication_id, fallback=f'发布记录-{position}')}｜{hashlib.sha256(publication_id.encode('utf-8')).hexdigest()[:8]}"
            content_dir = content_root / account / _folder_name(scope) / folder
            content_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                f"# 发布与复盘｜{publication_id}", "", f"账号：{account}", f"领域：{scope}",
                f"平台：{self._platform_label(publication.get('platform'))}",
                f"发布时间：{publication.get('published_at') or '未记录'}",
                f"发布状态：{publication.get('status') or '未记录'}",
                f"外部链接：{publication.get('external_video_url') or '未记录'}", "", "## 表现数据", "",
            ]
            observations = item.get("observations") if isinstance(item.get("observations"), list) else []
            lines.extend(
                f"- {observation.get('point_code') or '未说明'}｜状态：{observation.get('observation_status') or '未说明'}｜时间：{observation.get('observed_at') or '未记录'}｜数据：{self._display_value(observation.get('metrics_json'))}"
                for observation in observations if isinstance(observation, dict)
            )
            if not observations:
                lines.append("- 当前没有表现数据。")
            lines.extend(["", "## 复盘结果", ""])
            reviews = item.get("reviews") if isinstance(item.get("reviews"), list) else []
            if reviews:
                for review in reviews:
                    if isinstance(review, dict):
                        lines.extend([
                            f"- 复盘状态：{review.get('status') or '未说明'}",
                            f"- 选题判断：{review.get('selection_assessment') or '未记录'}",
                            f"- 叙事判断：{review.get('narrative_assessment') or '未记录'}",
                            f"- 素材判断：{review.get('material_assessment') or '未记录'}",
                            f"- 反馈候选：{self._display_value(review.get('feedback_candidate_json'))}", "",
                        ])
            else:
                lines.append("- 当前没有复盘结果。")
            (content_dir / "发布与复盘.md").write_text("\n".join(lines), encoding="utf-8")
            link = _mirror_page_link("自营内容", account, scope, folder, "发布与复盘")
            content_links_by_account.setdefault(account, []).append(f"- {link}｜{publication_id}｜{scope}")

        account_names = sorted({visible_account(item) for item in formal_accounts} | set(content_links_by_account))
        for account in account_names:
            account_dir = content_root / account
            account_dir.mkdir(parents=True, exist_ok=True)
            links = content_links_by_account.get(account) or ["- 当前没有已产生的自营内容。"]
            (account_dir / "index.md").write_text(
                "\n".join([f"# 自营内容｜{account}", "", "自营内容按账号归档，每条内容必须标明所属领域。", "", *links, ""]),
                encoding="utf-8",
            )
        account_index_links = [
            f"- {_mirror_link('自营内容', account, 'index')}｜{account}"
            for account in account_names
        ] or ["- 当前没有自营账号内容。"]
        (content_root / "index.md").write_text(
            "\n".join([
                "# 自营内容", "", "这里保存自营账号实际产生的文稿、音频和发布复盘。对标账号内容不进入这里。", "",
                *account_index_links, "",
            ]),
            encoding="utf-8",
        )
        (topic_root / "index.md").write_text(
            "\n".join(["# 正式选题", "", *(topic_links or ["- 当前没有正式选题。"]), ""]),
            encoding="utf-8",
        )
        (research_root / "index.md").write_text(
            "\n".join([
                "# 选题", "",
                f"- {_mirror_link('选题', '选题输入', 'index')}｜进入选题流程前的输入",
                f"- {_mirror_link('选题', '候选选题', 'index')}｜等待人工决定的候选",
                f"- {_mirror_link('选题', '正式选题', 'index')}｜已经确认要做的选题", "",
            ]),
            encoding="utf-8",
        )
        (archive_root / "index.md").write_text("# 归档\n\n这里保存已经取消、失效或被新版本替代的内容。\n", encoding="utf-8")

    def _write_style_and_preferences(
        self,
        *,
        staging_root: Path,
        account_registry: dict[str, list[dict[str, Any]]],
    ) -> None:
        root = _folder_path(staging_root, "风格与偏好")
        root.mkdir(parents=True, exist_ok=True)
        owned = [
            item for item in (account_registry.get("formal_accounts") or [])
            if isinstance(item, dict) and item.get("account_role") == "owned" and item.get("status") == "active"
        ]
        account_names = [_directory_name(str(item.get("display_name") or "未命名自营账号"), fallback="未命名自营账号") for item in owned]
        account_domains: dict[str, set[str]] = {}
        for item in owned:
            account = _directory_name(str(item.get("display_name") or "未命名自营账号"), fallback="未命名自营账号")
            account_domains.setdefault(account, set()).add(_scope_for(item))
        account_links: list[str] = []
        for account in account_names or ["未命名自营账号"]:
            account_root = root / account
            general_root = account_root / _folder_name("个人通用风格")
            domain_root = account_root / _folder_name("领域风格")
            sample_root = account_root / _folder_name("改稿样本")
            for folder in (account_root, general_root, domain_root, sample_root):
                folder.mkdir(parents=True, exist_ok=True)
            (general_root / "个人通用风格.md").write_text(
                "\n".join([
                    f"# 个人通用风格｜{account}", "",
                    "这里才是跨领域、稳定且经过确认的个人写作习惯。一次性的内容修改不会直接进入这里。", "",
                    "## 当前已确认偏好", "", "- 当前还没有已确认的个人通用风格。", "",
                    "## 形成规则", "", "- 多次改稿出现同一倾向，或用户明确确认后，才会沉淀为个人通用风格。", "",
                ]),
                encoding="utf-8",
            )
            domains = sorted(account_domains.get(account) or {"待补领域"})
            domain_links: list[str] = []
            sample_links: list[str] = []
            for domain in domains:
                domain_page = domain_root / f"{_folder_name(domain)}.md"
                domain_page.write_text(
                    "\n".join([
                        f"# 领域风格｜{domain}", "", f"所属账号：{account}", f"适用领域：{domain}", "",
                        "这里只保存该领域专属的表达要求、术语习惯和内容边界，不会影响其他领域。", "",
                        "## 当前已确认偏好", "", "- 当前还没有已确认的领域风格。", "",
                    ]),
                    encoding="utf-8",
                )
                sample_scope = sample_root / _folder_name(domain)
                sample_scope.mkdir(parents=True, exist_ok=True)
                (sample_scope / "index.md").write_text(
                    "\n".join([
                        f"# 改稿样本｜{domain}", "", f"所属账号：{account}",
                        "改稿样本保留原稿和修改后的稿子，用来提炼风格；它不等于已经确认的风格规则。", "",
                        "- 当前还没有可展示的改稿样本。", "",
                    ]),
                    encoding="utf-8",
                )
                domain_links.append(f"- {_mirror_link('风格与偏好', account, '领域风格', domain)}｜{domain}领域风格")
                sample_links.append(f"- {_mirror_link('风格与偏好', account, '改稿样本', domain, 'index')}｜{domain}改稿样本")
            (account_root / "index.md").write_text(
                "\n".join([
                    f"# 风格与偏好｜{account}", "",
                    f"- {_mirror_link('风格与偏好', account, '个人通用风格')}｜个人通用风格",
                    "", "## 领域风格", "", *domain_links,
                    "", "## 改稿样本", "", *sample_links, "",
                ]),
                encoding="utf-8",
            )
            account_links.append(f"- {_mirror_link('风格与偏好', account, 'index')}｜{account}")
        (root / "index.md").write_text(
            "\n".join([
                "# 风格与偏好", "",
                "个人通用风格归账号，领域风格归账号下的具体领域，改稿样本保留账号和领域关系。", "",
                *(account_links or ["- 当前没有自营账号。"]), "",
            ]),
            encoding="utf-8",
        )

    def _write_navigation(self, *, staging_root: Path) -> None:
        root = _folder_path(staging_root, "导航")
        root.mkdir(parents=True, exist_ok=True)
        sections = [
            ("账号与领域", "账号归属、对标账号和领域边界"),
            ("素材", "来源、数据、文案、评论和内容拆解"),
            ("选题", "选题输入、候选选题和正式选题"),
            ("自营内容", "自营账号实际产生的文稿、音频和复盘"),
            ("风格与偏好", "个人通用风格、领域风格和改稿样本"),
            ("经验", "待确认、已确认和历史经验候选"),
            ("系统记录", "运行状态、创作参数和人工确认"),
            ("归档", "取消、失效或被替代的内容"),
        ]
        (root / "index.md").write_text(
            "\n".join(["# 导航", "", *(f"- {_mirror_link(name, 'index')}｜{description}" for name, description in sections), ""]),
            encoding="utf-8",
        )

    def _write_clean_content_workbench(
        self,
        *,
        staging_root: Path,
        workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
    ) -> None:
        topic_root = _folder_path(staging_root, "选题研究", "正式选题")
        research_root = _folder_path(staging_root, "选题研究")
        delivery_root = _folder_path(staging_root, "文稿与音频")
        system_root = _folder_path(staging_root, "文稿与音频", "系统生成文稿")
        manual_root = _folder_path(staging_root, "文稿与音频", "人工改稿")
        audio_root = _folder_path(staging_root, "文稿与音频", "音频")
        feedback_root = _folder_path(staging_root, "发布与复盘")
        archive_root = _folder_path(staging_root, "归档")
        for root in (research_root, topic_root, delivery_root, system_root, manual_root, audio_root, feedback_root, archive_root):
            root.mkdir(parents=True, exist_ok=True)
        topic_links: list[str] = []
        draft_links: list[str] = []
        manual_links: list[str] = []
        audio_links: list[str] = []
        publication_links: list[str] = []
        writing_nodes = {
            "content_plan", "formal_draft", "copy_optimization",
            "de_ai_revision", "review",
        }
        for position, work in enumerate(workbench, start=1):
            topic = self._payload(work.get("topic"))
            artifact = self._payload(work.get("current_artifact"))
            title = str(topic.get("title") or topic.get("core_question") or f"未命名选题 {position}")
            scope = _scope_for(topic, "domain_label", "domain")
            task_id = str(work.get("task_id") or position)
            folder = f"{_directory_name(title, fallback='未命名选题')[:60]}｜{hashlib.sha256(task_id.encode('utf-8')).hexdigest()[:8]}"
            scope_folder = _folder_name(scope)
            topic_dir = topic_root / scope_folder / folder
            draft_dir = system_root / scope_folder / folder
            manual_dir = manual_root / scope_folder / folder
            audio_dir = audio_root / scope_folder / folder
            versions = work.get("artifact_versions") if isinstance(work.get("artifact_versions"), list) else []
            has_manuscript = (
                work.get("current_node") in writing_nodes and bool(artifact)
            ) or any(
                version.get("node") in writing_nodes and bool(version.get("artifact"))
                for version in versions
            )
            has_audio = bool(work.get("audio_attempts"))
            topic_dir.mkdir(parents=True, exist_ok=True)
            topic_links.append(f"- {_mirror_link('选题研究', '正式选题', scope, folder, '选题与研究')}｜{scope}")
            research_materials = [
                item for item in (work.get("research_materials") or [])
                if isinstance(item, dict)
            ]
            research_material_lines = [
                f"- {item.get('title') or '未命名材料'}｜{item.get('evidence_role') or '未说明'}｜{item.get('source_ref') or '无来源链接'}"
                for item in research_materials
            ]
            research_material_link = ""
            if research_materials:
                material_page = topic_dir / "研究材料.md"
                material_page.write_text(
                    "\n".join([
                        f"# 研究材料｜{title}", "",
                        "这里仅保存这个正式选题实际用到的研究材料，不保存通用爆款研究。", "",
                        *research_material_lines, "",
                    ]),
                    encoding="utf-8",
                )
                research_material_link = f"- {_mirror_page_link('选题研究', '正式选题', scope, folder, '研究材料')}"
            research_lines = [
                f"# 选题与研究｜{title}", "",
                f"适用范围：{scope}",
                f"任务编号：{task_id}",
                f"当前阶段：{self._node_label(work.get('current_node'))}",
                f"当前状态：{self._status_label(work.get('current_status'))}", "",
                "## 选题信息", "", *self._render_payload(topic),
                "## 当前研究或处理结果", "", *(self._render_payload(artifact) or ["- 当前没有研究结果。"]),
                "## 研究材料", "",
                *(research_material_lines or ["- 这个选题当前没有单独保存的研究材料。"]),
                *( ["", research_material_link] if research_material_link else [] ),
                "",
            ]
            (topic_dir / "选题与研究.md").write_text("\n".join(research_lines), encoding="utf-8")
            if has_manuscript:
                draft_dir.mkdir(parents=True, exist_ok=True)
                manual_dir.mkdir(parents=True, exist_ok=True)
                draft_links.append(f"- {_mirror_page_link('文稿与音频', '系统生成文稿', scope, folder, '系统生成文稿')}｜{scope}")
                manual_links.append(f"- {_mirror_page_link('文稿与音频', '人工改稿', scope, folder, '人工改稿')}｜{scope}")
                writing_versions = [
                    version for version in versions
                    if version.get("node") in writing_nodes
                ]
                current_writing = artifact if work.get("current_node") in writing_nodes else {}
                version_lines = [
                    f"# 系统生成文稿｜{title}", "",
                    "这里保存已经进入文稿阶段的系统生成内容。研究方案不放在这里。用户修改后的版本不写回这里。", "",
                    "## 当前版本", "", *(self._render_payload(current_writing) or ["- 当前没有正式文稿。"]),
                    "", "## 历史版本", "",
                ]
                for version in writing_versions:
                    version_lines.extend([
                        f"### {self._node_label(version.get('node'))}｜{version.get('created_at') or '未记录'}",
                        f"状态：{self._status_label(version.get('status'))}",
                        *self._render_payload(self._payload(version.get('artifact'))), "",
                    ])
                if not writing_versions:
                    version_lines.append("- 当前没有可展示的文稿版本。")
                (draft_dir / "系统生成文稿.md").write_text("\n".join(version_lines), encoding="utf-8")
                manual_page = manual_dir / "人工改稿.md"
                if not manual_page.exists():
                    manual_page.write_text(
                        "\n".join([
                            f"# 人工改稿｜{title}", "",
                            "这里专门保存用户实际修改后的文稿。系统只建立位置，不会用自动生成内容覆盖这里。", "",
                            f"对应系统生成文稿：{_mirror_page_link('文稿与音频', '系统生成文稿', scope, folder, '系统生成文稿')}",
                            "", "## 人工修改内容", "", "- 当前还没有记录人工修改内容。", "",
                        ]),
                        encoding="utf-8",
                    )
            if has_audio:
                audio_dir.mkdir(parents=True, exist_ok=True)
                audio_links.append(f"- {_mirror_page_link('文稿与音频', '音频', scope, folder, '音频')}｜{scope}")
                audio_lines = [f"# 音频｜{title}", "", "## 音频记录", ""]
                for attempt in work.get("audio_attempts") or []:
                    if isinstance(attempt, dict):
                        audio_lines.append(f"- 第 {attempt.get('attempt_number') or '?'} 次｜状态：{self._status_label(attempt.get('status'))}｜位置：{attempt.get('audio_ref') or '未记录'}")
                audio_lines.append("")
                (audio_dir / "音频.md").write_text("\n".join(audio_lines), encoding="utf-8")

        for position, item in enumerate(publications, start=1):
            publication = item.get("publication") if isinstance(item.get("publication"), dict) else {}
            publication_id = str(publication.get("publication_id") or f"发布记录-{position}")
            scope = _scope_for(publication)
            folder = f"{_directory_name(publication_id, fallback=f'发布记录-{position}')}｜{hashlib.sha256(publication_id.encode('utf-8')).hexdigest()[:8]}"
            publication_dir = feedback_root / _folder_name(scope) / folder
            publication_dir.mkdir(parents=True, exist_ok=True)
            publication_links.append(f"- {_mirror_link('发布与复盘', scope, folder, '发布与复盘')}｜{scope}")
            lines = [
                f"# 发布与复盘｜{publication_id}", "",
                f"适用范围：{scope}",
                f"平台：{self._platform_label(publication.get('platform'))}",
                f"发布时间：{publication.get('published_at') or '未记录'}",
                f"发布状态：{publication.get('status') or '未记录'}",
                f"外部链接：{publication.get('external_video_url') or '未记录'}", "",
                "## 表现数据", "",
            ]
            observations = item.get("observations") if isinstance(item.get("observations"), list) else []
            if observations:
                for observation in observations:
                    lines.append(
                        f"- {observation.get('point_code') or '未说明'}｜状态：{observation.get('observation_status') or '未说明'}｜时间：{observation.get('observed_at') or '未记录'}｜数据：{self._display_value(observation.get('metrics_json'))}"
                    )
            else:
                lines.append("- 当前没有表现数据。")
            lines.extend(["", "## 复盘结果", ""])
            reviews = item.get("reviews") if isinstance(item.get("reviews"), list) else []
            if reviews:
                for review in reviews:
                    lines.extend([
                        f"- 复盘状态：{review.get('status') or '未说明'}",
                        f"- 选题判断：{review.get('selection_assessment') or '未记录'}",
                        f"- 叙事判断：{review.get('narrative_assessment') or '未记录'}",
                        f"- 素材判断：{review.get('material_assessment') or '未记录'}",
                        f"- 外部条件：{review.get('external_conditions_assessment') or '未记录'}",
                        f"- 反馈候选：{self._display_value(review.get('feedback_candidate_json'))}", "",
                    ])
            else:
                lines.append("- 当前没有复盘结果。")
            (publication_dir / "发布与复盘.md").write_text("\n".join(lines), encoding="utf-8")

        (topic_root / "index.md").write_text("\n".join(["# 正式选题", "", *(topic_links or ["- 当前没有正式选题。"]), ""]), encoding="utf-8")
        (system_root / "index.md").write_text("\n".join(["# 系统生成文稿", "", "只有进入内容计划、初稿、优化稿、自然表达修改或最终审核阶段，才会出现在这里。研究方案不会被当成文稿。", "", *(draft_links or ["- 当前没有进入文稿阶段的内容。"]), ""]), encoding="utf-8")
        (manual_root / "index.md").write_text("\n".join(["# 人工改稿", "", "这里保留用户实际修改后的文稿，系统同步时不会覆盖人工改稿页面。只有已经有系统文稿的内容才会在这里建立对应位置。", "", *(manual_links or ["- 当前还没有进入人工改稿阶段的内容。"]), ""]), encoding="utf-8")
        (audio_root / "index.md").write_text("\n".join(["# 音频", "", "只有正式产生过音频记录的内容才会出现在这里。", "", *(audio_links or ["- 当前没有音频记录。"]), ""]), encoding="utf-8")
        (delivery_root / "index.md").write_text("\n".join(["# 文稿与音频", "", "文稿分成系统生成和人工改稿两条线，音频单独保存。", "", f"- {_mirror_link('文稿与音频', '系统生成文稿', 'index')}", f"- {_mirror_link('文稿与音频', '人工改稿', 'index')}", f"- {_mirror_link('文稿与音频', '音频', 'index')}", ""]), encoding="utf-8")
        (research_root / "index.md").write_text(
            "\n".join(
                [
                    "# 选题研究",
                    "",
                    f"- {_mirror_link('选题研究', '选题输入', 'index')}｜进入选题流程前的输入",
                    f"- {_mirror_link('选题研究', '候选选题', 'index')}｜系统已经形成、等待人工决定的候选",
                    f"- {_mirror_link('选题研究', '正式选题', 'index')}｜已经确认要做的选题",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (feedback_root / "index.md").write_text("\n".join(["# 发布与复盘", "", "这里保存正式发布登记、表现数据和复盘结果。", "", *(publication_links or ["- 当前没有正式发布记录。"]), ""]), encoding="utf-8")
        (archive_root / "index.md").write_text("# 归档\n\n这里保存已经取消、失效或被新版本替代的内容。\n", encoding="utf-8")

    def _write_clean_home(self, *, staging_root: Path) -> None:
        home_root = _folder_path(staging_root, "首页")
        home_root.mkdir(parents=True, exist_ok=True)
        foundation_root = _folder_path(staging_root, "项目基础")
        foundation_root.mkdir(parents=True, exist_ok=True)
        common_root = foundation_root / _folder_name("通用")
        common_root.mkdir(parents=True, exist_ok=True)
        (common_root / "分层和写入说明.md").write_text(
            "\n".join([
                "# 分层和写入说明", "",
                "原始素材把来源和基础数据放在一起，把转写文案和评论放在一起；内容整理只保存内容拆解；选题研究依次区分选题输入、真正候选选题、正式选题和该选题自己的研究材料；文稿与音频只保存真正产生的文稿、人工改稿和音频；可复用经验保存待确认经验、依据材料和已确认经验。", "",
                "系统每次同步都按正式记录的适用范围找位置，不根据文件名猜分类。检索标签、热点、问题拓展和用户提出方向只是输入，不会冒充候选选题；只有正式候选表里的记录才会进入候选选题区。没有转写、评论、拆解、研究材料、文稿或音频时，会明确显示‘当前没有’，不会把别的资料顶替过来。", "",
            ]),
            encoding="utf-8",
        )
        (home_root / "首页.md").write_text(
            "\n".join([
                f"# {KNOWLEDGE_BASE_NAME}", "",
                "这里是自媒体内容创作知识库首页。", "",
                "## 从这里开始", "",
                f"- {_mirror_link('项目基础', 'index')}",
                f"- {_mirror_link('原始素材', 'index')}",
                f"- {_mirror_link('内容整理', 'index')}",
                f"- {_mirror_link('选题研究', 'index')}",
                f"- {_mirror_link('文稿与音频', 'index')}",
                f"- {_mirror_link('发布与复盘', 'index')}",
                f"- {_mirror_link('可复用经验', 'index')}", "",
                "## 资料关系", "",
                "来源与基础信息是起点；文案与评论属于同一条来源的文字材料；内容整理是加工结果；研究材料只属于某个正式选题；人工改稿只放在人工改稿区。", "",
            ]),
            encoding="utf-8",
        )

    def _write_source_records(
        self,
        *,
        staging_root: Path,
        records: list[dict[str, Any]],
    ) -> None:
        raw_root = _folder_path(staging_root, "原始素材")
        source_root = _folder_path(raw_root, "全部来源")
        source_root.mkdir(parents=True, exist_ok=True)
        domain_links: dict[str, list[str]] = {}
        account_links: dict[tuple[str, str], list[str]] = {}
        for position, record in enumerate(records, start=1):
            domain = _domain_name(str(record.get("domain_label") or ""))
            account = _directory_name(str(record.get("account_name") or "未登记账号"), fallback="未登记账号")
            title = str(record.get("title") or record.get("source_id") or "未命名来源").strip()
            source_id = str(record.get("source_id") or "未命名来源")
            filename = _page_name(f"{source_id}｜{title}", position=position)
            source_dir = source_root / domain / account
            source_dir.mkdir(parents=True, exist_ok=True)
            link = _mirror_link("原始素材", "全部来源", domain, account, filename[:-3])
            domain_links.setdefault(domain, []).append(f"- {link}")
            account_links.setdefault((domain, account), []).append(f"- {link}")

            source_link_label = self._source_link_label(record)
            lines = [
                f"# 来源：{title}", "",
                f"领域：{domain}",
                f"账号：{account}",
                f"来源编号：{source_id}",
                f"平台：{record.get('platform') or '未记录'}",
                f"平台内容编号：{record.get('platform_item_id') or '未记录'}",
                f"发布时间：{record.get('publish_time') or '未记录'}",
                f"原始归档位置：{record.get('raw_archive_ref') or '未记录'}",
                f"是否排除：{record.get('excluded_reason') or '未排除'}",
                f"是否完成持续跟踪：{'是' if record.get('tracking_completed') else '否'}", "",
                f"## {source_link_label}", "",
                f"- [打开{source_link_label.replace('链接', '')}]({record.get('url')})" if record.get("url") else f"- 当前没有可点击的{source_link_label}", "",
                "## 当前指标", "",
            ]
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
            lines.extend(f"- {key}：{value}" for key, value in metrics.items())
            if not metrics:
                lines.append("- 当前没有保存可展示的指标")

            lines.extend(["", "## 高信号记录", ""])
            hits = record.get("hits") if isinstance(record.get("hits"), list) else []
            for hit in hits:
                lines.extend([
                    f"### {hit.get('hit_id') or '未命名高信号记录'}",
                    f"- 命中渠道：{hit.get('hit_channel') or '未记录'}",
                    f"- 判断把握：{hit.get('judgment_confidence') or '未记录'}",
                    f"- 准备状态：{hit.get('preparation_status') or '未记录'}",
                    f"- 进入高信号时间：{hit.get('promoted_at') or '未记录'}",
                ])
            if not hits:
                lines.append("- 当前没有进入高信号记录")

            lines.extend(["", "## 转写记录", ""])
            transcripts = record.get("transcripts") if isinstance(record.get("transcripts"), list) else []
            for transcript in transcripts:
                lines.extend([
                    f"### 第 {transcript.get('version') or '?'} 版转写",
                    f"- 处理状态：{transcript.get('processing_status') or '未记录'}",
                    f"- 转写模型：{transcript.get('asr_model') or '未记录'}",
                    "",
                    "#### 清理后的文字",
                    "",
                    transcript.get("cleaned_text") or transcript.get("raw_text") or "- 当前没有转写文字",
                ])
            if not transcripts:
                lines.append("- 当前没有转写记录")

            lines.extend(["", "## 评论材料", ""])
            comments = record.get("comments") if isinstance(record.get("comments"), list) else []
            for comment in comments:
                text = str(comment.get("text") or "").strip()
                if text:
                    lines.append(
                        f"> {text}（点赞：{comment.get('like_count') or 0}，样本序号：{comment.get('sample_rank') or '未记录'}）"
                    )
            if not comments:
                lines.append("- 当前没有评论材料")

            lines.extend(["", "## 内容分析", ""])
            analyses = record.get("deep_analysis") if isinstance(record.get("deep_analysis"), list) else []
            analysis_labels = {
                "topic_pattern": "主题模式",
                "hook_pattern": "开头模式",
                "structure_pattern": "结构模式",
                "model_name": "分析模型",
                "created_at": "分析时间",
            }
            for analysis in analyses:
                lines.append(f"### 第 {analysis.get('version') or '?'} 版分析")
                for key, label in analysis_labels.items():
                    if analysis.get(key) not in (None, "", [], {}):
                        lines.append(f"- {label}：{self._display_value(analysis.get(key))}")
            if not analyses:
                lines.append("- 当前没有内容分析记录")

            lines.extend(["", "## 采集检查", ""])
            checks = record.get("checks") if isinstance(record.get("checks"), list) else []
            for check in checks:
                lines.append(
                    f"- {check.get('checked_at') or '未记录'}｜第 {check.get('day_since_publish') or '?'} 天｜指标：{self._display_value(check.get('metrics'))}"
                )
            if not checks:
                lines.append("- 当前没有持续采集检查记录")

            lines.extend(["", "## 账号基线", ""])
            baselines = record.get("baselines") if isinstance(record.get("baselines"), list) else []
            for baseline in baselines:
                lines.append(
                    f"- {baseline.get('metric') or '未说明'}｜观察点：{baseline.get('observation_point') or '未说明'}｜样本数：{baseline.get('sample_count') or 0}｜中位数：{baseline.get('median_value') or '未记录'}"
                )
            if not baselines:
                lines.append("- 当前没有对应账号基线")
            lines.append("")
            (source_dir / filename).write_text("\n".join(lines), encoding="utf-8")

        for domain, links in domain_links.items():
            domain_dir = source_root / domain
            domain_dir.mkdir(parents=True, exist_ok=True)
            (domain_dir / "index.md").write_text(
                "\n".join([f"# {domain}全部来源", "", "这里列出该领域的所有正式来源，包括尚未整理完成的来源。", "", *links, ""]),
                encoding="utf-8",
            )
        for (domain, account), links in account_links.items():
            account_dir = source_root / domain / account
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "index.md").write_text(
                "\n".join([f"# {account}的全部来源", "", *links, ""]),
                encoding="utf-8",
            )
        root_links = [f"- {_mirror_link('原始素材', '全部来源', domain, 'index')}" for domain in sorted(domain_links)]
        (source_root / "index.md").write_text(
            "\n".join([
                "# 全部来源", "",
                f"当前共保留 {len(records)} 条来源记录。无论是否已经整理完成，都先在这里保留来源、状态和可追溯信息。", "",
                *(root_links or ["- 当前没有来源记录"]), "",
            ]),
            encoding="utf-8",
        )
        (raw_root / "index.md").write_text(
            "\n".join([
                "# 原始素材", "",
                "这里保存所有采集到的来源、用户提供的原始内容和来源状态。原始素材不等于经验，也不等于正式选题。", "",
                f"- {_mirror_link('原始素材', '全部来源', 'index')}",
                f"- {_mirror_link('原始素材', '用户提供', 'index')}",
                f"- 来源总数：{len(records)} 条", "",
            ]),
            encoding="utf-8",
        )

    def _write_selection_inputs(
        self,
        *,
        staging_root: Path,
        inputs: dict[str, list[dict[str, Any]]],
    ) -> dict[str, int]:
        input_root = _folder_path(staging_root, "选题研究", "选题输入")
        input_root.mkdir(parents=True, exist_ok=True)
        hotspots = inputs.get("hotspots") or []
        questions = inputs.get("question_sources") or []
        directions = inputs.get("saved_directions") or []
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        excluded_without_domain = 0
        for key, values in (("hotspots", hotspots), ("questions", questions), ("directions", directions)):
            for item in values:
                scope = _scope_for(item)
                if scope == "通用":
                    excluded_without_domain += 1
                    continue
                grouped.setdefault(scope, {}).setdefault(key, []).append(item)

        scope_links: list[str] = []
        for scope in sorted(grouped):
            scope_folder = _folder_name(scope)
            scope_root = input_root / scope_folder
            scope_root.mkdir(parents=True, exist_ok=True)
            scoped = grouped[scope]
            scoped_hotspots = scoped.get("hotspots", [])
            scope_index_links: list[str] = []
            if scoped_hotspots:
                hotspot_lines = [
                    f"# 热点记录｜{scope}", "",
                    f"适用范围：{scope}",
                    "热点只是选题输入，不等于候选选题，也不等于正式选题。", "",
                ]
                for item in scoped_hotspots:
                    hotspot_lines.append(
                        f"- {item.get('observed_at') or '未记录'}｜{item.get('title') or '未命名热点'}｜来源：{item.get('source_channel') or '未说明'}｜排名：{item.get('source_rank') or '未记录'}｜[打开来源]({item.get('url')})" if item.get("url") else
                        f"- {item.get('observed_at') or '未记录'}｜{item.get('title') or '未命名热点'}｜来源：{item.get('source_channel') or '未说明'}｜排名：{item.get('source_rank') or '未记录'}"
                    )
                (scope_root / "热点记录.md").write_text("\n".join(hotspot_lines + [""]), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('选题研究', '选题输入', scope, '热点记录')}")

            scoped_questions = scoped.get("questions", [])
            if scoped_questions:
                question_lines = [
                    f"# 问题拓展｜{scope}", "",
                    f"适用范围：{scope}",
                    "这些是从来源材料中整理出来的问题，不是已经形成的候选选题。", "",
                ]
                for item in scoped_questions:
                    question_lines.extend([
                        f"## {item.get('core_question') or '未命名问题'}",
                        f"- 验证结果：{item.get('validation_outcome') or '未说明'}",
                        f"- 验证时间：{item.get('validated_at') or '未记录'}",
                        f"- 来源依据：{self._display_value(item.get('parent_source_ref_json'))}", "",
                    ])
                (scope_root / "问题拓展.md").write_text("\n".join(question_lines + [""]), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('选题研究', '选题输入', scope, '问题拓展')}")

            scoped_directions = scoped.get("directions", [])
            if scoped_directions:
                direction_lines = [
                    f"# 用户提出方向｜{scope}", "",
                    f"适用范围：{scope}",
                    "这些是用户提出的方向，还没有自动变成候选选题。", "",
                ]
                for item in scoped_directions:
                    direction_lines.append(
                        f"- {item.get('core_question') or '未命名方向'}｜状态：{item.get('status') or '未说明'}｜保存时间：{item.get('saved_at') or '未记录'}"
                    )
                (scope_root / "用户提出方向.md").write_text("\n".join(direction_lines + [""]), encoding="utf-8")
                scope_index_links.append(f"- {_mirror_link('选题研究', '选题输入', scope, '用户提出方向')}")
            (scope_root / "index.md").write_text(
                "\n".join([
                    f"# 选题输入｜{scope}", "", f"适用范围：{scope}",
                    "这些内容只是进入选题流程的输入，必须经过候选判断或用户明确确认，不能直接进入研究和写稿。", "",
                    *(scope_index_links or ["- 当前没有选题输入。"]), "",
                ]),
                encoding="utf-8",
            )
            scope_links.append(f"- {_mirror_link('选题研究', '选题输入', scope, 'index')}｜{scope}")

        (input_root / "index.md").write_text(
            "\n".join([
                "# 选题输入", "",
                "这里保存进入选题流程前的输入。它们不是候选选题，也不是正式选题。", "",
                f"当前展示：{sum(len(group.get('hotspots', [])) for group in grouped.values())} 条热点、{sum(len(group.get('questions', [])) for group in grouped.values())} 条问题拓展、{sum(len(group.get('directions', [])) for group in grouped.values())} 条用户提出方向。", "",
                *(scope_links or ["- 当前没有可进入选题流程的输入。"]), "",
            ]),
            encoding="utf-8",
        )
        return {
            "hotspots": sum(len(group.get("hotspots", [])) for group in grouped.values()),
            "questions": sum(len(group.get("questions", [])) for group in grouped.values()),
            "directions": sum(len(group.get("directions", [])) for group in grouped.values()),
            "excluded_without_domain": excluded_without_domain,
        }

    def _write_candidate_pool(
        self,
        *,
        staging_root: Path,
        candidates: list[dict[str, Any]],
    ) -> None:
        candidate_root = _folder_path(staging_root, "选题研究", "候选选题")
        candidate_root.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[str]] = {}
        for position, item in enumerate(candidates, start=1):
            scope = _scope_name(item.get("domain_label"))
            if scope == "通用":
                continue
            payload = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
            title = str(payload.get("title") or payload.get("candidate_topic") or item.get("candidate_id") or f"候选选题-{position}")
            filename = _page_name(
                f"{item.get('candidate_version_id') or position}｜{title}",
                position=position,
                prefix="候选",
            )
            scope_folder = _folder_name(scope)
            page = candidate_root / scope_folder / filename
            page.parent.mkdir(parents=True, exist_ok=True)
            link = _mirror_link("选题研究", "候选选题", scope, filename[:-3])
            grouped.setdefault(scope, []).append(f"- {link}｜{title}")
            assessment = item.get("assessment") if isinstance(item.get("assessment"), dict) else {}
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            support_materials = item.get("support_materials") if isinstance(item.get("support_materials"), list) else []
            lines = [
                f"# 候选选题｜{title}", "",
                f"适用范围：{scope}",
                "当前状态：等待你决定",
                f"形成时间：{item.get('created_at') or '未记录'}", "",
                "## 候选内容", "",
                *(self._render_payload(payload) or ["- 当前没有候选内容。"]),
                "## 判断信息", "",
                f"- 来源类型：{item.get('source_type') or '未记录'}",
                f"- 来源时间：{item.get('source_time') or '未记录'}",
                f"- 评分：{assessment.get('total_score') if assessment.get('total_score') is not None else '未评分'}",
                f"- 评分时间：{assessment.get('assessed_at') or '未记录'}", "",
                "## 主要来源", "",
                f"- {self._display_value(source) if source else '当前没有保存主要来源。'}", "",
                "## 其他支持材料", "",
            ]
            if support_materials:
                for support in support_materials:
                    lines.append(
                        f"- {support.get('source_type') or '未记录'}｜{support.get('reason') or '未说明'}｜{self._display_value(support.get('source'))}"
                    )
            else:
                lines.append("- 当前没有其他支持材料。")
            lines.extend([
                "", "## 下一步", "",
                "你确认后，系统才会把这个候选选题转为正式选题；未确认前不会进入研究、文稿或音频。", "",
            ])
            page.write_text("\n".join(lines), encoding="utf-8")

        scope_links: list[str] = []
        for scope in sorted(grouped):
            scope_folder = _folder_name(scope)
            (candidate_root / scope_folder / "index.md").write_text(
                "\n".join([
                    f"# 候选选题｜{scope}", "",
                    "这里列出系统已经判断过、等待你决定的候选选题。", "",
                    *grouped[scope], "",
                ]),
                encoding="utf-8",
            )
            scope_links.append(f"- {_mirror_link('选题研究', '候选选题', scope, 'index')}｜{scope}")
        (candidate_root / "index.md").write_text(
            "\n".join([
                "# 候选选题", "",
                "这里才是真正的候选选题。选题输入不会直接出现在这里，正式选题也不会重复放在这里。", "",
                *(scope_links or ["- 当前没有等待你决定的候选选题。"]), "",
            ]),
            encoding="utf-8",
        )

    def _write_manual_sources(
        self,
        *,
        staging_root: Path,
        sources: list[dict[str, Any]],
    ) -> None:
        source_root = _folder_path(staging_root, "原始素材", "用户提供")
        source_root.mkdir(parents=True, exist_ok=True)
        links: list[str] = []
        for position, source in enumerate(sources, start=1):
            title = str(source.get("manual_source_id") or f"用户来源-{position}")
            filename = _page_name(title, position=position)
            scope = _scope_for(source)
            page = source_root / _folder_name(scope) / filename
            lines = [
                f"# 用户提供来源：{title}", "",
                f"适用范围：{scope}",
                f"来源类型：{source.get('source_kind') or '未说明'}",
                f"提交人：{source.get('submitted_by') or '未记录'}",
                f"提交时间：{source.get('submitted_at') or '未记录'}",
                "", "## 原始内容", "", self._display_value(source.get("original_content")), "",
                "## 后续探索", "",
            ]
            exploration = source.get("exploration")
            if exploration:
                lines.extend([
                    f"- 探索类型：{exploration.get('exploration_kind') or '未说明'}",
                    f"- 当前状态：{exploration.get('status') or '未说明'}",
                    f"- 材料数量：{exploration.get('material_count') or 0}", "",
                ])
            else:
                lines.append("- 当前没有关联探索任务。")
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("\n".join(lines), encoding="utf-8")
            links.append(f"- {_mirror_link('原始素材', '用户提供', scope, filename[:-3])}｜{scope}")
        (source_root / "index.md").write_text(
            "\n".join(["# 用户提供", "", *(links or ["- 当前没有用户提供来源。"]), ""]),
            encoding="utf-8",
        )

    def _write_experience_candidates(
        self,
        *,
        staging_root: Path,
        candidates: list[dict[str, Any]],
    ) -> None:
        candidate_root = _folder_path(staging_root, "可复用经验", "历史候选")
        candidate_root.mkdir(parents=True, exist_ok=True)
        grouped: dict[tuple[str, str], list[str]] = {}
        for position, candidate in enumerate(candidates, start=1):
            status = str(candidate.get("status") or "未说明")
            scope = _scope_for(candidate)
            proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
            content = proposal.get("candidate") if isinstance(proposal.get("candidate"), dict) else proposal
            summary = str(content.get("summary") or f"{scope}第{position:03d}条历史候选")
            filename = _page_name(
                summary,
                position=position,
                prefix="历史候选",
            )
            scope_folder = _folder_name(scope)
            page = candidate_root / scope_folder / filename
            page.parent.mkdir(parents=True, exist_ok=True)
            failure = candidate.get("failure") or {}
            page.write_text(
                "\n".join([
                    f"# 历史经验候选｜{summary}", "",
                    f"适用范围：{scope}",
                    f"当前状态：{status}",
                    f"来源数量：{candidate.get('source_count') or 0}",
                    "", "## 候选内容", "", self._display_value(proposal),
                    "", "## 失败或未形成候选的原因", "", self._display_value(failure), "",
                ]),
                encoding="utf-8",
            )
            grouped.setdefault((scope, status), []).append(f"- {_mirror_link('可复用经验', '历史候选', scope, filename[:-3])}")
        index_lines = ["# 历史经验候选", "", f"当前保留 {len(candidates)} 条候选记录，包括等待确认、未形成候选和失败记录；按领域分开查看。", ""]
        for scope in sorted({key[0] for key in grouped}):
            domain_links: list[str] = []
            for (item_domain, status), links in sorted(grouped.items()):
                if item_domain != scope:
                    continue
                domain_links.extend([f"## {status}", "", *links, ""])
            scope_folder = _folder_name(scope)
            (candidate_root / scope_folder / "index.md").write_text("\n".join([f"# 历史经验候选｜{scope}", "", *domain_links]), encoding="utf-8")
            index_lines.extend([f"- {_mirror_link('可复用经验', '历史候选', scope, 'index')}｜{scope}", ""])
        if not candidates:
            index_lines.append("- 当前没有历史经验候选。")
        (candidate_root / "index.md").write_text("\n".join(index_lines), encoding="utf-8")

    def _write_knowledge_catalog(
        self,
        *,
        staging_root: Path,
        counts: dict[str, int],
    ) -> None:
        system_root = _folder_path(staging_root, "系统记录")
        system_root.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 知识库内容清单", "",
            "这里列出系统当前需要长期保留、可以影响后续运行或需要人工追溯的业务数据。", "",
            "## 已纳入知识库", "",
        ]
        for label, count in counts.items():
            lines.append(f"- {label}：{count} 条")
        lines.extend([
            "", "## 不直接放入知识库的系统记录", "",
            "- 调度任务、模型调用账单、底层审计日志、命令回执和内部技术追踪继续保留在正式数据库。",
            "- 这些记录仍然是系统运行数据，但不作为日常知识库内容展示。",
            "- 知识库页面只保留会影响业务判断、运行状态、来源追溯和人工确认的内容。", "",
        ])
        (system_root / "04 数据统计.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_content_workbench(
        self,
        *,
        staging_root: Path,
        workbench: list[dict[str, Any]],
        publications: list[dict[str, Any]],
    ) -> None:
        research_root = _folder_path(staging_root, "选题研究")
        delivery_root = _folder_path(staging_root, "文稿与音频")
        feedback_root = _folder_path(staging_root, "发布与复盘")
        archive_root = _folder_path(staging_root, "归档")
        for root in (research_root, delivery_root, feedback_root, archive_root):
            root.mkdir(parents=True, exist_ok=True)
        topic_links: list[str] = []
        delivery_links: list[str] = []
        publication_links: list[str] = []
        for position, work in enumerate(workbench, start=1):
            topic = self._payload(work.get("topic"))
            artifact = self._payload(work.get("current_artifact"))
            title = str(topic.get("title") or topic.get("core_question") or f"未命名选题 {position}").strip()
            domain = _domain_name(str(topic.get("domain_label") or topic.get("domain") or ""))
            folder = f"{position:03d}-{_directory_name(title, fallback='未命名选题')}"
            topic_dir = research_root / domain / folder
            delivery_dir = delivery_root / domain / folder
            topic_dir.mkdir(parents=True, exist_ok=True)
            delivery_dir.mkdir(parents=True, exist_ok=True)
            topic_links.append(f"- {_mirror_link('选题研究', domain, folder, '选题与研究')}")
            delivery_links.append(f"- {_mirror_link('文稿与音频', domain, folder, '文稿与音频')}")

            research_lines = [
                f"# 选题与研究：{title}", "", f"领域：{domain}",
                f"当前阶段：{self._node_label(work.get('current_node'))}",
                f"当前状态：{self._status_label(work.get('current_status'))}", "",
                "## 选题信息", "",
            ]
            research_lines.extend(self._render_payload(topic))
            research_lines.extend(["## 当前研究或处理结果", ""])
            research_lines.extend(self._render_payload(artifact) or ["- 当前没有可展示的研究结果。", ""])
            research_lines.extend(["## 研究材料", ""])
            materials = work.get("research_materials") or []
            for material in materials:
                if isinstance(material, dict):
                    research_lines.append(
                        f"- {material.get('title') or '未命名材料'}｜{material.get('evidence_role') or '未说明'}｜{material.get('source_ref') or '无来源链接'}"
                    )
            if not materials:
                research_lines.append("- 当前没有正式研究材料。")
            research_lines.append("")
            (topic_dir / "选题与研究.md").write_text("\n".join(research_lines), encoding="utf-8")

            delivery_lines = [
                f"# 文稿与音频：{title}", "", f"领域：{domain}",
                f"当前阶段：{self._node_label(work.get('current_node'))}",
                f"当前状态：{self._status_label(work.get('current_status'))}", "",
                "## 当前内容版本", "",
            ]
            delivery_lines.extend(self._render_payload(artifact) or ["- 当前没有正式内容版本。", ""])
            delivery_lines.extend(["## 音频交付", ""])
            audio_attempts = work.get("audio_attempts") or []
            for attempt in audio_attempts:
                if not isinstance(attempt, dict):
                    continue
                delivery_lines.append(
                    f"- 第 {attempt.get('attempt_number') or '?'} 次｜状态：{self._status_label(attempt.get('status'))}｜标题：{attempt.get('title') or title}"
                )
                if attempt.get("audio_ref"):
                    delivery_lines.append(f"  - 音频位置：{attempt['audio_ref']}")
            if not audio_attempts:
                delivery_lines.append("- 当前没有音频交付记录。")
            delivery_lines.append("")
            (delivery_dir / "文稿与音频.md").write_text("\n".join(delivery_lines), encoding="utf-8")

        (research_root / "index.md").write_text(
            "\n".join([
                "# 选题研究", "",
                f"- {_mirror_link('选题研究', '选题输入', 'index')}",
                *(topic_links or ["- 当前没有正式选题。"]), "",
            ]),
            encoding="utf-8",
        )
        (delivery_root / "index.md").write_text(
            "\n".join(["# 文稿与音频", "", *(delivery_links or ["- 当前没有正式内容交付。"]), ""]),
            encoding="utf-8",
        )
        for position, item in enumerate(publications, start=1):
            publication = item.get("publication") if isinstance(item.get("publication"), dict) else {}
            domain = _domain_name(str(publication.get("domain_label") or ""))
            publication_id = str(publication.get("publication_id") or f"发布记录-{position}")
            folder = _directory_name(publication_id, fallback=f"发布记录-{position}")
            publication_dir = feedback_root / domain / folder
            publication_dir.mkdir(parents=True, exist_ok=True)
            filename = "发布与复盘.md"
            publication_links.append(f"- {_mirror_link('发布与复盘', domain, folder, filename[:-3])}")
            lines = [
                f"# 发布与复盘：{publication_id}", "",
                f"领域：{domain}",
                f"平台：{publication.get('platform') or '未记录'}",
                f"发布时间：{publication.get('published_at') or '未记录'}",
                f"发布状态：{publication.get('status') or '未记录'}",
                f"外部链接：{publication.get('external_video_url') or '未记录'}", "",
                "## 表现数据", "",
            ]
            observations = item.get("observations") if isinstance(item.get("observations"), list) else []
            for observation in observations:
                lines.append(
                    f"- {observation.get('point_code') or '未说明'}｜状态：{observation.get('observation_status') or '未说明'}｜时间：{observation.get('observed_at') or '未记录'}｜数据：{self._display_value(observation.get('metrics_json'))}"
                )
            if not observations:
                lines.append("- 当前没有表现数据")
            lines.extend(["", "## 复盘结果", ""])
            reviews = item.get("reviews") if isinstance(item.get("reviews"), list) else []
            for review in reviews:
                lines.extend([
                    f"- 复盘状态：{review.get('status') or '未说明'}",
                    f"- 选题判断：{review.get('selection_assessment') or '未记录'}",
                    f"- 叙事判断：{review.get('narrative_assessment') or '未记录'}",
                    f"- 素材判断：{review.get('material_assessment') or '未记录'}",
                    f"- 外部条件：{review.get('external_conditions_assessment') or '未记录'}",
                    f"- 反馈候选：{self._display_value(review.get('feedback_candidate_json'))}", "",
                ])
            if not reviews:
                lines.append("- 当前没有复盘结果")
            (publication_dir / filename).write_text("\n".join(lines), encoding="utf-8")
        (feedback_root / "index.md").write_text(
            "\n".join([
                "# 发布与复盘", "",
                "这里保留用户完成外部发布后登记的发布记录、表现数据和复盘结果。", "",
                *(publication_links or ["- 当前正式系统还没有可展示的发布反馈记录。"]),
                "- 对标内容表现与自有内容表现分开保存，不能混算。", "",
            ]),
            encoding="utf-8",
        )
        (archive_root / "index.md").write_text(
            "\n".join([
                "# 归档", "",
                "这里保存已经取消、失效或被新版本替代的内容。归档内容不再自动用于当前创作。", "",
            ]),
            encoding="utf-8",
        )

    def _write_prepared_material_cards(
        self,
        *,
        staging_root: Path,
        materials: list[dict[str, Any]],
    ) -> tuple[dict[str, str], int, int]:
        """Write one canonical card per prepared source and its current breakdown."""
        raw_root = _folder_path(staging_root, "原始素材")
        material_root = _folder_path(staging_root, "整理材料")
        raw_root.mkdir(parents=True, exist_ok=True)
        material_root.mkdir(parents=True, exist_ok=True)
        page_names: dict[str, str] = {}
        domain_links: dict[str, list[str]] = {}
        account_links: dict[tuple[str, str], list[str]] = {}
        status_counts: dict[str, int] = {}
        relationship_count = 0
        for position, material in enumerate(materials, start=1):
            domain = _domain_name(str(material.get("domain_label") or "").strip())
            account = _directory_name(
                str(material.get("account_name") or "未命名账号").strip(),
                fallback="未命名账号",
            )
            material_status = {
                "completed": "已完成整理",
                "failed": "整理失败",
            }.get(str(material.get("material_status") or ""), "待整理")
            breakdown_status = {
                "completed": "已完成拆解",
                "failed": "拆解失败",
                "pending": "待拆解",
            }.get(str(material.get("breakdown_status") or "pending"), "待拆解")
            status_dir = material_root / domain / account
            status_dir.mkdir(parents=True, exist_ok=True)
            title = str(material.get("title") or material.get("source_id") or "未命名视频").strip()
            source_id = str(material.get("source_id") or "").strip()
            filename = _page_name(f"{account}｜{title}｜{source_id}", position=position)
            page_names[source_id] = f"整理材料/{domain}/{account}/{filename[:-3]}"
            page_link = _mirror_link("整理材料", domain, account, filename[:-3])
            domain_links.setdefault(domain, []).append(f"- {page_link}")
            account_links.setdefault((domain, account), []).append(f"- {page_link}")
            status_counts[material_status] = status_counts.get(material_status, 0) + 1

            material_payload = material.get("material") if isinstance(material.get("material"), dict) else {}
            transcript = ""
            transcript_ref = str(material_payload.get("transcript_ref") or "").strip()
            if transcript_ref:
                try:
                    transcript = Path(transcript_ref).read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError):
                    transcript = ""
            comments = material_payload.get("comments") if isinstance(material_payload.get("comments"), list) else []
            breakdown = material.get("breakdown") if isinstance(material.get("breakdown"), dict) else {}
            deep_breakdown = breakdown.get("deep_breakdown") if isinstance(breakdown.get("deep_breakdown"), dict) else {}
            lines = [
                f"# 整理材料：{title}",
                "",
                f"领域：{domain}",
                f"账号：{account}",
                f"来源编号：{source_id}",
                f"整理状态：{material_status}",
                f"拆解状态：{breakdown_status}",
                f"整理更新时间：{material.get('material_updated_at') or '未记录'}",
                f"拆解更新时间：{material.get('breakdown_updated_at') or '未记录'}",
                "",
                "## 原始素材",
                "",
            ]
            url = str(material.get("url") or "").strip()
            lines.append(f"- [打开原文]({url})" if url else "- 当前没有可点击的原文地址")
            lines.extend(["", "## 原始指标", ""])
            metrics = material.get("metrics") if isinstance(material.get("metrics"), dict) else {}
            if metrics:
                lines.extend(f"- {key}：{value}" for key, value in sorted(metrics.items()))
            else:
                lines.append("- 当前没有保存可展示的指标")
            lines.extend(["", "## 转写文字", ""])
            lines.extend(transcript.splitlines() if transcript else ["- 当前没有可读取的转写文本"])
            lines.extend(["", "## 评论材料", ""])
            rendered_comments = 0
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                text = str(comment.get("text") or comment.get("content") or "").strip()
                if text:
                    lines.append(f"> {text}")
                    rendered_comments += 1
            if not rendered_comments:
                lines.append("- 当前没有保存可展示的评论材料")
            material_error = material.get("material_error") if isinstance(material.get("material_error"), dict) else {}
            if material_error:
                lines.extend(["", "## 整理问题", ""])
                lines.extend(f"- {key}：{value}" for key, value in sorted(material_error.items()))
            lines.extend(["", "## 内容拆解", ""])
            if deep_breakdown:
                lines.extend(self._render_breakdown_payload(deep_breakdown))
            elif breakdown_status == "failed":
                breakdown_error = material.get("breakdown_error") if isinstance(material.get("breakdown_error"), dict) else {}
                lines.append("- 这条材料的拆解没有通过，保留失败原因，不自动伪装成完成")
                lines.extend(f"- {key}：{value}" for key, value in sorted(breakdown_error.items()))
            else:
                lines.append("- 备料已完成，拆解尚未完成")
            lines.extend(
                [
                    "",
                    "## 使用边界",
                    "",
                    "- 这张卡是整理结果，不等于正式经验。",
                    "- 只有经过人工确认的经验，才会进入经验区供后续复用。",
                    "",
                ]
            )
            (status_dir / filename).write_text("\n".join(lines), encoding="utf-8")
            raw_dir = raw_root / domain / account
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_lines = [
                f"# 原始素材：{title}",
                "",
                f"领域：{domain}",
                f"账号：{account}",
                f"来源编号：{source_id}",
                f"来源时间：{material.get('publish_time') or '未记录'}",
                f"整理状态：{material_status}",
                "",
                "## 原文入口",
                "",
                f"- [打开原文]({url})" if url else "- 当前没有可点击的原文地址",
                "",
                "## 原始指标",
                "",
            ]
            raw_lines.extend(f"- {key}：{value}" for key, value in sorted(metrics.items()) or [("当前状态", "没有保存可展示的指标")])
            raw_lines.extend(["", "## 与整理材料的关系", "", f"- {_mirror_link('整理材料', domain, account, filename[:-3])}", ""])
            (raw_dir / filename).write_text("\n".join(raw_lines), encoding="utf-8")
            relationship_count += 1

        for domain, links in domain_links.items():
            domain_dir = material_root / domain
            domain_dir.mkdir(parents=True, exist_ok=True)
            (domain_dir / "index.md").write_text(
                "\n".join([f"# {domain}整理材料", "", "按账号查看整理结果。当前状态写在每张卡片里。", "", *links, ""]),
                encoding="utf-8",
            )
        for (domain, account), links in account_links.items():
            account_dir = material_root / domain / account
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "index.md").write_text(
                "\n".join([f"# {account}整理材料", "", "以下是这个账号的整理材料，页面会随着整理和拆解状态更新。", "", *links, ""]),
                encoding="utf-8",
            )
        root_links = [
            f"- {_mirror_link('整理材料', domain, 'index')}"
            for domain in sorted(domain_links)
        ]
        (material_root / "index.md").write_text(
            "\n".join(
                [
                    "# 整理材料",
                    "",
                    "这里保存已经进入整理流程的材料。分类顺序是领域 → 账号；具体状态写在每张材料卡里。",
                    "",
                    f"- 已生成原料卡：{len(page_names)} 条",
                    *[f"- {key}：{value} 条" for key, value in sorted(status_counts.items())],
                    "",
                    "## 按领域查看",
                    "",
                    *(root_links or ["- 当前没有进入备料的材料"]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        raw_root_links = [
            f"- {_mirror_link('原始素材', domain, 'index')}"
            for domain in sorted(domain_links)
        ]
        for domain in sorted(domain_links):
            raw_domain_dir = raw_root / domain
            raw_domain_dir.mkdir(parents=True, exist_ok=True)
            raw_domain_dir.joinpath("index.md").write_text(
                "\n".join(
                    [
                        f"# {domain}原始素材",
                        "",
                        "这里保存系统整理出的原始来源卡。转写、评论和拆解结果请到‘整理材料’查看。",
                        "",
                        f"- {_mirror_link('整理材料', domain, 'index')}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        (raw_root / "index.md").write_text(
            "\n".join(
                [
                    "# 原始素材",
                    "",
                    "这里保存采集到的原始来源和用户提供的原始材料。原始素材不等于经验，也不等于研究结论。",
                    "",
                    f"- 原始素材卡：{len(page_names)} 条",
                    "",
                    "## 按领域查看",
                    "",
                    *(raw_root_links or ["- 当前没有原始素材"]),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return page_names, len(page_names), relationship_count

    def _render_breakdown_payload(self, breakdown: dict[str, Any]) -> list[str]:
        lines = [
            f"- 内容主题类型：{breakdown.get('content_subject_type') or '未说明'}",
            f"- 表达形式：{breakdown.get('expression_form') or '未说明'}",
        ]
        assessment = breakdown.get("structure_assessment") if isinstance(breakdown.get("structure_assessment"), dict) else {}
        lines.append(f"- 结构级别：{assessment.get('level') or '未说明'}")
        if assessment.get("statement"):
            lines.append(f"- 结构判断：{assessment['statement']}")
        lines.extend(self._render_structured_lines(breakdown.get("structure_grasp"), limit=12))
        progression = breakdown.get("spoken_progression") if isinstance(breakdown.get("spoken_progression"), list) else []
        if progression:
            lines.append("- 口播推进：")
            for item in progression:
                if not isinstance(item, dict):
                    continue
                action = str(item.get("spoken_action") or "").strip()
                role = str(item.get("structural_role") or "").strip()
                if action or role:
                    lines.append(f"  - {item.get('sequence') or '?'}：{action or role}")
        else:
            lines.append("- 口播推进：原拆解没有记录可改变主线的推进节点")
        lines.extend(self._render_structured_lines(breakdown.get("recurring_evidence_patterns"), limit=12))
        full_analysis = breakdown.get("full_analysis")
        if isinstance(full_analysis, dict):
            lines.append("- 完整拆解：")
            lines.extend(self._render_full_analysis_lines(full_analysis, limit=60))
        cannot_infer = breakdown.get("cannot_infer")
        if cannot_infer:
            lines.append("- 不能推断：")
            lines.extend(f"  - {item}" for item in cannot_infer if str(item).strip())
        else:
            lines.append("- 不能推断：原拆解没有记录边界")
        return lines

    def _write_source_cards(
        self,
        *,
        staging_root: Path,
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, str], int]:
        source_dir = _folder_path(staging_root, "整理材料", "内容依据")
        source_dir.mkdir(parents=True, exist_ok=True)
        unique_cards: dict[str, dict[str, Any]] = {}
        for cards in source_cards_by_candidate.values():
            for card in cards:
                key = str(card.get("source_key") or "").strip()
                if key and key not in unique_cards:
                    unique_cards[key] = card
        page_names: dict[str, str] = {}
        for position, (source_key, card) in enumerate(sorted(unique_cards.items()), start=1):
            title = str(card.get("title") or "未找到标题").strip()
            account = str(card.get("account_name") or "未找到账号").strip()
            filename = _page_name(f"{account}｜{title}｜{source_key}", position=position)
            page_names[source_key] = filename[:-3]
            url = str(card.get("url") or "").strip()
            actions = card.get("evidence_actions") or []
            quotes = card.get("evidence_quotes") or []
            page_lines = [
                f"# 原文来源：{title}",
                "",
                f"账号：{account}",
                f"发布时间：{card.get('publish_time') or '未记录'}",
                f"来源状态：{card.get('archive_status') or '未说明'}",
                "",
                "## 原文入口",
                "",
            ]
            if url:
                page_lines.append(f"- [打开原文]({url})")
            else:
                page_lines.append("- 当前没有可点击的原文网址，只保留了正式拆解证据。")
            page_lines.extend(["", "## 从这条材料提炼了什么", ""])
            page_lines.extend(f"- {item}" for item in actions or ["当前没有提取到具体表达动作。"])
            page_lines.extend(["", "## 原文证据摘录", ""])
            page_lines.extend(f"> {item}" for item in quotes or ["当前没有保留可展示的原文摘录。"])
            page_lines.extend(
                [
                    "",
                    "## 追溯说明",
                    "",
                    "- 这是一张来源索引卡，不是新的经验，也不改变正式业务状态。",
                    "- 候选页面通过这张卡连接到原文入口和提炼证据；原始完整材料仍保留在正式系统中。",
                    "",
                ]
            )
            (source_dir / filename).write_text("\n".join(page_lines), encoding="utf-8")
        return page_names, len(page_names)

    def _write_breakdown_cards(
        self,
        *,
        staging_root: Path,
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, str], int]:
        """Write one readable, source-bound breakdown page per video."""
        breakdown_dir = _folder_path(staging_root, "整理材料", "内容拆解")
        breakdown_dir.mkdir(parents=True, exist_ok=True)
        unique_cards: dict[str, dict[str, Any]] = {}
        for cards in source_cards_by_candidate.values():
            for card in cards:
                source_key = str(card.get("source_key") or "").strip()
                if source_key and source_key not in unique_cards:
                    unique_cards[source_key] = card

        page_names: dict[str, str] = {}
        for position, (source_key, card) in enumerate(sorted(unique_cards.items()), start=1):
            title = str(card.get("title") or "未找到标题").strip()
            account = str(card.get("account_name") or "未找到账号").strip()
            filename = _page_name(f"拆解｜{account}｜{title}｜{source_key}", position=position)
            page_names[source_key] = filename[:-3]
            subject = str(card.get("content_subject_type") or "未说明")
            expression_form = str(card.get("expression_form") or "未说明")
            content_type_evidence = card.get("content_type_evidence") or []
            assessment = card.get("structure_assessment") or {}
            grasp = card.get("structure_grasp") or {}
            progression = card.get("spoken_progression") or []
            recurring = card.get("recurring_evidence_patterns") or []
            reactions = card.get("audience_reactions") or []
            cannot_infer = card.get("cannot_infer") or []

            def present(value: Any) -> bool:
                return bool(value) and value not in ({}, [])

            assessment_level = str(assessment.get("level") or "未说明") if isinstance(assessment, dict) else "未说明"
            assessment_statement = str(assessment.get("statement") or "").strip() if isinstance(assessment, dict) else ""
            grasp_ok = present(grasp)
            progression_ok = present(progression) or assessment_level in {"simple", "unclear"}
            quality_lines = [
                f"- 内容类型与表达形式：{'有结果' if subject != '未说明' and expression_form != '未说明' else '缺失'}",
                f"- 类型判断依据：{'有编号依据' if present(content_type_evidence) else '缺失'}",
                f"- 结构判断：{'有结果' if assessment_level != '未说明' and assessment_statement else '缺失'}（{assessment_level}）",
                f"- 结构理解：{'有结果' if grasp_ok else '缺失'}",
                f"- 口播推进：{'有推进节点' if present(progression) else ('按简单/不明确结构保留空数组' if progression_ok else '缺失')}",
                f"- 重复表达模式：{'有记录' if present(recurring) else '原拆解未记录'}",
                f"- 不能推断：{'有边界' if present(cannot_infer) else '未记录边界'}",
            ]
            page_lines = [
                f"# 单条拆解：{title}",
                "",
                f"账号：{account}",
                f"来源状态：{card.get('archive_status') or '未说明'}",
                "",
                "## 原文入口",
                "",
            ]
            url = str(card.get("url") or "").strip()
            page_lines.append(f"- [打开原文]({url})" if url else "- 当前没有可点击的原文网址。")
            page_lines.extend(
                [
                    "",
                    "## 拆解质量核对",
                    "",
                    *quality_lines,
                    "",
                    "这张页面展示的是单条视频拆解结果，不是经验结论。核对经验时，先看这里的结构判断和证据，再看候选经验是否真的超出了“简单盘点”。",
                    "",
                    "## 一、主题类型与表达形式",
                    "",
                    f"- 主题对象：{subject}",
                    f"- 表达形式：{expression_form}",
                    "- 类型判断依据：",
                ]
            )
            page_lines.extend(self._render_evidence_lines(content_type_evidence, limit=6) or ["- 未记录"])
            page_lines.extend(["", "## 二、结构判断", ""])
            page_lines.append(f"- 结构级别：{assessment_level}")
            if assessment_statement:
                page_lines.append(f"- 判断：{assessment_statement}")
            page_lines.extend(self._render_evidence_lines(assessment.get("source_evidence") if isinstance(assessment, dict) else [], limit=6))
            page_lines.extend(["", "## 三、结构理解", ""])
            page_lines.extend(self._render_structured_lines(grasp, limit=12) or ["- 未记录"])
            page_lines.extend(["", "## 四、口播推进", ""])
            if progression:
                for item in progression:
                    if not isinstance(item, dict):
                        continue
                    sequence = item.get("sequence") or "?"
                    action = str(item.get("spoken_action") or "").strip()
                    role = str(item.get("structural_role") or "").strip()
                    phase = str(item.get("mainline_phase") or "").strip()
                    page_lines.append(f"### 节点 {sequence}")
                    if phase:
                        page_lines.append(f"- 主线阶段：{phase}")
                    if action:
                        page_lines.append(f"- 口播动作：{action}")
                    if role:
                        page_lines.append(f"- 推进作用：{role}")
                    page_lines.extend(self._render_evidence_lines(item.get("source_evidence"), limit=4))
                    page_lines.append("")
            else:
                page_lines.append("- 原拆解没有记录可改变主线的推进节点；如果结构级别是 simple，这是合规的拆解结果。")
            page_lines.extend(["", "## 五、重复表达模式", ""])
            page_lines.extend(self._render_structured_lines(recurring, limit=12) or ["- 原拆解未记录重复表达模式。"])
            page_lines.extend(["", "## 六、观众反应", ""])
            page_lines.extend(self._render_structured_lines(reactions, limit=8) or ["- 原拆解未记录评论反应。"])
            page_lines.extend(["", "## 七、完整拆解", ""])
            page_lines.extend(
                self._render_full_analysis_lines(card.get("full_analysis") or {}, limit=100)
                or ["- 原拆解没有完整分析部分。"]
            )
            page_lines.extend(["", "## 八、不能从这条视频推出什么", ""])
            page_lines.extend(self._render_structured_lines(cannot_infer, limit=8) or ["- 原拆解未记录边界。"])
            page_lines.extend(
                [
                    "",
                    "## 使用边界",
                    "",
                    "- 这张卡只说明这条视频实际怎么组织和表达，不代表这种写法一定有效。",
                    "- 只有多条视频的拆解结果出现同一具体方法，才有资格进入经验候选；候选还要单独检查层级和适用边界。",
                    "",
                ]
            )
            (breakdown_dir / filename).write_text("\n".join(page_lines), encoding="utf-8")
        return page_names, len(page_names)

    @staticmethod
    def _render_evidence_lines(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        lines: list[str] = []
        for item in value:
            if len(lines) >= limit:
                break
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("statement") or "").strip()
                identifier = str(item.get("id") or "").strip()
                if text:
                    lines.append(f"> {identifier}：{text}" if identifier else f"> {text}")
            elif str(item).strip():
                lines.append(f"> {str(item).strip()}")
        return lines

    @classmethod
    def _render_structured_lines(cls, value: Any, *, limit: int) -> list[str]:
        lines: list[str] = []
        if isinstance(value, dict):
            for key in ("statement", "spoken_action", "structural_role", "observed_reaction"):
                text = str(value.get(key) or "").strip()
                if text and len(lines) < limit:
                    labels = {
                        "statement": "判断",
                        "spoken_action": "口播动作",
                        "structural_role": "结构作用",
                        "observed_reaction": "实际反应",
                    }
                    lines.append(f"- {labels[key]}：{text}")
            for key in ("core", "highlights", "tensions", "source_evidence", "comment_evidence", "related_spoken_evidence"):
                if len(lines) >= limit or key not in value:
                    continue
                nested = cls._render_structured_lines(value[key], limit=limit - len(lines))
                lines.extend(nested)
        elif isinstance(value, list):
            for item in value:
                if len(lines) >= limit:
                    break
                lines.extend(cls._render_structured_lines(item, limit=limit - len(lines)))
        elif str(value).strip():
            lines.append(f"- {str(value).strip()}")
        return lines[:limit]

    @classmethod
    def _render_full_analysis_lines(cls, value: Any, *, limit: int, depth: int = 0) -> list[str]:
        """Render the full prompt result without exposing private evidence IDs in prose."""
        lines: list[str] = []
        labels = {
            "target_audience": "目标受众", "theme": "文案主题", "structure": "文案结构",
            "script_formula": "脚本公式", "writing_methods": "创作手法", "tone_style": "语气风格",
            "opening_hook": "开头钩子", "emotional_arc": "情绪调动", "persona": "人设体现",
            "reusable_parts": "可以复用的部分", "adaptation_suggestions": "可复用结构与原创改编建议",
            "final_summary": "最后总结",
        }
        if len(lines) >= limit:
            return lines
        if isinstance(value, dict):
            for key, item in value.items():
                if len(lines) >= limit or key in {"source_evidence", "comment_evidence", "related_spoken_evidence"}:
                    continue
                label = labels.get(key, key)
                indent = "  " * depth
                if isinstance(item, (dict, list)):
                    lines.append(f"{indent}- {label}：")
                    lines.extend(cls._render_full_analysis_lines(item, limit=limit - len(lines), depth=depth + 1))
                elif str(item).strip():
                    lines.append(f"{indent}- {label}：{str(item).strip()}")
        elif isinstance(value, list):
            for item in value:
                if len(lines) >= limit:
                    break
                if isinstance(item, (dict, list)):
                    lines.extend(cls._render_full_analysis_lines(item, limit=limit - len(lines), depth=depth))
                elif str(item).strip():
                    lines.append(f"{'  ' * depth}- {str(item).strip()}")
        elif str(value).strip():
            lines.append(f"{'  ' * depth}- {str(value).strip()}")
        return lines[:limit]

    def _write_experience_review_queue(
        self,
        *,
        staging_root: Path,
        candidates: list[dict[str, Any]],
        source_cards_by_candidate: dict[str, list[dict[str, Any]]],
        source_page_names: dict[str, str],
        breakdown_page_names: dict[str, str],
        material_page_names: dict[str, str],
    ) -> tuple[int, int, int]:
        queue_root = _folder_path(staging_root, "可复用经验", "待确认经验")
        queue_root.mkdir(parents=True, exist_ok=True)
        links_by_layer: dict[str, list[str]] = {}
        source_link_count = 0
        breakdown_link_count = 0
        material_link_count = 0
        for position, candidate in enumerate(candidates, start=1):
            proposal = candidate.get("proposal") or {}
            content = proposal.get("candidate") if isinstance(proposal, dict) else {}
            if not isinstance(content, dict):
                content = {}
            domain = _domain_name(str(candidate.get("domain_label") or ""))
            layer_label = self._experience_layer_label(content.get("experience_layer"))
            domain_dir = queue_root / layer_label / domain
            domain_dir.mkdir(parents=True, exist_ok=True)
            summary = str(content.get("summary") or "未提供摘要").strip()
            filename = _page_name(summary, position=position)
            page = domain_dir / filename
            source_links: list[str] = []
            for card in source_cards_by_candidate.get(str(candidate["experience_candidate_id"]), []):
                source_key = str(card.get("source_key") or "").strip()
                source_page = source_page_names.get(source_key)
                if source_page:
                    source_links.append(
                        f"- {_mirror_link('整理材料', '内容依据', source_page)}"
                    )
            source_link_count += len(source_links)
            breakdown_links: list[str] = []
            for card in source_cards_by_candidate.get(str(candidate["experience_candidate_id"]), []):
                source_key = str(card.get("source_key") or "").strip()
                breakdown_page = breakdown_page_names.get(source_key)
                if breakdown_page:
                    breakdown_links.append(
                        f"- {_mirror_link('整理材料', '内容拆解', breakdown_page)}"
                    )
            breakdown_link_count += len(breakdown_links)
            material_links: list[str] = []
            for card in source_cards_by_candidate.get(str(candidate["experience_candidate_id"]), []):
                source_key = str(card.get("source_key") or "").strip()
                material_page = material_page_names.get(source_key)
                if material_page:
                    material_links.append(
                        f"- {_mirror_link(*material_page.split('/'))}"
                    )
            material_link_count += len(material_links)

            def lines(value: Any) -> list[str]:
                if isinstance(value, list):
                    return [str(item).strip() for item in value if str(item).strip()]
                text = str(value or "").strip()
                return [text] if text else []

            def bullet_block(title: str, value: Any) -> list[str]:
                values = lines(value)
                return [f"## {title}", "", *(f"- {item}" for item in values or ["未提供"]), ""]

            case_block: list[str] = ["## 案例", ""]
            cards = source_cards_by_candidate.get(str(candidate["experience_candidate_id"]), [])
            if cards:
                for case_number, card in enumerate(cards, start=1):
                    title = str(card.get("title") or "未找到标题").strip()
                    account = str(card.get("account_name") or "未找到账号").strip()
                    source_key = str(card.get("source_key") or "").strip()
                    source_page = source_page_names.get(source_key)
                    case_block.extend(
                        [
                            f"### 案例 {case_number}：{title}",
                            "",
                            f"- 账号：{account}",
                        ]
                    )
                    if source_page:
                        case_block.append(f"- 来源卡：{_mirror_link('整理材料', '内容依据', source_page)}")
                    url = str(card.get("url") or "").strip()
                    if url:
                        case_block.append(f"- 原文：[{title}]({url})")
                    actions = lines(card.get("evidence_actions"))
                    quotes = lines(card.get("evidence_quotes"))
                    if actions:
                        case_block.append(f"- 提炼出的动作：{'；'.join(actions[:2])}")
                    if quotes:
                        case_block.append(f"> {quotes[0]}")
                    case_block.append("")
            else:
                case_block.extend(
                    [
                        "- 当前没有解析到对应案例；这条候选不能直接接受。",
                        "",
                    ]
                )
            page.write_text(
                "\n".join(
                    [
                        f"# 待决定经验：{summary}",
                        "",
                        f"所属领域：{domain}",
                        f"材料依据数量：{int(candidate.get('source_count') or 0)} 条",
                        "",
                        "## 使用情况",
                        "",
                        f"- 经验层级：{self._experience_layer_label(content.get('experience_layer'))}",
                        f"- 使用位置：{self._experience_position_labels(content.get('use_positions'))}",
                        "",
                        "## 依据来源",
                        "",
                        *(source_links or ["- 当前没有解析到可点击的正式来源；这条候选不应直接接受。"]),
                        "",
                        "## 备料与拆解主卡",
                        "",
                        *(material_links or ["- 当前没有找到对应的备料主卡；这条候选不应直接接受。"]),
                        "",
                        "## 逐条拆解结果",
                        "",
                        *(breakdown_links or ["- 当前没有找到对应的逐条拆解结果；这条候选不能直接接受。"]),
                        "",
                        *case_block,
                        *bullet_block("什么时候适用", content.get("applicable_when")),
                        *bullet_block("怎么使用", content.get("method")),
                        *bullet_block("触发信号", content.get("trigger_signals")),
                        *bullet_block("不适用情况", content.get("not_applicable_when")),
                        *bullet_block("边界说明", content.get("boundary")),
                        "## 处理状态",
                        "",
                        "- 当前只是候选，不是正式经验。",
                        "- 请在 Codex 或 Hermes 中作出接受/拒绝决定；Obsidian页面不直接改正式状态。",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            links_by_layer.setdefault(layer_label, []).append(
                f"- {_mirror_link('可复用经验', '待确认经验', layer_label, domain, filename[:-3])}"
            )
        queue_index = _folder_path(staging_root, "可复用经验", "待确认经验.md")
        queue_index.write_text(
            "\n".join(
                [
                    "# 待确认经验",
                    "",
                    f"当前共有 {len(candidates)} 条经验候选等待确认。",
                    "这里是给人阅读的清单，不代表已经入库，也不会自动进入写作提示。",
                    "",
                    "## 怎么处理",
                    "",
                    "1. 在这里阅读候选内容和适用边界。",
                    "2. 回到正式业务入口做接受或拒绝决定。",
                    "3. 决定完成后重新生成镜像，正式经验区才会更新。",
                    "",
                    "## 先按层级理解，再看具体位置",
                    "",
                    "- 整体结构：能组织整条内容的主线，必须覆盖整条内容。",
                    "- 段落方法：只处理开头、主体、转场或结尾中的一个段落方法。",
                    "- 句内表达：只处理一句或相邻两句的表达动作，不能拿整段或整条内容来冒充。",
                    "",
                    "## 按经验层级查看",
                    "",
                    *[
                        line
                        for layer_label in ("整体结构", "段落方法", "句内表达", "未明确")
                        for line in (
                            [f"### {layer_label}", "", *links_by_layer.get(layer_label, ["- 当前没有候选。"]), ""]
                            if layer_label in links_by_layer
                            else []
                        )
                    ],
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return source_link_count, breakdown_link_count, material_link_count

    @staticmethod
    def _experience_layer_label(value: Any) -> str:
        return {
            "structure": "整体结构",
            "section_method": "段落方法",
            "local_detail": "句内表达",
        }.get(str(value or ""), "未明确")

    @staticmethod
    def _experience_position_labels(value: Any) -> str:
        labels = {
            "whole_content": "整条内容",
            "opening": "开头",
            "body": "主体",
            "transition": "转场",
            "ending": "结尾",
            "sentence": "句子层面",
        }
        if not isinstance(value, list):
            return "未明确"
        selected = [labels.get(str(item), str(item)) for item in value if str(item).strip()]
        return "、".join(selected) if selected else "未明确"

    def _write_source_boundary_page(self, *, staging_root: Path) -> None:
        foundation_root = _folder_path(staging_root, "项目基础")
        foundation_root.mkdir(parents=True, exist_ok=True)
        (foundation_root / _folder_name("通用") / "分层和写入说明.md").write_text(
            "\n".join(
                [
                    "# 分层和写入说明",
                    "",
                    "这套整理把‘原始素材、整理材料、待确认经验、已确认经验’分开，防止原始材料和未确认内容混进可复用经验。",
                    "",
                    "## 各层怎么区分",
                    "",
                    "- 原始素材：手动提供、系统采集和外部资料，只保留来源和原始状态。",
                    "- 整理材料：转写、评论整理、内容拆解、内容依据和表达方法，不等于经验。",
                    "- 待确认经验：系统根据材料形成的候选，必须人工接受或拒绝。",
                    "- 已确认经验：人工接受后才进入，供后续选题、写稿和复盘复用。",
                    "",
                    "## 谁负责什么",
                    "",
                    "- 正式系统负责保存真实状态和处理记录。",
                    "- Codex 或 Hermes负责理解候选并执行人工决定。",
                    "- Obsidian负责阅读、检索和查看整理结果，不直接改正式状态。",
                    "- 系统内部编号只用于保持关联，页面和目录使用中文名称。",
                    "- 外部资料库继续作为原始素材区，不自动并入已确认经验。",
                    "",
                    "## 当前原则",
                    "",
                    "- 不把内部编号、采集日志和模型日志复制成知识页面。",
                    "- 不把待确认经验冒充成已确认经验。",
                    "- 领域经验和通用经验分开保存，不能因为来源相同就混在一起。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
