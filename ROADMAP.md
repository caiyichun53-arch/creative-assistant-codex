# ROADMAP

> **这份文件的作用,和它跟别的文档的区别**:这是唯一一份"面向未来、排了先后顺序"的计划文档。仓库里其他类似文档都是回顾性质的——`GOAL-*.md`/`implementation_progress/*.md`/`PHASE_*_STATUS.yaml` 记录的是"做完了什么",`HANDOFF_STATE.md` 记录的是"现在进展到哪、上次做了什么"。这份 `ROADMAP.md` 记录的是"接下来打算按什么顺序做什么、为什么"——2026-07-09 的外部工程审计发现全仓库找不到这样一份文档,28 份 GOAL/PHASE 文件状态全部是"已完成",推进方式完全靠每次对话里临时提出新要求驱动。这份文件就是补这个洞的。
>
> **维护方法**:做完一项,把它从"进行中"挪到"已完成"并简单记一句结果(不用像 `HANDOFF_STATE.md` 那样写完整叙事,那是它的活);发现新方向,加进"未排期"分类,不要绕开这份文件直接开一个新的 `GOAL-*.md` 了事——那正是过去反应式推进的模式。这份文件里的优先级排序,凡是标了"用户决定"的都必须由用户拍板,不能自己排。

## 进行中 / 下一项

### 0. 生产激活:真实经验闭环 + Hermes 创作质量验证 —— **[阶段1-3已完成,阶段4-5待做]**

> 执行计划见 `C:\Users\15891\.claude\plans\warm-orbiting-kahn.md`(用户已批准)。目标:把"真实数据→经验提炼→人工审核+改稿变证据→经验影响下一次生成→真实 Hermes 产出质量由人评判"这条闭环打通。每阶段做完停下汇报,等用户确认再进下一阶段,不自动连续执行。

**阶段1(已完成,2026-07-11)—— 把 VersionRef 基础设施接回真实库**:
- `git mv` 把 `production/goal08_production_chain.py`(`VersionRef`)、`experience/goal09_experiments.py`(`ExperienceEvidence`/`recompute_experience_state`/tactic 生命周期)从 `archive/dead_goal_chain_20260709/` 挪回 `scripts/core/`——这是一次明确的、计划批准过的复活,不是清场回归。
- 新增 `scripts/core/persistence/install_versionref_schema_into_business_db.py`,把 `goal01/02/03_schema.sqlite.sql`(18张新表:`trace_root`/`trace_version`/`tactic_state`/`scheduler_job` 等)真实装进 `data/formal/production_activation.sqlite3`,装表前打了真实备份(`production_activation_pre_versionref_schema_20260709T184950Z.sqlite3`)。
- 真实验证:装表前后 8 张既有真实业务表(`competitor_accounts`=28/`competitor_videos`=1516/`video_checks`=1666/`baselines`=560/`hits`=431/`hit_transcripts`=431/`hit_comments`=23893/`hit_deep_analysis`=2)逐表内容哈希完全一致,新表存在且为空。13个新测试(`tests/core/test_install_versionref_schema.py`),每个检查都配了反向测试(故意造一张列结构不对的同名表验证会被拒绝、故意在 schema 里插一条会改真实业务表的语句验证会被检测到)。
- 顺手修了 `REQUIREMENT_CODE_TRACEABILITY.yaml` 里 `BR-EXPERIENCE-001` 的幽灵 `target_component: ExperienceEngine`,改成指向真实复活的 `goal09_experiments.py`。
- **对齐检查**:这一步做的是"让一个真实 formal artifact 存在",对应 `BR-EXPERIENCE-001`"经验真相源应为 formal artifact"这句话——只搭地基,`tactic_state`/`trace_root`/`trace_version` 目前全部是空表,还没有任何真实经验数据写进去,不冒充已经完成。

**已确认(用户 2026-07-11 核实)**:`hit_deep_analysis` 表里已有的 2 条记录(`model_name=xiaomi/mimo-v2.5-pro`,2026-07-08)确实是真实调用过 Hermes 的产物,不是回填的假数据——"还没花过一次钱"这句反复出现的表述是错的,已在 `HANDOFF_STATE.md` 更正。但**这次真实调用的分析结果从没被人看过**,不代表内容质量已经验证过,阶段3该先解决的"两条不同 hit 才能跑 tactic_extract"这个真实数据缺口依然存在(这 2 条记录是同一个 hit 的两次分析,不是两个不同的 hit)。

**阶段2(已完成,2026-07-11)—— 把真实证据登记进 VersionRef**:
- 新增 `scripts/core/experience/evidence_registry.py`,提供幂等函数 `register_hit_deep_analysis_evidence(conn, analysis_id)`,复用 `goal01_store.content_hash`,给一条真实 `hit_deep_analysis` 行(`topic_pattern`/`hook_pattern`/`structure_pattern` 三字段)登记一个 `trace_root`(`object_kind='hit_deep_analysis_evidence'`)+ `trace_version` + 一条指回真实来源的 `object_reference`(`target_object_kind='hit_deep_analysis'`,`target_stable_id=analysis_id`)。
- 真实对库里唯一存在的真实记录 `analysis_id='hit_37ae1202dd597fcc3039_v2'` 登记一次(装表前先打真实备份 `production_activation_pre_evidence_registration_20260709T220416Z.sqlite3`):产出的 `trace_version.content_hash` 与 `object_reference.target_content_hash` 都等于独立用 `content_hash()` 对同一份三字段 payload 重新算出来的哈希(`a316fc5d...`),`object_reference.target_stable_id` 真实等于 `hit_37ae1202dd597fcc3039_v2`。原地重复调用一次,返回值完全相同(`replayed=true`,同一个 root_id/version_id/reference_id),`trace_version`/`object_reference` 最终各只有 1 行——不是接口层面"看起来幂等",是真的没有插入第二行。装表前后 26 张既有真实业务表逐表内容哈希核对完全一致(`hits`=431/`hit_deep_analysis`=2/`competitor_accounts`=28/`hit_comments`=23893 等一个字节都没变)。
- 5个新测试(`tests/core/test_evidence_registry.py`):正向(真实哈希核对、root/version/reference 字段核对、两条不同 analysis 各自独立建 root)+ 反向(不存在的 analysis_id 显式抛 `EvidenceRegistrationError` 且不留任何孤儿 trace_root/trace_version/object_reference 行、空库同样拒绝)。
- **对齐检查**:这一步让这条真实分析结果第一次"有资格"被 `tactic_state.basis_version_id` 引用,还没有真的被引用——阶段3 才会真的产生引用它的 `tactic_state` 行。

**阶段3 数据缺口已解决(2026-07-11)——真实分析了20条新hit**:用户指定"爆款库里偏离值最高的前20篇"。库里没有现成的"偏离值"字段,经用户确认,口径定为"hit_channel 里记录的最高基线倍数"(如 `share_anomaly:422.75x` 取422.75)——新增 `deviation_value_from_hit_channel()`/`select_hits_pending_analysis_by_deviation()`(`run_sample_deep_analyze.py`,11个新测试,正向排序+反向排除comment_like_ratio/p90类无倍数hit)。

真实执行:补全 `.env` 里 `HERMES_BUSINESS_MODEL_TOKEN/BASE_URL/NAME/CLASS`(用户确认 `.env.live-gates` 里的凭证就是真实凭证,非测试专用)。对偏离值最高的20个真实hit(样例:`hit_d4360a5e74cf44ecd6ba` 偏离9441倍、`hit_498df4588ad2b75efe97` 偏离3068倍等,内容涵盖科普/健康/社会话题)真实调用 Hermes(`xiaomi/mimo-v2.5-pro`)跑 `sample_deep_analyze`——20次真实调用中5次首次失败(`FormalSkillValidationError: model output is not JSON`,job/scheduler bookkeeping本身是每次harness独立的内存态,不落盘,重跑时才捕获到真实报错原因),重试后全部20条成功,确认是模型偶发输出格式问题,非凭证/配置问题。库里 `hit_deep_analysis` 从2行增至22行(21个不同hit,含最初那条重复分析的hit)。全部22条(含此前2条)已用 `evidence_registry.py` 登记进 VersionRef(21条新登记+1条此前已登记的原样跳过,`trace_root`/`trace_version`/`object_reference` 现在各22行)。

**阶段3 完成(2026-07-11)——`tactic_extract` 第一次真实成功**:`dna_note_refs_max` 用户拍板从12上调到20,不用分批。新增地基三件套:`goal02_store.py`(通用 create_state/transition_state,这次只给tactic接真实调用)、`tactic_registry.py`(把归纳结果登记成真实 `trace_root`+`trace_version`+`tactic_state` 行)、`run_tactic_extract.py`(绑定层)。真实调用共7次,前6次失败(依次:超出当时12条上限→归纳出60条词汇堆砌+0范例,查出全部12个业务Skill里5个提示词系统性写得很糙,按优先级全部修过一轮→`schema_version`被埋在提示词中间导致AI遗漏→证据选择没过滤"只取最新版本",优先选到旧版低质量证据→`example_candidates`格式AI给了JSON对象不是纯字符串)。**用户要求先把全部21个真实hit重新拆解一遍**(用修好的`sample_deep_analyze`提示词,21/21全部成功,人工逐条对比质量,语言问题清零、"选题手法"从复述具体事实变成真正可复用手法),重新登记进VersionRef证据库后,第7次调用**成功**:真实产出6条互不相同、各引用2个真实视频为证的"共同规律"和6条范例候选,存进`tactic_state`(`state='candidate'`)。

**下一步(需要用户决定,不在本轮范围)**:`candidate`状态之后怎么变成`active`(`transition_state()`已就绪但没有调用方);`content_plan`/`script_generate`/`script_review`的提示词已修但从没真实调用验证过。

**2026-07-13 更新,避免和下面新批次混淆**:`candidate`之后怎么变成`active`这件事,在另一个不同的规划(`splendid-petting-rain.md`,见下方"已完成(2026-07-13...)"批次的 B1)里,针对**自营账号发布实验**这条闭环,已经把"评估一次实验→推动 active/watch/paused 状态机"的真实逻辑接通了——但这跟**本节阶段4"人工改稿变证据"**是两件不同的事(一个是"发布数据证明这条方法有效/无效",一个是"人工编辑稿子这件事本身要留痕变成证据"),阶段4-5 依然完全没开始,不要因为下面有新批次就以为这里也做完了。

**阶段4-5(未开始)**:人工改稿变证据(升级 `review_queue.py`)→ 真实 Hermes 创作质量验证。完整验收标准见计划文件。

### 1. 接通"选题→大纲→成稿"技术链路 —— **[技术环节已打通,业务流程还不完整]**

> **2026-07-10 用户纠偏**:2026-07-09 汇报"链路打通"时说法过头了——验证的只是"数据格式对得上、四个环节能串起来跑",不是"选题这件事做对了"。用户当场指出两个真实缺口:①三个环节之间完全没有人工审核,内容会自动一路流到成稿;②选题只用了"对标爆款"一种料源,当前设计要求的候选来源(对标爆款/评论区/研究缺口/当下热点)被跳过了大半。①已经在 2026-07-10 修复,②只补了四分之一(评论区),其余留在下面单独列出,不装作已经做完。
> **2026-07-11 用户再次纠偏**:上一版把"打分排序/选题判断维度"写成了选题环节的缺口——这是错误框定。**当前设计没有冻结"选题打分制"**,不接受把旧系统的评分维度/打分表/score-rank机制当作缺口补回。见下方"真实选题流程还缺什么"的更正版本。

`runtime_skills/` 下 12 个业务 Skill,这条主链路**2026-07-13 起是五环**(原来三环,F4/F5 新增文案优化/审核、最终稿两环)都已接上真实数据,且有一条端到端集成测试(`tests/core/test_topic_to_script_chain_integration.py`)证明"一条爆款分析结果"真的能一路流转成"一份最终稿",外键全程可追溯(`hit_deep_analysis → topic_candidates → content_plans → script_drafts → script_reviews → final_drafts`),**且中途卡在人工审核闸门上,不会没人看就自动流完**：

1. **`source_to_topic`**:从证据/来源转成候选选题。`scripts/core/experience/run_source_to_topic.py`——吃 `hit_deep_analysis`(sample_deep_analyze 的真实输出:选题/开头/结构手法)+ `hit_comments`(2026-07-10 新增:同一条爆款下面最热的3条真实评论)当证据,生成候选选题,写入新表 `topic_candidates`(带版本号)。18个测试。**明确的简化**:`relation_summary`(这条选题和现有内容是否重复/冲突)现在是老实的占位文字,不是真判断过——`content_relation_judge` 还没接,不冒充。
2. **`content_plan`**:选题→钩子+大纲。`scripts/core/experience/run_content_plan.py`——吃 `topic_candidates` 里 `topic_status='generated'` **且人工已审核通过**的候选选题,写入新表 `content_plans`(带版本号)。16个测试。**明确的简化**:`style_examples`(应来自范例库,物理载体还没定)复用同一条视频的真实转写文字稿摘句,真实数据、老实标注了替代关系,不是编造。`tactic_candidates` 这一项**2026-07-13(B2)已经接上真数据**:优先查这条选题所在领域真实处于 `active`/`watch` 的方法(见下方"已完成(2026-07-13...)"批次),只有该领域没有任何方法离开过候选状态时才退回旧的per-hit替代。**2026-07-11 更正**:曾经写过"这两个字段不影响实际调模型的两次调用"——那是一个真实bug(`tactic_candidates`/`evidence_items` 契约要求必传的字段没发给模型),已经修复,现在**会**真实影响输出。
3. **`script_generate`**:大纲+brief→成稿草稿。`scripts/core/experience/run_script_generate.py`——吃 `content_plans` 里**人工已审核通过**、还没生成过草稿的规划,写入新表 `script_drafts`(带版本号)。12个测试。**明确的简化**:`research_summary`(应由 `research_evidence_extract`/`production_research_plan` 产出,都还没接)现在是"汇总已有证据,不是真研究"的老实标注文字。
4. **`script_review`**(2026-07-13 新增,F4):成稿→文案优化+审核+AI味判定。`scripts/core/experience/run_script_review.py`——内部三个子节点,先 `creation_polish` 文案优化、再 `creation_review` 审核、最后 `ai_flavor_judge` 判AI味(**2026-07-13 之前顺序是反的**,已按置顶规则总表核对结果改成先优化后审核),写入新表 `script_reviews`。
5. **`final_draft`**(2026-07-13 新增,F5):审核通过的润色稿→复制成一条独立"最终稿",等待**单独的**人工确认(审核通过≠最终稿通过)。`scripts/core/experience/run_final_draft.py`,写入新表 `final_drafts`。不调用大模型,纯拷贝。

**人工审核闸门**(2026-07-10 新增,2026-07-13 从三段扩展到五段,`scripts/core/experience/review_queue.py`):`topic_candidates`/`content_plans`/`script_drafts`/`script_reviews`/`final_drafts` 五张表各带一个 `human_review_status` 字段,默认"待审核",下一环的查询都要求上一环已经明确标记"通过"才会处理。`python -m scripts.core.experience.review_queue --list` 看有哪些在等审核(可跟 `topic`/`plan`/`draft`/`review`/`final` 只看某一段),`--approve`/`--reject` 标记。**现在只有命令行,没有界面**——先把闸门本身做对,界面是以后的事。

**共同的、还没解决的缺口**:`sample_deep_analyze`/`tactic_extract` 真调用过 Hermes(`sample_deep_analyze` 2026-07-08 起步、2026-07-11 真实21条视频全部成功;`tactic_extract` 2026-07-11 第7次真实成功);`source_to_topic`/`content_plan`/`script_generate`/`script_review` 这四个绑定**仍然一次都没被真实调用过**(2026-07-13 更新:对应表在真实库里已经存在,只是从没被真实模型写过一行——之前"表都不存在"的说法已经过时)。测试套件全部用各 Skill 自带的确定性假模型端口,这个没变。真正调真实模型需要的 `HERMES_BUSINESS_BASE_URL`/`HERMES_BUSINESS_MODEL_NAME` 当前不在真实 `.env` 里(只有 `HERMES_BUSINESS_API_KEY`)。要不要补全这两个值、真的花一次钱验证端到端,需要用户决定。

六个绑定脚本的写法都参照同一个已验证过的先例 `scripts/core/experience/run_sample_deep_analyze.py`(真实数据组装→调用 Skill 的 `make_*_harness()`→写回业务库,Skill 本身不改)。

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
- **完整业务视图仍未建**:当前 `scripts/core/business_data/` 只是账号注册/首采/基线判定这一个切片,不是完整业务层(旧文档里的"观察池/候选池"设计已被 BR-HIT-001 取代,不是缺口,是设计变了)。2026-07-13 批次补上了自营账号(A2)、领域话题标签搜索(A3)、账号发现复查(A5)、热点人工登记(A6)几个新切片,仍然不是完整业务层。
- **飞书实时集成**:上一次 legacy removal 整体删除,还没重建,重建前需要用户确认是否真的现在需要。2026-07-13(A6)再次确认"先不做,单独立项"。
- **真实付费模型调用的凭据缺口**:`sample_deep_analyze`/`tactic_extract` 已经真花过钱、真实调用成功过;`source_to_topic`/`content_plan`/`script_generate`/`script_review` 仍然全部用确定性假 Provider 测试,真正的 `HERMES_BUSINESS_MODEL_TOKEN` 等只存在于 `.env.live-gates`,没进真实 `.env`,这四步还没真花过一次钱验证端到端。
- **GPT 供应商切换**(`BUSINESS_MODEL_SWITCH_TO_GPT_PLAN.md`):未开始,不是当前阻塞项,是未来用户主动触发的手动激活项。
- **候选(candidate)方法→正式生效(active)的晋升入口**(2026-07-13 新发现):`goal02_store.py` 的 `transition_state()` 现在已经有真实调用方了(`run_publication_experiment.py`),但只覆盖"已经离开 candidate 之后"的 `active`/`watch`/`paused` 流转——从 candidate 第一次晋升到 active 这一步,生产环境下该由谁在什么条件下触发,还没设计,目前只在测试里手工调用过。
- **领域搜索发现的外部视频→正式选题(`topic_candidates`)的路径**(2026-07-13 新发现,A3 遗留):`discovered_external_videos` 表已经建好,但 `topic_candidates.source_analysis_id` 是必填外键、指向 `hit_deep_analysis`,而搜索发现的视频压根没走过"深度分析"这一步,这条路径需要专门设计。
- **账号发现复查的四类语义判断 + 真实10条视频重新抓取**(2026-07-13 新发现,A5 遗留):原文档要求复查时拉账号最近10条可访问视频,统计"领域相关/口播适配/独立选题/排除内容"四类数量——这次(A5)只用了已经发现、已经落库的真实视频数量做统计,没做真的重新抓取,也没设计这四类判断需要的规则。
- **"研究"环节接真实数据 + 独立人工确认闸门**(2026-07-13 新发现):置顶规则总表要求的六个可暂停节点(研究/内容计划/初稿/文案优化/审核/最终稿),F5 补齐了后面五个,但"研究"本身(`research_evidence_extract`/`production_research_plan`)仍然没有真实绑定脚本,`script_generate` 的"研究摘要"字段现在是老实标注过的占位替代——没有真实产出,自然也没有东西可以让人审核。

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

## 已完成(2026-07-13,置顶规则总表核对后 —— 生产激活 Part1+Part2)

执行计划见 `C:\Users\15891\.claude\plans\splendid-petting-rain.md`(用户已批准,两部分)。核对依据是 V0.6.3 文档卷首新增的《置顶规则总表》,用只读子agent逐条核对了60条置顶规则,产出"文档有系统无/两边对不上/系统有文档无"三类问题,用户逐条给出处理决定。

**Part1(F1-F8,目录整改)**:
- F1:移除候选池打分机制(`half_life_days`/`reheat_boost`/`expire_heat`),不恢复score/rank/weight排序体系,`BR-TOPIC-002` 改写为不打分表述。
- F2:评论采集按 `purpose`(`early_topic`/`mature_analysis`/`mature_history`/`external_snapshot`)分阶段,`hit_comments` 主键从 `(hit_id, comment_id)` 扩到 `(hit_id, comment_id, purpose)`,真实23,893行数据迁移前先在副本上验证过。
- F3:`BR-RESEARCH-001` 加音乐娱乐领域评论例外(网易云/豆瓣等可作正式研究证据,其他领域仍一刀切禁止)。
- F4:文案优化(`creation_polish`)顺序改到审核(`creation_review`)之前,匹配"初稿→优化→审核"的真实设计。
- F5:六个可暂停节点里补齐后五个(内容计划/初稿/文案优化/审核/最终稿)的人工确认闸门,`review_queue.py` 从三段扩展到五段;新增"最终稿"(`final_drafts`)独立概念,审核通过≠最终稿通过。
- F6:新增 `scripts/validation/production_activation_gate.py`,综合检查管线完整性/人工闸门/顺序/无自动发布/无选题打分配置等9项,故意造错的 fixture 验证过闸门真的会拦。
- F7:新增第二正式领域(`config/domains/音乐娱乐.yaml`,`activation_status: pending_business_input`,等用户提供真实账号信息)。
- F8:新增 `BR-COLLECT-008`,把"网易云音乐必须独立于 MediaCrawler、单独封装"这个已经在代码里做对的决定正式写进 catalog。

**Part2(A1-A6/B1-B2/C,选题来源扩展 + 经验闭环地基)**——这次是基于本机真实找到的原总控文档(V0.6.2/V0.6.3)逐条核对后重新设计的,替换了前几版"没查到文档、自己推"的方案:
- A1:`ALLOWED_DOMAIN_LABELS` 从5处重复定义收敛到 `domain_labels.py` 一处。
- A2:`self_owned` 账号类型(原文档"7 账号类型"里本来就有的正式设计,不是另起一套)——`own_accounts`/`own_publications`/`own_publication_checks`/`own_publication_baselines`/`own_experiments` 五张表,P基线启用门槛/滚动窗口都是20条(原文档"29.1"真实数字)。
- A3:领域话题标签库——初始"活跃标签"来自每个领域的 `sources.yaml`(人工配置),爆款库冒出的新标签只进"建议"状态,人工确认才转正;7天轮换搜索、每次最多前20个未处理结果、最多5条进ASR/低成本判断、同标签连续3周期无产出自动暂停、每领域每天最多10条外部视频(全部是原文档"21"节的真实数字)。
- A4:`local_mediacrawler_executor.py` 新增 `source_kind="search"` 分支,对应工具本来就有的 `--type search --keywords` 关键词搜索模式(之前只接了按账号抓)。
- A5:账号发现复查——同一非对标账号30天内至少3条不同视频通过领域筛选才建复查任务,人工三选一"加入/忽略30天/永久忽略"(原文档"7.1"真实规则)。
- A6:热点转化人工登记 MVP——原文档自己说自动热点数据源是未完成节点,这次只做命令行登记入口 + 确定性关键词匹配,不做飞书真接入、不做TrendRadar自动抓取。
- B1:第一次真正调用了写好但全仓库从没人用过的 `goal09_experiments.py`——`compute_metric_signal()` 三态判断(≥1.5支持/≤0.8不支持/中间存疑)、`recompute_experience_state()` 的 `active`/`watch`/`paused`/`deprecated` 状态机。**顺手修了一个真实bug**:`tactic_state` 表(c3f8e70那次提交建的)缺 `watch` 状态,而且触发器错误地写成"只能前进、不能后退"——但真实设计是 active/watch/paused 三态可以自由来回流转,只有离开candidate和进入deprecated才是真正单向。
- B2:`content_plan` 的"打法候选"字段改成优先查真实 `active`/`watch` 状态的方法,只有该领域没有任何方法离开过candidate时才退回旧的per-hit替代。
- C:端到端闭环验证——用一条真实注册的 tactic(走真实注册路径,不是测试简化写法)+ 构造数据,把 A2→B1→B2 真正串成一个完整循环(active→watch→active→paused→paused→active),验证 §7.2-7.7 的具体规则真的按预期走。

**现实约束(不冒充已完成)**:B1/B2/C 全部只用构造数据验证过,没有任何自营账号真的发布过内容;A3的真实关键词搜索默认关闭(`live_enabled: false`),没真的花钱搜过;A5没做真的10条视频重新抓取。717→716个测试(见下一批次净减1),`production_activation_gate`/`preflight_checkpoint_check` 全程保持绿色。

## 已完成(2026-07-13,字符/token 上限彻底取消)

不属于任何规划文件,是用户在核实系统真实状态过程中当场做的决定,不是预先排期的工作:用户问"提示词和skill有没有限制token造成输入输出截断",核查后发现声明的token预算只是"存在性检查",从没真的数过要发的内容有多少token;而且已经发生过一次真实事故(2026-07-09,一处2200字的硬截断让模型对着被腰斩的转写编造了从没出现过的结尾)。用户在"截断后报错"和"彻底不设上限"两个选项里选了后者(不是折中方案)。

移除范围:全部12份 `*_BUSINESS_CONTRACT.yaml` 契约里的 `context_budget`/`token_budget`/`input_length_limits` + 对应的 `maxLength` 校验;`BUSINESS_MODEL_ROUTE_REGISTRY.yaml` 的15个 `token_context_budget`;`formal_skill_adapter.py`/`business_route_registry.py` 的运行时校验;6个真实绑定脚本(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`/`script_review`/`tactic_extract`)里全部截断常量和 `max_completion_tokens` 参数——包括 `tactic_extract` 那道"超过2500字就报错"的边界也一并取消,不是只取消静默截断。**用户明确接受的风险**:真实调用现在可能因为内容过长被 Hermes 自己拒绝、或者让单次成本变高,系统不会再提前拦截或警告。详见 `TECHNICAL_MANUAL.md`"字符/token 上限已彻底取消"小节。716个测试全绿。
