---
name: hotspot-report
description: 合并多平台热点，按用户偏好排除并交给 Core 计算综合榜；用于热点日报、Top 榜和排除清单，不自动生成或确认选题。
---

通过 `creation_assistant_hotspot_report` 使用本 Skill。`collect` 获取当前 TrendRadar 数据及已装配的任务；`prepare` 对已取得的快照重放。执行返回的 `task.skill.rendered_instructions`，把完整事件判断与原快照通过 `render` 交回，Core 计算分数并区分榜内、排名靠后和偏好排除。

业务指令正文在 [prompt.md](prompt.md)，由项目现有 runtime skill 装配器读取。用户偏好由 Core 注入，不依赖聊天记忆，不自己指定模型或调用模型服务。采集和运算由 Core 提供；Agent 只完成语义合并、偏好判断和汇报。

用户没有指定条数时返回全部保留事件，不自行设每日数量。热点榜不是候选选题榜；音乐不自动热点转题，泛科普—社会生活只在用户要求转题时进入既有公共流程，其他领域按用户要求和领域规则处理。不得把本工具的报告写成正式候选、研究或用户决定。
