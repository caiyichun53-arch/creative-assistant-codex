# GOAL-08 - Production Version Chain

## Scope

实现 research/content plan、script、review、批准、
publication capture 和人工修改。

## Acceptance

批准稿与实际发布稿分离。

每次正文变化必须产生新版本，不得原地覆盖。

偏好证据必须可追溯。

单次改稿不得自动成为永久内容偏好。

## Required process

1. 只读取V0.6.2中与GOAL-08直接相关的V0.6.1/R07章节，
   不完整重读整份文档。
2. 读取CODEX_GOAL_RESUME_PROTOCOL.md。
3. 创建或更新GOAL-08 ExecPlan和progress。
4. fixture、replay、fault、FakeClock测试优先。
5. 使用正式production handlers实现，不建立测试专用业务逻辑。
6. 执行两轮复核和跨章节职责检查。
7. 每个原子检查点完成最小测试、更新progress并提交checkpoint。
8. 生成validation report和clean-room proof。
9. 完成后停止等待批准，不自动进入GOAL-09。

## Hard stops

不得在规格之外新增业务状态、业务表、Agent、LLM调用
或用户确认步骤。

不得使用真实凭证或执行不可逆外部操作。

## R07 additions

记录以下来源中的明确偏好指令和evidence refs：

- 用户明确指令
- 人工改稿
- 批准
- 拒绝
- publication capture

单次人工修改不得自动成为持久偏好。
