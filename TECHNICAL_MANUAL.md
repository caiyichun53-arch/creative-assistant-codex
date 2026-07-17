# 创作助手 · 技术手册

## 1. 适用范围

本手册只说明当前已存在的运行入口、配置和验证方式；它不定义业务规则。
业务设计与实施裁决唯一依据为 [`docs/EFFECTIVE_DESIGN_BASELINE.md`](docs/EFFECTIVE_DESIGN_BASELINE.md)。本手册与该基线冲突时，以基线为准。

## 2. 配置与模型路由

- 运行期模型路由唯一配置：`config/model_routes.yaml`。
- 当前模型位点为 `dialogue_model`、`business_model`、`writing_model`；每个位点通过 `provider_ref` 显式绑定 provider。
- `fallback` 必须为 `none`。`scripts/core/model_gateway/model_router.py` 会拒绝任何非 `none` 的配置。
- 开发工具不属于运行期 Provider。Stage 0 的正式模型请求只能由 `scripts/core/production/stage0_content_core.py` 经 `ModelRouter` 创建；配置中的模型引用不能解析为具体模型时，Core 会失败关闭。

可执行的路由与直接模型调用检查：

```powershell
python scripts/core/model_gateway/business_route_registry.py
```

该命令将验证结果写入 `validation_evidence/`；输出是验证证据，不是设计来源。

## 3. 业务数据与受控入口

### 竞品数据层

- 本地业务数据位于 `data/formal/*.sqlite3`。
- 账号注册、检索、热点登记、备料、经验候选和创作链绑定位于 `scripts/core/business_data/` 与 `scripts/core/experience/`。
- `run_reverse_prep.py` 负责当前备料中的媒体/转写/评论准备边界；它不是旧 DNA 拆解入口。
- 会产生数据库写入、网络请求、安装或付费模型调用的脚本，必须先通过 `scripts/core/execution_contract.py` 校验有效基线引用。

常用受控入口由各脚本的 `--help` 说明参数；不要用临时片段绕过这些入口。

### 持久化与状态

`scripts/core/persistence/` 保存版本、引用和状态机基础设施。状态迁移必须通过受控代码路径；数据库字段名和物理表结构不构成业务设计权威。

Stage 0 第一条内容生产链的唯一正式写入入口是
`Stage0ContentProductionCore`。它的 production 身份只允许连接
`data/formal/production_activation.sqlite3`；`test`、`fixture`、`synthetic`、`replay`
和 `mock` 身份必须使用独立库。该入口目前只提供状态、不可变版本、人工决定、
Input Assembly、ModelGateway 运行记录和审计边界，不执行真实内容生成。

### Stage 1A：正式选题与研究方案待审核

`scripts/core/production/stage1a_research_plan.py` 仅将用户提交的正式选题推进到研究方案待审核：
`Stage1AResearchPlanService.submit_formal_topic` 创建不可变选题版本并停在待人工确认；
`confirm_formal_topic` 后，`generate_research_plan` 才能通过 `ModelGateway` 的显式
`stage0.research_plan` 路由调用模型。研究方案及其 Input Assembly、模型运行记录和版本均由
`Stage0ContentProductionCore` 写入 `data/formal/production_activation.sqlite3`，生成成功仍停在
`awaiting_human_review`。`view_artifact`、`approve_research_plan`、`return_research_plan` 与
`cancel_task` 分别用于查看、人工通过、退回并以新版本重做、取消。Stage 1A 不执行深度研究。

### Stage 1B：受控日常发现

`scripts/core/production/stage1b_daily_discovery.py` 是由项目 Core Runtime 直接执行的一次性 Stage 1B 批处理入口；它不是由 Codex 轮询的业务进程。每次必须显式给出一个领域、审计主体、幂等键和运行模式：`test_isolated` 只能使用非生产数据身份，`validation_live` 只形成隔离的真实验收结果，`production_daily` 才可能形成正式日产候选池。前两种模式不可自动升级，也不能进入日产能、Stage 1A、研究或经验系统。

`validation_live` 每次必须且只能指定一个 `--source-type`，用于按来源逐项验收；热点和标签搜索只校验自己需要的采集配置，不要求无关来源同时启用。一次 `validation_live` 批次只对第一个通过确定性过滤的 eligible 来源发起一次模型尝试，尝试成功、零候选、输出非法或模型失败后均停止本批次后续 eligible 处理，避免单次验收继续消耗多个模型调用。六个值是 `hotspot`、`daily_competitor_content`、`historical_high_signal`、`tag_discovery`、`question_expansion`、`saved_user_direction`。`production_daily` 不接受缺项，必须执行完整六来源集合。

新批次的固定顺序是：TrendRadar 热点采集→热点领域转化→对标日常来源→对标历史高信号→领域标签搜索→标签来源转化→已完成问题拓展→用户保存方向。标签搜索每天按最久未搜索优先轮换最多 3 个活跃标签，每个标签只请求第 1 页；该页返回多少条就在 `domain_search_page_observation` 留存多少条，不翻页补量、不按互动量另截固定条数。通过确定性过滤的记录才进入 `discovered_external_videos`，之后仍只是来源，不是候选。领域话题库只用于标签搜索；热点原始观察写入 `trendradar_hotspot_observation` 后，按 `config/domain_packs/*.yaml` 的版本化领域规则匹配，不读取活跃话题标签。

领域包的 `discovery.topic_search` 区分领域宽标签、通用流量标签和活动待复核词。`科普`、`知识`在泛科普领域保留；活动词只进入 `pending_review`。只有 `domain_search_activity_tag_registry` 中存在当前有效、带平台、活动身份、证据链接、有效期和理由的精确登记时，才会排除对应平台活动标签。新增领域通过新增领域包接入，通用 Stage 1 代码不增加领域枚举或复制流水线。

候选判断输入按 `source_to_topic.input.v1` 组装，必须通过 `runtime_skills/source_to_topic` 的原子 Skill 合同和 `business.source_to_topic` 运行期模型节点；`stage1b_model_run` 记录该 Skill 的 route、provider、model、binding、usage 和校验状态。Codex/Claude 只用于修改工程代码，不是这里的运行期业务模型。Core 在 Skill 返回后仍按领域包做确定性复核：模型把娱乐人物、偶像、粉丝等内容换写成“社会公平”或“公众情绪”角度时，会保存为确定性零候选；模型自述材料严重不足或事实无法确认时同样不得建立候选。Skill 输出必须符合 `source_to_topic.output.v1`；Runtime 只额外兼容一个完整的 JSON Markdown 代码块，不接受代码块外说明、多个对象或其他自由文本。

真实采集默认关闭。启用前分别核实所运行来源的 `live_enabled: true`；单独验收热点不要求同时启用标签搜索。官方 TrendRadar 固定安装在仓库内 `vendor/TrendRadar`，使用其锁定环境的命令为 `vendor/TrendRadar/.venv/Scripts/python.exe -m trendradar`。项目 Runtime 直接入口为 `python scripts/integrations/trendradar_runtime.py --trendradar-dir vendor/TrendRadar --output outputs/stage1/trendradar/latest.json --evidence-root validation_evidence/stage1/trendradar --timeout-seconds 300`。入口只运行一次官方命令，从本次更新的 `output/news/YYYY-MM-DD.db` 转换标准对象，并保存 stdout、stderr、命令、官方提交、原始数据库、输出、错误和耗时；不调用模型、不自动重试、不补造 URL 或数量。

TrendRadar 安装完整性检查允许上游命令自身产生或更新 `output/` 运行数据、项目批准的 `config/config.yaml` 和 Python `__pycache__`；它们不会被误判为源码篡改。除此之外，任何上游工作区改动仍会在发出网络请求前失败关闭。不要为了通过检查删除真实运行输出，也不要在 `vendor/TrendRadar` 内修改上游源码。

Stage 1 来源完成度统一使用 `DESIGNED → SCAFFOLDED → TECH_TESTED → ENV_READY → LIVE_VALIDATED → PRODUCTION_READY`。Mock、fixture、fake provider、文件存在或 pytest 通过最高只算 `TECH_TESTED`；没有项目内安装、正式本机配置、真实输入输出和运行证据，不得写“已补齐”“已接通”或“功能完成”。只有达到 `LIVE_VALIDATED` 的来源，才有资格在另行授权后进入真实候选发现。

```powershell
python scripts/core/production/stage1b_daily_discovery.py --domain fan_kepu_social_life --mode production_daily --actor <audited-user> --idempotency-key <stable-authorized-run-key> --batch-timeout-seconds 600
```

批处理在截止时间、中断或部分失败时会写入实际生命周期和审计，不伪装为完整成功；模型请求状态不确定、可能已消耗 token 或已离开明确失败状态时禁止自动重试。对既有真实验证运行，仅可通过同一受控入口一次性重分类，且必须显式标为 `validation_live`，不会重新读取来源或调用模型：

```powershell
python scripts/core/production/stage1b_daily_discovery.py --reclassify-run <run-id> --mode validation_live --actor <audited-user> --reason <audited-reclassification-reason> --idempotency-key <stable-reclassification-key>
```

已完成问题拓展和用户保存方向通过项目 Runtime 的确定性入口登记，不调用外部来源或模型：

```powershell
python -m scripts.core.production.stage1_source_runtime register-question-expansion --expansion-id <id> --domain fan_kepu_social_life --core-question <具体中文问题> --parent-source-ref <JSON对象> --actor <audited-user>
python -m scripts.core.production.stage1_source_runtime register-user-direction --direction-id <id> --domain fan_kepu_social_life --core-question <具体中文问题> --submitted-by <user-id>
```

物理删除错误 `validation_live` 结果只允许用户明确指定运行后执行。入口要求精确候选数和逐字确认令牌，拒绝已有用户决定的运行；它删除该运行及全部派生来源版本、过滤、输入组装、模型记录、候选、零候选、快照、冷却、回执和审计，不删除原始对标视频或历史高信号：

```powershell
python -m scripts.core.production.stage1_source_runtime purge-validation-run --run-id <run-id> --expected-candidates <count> --actor <audited-user> --reason <reason> --confirm DELETE_VALIDATION_LIVE_RUN:<run-id>
```

### 外部适配器

`scripts/core/external_adapters/` 隔离采集、评论、研究和转写等外部能力。适配器只传递受控输入与结果，不能自行改变业务流程或作为模型调用入口。

## 4. 原子 Skill 与人工闸门

### 业务工作流

`scripts/core/workflow/` 只承载当前可执行的流程编排；流程边界以有效基线和实际测试为准。

### 经验库

`scripts/core/experience/` 负责证据、候选打法、内容绑定和人工审核队列。候选或提案不等于正式经验，正式经验语义不得被自动改写。

- 可移植原子 Skill 位于 `runtime_skills/`，只处理其 schema 定义的输入和输出，不自行读取业务数据库或串联其他 Skill。
- `scripts/core/experience/run_source_to_topic.py`、`run_content_plan.py`、`run_script_generate.py`、`run_script_review.py`、`run_final_draft.py`、`review_queue.py` 与 `run_sample_deep_analyze.py` 保留为隔离测试/旧链辅助代码；必须显式给出非 production 数据身份，且当目标为正式库时会在建立连接前失败，不得推进正式状态。
- `FormalBusinessSkillHarness` 使用内存 `PersistenceStore`，只能带非 production 测试身份；正式模型运行记录必须由 Stage 0 Core 的 `CoreModelRunMaterializer` 落入正式事实源。
- `.agents/skills/` 是 Codex 的本地辅助 Skill 目录，不是运行期业务 Skill 加载路径，也不是业务设计来源。

## 5. 当前验证

### 工程验收脚本

`scripts/core/production/` 与 `scripts/core/staging/` 中保留的脚本仅用于当前技术验证或受控运行辅助；它们不能把历史验收状态当成现行业务依据。

基础技术回归：

```powershell
python -m pytest tests/core tests/validation -q
```

当前外部边界验证配置：`config/live_gates.example.yaml`。它只保留当前模型 Provider、ASR、研究适配器和外部采集适配器边界；不再验证退役的 Host、消息平台、阶段影子链或旧调度链。

```powershell
python scripts/validation/live_gates.py dry-run
```

`dry-run` 仅使用隔离/伪造输入，不产生外部副作用。任何真实调用必须显式启用对应配置、满足凭据和环境前置条件，并通过受控入口执行。

## 6. 维护纪律

- 新模块在同一改动中更新本手册的当前使用说明。
- 不将旧计划、历史报告、迁移说明、交接记录或旧会话 Skill 写回为设计依据。
- `AGENTS.md` 是唯一执行章程；不生成或依赖 `CLAUDE.md`。
