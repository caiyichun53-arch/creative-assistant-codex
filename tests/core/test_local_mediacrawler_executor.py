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


def _search_command(keywords: list[str], *, max_items: int = 20) -> ExternalAdapterCommand:
    return ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.keyword_search",
        executable="vendor/MediaCrawler/main.py",
        args=("douyin", "search"),
        input_payload={"platform": "douyin", "source_kind": "search", "keywords": keywords},
        max_items=max_items,
    )


class LocalMediaCrawlerExecutorSearchModeTests(unittest.TestCase):
    """2026-07-13 (置顶规则总表核对后, A4): the real vendored tool's own
    keyword-search mode (vendor/MediaCrawler/cmd_arg/arg.py's --keywords
    flag) was never wired in before -- only creator/detail were."""

    def test_search_mode_passes_comma_joined_keywords_not_a_source_url(self) -> None:
        executor = LocalMediaCrawlerExecutor(python_executable=Path("fake-python"))
        args = executor._build_args(_search_command(["攀比", "婚宴"]), Path("dummy"))  # noqa: SLF001

        self.assertIn("--type", args)
        self.assertEqual(args[args.index("--type") + 1], "search")
        self.assertIn("--keywords", args)
        self.assertEqual(args[args.index("--keywords") + 1], "攀比,婚宴")
        self.assertNotIn("--creator_id", args)
        self.assertNotIn("--specified_id", args)

    def test_search_mode_with_no_keywords_raises_instead_of_silently_searching_nothing(self) -> None:
        executor = LocalMediaCrawlerExecutor(python_executable=Path("fake-python"))
        with self.assertRaises(Exception):
            executor._build_args(_search_command([]), Path("dummy"))  # noqa: SLF001

    def test_search_mode_execute_parses_results_as_items(self) -> None:
        def fake_run(args, **kwargs):
            raw_dir = _save_data_path(args)
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                "\n".join(
                    json.dumps({"aweme_id": f"v{i}", "source_keyword": "攀比"}, ensure_ascii=False)
                    for i in range(3)
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp), python_executable=Path("fake-python"))
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                result = executor.execute(_search_command(["攀比"], max_items=20))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(result.payload["items"]), 3)
        self.assertEqual(result.payload["items"][0]["source_keyword"], "攀比")

    def test_unsupported_capability_still_rejected(self) -> None:
        command = ExternalAdapterCommand(
            adapter_id="collector.mediacrawler",
            capability="platform.something_unrelated",
            executable="vendor/MediaCrawler/main.py",
            args=(),
            input_payload={"platform": "douyin"},
        )
        executor = LocalMediaCrawlerExecutor(python_executable=Path("fake-python"))
        with self.assertRaises(Exception):
            executor.execute(command)


if __name__ == "__main__":
    unittest.main()
