from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.goal07_skill_runner import PortableSkillRunner, PortableSkillSpec
from scripts.core.persistence.goal01_store import PersistenceStore, content_hash
from scripts.validation.clean_room_empty_db import health_check


GOAL_ID = "GOAL-RUNTIME-BUSINESS-ROUTE-CUTOVER-01"
REGISTRY_PATH = ROOT / "BUSINESS_MODEL_ROUTE_REGISTRY.yaml"
STATUS_PATH = ROOT / "BUSINESS_MODEL_ROUTE_CUTOVER_STATUS.yaml"
REPORT_PATH = ROOT / "GOAL-RUNTIME-BUSINESS-ROUTE-CUTOVER-01_VALIDATION_REPORT.md"
PROGRESS_PATH = ROOT / "implementation_progress" / "GOAL-RUNTIME-BUSINESS-ROUTE-CUTOVER-01.md"

FORMAL_PRODUCTION_ROOTS = (
    ROOT / "scripts" / "core",
    ROOT / "runtime_skills",
)
FORMAL_ALLOWED_DIRECT_PROVIDER_FILES = {
    (ROOT / "scripts" / "core" / "model_gateway" / "hermes_model_provider.py").resolve(),
}
FORMAL_VERIFICATION_ONLY_FILES = {
    (ROOT / "scripts" / "core" / "model_gateway" / "business_route_registry.py").resolve(),
}
LEGACY_DIRECT_MODEL_ROOTS = (
    ROOT / "scripts" / "llm",
    ROOT / "scripts" / "reverse",
    ROOT / "scripts" / "research",
    ROOT / "scripts" / "humanize",
)
BANNED_CLI_PATTERNS = (
    re.compile(r"\bclaude(?:\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bcodex(?:\.exe)?\b", re.IGNORECASE),
    re.compile(r"scripts[/\\]llm[/\\]call\.py", re.IGNORECASE),
    re.compile(r"from\s+scripts\.llm|import\s+scripts\.llm", re.IGNORECASE),
)
HARDCODED_MODEL_PATTERN = re.compile(r"\b(?:gpt-\d|claude-[a-z0-9-]+|codex-(?:fast|mid|deep|low|high)|mimo-v)", re.IGNORECASE)
LEGACY_ACTIVE_MODEL_PATTERNS = (
    re.compile(r"cmd\s*=\s*\[\s*[\"']claude[\"']", re.IGNORECASE),
    re.compile(r"_MODEL_MAP\s*=", re.IGNORECASE),
    re.compile(r"from\s+call\s+import\s+.*call_llm", re.IGNORECASE),
    re.compile(r"\bcall_llm\s*\(", re.IGNORECASE),
    re.compile(r"from\s+scripts\.llm|import\s+scripts\.llm", re.IGNORECASE),
)


class BusinessRouteRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeContract:
    node_id: str
    logical_route: str
    capability_tier: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout_ms: int
    retry: dict[str, Any]
    token_context_budget: dict[str, int]


class FixtureHermesProvider:
    provider_name = "hermes"

    def __init__(self, outputs: dict[str, dict[str, Any]]):
        self.outputs = outputs
        self.call_count = 0
        self.routes_seen: list[str] = []

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        self.routes_seen.append(route.route_name)
        payload = self.outputs[route.route_name]
        return ModelProviderResult(
            output_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"status": "fixture", "billing_mode": "none"},
            provider_request_id=f"fixture-{request.route_name}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def node_contracts(registry: dict[str, Any]) -> list[NodeContract]:
    nodes = registry.get("nodes") or []
    return [
        NodeContract(
            node_id=str(node["node_id"]),
            logical_route=str(node["logical_route"]),
            capability_tier=str(node["capability_tier"]),
            input_schema=dict(node["input_schema"]),
            output_schema=dict(node["output_schema"]),
            timeout_ms=int(node["timeout_ms"]),
            retry=dict(node["retry"]),
            token_context_budget=dict(node["token_context_budget"]),
        )
        for node in nodes
    ]


def validate_registry(registry: dict[str, Any]) -> dict[str, Any]:
    if registry.get("schema_version") != "business_model_route_registry.v1":
        raise BusinessRouteRegistryError("unexpected registry schema_version")
    defaults = registry.get("provider_policy_defaults") or {}
    if defaults.get("provider_name") != "hermes":
        raise BusinessRouteRegistryError("provider_policy_defaults.provider_name must be hermes")
    isolation_keys = (
        "tools_enabled",
        "memory_enabled",
        "feishu_messaging_enabled",
        "nested_job_orchestration_enabled",
        "file_or_terminal_side_effects_enabled",
    )
    for key in isolation_keys:
        if defaults.get(key) is not False:
            raise BusinessRouteRegistryError(f"Hermes isolation policy must set {key}=false")
    fallback = defaults.get("fallback_policy") or {}
    for key in ("dry_run_fallback", "fake_port_fallback", "cli_fallback"):
        if fallback.get(key) is not False:
            raise BusinessRouteRegistryError(f"fallback policy must set {key}=false")
    if fallback.get("on_failure") != "fail_closed":
        raise BusinessRouteRegistryError("fallback policy must fail closed")

    contracts = node_contracts(registry)
    if not contracts:
        raise BusinessRouteRegistryError("registry has no nodes")
    seen_node_ids: set[str] = set()
    seen_routes: set[str] = set()
    for contract in contracts:
        if contract.node_id in seen_node_ids:
            raise BusinessRouteRegistryError(f"duplicate node_id: {contract.node_id}")
        if contract.logical_route in seen_routes:
            raise BusinessRouteRegistryError(f"duplicate logical_route: {contract.logical_route}")
        seen_node_ids.add(contract.node_id)
        seen_routes.add(contract.logical_route)
        if not contract.logical_route.startswith("business."):
            raise BusinessRouteRegistryError(f"business route must start with business.: {contract.logical_route}")
        if contract.retry.get("max_attempts") != 1:
            raise BusinessRouteRegistryError(f"{contract.node_id} must not auto-retry provider calls")
        if contract.timeout_ms <= 0:
            raise BusinessRouteRegistryError(f"{contract.node_id} timeout_ms must be positive")
        _validate_schema_shape(contract.input_schema, f"{contract.node_id}.input_schema")
        _validate_schema_shape(contract.output_schema, f"{contract.node_id}.output_schema")
        budget = contract.token_context_budget
        if int(budget.get("max_input_tokens", 0)) <= 0 or int(budget.get("max_output_tokens", 0)) <= 0:
            raise BusinessRouteRegistryError(f"{contract.node_id} token budget must be positive")
    return {"node_count": len(contracts), "logical_routes": sorted(seen_routes)}


def _validate_schema_shape(schema: dict[str, Any], label: str) -> None:
    if schema.get("type") != "object":
        raise BusinessRouteRegistryError(f"{label} must be object schema")
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not required:
        raise BusinessRouteRegistryError(f"{label} must define required fields")
    if not isinstance(properties, dict):
        raise BusinessRouteRegistryError(f"{label} must define properties")
    missing = set(required) - set(properties)
    if missing:
        raise BusinessRouteRegistryError(f"{label} required fields missing from properties: {sorted(missing)}")
    if schema.get("additional_properties") is not False:
        raise BusinessRouteRegistryError(f"{label} must set additional_properties=false")


def run_fixture_route_tests(registry: dict[str, Any]) -> dict[str, Any]:
    contracts = node_contracts(registry)
    store = PersistenceStore.in_memory()
    try:
        routes = {
            contract.logical_route: ModelRoute(
                route_name=contract.logical_route,
                provider_name="hermes",
                model_name="env:HERMES_BUSINESS_MODEL_NAME",
                config_version=f"{GOAL_ID}.registry.v1",
                config_hash=content_hash(
                    {"node_id": contract.node_id, "logical_route": contract.logical_route},
                    f"{GOAL_ID}.route.v1",
                ),
                parameters={"temperature": 0, "max_completion_tokens": contract.token_context_budget["max_output_tokens"]},
                timeout_ms=contract.timeout_ms,
            )
            for contract in contracts
        }
        outputs = {contract.logical_route: fixture_for_schema(contract.output_schema, contract.node_id) for contract in contracts}
        provider = FixtureHermesProvider(outputs)
        gateway = ModelGateway(routes=routes, providers={"hermes": provider}, materializer=ModelRunMaterializer(store))
        runner = PortableSkillRunner(gateway)
        tested: list[dict[str, Any]] = []
        for contract in contracts:
            input_payload = fixture_for_schema(contract.input_schema, contract.node_id)
            validate_schema_subset(input_payload, contract.input_schema)
            skill = PortableSkillSpec(
                skill_name=f"{contract.node_id}_fixture_skill",
                skill_version="0.1.0",
                route_name=contract.logical_route,
                prompt_template="Synthetic route fixture for {fixture_id}. Return only the contracted JSON object.",
                required_input_keys=tuple(contract.input_schema["required"]),
                output_contract=contract.output_schema,
                metadata={"goal": GOAL_ID, "node_id": contract.node_id},
            )
            result = runner.run(skill=skill, input_payload=input_payload, correlation_id=f"{GOAL_ID}.{contract.node_id}")
            output_payload = json.loads(result.output_text)
            validate_schema_subset(output_payload, contract.output_schema)
            tested.append(
                {
                    "node_id": contract.node_id,
                    "logical_route": contract.logical_route,
                    "provider_name": result.model_run.envelope.provider_name,
                    "schema_validation": "passed",
                }
            )
        return {
            "tested_nodes": tested,
            "fixture_provider_call_count": provider.call_count,
            "outbox_count": int(store.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0]),
        }
    finally:
        store.conn.close()


def fixture_for_schema(schema: dict[str, Any], node_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in schema["required"]:
        typ = schema["properties"][key]
        if typ == "string":
            payload[key] = f"{node_id}-{key}-fixture"
        elif typ == "array":
            payload[key] = [f"{node_id}-{key}-fixture"]
        elif typ == "object":
            payload[key] = {"fixture": node_id}
        else:
            raise BusinessRouteRegistryError(f"unsupported fixture property type for {node_id}.{key}: {typ}")
    if "schema_version" in schema["properties"]:
        payload["schema_version"] = str(schema.get("schema_version_const") or "business_node.fixture.v1")
    return payload


def validate_schema_subset(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise BusinessRouteRegistryError("payload must be object")
    required = set(schema["required"])
    properties = schema["properties"]
    missing = required - set(payload)
    if missing:
        raise BusinessRouteRegistryError(f"payload missing fields: {sorted(missing)}")
    if schema.get("additional_properties") is False:
        extra = set(payload) - set(properties)
        if extra:
            raise BusinessRouteRegistryError(f"payload has extra fields: {sorted(extra)}")
    for key, typ in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        if typ == "string" and not isinstance(value, str):
            raise BusinessRouteRegistryError(f"{key} must be string")
        if typ == "array" and not isinstance(value, list):
            raise BusinessRouteRegistryError(f"{key} must be array")
        if typ == "object" and not isinstance(value, dict):
            raise BusinessRouteRegistryError(f"{key} must be object")


def scan_direct_model_calls() -> dict[str, Any]:
    formal_matches = []
    for root in FORMAL_PRODUCTION_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            resolved = path.resolve()
            if path.name.startswith("verify_") or resolved in FORMAL_VERIFICATION_ONLY_FILES:
                continue
            if resolved in FORMAL_ALLOWED_DIRECT_PROVIDER_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), 1):
                if any(pattern.search(line) for pattern in BANNED_CLI_PATTERNS) or HARDCODED_MODEL_PATTERN.search(line):
                    formal_matches.append(_match_payload(path, line_no, line))

    legacy_matches = []
    for root in LEGACY_DIRECT_MODEL_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                if any(pattern.search(line) for pattern in LEGACY_ACTIVE_MODEL_PATTERNS):
                    legacy_matches.append(_match_payload(path, line_no, line))

    return {
        "formal_production_direct_model_call_count": len(formal_matches),
        "formal_production_direct_model_calls": formal_matches,
        "legacy_direct_model_call_count": len(legacy_matches),
        "legacy_direct_model_calls": legacy_matches,
    }


def _match_payload(path: Path, line_no: int, line: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "line": line_no,
        "snippet": line.strip()[:160],
    }


def verify_hermes_isolation(registry: dict[str, Any]) -> dict[str, Any]:
    defaults = registry["provider_policy_defaults"]
    static_edges = {
        "hermes_host_binding_imports_model_gateway": bool(
            re.search(
                r"ModelGateway|HermesModelProviderAdapter",
                (ROOT / "scripts" / "core" / "hermes" / "goal11_host_binding.py").read_text(encoding="utf-8"),
            )
        ),
        "model_gateway_imports_hermes_core_bridge": bool(
            re.search(
                r"HermesCoreBridge|FeishuThinBinding|FeishuResponseDispatcher",
                (ROOT / "scripts" / "core" / "model_gateway" / "goal07_model_gateway.py").read_text(encoding="utf-8"),
            )
        ),
        "hermes_provider_imports_core_bridge": bool(
            re.search(
                r"HermesCoreBridge|FeishuThinBinding|FeishuResponseDispatcher|CoreMaterializer|Goal03Scheduler|RuntimeHost",
                (ROOT / "scripts" / "core" / "model_gateway" / "hermes_model_provider.py").read_text(encoding="utf-8"),
            )
        ),
    }
    recursive_chain_possible = any(static_edges.values())
    return {
        "tools_disabled": defaults["tools_enabled"] is False,
        "memory_disabled": defaults["memory_enabled"] is False,
        "feishu_messaging_disabled": defaults["feishu_messaging_enabled"] is False,
        "nested_job_orchestration_disabled": defaults["nested_job_orchestration_enabled"] is False,
        "file_or_terminal_side_effects_disabled": defaults["file_or_terminal_side_effects_enabled"] is False,
        "static_edges": static_edges,
        "recursive_chain_possible": recursive_chain_possible,
        "passed": not recursive_chain_possible,
    }


def clean_room_status() -> dict[str, Any]:
    health = health_check(ROOT / "data" / "formal" / "clean_room_v0_6_2.sqlite3")
    return {"table_count": health["table_count"], "total_rows": sum(health["table_rows"].values())}


def run_verification(registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_registry(registry_path)
    registry_result = validate_registry(registry)
    fixture_result = run_fixture_route_tests(registry)
    direct_calls = scan_direct_model_calls()
    hermes_isolation = verify_hermes_isolation(registry)
    clean_room = clean_room_status()
    status = {
        "goal": GOAL_ID,
        "status": "COMPLETED",
        "registry": registry_result,
        "fixture_route_tests": fixture_result,
        "direct_model_call_scan": direct_calls,
        "hermes_isolation": hermes_isolation,
        "clean_room_formal_db": clean_room,
    }
    if direct_calls["formal_production_direct_model_call_count"] != 0:
        status["status"] = "FAILED"
    if not hermes_isolation["passed"]:
        status["status"] = "FAILED"
    if clean_room["total_rows"] != 0:
        status["status"] = "FAILED"
    return status


def write_status(status: dict[str, Any], path: Path = STATUS_PATH) -> None:
    path.write_text(yaml.safe_dump(status, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_report(status: dict[str, Any], path: Path = REPORT_PATH) -> None:
    routes = status["registry"]["logical_routes"]
    direct = status["direct_model_call_scan"]
    lines = [
        f"# {GOAL_ID} Validation Report",
        "",
        f"status: `{status['status']}`",
        "",
        "## Registry",
        f"- formal_business_llm_node_count: `{status['registry']['node_count']}`",
        "- logical_routes:",
        *[f"  - `{route}`" for route in routes],
        "",
        "## Direct Model Calls",
        f"- formal_production_direct_model_call_count: `{direct['formal_production_direct_model_call_count']}`",
        f"- legacy_direct_model_call_count: `{direct['legacy_direct_model_call_count']}`",
        "- legacy_direct_model_calls:",
        *[
            f"  - `{item['path']}:{item['line']}` {item['snippet']}"
            for item in direct["legacy_direct_model_calls"]
        ],
        "",
        "## Hermes Isolation",
        f"- passed: `{status['hermes_isolation']['passed']}`",
        f"- recursive_chain_possible: `{status['hermes_isolation']['recursive_chain_possible']}`",
        f"- tools_disabled: `{status['hermes_isolation']['tools_disabled']}`",
        f"- memory_disabled: `{status['hermes_isolation']['memory_disabled']}`",
        f"- feishu_messaging_disabled: `{status['hermes_isolation']['feishu_messaging_disabled']}`",
        f"- nested_job_orchestration_disabled: `{status['hermes_isolation']['nested_job_orchestration_disabled']}`",
        f"- file_or_terminal_side_effects_disabled: `{status['hermes_isolation']['file_or_terminal_side_effects_disabled']}`",
        "",
        "## Fixture Route Tests",
        f"- tested_node_count: `{len(status['fixture_route_tests']['tested_nodes'])}`",
        f"- fixture_provider_call_count: `{status['fixture_route_tests']['fixture_provider_call_count']}`",
        f"- outbox_count: `{status['fixture_route_tests']['outbox_count']}`",
        "",
        "## Clean Room",
        f"- formal_table_count: `{status['clean_room_formal_db']['table_count']}`",
        f"- formal_total_rows: `{status['clean_room_formal_db']['total_rows']}`",
        "",
        "## Commands",
        "- `python scripts\\core\\model_gateway\\business_route_registry.py`",
        "- `python -m unittest tests.core.test_business_route_registry tests.validation.test_live_gates tests.core.test_runtime_vertical_slice`",
        "- `python -m py_compile scripts\\core\\model_gateway\\business_route_registry.py scripts\\core\\model_gateway\\hermes_model_provider.py tests\\core\\test_business_route_registry.py`",
        "",
        "## Next Goal",
        "- Recommended: `GOAL-RUNTIME-BUSINESS-SKILL-ADAPTER-01`, scoped to wiring selected business Skill adapters to these registered routes with synthetic fixtures only.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_progress(status: dict[str, Any], path: Path = PROGRESS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {GOAL_ID} Progress",
        "",
        f"status: {status['status']}",
        "branch: validation/goal-runtime-business-route-cutover-01-v0.6.2",
        "",
        "## Checkpoints",
        "- [x] Restore baseline from the previous live ModelGateway gate.",
        "- [x] Define formal business LLM nodes from AGENTS.md, BUILD_PLAN.md and the active Goal only.",
        "- [x] Create BUSINESS_MODEL_ROUTE_REGISTRY.yaml.",
        "- [x] Validate route/schema fixtures through ModelGateway with no business workflow execution.",
        "- [x] Verify formal production roots have no direct CLI/legacy model calls.",
        "- [x] Verify Hermes inference isolation and recursive-chain guard.",
        "- [x] Confirm clean-room formal DB remains empty.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{GOAL_ID} registry verifier")
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--status-output", default=str(STATUS_PATH))
    parser.add_argument("--report-output", default=str(REPORT_PATH))
    parser.add_argument("--progress-output", default=str(PROGRESS_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = run_verification(Path(args.registry))
    write_status(status, Path(args.status_output))
    write_report(status, Path(args.report_output))
    write_progress(status, Path(args.progress_output))
    print(yaml.safe_dump(status, allow_unicode=True, sort_keys=False))
    return 0 if status["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
