# ROADMAP

> **这份文件的作用,和它跟别的文档的区别**:这是唯一一份"面向未来、排了先后顺序"的计划文档。仓库里其他类似文档都是回顾性质的——`GOAL-*.md`/`implementation_progress/*.md`/`PHASE_*_STATUS.yaml` 记录的是"做完了什么",`HANDOFF_STATE.md` 记录的是"现在进展到哪、上次做了什么"。这份 `ROADMAP.md` 记录的是"接下来打算按什么顺序做什么、为什么"——2026-07-09 的外部工程审计发现全仓库找不到这样一份文档,28 份 GOAL/PHASE 文件状态全部是"已完成",推进方式完全靠每次对话里临时提出新要求驱动。这份文件就是补这个洞的。
>
> **维护方法**:做完一项,把它从"进行中"挪到"已完成"并简单记一句结果(不用像 `HANDOFF_STATE.md` 那样写完整叙事,那是它的活);发现新方向,加进"未排期"分类,不要绕开这份文件直接开一个新的 `GOAL-*.md` 了事——那正是过去反应式推进的模式。这份文件里的优先级排序,凡是标了"用户决定"的都必须由用户拍板,不能自己排。

## 进行中 / 下一项

### 0. 生产激活:真实经验闭环 + Hermes 创作质量验证 —— **[阶段1已完成,阶段2-5待做]**

> 执行计划见 `C:\Users\15891\.claude\plans\warm-orbiting-kahn.md`(用户已批准)。目标:把"真实数据→经验提炼→人工审核+改稿变证据→经验影响下一次生成→真实 Hermes 产出质量由人评判"这条闭环打通。每阶段做完停下汇报,等用户确认再进下一阶段,不自动连续执行。

**阶段1(已完成,2026-07-11)—— 把 VersionRef 基础设施接回真实库**:
- `git mv` 把 `production/goal08_production_chain.py`(`VersionRef`)、`experience/goal09_experiments.py`(`ExperienceEvidence`/`recompute_experience_state`/tactic 生命周期)从 `archive/dead_goal_chain_20260709/` 挪回 `scripts/core/`——这是一次明确的、计划批准过的复活,不是清场回归。
- 新增 `scripts/core/persistence/install_versionref_schema_into_business_db.py`,把 `goal01/02/03_schema.sqlite.sql`(18张新表:`trace_root`/`trace_version`/`tactic_state`/`scheduler_job` 等)真实装进 `data/formal/production_activation.sqlite3`,装表前打了真实备份(`production_activation_pre_versionref_schema_20260709T184950Z.sqlite3`)。
- 真实验证:装表前后 8 张既有真实业务表(`competitor_accounts`=28/`competitor_videos`=1516/`video_checks`=1666/`baselines`=560/`hits`=431/`hit_transcripts`=431/`hit_comments`=23893/`hit_deep_analysis`=2)逐表内容哈希完全一致,新表存在且为空。13个新测试(`tests/core/test_install_versionref_schema.py`),每个检查都配了反向测试(故意造一张列结构不对的同名表验证会被拒绝、故意在 schema 里插一条会改真实业务表的语句验证会被检测到)。
- 顺手修了 `REQUIREMENT_CODE_TRACEABILITY.yaml` 里 `BR-EXPERIENCE-001` 的幽灵 `target_component: ExperienceEngine`,改成指向真实复活的 `goal09_experiments.py`。
- **对齐检查**:这一步做的是"让一个真实 formal artifact 存在",对应 `BR-EXPERIENCE-001`"经验真相源应为 formal artifact"这句话——只搭地基,`tactic_state`/`trace_root`/`trace_version` 目前全部是空表,还没有任何真实经验数据写进去,不冒充已经完成。

**已知发现,尚待你确认**(阻塞阶段3才需要答案,不阻塞已完成的阶段1):`hit_deep_analysis` 表里已有的 2 条记录(`model_name=xiaomi/mimo-v2.5-pro`,2026-07-08)看起来像是真实调用过 Hermes 的产物,但跟"还没花过一次钱"这句反复出现的表述矛盾——需要你确认这是①之前 `.env` 配置更全时的真实调用(说明这句表述本身错了,需要改),还是②别的方式回填的非真实数据(说明生产库里混进过来源不明的数据)。

**阶段2-5(未开始,待你确认阶段1结果后再继续)**:登记真实证据进 VersionRef → 绑定 `tactic_extract` → 人工改稿变证据(升级 `review_queue.py`)→ 真实 Hermes 创作质量验证。完整验收标准见计划文件。

### 1. 接通"选题→大纲→成稿"技术链路 —— **[技术环节已打通,业务流程还不完整]**

> **2026-07-10 用户纠偏**:2026-07-09 汇报"链路打通"时说法过头了——验证的只是"数据格式对得上、四个环节能串起来跑",不是"选题这件事做对了"。用户当场指出两个真实缺口:①三个环节之间完全没有人工审核,内容会自动一路流到成稿;②选题只用了"对标爆款"一种料源,当前设计要求的候选来源(对标爆款/评论区/研究缺口/当下热点)被跳过了大半。①已经在 2026-07-10 修复,②只补了四分之一(评论区),其余留在下面单独列出,不装作已经做完。
> **2026-07-11 用户再次纠偏**:上一版把"打分排序/选题判断维度"写成了选题环节的缺口——这是错误框定。**当前设计没有冻结"选题打分制"**,不接受把旧系统的评分维度/打分表/score-rank机制当作缺口补回。见下方"真实选题流程还缺什么"的更正版本。

`runtime_skills/` 下 12 个业务 Skill,这条主链路的三环都已接上真实数据,且有一条端到端集成测试(`tests/core/test_topic_to_script_chain_integration.py`)证明"一条爆款分析结果"真的能一路流转成"一份成稿草稿",外键全程可追溯(`hit_deep_analysis → topic_candidates → content_plans → script_drafts`),**且中途卡在人工审核闸门上,不会没人看就自动流完**：

1. **`source_to_topic`**:从证据/来源转成候选选题。`scripts/core/experience/run_source_to_topic.py`——吃 `hit_deep_analysis`(sample_deep_analyze 的真实输出:选题/开头/结构手法)+ `hit_comments`(2026-07-10 新增:同一条爆款下面最热的3条真实评论)当证据,生成候选选题,写入新表 `topic_candidates`(带版本号)。18个测试。**明确的简化**:`relation_summary`(这条选题和现有内容是否重复/冲突)现在是老实的占位文字,不是真判断过——`content_relation_judge` 还没接,不冒充。
2. **`content_plan`**:选题→钩子+大纲。`scripts/core/experience/run_content_plan.py`——吃 `topic_candidates` 里 `topic_status='generated'` **且人工已审核通过**的候选选题,写入新表 `content_plans`(带版本号)。16个测试。**明确的简化**:`tactic_candidates`(应由 `tactic_extract` 产出,还没接)复用同一条 `hit_deep_analysis` 的选题/开头/结构手法;`style_examples`(应来自范例库,物理载体还没定)复用同一条视频的真实转写文字稿摘句——都是真实数据、老实标注了替代关系,不是编造。已核实这两个字段目前不影响 `_run_content_plan()` 实际调模型的两次调用(只有 `brief`/`style_examples` 真正进了 prompt),风险可控。
3. **`script_generate`**:大纲+brief→成稿草稿。`scripts/core/experience/run_script_generate.py`——吃 `content_plans` 里**人工已审核通过**、还没生成过草稿的规划,写入新表 `script_drafts`(带版本号)。12个测试。**明确的简化**:`research_summary`(应由 `research_evidence_extract`/`production_research_plan` 产出,都还没接)现在是"汇总已有证据,不是真研究"的老实标注文字。

**人工审核闸门**(2026-07-10 新增,`scripts/core/experience/review_queue.py`):`topic_candidates`/`content_plans`/`script_drafts` 三张表各带一个 `human_review_status` 字段,默认"待审核",第2/3步的查询都要求上一环已经明确标记"通过"才会处理。`python -m scripts.core.experience.review_queue --list` 看有哪些在等审核,`--approve`/`--reject` 标记。**现在只有命令行,没有界面**——先把闸门本身做对,界面是以后的事。8个测试。

**共同的、还没解决的缺口(四个绑定都一样)**:还没花钱调用过一次真实大模型——测试全部用各 Skill 自带的确定性假模型端口,真正调真实模型需要的密钥(`HERMES_BUSINESS_MODEL_TOKEN` 等)只存在于 `.env.live-gates`(专门给一次性受限验证用),没进真实 `.env`。要不要把这几个值搬进真实 `.env`、真的花一次钱验证端到端,需要用户决定。

四个绑定脚本的写法都参照同一个已验证过的先例 `scripts/core/experience/run_sample_deep_analyze.py`(真实数据组装→调用 Skill 的 `make_*_harness()`→写回业务库,Skill 本身不改)。

#### 真实选题流程还缺什么(2026-07-10 用户指出并逐条核实,2026-07-11 更正框定)

**2026-07-11 更正**:此前把这一节写成"要重建选题判断维度这把评分尺子"是错误框定——当前设计没有冻结"选题打分制",不接受把旧系统的评分维度/打分表/score-rank机制当作缺口补回。**当前真正的缺口不是打分维度,而是:当前设计中的选题判断规则、领域约束、去重/冷却、候选来源、人工确认门槛,是否已经被真实代码落地并验证。**(2026-07-11 第二次更正:`BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml` 里 `BR-TOPIC-001` 已从"每日选题排序"打分/排序口径改写为这段口径,幽灵组件 `TopicPlanningService` 已替换为真实组件引用+`pending_implementation`标注。) 现在 `source_to_topic` 的真实状态:

- ✅ 对标爆款(`hit_deep_analysis`)——已接,候选来源之一。
- ⚠️ 评论区(`hit_comments` 最热3条)——2026-07-10 已接,但**"3条"这个数字是编的,没有任何文档依据**(已在代码里显式标注 `UNSOURCED`,见下方"评论数量待定"),需要用户确认合理值。
- ❌ 研究缺口——`research_evidence_extract` 还没接真实数据,这个候选来源目前完全没有。
- ❌ 当下热点——**2026-07-10 已核实:旧系统(`I:\Creation_assistant`,未清洗的完整版本)里翻遍 `scripts/collect`/`scripts/topics`/`scripts/research`/`scripts/reverse`/`vendor/MediaCrawler`/`tools/`,没有找到任何专门做"热点采集"的代码或开源项目引用**。"热点"在旧系统里只是一个内容分类标签(如"热点解读"),不是数据源;"研究"这一步靠的是通用网页搜索,不是热点榜单抓取。这个仓库的 git 历史起点是一次"清洗后上传",清洗之前如果确实用过某个开源项目,现在已经无从查起,需要用户直接指出名字。
- ❌ 选题判断规则/领域约束——`source_to_topic` 目前没有任何"这条候选是否符合本领域约束、是否值得立项"的判断规则,不管是评分式还是非评分式的规则都还没设计、没落地。**旧系统留下的"选题判断维度.md"评分标准不是这个缺口的答案**——全仓库/全vault路径搜索确认该文件不存在,产出它的技能(`.claude/skills/归纳`)已删除,**不予恢复,也不用别的方式重建同类评分机制**。这个规则需要重新设计,形态由用户决定(不预设是评分制)。
- ❌ 去重/冷却——候选选题之间(以及候选与已有内容之间)目前没有去重或冷却机制,`relation_summary` 字段现在是老实的占位文字,真判断要等 `content_relation_judge` 接入。
- ❌ 人工确认门槛——`review_queue.py` 现在只有"通过/不通过"二选一,而且**没有对接项目里已经存在的、更合适的"修正+留痕"框架**(`scripts/core/correction/goal10_corrections.py`,已归档,见下方"人工审核")。

**建议节奏**:这几项工作量都不小,不建议在治理修复的同一批里赶工。下一次专门做选题流程,建议顺序:研究缺口(复用已有的 research_evidence_extract 骨架)→ 热点采集(需要用户先定数据源/工具)→ 选题判断规则与领域约束(需要用户先定形态,不预设评分制)→ 去重/冷却机制 → 人工确认门槛形态重新设计(见下方 goal10_corrections 集成问题)。

#### 评论数量待定(2026-07-10 新发现)

`run_source_to_topic.py` 里 `TOP_COMMENTS_PER_HIT = 3` 已显式标注 `UNSOURCED`——这个数字没有任何文档依据,是随手定的。需要用户决定一个合理值(是否应该随该条爆款评论总数浮动,而不是固定数字)。

#### 人工审核该不该接 `goal10_corrections`(2026-07-10 新发现,2026-07-11 清场后状态变化)

`review_queue.py` 现在的"通过/不通过"是简化版。代码库里曾经有一套专门做"修正登记 + 影响传播 + 留痕"的框架(`scripts/core/correction/goal10_corrections.py`,1402行 + 独立验证脚本728行),概念上更贴近"审核不只是通过/不通过,还可能涉及编辑、要记下怎么改的、为什么改"这个真实需求。但它绑定在另一套完全独立的持久化系统(`PersistenceStore`/`VersionRef`,即 Goal01-12 那条"正式生产链路")上,而 `topic_candidates`/`content_plans`/`script_drafts` 这些新表用的是普通 SQLite 表,两套机制从来没有打通过。

**2026-07-11 更新**:经全仓库真实 import 传递闭包核实(不是凭文件名),`correction/`(含 `goal10_corrections.py`)、`production/`、`host/`、`hermes/`、`state/` 整个目录 + `workflow/goal_phase5_business_workflow.py` 确认零真实生产入口依赖,已按用户指令的"清场式保留重构"归档到 `archive/dead_goal_chain_20260709/`(详见该目录的 `README.md`)。这不是删除这个需求本身——如果未来确实需要"编辑+留痕"这个能力,是一次新的、独立的架构决策(要么给轻量方案单独加字段,要么重新设计一套接得上 `topic_candidates` 等真实表的修正框架),不是简单地把归档代码接回来。`persistence`/`scheduler`/`workflow/goal05_workflow.py`/`research/goal06_formal_research.py` 反而是这次闭包计算证实的、被真实生产代码(`model_gateway`/`external_adapters`)传递依赖的模块,保留在原地——这四个模块名字虽然带着旧的 `goal0X` 编号,但不能凭名字判断该不该归档。

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

## 已完成(2026-07-11,清场式保留重构批次)

同样不是业务功能,是一次基于《实现可信度审计报告》的清理动作,详细过程见 `HANDOFF_STATE.md`:

- 归档 `scripts/core/{correction,production,host,hermes,state}` 整个目录 + `workflow/goal_phase5_business_workflow.py` 到 `archive/dead_goal_chain_20260709/`(经真实 import 传递闭包核实零生产依赖,不是凭文件名)。
- `config/model_routes.yaml` 新增显性的 `model_positions:`(`dialogue_model`/`business_model`/`writing_model`),移除混入的 `engineering_execution` 路由;两个 example 配置同步,`multi_provider` 示例不再把 Codex CLI 列为业务模型候选。
- `.env.example` 移除死配置(`CREATION_LLM_*`),换成真正生效的 `HERMES_BUSINESS_*` 键。
- `AGENTS.md`(CLAUDE.md 机械生成)修正"开发用 Codex...创作走 Codex 订阅"等把开发工具和运行期 provider 混为一谈的表述。
- `REQUIREMENT_CODE_TRACEABILITY.yaml` 里 `ContentWorkflow/ContentGuard` 幽灵引用(BR-CONTENT-001/002/003)改为老实标注 `UNIMPLEMENTED`。

## 已完成(2026-07-11 第二轮,清场收尾)

第一轮报告里点名"未处理"的两个遗留问题,本轮补上:

- 重新基于当轮代码状态跑真实 import 传递闭包,确认 `scripts/core/runtime/`(`goal04_runtime_host.py`/`goal_runtime_vertical_slice.py` 等 12 个文件)同样零真实生产入口依赖,归档到 `archive/dead_goal_chain_20260709/`。归档时发现并修了一处配置层面的真实断链:`config/settings.{yaml,example.yaml}` 的 `sqlite_schema_chain`/`postgres_schema_chain` 曾引用已归档模块自带的 schema 文件,已移除对应条目。
- `tests/core/test_external_executor_adapters.py` 里混着 2 个测 `RuntimeHost`(依赖已归档模块)和 6 个测真实 `external_adapters` 类的用例——没有整个文件一起归档(会连带丢真实覆盖),只移除了那 2 个,6 个真实测试保留在原文件。
- 新增 `tests/core/test_goal05_workflow_orchestrator.py`(13个测试),补回第一轮归档 `test_phase5_business_workflow.py` 时连带丢掉的 `Goal05WorkflowOrchestrator` 覆盖——真实 `Goal03Scheduler.in_memory()`,不 mock 核心逻辑,覆盖初始化/合法输入下真实入队/幂等重放/workflow_id 确定性/7种非法输入显式失败/不产生部分入队。
- `dead_goal_chain_gate.py` 新增 3 项检查:`scripts.core.runtime` 加入死链前缀清单;新增基于路径而非清单的 `archive_unreachable_from_real_entrypoints` 检查(不依赖手工维护的前缀清单,直接判断真实闭包里是否有路径落在 `archive/` 下);新增 `goal05_workflow_orchestrator_has_independent_test_coverage`(机械验证 `test_goal05_workflow_orchestrator.py` 存在且真的 import 了 `Goal05WorkflowOrchestrator`)。
