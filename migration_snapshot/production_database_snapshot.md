# 正式数据库迁移前快照

捕获时间：2026-08-28 13:11:33 +08:00  
正式数据库：I:/Creation_assistant-runtime/formal/production_activation.sqlite3

## 文件摘要

| 项目 | 结果 |
|---|---|
| 文件大小 | 62418944 字节 |
| 最后修改时间 | 2026-08-28T09:35:59.0350537+08:00 |
| 操作前 SHA-256 | 5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD |
| 操作后 SHA-256 | 5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD |
| 迁移前备份 SHA-256 | 5262A76E395E1B01386192EDA39E6EBAE1018356CF259B90912FB54A095E82BD |
| 迁移前备份大小 | 62418944 字节 |
| schema 表数量 | 116 |
| schema 摘要 | b734947315c6defbf97f1a02f9d3c71aa282aebf4623c3ba782432ff7c1b09fa |

## 备份

本轮创建了一个新备份，没有覆盖旧备份：

I:/Creation_assistant-runtime/formal/backups/migration_baseline_20260828T130918779.sqlite3

备份是文件复制和摘要校验，不是数据库写入。没有执行 VACUUM、repair、UPDATE、DELETE、schema migration 或 lifecycle 修改。

## 关键表行数

| 表 | 行数 |
|---|---:|
| competitor_accounts | 50 |
| competitor_videos | 2153 |
| baselines | 2760 |
| hits | 647 |
| hit_comments | 8890 |
| hit_deep_analysis | 537 |
| hit_transcripts | 636 |
| video_checks | 1730 |
| stage0_cold_start | 2 |
| stage0_cold_start_configuration | 2 |
| stage0_competitor_registration | 50 |
| stage0_competitor_registration_item | 1074 |
| stage0_competitor_registration_model_run | 484 |
| stage0_competitor_registration_step | 207 |
| stage0_competitor_breakdown_attempt | 947 |
| stage0_competitor_material_collection_checkpoint | 35 |
| stage0_daily_run | 4 |
| stage0_daily_hit_breakdown | 71 |
| stage0_daily_hit_model_run | 108 |
| stage0_daily_hit_processing_failure | 36 |
| stage0_model_run | 14 |
| stage0_content_task | 1 |
| stage0_content_node_version | 33 |
| stage0_content_node_failure | 13 |
| stage0_input_assembly | 16 |
| stage0_research_material | 89 |
| stage0_human_decision_command | 101 |
| stage0_audit_event | 5108 |
| stage0_command_receipt | 2082 |
| command_receipt | 1 |
| object_reference | 43 |
| trace_root | 43 |
| trace_version | 43 |
| stage1_question_expansion_source | 39 |
| stage1_question_expansion_qualification | 39 |
| stage1b_discovery_run | 30 |
| stage1b_run_execution_context | 30 |
| stage1b_model_run | 65 |
| stage1b_source_version | 73 |
| stage1b_filter_result | 73 |
| stage1b_candidate_version | 36 |
| stage1b_candidate_assessment | 0 |
| stage1b_candidate_decision | 0 |
| stage1b_candidate_support | 36 |
| stage1b_candidate_pool_state | 36 |
| stage1b_candidate_absence | 13 |
| stage1b_daily_snapshot | 111 |
| stage0_experience_candidate | 14 |
| stage0_experience_candidate_model_run | 13 |
| stage0_experience_candidate_run | 1 |
| stage0_live_cold_start_preflight | 8 |
| stage0_knowledge_mirror_run | 6 |
| scheduler_job | 0 |
| scheduler_job_attempt | 0 |
| outbox_message | 0 |

## 关键状态

| 对象 | 状态和数量 |
|---|---|
| stage0_cold_start | completed 1；failed 1 |
| stage0_content_node_version | approved 1；awaiting_human_review 3；failed 13；processing 16 |
| stage0_experience_candidate | awaiting_human_decision 13；preparing 1 |
| stage0_experience_candidate_run | running 1 |
| stage1b_discovery_run | completed 6；failed 17；processing 7 |
| stage1b_candidate_version | awaiting_user_decision 36 |
| stage0_human_decision_command | completed 77；rejected 23；received 1 |
| stage0 cold-start content type candidate | awaiting_human_decision 1 |
| stage0 cold-start domain boundary candidate | failed 1 |
| stage0 cold-start tag library | accepted 1；awaiting_human_review 1 |

stage1b execution mode 和身份：

- production_daily / production：18；
- real_daily_validation / production：12。

这说明验证运行仍然使用 production 身份，是正式/测试隔离的现场异常。

## 模型历史身份

数据库中保留了多种执行历史，不能作为新运行的模型选择依据：

- stage0_model_run：mimo_main / hermes / mimo-v2.5-pro，succeeded 8、failed 6；
- stage1b_model_run：mimo_main / hermes / mimo-v2.5-pro，succeeded 64；relay_main / hermes / stealth/ox-alpha，failed 1；
- competitor registration：gpt-5.5、gpt-5.6-terra、mimo-v2.5-pro、stealth/ox-alpha、xiaomi/mimo-v2.5-pro；
- daily hit：gpt-5.5、mimo-v2.5-pro、stealth/ox-alpha、xiaomi/mimo-v2.5-pro；
- experience candidate：gpt-5.5。

## 数据库异常

当前数据库所有已检查的 data_identity 都是 production，没有发现 test 身份行。

runtime_identity.json 记录的旧数据库摘要是：

53b46277a07a7ada7ce2d428445c10103d8af3efd132b3eee962218866bb2b7a

它与本次读取到的当前数据库摘要不同。本轮只记录，不修改身份文件，不修改数据库，不重新计算或修复正式事实。

数据库操作前后文件摘要一致，因此本轮没有改变数据库内容；schema 摘要和表数量作为迁移起点记录。

