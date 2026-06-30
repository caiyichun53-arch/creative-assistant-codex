# 豆瓣语感燃料采集

定位: 豆瓣是平台专用多频道采集模块，不是通用爬虫。所有采集必须指定目标，禁止无目标热门榜、首页流、随机帖子采集。采集结果是原料，进入 SQLite/文件/Obsidian 原料笔记，不直接进入范例库。

## 入口

```powershell
python scripts\language_fuel\douban\collect.py reviews  --channel movie --query "罗马，不设防的城市" --limit 20
python scripts\language_fuel\douban\collect.py reviews  --channel book  --subject-id 4913064 --limit 20
python scripts\language_fuel\douban\collect.py reviews  --channel music --subject-id 5344708 --limit 20
python scripts\language_fuel\douban\collect.py comments --channel movie --subject-id 1296669 --limit 80
python scripts\language_fuel\douban\collect.py groups   --topic-url "https://www.douban.com/group/topic/..." --include-replies
```

兼容旧入口:

```powershell
python scripts\language_fuel\collect_douban.py --channel movie --query "罗马，不设防的城市"
```

## 内容类型

- `reviews`: 书影音长评全文，支持 `movie/book/music`。这是豆瓣最重要的采集形态。
- `comments`: 书影音短评，支持 `movie/book/music`。当前实测电影/音乐可采；图书短评匿名访问经常返回空正文，默认不作为稳定来源。
- `groups`: 小组主帖和回复。默认要求小组白名单，适合采集真实讨论语气、争论结构、吐槽方式和生活化表达。

## 采集策略

### 通用规则

- 先搜索，后指定目标。搜索结果不唯一时，必须用 `--list-subjects` 看候选，再用 `--subject-id` 精确采集。
- 不采摘要。长评必须拿全文，标 `content_status=full`。
- 不追求大而全。每次采集必须有写作任务、领域主题或作品对象。
- 不自动采小组。小组必须白名单或人工传 `--topic-url`。
- 同一对象可重复采集，但靠数据库 `platform_item_id` 去重更新。
- 先小样本验证，再正式采集。新对象先 `--limit 3 --no-obsidian` 看质量，再放量并默认写 Obsidian。

### 影视

用途: 写影评、影视娱乐、人物事件、剧情争议、观众情绪和长评论证。

优先级:

1. 长评全文: 优先采。
2. 短评: 用来补观众口语、神评论和高密度态度。
3. 小组: 只在需要争议讨论、饭圈/影迷社区语气时采。

数量:

- 单部作品初采: 长评 20 条，短评 80 条。
- 深度写作备料: 长评 50 条，短评 200 条。
- 主题横向对比: 3-5 部作品，每部长评 15-30 条，短评 50-100 条。
- 小组补充: 5-10 个指定帖子，每帖主帖 + 前 50-100 条回复。

### 图书

用途: 观点型写作、知识类选题、人文/社科/小说评论、读者真实理解偏差。

优先级:

1. 长评全文: 主来源。
2. 图书短评: 当前不稳定，默认跳过，除非确认登录态可采。
3. 小组/讨论: 只采指定帖子，用于读者争议和真实表达。

数量:

- 单本书初采: 长评 20 条。
- 深度备料: 长评 50-100 条。
- 主题书单: 3-8 本书，每本长评 10-30 条。
- 争议补充: 指定小组帖子 3-10 个，每帖前 50 条回复以内。

### 音乐

用途: 音乐领域资料搜集、歌手/专辑/歌曲内容创作、听众表达、歌词外延和时代情绪。

优先级:

1. 专辑/音乐条目长评: 用来补背景、审美判断、乐迷观点。
2. 短评: 用来补听众金句、情绪表达、口语化评价。
3. 网易云音乐: 歌曲级听众故事和评论另走网易云专用入口，不用豆瓣替代。

数量:

- 单张专辑/单个音乐条目初采: 长评 20 条，短评 80 条。
- 歌手专题: 3-8 个代表条目，每个长评 10-30 条，短评 50 条。
- 单曲写作: 豆瓣只作背景补充，主评论源优先网易云。

### 小组

用途: 真实网友语气、争论现场、玩梗、吐槽、生活化表达、社区切口。

边界:

- 默认必须在 `config/settings.yaml` 的 `group_whitelist` 中。
- 人工临时测试可用 `--allow-unlisted-group`，但不能放进自动任务。
- 不采首页流，不采无关键词列表，不采隐私敏感讨论。

数量:

- 单个明确话题: 1-3 个帖子，每帖主帖 + 前 50 条回复。
- 社区语气观察: 5-10 个帖子，每帖前 30-80 条回复。
- 大型争议复盘: 10-20 个帖子，总回复控制在 1000 条以内。

## 任务模板

### 单作品备料

1. `reviews --list-subjects` 确认条目。
2. `reviews --subject-id ... --limit 3 --no-obsidian` 小样本验质量。
3. `reviews --subject-id ... --limit 20/50` 正式采集。
4. 电影/音乐再补 `comments --limit 80/200`。

### 主题横向备料

1. 手工列 3-8 个目标条目。
2. 每个条目先采长评 10-30 条。
3. 只对表达密度高的条目补短评。
4. 小组只补明确相关帖子。

### 语感燃料补库

1. 优先选高讨论密度对象，不随机扫。
2. 每次只围绕一个主题或一个领域。
3. 达到数量后停止，交给后续提炼流程，不继续堆原料。

## 存储

- SQLite: `language_fuel_items` / `language_fuel_batches`
- 文件: `data/language_fuel/douban/<channel>/`
- Obsidian: `vault/语感燃料/<领域>/豆瓣/`

字段关键口径:

- `platform=douban`
- `channel=movie/book/music/group`
- `source_kind=long_review/short_comment/group_topic/group_reply`
- `content_status=full`
- `quality_status=usable/too_short/partial`
- `privacy_status=clean/redacted`

## 小组白名单

```yaml
language_fuel:
  platforms:
    douban:
      channels:
        group:
          require_whitelist: true
          group_whitelist: []
```

临时人工测试可加 `--allow-unlisted-group`，不要放进自动任务。
