# 创作助手 · MVP 构建路线图(BUILD_PLAN.md)

> 按阶段建,每阶段是**能停、能测**的里程碑。做完一阶段验收再下一阶段,**不跳建**。完整设计见 memory: `target-architecture.md`。

## MVP 范围
单领域(泛科普)+ 单账号(张芝士)+ 单平台(抖音)+ 全程人在环(自主度最低档)。

---

## 阶段 0 · 地基
- [x] `CLAUDE.md`(工程章程)
- [x] `BUILD_PLAN.md`(本文件)
- [x] 目录骨架 + 配置(`.env.example`、`config/settings.yaml`、领域/账号配置;`.env` 待用户填 DeepSeek / 飞书凭证)
- [x] 数据模型:SQLite 9 表 + candidate_pool 视图(`scripts/db/schema.sql` + `init_db.py` + `seed_mvp.py`,已建库并登记张芝士)
- [x] Obsidian 范例库目录结构 + frontmatter 模板(`vault/`:范例五桶 / 爆款拆解 / 人设 / 词表 / 参考 / 模板)
- [x] 挖料归档:禁词表→`config/banned_words.yaml`;人设→`vault/人设/张芝士.md`;对标种子20个→`config/domains/泛科普.yaml`;范文/痕迹库/方法论/旧模板→`vault/参考/`(溯源见其 README)
- **验收**:✅ 2026-06-13 目录 + 表 + 范例库就位,料归好(3615篇人类范文+痕迹库+禁词表+人设+对标种子)。

## 阶段 1 · 采集 + 对标账号
- [x] 抖音采集:完整 MediaCrawler(`vendor/MediaCrawler`,gitignore;扫码登录态已缓存;CDP 无头默认,`--show` 弹窗重登;内置 2s 限速)。**vendor 本地改了四处**(重克隆需重打,全标 `[创作助手改]` 可 grep):`base_config.py CDP_CONNECT_EXISTING=False`(自动拉起浏览器)、`browser_launcher.py 调试口绑 127.0.0.1`(安全)、`browser_launcher.py get_browser_info 不再跑 chrome --version`(**弹窗元凶**:Windows 上该命令会拉起用户浏览器弹主页窗口,改读安装目录版本号)、`douyin/client.py creator 模式尊重 CRAWLER_MAX_NOTES_COUNT`(每日增量限页)。另:采集脚本有互斥锁(.crawl.lock)+启动前清残留爬虫 Chrome,防重叠运行。
- [x] 采集两档节奏:`--mode daily`(默认,每账号最新20条=新发布+观察池复查,轻)/ `--mode full`(**仅注册时一次**,拉存量当基线种子)。
- [x] 基线自保鲜(2026-06-13 用户提出,替代周期全量复刷):基线指标=视频**出观察期时的点赞数**(Day7口径),daily 复查免费产生→样本自动长、自动新鲜。存量种子(终值口径,略偏高=阈值保守)随90天窗口滚动被洗成纯 Day7 样本。`video_checks` 快照表(观察期内每复查一行,≤7行/视频)攒生长曲线——现在只捕获不建模,数据够了再做早期预警(第N天异常陡→提前晋升/插队逆向)。
- [x] 对标账号注册:`scripts/collect/register_competitor.py`(贴链接/短链/sec_uid 解析;`--list` 看池子)。填名字搜索流未做(MVP 用贴链接)。
- [x] 新视频→观察池:`scripts/collect/crawl_competitors.py`(爬+入库 upsert 去重+复查计数+回填粉丝/简介)
- **验收**:✅ 2026-06-13 张见识(40w粉)注册;68 条视频入观察池(数据齐全);二次运行无头、新增0刷新68(增量去重OK)。待办:其余 19 个种子账号等用户给链接陆续注册。

## 阶段 2 · 爆款判定
- [x] 账号相对基线:`scripts/analyze/judge_hits.py`(已定型视频的滚动90天中位数/P90,样本<10扩窗;快照存 baselines 表)
- [x] 判定+流转:未晋升视频 ≥ max(中位数×3, P90) → hits 浅入库(算超额倍数+转评比);watching 到期(>7天)→ archived 转基线材料。注册存量一次性判定,观察中新视频随复查触发。
- **验收**:✅ 2026-06-13 张芝士对标张见识:基线 中位数6952/P90 115309(样本59);存量判出 6 条爆款(超额16.6~69.4x,转评比13~55 全过金标准);6 条新视频(≤7天)在观察池。阈值是起步假设,跑通后按数据校准。
- ⚠️ 修正记录:初版把注册存量全塞进了观察池(错);已改为 存量→基线+一次判定→archived,观察池只收新发布。表 observation_pool 改名 competitor_videos,观察池=watching 子集视图(migrate_001)。二次修正:**首采(last_crawled_at为空)一律归档**,年轻存量也不进观察池(曲线缺前几帧=建模噪音;其计数照样每日刷新+每轮重判,不吃亏)。

## 阶段 3 · 每日选题流(定时·快·不碰逆向转写)
> 飞书通道已于 2026-06-13 提前打通:应用 cli_aaa6f4f313f8dbcb(机器人+长连接事件订阅+收发实测OK),凭证/会话ID在 `.env`;本机装有官方 lark-cli(收发消息/监听事件都用它,文档 https://github.com/larksuite/cli )。
- [x] 多源汇总:`scripts/topics/daily_topics.py`(MVP两源:爆款库新货按超额排 / 候选池按热度排;分组卡片不统一排)。热点/抖音话题源二期加。
- [x] 推飞书:`scripts/feishu/push.py` 薄通道(交互卡片/文本,经 lark-cli;数据真相在本地)
- [x] 候选池机制:翻页(昨推未选→candidate)+ 衰减(0.5^(龄/7天) 重算)+ 低热淘汰(<0.1 expired)+ 回温(新爆款标签∩候选标签→加成标🔥;**词表打标启用前自动跳过**,逆向阶段激活)
- [x] 定时:Windows 计划任务 `CreationAssistant_Daily` 每天 09:00 跑 `scripts/run_daily.py`(采集→判定→选题,任一步失败飞书喊话)
- **验收**:✅ 2026-06-13 真推卡片到飞书(6条新晋爆款选题);模拟次日验证 翻页6条/衰减69.4→62.9(=×0.5^(1/7))/淘汰阈值在位;全链彩排 exit 0。**注**:MVP 选题判断暂无 LLM(选题范例库为空,判断无据;先按超额诚实排序),范例库长起来后接入。选中/打回的飞书回复监听=阶段4入口。

## 阶段 4 · 选中 → 研究 → 创作
- [x] 选中一个选题(飞书『选 N』)→ 触发:`listener.on_select` 在来源爆款已备料(transcript 在)时直接 `Popen prepare_topic`;没备料则诚实提示(不再过度承诺)。2026-06-14 接通(此前只置 selected、空喊话)。
- [x] 来源爆款按需逆向 + 研究 → 材料包:`prepare_topic.py`(若无 DNA 先 `dna.py --hit` 拆 → `research.py` 简版研究 → 组装 `data/topics/<id>/brief.md`)。⚠️ 2026-06-14 修回归:`dna.py` 改 `--hit/--batch` 后 prepare_topic 仍用旧位置参数会崩,已修。**依赖备料前移到入库**(见 [[reverse-prep-vs-analysis]]):选中时来源通常已转写+评论齐。
- [ ] Claude Code 创作对话:钩子→大纲→成稿(读范例,材料包装载,随时插嘴;阶段模板后台自动发;正向规格留 prompt,禁令不进 prompt)
  - **🆕 创作端三处定型(2026-06-21,见记忆 `brief-overdetermination-fix`)**:①`prepare_topic.assemble()` 双写 `brief.md`(写手版·剔除来源原文/逐字金句/预写洞察以防复述与抢戏)+ `brief_full.md`(完整版·规划+复述查重用);②写手提示词去禁令(文风毛病交下游闸,正向靠范例+真人写作基石驱动)+ 去 `/` 口播标注(写干净口语)——两条选题各跑多次验证:引擎/CLAUDE.md/禁令均非质量因,质量杠杆在 brief 取舍。
- [ ] **题目优先备料通路(2026-06-21 识别的缺口,见记忆 `topic-first-prep-gap`)**:当前管线全程**爆款锚定**(`daily_topics` 只从 hit 出题、`prepare_topic` 围着 source_hit 转)。缺一条"从题目/方向出发、不挂爆款 → 跑新研究 → 出 brief"的路,它同时卡两件事:①**原创选题**(自己开题、不二创);②**新方向重搜**(研究里冒出的可裂变选题/信息缺口,选中后按新主题重新搜资料——现在写手 mode B 只复用旧 research、不重搜、不立新选题)。`topics.source_type` 字段已预留(现只用 `'hit'`),架构有位置;补它=两件事一起解。
- **验收**:⏳ 选中→材料包链路已接通+回归修复;创作对话(钩子/大纲/成稿)待走通一篇。

## 阶段 5 · 校验 + 改稿 + 范例回写
- [x] **改稿 diff + 对照范例回写**(2026-06-15,纯确定性无LLM):`scripts/content/diff_draft.py <topic_id>` —— AI稿 vs 你改稿 句级对齐 → 抽"AI句→你改句"对照对 → 写 `vault/范例/对照/`(带可检索 frontmatter:层/桶/强度0.9/时效/标签)+ 记 `diffs` 表(changed/total/edit_ratio=改动量,越改越少=自主度指标)+ 你那版标 approved。`save_draft.py` 加 `--version`(多版并存时指定哪版是AI稿)。流程:创作出 draft_v1.md→`save_draft <id> --author ai`→你把 v1 另存 draft_v2.md 改→`diff_draft <id>`。✅ 测通(8处对照、改动量3%)。
- [x] 禁词 regex 校验(L1)→ 命中定点改:`scripts/content/check_banned.py`(确定性·无 LLM;扫 draft 出 RED/WARN/HINT 带行号,命中 RED 退出码1可当闸;实现 red/warn/red_patterns/hint_patterns/标点/书面词替换;四字成语需词典暂留审稿清单)。已接进 `创作.md` 第六步(审稿/优化→**禁词L1**→humanizer)。2026-06-25 合成用例五级全触发+真稿82跑通。
- [ ] AI 味判官(写完用"真人vs AI"尺 + 朱雀参照查→定点重写)
- [ ] "反复同类才提炼"→ 把高频对照/认可稿晋升成**文风范例**(现在单次=捕获;频次去重晋升待做)
- **验收**:✅ 改一篇稿 diff 能算、对照范例能落库;⬜ 禁词/AI味判官/文风晋升 待建。

## 阶段 6 · 逆向知识引擎(独立后台轨)
- [x] 本地 ASR 转写器(`tools/asr/transcribe.py`,独立 venv):爆款→下载视频→ffmpeg提音频→SenseVoice转写→质检→回写 hits→删临时文件。✅ 2026-06-13 张见识 6/6 爆款转写成功。**2026-06-14 砍掉 transcript_fix/haiku 修复节点**:每条全文发 `claude -p` 修错字、80条连发+重试,几小时打爆订阅会话窗口;生 ASR 文本直接落库,清洗交给下游 opus 拆 DNA。**本次定型两处**:
  - 模型走 **本机公共模型库**(真实路径由本机 `config/settings.yaml` 的 `reverse_engine.models_root` 配置,不入库)。离线本地加载不走 modelscope 下载、不占 C 盘。配置:`settings.yaml reverse_engine.models_root/asr_model/vad_model`。
  - **下载链接现取,不入库**:抖音 play 链接带 `sign=` 时效签名,入库即成死链(转写时过期→302到CDN→截断 `IncompleteRead`→慢+失败)。改为:采集入库剔除 `*_download_url`(`common.strip_ephemeral_urls`),转写前用 MediaCrawler detail 模式批量现取新鲜链接(`common.fetch_fresh_aweme`,复用浏览器互斥锁,jsonl 读完即归档不进增量)。
  - **🆕 备料合并(2026-06-14,见记忆 `reverse-prep-vs-analysis`)**:把"评论抓取"并进转写那趟 detail 爬——`fetch_fresh_aweme(urls, with_comments=True)` 同趟返回 `(详情含下载链接, 评论)`(MC base_config `CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES=60`);`fetch_comments.store_hit_comments()` 抽成复用件,转写循环逐条入库(幂等 `OR IGNORE`)。即"备料=转写+评论"一趟拿齐,属【入库】阶段。✅ 验:同趟爬回 1 详情(含下载链接)+ 60 评论;asr venv 跨目录 import 链通。⏳ 整条转写循环待下条新爆款入库时实跑(现 80 条已手动备好)。
- [x] LLM 拆 DNA(`scripts/reverse/dna.py`,**opus 4.7**·2026-06-14 用户改路由 `reverse_dna: claude-opus-4-7`):文案+数据 → 拆解笔记(选题/Hook/结构起承转合/张力/金句/可裂变选题,对照"爆款喜报"框架)→ `data/爆款拆解/`,回写 `hits.dna_note_path` + `reverse_status='done'`。✅ 2026-06-14 hit 68 单条实测通过,质量具体可模仿。
- [x] 逆向队列/批量器:`dna.py` 升级 `--hit`(单条/插队)+ `--batch`(断点续跑·本地日志 `logs/dna_batch.log`·撞限自停·幂等跳过已拆)+ `--status`;启动器 `启动逆向拆DNA.bat`。**用户定:不主动批量跑(opus 耗额度),双击 bat 空闲时段自己跑**(见 [[llm-batch-discipline]])。
  - **排队优先级 reverse_priority(2026-06-14 步骤1 落地,见 `reverse-prep-vs-analysis`)**:`= 强度 × 新意`,纯确定性算在 `cluster_hits.py`(asr venv 跑)。步骤1=同簇去重(洗稿/二创只拆代表,非代表 NULL 排除)+ 强度排序(log1p 超额);`dna.py` 队列已用上(80→77 待拆,3 洗稿排除验过)。步骤2=跨簇语义新意软分(缓做)。
  - **调度已接(2026-06-14·解耦+触发)**:`run_daily` 选题跑完→若有待备料 hit,fire-and-forget 后台拉起整理工 `tools/asr/reverse_prep_worker.py`(asr venv·DETACHED·不阻塞):分组算分(cluster_hits)→ 备料(transcribe --all-pending,转写+评论一趟)。拆 DNA 仍手动 bat。日志 `logs/reverse_prep.log`。验:hit 28 重置→整理工自动备料回来。
- [x] LLM-wiki 整合(`scripts/reverse/reduce.py`,reduce 找共性:80 条 DNA 笔记 → 归纳共性 → **领域层**范例库 选题/钩子/结构/文风 + 方法论路由表)。✅ 已跑:80 条 DNA 拆出(reverse_status=done) → reduce 产出 4 张打法路由表(`vault/方法论/`,frontmatter `source: competitor_hit_reduce, sample_n: 80`)+ 五桶范例(选题12/钩子15/结构12/文风13)。
  - **提炼三摊(2026-06-16~17 完成,见记忆 `experience-library-reorg-plan`)**:①方法论合并(路由表 + 钩子/结构「怎么做」配套)②范例升级(52 张全补「AI 反例 + 怎么照做」)③真人写作基石(100篇/11作者提炼通用真人写作痕迹,`vault/真人写作基石.md` always-on)。
- **验收**:✅ 单条爆款→拆解笔记;✅ 备料合并(转写+评论一趟);✅ DNA 批量器(80 条已拆);✅ reduce 归并入领域层范例库 + 提炼三摊收工。整个经验库/提炼线**已完备**——燃料齐,待创作环节点火。

## 阶段 7 · 追踪 + 进化闭环
- [ ] 自营 Day0-7 追踪(等账号有发布后)
- [ ] 接通回流:数据 → 盖章升级选题 / 结构范例;评分预估 → 数据校准(只读诊断)
- **验收**:有自营发布后,数据能回流进化(可能等账号运营后才完整验)。

---

## 二期(MVP 之后再加)
多领域 / 账号 / 平台、AI 味痕迹检测器、深度研究循环、配音(IndexTTS-2 / VoxCPM2,3090 本地)、自主度旋钮升档、创作 agent team、web 作战台。
