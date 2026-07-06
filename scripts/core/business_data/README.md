# scripts/core/business_data

竞品账号业务数据层:账号注册 + 首采存量 + 基线/爆款判定。独立库 `data/formal/production_activation.sqlite3`,与旧 `data/creation.db` 完全隔离(见 `_safe_db_path()`)。

## 文件

- `competitor_accounts_schema.sqlite.sql` —— 表结构:`competitor_accounts`、`competitor_videos`(带 `is_pinned`/`excluded_reason`)、`baselines`、`hits`。
- `register_competitor_accounts.py` —— 只做账号注册(从领域配置的 `competitor_seeds` 写入 `competitor_accounts`),**不采集视频、不判定**。幂等(`ON CONFLICT` upsert)。
- `run_competitor_registration_full.py` —— 真正的生产入口,两种模式:
  - **默认(全量)**:`register_from_domain()` 注册账号 → 逐账号跑 `crawl_registration_stock_once()`(经 `LocalMediaCrawlerExecutor` 调 MediaCrawler)→ `judge_domain()` 算基线/判爆款。
  - **`--rejudge-only`**:跳过注册和采集,只对已有数据重新跑 `judge_domain()`。用于"判定规则改了、数据不用重采"的场景——**这是唯一允许的重判路径**,不能用临时脚本片段直接改库。
- 两种模式启动时都会先跑 `validate_registration_execution_contract()`:配置(观察期=7天/窗口=90天/目标样本=30/最低可判样本=10)对不上就直接拒绝执行,不会跑出错的结果。

## 设计契约

完整规则见 `docs/production_execution_guardrails.md`。核心几条:
- 30 是目标样本数,不是硬门槛;10 才是最低可判门槛(样本<10 直接跳过该账号,标 `skipped_insufficient_sample`)。
- 90 天窗口内样本<10 才会用全部可用样本(不再局限于90天)兜底。
- 置顶、7天内、90天外的视频不进基线/爆款判定,但仍然入库(`excluded_reason` 标注)。
- 首采存量视频一律 `archived`,不是 `watching`;首采不抓评论。

## 怎么跑

```
python -m scripts.core.business_data.run_competitor_registration_full --domain-config config/domains/泛科普.yaml
python -m scripts.core.business_data.run_competitor_registration_full --rejudge-only --domain-config config/domains/泛科普.yaml
```

## 测试

- `tests/core/test_competitor_account_registration.py` —— 账号注册幂等性。
- `tests/core/test_competitor_registration_full.py` —— 判定规则(30/10/窗口/置顶)、`run_full_registration()` 端到端(mock 掉真实 MediaCrawler 子进程)、`run_rejudge_only()` 及契约拒绝。
- `tests/core/test_local_mediacrawler_executor.py` —— 采集执行器本身(成功/超时/非零退出/jsonl 解析),mock 子进程,不碰真实 vendor/MediaCrawler。
