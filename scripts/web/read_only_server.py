"""Serve the Core unified status through a small local read-only Web page."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from uuid import uuid4
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.core.core_entry import build_status
from scripts.core.formal_business_entrypoints import (
    CreationAssistantFormalBusinessCore,
)
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.runtime.runtime_storage import database_path_for_identity
from scripts.agent_platform.hermes_cold_start_action import HermesColdStartAction


STATIC_ROOT = Path(__file__).resolve().parent / "static"
IDENTITIES = {"production", "test"}


class ReadOnlyWebApplication:
    """Bind one local Web process to one explicit Core data identity."""

    def __init__(self, data_identity: str = "production", database_path: Path | str | None = None) -> None:
        identity = str(data_identity or "").strip().lower()
        if identity not in IDENTITIES:
            raise ValueError("data identity must be explicitly 'production' or 'test'")
        self.data_identity = identity
        self.database_path = Path(database_path) if database_path is not None else None

    def read_status(self) -> dict[str, Any]:
        """Read the authoritative status from Core and nowhere else."""

        return build_status(
            data_identity=self.data_identity,
            database_path=self.database_path,
        )

    def read_experience_candidates(self) -> list[dict[str, Any]]:
        """Read the current pre-topic experience candidates through Core."""

        database = Stage0ContentProductionCore.open_read_only(
            self.database_path or database_path_for_identity(self.data_identity),
            data_identity=self.data_identity,
        )
        try:
            candidates = database.list_pre_topic_experience_candidates()
            source_cards = database.list_experience_candidate_source_cards(
                candidate_ids=[str(item["experience_candidate_id"]) for item in candidates]
            )
            result: list[dict[str, Any]] = []
            for candidate in candidates:
                proposal = candidate.get("proposal")
                proposal = proposal if isinstance(proposal, Mapping) else {}
                body = proposal.get("candidate")
                body = body if isinstance(body, Mapping) else {}
                result.append(
                    {
                        "experience_candidate_id": candidate["experience_candidate_id"],
                        "domain_label": candidate["domain_label"],
                        "status": candidate["status"],
                        "source_count": candidate["source_count"],
                        "candidate": dict(body),
                        "sources": source_cards.get(
                            str(candidate["experience_candidate_id"]), []
                        ),
                    }
                )
            return result
        finally:
            database.close()


class ActionWebApplication(ReadOnlyWebApplication):
    """Bind one local Web process to Core's existing human action boundary."""

    _MUTATING_ACTIONS = frozenset(
        {
            "cold_start_confirm",
            "cold_start_stop",
            "cold_start_resume",
            "review_tags",
            "review_content_types",
            "review_domain_boundary",
            "daily_start",
            "daily_resume",
            "accept_experience_candidate",
            "reject_experience_candidate",
        }
    )
    _ACTION_NAMES = frozenset(
        {
            "cold_start_preview",
            "cold_start_confirm",
            "cold_start_stop",
            "cold_start_resume",
            "review_tags",
            "review_content_types",
            "review_domain_boundary",
            "daily_start",
            "daily_resume",
            "accept_experience_candidate",
            "reject_experience_candidate",
        }
    )

    def __init__(
        self,
        data_identity: str = "production",
        database_path: Path | str | None = None,
        *,
        actor: str = "local-web-user",
        carrier_binding_id: str = "local-web-carrier",
        config_dir: Path | None = None,
        task_model_resolver: Any | None = None,
        preflight_environment: dict[str, str] | None = None,
    ) -> None:
        super().__init__(data_identity=data_identity, database_path=database_path)
        self.actor = str(actor or "").strip()
        self.carrier_binding_id = str(carrier_binding_id or "").strip()
        if not self.actor:
            raise ValueError("Web action actor must be explicit")
        if not self.carrier_binding_id:
            raise ValueError("Web action carrier binding must be explicit")
        self.config_dir = config_dir
        self.task_model_resolver = task_model_resolver
        self.preflight_environment = preflight_environment
        self._session_ref = "local-web-session"
        self._cold_start_transport: HermesColdStartAction | None = None
        self._lock = RLock()

    def _adapter(self, core: Stage0ContentProductionCore) -> ColdStartHumanDecisionAdapter:
        return ColdStartHumanDecisionAdapter(
            core=core,
            config_dir=self.config_dir,
            preflight_environment=self.preflight_environment,
            task_model_resolver=self.task_model_resolver,
        )

    def _cold_start_action(self, adapter: ColdStartHumanDecisionAdapter) -> HermesColdStartAction:
        if self._cold_start_transport is None:
            self._cold_start_transport = HermesColdStartAction(
                adapter=adapter,
                carrier_binding_id=self.carrier_binding_id,
            )
        else:
            self._cold_start_transport.adapter = adapter
        return self._cold_start_transport

    def _current_domain(self, domain_label: Any) -> dict[str, Any]:
        label = str(domain_label or "").strip()
        if not label:
            raise StateTransitionError("Web action requires a domain")
        status = self.read_status()
        for domain in status.get("domains") or []:
            if str(domain.get("domain_identity") or "") == label:
                return dict(domain)
        raise StateTransitionError("the requested domain is not present in Core status")

    @staticmethod
    def _reject_transport_switches(payload: Mapping[str, Any]) -> None:
        if any(
            key in payload
            for key in (
                "data_identity",
                "database_path",
                "identity",
                "cold_start_id",
                "daily_run_id",
                "configuration_id",
            )
        ):
            raise StateTransitionError(
                "Web actions cannot choose Core identities or technical run identifiers"
            )

    def _cold_start_id(self, payload: Mapping[str, Any]) -> str:
        domain = self._current_domain(payload.get("domain_label"))
        current = domain.get("current_activation") or {}
        run_id = str(current.get("cold_start_id") or "").strip()
        if not run_id:
            raise StateTransitionError("the domain has no current cold-start run")
        return run_id

    def _review_command(
        self,
        *,
        action: str,
        cold_start_id: str,
        payload: Mapping[str, Any],
    ) -> FormalHumanDecisionCommand:
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise StateTransitionError("human review requires a reason")
        command_payload: dict[str, Any] = {
            "cold_start_id": cold_start_id,
            "reason": reason,
        }
        if "decisions" in payload:
            decisions = payload.get("decisions")
            if not isinstance(decisions, list):
                raise StateTransitionError("human review decisions must be a list")
            command_payload["decisions"] = [
                dict(item) for item in decisions if isinstance(item, Mapping)
            ]
        for key in ("unknown_topic_rule", "explicit_boundary"):
            if key in payload:
                value = payload.get(key)
                if not isinstance(value, Mapping):
                    raise StateTransitionError(f"{key} must be an object")
                command_payload[key] = dict(value)
        action_names = {
            "review_tags": "review_competitor_tag_library",
            "review_content_types": "review_cold_start_content_types",
            "review_domain_boundary": "review_cold_start_domain_boundary",
        }
        return FormalHumanDecisionCommand(
            command_id=f"web-human-{uuid4().hex}",
            carrier_binding_id=self.carrier_binding_id,
            session_ref=self._session_ref,
            action=action_names[action],
            target_ref=f"cold_start:{cold_start_id}",
            payload=command_payload,
            actor=self.actor,
            actor_kind="user",
        )

    def _experience_candidate_command(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
    ) -> FormalHumanDecisionCommand:
        candidate_id = str(payload.get("experience_candidate_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not candidate_id:
            raise StateTransitionError("experience candidate action requires a candidate")
        if not reason:
            raise StateTransitionError("experience candidate action requires a reason")
        candidate = next(
            (
                item
                for item in self.read_experience_candidates()
                if str(item.get("experience_candidate_id") or "") == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise StateTransitionError("the experience candidate is not in the current Core review set")
        if str(candidate.get("status") or "") != "awaiting_human_decision":
            raise StateTransitionError(
                "only an experience candidate awaiting human decision may be accepted or rejected"
            )
        return FormalHumanDecisionCommand(
            command_id=f"web-human-{uuid4().hex}",
            carrier_binding_id=self.carrier_binding_id,
            session_ref=self._session_ref,
            action=action,
            target_ref=f"experience_candidate:{candidate_id}",
            payload={
                "experience_candidate_id": candidate_id,
                "decision": "accepted" if action == "accept_experience_candidate" else "rejected",
                "reason": reason,
            },
            actor=self.actor,
            actor_kind="user",
        )

    def _invoke_core_action(
        self,
        core: Stage0ContentProductionCore,
        *,
        action: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        business = CreationAssistantFormalBusinessCore(core=core)
        adapter = self._adapter(core)

        if action == "cold_start_preview":
            configuration = payload.get("configuration")
            if not isinstance(configuration, Mapping):
                raise StateTransitionError("cold-start preview requires configuration")
            return dict(
                self._cold_start_action(adapter).invoke(
                    operation="preview",
                    configuration=dict(configuration),
                    actor=self.actor,
                    session_ref=self._session_ref,
                )
            )

        if action == "cold_start_confirm":
            return dict(
                self._cold_start_action(adapter).invoke(
                    operation="confirm",
                    configuration={},
                    actor=self.actor,
                    session_ref=self._session_ref,
                    explicit_user_confirmation=True,
                )
            )

        if action == "cold_start_stop":
            run_id = self._cold_start_id(payload)
            adapter.validate_cold_start_operation(
                operation="stop",
                actor=self.actor,
                cold_start_id=run_id,
                explicit_user_confirmation=True,
            )
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                raise StateTransitionError("stopping a cold-start requires a reason")
            return adapter.stop_current_cold_start(
                actor=self.actor,
                reason=reason,
                cold_start_id=run_id,
            )

        if action == "cold_start_resume":
            run_id = self._cold_start_id(payload)
            adapter.validate_cold_start_operation(
                operation="resume",
                actor=self.actor,
                cold_start_id=run_id,
                explicit_user_confirmation=False,
            )
            return adapter.resume_current_cold_start(
                actor=self.actor,
                cold_start_id=run_id,
            )

        if action in {"review_tags", "review_content_types", "review_domain_boundary"}:
            run_id = self._cold_start_id(payload)
            operation = {
                "review_tags": "review_tags",
                "review_content_types": "review_content_types",
                "review_domain_boundary": "review_domain_boundary",
            }[action]
            explicit_boundary = payload.get("explicit_boundary")
            adapter.validate_cold_start_operation(
                operation=operation,
                actor=self.actor,
                cold_start_id=run_id,
                explicit_user_confirmation=True,
                explicit_boundary=(
                    dict(explicit_boundary)
                    if isinstance(explicit_boundary, Mapping)
                    else None
                ),
            )
            command = self._review_command(
                action=action,
                cold_start_id=run_id,
                payload=payload,
            )
            handler = {
                "review_tags": adapter.review_tag_library,
                "review_content_types": adapter.review_content_types,
                "review_domain_boundary": adapter.review_domain_boundary,
            }[action]
            return dict(handler(command=command))

        if action in {"accept_experience_candidate", "reject_experience_candidate"}:
            command = self._experience_candidate_command(action=action, payload=payload)
            decision = str(command.payload["decision"])
            result = business.submit_human_decision(
                command=command,
                apply_formal_decision=lambda received: self._apply_experience_candidate_decision(
                    core=core,
                    command=received,
                    decision=decision,
                ),
            )
            return dict(result)

        if action == "daily_start":
            domain_label = str(payload.get("domain_label") or "").strip()
            selected_date, domains = business.normalize_daily_request(
                domain_labels=(domain_label,),
                business_date=(
                    str(payload["business_date"])
                    if payload.get("business_date") is not None
                    else None
                ),
                effective_at=None,
                now=datetime.now(timezone.utc),
            )
            if len(domains) != 1:
                raise StateTransitionError("Web daily start requires exactly one domain")
            return dict(
                business.request_daily(
                    domain_label=domains[0],
                    business_date=selected_date,
                    actor=self.actor,
                )
            )

        if action == "daily_resume":
            domain = self._current_domain(payload.get("domain_label"))
            current_daily = domain.get("current_daily") or {}
            daily_run_id = str(current_daily.get("daily_run_id") or "").strip()
            if not daily_run_id:
                raise StateTransitionError("the domain has no current daily run to resume")
            return dict(business.resume_daily(daily_run_id=daily_run_id, actor=self.actor))

        raise StateTransitionError(f"unsupported Web action: {action}")

    @staticmethod
    def _apply_experience_candidate_decision(
        *,
        core: Stage0ContentProductionCore,
        command: FormalHumanDecisionCommand,
        decision: str,
    ) -> dict[str, str]:
        return core.decide_experience_candidate(
            experience_candidate_id=str(command.payload["experience_candidate_id"]),
            decision=decision,
            actor=command.actor,
            actor_kind=command.actor_kind,
            reason=str(command.payload["reason"]),
        )

    def invoke_action(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Run one existing Core action and reread Core status afterwards."""

        if not isinstance(payload, Mapping):
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "动作请求必须是对象"}
        try:
            self._reject_transport_switches(payload)
            action = str(payload.get("action") or "").strip()
            if action not in self._ACTION_NAMES:
                raise StateTransitionError("unsupported Web action")
        except Exception as exc:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "outcome": "rejected", "error": str(exc)}

        with self._lock:
            try:
                if action in self._MUTATING_ACTIONS:
                    CreationAssistantFormalBusinessCore.assert_action_allowed(
                        data_identity=self.data_identity,
                    )
                open_read_only = action == "cold_start_preview"
                database = (
                    Stage0ContentProductionCore.open_read_only
                    if open_read_only
                    else Stage0ContentProductionCore.open
                )
                core = database(
                    self.database_path or database_path_for_identity(self.data_identity),
                    data_identity=self.data_identity,
                )
                try:
                    core_result = self._invoke_core_action(
                        core,
                        action=action,
                        payload=payload,
                    )
                finally:
                    core.close()
                outcome = str(
                    core_result.get("status")
                    or core_result.get("action")
                    or "accepted"
                )
                http_status = HTTPStatus.OK
                error = None
            except Exception as exc:
                core_result = None
                outcome = "rejected"
                http_status = HTTPStatus.OK
                error = str(exc)

            try:
                status = self.read_status()
            except Exception as exc:
                return HTTPStatus.SERVICE_UNAVAILABLE, {
                    "ok": False,
                    "source": "Creation Assistant Core",
                    "outcome": "status_refresh_failed",
                    "error": f"状态刷新失败：{exc}",
                }
            response: dict[str, Any] = {
                "ok": error is None,
                "outcome": outcome,
                "status": status,
            }
            if core_result is not None:
                response["core_result"] = core_result
            if error is not None:
                response["ok"] = False
                response["error"] = error
            return http_status, response


class ReadOnlyRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler with only status and static-asset GET routes."""

    server_version = "CreationAssistantReadOnlyWeb/1.0"

    def __init__(self, *args: Any, application: ReadOnlyWebApplication, **kwargs: Any) -> None:
        self.application = application
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._serve_status()
            return
        if path == "/api/experience-candidates":
            self._serve_experience_candidates()
            return
        if path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "\u672c\u5730\u9875\u9762\u53ea\u5141\u8bb8\u8bfb\u53d6"}, body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _serve_status(self) -> None:
        try:
            status = self.application.read_status()
        except Exception as exc:  # Core/database/config read failures are shown, never replaced.
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "source": "Creation Assistant Core",
                    "error": f"\u72b6\u6001\u8bfb\u53d6\u5931\u8d25\uff1a{exc}",
                },
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "status": status})

    def _serve_experience_candidates(self) -> None:
        try:
            candidates = self.application.read_experience_candidates()
        except Exception as exc:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "source": "Creation Assistant Core",
                    "error": f"experience candidate read failed: {exc}",
                },
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "source": "Creation Assistant Core",
                "candidates": candidates,
            },
        )

    def _serve_static(self, relative_name: str) -> None:
        try:
            candidate = (STATIC_ROOT / relative_name).resolve()
            candidate.relative_to(STATIC_ROOT.resolve())
        except (OSError, ValueError):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})
            return
        if not candidate.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "\u9875\u9762\u4e0d\u5b58\u5728"})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(candidate.suffix, "application/octet-stream")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": f"\u9875\u9762\u8bfb\u53d6\u5931\u8d25\uff1a{exc}"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        payload = json.dumps({"ok": False, "error": "\u672c\u5730\u9875\u9762\u53ea\u5141\u8bb8\u8bfb\u53d6"}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, body: bool = True) -> None:
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if body:
            self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the standard local request log on stderr only."""

        super().log_message(format, *args)


class ActionRequestHandler(ReadOnlyRequestHandler):
    """Read Core status and submit only the fixed Web action endpoint."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/api/action":
            self._method_not_allowed()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "outcome": "rejected", "error": f"动作请求无效：{exc}"},
            )
            return
        status, response = self.application.invoke_action(payload)
        self._send_json(HTTPStatus(status), response)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        payload = json.dumps(
            {"ok": False, "error": "本地页面只允许状态读取和Core正式动作"},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    data_identity: str = "production",
    database_path: Path | str | None = None,
) -> ThreadingHTTPServer:
    """Create a local server; the caller owns its lifecycle."""

    application = ReadOnlyWebApplication(data_identity=data_identity, database_path=database_path)
    handler = partial(ReadOnlyRequestHandler, application=application)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.application = application  # type: ignore[attr-defined]
    return server


def create_action_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    data_identity: str = "production",
    database_path: Path | str | None = None,
    actor: str = "local-web-user",
    carrier_binding_id: str = "local-web-carrier",
    config_dir: Path | None = None,
    task_model_resolver: Any | None = None,
    preflight_environment: dict[str, str] | None = None,
) -> ThreadingHTTPServer:
    """Create the Stage 7 Web server with the existing Core action boundary."""

    application = ActionWebApplication(
        data_identity=data_identity,
        database_path=database_path,
        actor=actor,
        carrier_binding_id=carrier_binding_id,
        config_dir=config_dir,
        task_model_resolver=task_model_resolver,
        preflight_environment=preflight_environment,
    )
    handler = partial(ActionRequestHandler, application=application)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.application = application  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Creation Assistant local Core Web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-identity", choices=("production", "test"), default="production")
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--actor", default="local-web-user")
    parser.add_argument("--carrier-binding-id", default="local-web-carrier")
    args = parser.parse_args(argv)
    server = create_action_server(
        args.host,
        args.port,
        data_identity=args.data_identity,
        database_path=args.database_path,
        actor=args.actor,
        carrier_binding_id=args.carrier_binding_id,
    )
    print(f"Creation Assistant local Core Web: http://{args.host}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
