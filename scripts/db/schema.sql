-- 创作助手 · SQLite 数据真相源
-- 设计依据 memory/target-architecture.md:
--   竞品数据侧: 采集 → 观察池 → 爆款判定(相对基线) → 爆款库
--   选题侧:    选题(候选池 = topics.status='candidate',见视图)
--   反馈侧:    稿件 → diff → 范例回写;自营 Day0-7 追踪
-- 全部 IF NOT EXISTS,init_db.py 可重复执行。

PRAGMA foreign_keys = ON;

-- ============ 自营账号 ============
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    domain        TEXT NOT NULL,                    -- 领域名,对应 config/domains/*.yaml
    platform      TEXT NOT NULL DEFAULT 'douyin',
    persona_path  TEXT,                             -- vault 人设笔记相对路径
    status        TEXT NOT NULL DEFAULT 'active',   -- active / paused
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============ 对标账号(竞品) ============
CREATE TABLE IF NOT EXISTS competitor_accounts (
    id              INTEGER PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'douyin',
    platform_uid    TEXT NOT NULL,                  -- 平台侧用户ID(抖音 sec_uid)
    name            TEXT NOT NULL,
    url             TEXT,
    domain          TEXT NOT NULL,
    follower_count  INTEGER,
    signature       TEXT,                           -- 简介
    status          TEXT NOT NULL DEFAULT 'active', -- active / paused
    last_crawled_at TEXT,                           -- 增量采集游标
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (platform, platform_uid)
);

-- ============ 竞品视频存储(全部竞品视频;观察池是其 watching 子集,见视图) ============
-- 状态流: 注册存量(已定型) → archived(基线材料,判定一次,爆款→hits)
--        新发布(≤观察窗口)  → watching(7天定期复查) → promoted(升爆款库) / archived(凉了归档)
CREATE TABLE IF NOT EXISTS competitor_videos (
    id               INTEGER PRIMARY KEY,
    competitor_id    INTEGER NOT NULL REFERENCES competitor_accounts(id),
    platform_item_id TEXT NOT NULL,                 -- 抖音 aweme_id
    title            TEXT,
    url              TEXT,
    publish_time     TEXT,
    duration_sec     INTEGER,
    like_count       INTEGER,
    comment_count    INTEGER,
    share_count      INTEGER,
    collect_count    INTEGER,
    first_seen_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_checked_at  TEXT,
    check_count      INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'watching', -- watching / promoted / archived
    raw_json         TEXT,                          -- 采集原始字段,留底
    UNIQUE (competitor_id, platform_item_id)
);
CREATE INDEX IF NOT EXISTS idx_vid_status ON competitor_videos(status, last_checked_at);

-- 观察池 = 仅"观察中的新视频"(日常发布≤窗口,等7天定生死)
CREATE VIEW IF NOT EXISTS observation_pool AS
    SELECT * FROM competitor_videos WHERE status = 'watching';

-- ============ 观察期复查快照(生长曲线原料;每视频≤窗口天数行,封顶) ============
-- 基线指标 = 视频出观察期时的点赞数(daily 采集免费产生→基线自保鲜,无需全量复刷存量)。
-- 快照攒够后可做早期预警(第N天异常陡→插队逆向);现在只捕获不建模。
CREATE TABLE IF NOT EXISTS video_checks (
    id            INTEGER PRIMARY KEY,
    video_id      INTEGER NOT NULL REFERENCES competitor_videos(id),
    checked_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    age_hours     REAL,              -- 距发布小时数(生长曲线的x轴)
    like_count    INTEGER,
    comment_count INTEGER,
    share_count   INTEGER,
    collect_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_checks_video ON video_checks(video_id, checked_at);

-- ============ 基线:对标账号滚动基线(数据自算,非拍脑袋) ============
CREATE TABLE IF NOT EXISTS baselines (
    id            INTEGER PRIMARY KEY,
    competitor_id INTEGER NOT NULL REFERENCES competitor_accounts(id),
    metric        TEXT NOT NULL DEFAULT 'like_count',
    window_days   INTEGER NOT NULL,
    sample_count  INTEGER NOT NULL,
    median_value  REAL,
    p90_value     REAL,
    computed_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_baseline_comp ON baselines(competitor_id, metric, computed_at);

-- ============ 爆款库:已验证赢家(一库两层:此处浅层元数据;深逆向产物在 vault) ============
CREATE TABLE IF NOT EXISTS hits (
    id                  INTEGER PRIMARY KEY,
    observation_id      INTEGER REFERENCES observation_pool(id),
    competitor_id       INTEGER NOT NULL REFERENCES competitor_accounts(id),
    platform_item_id    TEXT NOT NULL,
    title               TEXT,
    url                 TEXT,
    publish_time        TEXT,
    duration_sec        INTEGER,
    like_count          INTEGER,
    comment_count       INTEGER,
    share_count         INTEGER,
    collect_count       INTEGER,
    excess_ratio        REAL,                       -- 相对基线超额倍数 = 强度
    share_comment_ratio REAL,                       -- 转发/评论(转发>评论是金标准)
    tags                TEXT,                       -- JSON 数组,用 vault/词表/分类词表.md 的标签
    promoted_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    -- 深逆向(独立后台轨,本地 SenseVoice ASR,无额度约束)
    reverse_status      TEXT NOT NULL DEFAULT 'none', -- none / queued / extracting / done / skipped
    reverse_priority    REAL,                       -- 强度 × 新意,队列排序用
    transcript_path     TEXT,                       -- 本地 ASR 转写的口播文案文件
    dna_note_path       TEXT,                       -- data/爆款拆解 笔记(中间燃料)
    UNIQUE (competitor_id, platform_item_id)
);
CREATE INDEX IF NOT EXISTS idx_hits_reverse ON hits(reverse_status, reverse_priority);

-- ============ 选题(候选池 = status='candidate',见下方视图) ============
CREATE TABLE IF NOT EXISTS topics (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    angle          TEXT,                            -- 切入角度
    domain         TEXT NOT NULL,
    source_type    TEXT NOT NULL,                   -- hit / trend / candidate_revive / manual / original / spinoff
    source_hit_id  INTEGER REFERENCES hits(id),
    derive_source  TEXT,                            -- 衍生来源类别: 拆解·可裂变选题 / 研究·争议 / 研究·信息缺口
    derive_detail  TEXT,                            -- 衍生来源原文或评论依据
    tags           TEXT,                            -- JSON 数组(回温匹配用)
    initial_heat   REAL,
    current_heat   REAL,                            -- f(初始热度, 回温↑, 衰减↓),脚本定期重算
    reheat_count   INTEGER NOT NULL DEFAULT 0,
    last_reheat_at TEXT,
    status         TEXT NOT NULL DEFAULT 'pushed',  -- pushed / candidate / selected / rejected / expired
    pushed_at      TEXT,
    selected_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status, current_heat);

-- 候选池:每日推送的常驻分块(推了没选中的,回温/衰减由 current_heat 体现)
CREATE VIEW IF NOT EXISTS candidate_pool AS
    SELECT * FROM topics WHERE status = 'candidate' ORDER BY current_heat DESC;

-- ============ 稿件(文件存 data/drafts,这里登记版本链) ============
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY,
    topic_id     INTEGER NOT NULL REFERENCES topics(id),
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    version      INTEGER NOT NULL DEFAULT 1,
    stage        TEXT,                              -- outline / hook / full / final
    author       TEXT NOT NULL DEFAULT 'ai',        -- ai / human
    content_path TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',     -- draft / approved / published
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_drafts_topic ON drafts(topic_id, version);

-- ============ 改稿 diff(AI版 vs 人改版;短周期反馈源) ============
CREATE TABLE IF NOT EXISTS diffs (
    id                 INTEGER PRIMARY KEY,
    ai_draft_id        INTEGER NOT NULL REFERENCES drafts(id),
    human_draft_id     INTEGER NOT NULL REFERENCES drafts(id),
    diff_path          TEXT,                        -- diff 文件
    changed_chars      INTEGER,
    total_chars        INTEGER,
    edit_ratio         REAL,                        -- 改动量,随系统学会下降 = 升自主度指标
    feedback_processed INTEGER NOT NULL DEFAULT 0,  -- 0=未回写范例 1=已回写
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============ 自营追踪 Day0-7(长周期反馈源,等账号发布后启用) ============
CREATE TABLE IF NOT EXISTS tracking (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    draft_id         INTEGER REFERENCES drafts(id),
    platform_item_id TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    day_offset       INTEGER NOT NULL,              -- 0-7
    play_count       INTEGER,
    like_count       INTEGER,
    comment_count    INTEGER,
    share_count      INTEGER,
    collect_count    INTEGER,
    finish_rate      REAL,                          -- 完播率(自营后台才有)
    like_rate        REAL,
    share_rate       REAL,
    captured_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (platform_item_id, day_offset)
);

-- ============ 爆款评论(逆向备料·web 可查;代码过滤噪音后入库,非全量) ============
-- 抓取:fetch_comments.py 走 MediaCrawler detail+get_comment;入库前过 comment_filter(去广告/灌水/复读)。
CREATE TABLE IF NOT EXISTS hit_comments (
    id                INTEGER PRIMARY KEY,
    hit_id            INTEGER NOT NULL REFERENCES hits(id),
    comment_id        TEXT,
    content           TEXT,
    like_count        INTEGER,
    sub_comment_count INTEGER,
    ip_location       TEXT,
    comment_time      TEXT,
    parent_comment_id TEXT,
    collected_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (hit_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_hitcmt_hit ON hit_comments(hit_id, like_count);

-- ============ 音乐领域专用资料源:歌曲评论(先接网易云) ============
-- 边界:评论采集/过滤/入库是通用能力;网易云这条实现是音乐领域专用入口。
-- 主用途仍是网友语感/写作经验燃料;额外为音乐内容创作沉淀歌曲、歌手、专辑、听众故事。
CREATE TABLE IF NOT EXISTS music_tracks (
    id                INTEGER PRIMARY KEY,
    platform          TEXT NOT NULL DEFAULT 'netease',
    platform_track_id TEXT NOT NULL,
    name              TEXT NOT NULL,
    artist_names      TEXT,                         -- JSON 数组
    album_name        TEXT,
    duration_ms       INTEGER,
    url               TEXT,
    raw_json          TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_collected_at TEXT,
    UNIQUE (platform, platform_track_id)
);
CREATE INDEX IF NOT EXISTS idx_music_tracks_name ON music_tracks(platform, name);

CREATE TABLE IF NOT EXISTS music_comment_batches (
    id              INTEGER PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES music_tracks(id),
    platform        TEXT NOT NULL DEFAULT 'netease',
    source_query    TEXT,
    requested_limit INTEGER,
    fetched_count   INTEGER,
    kept_count      INTEGER,
    total_comments  INTEGER,
    status          TEXT NOT NULL DEFAULT 'done',   -- done / failed
    error           TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_music_batches_track ON music_comment_batches(track_id, collected_at);

CREATE TABLE IF NOT EXISTS music_comments (
    id                  INTEGER PRIMARY KEY,
    track_id            INTEGER NOT NULL REFERENCES music_tracks(id),
    platform            TEXT NOT NULL DEFAULT 'netease',
    platform_comment_id TEXT,
    content             TEXT NOT NULL,
    like_count          INTEGER,
    reply_count         INTEGER,
    user_id             TEXT,
    user_nickname       TEXT,
    comment_time        TEXT,
    comment_type        TEXT NOT NULL DEFAULT 'normal', -- hot / normal
    raw_json            TEXT,
    collected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (track_id, platform_comment_id)
);
CREATE INDEX IF NOT EXISTS idx_music_comments_track ON music_comments(track_id, like_count);

-- ============ 语感燃料通用原料(豆瓣长评/知乎回答/MediaCrawler 平台等) ============
-- 网易云保留 music_* 专表,其它平台默认进这里。原料层不等于范例,晋升前不得进 vault/范例。
CREATE TABLE IF NOT EXISTS language_fuel_items (
    id               INTEGER PRIMARY KEY,
    platform         TEXT NOT NULL,
    channel          TEXT,
    platform_item_id TEXT NOT NULL,
    source_kind      TEXT NOT NULL,                 -- short_comment / long_review / article_or_answer / post
    domain           TEXT NOT NULL,
    title            TEXT,
    subtitle         TEXT,
    url              TEXT,
    source_url       TEXT,
    author           TEXT,
    publish_time     TEXT,
    like_count       INTEGER,
    comment_count    INTEGER,
    content          TEXT NOT NULL,
    content_status   TEXT NOT NULL DEFAULT 'partial', -- full / partial / blocked_or_pending
    word_count       INTEGER,
    content_hash     TEXT,
    quality_status   TEXT NOT NULL DEFAULT 'raw',
    privacy_status   TEXT NOT NULL DEFAULT 'clean',
    raw_json         TEXT,
    collected_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (platform, platform_item_id)
);
CREATE INDEX IF NOT EXISTS idx_lfuel_platform ON language_fuel_items(platform, source_kind, collected_at);
CREATE INDEX IF NOT EXISTS idx_lfuel_channel ON language_fuel_items(platform, channel, source_kind, collected_at);

CREATE TABLE IF NOT EXISTS language_fuel_batches (
    id              INTEGER PRIMARY KEY,
    platform        TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    domain          TEXT NOT NULL,
    source_query    TEXT,
    requested_limit INTEGER,
    fetched_count   INTEGER,
    kept_count      INTEGER,
    status          TEXT NOT NULL DEFAULT 'done',
    error           TEXT,
    obsidian_note   TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============ 语感燃料提炼结果(LLM 生成节点; 从原始语料/爆款评论提炼出的可检索经验) ============
-- 边界:
-- 1) 原始采集仍在 hit_comments / music_comments / language_fuel_items 等表。
-- 2) 本表存“已提炼、已分类、可被创作材料包检索”的经验, 但不等于 vault/范例 的正式写手范例。
-- 3) 创作时按 domain + query + strength 少量取用, 形成闭环: 评论/长评 -> 提炼 -> 检索注入 -> 创作。
-- ============ real comment corpus (shared raw-material layer) ============
-- Stores short human reactions from Douban comments/replies, Zhihu comments,
-- hit comments, Netease comments, and future platforms. It is a unified
-- retrieval layer, not a replacement for platform-specific raw tables.
CREATE TABLE IF NOT EXISTS language_fuel_comments (
    id                  INTEGER PRIMARY KEY,
    platform            TEXT NOT NULL,
    channel             TEXT,
    domain              TEXT NOT NULL DEFAULT 'general',
    source_table        TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    parent_source_id    TEXT,
    parent_platform_id  TEXT,
    platform_comment_id TEXT,
    source_kind         TEXT NOT NULL DEFAULT 'comment',
    source_url          TEXT,
    author              TEXT,
    user_id             TEXT,
    publish_time        TEXT,
    like_count          INTEGER,
    reply_count         INTEGER,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    quality_status      TEXT NOT NULL DEFAULT 'raw',
    privacy_status      TEXT NOT NULL DEFAULT 'clean',
    raw_json            TEXT,
    collected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (platform, source_table, source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_lfuel_comments_lookup
ON language_fuel_comments(platform, domain, source_kind, like_count, collected_at);
CREATE INDEX IF NOT EXISTS idx_lfuel_comments_source
ON language_fuel_comments(source_table, source_id);

-- ============ content atoms (traceable reusable material units) ============
-- LLM may propose atoms, but stored atoms must point back to real source text.
-- Creation should retrieve a small set by relevance/strength, never full dumps.
CREATE TABLE IF NOT EXISTS content_atoms (
    id              INTEGER PRIMARY KEY,
    atom_type       TEXT NOT NULL,
    domain          TEXT NOT NULL DEFAULT 'general',
    title           TEXT,
    atom_text       TEXT NOT NULL,
    evidence_text   TEXT NOT NULL,
    source_table    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_url      TEXT,
    platform        TEXT,
    source_kind     TEXT,
    topic_tags      TEXT,
    strength        INTEGER NOT NULL DEFAULT 3,
    reliability     INTEGER NOT NULL DEFAULT 3,
    status          TEXT NOT NULL DEFAULT 'candidate',
    notes           TEXT,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (source_table, source_id, atom_type, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_content_atoms_retrieve
ON content_atoms(domain, atom_type, status, strength, reliability, created_at);
CREATE INDEX IF NOT EXISTS idx_content_atoms_source
ON content_atoms(source_table, source_id);

CREATE TABLE IF NOT EXISTS language_fuel_insights (
    id               INTEGER PRIMARY KEY,
    source_type      TEXT NOT NULL,                 -- hit_comments / music_comments / douban_reviews / zhihu_answers
    source_id        TEXT NOT NULL,                 -- hit_id / track_id / batch_id / subject_id
    platform         TEXT NOT NULL,
    domain           TEXT NOT NULL,
    category         TEXT NOT NULL,                 -- meme / proverb / rhyme / punchline / oral / hot_word / emotion / material
    use_stage        TEXT NOT NULL DEFAULT 'style', -- topic / hook / style / material / title / review
    title            TEXT,
    pattern          TEXT NOT NULL,                 -- 提炼出的语感打法/表达模式
    examples_json    TEXT NOT NULL,                 -- 原句例子 JSON 数组
    advice           TEXT,                          -- 怎么用, 不是禁令
    tags             TEXT,                          -- JSON 数组
    strength         INTEGER NOT NULL DEFAULT 3,    -- 1-5; 来自源爆款强度/评论互动/提炼质量
    source_count     INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'active', -- active / archived
    obsidian_note    TEXT,
    content_hash     TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (source_type, source_id, category, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_lfuel_insights_domain
ON language_fuel_insights(domain, use_stage, category, strength, created_at);

-- ============ human language texture observations (validation layer) ============
-- This table is intentionally separate from language_fuel_insights.
-- It stores context-preserving observations and cross-context candidates for
-- validating whether real comments can yield a non-template "human language
-- texture" base. It must not be retrieved by the normal writing flow until a
-- candidate is reviewed and promoted deliberately.
CREATE TABLE IF NOT EXISTS language_texture_observations (
    id                   INTEGER PRIMARY KEY,
    scope                TEXT NOT NULL,                 -- local_context / cross_context
    source_type          TEXT NOT NULL DEFAULT 'hit_comments',
    source_id            TEXT NOT NULL,                 -- hit id, or merge batch id
    platform             TEXT NOT NULL DEFAULT 'douyin',
    domain               TEXT NOT NULL DEFAULT 'general',
    title                TEXT,
    expression_tendency  TEXT NOT NULL,                 -- observation, not a formula
    context_conditions   TEXT NOT NULL,                 -- why it works in context
    not_for              TEXT,                          -- unsuitable contexts
    failure_boundary     TEXT NOT NULL,                 -- how it turns oily/template-like
    mechanical_misuse    TEXT NOT NULL,                 -- failed counterexample description
    diagnostic_question  TEXT,                          -- used for AI-flavor diagnosis
    only_local_signals   TEXT,                          -- why a local item may not generalize
    source_group_ids     TEXT NOT NULL DEFAULT '[]',    -- JSON hit ids supporting this item
    evidence_json        TEXT NOT NULL DEFAULT '{}',    -- comment indices / source observations
    notes_json           TEXT NOT NULL DEFAULT '{}',    -- group summaries, merge rationale
    strength             INTEGER NOT NULL DEFAULT 3,
    status               TEXT NOT NULL DEFAULT 'candidate', -- candidate / local_only / promoted / archived
    content_hash         TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (scope, source_type, source_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_texture_obs_lookup
ON language_texture_observations(domain, scope, status, strength, created_at);
CREATE INDEX IF NOT EXISTS idx_texture_obs_source
ON language_texture_observations(source_type, source_id);
