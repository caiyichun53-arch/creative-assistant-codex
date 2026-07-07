# HANDOFF_STATE

> 活文档:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。不是一次性快照——`CURRENT_REPOSITORY_BASELINE.md`/`RELEASE_CANDIDATE_BASELINE.md` 才是那种一次性冻结记录,这份文件永远反映"现在"。

## 现状(2026-07-07,本次收工时)

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **当前 GOAL**:`GOAL-V0.6.2-PRODUCTION-COMPLETION-01`(见 `implementation_progress/GOAL-V0.6.2-PRODUCTION-COMPLETION-01.md`),工程状态 `completed`,production activation 已跑过一次受控真实数据 pilot(竞品账号注册+首采+基线/爆款判定)。
- **工作区**:本次收工前已把所有未提交改动(评论/点赞比率并集通道 + 本次的所有配套修改)提交成一个 checkpoint commit——收工时工作区应为干净状态,如果不是,说明规则被违反了,先处理这个再往下做。
- **写这份文件的人/时间**:Claude Code,2026-07-07。下次不管谁接手(Codex 额度恢复或 Claude Code 继续),先读这份文件,不用重新考古。

## 权威闸门真实输出(不是转述)

```
$ python scripts/core/staging/verify_goal_v062_phase8_readiness.py
status: ENGINEERING_READY

$ python -m unittest <31个已知模块,find tests -iname "test_*.py" 取得>
Ran 283 tests in 18.3s
OK

$ python -m scripts.core.business_data.run_competitor_registration_full --rejudge-only
summary: {accounts: 28, videos: 1253, promoted_videos: 198, baselines: 56, hits: 198}
run_id: competitor_registration_rejudge_20260706T213052Z
(直接查真实库确认:28 账号里 0 个爆款数为 0;财经不眠姐从 0 -> 1,经由新的评论/点赞通道;
 hits.hit_channel 分布:like_threshold=130, comment_like_ratio=48, both=20)
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
- **业务表(观察池/爆款库/候选池等完整业务视图)仍未建**——当前只有 `scripts/core/business_data/` 这一个竞品账号+首采+基线/爆款的切片,不是完整业务层。切片本身现在验证得比较扎实了(端到端测试、执行器测试、README 都补齐了),下一步如果要继续业务开发,先看这个切片能不能直接扩展,不要另起炉灶。
- **本地监控面板**(`scripts/monitor/`,双击根目录 `启动监控面板.bat`)已就绪,可用来看 job/状态机/审计日志——业务数据面板还没做,等业务表长出来再说。
- **飞书集成**在上次 legacy removal 里被整体删除,还没重建,重建前先确认是否真的现在需要。
- production schedules 仍是 disabled 状态,GPT 切换未开始——这两项都不是当前 GOAL 的阻塞项,是未来用户主动触发的手动激活项(见 `PHASE_8_AUTHORIZATION_GATE_STATUS.yaml` 的 `future_user_initiated_activation`)。
