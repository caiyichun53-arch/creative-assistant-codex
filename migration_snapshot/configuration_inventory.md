# 配置现场清单

> 这是历史配置快照，不是当前配置。项目模型路由及其环境变量已于 2026-09-07 清理；模型由接入的 Agent 管理，不得按此快照恢复调用。


捕获时间：2026-08-28 13:11:33 +08:00  
安全规则：没有复制 token、API key、密码、密钥文件内容或环境变量值。

## 当前会影响运行的配置来源

### Core 和业务配置

- config/runtime_storage.json：正式 runtime 根目录、数据库文件名、切换和错误根目录规则。
- config/external_collection.yaml：TrendRadar、domain search、项目路径、外部运行路径、输出证据路径和采集参数。
- config/model_routes.yaml：active_provider、mimo_main、relay_main、codex_subscription、模型路由和 fallback none。
- config/business_guardrails/competitor_registration.json：competitor registration 业务限制。
- config/business_guardrails/content_production.json：内容生产业务限制。
- config/business_guardrails/daily_operations.json：daily 业务限制。
- config/business_guardrails/stage_registry.json：阶段登记。
- config/business_guardrails/system_governance.json：系统治理边界。
- config/domain_packs/music_entertainment.yaml：音乐娱乐领域配置。
- config/domain_packs/domain_1833831517eb.yaml：泛科普-社会生活领域配置。
- config/domain_packs/fan_kepu_social_life.yaml：当前工作树显示为删除状态。
- config/banned_words.yaml：词汇限制。

### 本地运行和开发工具配置

- .codex/config.toml；
- .codex/hooks.json；
- .env；
- .env.example；
- .env.runtime.local；
- .stage_runtime 下的探针和旧启动脚本；
- vendor/TrendRadar 下的外部工具配置。

## 环境变量名称，仅记录是否存在

### 模型和 provider

.env 中存在：

- CREATION_LLM_PROVIDER；
- CREATION_LLM_MODEL；
- CREATION_LLM_FALLBACK_ENABLED；
- MODEL_ACTIVE_PROVIDER_REF；
- HERMES_BUSINESS_API_KEY；
- HERMES_BUSINESS_BASE_URL；
- HERMES_BUSINESS_MODEL_TOKEN；
- HERMES_BUSINESS_MODEL_BASE_URL；
- HERMES_BUSINESS_MODEL_NAME；
- HERMES_BUSINESS_MODEL_CLASS；
- GPT_RELAY_API_KEY_FILE；
- GPT_RELAY_BASE_URL；
- GPT_RELAY_MODEL；
- CODEX_CLI_PATH；
- CODEX_WORKBENCH_MODEL；
- CODEX_WORKBENCH_MODEL_CLASS；
- CODEX_WORKBENCH_TIMEOUT_SECONDS。

当前没有发现独立名为 OPENROUTER 的活动变量；实际发现的是 relay_main 和 GPT_RELAY_*。

### 交互和外部账户

.env 中存在：

- FEISHU_APP_ID；
- FEISHU_APP_SECRET；
- FEISHU_CHAT_ID；
- FEISHU_USER_OPEN_ID。

### 外部能力和媒体

.env.runtime.local 中存在：

- SENSEVOICE_PYTHON；
- SENSEVOICE_WORKER；
- SENSEVOICE_ASR_MODEL；
- SENSEVOICE_VAD_MODEL；
- FFMPEG_PATH；
- VOXCPM2_PYTHON；
- VOXCPM2_GENERATE_SCRIPT；
- VOXCPM2_MODEL_DIR；
- VOXCPM2_DEVICE；
- MUSIC_AUDIENCE_PYTHON；
- NETEASE_MUSIC_PROFILE_DIR；
- DOUBAN_MUSIC_PROFILE_DIR。

其中部分路径仍指向旧项目 data/formal 目录，见 absolute_path_inventory.md。

## 责任归属判断

应属于 Creation Assistant：

- 正式 runtime 唯一位置；
- 正式/测试身份；
- 数据库打开边界；
- 业务 guardrail；
- domain 业务边界；
- 正式状态和人工确认规则；
- 外部采集、ASR 等能力的调用契约。

应属于 Hermes 或 Codex 调用方：

- 当前模型；
- provider、endpoint、token；
- Hermes profile；
- Codex CLI/app server；
- Feishu、浏览器、聊天和消息传输；
- 调用方自己的运行环境。

需要后续转换或退出：

- CREATION_LLM_* 重复模型入口；
- HERMES_BUSINESS_MODEL_* 作为 Core 当前模型来源；
- provider_name: hermes；
- HermesModelProviderAdapter 作为 Core 模型选择通道；
- 项目绝对路径；
- runtime 绝对路径；
- 旧的 Hermes profile 和页面服务路径。

## 安全说明

实际 .env 存在密钥相关变量，也存在外部密钥文件引用。本快照只记录变量名、来源和责任归属，没有复制任何值。

