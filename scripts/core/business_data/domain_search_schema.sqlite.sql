-- 领域话题标签库 + 轮换调度 + 搜索发现的外部视频(2026-07-13, 置顶规则总表核对后,
-- 原文档第21章"领域话题标签搜索")。物理上跟 competitor_accounts_schema.sqlite.sql
-- 分开一个文件,因为这套东西的证据地位天然跟正式追踪账号不一样(原文档21.3明确:
-- "外部视频没有该账号正式D基线,只能显示当前绝对数据...不得给出正式倍数或直接作为
-- L2/L3经验升级证据")——放进独立的表,而不是塞进 hits/competitor_videos,是结构上
-- 防止两种证据被混用的做法,不是命名习惯问题。

-- 标签本身。初始的活跃标签来自每个领域自己的 sources.yaml(人工配置,见
-- run_domain_search.py 的 load_sources_yaml())——这些标签本来就是人工选定放进
-- 配置文件的,human_review_status 在插入时直接记 approved,不需要再走一遍队列。
-- 爆款库里冒出来的新标签只能进 status='suggested' + human_review_status=
-- 'pending_review',要人工在 review_queue.py 里 --approve tag 才转正——复用已经
-- 验证过的 topic/plan/draft/review/final 五段同一套 human_review_status 机制,
-- 不是另起一套。下面的触发器只做一件机械的事:human_review_status 变成 approved
-- 且当前 status 还是 suggested 时,才把 status 也同步改成 active(sources_yaml
-- 来源的标签一开始就是 active,这个触发器对它们不起作用,不会误判)。
-- consecutive_cycles_without_validated_topic:原文档21.3"单标签连续3个轮换周期
-- 没有产出validated选题,自动设为suggested_pause"的计数器,每轮搜索后由调用方
-- 增减,不是数据库触发器算的(需要真的知道"这轮搜出来的视频有没有变成正式选题"
-- 这种跨表业务判断,数据库层做不了)。
CREATE TABLE IF NOT EXISTS domain_search_tags (
    tag_id TEXT PRIMARY KEY,
    tag TEXT NOT NULL,
    domain_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'suggested', 'suggested_pause')),
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

-- 轮换调度游标——原文档21.1"每个活跃标签当前默认7天内至少轮换搜索一次"。
-- last_searched_at IS NULL 表示从没搜过,永远排在最前面该轮到它。跟标签表分开
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

-- 账号发现复查任务(A5, 2026-07-13, 原文档7.1"标签搜索发现账号的复查触发")。
-- 一条账号只建一条复查任务(即使后续又通过筛选,也不会重复触发第二条——见
-- run_account_discovery.py 的 find_accounts_due_for_review() 排除已有记录的
-- 逻辑),直到这条任务被处理完(disposition 从 pending 变成别的)。
-- disposition 三选一,不是二选一的 approved/rejected——原文档明确要求"加入/
-- 忽略30天/永久忽略"三种结果,套不进 review_queue.py 现有的通过/拒绝二元机制,
-- 这里用自己的 resolve_account_review() 函数处理,不勉强复用。
-- video_count 是触发时真实符合条件的视频数(>=3),不是"最近10条"的固定数字——
-- 原文档要求复查任务额外拉这个账号"最近10条可访问视频"重新统计,但那需要一次
-- 新的真实网络请求(账号主页快照),这次没有实现,诚实地只用已经发现、已经落库
-- 的真实视频做统计,不假装拉了"最近10条"。
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

-- 热点转化(A6, 2026-07-13, 原文档第19章"热点转化", 范围收窄)。原文档自己写着
-- "自动热点数据源当前属于未完成节点;MVP可先通过飞书人工热点输入和通用
-- hotspot_event接口运行"——这次只做这一段:人工登记一条热点文本、系统存成
-- hotspot_event、用领域话题标签库(A3 的 domain_search_tags)做确定性关键词
-- 匹配来判断"这条热点跟当前领域有没有关系"(不调LLM,呼应 BR-TOPIC-001 的
-- 不打分排序原则)。TrendRadar 自动抓取整体不在这次范围内。source 目前只有
-- feishu_manual 一个值,虽然飞书当前实际没有真实接入(CLAUDE.md 已经写明)——
-- 这个字段名字保留"未来真接了飞书用哪个来源"的语义,这次的登记入口是命令行,
-- 不是真的飞书。
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
