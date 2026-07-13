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
    preparation_status TEXT NOT NULL DEFAULT 'pending',
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
    -- Per-attempt lifecycle, same 4-value vocabulary as hits.preparation_status
    -- (source document section 13).
    processing_status TEXT NOT NULL CHECK(processing_status IN ('pending', 'running', 'completed', 'failed')),
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hit_transcripts_hit
ON hit_transcripts(hit_id, version);

-- One row per top-level comment fetched alongside a MediaCrawler detail call
-- (source document section 16 / CLAUDE.md: "合并成一趟 detail 爬 -- 下载链接+
-- 评论一次拿"). Reply/second-level comments are excluded at the crawler level
-- (LocalMediaCrawlerExecutor always passes --get_sub_comment no), matching
-- section 16's "第一阶段关闭二级评论". sample_rank preserves the crawler's own
-- hotness ordering (section 16: "保留采集器热度顺序和sample_rank"), assigned
-- before any filtering/dedup so gaps in the sequence are visible where a
-- comment was dropped.
--
-- 2026-07-13 (BR-HIT-007, 置顶规则总表核对后): section 16 requires comment
-- collection to happen at up to two DIFFERENT points in a hit's lifecycle for
-- the same video -- early_topic (first candidate trigger, before D7) and
-- mature_analysis (D7/P+7d) are each a fresh, independent top-60 fetch, not
-- an update to the earlier one. purpose distinguishes which collection pass a
-- row belongs to (early_topic / mature_analysis / mature_history /
-- external_snapshot). The PRIMARY KEY includes purpose (not just
-- hit_id+comment_id) precisely so a comment that genuinely appears in both
-- the early_topic and mature_analysis passes gets two rows, not one silently
-- dropped by INSERT OR IGNORE -- losing that would misrepresent which
-- evidence was actually available at which observation point. Re-running the
-- SAME purpose's collection for a hit is still idempotent (identical
-- comment_id+purpose pairs are ignored on retry), which in practice also
-- covers BR-HIT-007's "must not create a duplicate batch" requirement without
-- a separate batch table -- a deliberate simplification, not the literal
-- (video, purpose, observation_point, sampling_strategy) unique constraint
-- the document names (that constraint additionally catches a batch retry
-- whose comment set has PARTIALLY changed since the last run; this schema
-- does not detect that case as a "duplicate batch", it just naturally
-- dedupes whatever comment_ids do overlap).
CREATE TABLE IF NOT EXISTS hit_comments (
    hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT,
    comment_id TEXT NOT NULL,
    text TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    parent_comment_id TEXT,
    sample_rank INTEGER NOT NULL,
    -- Which collection pass this row belongs to (section 16 / BR-HIT-007).
    -- NOT NULL with no default -- every caller must say which pass this is,
    -- rather than silently defaulting to one and hiding the real trigger.
    purpose TEXT NOT NULL CHECK(purpose IN ('early_topic', 'mature_analysis', 'mature_history', 'external_snapshot')),
    -- D/P point this batch was collected at (e.g. 'D0'..'D7', 'P+7d'); NULL
    -- when the purpose has no meaningful observation point (e.g. a one-off
    -- external_snapshot on a tag-searched video with no D/P baseline at all).
    observation_point TEXT,
    -- How this batch was selected -- currently only one real strategy exists
    -- (crawler's own popularity ranking, top N), kept as free text rather
    -- than an enum since new strategies are expected once purpose-tagged
    -- multi-stage collection is fully wired (see run_reverse_prep.py's
    -- module docstring for exactly what is/isn't implemented yet).
    sampling_strategy TEXT,
    run_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (hit_id, comment_id, purpose)
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
-- see topic_candidates above -- an approved draft is what run_script_review.py
-- (below) picks up next; it is not the end of the chain (2026-07-13: 置顶规则
-- 总表 requires 文案优化/审核/最终稿 after 初稿, see script_reviews/final_drafts).
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

-- One row per runtime_skills/script_review run over an approved script_drafts
-- record (BR-CONTENT-004, 2026-07-13). This Skill's own three subnodes
-- (business.creation_polish, then business.creation_review, then
-- business.ai_flavor_judge -- see formal_skill_adapter.py's
-- _run_script_review()) bundle 文案优化/审核/AI味判定 into one atomic call, so
-- this is ONE new pipeline stage, not three -- the 置顶规则总表's "文案优化在
-- 初稿之后、审核之前" ordering requirement is satisfied INSIDE this Skill call
-- (polish runs first), not by splitting it into separate database rows that
-- don't correspond to a real atomic Skill boundary. polished_text is the
-- actual optimized script; issues/revision_focus/revision_targets are JSON
-- arrays (Skill output, not further parsed here -- Core stores what the Skill
-- returned). human_review_status: same pattern as script_drafts -- an
-- approved review is what run_final_draft.py picks up next.
CREATE TABLE IF NOT EXISTS script_reviews (
    review_id TEXT PRIMARY KEY,
    source_draft_id TEXT NOT NULL REFERENCES script_drafts(draft_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'revise', 'fail')),
    issues TEXT NOT NULL,
    polished_text TEXT NOT NULL,
    revision_focus TEXT NOT NULL,
    ai_flavor_risk TEXT NOT NULL CHECK (ai_flavor_risk IN ('low', 'medium', 'high')),
    revision_targets TEXT NOT NULL,
    model_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_draft_id, version)
);

CREATE INDEX IF NOT EXISTS idx_script_reviews_source_draft
ON script_reviews(source_draft_id, version);

-- One row per "this reviewed draft is proposed as the final version" event
-- (BR-CONTENT-005, 2026-07-13). Deliberately a SEPARATE confirmation from
-- script_reviews.human_review_status='approved' -- 置顶规则总表条目5/56
-- requires "最终稿必须由用户确认" as its own explicit step, not something a
-- review approval implies for free. final_text is copied from the approved
-- review's polished_text at creation time (content does not change here;
-- this row's only real job is to carry a SEPARATE human_review_status so the
-- "this is final" confirmation is independently auditable). No further stage
-- exists after this one; publishing is manual and out of scope (see
-- CLAUDE.md/置顶规则总表条目5: "发布由用户人工完成").
CREATE TABLE IF NOT EXISTS final_drafts (
    final_draft_id TEXT PRIMARY KEY,
    source_review_id TEXT NOT NULL REFERENCES script_reviews(review_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    final_text TEXT NOT NULL,
    run_id TEXT NOT NULL,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_review_id, version)
);

CREATE INDEX IF NOT EXISTS idx_final_drafts_source_review
ON final_drafts(source_review_id, version);
