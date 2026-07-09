# ROADMAP

> **这份文件的作用,和它跟别的文档的区别**:这是唯一一份"面向未来、排了先后顺序"的计划文档。仓库里其他类似文档都是回顾性质的——`GOAL-*.md`/`implementation_progress/*.md`/`PHASE_*_STATUS.yaml` 记录的是"做完了什么",`HANDOFF_STATE.md` 记录的是"现在进展到哪、上次做了什么"。这份 `ROADMAP.md` 记录的是"接下来打算按什么顺序做什么、为什么"——2026-07-09 的外部工程审计发现全仓库找不到这样一份文档,28 份 GOAL/PHASE 文件状态全部是"已完成",推进方式完全靠每次对话里临时提出新要求驱动。这份文件就是补这个洞的。
>
> **维护方法**:做完一项,把它从"进行中"挪到"已完成"并简单记一句结果(不用像 `HANDOFF_STATE.md` 那样写完整叙事,那是它的活);发现新方向,加进"未排期"分类,不要绕开这份文件直接开一个新的 `GOAL-*.md` 了事——那正是过去反应式推进的模式。这份文件里的优先级排序,凡是标了"用户决定"的都必须由用户拍板,不能自己排。

## 进行中 / 下一项

### 1. 接通"选题→大纲→成稿"技术链路 —— **[技术环节已打通,业务流程还不完整]**

> **2026-07-10 用户纠偏**:2026-07-09 汇报"链路打通"时说法过头了——验证的只是"数据格式对得上、四个环节能串起来跑",不是"选题这件事做对了"。用户当场指出两个真实缺口:①三个环节之间完全没有人工审核,内容会自动一路流到成稿;②选题只用了"对标爆款"一种料源,项目自己原有的选题方法论(见下方"真实选题流程还缺什么")要求四种料源+打分排序+人工终审,被跳过了大半。①已经在 2026-07-10 修复,②只补了四分之一(评论区),其余留在下面单独列出,不装作已经做完。

`runtime_skills/` 下 12 个业务 Skill,这条主链路的三环都已接上真实数据,且有一条端到端集成测试(`tests/core/test_topic_to_script_chain_integration.py`)证明"一条爆款分析结果"真的能一路流转成"一份成稿草稿",外键全程可追溯(`hit_deep_analysis → topic_candidates → content_plans → script_drafts`),**且中途卡在人工审核闸门上,不会没人看就自动流完**：

1. **`source_to_topic`**:从证据/来源转成候选选题。`scripts/core/experience/run_source_to_topic.py`——吃 `hit_deep_analysis`(sample_deep_analyze 的真实输出:选题/开头/结构手法)+ `hit_comments`(2026-07-10 新增:同一条爆款下面最热的3条真实评论)当证据,生成候选选题,写入新表 `topic_candidates`(带版本号)。18个测试。**明确的简化**:`relation_summary`(这条选题和现有内容是否重复/冲突)现在是老实的占位文字,不是真判断过——`content_relation_judge` 还没接,不冒充。
2. **`content_plan`**:选题→钩子+大纲。`scripts/core/experience/run_content_plan.py`——吃 `topic_candidates` 里 `topic_status='generated'` **且人工已审核通过**的候选选题,写入新表 `content_plans`(带版本号)。16个测试。**明确的简化**:`tactic_candidates`(应由 `tactic_extract` 产出,还没接)复用同一条 `hit_deep_analysis` 的选题/开头/结构手法;`style_examples`(应来自范例库,物理载体还没定)复用同一条视频的真实转写文字稿摘句——都是真实数据、老实标注了替代关系,不是编造。已核实这两个字段目前不影响 `_run_content_plan()` 实际调模型的两次调用(只有 `brief`/`style_examples` 真正进了 prompt),风险可控。
3. **`script_generate`**:大纲+brief→成稿草稿。`scripts/core/experience/run_script_generate.py`——吃 `content_plans` 里**人工已审核通过**、还没生成过草稿的规划,写入新表 `script_drafts`(带版本号)。12个测试。**明确的简化**:`research_summary`(应由 `research_evidence_extract`/`production_research_plan` 产出,都还没接)现在是"汇总已有证据,不是真研究"的老实标注文字。

**人工审核闸门**(2026-07-10 新增,`scripts/core/experience/review_queue.py`):`topic_candidates`/`content_plans`/`script_drafts` 三张表各带一个 `human_review_status` 字段,默认"待审核",第2/3步的查询都要求上一环已经明确标记"通过"才会处理。`python -m scripts.core.experience.review_queue --list` 看有哪些在等审核,`--approve`/`--reject` 标记。**现在只有命令行,没有界面**——先把闸门本身做对,界面是以后的事。8个测试。

**共同的、还没解决的缺口(四个绑定都一样)**:还没花钱调用过一次真实大模型——测试全部用各 Skill 自带的确定性假模型端口,真正调真实模型需要的密钥(`HERMES_BUSINESS_MODEL_TOKEN` 等)只存在于 `.env.live-gates`(专门给一次性受限验证用),没进真实 `.env`。要不要把这几个值搬进真实 `.env`、真的花一次钱验证端到端,需要用户决定。

四个绑定脚本的写法都参照同一个已验证过的先例 `scripts/core/experience/run_sample_deep_analyze.py`(真实数据组装→调用 Skill 的 `make_*_harness()`→写回业务库,Skill 本身不改)。

#### 真实选题流程还缺什么(2026-07-10 用户指出,不是这几天才发现的边角问题)

项目自己原有的选题方法论(选题这个环节的会话技能文档)要求:从**对标爆款/评论区/研究缺口/当下热点**四种料源出候选 → 用"选题判断维度"(从爆款数据提炼的评分标准)逐项打分 → 收拢排序取前3-5个、"宁缺毋滥" → **人工拍板选哪个**。现在 `source_to_topic` 只做到:

- ✅ 对标爆款(`hit_deep_analysis`)——已接。
- ✅ 评论区(`hit_comments` 最热3条)——2026-07-10 已接。
- ❌ 研究缺口——`research_evidence_extract` 还没接真实数据,这个料源目前完全没有。
- ❌ 当下热点——**现在完全没有采集"热点"这件事的任何机制**,需要新建一条采集链路(工作量接近再建一个采集模块,不是小改动),需要用户决定用什么数据源。
- ❌ 打分排序(判 + 选)——"选题判断维度"这个评分标准本身要从每个领域的真实爆款数据里分析提炼出来,不是能凭空编的,需要单独做一轮数据分析才能建出这份标准;标准建出来之后,还要把 `source_to_topic` 从"一条证据生成一个选题"改造成"能产出多个候选、逐个打分、排序取前几个"。
- ❌ 人工拍板——`review_queue.py` 现在能做"通过/不通过",但还不是"从多个候选里挑一个"这种真正的选题终审形态,等打分排序做出来之后需要重新设计这一步怎么呈现给人看。

**建议节奏**:这几项工作量都不小,而且②③依赖①(打分排序要先有评分标准),不建议在治理修复的同一批里赶工。下一次专门做选题流程,建议顺序:研究缺口(复用已有的 research_evidence_extract 骨架)→ 选题判断维度分析(从真实爆款数据提炼)→ 热点采集(需要用户先定数据源)→ 多候选打分排序 → 终审形态重新设计。

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
- 新增 `scripts/validation/preflight_checkpoint_check.py`(收工/交接前一条命令跑完全部纪律检查)、`scripts/validation/ops_infra_checklist.py`(硬编码路径/凭证管理/`.gitignore` 覆盖率检查)、`scripts/validation/production_data_sanity_check.py`(真实生产库健全性抽查,复现2026-07-08"备料自动触发静默失效"那类坑)。
- 新增可选安装的 git post-commit 自检钩子(`scripts/scheduled/post_commit_self_check.py` + `install_post_commit_hook.ps1`)——默认不装,装了之后每次提交自动跑一遍轻量检查,不用等下次对话才发现问题。
