# 正式 runtime 目录快照

捕获时间：2026-08-28 13:11:33 +08:00  
正式 runtime 根目录：I:/Creation_assistant-runtime

## 顶层目录

| 名称 | 类型 | 备注 |
|---|---|---|
| formal | 目录 | 正式数据库、正式原始资料、转写和运行材料 |
| external | 目录 | 外部能力运行资料和浏览器资料 |
| external_collection | 目录 | TrendRadar 等外部采集输出 |
| agent_platform | 目录 | 平台日志、后台状态和旧入口运行记录 |
| local | 目录 | 本地运行资料 |
| model_service | 目录 | 模型服务相关运行资料 |
| runtime_identity.json | 文件 | 运行根目录身份凭证 |

## formal 目录

实际可见的主要子目录：

- backups；
- competitor_registration；
- competitor_registration_runtime；
- daily_operations；
- daily_repairs；
- mediacrawler_runtime；
- transcripts。

大体积采集、浏览器、媒体和转写材料没有整体复制到项目快照，只记录其所在目录和运行身份。

## agent_platform 关键文件

| 文件 | 大小 | 最后修改时间 | 现场意义 |
|---|---:|---|---|
| business_runtime_guard_events.jsonl | 396731 | 2026-08-28 09:33:55 | 运行守卫事件 |
| daily_collection_once.log | 2230244 | 2026-08-28 09:35:59 | daily 运行日志 |
| daily_operations_state.json | 2984 | 2026-08-27 08:11:13 | 最近 daily 状态 |
| cold_start_pause_state.json | 3435 | 2026-07-29 13:21:50 | cold-start 暂停状态 |
| collection_protection_state.json | 119 | 2026-07-23 16:15:45 | 采集保护状态 |
| mediacrawler_browser_state.json | 208 | 2026-07-23 16:08:45 | 浏览器运行状态 |
| cold_start_config_server.stdout.log | 15850 | 2026-07-23 22:32:26 | 已删除页面服务的历史日志 |
| cold_start_server.stdout.log | 93414 | 2026-07-27 04:43:10 | 旧服务历史日志 |
| cold_start_ui.stdout.log | 10994 | 2026-07-23 22:00:28 | 旧页面历史日志 |

## 后台执行残留

当前发现两个 cold-start executor 记录：

| 记录 | 对应 cold-start | 状态 | 开始 | 退出 |
|---|---|---|---|---|
| 664cfe12cbd870a461bf5050.executor.json | cold_start_cold_start_configuration_f05e0f0025c70c50414fa66b | stopped | 2026-08-24 07:34:09 UTC | 2026-08-24 13:15:11 UTC |
| d400fcd68b3a8c266c03a7bc.executor.json | cold_start_cold_start_configuration_2915c61ece8c460db45e5cd3a0738b03 | failed | 2026-08-26 17:34:34 UTC | 2026-08-26 17:38:05 UTC |

运行目录中一共发现 1574 个 attempt_state.json，其中 succeeded 1573 个、running 1 个。running 残留没有被清理。

## 最近记录

daily_operations_state.json 表明最近一次 all_domains_competitor_tracking 的触发标记是 daily_scheduled，validation_only 为 false，最终状态为 completed_with_failures。失败包括：

- domain_1833831517eb 的 content type registry 未冻结；
- music_entertainment 的正式边界未冻结；
- 候选数为 0。

该文件是历史现场证据，不在保护期内继续触发运行。

## runtime_identity.json

记录的正式根目录是 I:/Creation_assistant-runtime，数据库是 I:/Creation_assistant-runtime/formal/production_activation.sqlite3，迁移状态字段为 completed，重写旧路径数量为 2628。

其中保存的数据库摘要与当前数据库摘要不一致，已在正式数据库快照中记录。本轮不修正。

## 其它数据库文件

正式 runtime 下面还发现浏览器缓存数据库、TrendRadar 输出数据库和两个旧正式备份。它们不能被统称为测试数据库：

- 浏览器缓存数据库属于外部工具运行状态；
- TrendRadar 数据库属于外部采集材料；
- formal/backups 下两个旧文件属于历史正式备份；
- 本轮新增的 migration_baseline_20260828T130918779.sqlite3 是正式数据库的迁移前备份。

