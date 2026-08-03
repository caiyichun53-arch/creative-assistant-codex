from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalCommandResult,
    local_repo_path,
)
from scripts.core.external_adapters.windows_process import hidden_process_kwargs
from scripts.core.runtime.liveness import RuntimeLivenessError, run_process_with_liveness
from scripts.core.runtime.runtime_storage import require_runtime_path, runtime_path


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


# MediaCrawler uses one retained Douyin browser/login profile. Two crawler
# processes must never drive that profile at the same time. Creator-history
# requests have priority over repeated detail/comment calls so the remaining
# cold-start accounts cannot be starved while media preparation is running.
_MEDIACRAWLER_SESSION_LOCK = threading.Condition()
_MEDIACRAWLER_SESSION_ACTIVE = False
_MEDIACRAWLER_CREATOR_WAITERS = 0
_MEDIACRAWLER_LAST_REQUEST_FINISHED_AT = 0.0
_MEDIACRAWLER_JITTER = random.SystemRandom()
_MEDIACRAWLER_BROWSER_LOCK = threading.Lock()
_MEDIACRAWLER_CDP_PORT = 9222


def _active_douyin_profile_name() -> str:
    state_path = runtime_path("agent_platform", "mediacrawler_browser_state.json")
    if not state_path.is_file():
        return "primary"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ExternalAdapterError("Douyin browser account state is unreadable") from exc
    profile_name = str(payload.get("active_profile") or "").strip().lower()
    if not profile_name or not all(character.isalnum() or character in {"_", "-"} for character in profile_name):
        raise ExternalAdapterError("Douyin browser account state has an invalid active profile")
    return profile_name


def _active_douyin_profile_dir(mediacrawler_dir: Path) -> Path:
    del mediacrawler_dir
    profile_root = runtime_path("external", "mediacrawler", "browser_data")
    profile_name = _active_douyin_profile_name()
    if profile_name == "primary":
        return profile_root / "cdp_dy_user_data_dir"
    return profile_root / f"cdp_dy_user_data_dir_{profile_name}"


def _cdp_port_ready(port: int = _MEDIACRAWLER_CDP_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _find_windows_chrome() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ExternalAdapterError("Google Chrome is required for the retained Douyin browser session")


def _shared_browser_process_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    # Keep a real rendered browser without activating it over the user's work.
    startupinfo.wShowWindow = getattr(subprocess, "SW_SHOWMINNOACTIVE", 7)
    return {
        # The dedicated browser is intentionally independent from the short
        # collection launcher.  Without breakaway, Windows can end it when the
        # launcher exits, defeating long-lived session reuse.
        "creationflags": (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        ),
        "startupinfo": startupinfo,
    }


def _ensure_shared_douyin_browser(mediacrawler_dir: Path) -> None:
    """Require a separately maintained, reusable collector browser.

    A collection task must never start, close, or replace this browser. That
    would repeatedly reset the platform session and can steal focus from the
    user's work. The browser is started only through the explicit maintenance
    entrypoint below, then all collection tasks attach to it.
    """
    del mediacrawler_dir
    with _MEDIACRAWLER_BROWSER_LOCK:
        if _cdp_port_ready():
            return
        raise ExternalAdapterError(
            "the reusable collector browser is not ready; start or restore its dedicated logged-in session before collecting"
        )


def retained_douyin_collector_browser_status(mediacrawler_dir: Path) -> dict[str, Any]:
    """Report collector-browser readiness without starting or changing anything."""
    profile_name = _active_douyin_profile_name()
    profile_dir = _active_douyin_profile_dir(mediacrawler_dir)
    return {
        "status": "ready" if _cdp_port_ready() else "not_ready",
        "cdp_port": _MEDIACRAWLER_CDP_PORT,
        "profile_name": profile_name,
        "profile_dir": str(profile_dir),
        "profile_exists": profile_dir.is_dir(),
        "login_status": "not_verified",
    }


def start_retained_douyin_collector_browser(mediacrawler_dir: Path) -> None:
    """Explicit maintenance action: start the one reusable collector browser.

    This is deliberately separate from collection execution. It is called only
    when an operator decides to start or restore the dedicated session.
    """
    with _MEDIACRAWLER_BROWSER_LOCK:
        if _cdp_port_ready():
            return
        browser_path = _find_windows_chrome()
        profile_dir = _active_douyin_profile_dir(mediacrawler_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(browser_path),
            f"--remote-debugging-port={_MEDIACRAWLER_CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=TranslateUI",
            "--disable-sync",
            "--disable-extensions",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--start-minimized",
            f"--user-data-dir={profile_dir}",
            "https://www.douyin.com/",
        ]
        browser = subprocess.Popen(
            args,
            cwd=mediacrawler_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_shared_browser_process_kwargs(),
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if _cdp_port_ready():
                return
            if browser.poll() is not None:
                break
            time.sleep(0.25)
        raise ExternalAdapterError("retained Douyin browser could not start")


def _request_spacing_delay(
    *,
    last_finished_at: float,
    now: float,
    min_interval_seconds: float,
    jitter_seconds: float,
) -> float:
    """Return the remaining respectful pause before another crawler request."""
    return max(0.0, last_finished_at + min_interval_seconds + jitter_seconds - now)


@contextmanager
def _mediacrawler_session(
    *,
    creator_priority: bool,
    min_interval_seconds: float = 0.0,
    jitter_seconds: float = 0.0,
):
    global _MEDIACRAWLER_CREATOR_WAITERS, _MEDIACRAWLER_SESSION_ACTIVE
    global _MEDIACRAWLER_LAST_REQUEST_FINISHED_AT
    sampled_jitter = (
        _MEDIACRAWLER_JITTER.uniform(0.0, jitter_seconds)
        if jitter_seconds > 0
        else 0.0
    )
    with _MEDIACRAWLER_SESSION_LOCK:
        if creator_priority:
            _MEDIACRAWLER_CREATOR_WAITERS += 1
        try:
            while True:
                if _MEDIACRAWLER_SESSION_ACTIVE or (
                    not creator_priority and _MEDIACRAWLER_CREATOR_WAITERS > 0
                ):
                    _MEDIACRAWLER_SESSION_LOCK.wait()
                    continue
                spacing_delay = _request_spacing_delay(
                    last_finished_at=_MEDIACRAWLER_LAST_REQUEST_FINISHED_AT,
                    now=time.monotonic(),
                    min_interval_seconds=min_interval_seconds,
                    jitter_seconds=sampled_jitter,
                )
                if spacing_delay > 0:
                    _MEDIACRAWLER_SESSION_LOCK.wait(timeout=spacing_delay)
                    continue
                break
            _MEDIACRAWLER_SESSION_ACTIVE = True
        finally:
            if creator_priority:
                _MEDIACRAWLER_CREATOR_WAITERS -= 1
    try:
        yield
    finally:
        with _MEDIACRAWLER_SESSION_LOCK:
            _MEDIACRAWLER_LAST_REQUEST_FINISHED_AT = time.monotonic()
            _MEDIACRAWLER_SESSION_ACTIVE = False
            _MEDIACRAWLER_SESSION_LOCK.notify_all()


@dataclass(frozen=True)
class LocalMediaCrawlerExecutor:
    archive_root: Path | None = None
    python_executable: Path | None = None
    timeout_seconds: int | None = None
    creator_min_interval_seconds: float = 45.0
    creator_jitter_seconds: float = 30.0
    detail_min_interval_seconds: float = 15.0
    detail_jitter_seconds: float = 15.0
    progress_callback: Callable[[str], None] | None = None

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        if command.adapter_id not in {"collector.mediacrawler", "collector.comments"}:
            raise ExternalAdapterError(f"unsupported local MediaCrawler adapter: {command.adapter_id}")
        if command.capability not in {"platform.video_snapshot", "platform.comment_collection", "platform.keyword_search"}:
            raise ExternalAdapterError(f"unsupported local MediaCrawler capability: {command.capability}")

        mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        platform = _PLATFORM_MAP.get(str(command.input_payload.get("platform") or "").lower())
        if not platform:
            raise ExternalAdapterError("MediaCrawler platform is missing or unsupported")

        source_kind = str(command.input_payload.get("source_kind") or "detail")
        if any(
            value < 0
            for value in (
                self.creator_min_interval_seconds,
                self.creator_jitter_seconds,
                self.detail_min_interval_seconds,
                self.detail_jitter_seconds,
            )
        ):
            raise ExternalAdapterError("MediaCrawler pacing values cannot be negative")
        # The retained browser profile and the archive attempt are one indivisible
        # session. Creator-history requests get the next free slot before another
        # detail call. Creator runs also pause between accounts with a small random
        # offset so a cold start cannot hit Douyin in one fixed rapid pattern.
        with _mediacrawler_session(
            creator_priority=source_kind == "creator",
            min_interval_seconds=(
                self.creator_min_interval_seconds
                if source_kind == "creator"
                else self.detail_min_interval_seconds
            ),
            jitter_seconds=(
                self.creator_jitter_seconds
                if source_kind == "creator"
                else self.detail_jitter_seconds
            ),
        ):
            if platform == "dy":
                _ensure_shared_douyin_browser(mediacrawler_dir)
            self._cleanup_incomplete_run_dirs(command)
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
            self._write_attempt_state(run_dir, status="running")

            try:
                completed = run_process_with_liveness(
                    args,
                    cwd=mediacrawler_dir,
                    env=env,
                    kind="collection",
                    process_options=hidden_process_kwargs(),
                    on_activity=self.progress_callback,
                )
            except RuntimeLivenessError as exc:
                self._discard_incomplete_run(run_dir)
                return ExternalCommandResult(
                    status="failed_timeout",
                    payload={"error": str(exc), "liveness_state": exc.state, "activity_count": exc.activity_count},
                    raw_archive_ref=None,
                    external_side_effect=True,
                )

            stdout_path.write_text(completed.stdout or "", encoding="utf-8")
            stderr_path.write_text(completed.stderr or "", encoding="utf-8")

            if completed.returncode != 0:
                exit_code = completed.returncode
                self._discard_incomplete_run(run_dir)
                return ExternalCommandResult(
                    status=f"failed_exit_{exit_code}",
                    payload={"error": "MediaCrawler exited with a non-zero status; incomplete archive was removed"},
                    raw_archive_ref=None,
                    external_side_effect=True,
                )

            payload = self._read_payload(command, raw_dir)
            if command.capability == "platform.video_snapshot" and not payload.get("items"):
                stderr_tail = "\n".join((completed.stderr or completed.stdout or "").splitlines()[-12:]).strip()
                error = "MediaCrawler returned no historical items"
                if stderr_tail:
                    error += "; last output: " + stderr_tail[-2000:]
                self._discard_incomplete_run(run_dir)
                return ExternalCommandResult(
                    status="failed_empty_result",
                    payload={"error": error + "; incomplete archive was removed"},
                    raw_archive_ref=None,
                    external_side_effect=True,
                )

            item_count = len(payload.get("items") or payload.get("comments") or [])
            self._write_attempt_state(run_dir, status="succeeded", item_count=item_count)
            return ExternalCommandResult(
                status="succeeded",
                payload=payload,
                raw_archive_ref=str(run_dir),
                external_side_effect=True,
            )

    def _archive_root(self) -> Path:
        root = require_runtime_path(
            self.archive_root or runtime_path("formal", "mediacrawler_runtime"),
            purpose="MediaCrawler archive",
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _make_run_dir(self, command: ExternalAdapterCommand) -> Path:
        root = self._archive_root()
        counter = 1
        stem = command.capability.replace(".", "_")
        while True:
            candidate = root / f"{stem}_{counter:03d}"
            if not candidate.exists():
                candidate.mkdir(parents=True)
                return candidate
            counter += 1

    def _cleanup_incomplete_run_dirs(self, command: ExternalAdapterCommand) -> None:
        root = self._archive_root()
        stem = command.capability.replace(".", "_")
        for candidate in sorted(root.glob(f"{stem}_*")):
            if not candidate.is_dir():
                continue
            state_path = candidate / "attempt_state.json"
            if state_path.is_file():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    state = {}
                if state.get("status") == "succeeded":
                    continue
                self._discard_incomplete_run(candidate)
                continue

            # Compatibility with archives created before attempt markers existed:
            # a non-empty contents/comments file proves that the external collector
            # completed enough to produce raw source data.  Empty shells are residue.
            raw_dir = candidate / "raw"
            raw_files = list(raw_dir.rglob("*_contents_*.jsonl")) + list(raw_dir.rglob("*_comments_*.jsonl"))
            if any(path.is_file() and path.stat().st_size > 0 for path in raw_files):
                continue
            self._discard_incomplete_run(candidate)

    def _write_attempt_state(self, run_dir: Path, *, status: str, item_count: int | None = None) -> None:
        payload: dict[str, Any] = {"status": status}
        if item_count is not None:
            payload["item_count"] = item_count
        (run_dir / "attempt_state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _discard_incomplete_run(self, run_dir: Path) -> None:
        root = self._archive_root().resolve()
        candidate = run_dir.resolve()
        if candidate.parent != root:
            raise ExternalAdapterError("refusing to remove a MediaCrawler archive outside the configured archive root")
        shutil.rmtree(candidate)

    def _build_args(self, command: ExternalAdapterCommand, raw_dir: Path) -> list[str]:
        platform = _PLATFORM_MAP.get(str(command.input_payload.get("platform") or "").lower())
        if not platform:
            raise ExternalAdapterError("MediaCrawler platform is missing or unsupported")

        source_kind = str(command.input_payload.get("source_kind") or "detail")
        if command.capability == "platform.comment_collection":
            source_kind = "detail"
        # 2026-07-13 (置顶规则总表核对后, A4): "search" wires the real tool's
        # own keyword-search mode (vendor/MediaCrawler/cmd_arg/arg.py's
        # --keywords flag, CRAWLER_TYPE="search" in its own config) -- this
        # was never used before, only "creator"/"detail" were. Search mode
        # takes keywords, not a creator_id/specified_id -- a real, structural
        # difference in what MediaCrawler needs as input, not just a new
        # enum value plugged into the same shape.
        if source_kind not in {"creator", "detail", "search"}:
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
            "yes" if bool(command.input_payload.get("headless", False)) else "no",
            "--get_comment",
            "yes" if command.input_payload.get("with_comments") or command.capability == "platform.comment_collection" else "no",
            "--get_sub_comment",
            "no",
        ]
        if source_kind == "search":
            keywords = command.input_payload.get("keywords")
            if not keywords or not isinstance(keywords, list):
                raise ExternalAdapterError("MediaCrawler search mode requires a non-empty keywords list")
            args.extend(["--keywords", ",".join(str(k) for k in keywords)])
        else:
            source_urls = command.input_payload.get("source_urls")
            if isinstance(source_urls, list):
                normalized_urls = [
                    str(value).strip() for value in source_urls if str(value).strip()
                ]
                source_url = ",".join(dict.fromkeys(normalized_urls))
            else:
                source_url = str(command.input_payload.get("source_url") or "").strip()
            if not source_url:
                raise ExternalAdapterError("MediaCrawler source_url is required")
            if source_kind == "creator":
                args.extend(["--creator_id", source_url])
            else:
                args.extend(["--specified_id", source_url])
        if bool(command.input_payload.get("with_comments")):
            args.extend([
                "--max_comments_count_singlenotes",
                str(int(command.input_payload.get("max_comments_per_item") or 60)),
            ])
        if command.capability == "platform.comment_collection":
            args.extend(["--max_comments_count_singlenotes", str(command.max_items)])
        return args

    def _python(self) -> Path:
        mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
        candidates = (self.python_executable,) if self.python_executable else (
            mediacrawler_dir / ".venv" / "Scripts" / "python.exe",
            mediacrawler_dir / ".venv312" / "Scripts" / "python.exe",
        )
        for candidate in candidates:
            if candidate is None or not candidate.is_file():
                continue
            try:
                probe = subprocess.run(
                    [str(candidate), "-c", "import playwright, httpx"], cwd=mediacrawler_dir,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False,
                    **hidden_process_kwargs(),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return candidate
        if self.python_executable:
            raise ExternalAdapterError("configured MediaCrawler Python runtime cannot start")
        resolved = shutil.which("python") or sys.executable
        fallback = Path(resolved)
        try:
            probe = subprocess.run(
                [str(fallback), "-c", "import playwright, httpx"], cwd=mediacrawler_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, shell=False,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is None or probe.returncode != 0:
            raise ExternalAdapterError("no usable MediaCrawler Python runtime is available")
        return fallback

    def readiness_problems(self) -> list[str]:
        problems: list[str] = []
        mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
        if not (mediacrawler_dir / "main.py").is_file():
            problems.append("MediaCrawler main.py is missing")
        try:
            self._python()
        except ExternalAdapterError as exc:
            problems.append(str(exc))
        return problems

    def _read_payload(self, command: ExternalAdapterCommand, raw_dir: Path) -> dict[str, Any]:
        platform_dir = raw_dir / _payload_platform_dir(command)
        jsonl_dir = platform_dir / "jsonl"
        if command.capability == "platform.comment_collection":
            return {"comments": _read_jsonl_files(jsonl_dir.glob("*_comments_*.jsonl"), command.max_items)}
        payload = {"items": _read_jsonl_files(jsonl_dir.glob("*_contents_*.jsonl"), command.max_items)}
        if bool(command.input_payload.get("with_comments")):
            per_item = int(command.input_payload.get("max_comments_per_item") or 60)
            payload["comments"] = _read_jsonl_files(
                jsonl_dir.glob("*_comments_*.jsonl"),
                max(command.max_items, 1) * per_item,
            )
        return payload


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
