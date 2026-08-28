# 正式入口现场清单

捕获时间：2026-08-28 13:11:33 +08:00  
本清单不关闭任何入口，只记录迁移开始前的入口事实。

| 入口 | 当前状态 | 是否能启动或改变正式业务 | 现场判断 | 最终方向 |
|---|---|---|---|---|
| scripts/agent_platform/hermes_daily_entry.py | 文件存在 | 是，调用正式 daily | Hermes 命名，实际进入 Core 并写正式状态 | 转为外部调用适配，旧正式路线退出 |
| scripts/agent_platform/run_daily_collection_once.py | 文件存在 | 是，直接协调正式 daily | 可绕过 Hermes daily wrapper 直接运行 | 收回 Core 唯一正式入口 |
| scripts/core/production/stage1b_daily_discovery.py | 文件存在 | 是 | 支持 production_daily 和 real_daily_validation，当前验证记录仍是 production | 转为 Core 业务入口，隔离测试 |
| scripts/agent_platform/hermes_cold_start_management_entry.py | 文件存在 | 是 | 可预览、确认、状态、停止、恢复和人工审核 | 转为薄调用入口 |
| scripts/agent_platform/cold_start_background_entry.py | 文件存在 | 是 | 后台执行 cold-start，已有 stopped/failed executor 残留 | 由 Core 统一 Business Run/Execution |
| scripts/agent_platform/hermes_formal_research_entry.py | 文件存在 | 是 | 可建立、执行、批准、退回、重试、修订正式研究 | 回到 Core 正式研究入口 |
| scripts/agent_platform/hermes_daily_repair_entry.py | 文件存在 | 是 | 可计划和应用正式 daily 修复 | 保护期停止，后续回到 Core |
| scripts/agent_platform/hermes_knowledge_convergence_entry.py | 文件存在 | apply 可写 | audit/verify 查看，apply 写 Core 并生成知识镜像 | 保护期停止 apply，后续由 Core 统一 |
| scripts/agent_platform/hermes_competitor_breakdown_test_entry.py | 文件存在 | 当前主要读正式库并调用模型 | 名称含 test，但读取正式库，不能当作安全测试入口 | 迁入测试世界或退出 |
| scripts/agent_platform/hermes_native_feishu_outbound_entry.py | 文件存在 | 主要发送外部消息 | Hermes/Feishu 专属承载，不是业务真相源 | 变为外部通知适配 |
| scripts/agent_platform/hermes_tool_registry.py | 文件存在 | 暴露 cold-start 操作 | 目前是 Hermes 工具路由，不应持有状态 | 后续由 MCP 薄层替代 |
| scripts/agent_platform/hermes_daily_entry.sh | 文件存在 | 手工或 WSL 可调用 | 写死旧 WSL 项目路径和 Hermes daily | 后续转换或删除 |
| Windows Startup/CreationAssistant_Listener.vbs | 文件存在但目标失效 | 登录时存在尝试启动可能 | 指向不存在的 I:/Creation_assistant/scripts/feishu/listener.py | 新路线验证后删除 |
| Windows Startup/CreationAssistant_Platform.lnk | 文件存在但目标失效 | 登录时存在尝试启动可能 | 指向不存在的 Python 和已删除页面服务 | 新路线验证后删除 |
| Windows 计划任务 | 未发现匹配项 | 当前没有证据表明可自动触发 | 查询没有 Creation Assistant 相关任务 | 不新增调度器 |
| 当前 MCP | 未发现可用项目实现 | 否 | 本轮没有开始 MCP | 阶段 2 建立 |
| 当前网页控制台 | 未发现可用实现 | 否 | 旧页面文件已删除，但启动残留存在 | 目录迁移后阶段 6-7 建立 |

## 当前运行证据

- 当前没有发现匹配的相关进程。
- 当前没有发现匹配的 Windows 计划任务。
- 不能因此宣布自动启动残留已经清除。
- WSL 发行版运行状态因查询返回 E_ACCESSDENIED 未能确认。
- 迁移保护期内不关闭或修改这些入口，只禁止把它们作为新的正式业务入口使用。

