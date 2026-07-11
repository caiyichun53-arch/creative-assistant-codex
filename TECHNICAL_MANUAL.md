# 创作助手 · 技术手册

> 这份手册是写给**人看**的，讲系统实际怎么运行、怎么用——不是给机器解析的规则文件（那是 `BUSINESS_RULE_CATALOG.yaml`），也不是给下一个开发者接手用的交接记录（那是 `HANDOFF_STATE.md`）。三者分工不同，不要互相替代。
>
> **维护规则（写进 CLAUDE.md/AGENTS.md 的硬规则，不是本文件自愿遵守）**：一个模块从"设计中"变成"能跑、有测试通过"的那次提交里，必须同时把这份手册里对应的章节从"占位"改写成真内容；不能代码先合并、手册说"以后再补"——以后再补 = 从来不补，这是本项目已经吃过的亏（`BUILD_PLAN.md` 被删前就是反面例子）。
>
> 每章开头标注状态：
> - `[占位]`——模块还没建好，或者建好了但还没人回来写这一段，先留一个骨架和该写什么的提纲。
> - `[已完成]`——内容是真的照当前代码写的，不是转述设计意图。

---

## 目录

1. [系统是什么、给谁用](#1-系统是什么给谁用)
2. [整体运行流程](#2-整体运行流程)
3. [竞品数据层（business_data）](#3-竞品数据层business_data)
4. [生产 Host 边界（host）](#4-生产-host-边界host)
5. [模型路由（model_gateway）](#5-模型路由model_gateway)
6. [业务工作流（workflow）](#6-业务工作流workflow)
7. [调度（scheduler）](#7-调度scheduler)
8. [持久化与状态（persistence / state）](#8-持久化与状态persistence--state)
9. [纠错与实验（correction）](#9-纠错与实验correction)
10. [经验库（experience）](#10-经验库experience)
11. [外部适配器（external_adapters / hermes）](#11-外部适配器external_adapters--hermes)
12. [研究（research）](#12-研究research)
13. [日常使用：一个典型的一天](#13-日常使用一个典型的一天)
14. [出问题了怎么办](#14-出问题了怎么办)

---

## 1. 系统是什么，给谁用

[占位] 待补：面向第一次接触这个系统的人（不一定懂代码），用大白话讲清楚——这是什么、解决什么问题、日常谁会跟它打交道、跟直接用抖音后台或人工做内容比，区别在哪。

## 2. 整体运行流程

[占位] 待补：从"采集一个对标账号"到"一篇文案发布"，中间经过哪些阶段，每个阶段谁负责（代码 vs 人 vs LLM），配一张流程图。

## 3. 竞品数据层（business_data）

[已完成] 2026-07-07 按 `爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.md`（已存入仓库根目录）重写完成，规则定稿在 `BUSINESS_RULE_CATALOG.yaml` 的 BR-HIT-001/002/003/005（BR-HIT-006/007 是有意留白，见文末）。

**这一层干什么**：跟踪对标账号发的视频，判断哪些是"爆款"，供后面选题环节参考。

**一条视频从被发现到判定爆款,要经过什么**

1. **怎么发现的,决定了它属于哪一类**（三选一,终身不变）：
   - **历史成熟样本**：账号刚注册时,已经发了7天以上的老视频——存一次当前数据就完事,不再追踪。
   - **过渡样本**：账号刚注册时,发了1-6天的视频——先存一次,等它满7天了,再补存一次"成熟值"，这两次快照就是它的全部数据。
   - **正式新视频**：账号正式接入之后,每天例行采集时才第一次发现的视频——从发现那天起连续追踪8次(记作D0到D7),每天存一次数据。

2. **两种"背景参照线"（基线）**，用来判断"这条视频算不算表现异常"：
   - **成熟历史基线**：这个账号最近90天内、最多50条老视频的点赞/评论/收藏/转发中位数。账号要攒够20条符合条件的视频才能算这条基线,不够就先不算。
   - **正式D基线**：专门给"正式新视频"用的,按第几天分别算一条基线(比如"这个账号所有视频在第3天时点赞数的中位数")。账号要攒够20条完整追踪过D0-D7的视频才启用,启用后每次都用最近50条去算,不是永远只看20条。

3. **什么样的表现算爆款**（够上面任意一条就算,不用同时满足）：
   - 点赞、评论、收藏、转发,任意一项达到基线的2倍
   - 或者其中任意两项同时达到基线的1.6倍(单项不到2倍也算)
   - 或者这条视频自己的"评论数/点赞数"比例达到0.2(这条不需要任何基线,随时能查)

4. **一旦判定爆款,就不会再取消**——哪怕后来数据涨得慢了、被别的视频比下去了,判过一次就永久记录在案,不会被撤销。以后每次重新判定,只会往这条视频的"命中记录"里补充新发现的规则,不会删掉已有的。

**怎么触发这套流程**：`scripts/core/business_data/run_competitor_registration_full.py`——首次注册账号用默认参数,只重新判定、不重新抓取用 `--rejudge-only`,真正的"每天抓一遍"用 `--daily-incremental`。所有写操作都要走这一个脚本,不能用临时代码片段直接改数据库。

**每天的采集(2026-07-08 接上真实定时,之前只是能手动跑的开关)**:Windows 计划任务 `CreationAssistant_Daily`(`scripts/scheduled/register_daily_task.ps1` 注册,每天本地时间08:00 跑 `scripts/scheduled/run_daily_incremental.bat`)。一次采集流程:先按批抓账号最新一批视频(`_crawl_accounts_batch()`,一批共用一次 MediaCrawler 浏览器会话,批大小 `crawler.accounts_per_batch`,默认10个账号一批,不是28个全塞一次会话——真实数据验证过,28个账号从"每个账号单独开一次浏览器"的7分26秒降到"分3批共用会话"的4分6秒),再对账后判定,最后照上面说的自动接一遍备料。**目前是"发现新视频+刷新追踪中的旧视频"合并成一次抓最新N条来做,不是分开单独查每条到期视频**——如果账号发帖特别勤(超过这批抓取的条数),追踪中的视频理论上可能被挤出这批之外、断更;真实28个账号验证过一轮,最勤的账号一周也就15条,在能抓到的范围内,暂时不用担心,但账号发帖节奏变化后需要重新核实这一点。

**判定爆款之后,备料这一步怎么做**(2026-07-08 新增,同天对照总控文档第13/16章补齐过一版,同天又接了自动触发):`scripts/core/business_data/run_reverse_prep.py`——把已经判定爆款、但还没转写的视频(`hits.reverse_status='pending'`),逐条转成"文字稿+评论"存进库里。**现在不用手动喊它跑**——上面"怎么触发这套流程"提到的三个判定入口,判定完都会自动就地跑完这一步(只处理这一轮新判定出来的爆款,不会去动历史积压),因为爆款判定完不备料,后面拆经验就是空的。

- **怎么抓**:每条视频只跑一次 MediaCrawler 详情抓取,同一趟顺手拿到下载链接和评论(不分两次抓),下载链接只在内存里临时用一下,从不存进数据库;下载完的音频用本地的 SenseVoice 模型转写成文字(不联网、不调用任何大模型)。二级评论(回复的回复)在抓取这一步就被关掉,不会进库。
- **转写怎么存**:原始转写(`raw_transcript_text`)和清理过的版本(`cleaned_transcript_text`,只做去空白、去掉连续重复句子这类确定性清理,不改原意、不用大模型)分开存,而且是"只增不改"——每次重跑都是新加一条记录(带版本号),不会覆盖旧的。同时记下用的哪个模型、哪个音频文件(哈希值),方便以后追溯。
- **质量检查**:转写完全是空的、字数太少、或者明显在"卡帧复读"(同一句话反复出现)——这三种情况会被标出来,这条视频标记为"失败"、留着人工看,不会悄悄当正常数据用。
- **评论怎么存**:先过滤掉太短的、去掉重复的(纯代码判断,不靠大模型),同时记下每条评论在抓取结果里原来的顺序号,方便看出"哪条被过滤掉了"。**2026-07-13 更新**:每条评论现在都会标"这是哪个阶段采的"(`purpose`,目前这个脚本只会打 `mature_analysis` 这一种标签,因为它只在爆款判定完之后跑一次)、"对应哪个D/P观察点"(`observation_point`,取视频自己真实的首次触发点,没有就留空)、"用什么策略选出来的"(`sampling_strategy`)——这是为了以后同一条视频要分阶段多采几次评论时,同一条评论在不同阶段各存一份,不会被旧的去重逻辑悄悄吞掉一份。已有的旧评论数据(23893条真实评论)会在下次任何脚本调用 `install_schema()` 时自动补上 `purpose='mature_analysis'` 这个标签,不会丢数据。
- **处理顺序**:不是先来后到,而是分了优先级——已经攒够正式基线数据的视频优先、今天新发现的视频其次、历史高信号视频再次、其余的最后处理。
- **怎么跑**:`python -m scripts.core.business_data.run_reverse_prep --limit N`(`N` 是这次要处理几条,默认只处理1条)。

**明确没做的**（不是遗漏,是刻意留到以后,见 BUSINESS_RULE_CATALOG.yaml BR-HIT-002/006/007）：判过爆款以后要不要配"普通对照组"辅助分析、要不要按视频热度分级决定要不要用更贵的模型深入分析——这两块是更大的评论/样本处理体系,要等按这个体系设计的采集流水线真的建起来之后再回头做。**评论"按用途分阶段采集"这一块,数据库层面已经支持了(见上一条),但触发时机还没接**——现在只有"爆款判定完之后采一次"这一个真实触发点(`purpose=mature_analysis`),"视频刚冒头就先采一次"(`purpose=early_topic`)这个触发点还需要接进 D0-D6 候选检测那条流程,现在做就是无米之炊,留到那条流程真的建起来再回头接。

**如果你要看真实数据**（2026-07-08 更新,上面那句"还没迁移"已经是旧状态）：正式生产库(`data/formal/production_activation.sqlite3`)已经清空重建、按新表结构真实抓过28个账号,并且备料也已经补齐——库里全部430条爆款的转写+评论都已完成(`hits.reverse_status='completed'`),没有卡在待处理状态的。

## 4. 生产 Host 边界（host）

`[已归档]` 2026-07-11:`scripts/core/host/`(`production_host.py`,提供 `ProductionHostBridge` 概念)经全仓库真实 import 传递闭包核实——从真实生产入口(`run_competitor_registration_full.py`/`run_reverse_prep.py`/`run_source_to_topic.py`/`run_content_plan.py`/`run_script_generate.py`/`run_sample_deep_analyze.py`/`review_queue.py`)出发,没有任何一条路径依赖这个模块,只被自己家族的 `verify_goal_*.py` 自证脚本调用过。已归档到 `archive/dead_goal_chain_20260709/`,`scripts/core/` 下目前**没有**这一层。

**现状**:外部系统(飞书/Codex/Claude Code)目前并没有一个真正生效的"先变成 Host message 再经白名单进 Core"边界——`.claude/skills/*` 直接在 Claude Code 会话内读写业务库相关文件,`runtime_skills/` 经 `scripts/core/experience/run_*.py` 绑定脚本直接连业务 sqlite,都没有经过任何 Host 边界。如果未来真的需要这层隔离(比如要接入一个新的外部助手,又不想让它直接碰数据库),这是一次新的架构决策,不能假设归档代码可以直接复活复用——它当初设计时绑定的 `PersistenceStore`/`Goal03Scheduler` 中 `PersistenceStore` 部分还在(`scripts/core/persistence/`),但 `state/goal02_core.py`(`CoreCommandEnvelope`/`CoreMaterializer`)已经一起归档了。

## 5. 模型路由（model_gateway）

`[已完成]` 2026-07-11,按"清场式保留重构"要求补齐:

**唯一显性配置入口**:`config/model_routes.yaml`。文件顶部的 `model_positions:` 块点名系统运行期恰好三个模型位点:
- `dialogue_model`(对应 `model_routes.daily_chat`)——日常对话/系统状态说明。
- `business_model`(对应 `model_routes.business_analysis`)——选题判断、研究综合、关系判定等分析类 Skill 节点(`runtime_skills/` 里除 `content_plan`/`script_generate`/`script_review` 外的 9 个 Skill 都走这条)。
- `writing_model`(对应 `model_routes.writing_generation`)——钩子、大纲、成稿、润色、AI 味判定等创作生成类节点(`content_plan`/`script_generate`/`script_review` 走这条)。

**当前状态**:三个位点全部绑定同一个 provider `model_providers.mimo_main`(`type: mimo`,`provider_name: hermes`,真实凭据来自 `.env` 的 `HERMES_BUSINESS_API_KEY`/`HERMES_BUSINESS_BASE_URL`/`HERMES_BUSINESS_MODEL_NAME`/`HERMES_BUSINESS_MODEL_CLASS`)。三条路由都写死 `fallback: none`——`scripts/core/model_gateway/model_router.py` 的 `ModelRouter.from_config()` 会在任何路由的 `fallback` 不等于 `"none"` 时直接拒绝加载整个配置文件,不存在"允许一部分路由有 fallback"的中间状态。

**Claude Code / Codex 不是运行期 provider**:写这个仓库代码用的工具(Claude Code、Codex)和上面三个位点绑定什么模型完全无关——`config/model_routes.yaml` 里原来混入过一个 `engineering_execution` 路由(描述 code_editing/command_execution 等开发活动),2026-07-11 已删除,因为那本质是在给"写代码"这件事配一个"运行期业务路由",违反"开发工具 ≠ 业务 runtime 模型"这条规则。`scripts/core/model_gateway/business_route_registry.py` 里的 `BANNED_CLI_PATTERNS`/`HARDCODED_MODEL_PATTERN`/`LEGACY_ACTIVE_MODEL_PATTERNS` 三组正则会扫描 `scripts/core`/`runtime_skills` 下所有非 verify 文件,禁止出现 `claude`/`codex`/硬编码模型名字符串/`scripts.llm.call_llm` 这类旧式直连调用。

**换 provider 怎么做**:不是改代码,是改 `config/model_routes.yaml` 里对应位点的 `provider_ref`(先在 `model_providers:` 下新增一个 provider 定义,再把 `model_routes.<route>.provider_ref` 指过去)。`config/model_routes.example.multi_provider.yaml` 演示了 `business_model`/`writing_model` 换成一个 `openai_compatible_api` 类型 provider 的样子(`dialogue_model` 仍留在 Mimo)——注意这个示例里没有、也不应该出现任何 `codex`/`claude_cli` 类型的 provider。真实的"切到 GPT"流程见 `BUSINESS_MODEL_SWITCH_TO_GPT_PLAN.md`,要求原子切换、单一生效绑定、旧绑定仅作历史记录、不允许自动回退。

## 6. 业务工作流（workflow）

[占位] 待补：formal Skill 是怎么被组织成一条工作流的，一条工作流从触发到完成经过哪些环节。

## 7. 调度（scheduler）

[已完成] 2026-07-08:目前只有"每日采集"这一个真正接了真实定时的任务,别的都还是要手动跑或者安全默认关闭。

**现在开着的**:Windows 计划任务 `CreationAssistant_Daily`,每天本地时间08:00 自动跑一次每日采集(见第3节)。注册脚本是 `scripts/scheduled/register_daily_task.ps1`,实际执行的是 `scripts/scheduled/run_daily_incremental.bat`(负责切到项目目录、把输出记到 `logs/daily_incremental_YYYYMMDD.log`)。想改时间或者停掉,直接改这个 `.ps1` 脚本里的 `-At 8:00AM` 再重新跑一遍,或者用 Windows 自带的"任务计划程序"图形界面找到这个名字手动关。

**默认关闭、需要用户手动开**:`CreationAssistant_Listener`(常驻监听,用途还没重建)、真实的"逆向拆经验"批量调度(现在只能手动跑 `run_sample_deep_analyze.py`,见第10节)。这些为什么默认关着:都涉及"无人值守自动执行真实外部动作"(联网、调用付费模型),按项目规矩必须用户明确拍板才能开,不能因为技术上能做就默认打开。

**一个真实踩过的坑**:注册 `CreationAssistant_Daily` 的时候发现这个名字**之前就真的存在过一个任务**(上次运行是失败的),用覆盖注册的方式建的新任务,没能力找回旧任务原来配的是什么——如果以后要新建任何计划任务,先用 `Get-ScheduledTask -TaskName "名字"` 查一下是不是已经有同名的,不要直接覆盖。

## 8. 持久化与状态（persistence / state）

[占位] 待补：数据实际存在哪些库/表里，SQLite 和飞书的关系（谁是真相源），普通人想自己查一条记录该去哪查。

## 9. 纠错与实验（correction）

[占位] 待补：发布后发现文案跟审核通过的版本不一致，或者数据算错了，系统怎么处理，会不会影响已经用过的历史结论。

## 10. 经验库（experience）

[已完成](部分) "选题→大纲→成稿→文案优化/审核→最终稿"这条创作主链路六步全部接上真实数据(2026-07-08 首步,2026-07-09 三步接完到成稿,2026-07-13 置顶规则总表核对后再接文案优化/审核+最终稿两步,均有端到端集成测试验证全链路真的能跑通)。"归纳共性"(`tactic_extract`)2026-07-11 也接上了真实数据(见下方新增段落)。"自营P基线怎么用"这一块**表、登记函数、评估逻辑三层都已经建好**(2026-07-13,B1,置顶规则总表核对后,`BR-EXPERIENCE-004`):`own_publications_schema.sqlite.sql` + `own_publications.py`(`own_accounts`/`own_publications`/`own_publication_checks`/`own_publication_baselines`/`own_experiments` 五张表 + `record_own_experiment()`)负责把真实发布事实存进去;`scripts/core/business_data/run_publication_experiment.py`(新增)第一次真正调用了之前写好但全仓库从没人用过的 `goal09_experiments.py`——`compute_metric_signal()` 三态判断(观测/基线比率 ≥1.5 支持、≤0.8 不支持、中间存疑)+ `recompute_experience_state()` 的 `active`/`watch`/`paused`/`deprecated` 状态机(见下方"推荐状态状态机"小节)。而且现实约束摆在这:目前没有任何自营账号真的发布过内容,这整套链路只用构造数据测过(`tests/core/test_goal09_experiments.py` 26个 + `tests/core/test_run_publication_experiment.py` 13个),从没被真实数据喂过。

**这一层干什么**：把已经备好料(有转写文字稿+评论)的爆款,喂给大模型做"深度分析",提炼出可复用的选题手法/开头手法/结构手法——旧系统管这叫"DNA拆解",现在不这么叫了,对应的是 `runtime_skills/sample_deep_analyze` 这个独立的原子能力。分析结果再喂给 `runtime_skills/source_to_topic` 生成候选选题,候选选题再喂给 `runtime_skills/content_plan` 规划钩子(开头)和大纲,大纲再喂给 `runtime_skills/script_generate` 写出成稿草稿,草稿再喂给 `runtime_skills/script_review` 做文案优化+审核+AI味判定,审核通过的润色稿最后被复制成一条"最终稿"等待单独确认——这是"选题→大纲→成稿→文案优化/审核→最终稿"这条创作主链路的全部六环,`tests/core/test_topic_to_script_chain_integration.py` 会真的把一条测试爆款从头跑到尾,验证 `hit_deep_analysis → topic_candidates → content_plans → script_drafts → script_reviews → final_drafts` 这条外键链路真的能走通,不是六段各自独立能跑但拼不起来。

**为什么"能力"和"接线"是分开的两件事**：`runtime_skills/` 下的原子 Skill 本身早就写好了,而且真的用真实模型调用验证过能跑通——但那套验证用的是一个专门隔离出来的验证库,不是真实生产数据库。也就是说,"这个能力本身没问题"和"能不能真的接到日常爆款数据上用"是两件独立的事。这么设计是为了保证 Skill 本身"原子化"——不管换到别的项目、还是被单独拿出来调用,Skill 都不需要知道任何关于这个项目数据库长什么样的信息,只认自己的输入输出格式。真正懂数据库的部分,全部写在 `scripts/core/experience/` 下对应的绑定脚本里(**这一层故意不放在 `business_data` 目录下**,因为 `business_data` 那层被强制要求不准碰大模型,分析/选题/规划这些步骤恰恰整个就是在调用大模型)。

**怎么把一条真实爆款变成一份成稿草稿**：
1. `python -m scripts.core.experience.run_sample_deep_analyze --limit N`——挑一条已经完成备料(转写完成)、但还没做过分析的爆款,只把大模型"分析"这一步真正需要的东西喂给它:文字稿(超过7000字才会截断)、点赞/评论/收藏/转发数、领域标签——**不会把数据库内部的字段名、内部编号这些东西传进去**。拿到结果(选题手法/开头手法/结构手法三段话)后存进 `hit_deep_analysis` 表,带版本号,不覆盖旧分析。
2. `python -m scripts.core.experience.run_source_to_topic --limit N`——挑一条已经分析过、但还没生成过候选选题的分析结果,把"选题手法/开头手法/结构手法"三段话当证据喂给 `source_to_topic`,生成一条候选选题(标题+切入角度+支撑证据)。**关联判断(这条选题是否和已有内容重复/冲突)目前是老实的占位文字**,不是真判断过——那是另一个还没接的独立能力(`content_relation_judge`)的活,没有冒充。结果存进 `topic_candidates` 表,同样带版本号。
3. `python -m scripts.core.experience.run_content_plan --limit N`——挑一条状态是"已生成"(`topic_status='generated'`)、但还没规划过的候选选题,喂给 `content_plan` 生成开头候选(1-5个)、选定开头、分段大纲。**一处替代已经在 B2(2026-07-13)接上真数据**:"打法候选"现在优先查这条选题所在领域真实处于 `active`/`watch` 的 `tactic_state` 行(`tactic_extract` 归纳出的真实 `common_patterns`,按 active 优先、再按最近更新时间排序,最多12条),只有当这个领域还没有任何方法离开过 `candidate` 状态时,才退回旧的替代(复用同一条爆款自己的选题/开头/结构手法)——**现实情况是这次写这段文档时,所有领域的全部方法确实都还停在 candidate**(见下方"推荐状态状态机"小节),所以这条真实路径目前还没有在真实调用里被触发过,只在测试里验证过。另**一处明确的替代未变**:理应来自范例库(物理载体还没定)的"风格范例",现在复用同一条爆款自己的真实转写文字稿摘句,仍是真实数据、老实标了替代关系。**2026-07-11 更正**:这句话曾经写着"已核实这两项不影响模型实际生成的内容(只读了brief/style_examples)"——那是当时的真实情况,但也是一个真实bug(`tactic_candidates`/`evidence_items` 两个契约要求必传的字段压根没发给模型),这次已经修复,现在这两项**会**真实影响模型输出,不再是"不影响"。结果存进 `content_plans` 表,同样带版本号。
4. `python -m scripts.core.experience.run_script_generate --limit N`——挑一条还没生成过草稿的规划,把选定的开头、分段大纲、支撑证据喂给 `script_generate`,产出一份成稿草稿(50-6000字)。**一处明确的替代**:理应由 `research_evidence_extract`/`production_research_plan`(都还没接)产出的"研究摘要",现在是一句老实标注"未经真实研究流程,仅汇总已有证据"的文字,不冒充真研究过。结果存进 `script_drafts` 表,同样带版本号。
5. `python -m scripts.core.experience.run_script_review --limit N`——挑一条已通过审核的成稿草稿,喂给 `script_review`(内部三个子节点:先 `creation_polish` 文案优化、再 `creation_review` 审核、最后 `ai_flavor_judge` 判AI味——**2026-07-13 之前顺序是反的**,先审核再优化,置顶规则总表核对后改成先优化后审核)。**一处明确的替代**:理应来自范例库的"人类参考文本"(`human_reference_refs`),现在复用同一条爆款自己的真实转写文字稿摘句——跟 `content_plan` 的 `style_examples` 同款替代思路,但**这里没有placeholder兜底**,转写文字稿缺失时直接报错,不会拿一句系统占位文字冒充"真实人类写作参考"去做AI味判断。产出润色稿+审核意见+AI味风险,存进 `script_reviews` 表。
6. `python -m scripts.core.experience.run_final_draft --limit N`——挑一条已通过审核的 `script_reviews` 行,把它的润色稿原样复制成一条新的"最终稿"记录,等待**单独的**人工确认——审核通过不等于最终稿通过,这是置顶规则总表明确要求的两个独立确认动作。存进 `final_drafts` 表。往后没有下一步了,发布是人工手动做的事,不在这条链路里。

**人工审核闸门(2026-07-10 新增,2026-07-13 扩展到五段)**:`topic_candidates`/`content_plans`/`script_drafts`/`script_reviews`/`final_drafts` 五张表各有一个 `human_review_status` 字段,新产出的一行默认是 `pending_review`(待审核)。**没有人明确标记"通过",内容不会自动流到下一步**——第2步查询"还有哪些分析结果没生成选题"不受这个字段影响,但第3/4/5/6步分别只会挑上一步已经 `human_review_status='approved'` 的行。这是补给之前一个真实缺口的:最早接这条链路的时候,几步命令挨个跑,中间完全没有人看一眼的环节。审核用这个命令:
```
python -m scripts.core.experience.review_queue --list          # 看现在有哪些在等审核(人能读的摘要,不用查数据库)
python -m scripts.core.experience.review_queue --approve topic t1_topic_v1   # 通过
python -m scripts.core.experience.review_queue --reject plan p1_plan_v1 --note "开头太标题党"  # 不通过,可以附一句理由
```
`--list` 后面可以跟 `topic`/`plan`/`draft`/`review`/`final` 只看某一段(`review`/`final` 是 2026-07-13 新加的)。**现在还没有任何界面**,只有这个命令行工具——先把"必须有人确认"这道硬闸门加上,界面好不好用是以后的事,不能因为没界面就放过审核这一步。

**归纳共性——`tactic_extract`,以及"归纳出来的东西存哪儿"这层地基(2026-07-11 新增)**：这一块之前一直没做,不只是因为 Skill 没接,还因为深挖之后发现"归纳完的结果压根没地方正式存"——负责这件事的底层机制(`core_command_envelope` + `tactic_state`/`topic_state`/`claim_state`/`experiment_state`/`production_task_state` 五张并列的表,`goal02_schema.sqlite.sql` 里写好了但全仓库从没一行 Python 代码用过)必须先补上,才谈得上真的"归纳"。分两层:
- `scripts/core/persistence/goal02_store.py`(新增)——通用的"创建一条状态记录/往前流转状态"机制,横跨五种对象类型(仿照 `PersistenceStore` 本身通用横跨 goal01 的做法)。**这次只给"打法/方法"(tactic)这一种类型接了真实调用**,另外四种(选题/待核实说法/实验/生产任务)的表结构已经通用支持,但具体每种状态该怎么排序还没设计,调用会明确报错而不是瞎猜。
- `scripts/core/experience/tactic_registry.py` + `scripts/core/experience/run_tactic_extract.py`(新增)——把 `evidence_registry.py`(下一段说)登记好的一批真实证据,喂给 `runtime_skills/tactic_extract`,归纳出来的"共同规律"(`common_patterns`)和"范例候选"(`example_candidates`)正式存成一条 `tactic_state` 行(初始状态 `candidate`,候选状态),并且记下这条结论是从哪几条真实证据归纳出来的,查得回去。

**证据怎么变得"可以被引用"——`evidence_registry.py`(2026-07-11 补写文档,代码本身在这之前已经做好并跑过真实数据)**：`sample_deep_analyze` 分析出的每一条真实结果(`hit_deep_analysis` 表里的一行),要先经 `scripts/core/experience/evidence_registry.py` 的 `register_hit_deep_analysis_evidence()` 登记成一份正式、带内容哈希、可追溯的"证据"(`trace_version`),`tactic_extract` 才能引用它——这是一次性的"确权"动作,幂等(重复登记同一条不会变成两条)。

```
python -m scripts.core.experience.run_tactic_extract --domain-label fan_kepu_social_life --limit 20
```
一次调用最多归纳 2-20 条同领域的证据(`tactic_extract` 自己的契约规定,2026-07-11 从 12 条上调到 20 条)。每条证据摘要的字符上限先从160调到320、再到450,**最后按用户明确指示彻底取消了逐字段截断**——现在每条证据的选题/开头/结构手法三段真实内容原样完整传给模型,不再按固定字数硬切;`dna_note_ref_max` 留了一个2500字符的schema声明式上限(数学上算出来的真实天花板,不是拍脑袋的数字,正常情况永远碰不到,细节见 `TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml` 注释)。**不同领域不能混在一次调用里**,这一点没变。

**`tactic_extract` 真实调用现状(2026-07-11,7次,第7次成功)**:前6次依次失败:①当时"共同规律"数量上限是12,真实模型对20条丰富材料归纳出60条,被拦下,诊断发现内容质量也不合格(词汇堆砌+0范例候选)——往上查根因,发现全部12个业务Skill的真实运行时提示词系统性写得很糙(`script_review`三个子节点完全没传任务内容),已按优先级全部修过一轮;②`schema_version`被埋在提示词中间,AI生成到最后遗漏了这个字段,改成结尾再提醒一次;③证据选择函数没有"只取每个hit最新版本"的过滤,会优先选到重新拆解前的旧版低质量证据;④`example_candidates`提示词没说清楚"每条要写成一个字符串",AI给了结构化JSON对象被拒收。用户要求先把全部21个真实hit重新拆解一遍(`sample_deep_analyze`,21/21成功,质量对比过,语言问题清零、"选题手法"变得真正可复用),重新登记证据后第7次调用**真实成功**:产出6条互不相同、各引用2个真实视频为证的"共同规律"+6条范例候选,存进 `tactic_state`(`state='candidate'`)——这是这条链路第一次真正跑通产出可用结果。

**推荐状态状态机(2026-07-13 新增, B1, 原文档"7 推荐状态状态机", `BR-EXPERIENCE-004`)**:`tactic_state` 之前(c3f8e70 那次提交)被建成跟其它四张 `*_state` 表共用同一套"只能前进、绝对不能后退"的触发器,而且 `CHECK` 约束漏了 `watch` 这个词——这是没查到原文档就建错的真实 bug,不是设计取舍。这次改成:`CHECK` 加回 `watch`;触发器改成只挡两种真正的单向边界(离开 `candidate` 之后不能再回去、`deprecated` 是唯一真正的终点),`active`/`watch`/`paused` 三者之间随便怎么来回流转都放行,由 `recompute_experience_state()` 的业务逻辑决定该往哪走,不是数据库层硬挡。三态之间的规则:自最近一次正式P支持起算,出现1次新失败或2个独立外部反例 → `watch`(外部反例单独出现不够,必须凑够2个;反例永远不能单独导致 `paused`);同一窗口内累计3次不支持且覆盖3个不同"核心问题"(同一问题重复失败只算一次) → `paused`;出现一条晚于当前所有风险证据的新支持 → 回到 `active`。`paused` 的恢复规则原文档明确写的是"重新计数"而不是同一套窗口:自最近一次失败起算,要攒够2次支持且覆盖2个不同核心问题才能回到 `active`,期间只要再冒出一次新失败,这个计数就清零重来。"核心问题"这个维度来自人工登记实验时必填的 `own_experiments.core_question` 字段(系统不会替人猜同一条方法下测的是不是同一个问题)。`run_publication_experiment.py` 每评估完一次实验,就重新拉这条方法名下全部已判定的历史实验、按发布时间真实排序、跑一遍这套规则,状态真的变了才落一次 `tactic_state` 转移,没变就不写。

**闭环端到端验证(2026-07-13 新增, C)**:`tests/core/test_experience_closed_loop_integration.py` 用一条真实注册出来的 tactic(走 `tactic_registry.py`/`evidence_registry.py` 的真实注册路径,不是测试专用的简化写法)+ 一串构造出来的自营发布数据(不用等真实7天),把 A2(自营发布/基线)→ B1(`run_publication_experiment.py` 真实评估)→ B2(`content_plan` 真实打法归因)整条链路真正串起来跑了一遍完整循环:候选→晋升 active → 1次失败进 watch → 1次晚于风险的新支持恢复 active → 3次覆盖3个不同核心问题的失败进 paused(此时 `content_plan` 真的看不到这条方法了)→ 恢复不足(1次支持)仍留在 paused → 新失败让恢复计数清零 → 2次覆盖2个不同核心问题的支持恢复 active(`content_plan` 又能看到它了)。这条测试证明的是"这几块拼起来真的能跑成一个完整循环",不是重新验证每一块各自的边界情况——那些已经在各自的单元测试里覆盖过。**仍然没做、也不在这次范围内的**:真的等一个自营账号发布内容、真的等7天、真的观察一次 Day7 结果——这条测试只证明状态机逻辑对构造数据是对的,不是说已经被真实数据验证过。

**明确没做的**（不是遗漏,是天然排在后面,见 `ROADMAP.md`）：
- **候选(candidate)之后怎么变成正式生效(active)——这一步仍然没有专门的自动/人工入口**。`goal02_store.py` 的 `transition_state()` 现在已经有真实调用方了(`run_publication_experiment.py`,B1 新增),但只覆盖"已经离开 candidate 之后"的 `active`/`watch`/`paused` 流转——从 `candidate` 第一次晋升到 `active` 这一步,目前测试里是直接手工调 `transition_state()` 完成的,生产环境下这一步该由谁在什么条件下触发,还没设计。
- `content_plan` 里"打法候选"字段的真实接线已经在 B2(2026-07-13)做完(见上文第3步),但因为上一条("候选之后怎么变成active"没有真实入口)还没解决,目前没有任何领域有真的 `active`/`watch` 方法可用——这条真实路径还没在生产环境里被真正触发过,只在测试里验证过 active 优先于 watch、candidate/paused/deprecated 被正确排除。
- **选题目前只用了"对标爆款+评论区"两种候选来源**——研究缺口/当下热点两种候选来源还没接。**2026-07-11 澄清:当前设计没有冻结"选题打分制"**,`.claude/skills/选题/SKILL.md` 里描述的"选题判断维度"评分排序是该交互式技能自己的旧方法,不代表 `runtime_skills/source_to_topic` 当前设计要对齐的目标,不得当作缺口去补。真正的缺口是:当前设计里的选题判断规则、领域约束、去重/冷却、候选来源、人工确认门槛,有没有被真实代码落地并验证——这是 2026-07-09/11 用户直接指出并两次纠偏过的真实缺口,详见 `ROADMAP.md`。**2026-07-13 更新**:补上了第三种来源"领域话题标签搜索"(见下方新增小节),但"搜到的视频怎么变成正式选题"这一环还没接(见下方说明)——真正能喂给 `source_to_topic` 的候选来源目前还是只有两种。
- **`sample_deep_analyze`/`tactic_extract`已经真花过钱且已验证内容质量**(见上)。`source_to_topic`/`content_plan`/`script_generate`/`script_review` 这四步仍然一次都没真调用过真实大模型,测试用的还是各 Skill 自带的"假模型"——但这四个的提示词这次已经系统性修过一轮已知缺陷,不是带着确认过的bug等下次调用。`final_draft` 不调用大模型(纯拷贝润色稿),不算在这条"还没真调用"的清单里。
- **"研究"这一步没有独立的人工确认闸门**。置顶规则总表要求的六个可暂停节点是"研究/内容计划/初稿/文案优化/审核/最终稿",这次(2026-07-13)补齐了后面五个,但"研究"本身目前没有真实绑定脚本(`research_evidence_extract`/`production_research_plan` 两个原子 Skill 都还没接真实数据,`script_generate` 的"研究摘要"字段现在是老实标注过的占位替代,见上文第4步)——没有真实产出,自然也没有东西可以让人审核。等研究这一步真的接上真实数据后,要补一张 `research_outputs`(或类似)表 + `review_queue.py` 的 `research` 阶段,现在先不做。

**怎么跑**：见上面六条命令依次执行,每次生成完记得跑一下 `review_queue.py --list` 看有没有要审核的,通过了才会继续流到下一步。`N` 是这次要处理几条,默认1条。也可以直接跑 `python -m pytest tests/core/test_topic_to_script_chain_integration.py` 看一次完整的假数据端到端演练(含全部五道审核通过的环节,不花钱、不联网)。

**领域话题标签搜索——第三种选题来源(2026-07-13 新增, 置顶规则总表核对后, 原文档第21章)**:每个领域一份 `sources.yaml`(人工写初始"活跃标签",比如"房产中介""运动鞋"这种短标签,不是完整选题),`scripts/core/business_data/run_domain_search.py` 读进来建成 `domain_search_tags` 表。爆款库里真实视频描述里的 `#xxx` 标签也会被扫出来,但只会落成"建议"状态,要在 `review_queue.py --approve tag <id>` 里人工确认才转正成"活跃"——不自动转正。活跃标签**默认7天轮换搜索一次**,每次调真实的 MediaCrawler 关键词搜索模式(之前只有"按账号抓"能用,这次把工具本来就有的"按关键词搜"模式接上了),读回来的结果先做零成本的确定性过滤(去重+标签真的命中)、再按热度粗排,最多留5条。搜到的视频存进独立的 `discovered_external_videos` 表,**不写进 `hits`/`competitor_videos`**——因为这些视频没有正式追踪账号那样的基线数据,不能跟正式证据混用。真实抓取默认关闭(`config/settings.yaml` 的 `domain_search.live_enabled: false`),这次只跑通了"关闭状态下该报错、打开后该怎么跑"这条逻辑链路,没有真的花钱搜过。**明确没做的**:搜到的视频怎么变成一条真正的 `topic_candidates`,现在完全没有路径——`topic_candidates.source_analysis_id` 是必填外键,指向 `hit_deep_analysis`,而这些搜索发现的视频压根没有走过"深度分析"这一步,这条路径需要专门设计,这次没做。

**账号发现复查(2026-07-13 新增, 原文档7.1)**:领域话题标签搜索搜出来的视频,如果发现"同一个还没追踪的账号,30天内有至少3条不同视频"命中了同一个领域,`scripts/core/business_data/run_account_discovery.py` 会建一条复查任务——不是搜到一次就建,系统也不打分,只报告"这个账号客观上反复出现"这个事实。这条任务需要人工三选一:"加入"(值得追踪,但这个函数本身不会自动注册,还是要走现有的 `register_competitor_accounts.py` 正式注册流程)/"忽略30天"(30天后如果又满足条件会重新触发)/"永久忽略"。**明确没做的**:原文档要求复查时"拉这个账号最近10条可访问视频"重新统计"领域相关/口播适配/独立选题/排除内容"四类数量——这次没有做真的重新拉取(需要一次新的真实网络请求),也没有实现这四类判断需要的规则(这些偏语义判断,不是简单能写死的规则),诚实地只用已经发现、已经落库的真实视频数量做统计。

**热点转化——人工登记 MVP(2026-07-13 新增, 原文档第19章, BR-TOPIC-006)**:原文档自己说"自动热点数据源当前属于未完成节点",MVP 阶段本来就是靠人工在飞书里输入。这次只做了这一段的最小闭环:`python -m scripts.core.business_data.run_hotspot_registration --domain-label <领域> --text "<热点文本>" --created-by "<登记人>" --db <数据库路径>`,把一条热点文本存成 `hotspot_events` 表里的一行,并用当前领域已经"活跃"(`active`,不含还没人工确认的 `suggested`/`suggested_pause`)的话题标签(A3 的 `domain_search_tags`)做确定性关键词重合匹配,算出 `matched_tags`——不调 LLM 打分,零命中是诚实的正常结果,不是错误。**明确没做的**:这不是真的飞书接入(`source` 字段目前只有 `feishu_manual` 一个值,是给未来真接了飞书预留的语义,这次的真实登记入口是命令行,见 CLAUDE.md 关于飞书现状的说明);TrendRadar 或任何自动热点抓取整体不在这次范围内,是原文档自己标注的"未完成节点"的一个可选后续扩展。

## 11. 外部适配器（external_adapters / hermes）

[占位] 待补：这两个模块具体接的是什么外部系统、能做什么不能做什么。

## 12. 研究（research）

[占位] 待补：选题研究这一步怎么触发、用不用联网搜索、结果长什么样。

## 13. 日常使用：一个典型的一天

[占位] 待补：等主要模块都写完之后，最后补一个"用户视角"的完整例子——今天系统做了什么、我需要看什么、需要点哪些按钮。这一章应该放在所有模块都至少有一版内容之后再写，避免先写例子、模块细节又对不上。

## 14. 出问题了怎么办

`[已完成]` 2026-07-09（这一节从占位补成实际内容，起因是 2026-07-09 的一次外部工程审计——见下文"审计发现"——审计过程中新建了三个检查脚本，按项目自己"模块建好即写手册"的纪律在同一批工作里补上说明，不留到"以后"）

### 收工/交接前，跑这一条命令就够了

```
python -m scripts.validation.preflight_checkpoint_check
```

这一条命令把"执行纪律"要求的几件事一次性跑完，不用再分别记着做：
1. `git status` 是否干净（没有该提交但忘了提交的改动）
2. 全部测试是否绿（`tests/core` + `tests/validation`）
3. 权威闸门 `verify_goal_v062_phase8_readiness.py` 是否 `ENGINEERING_READY`
4. 运维基础设施检查（见下一节）是否干净
5. 真实生产数据库健全性抽查（见下下节）是否干净
6. 有没有新加的业务常量没写来源（见下下下节）

任何一项没过，命令会打印具体是哪一项、哪个文件/哪条测试没过，返回码非0。想跳过测试只看另外几项（比如中途快速自查）可以加 `--skip-tests`，但**收工前的最终检查不要跳**。

### 运维基础设施检查（凭证、硬编码路径、忽略规则）

```
python -m scripts.validation.ops_infra_checklist
```

这道检查管的是业务规则文档管不到的一类问题——2026-07-09 审计发现的真实案例：本地 ffmpeg 和 ASR Python 解释器的绝对路径被硬编码在两个文件里、且不在任何配置文件中，换机器就会失效，而且这类问题不会被任何 `BR-*` 业务规则闸门发现，因为它压根不是业务规则。现在这个脚本机械检查三件事：
1. 有没有新的硬编码绝对路径（`C:/`、`I:/`、`/Users/`、`/home/` 这类）混进了非测试的正式代码——真实值应该放进 `config/settings.yaml`，不是写死在代码里。
2. `.gitignore` 是否仍然覆盖了敏感/机器本地文件（`.env`、真实的 `config/settings.yaml`、`*.sqlite3`、`vendor/`、`logs/`、`data/`）。
3. 有没有真实的 `.env`/`.env.*` 文件被误提交（只允许 `.env.example` 这类模板）。

### 真实生产数据库健全性抽查

```
python -m scripts.validation.production_data_sanity_check
```

跟 `tests/core`/`tests/validation` 不一样——那些测试用的是构造出来的假数据，这个脚本直接连真实的 `data/formal/production_activation.sqlite3`（本地文件不存在时会优雅跳过，不报错）。检查内容对应 2026-07-08 真实踩过的坑："自动触发备料"曾经因为表默认值漂移而静默失效，导致新爆款卡在 `reverse_status='pending'` 却没有任何东西真正处理它，好几天没人发现。现在脚本检查：必需的表都在、有没有 `hits` 行卡在 `pending`/`running` 状态超过3天、有没有 `hits` 引用了不存在的账号。

### 新加的业务常量有没有写来源

```
python -m scripts.validation.unsourced_constant_check
```

2026-07-10 新增，起因是一次真实的"防跑偏机制当场失效"：给 `source_to_topic` 接评论区证据时，随手定了一个"取最热3条评论"的数字，写了一段解释"为什么用评论"的注释，但**从没说这个"3"本身是编的，没有任何文档依据**——用户追问"这个数字是你随手定的还是文档写的"才发现。这跟项目历史上 `baseline_min_samples=10` 那次真实事故是同一类错误，而且是在专门整治这类问题的同一次会话里又发生了一次。

这道检查管的是：`scripts/core/business_data/`、`scripts/core/experience/` 里任何新出现的模块级大写常量（比如 `TOP_COMMENTS_PER_HIT = 3` 这种），前后几行注释里必须有下面三种之一，缺哪种都不行：
1. 引用了 `BR-*`/`GATE-*` 这类业务规则编号；
2. 引用了某个 `.yaml`/`_CONTRACT`/`_SCHEMA` 文件（说明这个数字是从契约/schema 抄来的，不是编的）；
3. 明确写了 `UNSOURCED`，老实承认"这个数字没有依据，是拍脑袋定的，需要确认"。

**这道闸门管不住"数字对不对"，只管得住"有没有老实说这个数字从哪来"**——一个诚实标注"UNSOURCED"的数字能通过检查，一个看起来言之凿凿但其实没来源的数字过不了。这是它的边界，不是它的漏洞：逼着写代码的人（包括AI）每次都要老实回答"这个数字我是编的还是有依据的"，不能含糊过去。

### 非对话触发的定期自检（可选，需要你自己决定要不要装）

前面这几个检查目前都是"想起来手动跑"或者"收工前跑一次"。如果想要**不用记得跑、每次提交代码后自动检一遍**，有一个现成但**默认没有安装**的选项：

```
powershell -ExecutionPolicy Bypass -File scripts\scheduled\install_post_commit_hook.ps1
```

装上之后，每次 `git commit` 完成后会自动跑一遍权威闸门 + 运维基础设施检查 + 生产数据库健全性抽查（几秒钟，不跑全部测试，太慢会导致提交体验变差，反而没人愿意用），发现问题只在终端打印提醒，**不会阻止提交、不会改动任何东西**。要卸载就删掉 `.git\hooks\post-commit`。这一步是可选的，故意没有帮你自动装上——装不装是你的选择，不是脚本自己决定的。

**这个脚本管不到、需要人工定期核对的运维项**（没有假装自动化，说清楚现在还得靠人）：
- Windows 计划任务是否健康：`Get-ScheduledTask -TaskName CreationAssistant_Daily | Select TaskName,State`，`Get-ScheduledTaskInfo -TaskName CreationAssistant_Daily` 看 `LastTaskResult` 是不是 0（失败会是非0错误码）。
- 生产数据库最近一次备份是多久之前——`data/formal/production_activation.sqlite3` 的时间戳备份副本（命名规律是 `production_activation_pre_<操作>_<时间戳>.sqlite3`，可以直接在 `data/formal/` 下用文件名搜）多久没更新过。
- 第三方依赖（`vendor/MediaCrawler`、本地 ASR 模型 `I:/AI_Models`）是否还是当初验证过的版本，有没有被后续操作意外改动。

### 测试怎么跑

```
python -m pytest tests/core tests/validation -q
```

（`python -m unittest discover` 在这个仓库跑不起来——`tests/` 下没有 `__init__.py`，unittest 的旧式 discovery 会报 `Start directory is not importable`；用 pytest。）

### 日志在哪看

- `logs/daily_incremental_YYYYMMDD.log`：每日定时增量采集的完整输出。
- `logs/reverse_prep_*.log`：手动批量跑备料流水线时的输出（文件名里带具体动作，比如 `_backfill_`/`_daily_backlog_`）。
- Windows 事件层面的计划任务历史：`Get-ScheduledTaskInfo -TaskName CreationAssistant_Daily`（`LastRunTime`/`LastTaskResult`），不是文件日志，是系统状态。

### 不懂代码，怎么描述问题给下一个开发者/AI

不用去猜是哪个函数的问题。把下面这三样直接贴给下一个人（或下一次对话）就够：
1. `python -m scripts.validation.preflight_checkpoint_check` 的完整输出（哪一项失败、失败详情）。
2. 你实际做了什么操作、看到了什么和预期不一样的现象（比如"定时任务弹出了一个黑框窗口"这种，不用翻译成技术术语）。
3. 如果是数据看起来不对，直接说"我看到 XX 数字是 Y，我以为应该是 Z"，不用自己先猜原因——本项目的纪律要求任何人（包括AI）核实问题时都要去查真实数据/真实闸门输出，而不是听转述就下结论。

## 15. 工程验收脚本（production / runtime / staging）

[占位] `scripts/core/production/`、`scripts/core/runtime/`、`scripts/core/staging/` 这三个目录跟前面几章不一样——不是持续运行的业务模块，是**每个 GOAL 自己的一次性验收/就绪检查脚本**（比如 `verify_goal_v062_phase8_readiness.py` 这类，本次会话反复用来核实"完成"是不是真完成）。待补：这些脚本各自对应哪个 GOAL、还有没有在被使用、要不要清理掉已经彻底结束的 GOAL 留下的脚本。
