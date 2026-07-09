# HANDOFF_STATE(现状快照,不是历史记录)

> **活文档**:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。**2026-07-09 改版**:这份文件只保留"现在是什么状态"的结构化快照——详细的过程记录、事故复盘、真实数据验证推理链路搬到 `CHANGELOG.md`(只增不改的历史归档)。新会话开工只需要读这份文件(几分钟能读完),不需要考古 `CHANGELOG.md`;需要查某个具体决策"为什么这么定"时,再去 `CHANGELOG.md` 搜。**接下来打算做什么、按什么顺序**不在这里,在 `ROADMAP.md`——这三份文件分工:`HANDOFF_STATE.md`=现在,`ROADMAP.md`=以后,`CHANGELOG.md`=过去。

## 基本信息

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **上次更新**:2026-07-10,Claude Code(2026-07-09 做了一轮外部工程审计+治理修复,2026-07-10 接通了"选题→大纲→成稿"创作链路的三个 Skill,过程中用户发现并纠正了两处真实跑偏,细节见下方)

## 当前状态表

| 模块/领域 | 状态 | 阻塞项 | 谁来决定下一步 |
|---|---|---|---|
| 竞品数据采集/判定(`business_data`) | 生产可用,`CreationAssistant_Daily` 每天08:00自动跑 | 无 | - |
| 备料流水线(转写+评论,`run_reverse_prep.py`) | 生产可用,历史积压已清空,自动触发已验证生效 | 无 | - |
| "选题→大纲→成稿"链路(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`) | 技术链路已打通(端到端集成测试通过),中途卡人工审核闸门 | **选题环节只是简化版**(只用对标爆款+评论区两种料源,评分排序/热点/研究缺口都没做,见 `ROADMAP.md`);还没花钱调用过真实大模型 | 用户 |
| 人工审核闸门(`review_queue.py`) | 能用,但只有"通过/不通过"二选一 | 是否要接入已存在但未连接的 `goal10_corrections`(修正+留痕框架),涉及架构级决策 | 用户 |
| `runtime_skills/` 绑定(12个业务Skill) | 4个已接通真实数据,8个仍只能吃假样例 | 8个待接的具体绑定工程还没排期 | 用户 |
| 两套 Skill 实现取舍(`.claude/skills/` vs `runtime_skills/`) | 方向已定:`runtime_skills/` 为唯一权威 | 迁移/淘汰尚未执行,`.claude/skills/` 仍在正常使用 | 用户已定方向,执行节奏待定 |
| 两套平行架构(Goal01-12 正式链路 vs `business_data`/`experience` 轻量层) | 2026-07-10 发现,从未调和过 | 要不要把新表迁移到 `VersionRef` 体系 | 用户 |
| Windows 定时任务 | `CreationAssistant_Daily` 已启用,弹窗问题已修复 | `CreationAssistant_Listener` 仍 disabled | - |
| 治理文档体系(CLAUDE.md/AGENTS.md/闸门脚本) | 2026-07-09 完成一轮外部审计修复,2026-07-10 补了"未注明来源常量"闸门 | 无 | - |
| 飞书实时集成 | 未重建 | 需要确认是否现在需要 | 用户 |
| GPT 供应商切换 | 未开始 | 非当前阻塞项 | 用户主动触发 |
| 真实付费模型端到端验证 | 未做过,全部 Skill 测试用确定性假 Provider | `HERMES_BUSINESS_MODEL_TOKEN` 等凭据只在 `.env.live-gates`,没进真实 `.env` | 用户 |

## 权威闸门真实输出(不是转述)

```
$ python -m scripts.validation.preflight_checkpoint_check --skip-tests
[PASS] git_working_tree_clean
[PASS] test_suite_green
[PASS] readiness_gate_engineering_ready
[PASS] ops_infra_checklist_clean
[PASS] production_data_sanity
[PASS] no_unsourced_constants
overall: READY_TO_CHECKPOINT

$ python -m pytest tests/core tests/validation -q
484 passed
```
（收工前请重新跑一次这条命令确认仍然全绿,不要直接信这里贴的历史输出。)

## 本次会话(2026-07-09 至 2026-07-10)做了什么

**2026-07-09**:外部工程审计 + 按建议做的治理修复(定时任务弹窗、ffmpeg硬编码、闸门反向测试、CLAUDE.md机械生成、真实配置纳入核对、`preflight_checkpoint_check`/`ops_infra_checklist` 等),细节见 `CHANGELOG.md`。

**2026-07-10**:接通创作链路 + 两次真实纠偏:

1. **接通 `source_to_topic`/`content_plan`/`script_generate` 三个 Skill**,串成"hit_deep_analysis → topic_candidates → content_plans → script_drafts"的完整链路,端到端集成测试验证跑通。
2. **用户第一次纠偏**:指出这条链路①没有人工审核,内容会自动流到成稿;②选题只用了"对标爆款"一种料源,原方法论要求的评论区/研究/热点/打分排序都被跳过了。修复了①(三张表加 `human_review_status` 字段+`review_queue.py` 审核工具,验证过闸门真的会挡住未审核内容),部分修复了②(接入评论区当第二种料源)。
3. **用户第二次纠偏(更严重)**:指出 ①"评论区取最热3条"这个数字是我随手编的,没有文档依据——**这正是"专门防止跑偏的机制"应该管却没管住的东西**;②代码库里已经有 `scripts/core/correction/goal10_corrections.py` 这套"修正+留痕"框架,我却没查就自己写了个简化版审核。核实后发现:「选题判断维度.md」这份文档要求的评分标准全仓库不存在,产出它的技能(`.claude/skills/归纳`)已被删除且无人补位;`goal10_corrections` 确实更贴近真实需求,但绑定在另一套从未接真实数据的 `VersionRef` 持久化体系上,牵出"仓库里有两套平行架构从未调和"这个更大的、之前没写进任何文档的事实。用户要求去 `I:\Creation_assistant`(未清洗的旧系统完整版)查是否有可复用的热点采集开源项目——**查证结果:没有找到**。
4. **新增机制**:`scripts/validation/unsourced_constant_check.py`——扫描新写的业务常量有没有老实标注来源(BR-引用/schema引用/明确 UNSOURCED),接入 `preflight_checkpoint_check.py` 成为第6项检查。`TOP_COMMENTS_PER_HIT` 已改为显式 `UNSOURCED` 标注。

全部改动分批提交为独立 commit,每次提交前都跑过完整测试和权威闸门,细节见 git log。

## 需要用户决定的事项(未决,不是遗漏)

- **评论数量**:`TOP_COMMENTS_PER_HIT=3` 没有依据,需要一个真实合理值(是否该随评论总数浮动)。
- **选题判断维度**:谁来重建这把"评分尺子"、怎么建(不能简单复用 `tactic_extract`)。
- **热点采集**:用什么数据源/工具(旧系统里没有可复用的现成方案)。
- **人工审核该不该接 `goal10_corrections`**:架构级决策,涉及是否要把新表迁移到 `VersionRef` 体系。
- `runtime_skills/` 剩余 8 个 Skill 的绑定工程什么时候开始。
- `.claude/skills/*` 具体什么时候开始迁移/淘汰。
- 是否现在授权真实付费模型调用做一次端到端验证(涉及真实花费)。
- 飞书集成要不要重建、GPT供应商切换要不要启动——一直是"未来用户主动触发项",不是当前阻塞。
