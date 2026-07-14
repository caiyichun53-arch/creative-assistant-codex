from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Maps each top-level scripts/core/<x> module to the manual section that must
# exist and mention it. This is a structural presence check only -- it cannot
# verify the prose is accurate or complete, only that nobody shipped a whole
# new module while leaving the manual untouched (the exact failure mode the
# "module built, manual never updated" rule in AGENTS.md
# exists to prevent).
CORE_MODULE_TO_MANUAL_SECTION = {
    "business_data": "竞品数据层",
    "host": "生产 Host 边界",
    "model_gateway": "模型路由",
    "workflow": "业务工作流",
    "scheduler": "调度",
    "persistence": "持久化与状态",
    "state": "持久化与状态",
    "correction": "纠错与实验",
    "experience": "经验库",
    "external_adapters": "外部适配器",
    "hermes": "外部适配器",
    "research": "研究",
    "production": "工程验收脚本",
    "runtime": "工程验收脚本",
    "staging": "工程验收脚本",
}


class TechnicalManualSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        manual_path = ROOT / "TECHNICAL_MANUAL.md"
        self.assertTrue(
            manual_path.exists(),
            "TECHNICAL_MANUAL.md must exist -- see the AGENTS.md rule "
            "requiring every scripts/core/<module> to have a corresponding section.",
        )
        self.manual_text = manual_path.read_text(encoding="utf-8")

    def test_every_core_module_has_a_manual_section(self) -> None:
        core_dir = ROOT / "scripts" / "core"
        actual_modules = {
            path.name
            for path in core_dir.iterdir()
            if path.is_dir() and not path.name.startswith("__")
        }
        missing = []
        for module in sorted(actual_modules):
            if module not in CORE_MODULE_TO_MANUAL_SECTION:
                missing.append(
                    f"scripts/core/{module} has no entry in "
                    "CORE_MODULE_TO_MANUAL_SECTION -- add one, and a matching "
                    "section in TECHNICAL_MANUAL.md, in the same commit that "
                    "introduced this module."
                )
                continue
            section = CORE_MODULE_TO_MANUAL_SECTION[module]
            if section not in self.manual_text:
                missing.append(
                    f"scripts/core/{module} is mapped to manual section "
                    f"'{section}' but that section heading is not present in "
                    "TECHNICAL_MANUAL.md."
                )
        self.assertEqual(missing, [], "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
