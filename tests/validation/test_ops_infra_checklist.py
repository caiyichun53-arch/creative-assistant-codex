from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validation.ops_infra_checklist import (
    check_gitignore_covers_sensitive_paths,
    check_no_committed_real_env_files,
    run_checklist,
    scan_hardcoded_paths,
)


class ScanHardcodedPathsTests(unittest.TestCase):
    """Regression coverage for the audit finding this script generalizes:
    ffmpeg_path/local_asr_python were hardcoded absolute paths, duplicated in
    two files, invisible to every business-rule gate. Proves the detector
    would actually catch a new instance, not just that today's repo is clean."""

    def test_flags_a_new_windows_drive_letter_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "tools" / "new_offender.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text('FFMPEG = "I:/AI_Models/ffmpeg/ffmpeg.exe"\n', encoding="utf-8")

            with patch("scripts.validation.ops_infra_checklist.ROOT", fake_root):
                hits = scan_hardcoded_paths(["scripts/tools/new_offender.py"])

        self.assertEqual(len(hits), 1)
        self.assertIn("I:/AI_Models/ffmpeg/ffmpeg.exe", hits[0]["hardcoded_paths"])

    def test_flags_a_new_unix_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "tools" / "new_offender.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text('MODEL_DIR = "/Users/someone/models"\n', encoding="utf-8")

            with patch("scripts.validation.ops_infra_checklist.ROOT", fake_root):
                hits = scan_hardcoded_paths(["scripts/tools/new_offender.py"])

        self.assertEqual(len(hits), 1)

    def test_test_files_are_exempt_since_they_legitimately_use_fake_paths_as_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "tests" / "core" / "test_something.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text('DISTINCTIVE = "C:/distinctive/ffmpeg.exe"\n', encoding="utf-8")

            with patch("scripts.validation.ops_infra_checklist.ROOT", fake_root):
                hits = scan_hardcoded_paths(["tests/core/test_something.py"])

        self.assertEqual(hits, [])

    def test_clean_file_produces_no_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "tools" / "clean.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text('FFMPEG = settings["reverse_engine"]["ffmpeg_path"]\n', encoding="utf-8")

            with patch("scripts.validation.ops_infra_checklist.ROOT", fake_root):
                hits = scan_hardcoded_paths(["scripts/tools/clean.py"])

        self.assertEqual(hits, [])


class GitignoreCoverageTests(unittest.TestCase):
    def test_real_repo_gitignore_covers_all_required_patterns(self) -> None:
        result = check_gitignore_covers_sensitive_paths()
        self.assertTrue(result["passed"], result["detail"])


class NoCommittedRealEnvFilesTests(unittest.TestCase):
    def test_real_repo_has_no_committed_real_env_file(self) -> None:
        result = check_no_committed_real_env_files()
        self.assertTrue(result["passed"], result["detail"])


class RunChecklistTests(unittest.TestCase):
    def test_real_repo_passes_the_whole_checklist(self) -> None:
        result = run_checklist()
        self.assertEqual(result["status"], "PASS", result["checks"])


if __name__ == "__main__":
    unittest.main()
