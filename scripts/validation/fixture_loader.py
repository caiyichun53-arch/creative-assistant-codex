from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.clean_room_empty_db import ensure_safe_db_path, install_schema


ALLOWED_FIXTURE_ROOTS = (
    ROOT / "tests" / "fixtures",
    ROOT / "validation_evidence" / "clean_room" / "fixtures",
)
DEFAULT_FIXTURE_DB = ROOT / "tests" / "fixtures" / "clean_room" / "fixture_runtime.sqlite3"

SYNTHETIC_CASES = [
    ("account_19_videos", {"video_count": 19, "domain": "泛科普"}),
    ("account_20_videos", {"video_count": 20, "domain": "泛科普"}),
    ("publish_first_seen_gap", {"publish_time": "2026-01-01T10:00:00", "first_seen_at": "2026-01-03T10:00:00"}),
    ("duplicate_platform_video_id", {"platform": "douyin", "platform_item_id": "synthetic-duplicate"}),
    ("with_comments", {"comments": ["synthetic comment"], "asr": None}),
    ("without_comments", {"comments": [], "asr": "synthetic transcript"}),
    ("performance_high", {"performance_band": "high"}),
    ("performance_normal", {"performance_band": "normal"}),
    ("performance_low", {"performance_band": "low"}),
    ("domain_fan_kepu", {"domain": "泛科普"}),
    ("domain_music_entertainment", {"domain": "音乐娱乐"}),
    ("domain_third_neutral", {"domain": "third-neutral"}),
    ("model_timeout", {"model_result": "timeout"}),
    ("model_empty_result", {"model_result": "empty"}),
    ("model_bad_format", {"model_result": "bad_format"}),
    ("retry", {"attempts": 2}),
    ("idempotency", {"idempotency_key": "synthetic-fixture-idempotency"}),
]


def _is_under(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def require_test_fixture_mode(*, allow_flag: bool) -> None:
    env = os.environ.get("CREATION_ASSISTANT_ENV", "")
    allow_env = os.environ.get("CREATION_ASSISTANT_ALLOW_FIXTURES", "")
    if env != "test" or allow_env != "1" or not allow_flag:
        raise RuntimeError(
            "fixture loading is test-only; require CREATION_ASSISTANT_ENV=test, "
            "CREATION_ASSISTANT_ALLOW_FIXTURES=1 and --allow-test-fixtures"
        )


def ensure_fixture_db_path(db_path: Path) -> Path:
    resolved = ensure_safe_db_path(db_path)
    if not _is_under(resolved, ALLOWED_FIXTURE_ROOTS):
        raise ValueError(f"fixture DB must be under tests/fixtures or validation_evidence/clean_room/fixtures: {resolved}")
    return resolved


def load_synthetic_fixtures(db_path: Path, *, allow_flag: bool, destroy_existing: bool = False) -> dict[str, Any]:
    require_test_fixture_mode(allow_flag=allow_flag)
    resolved = ensure_fixture_db_path(db_path)
    install_schema(resolved, destroy_existing=destroy_existing)
    conn = sqlite3.connect(resolved)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_fixture_case (
                case_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for case_id, payload in SYNTHETIC_CASES:
            conn.execute(
                """
                INSERT OR IGNORE INTO test_fixture_case(case_id, payload_json)
                VALUES(?, ?)
                """,
                (case_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        conn.commit()
        count = int(conn.execute("SELECT COUNT(*) FROM test_fixture_case").fetchone()[0])
        return {"database": resolved.as_posix(), "fixture_case_count": count, "mode": "test_only"}
    finally:
        conn.close()


def destroy_fixture_db(db_path: Path, *, allow_flag: bool) -> dict[str, Any]:
    require_test_fixture_mode(allow_flag=allow_flag)
    resolved = ensure_fixture_db_path(db_path)
    existed = resolved.exists()
    if existed:
        resolved.unlink()
    return {"database": resolved.as_posix(), "destroyed": existed}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Load synthetic clean-room fixtures into an isolated test DB only.")
    parser.add_argument("--db", default=DEFAULT_FIXTURE_DB.as_posix())
    parser.add_argument("--load-synthetic", action="store_true")
    parser.add_argument("--destroy", action="store_true")
    parser.add_argument("--destroy-existing", action="store_true")
    parser.add_argument("--allow-test-fixtures", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.destroy:
        result = destroy_fixture_db(Path(args.db), allow_flag=args.allow_test_fixtures)
    elif args.load_synthetic:
        result = load_synthetic_fixtures(
            Path(args.db),
            allow_flag=args.allow_test_fixtures,
            destroy_existing=args.destroy_existing,
        )
    else:
        raise SystemExit("choose --load-synthetic or --destroy")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
