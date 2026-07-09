# 创作助手 · 工程章程(AGENTS.md)

> **每次开工先读:本文件 + `BUSINESS_RULE_CATALOG.yaml`(现行系统唯一算数的业务规则依据)+ memory 里的 `target-architecture.md`(完整蓝图)/ `rebuild-direction.md`(推理依据)。本文件是防漂移的宪法。**
> **`BUILD_PLAN.md` 已于 2026-07-07 删除(git 历史可查):那是清空重建(GOAL-DATA-RESET-01)前旧系统的构建日志,写的是已经隔离下线的旧代码路径,不是现行系统的设计依据——曾被当成"已定型设计"直接引用过,导致真实跑偏一次。以后任何"设计是不是已经讨论过"的问题,只认 `BUSINESS_RULE_CATALOG.yaml`,不接受旧计划文档/对话记忆当权威。**
> **总控文档(`爆款口播内容经验库系统_...`)已于 2026-07-08 从仓库彻底移出、不再入库(真实路径写在本机配置)**:同一次会话里,绕开 `BUSINESS_RULE_CATALOG.yaml` 直接翻总控文档原文,真实导致跑偏三次(把已被 catalog 明确覆盖的旧数字/旧候选方案当成现行设计)。用户的诊断:这不是"记得先查 catalog"的习惯问题,是"仓库里放着源文档"这个机制本身在诱导跳过 catalog——所以不留在仓库里,`tests/validation/test_source_document_not_tracked.py` 强制检查它不得再被跟踪。常规开发期间不该、也不能读到它;只有用户主动发起的、逐条对照复核的正式重新对齐会话(如 BR-HIT-001 那次)才需要用户重新提供该文件,且仅限那次会话使用,用完即弃,不写回仓库。

## 这是什么
一个 **Codex 工程**(不是独立程序):写死的 Python 脚本干确定性脏活,Codex 当创作驾驶舱。目标 = 一个"越用越好"的抖音内容创作系统(自进化 agent 的**实质**,载体是程序)。开发用 Codex(Opus 4.8 / 可切 Fable 5),日常 LLM 节点走 Codex 订阅低阶模型(Codex 无头;Codex 不装 cc-switch 用不了 DeepSeek,故不接),创作走用户的 Codex 订阅。飞书/Obsidian 目前只是 `scripts/core/host/` 预留的适配器位置(`DEFAULT_ALLOWED_HOSTS` 里的 `feishu`),**不是现状**——飞书实时集成在一次 legacy removal 里被整体删掉、还没重建;Obsidian 在当前代码里几乎没有真实写入(只有 `scripts/core/workflow/goal_phase5_business_workflow.py` 一处引用)。不要假设这两者已经在跑。

## 三根支柱(一切决策的总纲)
1. **设计权威只有一份**:`BUSINESS_RULE_CATALOG.yaml`(+ `REQUIREMENT_CODE_TRACEABILITY.yaml` 核对实现对齐状态)。不接受旧文档、聊天记忆、或本文件自己的旧版描述当权威——本文件下面的"模块/数据地图"故意写得很薄,就是为了不再重蹈"这里写了细节、代码往前走了、文档没跟上"的覆辙(已经真实发生过两次:一次是 `BUILD_PLAN.md` 被当权威引用,一次是这份文件自己的模块地图,一直停在 clean-room 重建前的旧架构,直到 2026-07-08 才被用户发现)。
2. **确定性执行 ⟂ 生成**。采集/判定/存储/检索/追踪固定路线写死成被调用的脚本/函数;LLM 只允许出现在 CR-003A 定义的"原子 Skill"节点里,经 Runner/Model Port 调用,不允许决定确定性流程该怎么走。
3. **一切会产生真实外部后果的执行只能走一个受控入口**:真实数据库写入、真实网络请求、装真实软件、调真实付费模型接口,都必须经 `scripts/core/execution_contract.py` 的 `require_catalog_citations()` 校验(点名一个 `BUSINESS_RULE_CATALOG.yaml` 的 `requirement_id`,查不到就拒绝执行),不能用临时命令/代码片段直接碰真实世界。

## 硬规则(违反即跑偏)
- **禁令进代码**:内容硬约束(禁词等)= 写完后的代码校验器(regex/检测),**绝不塞进生成 prompt**(否则禁令 bloat 把文案写废)。
- **范例不是规则**:学到的东西 → 范例库,不是越堆越多的 prompt 规则。AI 味靠**正向范例驱动文风 + 代码兜底**,不靠堆禁令。范例库的物理载体(Obsidian 还是别的)尚未锁定,不要假设是 Obsidian。
- **数据真相源本地**:业务数据以 `data/formal/*.sqlite3` 为准,任何外部视图(飞书等)只镜像、不当主数据库。
- **模型路由 per-node**:默认 Codex 低阶(haiku,走订阅);逆向/研究用中阶(sonnet);创作走 Codex 对话。可替换适配器(以后要 DeepSeek/MiMo 再接),**透传各引擎特性,不做最小公分母**。
- **固定技术路线 = 函数**:如本地 ASR 提口播文案,写死成被调用的函数,不给 LLM "重新决定怎么做"的余地(电梯拆了只留楼梯)。
- **领域 = 数据不是代码**:新建领域/账号 = 加配置 + 空范例桶;流水线一份代码、领域无关。
- **范例:少而厚**:限量 + 强度 + 去重 + 时效淘汰;检索按 `相关度 × 强度` 取少量注入,**绝不全量**。
- **经验只在一个共享库,不散在各 agent / 各维度**;捕获在阶段,晋升在结果。

## 模块 / 数据地图(只做定位,不复述细节——细节以 `BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml`/`TECHNICAL_MANUAL.md`/`HANDOFF_STATE.md` 为准,不在本文件重复,防止再次和实现进度脱节)
- **生产 Host 边界**:`scripts/core/host/` 是平台中立入口,外部平台先变成 Host message,再经白名单 Tool/Core/Job/Outbox;Hermes、Codex、Claude Code、飞书等只能是适配器,不直接写 Core 或业务库(`DEFAULT_ALLOWED_HOSTS` 现含 `hermes`/`codex`/`claude`/`feishu`)。
- **竞品业务数据层**:`scripts/core/business_data/`——账号注册、首采存量分类、基线/爆款判定(`BR-HIT-001`)、每日增量采集(`BR-COLLECT-*`)、逆向备料/转写+评论(`BR-ASR-*`),独立库 `data/formal/production_activation.sqlite3`,受 `execution_contract.py` 契约闸门约束。真实数据的重判/每日采集只能走 `run_competitor_registration_full.py` 的 `--rejudge-only`/`--daily-incremental`,不能用临时脚本片段绕过契约闸门。**没有"观察池"/"候选池"这类独立状态机或视图**——BR-HIT-001 用"三类首次接触(historical_mature/transition/formal_new)+ D0-D7 发现批次锚定 + 四种基线"取代了这套旧设计,不要再假设 watching/archived 状态或候选池表存在。
- **原子 Skill 层**:`runtime_skills/`——按 CR-003A(可移植原子 Skill:自带输入输出 schema、不读数据库、不串联其他 Skill)设计的独立 Skill 包,目前有 12 个业务 Skill(`sample_deep_analyze`/`tactic_extract`/`source_to_topic`/`script_generate`/`script_review`/`content_plan`/`content_classify`/`content_relation_judge`/`research_evidence_extract`/`production_research_plan`/`experiment_review`/`experience_revision_propose`)+ 1 个测试探针(`runtime_probe`,不对应真实业务能力,不要算进"13个业务Skill")。**Binding 现状**(2026-07-10 更新):`sample_deep_analyze`/`source_to_topic`/`content_plan`/`script_generate` 四个已接上真实数据,串成"选题→大纲→成稿"主链路且有端到端集成测试(`tests/core/test_topic_to_script_chain_integration.py`),中途卡在人工审核闸门(`scripts/core/experience/review_queue.py`)上;其余 8 个仍只能吃 `fixtures.yaml` 假样例过契约测试。**选题这一环目前只是简化版**(只用"对标爆款+评论区"两种料源,原方法论要求的"研究缺口/当下热点"两种料源、"选题判断维度"打分排序、人工从多候选终审都还没做——产出这把"评分尺子"的技能`.claude/skills/归纳`已被删除且无人补位),详见 `ROADMAP.md`,不要臆测选题环节已经做对。
- **旧 `.claude/skills/*` 的 Claude Code 会话技能**(选题/大纲/钩子/成稿/审稿等)与上面的 `runtime_skills/` 原子 Skill 是同一批业务问题的两套不同实现。**2026-07-09 用户已拍板方向**:以 `runtime_skills/` 为唯一权威,裁决标准是"能否跨平台独立执行"(`.claude/skills/*` 依赖 Claude Code 会话内工具/子 agent 机制,天然不满足;`runtime_skills/` 经 Model Port 抽象层验证过可替换供应商,满足)。**但迁移/淘汰还没开始执行**,`.claude/skills/*` 目前仍在正常使用,不要假设它已经废弃——具体执行节奏见 `ROADMAP.md`。
- **仓库里同时存在两套平行架构,尚未调和**(2026-07-10 发现):`scripts/core/{persistence,scheduler,production,correction,workflow}` 等目录是一条完整但从未接过真实数据的"正式生产链路"(Goal01-12,`PersistenceStore`/`VersionRef`/`goal10_corrections` 修正传播框架都在这套里,只在各自的 `verify_goal_*.py` 自证脚本里跑通过);`scripts/core/business_data`/`scripts/core/experience` 是务实轻量、目前真正在跑真实数据的层,用的是普通 SQLite 表,不接入前者的版本追溯体系。两套从没打通过,新写代码前先确认在哪一层里,不要假设某个通用能力(如"修正留痕")已经可用——它可能只存在于没接数据的那一套里。

## 开工纪律
- 每步对照 `BUSINESS_RULE_CATALOG.yaml` + `REQUIREMENT_CODE_TRACEABILITY.yaml`,**做完一步验一步,不跳建**。
- 固定决策有疑问 → 查本文件 / 蓝图,**不自行改技术路线**。
- 改了模块结构 → 更新本文件的模块地图,**且必须在同一次提交里做**,不能代码先跑、文档以后再补(以后再补=从来不补)。
- **两份宪法文件点名要读的治理依据(如 `target-architecture.md`/`rebuild-direction.md`)如果在仓库和 memory 里都找不到,必须停下来问用户,不能记一笔"找不到"就当警告放过、继续往下做**。这是 2026-07-06 之前 124 次提交里真实发生过的跑偏根因:治理依据缺失被诚实记录、但从未真正拦停过任何一次开工。

## 执行纪律(2026-07-06 新增,防止再次跑偏)
> 背景:GOAL-V0.6.2-PRODUCTION-COMPLETION-01 分支上,`baseline_min_samples: 30` 一度被写成硬门槛(应为目标样本、10 才是最低可判门槛),导致 28 个竞品账号全部基线=0/爆款=0;修复该问题时,又用没留痕的临时路径重新跑了判定(绕开了刚建好的契约闸门),且汇报"完成"时没有核对真正的权威闸门脚本(闸门实际返回 `ENGINEERING_NOT_READY`)。这五条是从这两次真实事故里提炼的硬闸,不是预防性堆砌:
- **治理依据缺失 = 硬停**(见上,重复强调因为这是本次事故的制度性根因)。
- **一切会产生真实外部后果的执行只能走一个受控入口**(2026-07-08 从"只管数据库写"扩大为通用规则:同一次会话里,在备料/ASR 这块,直接用 Bash/PowerShell 装软件、调真实 MediaCrawler 下载、存真实密钥,全程没有走任何受控入口、没有先核对 `BUSINESS_RULE_CATALOG.yaml`,跟数据库那次是同一类漏洞,只是换了个模块——说明问题不在某一个模块,而在"临时命令能不能直接碰真实世界"这件事本身没被普遍管住):真实数据库写入、真实网络请求(下载/调用外部平台或 API)、安装/下载真实软件依赖、调用真实付费模型接口——都必须走一个提交过、内部调用 `scripts/core/execution_contract.py` 共享契约校验的脚本入口,不能用临时命令/代码片段直接对真实世界执行。探索性一次性验证(如本次 ASR 效果对比)如果确实需要临时脚本,也必须先在脚本里写出它对应 `BUSINESS_RULE_CATALOG.yaml` 的哪个 requirement_id、跑校验通过后才能执行,不能跳过这一步直接跑命令。
- **完成状态必须贴真实闸门输出,不能凭自己判断**:任何地方标记 `completed`/`succeeded`/`ENGINEERING_READY` 之类的状态,必须附上刚跑过的对应权威闸门脚本(如 `scripts/core/staging/verify_goal_v062_phase8_readiness.py`)的真实终端输出,不能转述、不能只看局部测试就下"完成"结论。
- **收工/额度耗尽前必须提交干净**:当前进度必须提交成一个能跑的 checkpoint commit,不能把大批未提交改动扔在工作区——交接给另一个执行者(Codex/Claude Code 互相接力)时,工作区状态就是唯一可信的现状。
- **AGENTS.md 是唯一手工编辑源,CLAUDE.md 由脚本生成,不再靠人工逐字同步**(2026-07-09 改):改完 AGENTS.md 后跑 `python -m scripts.validation.generate_constitution_mirror` 刷新 CLAUDE.md,两份一起提交;`tests/validation/test_constitution_sync.py` 强制检查 CLAUDE.md 与生成结果逐字一致,不一致时的修复方法是跑生成脚本,不是手动改两份文件——这道题以前只靠"记得同步",124 次提交里从没被同步过一次,现在改成机械生成,不给"忘记"留空间。
- **模块建好即写技术手册,不能"以后再补"**(2026-07-07 新增):`TECHNICAL_MANUAL.md` 是写给人看的运行/使用说明,跟 `BUSINESS_RULE_CATALOG.yaml`(机器可核对的规则)、`HANDOFF_STATE.md`(交接记录)分工不同。一个模块从"设计中"变成"能跑、有测试通过"的那次提交里,必须同一次提交把该模块在 `TECHNICAL_MANUAL.md` 里对应的章节从 `[占位]` 改写成 `[已完成]` 的真内容,不能代码合并、手册留白等以后补——以后补 = 从来不补。
