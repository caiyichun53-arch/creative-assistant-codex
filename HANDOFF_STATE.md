# HANDOFF_STATE(现状快照,不是历史记录)

> **活文档**:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。**2026-07-09 改版**:这份文件只保留"现在是什么状态"的结构化快照——详细的过程记录、事故复盘、真实数据验证推理链路搬到 `CHANGELOG.md`(只增不改的历史归档)。新会话开工只需要读这份文件(几分钟能读完),不需要考古 `CHANGELOG.md`;需要查某个具体决策"为什么这么定"时,再去 `CHANGELOG.md` 搜。**接下来打算做什么、按什么顺序**不在这里,在 `ROADMAP.md`——这三份文件分工:`HANDOFF_STATE.md`=现在,`ROADMAP.md`=以后,`CHANGELOG.md`=过去。

## 基本信息

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **上次更新**:2026-07-09,Claude Code(本次会话:外部工程审计 + 按审计建议做的一批治理修复,细节见下方"本次会话做了什么"和 git log)

## 当前状态表

| 模块/领域 | 状态 | 阻塞项 | 谁来决定下一步 |
|---|---|---|---|
| 竞品数据采集/判定(`business_data`) | 生产可用,`CreationAssistant_Daily` 每天08:00自动跑 | 无 | - |
| 备料流水线(转写+评论,`run_reverse_prep.py`) | 生产可用,历史积压已清空,自动触发已验证生效 | 无 | - |
| `runtime_skills/` 绑定(12个业务Skill) | 1个已接通真实数据(`sample_deep_analyze`),11个仍只能吃假样例 | 11个待接的具体绑定工程还没开始 | 用户已定优先顺序(见 ROADMAP.md),工程量本身待排期 |
| 两套 Skill 实现取舍(`.claude/skills/` vs `runtime_skills/`) | 方向已定:`runtime_skills/` 为唯一权威 | 迁移/淘汰尚未执行,`.claude/skills/` 仍在正常使用 | 用户已定方向,执行节奏待定 |
| Windows 定时任务 | `CreationAssistant_Daily` 已启用;2026-07-09 修复了弹窗问题(隐藏窗口方案,免提权) | `CreationAssistant_Listener` 仍 disabled | - |
| 治理文档体系(CLAUDE.md/AGENTS.md/闸门脚本) | 2026-07-09 完成一轮外部审计后的修复(见下) | 无 | - |
| 飞书实时集成 | 未重建(上次 legacy removal 整体删除) | 需要确认是否现在需要 | 用户 |
| GPT 供应商切换 | 未开始 | 非当前阻塞项 | 用户主动触发 |
| 真实付费模型端到端验证 | 未做过,`sample_deep_analyze` 测试全用确定性假 Provider | `HERMES_BUSINESS_MODEL_TOKEN` 等凭据只在 `.env.live-gates`,没进真实 `.env` | 用户 |

## 权威闸门真实输出(不是转述)

```
$ python -m scripts.validation.preflight_checkpoint_check --skip-tests
[PASS] git_working_tree_clean
[PASS] test_suite_green
[PASS] readiness_gate_engineering_ready
[PASS] ops_infra_checklist_clean
overall: READY_TO_CHECKPOINT

$ python -m pytest tests/core tests/validation -q
397 passed
```
（收工前请重新跑一次这条命令确认仍然全绿,不要直接信这里贴的历史输出——这正是本次审计发现的"完成状态必须贴真实闸门输出"这条纪律要检查的事。)

## 本次会话(2026-07-09)做了什么

用户要求对整个系统做一次外部工程审计,审计发现系统缺少路线图、11/12个原子Skill未接真实数据、部分治理闸门存在自证/覆盖盲区等问题(审计报告见对话记录)。审计过程中先处理了一个真实的紧急问题(定时采集弹cmd窗口),随后按审计给出的建议逐项修复:

1. **定时采集弹窗根因修复**:`CreationAssistant_Daily` 计划任务的 `LogonType=Interactive` 导致控制台窗口在桌面会话里弹出——改用 `wscript.exe` 隐藏窗口包装(`scripts/scheduled/run_daily_incremental_hidden.vbs`),免提权,已通过真实 Task Scheduler 一次性测试任务验证生效。
2. **ffmpeg / 本地 ASR Python 解释器路径去硬编码**:原来在两个文件里各写死一份绝对路径,现在从 `config/settings.yaml` 的 `reverse_engine.ffmpeg_path`/`local_asr_python` 读取。
3. **`legacy_removal_gate.py` 补反向测试**:原来只验证"现状干净",没有验证过闸门真的会因为真实违规而报错——现在补了注入真实违规的反向测试。
4. **CLAUDE.md/AGENTS.md 改为机械生成**:`AGENTS.md` 是唯一手工编辑源,`CLAUDE.md` 由 `scripts/validation/generate_constitution_mirror.py` 生成,`test_constitution_sync.py` 检查整份文件(不再只检查 `## 三根支柱` 之后的部分)。
5. **真实生产 `config/settings.yaml` 纳入规则核对**:之前 `test_business_rule_traceability.py` 只检查 `settings.example.yaml`,现在真实(gitignored)配置也接受同样的核对,当前结果干净。
6. **新增 `scripts/validation/preflight_checkpoint_check.py`**:收工/交接前一条命令跑完"工作区干净 + 测试全绿 + 权威闸门 ENGINEERING_READY + 运维基础设施检查"四项。
7. **新增 `scripts/validation/ops_infra_checklist.py`**:机械检查硬编码绝对路径、`.gitignore` 覆盖率、有没有误提交真实 `.env` 文件。
8. **两套 Skill 实现取舍拍板**:以 `runtime_skills/` 为权威,标准是"能否跨平台独立执行"(见上表 + `ROADMAP.md`)。
9. **确定下一步创作链路优先级**:`source_to_topic → content_plan → script_generate`(见 `ROADMAP.md`)。
10. **`TECHNICAL_MANUAL.md` 第14节("出问题了怎么办")从占位补成实际内容**,收录上面几个新脚本的用法。
11. **`HANDOFF_STATE.md` 本身拆分**:详细历史记录搬到 `CHANGELOG.md`,新增 `ROADMAP.md` 承接"接下来做什么"。

全部改动分批提交为独立 commit,每次提交前都跑过 `python -m pytest tests/core tests/validation -q` 和权威闸门,细节见 git log(`activation/goal-v0.6.2-production-activation-01` 分支,2026-07-09 当天的提交)。

## 需要用户决定的事项(未决,不是遗漏)

- `runtime_skills/` 剩余 11 个 Skill 的绑定工程什么时候开始(工程量不小,建议单独排期,不要和治理修复混在一起做)。
- `.claude/skills/*` 具体什么时候开始迁移/淘汰,以什么标准判定"新链路已经够格替代旧的"。
- 是否现在授权真实付费模型调用做一次端到端验证(涉及真实花费)。
- 飞书集成要不要重建、GPT供应商切换要不要启动——这两项一直是"未来用户主动触发项",不是当前阻塞。
