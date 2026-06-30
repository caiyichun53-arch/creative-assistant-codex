"""Store reviewed content atoms with source-text verification.

Input JSON format:
[
  {
    "source_table": "language_fuel_items",
    "source_id": 123,
    "atom_type": "counterintuitive",
    "atom_text": "People often mistake sugar-free drinks for harmless.",
    "evidence_text": "source sentence copied from the raw item",
    "topic_tags": ["sugar", "health"],
    "strength": 4,
    "reliability": 3
  }
]

The script rejects atoms whose evidence_text cannot be found in the source
content. That keeps the atom layer traceable to real copy/comments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"

sys.stdout.reconfigure(encoding="utf-8")

ATOM_TYPES = {
    "fact",
    "counterintuitive",
    "scene",
    "experience",
    "emotion",
    "objection",
    "phrase",
    "analogy",
    "hook",
}


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "")


def content_hash(*parts: str) -> str:
    raw = "\n".join(parts)
    return hashlib.sha1(normalize(raw).encode("utf-8")).hexdigest()


def clamp_score(value: Any, default: int = 3) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(5, score))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def source_row(conn: sqlite3.Connection, source_table: str, source_id: str) -> sqlite3.Row:
    if source_table == "language_fuel_items":
        sql = """SELECT id, platform, domain, source_kind, title, url, source_url, content
                 FROM language_fuel_items WHERE id=?"""
    elif source_table == "language_fuel_comments":
        sql = """SELECT id, platform, domain, source_kind, NULL AS title, source_url AS url,
                        source_url, content
                 FROM language_fuel_comments WHERE id=?"""
    elif source_table == "hit_comments":
        sql = """SELECT c.id, 'douyin' AS platform, ca.domain, 'short_comment' AS source_kind,
                        h.title, h.url, h.url AS source_url, c.content
                 FROM hit_comments c
                 JOIN hits h ON h.id = c.hit_id
                 JOIN competitor_accounts ca ON ca.id = h.competitor_id
                 WHERE c.id=?"""
    elif source_table == "music_comments":
        sql = """SELECT c.id, c.platform, 'music' AS domain, 'short_comment' AS source_kind,
                        t.name AS title, t.url, t.url AS source_url, c.content
                 FROM music_comments c
                 JOIN music_tracks t ON t.id = c.track_id
                 WHERE c.id=?"""
    else:
        raise ValueError(f"unsupported source_table: {source_table}")

    row = conn.execute(sql, (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"source not found: {source_table}:{source_id}")
    return row


def load_atoms(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "atoms" in data:
        data = data["atoms"]
    if not isinstance(data, list):
        raise ValueError("input must be a JSON array or an object with an atoms array")
    return data


def store_atom(conn: sqlite3.Connection, atom: dict[str, Any], *, status: str) -> bool:
    source_table = str(atom.get("source_table") or "")
    source_id = str(atom.get("source_id") or "")
    atom_type = str(atom.get("atom_type") or "")
    atom_text = str(atom.get("atom_text") or "").strip()
    evidence_text = str(atom.get("evidence_text") or "").strip()

    if atom_type not in ATOM_TYPES:
        raise ValueError(f"unsupported atom_type: {atom_type}")
    if not atom_text or not evidence_text:
        raise ValueError("atom_text and evidence_text are required")

    row = source_row(conn, source_table, source_id)
    if normalize(evidence_text) not in normalize(row["content"]):
        raise ValueError(f"evidence not found in source: {source_table}:{source_id}")

    tags = atom.get("topic_tags") or atom.get("tags") or []
    if isinstance(tags, str):
        tags_json = json.dumps([tags], ensure_ascii=False)
    else:
        tags_json = json.dumps(list(tags), ensure_ascii=False)

    raw_hash = content_hash(atom_type, atom_text, evidence_text)
    cur = conn.execute(
        """INSERT OR IGNORE INTO content_atoms
           (atom_type, domain, title, atom_text, evidence_text, source_table,
            source_id, source_url, platform, source_kind, topic_tags, strength,
            reliability, status, notes, content_hash, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            atom_type,
            atom.get("domain") or row["domain"] or "general",
            atom.get("title") or row["title"],
            atom_text,
            evidence_text,
            source_table,
            source_id,
            atom.get("source_url") or row["source_url"] or row["url"],
            atom.get("platform") or row["platform"],
            atom.get("source_kind") or row["source_kind"],
            tags_json,
            clamp_score(atom.get("strength")),
            clamp_score(atom.get("reliability")),
            atom.get("status") or status,
            atom.get("notes"),
            raw_hash,
            json.dumps(atom, ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="JSON file containing reviewed atom candidates")
    parser.add_argument("--status", choices=["candidate", "approved"], default="candidate")
    args = parser.parse_args()

    atoms = load_atoms(args.input)
    conn = connect()
    inserted = 0
    try:
        with conn:
            for index, atom in enumerate(atoms, start=1):
                try:
                    if store_atom(conn, atom, status=args.status):
                        inserted += 1
                except Exception as exc:
                    raise RuntimeError(f"atom #{index} rejected: {exc}") from exc
    finally:
        conn.close()

    print(f"content_atoms store complete: scanned {len(atoms)}, inserted {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
