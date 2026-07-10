# HANDOFF_STATE(现状快照,不是历史记录)

> **活文档**:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。**2026-07-09 改版**:这份文件只保留"现在是什么状态"的结构化快照——详细的过程记录、事故复盘、真实数据验证推理链路搬到 `CHANGELOG.md`(只增不改的历史归档)。新会话开工只需要读这份文件(几分钟能读完),不需要考古 `CHANGELOG.md`;需要查某个具体决策"为什么这么定"时,再去 `CHANGELOG.md` 搜。**接下来打算做什么、按什么顺序**不在这里,在 `ROADMAP.md`——这三份文件分工:`HANDOFF_STATE.md`=现在,`ROADMAP.md`=以后,`CHANGELOG.md`=过去。

## 基本信息

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **上次更新**:2026-07-11(第五轮,生产激活阶段1),Claude Code(2026-07-09 做了一轮外部工程审计+治理修复,2026-07-10 接通了"选题→大纲→成稿"创作链路的三个 Skill,2026-07-11 三轮"清场式保留重构"——归档 Goal01-12 死链/`scripts/core/runtime/`/`verify_goal_05.py`/`verify_goal_06.py`、统一模型路由为三个显性位点、修正 BR-TOPIC-001 口径与其他文档措辞;2026-07-11 第四轮:清场阶段正式结束,基线封存为 tag `v0.6.3-clean-activation-base`;2026-07-11 第五轮:进入 production activation 阶段,按用户批准的计划完成"生产激活:真实经验闭环"阶段1,细节见下方 ROADMAP.md 第0项)

## 阶段状态(2026-07-11 第四轮新增,第五轮更新)

**清场阶段已结束**。基线已封存为 tag `v0.6.3-clean-activation-base`(commit `3232c4b5ae72b464b0836067ae3675e6f41fce70`)——如果需要回到清场刚完成、production activation 还没开始的干净状态,`git checkout v0.6.3-clean-activation-base`。

**当前阶段:production activation,正在执行**。执行方案见 `C:\Users\15891\.claude\plans\warm-orbiting-kahn.md`(用户已批准),目标是"生产激活:真实经验闭环 + Hermes 创作质量验证",分5个阶段、每阶段做完停下等确认,不自动连续执行——详见 `ROADMAP.md` 第0项,阶段1(把 VersionRef 基础设施接回真实库)、阶段2(把真实证据登记进 VersionRef)已完成。不要再凭"发现了另一处可疑代码"就主动开一轮新的全仓库扫描或扩大 `archive/` 范围,那是清场阶段的活,已经做完;当前阶段的范围以计划文件为准,不要中途改去做计划外的事。

**阶段2(2026-07-11)做了什么**:新增 `scripts/core/experience/evidence_registry.py`,给真实 `hit_deep_analysis` 行登记 `trace_root`+`trace_version`+`object_reference` 的幂等函数。真实对库里唯一存在的记录 `hit_37ae1202dd597fcc3039_v2` 登记一次(登记前打了真实备份 `production_activation_pre_evidence_registration_20260709T220416Z.sqlite3`),验证了:①`trace_version.content_hash`/`object_reference.target_content_hash` 与独立重算的哈希一致;②`object_reference.target_stable_id` 等于真实 `analysis_id`;③原地重复登记一次,返回同一个 root_id/version_id/reference_id(`replayed=true`),`trace_version`/`object_reference` 各自只有 1 行;④装表前后 26 张既有真实业务表逐表哈希核对完全一致,没有动过一条已有数据。5个新测试(正向+反向各有),详见 `ROADMAP.md` 第0项。

**阶段3 数据缺口(2026-07-11 用户指令)已解决**:用户要求"爆款库里偏离值最高的前20篇做拆解"。库里没有现成"偏离值"字段,用户确认口径 = `hit_channel` 里记录的最高基线倍数(如 `share_anomaly:422.75x`)。新增 `deviation_value_from_hit_channel()`/`select_hits_pending_analysis_by_deviation()`(11个新测试)。用户确认 `.env.live-gates` 里的 Hermes 凭证是真实凭证(不是测试专用),已补全进真实 `.env`(`HERMES_BUSINESS_MODEL_TOKEN/BASE_URL/NAME/CLASS`;`.gitignore` 已覆盖,未入库)。真实对偏离值最高的20个hit跑了 `sample_deep_analyze`(真实调用 `xiaomi/mimo-v2.5-pro`):20次里5次首次失败(`FormalSkillValidationError: model output is not JSON`——job/scheduler 记账本身是每次 harness 独立的内存态,不落盘,第二次跑才捕获到具体报错原因),重试后全部20条成功,确认是模型偶发输出格式问题,不是凭证/配置问题。`hit_deep_analysis` 从2行增至22行(21个不同hit)。全部22条(含此前2条)已用 `evidence_registry.py` 登记进 VersionRef(21条新登记+1条原样跳过重复登记),`trace_root`/`trace_version`/`object_reference` 现在各22行。

**阶段3 进展(2026-07-11)**:`dna_note_refs_max` 用户拍板从12上调到20(一批就能装下全部20条证据,不用分批)。新增三个模块把"归纳完的结果怎么正式存下来"这层此前完全空白的地基搭起来:`scripts/core/persistence/goal02_store.py`(通用 `create_state`/`transition_state`,这次只给 tactic 一种对象类型接了真实调用)、`scripts/core/experience/tactic_registry.py`(把 tactic_extract 结果登记成真实 `trace_root`+`trace_version`+`tactic_state` 行)、`scripts/core/experience/run_tactic_extract.py`(绑定层)。**真实调用 tactic_extract 目前还没有一次成功过**:第1次调用失败(`common_patterns has too many items`,当时上限是12);诊断后发现真实模型返回了60条——几乎是把20条材料里出现过的词汇挨个罗列了一遍,不是真的"跨材料找共性",而且 `example_candidates` 返回了0条。**根因排查揪出一个更大的问题**:系统性检查了全部12个业务Skill的真实运行时提示词(不是 `prompt.md`,是 `model_binding.prompt_template`/`_run_content_plan()`/`_run_script_review()` 里真正发给模型的那句话),发现:
1. `script_review` 三个子节点(审稿/润色/判AI味)的提示词**完全没有把任务内容传给AI**——审稿提示词原文就是"Return only JSON with verdict, issues, schema_version.",连要审的稿子本身都没发过去,三个子节点都这样,处于完全不能用的状态。
2. `content_plan`/`script_generate` 有几个契约里要求必传的字段(`tactic_candidates`/`evidence_items`/`selected_hook`/`research_summary`)一直没真正发给模型。
3. `sample_deep_analyze`/`tactic_extract` 的提示词从没规定过输出语言/字数/什么叫"共性",导致真实数据里22条分析有2条随机输出成英文,tactic_extract 归纳质量差。

已按优先级(`script_generate`→`content_plan`→`script_review`→`sample_deep_analyze`→`tactic_extract`)逐个修复:补上所有缺失字段的真实传递、重写全部提示词(明确中文/口语化/具体质量标准,`script_review` 的判AI味标准取材于 `.claude/skills/humanizer-zh/SKILL.md`)。6个新测试直接抓取"真实发给模型的完整提示词字符串"断言之前缺失的字段确实出现了,不是只查schema声明。`tactic_extract` 的 `common_patterns`/`example_candidates` 数量上限暂时放宽到200(诊断性数值,不是最终值)。

**阶段3 完成:`tactic_extract` 第一次真实成功(2026-07-11)**。用修好的 `sample_deep_analyze` 提示词把全部21个不同真实hit重新拆解了一遍(16条真实新调用,16/16成功,含之前已拆过的5条对比样本;5条对比样本也重跑,全部改善——语言问题清零、"选题手法"从复述具体事实变成真正可复用的手法描述)。全部21条新版本重新登记进 VersionRef 证据库。真实调用 `tactic_extract`(第6/7次真实调用)期间又发现并修复两个真实bug:①`select_evidence_for_tactic_batch()` 之前没有"只取每个hit最新版本"的过滤,会优先选到质量较差的旧版本证据(新增1个回归测试);②`example_candidates` 提示词没说清楚"每条要写成一句话"，AI给了结构化JSON对象被拒收，改成明确要求"ONE PLAIN STRING"后第7次调用**成功**——真实产出6条互不相同、各自引用2个真实视频做证据的"共同规律"，正式存进了一条 `tactic_state` 行(`state='candidate'`)。这是这条"从真实爆款到归纳出可复用方法"链路第一次真正跑通产出可用结果。**下一步**(需要用户决定,不是本轮范围):`candidate`状态之后要不要、怎么变成`active`(`transition_state()`已就绪但没有调用方);要不要继续对`content_plan`/`script_generate`/`script_review`做真实调用验证(提示词已修但从没真调用过)。

**后续清理收紧为例外触发,不再是常规工作**:只有下面四种情况出现时,才允许做局部清理,且清理范围只限于触及到的具体文件,不得借机扩大成新一轮审计:
1. **阻塞真实运行**——某个真实生产入口(`business_data`/`experience` 下的 `run_*.py`/`review_queue.py`)跑不起来。
2. **`archive/` import 复发**——有 active 文件重新 import 了 `archive/dead_goal_chain_20260709/` 下的死链(`dead_goal_chain_gate.py` 的 `no_active_reference_into_archive`/`active_verify_goal_scripts_dont_import_dead_chain` 检查会先报警)。
3. **fallback/provider 混入**——出现隐式 provider 切换、`fallback` 非 `none`、或 Codex/Claude Code 被当成运行期 provider。
4. **死配置复发**——出现读不到的配置键、或幽灵 `target_component`(指向代码里不存在的类)。

除以上四种,不要主动扩大清理范围、不要新增资产分类、不要为了"顺手"再审一遍旧代码。

## 当前状态表

| 模块/领域 | 状态 | 阻塞项 | 谁来决定下一步 |
|---|---|---|---|
| 竞品数据采集/判定(`business_data`) | 生产可用,`CreationAssistant_Daily` 每天08:00自动跑 | 无 | - |
| 备料流水线(转写+评论,`run_reverse_prep.py`) | 生产可用,历史积压已清空,自动触发已验证生效 | 无 | - |
| "选题→大纲→成稿"链路(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`) | 技术链路已打通(端到端集成测试通过),中途卡人工审核闸门 | **选题环节只是简化版**(只用对标爆款+评论区两种料源,评分排序/热点/研究缺口都没做,见 `ROADMAP.md`)。**2026-07-11 更正**:`sample_deep_analyze` 已真实花钱调用过22次(21次成功);`source_to_topic`/`content_plan`/`script_generate`/`script_review` 仍然一次都没真实调用过——但这四个的提示词/缺失字段问题这次已经系统性修过一轮(见上"阶段3进展"),下次真调用前不会再是带着已知bug上场 | 用户 |
| 人工审核闸门(`review_queue.py`) | 能用,但只有"通过/不通过"二选一 | 是否要接入已存在但未连接的 `goal10_corrections`(修正+留痕框架),涉及架构级决策 | 用户 |
| `runtime_skills/` 绑定(12个业务Skill) | 5个已接通真实数据(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`/`tactic_extract`,阶段3新增),7个仍只能吃假样例 | 7个待接的具体绑定工程还没排期;已接的5个里`sample_deep_analyze`(21个真实hit)和`tactic_extract`(第7次调用起成功)都已真实调用验证过内容质量;`source_to_topic`/`content_plan`/`script_generate`一次都没真实调用过(提示词已修但未验证) | 用户 |
| 两套 Skill 实现取舍(`.claude/skills/` vs `runtime_skills/`) | 方向已定:`runtime_skills/` 为唯一权威 | 迁移/淘汰尚未执行,`.claude/skills/` 仍在正常使用 | 用户已定方向,执行节奏待定 |
| 两套平行架构(Goal01-12 正式链路 vs `business_data`/`experience` 轻量层) | **2026-07-11 清场完成(两轮)**:`correction`/`production`/`host`/`hermes`/`state`/`runtime` 整个目录 + `workflow/goal_phase5_business_workflow.py`,经全仓库传递闭包核实零真实生产入口依赖后,归档到 `archive/dead_goal_chain_20260709/`。`persistence`/`scheduler`/`workflow/goal05_workflow.py`/`research/goal06_formal_research.py`/`external_adapters/goal_phase4_external_adapters.py` 意外保留(被 `model_gateway`/`external_adapters` 真实传递依赖)。归档 `runtime/` 时连带修了两处真实断链:`config/settings.{yaml,example.yaml}` 的 schema chain 曾引用已归档的 `goal_runtime_vertical_slice_schema.*.sql`(已移除);`test_external_executor_adapters.py` 里两个测 `RuntimeHost` 的用例已随 runtime 一起移除,其余 6 个测真实 `external_adapters` 类的用例保留在原文件 | `goal10_corrections.py` 的"修正+留痕"能力已随 correction/ 一起归档,若未来需要该能力,是一次新的架构决策,不是"接入" | 用户 |
| `Goal05WorkflowOrchestrator` 测试覆盖 | **2026-07-11 补回**:第一轮归档 `test_phase5_business_workflow.py` 时连带丢了唯一覆盖它的测试,新增 `tests/core/test_goal05_workflow_orchestrator.py`(13个测试,真实 `Goal03Scheduler.in_memory()`,无 mock 核心逻辑) | 无 | - |
| Windows 定时任务 | `CreationAssistant_Daily` 已启用,弹窗问题已修复 | `CreationAssistant_Listener` 仍 disabled | - |
| 治理文档体系(CLAUDE.md/AGENTS.md/闸门脚本) | 2026-07-09 完成一轮外部审计修复,2026-07-10 补了"未注明来源常量"闸门,2026-07-11 修正了"创作走 Codex/Claude 订阅"这类把开发工具和运行期 provider 混为一谈的表述 | 无 | - |
| 模型路由(`config/model_routes.yaml`) | **2026-07-11 重构**:唯一显性入口,三个具名位点 `dialogue_model`/`business_model`/`writing_model`,当前全部 = Mimo(经 Hermes),移除了此前混进来的 `engineering_execution` 路由(那本质是 Codex/Claude Code 的开发工具活动,不该被建模成运行期业务路由) | 无 | - |
| 飞书实时集成 | 未重建 | 需要确认是否现在需要 | 用户 |
| GPT 供应商切换 | 未开始 | 非当前阻塞项 | 用户主动触发 |
| 真实付费模型端到端验证 | **2026-07-11 大幅更新,`tactic_extract`首次真实成功**。`HERMES_BUSINESS_MODEL_TOKEN/BASE_URL/NAME/CLASS` 已确认真实、已补进真实 `.env`。`sample_deep_analyze` 真实调用38次(21个不同hit,含首次分析+prompt修复后重新分析,21/21最终成功,人工逐条抽查过质量,基本可用)。`tactic_extract` 真实调用7次,**前6次失败、第7次成功**(依次修了:①参数超限,②归纳质量太差(60条词汇堆砌+0范例),③`schema_version`被埋在提示词中间导致AI遗漏,④证据选择没有"只取最新版本"过滤,⑤`example_candidates`格式AI给了JSON对象不是纯字符串)——过程中系统性查出并修复了全部12个业务Skill里5个的真实运行时提示词缺陷,详见上"阶段3进展"。第7次真实产出6条互不相同、各引用2个真实视频为证的"共同规律",已存进`tactic_state`(`state='candidate'`)。`source_to_topic`/`content_plan`/`script_generate`/`script_review` 仍然一次都没被真实调用过(提示词已修但未验证)。**创作链路当前状态**:`sample_deep_analyze`+`tactic_extract`两步已验证内容质量可用;`content_plan`/`script_generate`/`script_review`修完的提示词还没有真实调用验证过;`tactic_state`目前只到`candidate`,没有变成`active`的路径——不得宣称"创作功能已完成" | 下一步真实调用(`content_plan`等)需要用户重新授权(真花钱) | 用户 |

## 权威闸门真实输出(不是转述)

```
$ python -m scripts.validation.preflight_checkpoint_check
[FAIL] git_working_tree_clean   (只有 Creation_assistant-codex.zip 一个未跟踪文件——会话开始前就在,不是本轮产生的,没动它)
[PASS] test_suite_green
[PASS] readiness_gate_engineering_ready
[PASS] ops_infra_checklist_clean
[PASS] production_data_sanity
[PASS] no_unsourced_constants
[PASS] dead_goal_chain_clean_sweep
overall: NOT_READY(仅因上面那个无关 zip 文件)

$ python -m pytest tests/core tests/validation -q
488 passed
```
（收工前请重新跑一次这条命令确认仍然全绿,不要直接信这里贴的历史输出。）

## 本次会话(2026-07-09 至 2026-07-10)做了什么

**2026-07-09**:外部工程审计 + 按建议做的治理修复(定时任务弹窗、ffmpeg硬编码、闸门反向测试、CLAUDE.md机械生成、真实配置纳入核对、`preflight_checkpoint_check`/`ops_infra_checklist` 等),细节见 `CHANGELOG.md`。

**2026-07-10**:接通创作链路 + 两次真实纠偏:

1. **接通 `source_to_topic`/`content_plan`/`script_generate` 三个 Skill**,串成"hit_deep_analysis → topic_candidates → content_plans → script_drafts"的完整链路,端到端集成测试验证跑通。
2. **用户第一次纠偏**:指出这条链路①没有人工审核,内容会自动流到成稿;②选题只用了"对标爆款"一种料源,原方法论要求的评论区/研究/热点/打分排序都被跳过了。修复了①(三张表加 `human_review_status` 字段+`review_queue.py` 审核工具,验证过闸门真的会挡住未审核内容),部分修复了②(接入评论区当第二种料源)。
3. **用户第二次纠偏(更严重)**:指出 ①"评论区取最热3条"这个数字是我随手编的,没有文档依据——**这正是"专门防止跑偏的机制"应该管却没管住的东西**;②代码库里已经有 `scripts/core/correction/goal10_corrections.py` 这套"修正+留痕"框架,我却没查就自己写了个简化版审核。核实后发现:「选题判断维度.md」这份文档要求的评分标准全仓库不存在,产出它的技能(`.claude/skills/归纳`)已被删除且无人补位;`goal10_corrections` 确实更贴近真实需求,但绑定在另一套从未接真实数据的 `VersionRef` 持久化体系上,牵出"仓库里有两套平行架构从未调和"这个更大的、之前没写进任何文档的事实。用户要求去 `I:\Creation_assistant`(未清洗的旧系统完整版)查是否有可复用的热点采集开源项目——**查证结果:没有找到**。
4. **新增机制**:`scripts/validation/unsourced_constant_check.py`——扫描新写的业务常量有没有老实标注来源(BR-引用/schema引用/明确 UNSOURCED),接入 `preflight_checkpoint_check.py` 成为第6项检查。`TOP_COMMENTS_PER_HIT` 已改为显式 `UNSOURCED` 标注。

全部改动分批提交为独立 commit,每次提交前都跑过完整测试和权威闸门,细节见 git log。

> **2026-07-11 更正**:上面第2/3条把"打分排序"/「选题判断维度.md」评分标准写成了选题环节的缺口——这是当时的错误框定,原样保留作历史记录,但**不再是当前有效的判断**:当前设计没有冻结"选题打分制",不接受把旧系统的评分维度当作缺口补回,详见下方"需要用户决定的事项"里的更正条目。

## 本次会话(2026-07-11,清场式保留重构)做了什么

用户拿着上一份《实现可信度审计报告》,要求执行一次"清场式保留重构":不新增功能,只清理"真系统"和"假系统"并存的问题。全部改动跑过 `python -m pytest tests/core tests/validation -q`(454 passed,比归档前的 484 少 30 条——4 个只测试被归档模块的 pytest 文件一并归档,详见下方)。

1. **归档 DEAD_GOAL_CHAIN**:不凭文件名判断,而是从真实生产入口(`run_competitor_registration_full.py`/`run_reverse_prep.py`/`run_source_to_topic.py`/`run_content_plan.py`/`run_script_generate.py`/`run_sample_deep_analyze.py`/`review_queue.py`/`compare_asr_providers.py`)出发算了一遍真实的 Python import 传递闭包(脚本见 `archive/dead_goal_chain_20260709/README.md` 的方法说明),再决定谁能归档。结果:`correction`/`production`/`host`/`hermes`/`state` 整个目录 + `workflow/goal_phase5_business_workflow.py`(不是整个 `workflow/`)确认零真实依赖,归档到 `archive/dead_goal_chain_20260709/`,连同只测试它们的 `tests/core/test_phase5_business_workflow.py`/`test_phase6_hermes_whitelist_tool.py`/`test_phase7_synthetic_acceptance.py`/`test_production_host_boundary.py`、以及 `experience/goal09_experiments.py`+`verify_goal_09.py`(production 归档后断链的死代码)、`scripts/monitor/queries.py`(state 归档后断链、且本来就没人调用)。**闭包计算揭示了两个反直觉的结果**:`persistence`(`content_hash`/`PersistenceStore`)、`scheduler`(`Goal03Scheduler`)、`workflow/goal05_workflow.py`、`research/goal06_formal_research.py` 这四个名字里带着 `goal0X`、看起来像旧链路的模块,实际被 `model_gateway/formal_skill_adapter.py` 的 `make_*_harness()` 或 `external_adapters/goal_phase4_external_adapters.py` 真实传递依赖,而后者又被两个真实生产脚本直接 import——全部保留在原地,没有归档。归档后修了两处真实断链(`scripts/core/model_gateway/business_route_registry.py` 里两处硬编码路径读取归档文件的逻辑,改成存在性检查,不是硬编码 False)和两个测试(`test_live_gates.py` 里 4 个依赖 Feishu/Hermes-host 的 gate 从"DRY_RUN_PASSED"改为诚实的"NOT_READY"——这些 gate 本来测的就是"未重建"的飞书集成,不是新的功能回退)。
2. **统一模型路由为三个显性位点**:`config/model_routes.yaml` 新增 `model_positions:` 顶层块,明确点名 `dialogue_model`/`business_model`/`writing_model`,当前全部指向同一个 `mimo_main` provider(Mimo/Hermes)。删除了此前混入的 `engineering_execution` 路由——那描述的是 Codex/Claude Code 写代码这件事本身,不是运行期业务路由,留着就是"把开发工具误建模成 runtime provider"。两个 example 配置文件同步更新;`model_routes.example.multi_provider.yaml` 里原来有一个 `gpt_subscription_codex`(`type: codex_cli_subscription`)provider,直接把 Codex 列成业务模型候选项——已删除,换成 `gpt_api_gateway`(标准 OpenAI 兼容 API)示范"未来可以换 provider",不再示范"可以把 Codex CLI 当业务 provider"。
3. **清理死配置**:`.env.example` 里 `CREATION_LLM_PROVIDER`/`CREATION_LLM_MODEL`/`CREATION_LLM_FALLBACK_ENABLED` 三个键——读它们的 `scripts/llm/call.py` 早在 2026-07-04 就被删除,这三行已经是没人读的死配置,换成真正生效的 `HERMES_BUSINESS_API_KEY`/`HERMES_BUSINESS_BASE_URL`/`HERMES_BUSINESS_MODEL_NAME`/`HERMES_BUSINESS_MODEL_CLASS`。
4. **修正文档口径**:`AGENTS.md`(CLAUDE.md 由此机械生成)里"开发用 Codex...创作走用户的 Codex 订阅"、"模型路由 per-node:默认 Codex 低阶...创作走 Codex 对话"这两处原文,是审计报告点名的"把写代码工具误当成业务 runtime 模型"的源头之一——已重写,明确"Codex/Claude Code 只是工程工具,系统运行期统一 Mimo,见三个显性位点"。`REQUIREMENT_CODE_TRACEABILITY.yaml` 里 BR-CONTENT-001/002/003 三条的 `target_component: ContentWorkflow/ContentGuard`——这个类全仓库不存在,是幽灵引用——已改为老实标注 `UNIMPLEMENTED`。
5. **未做的事**(有意,不是遗漏):没有重命名 `runtime_skills/*/binding.yaml` 里的 `route_id: business_analysis`/`writing_generation` 字符串去匹配 `business_model`/`writing_model` 这两个新名字——那需要同时改 12 个 binding.yaml + `model_router.py` 的两个常量,风险和收益不对等,`model_positions:` 这个新增的顶层映射已经能让"三个位点叫什么、各自绑定哪个 route_id"一眼可查,不需要底层也重命名。`scripts/core/runtime/`(`goal04_runtime_host.py` 等,已在下面第二轮处理)和 `scripts/core/external_adapters/goal_phase4_external_adapters.py` 本轮没动——后者是真实依赖,本来就不该动。

## 本次会话(2026-07-11 第二轮,清场收尾)做了什么

上一轮报告点名的两个"仍然未完成的问题"——`scripts/core/runtime/` 没处理、`Goal05WorkflowOrchestrator` 丢了测试覆盖——本轮补上:

1. **`scripts/core/runtime/` 归档**:重新基于当轮代码(前一轮 4 个 commit 之后)跑真实闭包,12 个文件(`goal04_runtime_host.py`/`goal_runtime_vertical_slice.py` 等)确认零真实生产入口依赖,归档到 `archive/dead_goal_chain_20260709/scripts_core_runtime/`。归档暴露了两处上一轮没扫到的真实断链(都不是 Python import,闭包脚本本身扫不到):①`config/settings.yaml`/`config/settings.example.yaml` 的 `sqlite_schema_chain`/`postgres_schema_chain` 引用了 runtime 自带的 schema 文件,已从两处配置移除;②`tests/core/test_external_executor_adapters.py` 里 8 个测试有 2 个(`test_runtime_host_rejects_external_adapter_by_default_and_allows_phase4_opt_in`、`test_external_adapter_failure_does_not_fallback_or_complete_runtime_job`)依赖 `RuntimeHost`——这 2 个随 runtime 一起从活跃测试里移除,其余 6 个测真实 `external_adapters` 类的用例原地保留,没有像上一轮 `test_phase5_business_workflow.py` 那样整个文件一锅端。
2. **补回 `Goal05WorkflowOrchestrator` 测试覆盖**:新增 `tests/core/test_goal05_workflow_orchestrator.py`,13 个测试全部用真实 `Goal03Scheduler.in_memory()`(不 mock 调度器/持久层),覆盖:初始化、合法多步 workflow 真实入队(读 `scheduler.get_job()` 验证 job_kind/status/priority/`_workflow`血缘元数据/correlation_id)、workflow_id 是确定性 content_hash(两个独立 scheduler 同输入得同 id)、同 idempotency_key 重放不重复入队、7 种非法输入(空 workflow_name/空 idempotency_key/零步骤/重复 step_key/空 step_key/空 job_kind/max_attempts≤0)各自显式抛 `WorkflowError`、非法输入不产生部分入队(`scheduler_job` 表行数验证为 0)、模块源码不引用任何已归档模块或 `fallback` 字样。
3. **清场闸门新增 3 项检查**(`scripts/validation/dead_goal_chain_gate.py`,共 11 项):`scripts.core.runtime` 加入 `DEAD_MODULE_DOTTED_PREFIXES`;新增 `archive_unreachable_from_real_entrypoints`(路径而非清单判断,即使某天有人忘记维护前缀清单也能兜底);新增 `goal05_workflow_orchestrator_has_independent_test_coverage`(机械验证测试文件存在、真的 import 了 `Goal05WorkflowOrchestrator`、有 `test_*` 方法)。
4. **仍未处理、本轮核实过确实是死代码但仍不在任何清单里的**:`scripts/core/workflow/verify_goal_05.py`/`scripts/core/research/verify_goal_06.py` 仍 import 已归档的 `RuntimeHost`,手动运行会报错——本轮任务范围没要求处理,只记录,2026-07-11 第三轮已处理(见下)。

## 本次会话(2026-07-11 第三轮,生产激活前基线封口)做了什么

按用户指令处理两个封口问题,不开新一轮清理、不扩大 archive 范围:

1. **BR-TOPIC-001 口径更正**:`BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml` 里的 BR-TOPIC-001 原描述"每日选题排序"(`cluster_boost` 聚类加权、`top_new`/`top_candidates` 截断排序),已改写为当前设计口径:"每日候选选题筛选、去重、冷却、领域约束、来源追溯与人工确认",不再是打分/排序规则,`defaults`/`thresholds` 里的评分参数已清空,不新增替代参数。幽灵组件 `target_component: TopicPlanningService`(全仓库不存在)已替换为真实组件引用:候选生成+人工确认已实现(`scripts/core/experience/run_source_to_topic.py` + `review_queue.py`),去重/冷却/领域约束标注 `pending_implementation`,不冒充完成。旧口径原文以"原文档一节标题如此"的方式保留在 `source_section`/`observed_legacy_behavior` 里作历史引用,不当作当前设计。
2. **`verify_goal_05.py`/`verify_goal_06.py` 归档**:重新核实两者确实零真实生产入口依赖(只被历史 GOAL 报告类 md/yaml 文档提及,不在任何 pytest/preflight 路径里),按用户给的"移入 archive 或标注 retired"二选一,移入 `archive/dead_goal_chain_20260709/scripts_core_workflow/`、`archive/dead_goal_chain_20260709/scripts_core_research/`。它们各自验证的真实模块(`goal05_workflow.py`/`goal06_formal_research.py`)本身未动,仍在原地。
3. **清场闸门新增 1 项检查**(共 12 项):`check_active_verify_scripts_dont_import_dead_chain`——扫描 `scripts/core/**/verify_goal_*.py`,防止未来任何一个还留在活跃树里的自证脚本重新引用已归档模块或 `archive/` 本身;这类脚本不在 pytest 套件里,之前正是这样才漏检了 verify_goal_05/06。

## 需要用户决定的事项(未决,不是遗漏)

- **评论数量**:`TOP_COMMENTS_PER_HIT=3` 没有依据,需要一个真实合理值(是否该随评论总数浮动)。
- **选题判断规则/领域约束怎么设计**(2026-07-11 更正框定,不再叫"选题判断维度"):当前设计没有冻结"选题打分制",这不是"重建一把评分尺子"的问题——需要用户决定的是:候选判断规则、领域约束、去重/冷却、人工确认门槛分别用什么形态落地(不预设是评分式)。旧系统的「选题判断维度.md」及产出它的 `.claude/skills/归纳` 技能不予恢复,不是候选方案。
- **热点采集**:用什么数据源/工具(旧系统里没有可复用的现成方案)。
- **`goal10_corrections.py` 要不要复活**:已随 correction/ 一起归档到 `archive/dead_goal_chain_20260709/`,若未来确实需要它的"修正+留痕"能力,是一次新的、独立的架构决策,不是简单地"接回来"。
- `runtime_skills/` 剩余 8 个 Skill 的绑定工程什么时候开始。
- `.claude/skills/*` 具体什么时候开始迁移/淘汰。
- `external_adapters/goal_phase4_external_adapters.py` 是真实依赖,不需要处理;`scripts/core/runtime/` 已在 2026-07-11 第二轮清场归档完毕(见上方状态表)。
- 是否现在授权真实付费模型调用做一次端到端验证(涉及真实花费)。
- 飞书集成要不要重建、GPT供应商切换要不要启动——一直是"未来用户主动触发项",不是当前阻塞。
