---
mode: implementation
stage: stage_1_daily_discovery
design_complete: true
implementation_authorized: true
external_calls_authorized: false
environment_changes_authorized: false
formal_data_writes_authorized: false
production_authorized: false
completion_status: SOURCE_TO_TOPIC_STAGE1B_ENTRYPOINT_FIX_IMPLEMENTED
stage_status: TECH_TESTED_PENDING_LIVE_VALIDATION_AUTHORIZATION
source_evidence_level: LIVE_RUN_REJECTED
claimed_source_level: NONE
next_action: close_entrypoint_fix_authorization_after_commit_and_wait_for_user_live_validation_decision
baseline_sha256: 6822f5e121073f321b2a7d5a2ae1043d1bb46c001ca72edc8263371f2d2b70e8
base_commit: 0dff7265d5ca424339b1a0db397dc88461634bc9
allowed_design_sources:
  - docs/EFFECTIVE_DESIGN_BASELINE.md
execution_state_sources:
  - docs/IMPLEMENTATION_EXECUTION_BASELINE.md
  - execution/current_stage.yaml
prohibited_design_sources:
  - V0.x documents
  - CURRENT_DESIGN_BASELINE.md
  - DESIGN_AUDIT.md
  - old Goal documents
  - ROADMAP
  - BUILD_PLAN
  - Git history old designs
  - old code comments business rules
requirements:
  - id: STAGE1-HOTSPOT-DESIGN-CORRECTION-001
    description: Stage 1B 已在代码和直接测试中把合格发现来源接入已确认的 source_to_topic 原子 Skill 合同；真实来源、模型调用、正式库写入、production_daily 和 validation_live 仍未授权。
    baseline_refs:
      - docs/EFFECTIVE_DESIGN_BASELINE.md#3.3.1 热点转选题的受控搜索与 Skill 审核
      - docs/EFFECTIVE_DESIGN_BASELINE.md#20.1 来源与选题 Skill
      - docs/IMPLEMENTATION_EXECUTION_BASELINE.md#当前执行指针
    tests:
      - [python, -m, pytest, tests/test_workflow_guard.py, tests/core/test_stage1b_daily_discovery.py, tests/core/test_source_to_topic_skill.py, tests/core/test_run_source_to_topic.py, -q]
allowed_paths:
  - docs/IMPLEMENTATION_EXECUTION_BASELINE.md
  - execution/current_stage.yaml
  - scripts/core/production/stage1b_daily_discovery.py
  - scripts/core/production/stage0_content_core.py
  - tests/core/test_stage1b_daily_discovery.py
  - TECHNICAL_MANUAL.md
frozen_acceptance_tests:
  - tests/core/test_local_trendradar_executor.py
frozen_contracts:
  - BUSINESS_MODEL_ROUTE_REGISTRY.yaml
forbidden_actions:
  - business_code_change
  - stage1_acceptance_test_change
  - any_external_source_call
  - any_model_call
  - environment_install_or_change
  - formal_data_write
  - production_daily
  - validation_live
  - stage1a_handoff
  - research_stage_entry
  - automatic_retry
  - workflow_guard_bypass
---

# V1.3 实施执行基线

> 本文件是当前项目唯一的实施执行基线：它把 [`EFFECTIVE_DESIGN_BASELINE.md`](EFFECTIVE_DESIGN_BASELINE.md) 映射到已核实的 Git、代码、数据与测试事实，并规定唯一续写动作。它不重定义、补充或覆盖任何业务设计；发生冲突时，有效设计基线优先。

## 机器可读工作流状态

`execution/current_stage.yaml` 是 `scripts/workflow_guard.py` 唯一读取的当前工作流状态。本文顶部 YAML 只保留为人工对照，不作为门禁事实源。`baseline_sha256` 固定校验 `docs/EFFECTIVE_DESIGN_BASELINE.md` 的完整文件 SHA-256；`base_commit` 是本轮允许改动相对于的冻结提交。每项 `requirements` 必须同时提供现行文件中的 `baseline_refs` 和可执行 `tests`，缺一即不允许完成。

## 工作流硬门禁

- `start` 在任何代码修改前校验 `execution/current_stage.yaml`、设计完整性、基线哈希、需求映射、允许路径、实施授权、外部调用授权和环境改动授权。`design_complete: false` 必须返回 `BASELINE_GAP`，业务代码、真实运行、安装和外部调用不得开始。
- `check` 校验当前改动范围；任何不在 `allowed_paths` 的改动、未授权业务代码改动或冻结验收测试改动返回 `SCOPE_MISMATCH`。声明或检测到未经授权的外部调用、安装或环境改动请求时返回 `USER_AUTH_REQUIRED`。
- `finish` 只接受全部已暂存、没有未暂存或未跟踪文件的确定版本，运行每项 requirement 的去重测试命令并生成与当前 Git 索引绑定的 finish 凭证。测试、范围、哈希、需求映射或授权任一未通过，不得提交；`design_complete: false` 时即使本次治理任务允许提交，也必须输出 `task_finish_status: PASS` 与 `stage_status: BASELINE_GAP`，不得宣布业务完成。
- Git pre-commit 钩子只接受与当前索引完全一致的 finish 凭证。`--no-verify`、移除钩子、伪造凭证、直接调用真实来源/模型或修改门禁结果均属于 `workflow_guard_bypass`。
- `baseline_gap_resolution` 且改动完全位于治理白名单时，`finish` 可以完成门禁、章程、执行状态或已确认有效基线条款本身的受控变更，但输出仍保留 `stage_status: BASELINE_GAP`、`implementation_authorized: false`、`production_authorized: false`；这不表示业务 Stage 已通过，也不授权任何业务实现。若用户已确认新口径并写入唯一有效基线，`next_action` 可以指向设计完成确认与验收测试授权。

机器状态码固定为：`PASS=0`、`BASELINE_GAP=10`、`SCOPE_MISMATCH=11`、`USER_AUTH_REQUIRED=12`、`REQUIREMENT_GAP=13`、`TEST_FAILED=14`、`FINISH_REQUIRED=15`、`WORKTREE_NOT_READY=16`。调用方必须按状态码失败关闭，不得只解析自然语言。

## BASELINE_GAP_RESOLUTION 默认边界

默认 Stage 为 `stage_1_daily_discovery`，`design_complete: false`，`stage_status: BASELINE_GAP`。该模式只允许根据 `docs/EFFECTIVE_DESIGN_BASELINE.md` 识别具体缺口、说明缺口影响、输出 `BASELINE_GAP` 并等待用户提供或确认新口径；`docs/IMPLEMENTATION_EXECUTION_BASELINE.md` 只用于执行状态，不得用于补造业务设计。禁止搜索或读取旧材料寻找答案，禁止自行提出旧规则恢复方案，禁止修改业务代码、配置、数据库、业务测试、真实来源、真实模型、安装或正式运行。

禁止作为当前设计恢复来源的材料包括但不限于：V0.x 文档、`CURRENT_DESIGN_BASELINE.md`、`DESIGN_AUDIT.md`、旧 Goal、ROADMAP、BUILD_PLAN、Git 历史中的旧设计和旧代码注释中的业务规则。任何来自这些材料的内容不得据此修改有效基线或代码。

## Stage 1 热点源单次真实验收授权

用户已明确确认：“恢复 Stage 1 热点选题测试，并允许这一次真实热点和模型调用。”本次只恢复 `STAGE_1_HOTSPOT_SOURCE_VALIDATION` 这一条窄边界，视为该单次验收的设计已完整，不代表 Stage 1 其他来源、正式日产或后续阶段设计已恢复。

本次允许项目 Runtime 手动执行一次真实 TrendRadar 热点采集，并只把该次采集形成的合格热点来源送入当前显式配置的候选判断模型。运行身份必须是 `validation_live`；可以形成零候选。领域只允许“泛科普—社会生活”，送入候选判断的合格来源最多 6 条。不得同时验证对标日常、对标历史、标签搜索、问题拓展或人工任务，不得运行 `production_daily`，不得选择候选、进入 Stage 1A、研究、经验或日产能。真实调用只允许一次，不确定请求、超时或部分失败不得自动重试。

执行前必须核实本机 TrendRadar、规范化导出、当前配置候选判断模型的显式路由、凭证和受控入口均已配置；任一缺失即记录阻断并停止，不得用临时脚本、其他热点源、其他模型、旧结果、fixture 或手工数据替代。运行结束后必须核对采集运行、热点原始观察、领域过滤、模型运行、候选/零候选和 `validation_live` 隔离审计，然后立即把 `external_call_authorized` 恢复为 `false`、模式恢复为 `VALIDATION_AUTHORIZATION_WAIT`，提交执行基线并停止；设计基线仍保持完整，后续来源必须逐项重新获得真实调用授权。

### 本次热点 validation_live 阻断与修复边界

2026-07-14 以 `source_type=hotspot`、`domain=fan_kepu_social_life`、`mode=validation_live` 启动的批次 `discovery_run_cf43d61dd5024db393c21d2e65e19100` 在 TrendRadar 桥接预检阶段确定性失败。桥接把上一次受控真实运行自己生成的 `vendor/TrendRadar/output/news/2026-07-14.db` 误判为未经批准的上游源码改动，因此在创建新证据目录、启动 TrendRadar 上游命令和调用候选判断模型之前以退出码 2 结束。该批次状态为 `completed_with_failures`；热点原始观察、来源版本、输入组装、模型运行、候选、无候选记录和用户决定均为零，只保存 1 条 `candidate_version_id=NULL` 的空 `validation_live` 快照占位、3 条命令回执和 3 条审计事件。数据库完整性检查为 `ok`，外键违规为零。不得把它认定为一次真实热点业务验收，也不得提升 TrendRadar 的六级状态。

项目桥接的安装完整性判定已修复：继续拒绝 TrendRadar 上游源码和其他陌生文件的改动，仅允许项目批准的 `config/config.yaml`、明确限定在 `output/` 下的 TrendRadar 自身运行产物以及 Python `__pycache__/`。`tests/core/test_local_trendradar_executor.py` 8 项和 `tests/core/test_stage1b_daily_discovery.py` 22 项通过；修复期间没有调用真实来源或模型。用户现已明确批准保留原失败批次 `discovery_run_cf43d61dd5024db393c21d2e65e19100`，并使用直接包含原批次编号的新恢复编号执行一次相同范围的热点验收。恢复必须继续使用 `fan_kepu_social_life`、`source_type=hotspot`、`mode=validation_live`、600 秒批次上限及当前显式候选判断路由；不得自动重试或再次恢复。

恢复批次 `discovery_run_039ffe6217834d3d933f49631461ffc1` 已按新恢复编号执行且没有自动重试。TrendRadar 在 50.749 秒内完成 11 平台采集，得到 255 条真实热点观察；当前社会生活匹配读取 4 条，4 次模型调用均经显式路由且 `retry_status=not_retried`。其中 1 条返回确定性无候选，2 条返回文本无法按 JSON 解析并记为 `model_output_invalid`，1 条把“安宥真住房申购”娱乐人物线索包装为社会公平候选，同时自述“材料严重不足、无法确认事实”。批次因此为 `completed_with_failures`，只形成 1 条 `formal_candidate_pool=false` 的隔离候选、3 条隔离无候选记录和 1 条隔离快照；没有用户决定、Stage 1A、研究、经验或正式日产写入。数据库完整性为 `ok`、外键违规为零。该结果不得认定为 `LIVE_VALIDATED`。

本次不联网修复已把版本化领域包的排除词和热点匹配词放入候选判断冻结输入，并在模型返回后由 Core 再做一次确定性领域检查；社会生活领域新增粉丝、偶像、艺人、爱豆和网红排除词，娱乐内容不得通过改写社会角度建立候选。模型自述“材料严重不足”“事实无法确认”等阻断缺口时改记为确定性零候选，不算技术失败。输出解析只额外兼容一个完整 JSON 代码块，代码块外说明、多个对象或其他自由文本仍失败关闭。`tests/core/test_stage1b_daily_discovery.py` 27 项通过，TrendRadar、来源采集与领域标签相关测试 48 项通过；修复期间没有真实来源或模型调用。

### 本次热点 validation_live 真实验收结果

用户在对话中明确说明“验收”指真实执行后，本轮只授权并执行一次热点来源 `validation_live`。运行命令为 `python -m scripts.core.production.stage1b_daily_discovery --domain fan_kepu_social_life --source-type hotspot --actor codex_user_authorized_validation --idempotency-key stage1-hotspot-validation-live-20260714-user-acceptance-01 --mode validation_live --discovery-date 2026-07-14 --batch-timeout-seconds 600`；没有执行 `production_daily`、Stage 1A 交接、研究、经验、日产能或自动重试。

批次 `discovery_run_cde00cb53c994439b2a37111e5223a3f` 于 `2026-07-14T10:42:21.302942+00:00` 创建，`2026-07-14T10:45:15.919981+00:00` 完成，运行身份为 `validation_live`，生命周期为 `completed`。TrendRadar 采集批次 `trendradar_run_4c39b21e03819130d537` 成功，11 个平台全部成功，取得 255 条真实热点观察；证据目录为 `validation_evidence/stage1/trendradar/20260714T104221.505572Z/`，规范化输出 `outputs/stage1/trendradar/latest.json` 的 SHA-256 为 `a79867f757d250d1f8e407227d14376a8cbb355e12f1d768d7e3e31be1015896`。

本次领域匹配后进入 Stage 1B 候选判断的热点来源版本为 3 条；3 次模型调用均经 `business_analysis` 显式路由、`hermes` Provider、`xiaomi/mimo-v2.5-pro` 模型、`retry_status=not_retried`、`validation_status=passed`。3 条结果全部记录为 `model_returned_no_candidate`，候选数为 0，并生成 1 条 `candidate_version_id=NULL` 的 `validation_live` 快照。正式库 `data/formal/production_activation.sqlite3` 检查结果为 `PRAGMA integrity_check=ok`，外键违规为 0；命令回执和 `stage0_audit_event` 已记录运行开始、来源记录、过滤、输入组装、模型网关、零候选、快照完成和批次完成事件。

用户审核该真实执行结果后明确拒绝其业务验收结论：简单确定性排除仍消耗 LLM，重复热点没有先聚合去重，裸热榜标题缺少受控搜索和最小材料包支撑，且 6 条读取结果进入 3 条模型判断的标准不清晰。因此，本次运行只能证明真实采集、模型路由、审计和数据库完整性链路发生过，不得把热点来源标记为 `LIVE_VALIDATED`，不得把 0 候选解释为设计有效，也不得继续推进剩余来源验收或 `production_daily`。

用户已确认修正口径后，当前结论进入 `SOURCE_TO_TOPIC_SKILL_CONTRACT_REVISION_AUTHORIZED`：真实调用授权仍关闭，模型调用、外部来源、正式库写入和真实运行均未授权；只允许修订 `runtime_skills/source_to_topic` 合同、输入输出、Prompt、fixture 和直接测试。修订完成并经用户复核前，不得讨论新的真实验收授权。

用户确认修订后的 `source_to_topic` Skill 合同后，又授权一次真实热点验收，并要求明确说明哪些节点使用 LLM、使用什么模型，以及是项目运行期 LLM 还是 Codex 写代码 LLM。本轮命令为 `python -m scripts.core.production.stage1b_daily_discovery --domain fan_kepu_social_life --source-type hotspot --actor codex_user_authorized_validation --idempotency-key stage1-hotspot-source-to-topic-live-20260714-user-confirmed-01 --mode validation_live --discovery-date 2026-07-14 --batch-timeout-seconds 600`；命令退出码为 1，批次 `discovery_run_b99a2d88fc534ac69a52046af2733d29` 的执行身份为 `validation_live`，生命周期为 `completed_with_failures`。

该批次真实调用了 TrendRadar 热点采集，采集批次 `trendradar_run_187a1490c909812f5406` 完成，取得 254 条热点观察；社会生活领域读取 3 条热点来源版本，候选数为 0，生成 1 条 `candidate_version_id=NULL` 的隔离快照。正式库 `data/formal/production_activation.sqlite3` 检查结果为 `PRAGMA integrity_check=ok`，外键违规为 0。3 条来源分别处理为：`未来5年消费“路线图”来了` 进入模型后记录 `candidate_material_insufficient`；`24小时制能否成为酒店业服务新方向？我们找消费者和从业者聊了聊` 进入模型后记录 `model_output_invalid`；`邹市明冉莹颖，谁该为之前的投资失败乃至当前的婚姻状态付更大的责？` 被确定性过滤为 `source_already_processed`，没有进入模型。

本次实际发生 2 次业务运行期 LLM 调用，均经项目 `ModelGateway`，不是 Codex 写代码或聊天使用的 LLM。两次调用的节点均为 `stage1b.daily_discovery`，`prompt_version=stage1b.daily_discovery.prompt.v1`，表中记录的 `skill_version=stage1b.source_to_topic.skill.v1` 只是该旧入口写入的字段；实际代码路径是 `scripts/core/production/stage1b_daily_discovery.py` 中 `gateway.complete(daily_discovery_prompt(...))`，没有调用 `runtime_skills/source_to_topic` 或 `scripts.core.experience.run_source_to_topic` 的已确认原子 Skill 合同。两次业务模型均走 `route_id=business_analysis`、`provider_ref=mimo_main`、`provider_name=hermes`、`model_name=xiaomi/mimo-v2.5-pro`、`via_model_gateway=1`、`retry_status=not_retried`；token 记录分别为 1689 和 1981。

因此，本次真实验收结论为 `SOURCE_TO_TOPIC_HOTSPOT_LIVE_VALIDATION_REJECTED_PATH_MISMATCH`：它证明真实采集、部分确定性过滤、模型路由、正式库写入和审计链路发生过，但没有证明用户已确认的新版 `source_to_topic` Skill 被真实入口调用，也不能证明热点源转好选题能力通过。不得把本批次标记为 `LIVE_VALIDATED`、`PRODUCTION_READY` 或 Stage 1 真实完成；不得继续真实运行、外部调用、模型调用、正式库写入、Stage 1A 交接或 `production_daily`。下一唯一动作是修复 Stage 1B 真实入口，使其调用已确认的 `source_to_topic` 原子 Skill 合同；修复后需重新提交并由用户另行授权下一次真实验收。

本轮按用户“修复”指令完成代码级接线，不调用真实来源、不调用真实模型、不写正式库。`scripts/core/production/stage1b_daily_discovery.py` 现在把确定性过滤后的来源组装为 `source_to_topic.input.v1`，通过 `runtime_skills/source_to_topic` 的 `FormalSkillContract`、`business.source_to_topic` 节点、合同 Prompt、输入/输出 schema、语义校验和审查清单处理结果；旧 `stage1b.daily_discovery.prompt.v1` 不再是 Stage 1B 候选判断运行节点。`scripts/core/production/stage0_content_core.py` 的 Stage 1B 模型落库校验同步接受正式 Skill binding，使 `stage1b_model_run` 能记录 `source_to_topic` 的 route、provider、model、binding、usage 和校验状态。`tests/core/test_stage1b_daily_discovery.py` 已更新为验证 `source_to_topic.input.v1`、`business.source_to_topic` 路由、零候选、失败关闭、领域复核、材料不足、冷却和 Stage 1A 隔离。`TECHNICAL_MANUAL.md` 已同步说明该入口使用项目运行期业务模型，不是 Codex 写代码模型。回归命令 `python -m pytest tests/test_workflow_guard.py tests/core/test_stage1b_daily_discovery.py tests/core/test_source_to_topic_skill.py tests/core/test_run_source_to_topic.py -q` 通过 93 项；这仍只达到 `TECH_TESTED_PENDING_LIVE_VALIDATION_AUTHORIZATION`，不能升级为 `LIVE_VALIDATED`。

### 上一次单次验收授权的阻断结果

本次只完成了受控运行前检查，没有发出任何真实热点或模型请求，也没有创建发现运行。检查结果：Mimo 的 `business_analysis` 路由可解析为显式 Hermes 适配器，模型引用已配置且 `fallback: none`；但 `config/settings.yaml` 中 `hotspot_collection.live_enabled` 仍为 `false`，`project_dir`、`executable` 和 `normalized_json_path` 均为空，本机未发现 TrendRadar 命令、项目目录或 Docker 容器。因此命中“任一真实运行配置缺失即停止”的规则，未使用其他热点源、旧数据、fixture 或临时脚本替代。一次性外部调用授权未被消耗为真实请求，现已关闭。

## Stage 1 热点运行时补齐授权

用户在得知此前只完成接口框架、没有安装或接通 TrendRadar 后，明确要求立即把它真正安装并接通。本轮批准从 TrendRadar 官方来源完成本机安装，补充项目内必要且有限的确定性输出转换入口，配置被 Git 忽略的本机运行参数，运行对应测试并提交，然后手动执行一次真实热点来源验收。本轮暂停候选生成，不调用 Mimo 或其他模型。

安装必须位于仓库内 `vendor/TrendRadar`，不得把仓库外目录作为正式运行位置。接入不得修改 TrendRadar 上游业务源码，不得使用非官方分发、其他热点源、浏览器临时抓取、旧热点、fixture 或人工伪造输出。项目转换入口只能读取 TrendRadar 本次真实运行产生的本地 SQLite 结果并输出规范化 JSON，不作业务判断、不调用模型、不补造 URL 或数量。真实验收只允许热点来源，不得同时启动标签搜索、对标来源、候选生成、`production_daily`、Stage 1A、研究或经验系统；超时、部分失败和不确定请求不得自动重试。

完成后必须记录官方来源版本、本机安装位置、真实运行命令、规范化输出、原始数据库、标准输出、标准错误、开始/结束时间和耗时。随后立即关闭外部调用授权并恢复 `baseline_gap_resolution`；任何实际无法完成的环节必须标记 `ENV_NOT_READY` 并停止，不得再次把接口、测试或安装文件存在称为“已接通”。

Stage 1 来源统一只使用六个完成等级：`DESIGNED`、`SCAFFOLDED`、`TECH_TESTED`、`ENV_READY`、`LIVE_VALIDATED`、`PRODUCTION_READY`。等级必须逐级满足；Mock、fixture、fake provider、文件存在或 pytest 通过最高只能达到 `TECH_TESTED`。`ENV_READY` 必须有项目内安装、依赖、正式本机配置和可执行命令。上游采集器真实启动并取得原始数据，只能证明运行环境和原始转换可用，仍只到 `ENV_READY`。`LIVE_VALIDATED` 必须让本次真实来源经过当前版本的标准来源对象转换、领域匹配、领域排除规则、时效/风险/重复过滤和热点转具体问题，并形成可审计的合格候选或确定性零候选原因；需要模型判断的节点还必须经过显式模型路由且无技术失败。`PRODUCTION_READY` 还必须通过进入受控日常发现链路的独立验收。只有 `LIVE_VALIDATED` 或 `PRODUCTION_READY` 的来源才能用于之后另行授权的真实候选发现。

### TrendRadar 本轮运行时真实测试结果（不构成 Stage 1 业务验收）

项目内受控入口已由提交 `a6306535c7cea56babd31541723eaf9288c1ea83` 固定。本轮只执行一次 `vendor/TrendRadar/.venv/Scripts/python.exe -m trendradar`，没有自动重试、模型调用、候选生成或正式库写入。官方来源为 `https://github.com/sansan0/TrendRadar.git`，版本 V6.10.0，提交 `1f178da10e6680e5b652b0dec781e675fe73cf31`，安装位置为 `vendor/TrendRadar`。

运行从 `2026-07-14T06:13:46.033845+00:00` 到 `2026-07-14T06:14:43.939924+00:00`，耗时 `57.871` 秒，退出码 0。11 个配置平台全部成功，失败来源 0；真实原始库为 `vendor/TrendRadar/output/news/2026-07-14.db`，抓取批次 `14-14`。项目转换得到 255 条原始热榜条目；其中的标题是热榜条目名称，URL 是该条目的原始落地链接，不表示视频，也不是选题。255 条标题、URL 和 ID 均非空且 ID 唯一，输出 SHA-256 为 `ec8d524ee72d6be176e28d92622a8a181d9bb421c96df674499f3307a9552069`。

本机审计证据保存在 `validation_evidence/stage1/trendradar/20260714T061346.033845Z/`：`manifest.json` 记录命令、安装身份、开始/结束时间、耗时、原始库、平台状态和输出路径；`stdout.log` 为 3,038 字节；`stderr.log` 为 0 字节。规范化原始来源输出保存在 `outputs/stage1/trendradar/latest.json`。该次运行没有写入正式 Stage 1B 结果。本轮没有执行领域匹配、社会生活排除规则、热点转具体问题、候选判断或确定性零候选审计，因此 TrendRadar 只保持 `ENV_READY`，不是 `LIVE_VALIDATED`，更不是 `PRODUCTION_READY`。

## Stage 1 设计恢复决定

- 本轮真实验收只使用“泛科普—社会生活”。音乐娱乐不运行、不补位；通用能力必须通过版本化领域包支持以后新增领域，第三领域 fixture 只验证核心没有写死，不能获得真实来源等级。
- 领域话题库只用于视频平台标签搜索，与 TrendRadar 热点无关。每天轮换最多 3 个活跃标签，每标签只采集第 1 页，返回多少保存多少；`科普`、`知识`属于可保留的领域标签。
- 平台活动标签必须以版本化登记中的平台、活动身份或页面、有效期和理由为排除证据。活动词面只进入待复核，不能自动删除；现实事件、节日和季节性议题不因时效性自动排除。
- 候选判断依赖显式“当前配置的候选判断模型”位点，不把当前 Mimo 绑定写成业务语义或永久依赖。
- 先前错误验证及其全部派生结果已按用户明确授权通过已提交的受控清理入口物理删除；当前工作树和正式库不保留这些结果的标识、对象或审计副本，也不把它们作为测试样本、冷却依据、产能、经验或验收证据。全库残留扫描为零，数据库完整性和外键检查通过，原始对标来源保持不变。

## 当前执行指针

| 项目 | 当前事实 |
| --- | --- |
| 当前阶段 | Stage 1B / `stage_1_daily_discovery` |
| 当前状态 | `SOURCE_TO_TOPIC_STAGE1B_ENTRYPOINT_FIX_IMPLEMENTED`；Stage 1B 入口已在代码中接入已确认的 `source_to_topic` 原子 Skill，尚未重新真实验收 |
| 当前动作 | 提交本次入口修复并关闭实施授权；真实来源、业务模型调用、正式验收写入、`production_daily`、Stage 1A 交接、研究、经验和自动重试均未授权 |
| 当前分支 | `implementation/v1.3-stage1b-daily-discovery` |
| 当前 HEAD | 每次新会话以 `git rev-parse HEAD` 实时核对；不得以文档内旧哈希替代当前 Git 事实 |
| 基础提交 | `9c5b4a8130deb7b5f1990ca66053817614f5793f` |
| Stage 0 | 已完成并提交：`e88c86a` |
| 参数治理 | 已完成并提交：`d766a51` |
| Stage 1A | 已完成并提交：`6693d3a`；尚未经过真实日常候选上游接入 |
| Stage 1B | 当前标记为 `TECH_TESTED_PENDING_LIVE_VALIDATION_AUTHORIZATION`；没有来源可按修正后设计标记为 `LIVE_VALIDATED`，不得冒充 `PRODUCTION_READY` 或真实日常完成 |
| 仓库治理 | Codex 已成为唯一代码执行入口；Claude 专属入口与双文件镜像已在 `cc134ac1f6fb3f7c20834d90349e2149d991f466` 清理 |
| 真实来源状态 | TrendRadar 原始采集环境保持可用事实，但热点转选题业务验收被拒绝；真实调用授权已关闭 |
| 当前阻断 | 本轮只授权代码修复，不授权真实来源、模型调用、正式库写入、环境改动或生产运行；`external_calls_authorized: false`、`formal_data_writes_authorized: false`、`environment_changes_authorized: false`、`production_authorized: false` |
| 下一唯一动作 | 关闭实施授权并等待用户决定是否授权下一次真实验收 |

### 创建本文件时的 Git 事实

- HEAD：`6693d3acab913a6845ad1c7665ff15cc4da6aefa`。
- 当前分支从已提交的 Stage 1A 创建；`docs/EFFECTIVE_DESIGN_BASELINE.md` 与 `config/model_routes.yaml` 相对 HEAD 未修改。
- Stage 1B WIP 不属于本文件提交：
  - `scripts/core/production/stage0_content_core.py`
  - `scripts/core/production/stage1b_daily_discovery.py`
  - `tests/core/test_stage1b_daily_discovery.py`
- 唯一正式事实源：`data/formal/production_activation.sqlite3`。
- 唯一正式写入边界：`scripts/core/production/stage0_content_core.py` 中的 `Stage0ContentProductionCore`。
- 运行期模型唯一配置：`config/model_routes.yaml`；三个显式位点是 `daily_chat`、`business_analysis`、`writing_generation`，均为 `fallback: none`。

## 全局防跑偏执行规则

1. Codex 每次只能执行“当前执行指针”中写明的唯一动作。当前指针、分支、HEAD 或工作区状态任一不一致时，立即停止并汇报。
2. 新会话恢复必须先阅读：
   1. `docs/EFFECTIVE_DESIGN_BASELINE.md`；
   2. 本文件；
   然后核对分支、HEAD、Git 状态和当前动作。聊天记忆、旧 Goal、ROADMAP、交接包、历史报告和旧 Skill 不是当前规则来源。
3. 未达到本阶段验收条件，不得进入下一阶段。用户已批准的当前阶段内，按当前唯一动作连续完成普通技术步骤、测试、修复、基线更新和阶段提交；只在执行指针列明的用户门禁或本文件定义的停止条件处等待审核。
4. 不得自行扩展范围、顺手重构、顺手清理、顺手实现后续节点，或为了全绿而降低验证标准。
5. 发现问题时，只有以下情况可以在当前阶段处理：阻断当前阶段；会让错误模型、Provider、事实源或旧规则进入；破坏追溯；污染正式数据。其他问题只登记到对应未来阶段，不改变当前动作。
6. 任何“已完成”都必须有受控入口、合格输入、实际处理、实际输出、正式写入、人工动作、异常恢复和测试证据。Mock、fixture、fake provider、文件存在或 pytest 通过不能单独证明真实业务能力。
7. 非 production 身份不得写正式事实；生产写入只能经唯一 Core 和唯一正式库。不得以 JSON、Markdown、内存存储或第二数据库并行承担正式事实。
8. 正式模型调用只能经 `ModelGateway` 和显式模型路由。未绑定、fallback 非 `none`、Provider/模型不一致、请求状态不确定时失败关闭，不得隐性切换或自动重试。
9. 每次阶段提交必须同步更新本文件的当前状态、验收证据、提交哈希、当前阻断和下一唯一动作。
10. 不再为单个阶段创建 Goal、ROADMAP、交接包或独立审计报告；本文件是唯一实施续写索引。

## 统一阶段模板与判定标准

每个 Stage 必须按以下二十项书写并执行：

1. 阶段目标和真实业务价值；2. 对应 V1.3 章节；3. 当前代码和数据事实；4. 前置条件；5. 允许修改模块；6. 禁止修改/本阶段不做；7. 顺序动作；8. 输入；9. 处理；10. 输出及数据去向；11. 人工确认节点；12. 单模块测试；13. 真实模型、真实数据和真实业务验证；14. Mock/fixture/fake provider 证明边界；15. 必须提交的验收证据；16. 阻断和停止条件；17. 异常恢复；18. Git 分支和提交检查点；19. 下一阶段进入条件；20. 当前状态和缺口。

## Stage 0：受控执行底座

1. **阶段目标和真实业务价值**：建立第一条生产链的唯一写入边界、身份隔离、版本/状态/人工确认和模型运行防护，避免测试、旧 CLI 或模型绕过正式事实。
2. **对应 V1.3 章节**：2.3、7、10、12、13、18、19、21、22、23。
3. **当前代码和数据事实**：`Stage0ContentProductionCore` 已提交；生产身份仅能连接 `production_activation.sqlite3`；旧 CLI 在连接正式库前拒绝；Stage 0 没有创建真实生产任务。
4. **前置条件**：有效设计基线可读、正式库路径唯一、`config/model_routes.yaml` 可解析、执行契约可通过。
5. **允许修改模块**：Core 通用状态、版本、审计、Input Assembly、运行记录、身份隔离及直接测试。
6. **禁止修改/本阶段不做**：不做真实研究、采集、计划、写稿、审核、工作台、飞书、音频或数据迁移。
7. **顺序动作**：核对分支/基线→经 Core 建表→创建相邻版本→写 Input Assembly→经 ModelGateway 记录运行→人工决定或显式失败。
8. **输入**：受控命令、明确身份、当前任务/节点、精确上游版本、幂等键、冻结输入和显式模型绑定。
9. **处理**：Core 校验身份、状态、版本、上游、幂等、过期和模型路径；所有写入在同一 Core 事务语义内完成。
10. **输出及数据去向**：任务、不可变版本、决定、Input Assembly、模型运行和审计写入唯一正式库；测试输出仅进入隔离库。
11. **人工确认节点**：正式选题、研究方案、研究结果、内容计划、初稿和最终确认均必须停在用户动作。
12. **单模块测试**：相邻门、上游未确认、退回新版本、历史不可覆盖、过期结果、幂等、身份隔离、直接 Provider 绕过、未绑定和 fallback。
13. **真实模型、真实数据和真实业务验证**：仅在授权下做最小受控真实调用，记录实际 Provider、模型、路由、调用次数、token、费用、延迟；技术成功不推进业务。
14. **Mock/fixture/fake provider 证明边界**：只证明 Core 状态机、异常和隔离；不能证明真实 Provider、正式业务内容或生产能力。
15. **必须提交的验收证据**：受影响 core/validation 测试、`git diff --check`、正式库未被测试身份写入、唯一入口/事实源检查。
16. **阻断和停止条件**：双入口、双事实源、身份冲突、未绑定模型、fallback、过期版本或请求不确定时停止。
17. **异常恢复**：保留失败运行与审计；用户重新提交、退回新版本、取消或修复明确配置后重试。
18. **Git 分支和提交检查点**：已提交 `e88c86a feat: establish Stage 0 controlled production gates`。
19. **下一阶段进入条件**：Stage 0 边界测试通过，正式入口/事实源/模型绑定均唯一且可核实。
20. **当前状态和缺口**：已完成底座；它不等于任何后续业务节点已经实现。

## 参数治理修复：脚本生成证据上限

1. **阶段目标和真实业务价值**：将 `EVIDENCE_ITEMS_MAX=12` 从业务硬编码迁入版本化配置，防止参数静默漂移。
2. **对应 V1.3 章节**：13.1、19.2、19.3、22.1、23.4。
3. **当前代码和数据事实**：`d766a51` 修改脚本生成、适配器、模型信封及五个测试文件；实际值仍为 12。
4. **前置条件**：现有版本化配置解析和受控模型运行记录。
5. **允许修改模块**：该单一参数、校验、运行记录和对应测试。
6. **禁止修改/本阶段不做**：不重构参数系统，不运行正式初稿，不改变模型/Provider/费用配置。
7. **顺序动作**：读取版本化配置→校验值→注入运行→记录实际值和配置版本→失败关闭。
8. **输入**：正式配置、配置版本、参数值和显式运行身份。
9. **处理**：缺失、非法或不可解析时拒绝；不存在隐性默认值。
10. **输出及数据去向**：运行记录保存实际参数和配置版本；不创建业务产物。
11. **人工确认节点**：参数变化需走后续版本化批准，当前提交没有授予运行期自动改参权限。
12. **单模块测试**：缺失配置、非法配置、实际值记录和旧 validation 缺陷回归。
13. **真实模型、真实数据和真实业务验证**：不需要为本修复调用真实模型；真实脚本运行时仍需记录同一参数事实。
14. **Mock/fixture/fake provider 证明边界**：可证明解析和记录，不能证明正式初稿质量或正式生成能力。
15. **必须提交的验收证据**：相关核心/validation 测试和 `git diff --check`。
16. **阻断和停止条件**：配置缺失、非法或版本不明即停止运行。
17. **异常恢复**：修复受控配置后以同一入口重新提交；不得回退到代码默认值。
18. **Git 分支和提交检查点**：已提交 `d766a51 fix: govern script generation evidence limit`。
19. **下一阶段进入条件**：参数治理不阻塞 Stage 1，但不得被当成初稿节点验收。
20. **当前状态和缺口**：已完成狭义修复；正式初稿业务仍属于 Stage 4。

## Stage 1：真实日常发现与候选选择

1. **阶段目标和真实业务价值**：由真实来源自然形成可追溯候选和日报快照，等待用户选择；发现不自动立项、不占用正式日产能。
2. **对应 V1.3 章节**：3.1—3.5、4.1、16.6—16.9、17.1—17.3、19、20.1、21、22、24.2。
3. **当前代码和数据事实**：错误验证及其派生结果已经物理删除；正式库已存在本轮热点 `validation_live` 的运行、来源版本、输入组装、模型记录、零候选、快照、命令回执和审计记录，候选、冷却和用户决定仍为零。领域包、热点独立匹配、标签证据分类、六来源读取、单来源验收模式和项目 Runtime 入口已提交并通过技术测试；当前只根据真实安装、配置、正式库记录和运行证据采用下表等级。

### Stage 1 来源六级状态

| 来源能力 | 当前等级 | 当前证据 | 未达到下一等级的缺口 |
| --- | --- | --- | --- |
| TrendRadar 热点 | `ENV_READY` | TrendRadar 已在本机受控入口完成真实采集，11 平台采集成功并取得 255 条真实热点观察；后续 `validation_live` 批次 `discovery_run_cde00cb53c994439b2a37111e5223a3f` 真实发生，正式库完整性 `ok`、外键违规 0，但用户审核拒绝其业务验收结论 | 需要先补齐事件聚合去重、确定性前置排除、受控搜索最小材料包、6 到 3 的稳定处理规则、跨领域独立处理，以及经用户审核的原子化来源转选题 Skill；修正设计并重新授权真实验收前不得升为 `LIVE_VALIDATED` |
| 对标账号日常内容 | `ENV_READY` | 正式库有 1,525 条真实抖音对标视频，MediaCrawler 原始归档仍在，真实环境可用 | 唯一真实候选链验收暴露了火箭、硬核工程和音乐娱乐等领域过滤错误；修复后未重新真实验收 |
| 历史高信号 | `ENV_READY` | 正式库有 121 条 formal 与 323 条 rough hit，真实来源数据可用 | 唯一真实候选链验收使用了修复前过滤逻辑；当前版本未重新真实验收 |
| 领域标签搜索 | `TECH_TESTED` | 标签轮换、每标签第 1 页、返回多少保存多少、科普/知识保留、活动词待复核、活动证据精确排除及 MediaCrawler 参数路径均有测试 | 正式搜索开关仍关闭，尚无真实搜索运行证据 |
| 有限问题拓展 | `TECH_TESTED` | 已有 Core 正式来源表、受控登记、精确来源版本、统一过滤/模型输入和回归测试 | 尚无泛科普—社会生活真实完成拓展输入及真实候选/零候选运行证据 |
| 保存的用户方向 | `TECH_TESTED` | 已有 Core 正式来源表、受控登记、精确来源版本、统一过滤/模型输入和回归测试 | 尚无本轮用户真实方向输入及真实候选/零候选运行证据 |

以上状态不因代码、Mock、fixture、fake provider、pytest 通过或上游采集器成功返回原始数据而自动升级。只有完整经过修正后业务过滤、受控搜索材料包、候选/零候选审计和用户认可验收的 `LIVE_VALIDATED` 或 `PRODUCTION_READY` 来源，才允许进入后续真实候选发现。当前没有任何来源达到 `LIVE_VALIDATED`，Stage 1B 只处于 `DESIGN_CONFIRMED_PENDING_SKILL_CONTRACT`，仍不得运行 `production_daily`。
4. **前置条件**：Stage 0、明确领域、来源资格、来源精确版本/时间、显式运行模式和显式模型路由；只有 `production_daily` 可形成正式日产候选，音乐娱乐须先有用户提供并受控登记的真实输入。
5. **允许修改模块**：唯一 Core 内的来源记录、候选版本、过滤/冷却、日快照、用户决定、模型判断、Stage 1A 衔接和直接测试。
6. **禁止修改/本阶段不做**：不自动选题、不做深度研究、D0—D7/P0—P7、音频、飞书、完整工作台、第二事实源或人工编造候选。
7. **顺序动作**：显式声明运行模式→TrendRadar 热点采集→热点领域转化→对标日常→对标历史高信号→每天最多 3 个活跃标签的第 1 页搜索→标签来源转化→后续已完成拓展验证与人工提交→统一确定性过滤→只把合格少量对象送 ModelGateway→保存候选/零候选、快照和实际批次状态。每个标签该页返回多少就留存多少，不翻页、不补量、不按互动量截断。
8. **输入**：领域、来源对象及版本、发现时间、时效、已有候选/正式任务、冷却痕迹、风险边界和用户决定。
9. **处理**：来源资格、领域、排除类型、完全重复、已生产、三天冷却、过期、风险和材料准备度先确定性过滤；泛科普—社会生活额外排除纯编程、硬核工程、火箭、材料技术和音乐娱乐对象；模型只作有限候选判断，原始来源标题可保留原语言，但面向用户的候选标题、核心问题、价值和风险必须为自然中文。
10. **输出及数据去向**：来源记录、过滤原因、候选版本、快照、批次模式/实际生命周期和审计写入唯一正式库；`test_isolated` 与 `validation_live` 的候选只可查看和审计，不能进入正式候选池、日产能、Stage 1A、研究或经验系统；未选择不得创建任务。
11. **人工确认节点**：用户选择、暂缓、拒绝、要求换角度或保留常青；选择是创建正式选题的唯一动作。
12. **单模块测试**：无真实来源零候选、重复、三天冷却、过期、社会生活领域排除、面向用户字段中文、无 score/rank/weight、热点先于标签、热点/标签正式追溯、每天最多 3 标签、标签不足不补位、每标签第 1 页完整留存、超时/中断/部分失败无自动重试、模型仅处理过滤后对象、候选不占产能、运行模式不可升级、选择精确关联、日产能、幂等与测试身份隔离。
13. **真实模型、真实数据和真实业务验证**：另行授权后只以泛科普—社会生活逐来源读取真实输入，经显式候选判断模型位点和 ModelGateway 形成有限候选或确定性零候选；音乐娱乐不运行、不补位。新增领域能力只通过领域包 fixture 和回归证明可扩展，不能据此升级真实来源等级。
14. **Mock/fixture/fake provider 证明边界**：本轮只证明 Runtime 顺序、页数/数量边界、原始观察留存、来源追溯、过滤、状态、幂等、异常、隔离和新增领域不需要修改通用核心；没有真实来源和当前配置模型调用，不能证明任何来源真实可用或能形成合格候选。
15. **必须提交的验收证据**：运行模式、真实发现运行 ID、来源/过滤/候选/快照/审计记录、模型运行记录、已授权领域清单和未运行领域边界、测试结果、`git diff --check`。已删除的错误结果不得出现在新验收证据、测试输入、冷却或日产能读取中。
16. **阻断和停止条件**：获授权领域没有任何真实来源时停止并报告最小阻断；音乐娱乐无来源时不得以 fixture、fake provider 或人工来源补位；模型非法输出、来源过期/重复/风险或批次超时、中断、请求状态不确定时不得形成或升级为完整生产结果。
17. **异常恢复**：保留关闭原因、失败运行和来源级审计；明确未发出的暂态失败才可按相同冻结输入、路由、Provider、模型、配置和 Prompt 受控重试。请求状态不确定、可能已消耗 token 或已离开明确失败状态时禁止自动重试，须由用户重新提交、取消或创建新运行。
18. **Git 分支和提交检查点**：当前分支 `implementation/v1.3-stage1b-daily-discovery`；前序 `fix: separate live validation from daily production discovery` 只记录运行模式隔离和技术实现，不能证明 TrendRadar 或标签搜索已真实可用。本轮先提交项目内 TrendRadar 安装约定、确定性转换入口、技术测试和状态纠正；真实来源运行证据产生后再关闭授权提交。
19. **下一阶段进入条件**：新建完整 `production_daily` 运行中，用户选择一个精确候选版本，来源链、领域、日产能、运行模式和版本均通过。
20. **当前状态和缺口**：Stage 1 不是整体完成。错误验证结果已物理删除且无正式库残留。TrendRadar、对标日常和历史高信号仅 `ENV_READY`；标签搜索、问题拓展和保存的用户方向仅 `TECH_TESTED`。当前没有任何来源达到 `LIVE_VALIDATED`，不得运行 `production_daily`。

### Stage 1B 真实运行恢复边界与验收条件

- 重试只可针对同一冻结输入、同一显式模型路由、同一 Provider、同一模型、同一配置版本和同一 Prompt；每次尝试必须可见、有界并写入审计。
- 请求状态不确定、可能已经消耗 Token 或已离开明确失败状态时，不得自动重试；不得切换模型、Provider、Prompt、配置版本或旧执行路径。
- 模型调用失败后不得复用旧 `model_result`；该来源停止候选判断并留下失败记录。
- 模型确认无候选与技术失败必须分别记录，均不得伪造候选或吞没原因。
- 任一来源的部分失败必须逐来源记录；整批运行不得伪装为完全成功，日报必须呈现成功、零候选和失败的实际边界。
- `test_isolated`、`validation_live`、`production_daily` 是不可自动升级的运行身份；前两者及其候选不得写入正式候选池或被 Stage 1A、研究、经验与日产能读取。
- 批次超时和中断必须写入实际生命周期；项目 Core Runtime 的一次性批处理入口直接结束并返回结构化状态，不由 Codex 长时间轮询业务进程。

### 跨阶段质量防跑偏保留项

- 全部人工确认节点保持有效；Stage 4 的初稿、优化、定向去 AI、审核和最终确认保持独立、不可跳过的节点。
- Stage 3 的经验考虑/采用/拒绝及理由，和 Stage 5 的经验选用、质量对照与真实验证保持独立审计；单次结果不得自动升级为经验。
- 真实能力证据不能由 Mock、fixture、fake provider、文件存在或 pytest 通过替代；每个 Skill 均须在所属阶段完成独立的真实边界验证。
- 第三领域 fixture 只用于验证通用系统未把逻辑写死于两个正式领域，不构成第三个正式运营领域或真实来源。
- Stage 6 音频、Stage 7 P0—P7/P7 复盘、Stage 8 工作台与操作手册仍为独立后续阶段，不得由 Stage 1 提前进入。

## Stage 2：正式选题、研究方案与深度研究

1. **阶段目标和真实业务价值**：把用户选择转为不可变正式选题，形成可审研究方案，并在方案通过后进行主张—证据深度研究。
2. **对应 V1.3 章节**：4、5.1—5.2、7、10、18—20、22、24.2。
3. **当前代码和数据事实**：`6693d3a` 已提交 `Stage1AResearchPlanService`：正式选题提交/确认、研究方案生成/查看/通过/退回/取消；研究方案停在 `awaiting_human_review`。深度研究未实现。
4. **前置条件**：Stage 1 用户选择、精确候选版本、来源链、领域日产能、生产身份、有效模型绑定。
5. **允许修改模块**：正式选题与研究方案衔接，深度研究需要的受控材料、主张、证据和结果对象及直接测试。
6. **禁止修改/本阶段不做**：不以未审核方案进入深度研究；正式研究不读视频平台内容；不进入内容计划/初稿；不自动通过或换 Provider。
7. **顺序动作**：选择候选→创建正式选题版本→用户确认→冻结方案输入→ModelGateway 生成→校验→方案待审→用户通过/退回/取消→通过后深度研究。
8. **输入**：候选精确版本、用户要求、来源关系、领域规则、允许材料、研究预算和停止条件。
9. **处理**：Core 校验上游/容量/版本；模型只把冻结输入组织为研究方案；深度研究以主张、证据、关系和材料台账处理。
10. **输出及数据去向**：正式选题、研究方案、后续研究结果、材料/主张/证据、Input Assembly、模型运行和审计进入正式库。
11. **人工确认节点**：确认正式选题；审核研究方案；审核研究结果；通过前不得下推。
12. **单模块测试**：未确认阻断、精确上游、版本不可覆盖、非法输出拒绝、自动批准禁止、退回新版本、过期审核、未通过方案阻断研究。
13. **真实模型、真实数据和真实业务验证**：仅在用户选中真实候选后调用真实模型；记录 Provider、模型、路由、输入、版本、token/费用/延迟和输出校验。
14. **Mock/fixture/fake provider 证明边界**：只能证明状态机和错误路径，不能证明材料充分、研究方案质量、证据质量或深度研究能力。
15. **必须提交的验收证据**：候选到正式选题的精确关系、真实研究方案待审记录、人工决定、后续深度研究与研究结果审核证据、回归和 diff 检查。
16. **阻断和停止条件**：未确认、版本失效、材料/阻断证据不足、模型请求不确定、预算耗尽或风险冲突时停止。
17. **异常恢复**：补材料、缩小问题、退回方案创建新版本、取消或修复明确配置后重跑；不覆盖历史。
18. **Git 分支和提交检查点**：方案前半切片已在 `6693d3a`；深度研究必须单独提交。
19. **下一阶段进入条件**：研究结果经用户确认，阻断主张满足领域证据规则，路线和材料台账可追溯。
20. **当前状态和缺口**：部分完成且已提交；只到研究方案待审核，尚不能进入深度研究。

## Stage 3：内容计划与经验选择

1. **阶段目标和真实业务价值**：将确认研究转为可执行内容计划，并让经验与偏好以“考虑/采用/拒绝及理由”受控进入生产。
2. **对应 V1.3 章节**：5.2—5.3、7、9、10、17.6、18—20、24.3。
3. **当前代码和数据事实**：旧 `run_content_plan.py` 存在但被 Stage 0 隔离，不可写正式状态；没有 V1.3 正式内容计划或经验选择入口。
4. **前置条件**：Stage 2 已确认研究结果、路线、主张—证据映射、领域规则和经 Core 预筛的经验/偏好。
5. **允许修改模块**：内容计划 Core 状态/版本、经验检索与决策记录、Input Assembly、模型运行、人工审核和测试。
6. **禁止修改/本阶段不做**：不读全库经验，不让模型自行选经验，不以未确认研究生成计划，不直接写初稿或迁入旧 CLI 产物。
7. **顺序动作**：冻结研究结果→确定性检索候选经验/偏好→记录考虑/采用/拒绝→组装计划输入→生成/校验→写计划版本→用户通过或退回。
8. **输入**：确认研究结果、路线、用户当次要求、领域规则、可核对主张/证据、预筛经验和偏好。
9. **处理**：Core 决定可用候选；模型只组织计划；确定性校验检查主张—证据、经验范围、输入完整性和版本一致性。
10. **输出及数据去向**：不可变内容计划、经验决策、风险/不确定性、Input Assembly、模型运行、审计和引用关系写正式库。
11. **人工确认节点**：用户通过、退回、换路线、补充边界或要求重新研究；未通过不得创建初稿。
12. **单模块测试**：应引/不应引、冲突优先级、研究版本失效、经验暂停、计划退回、新版本和幂等。
13. **真实模型、真实数据和真实业务验证**：使用确认研究和真实可用经验做一次计划生成与人工审核，核查采用/拒绝理由和主张—证据映射。
14. **Mock/fixture/fake provider 证明边界**：只能证明选取规则、结构和失败路径；不能证明经验有效、计划可写或用户偏好已被正确满足。
15. **必须提交的验收证据**：计划正式入口、实际输入组装、真实运行记录、正式计划版本、人工决定、经验决策审计、模块/回归测试和 diff 检查。
16. **阻断和停止条件**：研究未确认、关键证据缺失、路线不成立、经验冲突未解、输入版本失效时停止在计划节点。
17. **异常恢复**：补研究、换路线、更新受控经验候选、退回生成新版本或取消；不得静默修改现有计划。
18. **Git 分支和提交检查点**：创建专用 Stage 3 分支；计划、经验决策和测试以单独提交收口。
19. **下一阶段进入条件**：用户已通过精确内容计划版本，主张/证据和经验决策均冻结可追溯。
20. **当前状态和缺口**：未开始；旧计划脚本不算 Stage 3 能力。

## Stage 4：初稿、优化、去 AI、审核与最终确认

1. **阶段目标和真实业务价值**：以相邻不可变版本把批准计划转为初稿、优化、定向去 AI 修改、审核和用户最终确认。
2. **对应 V1.3 章节**：5.1、5.4—5.6、7、10、18—20、24.3。
3. **当前代码和数据事实**：旧生成/审核 CLI 与 runtime skill 存在，但 Stage 0 已禁止其直接写正式库；没有 V1.3 正式稿件链。
4. **前置条件**：Stage 3 已批准计划、冻结主张/证据映射、用户要求、审核规则和可用模型路由。
5. **允许修改模块**：初稿、优化、去 AI、审核、最终确认的 Core 节点、版本/Diff、确定性预检、模型记录和测试。
6. **禁止修改/本阶段不做**：不绕过计划/研究，不自动最终确认，不让审核 Skill 改稿，不把旧稿或固定返回写入正式链。
7. **顺序动作**：初稿→用户确认→整体优化→去 AI 定向修改→审核→用户最终确认；每次退回都创建新版本。
8. **输入**：批准计划、允许主张/证据、上游正式稿、用户修改意图、审核目标、冻结输入和领域边界。
9. **处理**：模型只处理当前相邻节点；确定性预检先检查引用/禁项/版本；审核输出原子问题而不改变稿件状态。
10. **输出及数据去向**：稿件版本、Diff、修改理由、审核问题、校验结果、Input Assembly、模型运行和人工决定写正式库。
11. **人工确认节点**：初稿确认、可继续/退回/停止的人工改稿节点、审核后的最终用户确认。
12. **单模块测试**：相邻门、计划引用、过期输出、退回新版本、审核问题定位、最终确认权限和幂等。
13. **真实模型、真实数据和真实业务验证**：以已确认真实计划生成真实初稿并经过人工审核；验证事实映射、口播感、定向去 AI 和审核真实问题。
14. **Mock/fixture/fake provider 证明边界**：只能验证状态/Schema/预检；不能证明文案质量、真实表达、去 AI 有效或审核质量。
15. **必须提交的验收证据**：正式入口、真实上游输入、每个稿件版本/Diff、模型/校验记录、人工决定、异常恢复测试、核心/验证回归和 diff 检查。
16. **阻断和停止条件**：输入失效、结构不合格、关键事实映射缺失、审核要求重研/重计划或用户未确认时停止。
17. **异常恢复**：保留失败和审核问题；退回对应上游创建新版本，不覆盖已确认历史。
18. **Git 分支和提交检查点**：按初稿链完整可审计切片单独提交；不与音频、发布或经验阶段混合。
19. **下一阶段进入条件**：用户最终确认精确文案版本，审核与版本链无未处理阻断。
20. **当前状态和缺口**：未开始；旧 CLI 和 pytest 不能证明正式稿件能力。

## Stage 5：经验机制和文案质量真实验证

1. **阶段目标和真实业务价值**：把实际引用、反馈、反例、质量观察和经验提案分开记录，避免一次成功或一次数据点被自动升级为长期经验。
2. **对应 V1.3 章节**：5.4—5.5、9、12.1—12.4、17.4—17.7、20.2、24.4。
3. **当前代码和数据事实**：存在旧经验、偏好和 tactic 模块，但没有与 V1.3 正式稿件链相连的真实验证闭环。
4. **前置条件**：Stage 4 用户最终确认版本、实际经验引用、可追溯反馈/反例或批准实验边界。
5. **允许修改模块**：经验候选、引用反馈、质量观察、提案、局部暂停、用户审核和测试。
6. **禁止修改/本阶段不做**：不让模型直接改正式经验，不跨领域推广，不以互动数据证明因果，不用单一现象形成经验。
7. **顺序动作**：记录实际引用→收集反馈和反例→形成候选/无动作→必要时提案或暂停→用户通过/拒绝/恢复。
8. **输入**：确认稿件版本、计划中的经验决定、领域/条件、反馈、反例、实际观察和实验边界。
9. **处理**：确定性聚合和作用域检查；模型只提取原子候选或说明无提案；Core 判断状态变化和权限。
10. **输出及数据去向**：经验候选、引用反馈、质量验证、提案、暂停/恢复和审计进入经验与验证数据域。
11. **人工确认节点**：用户审核经验提案、跨条件扩展、暂停、恢复或拒绝；未审核不得改变正式经验语义。
12. **单模块测试**：单一现象不升级、反例保留、跨领域拒绝、暂停/恢复、候选为空、重复反馈和幂等。
13. **真实模型、真实数据和真实业务验证**：基于实际确认稿及真实反馈做受控验证，检查引用是否真实、质量判断是否可回溯。
14. **Mock/fixture/fake provider 证明边界**：只能证明候选/提案状态机；不能证明经验长期有效、文案质量提升或用户真实偏好。
15. **必须提交的验收证据**：实际引用记录、真实反馈/反例、候选或无动作、用户审核、状态变化审计、回归和 diff 检查。
16. **阻断和停止条件**：缺少实际引用、证据不足、反馈冲突、范围不明或用户未审核时停止在候选/待审状态。
17. **异常恢复**：局部暂停新引用，补充反馈/反例或由用户恢复；历史经验不物理删除。
18. **Git 分支和提交检查点**：经验闭环单独分支/提交；不得与生产稿生成或发布采集混合。
19. **下一阶段进入条件**：本阶段不直接放行生产；经审核的经验状态可成为后续计划的受控候选。
20. **当前状态和缺口**：未开始；旧经验模块不构成真实验证机制。

## Stage 6：音频交付

1. **阶段目标和真实业务价值**：对用户最终确认的文案生成、审核并交付版本化音频，确保音频配置和交付事实可追溯。
2. **对应 V1.3 章节**：6、7、10、18、19、24.3。
3. **当前代码和数据事实**：没有接入正式生产链的音频生成、审核或交付能力。
4. **前置条件**：Stage 4 用户最终确认文案、保存的声音配置版本、明确的受控音频适配器和交付边界。
5. **允许修改模块**：声音配置对象、音频生成入口、音频版本、审核/交付记录、异常恢复和测试。
6. **禁止修改/本阶段不做**：不修改文案、不替代最终确认、不启动发布反馈、不使用未受控音频 Provider。
7. **顺序动作**：冻结最终文案/声音配置→受控生成→写音频版本→音频审核→用户确认交付。
8. **输入**：最终确认文案版本、标题、声音配置版本、用户交付要求和授权身份。
9. **处理**：Core 校验文案与配置版本；适配器生成；确定性检查文件完整性和关联；审核只判断音频交付本身。
10. **输出及数据去向**：音频资产引用、实际声音配置、审核、交付时间和审计写正式库；资产存储只由受控对象引用。
11. **人工确认节点**：用户审核音频；发现文案/事实/立场问题时按性质退回上游。
12. **单模块测试**：未确认文案阻断、配置版本冻结、生成失败、资产关联、审核权限、重复请求和取消。
13. **真实模型、真实数据和真实业务验证**：使用真实确认文案和真实音频适配器生成一次可审核音频，记录配置、成本、时延和输出完整性。
14. **Mock/fixture/fake provider 证明边界**：只能证明版本/状态/适配器失败路径；不能证明声音质量、可用性或实际交付。
15. **必须提交的验收证据**：正式入口、真实音频版本、配置/运行记录、审核动作、资产完整性检查、恢复演练、回归和 diff 检查。
16. **阻断和停止条件**：文案未最终确认、配置缺失、Provider 未绑定、请求不确定、文件不完整或审核未通过时停止。
17. **异常恢复**：同配置且明确未发出时受控重试；否则保留失败并由用户重新提交、换配置或退回文案。
18. **Git 分支和提交检查点**：音频能力单独提交；不得同提交加入发布/P0—P7。
19. **下一阶段进入条件**：用户已确认精确音频版本和交付记录，文案/配置/审核关系完整。
20. **当前状态和缺口**：未开始；没有正式音频入口或真实交付证据。

## Stage 7：实际发布关联、P0—P7 和 P7 复盘

1. **阶段目标和真实业务价值**：把实际发布与确认稿/音频关联，在同口径 P0—P7 观察中形成可审复盘和反馈候选。
2. **对应 V1.3 章节**：8、9.3—9.4、16.3、16.6、21.3、24.1、24.4。
3. **当前代码和数据事实**：有旧对标 D 观察能力；自营发布关联、P0—P7 和 P7 复盘没有 V1.3 正式实现。
4. **前置条件**：Stage 6 已交付、用户提供实际发布关联、账号/平台授权、P 基线及可用受控适配器。
5. **允许修改模块**：发布事实、P 观察作业、快照、缺失记录、P7 复盘和反馈候选。
6. **禁止修改/本阶段不做**：不把对标 D 与自营 P 混算，不把不同观察点/账号横向排名，不用互动直接证明传播或经验因果。
7. **顺序动作**：登记发布→关联正式版本→创建 P0—P7 作业→采集快照/缺失→P7 复盘→形成待审反馈。
8. **输入**：实际发布标识、确认文案/音频版本、账号、观察点、适配器能力、P 基线和时间策略。
9. **处理**：作业/租约/幂等控制；同观察点可比性检查；复盘分离事实、解释、反例和不可复制因素。
10. **输出及数据去向**：发布关联、P 快照、缺失说明、P7 复盘、反馈候选和审计进入反馈/验证数据域。
11. **人工确认节点**：用户确认发布关联和复盘处理；经验语义变化仍回到 Stage 5 审核。
12. **单模块测试**：发布版本不匹配、重复回调、漏采、不同观察点混用、不可比性、作业恢复和 P7 复盘边界。
13. **真实模型、真实数据和真实业务验证**：使用用户真实发布的对象完成至少一条受控 P 观察链和一次 P7 复盘；记录实际缺失而不补造。
14. **Mock/fixture/fake provider 证明边界**：只能证明调度、口径和恢复；不能证明真实发布、表现、传播效果或经验因果。
15. **必须提交的验收证据**：发布关联、P0—P7 快照/缺失、P7 复盘、作业与审计、真实适配器记录、回归和 diff 检查。
16. **阻断和停止条件**：没有真实发布、授权/适配器不可用、观察点缺失或可比性不成立时停止并保留缺失。
17. **异常恢复**：租约恢复保留原尝试；漏采不顺延或伪造，用户可重新登记发布或等待下一可用观察。
18. **Git 分支和提交检查点**：发布反馈链单独提交；不得与内容生成、音频或界面交付混合。
19. **下一阶段进入条件**：可由 Stage 8 展示的发布、观察、复盘和异常对象均经 Core 可查询且权限受控。
20. **当前状态和缺口**：未开始；不存在真实自营 P 链或 P7 复盘。

## Stage 8：飞书、工作台和操作手册

1. **阶段目标和真实业务价值**：让用户在同一事实源上接收日报、审核和异常恢复，而非建立第二状态链。
2. **对应 V1.3 章节**：2.3、11、21.1—21.2、23.1—23.3。
3. **当前代码和数据事实**：`TECHNICAL_MANUAL.md` 已说明 Stage 0/1A 受控入口；没有完整工作台或飞书正式接入。
4. **前置条件**：Stage 1—7 的受控查询/动作、权限边界、签名/allowlist、明确异常路径和可展示的正式对象。
5. **允许修改模块**：薄 UI/飞书适配、白名单动作、查询视图、操作说明、签名/权限/回调幂等测试。
6. **禁止修改/本阶段不做**：不让界面/飞书直接写数据库，不以自然语言自由解释高风险动作，不让远程文档替代正式版本链。
7. **顺序动作**：定义 Core 查询/动作→实现固定按钮/编号选择→验证签名/身份→展示状态/阻断→同步操作说明。
8. **输入**：用户身份、已签名事件、固定业务动作、精确对象/版本、当前状态和必要理由。
9. **处理**：入口验证签名、allowlist、事件幂等；只把白名单动作转发 Core；界面仅读取并展示正式事实。
10. **输出及数据去向**：用户界面/卡片/消息和操作回执；正式状态仍只写 Core/正式库，界面不保存业务事实。
11. **人工确认节点**：所有选择、通过、退回、取消、补材料和最终确认仍是用户动作，界面不得替代确认。
12. **单模块测试**：签名失败、未授权身份、重复事件、版本不匹配、白名单外动作、按钮幂等和异常提示。
13. **真实模型、真实数据和真实业务验证**：以真实正式对象做只读展示和一个受控用户动作；不需要开放式模型解析来证明入口。
14. **Mock/fixture/fake provider 证明边界**：只能证明渲染/回调/权限分支；不能证明正式写入、真实用户审核或飞书生产可用。
15. **必须提交的验收证据**：Core 调用追溯、身份/签名日志、对象版本回显、用户动作审计、操作说明、端到端测试和 diff 检查。
16. **阻断和停止条件**：签名、身份、对象版本、Core 状态或权限不一致时停止；不得降级为直接数据库写入。
17. **异常恢复**：显示节点、冻结版本、已完成/未完成前置和可选恢复动作；用户从确定性动作重新提交。
18. **Git 分支和提交检查点**：飞书/工作台薄入口与操作手册同一阶段提交；不得携带业务状态机重构。
19. **下一阶段进入条件**：所有生产启动前需要的用户操作、异常和状态查询均经同一 Core 与事实源验证。
20. **当前状态和缺口**：未开始；没有正式 UI、飞书签名入口或端到端操作证据。

## Stage 9：正式生产激活

1. **阶段目标和真实业务价值**：在完整链路、真实数据边界、人工审核、监控、备份和恢复证据齐备后，受控允许正式日常生产。
2. **对应 V1.3 章节**：全基线，重点为 12、13、14、21—24。
3. **当前代码和数据事实**：正式库与 Core 边界存在；完整内容生产、音频、发布反馈、工作台和生产验收均未完成。
4. **前置条件**：Stage 0—8 全部验收、真实领域来源可用、人工停点收敛、模型/Provider 明确、监控/备份/恢复完成、用户明确批准。
5. **允许修改模块**：生产激活检查、受控开关、监控/备份/恢复验收和必要的安全修复。
6. **禁止修改/本阶段不做**：不以局部 Mock、静态文档、单次模型返回、旧链或历史数据替代全链路验收；不在激活时顺手开发新业务。
7. **顺序动作**：逐项核验入口/事实源/身份/模型/版本/人工审核/监控/备份→受控演练→用户批准→启用日常作业。
8. **输入**：各阶段验收包、真实运行记录、生产配置版本、备份/恢复结果、用户批准和当前风险清单。
9. **处理**：Core/运维检查逐项失败关闭；确认没有隐性 Provider、旧规则、第二事实源或测试污染。
10. **输出及数据去向**：生产激活决定、检查结果、监控/备份状态、用户批准和审计写正式库/受控运行记录。
11. **人工确认节点**：用户明确批准激活、暂停和恢复；系统不得自动从测试或局部通过升级生产。
12. **单模块测试**：激活前置缺失、身份隔离、路由缺失、备份失败、恢复失败、重复激活动作和暂停。
13. **真实模型、真实数据和真实业务验证**：受控真实全链路演练，覆盖来源→候选→选题→研究→计划→稿件→审核→交付及必要反馈边界。
14. **Mock/fixture/fake provider 证明边界**：只能证明局部机制；绝不能作为正式生产激活证据。
15. **必须提交的验收证据**：各阶段提交/测试、真实演练、模型/费用/时延、正式对象链、人工批准、监控、备份、恢复、隔离和 diff 检查。
16. **阻断和停止条件**：任一阶段未验收、来源不可用、请求不确定、追溯缺口、监控/备份失败或用户未批准即停止激活。
17. **异常恢复**：暂停新任务、保留历史和审计、修复明确阻断后重新演练；不得删除/重置真实数据以伪造通过。
18. **Git 分支和提交检查点**：生产激活只在完整验收后单独提交；不得与功能开发混合。
19. **下一阶段进入条件**：无；生产激活后仍按本文件的当前指针和阶段化修复规则运行。
20. **当前状态和缺口**：未开始，当前绝不满足正式生产激活条件。

## 每次续写前核对清单

1. 核对当前分支、HEAD、工作区和本文件执行指针；不一致即停止。
2. 阅读有效设计基线和本文件；不读取旧 Goal/ROADMAP/交接材料作为现行规则。
3. 只执行当前阶段、当前动作的允许范围；把其他问题留在未来阶段。
4. 真实模型、真实网络或正式库写入前，复核身份、Core、事实源、精确版本、幂等键、Input Assembly、模型绑定和恢复路径。
5. 完成后运行相称测试和 `git diff --check`，更新本文件指定字段，停止等待用户审核。
