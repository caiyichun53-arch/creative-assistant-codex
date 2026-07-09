# HANDOFF_STATE(现状快照,不是历史记录)

> **活文档**:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。**2026-07-09 改版**:这份文件只保留"现在是什么状态"的结构化快照——详细的过程记录、事故复盘、真实数据验证推理链路搬到 `CHANGELOG.md`(只增不改的历史归档)。新会话开工只需要读这份文件(几分钟能读完),不需要考古 `CHANGELOG.md`;需要查某个具体决策"为什么这么定"时,再去 `CHANGELOG.md` 搜。**接下来打算做什么、按什么顺序**不在这里,在 `ROADMAP.md`——这三份文件分工:`HANDOFF_STATE.md`=现在,`ROADMAP.md`=以后,`CHANGELOG.md`=过去。

## 基本信息

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **上次更新**:2026-07-11(第二轮),Claude Code(2026-07-09 做了一轮外部工程审计+治理修复,2026-07-10 接通了"选题→大纲→成稿"创作链路的三个 Skill,2026-07-11 第一轮按用户指令执行"清场式保留重构"——归档 Goal01-12 死链、统一模型路由为三个显性位点、修正文档口径;2026-07-11 第二轮补收尾:归档 `scripts/core/runtime/`、补回 `Goal05WorkflowOrchestrator` 的独立测试、清场闸门新增 3 项检查,细节见下方)

## 当前状态表

| 模块/领域 | 状态 | 阻塞项 | 谁来决定下一步 |
|---|---|---|---|
| 竞品数据采集/判定(`business_data`) | 生产可用,`CreationAssistant_Daily` 每天08:00自动跑 | 无 | - |
| 备料流水线(转写+评论,`run_reverse_prep.py`) | 生产可用,历史积压已清空,自动触发已验证生效 | 无 | - |
| "选题→大纲→成稿"链路(`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate`) | 技术链路已打通(端到端集成测试通过),中途卡人工审核闸门 | **选题环节只是简化版**(只用对标爆款+评论区两种料源,评分排序/热点/研究缺口都没做,见 `ROADMAP.md`);还没花钱调用过真实大模型 | 用户 |
| 人工审核闸门(`review_queue.py`) | 能用,但只有"通过/不通过"二选一 | 是否要接入已存在但未连接的 `goal10_corrections`(修正+留痕框架),涉及架构级决策 | 用户 |
| `runtime_skills/` 绑定(12个业务Skill) | 4个已接通真实数据,8个仍只能吃假样例 | 8个待接的具体绑定工程还没排期 | 用户 |
| 两套 Skill 实现取舍(`.claude/skills/` vs `runtime_skills/`) | 方向已定:`runtime_skills/` 为唯一权威 | 迁移/淘汰尚未执行,`.claude/skills/` 仍在正常使用 | 用户已定方向,执行节奏待定 |
| 两套平行架构(Goal01-12 正式链路 vs `business_data`/`experience` 轻量层) | **2026-07-11 清场完成(两轮)**:`correction`/`production`/`host`/`hermes`/`state`/`runtime` 整个目录 + `workflow/goal_phase5_business_workflow.py`,经全仓库传递闭包核实零真实生产入口依赖后,归档到 `archive/dead_goal_chain_20260709/`。`persistence`/`scheduler`/`workflow/goal05_workflow.py`/`research/goal06_formal_research.py`/`external_adapters/goal_phase4_external_adapters.py` 意外保留(被 `model_gateway`/`external_adapters` 真实传递依赖)。归档 `runtime/` 时连带修了两处真实断链:`config/settings.{yaml,example.yaml}` 的 schema chain 曾引用已归档的 `goal_runtime_vertical_slice_schema.*.sql`(已移除);`test_external_executor_adapters.py` 里两个测 `RuntimeHost` 的用例已随 runtime 一起移除,其余 6 个测真实 `external_adapters` 类的用例保留在原文件 | `goal10_corrections.py` 的"修正+留痕"能力已随 correction/ 一起归档,若未来需要该能力,是一次新的架构决策,不是"接入" | 用户 |
| `Goal05WorkflowOrchestrator` 测试覆盖 | **2026-07-11 补回**:第一轮归档 `test_phase5_business_workflow.py` 时连带丢了唯一覆盖它的测试,新增 `tests/core/test_goal05_workflow_orchestrator.py`(13个测试,真实 `Goal03Scheduler.in_memory()`,无 mock 核心逻辑) | 无 | - |
| Windows 定时任务 | `CreationAssistant_Daily` 已启用,弹窗问题已修复 | `CreationAssistant_Listener` 仍 disabled | - |
| 治理文档体系(CLAUDE.md/AGENTS.md/闸门脚本) | 2026-07-09 完成一轮外部审计修复,2026-07-10 补了"未注明来源常量"闸门,2026-07-11 修正了"创作走 Codex/Claude 订阅"这类把开发工具和运行期 provider 混为一谈的表述 | 无 | - |
| 模型路由(`config/model_routes.yaml`) | **2026-07-11 重构**:唯一显性入口,三个具名位点 `dialogue_model`/`business_model`/`writing_model`,当前全部 = Mimo(经 Hermes),移除了此前混进来的 `engineering_execution` 路由(那本质是 Codex/Claude Code 的开发工具活动,不该被建模成运行期业务路由) | 无 | - |
| 飞书实时集成 | 未重建 | 需要确认是否现在需要 | 用户 |
| GPT 供应商切换 | 未开始 | 非当前阻塞项 | 用户主动触发 |
| 真实付费模型端到端验证 | 未做过,全部 Skill 测试用确定性假 Provider——**创作链路当前只到"状态机已验证",不得宣称"创作功能已完成"** | `HERMES_BUSINESS_MODEL_TOKEN` 等凭据只在 `.env.live-gates`,没进真实 `.env` | 用户 |

## 权威闸门真实输出(不是转述)

```
$ python -m scripts.validation.preflight_checkpoint_check --skip-tests
[PASS] git_working_tree_clean
[PASS] test_suite_green
[PASS] readiness_gate_engineering_ready
[PASS] ops_infra_checklist_clean
[PASS] production_data_sanity
[PASS] no_unsourced_constants
[PASS] dead_goal_chain_clean_sweep
overall: READY_TO_CHECKPOINT

$ python -m pytest tests/core tests/validation -q
473 passed
```
（收工前请重新跑一次这条命令确认仍然全绿,不要直接信这里贴的历史输出。上面这次是提交后跑的。)

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
4. **仍未处理、本轮核实过确实是死代码但仍不在任何清单里的**:无——本轮结束后闭包计算未再发现新的孤立模块。

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
