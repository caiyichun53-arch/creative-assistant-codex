# 创作助手 · 技术手册

## 1. 适用范围

本手册只说明当前已存在的运行入口、配置和验证方式；它不定义业务规则。
业务设计与实施裁决唯一依据为 [`docs/EFFECTIVE_DESIGN_BASELINE.md`](docs/EFFECTIVE_DESIGN_BASELINE.md)。本手册与该基线冲突时，以基线为准。

## 2. 配置与模型路由

- 运行期模型路由唯一配置：`config/model_routes.yaml`。
- 当前模型位点为 `dialogue_model`、`business_model`、`writing_model`；每个位点通过 `provider_ref` 显式绑定 provider。
- `fallback` 必须为 `none`。`scripts/core/model_gateway/model_router.py` 会拒绝任何非 `none` 的配置。
- 开发工具不属于运行期 Provider。运行入口通过 `ModelRouter` 和 `HermesModelProviderAdapter` 使用当前配置。

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

### 外部适配器

`scripts/core/external_adapters/` 隔离采集、评论、研究和转写等外部能力。适配器只传递受控输入与结果，不能自行改变业务流程或作为模型调用入口。

## 4. 原子 Skill 与人工闸门

### 业务工作流

`scripts/core/workflow/` 只承载当前可执行的流程编排；流程边界以有效基线和实际测试为准。

### 经验库

`scripts/core/experience/` 负责证据、候选打法、内容绑定和人工审核队列。候选或提案不等于正式经验，正式经验语义不得被自动改写。

- 可移植原子 Skill 位于 `runtime_skills/`，只处理其 schema 定义的输入和输出，不自行读取业务数据库或串联其他 Skill。
- 运行绑定位于 `scripts/core/experience/run_*.py` 与 `scripts/core/model_gateway/formal_skill_adapter.py`。
- 需要人工确认的内容通过 `scripts/core/experience/review_queue.py` 管理；自动化代码不得把候选经验直接升级为正式经验。
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
- 修改 `AGENTS.md` 后，运行 `python -m scripts.validation.generate_constitution_mirror` 刷新 `CLAUDE.md`，随后运行宪法同步测试。
