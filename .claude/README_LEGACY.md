# Claude Legacy

**2026-07-11 修正**:这份文件此前自相矛盾——标题说自己是"历史参考",正文却说"当前主链"包含 `scripts/llm/call.py -> codex exec`,而那个脚本早在 2026-07-04(commit `46b421c`)就已经删除,不是当前任何链路的一部分。以下是核实过的现状,不是转述这份文件的旧内容。

**当前唯一的生产加载路径**:`runtime_skills/`(12 个业务 Skill + 1 个测试探针 `runtime_probe`),经 `scripts/core/model_gateway/` 的 Model Port 抽象层调用,4 个(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`)已接上真实数据,详见 `AGENTS.md`/`HANDOFF_STATE.md`。

**不是生产加载路径,但仍在正常使用**(2026-07-09 用户已拍板方向:以 `runtime_skills/` 为唯一权威,但尚未执行迁移):`.claude/skills/*`——依赖 Claude Code 会话内工具/子 agent 机制交互执行,不满足"能否跨平台独立执行"这条裁决标准,但目前仍在正常使用,不要假设它已废弃。

**不是生产加载路径,仅作本地工作流/源材料参考**(`LEGACY_RETIREMENT_MATRIX.yaml` 已标记):`.agents/skills/*`、`.codex/agents/*`——其中多个 SKILL.md(如 `analyze-hit-dna`)引用的脚本(`scripts/reverse/dna.py`、`scripts/llm/call.py`、`scripts/collect/*`、`scripts/topics/*`、`scripts/content/*`)已在 2026-07-04 的 legacy removal 里删除,这些 Skill 的非交互触发链路已断,不得被当作可运行的生产入口。

不要把 `.claude/commands/创作.md` 当成当前入口。它只保留旧流程细节,便于迁移对照。
