# 项目现场目录清单

捕获时间：2026-08-28 13:11:33 +08:00  
项目完整路径：I:/Creation_assistant-codex

## 根目录实际存在的主要内容

- .codex：本地 Codex 配置、状态和 hooks。
- .env、.env.example、.env.runtime.local：环境配置来源；实际值未复制。
- .git：Git 元数据。
- .playwright-cli：浏览器测试状态。
- .pytest_cache：测试缓存。
- .stage_runtime：阶段探针、旧页面启动脚本和临时运行状态；被忽略但可能影响判断。
- .workbuddy：验证材料。
- AGENTS.md：项目边界说明。
- config：业务 guardrail、domain pack、外部采集、模型路由和存储配置。
- docs：当前和历史设计、执行和验证文档。
- execution：阶段说明和当前实现状态文件。
- runtime_skills：Skill 合同、输入输出和提示材料。
- scripts：Core、agent platform、外部能力和直接入口。
- tests：测试代码，主要使用内存数据库或临时目录。
- validation：验证材料和验证入口。
- topic-structure-check-*：临时结构检查目录。
- kb-install-check-*：知识库安装检查目录。
- vendor：外部工具和 TrendRadar。
- vault：Junction，实际指向 I:/Obsidian/创作助手/经验库。
- wsl$、wsl.localhost：本机 WSL 访问入口痕迹。
- CUsers15891：未跟踪的、类似 Hermes profile 的异常目录副本。

## 规模提示

- Git 已跟踪文件约 166 个。
- 根目录即时条目约 36 个。
- 递归 Python 文件约 17572 个，包含 vendor 和环境目录，不能等同于项目业务代码数量。
- 递归 Markdown 文件约 548 个。

## 当前重要事实

- execution/current_stage.yaml 不存在。
- scripts/execution/stage_control.py 不存在。
- docs/EFFECTIVE_DESIGN_BASELINE.md 存在，但文档不是唯一运行事实。
- 项目当前工作树在快照前已经很脏；快照没有整理、提交或清理它。
- 当前项目目录没有 data/formal、data/test、outputs、logs 正式数据目录。
- 正式数据库不在项目目录，而在 I:/Creation_assistant-runtime/formal/。

