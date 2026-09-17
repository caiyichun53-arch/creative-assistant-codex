---
name: candidate-priority
description: 对 Core 提供的已合并候选及支持材料评分，说明制作优先级并交回 Core 排序；不生成选题、不替用户选择。
---

通过现有 MCP 的 `creation_assistant_get_external_task` 获取 `candidate_priority` 任务，身份是 `run_id` 和 `domain_label`。执行任务中装配的 [评分指令](prompt.md)，通过 `creation_assistant_submit_external_result` 交回完整结果和实际执行者信息，再继续原日报。模型始终由当前 Agent 决定，项目没有评分模型路由。

只评 Core 提供的全部合并候选，不把前 10 当作处理上限。提交时保留输入指纹；材料改变后按新输入重评，不沿用旧判断。分数只说明制作优先级，不代表播放量或用户批准。热点榜另行计算，不能用于替代本评分。

先读 domain_rules，再读候选及材料中的成题搜索记录。具体领域排除优先于通用内容形式，不能靠降分把未成形或已排除的方向当成合格选题。提交响应的 continuation 用于继续同一批次，不自行新建流程。

提交后可通过 `creation_assistant_get_external_result` 配合同一任务身份读取完整评分结果（包括优先展示之外的候选）。继续原日报时沿用已有等待阶段与采集结果，不另起日报或再次采集。
