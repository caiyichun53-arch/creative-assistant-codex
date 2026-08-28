# 阶段0迁移现场快照

快照类型：Creation Assistant 独立化迁移阶段0  
快照时间：2026-08-28 13:11:33 至 13:17:00 +08:00，数据库备份完成于 13:09:18 +08:00  
快照根目录：I:/Creation_assistant-codex/migration_snapshot/

这份目录保存迁移保护期开始前的可追溯现场证据。它记录当前脏工作树、正式数据库摘要、正式 runtime、配置来源、入口和绝对路径。

正式数据库没有复制到项目目录。正式数据库的迁移前备份仍在：

I:/Creation_assistant-runtime/formal/backups/migration_baseline_20260828T130918779.sqlite3

快照不是新的业务状态源，也不参与 daily、cold-start、resume、候选、研究、修复或知识判断。快照生成后不应被当作运行配置修改。阶段0完成后正式进入迁移保护期。

生成快照时没有：

- 启动正式业务；
- 调用模型；
- 修改正式数据库内容或 schema；
- 清理 failed、processing、running；
- 修改配置、环境变量、Hermes、Skill、Obsidian、Windows 或 WSL；
- commit、reset、stash、checkout、clean 或删除临时文件。

注意：git_state.txt 记录的是快照目录加入之前的 Git 状态，因此它包含当时已经存在的迁移基线文件，但不包含本快照目录本身。
