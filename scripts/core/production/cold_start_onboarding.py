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
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import yaml

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    configured_domain_packs,
    get_domain_pack,
    set_domain_pack_config_dir,
)
from scripts.core.production.live_music_cold_start_preflight import (
    LiveColdStartPreflight,
    LiveColdStartRequest,
)
from scripts.core.production.stage0_content_core import (
    COLD_START_CONTRACT_VERSION,
    COLD_START_COMPETITOR_MAX,
    COLD_START_COMPETITOR_MIN,
    Stage0ContentProductionCore,
    StateTransitionError,
)


SUPPORTED_PLATFORMS = ("douyin",)


def _clean(value: Any) -> str:
    text = str(value or "")
    # Feishu/Hermes can expose UTF-16 surrogate code units for malformed
    # characters. Rebuild valid pairs (including emoji) and replace only
    # unpaired surrogates before any UTF-8 hashing or persistence.
    text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return re.sub(r"\s+", " ", text).strip()


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
        execution_registration_service: Any | None = None,
        task_model_resolver: Any | None = None,
        background_execution_launcher: Any | None = None,
        background_execution_inspector: Any | None = None,
        background_execution_stopper: Any | None = None,
        background_notification_target_reader: Any | None = None,
        execution_progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.core = core
        self.config_dir = (config_dir or DOMAIN_CONFIG_DIR).resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if config_dir is not None:
            set_domain_pack_config_dir(self.config_dir)
        self.preflight_environment = preflight_environment
        # The business path always uses the real cold-start orchestrator. An
        # isolated caller may inject only the single-account external-boundary
        # service so the same orchestration can run without side effects.
        self.execution_registration_service = execution_registration_service
        self.task_model_resolver = task_model_resolver
        self.background_execution_launcher = background_execution_launcher
        self.background_execution_inspector = background_execution_inspector
        self.background_execution_stopper = background_execution_stopper
        self.background_notification_target_reader = background_notification_target_reader
        self.execution_progress_callback = execution_progress_callback

    def _managed_background_enabled(self) -> bool:
        return bool(
            self.background_execution_launcher is not None
            or self.background_execution_inspector is not None
            or self.background_execution_stopper is not None
        )

    def _find_actor_run(
        self,
        *,
        cold_start_id: str | None = None,
    ) -> Any | None:
        """Resolve one exact run by its explicit business identifier."""
        run_id = _clean(cold_start_id)
        if not run_id:
            raise StateTransitionError(
                "cold-start status/stop/resume requires the exact cold_start_id"
            )
        base_query = (
            "SELECT configuration.configuration_id, configuration.status AS configuration_status, "
            "run.cold_start_id, run.status AS run_status, run.created_at, run.completed_at "
            "FROM stage0_cold_start_configuration configuration "
            "JOIN stage0_cold_start run ON run.cold_start_id=configuration.cold_start_id "
            "AND run.data_identity=configuration.data_identity "
            "WHERE configuration.data_identity=? AND run.data_identity=? "
            "AND run.cold_start_id=? "
        )
        params: tuple[Any, ...] = (
            self.core.data_identity,
            self.core.data_identity,
            run_id,
        )
        self.core.require_domain_activation_schema()
        base_query += (
            "AND EXISTS (SELECT 1 FROM stage0_domain_activation current_activation "
            "WHERE current_activation.domain_label=run.domain_label "
            "AND current_activation.cold_start_id=run.cold_start_id "
            "AND current_activation.data_identity=? AND current_activation.is_current=1) "
        )
        params += (self.core.data_identity,)
        row = self.core.conn.execute(base_query, params).fetchone()
        if row is None:
            raise StateTransitionError(
                "the requested cold-start run does not exist in this data identity"
            )
        return row

    def _inspect_background(self, *, cold_start_id: str) -> dict[str, Any]:
        inspector = self.background_execution_inspector
        if inspector is None:
            raise StateTransitionError(
                "managed cold-start status requires an injected executor inspector"
            )
        return dict(inspector(cold_start_id=cold_start_id))

    def _stop_background(self, *, cold_start_id: str) -> dict[str, Any]:
        stopper = self.background_execution_stopper
        if stopper is None:
            raise StateTransitionError(
                "managed cold-start stop requires an injected executor stopper"
            )
        return dict(stopper(cold_start_id=cold_start_id))

    def _launch_background(
        self,
        *,
        configuration_id: str,
        cold_start_id: str,
        actor: str,
        notification_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        launcher = self.background_execution_launcher
        if launcher is None:
            raise StateTransitionError(
                "managed cold-start execution requires an injected executor launcher"
            )
        # The detached worker needs a non-empty diagnostic execution label.
        # It is not a run owner, operator, or permission identity.
        execution_actor = "system"
        return dict(
            launcher(
                configuration_id=configuration_id,
                cold_start_id=cold_start_id,
                actor=execution_actor,
                notification_target=(dict(notification_target) if notification_target else None),
            )
        )

    @staticmethod
    def _notification_target(
        value: Mapping[str, Any] | None,
    ) -> dict[str, str] | None:
        if not isinstance(value, Mapping):
            return None
        platform = _clean(value.get("platform")).lower()
        chat_id = _clean(value.get("chat_id"))
        if platform != "feishu" or not chat_id:
            return None
        target = {
            "platform": "feishu",
            "chat_id": chat_id,
        }
        thread_id = _clean(value.get("thread_id"))
        if thread_id:
            target["thread_id"] = thread_id
        return target

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
        actor_value = _clean(actor)
        change_reason = _clean(reason)
        if mode not in {"manual_guard", "mature_automatic"}:
            raise StateTransitionError(
                "workflow mode must be manual_guard or mature_automatic"
            )
        if not actor_value or not change_reason:
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
        self.core._audit(
            None,
            "domain_workflow_mode_changed",
            {
                "domain_label": label,
                "previous_mode": previous,
                "workflow_mode": mode,
                "actor": actor_value,
                "reason": change_reason,
            },
        )
        return {
            "domain_label": label,
            "previous_mode": previous,
            "workflow_mode": mode,
            "actor": actor_value,
            "reason": change_reason,
        }

    def overlap_suggestions(self, *, domain_name: str) -> list[dict[str, Any]]:
        proposed = _text_features(domain_name)
        suggestions: list[dict[str, Any]] = []
        for item in self.list_domains():
            existing = _text_features(item["name"])
            union = proposed | existing
            score = len(proposed & existing) / len(union) if union else 0.0
            exact_name = re.sub(r"\s+", "", domain_name).lower() == re.sub(r"\s+", "", item["name"]).lower()
            suggestions.append({
                "domain_label": item["domain_label"],
                "name": item["name"],
                "overlap_score": round(1.0 if exact_name else score, 3),
                "recommendation": "可能重复，请核对领域名称" if exact_name or score >= 0.24 else "名称不同",
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
            domain_label = existing_label
        else:
            domain_name = _clean(payload.get("domain_name"))
            domain_label = "domain_" + hashlib.sha256(
                domain_name.encode("utf-8")
            ).hexdigest()[:12]
        return {
            "domain_mode": domain_mode,
            "existing_domain_label": existing_label,
            "domain_label": domain_label,
            "domain_name": domain_name,
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
            # Feishu/Hermes identity is transport context only.
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
            if not normalized["domain_name"]:
                block("domain", "新领域必须填写领域名称")
            if any(item["overlap_score"] == 1.0 for item in self.overlap_suggestions(
                domain_name=normalized["domain_name"],
            )):
                block("duplicate_domain", "领域名称与已有领域相同，请直接复用")
        if normalized["platform"] not in SUPPORTED_PLATFORMS:
            block("platform", "当前只支持抖音账号")
        if not normalized["owned_account"]["display_name"] or not normalized["owned_account"]["external_account_ref"]:
            block("owned_account", "请填写自营账号名称和可识别的主页链接或账号标识")
        competitors = normalized["competitor_accounts"]
        if len(competitors) != COLD_START_COMPETITOR_MIN:
            block(
                "competitor_account_count",
                f"冷启动必须填写恰好 {COLD_START_COMPETITOR_MIN} 个对标账号，当前有 {len(competitors)} 个",
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
        suggestions = self.overlap_suggestions(
            domain_name=normalized["domain_name"],
        ) if normalized["domain_mode"] == "create" else []
        return {
            "ready_to_confirm": not blockers,
            "blockers": blockers,
            "normalized": normalized,
            "overlap_suggestions": suggestions,
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
            return path, None
        pack = get_domain_pack(normalized["domain_label"])
        path = Path(str(pack["config_path"]))
        original = path.read_text(encoding="utf-8")
        if _clean(pack.get("activation_status")) not in {"approved", "active", "approved_for_phase8_pilot"}:
            updated = {key: value for key, value in pack.items() if key != "config_path"}
            updated["activation_status"] = "approved"
            self._write_pack(path=path, payload=updated)
            return path, original
        return path, None

    @staticmethod
    def _input_snapshot(normalized: dict[str, Any]) -> dict[str, Any]:
        return {
            "domain_label": normalized["domain_label"],
            "domain_name": normalized["domain_name"],
            "platform": normalized["platform"],
            "owned_account": dict(normalized["owned_account"]),
            "competitor_accounts": [dict(item) for item in normalized["competitor_accounts"]],
        }

    def _automatic_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        trusted_internal_context: dict[str, Any] | None = None,
        task_model_binding: dict[str, Any] | None = None,
        notification_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one run, then detach production execution from this call."""
        del task_model_binding
        configuration = self.core.get_cold_start_configuration(
            configuration_id=configuration_id
        )
        existing_run_id = str(configuration.get("cold_start_id") or "").strip()
        if existing_run_id:
            run = self.core.conn.execute(
                "SELECT status FROM stage0_cold_start "
                "WHERE cold_start_id=? AND data_identity=?",
                (existing_run_id, self.core.data_identity),
            ).fetchone()
            if run is not None:
                registrations = self.core.conn.execute(
                    "SELECT registration_id FROM stage0_competitor_registration "
                    "WHERE cold_start_id=? AND data_identity=? "
                    "ORDER BY created_at, registration_id",
                    (existing_run_id, self.core.data_identity),
                ).fetchall()
                return {
                    "configuration_id": configuration_id,
                    "cold_start_id": existing_run_id,
                    "registration_ids": [
                        str(row["registration_id"]) for row in registrations
                    ],
                    "status": str(run["status"]),
                    "run_model": "",
                    "run_model_binding": {},
                    "execution": {
                        "status": "background_already_started",
                        "started": False,
                        "cold_start_id": existing_run_id,
                    },
                    "automatic_start": False,
                    "background_executor_reused": True,
                    "cold_start_contract_version": COLD_START_CONTRACT_VERSION,
                }
        normalized_notification_target = self._notification_target(notification_target)
        use_background = (
            self.background_execution_launcher is not None
            or (
                self.core.data_identity == "production"
                and self.execution_registration_service is None
            )
        )
        if (
            use_background
            and self.core.data_identity == "production"
            and self.background_execution_launcher is None
            and normalized_notification_target is None
        ):
            raise StateTransitionError(
                "formal cold-start background execution requires the current Feishu chat target"
            )
        result = self.core.start_configured_cold_start(
            configuration_id=configuration_id,
            actor=_clean(actor),
        )
        if use_background:
            try:
                execution = self._launch_background(
                    configuration_id=str(result["configuration_id"]),
                    cold_start_id=str(result["cold_start_id"]),
                    notification_target=normalized_notification_target,
                    actor=_clean(actor),
                )
                result = {
                    **result,
                    "status": "running",
                    "execution": dict(execution),
                }
            except Exception as exc:
                run = self.core.conn.execute(
                    "SELECT status FROM stage0_cold_start "
                    "WHERE cold_start_id=? AND data_identity=?",
                    (str(result["cold_start_id"]), self.core.data_identity),
                ).fetchone()
                if run is not None and str(run["status"]) == "failed":
                    closure = {
                        "configuration_id": str(result["configuration_id"]),
                        "cold_start_id": str(result["cold_start_id"]),
                        "status": "failed",
                        "resumable": True,
                        "reason": str(exc),
                    }
                else:
                    closure = self.core.fail_configured_cold_start(
                        configuration_id=str(result["configuration_id"]),
                        actor=_clean(actor),
                        reason=f"background launch failed: {exc}",
                    )
                result = {
                    **result,
                    "status": "failed",
                    "execution": {
                        "status": "background_launch_failed",
                        "started": False,
                        "cold_start_id": str(result["cold_start_id"]),
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                        "run_closure": closure,
                    },
                }
        else:
            # Isolated callers may still inject the existing registration
            # boundary and exercise orchestration synchronously.
            result = self._run_current_cold_start_execution(
                result=result,
                actor=actor,
                trusted_internal_context=trusted_internal_context,
            )
        return {
            **result,
            "automatic_start": bool(
                (result.get("execution") or {}).get("started", True)
            ),
            "cold_start_contract_version": COLD_START_CONTRACT_VERSION,
        }

    def _run_current_cold_start_execution(
        self,
        *,
        result: dict[str, Any],
        actor: str,
        trusted_internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enter the configured content execution chain after run creation.

        Production uses the configured external-boundary executor. Isolated
        verification may inject the equivalent registration service, but the
        orchestrator and all business-state transitions remain the same.
        """
        execution_report: dict[str, Any] | None = None
        if self.core.data_identity == "production":
            try:
                execution_report = self.inspect_configuration(
                    str(result["configuration_id"]),
                    trusted_internal_context=trusted_internal_context,
                )
            except Exception as exc:
                # The report is diagnostic only.  A failed inspection must not
                # turn an already-created run back into a creation failure.
                execution_report = {
                    "ready": False,
                    "checks": [],
                    "inspection_error": {
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                }

        from scripts.core.production.cold_start_orchestrator import (
            ColdStartExecutionOrchestrator,
        )
        from scripts.core.production.stage1_competitor_registration import (
            CompetitorRegistrationService,
            build_configured_competitor_registration_executor,
        )
        from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired

        try:
            registration_service = self.execution_registration_service
            if registration_service is None:
                if self.core.data_identity != "production":
                    # Existing isolated unit callers that only exercise the
                    # onboarding boundary do not opt into content execution.
                    # Full isolated runs pass an external-boundary substitute
                    # and therefore use the exact production orchestration path.
                    return result
                registration_progress_callback = None
                if self.execution_progress_callback is not None:
                    def registration_progress_callback(
                        phase: str, detail: str,
                    ) -> None:
                        self.execution_progress_callback({
                            "phase": phase, "detail": detail,
                        })
                executor = build_configured_competitor_registration_executor(
                    self.core,
                    progress_callback=registration_progress_callback,
                )
                registration_service = CompetitorRegistrationService(
                    core=self.core, executor=executor
                )
            external_executor = getattr(
                getattr(registration_service, "executor", None),
                "external_executor",
                None,
            )
            execution = ColdStartExecutionOrchestrator(
                core=self.core, registration_service=registration_service,
                progress_callback=self.execution_progress_callback,
                external_executor=external_executor,
            ).run(
                cold_start_id=str(result["cold_start_id"]),
                actor=_clean(actor),
                idempotency_key=f"cold-start-orchestrator:{result['cold_start_id']}",
            )
        except ExternalIntelligenceRequired as exc:
            execution = {
                "status": "requires_external_intelligence",
                "started": False,
                "resumable": True,
                "task": exc.task,
                "preflight": execution_report,
            }
        except Exception as exc:
            # Close the execution ledger on process/runtime failure while
            # preserving every completed artifact for an explicit same-run
            # resume.
            closure: dict[str, Any] | None = None
            try:
                closure = self.core.fail_configured_cold_start(
                    configuration_id=str(result["configuration_id"]),
                    actor=_clean(actor),
                    reason=f"execution failed: {exc}",
                )
            except Exception:
                closure = None
            execution = {
                "status": "execution_failed",
                "started": False,
                "resumable": True,
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "run_closure": closure,
                "preflight": execution_report,
            }
        if (
            isinstance(execution, dict)
            and (
                execution.get("status") == "running_with_resumable_failures"
                or bool(execution.get("failures"))
            )
        ):
            first_failure = next(
                (
                    item for item in execution.get("failures", [])
                    if isinstance(item, dict)
                ),
                {},
            )
            closure = self.core.fail_configured_cold_start(
                configuration_id=str(result["configuration_id"]),
                actor=_clean(actor),
                reason=str(first_failure.get("reason") or "execution returned a resumable failure"),
            )
            execution = {
                **execution,
                "status": "execution_failed",
                "resumable": True,
                "run_closure": closure,
            }
        if isinstance(execution, dict) and execution.get("status") == "execution_failed":
            run_status = str((execution.get("run_closure") or {}).get("status") or "failed")
        else:
            run_status = str(
                self.core.refresh_cold_start_run_lifecycle(
                    cold_start_id=str(result["cold_start_id"]),
                    actor=_clean(actor),
                )["status"]
            )
        return {**result, "status": run_status, "execution": execution}
    def confirm(
        self,
        payload: dict[str, Any],
        *,
        transport_actor: str = "",
        trusted_internal_context: dict[str, Any] | None = None,
        notification_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one run from the normalized preview held by the caller."""
        preview = self.preview(payload)
        if not preview["ready_to_confirm"]:
            raise StateTransitionError("cold-start configuration still has unresolved required items")
        normalized = preview["normalized"]
        self.core.require_domain_zero_state(domain_label=normalized["domain_label"])
        path: Path | None = None
        restore_text: str | None = None
        configuration_id: str | None = None
        try:
            path, restore_text = self._activate_domain(normalized)
            configuration = self.core.configure_cold_start_subjects(
                domain_mode=normalized["domain_mode"],
                domain_label=normalized["domain_label"],
                domain_name=normalized["domain_name"],
                platform=normalized["platform"],
                owned_account=normalized["owned_account"],
                competitor_accounts=tuple(normalized["competitor_accounts"]),
                actor=_clean(transport_actor),
            )
            configuration_id = str(configuration["configuration_id"])
            result = self._automatic_start(
                configuration_id=configuration_id,
                actor=_clean(transport_actor),
                trusted_internal_context=trusted_internal_context,
                notification_target=notification_target,
            )
        except Exception as exc:
            if configuration_id:
                try:
                    self.core.discard_unstarted_cold_start_configuration(
                        configuration_id=configuration_id,
                    )
                except Exception:
                    pass
            try:
                self.core.record_cold_start_onboarding_failure(
                    configuration_id=configuration_id,
                    domain_label=normalized["domain_label"],
                    input_snapshot=self._input_snapshot(normalized),
                    reason=str(exc),
                )
            finally:
                if path is not None and restore_text is not None:
                    path.write_text(restore_text, encoding="utf-8")
                elif path is not None and normalized["domain_mode"] == "create" and path.exists():
                    path.unlink()
            raise StateTransitionError(f"冷启动启动前检查或创建失败：{exc}") from exc
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        return {**configuration, **result}

    def inspect_configuration(
        self,
        configuration_id: str,
        *,
        trusted_internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        report = LiveColdStartPreflight(
            core=self.core, environment=self.preflight_environment,
        ).inspect(
            LiveColdStartRequest(
                domain_label=configuration["domain_label"],
                owned_account_id=configuration["owned_account_id"],
                competitor_account_ids=tuple(configuration["competitor_account_ids"]),
            ),
            trusted_internal_context=trusted_internal_context,
        )
        labels = {
            "formal_production_identity": ("已连接正式业务数据", "当前是隔离验证数据，不能启动真实采集"),
            "domain_pack_registered": ("领域配置已经登记", "领域配置尚未登记"),
            "domain_business_inputs_approved": ("领域和账号输入已经确认", "领域仍在等待正式业务输入"),
            "owned_account_registered": ("自营账号已经正式登记", "自营账号标识不完整或尚未登记"),
            "competitor_accounts_registered": ("全部对标账号已经正式登记", "对标账号存在缺失、重复或领域不一致"),
            "mediacrawler_runtime": ("真实平台采集工具可以运行", "真实平台采集工具或运行环境尚未准备好"),
            "mediacrawler_account_session": ("共享抖音采集浏览器已连接", "共享抖音采集浏览器当前未连接"),
            "model_routes": ("分析模型已按明确配置连接", "分析模型配置尚未完整连接"),
            "sensevoice_runtime": ("本地转写能力可以运行", "本地转写环境或模型尚未准备好"),
            "ffmpeg_runtime": ("音视频处理工具可以运行", "音视频处理工具尚未准备好"),
            "domain_runtime_requirements_supported": ("该领域需要的专用能力已经接入", "该领域仍有专用能力没有接入"),
            "music_audience_sessions": ("音乐听众材料来源的登录状态已经准备好", "网易云音乐或豆瓣的独立登录状态尚未准备好"),
            "validated_human_decision_entry": ("正式人工入口已通过真实往返验证", "还没有人工入口通过真实往返验证"),
        }
        guidance = {
            "formal_production_identity": "请切换到正式业务数据后再启动，隔离验证数据不能发起真实采集。",
            "domain_pack_registered": "请先完成领域登记。",
            "domain_business_inputs_approved": "请先确认领域、自营账号和对标账号。",
            "owned_account_registered": "请补全自营账号名称和抖音主页链接。",
            "competitor_accounts_registered": "请检查是否有空账号、重复账号或不属于当前领域的账号。",
            "mediacrawler_runtime": "请先准备抖音采集工具及其专用运行环境。",
            "mediacrawler_account_session": "这是采集浏览器连接诊断，不判断自营账号或目标账号是否登录，也不阻断冷启动创建。",
            "model_routes": "请先完成分析模型的明确配置；写作模型属于正式生产阶段。",
            "sensevoice_runtime": "请先准备本地转写环境和模型。",
            "ffmpeg_runtime": "请先准备音视频处理工具。",
            "domain_runtime_requirements_supported": "该领域声明了尚未接入的专用能力，需要先完成对应连接。",
            "music_audience_sessions": "这只在以后运行音乐人物听众材料采集时需要，不会阻断本次对标账号冷启动；到人物研究阶段再分别登录网易云音乐和豆瓣即可。",
            "validated_human_decision_entry": "请先通过已经完成真实往返验证的人工决定接入完成身份验证。",
        }
        for check in report["checks"]:
            passed, failed = labels.get(check["code"], ("该项已经准备好", "该项尚未准备好"))
            check["detail"] = passed if check["passed"] else failed
            check["guidance"] = "" if check["passed"] else guidance.get(check["code"], "请按提示补齐后重新检查。")
        return report

    def start(self, *, configuration_id: str, actor: str) -> dict[str, Any]:
        """Legacy internal continuation path; new users never see a second start button."""
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        if str(configuration.get("cold_start_id") or "").strip():
            return self.continue_current_cold_start(configuration_id=configuration_id, actor=actor)
        return self._automatic_start(configuration_id=configuration_id, actor=actor)

    def continue_current_cold_start(self, *, configuration_id: str, actor: str, trusted_internal_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Continue the existing run identity without creating another run."""
        configuration = self.core.get_cold_start_configuration(configuration_id=configuration_id)
        cold_start_id = str(configuration.get("cold_start_id") or "").strip()
        if not cold_start_id:
            raise StateTransitionError("there is no current cold-start run to continue")
        run = self.core.conn.execute(
            "SELECT status FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.core.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("the configured current cold-start run does not exist")
        run_status = str(run["status"])
        if run_status in {"stopped", "failed"}:
            self.core.resume_stopped_cold_start(
                configuration_id=configuration_id,
                actor=_clean(actor),
            )
        elif run_status != "running":
            raise StateTransitionError(
                "the current cold-start run has no automatic work to continue"
            )

        result = self.core.start_configured_cold_start(
            configuration_id=configuration_id,
            actor=_clean(actor),
            preflight_receipt_id="",
        )
        return self._run_current_cold_start_execution(result=result, actor=actor, trusted_internal_context=trusted_internal_context)

    def stop_current_cold_start(
        self,
        *,
        reason: str,
        cold_start_id: str | None = None,
        trusted_internal_context: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Stop the one exact run; trusted Feishu context is transport-only."""
        actor_value = _clean(actor)
        reason_value = _clean(reason) or "user requested cold-start stop"
        row = self._find_actor_run(
            cold_start_id=cold_start_id,
        )
        if row is None:
            return {
                "status": "no_cold_start",
                "stopped": False,
                "message": "当前没有冷启动运行。",
            }
        cold_start_id = str(row["cold_start_id"])
        run_status = str(row["run_status"])
        if run_status in {"waiting_human", "completed"}:
            status_snapshot = self.current_cold_start_status(
                actor=actor_value,
                cold_start_id=cold_start_id,
                trusted_internal_context=trusted_internal_context,
            )
            return {
                "status": run_status,
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "resumed": False,
                "created_new_run": False,
                "resume_source": "current_user_cold_start",
                "pending_actions": list(status_snapshot.get("pending_actions") or []),
                "human_confirmation": dict(status_snapshot.get("human_confirmation") or {}),
                "message": ("当前冷启动已完成，无需恢复。" if run_status == "completed" else "当前冷启动正在等待人工确认；恢复不会重跑。"),
            }
        managed = self._managed_background_enabled()
        inspection = (
            self._inspect_background(cold_start_id=cold_start_id)
            if managed
            else {"active": False, "process_exists": False, "managed": False}
        )
        stop_result: dict[str, Any] | None = None
        if managed and (inspection.get("active") or inspection.get("process_exists")):
            stop_result = self._stop_background(cold_start_id=cold_start_id)
            if stop_result.get("active") or stop_result.get("process_exists"):
                raise StateTransitionError(
                    "cold-start executor did not stop; lifecycle was not changed"
                )
        if run_status == "stopped":
            return {
                "configuration_id": str(row["configuration_id"]),
                "cold_start_id": cold_start_id,
                "status": "stopped",
                "stopped": True,
                "created_new_run": False,
                "message": "当前冷启动已经停止。",
                "executor_stop": stop_result,
            }
        if run_status != "running":
            return {
                "configuration_id": str(row["configuration_id"]),
                "cold_start_id": cold_start_id,
                "status": run_status,
                "stopped": False,
                "created_new_run": False,
                "executor_stop": stop_result,
                "message": "当前运行不处于可停止的 running 状态。",
            }
        result = self.core.stop_configured_cold_start(
            configuration_id=str(row["configuration_id"]),
            actor=actor_value,
            reason=reason_value,
        )
        return {
            **result,
            "stopped": True,
            "created_new_run": False,
            "executor_stop": stop_result,
        }

    def current_cold_start_status(
        self,
        *,
        cold_start_id: str | None = None,
        trusted_internal_context: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Return one current real run status with counts derived from Core."""
        actor_value = _clean(actor)
        row = self._find_actor_run(
            cold_start_id=cold_start_id,
        )
        if row is None:
            return {
                "status": "no_cold_start",
                "cold_start_id": None,
                "message": "当前没有冷启动运行。",
            }
        configuration_id = str(row["configuration_id"])
        cold_start_id = str(row["cold_start_id"])
        run_status = str(row["run_status"])
        managed_background = self._managed_background_enabled()
        raw_executor = (
            self._inspect_background(cold_start_id=cold_start_id)
            if managed_background
            else {
                "managed": False,
                "active": False,
                "process_exists": False,
                "responsive": False,
                "reserved": False,
                "state": "unmanaged_isolation",
                "pid": None,
            }
        )
        executor_reconciled = False
        if managed_background and run_status == "running" and not raw_executor.get("active"):
            self.core.fail_configured_cold_start(
                configuration_id=configuration_id,
                actor=actor_value,
                reason="background executor is no longer active",
            )
            run_status = "failed"
            executor_reconciled = True
        executor_status = {
            **raw_executor,
            "active": bool(
                run_status == "running" and raw_executor.get("active")
            ),
            "lifecycle_status": run_status,
            "reconciled_missing_executor": executor_reconciled,
        }
        if run_status == "running":
            executor_status["consistency"] = "consistent"
        elif raw_executor.get("active") or raw_executor.get("process_exists"):
            executor_status["consistency"] = "terminal_lifecycle_with_process"
        elif executor_reconciled:
            executor_status["consistency"] = "missing_executor_marked_failed"
        else:
            executor_status["consistency"] = "consistent"
        report = self.progress(configuration_id)
        summary = dict(report.get("progress_summary") or {})
        stages = dict(summary.get("stages") or {})
        items = dict(summary.get("items") or {})
        selection = dict(summary.get("selection_summary") or {})
        registrations = list(report.get("registrations") or [])
        competitor_total = len(report["configuration"].get("competitor_account_ids") or [])
        owned_total = sum(
            item.get("account_role") == "owned"
            for item in report["configuration"].get("accounts") or []
        )
        prepared = dict(items.get("transcripts_and_comments") or {})
        breakdown = dict(items.get("breakdown") or {})

        tag_library = self.core.get_cold_start_tag_library(
            cold_start_id=cold_start_id
        )
        content_types = self.core.get_cold_start_content_type_candidate(
            cold_start_id=cold_start_id
        )
        domain_boundary = self.core.get_cold_start_domain_boundary_candidate(
            cold_start_id=cold_start_id
        )
        domain_label = str(report["configuration"].get("domain_label") or "").strip()
        boundary_confirmed = bool(
            self.core.cold_start_domain_boundary_is_frozen(
                cold_start_id=cold_start_id
            )
        )
        if boundary_confirmed and domain_boundary is None:
            registry = self.core.get_production_boundary_for_qualification(
                domain_label=domain_label,
                require_frozen=False,
            )
            domain_boundary = {
                "cold_start_id": cold_start_id,
                "domain_label": domain_label,
                "status": "frozen",
                "source": str(
                    (registry.get("provenance") or {}).get("source") or ""
                ),
                "proposal": {
                    "schema_version": "production_boundary_frozen.v1",
                    "source": str(
                        (registry.get("provenance") or {}).get("source") or ""
                    ),
                    "in_boundary_principles": registry.get(
                        "in_boundary_principles", []
                    ),
                    "out_boundary_principles": registry.get(
                        "out_boundary_principles", []
                    ),
                    "unknown_topic_rule": registry.get(
                        "unknown_topic_rule", {}
                    ),
                },
                "freeze_provenance": dict(registry.get("provenance") or {}),
                "model_run": {},
                "data_identity": self.core.data_identity,
            }
        latest_failure_row = self.core.conn.execute(
            "SELECT attempt.source_id, attempt.reason, attempt.created_at "
            "FROM stage0_competitor_breakdown_attempt attempt "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=attempt.registration_id "
            "AND registration.data_identity=attempt.data_identity "
            "WHERE registration.cold_start_id=? AND attempt.data_identity=? "
            "AND attempt.outcome='failed' "
            "ORDER BY attempt.created_at DESC, attempt.breakdown_attempt_id DESC LIMIT 1",
            (cold_start_id, self.core.data_identity),
        ).fetchone()
        latest_failure = (
            {
                "stage": "hit_breakdown",
                "source_id": str(latest_failure_row["source_id"] or ""),
                "reason": str(latest_failure_row["reason"] or ""),
                "at": str(latest_failure_row["created_at"] or ""),
            }
            if latest_failure_row is not None
            else None
        )
        latest_audit = self.core.conn.execute(
            "SELECT action, created_at FROM stage0_audit_event "
            "WHERE data_identity=? AND payload_json LIKE ? "
            "ORDER BY created_at DESC, audit_id DESC LIMIT 1",
            (self.core.data_identity, f"%{cold_start_id}%"),
        ).fetchone()
        latest_update_at = max(
            (
                value
                for value in (
                    str(summary.get("last_activity_at") or ""),
                    str(row["created_at"] or ""),
                    str(row["completed_at"] or ""),
                    (
                        str(latest_audit["created_at"] or "")
                        if latest_audit is not None
                        else ""
                    ),
                )
                if value
            ),
            default=None,
        )

        if run_status == "stopped":
            current_stage = "stopped"
            current_action = "已停止；只保留现有结果，等待用户明确恢复或清理。"
        elif run_status == "failed":
            current_stage = "failed"
            current_action = "执行失败；保留现有结果，等待修复后恢复。"
        elif int(stages.get("historical_collection") or 0) < competitor_total:
            current_stage = "historical_collection"
            current_action = "采集尚未完成的对标账号历史材料。"
        elif int(stages.get("baseline_calculation") or 0) < competitor_total:
            current_stage = "baseline_calculation"
            current_action = "建立尚未完成的账号基线。"
        elif int(stages.get("hit_filtering") or 0) < competitor_total:
            current_stage = "hit_filtering"
            current_action = "筛选尚未完成的高信号内容。"
        elif int(prepared.get("completed") or 0) < int(prepared.get("total") or 0):
            current_stage = "hit_material_preparation"
            current_action = "准备详情、评论、音频和转写材料。"
        elif int(breakdown.get("completed") or 0) < int(breakdown.get("total") or 0):
            current_stage = "hit_breakdown"
            current_action = "逐条拆解已经备料的高信号内容。"
        elif tag_library is None:
            current_stage = "domain_tag_library"
            current_action = "生成待确认的领域标签库。"
        elif content_types is None or content_types.get("status") != "frozen":
            current_stage = "content_type_review"
            current_action = "等待内容类型候选生成或用户确认。"
        elif not boundary_confirmed:
            current_stage = "domain_boundary_review"
            current_action = "等待领域生产边界候选生成或用户确认。"
        else:
            current_stage = "completion"
            current_action = "完成冷启动收口。"

        tags_confirmed = str((tag_library or {}).get("status") or "") == "accepted"
        content_types_confirmed = bool(
            content_types
            and content_types.get("status") == "frozen"
            and self.core.cold_start_content_types_are_frozen(cold_start_id=cold_start_id)
        )
        pending_actions: list[str] = []
        if not tags_confirmed:
            pending_actions.append("review_tags")
        if not content_types_confirmed:
            pending_actions.append("review_content_types")
        if not boundary_confirmed:
            pending_actions.append("review_domain_boundary")

        stage_report = [
            {
                "stage": "run_created",
                "status": "completed",
                "run_count": 1,
            },
            {
                "stage": "account_registration",
                "status": "completed" if len(registrations) == competitor_total else "in_progress",
                "owned_account_count": int(owned_total),
                "competitor_account_count": len(registrations),
                "processing_account_count": len(registrations),
                "expected_competitor_count": competitor_total,
            },
            {
                "stage": "historical_collection",
                "completed_account_count": int(stages.get("historical_collection") or 0),
                "account_total": competitor_total,
                "historical_item_count": int(selection.get("historical_item_count") or 0),
            },
            {
                "stage": "baseline_calculation",
                "completed_account_count": int(stages.get("baseline_calculation") or 0),
                "account_total": competitor_total,
                "baseline_item_count": int(selection.get("baseline_item_count") or 0),
            },
            {
                "stage": "hit_filtering",
                "completed_account_count": int(stages.get("hit_filtering") or 0),
                "account_total": competitor_total,
                "selected_hit_count": int(selection.get("selected_hit_count") or 0),
            },
            {
                "stage": "hit_material_preparation",
                **prepared,
            },
            {
                "stage": "hit_breakdown",
                **breakdown,
            },
            {
                "stage": "domain_tag_library",
                "status": str((tag_library or {}).get("status") or "not_created"),
            },
            {
                "stage": "content_type_review",
                "status": str((content_types or {}).get("status") or "not_created"),
            },
            {
                "stage": "domain_boundary_review",
                "status": str((domain_boundary or {}).get("status") or "not_created"),
            },
            {
                "stage": "completion",
                "status": run_status,
            },
        ]
        collection_succeeded = int(stages.get("historical_collection") or 0) > 0
        return {
            "status": run_status,
            "configuration_status": str(row["configuration_status"]),
            "configuration_id": configuration_id,
            "cold_start_id": cold_start_id,
            "current_stage": current_stage,
            "latest_update_at": latest_update_at,
            "run_model": "",
            "run_model_source": "external_executor",
            "current_action": current_action,
            "latest_failure": latest_failure,
            "executor": executor_status,
            "counts": {
                "owned_accounts": int(owned_total),
                "competitor_accounts": len(registrations),
                "historical_items": int(selection.get("historical_item_count") or 0),
                "selected_hits": int(selection.get("selected_hit_count") or 0),
                "prepared_items": int(prepared.get("completed") or 0),
                "breakdown_completed": int(breakdown.get("completed") or 0),
                "breakdown_failed": int(breakdown.get("failed") or 0),
            },
            "collector_state": {
                "status": (
                    "real_collection_succeeded"
                    if collection_succeeded
                    else "no_successful_collection_recorded"
                ),
                "completed_account_count": int(
                    stages.get("historical_collection") or 0
                ),
                "diagnostic_browser_state_is_not_account_login": True,
            },
            "stage_report": stage_report,
            "human_confirmation": {
                "tags": tag_library,
                "content_types": content_types,
                "production_boundary": domain_boundary,
                "tags_confirmed": tags_confirmed,
                "content_types_confirmed": content_types_confirmed,
                "production_boundary_confirmed": boundary_confirmed,
            },
            "pending_actions": pending_actions,
            "recent_activity": (
                {
                    "action": str(latest_audit["action"] or ""),
                    "at": str(latest_audit["created_at"] or ""),
                }
                if latest_audit is not None
                else None
            ),
        }


    def resume_current_cold_start(
        self,
        *,
        trusted_internal_context: dict[str, Any] | None = None,
        cold_start_id: str | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Resume one stopped or failed run through a new unique executor."""
        actor_value = _clean(actor)
        row = self._find_actor_run(
            cold_start_id=cold_start_id,
        )
        if row is None:
            return {
                "status": "no_unfinished_cold_start",
                "resumed": False,
                "message": "当前没有未完成的冷启动运行，请先创建新的冷启动。",
            }
        configuration_id = str(row["configuration_id"])
        cold_start_id = str(row["cold_start_id"])
        run_status = str(row["run_status"])
        if run_status in {"waiting_human", "completed"}:
            status_snapshot = self.current_cold_start_status(
                actor=actor_value,
                cold_start_id=cold_start_id,
                trusted_internal_context=trusted_internal_context,
            )
            return {
                "status": run_status,
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "resumed": False,
                "created_new_run": False,
                "resume_source": "current_user_cold_start",
                "pending_actions": list(status_snapshot.get("pending_actions") or []),
                "human_confirmation": dict(status_snapshot.get("human_confirmation") or {}),
                "message": ("当前冷启动已完成，无需恢复。" if run_status == "completed" else "当前冷启动正在等待人工确认；恢复不会重跑。"),
            }
        if run_status == "running":
            return {
                "status": "running",
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "resumed": False,
                "created_new_run": False,
                "message": "当前冷启动已经在运行。",
            }
        managed = self._managed_background_enabled()
        inspection = (
            self._inspect_background(cold_start_id=cold_start_id)
            if managed
            else {"active": False, "process_exists": False, "reserved": False}
        )
        bound_notification_target: dict[str, str] | None = None
        if managed and self.background_execution_launcher is None:
            reader = self.background_notification_target_reader
            if reader is None:
                raise StateTransitionError(
                    "managed cold-start resume requires an injected notification target reader"
                )
            bound_notification_target = reader(cold_start_id=cold_start_id)
            if bound_notification_target is None:
                raise StateTransitionError(
                    "the cold-start run has no original Feishu notification target"
                )
        if run_status not in {"stopped", "failed"}:
            raise StateTransitionError(
                "only a stopped or failed cold start can be resumed"
            )
        if inspection.get("active") or inspection.get("process_exists") or inspection.get("reserved"):
            raise StateTransitionError(
                "the existing cold-start executor must exit before resume"
            )
        resume_prior_status = run_status
        lifecycle = self.core.resume_stopped_cold_start(
            configuration_id=configuration_id,
            actor=actor_value,
        )
        if not managed:
            result = self.continue_current_cold_start(
                configuration_id=configuration_id,
                actor=actor_value,
                trusted_internal_context=trusted_internal_context,
            )
            return {
                **result,
                "resumed": True,
                "created_new_run": False,
                "resume_source": "current_user_unfinished_cold_start",
            }
        try:
            execution = self._launch_background(
                configuration_id=configuration_id,
                cold_start_id=cold_start_id,
                actor=actor_value,
                notification_target=bound_notification_target,
            )
            if not bool(execution.get("started")):
                raise StateTransitionError(
                    "background launcher did not confirm executor startup"
                )
        except Exception as exc:
            # The launcher failed before it returned a successful executor
            # startup receipt.  Restore the lifecycle that existed before the
            # resume request; this is not an execution failure.
            rollback_reason = f"background resume launch failed before executor start: {exc}"
            if resume_prior_status == "stopped":
                rollback = self.core.stop_configured_cold_start(
                    configuration_id=configuration_id,
                    actor=actor_value,
                    reason=rollback_reason,
                )
            else:
                rollback = self.core.fail_configured_cold_start(
                    configuration_id=configuration_id,
                    actor=actor_value,
                    reason=rollback_reason,
                )
            return {
                **lifecycle,
                "status": str(rollback.get("status") or resume_prior_status),
                "resumed": False,
                "created_new_run": False,
                "run_model": "",
                "run_model_binding": {},
                "execution": {
                    "status": "background_launch_failed",
                    "started": False,
                    "reason": str(exc),
                    "lifecycle_rollback": rollback,
                },
            }
        return {
            **lifecycle,
            "status": "running",
            "resumed": True,
            "created_new_run": False,
            "resume_source": "current_user_unfinished_cold_start",
            "run_model": "",
            "run_model_binding": {},
            "execution": execution,
        }

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
                        if reason == "payload has unexpected fields":
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
