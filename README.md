# Creation Assistant Codex

抖音内容创作助手的 Codex 工程。确定性任务由 Python 脚本执行，LLM 只用于选题判断、研究综合、创作、逆向 DNA 拆解和 AI 味判断。

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
python scripts/validation/production_startup_smoke.py --require-legacy-absent
python scripts/core/staging/verify_goal_v062_phase8_readiness.py
python -m unittest tests.core.test_phase5_business_workflow tests.core.test_phase6_hermes_whitelist_tool tests.core.test_phase7_synthetic_acceptance
```

当前正式链路为: Feishu -> Hermes -> whitelist Tool -> Core API -> Job/Worker -> Input Assembly -> Workflow/Skill Dispatcher -> Portable Skill -> ModelGateway -> Materializer -> Outbox。

项目约束和构建路线见 `AGENTS.md` 与 `BUILD_PLAN.md`。
