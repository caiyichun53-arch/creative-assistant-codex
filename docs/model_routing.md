# 模型路由说明

业务设计依据为 [`EFFECTIVE_DESIGN_BASELINE.md`](EFFECTIVE_DESIGN_BASELINE.md)。本文件只说明当前配置如何被读取。

运行期模型路由唯一入口是 [`../config/model_routes.yaml`](../config/model_routes.yaml)。

- 三个模型位点：`dialogue_model`、`business_model`、`writing_model`。
- 每个位点映射到一个路由，再由 `provider_ref` 显式选择 provider。
- 当前 provider 定义为 `mimo_main`；密钥、地址和模型名从环境变量引用，不写入仓库。
- 每条路由必须使用 `fallback: none`；加载器会拒绝自动降级或隐式 provider 切换。
- 工作流节点只绑定 `route_id`，不得在节点中写入 provider、模型名或开发工具名称。

配置解析与 fail-closed 校验由 `scripts/core/model_gateway/model_router.py` 完成。
