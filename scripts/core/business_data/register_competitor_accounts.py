from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "data" / "formal" / "production_activation.sqlite3"
SCHEMA_PATH = Path(__file__).with_name("competitor_accounts_schema.sqlite.sql")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Register competitor accounts from a domain config.")
    parser.add_argument("--domain-config", default="config/domains/泛科普.yaml")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--list", action="store_true", help="List registered competitor accounts after registration.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    args = parser.parse_args(argv)

    domain_config_path = _repo_path(args.domain_config)
    db_path = _safe_db_path(Path(args.db))
    domain = _load_domain_config(domain_config_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        report = register_from_domain(conn, domain, source_config_ref=_relative_ref(domain_config_path))
        conn.commit()
        if args.list:
            report["registered_accounts"] = list_accounts(conn, domain.get("formal_domain_label"))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0


def install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # 2026-07-08: CREATE TABLE IF NOT EXISTS does not retroactively add columns
    # to a table that already exists (e.g. the real production DB) -- this
    # lazily backfills newly-added nullable columns on every call, so no
    # separate migration invocation is needed.
    _ensure_column(conn, "video_checks", "day_since_publish", "INTEGER")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def register_from_domain(conn: sqlite3.Connection, domain: dict[str, Any], *, source_config_ref: str) -> dict[str, Any]:
    platform = _required_text(domain.get("platform"), "platform")
    domain_label = _required_text(domain.get("formal_domain_label"), "formal_domain_label")
    domain_name = _required_text(domain.get("name"), "name")
    policy = domain.get("collector_policy") or {}
    first_crawl_policy = _required_text(policy.get("first_crawl"), "collector_policy.first_crawl")
    comments_policy = _required_text(policy.get("comments"), "collector_policy.comments")
    seeds = domain.get("competitor_seeds")
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("competitor_seeds must be a non-empty list")

    inserted = 0
    updated = 0
    rows: list[dict[str, Any]] = []
    for seed in seeds:
        if not isinstance(seed, dict):
            raise ValueError("competitor seed must be an object")
        name = _required_text(seed.get("name"), "competitor_seeds.name")
        url = _required_text(seed.get("url"), f"competitor_seeds[{name}].url")
        sec_uid = parse_douyin_sec_uid(url) if platform in {"douyin", "dy"} else _required_text(url, "source id")
        homepage_url = canonical_homepage_url(platform, sec_uid)
        account_id = stable_account_id(platform, sec_uid)
        existed = conn.execute(
            "SELECT account_id FROM competitor_accounts WHERE platform=? AND sec_uid=?",
            (platform, sec_uid),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO competitor_accounts(
                account_id, platform, domain_label, domain_name, account_name,
                sec_uid, homepage_url, source_config_ref, registration_status,
                first_crawl_policy, comments_policy
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(platform, sec_uid) DO UPDATE SET
                domain_label=excluded.domain_label,
                domain_name=excluded.domain_name,
                account_name=excluded.account_name,
                homepage_url=excluded.homepage_url,
                source_config_ref=excluded.source_config_ref,
                registration_status='active',
                first_crawl_policy=excluded.first_crawl_policy,
                comments_policy=excluded.comments_policy
            """,
            (
                account_id,
                platform,
                domain_label,
                domain_name,
                name,
                sec_uid,
                homepage_url,
                source_config_ref,
                first_crawl_policy,
                comments_policy,
            ),
        )
        if existed:
            updated += 1
        else:
            inserted += 1
        rows.append(
            {
                "account_id": account_id,
                "name": name,
                "platform": platform,
                "domain_label": domain_label,
                "sec_uid": sec_uid,
                "homepage_url": homepage_url,
            }
        )

    total = conn.execute(
        "SELECT count(*) FROM competitor_accounts WHERE domain_label=? AND platform=? AND registration_status='active'",
        (domain_label, platform),
    ).fetchone()[0]
    return {
        "status": "succeeded",
        "database": _relative_ref(Path(conn.execute("PRAGMA database_list").fetchone()[2])),
        "domain": domain_name,
        "domain_label": domain_label,
        "platform": platform,
        "source_config_ref": source_config_ref,
        "requested_count": len(seeds),
        "inserted_count": inserted,
        "updated_count": updated,
        "active_count_for_domain": int(total),
        "no_video_collection_started": True,
        "no_hit_judgement_started": True,
        "accounts": rows,
    }


def list_accounts(conn: sqlite3.Connection, domain_label: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    where = ""
    if domain_label:
        where = "WHERE domain_label=?"
        params = (domain_label,)
    rows = conn.execute(
        f"""
        SELECT account_id, account_name, platform, domain_label, sec_uid, homepage_url, registration_status
          FROM competitor_accounts
          {where}
         ORDER BY domain_label, platform, account_name
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def parse_douyin_sec_uid(value: str) -> str:
    text = value.strip()
    if text.startswith("MS4wLjABAAAA"):
        return text.split("?", 1)[0].strip("/")
    match = re.search(r"douyin\.com/user/([^/?#]+)", text)
    if match:
        return match.group(1)
    raise ValueError(f"unable to parse douyin sec_uid from account homepage: {value}")


def canonical_homepage_url(platform: str, sec_uid: str) -> str:
    if platform in {"douyin", "dy"}:
        return f"https://www.douyin.com/user/{sec_uid}"
    return sec_uid


def stable_account_id(platform: str, sec_uid: str) -> str:
    digest = hashlib.sha256(f"{platform}:{sec_uid}".encode("utf-8")).hexdigest()[:16]
    return f"ca_{digest}"


def _load_domain_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"domain config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("domain config must be an object")
    return data


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _relative_ref(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_db_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    allowed = (ROOT / "data" / "formal").resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError(f"competitor registration DB must stay under data/formal: {resolved}")
    if resolved.name == "creation.db":
        raise ValueError("refusing to write old data/creation.db")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
