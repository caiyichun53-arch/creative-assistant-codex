-- 领域话题标签库、轮换调度和搜索发现的外部视频。物理上与正式追踪账号表分开，
-- 防止没有正式 D 基线的搜索来源被混入 hits 或直接作为经验升级证据。

-- 标签本身。初始的活跃标签来自每个领域自己的 sources.yaml(人工配置,见
-- run_domain_search.py 的 load_sources_yaml())——这些标签本来就是人工选定放进
-- 配置文件的,human_review_status 在插入时直接记 approved,不需要再走一遍队列。
-- 爆款库里冒出来的新标签只能进 status='suggested' + human_review_status=
-- 'pending_review',要人工在 review_queue.py 里 --approve tag 才转正——复用已经
-- 验证过的 topic/plan/draft/review/final 五段同一套 human_review_status 机制,
-- 不是另起一套。下面的触发器只做一件机械的事:human_review_status 变成 approved
-- 且当前 status 还是 suggested 时,才把 status 也同步改成 active(sources_yaml
-- 来源的标签一开始就是 active,这个触发器对它们不起作用,不会误判)。
-- consecutive_cycles_without_validated_topic 是连续无有效选题周期的可追溯计数器，
-- 每轮搜索后由调用方
-- 增减,不是数据库触发器算的(需要真的知道"这轮搜出来的视频有没有变成正式选题"
-- 这种跨表业务判断,数据库层做不了)。
CREATE TABLE IF NOT EXISTS domain_search_tags (
    tag_id TEXT PRIMARY KEY,
    tag TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suggested', 'pending_review', 'paused', 'closed')),
    source TEXT NOT NULL CHECK(source IN ('sources_yaml', 'discovered')),
    source_video_id TEXT,
    human_review_status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK(human_review_status IN ('pending_review', 'approved', 'rejected')),
    reviewed_at TEXT,
    reviewed_note TEXT,
    consecutive_cycles_without_validated_topic INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_label, tag)
);

CREATE TRIGGER IF NOT EXISTS domain_search_tags_promote_on_approval
AFTER UPDATE OF human_review_status ON domain_search_tags
WHEN NEW.human_review_status = 'approved' AND OLD.status = 'suggested'
BEGIN
    UPDATE domain_search_tags SET status = 'active' WHERE tag_id = NEW.tag_id;
END;

CREATE INDEX IF NOT EXISTS idx_domain_search_tags_domain_status
ON domain_search_tags(domain_label, status);

-- 只有这里存在一条当前有效、可追溯的精确登记时，短期平台活动标签才会被
-- 确定性排除。词面关键词不写入此表，也不能自行构成排除证据。
CREATE TABLE IF NOT EXISTS domain_search_activity_tag_registry (
    registry_id TEXT PRIMARY KEY,
    domain_label TEXT NOT NULL,
    tag TEXT NOT NULL,
    platform TEXT NOT NULL,
    activity_identity TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    exclusion_reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain_label, tag, platform, activity_identity)
);

CREATE INDEX IF NOT EXISTS idx_domain_search_activity_tag_lookup
ON domain_search_activity_tag_registry(domain_label, tag, platform, status, valid_from, valid_until);

-- 每日轮换调度游标。last_searched_at IS NULL 表示从没搜过,永远排在最前面。
-- Runtime 每个领域每天最多选择三个最久未搜索的活跃标签；不足三个不补位。
-- 跟标签表分开
-- 一张表(不是在 domain_search_tags 上直接加一列),因为"轮换到了没有"和"标签本身
-- 的状态"是两件独立的事——一个 suggested_pause 的标签理论上仍然可能被游标记录过
-- 历史搜索时间,拆开存不会因为标签状态变化而丢失轮换历史。
CREATE TABLE IF NOT EXISTS domain_search_cursor (
    tag_id TEXT PRIMARY KEY REFERENCES domain_search_tags(tag_id) ON DELETE RESTRICT,
    last_searched_at TEXT,
    run_id TEXT
);

-- 关键词搜索发现的外部视频——独立于 competitor_videos,structurally 防止跟正式
-- 追踪账号的证据混用(见文件顶部说明)。is_tracked_account 记录发现时这个视频
-- 所属账号是否已经在 competitor_accounts 里——已追踪的账号直接跳过(A3 的前置
-- 过滤,这个账号的新视频已经由每日增量抓取覆盖,搜索没必要重复发现);未追踪账号
-- 的视频保留在这张表里,供 A5(账号发现复查)统计"同一账号30天内至少3条不同视频"
-- 时使用。
CREATE TABLE IF NOT EXISTS discovered_external_videos (
    discovered_video_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    account_handle TEXT,
    account_platform_id TEXT,
    is_tracked_account INTEGER NOT NULL DEFAULT 0 CHECK(is_tracked_account IN (0, 1)),
    tag_id TEXT NOT NULL REFERENCES domain_search_tags(tag_id) ON DELETE RESTRICT,
    domain_label TEXT NOT NULL,
    title TEXT,
    url TEXT,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    search_position INTEGER,
    raw_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT NOT NULL,
    UNIQUE(platform, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_discovered_external_videos_account
ON discovered_external_videos(account_platform_id, is_tracked_account, discovered_at);

CREATE INDEX IF NOT EXISTS idx_discovered_external_videos_tag
ON discovered_external_videos(tag_id, discovered_at);

-- 每个标签第 1 页的原始搜索观察。页面返回多少就逐条留多少，包含被确定性
-- 过滤的记录；eligible 记录才会同时进入 discovered_external_videos。
CREATE TABLE IF NOT EXISTS domain_search_page_observation (
    observation_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_item_id TEXT,
    tag_id TEXT NOT NULL REFERENCES domain_search_tags(tag_id) ON DELETE RESTRICT,
    domain_label TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK(page_number = 1),
    search_position INTEGER NOT NULL,
    title TEXT,
    url TEXT,
    filter_outcome TEXT NOT NULL CHECK(filter_outcome IN ('eligible', 'excluded')),
    filter_reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT NOT NULL,
    UNIQUE(run_id, tag_id, search_position)
);

CREATE INDEX IF NOT EXISTS idx_domain_search_page_observation_run
ON domain_search_page_observation(run_id, domain_label, tag_id, search_position);

-- 标签搜索发现账号的复查任务。
-- 一条账号只建一条复查任务(即使后续又通过筛选,也不会重复触发第二条——见
-- run_account_discovery.py 的 find_accounts_due_for_review() 排除已有记录的
-- 逻辑),直到这条任务被处理完(disposition 从 pending 变成别的)。
-- disposition 使用加入/忽略30天/永久忽略三种结果，不能套用通过/拒绝二元机制，
-- 这里用自己的 resolve_account_review() 函数处理,不勉强复用。
-- video_count 是触发时真实符合条件的视频数(>=3),不是"最近10条"的固定数字——
-- 这里只使用已经发现、已经落库的真实视频做统计，不伪造额外账号主页抓取。
CREATE TABLE IF NOT EXISTS discovered_account_review (
    review_id TEXT PRIMARY KEY,
    account_platform_id TEXT NOT NULL,
    account_handle TEXT,
    domain_label TEXT NOT NULL,
    video_count INTEGER NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    disposition TEXT NOT NULL DEFAULT 'pending'
        CHECK(disposition IN ('pending', 'added', 'ignored_30d', 'ignored_permanently')),
    ignored_until TEXT,
    resolved_at TEXT,
    resolved_note TEXT,
    run_id TEXT NOT NULL,
    UNIQUE(account_platform_id, domain_label)
);

CREATE INDEX IF NOT EXISTS idx_discovered_account_review_disposition
ON discovered_account_review(disposition, domain_label);

-- 人工热点登记仍作为独立来源保留。TrendRadar 的每日原始观察写入下方独立表，
-- 先做领域匹配和热点转化，不直接伪装成人工热点或候选。
CREATE TABLE IF NOT EXISTS hotspot_events (
    event_id TEXT PRIMARY KEY,
    domain_label TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'feishu_manual' CHECK(source IN ('feishu_manual')),
    matched_tags TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hotspot_events_domain
ON hotspot_events(domain_label, created_at);

CREATE TABLE IF NOT EXISTS trendradar_collection_run (
    collection_run_id TEXT PRIMARY KEY,
    discovery_run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'completed_with_failures', 'failed', 'timed_out', 'interrupted')),
    item_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    command_hash TEXT NOT NULL,
    raw_archive_ref TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS trendradar_hotspot_observation (
    observation_id TEXT PRIMARY KEY,
    provider_item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    source_rank INTEGER,
    observed_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    collection_run_id TEXT NOT NULL REFERENCES trendradar_collection_run(collection_run_id) ON DELETE RESTRICT,
    UNIQUE(collection_run_id, provider_item_id)
);

CREATE INDEX IF NOT EXISTS idx_trendradar_hotspot_observed
ON trendradar_hotspot_observation(observed_at, provider_item_id);
