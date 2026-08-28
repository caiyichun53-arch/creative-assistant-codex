# 绝对路径现场清单

捕获时间：2026-08-28 13:11:33 +08:00  
本清单只记录，不修复。

## 项目路径和 runtime 路径

| 位置 | 行或配置位置 | 当前用途 | 当前判断 | 后续阶段 |
|---|---|---|---|---|
| config/external_collection.yaml | project_dir 配置 | 外部采集使用项目根目录 | 活动配置 | 阶段 1、4 |
| config/runtime_storage.json | formal_runtime_root | 指定正式 runtime | 活动配置 | 阶段 1 |
| scripts/agent_platform/hermes_daily_entry.sh | 第 4 行 | WSL 切换到旧项目目录 | 活动文件，WSL 运行未证实 | 阶段 1、8 |
| .codex/hooks/business_command_gate.py | 第 40-42 行 | 识别正式 runtime 路径 | 活动 hook | 阶段 1、4 |
| .env.runtime.local | 第 2、10-12 行 | SenseVoice、MediaCrawler 和音乐 profile 路径 | 部分仍指向旧项目目录 | 阶段 1、4 |
| .stage_runtime/superpower_manual/launch_ui_hidden.vbs | 第 2 行 | 旧页面服务启动工作目录 | 失效旧入口 | 阶段 8 |
| .stage_runtime/voxcpm_probe_active.json | 第 2、3、5 行 | 阶段探针输出目录 | 临时现场 | 阶段 3、8 |

## 旧项目目录名称

发现以下路径或路径变体：

- I:/Creation_assistant-codex；
- /mnt/i/Creation_assistant-codex；
- I:/Creation_assistant；
- I:/Creation_assistant-codex/data/formal；
- /mnt/i/creation_assistant-runtime/formal。

其中 I:/Creation_assistant-codex 是当前项目目录，不能在本轮修改。I:/Creation_assistant 和 data/formal 是旧入口或旧外部能力路径，不能在本轮修复。

## Hermes 和外部路径

| 位置 | 行或配置位置 | 当前用途 | 当前判断 |
|---|---|---|---|
| scripts/agent_platform/hermes_knowledge_convergence_entry.py | 第 21 行 | 直接指定 Obsidian 知识镜像目录 | 活动入口，Hermes 命名 |
| scripts/agent_platform/hermes_native_feishu_outbound.py | 第 20、22 行 | 指定 Hermes agent 和 profile | Hermes 专属外部承载 |
| Windows Startup/CreationAssistant_Listener.vbs | 第 1 行 | 指向旧 I:/Creation_assistant listener | 失效但有启动残留 |
| Windows Startup/CreationAssistant_Platform.lnk | 快捷方式属性 | 指向旧 Python 和已删除页面服务 | 失效但有启动残留 |
| 工作区 CUsers15891/.hermes | 目录结构 | 未跟踪的 Hermes profile 类副本 | 当前有效性未证实 |

## 知识库路径

- vault Junction 实际指向 I:/Obsidian/创作助手/经验库；
- scripts/agent_platform/hermes_knowledge_convergence_entry.py 第 21 行直接使用 I:/Obsidian/创作助手/经验库；
- 知识库不是 Obsidian 本身，未来要迁移的是知识库概念、资料分层和正式规则发布边界。

## 其它外部绝对路径

- I:/AI_Models/sensevoice/sensevoice-small；
- I:/AI_Models/modelscope_cache/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch；
- I:/AI_Models/ffmpeg/ffmpeg.exe；
- I:/AI_Models/VoxCPM2；
- I:/api-key.txt；
- /home/caibin/.hermes/hermes-agent；
- /home/caibin/.hermes/profiles/creator。

这些路径分别属于本地模型/程序、密钥文件或 Hermes 外部承载；本轮只记录，不改动。

