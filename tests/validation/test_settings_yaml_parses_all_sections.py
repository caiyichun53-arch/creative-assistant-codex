from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# 2026-07-08: config/settings.yaml was found to have a real, silent data-
# corruption bug -- an encoding round-trip somewhere in its history dropped
# the newline character immediately before three top-level section headers
# (crawler:, humanize:, language_fuel:) and one nested key (crawler.
# incremental), gluing each one onto the end of the preceding Chinese
# comment line. YAML parsing did not fail (the glued text just became part
# of a `#`-comment), so yaml.safe_load() silently returned a dict missing
# those three sections entirely -- no error until code actually did
# settings["crawler"], which nothing did until this session wired up the
# --daily-incremental CLI flag. This test makes that class of silent
# section-loss mechanically detectable: every top-level key documented in
# settings.example.yaml must actually be loadable from the real settings.yaml.
#
# 2026-07-09: found the same bug class one level deeper -- paths.raw,
# candidate_pool.expire_heat, candidate_pool.reheat_boost, and
# legacy_runtime.runtime_authority were all silently glued into a preceding
# comment (same mechanism, just at a nested key instead of a top-level
# section), invisible to the check above since it only compared top-level
# keys. Added a recursive version that walks every nested dict.
def _missing_keys_recursive(real: dict, example: dict, path: str = "") -> list[str]:
    if not isinstance(example, dict):
        return []
    real = real or {}
    missing = [f"{path}.{k}" if path else k for k in example if k not in real]
    for k, v in example.items():
        if isinstance(v, dict) and k in real:
            missing.extend(_missing_keys_recursive(real.get(k), v, f"{path}.{k}" if path else k))
    return missing


class SettingsYamlParsesAllSectionsTests(unittest.TestCase):
    def test_real_settings_has_every_top_level_key_the_example_documents(self) -> None:
        real_path = ROOT / "config" / "settings.yaml"
        example_path = ROOT / "config" / "settings.example.yaml"
        if not real_path.exists():
            self.skipTest("config/settings.yaml is a local-only file, not present in this checkout")

        real = yaml.safe_load(real_path.read_text(encoding="utf-8"))
        example = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        missing = set(example.keys()) - set(real.keys())
        self.assertEqual(
            missing, set(),
            f"config/settings.yaml is missing top-level section(s) {missing} that "
            "config/settings.example.yaml documents -- likely a newline swallowed by an "
            "encoding round-trip gluing the section header onto a comment line (see this "
            "test's module docstring for the exact bug this caught on 2026-07-08).",
        )

    def test_real_settings_has_every_nested_key_the_example_documents(self) -> None:
        real_path = ROOT / "config" / "settings.yaml"
        example_path = ROOT / "config" / "settings.example.yaml"
        if not real_path.exists():
            self.skipTest("config/settings.yaml is a local-only file, not present in this checkout")

        real = yaml.safe_load(real_path.read_text(encoding="utf-8"))
        example = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        missing = _missing_keys_recursive(real, example)
        self.assertEqual(
            missing, [],
            f"config/settings.yaml is missing nested key(s) {missing} that "
            "config/settings.example.yaml documents -- the 2026-07-08 bug one level "
            "deeper (see this test's module docstring for the 2026-07-09 real example).",
        )

    def test_crawler_section_has_daily_max_notes(self) -> None:
        real_path = ROOT / "config" / "settings.yaml"
        if not real_path.exists():
            self.skipTest("config/settings.yaml is a local-only file, not present in this checkout")

        real = yaml.safe_load(real_path.read_text(encoding="utf-8"))
        self.assertIn("crawler", real)
        self.assertIsInstance(real["crawler"].get("daily_max_notes"), int)
        self.assertGreaterEqual(real["crawler"]["daily_max_notes"], 1)


class MissingKeysRecursiveTests(unittest.TestCase):
    def test_flags_a_missing_nested_key(self) -> None:
        real = {"section": {"a": 1}}
        example = {"section": {"a": 1, "b": 2}}
        self.assertEqual(_missing_keys_recursive(real, example), ["section.b"])

    def test_flags_a_missing_top_level_key(self) -> None:
        real = {}
        example = {"section": {"a": 1}}
        self.assertEqual(_missing_keys_recursive(real, example), ["section"])

    def test_no_false_positive_when_everything_present(self) -> None:
        real = {"section": {"a": 1, "b": {"c": 2}}}
        example = {"section": {"a": 1, "b": {"c": 2}}}
        self.assertEqual(_missing_keys_recursive(real, example), [])


if __name__ == "__main__":
    unittest.main()
