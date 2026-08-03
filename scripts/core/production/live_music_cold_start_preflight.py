from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from scripts.core.business_data.domain_labels import get_domain_pack
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor
from scripts.core.model_gateway.model_router import ModelRouter
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError


APPROVED_ACTIVATION_STATES = frozenset({"approved", "active", "approved_for_phase8_pilot"})


@dataclass(frozen=True)
class LiveColdStartRequest:
    domain_label: str
    owned_account_id: str
    competitor_account_ids: tuple[str, ...]
    voice_profile_id: str = ""


class LiveColdStartPreflight:
    """One generic, fail-closed gate before any domain's first real external run."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        repo_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.core = core
        self.repo_root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self.dotenv_path = self.repo_root / ".env"
        self.runtime_env_path = self.repo_root / ".env.runtime.local"

    def _env(self, key: str) -> tuple[str, str | None]:
        process_value = str(self.environment.get(key) or "").strip()
        file_values: list[str] = []
        for path in (self.runtime_env_path, self.dotenv_path):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{key}="):
                    file_values.append(line.split("=", 1)[1].strip().strip('"').strip("'"))
                    break
        values = {value for value in [process_value, *file_values] if value}
        if len(values) > 1:
            return "", "process and local configuration values conflict"
        value = next(iter(values), "")
        return value, None if value else "configuration value is unresolved"

    @staticmethod
    def _check(
        code: str,
        passed: bool,
        blocker_kind: str,
        detail: str,
        *,
        required_for_cold_start: bool = True,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "passed": bool(passed),
            "blocker_kind": blocker_kind,
            "detail": detail,
            "required_for_cold_start": required_for_cold_start,
        }

    @staticmethod
    def _profile_has_state(path: Path) -> bool:
        return path.is_dir() and any(item.is_file() for item in path.rglob("*"))

    def inspect(self, request: LiveColdStartRequest) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        add = checks.append
        add(self._check(
            "formal_production_identity", self.core.data_identity == "production", "formal_data",
            "formal production database is bound" if self.core.data_identity == "production" else "not bound to formal production data",
        ))
        try:
            pack = get_domain_pack(request.domain_label)
            pack_found = True
        except ValueError:
            pack, pack_found = {}, False
        add(self._check("domain_pack_registered", pack_found, "business_input", "domain pack exists" if pack_found else "domain pack is missing"))
        activation = str(pack.get("activation_status") or "")
        add(self._check(
            "domain_business_inputs_approved", activation in APPROVED_ACTIVATION_STATES, "business_input",
            "domain business inputs are approved" if activation in APPROVED_ACTIVATION_STATES else f"domain activation is {activation or 'unset'}",
        ))
        discovery = pack.get("discovery") if isinstance(pack.get("discovery"), dict) else {}
        topic_search = discovery.get("topic_search") if isinstance(discovery.get("topic_search"), dict) else {}
        # Tags and keywords are learned after the competitor-registration cold start.
        # They are deliberately not cold-start form requirements.

        owned = self.core.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (request.owned_account_id, self.core.data_identity),
        ).fetchone()
        owned_ok = bool(
            owned is not None and owned["account_role"] == "owned" and owned["status"] == "active"
            and owned["domain_label"] == request.domain_label and str(owned["external_account_ref"] or "").strip()
        )
        add(self._check("owned_account_registered", owned_ok, "business_input", "owned account is formally registered" if owned_ok else "owned account identity is incomplete or not registered"))
        competitor_rows = [
            self.core.conn.execute(
                "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
                (account_id, self.core.data_identity),
            ).fetchone()
            for account_id in request.competitor_account_ids
        ]
        competitors_ok = bool(request.competitor_account_ids) and len(set(request.competitor_account_ids)) == len(request.competitor_account_ids) and all(
            row is not None and row["account_role"] == "competitor" and row["status"] == "active"
            and row["domain_label"] == request.domain_label and str(row["external_account_ref"] or "").strip()
            for row in competitor_rows
        )
        add(self._check("competitor_accounts_registered", competitors_ok, "business_input", "competitor accounts are formally registered" if competitors_ok else "competitor account identities are missing, duplicated or outside the domain"))

        vendor = self.repo_root / "vendor" / "MediaCrawler"
        media_python_value, media_python_error = self._env("MEDIACRAWLER_PYTHON")
        media_python_candidates = [Path(media_python_value)] if media_python_value else [
            vendor / ".venv" / "Scripts" / "python.exe", vendor / ".venv312" / "Scripts" / "python.exe",
        ]
        if self.repo_root == Path(__file__).resolve().parents[3]:
            media_problems = LocalMediaCrawlerExecutor(
                python_executable=Path(media_python_value) if media_python_value else None
            ).readiness_problems()
            media_runtime_ok = not media_problems
            media_runtime_detail = "MediaCrawler code and usable Python runtime are present" if media_runtime_ok else "; ".join(media_problems)
        else:
            media_runtime_ok = (vendor / "main.py").is_file() and any(path.is_file() for path in media_python_candidates)
            media_runtime_detail = "MediaCrawler code and Python runtime are present" if media_runtime_ok else media_python_error or "MediaCrawler code or Python runtime is missing"
        add(self._check("mediacrawler_runtime", media_runtime_ok, "runtime", media_runtime_detail))
        media_profile_ok = self._profile_has_state(vendor / "browser_data")
        add(self._check("mediacrawler_account_session", media_profile_ok, "runtime_login", "MediaCrawler retained browser state exists" if media_profile_ok else "MediaCrawler own-account login state has not been prepared"))

        try:
            router = ModelRouter.from_file(self.repo_root / "config" / "model_routes.yaml")
            for route_id in ("business_analysis", "writing_generation"):
                router.resolve_bound_route(route_id, environment=self.environment, env_path=self.dotenv_path)
            model_routes_ok, model_routes_detail = True, "business and writing model routes resolve with no fallback"
        except Exception as exc:
            model_routes_ok, model_routes_detail = False, f"model route is unresolved: {exc}"
        for ref in ("HERMES_BUSINESS_API_KEY", "HERMES_BUSINESS_BASE_URL"):
            value, error = self._env(ref)
            if error:
                model_routes_ok, model_routes_detail = False, f"{ref}: {error}"
            if ref.endswith("BASE_URL") and value and not value.startswith(("http://", "https://")):
                model_routes_ok, model_routes_detail = False, f"{ref}: endpoint is not HTTP(S)"
        add(self._check("model_routes", model_routes_ok, "runtime_configuration", model_routes_detail))

        sense_python, sense_python_error = self._env("SENSEVOICE_PYTHON")
        sense_worker, sense_worker_error = self._env("SENSEVOICE_WORKER")
        sense_model, sense_model_error = self._env("SENSEVOICE_ASR_MODEL")
        sense_vad, sense_vad_error = self._env("SENSEVOICE_VAD_MODEL")
        sense_ok = Path(sense_python).is_file() and Path(sense_worker).is_file() and bool(sense_model and sense_vad)
        add(self._check("sensevoice_runtime", sense_ok, "runtime", "SenseVoice runtime and configured models are present" if sense_ok else sense_python_error or sense_worker_error or sense_model_error or sense_vad_error or "SenseVoice runtime file is missing"))
        ffmpeg, ffmpeg_error = self._env("FFMPEG_PATH")
        add(self._check("ffmpeg_runtime", Path(ffmpeg).is_file(), "runtime", "ffmpeg runtime is present" if Path(ffmpeg).is_file() else ffmpeg_error or "ffmpeg executable is missing"))

        supported_runtime_requirements = {"music_audience_browser"}
        requested_requirements = {str(item) for item in pack.get("runtime_requirements", [])}
        unknown_requirements = requested_requirements - supported_runtime_requirements
        add(self._check("domain_runtime_requirements_supported", not unknown_requirements, "code_connector", "domain runtime requirements are supported" if not unknown_requirements else "unsupported runtime requirement: " + ", ".join(sorted(unknown_requirements))))
        if "music_audience_browser" in requested_requirements:
            audience_python, _ = self._env("MUSIC_AUDIENCE_PYTHON")
            netease_profile, _ = self._env("NETEASE_MUSIC_PROFILE_DIR")
            douban_profile, _ = self._env("DOUBAN_MUSIC_PROFILE_DIR")
            audience_ok = (
                Path(audience_python).is_file()
                and (self.repo_root / "scripts" / "core" / "external_adapters" / "music_audience_worker.py").is_file()
                and self._profile_has_state(Path(netease_profile))
                and self._profile_has_state(Path(douban_profile))
            )
            add(self._check(
                "music_audience_sessions",
                audience_ok,
                "runtime_login",
                "separate NetEase and Douban signed-in profiles are ready" if audience_ok else "music audience Python or separate signed-in profiles are not ready",
                required_for_cold_start=False,
            ))

        carriers = self.core.list_validated_human_decision_carriers()
        add(self._check("validated_human_decision_entry", bool(carriers), "human_architecture", "a real decision carrier round trip has been validated" if carriers else "no real Codex, Hermes or Feishu decision carrier has passed round-trip validation"))
        ready = bool(checks) and all(
            item["passed"] or not item["required_for_cold_start"]
            for item in checks
        )
        return {"domain_label": request.domain_label, "ready": ready, "checks": checks}

    def require_ready(self, request: LiveColdStartRequest) -> dict[str, Any]:
        report = self.inspect(request)
        if not report["ready"]:
            blockers = [
                item["code"]
                for item in report["checks"]
                if item["required_for_cold_start"] and not item["passed"]
            ]
            raise StateTransitionError("live cold start is blocked before external execution: " + ", ".join(blockers))
        receipt = self.core.record_live_cold_start_preflight(
            domain_label=request.domain_label, owned_account_id=request.owned_account_id,
            competitor_account_ids=request.competitor_account_ids, voice_profile_id=request.voice_profile_id,
            report=report,
        )
        return {**report, **receipt}
