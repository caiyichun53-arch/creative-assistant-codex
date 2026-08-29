# Creation Assistant 迁移保护期

保护期开始依据：阶段0封存完成  
记录时间：2026-08-28 13:17:00 +08:00

## 规则

从阶段0完成开始，暂停新的正式业务写入。

在独立 Core、MCP 正式路线、production/test 可靠隔离、旧历史状态不再控制新执行，并且用户明确允许恢复前，不得启动：

- 新的正式 daily；
- 新的 cold-start；
- 正式 resume；
- candidate discovery；
- formal research；
- daily repair；
- knowledge apply；
- real_daily_validation；
- repair/conversion 写正式库；
- 其它改变正式业务状态的入口。

## 允许

- 只读查询；
- 正式数据库备份；
- 文件和数据库摘要校验；
- 迁移现场盘点；
- 生成迁移凭证；
- 开发；
- 独立测试世界中的测试。

## 禁止

- 修改正式数据库业务内容；
- 清理 failed、processing、running；
- 修正正式记录；
- 补写正式记录；
- 迁移正式数据；
- 为迁移问题改正式事实；
- 通过修改正式数据库实现只读；
- 通过旧 Hermes 入口继续正式业务；
- 通过 direct CLI 写正式业务；
- 运行真实 daily、cold-start 或正式 resume；
- 调用模型。

## 当前自动运行判断

当前未发现相关运行进程，也未发现匹配的 Windows 计划任务。

但仍发现：

- Windows Startup 中的旧 listener 启动项；
- Windows Startup 中的旧 platform 页面启动项；
- WSL 中的旧 Hermes daily shell；
- agent_platform 中的 daily_scheduled 历史状态；
- 已停止或失败的 cold-start executor 记录。

这些残留本轮没有被删除或修改。当前没有证据表明它们能在本轮直接成功写入正式数据，但它们代表潜在自动尝试，不能视为已清除。

## 边界声明

本轮没有建立 migration lock、permission framework、database write firewall、watchdog 或 global write fence。

保护期当前由迁移纪律和入口收敛规则维持。技术层强制只读留到后续独立 Core 和正式/测试隔离阶段。

## Codex Hook 语义边界

项目 PreToolUse 和 Stop Hook 只负责开发期的提前阻断、状态核对和审计记录。它们能够触发时用于尽早发现跑偏，但不是正式业务安全边界，也不是业务正确性的必要条件。

即使 Codex 没有触发项目 Hook，正式业务仍必须由 Core、runtime 以及 FORMAL/TEST 隔离独立拒绝直接正式写入、身份错误和正式/测试串库。Hook 不触发不代表操作已获准；它只代表少了一层提前提醒或开发期门禁。

## Stage 5 closure record

Recorded: 2026-08-30 +08:00

Stage 5 Core observability is complete. The Core now provides one read-only
current/history status result for CLI, MCP and later consumers. MCP reuses
that result and does not infer a second status model. FORMAL and TEST remain
explicitly isolated.

Current-state authority remains the current domain activation. When a domain
has no current activation, its current cold-start and current daily are empty.
Historical failed, stopped, completed, processing and running rows remain
historical records and do not receive current business control. The formal
database was not changed. Stage 6 is not opened by this record.

Validation record: 33 focused status and isolation tests passed; the full test
run passed 340 tests and retained the 9 pre-existing missing trusted_context
fixture errors without adding exclusions.

Formal database SHA256:
B8D5A61E656798C485DC51DF64623F3985617C9B9A1BB1ECB2D3C38FDBA174F8
