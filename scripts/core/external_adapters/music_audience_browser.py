from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from scripts.core.external_adapters.goal_phase4_external_adapters import ExternalAdapterError
from scripts.core.external_adapters.windows_process import hidden_process_kwargs


@dataclass(frozen=True)
class LocalMusicAudienceBrowserExecutor:
    """Run NetEase and Douban in separate persistent browser profiles and one session per platform."""

    python_executable: Path
    worker_path: Path
    netease_profile_dir: Path
    douban_profile_dir: Path
    timeout_seconds: int = 900

    def collect_initial_person_scan(
        self,
        *,
        person_name: str,
        works: tuple[dict[str, Any], ...],
        representative_item_minimum: int,
        representative_item_maximum: int,
        total_viewed_limit: int,
    ) -> dict[str, Any]:
        if (
            not person_name.strip()
            or not representative_item_minimum <= len(works) <= representative_item_maximum
            or total_viewed_limit <= 0
        ):
            raise ExternalAdapterError("music person scan does not meet its explicit domain limits")
        if not self.python_executable.is_file() or not self.worker_path.is_file():
            raise ExternalAdapterError("music audience browser runtime is not configured")
        request = {
            "person_name": person_name.strip(),
            "works": list(works),
            "netease_profile_dir": str(self.netease_profile_dir),
            "douban_profile_dir": str(self.douban_profile_dir),
            "limits": {
                "netease_comments_per_song": 20,
                "douban_long_reviews_per_subject": 5,
                "douban_short_reviews_per_subject": 20,
                "total_viewed": total_viewed_limit,
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            request_path, output_path = Path(folder) / "request.json", Path(folder) / "output.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [str(self.python_executable), str(self.worker_path), str(request_path), str(output_path)],
                text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout_seconds, check=False, shell=False,
                **hidden_process_kwargs(),
            )
            if completed.returncode != 0 or not output_path.is_file():
                raise ExternalAdapterError("music audience browser worker failed without a valid retained result")
            value = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("platforms"), dict) or not isinstance(value.get("materials"), list):
            raise ExternalAdapterError("music audience browser result is invalid")
        return value
