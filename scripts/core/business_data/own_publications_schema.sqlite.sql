-- self_owned 账号 + 发布追踪 + P+ 基线(2026-07-13, 置顶规则总表核对后, 原文档
-- 第7/11/29章)。物理上跟 competitor_accounts_schema.sqlite.sql 分开一个文件,
-- 因为原文档"7 账号类型、状态与纳入"把 self_owned/benchmark 定义成同一个"账号"
-- 概念下的两种类型,但现有 competitor_accounts 表绝大多数列(D0-D7/基线模式/
-- 触发规则……)只有 benchmark 类型用得上——给 self_owned 硬套这些列不合理。
-- 两套表仍然装进同一个物理数据库文件(data/formal/production_activation.sqlite3),
-- 跟 goal01/goal02(trace_root/tactic_state)共享同一个连接,own_publications.
-- tactic_id 才能真的外键指向 trace_root,不是两个隔离的库。

-- 账号状态三态照抄原文档"账号类型、状态与纳入"里的状态表(active/paused/archived),
-- 跟 competitor_accounts 目前用的 registration_status 词汇不完全一样,故意不复用
-- 同一个 CHECK 约束字符串——self_owned 账号的"active"含义是"这个账号在正常运营、
-- 会有新发布",不是"在追踪采集中",两者语义不同,共享同一个词容易混淆。
CREATE TABLE IF NOT EXISTS own_accounts (
    account_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    handle TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, handle)
);

-- 一条真实发布事实的记录。human_confirmed_by 必填且没有默认值:这是一个真实
-- 世界事实(真人确认"这条确实发布了"),系统不能自己判定"已发布"。
-- planned_tactic_id/actually_used_tactic_id 分开存(原文档"29.2 实验记录"的
-- planned/actually_used 区分)——生产计划阶段打算用的候选方法,和最终稿实际
-- 用出来的方法,可能不是同一条,两个字段都要留痕,不能只存一个。两者都是可空
-- 外键(指向 trace_root.root_id,object_kind='tactic')——未归因时留空,不强行
-- 塞一个不准确的值。target_age_hours/actual_age_hours 原文档"11 自营账号P+
-- 时间模型"明确要求两个都存(计划的发布时点 vs 实际发布时点,用于P+序列对齐)。
CREATE TABLE IF NOT EXISTS own_publications (
    publication_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES own_accounts(account_id) ON DELETE RESTRICT,
    script_draft_id TEXT NOT NULL REFERENCES script_drafts(draft_id) ON DELETE RESTRICT,
    planned_tactic_id TEXT REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    actually_used_tactic_id TEXT REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    platform_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    target_age_hours REAL,
    actual_age_hours REAL,
    human_confirmed_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_own_publications_account
ON own_publications(account_id, published_at);

-- 按天记录真实点赞/评论/分享/收藏——形状直接照抄已经验证过的 video_checks 表,
-- 不重新设计。day_since_publish 是"发布后第几天"(原文档P+模型的P点),不是
-- competitor 那边 discovery_batch_index 的"发现后第几天"概念,自营场景只有
-- 这一种锚定方式(发布时间是真实已知的,不像对标账号那样要处理"发现延迟")。
CREATE TABLE IF NOT EXISTS own_publication_checks (
    check_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES own_publications(publication_id) ON DELETE RESTRICT,
    day_since_publish INTEGER NOT NULL,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_own_publication_checks_publication
ON own_publication_checks(publication_id, day_since_publish);

-- P基线——形状照抄已验证过的竞品 baselines 表,但账号指向 own_accounts 而不是
-- competitor_accounts。原文档"29.1 当前默认"给的真实数字:正式启用门槛/滚动
-- 窗口都是20条完整P序列;不足20条时用当前全部样本的中位数粗略展示,
-- sample_count 必须真实反映当前样本数,不能假装已经到20条(下面用一个
-- CHECK 约束防止 sample_count 被悄悄写成一个跟真实计算对不上的常量20)。
CREATE TABLE IF NOT EXISTS own_publication_baselines (
    baseline_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES own_accounts(account_id) ON DELETE RESTRICT,
    metric TEXT NOT NULL CHECK(metric IN ('like_count', 'comment_count', 'collect_count', 'share_count')),
    median_value REAL NOT NULL,
    sample_count INTEGER NOT NULL CHECK(sample_count >= 0),
    is_formally_activated INTEGER NOT NULL DEFAULT 0 CHECK(is_formally_activated IN (0, 1)),
    run_id TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_own_publication_baselines_account
ON own_publication_baselines(account_id, metric, computed_at);

-- 原文档"29.2 实验记录"+"29.3 确定性指标信号"两节合并成一张表:一次实验的
-- "发布前先冻结的规则"(primary_metric/success_rule/failure_rule/
-- evaluation_observation,原文档要求必须在看到结果前就存好,不能先看结果再
-- 定规则——所以这些字段没有默认值,调用方必须显式提供)+"最终结果"
-- (result,由 goal09_experiments.py 已经写好的 compute_metric_signal() 算出,
-- 这张表只负责存,不重新实现判断逻辑)。confounders 是 JSON 数组,原文档举例
-- 的具体项:热点/画面/投流/人物/发布时间/账号异常等。
CREATE TABLE IF NOT EXISTS own_experiments (
    experiment_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES own_publications(publication_id) ON DELETE RESTRICT,
    primary_hypothesis_tactic_id TEXT NOT NULL REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    supporting_tactic_ids TEXT NOT NULL DEFAULT '[]',
    primary_metric TEXT NOT NULL CHECK(primary_metric IN ('like_count', 'comment_count', 'collect_count', 'share_count')),
    success_rule TEXT NOT NULL,
    failure_rule TEXT NOT NULL,
    evaluation_observation TEXT NOT NULL,
    expected_metrics TEXT NOT NULL DEFAULT '[]',
    confounders TEXT NOT NULL DEFAULT '[]',
    result TEXT CHECK(result IN ('supported', 'not_supported', 'inconclusive') OR result IS NULL),
    result_computed_at TEXT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_own_experiments_publication
ON own_experiments(publication_id, created_at);
