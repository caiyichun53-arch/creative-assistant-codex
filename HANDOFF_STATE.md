# HANDOFF_STATE

> 活文档:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。不是一次性快照——`CURRENT_REPOSITORY_BASELINE.md`/`RELEASE_CANDIDATE_BASELINE.md` 才是那种一次性冻结记录,这份文件永远反映"现在"。

## 现状(2026-07-08,本次收工时)

- **分支**:`activation/goal-v0.6.2-production-activation-01`(与下方 2026-07-07 记录同一分支延续)。
- **本次做的事**:上一轮"master doc 完全对齐重写"之后,用户用真实数据追问了三个具体问题,顺藤摸瓜修了三个真问题(细节见 `BUSINESS_RULE_CATALOG.yaml` 的 `amendment_2026_07_08_backfill_counting_and_daily_refresh`):
  1. **窄范围重新引入"窗口外回补"机制**:账号 90 天内样本不足 20 条、但账号全部历史样本 ≥20 且量级明显够高(同 `_account_small_status` 的判定口径)时,用全部历史(封顶 50 条)顶上,不再永久判定"无基线"。
  2. **修了一个真实存在的统计口径 bug**:`judge_account()` 的 `rough_hit_count`/`formal_hit_count` 之前按 `judgment_confidence`(会被 comment_like_ratio 覆盖成 formal)计数,导致 30 条同时命中两个通道的视频被漏计成 rough——现在改按 `baseline_mode`(不会被覆盖)计数。写入 `hits`/`competitor_videos` 的记录本身一直是对的,只是运行报告的汇总数字算错了。
  3. **过渡期视频改成每天都刷新+参与判断**:之前 `ingest_daily_incremental_items()` 对过渡期(1-7天)视频只在满 7 天那天才更新数据,期间(第1-6天)完全冻结在首次采集时的快照——现在每天都会刷新数值、记一条 `video_checks`,跟正式跟踪视频(formal_new)的节奏对齐。没有加"发布不满N天不判断"这类年龄门槛(用户明确要求从简)。
- **真实数据重判结果**(`reset_judgement_state` 清空后 `--rejudge-only` 重新判定,28 账号):`total_promoted=441`(112 formal + 363 rough,5 个账号用到了新回补机制),`小椰子专栏`(此前挂零的高体量账号)现在有 4 条基线(sample_count=50,回补生效)、19 条真实命中。
- **紧接着又补了一个字段**:用户追问"过渡期视频每天观察,到底记在哪儿"——发现`discovery_batch_index`只服务正式跟踪视频的D0-D7(发现批次锚定),过渡期视频每天的check完全没有"第几天"标签。加了`video_checks.day_since_publish`(日历天数锚定,跟discovery_batch_index刻意分开存,不混用),`install_schema()`现在会对已存在的库懒加这个新列(`CREATE TABLE IF NOT EXISTS`不会给已有表补列)。已经通过`--rejudge-only`真实调用过一次,确认真实生产库`video_checks`表已经有这一列。
- **测试**:`tests/core` 259 个、`tests/validation` 44 个全过(新增 6 个测试:回补机制 3 个、计数修复 1 个、每日刷新 1 个、day_since_publish 1 个)。
- **写这份文件的人/时间**:Claude Code,2026-07-08。

## 历史记录(2026-07-07,上一次收工时)

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **当前 GOAL**:`GOAL-V0.6.2-PRODUCTION-COMPLETION-01`(见 `implementation_progress/GOAL-V0.6.2-PRODUCTION-COMPLETION-01.md`),工程状态 `completed`,production activation 已跑过一次受控真实数据 pilot(竞品账号注册+首采+基线/爆款判定)。
- **工作区**:本次收工前已把所有未提交改动(评论/点赞比率并集通道 + 本次的所有配套修改)提交成一个 checkpoint commit——收工时工作区应为干净状态,如果不是,说明规则被违反了,先处理这个再往下做。
- **写这份文件的人/时间**:Claude Code,2026-07-07。下次不管谁接手(Codex 额度恢复或 Claude Code 继续),先读这份文件,不用重新考古。

## 权威闸门真实输出(不是转述)

```
$ python scripts/core/staging/verify_goal_v062_phase8_readiness.py
status: ENGINEERING_READY

$ python -m unittest <31个已知模块,find tests -iname "test_*.py" 取得>
Ran 287 tests in 26.3s
OK

$ python -m scripts.core.business_data.run_competitor_registration_full --rejudge-only
summary: {accounts: 28, videos: 1253, promoted_videos: 245, baselines: 84, hits: 245}
run_id: competitor_registration_rejudge_20260707T023426Z
(直接查真实库确认:28 账号里 0 个爆款数为 0;每账号爆款数1~17条;excess_threshold 从3改2倍后
 总爆款数 198 -> 245;hits.hit_channel 分布:like_threshold=177, comment_like_ratio=43, both=25)
```

**踩过的一个坑,记录下来避免重复踩**:`legacy_removal_gate.py` 是按 `git ls-files`(已跟踪文件)扫描的。`scripts/core/business_data/` 之前是未提交状态,对这个闸门是"隐形"的,所以早前跑闸门一直是绿的;**提交之后**同名表(`competitor_videos`/`hits`)和 guard 消息里的 `creation.db` 字符串才被扫到、闸门转红。也就是说:**未提交的文件不算真正验证过**——闸门/测试的绿灯只在文件已提交(至少是 `git add` 过、能被 `git ls-files` 看到)的前提下才可信。已经把这两个新文件加进 `ALLOWED_GUARD_REFERENCE_FILES`(guard 消息误报)。另外新增的两个监控面板 `.bat` 启动器也撞上了"任何 .bat/.vbs 都算 legacy executable path"这条硬规则——用户明确要求保留 `.bat`(要的是双击可用),已加进新的 `ALLOWED_NEW_EXECUTABLE_PATHS` 例外,不是放松了旧 legacy 路径的检测。

## 本次会话做了什么(核实 + 修复,不是新功能开发)

用户让 Claude Code 接手开发前,先核实此前 Codex 自述的一次"跑偏"修复是否属实。核实结果好坏参半,已修复的问题:

1. **`PHASE_8_AUTHORIZATION_GATE_STATUS.yaml` 状态字段枚举不匹配**:`production_activation_status`/`real_data_pilot_status` 曾写成描述性自造词,导致权威闸门脚本判 `ENGINEERING_NOT_READY`,但对外汇报"completed"——已改成闸门认的精确枚举值(`controlled_pilot_started`/`succeeded`),重新验证闸门真实转绿。
2. **`scripts/validation/clean_room_readiness.py` 的 P2-BLOCK-002 假阳性**:遗留数据扫描拿 `competitor_videos`/`FROM hits` 等字符串扫 `scripts/core/**`,而新建的 `scripts/core/business_data/` 自己有同名但完全隔离的新表——已加排除,`phase_2_safe_to_execute` 从 `False` 转 `True`。
3. **补了一个受契约约束的"只重判、不重新采集"入口**:`run_competitor_registration_full.py --rejudge-only`(对应新函数 `run_rejudge_only()`),此前那次数据修复是走没留痕的临时路径做的,以后同类修复必须走这个入口,配了两个测试(正常重判 + 契约不匹配拒绝执行)。
4. **`scripts/core/host/production_host.py` 新增 `ClaudeHostBinding`**(仿照已有的 `CodexHostBinding`),`DEFAULT_ALLOWED_HOSTS` 加入 `claude`,配了镜像测试。
5. **新增 `tests/validation/test_constitution_sync.py`**:防止 CLAUDE.md/AGENTS.md 再次像之前 124 次提交那样悄悄漂移——两份文件除一行模型路由描述外必须逐字一致。
6. **AGENTS.md/CLAUDE.md 加了"执行纪律"一节**(五条硬规则),把这次两次真实事故(30 当硬门槛、rejudge 走临时路径、汇报完成没核对权威闸门)提炼成制度。

**同一天的第二轮(用户要求"先不写新功能,把现有切片补齐验证/文档")**:

7. 补了 `LocalMediaCrawlerExecutor` 的子进程执行路径测试(成功/非零退出/超时/jsonl 解析/run_dir 冲突递增)——之前只测了参数拼装,真正的 `execute()` 执行逻辑完全没测过。过程中发现超时/非零退出分支的 `external_side_effect` 靠 dataclass 隐式默认值 `True`,不是显式设置,跟 `live_gates.py` 里"失败/未运行必须显式 `False`"的既有约定不一致——已改成显式 `True`(进程确实跑过、可能已产生真实外部副作用,不是没跑)。
8. 补了 `run_full_registration()` 的端到端测试(之前只测过它内部各个子函数,从没测过这个真正在生产 28 账号 pilot 里跑的入口函数本身),mock 掉真实 MediaCrawler 子进程,测完自动清理产生的 report 目录。
9. 新增 `scripts/core/business_data/README.md`(模块说明:文件用途、设计契约要点、怎么跑、测试清单)。
10. **提交后才发现的两个闸门回归**(见上面"踩过的坑"):`legacy_removal_gate.py` 对 `scripts/core/business_data/` 的 guard 消息误报(已加 `ALLOWED_GUARD_REFERENCE_FILES` 例外)、对新增的两个 `.bat` 启动器误判为 legacy executable path(已加 `ALLOWED_NEW_EXECUTABLE_PATHS` 例外,用户明确要求保留这两个 `.bat`)。

**同一天的第三轮(用户逐条追问"45 个爆款是不是太少/条件是不是不对",查出真正的设计漂移,不是口味问题)**:

11. **`BR-BASELINE-003` 真漂移,已修**:代码里 `baseline_min_judgement_samples=10` 这个数字在旧系统(`scripts/analyze/judge_hits.py`,git 历史里找到)和新系统正式设计依据 `BUSINESS_RULE_CATALOG.yaml` 里都不存在——两边写的都是 `minimum_sample_count: 30` + `legacy_supplement: true`(90天窗口不足30条,补到最近30条,不看窗口)。已删掉这个自造的 `10`(`BASELINE_MIN_JUDGEMENT_SAMPLES`→改名 `BASELINE_HARD_MINIMUM_SAMPLES=2`,只是"能不能算出中位数"的数学下限,不是业务门槛),`select_baseline_sample()` 改回按 30 补足。同时给 `baselines`/`hits` 表加了 `evidence_status` 列(`sufficient`/`insufficient_sample`),样本不到 30 时判定仍然跑但明确标注证据不足,不再悄悄当满血基线用——这是 `BUSINESS_RULE_CATALOG.yaml` 里 `forbidden_behavior: promote hit from underpowered baseline without flag` 这条硬性要求。
12. 顺带发现并修了 `first_crawl_excluded_reason()` 硬编码 `timedelta(days=7)`/`timedelta(days=90)` 不读配置的问题——现在从 `hit_cfg` 读,配置改了这里会跟着变,不再是两套真相源。
13. **发现真正的"检查机制"其实是废的**:`REQUIREMENT_CODE_TRACEABILITY.yaml` 只被 `legacy_removal_gate.py` 当"历史文档"跳过扫描,从没有任何测试真正解析它、拿去对代码断言——这就是为什么"10"这种发明能混过去这么久没人发现。新增 `tests/validation/test_business_rule_traceability.py`,把 `BUSINESS_RULE_CATALOG.yaml` 里几条可核对的阈值(30/7/90/3.0/p90_required)编码成真断言,以后再有人凭空发明数字,跑测试直接报错。同时把 `REQUIREMENT_CODE_TRACEABILITY.yaml` 补了一节 `business_data_competitor_registration_execution`,标注当前 14 条相关 BR-* 规则的真实对齐状态(不再冻结在 07-02 那次审计)。
14. **加了"重判时撤销不再合格的旧爆款"逻辑**:之前重判只会新增合格的,不会把"以前判过、现在按新规则不够格了"的旧记录退回去——查真实库时发现 2 条这样的残留。已在 `judge_account()` 里加了退回逻辑(范围只限于本轮判定实际重新评估过的样本,不去动本轮没碰到的历史记录,避免破坏审计不可变性原则),配了测试,并且**真的用正式的 `--rejudge-only` 入口对着真实生产库(`data/formal/production_activation.sqlite3`)重跑了一遍**(不是临时脚本手动改库),验证过 0 条残留。
15. **用户要求彻底清空重来**:清空 `data/formal/production_activation.sqlite3`,用修好的逻辑对 28 个账号跑了一次真实全新首采(经过环境预检:MediaCrawler venv/Playwright Chromium/抖音登录缓存都在;`--headless yes` 会覆盖 MediaCrawler 配置文件里 `HEADLESS=False` 的默认值,实际是无头跑的,不会弹窗——这点我一开始查漏了只看了静态配置,被用户纠正过)。后台跑完(约几分钟),直接查真实库核对过(不只信自报的 JSON):28 账号、840 视频全部是同一个 run_id(证明是干净单次采集,不是历史补丁)、52 条爆款、0 条残留不合格记录、两个权威闸门仍绿。

**同一天的第四轮(用户追问"P90 是不是该拿掉",查出 P90 本身机械限制爆款数的真问题)**:

16. **`BR-HIT-001` 阈值公式改了(用户明确决策,不是我自己判断)**:原公式 `max(中位数×3, P90)` 里,P90 是"这批小样本自己排前10%"的数学定义,不管账号是否真有离群爆款,都会把爆款数摁死在样本量约一成——真实数据验证过:28 账号里 23 个的爆款数被 P90 锁死在恰好"2"。跟用户来回确认后定的新公式:`门槛 = max(中位数×3, min(P90, hit_floor_absolute_like_count))`,`hit_floor_absolute_like_count`(当前 2000,可配置,不能写死)给 P90 封顶——大账号不会被 P90 顶到天上,小账号(P90 本来就低于这个绝对值)也不会被绝对值卡死。**不再有 `p90_required` 这个开关**,P90 一直参与,只是被这个绝对值封顶。同步改了三处:`config/settings.yaml`/`config/settings.example.yaml`(新增 `hit_floor_absolute_like_count`,删掉 `p90_required`)、`judge_account()` 的阈值计算、`BUSINESS_RULE_CATALOG.yaml` BR-HIT-001(加了 `amendment_2026_07_06` 字段记录这次业务决策和依据,不是默默改掉旧规则)。加了测试锁定这个公式(`test_hit_floor_caps_p90_instead_of_p90_setting_an_unbounded_bar`)。**再次用 `--rejudge-only` 对真实库重跑验证**:117 条爆款(比之前52条多,因为不再被 P90 机械压制),每账号爆款数 1~8 浮动(不再是清一色的"2"),0 条残留不合格记录,两个权威闸门仍绿。**当前真实数据(2026-07-06,P90 封顶后)**:28 账号、840 视频、117 条爆款,全部诚实标 `evidence_status=insufficient_sample`(样本量还没到 30,原因同上,等每日增量采集攒够样本后自然转 `sufficient`)。
17. **首采抓取数量补了缓冲**:置顶、发布不到7天的视频永久不参与基线计算,首采只抓 `baseline_min_samples`(30)条原始视频的话,刨掉这些,结构性地永远凑不满30条能用的——这不是"数据还没攒够"能解释的,是抓取数量本身没留余量。查了真实数据:28 账号里最坏的一个排除了14/30条,平均排除6条。按最坏情况留余量,新增配置项 `first_crawl_fetch_buffer: 15`(首采改成抓 30+15=45 条,不写死在代码里,`resolve_max_notes()` 读配置算),`--max-notes` 显式传参仍然优先。
18. **用户要求彻底清空、按新缓冲量真实重新采集验证**:清空 `data/formal/production_activation.sqlite3`,按 45 条/账号重新真实采集 28 个账号。直接查真实库核对过:28 账号、1253 视频(全部同一个 run_id)、**每个账号能用的样本量 31~44 条,全部 ≥ 30(之前是 28 个账号全部 < 30)**、150 条爆款、**全部 28 条基线和 150 条爆款的 `evidence_status` 都变成了 `sufficient`(之前全部是 `insufficient_sample`)**、每账号爆款数 1~11 浮动、27/28 账号有爆款、0 条残留不合格记录,两个权威闸门仍绿。**当前真实数据(2026-07-06,缓冲量生效后)是本次会话第一批"证据充分"的真实业务数据**,今天四轮修复(30目标样本+证据标注、P90封顶、抓取缓冲)加起来的效果在这批数据上完整体现了。

**第二天(2026-07-07):爆款判定加了第二条并集通道(评论/点赞比率),因为纯点赞公式漏判了一个真实账号**

19. **发现问题**:上面第18条那批数据里,27/28 账号有爆款,唯独 **财经不眠姐 完全挂零**——纯点赞公式对它来说门槛太高。用户要求方案必须"结合官方数据、爆款在这些比率数据上的真实表现",而不是又编一个内部百分位数;还要求"领域不同、看重的维度不同"这层考虑。中间走了不少弯路(试过候选池分级、加权综合分、AND 投票,都被用户否掉或验证后发现不可靠——加权综合分和 AND 投票都因为点赞/评论/转发/收藏四个数字本身高度相关,揉在一起反而让判定失灵,财经不眠姐还是被误判)。
   - **播放量查证**:用户问能不能拿播放量算真正的官方比率(点赞率=赞/播放量等)。查证结果——**拿不到,不是工具问题,是平台规则**:直接看了 MediaCrawler 实际调用的抖音网页详情接口(`/aweme/v1/web/aweme/detail/`)返回的原始 `statistics` 字段,里面根本没有 play_count;另外网上查证抖音就是不对外暴露别人视频的播放量,只有博主自己在创作者中心能看到。第三方工具号称能查,但那是靠点赞倒推估算的,不是真实数据。
   - **改用"点赞当分母"的官方比率**:用户明确要求"不要硬套官方数字,学它用比率判断的方法"——查到抖音官方公开的运营经验数据里,"评论数/点赞数"这个比率有明确的公开阈值区间(10%及格线、30%左右是典型爆款),不依赖播放量。另外还查到"收藏数是点赞数的3-5倍"这个说法用于知识类内容,但**拿真实1253条视频验证,一条都不满足这个3-5倍**,所以没有采用这一条(收藏、转发这两个维度暂时没有能扛住真实数据验证的官方数字,不强行加)。
   - **真实数据反复验证选阈值**:分别拿15%/18%/20%/30%对真实1253条视频、28个账号做了并集模拟(不是单独用这条,是跟原点赞公式取并集)。30%太严(14/28账号挂零);10%太松(财经不眠姐直接从0跳到20个,45条里近一半,重新引入之前加权综合分/AND投票暴露过的"财经不眠姐容易被过度判定"风险);15%已经出现"加速松动"迹象(财经不眠姐、肯塔基基等账号涨幅明显比18%→20%那一档更快)。**最终选定 20%**,因为它精确解决了挂零问题(财经不眠姐 0→2,某次快速核算)且没有让任何其他账号挂零,财经不眠姐的涨幅也最克制。
   - **实现**:`judge_account()` 加了第二条独立通道——`comment_count/like_count >= comment_like_ratio_threshold`(0.2,新配置项),跟原有点赞公式取**并集**(命中任意一条就算爆款),不是替代,也不是加权融合。`hits` 表新增 `hit_channel` 字段(`like_threshold`/`comment_like_ratio`/`both`),记录每条爆款是靠哪条通道判定的,新旧数据兼容迁移(`_ensure_hit_channel_column`)。`validate_registration_execution_contract()` 把 `comment_like_ratio_threshold` 也纳入契约校验(必须是 (0,1] 内的数字)。同步更新 `BUSINESS_RULE_CATALOG.yaml` BR-HIT-001(新增 `amendment_2026_07_07`,完整记录决策理由、真实数据验证过程、为什么收藏/转发维度没有加)、`REQUIREMENT_CODE_TRACEABILITY.yaml`,新增5个测试(3个行为测试 + 2个契约测试)。
   - **真实验证**:全部269个已知测试跑过(含新增的5个),两个权威闸门(`verify_goal_v062_phase8_readiness.py` → `ENGINEERING_READY`;完整测试套件 → `OK`)都是绿的。用正式的 `--rejudge-only` 入口对真实生产库重跑:28账号里 **0 个挂零**(财经不眠姐 0→1,经由新通道);总爆款数从150涨到198(48条纯靠新通道判定、20条两条通道都命中);`hits.hit_channel` 字段迁移和写入都验证过是真实分布,不是猜的。

**第三天(仍是 2026-07-07):建"规则不能靠人盯"的机械闸门,起因是又发现一个文档早写好、代码没照做的漏洞**

20. **发现的漏洞**:开始设计"每日增量采集"时查代码发现,`excluded_reason='younger_than_7_days'` 被写成了**永久拉黑**——一条视频只要首采时不到7天,就永远不会被拿去跟基线比对、永远判不了爆款,哪怕后来早就满7天、数据也稳定了。这跟 CLAUDE.md 自己写的"archived 计数仍被 daily 刷新、每轮重判,过阈值照样晋升"直接矛盾。**这不是一个新的设计分歧,是之前写代码时没有对照已经写定的文档**,属于真实 bug,还没来得及修(见下面"下一步")。
    用户在这个点上表达了强烈的疲惫感——不是针对这一个 bug,而是"设计早就讨论定型的东西,落地时又要重新一点点敲定细节"这种模式反复出现(点赞门槛30被写成10、现在这次年轻视频永久拉黑,都是同一类:文档写对了,代码没照做,而且事先没有任何机制会自动发现,只能靠用户自己在对话里发现)。用户明确说:不要道歉,道歉没用,要的是"不靠人来把控"的机制。
21. **建了一道机械闸门,不是承诺**:新增 `tests/validation/test_business_rule_test_coverage.py`——这个测试会真的去读 `BUSINESS_RULE_CATALOG.yaml`(规则清单)和 `REQUIREMENT_CODE_TRACEABILITY.yaml`(哪些规则声称"代码对得上"),再扫描全部测试文件找有没有真的写了对应的 `BR-*` 编号的测试,**只要有规则声称"对得上"但一个测试都没有,直接报错**。已经验证过这道闸门真的管用(手动模拟往对照表里加一条没有测试撑腰的假声明,闸门立刻报错,不是摆设)。
    顺带把之前一直"声称对得上但其实没测试"的6条规则全部补齐了(`BR-BASELINE-002`、`BR-COLLECT-003`、`BR-COLLECT-007`、`BR-HIT-002`、`BR-HIT-003`、`BR-HIT-004`)——3条是已有测试但没写编号引用,补了引用;3条是真的没测试,新写了(视频去重不重复入库、分享评论比不能单独当判定通道、collection模块不接LLM的静态代码扫描)。全部273个测试(含新增4个)跑过,两个权威闸门仍绿。

**同一天(2026-07-07)紧接着:把每日增量采集写完了,先查文档、再写测试、后写代码**

22. **查文档发现每日增量的设计早就写好了,不是新讨论**:`BUILD_PLAN.md` 第20-31行(2026-06-13,用户自己写的)完整记录了:抓取节奏(一次抓最新20条,同时干"发现新视频"+"刷新观察池"两件事)、基线口径(视频**出观察期时**的点赞数=Day7口径,不是提前判定)、`video_checks` 快照表(现在只攒生长曲线数据,不建模,早期预警是以后的事)、首采时太年轻被排除的视频为什么不进观察池(曲线缺前几帧=以后建模的噪音,但"其计数照样每日刷新+每轮重判,不吃亏"——这句原文直接证明第20条发现的"永久拉黑"是真bug)。同时确认了 CLAUDE.md 点名要读的 `target-architecture.md`/`rebuild-direction.md` 在仓库和 memory 里都不存在(旧坑,不是新问题),已告知用户,用户认可继续用 `BUILD_PLAN.md` 作为依据往下做。
23. **先写测试、再写代码,照文档来**:
    - 修复 `judge_account`/`settled_sample_count` 的查询条件:把 `excluded_reason='younger_than_7_days'` 从永久排除改成只是历史标注(查询条件放开成 `status IN (archived,promoted,watching)` + `excluded_reason IN (NULL,older_than_90_days,younger_than_7_days)`),两处查询合并成一个共享常量 `SETTLED_SAMPLE_QUERY`,不再各写一份容易漂移。`select_baseline_sample()` 自带的活时间校验保证了不会误判还没真的满7天的视频。
    - `judge_account` 的"不合格"分支拆成两种:原来是爆款、现在不够格→撤销(不变);原来是"观察中"、满7天了还是不够格→**毕业归档**(新逻辑,新计数器 `graduated_count`)。
    - 明确不做"提前晋升":观察中(不到7天)的视频哪怕数据已经很炸,也不会提前判定,只会更新数据+记一笔 `video_checks` 快照,严格照 `BUILD_PLAN.md` "现在只捕获不建模"来。
    - 新增 `video_checks` 表(schema,append-only,攒生长曲线用)。
    - 新增 `ingest_daily_incremental_items()`:处理顺序固定为"先对刷新观察池里已有的视频(更新数据+记快照),再把剩下没对上的当新发现处理"(用户明确要求的顺序)；新发现的视频,发布在7天内的进观察池,置顶只认平台给的明确标记(不用首采那套位置猜测);如果发现时已经超过7天,直接当存量处理(不进观察池)。
    - 新增 `run_daily_incremental()`:一个受契约校验的入口,把"抓取(用 `crawler.daily_max_notes` 上限)→ 摄入 → 判定"串成一次调用,跟 `run_full_registration`/`run_rejudge_only` 同一套纪律。
    - 全部新写了10个测试覆盖这些规则(先写测试确认失败,再写代码让测试通过,不是反过来),`REQUIREMENT_CODE_TRACEABILITY.yaml` 里 `BR-COLLECT-002`/`BR-COLLECT-004` 从 `missing_in_code` 改成 `exact_match`(带完整修订说明)。全部283个测试(含新增10个)跑过,两个权威闸门仍绿。
    - **还没做的**:没有真的对生产库跑一次真实的 `run_daily_incremental`(会真的联网抓28个账号),只验证到单元测试这一层——这是刻意留白,真实联网抓取属于"影响外部真实系统"的动作,应该先跟用户确认再触发,不是我自己决定跑。

**同一天(2026-07-07)最后一轮:发现 `BUILD_PLAN.md` 是旧系统文档,当场清掉旧系统残留**

24. **用户发现 `BUILD_PLAN.md` 里 2026-06-13 的记录,说的是清空重建前的旧系统**:里面点名的代码路径(`scripts/collect/register_competitor.py`、`scripts/analyze/judge_hits.py`、`scripts/topics/daily_topics.py`、`scripts/feishu/push.py`)全部都在 `config/settings.yaml` 的 `legacy_runtime.quarantined_entrypoints` 里——这些是 GOAL-DATA-RESET-01 清空重建时就被隔离禁用的旧系统代码。我在上面第22条把这份文档当成"现在系统的权威设计"直接引用,是真的查错了权重,不是"又一次重新讨论已经定型的东西"这么简单——是引用来源本身就选错了。
    - 复核确认:`BR-COLLECT-002`/`BR-COLLECT-004`/`BR-BASELINE-002` 这几条我在翻 `BUILD_PLAN.md` **之前**就已经在 `BUSINESS_RULE_CATALOG.yaml`(清空重建时从"V0.6.2 最终执行总控文档"整理出来的、现在真正权威的依据,当前分支就叫 `goal-v0.6.2-production-activation-01`)里独立查到过,这部分立得住,不受影响。
    - **唯独"观察期内不能提前判定爆款,等以后数据够了再做早期预警建模"这一条,`BUSINESS_RULE_CATALOG.yaml` 里完全没有提到**,是我从旧文档里搬来当成"已定型设计"的,属于没有独立证据支撑的假设,已经如实告知用户,由用户决定是否保留。
25. **用户要求把旧系统彻底清掉,不再留任何会被误当成权威的残留**:
    - 查证 `scripts/collect`、`scripts/topics`、`scripts/reverse`、`scripts/research`、`scripts/content`、`scripts/language_fuel`、`scripts/music`、`scripts/feishu`、`tools/asr` 这9个目录——**里面已经没有真实源代码,只剩 Python 自动生成的 `.pyc` 缓存垃圾,而且这些缓存从未被 git 记录过**(`git ls-files` 确认过);删除不会丢任何东西,也确认了当前 `scripts/core/**`/`tests/**` 没有任何代码真的 import 这些路径(只有一处历史注释提到 `scripts/analyze/judge_hits.py`,不是依赖)。
    - 权限系统(auto-mode 分类器)两次拦截了删除动作,要求用户逐一亲自点名要删的具体路径(不接受"删除旧系统"这种笼统指令,也不接受我列清单让用户回复"删掉"就算数)——用户照做后,删除了这9个目录 + `BUILD_PLAN.md`(git 有记录,不是真的消失)。
    - 同步修了 `CLAUDE.md`/`AGENTS.md` 里两处指向 `BUILD_PLAN.md` 当权威的说法(开头"每次开工先读"那句、"开工纪律"里"每步对照"那句),改成明确指向 `BUSINESS_RULE_CATALOG.yaml`,并加了一句永久说明:以后任何"设计是不是已经讨论过"的问题,只认 `BUSINESS_RULE_CATALOG.yaml`,不接受旧计划文档当权威。**没有动 `legacy_runtime` 的隔离清单本身**(那是另一套真实在生效、防止旧代码复活的安全机制,跟"文档被误当权威"是两个不同的问题,不能混着改)。
    - 全部283个测试(含 `test_constitution_sync`)跑过,`clean_room_readiness`/权威闸门都仍是绿的/`ENGINEERING_READY`,确认删除没有破坏任何现行代码。

**同一天(2026-07-07)最后一轮:找到独立确证,并按用户明确要求把"提前判定"加回去**

26. **补搜到了一份之前没查到的正式文件**:`BUSINESS_DECISION_TABLES.md`(标题就是 GOAL-ALIGNMENT-01,清空重建后的正式决策记录,不是 `BUILD_PLAN.md` 那种旧系统文档)。这份文件整份(94行,不是摘录)独立确认了:首采年轻存量视频直接归档不进观察池、观察期(0-7天)只记录数据不主动判定、满7天归档为基线材料——前两条跟第22条从 `BUILD_PLAN.md` 查到的一致,不是巧合,是真的有独立正式记录。**但这份文件同样没有解释"为什么观察期内不能提前判定"这个为什么,只是陈述了结论。**
27. **用户明确要求:新视频自己0-7天攒的 `video_checks` 曲线,必须真的拿来判断,不能只攒不用**——用户原话"不然搞尼玛的0-7天曲线"。查证过没有任何文档说"不能提前判定"是技术限制,只是没写为什么;用户的诉求是直接用**已经算好的账号门槛**(不是重新建一个基于曲线陡峭度的预测模型,那个需要历史数据校准、现在没有)去检查观察中视频的当前数据,只要达标立刻判定,不用等满7天。
    - `judge_account()` 新增一段:每次判定时,除了处理已经"安全"的样本(archived/promoted/满7天的watching),**还会额外把当前所有"观察中"的视频(不管年龄)拿现有门槛去检查一遍**——达标立刻判定爆款(新计数器 `promoted_early_count`),不达标就不动,继续观察或者等满7天走"毕业"那条路。
    - 抽出了两个共享小函数(`_evaluate_hit_channels`、`_promote_hit`)避免"判定通道"这段逻辑在两个地方各写一份、以后容易漂移。
    - 把之前那个"不能提前判定"的测试**反过来改了**(先明确写清楚为什么反过来,不是偷偷改掉装作没发生过),另外新加一个"没达标就不该提前判"的对照测试。全部284个测试跑过,两个权威闸门仍绿。
    - `BUSINESS_RULE_CATALOG.yaml` BR-HIT-001 和 `REQUIREMENT_CODE_TRACEABILITY.yaml` 都补了新的修订记录,如实写清楚:这条规则的"不能提前判"是从旧文档搬来、没有独立证据的假设,现在被用户直接推翻,改成"能提前判",且这不是新建一个预测模型,是把已经验证过的判定标准提前用而已。
28. **用户追问下去,指出27条做的还不够**:第27条只是拿"成熟视频的门槛"(中位数×3)去比还没满7天的新视频,这本身就不对等——一条视频才第2天,数据天然就比第7天少,拿这个去够"成熟门槛"基本上只有真的爆炸性的视频才够得着。用户要的是:拿这个账号自己的视频,在**同一个天数**积累的历史数据,算出"第N天通常能长到多少"这么一个参照,新视频拿自己当前第几天的数据去跟"账号自己第几天的历史中位数"比,不是跟成熟门槛比。而且这个倍数用户明确说不能是3倍(会把体量大的账号门槛拉得太高),改成**2倍**——这跟当年定 `excess_threshold=3.0` 的道理一样,只是数字更低。
    - 新增 `_account_day_reference_median()`:拿 `video_checks` 这张历史表,按"这条视频的检查时间减去发布时间,正好等于N天"筛出同一账号其他视频在第N天时的点赞数,取中位数;**如果这一天的历史记录不够3条,直接返回"没有",不会用不够的数据硬凑一个参照**(不是返回0,是明确"没法用")。
    - 新增配置 `early_excess_threshold: 2.0`,专门给这条新通道用。
    - 观察中的视频,如果原有两条通道(点赞门槛、评论比例)都没达标,再多加这第三条通道判一次:当前点赞数是不是达到"账号自己同一天历史中位数 × 2"。达标就提前判定爆款(新计数器 `promoted_via_day_reference_count`),复用 `hit_channel='like_threshold'` 这个标签(没建新的枚举值,避免要去改真实生产库那张 `hits` 表已经建好的约束,风险更小)。
    - **老实说清楚现在的数据现实**:`video_checks` 这张表刚建、增量采集也还没真跑过,现在**没有真实的"第N天历史数据"**,这条新通道写好了、测试过了,但实际生效要等每日增量真的连续跑几天、攒够同一天数的历史记录才有得比——不是写完代码就能立刻在真实数据上看到效果。
    - 新写了2个测试(历史数据够、能提前判;历史数据不够、该按兵不动),`BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml` 都补了修订记录。全部286个测试跑过,两个权威闸门仍绿。
29. **用户紧接着指出:成熟视频那条门槛(`excess_threshold`)也要改成2倍,不是只有新通道**——原话"我他妈的说的成熟的视频也是2倍"。之前我理解错了,以为2倍只是给新的"按天参照"通道用,已经拿真实数据验证过的3倍不用动——这是我理解片面,用户明确要求全部改成2倍。
    - `excess_threshold` 从 3.0 改成 2.0(`config/settings.yaml`、`config/settings.example.yaml`、`BUSINESS_RULE_CATALOG.yaml` 的 `thresholds.excess_threshold` 三处同步改)。
    - **这次改动影响的是判定所有已归档/已判定爆款视频的主门槛公式,不只是新观察视频**,所以立刻用 `--rejudge-only` 对真实生产库重新判定一遍验证,不是改完就当完事:**28账号,总爆款数从198涨到245(+47),0个账号挂零,每账号爆款数1~17条(没有账号暴增到不正常的比例)**,`hit_channel` 分布从 `like_threshold=130/comment_like_ratio=48/both=20` 变成 `like_threshold=177/comment_like_ratio=43/both=25`——涨幅集中在点赞门槛通道,符合"门槛降低、点赞门槛更容易达标"这个预期,不是判定逻辑坏了。
    - `BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml` 都补了这次修订记录(含真实验证数字)。全部286个测试跑过,两个权威闸门仍绿。
30. **用户追问:测试这么多,为什么连"有没有按设计执行"这种基本问题都测不出来**——查了实际网上通行做法(ADR"供奉人"机制、文档当代码一样跑CI检查/linting)确认这类问题确实有正经解法,不是没法测。新增 `tests/validation/test_authoritative_docs_not_legacy.py`:任何被 `CLAUDE.md`/`AGENTS.md` 点名要读的权威设计文档,自动扫描有没有提到 `scripts/validation/clean_room_readiness.py` 里 `LEGACY_QUARANTINE_ROOTS` 那份已隔离旧路径清单——提到了就直接报错,不用等人发现。已验证过这道闸门真的管用(手动模拟一段引用旧路径的文本,闸门立刻报错)。
31. **用户看到"`BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml` 故意不检查"这句解释后炸了**——原话"别搞任何旧代码,一切按新设计的来"。用户不接受"这两份文件合法引用旧代码"这种例外,要求彻底清干净,不留任何例外。
    - 查了这两份文件里到底有多少处引用旧路径:`BUSINESS_RULE_CATALOG.yaml` 2处,`REQUIREMENT_CODE_TRACEABILITY.yaml` 57个条目里有104处(`legacy_files`/`legacy_functions` 这两个字段,专门存旧代码路径用的)。
    - **中途写脚本删除时出过一次真实事故**:第一版脚本逻辑写反了,直接把 `REQUIREMENT_CODE_TRACEABILITY.yaml` 改坏了(字段名被删掉、只剩列表项,YAML结构损坏)。**立刻用 `git checkout --` 撤回**(还没提交,能完整恢复),重写脚本、这次先输出到临时文件、用 `yaml.safe_load` 验证解析正常+条目数没少(57条都还在)才正式替换,不是改完就当完事。
    - 干净删除了两份文件里全部旧路径引用(`legacy_files`/`legacy_functions` 整段删除,另外2处零散引用改写成不点名旧路径的说法)。`test_authoritative_docs_not_legacy.py` 的例外名单也去掉了,现在这两份文件也一起接受检查,没有例外。
    - 这次改动顺带把我自己新写的检查脚本也检出一个假阳性(脚本里为了举例写的旧路径字符串,被另一个闸门当成"生产代码引用旧路径"报错)——按现有惯例加进白名单,不是放松检测。
    - 全部287个测试跑过,两个权威闸门仍绿,`legacy_removal_gate`/`clean_room_readiness` 都确认干净。

32. **用户发现仓库根目录一直有这份总控文档原始 docx**(`爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`),质问"为什么之前 Codex 没把文档当设计来源"——查证后确认这份文档是 `BUSINESS_RULE_CATALOG.yaml` 里 BR-HIT-001 等大多数规则的 `source_document` 引用来源,是真实存在的原始设计,不是编的。逐章跟用户对照文档原文(第8/9/10/14/15章 + 附录A),把这周之前那套"3倍/2倍混用、观察池、baseline_min_samples 30/10、P90封顶"的设计,跟文档原文一条条核对、逐条拍板定稿:
    - **D0-D7 改成"发现批次"锚定**(不是发布日历天):D0=系统第一次发现这条视频的那次采集批次。
    - **首次接触分三类**(历史成熟样本/过渡样本/正式新视频),过渡样本存两次快照(现在一次+满7天一次),不再是"首采不满7天被永久排除"的bug修复思路。
    - **成熟历史基线**:最近90天、最多50条、滚动窗口。**正式D基线**:20条启用(用户修正文档原文"20条既是启用门槛又是计算窗口"的表述,改成"20启用、50计算窗口对齐成熟历史基线")。
    - **触发规则从混用倍率统一成**:单指标(点赞/评论/收藏/分享各自)2.0倍、多指标组合(4项任2项)1.6倍、评论/点赞比例(这周新增,文档没有,用户明确保留)。冷启动D7粗略参照倍率也对齐同一套数字,不用文档原文的2.5/2.0/3.0。
    - **置顶判断整个删除**——用户推导出"只要年龄过滤用真实发布时间,置顶不置顶不影响基线资格",验证成立,不再单独识别置顶。
    - **P90封顶+绝对值下限公式、watching/archived/promoted状态机+退回/graduated逻辑、baseline_min_samples 30/10方案,全部按用户"没点名保留的一律不留"的指示删除**。
    - **自营P基线保留**(不是文档字面写的用途,是因为经验迭代需要跟自己历史比),但怎么用还没定,明确留给经验库层设计,不在本次瞎猜。
    - 顺带发现文档里还有一整块没讨论过的候选处理设计(第15章"双阶段处理"+候选记录字段+"首次触发后永久保留不删除"的明文规定、第16章评论采集两阶段策略、第17章对照/低表现样本的完整规则)——BR-HIT-002 从一句话占位补全,新增 BR-HIT-005(候选记录字段+永久保留,比之前"文档没提退回"的说法更准更硬——文档原文是"明确规定不能退回")、BR-HIT-006(五级分级处理+深度分析资格)、BR-HIT-007(评论采集purpose标记策略)。BR-HIT-006/007 连同 BR-HIT-002 的完整实现本次**有意留白**,不是漏掉——这三块都要挂在评论采集流水线上,而这条流水线现在还没建,现在硬做就是无米之炊。
    - 把这份文档整个转成纯文字存进仓库根目录(8423行),以后不用每次重新转换 docx。
    - 新增 `TECHNICAL_MANUAL.md`(给人看的运行手册骨架)+ `tests/validation/test_technical_manual_sync.py`(模块建好没写手册会报错)+ CLAUDE.md/AGENTS.md 新硬规则(模块建好必须同commit写手册,不能留白拖以后)。
33. **用户拍板"现在可以重构代码了"**——把上面这套最终设计整个重写进 `scripts/core/business_data/`(schema、`run_competitor_registration_full.py` 全部重写,不是增量patch):
    - `competitor_accounts_schema.sqlite.sql`:`competitor_videos` 去掉 `is_pinned`/`status`,加 `first_contact_category`/`discovery_delay_hours`/`mature_snapshot_taken_at`/`tracking_completed` + BR-HIT-005 候选字段(`first_trigger_observation`/`first_trigger_at`/`trigger_rules`/`peak_observation`/`baseline_mode`/`judgment_confidence`);`video_checks` 加 `discovery_batch_index`;`baselines` 改成 `baseline_mode`+`observation_point` 区分四种基线;`hits.hit_channel` 从固定枚举改成逗号拼接的自由文本(6通道组合太多,不适合再用小枚举)。
    - `config/settings.yaml`/`config/settings.example.yaml` 的 `hit_detection` 整块重写,旧 key(`excess_threshold`/`hit_floor_absolute_like_count`/`early_excess_threshold`/`baseline_min_samples`/`baseline_window_days`/`first_crawl_fetch_buffer` 的旧含义)全部废弃,换成新 key 集合(`single_metric_excess_threshold`/`multi_indicator_excess_threshold`/`unified_min_samples`/`formal_baseline_activation_min_samples`/`formal_baseline_computation_window` 等)。
    - `BUSINESS_RULE_CATALOG.yaml` 的 `BR-COLLECT-001/002`、`BR-BASELINE-001/002/003` 同步改写(标注被 BR-HIT-001 取代的部分,不再重复维护两份口径)。
    - 新写 34 个测试(`tests/core/test_competitor_registration_full.py` 整个替换,不是增量),覆盖三类分类、两种基线的启用门槛/窗口/滚动、六通道触发、候选永久保留、执行契约、入口冒烟测试。`tests/validation/test_business_rule_traceability.py` 同步重写(旧的 `excess_threshold`/`hit_floor_absolute_like_count` 断言全部替换)。
    - `REQUIREMENT_CODE_TRACEABILITY.yaml`:BR-HIT-002/006/007 从 `exact_match` 降级成 `missing_in_code`(如实标记未建,不是隐瞒);BR-HIT-004(旧的"分享/评论比"规则)标记为"被取代",因为新设计里分享数已经变成独立的 `share_anomaly` 通道,不再是这条规则字面描述的东西。
    - **验证结果**:`tests/core`(246个测试)、`tests/validation`(43个测试)全绿;`verify_goal_v062_phase8_readiness.py` 仍 `ENGINEERING_READY`。
    - **明确留了一个没做的决定,没有擅自处理**:真实生产库 `data/formal/production_activation.sqlite3` 现有1253条视频还是旧表结构(`is_pinned`/`status` 等),这次重写完全没碰它——新代码和旧库结构对不上,需要专门的迁移/重建步骤才能把真实数据接上新代码,这属于"会修改生产业务数据库结构"的动作,按硬规则不能顺手做,需要用户先决定怎么处理旧数据(整表重建,还是想办法保留原始爬取字段迁移过来)。

34. **用户拍板"清空重建"（第一版理解）**——新写 `scripts/core/business_data/migrate_master_doc_realignment.py`(committed、有 `--confirm` 二次确认、跑之前先把整个DB文件备份成带时间戳的副本,不确认就拒绝执行),用小型合成旧库先验证过逻辑(`tests/core/test_migrate_master_doc_realignment.py`)才对真实库动手:重新分类了旧的1253条视频(1148历史成熟+105过渡),`--rejudge-only` 后总命中96条(全靠评论/点赞比例通道,倍数异常通道一条没触发——因为迁移进来的全是历史/过渡样本,没有D0-D7序列)。
35. **用户追问两点,都问到实处**：(1) "这些历史存量视频应该拿去跟历史成熟基线比啊"——查代码发现是真漏洞:倍数异常判断只接在了"正式D基线"路径上,历史成熟/过渡样本完全没走这条检查,只有评论/点赞比例在工作。**已修复**:`judge_account()` 现在对每条历史成熟/过渡样本视频,也会拿它自己的数据去跟成熟历史基线比(用"冷启动粗略"那套2.0/1.6倍阈值),命中了标记"粗略信号"(不自动变成正式爆款,这是文档原文规定的)。(2) "你迁移了1253条视频，这些视频不是按新方案采集的，你清空了什么"——如实承认:上一步做的是"重新分类旧数据",不是"清空后用新方案重新采集",两者是真的不一样的事。用户明确要求"真正的清空重建,现在就执行":
    - `migrate_master_doc_realignment.py` 加了 `--mode wipe`(备份后彻底清空 `competitor_videos`/`baselines`/`hits`/`video_checks`,`competitor_accounts` 不动,注册本身是幂等的),配了测试。
    - 真实执行:先备份(`production_activation_pre_true_clean_rebuild_20260707T125656Z.sqlite3`),清空后对真实28个对标账号跑了一次**真实联网抓取**(`run_full_registration`,这个动作有 `PHASE_8_REAL_NEW_DATA_APPROVAL.yaml` 既有批准覆盖,加上用户本轮明确说"现在就执行"),28个账号全部成功、0失败。
    - **真实结果**:1497条视频(比旧数据集多,因为新配置 `first_crawl_max_notes=50+缓冲5=55`,比旧配置抓得多),1372条历史成熟+125条过渡+0条正式新视频(符合预期——首次注册抓的都是存量,不是"正式跟踪后日常发现"的新视频)。总命中112条(评论/点赞比例通道),历史高信号粗略信号613条。同样4个账号(万物电台、小时好食禄、小椰子专栏、食侠客)暂时挂零。
    - 最终验证:`tests/core` 249个测试、`tests/validation` 43个测试全绿,`verify_goal_v062_phase8_readiness.py` 仍 `ENGINEERING_READY`。
36. **用户质问:"什么正式爆款,粗略爆款,我什么时候用了这些名字分类的?"**——`judgment_confidence` 那两个词是我从代码字段直接搬去跟用户讲的,用户没要求这套分层说法,只想知道机制本身:除了评论/点赞比例,还有一种是"这条视频自己的数据跟账号历史中位数比,任一项2倍或任两项1.6倍"。之后用户要求现场测试调参(3倍单指标/2倍多指标/点赞绝对下限2000/小账号改用P90当门槛、标清楚走哪条通道),我先写了一个**只读**对比脚本跑真实数据给用户看数字(不碰数据库),用户确认"符合预期,可以定下来,但要标注清楚满足哪些条件"后正式落地:
    - `cold_start_d7_single_metric_threshold`/`cold_start_d7_multi_indicator_threshold` 从2.0/1.6改成3.0/2.0(只改成熟历史/粗略这条通道,正式D通道不变——刻意设计成"证据越弱、门槛越高"这个非对称,不是笔误)。
    - 新增 `mature_history_absolute_like_floor=2000`(点赞数硬门槛,不管是哪个指标触发的倍数异常都要点赞过这条线才算,评论/点赞比例通道不受影响)。
    - 新增 `small_account_p90_percentile=0.9`:账号历史中位数×3倍都够不到2000的,判定为"小账号",改用自己历史点赞数据的P90当门槛,命中标注 `p90_small_account:like=X>=p90:Y`,不跟正常通道混在一起。
    - **所有命中原因从裸规则名改成带具体数值的标注**(如 `like_anomaly:3.24x`、`multi_indicator:comment_anomaly=2.10x;collect_anomaly=2.30x`、`comment_like_ratio:0.250`),按用户要求"标注清楚满足哪些条件"。
    - 新写3个测试(小账号P90正确区分普通视频和真标兵;绝对下限能拦住只靠非点赞指标触发的视频;正式D通道multi_indicator标注不跟独立like_anomaly混淆),`BUSINESS_RULE_CATALOG.yaml` 补了完整修订记录。
37. **锁定新参数后,重跑判定发现:真实数据库里已有的644条命中还是旧参数(2.0/1.6)判出来的**——因为"命中永久保留"这条规则本来就设计成不会自动撤销,单纯重跑 `--rejudge-only` 不会把旧参数判的命中换成新参数的结果。用户一开始因为"又要重判"而不满("还重判什么,前面不是重判过了?"),说清楚这不是重新问要不要用新参数(那已经定了),是提醒"永久保留"这条规则保护的是"数据正常波动",不是保护"已经被换掉的旧判定标准"本身。用户随即明确要求"清空hits/baselines表,用新参数重判":
    - 新增 `migrate_master_doc_realignment.py --mode reset_judgement`:只清 `hits`/`baselines` 表和 `competitor_videos` 里的候选字段(`first_trigger_at`等),**不碰原始爬取数据**,不需要重新爬取。
    - 这个动作(对真实库执行DELETE)第一次被 Claude Code 自动权限分类器拦下——判定这是"这次会话里现场发明的动作,用户原话没有明确点名删hits/baselines"，要求用户用更明确的话重新确认。用户照做后正式执行。
    - **真实最终结果**:先备份(`production_activation_pre_judgement_reset_20260707T194617Z.sqlite3`),清空后重判,总命中480条(正式112条评论/点赞比例通道不变+粗略368条历史高信号通道,其中27条是走P90小账号通道)。仍然3个账号(万物电台、小时好食禄、小椰子专栏)挂零——食侠客这次转正了。
    - 最终验证:`tests/core` 252个测试、`tests/validation` 43个测试全绿,`verify_goal_v062_phase8_readiness.py` 仍 `ENGINEERING_READY`。

## 下一步该干嘛

- **要让倍数异常通道(点赞/评论/收藏/分享 vs 正式D基线)真正工作,必须让 `run_daily_incremental()` 对着这28个真实账号连续跑起来**——现在的真实数据集里 `formal_new_videos` 还是0,因为这次的"真正清空重建"抓的是首次注册的存量快照,不是"正式跟踪后日常发现"的新视频。账号要攒够20条走完D0-D7的新发现视频,这条通道才会启用。这是下一步最值得做的事,不是可选项——但这也是真实联网抓取,要不要现在就开始按日调度,需要用户明确同意。
- **BR-HIT-002(对照/低表现样本)/BR-HIT-006(五级处理+深度分析资格)/BR-HIT-007(评论采集)三块有意留白**——都要挂在评论采集流水线上,现在这条流水线还没建,建起来之后再回头做这三块,不是忘了。
- **业务表(观察池/爆款库/候选池等完整业务视图)仍未建**——当前只有 `scripts/core/business_data/` 这一个竞品账号+首采+基线/爆款的切片,不是完整业务层。切片本身现在验证得比较扎实了(端到端测试、执行器测试、README 都补齐了),下一步如果要继续业务开发,先看这个切片能不能直接扩展,不要另起炉灶。
- **本地监控面板**(`scripts/monitor/`,双击根目录 `启动监控面板.bat`)已就绪,可用来看 job/状态机/审计日志——业务数据面板还没做,等业务表长出来再说。
- **飞书集成**在上次 legacy removal 里被整体删除,还没重建,重建前先确认是否真的现在需要。
- production schedules 仍是 disabled 状态,GPT 切换未开始——这两项都不是当前 GOAL 的阻塞项,是未来用户主动触发的手动激活项(见 `PHASE_8_AUTHORIZATION_GATE_STATUS.yaml` 的 `future_user_initiated_activation`)。
