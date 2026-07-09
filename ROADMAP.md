# ROADMAP

> **这份文件的作用,和它跟别的文档的区别**:这是唯一一份"面向未来、排了先后顺序"的计划文档。仓库里其他类似文档都是回顾性质的——`GOAL-*.md`/`implementation_progress/*.md`/`PHASE_*_STATUS.yaml` 记录的是"做完了什么",`HANDOFF_STATE.md` 记录的是"现在进展到哪、上次做了什么"。这份 `ROADMAP.md` 记录的是"接下来打算按什么顺序做什么、为什么"——2026-07-09 的外部工程审计发现全仓库找不到这样一份文档,28 份 GOAL/PHASE 文件状态全部是"已完成",推进方式完全靠每次对话里临时提出新要求驱动。这份文件就是补这个洞的。
>
> **维护方法**:做完一项,把它从"进行中"挪到"已完成"并简单记一句结果(不用像 `HANDOFF_STATE.md` 那样写完整叙事,那是它的活);发现新方向,加进"未排期"分类,不要绕开这份文件直接开一个新的 `GOAL-*.md` 了事——那正是过去反应式推进的模式。这份文件里的优先级排序,凡是标了"用户决定"的都必须由用户拍板,不能自己排。

## 进行中 / 下一项

### 1. 接通"选题→大纲→成稿"全链路(优先级最高,2026-07-09 用户拍板)

`runtime_skills/` 下 12 个业务 Skill 里,目前只有 `sample_deep_analyze` 真正接上了真实数据(见 `scripts/core/experience/run_sample_deep_analyze.py`),其余 11 个只能吃 `fixtures.yaml` 里的假样例过契约测试,拿不到一条真实数据的产出。用户明确要求按这个顺序接下去:

1. **`source_to_topic`**:从证据/来源转成候选选题。需要先确认输入从哪来(哪些真实数据表能组装出它的 input_schema)。
2. **`content_plan`**:选题→钩子+大纲(内部会连续调用 `business.creation_hook`/`business.creation_outline` 两个模型路由,见 `formal_skill_adapter.py:653-725`)。
3. **`script_generate`**:大纲+brief→成稿草稿。

三个绑定脚本的写法参照已验证过的先例 `scripts/core/experience/run_sample_deep_analyze.py`(真实数据组装→调用 Skill 的 `make_*_harness()`→写回业务库,Skill 本身不改)。**这是一块新的工程量,不是文档/治理层面的小修补**,建议单独开一次会话/一个 GOAL 来做,不要和治理修复混在一次提交里。

### 2. 两套 Skill 实现的迁移(2026-07-09 用户拍板方向,尚未开始执行)

**决定**:以 `runtime_skills/` 为唯一权威,裁决标准是"能否跨平台独立执行"——`.claude/skills/*`(选题/大纲/钩子/成稿/审稿/精修/优化诊断/研究/对齐/范例升级/评论真人味/真人写作基石/humanizer-zh,共13个)依赖 Claude Code 会话内 `Read`/`Write` 工具和子 agent 机制,天然不满足这条标准;`runtime_skills/` 经 Model Port 抽象层(`model_router.py` 的 `validate_workflow_node_bindings()` + `business_route_registry.py` 的静态扫描护栏)验证过真正可替换供应商,满足。

**现状**:这只是定了方向和标准,`.claude/skills/*` 一个都还没有迁移/淘汰,目前仍在正常使用,**不要假设它已经废弃**。执行计划(需要用户确认节奏,不由 AI 单方面排):
- 上面"第1项"跑通后,`.claude/skills/` 里的"钩子"/"大纲"/"成稿"三个与 `content_plan`+`script_generate` 职责重叠的会话技能,理论上可以退役——但退役前需要用户确认新链路产出质量不低于旧链路。
- `.claude/skills/审稿`/`精修`/`优化诊断` 与 `runtime_skills/script_review` 的三段式(review+polish+ai_flavor_judge)重叠,同样等新链路验证过再谈退役。
- `范例升级`/`评论真人味`/`真人写作基石`/`选题`/`对齐`/`研究`/`humanizer-zh` 这几个和 `runtime_skills/` 侧的对应关系还没有逐条核实清楚(2026-07-09 审计只按一句话描述做了推断,没打开正文逐条比对),需要单独一次核实再决定去留。

## 未排期(已知缺口,顺序需要用户确认)

以下条目来自历史 `HANDOFF_STATE.md` 记录里明确写过、但从未排进任何计划顺序的已知缺口,搬到这里统一管理,不再散落在交接记录的叙事里:

- **BR-HIT-002(对照/低表现样本)/ BR-HIT-006(五级分级处理 + 深度分析资格)/ BR-HIT-007(评论采集 purpose 标记策略)**:三块规则已在 `BUSINESS_RULE_CATALOG.yaml` 写好,代码故意留白,因为都要挂在"评论采集流水线"上,而这条流水线还没建。
- **完整业务视图仍未建**:当前 `scripts/core/business_data/` 只是账号注册/首采/基线判定这一个切片,不是完整业务层(旧文档里的"观察池/候选池"设计已被 BR-HIT-001 取代,不是缺口,是设计变了)。
- **飞书实时集成**:上一次 legacy removal 整体删除,还没重建,重建前需要用户确认是否真的现在需要。
- **`tactic_extract` 绑定**:排在"选题→大纲→成稿"链路之后,因为它需要至少2条 `sample_deep_analyze` 结果打底做归约,且这次用户明确选了先接创作主链路。
- **真实付费模型调用的凭据缺口**:`sample_deep_analyze` 目前的测试全部用确定性假 Provider,真正的 `HERMES_BUSINESS_MODEL_TOKEN` 等只存在于 `.env.live-gates`,没进真实 `.env`,还没真花过一次钱验证端到端。
- **GPT 供应商切换**(`BUSINESS_MODEL_SWITCH_TO_GPT_PLAN.md`):未开始,不是当前阻塞项,是未来用户主动触发的手动激活项。

## 已完成(2026-07-09,本次治理修复批次)

这批不是业务功能,是这次外部审计后的机制修复,记在这里而不是 `HANDOFF_STATE.md`,因为它们是"一次性做完"的治理动作,不是需要持续维护状态的进行中工作:

- 定时采集弹窗根因修复(计划任务 LogonType 问题,免提权隐藏窗口方案)。
- ffmpeg / 本地 ASR Python 解释器路径改为读配置,不再硬编码。
- `legacy_removal_gate.py` 补了反向测试(证明闸门真的会因为真实违规而报错,不只是"现状干净")。
- `CLAUDE.md` 改为从 `AGENTS.md` 机械生成,不再人工逐字同步。
- 真实生产 `config/settings.yaml`(不只是 example 文件)现在也接受 `BUSINESS_RULE_CATALOG.yaml` 阈值核对。
- 新增 `scripts/validation/preflight_checkpoint_check.py`(收工/交接前一条命令跑完三项纪律检查)和 `scripts/validation/ops_infra_checklist.py`(硬编码路径/凭证管理/`.gitignore` 覆盖率检查)。
