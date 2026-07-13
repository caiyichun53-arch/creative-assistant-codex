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
    # 2026-07-08: hit_transcripts/hit_comments were redesigned (raw/cleaned
    # transcript versioning + provenance, comment sample_rank) the same day
    # they were first introduced -- CREATE TABLE IF NOT EXISTS cannot reshape
    # an existing table, so drop the old-shaped ones (if present) before
    # recreating. User-confirmed 2026-07-08: only ever held this session's own
    # 2 verification rows, nothing else reads the old shape.
    _drop_table_if_old_shape(conn, "hit_transcripts", removed_column="transcript_text")
    _drop_table_if_old_shape(conn, "hit_comments", added_not_null_column="sample_rank")
    _migrate_hits_preparation_status(conn)
    # 2026-07-13 (BR-HIT-007): hit_comments gained purpose/observation_point/
    # sampling_strategy and its PRIMARY KEY changed to include purpose -- a
    # real column + PK change, not something ALTER TABLE ADD COLUMN can do,
    # and this table holds real production data by now (unlike the 2026-07-08
    # redesign above), so this is a copy-migrate, not a drop.
    _migrate_hit_comments_add_purpose(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # 2026-07-08: CREATE TABLE IF NOT EXISTS does not retroactively add columns
    # to a table that already exists (e.g. the real production DB) -- this
    # lazily backfills newly-added nullable columns on every call, so no
    # separate migration invocation is needed.
    _ensure_column(conn, "video_checks", "day_since_publish", "INTEGER")
    # Source document section 13 vocabulary is pending/running/completed/
    # failed; the field was first introduced this session with none/done --
    # normalize any rows already written under the old vocabulary.
    conn.execute("UPDATE hits SET preparation_status='pending' WHERE preparation_status='none'")
    conn.execute("UPDATE hits SET preparation_status='completed' WHERE preparation_status='done'")


def _migrate_hits_preparation_status(conn: sqlite3.Connection) -> None:
    """Rename the retired queue field before any current writer uses it."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hits)").fetchall()}
    if "reverse_status" in columns and "preparation_status" not in columns:
        conn.execute("ALTER TABLE hits RENAME COLUMN reverse_status TO preparation_status")


def _drop_table_if_old_shape(
    conn: sqlite3.Connection, table: str, *, removed_column: str | None = None, added_not_null_column: str | None = None
) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not columns:
        return  # table doesn't exist yet -- nothing to migrate
    is_old_shape = (removed_column is not None and removed_column in columns) or (
        added_not_null_column is not None and added_not_null_column not in columns
    )
    if is_old_shape:
        conn.execute(f"DROP TABLE {table}")


# Every existing hit_comments row was collected by the single fetch-per-
# promoted-hit path run_reverse_prep.py currently implements, which fires
# once a hit clears judgement -- of the four purposes BR-HIT-007 defines,
# that is closest to "D7/P+7d 才首次正式触发 -> 采一次 mature_analysis"
# (the D7-first-trigger case), not an early-topic pass. Backfilling existing
# rows to anything else would be guessing at history this schema never
# recorded; 'mature_analysis' is the one honestly defensible default given
# what the collector actually did at the time.
_HIT_COMMENTS_BACKFILL_PURPOSE = "mature_analysis"
_HIT_COMMENTS_BACKFILL_SAMPLING_STRATEGY = "top_n_by_platform_popularity"


def _migrate_hit_comments_add_purpose(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hit_comments)").fetchall()}
    if not columns or "purpose" in columns:
        return  # table doesn't exist yet, or already migrated -- nothing to do
    conn.execute(
        """
        CREATE TABLE hit_comments_new (
            hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT,
            comment_id TEXT NOT NULL,
            text TEXT NOT NULL,
            like_count INTEGER NOT NULL DEFAULT 0,
            parent_comment_id TEXT,
            sample_rank INTEGER NOT NULL,
            purpose TEXT NOT NULL CHECK(purpose IN ('early_topic', 'mature_analysis', 'mature_history', 'external_snapshot')),
            observation_point TEXT,
            sampling_strategy TEXT,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (hit_id, comment_id, purpose)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO hit_comments_new(
            hit_id, comment_id, text, like_count, parent_comment_id, sample_rank,
            purpose, observation_point, sampling_strategy, run_id, fetched_at
        )
        SELECT hit_id, comment_id, text, like_count, parent_comment_id, sample_rank,
               ?, NULL, ?, run_id, fetched_at
        FROM hit_comments
        """,
        (_HIT_COMMENTS_BACKFILL_PURPOSE, _HIT_COMMENTS_BACKFILL_SAMPLING_STRATEGY),
    )
    conn.execute("DROP TABLE hit_comments")
    conn.execute("ALTER TABLE hit_comments_new RENAME TO hit_comments")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hit_comments_hit ON hit_comments(hit_id, sample_rank)")


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
