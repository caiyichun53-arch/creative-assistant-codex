# Creation Assistant 独立化迁移基线

盘点日期：2026-08-28  
盘点范围：I:/Creation_assistant-codex 及其实际引用的外置运行目录、Windows 启动项、WSL 入口、Hermes 本机痕迹、知识库目录。  
本文件性质：开发前现状确认和唯一总体迁移方案。

本轮在不启动正式业务、不调用模型、不修改正式数据库内容的前提下，修订本基线并封存迁移现场。允许的新增内容只有本文件和 migration_snapshot/ 快照目录；正式数据库只创建一份不覆盖旧备份的迁移前备份。

## 本轮修订的迁移硬规则

### 1. 迁移保护期暂停正式业务

从阶段 0 封存完成开始，Creation Assistant 进入迁移保护期。核心迁移期间暂停新的正式业务写入。

在以下条件全部完成并验证前，不得恢复正式运行：

1. Creation Assistant 独立 Core 已真实成立；
2. MCP 正式路线可用；
3. production 和 test 已可靠隔离；
4. 旧历史状态不会控制新执行；
5. 用户明确允许恢复正式运行。

保护期内不得启动新的正式 daily、cold-start、resume、candidate discovery、formal research、daily repair、knowledge apply 或其它改变正式业务状态的入口。正式数据库和正式运行资料只允许读取、盘点、备份、校验和生成迁移凭证，不得清理 failed、processing、running，不得修正正式记录，不得迁移正式数据，不得补写正式记录。

本保护期不通过修改正式数据库实现只读，也不新增 migration lock、权限框架、数据库写防火墙、watchdog 或全局写入栅栏。当前先靠迁移纪律和入口收敛；技术层强制只读留到后续独立 Core 和数据隔离阶段。

### 2. 自动触发和模型执行必须分开

Creation Assistant 负责判断业务状态、判断今天是否已有 Business Run、保存流程和结果、提供下一项需要智能处理的任务。

Creation Assistant 不负责选择模型。

如果未来每天 08:00 自动运行，自动触发和模型执行必须分开：

    08:00 外部动作启动指定 Agent
    Agent 通过 MCP 使用 Creation Assistant
    Agent 使用自己的当前模型

当前可以由 Hermes 作为外部启动者；未来替换 Hermes 时，只替换外部执行者，不修改 Creation Assistant 的业务流程。MCP 不是调度器。本轮不设计新调度系统。

### 3. Skill 只有一份正式来源

Creation Assistant 中只能存在一份当前正式 Skill 定义。不能长期同时维护 Hermes 一份正式 Skill、Codex 一份正式 Skill和项目里另一份正式 Skill。

外部 Agent 获得智能任务时，必须获得该任务对应的当前正式 Skill、输入材料、业务限制、输出要求和必要来源身份。

Hermes 和 Codex 可以有自己的通用能力，但不能维护 Creation Assistant 正式 Skill 的平行副本。本轮只记录和补充设计，不实现 Skill 收敛。

### 4. 身份边界不能只靠提示词

开发、测试、正式运行、知识维护四类身份继续保留。

最终不能只靠自然语言提醒“现在是正式运行，所以不要改代码”。正式运行时，Agent 应通过 MCP 使用 Creation Assistant 正式业务接口，而不是靠直接修改项目文件完成业务。开发时主要接触代码和测试环境；正式运行时主要接触 MCP 和正式业务接口。

本轮不建立复杂权限系统，但后续架构不能把身份边界完全建立在 Agent 自觉上。

### 5. 项目目录迁移提前为独立阶段

项目目录从 I:/Creation_assistant-codex 改为 I:/Creation-Assistant，不再放到最后清理阶段。

必须先完成独立 Core、MCP、production/test 隔离，并确认新代码不再依赖旧项目绝对路径，然后单独执行项目目录迁移阶段；项目目录迁移完成后才开始网页开发。这样可以避免网页、启动脚本和观察接口围绕旧目录名建设后再整体搬迁。

### 6. ModelGateway 先拆职责，不整体搬迁

当前不能提前认定 ModelGateway 这个结构或名字要保留。后续先逐项拆分它实际承担的：

- provider 选择；
- model 选择；
- 模型调用；
- 执行记录；
- 结果解析；
- 正式结果校验；
- 审计记录。

目标架构下，模型选择和模型调用责任必须退出 Core。结果接收、正式校验、执行记录和必要审计仍可能属于 Creation Assistant，但不要求继续使用原 ModelGateway 结构，也禁止为了兼容旧架构把它整体搬进新 Core。

## 0. 先说结论

Creation Assistant 已经有一部分真正的业务核心：正式数据库、正式业务表、采集和本地转写适配层、多个正式流程、人工确认记录，以及把正式运行数据库放在项目目录外的存储边界。

但它还没有真正独立。当前最明显的事实是：

1. daily 入口名字和启动假设仍然以 Hermes 为中心。
2. daily 会从项目配置中读取 Hermes 当前模型，并把这个模型绑定写入本次执行；这不等于“谁调用谁使用自己的当前模型”。
3. Core 内部仍然通过 ModelGateway 和 Hermes 模型适配器完成模型调用。
4. 直接的生产 daily、候选发现、cold-start、研究、修复和知识汇合入口不止一条。
5. 当前正式数据库里既有正式业务记录，也有名为 real_daily_validation 的验证记录，而且这些记录的身份仍是 production。
6. 运行目录中有 processing、running、failed 等残留状态和旧后台执行记录，不能直接假设它们只是无害日志。
7. Windows 启动项和 WSL 启动脚本仍指向已不存在的旧项目路径、旧脚本或已经删除的页面服务。
8. 当前工作区有大量未提交改动和未跟踪文件，因此它只能作为“当前工作树事实”，不能被当作一个已经封版的迁移起点。

下面的处置都是后续计划。本轮不执行处置。

## 1. 盘点依据和当前工作区身份

判断事实时采用以下顺序：

1. 当前实际代码和当前工作树；
2. 当前实际正式数据库和 schema；
3. 当前实际启动入口；
4. 当前实际配置和环境文件；
5. Windows、WSL、Hermes 本机运行痕迹；
6. 旧设计文档只作为辅助说明。

当前 Git 分支是：

    implementation/v1.3-stage1b-daily-discovery

当前 HEAD 是：

    0bfb7960c40d53fd96bc4d9ba2c6d57780ce6d7e

当前工作树不是干净状态。它同时包含已修改、已删除、已新增和未跟踪的代码、配置、文档、运行验证材料以及若干临时目录。这里不能把未提交内容自动当作已经确认的最终设计，也不能为了本次盘点清理它们。

当前没有找到：

- 当前目录下的正式数据库副本；
- 当前目录下的 data/formal、data/test、outputs、logs 正式运行目录；
- 当前目录下的 scripts/feishu；
- 当前目录下的 tools 目录；
- 当前可用的项目 MCP 服务文件；
- 与 Creation Assistant、Hermes、Daily、Listener、Cold Start、Assistant 等关键词匹配的 Windows 计划任务。

## A. 最终架构

### A.1 Creation Assistant Core

Core 是真正的内容生产业务系统。

它保存和判断正式业务事实，负责正式业务流程，负责 daily、cold-start、候选、研究、内容、人工确认、resume、完成状态和经验回流。它也负责调用采集、ASR 等外部能力，并把结果按正式规则写入自己的数据库。

Core 不属于 Hermes，也不属于 Codex。即使 Hermes 和 Codex 都没有启动，Core 仍然应该能够独立启动、打开自己的数据库、查看正式状态、执行不依赖模型的正式业务能力，并且能够明确报告哪些步骤需要外部智能执行者提供结果。

Core 不选择 MiMo、OpenRouter、Ox、Codex 或其它模型。Core 不做 provider fallback，不根据自己的偏好切换模型。它只接收这次智能任务的执行者和执行结果，并记录必要的执行事实。

### A.2 Skills

Skill 是原子智能任务的工作方法。

例如 competitor_breakdown、source_to_topic、outline_planner、content review、humanizer。Skill 说明某一项任务应该如何分析、输入什么、输出什么、如何检查结果。

Skill 不保存正式 daily 生命周期，不拥有 resume，不自己判断数据库事实，不自己建立另一套 processing 或 completed 状态，也不能绕过 Core 写正式业务数据。

正式状态由 Core 持有。Skill 产出的结果必须回到 Core，由 Core 按正式规则决定是否进入下一步，遇到人工决定时必须停在人工决定处。

### A.3 知识库

正式概念是 Creation Assistant 知识库，不是 Obsidian。

知识库可以包含经验、方法论、研究资料、Skill 知识、案例、Wiki、关联和长期学习材料。Obsidian 是当前查看和编辑知识库的重要工具，但它只是一个使用界面或镜像位置。

普通经验可以由人或 LLM 维护。会改变正式业务行为的规则不能因为某篇笔记被自动修改就直接生效。正式规则必须经过明确确认、发布和版本记录。

### A.4 MCP

MCP 是 Hermes、Codex 等外部使用者访问 Core 的标准接口。

MCP 只负责把 Core 已经存在的正式能力暴露出去、接收调用方传入的信息、返回 Core 的结果。MCP 不重新实现 daily、resume、人工确认或生命周期，不持有第二份正式状态，不直接改正式数据库。

### A.5 网页

本地网页是人的入口。

网页展示什么、进行什么操作，都必须读取或调用 Core。网页不自己计算当前状态，不自己推断“已经完成”，不自己决定是否跳过或继续，不自己维护另一份进度。

网页和 MCP 是两种入口，最后都回到同一个 Core。

### A.6 Hermes 和 Codex

Hermes 和 Codex 都是外部使用者。

谁执行这次智能任务，谁使用自己当前的模型。Hermes 用 Hermes 当前模型，Codex 用 Codex 当前模型。Creation Assistant 只负责业务上下文、输入、正式规则、结果接收和记录，不负责替调用者选模型。

Hermes 正式运行时可以查看状态、使用 Skill、执行智能任务、提交正式结果和报告问题，但不能修改 Creation Assistant 源代码、Skill、项目配置、schema，也不能为了现场失败直接打补丁。

Codex 的任务身份必须明确区分：

1. 开发；
2. 测试；
3. 正式运行；
4. 知识维护。

同一项重要任务原则上只属于一种身份。尤其不能一边修改系统，一边继续正式运行同一个业务。

### A.7 正式数据、测试数据、Business Run、Execution

正式世界和测试世界必须物理或边界可靠隔离。

Business Run 表示哪一次正式业务，例如某次 daily 或某次 cold-start。Execution 表示这次业务由谁在什么时候执行。一次 Business Run 可以有多次 Execution；失败后继续仍然属于原来的 Business Run，不因为换了 Hermes、Codex 或模型就另建第二个正式 daily。

## B. 当前真实架构

### B.1 启动和代码分布

当前代码分成两层：

- scripts/core 里面已经有内容生产 Core、数据库持久化、正式阶段、外部采集适配器、ASR 和模型网关；
- scripts/agent_platform 里面有大量以 Hermes 命名的交互桥、daily 入口、cold-start 入口、研究入口、修复入口、知识汇合入口以及后台执行入口。

这说明业务代码和交互承载已经有部分分层，但当前实际入口仍然把 Hermes 当成主要启动者和模型承载者。

README 声称项目不包含定时任务、监控面板和定时运行能力；但当前工作树实际存在 agent platform 入口、外置运行状态、启动项残留和 daily 运行日志。因此 README 是目标性或历史性说明，不能作为当前运行事实。

### B.2 daily

当前主要 daily 路线是：

1. Hermes 命名的 daily 入口；
2. 它调用 daily collection once；
3. daily operations runtime 协调采集、账号完成判断、观察结果和候选发现；
4. Core 打开外置正式数据库并写正式业务状态。

这个入口的说明明确写着 Hermes 负责调度和调用。实际 daily 代码还会从项目模型路由中解析当前 Hermes 执行绑定，再把该绑定交给 daily coordinator。

同时，daily collection once 本身也可以被直接调用，候选发现脚本也可以直接调用。因此现在不是一个唯一的正式入口。

外置运行状态显示最近 daily 记录为 completed_with_failures，失败阶段是 collection，阻塞原因是 content type registry 无效或未冻结。这个状态说明真实运行曾发生过失败，但不证明本轮修复或正式业务已完成。

### B.3 cold-start

当前存在：

- Hermes cold-start 管理入口；
- cold-start 后台执行入口；
- cold-start 配置、预览、确认、状态查看、停止、恢复、人工审核等动作；
- 一个当前已经失败的正式 cold-start 记录；
- 一个已经完成的正式 cold-start 记录。

当前 cold-start 管理桥本身声称不直接写数据库，实际通过 Core 适配器执行。但入口、上下文标记、载体绑定和执行绑定仍然是 Hermes 专属表达。

当前的 Core 代码还明确要求新 cold-start 和 cold-start resume 使用当前 Hermes 模型绑定。这与最终“调用者使用自己的当前模型、Core 不绑定 Hermes”的方向直接冲突。

### B.4 resume、正式人工确认和修复

当前 resume 不是一个完全独立的 Core 统一入口，而是分散在不同的业务入口和后台入口中：

- daily 通过 daily coordinator 和已有 daily run 继续；
- cold-start 通过 Hermes cold-start 管理入口的 resume 动作继续；
- 研究通过 Hermes formal research 入口执行、批准、退回、重试、修订；
- daily 观察问题通过 Hermes daily repair 入口计划或应用修复；
- 人工决定由 Core 中的 human decision command 等表承载，但具体入口仍由 Hermes 风格的桥接层暴露。

当前正式数据库有 101 条人工决定命令，其中 77 条 completed、23 条 rejected、1 条 received。人工决定确实已经进入 Core 数据库，但入口还没有统一成独立 Core 的正式业务接口。

### B.5 模型调用和 ModelGateway

当前 Core 内有 ModelGateway、ModelRouter、HermesModelProviderAdapter、CodexAppServerProviderAdapter 等组件。

当前正式模型路由情况是：

- mimo_main 和 relay_main 的 provider_name 都是 hermes；
- 业务分析、研究规划和写作路线默认走 active_provider；
- 只有 workbench_read_only_assistant 路线使用 Codex app server；
- fallback 配置为 none；
- 当前没有发现名为 OPENROUTER 的独立活动配置，实际看到的是 relay_main 和 GPT_RELAY_* 这条外部中转配置；
- 实际模型执行记录全部标记为通过 ModelGateway；
- 正式数据库中的绝大多数模型记录来自 Hermes provider 路径。

这不是最终的模型无关架构。当前 ModelGateway 既承担模型调用，又把 provider、model、endpoint 等执行身份作为正式运行事实的一部分；同时 daily 和 cold-start 代码会主动解析 Hermes 当前模型绑定。后续必须把“谁执行、使用什么模型、如何调用模型”从 Core 的业务规则中拆出，保留必要的执行审计，但不让 Core 决定调用方模型。

### B.6 外部采集和本地能力

当前 Core 的外部适配器包括 MediaCrawler、SenseVoice、VoxCPM2、TrendRadar、AnySearch、Windows 进程和音乐受众浏览器等。

这些是被业务调用的外部能力，不等于 Hermes 业务。它们可以保留在 Core 的外部能力边界之外，由 Core 以明确的输入输出调用。当前 TrendRadar 配置已经把输出证据放在外置运行目录；domain search 的 live_enabled 为 true。当前配置仍然写有旧项目绝对路径，因此可用性和可迁移性仍未完成。

### B.7 MCP 和网页

当前工作树没有找到真正的项目 MCP 服务实现，也没有找到可用的 MCP 配置文件。

当前没有运行中的独立网页控制台。旧的 cold-start 配置服务文件已在当前工作树中删除，但 Windows 启动快捷方式和一个阶段运行启动脚本仍然试图启动它。这些是旧启动残留，不是当前可用网页。

### B.8 Hermes Plugin 和本机 Hermes

当前本机 Hermes 目录实际能确认的直接内容主要是浏览器配置目录，没有确认到当前有效的 Hermes config.yaml、插件目录或可工作的项目 profile。

当前代码中仍有 Hermes 命名的桥接和 Feishu 载体绑定。工作区还有一份未跟踪、被忽略的类似 Hermes profile 的目录副本，包含 humanizer、llm-wiki 等技能名称。没有证据证明这份副本是当前正式运行必需组件；它更像历史或测试残留，但在确认没有外部启动者使用前不能本轮删除。

### B.9 知识库

项目根目录的 vault 是一个 Junction，实际指向：

    I:/Obsidian/创作助手/经验库

实际知识资料目录包括：

    I:/Obsidian/创作助手/经验库
    I:/Obsidian/创作助手/经验库/自媒体内容创作知识库

vault 下约有 1562 个 Markdown 文件，目录包括人设、模板、知识库、词表，以及知识库中的导航、账号与领域、素材、选题、自营内容、风格与偏好、经验、系统记录和归档。

现有目录已经把部分经验分成待确认和已确认，但没有证据表明所有正式业务规则都共享一个统一的发布版本号，也没有证据表明任意 Obsidian 修改会自动成为正式规则。当前知识汇合入口存在 audit、verify 和 apply 三种动作；apply 会通过 Core 生成或更新知识镜像。这说明知识库更接近 Core 数据的镜像和维护面，而不是正式业务真相源。

知识汇合入口还硬编码了 Obsidian 路径，并使用 Hermes 命名，因此后续要迁移的是“知识库概念和发布边界”，不是简单把 Obsidian 改名。

### B.10 Windows、WSL 和自动启动

当前 Windows 计划任务查询没有找到 Creation Assistant 相关任务。

但 Windows Startup 文件夹里有：

- CreationAssistant_Listener.vbs；
- CreationAssistant_Platform.lnk。

Listener 启动脚本指向 I:/Creation_assistant/scripts/feishu/listener.py。这个旧目录和脚本当前不存在，因此它是失效的旧入口。

Platform 快捷方式指向一台当前不存在的 Python 路径，并尝试启动已经删除的 cold_start_config_server 模块。阶段运行目录还有一个类似的隐藏启动脚本，仍然指向旧页面服务。因此当前 Windows 没有找到可用的计划任务，不代表没有自动启动残留。

WSL daily shell 仍然写着：

    /mnt/i/Creation_assistant-codex

并调用 Hermes 命名的 daily 入口。WSL 服务查询返回 E_ACCESSDENIED，因此本轮不能确认 WSL 发行版当前是否正在运行；只能确认这个脚本本身存在且路径仍然写死。

### B.11 当前正式运行目录

当前正式运行根目录是：

    I:/Creation_assistant-runtime

存储配置显示迁移状态为 completed，并明确要求：

- 正式运行根目录必须在构建目录之外；
- 只能有一个活动正式根目录；
- 错误根目录必须阻止正式操作；
- 切换后不能回退到旧目录；
- 复制、校验、切换和再次校验是一个完整动作。

正式数据库是：

    I:/Creation_assistant-runtime/formal/production_activation.sqlite3

该文件约 62 MB，最后写入时间为 2026-08-28 09:35:59。运行身份文件记录了正式数据库的 SHA-256 和迁移时重写的 2628 个旧路径值。

外置运行目录还包含正式备份、competitor registration、daily operations、daily repairs、MediaCrawler、transcripts、TrendRadar 以及 agent platform 状态和日志。

### B.12 正式数据库的主要表分类

当前正式数据库共发现 116 张表。以下是按当前字段、行数、状态和调用关系做的主要分类。分类是迁移判断，不代表本轮已经修改表。

#### 1. 明确的业务事实

这些表直接描述 Creation Assistant 的业务对象和业务结果：

- competitor_accounts、competitor_videos、baselines；
- hits、hit_comments、hit_deep_analysis、hit_transcripts；
- stage0_content_account、stage0_research_material；
- stage0_experience_candidate、stage0_confirmed_experience；
- stage1_question_expansion_source、stage1_question_expansion_qualification；
- stage1b_candidate_version、stage1b_candidate_support、stage1b_candidate_relation；
- stage1b_daily_snapshot、stage1b_candidate_pool_state；
- stage0_publication_registration、stage0_publication_observation；
- content_preference_profile、content_preference_revision；
- domain_search_tags、domain_search_page_observation、domain_search_activity_tag_registry。

当前可见的业务事实包括 50 个账号、2153 个视频、2760 个 baseline、647 个 hits、636 个转写、89 条研究资料和 36 个 stage1b 候选版本。它们原则上是独立化后仍有明确意义的数据。

#### 2. 技术执行事实

这些表描述一次业务怎样被执行、外部来源怎样被观察、模型或任务怎样产生结果：

- stage0_model_run、stage1b_model_run；
- stage0_experience_candidate_model_run；
- stage0_competitor_registration_model_run；
- stage0_competitor_breakdown_attempt；
- stage0_competitor_material_collection_checkpoint；
- stage1b_discovery_run、stage1b_run_domain_scope、stage1b_execution_context；
- stage1b_source_version、stage1b_filter_result；
- stage0_input_assembly、stage1b_input_assembly、stage1a_artifact_payload；
- daily_collection_account_completion；
- stage0_audio_production、stage0_audio_delivery、stage0_audio_decision；
- trendradar_collection_run、trendradar_hotspot_observation。

这些表不能全部删除，但要把业务事实和技术执行事实分开。尤其 model_run 可以记录“这次是谁用什么模型执行”，但不能成为 Core 选择下一次模型的依据。

#### 3. 历史和审计

这些表主要用于追溯，不应直接推进业务：

- audit_event、stage0_audit_event；
- command_receipt、stage0_command_receipt；
- object_reference；
- trace_root、trace_version；
- outbox_message；
- core_command_envelope；
- stage0_knowledge_mirror_run；
- publication 的历史审核和观察记录。

当前 audit_event 行数很少，但 stage0_audit_event 约 5108 行，command_receipt 约 2082 行，trace 相关记录约 43 组。它们是重要历史，不应被当作当前状态表。

#### 4. 当前能确认属于测试或非正式验证的内容

正式数据库中没有发现 data_identity=test 的记录，也没有发现单独的长期测试数据库。真正明确的测试数据主要在 tests 使用的内存数据库和临时目录里。

但 stage1b 的 real_daily_validation、Codex repair/conversion 等记录仍使用 production 身份。因此它们不能直接归入测试类；必须先由用户确认是正式事实、历史记录，还是应重分类到测试世界。

#### 5. 旧架构遗留或当前不应继续拥有正式控制权的内容

以下内容有较强的遗留或控制权风险：

- 已经失败或长期 processing 的 discovery、content node、cold-start 和 experience 状态；
- 旧 Hermes 模型绑定和旧 provider/model 记录；
- cold_start_background 相关的旧执行凭证和锁；
- scheduler_job、scheduler_job_attempt 当前没有正式业务行，但旧入口仍可能存在；
- core_permission、binding_manifest、content preference 等当前为空或没有形成活动主线的表；
- manual_exploration、stage0_manual_source、evidence card 等当前为空的旧机制表。

空表不等于必须删除；它们只能说明当前没有发现活动业务事实。是否退出要看代码引用、配置引用和未来正式设计。

#### 6. 当前无法仅凭现状判定的内容

以下表或记录需要结合用户业务认定：

- domain_1833831517eb 失败 cold-start 及其相关配置、账号和候选；
- real_daily_validation 记录；
- 仍处于 processing、running、awaiting_human_decision 的业务；
- 已完成 music_entertainment 业务是否仍是当前正式世界；
- 备份中的旧数据库是否仍需作为可恢复正式快照；
- 当前为空但可能被旧入口重新调用的机制表。

数据库分类结论是：正式数据库不应整体推倒重做；应保留业务事实，隔离测试和验证，冻结历史的控制权，并把执行身份从“模型选择”改成“审计事实”。

## C. 当前差距

### C.1 独立启动差距

数据库和部分业务 Core 已经能脱离项目根目录工作，但对外的主要启动入口仍然是 Hermes 命名和 Hermes 语义。还没有证明“没有 Hermes 进程、profile 和 Hermes 模型配置时，Creation Assistant 仍能独立启动并完成不依赖模型的正式能力”。

### C.2 模型边界差距

当前 daily、cold-start resume 和模型适配层都依赖 Hermes 执行绑定或 Hermes provider。Core 还不是模型无关的业务系统。

Codex 目前只在一个 workbench read-only 路线上出现，不能说明 Codex 已经成为正式业务执行者，也不能说明 Hermes 和 Codex 已经共享同一个 Core 接口。

### C.3 入口差距

当前至少存在 Hermes daily、直接 daily collection、直接 stage1b discovery、cold-start 管理、cold-start 后台、正式研究、daily repair、知识汇合、WSL daily 和 Windows 启动残留等入口或入口残片。

其中部分能写正式数据，部分能启动业务，部分虽然已失效仍可能被人或系统误认为有效。新旧入口没有完成退出，因此有双轨风险。

### C.4 正式/测试差距

正式数据库中只有 production 身份记录，没有发现独立的长期测试数据库。测试代码大量使用内存数据库和临时目录，这对单元测试是隔离的，但不等于已经存在一个完整、可重复的测试世界。

更危险的是，stage1b 记录中存在 real_daily_validation 和 Codex repair/conversion 产生的运行记录，但它们的 data_identity 仍是 production。也就是说，当前“验证”并没有可靠地等同于测试世界。

### C.5 状态和旧数据差距

正式数据库里仍有：

- 7 条 stage1b discovery processing 记录；
- 16 条 content node processing 记录；
- 1 条 experience run running 记录；
- 13 条 stage1b candidate absence 记录；
- 17 条 stage1b discovery failed 记录；
- 1 条 cold-start failed 记录；
- 旧后台执行记录；
- daily repairs 中至少 1 个仍为 running 的 attempt_state；
- 多种旧模型、provider 和 Hermes 执行身份。

这些记录可能只是历史，但只要 resume、完成判断、processing 判断或模型绑定逻辑仍会读取它们，它们就仍然拥有业务控制权。迁移前必须逐项确认“历史记录”和“当前可继续业务”。

### C.6 模型历史污染

正式数据库中能看到 mimo-v2.5-pro、stealth/ox-alpha、xiaomi/mimo-v2.5-pro、gpt-5.5、gpt-5.6-terra 等模型身份，provider_name 大多为 hermes。

这些身份可以作为审计历史保留，但不能被当作新业务的模型选择依据，也不能因为某次 resume 读取了旧的冻结绑定就暗中把新执行者限制为 Hermes 或旧模型。

### C.7 路径差距

仍然存在以下旧路径引用：

- I:/Creation_assistant-codex；
- /mnt/i/Creation_assistant-codex；
- I:/Creation_assistant；
- I:/Obsidian/创作助手/经验库；
- /home/caibin/.hermes/hermes-agent；
- /home/caibin/.hermes/profiles/creator；
- I:/api-key.txt；
- I:/Creation_assistant-codex/data/formal 下的音乐受众目录。

本轮不改路径。未来正式项目目录建议使用 I:/Creation-Assistant，但必须在独立迁移阶段执行，不能和本轮盘点混在一起。

## D. 旧资产处置总表

处置类别含义：

- 保留：新架构仍然需要，原样保留；
- 迁移：内容仍有效，但移动到新位置；
- 转换：内容有效，但职责变化，需要转换后保留；
- 历史保留：可以查看，但不能参与新的正式判断；
- 测试隔离：只能进入测试世界；
- 删除：迁移完成且确认无活动引用后退出；
- 待用户决定：会改变正式数据、正式行为或不可逆删除范围，不能由技术人员代替决定。

| 资产 | 当前真实位置 | 当前作用 | 是否仍活跃 | 最终处置 | 处置原因 | 处理阶段 |
|---|---|---|---|---|---|---|
| A. Core 源代码 | I:/Creation_assistant-codex/scripts/core | 数据库、正式流程、外部能力和业务规则 | 是 | 保留并转换 | 这是独立系统的业务基础，但要去掉 Hermes 专属业务绑定 | 阶段 1-2 |
| B. 正式数据库和 schema | I:/Creation_assistant-runtime/formal/production_activation.sqlite3 | 保存正式业务事实、正式状态、审计和执行记录 | 是 | 保留并迁移 | 正式数据价值高，当前已在项目外；后续只做受控迁移或原地接管 | 阶段 1、3 |
| C. 正式业务数据 | 正式数据库及 I:/Creation_assistant-runtime/formal 下的正式素材 | 账号、视频、候选、daily、cold-start、人工决定、研究资料 | 是 | 大部分保留；部分待用户决定 | 已经有大量正式事实，但失败现场、验证记录和未完成状态不能自动当作当前有效 | 阶段 3 |
| D. 测试数据 | tests、validation、临时目录、内存数据库、topic-structure-check-* 等 | 单元、集成、验证和临时试验 | 是，分散 | 测试隔离 | 当前测试多使用内存或临时库，但没有统一测试世界；验证记录已污染 production 身份 | 阶段 3 |
| E. 历史运行记录 | formal 数据库的 audit、model_run、command_receipt、stage0_audit，以及 agent_platform 日志 | 审计、错误追踪、执行历史 | 是，作为记录存在 | 历史保留 | 审计有价值，但不能继续控制 resume、完成状态或模型选择 | 阶段 3-4 |
| F. 业务配置 | config/runtime_storage.json、config/external_collection.yaml、config/model_routes.yaml、guardrail 和 domain pack | 存储根、外部能力、业务限制、模型路由和领域边界 | 是 | 转换 | 业务规则留在 Core；调用方模型、provider、凭据和 profile 要移到调用方 | 阶段 1-2 |
| G. 环境变量和环境文件 | .env、.env.example、.env.runtime.local | 密钥、模型、路径、ASR、音频和外部程序位置 | 是 | 转换；无用重复项最终删除 | 凭据和调用方模型不是 Core 责任；本地能力路径仍需保留在明确的外部能力配置中 | 阶段 1-2 |
| H. Hermes Plugin 和 profile | 本机 .hermes 浏览器目录、工作区未跟踪的 CUsers15891/.hermes 副本、Hermes 桥接代码 | 交互承载、浏览器状态、技能副本、载体绑定 | 部分可见，当前有效性未证实 | 转换后旧正式路线删除；无法确认的副本待用户决定 | Hermes 是外部使用者，不应拥有第二套业务状态；未确认引用前不能直接删除 | 阶段 2、8 |
| I. ModelGateway | scripts/core 的 model gateway、model_router、configured provider | 同时承担模型选择、模型调用、执行封装、结果处理和 provider/model 记录 | 是 | 先拆分，结构待定 | 模型选择和模型调用必须退出 Core；只保留经确认仍属于 Core 的结果接收、正式校验、执行记录和必要审计能力 | 阶段 2 |
| J. Hermes 专属逻辑 | scripts/agent_platform/hermes_*、HermesTaskModelBinding、Feishu/Hermes carrier marker | Hermes 到 Core 的桥接、当前模型绑定、Feishu 交互 | 是 | 转换后删除正式专属业务路径 | 部分桥接可以变成薄适配器，但不能长期拥有自己的业务流程 | 阶段 2、8 |
| K. Skill | runtime_skills/competitor_breakdown、source_to_topic、research_plan、内容生成和审核等目录 | 原子智能任务合同、输入输出和提示 | 是 | 保留并转换 | 当前 Skill 合同已经明确不负责正式状态，但 route metadata 仍绑定现有模型系统 | 阶段 2 |
| L. Obsidian 和 Markdown 知识资料 | vault Junction、I:/Obsidian/创作助手/经验库及其子目录 | 查看、编辑、经验沉淀和知识镜像 | 是 | 迁移概念；资料分层保留 | 知识库不是 Obsidian；正式规则必须有确认和发布，不由任意笔记直接控制业务 | 阶段 4、9 |
| M. 测试 | tests、validation 和测试入口 | 验证 Core、生命周期、隔离和格式 | 是 | 测试隔离 | 测试要有独立数据库和独立外部数据边界，不能用 production 身份做验证 | 阶段 3 |
| N. Windows 计划任务和启动项 | Windows Startup 文件夹；计划任务查询未找到匹配项 | 自动启动 listener、页面服务或旧平台 | 启动项残留；计划任务未发现 | 旧项删除；新项仅在新正式路线验证后决定 | 当前两项已指向旧路径或已删除服务，保留会造成误启动和双轨 | 阶段 8 |
| O. WSL 启动路径 | scripts/agent_platform/hermes_daily_entry.sh | 从 WSL 启动 Hermes 命名的 daily | 文件存在，运行状态未证实 | 转换或删除 | 它仍写死旧项目路径，且入口责任仍以 Hermes 为中心 | 阶段 1、8 |
| P. runtime 目录 | I:/Creation_assistant-runtime/formal、external、external_collection、agent_platform、model_service | 正式数据、采集原始材料、浏览器状态、日志、后台执行状态 | 是 | 正式部分保留；状态和缓存转换或历史保留 | 不能把正式业务数据、执行事实、浏览器缓存和临时状态混成一类 | 阶段 1、3、4 |
| Q. 临时文件和缓存 | .stage_runtime、.pytest_cache、.playwright-cli、__pycache__、topic-structure-check-* | 阶段探针、测试缓存、浏览器测试和临时验证 | 部分存在 | 测试隔离；确认无引用后删除 | 一般不属于正式业务，但当前不能在未检查引用前删除 | 阶段 3、8 |
| R. 旧设计文档 | docs 下历史基线、执行计划、当前决定状态等 | 设计说明、历史决策、实施记录 | 作为阅读材料存在 | 历史保留；未来指定唯一当前规则源 | 文档不能覆盖代码、数据库和真实启动事实，旧文档不能继续控制运行 | 阶段 1、8 |
| S. 旧兼容入口 | 直接 Python CLI、Hermes wrappers、WSL wrapper、旧启动项 | 直接启动或改变正式业务 | 是，至少有多个可写入口 | 转换后删除 | 长期双轨会导致不同入口拥有不同状态判断 | 阶段 1、2、8 |
| T. 当前正式运行入口 | Hermes daily、直接 daily、stage1b discovery、cold-start、研究、修复、知识汇合 | 实际写入正式状态或启动正式业务 | 是 | 收敛为 Core 正式业务入口，再由 MCP、网页和外部 Agent 调用 | 业务规则只能有一条正式主线 | 阶段 1-8 |

## E. 数据处置方案

### E.1 正式数据是否大部分保留

可以保留大部分正式数据，但不能把“保留”理解为“所有旧记录继续有效”。

当前正式数据库有明确业务价值的事实包括：

- 50 个 competitor accounts；
- 2153 个 competitor videos；
- 2760 个 baselines；
- 50 条 daily collection account completion；
- 647 个 hits；
- 8890 条 hit comments；
- 537 条 hit deep analysis；
- 636 条 hit transcripts；
- 50 个已完成 competitor registration；
- 1073 个已完成 registration items；
- 4 次 stage0 daily run；
- 2 个 cold-start，其中 1 个 completed、1 个 failed；
- 101 条人工决定命令；
- 89 条研究资料；
- 36 个 stage1b candidate version；
- 111 个 daily snapshot；
- 审计、命令回执、执行记录和错误记录。

这些内容不能本轮删除，也不应因为要独立化就全部重做。

### E.2 正式数据需要迁移什么

后续需要迁移或确认的不是简单复制文件，而是：

1. 正式数据库仍由唯一的正式运行根目录指向；
2. 所有正式业务入口都指向同一份数据库；
3. 外置正式素材、转写、采集结果和数据库之间的引用仍然有效；
4. 旧路径引用已经被确认并按迁移工具完成校验；
5. 运行身份文件、数据库摘要和实际数据库一致；
6. 新入口不会在项目目录偷偷创建第二份正式数据库。

当前 storage migration 已经记录 completed，不能据此推断整个独立化迁移已经完成；模型、入口、测试和运行状态仍未完成迁移。

### E.3 历史只保留查看

以下内容原则上保留查看，但不再参与新的正式业务判断：

- 旧模型和旧 provider 身份；
- 旧 Hermes 执行绑定；
- 失败的模型运行；
- 旧 audit、command receipt、stage0 audit；
- 已结束的后台 executor 记录；
- real_daily_validation 的运行事实，除非用户确认它们属于正式业务；
- 旧路径重写前后的迁移凭证；
- 旧设计文档和旧启动记录。

历史保留的关键规则是：存在不等于有控制权。

### E.4 需要测试隔离的内容

测试应只写测试数据库、内存数据库或明确的临时测试目录。测试账号、模拟候选、fixture、错误场景和删除重置操作不得进入正式数据库。

当前 tests 中大量使用 :memory:、TemporaryDirectory 和 data_identity=test，这说明不少测试已经有局部隔离。可是测试文件中也出现 data_identity=production 的辅助对象，且正式数据库中已有 real_daily_validation 记录。当前不能据此宣布整体隔离完成。

后续必须把：

- 测试数据库；
- 测试运行目录；
- 测试外部采集；
- 测试模型结果；
- 测试身份；
- 测试启动入口；

作为一个完整测试世界管理。不能只改一个字段。

### E.5 旧状态不能继续参与正式判断

在新正式运行开始前，以下状态必须经过逐项分类：

- stage1b discovery 的 processing、failed 和 real_daily_validation；
- content node 的 processing、failed 和 awaiting_human_review；
- experience run 的 running；
- daily repair 中仍为 running 的 attempt_state；
- cold-start executor 的 stopped、failed 和锁文件；
- 旧的模型冻结绑定；
- 旧的 domain 运行和候选池状态；
- 旧的生产身份验证记录。

分类结果只能是：

1. 仍属于某个正式 Business Run，并允许由新的 Execution 继续；
2. 已经结束，只能作为历史；
3. 实际是测试或验证，应迁到测试世界或由用户确认后作为历史；
4. 数据事实不可靠，需要用户决定；
5. 迁移完成后才允许删除。

本轮不改变这些状态。

### E.6 必须用户决定后才能删除或重分类的内容

以下事项不能由技术盘点替用户决定：

- domain_1833831517eb 这个失败 cold-start 及其泛科普-社会生活配置，是继续作为正式业务、转为历史，还是放弃；
- real_daily_validation 和 Codex repair/conversion 产生的 production 身份记录，是否被用户承认是正式业务事实；
- 已完成的 music_entertainment 正式数据是否继续作为当前正式世界，还是只作为历史资料；
- 旧备份、旧模型执行记录、旧 Hermes profile 副本和旧原始素材的不可逆删除范围；
- 当前正式数据库中未完成、失败或等待人工决定的业务，哪些可以关闭，哪些必须继续。

## F. 配置处置方案

### F.1 最终属于 Creation Assistant 的配置

以下责任应属于 Core：

- 唯一正式运行根目录和数据库位置；
- 正式/测试身份边界；
- schema 和数据库打开规则；
- 业务规则、状态转移、人工决定门槛；
- domain pack 的业务边界；
- 外部采集、ASR 和本地能力的调用契约；
- Skill 的输入输出契约；
- Business Run 和 Execution 的业务关联；
- 正式审计和错误记录规则。

Core 可以知道某项能力需要一个外部模型结果，但不能把 Hermes provider、Codex provider 或具体模型写成 Core 的业务选择。

### F.2 最终属于 Hermes 或 Codex 的配置

以下责任属于调用方或调用方运行环境：

- 当前使用的模型；
- provider、endpoint、API token；
- Hermes profile；
- Codex CLI 或 app server 路径；
- Codex 会话模型；
- Feishu、浏览器、聊天和消息发送配置；
- 调用方自己的沙箱和本地工具。

Core 可以记录“本次由谁、以什么模型执行”的审计事实，但不能用这条历史事实替未来任务选择执行者。

### F.3 当前需要转换的配置

当前配置里仍有：

- active_provider_ref；
- mimo_main；
- relay_main；
- provider_name: hermes；
- HermesModelProviderAdapter；
- HERMES_BUSINESS_MODEL_*；
- GPT_RELAY_*；
- CODEX_WORKBENCH_*；
- CREATION_LLM_PROVIDER；
- CREATION_LLM_MODEL；
- CREATION_LLM_FALLBACK_ENABLED；
- Feishu 变量；
- 项目绝对路径；
- runtime 绝对路径。

后续要把它们拆成三类：

1. Core 的业务和存储配置；
2. 外部能力的位置和调用配置；
3. Hermes/Codex 各自的模型、凭据和交互配置。

当前不能因为有兼容需要就无限期保留重复的 CREATION_LLM_* 和 HERMES_BUSINESS_* 两套模型入口。等正式新入口完成验证、活动引用清零后，旧配置应退出。

### F.4 不应自动加入的机制

本次基线不新增 retry、fallback、cache、并发上限、自动恢复、自动跳过、自动降级、消息队列、权限系统、微服务、通用插件框架或新的调度器。现有配置中 fallback 已明确为 none；后续若要改变，必须有新的有效设计或真实故障依据。

## G. 正式入口迁移

### G.1 当前可以启动或改变正式业务的入口

当前可确认的入口分为以下几类：

1. Hermes daily one-shot：可以启动正式 daily。
2. 直接 daily collection once：可以直接启动正式 daily 协调流程。
3. 直接 stage1b daily discovery：可以用 production_daily 或 real_daily_validation 等模式写入正式身份记录。
4. Hermes cold-start management：可以预览、确认、查看状态、停止、恢复和人工审核 cold-start。
5. cold-start background entry：可以在后台执行 cold-start。
6. Hermes formal research：可以建立、执行、批准、退回、重试和修订正式研究。
7. Hermes daily repair：可以计划和应用正式 daily 观察修复。
8. Hermes knowledge convergence：audit、verify 为查看；apply 会把结果写回 Core 并生成知识镜像。
9. WSL Hermes daily shell：仍然是旧项目路径下的启动入口，运行状态未确认。
10. Windows Startup listener：当前指向不存在的旧目录和脚本，属于失效入口。
11. Windows Platform shortcut：当前指向不存在的 Python 和已删除页面服务，属于失效入口。
12. 当前没有确认到可用 MCP 入口。
13. 当前没有确认到可用网页入口。

### G.2 最终正式入口

最终只保留一套 Core 正式业务主线：

- daily 由 Core 的 daily 正式入口负责；
- cold-start 由 Core 的 cold-start 正式入口负责；
- resume 由 Core 按 Business Run 和 Execution 规则负责；
- 人工确认由 Core 的正式人工决定入口负责；
- 研究、候选、内容和修复都回到 Core；
- Hermes、Codex、MCP 和网页都只是调用这些同一入口。

外部采集和 ASR 可以有独立的外部能力进程，但它们不能形成第二套业务状态。

### G.3 旧入口退出条件

旧正式路线只有在以下条件全部满足后才能退出：

1. 新的 Core 正式入口能覆盖原入口的正式能力；
2. daily、cold-start、resume、人工确认、研究和修复的状态都回到同一个 Core；
3. Hermes 和 Codex 都能通过同一个标准接口提交自己的模型结果；
4. 正式数据库和测试数据库已经可靠隔离；
5. 新入口能读到已有正式数据，并且历史不会暗中控制新运行；
6. 已经完成真实、受控、可追溯的正式端到端验收；
7. 已经确认旧入口没有活动调用方；
8. 已经准备好可回看的迁移凭证。

本轮不做新旧入口切换，也不做双轨长期运行设计。

## H. 唯一总体迁移方案和阶段

总体采用单向十阶段，编号为阶段 0 到阶段 9。每个阶段完成后再进入下一阶段；阶段 0、阶段 1A、阶段 1B-1 和阶段 1B-2 已完成，本轮不进入阶段 2。

### 阶段 0：封存迁移起点

目标：

在修改系统前，封存一个可以回答“迁移前代码、未提交修改、正式数据库、正式运行目录、配置、入口、旧路径和已有异常分别是什么”的真实起点。

改什么：

- 修订本迁移基线；
- 读取并记录 branch、HEAD、git status、修改/删除/未跟踪/重要 ignored 内容；
- 读取并记录正式数据库摘要、schema 摘要、关键表行数、关键状态和迁移前备份；
- 读取并记录正式 runtime 目录、身份文件、日志、后台状态和残留状态；
- 读取并记录配置、环境变量名称、启动项、入口和绝对路径；
- 在项目内新增 migration_snapshot/ 证据目录，但不把正式数据库复制进项目目录。

不改什么：

- 不修改代码、数据库、schema、配置、环境变量、Hermes、Plugin、Skill、Obsidian、Windows、WSL 或项目目录名；
- 不 commit、reset、stash、checkout、clean 或删除临时文件；
- 不启动正式业务、不调用模型、不执行 MCP、网页或独立 Core 改造；
- 不清理 failed、processing、running，不修正式记录，不迁移正式数据；
- 不整体复制大体积正式采集素材。

真实完成标准：

- 基线已经写入迁移保护期、自动触发与模型执行分离、Skill 单一来源、身份边界、目录迁移阶段和 ModelGateway 拆分原则；
- 项目现场快照可追溯，且快照记录的是封存前的脏工作树事实；
- 正式数据库有不覆盖旧备份的迁移前备份；
- 原库操作前后摘要、大小和 schema 摘要一致；
- 正式运行目录有清单，配置有清单，正式入口有清单，旧路径有清单；
- 已明确当前异常，但没有为了整理异常改变正式事实；
- 迁移保护期规则已生效，后续不再启动新的正式业务。

该阶段退出的旧东西：

- 不删除任何旧资产；
- 只把迁移起点从“未封存状态”变为“有凭证的封存状态”。

### 阶段 1：锁定独立 Core 的启动和存储边界

目标：

让 Creation Assistant Core 能在不依赖 Hermes 启动的情况下独立打开数据库、读取正式状态，并在安全的测试世界完成不依赖模型的 Core 能力验证。

改什么：

- 确认唯一 Core 启动边界；
- 把正式数据库、正式运行目录和测试目录关系写成唯一有效事实；
- 把 daily、cold-start、resume、人工决定的正式入口收回 Core；
- 确认外部采集和 ASR 只是被调用能力；
- 盘点并封存仍会直接写正式数据的旧入口。

不改什么：

- 不在本阶段选择新模型；
- 不把 Hermes 或 Codex 变成 Core 的内置 provider；
- 不删除正式数据；
- 不把历史状态自动改成完成；
- 不启动真实 daily 或真实 cold-start；
- 不重命名项目目录。

真实完成标准：

- 没有 Hermes 进程、profile 或 Hermes 专属启动服务时，Core 仍能独立启动；
- Core 打开的正式数据库唯一且可验证；
- 测试启动不会触碰正式数据库；
- 正式业务能力只有一条 Core 主线；
- 旧入口的活动引用、写入能力和退出边界已经有证据。

该阶段退出的旧东西：

- 旧的“由 Hermes 负责启动业务”的假设；
- 直接绕过 Core 主线的正式写入入口；
- 失效启动项不会在新路线中继续承担任何职责，但实际删除留到阶段 8。

#### 阶段 1A 实际完成情况（2026-08-28）

阶段 1A 只验证独立 Core 启动与存储边界，已经完成。阶段 1B-1 完成了正式业务入口边界和隔离验证；阶段 1B-2 完成了旧正式入口改薄。本轮仍不删除旧入口、不切换 WSL/Windows 残留、不进入阶段 2。

本轮新增了一个最小的 Creation Assistant Core 独立入口。它只加载 runtime storage 配置和标准数据库能力，不导入 agent_platform、model gateway、Hermes 业务入口、Hermes profile、Hermes Plugin、Feishu 或任何模型适配器。

该入口实际完成了：

- 找到唯一正式 runtime：I:/Creation_assistant-runtime；
- 找到唯一正式数据库：I:/Creation_assistant-runtime/formal/production_activation.sqlite3；
- 校验 runtime identity receipt 的路径和正式身份；
- 使用 SQLite 只读方式打开正式数据库；
- 读取 116 张表的 schema 数量和 daily、cold-start、candidate discovery、content task 基本状态；
- 明确报告启动不需要模型，模型任务以后需要外部 Agent；
- 明确不自动启动 daily、cold-start、resume、candidate discovery、research、repair 或 knowledge apply。

验证结果：

- 测试世界测试 3 项全部通过，使用临时数据库和内存数据库；
- 真实正式库只读启动成功，正常退出；
- 启动过程中 Hermes 未启动、Hermes profile 未读取、Hermes Plugin 未读取、ModelGateway 未调用、没有模型调用；
- 正式数据库验证前后 SHA-256 都是 5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD；
- 没有创建 Business Run、Execution、audit、command receipt、model run 或其它正式记录；
- 没有创建第二份正式数据库或新的正式 runtime。

当前 Core 可以独立存在，8 条已确认的活跃旧正式入口已经切换为薄调用入口。以下仍留待后续阶段：

- 旧模型绑定和 ModelGateway 的职责拆分；
- MCP、production/test 隔离和项目目录迁移。

#### 阶段 1B-1 实际完成情况：建立唯一 Core 正式业务入口边界（2026-08-28）

本子阶段解决的是“正式业务判断归谁”，不是重写 daily、cold-start、resume 或人工决定，也不是把旧 Hermes 入口删除。新建的 Core 边界位于 `scripts/core/formal_business_entrypoints.py`，它只接收一个已经打开的 Core，并把正式决定交给现有 Core 业务方法；它不认识 Hermes、Codex、MCP、网页或调度器，也不自己选择模型。

本轮建立的正式能力边界如下：

| 正式能力 | Core 边界 | 仍然复用的现有规则 |
|---|---|---|
| daily | `request_daily`、`resume_daily`、`finish_daily` | daily run 按领域和业务日期唯一；完成的不再重建；failed/stopped 只能按明确 resume 继续；生命周期由现有 Core 校验 |
| cold-start | `start_cold_start`、`resume_cold_start`、`stop_cold_start` | 一个确认配置对应一个 cold-start run；恢复使用原 run；模型绑定仍是现有阶段的输入，模型拆分留到阶段 2 |
| 人工决定 | `submit_human_decision` | 先由 Core 接收并校验正式动作，再由已有 Core 服务完成结果登记；传输层不能直接把决定写成完成 |
| 候选决定 | `record_discovery_decision`、`select_discovery_candidate` | 候选选择、接受和拒绝继续由 Core 校验和写入 |
| formal research | `execute_formal_research` | 研究任务、计划版本、审批、退回和重试统一由 Core 判断；旧入口只做请求转换和当前模型承载 |
| daily repair | `validate_daily_repair_request`、`plan_daily_repair`、`apply_daily_repair` | 修复日期资格、修复计划和正式观察写入统一由 Core 判断；旧入口只负责外部采集、备份和结果返回 |
| knowledge apply | `apply_knowledge_convergence` | 知识汇合的正式删除和写入统一由 Core 判断；镜像输出仍是外部资料动作 |

这层边界现在既是隔离测试落点，也是 8 条活跃旧正式入口的唯一正式业务落点。旧入口没有删除，仍保留参数接收、输入转换、外部执行承载、日志和结果返回；正式业务判断和正式状态变更不再由旧入口完成。

##### 历史迁移摘要的控制权检查

阶段 0 记录的旧摘要实际保存在正式 runtime 的 `runtime_identity.json` 中。当前摘要是迁移时复制校验留下的历史值，与当前正式数据库摘要不同。运行时读取它的地方是 runtime storage 的身份凭证检查和独立 Core 的状态报告。

检查结果：

- 它只校验正式 runtime 根目录、正式数据库路径、凭证结构和“迁移时摘要是否仍等于当前文件”的历史信息；
- 它不选择数据库，不创建或选择 Business Run，不改变 daily 或 cold-start lifecycle，不决定 resume，不选择 provider 或模型；
- 正式运行保护检查使用的是不可变的 runtime 根目录、正式数据库路径和凭证存在性，不把这个历史摘要当作实时写入锁；
- 因此它目前只是历史迁移凭证，不拥有正式业务控制权；本轮没有修改正式数据库或该凭证。

##### 当前正式写入口和迁移映射

按“能够被外部启动或调用，并可能改变正式业务”统计，当前确认有 8 条可调用的正式写路径，另有 1 条 WSL 包装路径的运行状态未确认；两个 Windows 启动残留已经指向不存在的旧目标，不计入可运行入口。Core 内部的人工作务服务是业务能力，不另计为一个外部入口。

| 旧入口 | 1B-2 前的业务判断 | 1B-2 后调用的 Core 能力 | 仍保留职责 | 是否仍可直接写正式业务状态 | 是否仍包含模型执行职责 | 最终退出阶段 |
|---|---|---|---|---|---|---|
| Hermes daily one-shot | 外层判断领域、日期、已有 run、完成/失败/停止、resume 和生命周期 | daily 请求规范化、执行准备、执行聚合、生命周期收口 | 传参、日志、当前模型绑定、采集承载和结果返回 | 否 | 是 | 阶段 8 |
| direct daily collection once | 与 Hermes daily 共用 coordinator，但自身仍可单独启动 | 与 Hermes daily 使用同一 daily Core 能力 | 参数解析、当前模型绑定、采集承载和结果返回 | 否 | 是 | 阶段 8 |
| direct stage1b daily discovery | 外层和服务层判断执行模式、来源范围、复用和最终状态 | discovery 执行、快照、候选交接、资格和收口 | 参数转换、外部来源承载和结果返回 | 否 | 是 | 阶段 8 |
| Hermes cold-start management | 外层参与操作许可、状态相关确认和 resume 入口选择 | cold-start 操作校验、人工作务提交和生命周期能力 | Hermes 传输、会话、显式确认和后台能力注入 | 否 | 否 | 阶段 8 |
| cold-start background | 异常处理和退出逻辑可能决定是否写入失败状态 | 后台资格校验、执行、生命周期读取和失败收口 | 进程、回执、心跳、通知和模型执行承载 | 否 | 是 | 阶段 8 |
| Hermes formal research | 外层按任务状态分支决定 retry/revise 和具体业务动作 | formal research 统一动作分派和任务状态判断 | 请求转换、当前研究模型承载和结果返回 | 否 | 是 | 阶段 8 |
| Hermes daily repair | 外层判断修复日期资格并直接组织正式应用 | 修复请求资格、计划和正式应用 | 外部采集、备份、验证和结果返回 | 否 | 否 | 阶段 8 |
| Hermes knowledge convergence | 外层触发正式 apply 并直接调用汇合方法 | knowledge convergence 正式应用 | 只读审计、镜像导出、参数转换和结果返回 | 否 | 否 | 阶段 8/9 |
| WSL Hermes daily shell | 未确认；本轮不读取其运行链 | 未处理，保持原样 | 旧路径包装；本轮不改 | 未确认 | 未确认 | 阶段 8 删除 |

当前入口的共同事实是：它们现在通过 Core 正式边界进入已有业务能力；外层不再决定“是否开始、是否恢复、用哪个 run、如何结束”，也不再直接拥有正式写入权。本轮没有删除旧正式入口，没有修改 WSL 和 Windows 启动残留。

##### 当前入口逐项结果

下面记录改薄后的逐项确认。“不能绕过新边界”指正式业务动作必须先进入 Core 正式边界；旧入口仍可承载外部执行和只读报告。

- Hermes daily one-shot：不能自己创建或选择 Business Run，不能自己决定 resume、复用或生命周期，不能正式确认人工决定，也不能绕过 Core 写正式状态；仍保留调度承载、模型绑定、采集、日志和结果返回。
- direct daily collection once：与 Hermes daily 进入同一套 Core daily 规则；上述七项业务判断均不在入口内；仍保留直接启动和执行承载。
- direct stage1b daily discovery：执行模式和输入格式属于请求转换；正式 discovery run、来源复用、候选决定、最终收口和写入均经 Core；不创建替代父 Business Run。
- Hermes cold-start management：操作白名单、会话和显式确认属于传输层；当前 run、resume、停止和正式人工决定均由 Core 能力处理；不直接写正式状态。
- cold-start background entry：只按明确 configuration/run 执行外部任务；程序异常只通过 Core 的失败收口能力提交，不自行写 lifecycle，不创建替代 run，不自动 resume 或跳过步骤。
- Hermes formal research：只做请求解码和当前模型承载；retry/revise、任务定位、计划和结果状态均由 Core 正式研究能力处理。
- Hermes daily repair：只做日期参数转换、外部采集、备份、只读验证和结果返回；修复资格、目标计划和正式观察写入均由 Core 处理。
- Hermes knowledge convergence：只读审计和镜像导出仍在入口；正式 apply 通过 Core 汇合能力完成，入口不拥有正式删除或写入权。
- WSL Hermes daily shell：本轮不处理，保持原样；其运行状态和是否仍能间接触发正式业务仍未确认。

Core 内部的 `HumanDecisionCommandService` 是正式业务服务，不是另一条外部入口。外部人工作务传输现在经 Core 正式边界进入该服务；后台技术异常也通过 Core 的生命周期收口能力处理，不形成第二套状态规则。

##### 五个隔离业务事实验证

本轮测试只使用临时测试数据库，未打开正式数据库写入：

1. 同一天同领域连续两次 daily 请求只得到同一个 Business Run；
2. failed Business Run resume 后仍是原 Business Run，没有创建第二个；
3. completed Business Run 再次请求只复用完成事实，不新建 run；
4. 人工决定必须经过 Core 的正式动作和命令记录边界，随后才能完成；
5. completed daily 被要求转回 running 时被 Core 拒绝。

这五项证明新 Core 边界的正式判断不是由调用者自己决定。它们不等于真实正式业务验收，也没有启动正式业务。

隔离测试实际结果：核心业务边界相关测试 42 项通过，冷启动后台承载相关测试 21 项通过，扩展冷启动和入口薄化检查 84 项通过。测试使用临时测试数据库，没有写入正式数据库，也没有调用模型或正式外部采集。

#### 阶段 1B-2 实际完成情况：旧正式入口改薄（2026-08-28）

本轮实际处理 8 条仍活跃、能够改变正式业务的旧正式入口：Hermes daily、direct daily、direct stage1b daily discovery、Hermes cold-start management、cold-start background、Hermes formal research、Hermes daily repair、Hermes knowledge convergence。旧入口全部保留，但已经只承担参数接收、输入转换、外部执行承载、日志或结果返回。

改薄后的共同规则：

- daily 的 Business Run 创建、复用、resume、执行准备和 completed/failed/stopped 收口统一进入 Core；Hermes daily 和 direct daily 共用同一条 Core 规则。
- cold-start 的状态校验、resume、停止、后台执行资格和异常失败收口统一进入 Core；后台进程本身不创建替代 run、不自动 resume、不跳过步骤。
- 人工决定由传输层提交，是否正式接受和命令结果记录由 Core 决定。
- research、repair、knowledge apply 的正式动作均经 Core 正式能力；模型调用、外部采集和知识镜像仍保留在允许的承载位置。
- Core 不再反向加载旧 Hermes/后台入口；后台技术能力由旧入口注入 Core，依赖方向保持为“旧入口到 Core”。
- WSL 包装入口和 Windows 启动残留本轮原样保留；没有开始 MCP，没有改变 ModelGateway 整体边界，没有运行真实业务。

阶段 1 整体完成状态：阶段 1A、1B-1、1B-2 均已完成，阶段 1可以正式关闭；阶段 2A已完成，阶段 2B尚未开始，后续不得在本轮自动进入。

### 阶段 2：把模型执行权交还给调用者，并建立 MCP

#### 阶段 2A 实际完成情况：智能任务执行权从 Core 中分离（2026-08-28）

本轮只把“每日来源转选题”作为代表性正式智能链接入外部执行边界，没有接入 MCP、没有调用真实模型、没有运行正式业务。

本轮形成的最小交接方式：

- Core 先从当前 processing 的 Business Run、来源版本和输入组装中准备一项智能任务；任务只带完成这一项工作所需的当前正式 Skill、输入材料、来源身份、领域限制和结构化输出要求。
- 任务包不带模型、provider 或具体执行者选择。Skill 仍只有项目内当前正式 Skill 这一份来源，任务包只是把它作为内容和正式引用交给外部执行者。
- 外部执行者只提交结构化结果和执行审计事实；不能创建 Business Run，不能决定 resume、lifecycle、复用、人工决定或下一步业务。
- Core 验证提交结果的输入材料是否仍对应当前组装、必填字段和结构是否完整、来源证据是否来自已提供材料、来源版本是否匹配、领域和风险限制是否满足；通过后才写入测试身份下的候选结果。
- 没有外部执行者时，Core 返回“当前需要外部智能执行”，保留现有 processing 事实，不把业务伪装成 completed，不调用旧模型路线，不自动 fallback 或 retry。
- 外部执行者和模型身份只作为本次执行的审计事实记录，不决定下一次 Execution，也不创建第二个 Business Run。已有的旧模型记录不会成为新执行者选择依据。

智能任务迁移表：

| 当前正式智能任务类别 | 2A状态 | Core 当前边界 | 后续处理 |
| --- | --- | --- | --- |
| source_to_topic / daily candidate discovery（含 historical_high_signal 等来源输入） | 已迁移 | Core准备最小任务、接收结构化结果、正式校验并继续候选流程 | 2B通过 MCP 暴露同一边界 |
| competitor_breakdown（竞品注册与 daily hit） | 2B-2 第一轮已迁移 | Core 生成最小外部任务，外部执行者提交结构化结果，正式结果仍须回到 Core 校验 | 已完成第一轮，内容生产三类留给第二轮 |
| research_plan / formal research | 2B-2 第一轮已迁移 | 研究计划和 deep research 均等待外部智能结果；研究的正式判断和写入仍归 Core | 已完成第一轮，内容生产三类留给第二轮 |
| content plan、内容生成、审核 | 待迁移 | 当前仍保留既有模型承载路径；内容正式状态仍归 Core | 沿用 2A边界继续拆分 |
| experience candidate proposal | 2B-2 第一轮已迁移 | Core 生成外部任务，合法提案仍停留在等待人工决定，不能自动成为正式规则 | 已完成第一轮，内容生产三类留给第二轮 |
| cold-start domain boundary / preflight 智能判断 | 2B-2 第一轮已迁移 | 仅迁移其中的竞品拆解智能判断；cold-start 生命周期、resume、人工确认和完成条件仍归 Core | 已完成第一轮，内容生产三类留给第二轮 |

本轮退出 Core 的职责只包括代表链范围内的模型选择、provider 选择和模型 API 调用；代表链仍保留现有的结果结构校验、来源语义校验、正式写入和审计记录。原有模型承载结构没有整体删除，也没有复制出第二份 Skill 或第二套智能任务生命周期。

阶段 2A 完成状态：代表性正式智能链已完成外部结果交接和 Core 正式验收；其它当前智能任务已逐项标注，阶段 2B尚未开始。

目标：

让 Hermes 和 Codex 都能连接同一个 Core；谁执行智能任务，谁提供自己的当前模型和结果；Core 不选择 provider、模型或 fallback。

改什么：

- 把现有模型网关转换为调用者提供执行信息、Core 接收结果和记录审计的边界；
- 把 HermesTaskModelBinding 从 Core 的业务前提转换为外部执行事实；
- 建立薄 MCP 接口；
- 让 Hermes 和 Codex 的入口都调用同一 Core；
- 保留必要的执行身份记录，但不让旧身份控制新运行。

不改什么：

- MCP 不重新实现 daily、resume 或人工确认；
- 不新增第二套状态；
- 不自动切换模型；
- 不增加 fallback、retry 或缓存；
- 不让 Hermes 正式运行修改项目文件；
- 不在本阶段开发网页。

真实完成标准：

- 同一个 Core 能接收 Hermes 和 Codex 的智能任务；
- 两个调用者使用各自当前模型；
- Core 不读取 Hermes 环境变量来决定 Codex 或新业务的模型；
- MCP 只是接口，没有自己的业务数据库和生命周期；
- 失败结果能回到 Core 的正式规则处理，而不是由 MCP 私自恢复。

该阶段退出的旧东西：

- Core 里的 Hermes 模型选择责任；
- 以 Hermes provider 为默认业务事实的旧模型路由；
- Hermes 专属的第二套业务状态。

#### 阶段 2B-1 实际进展：Creation Assistant MCP（2026-08-28）

本轮只把阶段 2A 已完成的 `source_to_topic / daily candidate discovery` 代表链通过 Creation Assistant 自己的 MCP 暴露出来，没有迁移其它七类智能任务，没有开始 2B-2。

已实现的最小 MCP 能力：

- 读取连接的 Core 基本事实；
- 按已有 Core 标识获取一项 `source_to_topic` 外部智能任务；
- 提交结构化结果和执行审计事实；
- 读取 Core 对该次外部执行的处理结果。

MCP 没有自己的数据库、业务状态、任务队列或生命周期。取任务时，Skill、输入材料、来源身份、领域限制和输出要求都由 Creation Assistant 当前正式 Skill 和 Core 已有输入组装生成；客户端不能提交一份替代 Skill 或自行拼装正式材料。任务不包含模型、provider 或模型选择。

结果提交只接受结构化字段和执行事实，仍经现有 `source_to_topic` Core 校验；非法字段、非法来源或不符合业务限制的结果会被拒绝，MCP 不做 retry、fallback、换模型、建新 Business Run 或自动跳过。

隔离测试已用真实 stdio JSON-RPC 进程验证 MCP 初始化、工具发现、取任务、结构化提交、非法结果拒绝，以及同一 Business Run 先后接受 Hermes 和 Codex 执行事实。测试只使用临时测试数据库；没有正式业务运行和真实模型结果。

最终客户端验收事实：

- Hermes 0.20.5 使用 MiMo v2.5 Pro，已真实发现 Creation Assistant MCP、获取 `source_to_topic` 任务、使用 Creation Assistant 正式 Skill 完成执行、通过结构化提交，并由 Core 接受结果。
- WSL Codex 0.150.1 的 MCP Server discovery、启用状态和协议层 `tools/list` 已确认；但 GPT-5.5 和 GPT-5.6 Sol 的新会话都没有把四个 Creation Assistant 工具注入模型可见工具集合，因此 Codex 的 `source_to_topic` 端到端尚未完成。
- Codex 的兼容状态记录为“待复验”，问题边界是 Codex 客户端 MCP 工具注入，不是 Creation Assistant 不支持 Codex，也不是 Creation Assistant MCP 失败。没有为 Codex 增加私有 MCP、Skill、任务格式、fallback、模型或业务流程。

阶段 2B-1 正式完成：Creation Assistant 已建立独立于 Hermes 和 Codex 的标准 MCP 边界，并已通过 Hermes 真实完整端到端证明外部执行者可以使用同一个 Core。Codex 的客户端工具注入问题作为后续兼容性复验事项记录，不重新打开阶段 2B-1，除非以后发现 Creation Assistant 自身存在协议或业务问题。

Codex 后续只需重新验证：状态工具、`get_external_task`、当前 Codex 模型执行、结构化 `submit` 和 Core 接受；不得为当前客户端问题新增专用兼容逻辑。阶段 2B-2 仍待迁移的七类是：`competitor_breakdown`、`research`、内容规划、内容生成、审核、经验候选提案、cold-start 智能判断。

#### 阶段 2B-2 第一轮实际进展：分析与判断型智能路径（2026-08-29）

本轮只迁移四类正式智能路径：`competitor_breakdown`、`research`、经验候选提案和 cold-start 智能判断。四类均复用阶段 2A 已成立的同一条边界：Core 准备当前正式 Skill、最小充分材料和约束，外部执行者返回结构化字段，Core 负责正式校验、审计和业务状态推进。

- `competitor_breakdown`：竞品注册和每日竞品拆解不再由正式路径主动选择或调用模型；外部任务保留来源身份、原始材料、领域限制和严格证据约束，结果仍由 Core 校验后写入对应拆解事实。
- `research`：研究计划和已有研究材料上的 deep research 智能步骤不再由正式路径主动调用模型；没有外部执行者时明确等待，不会偷偷调用旧 ModelGateway。
- 经验候选提案：外部执行者只能生成候选提案；Core 仍把合法结果置于等待人工决定，不能自动变成正式经验或修改正式 Skill。
- cold-start 智能判断：本轮只迁移 cold-start 注册过程中的竞品拆解判断，cold-start 的 lifecycle、resume、人工确认和完成条件没有改变。

四类任务包均不包含模型、provider、fallback 或 retry 选择；executor 和实际模型只作为执行审计事实。历史模型记录不会决定下一次执行者。结果只能通过 Core 的结构化校验入口进入正式业务事实；没有外部执行者时，当前步骤保持等待外部智能执行，不建立第二套任务生命周期。

本轮未迁移内容规划、内容生成和审核，三类留待阶段 2B-2 第二轮。未新增 MCP、任务队列、模型路由、兼容路线或第二套业务状态，也未运行正式业务和真实模型。

#### 阶段 2B-2 第二轮实际进展：内容生产链智能执行权迁移（2026-08-29）

本轮完成内容规划、内容生成和审核三类正式智能步骤的外部执行迁移。内容服务继续由 Core 按原有顺序准备选题、研究结果、规划版本、正文版本和审核版本；每一步只把当前 Skill、必要材料、来源身份、业务限制和结构化输出要求交给外部执行者。

- 内容规划使用 `content_plan_generation`，外部结果保留规划文本和必要结构，Core 校验当前内容任务、选题对应关系、必填内容和业务限制。
- 内容生成覆盖正式成稿、文案优化和去模板化，分别使用现有正式 Skill；正文以结构化结果中的明确文本字段提交，Core 校验当前计划、内容任务和版本关系。
- 审核使用 `final_content_review`，外部执行者只提交审核结果和问题；Core 校验审核对象和必要字段，审核结果不会直接覆盖正文，也不会自动形成最终稿。
- 三类均支持无外部执行者时停在“等待外部智能执行”，不会调用旧 ModelGateway、自动跳过、自动重试或新增审核循环。

本轮没有改变内容生产顺序、人工确认、版本关系或正式生命周期，没有新增 MCP、任务队列或第二套状态。隔离组合链已验证“规划—生成—审核”始终回到同一 Core 业务主线；未运行正式业务，未调用真实模型，正式数据库摘要保持不变。

### 阶段 2：完成

阶段 2 完成后的正式架构事实：

- Core 负责当前业务步骤、是否需要智能工作、Business Run、lifecycle、resume、正式结果是否接受、人工决定和正式状态推进。
- 正式业务不再由 Core 选择 LLM、选择 provider、主动调用 LLM 或自动 fallback 到其它模型。
- 正式智能工作统一采用“Core 产生 external task—外部 Agent 使用自己的当前模型执行—结构化结果提交—Core 校验—Core 继续业务流程”。
- 当前已迁移的正式智能路径包括 source_to_topic、competitor_breakdown、research、experience proposal、cold-start 智能判断、content planning、content generation 和 review。
- 正式 Skill 只有 Creation Assistant 内部一份当前来源。
- Hermes 不是 Creation Assistant 正式业务成立的必要依赖。
- ModelGateway 已退出正式可达业务中的模型选择、provider 选择和模型调用；历史及测试辅助代码暂时保留，不代表仍属于正式业务。

阶段 2B-2 第二轮完成。阶段 2 已正式关闭；后续 Git 可信提交作为本次收口节点建立。

阶段 2 收口前的迁移保护事件记录：2026-08-29 00:00:01—00:11:26 UTC，旧 `daily_collection_once / daily-once` 自动路径仍以 production 身份启动正式 daily，产生两条 daily run、正式采集和 discovery 记录。该事件证明旧自动调度尚未完全退出，但不推翻阶段 2 的模型执行权迁移结论；本事件产生的正式数据不在本轮删除或恢复。

文件摘要记录：`5262A76E...` 是阶段 0 迁移起点，`33E2B8D7...` 是第一次旧 daily 触碰后的文件摘要，`09B245CD...` 是本次自动 daily 写入后的当前文件摘要。当前正式数据库与阶段 0 备份相比已经存在正式业务记录差异，因此 `09B245CD...` 只作为当前观察事实记录，不作为干净迁移监测基准。

### 阶段 3：正式数据和测试数据物理隔离，并清理状态控制权

目标：

让正式世界、测试世界、验证世界和历史世界各自有清晰边界。

改什么：

- 建立独立测试数据库和测试运行目录；
- 让测试身份在边界上不能打开正式数据库；
- 对 real_daily_validation、Codex repair/conversion 和其它验证记录逐项重分类；
- 对 processing、running、failed、awaiting decision 记录逐项判定是否仍属于正式 Business Run；
- 让历史模型和历史 provider 只用于查看和审计。

不改什么：

- 不自动删除旧数据；
- 不自动关闭正式失败或等待人工决定的业务；
- 不把测试结果写回正式数据；
- 不为了隔离重做所有正式业务数据；
- 不修改用户尚未决定的业务事实。

真实完成标准：

- 生产和测试有明确不同的数据库和运行根目录；
- 边界会阻止测试打开正式数据库；
- 所有验证运行都有明确测试或历史身份；
- resume、完成、processing、模型和 provider 判断不会读取历史记录作为当前命令；
- 用户需要决定的记录已经单独列出。

该阶段退出的旧东西：

- “real_daily_validation 但 data_identity=production”的混合做法；
- 测试入口直接写正式身份的做法；
- 旧 running 状态对新业务的隐式控制。

### 阶段 4：项目目录迁移

目标：

在独立 Core、MCP、production/test 隔离和新代码旧路径清零后，把项目从旧目录迁到 I:/Creation-Assistant。

改什么：

- 先验证独立 Core、MCP 和正式/测试隔离已经完成；
- 重新确认新代码、配置、启动入口、外部能力和运行凭证不再依赖旧项目绝对路径；
- 单独执行目录迁移，并重新校验项目、正式 runtime、知识库和外部能力之间的引用。

不改什么：

- 不在目录迁移阶段改变业务规则；
- 不同时开始网页开发；
- 不借目录迁移删除正式数据、历史、备份或用户未决定的资产；
- 不让旧目录和新目录长期同时承担正式业务。

真实完成标准：

- 目标目录名称和位置已经明确生效；
- 新代码不再依赖旧项目绝对路径；
- 正式数据库仍指向唯一正式 runtime；
- 测试世界仍与正式世界隔离；
- 旧目录不再作为正式入口。

该阶段退出的旧东西：

- I:/Creation_assistant-codex 作为正式项目目录；
- 旧项目路径启动脚本和配置引用；
- 目录迁移完成后，才允许进入网页阶段。

### 阶段 5：Core 提供完整、可观察的正式状态

目标：

让任何入口都能从 Core 得到同一份答案：当前在做什么、做到哪一步、哪里失败、等待谁决定、继续时复用什么。

改什么：

- 统一 Business Run、Execution、节点状态、失败原因、人工决定和复用关系的查询；
- 让状态和审计来自 Core；
- 把当前运行、历史运行、测试运行明确分开；
- 为 Hermes、Codex、MCP 和未来网页提供同一套只读观察能力。

不改什么：

- 不由页面或 MCP 自己重新计算状态；
- 不增加新的自动恢复机制；
- 不改变已有正式业务门槛；
- 不因为可观察就自动推进流程。

真实完成标准：

- 同一业务从不同入口查看，得到同一状态；
- 能区分 Business Run 和每次 Execution；
- 能说明失败原因和等待的人工决定；
- 能说明继续时复用哪份已完成结果；
- 历史状态不再伪装成当前状态。

该阶段退出的旧东西：

- 各个 Hermes 入口自己拼接状态的做法；
- 运行日志代替正式状态的做法。

### 阶段 6：本地网页第一版，只读展示

目标：

提供给人的本地控制台，只做状态展示和历史查看。

改什么：

- 读取 Core 的观察接口；
- 展示运行、失败、数据量、人工决定、复用结果和触发记录；
- 让网页区分正式世界、测试世界和历史世界。

不改什么：

- 不在网页里写业务判断；
- 不在网页里创建第二份状态；
- 不在网页里直接改数据库；
- 不做正式操作；
- 不引入账号系统、权限系统、多租户或复杂前端框架。

真实完成标准：

- 网页显示与 Core 查询完全一致；
- 网页不连接另一份数据库；
- 网页不能绕过 Core 改正式数据；
- Core 停止时网页只报告不可用，不自行伪造状态。

该阶段退出的旧东西：

- 旧的 cold-start 页面服务启动残留只能作为待清理资产，不再作为新网页基础。

### 阶段 7：网页增加正式操作

目标：

让人可以通过网页执行正式业务操作，但所有操作仍然进入 Core。

改什么：

- 增加人工确认、继续、停止、查看失败详情和其它已明确的正式操作；
- 网页把用户决定完整提交给 Core；
- Core 写正式状态和审计；
- 网页显示操作结果。

不改什么：

- 网页不自动代替用户做决定；
- 网页不跳过人工确认；
- 网页不选择模型；
- 网页不直接写正式数据库；
- 不新增未定义的业务门槛。

真实完成标准：

- 每个正式网页操作都有 Core 记录；
- 人工决定和状态变化能回溯到操作者和时间；
- 网页与 MCP 执行同一套正式规则；
- 同一个 Business Run 不因入口不同而生成平行正式业务。

该阶段退出的旧东西：

- 只在 Hermes 交互中才能进行的正式人工操作；
- 任何与 Core 规则不一致的旧页面动作。

### 阶段 8：旧 Hermes 正式路线退出和最终清理

目标：

新正式路线验证完成后，停止旧 Hermes 专属正式路线，清理失效启动项、旧兼容入口和阶段4之后仍确认无用的残留。

改什么：

- 确认 MCP、网页和独立 Core 入口已经覆盖正式业务；
- 关闭旧 Hermes daily、cold-start、research、repair 和直接写入入口；
- 删除确认没有活动引用的 Windows Startup 残留、WSL 旧 wrapper 和旧兼容入口；
- 清理阶段4完成后仍残留、且已经确认没有活动引用的旧配置和路径；
- 删除或归档确认无用的临时文件、缓存和旧副本。

不改什么：

- 不删除用户未决定的正式数据；
- 不删除仍需审计的历史；
- 不删除仍被外部能力使用的浏览器或采集资料；
- 不保留长期双轨；
- 不在清理阶段重新改变业务规则。

真实完成标准：

- 新正式路线有受控真实端到端结果，并能对应正式数据和人工决定；
- 旧入口不再能承担同一正式职责；
- 旧路径不再影响新运行；
- 没有失效启动项把人带回旧路线；
- 正式数据库、外部资料和知识库引用均经过校验；
- 删除范围和不可逆动作已经有明确批准。

该阶段退出的旧东西：

- Hermes 专属正式业务路线；
- 旧正式 daily 和 cold-start wrappers；
- 旧 Windows/WSL 启动入口；
- 已确认无引用的旧 provider 配置、旧兼容入口、旧临时目录。

### 阶段 9：知识库智能化另行推进

目标：

在 Core、MCP、网页和正式规则发布边界稳定后，再推进知识库自动整理、关联、Wiki 和 LLM 辅助维护。

改什么：

- 明确知识库的资料类型、状态和发布边界；
- 让 Obsidian 成为可替换的查看编辑工具；
- 可以另行研究 LLM Wiki、自动整理和自动关联。

不改什么：

- 不让自动修改的笔记直接改变正式业务；
- 不把 Obsidian 变成正式数据库；
- 不把 Skill 变成知识库状态管理器；
- 不在本阶段反向重写已完成的正式业务事实。

真实完成标准：

- 普通经验和正式规则有不同状态；
- 正式规则变更可确认、可发布、可追溯；
- 更换 Obsidian 后知识库仍然存在；
- Core 不需要读取某个特定软件的隐藏状态才能运行。

该阶段退出的旧东西：

- “知识库等于 Obsidian 目录”的旧概念；
- 任意笔记修改就能直接影响正式行为的隐含路径。

## I. 迁移完成后不应该再存在什么

迁移全部完成后，以下情况都不应该再存在：

1. Creation Assistant 仍然必须先启动 Hermes 才能启动 Core。
2. Core 自己选择 MiMo、OpenRouter、Ox、Codex 或其它模型。
3. Core 自己决定 provider、fallback 或模型切换。
4. 仍有 Hermes 专属业务路径拥有 daily、cold-start、resume 或正式人工决定。
5. 旧模型或旧 provider 记录能够决定新的正式执行。
6. 正式数据、测试数据和验证数据仍然混在同一个正式身份里。
7. 网页自己计算或猜测业务状态。
8. MCP 拥有第二份正式状态、第二份 resume 或第二份生命周期。
9. Skill 自己维护 daily 生命周期或正式状态。
10. 旧入口和新入口长期承担同一个正式职责。
11. 项目仍然依赖 I:/Creation_assistant-codex 或 /mnt/i/Creation_assistant-codex 等旧绝对路径。
12. 已经废弃的 Hermes、Feishu、旧页面、旧 WSL 或旧启动项仍然能影响新运行。
13. Obsidian 被当成知识库唯一真相源。
14. 普通知识笔记的自动修改能直接改变正式业务规则。
15. Codex 的开发、测试、正式运行和知识维护身份混在一起。
16. Hermes 正式运行时可以修改项目源代码、Skill、配置或 schema。
17. 同一个 Business Run 因为换了入口、调用者或模型而被拆成第二个正式业务。

## J. 用户需要决定的问题

这里只列不能由技术实现者替用户决定的事项：

1. 失败的 domain_1833831517eb cold-start 及其泛科普-社会生活配置，是继续作为正式业务、转为历史，还是放弃。
2. real_daily_validation 以及 Codex repair/conversion 产生的 production 身份记录，是否承认是正式业务事实。
3. 已完成的 music_entertainment 正式数据是否继续作为当前正式世界，还是只保留为历史资料。
4. 当前仍处于 processing、running、failed 或 awaiting_human_decision 的正式业务，哪些必须继续，哪些可以正式关闭。
5. 旧数据库备份、旧模型执行记录、旧 Hermes profile 副本、旧浏览器状态和旧原始素材，哪些允许不可逆删除。
6. 知识库中目前混在经验、方法论、正式规则和设计说明之间的资料，哪些应发布为正式规则，哪些只能作为普通知识或历史资料。

以下不是用户决策项，而是实现者后续应自行处理的技术工作：使用哪种网页框架、数据库连接方式、内部模块拆分和普通文件布局。它们不能改变上述业务边界。

## 偏差检查

### 1. Creation Assistant 是否真正独立

目标要求是独立。当前事实是部分独立：数据库、Core 和外部能力已有分层，但主要启动和模型执行仍受 Hermes 语义影响。迁移方案把独立启动作为阶段 1 的硬门槛。

### 2. 是否仍暗中绑定 Hermes

是。daily、cold-start 绑定、HermesModelProviderAdapter、Hermes 命名入口、Feishu carrier 和环境变量都表明当前仍有暗中绑定。迁移方案明确要求在阶段 2 转换、阶段 7 退出。

### 3. 是否仍暗中绑定具体模型

是。当前正式记录包含多种旧模型身份，daily 和 cold-start 代码读取 Hermes 当前模型绑定。迁移方案保留审计、取消新业务选择权。

### 4. MCP 是否只是接口而非第二套系统

当前没有确认到可用 MCP 实现，因此当前不存在可以验证的 MCP 第二套状态。目标方案明确规定 MCP 只能是薄接口，不拥有业务状态。

### 5. 网页是否只是 Core 的人类入口

当前没有可用网页；旧启动残留指向已删除页面服务。目标方案把网页分为只读展示和正式操作两阶段，并要求全部回到 Core。

### 6. Skills 与正式状态是否分离

大体已经分离。当前 Skill 合同普遍声明不负责正式数据写入、状态转移、人工确认和其它 Skill；但 Skill 仍通过 FormalBusinessSkillAdapter 接入当前模型路由，模型边界仍需转换。

### 7. Codex 四种任务身份是否明确

当前项目运行代码没有形成完整而统一的四身份边界。目标方案明确区分开发、测试、正式运行、知识维护。

### 8. Hermes 正式运行时是否无权修改项目

目标边界是无权修改。当前可见的 Hermes 入口主要通过 Core 执行业务，没有发现本轮运行中它直接改源代码的证据；但权限和入口边界尚未形成统一的独立 Core 约束，因此不能宣布已完成。

### 9. 正式数据和测试数据是否有明确迁移目标

目标有，当前未完成。当前正式数据库只有 production 身份，测试多用内存或临时数据库，但 real_daily_validation 已写入 production 身份。阶段 3 专门处理。

### 10. Business Run 与 Execution 是否分开

部分已经存在。stage1b 有 discovery run、execution context 和 daily snapshot；但所有业务阶段还没有统一的完整边界，且旧入口仍可能自己组织继续逻辑。阶段 1、3、4 完成统一。

### 11. 知识库与 Obsidian 是否正确区分

目标方案区分。当前实际是 Core 数据和 Obsidian Junction/镜像并存，Obsidian 仍是重要查看编辑面，但不是数据库真相源。

### 12. 是否考虑未来 LLM Wiki 而没有现在过度实现

是。当前发现外部 Hermes profile 副本中有 llm-wiki 名称，但没有把它当作当前 Creation Assistant 正式能力。方案只把知识库智能化放在阶段 8。

### 13. 是否考虑旧代码污染

是。已记录 Hermes wrappers、直接 CLI、已删除页面的启动残留、旧知识汇合入口、旧 WSL wrapper 和当前未提交代码混合状态。

### 14. 是否考虑旧数据污染

是。已记录 processing、running、failed、real_daily_validation、旧模型绑定、验证身份和后台 executor 残留。

### 15. 是否考虑旧配置污染

是。已记录 Hermes、provider、model、OpenRouter/relay、Ox、MiMo、Xiaomi、Feishu、profile、插件路径、项目路径和 runtime 路径。

### 16. 是否考虑旧入口污染

是。已列出 daily、候选发现、cold-start、研究、修复、知识汇合、WSL、Windows Startup 和当前没有 MCP/网页的事实，并规定新正式路线验证后旧路线退出。

### 17. 是否考虑旧路径污染

是。已记录 I:/Creation_assistant-codex、/mnt/i/Creation_assistant-codex、I:/Creation_assistant、Obsidian 路径、Hermes home 路径、密钥文件路径和旧 data/formal 路径。

## 阶段0封存结果（本轮）

阶段0封存记录起点为 2026-08-28T13:11:33+08:00，完成确认时间为 2026-08-28T13:17:00+08:00。

项目现场快照位于：

    I:/Creation_assistant-codex/migration_snapshot/

快照记录的是：

- 分支 implementation/v1.3-stage1b-daily-discovery；
- HEAD 0bfb7960c40d53fd96bc4d9ba2c6d57780ce6d7e；
- 快照生成前的脏工作树状态；
- 当前主要目录、配置来源、入口、运行目录和旧路径；
- 当前正式数据库摘要、schema 摘要、关键表行数和关键状态；
- 当前异常、自动启动残留和后台残留。

正式数据库迁移前备份位于：

    I:/Creation_assistant-runtime/formal/backups/migration_baseline_20260828T130918779.sqlite3

本次备份没有覆盖已有备份。原数据库操作前摘要、操作后摘要和备份摘要均为：

    5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD

原数据库大小和备份大小均为 62418944 字节。操作前后大小一致，原库摘要一致，备份摘要一致。

schema 摘要为：

    116 张表
    b734947315c6defbf97f1a02f9d3c71aa282aebf4623c3ba782432ff7c1b09fa

封存时发现一个必须保留为异常的事实：runtime_identity.json 里的旧正式数据库摘要为 53b46277a07a7ada7ce2d428445c10103d8af3efd132b3eee962218866bb2b7a，与当前正式数据库摘要不一致。本轮没有修正身份文件，也没有修改数据库。

正式运行冻结从阶段0完成时开始。当前未发现正在运行的相关进程，也未发现匹配的 Windows 计划任务；但 Windows Startup 和 WSL 仍有旧入口残留，不能把它们当作已经清除。它们没有在本轮被关闭或修改。

## 迁移保护期间自动触发器收口（2026-08-29）

本轮追查了 2026-08-28 23:50:14 的 `daily_collection_once` 运行。运行日志确认它使用 production 身份打开了正式数据库；它没有新增或修改正式业务记录，但普通可写 Core 启动造成了 SQLite 文件头变化。

当前找到的明确危险自动来源是用户级 Codex 自动化任务 `creation-assistant`。它的任务内容直接调用正式的一次性 daily 入口，工作目录指向当前项目；当前配置状态已经是 `PAUSED`，因此该已知自动来源目前处于停用状态。入口脚本和 daily 业务能力保留，没有删除或改写。

需要区分两件事：该自动化配置当前声明的日程是每天中国时间 09:00，而异常发生在 23:50。因此现有本机证据可以确认它是一个会自动启动正式业务的旧正式入口，但不能仅凭当前配置把它百分之百认定为 23:50 那一分钟的唯一父触发进程。当前可读取的 Windows Startup、已知计划任务、WSL cron、WSL systemd 用户目录和活动进程中，没有发现其它已确认的自动正式业务触发器；系统级计划任务的完整读取仍受权限限制，不能据此排除不可见的系统级任务。

WSL 中的旧 Hermes daily 脚本仍然保留，但当前只确认它可以被手工调用；没有发现它被 cron、systemd 或当前活动进程自动调用。本轮没有修改它。

迁移保护规则补充如下：保护期不仅禁止主动执行正式业务，也禁止存在仍会自动启动正式业务入口的活动定时触发器。已确认的危险触发器应停用而不是删除业务代码。SQLite 完整文件摘要用于发现正式文件是否被触碰，但摘要变化本身不等于正式业务事实发生变化；发现变化后必须继续比较 schema 和业务内容，并调查触发来源。

本轮正式数据库摘要事实记录为：`5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD` 是阶段 0 迁移起点及备份摘要；`33E2B8D7CB902B78AD570A3C8214FEA9864A8A526A11F4303E42643AA3A8A732` 是 2026-08-28 23:50:14 旧 daily 触碰后的当前文件摘要。逐表比较确认两者的 116 张表和业务记录一致；本轮不恢复旧备份。

## 迁移结束后的开发重新立基线

本迁移基线只负责本次架构迁移，不是未来完整产品设计。迁移核心完成、旧正式路线退出后，恢复普通功能开发以前，必须重新建立 Creation Assistant 唯一当前开发基线。

旧设计资料必须重新分类为：

- 已经实现并仍然有效；
- 仍然有效但尚未开发；
- 已经被新架构或后续决定废弃；
- 当前无法判断，需要用户决定。

只有前两类可以进入新的当前开发基线。无法判断的旧需求不能自行恢复。新的当前开发基线形成后，旧设计文档全部降级为历史参考。

后续开发只依据当前开发基线、当前真实代码、已确认的新业务决定和真实验证结果。当前基线始终只有一份；不再通过 V1、V2、最终版、修订版或无损版等多份并行总文档管理当前设计，历史变化交给 Git 保存。

### Git 可信版本节点纪律

Git 用于保存已经确认可以作为下一步起点的代码版本。以后默认在一个迁移阶段或明确开发阶段真正完成，并且必要测试和验证完成后，建立一次 Git 提交；阶段中途不为了暂存而建立完成版本。

迁移期间不随意切换或新建分支，不把正式数据、密钥、运行缓存或临时输出提交到 Git。Git 提交代表代码版本可信节点，不代表正式业务运行完成；两者必须分开。GitHub 仅作为以后 Git 历史的远程私人备份，本基线不要求推送。

### 外部开发工具版本检查纪律

重大迁移或开发阶段开始前，先只读检查 Codex CLI 和 Hermes 的当前版本。发现有新版时，先提醒用户并判断当前阶段是否适合更新；只有用户确认后才更新。不得发现新版后自动更新并继续执行项目。

这样做是因为外部 Agent 版本变化可能改变模型、MCP、审批或工具行为。不得建立自动升级服务。

## 本轮边界确认

本轮允许写入项目的只有本文件和 migration_snapshot/ 快照目录。正式数据库只允许在外置正式运行目录中新增一份不覆盖旧备份的迁移前副本。

本轮没有：

- 修改代码；
- 修改正式数据库内容或 schema；
- 修改配置或环境变量；
- 修改 Hermes、Hermes Plugin、Skill 或 Obsidian；
- 修改 Windows 计划任务、WSL 或正式业务数据；
- 运行真实 daily 或 cold-start；
- 调用模型；
- 开始独立 Core 改造；
- 开始 MCP 改造；
- 开始网页开发；
- 重命名项目目录；
- 删除任何旧资产；
- 清理 failed、processing、running 或补写正式记录。

阶段0已完成。后续迁移必须从本基线和 migration_snapshot/ 开始，并在每个阶段完成真实完成标准后再进入下一阶段。不得把阶段0封存误认为独立 Core、MCP、数据隔离或正式业务验收已经完成。

## 阶段2可信提交与阶段3第一轮实际进展（2026-08-29）

阶段2已建立可信 Git 节点：`054bd79`（`Creation Assistant migration Stage 2 complete`）。该提交包含阶段2正式代码、测试和迁移基线，不包含正式数据库、runtime、日志、用户级 Agent 配置、认证信息或未裁决历史资料。阶段2的正式模型执行权迁移结论保持有效。

阶段3第一轮只收口“谁有权按时间启动业务”和“需要智能任务时如何启动当前外部执行者”，没有开始 cold-start 数据重置或清理。

当前架构事实如下：

- Windows Startup 只保留一条 Creation Assistant 平台启动快捷方式，职责是启动项目运行时；它不再保存 daily 时间、领域时间、Agent 名称、模型或 provider。该快捷方式已从失效的旧平台服务改为当前项目的 runtime bootstrap，并以常驻方式启动。
- Windows 的消息监听器仍保留，因为已确认它只接收消息，不按时间主动启动 daily；它不是业务计划来源。
- Creation Assistant 的唯一业务计划来源是 `config/schedule_registry.json`。当前只有一个 `daily` 计划，领域范围由 Core 管理，迁移保护期内 `enabled=false`，时间记录为 `08:00`，但不会自动执行。
- 业务事件（例如候选准备完成后的汇报）不登记为时间计划；多领域共用同一个 `daily` 计划，不按领域拆出多个计划。
- 当前默认外部执行者是 `hermes`。启动配置只记录通过 WSL stdio 启动 Hermes 的方式，不记录模型或 provider。执行者是否已经运行由调用方提供；已经运行时不重复启动，启动失败只报告不可用，不切换 Codex，也不切换模型或 provider。
- 旧的自动 daily 来源保持暂停。WSL Hermes daily 脚本和旧业务入口保留作为兼容资产，但不再拥有计划权；在当前迁移保护配置下，旧自动触发会在打开正式数据库前被拒绝。本轮确认并禁用了 Windows 计划任务 `CreationAssistant_Daily`（每天 08:00 启动旧 Hermes daily 入口），任务和脚本均未删除；此前已确认的 Codex 自动 daily 任务继续保持暂停。
- 项目代码增加了静态守卫：除 runtime bootstrap 外，业务代码不能直接创建 Windows 计划任务、cron 或 systemd timer。测试还验证唯一计划、测试环境 run-now、不改正式时间、执行者启动失败不回退，以及迁移期间 daily 不运行。

本轮不改变 daily 业务规则，不运行正式业务，不调用真实模型，不修改正式数据库，也不处理 Codex MCP 兼容性。当前正式数据库文件摘要仍以取证后的 `09B245CDCF9969DAEFE163FC6F33EFEDE978E5F9A594CEDA2B6041371B8454A3` 为文件触碰监测基准；阶段0的 `5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD` 和第一次变化后的 `33E2B8D7CB902B78AD570A3C8214FEA9864A8A526A11F4303E42643AA3A8A732` 仍作为历史凭证保存，不恢复旧备份。

阶段3第一轮完成后，下一轮才处理单领域 cold-start 重置、active 状态指针和历史/知识资产边界。本轮不提交新的阶段3 Git 节点，等待用户确认。

## 阶段3第二轮第二步完成记录（2026-08-29）

本步完成单领域重新开始 cold-start 的正式 Core 边界，并准备建立可信 Git 节点。

### 当前 activation 和 reset 语义

- 每个领域最多只有一个 current activation。
- current activation 只绑定当前 cold-start、当前配置和已有运行身份，不给全库增加 generation_id 或 activation_id。
- 没有 current activation 时，Core 按必要输入判断是否允许新的 cold-start；历史记录本身不再构成阻塞。
- reset 只解除该领域 current activation，并记录审计事实；不删除、不改写旧业务记录。
- reset 后，普通 resume 只能作用于当前 activation 对应的 cold-start。已解除的旧 run 不会重新成为当前世界。

### 已收口的旧状态查询

own account、competitor、tag、content type、production boundary、daily、baseline、D0-D7、候选和 discovery/content 运行关系，当前业务读取均以 current activation 对应的已有 run/configuration 身份为边界。旧记录继续保留为历史，但不能因为仍存在就被当作当前配置、当前 daily 或当前候选池。

候选经验不再参与 cold-start 零状态阻塞判断。候选经验仍然保持候选状态，不会自动变成正式规则。

### 知识资产保留边界

正式经验、候选经验、候选经验来源、候选经验的证据，以及为追溯所需的拆解、研究、视频、评论和转写材料均不因 reset 删除。验证确认了 reset 前后知识资产数量和来源证据解析结果保持一致；未发现本步新增的删除级联。

### 验证结果

- 单领域已验证 cold-start、reset、cold-start、reset、cold-start 可连续重复执行。
- 同一 own account 和相同 competitor 可在后续 cold-start 重新使用。
- 领域之间互不影响。
- 专项 reset 测试 2 项通过。
- 完整测试 325 项通过，0 失败，0 错误。
- 测试只使用隔离测试数据库和 fixture，没有调用真实模型。
- 正式数据库未执行 reset，未执行 INSERT、UPDATE、DELETE、DROP、VACUUM、restore 或替换。
- 正式数据库当前文件摘要仍为 `09B245CDCF9969DAEFE163FC6F33EFEDE978E5F9A594CEDA2B6041371B8454A3`。

本步不修改 Agent/MCP 架构，不进入阶段4，不实现正式领域 reset 操作，不处理 16 份未跟踪历史/异常资料。上述代码、测试和本记录由本次可信 Git 提交一并封存。

## 阶段3第三轮正式/测试运行环境物理隔离完成记录（2026-08-29）

本轮完成正式运行世界与测试运行世界的物理隔离。正式运行继续使用外置正式运行目录中的正式数据库；测试运行使用临时测试运行目录中的测试数据库。两者的数据库、运行状态、临时文件、业务输出和会被程序再次读取的日志状态不共用可写位置。

### 运行身份和路径边界

- 运行身份必须明确选择正式或测试；没有身份时，路径解析直接拒绝，不再默认落到正式运行。
- 每个身份只能解析到自己的运行根目录和数据库；正式、测试运行根目录必须互相独立。
- 测试数据库不存在时只在测试运行世界创建，不能回退读取或创建正式数据库。
- 测试身份指向正式运行目录中的数据库会被拒绝；正式身份指向测试数据库也会被拒绝。
- 正式运行配置只保留一个明确的正式数据库来源；旧库和归档库不进入默认查找或自动发现路径。

### 入口边界

Core、MCP、调度器、冷启动、daily、研究和内容相关运行均沿用明确的运行身份。测试 MCP 只能连接测试数据库；正式 MCP 只能连接正式数据库；不存在根据路径或参数缺失猜测数据库的共用入口。迁移保护期间，调度器的立即运行只允许测试身份，正式 daily 仍不自动运行。

### 验证结果

- 运行身份和路径专项测试通过；正式/测试隔离专项测试 7 项通过。
- 完整项目测试 332 项通过，0 失败，0 错误；另有 9 个既有辅助测试节点未收集。它们来自此前已经存在的被导入测试函数，并依赖当前未提供的 `trusted_context` 夹具；本轮没有新增排除或跳过规则，也没有修改其来源测试文件。
- 本轮新增的隔离测试证明：测试数据库缺失时不会回退正式数据库，错误身份和错误数据库会直接拒绝，测试 MCP 返回测试身份，调度器立即运行不触碰正式运行，冷启动/reset 回归仍在测试世界内完成。
- 未调用真实模型，未执行正式 cold-start、reset 或 daily。
- 正式数据库开始和结束的 SHA256 都是 `09B245CDCF9969DAEFE163FC6F33EFEDE978E5F9A594CEDA2B6041371B8454A3`。
- 本轮没有修改正式数据库内容或结构；16 份未跟踪历史/异常资料保持原样且未纳入提交。

阶段3第三轮正式关闭。上述隔离代码、测试和本轮记录作为一个可信 Git 节点封存；不进入阶段4。
