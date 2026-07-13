# Creation Assistant Codex

抖音内容创作助手的 Codex 工程。确定性任务由 Python 脚本执行；模型调用仅能通过当前原子 Skill 与显式模型路由发生。

## 本地配置

真实运行配置不入库。首次克隆后复制示例文件再填写本机值:

```powershell
Copy-Item .env.example .env
Copy-Item config/settings.example.yaml config/settings.yaml
Copy-Item config/accounts/example.yaml config/accounts/example_account.yaml
Copy-Item config/domains/example.yaml config/domains/example_domain.yaml
```

以下内容默认只保留在本机:

- `.env`: 飞书、采集登录态等凭证。
- `config/settings.yaml`: 本机模型库、语料库、Codex 命令等路径。
- `config/accounts/*.yaml`: 自营账号配置。
- `config/domains/*.yaml`: 领域配置和对标账号种子。
- `data/`, `outputs/`, `logs/`, `vault/`, `vendor/`: 数据、生成物、日志、Obsidian 库和第三方采集器。

## 当前有效入口

```powershell
python -m pytest tests/core tests/validation -q
python scripts/validation/live_gates.py dry-run
python -m scripts.validation.preflight_checkpoint_check --skip-tests
```

当前可执行链路由受控业务数据入口、原子 Skill 绑定、人工审核闸门和显式模型路由组成；未重建的外部平台集成不属于当前运行入口。

当前唯一的业务设计与实施裁决入口是
[`docs/EFFECTIVE_DESIGN_BASELINE.md`](docs/EFFECTIVE_DESIGN_BASELINE.md)。
工程协作约束见 [`AGENTS.md`](AGENTS.md)；旧 Goal、迁移计划和历史报告不构成设计依据。
