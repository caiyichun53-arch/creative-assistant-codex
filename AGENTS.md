# 创作助手 · 工程章程(AGENTS.md)

## 当前唯一设计基线

`docs/EFFECTIVE_DESIGN_BASELINE.md` 是当前唯一的业务设计与实施裁决入口。

它是自包含基线：产品、实施、测试、界面和操作说明不得要求执行者再从旧 Goal、旧迁移计划、历史报告、交接状态、聊天记忆或其他设计材料拼接规则。任何文件与该基线冲突时，以该基线为准；未被它冻结的实现细节必须明确标为候选或待校准，不得由旧文件或现有代码自动升级为正式设计。

本仓库是一个 **Codex 工程**，Codex 当创作驾驶舱；Codex、Claude Code 等仅是工程开发工具，不是运行期业务 Provider。运行期模型路由只由 `config/model_routes.yaml` 的显式位点决定，禁止 fallback、自动降级或隐式 Provider 切换。

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

## 交付纪律

- 每个改动以 `docs/EFFECTIVE_DESIGN_BASELINE.md` 为验收依据，并运行与改动相称的测试或闸门。
- 完成状态必须附对应闸门的真实输出；局部测试不能替代完整闸门。
- `AGENTS.md` 是唯一手工编辑源。修改后必须运行 `python -m scripts.validation.generate_constitution_mirror` 生成 `CLAUDE.md`，并运行宪法同步测试。
- 修改模块或运行入口时，同一提交更新 `TECHNICAL_MANUAL.md` 中相应的操作说明；不要把旧设计、Goal 日志或未验证的报告重新引入根目录。
