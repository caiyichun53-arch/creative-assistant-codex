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

-- BR-HIT-001 section B (2026-07-07 master-doc realignment): every video is
-- classified at first contact into exactly one of three categories, per the
-- document's "历史回填与三类视频". This replaces the prior watching/archived/
-- promoted status machine entirely -- there is no retraction/graduation
-- lifecycle anymore (BR-HIT-005: once a video first formally triggers, its
-- candidate fields are permanent).
--   historical_mature: already published >=7 days when first seen. One
--     cumulative snapshot now. Feeds mature_history baseline only.
--   transition: published 1-7 days when first seen. One snapshot now, a
--     second "matured" snapshot once it turns exactly 7 days published. No
--     D-series. Feeds mature_history baseline only, once matured.
--   formal_new: first discovered via the account's ongoing daily batch (not
--     at first registration). D0 at discovery, D1-D7 on the next 7 discovery
--     batches. Feeds the formal D baseline once the full D0-D7 set is
--     complete AND discovery_delay_hours <= 36.
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
    -- NULL only when publish_time was unusable at ingest (excluded_reason=
    -- 'missing_publish_time') -- a video that genuinely cannot be classified.
    first_contact_category TEXT
        CHECK(first_contact_category IN ('historical_mature', 'transition', 'formal_new') OR first_contact_category IS NULL),
    -- publish_time -> first_seen_at gap in hours. A formal_new video only ever
    -- becomes a formal_d_series baseline member if this is <= 36 (BR-HIT-001
    -- section A). historical_mature/transition videos are not gated by this --
    -- they never join the formal D baseline regardless of discovery delay.
    discovery_delay_hours REAL,
    -- transition-only: set once the second "matured" snapshot (at day 7) has
    -- been recorded. NULL means still waiting on that second snapshot (or not
    -- a transition-category video at all).
    mature_snapshot_taken_at TEXT,
    -- formal_new-only: true once D0..D7 have all been recorded in video_checks.
    tracking_completed INTEGER NOT NULL DEFAULT 0 CHECK(tracking_completed IN (0, 1)),
    -- BR-HIT-005: candidate record fields. Permanent once first written --
    -- never cleared or overwritten backwards, even if later data would no
    -- longer clear the bar (verbatim document rule: "首次正式触发后保留记录,
    -- 即使后续倍数回落也不删除").
    first_trigger_observation TEXT,
    first_trigger_at TEXT,
    -- JSON array of every rule name that has ever fired for this video across
    -- all observation points (cumulative, append-only -- not just the first).
    trigger_rules TEXT,
    peak_observation TEXT,
    baseline_mode TEXT
        CHECK(baseline_mode IN ('mature_history', 'formal_d_series') OR baseline_mode IS NULL),
    judgment_confidence TEXT
        CHECK(judgment_confidence IN ('rough', 'formal') OR judgment_confidence IS NULL),
    excluded_reason TEXT,
    registration_run_id TEXT,
    raw_archive_ref TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    check_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_competitor_videos_account_category
ON competitor_videos(account_id, first_contact_category, publish_time);

-- One append-only row per observation of a video. discovery_batch_index is
-- the D-point (0..7) for formal_new videos, discovery-anchored (D0 = the
-- batch that first discovered the video, not a calendar-day count since
-- publish); NULL for historical_mature/transition snapshots. day_since_publish
-- is the OPPOSITE anchoring -- calendar days since publish_time, deliberately
-- a separate column so the two are never conflated: it is populated for
-- historical_mature/transition checks (2026-07-08: transition videos are now
-- refreshed daily while observing, not just once at day 7, so their 0-7
-- lifecycle needs its own explicit day label), and left NULL for formal_new
-- rows, which already have discovery_batch_index for that purpose.
CREATE TABLE IF NOT EXISTS video_checks (
    check_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES competitor_videos(video_id) ON DELETE RESTRICT,
    discovery_batch_index INTEGER,
    day_since_publish INTEGER,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_video_checks_video
ON video_checks(video_id, discovery_batch_index, checked_at);

-- BR-HIT-001 section C: four baseline types share this table, distinguished
-- by baseline_mode. mature_history has observation_point=NULL (one
-- account-wide value per metric); formal_d_series has one row per D-point
-- per metric (observation_point='D0'..'D7').
CREATE TABLE IF NOT EXISTS baselines (
    baseline_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES competitor_accounts(account_id) ON DELETE RESTRICT,
    baseline_mode TEXT NOT NULL CHECK(baseline_mode IN ('mature_history', 'formal_d_series')),
    metric TEXT NOT NULL CHECK(metric IN ('like_count', 'comment_count', 'collect_count', 'share_count')),
    observation_point TEXT,
    sample_count INTEGER NOT NULL,
    median_value REAL NOT NULL,
    run_id TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_baselines_account
ON baselines(account_id, baseline_mode, metric, observation_point, computed_at);

-- hit_channel is a comma-joined list of every channel that fired at
-- promotion time (like_anomaly/comment_anomaly/collect_anomaly/share_anomaly/
-- multi_indicator/comment_like_ratio/cold_start_d7_rough) -- not a fixed
-- small enum, since BR-HIT-001's 6-channel OR design allows many combinations
-- and BR-HIT-005 requires the video's own trigger_rules to be the cumulative
-- record. This row records the FIRST promotion only; once inserted it is
-- never deleted or reverted (BR-HIT-005 permanence) -- later judgement
-- passes may only add to competitor_videos.trigger_rules, not touch this row.
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
    hit_channel TEXT NOT NULL,
    judgment_confidence TEXT NOT NULL CHECK(judgment_confidence IN ('rough', 'formal')),
    -- Informational only, no FK: this references a synthetic per-judgement
    -- composite id (one judgement pass may write several per-metric baselines
    -- rows), not a single literal baselines.baseline_id row.
    baseline_id TEXT,
    run_id TEXT NOT NULL,
    -- Source document section 13: "处理状态只保留 pending、running、completed、
    -- failed" -- this is the reverse-prep (transcript+comment) queue status,
    -- not a business judgement field. No CHECK constraint (would need a full
    -- table rebuild on an existing production table); enforced in Python by
    -- run_reverse_prep.py, which is the only writer.
    reverse_status TEXT NOT NULL DEFAULT 'pending',
    promoted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_hits_account
ON hits(account_id, promoted_at);

-- Reverse-prep transcript output (BR-ASR-001/BR-ASR-002), matching source
-- document section 13's raw/cleaned versioning: APPEND-ONLY, one row per
-- attempt, never overwritten ("转写版本不可覆盖") -- a hit_id can have several
-- rows (retries), version increasing; the latest completed row is the current
-- one. raw_transcript_text is the verbatim local SenseVoice output;
-- cleaned_transcript_text is a deterministic (no LLM) whitespace/duplicate-
-- line cleanup pass over it -- section 13.1's "cleaned_transcript v1" tier.
-- Provenance fields (asr_model/vad_model/audio_sha256/processing_method) are
-- section 13's explicit traceability requirement. Never stores the signed
-- MediaCrawler download URL (BR-ASR-002 forbidden_behavior) -- only a hash of
-- the downloaded audio bytes.
CREATE TABLE IF NOT EXISTS hit_transcripts (
    transcript_id TEXT PRIMARY KEY,
    hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    raw_transcript_text TEXT NOT NULL,
    cleaned_transcript_text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    asr_model TEXT NOT NULL,
    vad_model TEXT NOT NULL,
    processing_method TEXT NOT NULL DEFAULT 'local_sensevoice_funasr',
    audio_sha256 TEXT NOT NULL,
    -- Deterministic (no LLM) anomaly checks section 13.1 requires: comma-
    -- joined subset of empty/too_short/high_repetition.
    quality_flags TEXT NOT NULL DEFAULT '',
    -- Per-attempt lifecycle, same 4-value vocabulary as hits.reverse_status
    -- (source document section 13).
    processing_status TEXT NOT NULL CHECK(processing_status IN ('pending', 'running', 'completed', 'failed')),
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hit_transcripts_hit
ON hit_transcripts(hit_id, version);

-- One row per top-level comment fetched alongside the same MediaCrawler
-- detail call that resolved the download URL (source document section 16 /
-- CLAUDE.md: "合并成一趟 detail 爬 -- 下载链接+评论一次拿"), not a separate
-- comment-collection pass. Reply/second-level comments are excluded at the
-- crawler level (LocalMediaCrawlerExecutor always passes --get_sub_comment
-- no), matching section 16's "第一阶段关闭二级评论". sample_rank preserves
-- the crawler's own hotness ordering (section 16: "保留采集器热度顺序和
-- sample_rank"), assigned before any filtering/dedup so gaps in the sequence
-- are visible where a comment was dropped. INSERT OR IGNORE on retry:
-- comments are append-only evidence, a rerun should not duplicate rows
-- already captured.
CREATE TABLE IF NOT EXISTS hit_comments (
    hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT,
    comment_id TEXT NOT NULL,
    text TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    parent_comment_id TEXT,
    sample_rank INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hit_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_hit_comments_hit
ON hit_comments(hit_id, sample_rank);

-- Output of runtime_skills/sample_deep_analyze (BR-DNA-001), one already-
-- prepared hit analyzed per row. Append-only like hit_transcripts -- a re-run
-- adds a new version rather than overwriting a prior analysis. The Skill
-- itself never writes here (CR-003A: no database access from inside a
-- portable Skill) -- this is Core's Output Binding, converting the Skill's
-- public output_schema (topic_pattern/hook_pattern/structure_pattern) into a
-- business record. request_id doubles as the Skill's idempotency key
-- (SAMPLE_DEEP_ANALYZE_BUSINESS_CONTRACT.yaml: "sample_deep_analyze:{request_id}").
CREATE TABLE IF NOT EXISTS hit_deep_analysis (
    analysis_id TEXT PRIMARY KEY,
    hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    topic_pattern TEXT NOT NULL,
    hook_pattern TEXT NOT NULL,
    structure_pattern TEXT NOT NULL,
    model_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hit_id, version)
);

CREATE INDEX IF NOT EXISTS idx_hit_deep_analysis_hit
ON hit_deep_analysis(hit_id, version);

-- One row per source_to_topic run over a hit_deep_analysis record.
-- Append-only like hit_deep_analysis -- a re-run over the same analysis adds
-- a new version rather than overwriting a prior candidate topic. The Skill
-- itself never writes here (CR-003A) -- this is Core's Output Binding,
-- converting source_to_topic's public output_schema (topic_status/
-- candidate_topic/topic_angle/supporting_evidence/source_constraints/
-- no_result_reason/confidence) into a business record. supporting_evidence
-- and source_constraints are stored as JSON-encoded text (both are bounded
-- arrays of short strings per the Skill's own schema, not queried by value).
--
-- 2026-07-10: human_review_status is the production human-review gate --
-- every row starts 'pending_review'; content_plan's select query will not
-- pick up a topic until it is 'approved' (see review_queue.py). This is a
-- real gap found and fixed the same day the chain was first wired: nothing
-- previously stopped a candidate topic from auto-flowing all the way to a
-- script draft with no human ever looking at it.
CREATE TABLE IF NOT EXISTS topic_candidates (
    topic_id TEXT PRIMARY KEY,
    source_analysis_id TEXT NOT NULL REFERENCES hit_deep_analysis(analysis_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    topic_status TEXT NOT NULL,
    candidate_topic TEXT NOT NULL,
    topic_angle TEXT NOT NULL,
    supporting_evidence TEXT NOT NULL,
    source_constraints TEXT NOT NULL,
    no_result_reason TEXT NOT NULL,
    confidence TEXT NOT NULL,
    model_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_analysis_id, version)
);

CREATE INDEX IF NOT EXISTS idx_topic_candidates_source_analysis
ON topic_candidates(source_analysis_id, version);

-- One row per content_plan run over a topic_candidates record. Append-only
-- like topic_candidates. hooks/beats are JSON-encoded text (bounded arrays
-- of short strings per the Skill's own schema). human_review_status: see
-- topic_candidates above -- script_generate's select query requires
-- 'approved' here too.
CREATE TABLE IF NOT EXISTS content_plans (
    plan_id TEXT PRIMARY KEY,
    source_topic_id TEXT NOT NULL REFERENCES topic_candidates(topic_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    hooks TEXT NOT NULL,
    selected_hook TEXT NOT NULL,
    beats TEXT NOT NULL,
    model_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_topic_id, version)
);

CREATE INDEX IF NOT EXISTS idx_content_plans_source_topic
ON content_plans(source_topic_id, version);

-- One row per script_generate run over a content_plans record. Append-only
-- like content_plans. draft_text is the Skill's raw output_schema field
-- (50-6000 chars per script_generate's own schema). human_review_status:
-- see topic_candidates above -- this is the final gate before a draft would
-- be considered ready for any future publishing step (none exists yet).
CREATE TABLE IF NOT EXISTS script_drafts (
    draft_id TEXT PRIMARY KEY,
    source_plan_id TEXT NOT NULL REFERENCES content_plans(plan_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    model_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_plan_id, version)
);

CREATE INDEX IF NOT EXISTS idx_script_drafts_source_plan
ON script_drafts(source_plan_id, version);
