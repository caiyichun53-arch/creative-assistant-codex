# scripts/core/business_data

竞品账号业务数据层:账号注册 + 首采存量 + 基线/爆款判定。独立库 `data/formal/production_activation.sqlite3`,与旧 `data/creation.db` 完全隔离(见 `_safe_db_path()`)。

## 文件

- `competitor_accounts_schema.sqlite.sql` —— 表结构:`competitor_accounts`、`competitor_videos`(带 `is_pinned`/`excluded_reason`)、`baselines`、`hits`。
- `register_competitor_accounts.py` —— 只做账号注册(从领域配置的 `competitor_seeds` 写入 `competitor_accounts`),**不采集视频、不判定**。幂等(`ON CONFLICT` upsert)。
- `run_competitor_registration_full.py` —— 真正的生产入口,两种模式:
  - **默认(全量)**:`register_from_domain()` 注册账号 → 逐账号跑 `crawl_registration_stock_once()`(经 `LocalMediaCrawlerExecutor` 调 MediaCrawler)→ `judge_domain()` 算基线/判爆款。
  - **`--rejudge-only`**:跳过注册和采集,只对已有数据重新跑 `judge_domain()`。用于"判定规则改了、数据不用重采"的场景——**这是唯一允许的重判路径**,不能用临时脚本片段直接改库。
  - **判定完自动备料**(2026-07-08,用户明确要求"爆款文案是要提取经验的,所以备料这里最后自动执行"):`run_full_registration()`/`run_rejudge_only()`/`run_daily_incremental()` 三个入口在 `judge_domain()` 提交后,都会调用 `_auto_reverse_prep()` 就地**同步**跑完 `run_reverse_prep()`,只处理**这一轮判定新产生的** `pending` 爆款(按 `hits.run_id` 限定,不会误扫到历史积压的待备料爆款),跑完才返回(报告里多一个 `reverse_prep` 字段)。判定本身仍然是纯本地计算、很快;真正变慢的是这一步(每条都要真实联网+本地跑模型),这是判定入口自己愿意等的,不是判定逻辑本身变重。
- `run_reverse_prep.py` —— 备料入口(BR-ASR-001/002、BR-COLLECT-005/006,2026-07-08 对照总控文档第13/16章补齐):对 `hits.preparation_status='pending'` 的已判定爆款,按 5 档优先级(`_priority_tier()`,受限于现有 schema,tier1/部分tier2/4 暂无对应字段——见函数 docstring)排序处理。逐条跑一趟 MediaCrawler detail 抓取(`get_comment=yes`,下载链接+评论一次拿;二级评论在执行器层已关闭),本地 SenseVoice 转写。`hit_transcripts` 是**只增不改**的版本化表(`raw_transcript_text`/`cleaned_transcript_text`各存一份,`cleaned` 只做确定性空白/连续重复句清理,不用 LLM),带 `asr_model`/`vad_model`/`audio_sha256` 溯源字段;`quality_flags`(`empty`/`too_short`/`high_repetition`,纯代码判断)非空则整条标 `processing_status='failed'`、`hits.preparation_status='failed'`。评论经确定性过滤(最短长度/去重,无 LLM)写入 `hit_comments`,带 `sample_rank` 保留抓取器原始热度顺序。签名下载链接只在内存里用,从不落库。单条失败不影响批次里其它条,`hits.preparation_status` 走 `pending → running → completed/failed`。
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
python -m scripts.core.business_data.run_reverse_prep --limit 1
```

## 测试

- `tests/core/test_competitor_account_registration.py` —— 账号注册幂等性。
- `tests/core/test_competitor_registration_full.py` —— 判定规则(30/10/窗口/置顶)、`run_full_registration()` 端到端(mock 掉真实 MediaCrawler 子进程)、`run_rejudge_only()` 及契约拒绝。
- `tests/core/test_local_mediacrawler_executor.py` —— 采集执行器本身(成功/超时/非零退出/jsonl 解析),mock 子进程,不碰真实 vendor/MediaCrawler。
- `tests/core/test_run_reverse_prep.py` —— 契约校验、`select_pending_hits`(状态过滤+5档优先级排序+按 `judgement_run_id` 限定)、`clean_transcript`(去重复句)、`detect_quality_flags`(空/过短/复读)、`filter_comments`(过滤/去重/缺 id 兜底/sample_rank)、`fetch_detail_with_comments`(同一趟抓取拿到视频+评论,二级评论关闭)、`prep_one_hit`/`run_reverse_prep` 端到端(成功/转写过短/MediaCrawler 失败/重试追加新版本不覆盖),全部 mock 掉真实网络请求、ffmpeg、本地 ASR 子进程。真实数据验证:对 4 条真实爆款完整跑通(含一次 schema 重设计前后各 2 条),过程中发现并修复了 `_local_asr_transcribe.py` 的真实 bug(库提示文字混入转写结果、非 UTF-8 编码导致中文乱码)。
- `tests/core/test_competitor_registration_full.py` 的 `AutoReversePrepTests` —— `_auto_reverse_prep()`:没有待备料爆款时不调用 `run_reverse_prep`;有待备料爆款时正确按 `judgement_run_id` 限定范围(不误扫其它轮次的积压)。`EntrypointSmokeTests` 三个端到端测试也验证了三个判定入口都会带着正确的 `judgement_run_id` 触发它(mock 掉 `run_reverse_prep` 本身,不重复测内部逻辑)。
