PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS competitor_accounts (
    account_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    domain_name TEXT NOT NULL,
    account_name TEXT NOT NULL,
    sec_uid TEXT NOT NULL,
    homepage_url TEXT NOT NULL,
    source_config_ref TEXT NOT NULL,
    registration_status TEXT NOT NULL DEFAULT 'active'
        CHECK(registration_status IN ('active', 'disabled')),
    first_crawl_policy TEXT NOT NULL,
    comments_policy TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, sec_uid)
);

CREATE INDEX IF NOT EXISTS idx_competitor_accounts_domain
ON competitor_accounts(domain_label, platform, registration_status);

CREATE TRIGGER IF NOT EXISTS competitor_accounts_updated_at
AFTER UPDATE ON competitor_accounts
BEGIN
    UPDATE competitor_accounts
       SET updated_at=CURRENT_TIMESTAMP
     WHERE account_id=NEW.account_id;
END;

CREATE TABLE IF NOT EXISTS competitor_videos (
    video_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES competitor_accounts(account_id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    publish_time TEXT,
    duration_sec INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1)),
    excluded_reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('watching', 'archived', 'promoted')),
    registration_run_id TEXT,
    raw_archive_ref TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    check_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_competitor_videos_account_status
ON competitor_videos(account_id, status, publish_time);

CREATE VIEW IF NOT EXISTS observation_pool AS
    SELECT * FROM competitor_videos WHERE status='watching';

-- BR-COLLECT-004 / BUILD_PLAN.md 阶段1 (2026-06-13): one append-only row per daily
-- recheck of a video, captured while it is in the observation window. Purpose is to
-- accumulate a growth curve for future modeling (day-N steepness -> early promotion) --
-- captured now, not modeled yet. Never updated in place, never deleted.
CREATE TABLE IF NOT EXISTS video_checks (
    check_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES competitor_videos(video_id) ON DELETE RESTRICT,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_video_checks_video
ON video_checks(video_id, checked_at);

CREATE TABLE IF NOT EXISTS baselines (
    baseline_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES competitor_accounts(account_id) ON DELETE RESTRICT,
    metric TEXT NOT NULL DEFAULT 'like_count',
    window_days INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    median_value REAL NOT NULL,
    p90_value REAL NOT NULL,
    threshold_value REAL NOT NULL,
    evidence_status TEXT NOT NULL DEFAULT 'sufficient' CHECK(evidence_status IN ('sufficient', 'insufficient_sample')),
    run_id TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_baselines_account
ON baselines(account_id, metric, computed_at);

CREATE TABLE IF NOT EXISTS hits (
    hit_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES competitor_videos(video_id) ON DELETE RESTRICT,
    account_id TEXT NOT NULL REFERENCES competitor_accounts(account_id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    publish_time TEXT,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    excess_ratio REAL,
    share_comment_ratio REAL,
    baseline_id TEXT NOT NULL REFERENCES baselines(baseline_id) ON DELETE RESTRICT,
    hit_channel TEXT NOT NULL DEFAULT 'like_threshold'
        CHECK(hit_channel IN ('like_threshold', 'comment_like_ratio', 'both')),
    evidence_status TEXT NOT NULL DEFAULT 'sufficient' CHECK(evidence_status IN ('sufficient', 'insufficient_sample')),
    run_id TEXT NOT NULL,
    reverse_status TEXT NOT NULL DEFAULT 'none',
    promoted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_hits_account
ON hits(account_id, promoted_at);
