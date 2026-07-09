"""Generates CLAUDE.md from AGENTS.md, the canonical source of the project's
"constitution" (governance rules for whichever coding agent is doing the work).

Why this exists: CLAUDE.md and AGENTS.md used to be two independently
hand-edited files that were supposed to stay word-for-word identical except
for a couple of tool-name mentions. That manual-sync discipline held for
124 commits without anyone noticing it had already drifted (CLAUDE.md's body
kept describing the retired pre-rebuild architecture long after AGENTS.md,
and the repo's actual code, had moved on -- see AGENTS.md "开工纪律"). A test
(tests/validation/test_constitution_sync.py) can catch drift after the fact,
but it can't stop someone from hand-editing CLAUDE.md and forgetting AGENTS.md
in the first place. Making CLAUDE.md a generated artifact does: there is
nothing to "remember to sync", only one file to edit and one command to run.

Usage:
    Edit AGENTS.md only. Then regenerate CLAUDE.md:
        python -m scripts.validation.generate_constitution_mirror
    tests/validation/test_constitution_sync.py fails if CLAUDE.md does not
    exactly match what this script would produce from the current AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS_MD_PATH = ROOT / "AGENTS.md"
CLAUDE_MD_PATH = ROOT / "CLAUDE.md"

# Ordered, exact-phrase substitutions applied to AGENTS.md's text to produce
# CLAUDE.md. Deliberately not a blanket "Codex" -> "Claude Code" replace:
# the source text itself is inconsistent (e.g. "用户的 Codex 订阅" mirrors to
# "用户的 Claude 订阅", not "用户的 Claude Code 订阅") because that is the
# actual correct English on both sides -- "Claude Code" is the tool, "Claude"
# is the subscription/model brand it runs on. A naive global replace would
# have silently produced wrong prose. Each entry here is verified against the
# real CLAUDE.md by tests/validation/test_constitution_sync.py.
SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("工程章程(AGENTS.md)", "工程章程(CLAUDE.md)"),
    ("一个 **Codex 工程**", "一个 **Claude Code 工程**"),
    ("Codex 当创作驾驶舱", "Claude Code 当创作驾驶舱"),
    ("开发用 Codex(", "开发用 Claude Code("),
    ("节点走 Codex 订阅低阶模型", "节点走 Claude 订阅低阶模型"),
    ("Codex 无头", "Claude Code 无头"),
    ("Codex 不装 cc-switch", "Claude Code 不装 cc-switch"),
    ("用户的 Codex 订阅", "用户的 Claude 订阅"),
    ("默认 Codex 低阶", "默认 Claude 低阶"),
    ("创作走 Codex 对话", "创作走 Claude Code 对话"),
)


def generate_claude_md(agents_md_text: str) -> str:
    # Note: some "Codex" mentions are deliberately shared/identical between
    # both files (e.g. host-adapter lists like "Hermes、Codex、Claude Code、
    # 飞书" and handoff prose like "交接给另一个执行者(Codex/Claude Code 互相
    # 接力)") -- those name both tools together and must NOT be substituted.
    # So this function does not assert "no Codex left"; the real safety net
    # is tests/validation/test_constitution_sync.py asserting an exact match
    # against the committed CLAUDE.md, which will fail loudly if a new
    # Codex-only phrase needs a substitution rule that isn't here yet.
    text = agents_md_text
    for old, new in SUBSTITUTIONS:
        if old not in text:
            raise ValueError(
                f"expected AGENTS.md phrase not found, substitution table is stale: {old!r}. "
                "AGENTS.md changed in a way this generator doesn't know about yet -- "
                "update SUBSTITUTIONS in scripts/validation/generate_constitution_mirror.py."
            )
        text = text.replace(old, new)
    return text


def main() -> int:
    agents_md_text = AGENTS_MD_PATH.read_text(encoding="utf-8")
    generated = generate_claude_md(agents_md_text)
    CLAUDE_MD_PATH.write_text(generated, encoding="utf-8")
    print(f"wrote {CLAUDE_MD_PATH.relative_to(ROOT)} from {AGENTS_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
