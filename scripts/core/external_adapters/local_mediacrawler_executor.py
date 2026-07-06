from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalCommandResult,
    local_repo_path,
)


_PLATFORM_MAP = {
    "douyin": "dy",
    "dy": "dy",
    "xiaohongshu": "xhs",
    "xhs": "xhs",
    "kuaishou": "ks",
    "ks": "ks",
    "bilibili": "bili",
    "bili": "bili",
    "weibo": "wb",
    "wb": "wb",
    "tieba": "tieba",
    "zhihu": "zhihu",
}


@dataclass(frozen=True)
class LocalMediaCrawlerExecutor:
    archive_root: Path | None = None
    python_executable: Path | None = None
    timeout_seconds: int | None = None

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        if command.adapter_id not in {"collector.mediacrawler", "collector.comments"}:
            raise ExternalAdapterError(f"unsupported local MediaCrawler adapter: {command.adapter_id}")
        if command.capability not in {"platform.video_snapshot", "platform.comment_collection"}:
            raise ExternalAdapterError(f"unsupported local MediaCrawler capability: {command.capability}")

        run_dir = self._make_run_dir(command)
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        args = self._build_args(command, raw_dir)
        stdout_path = run_dir / "stdout.txt"
        stderr_path = run_dir / "stderr.txt"
        manifest_path = run_dir / "command_manifest.json"
        manifest_path.write_text(
            json.dumps(
                command.sanitized_manifest()
                | {"raw_dir": str(raw_dir), "effective_timeout_seconds": self.timeout_seconds or command.timeout_seconds},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            completed = subprocess.run(
                args,
                cwd=mediacrawler_dir,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds or command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "MediaCrawler timed out.", encoding="utf-8")
            # The subprocess was actually launched (real browser/network activity may have
            # started) before timing out, so this is a real external side effect, not a
            # no-op -- make that explicit rather than relying on the dataclass default.
            return ExternalCommandResult(
                status="failed_timeout",
                payload={"error": "MediaCrawler timed out"},
                raw_archive_ref=str(run_dir),
                external_side_effect=True,
            )

        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")

        if completed.returncode != 0:
            # Same reasoning as the timeout branch: the process ran, so treat it as a
            # real external side effect rather than leaving this to the default value.
            return ExternalCommandResult(
                status=f"failed_exit_{completed.returncode}",
                payload={"error": "MediaCrawler exited with a non-zero status"},
                raw_archive_ref=str(run_dir),
                external_side_effect=True,
            )

        payload = self._read_payload(command, raw_dir)
        return ExternalCommandResult(
            status="succeeded",
            payload=payload,
            raw_archive_ref=str(run_dir),
            external_side_effect=True,
        )

    def _make_run_dir(self, command: ExternalAdapterCommand) -> Path:
        root = self.archive_root or local_repo_path("data", "formal", "phase8_activation", "mediacrawler")
        root.mkdir(parents=True, exist_ok=True)
        counter = 1
        stem = command.capability.replace(".", "_")
        while True:
            candidate = root / f"{stem}_{counter:03d}"
            if not candidate.exists():
                candidate.mkdir(parents=True)
                return candidate
            counter += 1

    def _build_args(self, command: ExternalAdapterCommand, raw_dir: Path) -> list[str]:
        platform = _PLATFORM_MAP.get(str(command.input_payload.get("platform") or "").lower())
        if not platform:
            raise ExternalAdapterError("MediaCrawler platform is missing or unsupported")
        source_url = str(command.input_payload.get("source_url") or "").strip()
        if not source_url:
            raise ExternalAdapterError("MediaCrawler source_url is required")

        source_kind = str(command.input_payload.get("source_kind") or "detail")
        if command.capability == "platform.comment_collection":
            source_kind = "detail"
        if source_kind not in {"creator", "detail"}:
            raise ExternalAdapterError(f"unsupported MediaCrawler source_kind: {source_kind}")

        python = self._python()
        args = [
            str(python),
            "main.py",
            "--platform",
            platform,
            "--type",
            source_kind,
            "--save_data_option",
            "jsonl",
            "--save_data_path",
            str(raw_dir),
            "--crawler_max_notes_count",
            str(command.max_items),
            "--max_concurrency_num",
            "1",
            "--headless",
            "yes" if bool(command.input_payload.get("headless", True)) else "no",
            "--get_comment",
            "yes" if command.input_payload.get("with_comments") or command.capability == "platform.comment_collection" else "no",
            "--get_sub_comment",
            "no",
        ]
        if source_kind == "creator":
            args.extend(["--creator_id", source_url])
        else:
            args.extend(["--specified_id", source_url])
        if command.capability == "platform.comment_collection":
            args.extend(["--max_comments_count_singlenotes", str(command.max_items)])
        return args

    def _python(self) -> Path:
        if self.python_executable:
            return self.python_executable
        mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
        for candidate in (
            mediacrawler_dir / ".venv" / "Scripts" / "python.exe",
            mediacrawler_dir / ".venv312" / "Scripts" / "python.exe",
        ):
            if candidate.exists():
                return candidate
        resolved = shutil.which("python") or sys.executable
        return Path(resolved)

    def _read_payload(self, command: ExternalAdapterCommand, raw_dir: Path) -> dict[str, Any]:
        platform_dir = raw_dir / _payload_platform_dir(command)
        jsonl_dir = platform_dir / "jsonl"
        if command.capability == "platform.comment_collection":
            return {"comments": _read_jsonl_files(jsonl_dir.glob("*_comments_*.jsonl"), command.max_items)}
        return {"items": _read_jsonl_files(jsonl_dir.glob("*_contents_*.jsonl"), command.max_items)}


def _payload_platform_dir(command: ExternalAdapterCommand) -> str:
    platform = str(command.input_payload.get("platform") or "").lower()
    return "douyin" if platform in {"douyin", "dy"} else platform


def _read_jsonl_files(files: Any, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(files):
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(rows) >= limit:
                    return rows
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if isinstance(value, dict):
                    rows.append(value)
    return rows
