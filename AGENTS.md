# 创作助手 · 工程章程(AGENTS.md)

## 当前唯一设计基线

`docs/EFFECTIVE_DESIGN_BASELINE.md` 是当前唯一的业务设计与实施裁决入口。

它是自包含基线：产品、实施、测试、界面和操作说明不得要求执行者再从旧 Goal、旧迁移计划、历史报告、交接状态、聊天记忆或其他设计材料拼接规则。任何文件与该基线冲突时，以该基线为准；未被它冻结的实现细节必须明确标为候选或待校准，不得由旧文件或现有代码自动升级为正式设计。

本仓库是一个 **Codex 工程**；Codex 是当前唯一代码执行入口，不是运行期业务 Provider。运行期模型路由只由 `config/model_routes.yaml` 的显式位点决定，禁止 fallback、自动降级或隐式 Provider 切换。

## 工程边界

- 确定性采集、判定、存储、检索、追踪与校验必须由代码实现；LLM 只能出现在基线允许的原子 Skill 节点中。
- 真实数据库写入、真实网络请求、安装软件或调用付费模型，必须经已提交的受控入口并通过 `scripts/core/execution_contract.py` 的契约校验；不得用临时命令或片段代码绕过。
- `data/formal/*.sqlite3` 是当前本地业务数据事实源；外部视图不是事实源。
- 领域差异通过配置和数据表达；不得复制领域专用流水线代码。
- 内容硬约束由生成后的确定性校验器处理；不得把禁令堆入生成 Prompt。

## 文档与状态治理

- `docs/EFFECTIVE_DESIGN_BASELINE.md` 是唯一设计基线；`docs/` 中其余文件只可作为操作说明，不能改变设计。
- `execution/STAGE_REGISTER.yaml` 只登记业务 Stage 顺序、类型、状态、依赖、适用基线章节和验收摘要；它不是第二份设计基线。
- 只有 `type: business` 的 Stage 才能进入系统建设进度；`type: maintenance` 只能作为维护任务记录，不能替换业务 Stage、更新业务完成率或触发下一个业务 Stage。
- `execution/current_stage.yaml` 不再作为业务 Stage 唯一来源长期维护；`scripts/execution/stage_control.py prepare` 必须从 `STAGE_REGISTER.yaml` 自动选择唯一的下一个 business Stage，并把临时合同写入 `.stage_runtime/current_stage.yaml`。
- 运行配置、Skill 合同、测试 fixture 与闸门输入仅在代码实际读取时保留；生成报告、一次性验证输出、旧 Goal、迁移材料、历史交接与旧 Skill 说明不构成设计依据。
- 移动、删除或归档文档前，必须搜索 Python、PowerShell、Shell、测试、CI、配置读取和模型指令的实际引用；发现引用时先解除引用并验证。
- 任何模型指令、README、代理配置或启动脚本不得引导读取旧设计或缺失的治理文件。

## 会话启动与自驱执行协议

- 每个新 Stage 会话必须读取本章程和 `execution/STAGE_REGISTER.yaml`，然后运行 `python scripts/execution/stage_control.py prepare`；不得全文读取完整业务基线、大型源码、大型 JSON、日志或归档材料。
- `prepare` 必须只选择 `type: business`、`status` 不是 `passed`、所有 `depends_on` 均为 `passed`、且排序最靠前的唯一 Stage；出现多个候选、没有候选、依赖状态冲突或来源不明确时必须停止。
- `prepare` 输出的 `.stage_runtime/current_stage.yaml` 是本次会话临时合同，不是长期事实源。后续只按该合同列出的基线章节、代码符号、JSON 节点和限定搜索结果读取上下文。
- 上述事实一致时，直接执行自动选出的当前唯一业务 Stage；不得将历史文档、旧 Goal、交接、聊天记忆或旧代码作为业务设计依据。
- 在用户已批准的当前阶段内，Codex 连续完成：核查→修改→测试→修复本阶段内问题→更新执行基线→创建阶段提交，并只在真正需要用户决定的位置停止。
- 普通技术步骤、同一阶段内的失败修复、测试和提交不重复询问下一步。只有需要业务选择、存在两个以上无法裁决的方案、与有效设计基线冲突、涉及正式数据删除/迁移/不可逆操作、模型/Provider/凭证/费用不明确，或当前阶段验收完成而需批准进入下一阶段时，才必须停止。
- 不生成、不读取也不依赖 `CLAUDE.md`；`.claude/` 不是当前执行依据。`AGENTS.md` 是唯一仓库执行章程的手工维护入口。

## 强制工作流门禁

- 所有 Stage 任务在修改文件前必须运行 `python scripts/execution/stage_control.py prepare`；非零退出必须立即停止，不得继续实施。
- 工作中只能用 `stage_control.py read-code`、`read-json` 和 `search` 做受控读取；大型 Python 文件按符号读取，大型 JSON 按摘要或 JSON Pointer 读取，搜索必须限定目录、条数和单行长度。
- 完成业务 Stage 前必须运行 `python scripts/execution/stage_control.py accept`。只有 `accept` 返回 `PASS` 才能声明当前业务 Stage 完成；局部测试、静态文件、Mock、fixture-only 或内部函数直调不能替代真实工作流验收。
- 真实数据库写入、真实网络/来源、安装、环境改动、付费服务和模型调用必须由当前临时 Stage 合同和用户授权同时允许；未授权必须停止，不得先调用后补授权。
- `mode: baseline_gap_resolution` 只能根据 `docs/EFFECTIVE_DESIGN_BASELINE.md` 识别具体缺口、说明缺口影响、输出 `BASELINE_GAP` 并等待用户提供或确认新口径；不得搜索旧材料寻找答案，不得自行提出旧规则恢复方案，不得修改业务代码。`docs/IMPLEMENTATION_EXECUTION_BASELINE.md` 只用于执行状态，不得用于补造业务设计。
- 任何旧文档或旧材料，包括 V0.x 文档、`CURRENT_DESIGN_BASELINE.md`、`DESIGN_AUDIT.md`、旧 Goal、ROADMAP、BUILD_PLAN、Git 历史中的旧设计和旧代码注释中的业务规则，均不得作为当前设计恢复来源，也不得据此修改基线或代码。
- 每项验收必须能由临时 Stage 合同中的正式入口或证据命令提供；缺少可执行证据时不得声明完成。

## 调试与完成验证

- 出现测试失败、安装失败、运行异常、接入失败、路径异常、Provider 或数据库不明确、结果与有效基线不符时，修改前必须使用 `$project-systematic-debugging`。
- 对代码、配置、依赖、安装或业务链路进行变更后，在宣布完成、修复或通过前必须使用 `$project-verification-before-completion`。
- 当前 Stage 只以既有工作流中的唯一真实验收入口作为最终裁决；单元测试、Mock、静态检查和 Agent 自述不能替代它。
- 真实验收后再次修改受控代码、配置或依赖，原验收证据立即失效。
- 禁止为执行这两项规则创建新的 ROADMAP、BUILD_PLAN、GOAL 或平行 Stage 体系。

## 交付纪律

- 每个改动以 `docs/EFFECTIVE_DESIGN_BASELINE.md` 为验收依据，并运行与改动相称的测试或闸门。
- 完成状态必须附对应闸门的真实输出；局部测试不能替代完整闸门。
- `AGENTS.md` 是唯一手工编辑源；不生成 `CLAUDE.md`，也不存在 AGENTS—CLAUDE 镜像或同步门禁。
- 修改模块或运行入口时，同一提交更新 `TECHNICAL_MANUAL.md` 中相应的操作说明；不要把旧设计、Goal 日志或未验证的报告重新引入根目录。
