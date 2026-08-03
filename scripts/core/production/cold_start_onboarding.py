"""Guided, domain-neutral cold-start configuration.

The guide collects only durable business boundaries: the owned account, the
domain boundary, required competitor accounts, the user's reuse/create choice,
and the explicit confirmation that makes those subjects formal.  Keywords,
audience prose and day-to-day conversation are intentionally not required.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    configured_domain_packs,
    get_domain_pack,
    refresh_domain_packs,
    set_domain_pack_config_dir,
)
from scripts.core.production.live_music_cold_start_preflight import (
    LiveColdStartPreflight,
    LiveColdStartRequest,
)
from scripts.core.production.stage0_content_core import (
    COLD_START_COMPETITOR_MAX,
    COLD_START_COMPETITOR_MIN,
    Stage0ContentProductionCore,
    StateTransitionError,
)


SUPPORTED_PLATFORMS = ("douyin",)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _identity_ref(platform: str, value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    if "://" in text or re.match(r"^[a-z0-9_-]+:", text, flags=re.IGNORECASE):
        return text
    return f"{platform}:{text}"


def _is_douyin_identity_ref(value: Any) -> bool:
    text = _clean(value)
    if not text:
        return False
    if "://" in text:
        hostname = (urlparse(text).hostname or "").lower()
        return hostname == "douyin.com" or hostname.endswith(".douyin.com")
    prefix, separator, identifier = text.partition(":")
    return separator == ":" and prefix.lower() == "douyin" and bool(identifier.strip())


def _text_features(value: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value.lower())
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


class ColdStartOnboardingService:
    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        config_dir: Path | None = None,
        preflight_environment: dict[str, str] | None = None,
    ) -> None:
        self.core = core
        self.config_dir = (config_dir or DOMAIN_CONFIG_DIR).resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if config_dir is not None:
            set_domain_pack_config_dir(self.config_dir)
        self.preflight_environment = preflight_environment

    @staticmethod
    def _pack_boundary(pack: dict[str, Any]) -> str:
        direct = _clean(pack.get("description") or pack.get("boundary"))
        if direct:
            return direct
        hotspot = pack.get("hotspot_direction") if isinstance(pack.get("hotspot_direction"), dict) else {}
        audience = _clean(hotspot.get("target_audience"))
        excluded = "、".join(_clean(item) for item in hotspot.get("do_not_cover", []) if _clean(item))
        parts = [part for part in (audience, f"不覆盖：{excluded}" if excluded else "") if part]
        return "；".join(parts) or _clean(pack.get("name"))

    def list_domains(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for label, pack in sorted(configured_domain_packs().items()):
            workflow_mode = _clean(pack.get("workflow_mode"))
            if workflow_mode not in {"manual_guard", "mature_automatic"}:
                raise StateTransitionError(
                    f"domain {label} must explicitly set workflow_mode"
                )
            result.append({
                "domain_label": label,
                "name": _clean(pack.get("name")) or label,
                "boundary": self._pack_boundary(pack),
                "activation_status": _clean(pack.get("activation_status")) or "pending_business_input",
                "workflow_mode": workflow_mode,
                "activation_label": {
                    "approved": "可用于正式冷启动",
                    "active": "已启用",
                    "approved_for_phase8_pilot": "可用于正式冷启动",
                    "pending_business_input": "等待账号等业务信息",
                }.get(_clean(pack.get("activation_status")), "尚未启用"),
            })
        return result

    def change_workflow_mode(
        self,
        *,
        domain_label: str,
        workflow_mode: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Apply the user-controlled domain switch; never infer or auto-change it."""
        label = _clean(domain_label)
        mode = _clean(workflow_mode)
        operator = _clean(actor)
        change_reason = _clean(reason)
        if mode not in {"manual_guard", "mature_automatic"}:
            raise StateTransitionError(
                "workflow mode must be manual_guard or mature_automatic"
            )
        if not operator or not change_reason:
            raise StateTransitionError(
                "workflow mode changes require the user and a reason"
            )
        pack = get_domain_pack(label)
        previous = _clean(pack.get("workflow_mode")) or "manual_guard"
        if previous == mode:
            raise StateTransitionError("the domain is already using this workflow mode")
        path = Path(str(pack["config_path"])).resolve()
        updated = {key: value for key, value in pack.items() if key != "config_path"}
        updated["workflow_mode"] = mode
        self._write_pack(path=path, payload=updated)
        refresh_domain_packs()
        self.core._audit(
            None,
            "domain_workflow_mode_changed",
            {
                "domain_label": label,
                "previous_mode": previous,
                "workflow_mode": mode,
                "actor": operator,
                "reason": change_reason,
            },
        )
        return {
            "domain_label": label,
            "previous_mode": previous,
            "workflow_mode": mode,
            "actor": operator,
            "reason": change_reason,
        }

    def overlap_suggestions(self, *, domain_name: str, domain_boundary: str) -> list[dict[str, Any]]:
        proposed = _text_features(f"{domain_name} {domain_boundary}")
        suggestions: list[dict[str, Any]] = []
        for item in self.list_domains():
            existing = _text_features(f"{item['name']} {item['boundary']}")
            union = proposed | existing
            score = len(proposed & existing) / len(union) if union else 0.0
            exact_name = re.sub(r"\s+", "", domain_name).lower() == re.sub(r"\s+", "", item["name"]).lower()
            suggestions.append({
                **item,
                "overlap_score": round(1.0 if exact_name else score, 3),
                "recommendation": "优先复用" if exact_name or score >= 0.24 else "需要人工比较边界",
            })
        return sorted(suggestions, key=lambda item: (-item["overlap_score"], item["name"]))

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        domain_mode = _clean(payload.get("domain_mode"))
        platform = _clean(payload.get("platform")).lower()
        owned = payload.get("owned_account") if isinstance(payload.get("owned_account"), dict) else {}
        competitors = payload.get("competitor_accounts") if isinstance(payload.get("competitor_accounts"), list) else []
        existing_label = _clean(payload.get("existing_domain_label"))
        if domain_mode == "reuse" and existing_label in configured_domain_packs():
            pack = get_domain_pack(existing_label)
            domain_name = _clean(pack.get("name")) or existing_label
            domain_boundary = self._pack_boundary(pack)
            domain_label = existing_label
        else:
            domain_name = _clean(payload.get("domain_name"))
            domain_boundary = _clean(payload.get("domain_boundary"))
            domain_label = "domain_" + hashlib.sha256(
                f"{domain_name}\n{domain_boundary}".encode("utf-8")
            ).hexdigest()[:12]
        return {
            "domain_mode": domain_mode,
            "existing_domain_label": existing_label,
            "domain_label": domain_label,
            "domain_name": domain_name,
            "domain_boundary": domain_boundary,
            "independent_domain_confirmed": payload.get("independent_domain_confirmed") is True,
            "platform": platform,
            "owned_account": {
                "display_name": _clean(owned.get("display_name")),
                "external_account_ref": _identity_ref(platform, owned.get("external_account_ref")),
            },
            "competitor_accounts": [
                {
                    "display_name": _clean(item.get("display_name")),
                    "external_account_ref": _identity_ref(platform, item.get("external_account_ref")),
                }
                for item in competitors if isinstance(item, dict)
            ],
            "actor": _clean(payload.get("actor")),
        }

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(payload)
        blockers: list[dict[str, str]] = []

        def block(code: str, message: str) -> None:
            blockers.append({"code": code, "message": message})

        if normalized["domain_mode"] not in {"reuse", "create"}:
            block("domain_mode", "请选择复用已有领域或新建独立领域")
        if normalized["domain_mode"] == "reuse" and normalized["existing_domain_label"] not in configured_domain_packs():
            block("existing_domain", "请选择一个已经存在的领域")
        if normalized["domain_mode"] == "create":
            if not normalized["domain_name"] or not normalized["domain_boundary"]:
                block("new_domain_boundary", "新领域必须填写领域名称和清晰边界")
            if not normalized["independent_domain_confirmed"]:
                block("independent_domain_confirmation", "请先比较已有领域，并确认确实不能复用")
            if any(item["overlap_score"] == 1.0 for item in self.overlap_suggestions(
                domain_name=normalized["domain_name"], domain_boundary=normalized["domain_boundary"],
            )):
                block("duplicate_domain", "领域名称与已有领域相同，请直接复用")
        if normalized["platform"] not in SUPPORTED_PLATFORMS:
            block("platform", "当前只支持抖音账号")
        if not normalized["owned_account"]["display_name"] or not normalized["owned_account"]["external_account_ref"]:
            block("owned_account", "请填写自营账号名称和可识别的主页链接或账号标识")
        competitors = normalized["competitor_accounts"]
        if len(competitors) < COLD_START_COMPETITOR_MIN:
            block(
                "competitor_account_minimum",
                f"冷启动至少需要 {COLD_START_COMPETITOR_MIN} 个对标账号，当前只有 {len(competitors)} 个",
            )
        if len(competitors) > COLD_START_COMPETITOR_MAX:
            block(
                "competitor_account_maximum",
                f"冷启动最多允许 {COLD_START_COMPETITOR_MAX} 个对标账号，当前有 {len(competitors)} 个",
            )
        if any(not item["display_name"] or not item["external_account_ref"] for item in competitors):
            block("competitor_account_fields", "每个对标账号都必须填写名称和主页链接或账号标识")
        all_account_refs = [normalized["owned_account"]["external_account_ref"], *[
            item["external_account_ref"] for item in competitors
        ]]
        if any(ref and not _is_douyin_identity_ref(ref) for ref in all_account_refs):
            block("non_douyin_account", "当前只接受抖音主页链接或抖音账号标识")
        refs = [item["external_account_ref"] for item in competitors]
        nonempty_refs = [ref for ref in refs if ref]
        if len(nonempty_refs) != len(set(nonempty_refs)) or (
            normalized["owned_account"]["external_account_ref"]
            and normalized["owned_account"]["external_account_ref"] in set(nonempty_refs)
        ):
            block("duplicate_accounts", "自营账号和各对标账号不能重复")
        if not normalized["actor"]:
            block("actor", "请填写本次确认人")
        preview_token = hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest()
        suggestions = self.overlap_suggestions(
            domain_name=normalized["domain_name"], domain_boundary=normalized["domain_boundary"],
        ) if normalized["domain_mode"] == "create" else []
        return {
            "ready_to_confirm": not blockers,
            "blockers": blockers,
            "normalized": normalized,
            "overlap_suggestions": suggestions,
            "preview_token": preview_token,
        }

    def _write_pack(self, *, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".new")
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _activate_domain(self, normalized: dict[str, Any]) -> tuple[Path, str | None]:
        if normalized["domain_mode"] == "create":
            path = self.config_dir / f"{normalized['domain_label']}.yaml"
            if path.exists():
                raise StateTransitionError("the proposed new domain configuration already exists")
            pack = {
                "name": normalized["domain_name"],
                "formal_domain_label": normalized["domain_label"],
                "description": normalized["domain_boundary"],
                "activation_status": "approved",
                "workflow_mode": "manual_guard",
                "runtime_requirements": [],
                "discovery": {
                    "hotspot_match_terms": [], "risk_block_terms": [], "exclude_terms": [],
                    "topic_search": {
                        "active_tags": [], "broad_domain_tags": [], "generic_tags": [],
                        "activity_review_terms": [],
                    },
                },
            }
            self._write_pack(path=path, payload=pack)
            refresh_domain_packs()
            return path, None
        pack = get_domain_pack(normalized["domain_label"])
        path = Path(str(pack["config_path"]))
        original = path.read_text(encoding="utf-8")
        if _clean(pack.get("activation_status")) not in {"approved", "active", "approved_for_phase8_pilot"}:
            updated = {key: value for key, value in pack.items() if key != "config_path"}
            updated["activation_status"] = "approved"
            self._write_pack(path=path, payload=updated)
            refresh_domain_packs()
            return path, original
        return path, None

    def confirm(self, payload: dict[str, Any], *, preview_token: str) -> dict[str, Any]:
        preview = self.preview(payload)
        if preview["preview_token"] != _clean(preview_token):
            raise StateTransitionError("configuration changed after preview; preview it again before confirming")
        if not preview["ready_to_confirm"]:
            raise StateTransitionError("cold-start configuration still has unresolved required items")
        existing = next(
            (
                item for item in self.core.list_cold_start_configurations()
                if item["confirmation_key"] == preview["preview_token"]
            ),
            None,
        )
        if existing is not None:
            return {**existing, "preflight": self.inspect_configuration(existing["configuration_id"])}
        normalized = preview["normalized"]
        duplicate_subject = next(
            (
                item
                for item in self.core.list_cold_start_configurations()
                if item.get("status") != "cancelled"
                and item.get("domain_label") == normalized["domain_label"]
                and any(
                    account.get("account_role") == "owned"
                    and _clean(account.get("external_account_ref"))
                    == normalized["owned_account"]["external_account_ref"]
                    for account in item.get("accounts", [])
                )
            ),
            None,
        )
        if duplicate_subject is not None:
            raise StateTransitionError(
                "this domain and owned account already have a formal cold-start configuration"
            )
        path: Path | None = None
        restore_text: str | None = None
        try:
            path, restore_text = self._activate_domain(normalized)
            configuration = self.core.configure_cold_start_subjects(
                confirmation_key=preview["preview_token"],
                domain_mode=normalized["domain_mode"],
                domain_label=normalized["domain_label"],
                domain_name=normalized["domain_name"],
                domain_boundary=normalized["domain_boundary"],
                platform=normalized["platform"],
                owned_account=normalized["owned_account"],
                competitor_accounts=tuple(normalized["competitor_accounts"]),
                actor=normalized["actor"],
            )
        except Exception:
            if path is not None and restore_text is not None:
                path.write_text(restore_text, encoding="utf-8")
                refresh_domain_packs()
            elif path is not None and normalized["domain_mode"] == "create" and path.exists():
                path.unlink()
                refresh_domain_packs()
            raise
        return {**configuration, "preflight": self.inspect_configuration(configuration["configuration_id"])}

    def inspect_configuration(self, configuration_id: str) -> dict[str, Any]:
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        report = LiveColdStartPreflight(
            core=self.core, environment=self.preflight_environment,
        ).inspect(LiveColdStartRequest(
            domain_label=configuration["domain_label"],
            owned_account_id=configuration["owned_account_id"],
            competitor_account_ids=tuple(configuration["competitor_account_ids"]),
        ))
        labels = {
            "formal_production_identity": ("已连接正式业务数据", "当前是隔离验证数据，不能启动真实采集"),
            "domain_pack_registered": ("领域配置已经登记", "领域配置尚未登记"),
            "domain_business_inputs_approved": ("领域边界和账号输入已经确认", "领域仍在等待正式业务输入"),
            "owned_account_registered": ("自营账号已经正式登记", "自营账号标识不完整或尚未登记"),
            "competitor_accounts_registered": ("全部对标账号已经正式登记", "对标账号存在缺失、重复或领域不一致"),
            "mediacrawler_runtime": ("真实平台采集工具可以运行", "真实平台采集工具或运行环境尚未准备好"),
            "mediacrawler_account_session": ("自营账号采集登录状态已经准备好", "尚未准备自营账号的采集登录状态"),
            "model_routes": ("分析和写作模型均已按明确配置连接", "分析或写作模型配置尚未完整连接"),
            "sensevoice_runtime": ("本地转写能力可以运行", "本地转写环境或模型尚未准备好"),
            "ffmpeg_runtime": ("音视频处理工具可以运行", "音视频处理工具尚未准备好"),
            "domain_runtime_requirements_supported": ("该领域需要的专用能力已经接入", "该领域仍有专用能力没有接入"),
            "music_audience_sessions": ("音乐听众材料来源的登录状态已经准备好", "网易云音乐或豆瓣的独立登录状态尚未准备好"),
            "validated_human_decision_entry": ("正式人工入口已通过真实往返验证", "还没有人工入口通过真实往返验证"),
        }
        guidance = {
            "formal_production_identity": "请切换到正式业务数据后再启动，隔离验证数据不能发起真实采集。",
            "domain_pack_registered": "请先完成领域登记，确认领域名称和业务边界。",
            "domain_business_inputs_approved": "请先确认领域边界、自营账号和对标账号。",
            "owned_account_registered": "请补全自营账号名称和抖音主页链接。",
            "competitor_accounts_registered": "请检查是否有空账号、重复账号或不属于当前领域的账号。",
            "mediacrawler_runtime": "请先准备抖音采集工具及其专用运行环境。",
            "mediacrawler_account_session": "请先在抖音采集工具中登录自营账号一次，并保留该登录状态。",
            "model_routes": "请先完成分析模型和写作模型的明确配置。",
            "sensevoice_runtime": "请先准备本地转写环境和模型。",
            "ffmpeg_runtime": "请先准备音视频处理工具。",
            "domain_runtime_requirements_supported": "该领域声明了尚未接入的专用能力，需要先完成对应连接。",
            "music_audience_sessions": "这只在以后运行音乐人物听众材料采集时需要，不会阻断本次对标账号冷启动；到人物研究阶段再分别登录网易云音乐和豆瓣即可。",
            "validated_human_decision_entry": "请先在页面顶部填写确认人并完成身份验证。",
        }
        for check in report["checks"]:
            passed, failed = labels.get(check["code"], ("该项已经准备好", "该项尚未准备好"))
            check["detail"] = passed if check["passed"] else failed
            check["guidance"] = "" if check["passed"] else guidance.get(check["code"], "请按提示补齐后重新检查。")
        return report

    def start(self, *, configuration_id: str, actor: str) -> dict[str, Any]:
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        request = LiveColdStartRequest(
            domain_label=configuration["domain_label"],
            owned_account_id=configuration["owned_account_id"],
            competitor_account_ids=tuple(configuration["competitor_account_ids"]),
        )
        visible_report = self.inspect_configuration(configuration_id)
        if not visible_report["ready"]:
            missing = [item["detail"] for item in visible_report["checks"] if not item["passed"]]
            raise StateTransitionError("真实启动仍被安全拦截：" + "；".join(missing))
        preflight = LiveColdStartPreflight(
            core=self.core, environment=self.preflight_environment,
        ).require_ready(request)
        return self.core.start_configured_cold_start(
            configuration_id=configuration_id,
            actor=_clean(actor),
            idempotency_key=f"start:{configuration_id}",
            preflight_receipt_id=preflight["preflight_receipt_id"],
        )

    def progress(self, configuration_id: str) -> dict[str, Any]:
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        cold_start_id = str(configuration.get("cold_start_id") or "")
        registrations: list[dict[str, Any]] = []
        progress_summary: dict[str, Any] = {
            "account_total": 0,
            "stages": {},
            "items": {},
            "selection_summary": {},
            "last_activity_at": None,
        }
        if cold_start_id:
            rows = self.core.conn.execute(
                "SELECT registration.registration_id, registration.current_step, registration.status, "
                "registration.created_at, registration.completed_at, account.display_name, account.external_account_ref "
                "FROM stage0_competitor_registration registration "
                "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
                "WHERE registration.cold_start_id=? AND registration.data_identity=? AND account.data_identity=? "
                "ORDER BY registration.created_at, registration.registration_id",
                (cold_start_id, self.core.data_identity, self.core.data_identity),
            ).fetchall()
            registrations = [{key: row[key] for key in row.keys()} for row in rows]
            registration_ids = [str(item["registration_id"]) for item in registrations]
            completed_steps: dict[str, dict[str, str]] = {registration_id: {} for registration_id in registration_ids}
            item_progress: dict[str, dict[str, dict[str, Any]]] = {registration_id: {} for registration_id in registration_ids}
            historical_item_counts: dict[str, int] = {registration_id: 0 for registration_id in registration_ids}
            expected_hit_counts: dict[str, int] = {registration_id: 0 for registration_id in registration_ids}
            baseline_qualities: dict[str, str] = {registration_id: "not_ready" for registration_id in registration_ids}
            baseline_item_counts: dict[str, int] = {registration_id: 0 for registration_id in registration_ids}
            older_history_backfills: dict[str, bool] = {registration_id: False for registration_id in registration_ids}
            selection_policy_versions: dict[str, str] = {registration_id: "" for registration_id in registration_ids}
            model_activity: dict[str, str] = {}
            model_usage: dict[str, Any] = {
                "providers": [],
                "models": [],
                "call_count": 0,
                "provider_returned": 0,
                "provider_failed": 0,
                "total_tokens": 0,
                "last_activity_at": None,
            }
            if registration_ids:
                placeholders = ",".join("?" for _ in registration_ids)
                step_rows: list[dict[str, Any]] = []
                for registration_id in registration_ids:
                    for step in self.core.list_competitor_registration_steps(
                        registration_id=registration_id
                    ):
                        step_rows.append({
                            "registration_id": registration_id,
                            "step_name": step["step_name"],
                            "completed_at": step["completed_at"],
                            "artifact_refs": step["artifact_refs"],
                        })
                for row in step_rows:
                    registration_id = str(row["registration_id"])
                    step_name = str(row["step_name"])
                    completed_steps[registration_id][step_name] = str(row["completed_at"])
                    if step_name in {"historical_material", "high_signal_identification"}:
                        artifacts = (
                            row["artifact_refs"]
                            if isinstance(row.get("artifact_refs"), list)
                            else []
                        )
                    else:
                        artifacts = []
                    if step_name == "historical_material":
                        historical_item_counts[registration_id] = sum(
                            int(artifact.get("item_count") or len(artifact.get("items", [])))
                            for artifact in artifacts
                            if isinstance(artifact, dict)
                        )
                    if step_name == "high_signal_identification":
                        expected_hit_counts[registration_id] = sum(
                            int(artifact.get("selected_count") or len(artifact.get("selected_items", [])))
                            for artifact in artifacts
                            if isinstance(artifact, dict)
                        )
                        signal_artifact = next(
                            (artifact for artifact in artifacts if isinstance(artifact, dict)),
                            {},
                        )
                        baseline_qualities[registration_id] = str(
                            signal_artifact.get("baseline_quality") or "legacy_unverified"
                        )
                        baseline_item_counts[registration_id] = int(
                            signal_artifact.get("baseline_item_count") or 0
                        )
                        older_history_backfills[registration_id] = bool(
                            signal_artifact.get("used_older_history_backfill")
                        )
                        selection_policy_versions[registration_id] = str(
                            signal_artifact.get("selection_policy_version") or ""
                        )

                item_rows = self.core.conn.execute(
                    "SELECT registration_id, step_name, status, COUNT(*) AS item_count, "
                    "SUM(attempt_count) AS attempt_count, MAX(updated_at) AS updated_at "
                    "FROM stage0_competitor_registration_item "
                    f"WHERE data_identity=? AND registration_id IN ({placeholders}) "
                    "GROUP BY registration_id, step_name, status",
                    (self.core.data_identity, *registration_ids),
                ).fetchall()
                for row in item_rows:
                    registration_id = str(row["registration_id"])
                    step_name = str(row["step_name"])
                    bucket = item_progress[registration_id].setdefault(
                        step_name,
                        {"completed": 0, "failed": 0, "excluded": 0, "retryable": 0, "total": 0, "attempt_count": 0, "last_activity_at": None},
                    )
                    count = int(row["item_count"] or 0)
                    if row["status"] in {"completed", "failed", "excluded"}:
                        bucket[str(row["status"])] = count
                    else:
                        bucket["retryable"] = count
                    bucket["total"] += count
                    bucket["attempt_count"] += int(row["attempt_count"] or 0)
                    updated_at = str(row["updated_at"] or "") or None
                    if updated_at and (bucket["last_activity_at"] is None or updated_at > bucket["last_activity_at"]):
                        bucket["last_activity_at"] = updated_at

                model_rows = self.core.conn.execute(
                    "SELECT registration_id, provider_name, model_name, status, usage_json, created_at "
                    "FROM stage0_competitor_registration_model_run "
                    f"WHERE data_identity=? AND registration_id IN ({placeholders}) AND step_name='breakdown'",
                    (self.core.data_identity, *registration_ids),
                ).fetchall()
                providers: set[str] = set()
                models: set[str] = set()
                for row in model_rows:
                    registration_id = str(row["registration_id"])
                    created_at = str(row["created_at"] or "")
                    if created_at and created_at > model_activity.get(registration_id, ""):
                        model_activity[registration_id] = created_at
                    provider_name = str(row["provider_name"] or "").strip()
                    model_name = str(row["model_name"] or "").strip()
                    if provider_name:
                        providers.add(provider_name)
                    if model_name:
                        models.add(model_name)
                    model_usage["call_count"] += 1
                    if str(row["status"]) == "succeeded":
                        model_usage["provider_returned"] += 1
                    else:
                        model_usage["provider_failed"] += 1
                    try:
                        usage = json.loads(str(row["usage_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        usage = {}
                    model_usage["total_tokens"] += int(usage.get("total_tokens") or 0)
                    if created_at and (
                        model_usage["last_activity_at"] is None
                        or created_at > model_usage["last_activity_at"]
                    ):
                        model_usage["last_activity_at"] = created_at
                model_usage["providers"] = sorted(providers)
                model_usage["models"] = sorted(models)

            for registration in registrations:
                registration_id = str(registration["registration_id"])
                steps = completed_steps[registration_id]
                items = item_progress[registration_id]
                expected_total = int(expected_hit_counts.get(registration_id) or 0)
                prepared_bucket = items.setdefault(
                    "transcripts_and_comments",
                    {
                        "completed": 0,
                        "failed": 0,
                        "excluded": 0,
                        "retryable": 0,
                        "total": 0,
                        "attempt_count": 0,
                        "last_activity_at": None,
                    },
                )
                prepared_bucket["total"] = expected_total
                prepared_bucket["pending"] = max(
                    0,
                    expected_total
                    - int(prepared_bucket.get("completed") or 0)
                    - int(prepared_bucket.get("failed") or 0)
                    - int(prepared_bucket.get("excluded") or 0)
                    - int(prepared_bucket.get("retryable") or 0),
                )
                breakdown_bucket = items.setdefault(
                    "breakdown",
                    {
                        "completed": 0,
                        "failed": 0,
                        "excluded": 0,
                        "retryable": 0,
                        "total": 0,
                        "attempt_count": 0,
                        "last_activity_at": None,
                    },
                )
                breakdown_bucket["eligible"] = int(prepared_bucket.get("completed") or 0)
                breakdown_bucket["pending"] = max(
                    0,
                    expected_total
                    - int(breakdown_bucket.get("completed") or 0)
                    - int(breakdown_bucket.get("failed") or 0)
                    - int(breakdown_bucket.get("excluded") or 0)
                    - int(breakdown_bucket.get("retryable") or 0),
                )
                breakdown_bucket["pending_preparation"] = max(
                    0,
                    expected_total - int(breakdown_bucket["eligible"]),
                )
                breakdown_bucket["pending_analysis"] = max(
                    0,
                    int(breakdown_bucket["eligible"])
                    - int(breakdown_bucket.get("completed") or 0)
                    - int(breakdown_bucket.get("failed") or 0)
                    - int(breakdown_bucket.get("excluded") or 0)
                    - int(breakdown_bucket.get("retryable") or 0),
                )
                breakdown_bucket["total"] = expected_total
                activity_candidates = [
                    str(registration.get("completed_at") or ""),
                    str(registration.get("created_at") or ""),
                    *steps.values(),
                    model_activity.get(registration_id, ""),
                    *[
                        str(bucket.get("last_activity_at") or "")
                        for bucket in items.values()
                    ],
                ]
                registration["completed_steps"] = steps
                registration["item_progress"] = items
                registration["historical_item_count"] = int(
                    historical_item_counts.get(registration_id) or 0
                )
                registration["selected_hit_count"] = expected_total
                registration["hit_selection_ratio"] = (
                    expected_total / registration["historical_item_count"]
                    if registration["historical_item_count"] else 0.0
                )
                registration["baseline_quality"] = baseline_qualities.get(
                    registration_id, "not_ready"
                )
                registration["baseline_item_count"] = int(
                    baseline_item_counts.get(registration_id) or 0
                )
                registration["used_older_history_backfill"] = bool(
                    older_history_backfills.get(registration_id)
                )
                registration["selection_policy_version"] = selection_policy_versions.get(
                    registration_id, ""
                )
                registration["last_activity_at"] = max(
                    (value for value in activity_candidates if value),
                    default=None,
                )

            stage_step_map = {
                "historical_collection": "historical_material",
                "baseline_calculation": "high_signal_identification",
                "hit_filtering": "high_signal_identification",
                "hit_material_preparation": "transcripts_and_comments",
                "hit_breakdown": "breakdown",
            }
            stages = {
                stage: sum(
                    step_name in registration["completed_steps"]
                    for registration in registrations
                )
                for stage, step_name in stage_step_map.items()
            }
            tag_library = self.core.get_cold_start_tag_library(cold_start_id=cold_start_id)
            stages["domain_tag_library"] = len(registrations) if tag_library is not None else 0
            completed_count = sum(registration["status"] == "completed" for registration in registrations)
            stages["registration_finalizing"] = completed_count
            stages["registration_completed"] = completed_count

            items_summary: dict[str, dict[str, Any]] = {}
            for step_name in ("transcripts_and_comments", "breakdown"):
                buckets = [
                    registration["item_progress"].get(step_name, {})
                    for registration in registrations
                ]
                items_summary[step_name] = {
                    "completed": sum(int(bucket.get("completed") or 0) for bucket in buckets),
                    "failed": sum(int(bucket.get("failed") or 0) for bucket in buckets),
                    "excluded": sum(int(bucket.get("excluded") or 0) for bucket in buckets),
                    "retryable": sum(int(bucket.get("retryable") or 0) for bucket in buckets),
                    "pending": sum(int(bucket.get("pending") or 0) for bucket in buckets),
                    "total": sum(int(bucket.get("total") or 0) for bucket in buckets),
                    "attempt_count": sum(int(bucket.get("attempt_count") or 0) for bucket in buckets),
                    "last_activity_at": max(
                        (
                            str(bucket.get("last_activity_at"))
                            for bucket in buckets if bucket.get("last_activity_at")
                        ),
                        default=None,
                    ),
                }
                if step_name == "breakdown":
                    items_summary[step_name]["eligible"] = sum(
                        int(bucket.get("eligible") or 0) for bucket in buckets
                    )
                    items_summary[step_name]["pending_preparation"] = sum(
                        int(bucket.get("pending_preparation") or 0) for bucket in buckets
                    )
                    items_summary[step_name]["pending_analysis"] = sum(
                        int(bucket.get("pending_analysis") or 0) for bucket in buckets
                    )
                    failure_rows = self.core.conn.execute(
                        "SELECT registration_id, item_ref, error_json "
                        "FROM stage0_competitor_registration_item "
                        f"WHERE data_identity=? AND registration_id IN ({placeholders}) "
                        "AND step_name='breakdown' AND status IN ('failed', 'excluded') "
                        "ORDER BY registration_id, item_ref",
                        (self.core.data_identity, *registration_ids),
                    ).fetchall()
                    registration_names = {
                        str(registration.get("registration_id") or ""): str(
                            registration.get("display_name") or ""
                        )
                        for registration in registrations
                    }
                    categories: dict[str, dict[str, Any]] = {}
                    for failure in failure_rows:
                        try:
                            error = json.loads(str(failure["error_json"] or "{}"))
                            reason = str(error.get("reason") or "")
                            returned_content = str(error.get("returned_content") or "")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            reason = ""
                            returned_content = ""
                        if "blocked until its same-input quality comparison" in reason:
                            label = "历史拦截：当时未发出请求"
                        elif reason == "payload has unexpected fields":
                            label = "接收规则未同步：返回未保存"
                        elif "provider returned empty visible content" in reason:
                            label = "外部服务没有返回内容"
                        elif returned_content == "The request was rejected because it was considered high risk":
                            label = "外部服务以高风险为由拒绝处理"
                        elif "The request was rejected because it was considered high risk" in returned_content:
                            label = "返回内容末尾混入高风险拒绝语"
                        elif reason.startswith("model output is not JSON"):
                            label = "返回内容格式损坏，不能保存"
                        elif "must be a string" in reason or "is not an allowed value" in reason:
                            label = "返回格式不符合固定要求：未保存"
                        else:
                            label = "其他未完成：需要核对"
                        category = categories.setdefault(label, {"count": 0, "accounts": [], "videos": []})
                        category["count"] += 1
                        account_name = registration_names.get(str(failure["registration_id"] or ""), "")
                        if account_name and account_name not in category["accounts"]:
                            category["accounts"].append(account_name)
                        source_id = str(failure["item_ref"] or "")
                        video = self.core.conn.execute(
                            "SELECT title, url FROM competitor_videos WHERE platform_item_id=? LIMIT 1",
                            (source_id,),
                        ).fetchone()
                        category["videos"].append({
                            "account_name": account_name,
                            "source_id": source_id,
                            "title": str(video["title"] or "") if video is not None else "",
                            "url": str(video["url"] or "") if video is not None else "",
                        })
                    items_summary[step_name]["failure_summary"] = [
                        {
                            "label": label,
                            "count": int(value["count"]),
                            "accounts": value["accounts"],
                            "videos": value["videos"],
                        }
                        for label, value in categories.items()
                    ]
            model_usage["saved_breakdowns"] = int(
                items_summary.get("breakdown", {}).get("completed") or 0
            )
            model_usage["returned_but_not_saved"] = max(
                0,
                int(model_usage["provider_returned"])
                - int(model_usage["saved_breakdowns"]),
            )
            progress_summary = {
                "account_total": len(registrations),
                "stages": stages,
                "items": items_summary,
                "selection_summary": {
                    "historical_item_count": sum(historical_item_counts.values()),
                    "baseline_item_count": sum(baseline_item_counts.values()),
                    "older_history_backfill_account_count": sum(
                        bool(value) for value in older_history_backfills.values()
                    ),
                    "selected_hit_count": sum(expected_hit_counts.values()),
                    "selection_ratio": (
                        sum(expected_hit_counts.values()) / sum(historical_item_counts.values())
                        if sum(historical_item_counts.values()) else 0.0
                    ),
                    "insufficient_history_account_count": sum(
                        quality == "insufficient_history"
                        for quality in baseline_qualities.values()
                    ),
                    "policy_versions": sorted({
                        version for version in selection_policy_versions.values() if version
                    }),
                },
                "model_usage": model_usage,
                "last_activity_at": max(
                    (
                        str(registration["last_activity_at"])
                        for registration in registrations if registration.get("last_activity_at")
                    ),
                    default=None,
                ),
            }
        return {
            "configuration": configuration,
            "cold_start_id": cold_start_id or None,
            "registrations": registrations,
            "progress_summary": progress_summary,
        }
