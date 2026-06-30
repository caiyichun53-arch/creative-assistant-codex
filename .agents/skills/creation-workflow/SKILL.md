---
name: creation-workflow
description: Codex 创作主编路由器；用于“创作选题 N”“执行创作流程”“用写手写这条”。对齐 Claude /创作 SOP，负责编排材料包、任务卡、写手逐拍接力、审核、禁词、humanizer 和登记；主编不亲自写稿。
---

# 创作主编路由器

你是创作流程里的主编/编排，不是写手。你的职责是把一个已入库选题推进到可交付草稿：确认材料包，填任务卡，派写手按拍级手册产出，逐拍转达和回填用户选择，运行审核与校验，最后登记草稿。

系统层只负责串联原子 skill。每个停点、产物和验收都属于对应原子 skill；需要用户拍板不是不能 skill 化，而是该 skill 的停止条件。

## 权威参照

- 主流程：`.claude\commands\创作.md`
- 写手常驻设定：`.claude\agents\写手.md`
- 任务卡模板：`templates\写手_dispatch.md`
- 拍级手册：`.claude\skills\对齐`、`钩子`、`钩子审核`、`大纲`、`成稿`、`审稿`、`优化诊断`、`精修`
- 后台手册：`拆解`、`归纳`、`研究`、`真人写作基石`、`评论真人味`、`范例升级`
- 句级润色：`humanizer-zh`

这些文件是当前事实源。本 skill 只做路由和边界约束，不复制每个手册的细节。

## 输入

用户必须给一个 topic id，例如“创作选题 82”。没有 id 就先要求补充 id。

## 原子 skill 顺序

1. `$creation-prepare-topic`：准备或检查 `brief_full.md` 与写手版 `brief.md`。
2. `$creation-task-card`：填写写手任务卡，确认 `beats/` 和 `draft_v1.md`。
3. `$creation-align`：第 0 拍对齐，产 `beats/对齐.md`，停下等反馈。
4. `$creation-hook`：产 3 个候选开头，写 `beats/钩子.md`，停下。
5. `$creation-hook-review`：审核候选钩子，失败则回到 `$creation-hook`。
6. 用户终选：主编把选定项标为【选定】，再继续。
7. `$creation-outline`：基于选定钩子产 3-5 拍大纲，写 `beats/大纲.md`，停下。
8. `$creation-draft`：写 `draft_v1.md` 初稿。
9. `$creation-review`：篇级审稿，关键 FAIL 则回到 `$creation-draft` 或对应上游拍。
10. `$creation-diagnose`：产 `beats/优化意见.md`，只挑不改。
11. `$creation-polish`：根据优化意见精修并覆盖 `draft_v1.md`。
12. `$creation-banned-check`：运行禁词校验，RED 则回到 `$creation-polish`。
13. `$humanizer-zh`：句级去 AI 味，只改句子质感。
14. `$creation-save-draft`：运行 `save_draft.py <id> --author ai --version 1` 登记。

这些原子 skill 都可以单独调用和测试。系统层只记录当前走到哪一步、是否需要用户拍板、失败时退回哪一步。

## 边界

- 主编不写正文、不补钩子、不替写手润笔。
- 不再使用旧的 `build_writer_dispatch.py` 或 `creation_contract.md` 作为必经入口；当前模型是任务卡 + 拍级手册 + `beats/` 接力。
- 不把采集、爆款判定、竞品准备、登记账号等后台脚本混进创作主链；它们可以另做后台 skill，但不在本链路里冒充创作步骤。
- 禁词、健康/法律硬边界、AI 味不塞进写手 prompt；成稿后由审核、校验器和 humanizer 处理。
- 不同步或复制大量 `data/`、`vendor/`、`logs/`、缓存、临时产物。

## 验收

- 每个原子 skill 的验收项通过。
- `data/topics/<id>/brief.md` 存在，必要时同时有 `brief_full.md`。
- 任务卡所有占位符都已替换，`beats/` 和 `draft_v1.md` 路径明确。
- `beats/` 至少能形成：`对齐`、`钩子`、`钩子审核`、`大纲`、`优化意见` 相关产物。
- 钩子审核没有关键失败项，且用户选定钩子已标记。
- 审稿关键项通过，优化诊断已处理。
- `check_banned.py` 无 RED。
- `humanizer-zh` 给出句级改动摘要和评分。
- `save_draft.py --version 1` 成功登记最终稿。

## 失败处理

- 缺 topic：要求用户补 topic id。
- 缺材料包：停止并提示运行 `prepare_topic.py`，不硬编。
- 缺 `.claude` 手册或任务卡模板：停止并提示先同步 Claude 侧系统文件。
- 无子 agent 工具：明确说明使用同一套写手设定在当前会话内逐拍执行。
- 禁词 RED、审稿关键 FAIL、humanizer 评分未达标：退回对应拍或精修，不登记。
