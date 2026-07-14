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
- 运行配置、Skill 合同、测试 fixture 与闸门输入仅在代码实际读取时保留；生成报告、一次性验证输出、旧 Goal、迁移材料、历史交接与旧 Skill 说明不构成设计依据。
- 移动、删除或归档文档前，必须搜索 Python、PowerShell、Shell、测试、CI、配置读取和模型指令的实际引用；发现引用时先解除引用并验证。
- 任何模型指令、README、代理配置或启动脚本不得引导读取旧设计或缺失的治理文件。

## 会话启动与自驱执行协议

- 每个新会话必须依次读取 `docs/EFFECTIVE_DESIGN_BASELINE.md`、`docs/IMPLEMENTATION_EXECUTION_BASELINE.md`，然后校验项目目录、当前分支、HEAD、工作区和当前执行指针；任一不一致即停止并汇报。
- 上述事实一致时，直接执行执行指针中的当前唯一任务，不等待聊天重新规划，也不得将历史文档、旧 Goal、交接、聊天记忆或旧代码作为业务设计依据。
- 在用户已批准的当前阶段内，Codex 连续完成：核查→修改→测试→修复本阶段内问题→更新执行基线→创建阶段提交，并只在真正需要用户决定的位置停止。
- 普通技术步骤、同一阶段内的失败修复、测试和提交不重复询问下一步。只有需要业务选择、存在两个以上无法裁决的方案、与有效设计基线冲突、涉及正式数据删除/迁移/不可逆操作、模型/Provider/凭证/费用不明确，或当前阶段验收完成而需批准进入下一阶段时，才必须停止。
- 不生成、不读取也不依赖 `CLAUDE.md`；`.claude/` 不是当前执行依据。`AGENTS.md` 是唯一仓库执行章程的手工维护入口。

## 强制工作流门禁

- 所有任务在读取两份基线、核对目录、分支、HEAD、工作区和执行指针后，修改文件前必须运行 `python scripts/workflow_guard.py start`；非零退出必须立即停止，不得继续实施。
- 工作中可运行 `python scripts/workflow_guard.py check`；出现 `BASELINE_GAP`、`SCOPE_MISMATCH`、`USER_AUTH_REQUIRED`、需求映射缺失、基线哈希不一致或冻结验收测试被修改时，必须停止并汇报，不得移动文件、改写 Git 状态、删除检查或改门禁输出来规避。
- 完成前必须暂存本轮全部文件，确认没有未暂存和未跟踪文件，再运行 `python scripts/workflow_guard.py finish`。只有 `finish` 的 `task_finish_status: PASS` 并为当前 Git 索引生成凭证后才允许提交；这只表示当前治理/设计恢复任务可提交，不表示业务 Stage 具备实施资格或已经完成。
- Git 提交必须通过仓库 `.githooks/pre-commit` 对 finish 凭证的校验。禁止 `--no-verify`、禁用或替换 `core.hooksPath`、删除钩子、伪造凭证或绕过门禁。
- 真实数据库写入、真实网络/来源、安装、环境改动、付费服务和模型调用必须在 `execution/current_stage.yaml` 中显式授权，并在命令执行前以 `--formal-data-write`、`--external-call` 或 `--environment-change` 通过门禁；未授权必须返回 `USER_AUTH_REQUIRED`。不得先调用后补授权。
- 只有 `mode: design_recovery` 可以恢复设计；设计恢复只能修改 `docs/EFFECTIVE_DESIGN_BASELINE.md`，除非当前执行指针明确把门禁或章程治理文件列入白名单。旧 Goal、旧迁移计划、历史报告、交接状态、聊天记忆和旧代码不得直接影响业务代码。
- 每项 requirement 必须同时指向现行基线位置和直接测试；缺少任一项时 `finish` 不得通过。任何业务实现恢复前必须把 `design_complete` 和 `implementation_authorized` 明确改为 `true`，并由用户确认对应 Stage 详细设计。

## 交付纪律

- 每个改动以 `docs/EFFECTIVE_DESIGN_BASELINE.md` 为验收依据，并运行与改动相称的测试或闸门。
- 完成状态必须附对应闸门的真实输出；局部测试不能替代完整闸门。
- `AGENTS.md` 是唯一手工编辑源；不生成 `CLAUDE.md`，也不存在 AGENTS—CLAUDE 镜像或同步门禁。
- 修改模块或运行入口时，同一提交更新 `TECHNICAL_MANUAL.md` 中相应的操作说明；不要把旧设计、Goal 日志或未验证的报告重新引入根目录。
