"""Run the repository-local official TrendRadar and export its real latest crawl.

This is a deterministic runtime bridge.  It does not filter topics, call a model,
retry a request, or invent missing fields.  Every invocation keeps stdout, stderr,
timing, command, upstream identity, raw SQLite reference, and normalized output.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import tomllib
from typing import Any
from zoneinfo import ZoneInfo


OFFICIAL_ORIGIN = "https://github.com/sansan0/TrendRadar.git"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _git(trendradar_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(trendradar_dir), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    return completed.stdout.strip()


def verify_official_install(trendradar_dir: Path) -> dict[str, Any]:
    trendradar_dir = trendradar_dir.resolve()
    python_executable = trendradar_dir / ".venv" / "Scripts" / "python.exe"
    config_path = trendradar_dir / "config" / "config.yaml"
    pyproject_path = trendradar_dir / "pyproject.toml"
    if not (trendradar_dir / ".git").is_dir():
        raise RuntimeError("TrendRadar install is not a Git checkout")
    origin = _git(trendradar_dir, "remote", "get-url", "origin")
    if origin.rstrip("/") != OFFICIAL_ORIGIN.rstrip("/"):
        raise RuntimeError(f"TrendRadar origin is not the approved official repository: {origin}")
    if not python_executable.is_file():
        raise RuntimeError("TrendRadar locked virtual environment is missing")
    if not config_path.is_file():
        raise RuntimeError("TrendRadar config/config.yaml is missing")
    changed = [line for line in _git(trendradar_dir, "status", "--short").splitlines() if line.strip()]
    unexpected = [line for line in changed if not line.endswith(" config/config.yaml") and "__pycache__/" not in line]
    if unexpected:
        raise RuntimeError(f"TrendRadar upstream source has unapproved local changes: {unexpected}")
    with pyproject_path.open("rb") as handle:
        version = str(tomllib.load(handle)["project"]["version"])
    return {
        "origin": origin,
        "commit": _git(trendradar_dir, "rev-parse", "HEAD"),
        "version": version,
        "python_executable": str(python_executable),
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }


def crawl_record_snapshot(trendradar_dir: Path) -> dict[tuple[str, str], tuple[int, str]]:
    records: dict[tuple[str, str], tuple[int, str]] = {}
    for database in sorted((trendradar_dir / "output" / "news").glob("*.db")):
        try:
            with closing(sqlite3.connect(database)) as conn:
                for crawl_time, total_items, created_at in conn.execute(
                    "SELECT crawl_time, total_items, created_at FROM crawl_records"
                ):
                    records[(str(database.resolve()), str(crawl_time))] = (int(total_items), str(created_at))
        except sqlite3.Error:
            continue
    return records


def changed_crawl_record(
    before: dict[tuple[str, str], tuple[int, str]],
    after: dict[tuple[str, str], tuple[int, str]],
) -> tuple[Path, str] | None:
    changed = [key for key, value in after.items() if before.get(key) != value]
    if not changed:
        return None
    changed.sort(key=lambda key: (after[key][1], key[0], key[1]))
    database, crawl_time = changed[-1]
    return Path(database), crawl_time


def _observed_at(database: Path, crawl_time: str) -> str:
    normalized_time = crawl_time.replace("-", ":")
    local = datetime.strptime(f"{database.stem} {normalized_time}", "%Y-%m-%d %H:%M")
    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()


def export_crawl(database: Path, crawl_time: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    observed_at = _observed_at(database, crawl_time)
    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT n.id, n.title, n.platform_id, p.name AS platform_name,
                   n.rank, n.url, n.mobile_url
            FROM news_items n
            JOIN platforms p ON p.id = n.platform_id
            WHERE n.last_crawl_time = ?
            ORDER BY n.platform_id, n.rank, n.id
            """,
            (crawl_time,),
        ).fetchall()
        statuses = conn.execute(
            """
            SELECT css.platform_id, css.status
            FROM crawl_source_status css
            JOIN crawl_records cr ON cr.id = css.crawl_record_id
            WHERE cr.crawl_time = ?
            ORDER BY css.platform_id
            """,
            (crawl_time,),
        ).fetchall()
    items = [
        {
            "id": f"{row['platform_id']}:{row['id']}",
            "title": str(row["title"]),
            "url": str(row["url"] or row["mobile_url"] or ""),
            "channel": str(row["platform_name"]),
            "platform_id": str(row["platform_id"]),
            "rank": int(row["rank"]),
            "observed_at": observed_at,
            "raw_source": {
                "database": str(database.resolve()),
                "table": "news_items",
                "record_id": int(row["id"]),
                "crawl_time": crawl_time,
            },
        }
        for row in rows
    ]
    return items, [dict(row) for row in statuses]


def run_once(
    *,
    trendradar_dir: Path,
    output: Path,
    evidence_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    install = verify_official_install(trendradar_dir)
    trendradar_dir = trendradar_dir.resolve()
    output = output.resolve()
    evidence_root = evidence_root.resolve()
    started = datetime.now(timezone.utc)
    run_name = started.strftime("%Y%m%dT%H%M%S.%fZ")
    evidence_dir = evidence_root / run_name
    evidence_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = evidence_dir / "stdout.log"
    stderr_path = evidence_dir / "stderr.log"
    manifest_path = evidence_dir / "manifest.json"
    command = [install["python_executable"], "-m", "trendradar"]
    before = crawl_record_snapshot(trendradar_dir)
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    begin_monotonic = time.monotonic()
    status = "failed"
    return_code: int | None = None
    error: str | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=trendradar_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        return_code = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
        if completed.returncode != 0:
            error = f"TrendRadar exited with status {completed.returncode}; no retry was attempted"
        else:
            status = "command_completed"
    except subprocess.TimeoutExpired as exc:
        status = "timed_out"
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        error = f"TrendRadar exceeded {timeout_seconds} seconds; no retry was attempted"
    except KeyboardInterrupt:
        status = "interrupted"
        error = "TrendRadar was interrupted; no retry was attempted"
    duration_seconds = round(time.monotonic() - begin_monotonic, 3)
    completed_at = datetime.now(timezone.utc)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    base_manifest: dict[str, Any] = {
        "status": status,
        "error": error,
        "started_at": started.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": duration_seconds,
        "return_code": return_code,
        "command": command,
        "cwd": str(trendradar_dir),
        "automatic_retry": False,
        "install": install,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output),
        "evidence_dir": str(evidence_dir),
    }
    if status != "command_completed":
        _atomic_json(manifest_path, base_manifest)
        return base_manifest | {"manifest_path": str(manifest_path), "items": []}

    after = crawl_record_snapshot(trendradar_dir)
    changed = changed_crawl_record(before, after)
    if changed is None:
        base_manifest.update(status="failed_no_new_crawl", error="official command produced no new or updated crawl record")
        _atomic_json(manifest_path, base_manifest)
        return base_manifest | {"manifest_path": str(manifest_path), "items": []}
    database, crawl_time = changed
    items, source_statuses = export_crawl(database, crawl_time)
    failed_sources = [row["platform_id"] for row in source_statuses if row["status"] != "success"]
    final_status = "completed_with_failures" if failed_sources else "completed"
    if not items:
        final_status = "failed_empty_output"
        error = "real crawl record contained no hotspot items"
    provenance = {
        "provider": "TrendRadar",
        "official_origin": install["origin"],
        "official_commit": install["commit"],
        "official_version": install["version"],
        "raw_database": str(database.resolve()),
        "crawl_time": crawl_time,
        "source_statuses": source_statuses,
        "failed_sources": failed_sources,
        "evidence_dir": str(evidence_dir),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "manifest_path": str(manifest_path),
        "started_at": started.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": duration_seconds,
        "automatic_retry": False,
    }
    normalized = {"items": items, "provenance": provenance}
    _atomic_json(output, normalized)
    base_manifest.update(
        status=final_status,
        error=error,
        item_count=len(items),
        raw_database=str(database.resolve()),
        crawl_time=crawl_time,
        source_statuses=source_statuses,
        failed_sources=failed_sources,
    )
    _atomic_json(manifest_path, base_manifest)
    return base_manifest | {"manifest_path": str(manifest_path), "items": items}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trendradar-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    try:
        result = run_once(
            trendradar_dir=args.trendradar_dir,
            output=args.output,
            evidence_root=args.evidence_root,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(_canonical({"status": "preflight_failed", "error": str(exc), "automatic_retry": False}))
        return 2
    printable = {key: value for key, value in result.items() if key != "items"}
    print(_canonical(printable))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
