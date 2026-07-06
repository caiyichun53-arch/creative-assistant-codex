from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.external_adapters import ExternalAdapterCommand
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor


def _snapshot_command(max_items: int = 5) -> ExternalAdapterCommand:
    return ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.video_snapshot",
        executable="vendor/MediaCrawler/main.py",
        args=("douyin", "creator"),
        input_payload={
            "platform": "douyin",
            "source_url": "https://www.douyin.com/user/MS4wLjABAAAAabc",
            "source_kind": "creator",
        },
        max_items=max_items,
    )


def _save_data_path(args: list[str]) -> Path:
    return Path(args[args.index("--save_data_path") + 1])


class LocalMediaCrawlerExecutorTests(unittest.TestCase):
    def test_execute_succeeds_and_parses_jsonl_payload(self) -> None:
        def fake_run(args, **kwargs):
            raw_dir = _save_data_path(args)
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                "\n".join(
                    json.dumps({"aweme_id": f"v{i}", "liked_count": i}, ensure_ascii=False)
                    for i in range(3)
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp), python_executable=Path("fake-python"))
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                result = executor.execute(_snapshot_command(max_items=5))

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.external_side_effect)
        self.assertEqual(len(result.payload["items"]), 3)
        self.assertEqual(result.payload["items"][0]["aweme_id"], "v0")

    def test_execute_respects_max_items_limit_and_skips_blank_lines(self) -> None:
        def fake_run(args, **kwargs):
            raw_dir = _save_data_path(args)
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            lines = [json.dumps({"aweme_id": f"v{i}"}, ensure_ascii=False) for i in range(5)]
            lines.insert(2, "")
            (jsonl_dir / "1_contents_2026.jsonl").write_text("\n".join(lines), encoding="utf-8")
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp), python_executable=Path("fake-python"))
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                result = executor.execute(_snapshot_command(max_items=2))

        self.assertEqual(len(result.payload["items"]), 2)

    def test_execute_reports_non_zero_exit_as_failed(self) -> None:
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp), python_executable=Path("fake-python"))
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                result = executor.execute(_snapshot_command())

        self.assertEqual(result.status, "failed_exit_1")
        # The subprocess actually ran (and may have touched the real external system)
        # before failing, so this must be True, not silently left at a default.
        self.assertTrue(result.external_side_effect)

    def test_execute_reports_timeout_as_failed(self) -> None:
        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1, output="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(
                archive_root=Path(tmp), python_executable=Path("fake-python"), timeout_seconds=1
            )
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                result = executor.execute(_snapshot_command())

        self.assertEqual(result.status, "failed_timeout")
        self.assertTrue(result.external_side_effect)

    def test_make_run_dir_increments_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp))
            first = executor._make_run_dir(_snapshot_command())  # noqa: SLF001 - directly exercising the collision behavior.
            second = executor._make_run_dir(_snapshot_command())  # noqa: SLF001

        self.assertNotEqual(first, second)
        self.assertTrue(first.name.endswith("_001"))
        self.assertTrue(second.name.endswith("_002"))


if __name__ == "__main__":
    unittest.main()
