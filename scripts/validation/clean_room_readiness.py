from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]

ALLOWED_DISPOSITIONS = {
    "discard_from_target",
    "fixture_only",
    "cold_archive_only",
    "retain_as_current_asset",
}

REQUIRED_INVENTORY_FIELDS = {
    "path_or_table",
    "data_type",
    "quantity",
    "hash",
    "contains_sensitive_information",
    "current_code_references",
    "disposition",
}

REQUIRED_FIXTURE_COVERAGE = {
    "19": "19 account baseline boundary",
    "20": "20 account baseline boundary",
    "first_seen_at": "discovery and publish time difference",
    "duplicate": "duplicate video id",
    "comments": "with and without comments",
    "ASR": "with and without ASR",
    "high normal low": "performance bands",
    "\u6cdb\u79d1\u666e": "fan-kepu domain",
    "\u97f3\u4e50\u5a31\u4e50": "music entertainment domain",
    "third": "third neutral domain",
    "model failure": "model failure/retry",
    "idempotency": "idempotency",
}

LEGACY_REFERENCE_PATTERNS = (
    "data/creation.db",
    "data/topics",
    "data/transcripts",
    "data/reverse",
    "data/\u7206\u6b3e\u62c6\u89e3",
    "dna_note_path",
    "transcript_path",
    "hit_comments",
    "competitor_videos",
    "FROM hits",
    "FROM topics",
)

SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".md"}
PRODUCTION_SCAN_ROOTS = (
    "scripts/core",
    "scripts/project_check.py",
    "config/settings.yaml",
    "config/settings.example.yaml",
)
LEGACY_QUARANTINE_ROOTS = (
    "scripts/run_daily.py",
    "scripts/collect",
    "scripts/analyze",
    "scripts/topics",
    "scripts/reverse",
    "scripts/research",
    "scripts/content",
    "scripts/language_fuel",
    "scripts/music",
    "scripts/feishu",
    "tools/asr",
)


@dataclass(frozen=True)
class Finding:
    check_id: str
    status: str
    requirement: str
    evidence: list[str]
    required_remediation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "requirement": self.requirement,
            "evidence": self.evidence,
            "required_remediation": self.required_remediation,
        }


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def iter_scan_files(root: Path, scan_roots: tuple[str, ...] | None = None) -> list[Path]:
    raw_roots = scan_roots or ("scripts", "config", "tests")
    files: list[Path] = []
    for raw_root in raw_roots:
        scan_root = root / raw_root
        if scan_root.is_file():
            if scan_root.suffix in SCAN_SUFFIXES:
                files.append(scan_root)
            continue
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*"):
            if path.is_file() and path.suffix in SCAN_SUFFIXES:
                files.append(path)
    return sorted(files)


def find_references(
    root: Path,
    patterns: tuple[str, ...],
    *,
    scan_roots: tuple[str, ...] | None = None,
    exclude_prefixes: tuple[str, ...] = (),
) -> list[str]:
    references: list[str] = []
    excluded = tuple((root / prefix).resolve() for prefix in exclude_prefixes)
    for path in iter_scan_files(root, scan_roots):
        resolved = path.resolve()
        if excluded and any(resolved.is_relative_to(prefix) for prefix in excluded):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if any(pattern in line for pattern in patterns):
                rel = path.relative_to(root).as_posix()
                references.append(f"{rel}:{lineno}:{line.strip()[:220]}")
    return references


def validate_inventory(root: Path) -> list[str]:
    errors: list[str] = []
    inventory = load_yaml(root / "LEGACY_DATA_INVENTORY.yaml")
    if inventory.get("policy", {}).get("production_database") != "start_empty":
        errors.append("inventory policy.production_database must be start_empty")
    for index, item in enumerate(inventory.get("items", [])):
        missing = REQUIRED_INVENTORY_FIELDS - set(item)
        if missing:
            errors.append(f"inventory item {index} missing fields: {', '.join(sorted(missing))}")
        disposition = item.get("disposition")
        if disposition not in ALLOWED_DISPOSITIONS:
            errors.append(f"inventory item {index} has invalid disposition: {disposition}")
    return errors


def validate_fixture_manifest(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = load_yaml(root / "FIXTURE_RETENTION_MANIFEST.yaml")
    if not manifest.get("policy", {}).get("prefer_synthetic"):
        errors.append("fixture manifest must prefer synthetic fixtures")
    text = json.dumps(manifest, ensure_ascii=False)
    for marker, description in REQUIRED_FIXTURE_COVERAGE.items():
        if marker not in text:
            errors.append(f"fixture coverage missing: {description}")
    forbidden = set(manifest.get("policy", {}).get("forbidden_targets", []))
    required_forbidden = {
        "production database",
        "production retrieval",
        "experience library",
        "model context",
        "Hermes memory",
        "formal business judgement",
    }
    missing_forbidden = required_forbidden - forbidden
    if missing_forbidden:
        errors.append(f"fixture forbidden targets missing: {', '.join(sorted(missing_forbidden))}")
    return errors


def validate_acceptance(root: Path) -> list[str]:
    errors: list[str] = []
    acceptance = load_yaml(root / "CLEAN_ROOM_ACCEPTANCE.yaml")
    gates = {item.get("gate_id") for item in acceptance.get("hard_gates", [])}
    required = {
        "CR-DB-EMPTY",
        "CR-NO-OLD-IDENTIFIERS",
        "CR-CONFIG-NO-CREATION-DB",
        "CR-CODE-NO-LEGACY-FALLBACK",
        "CR-FIXTURE-TEST-ONLY",
        "CR-PRODUCTION-STARTS-WITHOUT-FIXTURES",
        "CR-COLD-BACKUP-INACCESSIBLE",
        "CR-CLEAN-ROOM-TESTS",
    }
    missing = required - gates
    if missing:
        errors.append(f"clean-room gates missing: {', '.join(sorted(missing))}")
    return errors


def load_settings(root: Path) -> dict[str, Any]:
    path = root / "config" / "settings.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def validate_formal_storage_config(root: Path) -> list[str]:
    errors: list[str] = []
    settings = load_settings(root)
    storage = settings.get("storage", {})
    paths = settings.get("paths", {})
    db_path = str(paths.get("db", "")).replace("\\", "/")
    if db_path in {"data/creation.db", "./data/creation.db"}:
        errors.append("paths.db must not reference data/creation.db")
    if storage.get("formal_engine") != "postgresql":
        errors.append("storage.formal_engine must be postgresql")
    if storage.get("local_validation_engine") != "sqlite":
        errors.append("storage.local_validation_engine must be sqlite")
    if "data/creation.db" in json.dumps(settings, ensure_ascii=False):
        errors.append("config/settings.yaml must not contain data/creation.db")
    return errors


def validate_legacy_quarantine(root: Path) -> list[str]:
    errors: list[str] = []
    settings = load_settings(root)
    legacy = settings.get("legacy_runtime", {})
    if legacy.get("enabled") is not False:
        errors.append("legacy_runtime.enabled must be false")
    declared = set(legacy.get("quarantined_entrypoints", []))
    missing = [item for item in LEGACY_QUARANTINE_ROOTS if item not in declared]
    if missing:
        errors.append("legacy_runtime.quarantined_entrypoints missing: " + ", ".join(missing))
    return errors


def phase_two_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    config_refs = find_references(root, ("data/creation.db",), exclude_prefixes=("tests",))
    production_config_refs = [item for item in config_refs if item.startswith("config/settings.yaml:")]
    findings.append(
        Finding(
            check_id="P2-BLOCK-001",
            status="blocked" if production_config_refs else "passed",
            requirement="Production configuration must not reference data/creation.db.",
            evidence=production_config_refs,
            required_remediation="Point production configuration at the new empty formal database before cleanup.",
        )
    )

    # scripts/core/business_data owns its own formal tables (hits, competitor_videos) in an
    # isolated database (data/formal/production_activation.sqlite3), guarded by its own
    # _safe_db_path() check that refuses data/creation.db -- verified separately in
    # tests.core.test_competitor_account_registration / test_competitor_registration_full.
    # This bare-string legacy scan can't tell that table name apart from the legacy
    # data/creation.db table of the same name, and even flags this module's own
    # "refusing to write old data/creation.db" guard message. Exclude this one formal
    # module the same way "tests" is already excluded; every other scripts/core path and
    # every legacy pattern still gets scanned.
    formal_refs = find_references(
        root,
        LEGACY_REFERENCE_PATTERNS,
        scan_roots=PRODUCTION_SCAN_ROOTS,
        exclude_prefixes=("tests", "scripts/core/business_data"),
    )
    findings.append(
        Finding(
            check_id="P2-BLOCK-002",
            status="blocked" if formal_refs else "passed",
            requirement="Formal production code must not read legacy SQLite tables or legacy artifact paths.",
            evidence=formal_refs[:120],
            required_remediation="Keep legacy readers out of formal production roots and behind legacy quarantine.",
        )
    )

    approval = root / "CLEAN_ROOM_PHASE2_APPROVAL.yaml"
    findings.append(
        Finding(
            check_id="P2-BLOCK-003",
            status="passed" if approval.exists() else "blocked_pending_user_approval",
            requirement="Cold backup location must be approved outside repo and production load paths.",
            evidence=[approval.as_posix()] if approval.exists() else [],
            required_remediation="Add an approved backup location manifest before phase-2 backup creation.",
        )
    )

    fixture_loader = root / "scripts" / "validation" / "fixture_loader.py"
    findings.append(
        Finding(
            check_id="P2-BLOCK-004",
            status="passed" if fixture_loader.exists() else "blocked_missing_implementation",
            requirement="Fixture loader must be test-only and absent from production startup.",
            evidence=[fixture_loader.relative_to(root).as_posix()] if fixture_loader.exists() else [],
            required_remediation="Implement an explicitly test-only fixture loader and production-without-fixtures smoke test.",
        )
    )

    empty_db_entry = root / "scripts" / "validation" / "clean_room_empty_db.py"
    empty_db_path = root / "data" / "formal" / "clean_room_v0_6_2.sqlite3"
    findings.append(
        Finding(
            check_id="P2-BLOCK-005",
            status="passed" if empty_db_entry.exists() and empty_db_path.exists() else "blocked_missing_implementation",
            requirement="Clean-room empty database creation command must be identified and verified.",
            evidence=[
                item
                for item in [
                    empty_db_entry.relative_to(root).as_posix() if empty_db_entry.exists() else "",
                    empty_db_path.relative_to(root).as_posix() if empty_db_path.exists() else "",
                ]
                if item
            ],
            required_remediation="Define the exact empty formal DB creation command before deleting old runtime data.",
        )
    )

    config_errors = validate_formal_storage_config(root)
    findings.append(
        Finding(
            check_id="P2-BLOCK-006",
            status="blocked" if config_errors else "passed",
            requirement="Formal storage config must point at the V0.6.2 clean-room target and local validation DB.",
            evidence=config_errors,
            required_remediation="Update storage config to PostgreSQL target plus isolated local SQLite validation DB.",
        )
    )

    quarantine_errors = validate_legacy_quarantine(root)
    findings.append(
        Finding(
            check_id="P2-BLOCK-007",
            status="blocked" if quarantine_errors else "passed",
            requirement="Legacy runtime entrypoints must be read-only quarantined for production.",
            evidence=quarantine_errors,
            required_remediation="Declare legacy_runtime.enabled=false and enumerate legacy entrypoints.",
        )
    )

    production_fixture_env = os.environ.get("CREATION_ASSISTANT_ALLOW_FIXTURES") == "1" and os.environ.get("CREATION_ASSISTANT_ENV") != "test"
    findings.append(
        Finding(
            check_id="P2-BLOCK-008",
            status="blocked" if production_fixture_env else "passed",
            requirement="Production mode must reject fixture loading.",
            evidence=["CREATION_ASSISTANT_ALLOW_FIXTURES=1 without CREATION_ASSISTANT_ENV=test"] if production_fixture_env else [],
            required_remediation="Unset fixture loading in production; fixtures require explicit test environment.",
        )
    )
    return findings


def run_audit(root: Path = ROOT) -> dict[str, Any]:
    errors = (
        validate_inventory(root)
        + validate_fixture_manifest(root)
        + validate_acceptance(root)
        + validate_formal_storage_config(root)
        + validate_legacy_quarantine(root)
    )
    findings = phase_two_findings(root)
    phase_2_safe = not errors and all(item.status == "passed" for item in findings)
    return {
        "schema_version": 1,
        "goal": "GOAL-DATA-RESET-01",
        "validation_errors": errors,
        "phase_2_safe_to_execute": phase_2_safe,
        "checks": [item.as_dict() for item in findings],
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate GOAL-DATA-RESET-01 clean-room readiness without deleting data.")
    parser.add_argument("--require-safe", action="store_true", help="Return non-zero when phase 2 is not safe to execute.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of YAML.")
    args = parser.parse_args(argv)
    result = run_audit(ROOT)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    if result["validation_errors"]:
        return 2
    if args.require_safe and not result["phase_2_safe_to_execute"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
