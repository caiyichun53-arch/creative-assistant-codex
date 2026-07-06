# HANDOFF_STATE

> 活文档:每次收工(额度耗尽/告一段落)前必须更新这份文件,交给下一个接手的执行者(Codex 或 Claude Code)。不是一次性快照——`CURRENT_REPOSITORY_BASELINE.md`/`RELEASE_CANDIDATE_BASELINE.md` 才是那种一次性冻结记录,这份文件永远反映"现在"。

## 现状(2026-07-06,本次收工时)

- **分支**:`activation/goal-v0.6.2-production-activation-01`
- **当前 GOAL**:`GOAL-V0.6.2-PRODUCTION-COMPLETION-01`(见 `implementation_progress/GOAL-V0.6.2-PRODUCTION-COMPLETION-01.md`),工程状态 `completed`,production activation 已跑过一次受控真实数据 pilot(竞品账号注册+首采+基线/爆款判定)。
- **工作区**:本次收工前已把所有未提交改动(host 边界重构、business_data 竞品数据层、model_router、MediaCrawler 执行适配器、本次的修复)提交成一个 checkpoint commit——收工时工作区应为干净状态,如果不是,说明规则被违反了,先处理这个再往下做。
- **写这份文件的人/时间**:Claude Code,2026-07-06。下次不管谁接手(Codex 额度恢复或 Claude Code 继续),先读这份文件,不用重新考古。

## 权威闸门真实输出(不是转述)

```
$ python scripts/core/staging/verify_goal_v062_phase8_readiness.py
status: ENGINEERING_READY
failures: []

$ python -c "from scripts.validation.clean_room_readiness import run_audit; from pathlib import Path; print(run_audit(Path('.').resolve())['phase_2_safe_to_execute'])"
True

$ python -m unittest tests.validation.test_legacy_removal_gate tests.validation.test_business_rule_traceability
Ran 7 tests
OK

$ python -m unittest <29 个已知测试模块,见本文件 git 历史或直接问上一个执行者要清单>
Ran 261 tests in 25.3s
OK
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
15. **用户要求彻底清空重来**:清空 `data/formal/production_activation.sqlite3`,用修好的逻辑对 28 个账号跑了一次真实全新首采(经过环境预检:MediaCrawler venv/Playwright Chromium/抖音登录缓存都在;`--headless yes` 会覆盖 MediaCrawler 配置文件里 `HEADLESS=False` 的默认值,实际是无头跑的,不会弹窗——这点我一开始查漏了只看了静态配置,被用户纠正过)。后台跑完(约几分钟),直接查真实库核对过(不只信自报的 JSON):28 账号、840 视频全部是同一个 run_id(证明是干净单次采集,不是历史补丁)、52 条爆款、0 条残留不合格记录、两个权威闸门仍绿。**当前真实数据(2026-07-06 全新采集后)**:28 账号、840 视频、52 条爆款,全部诚实标 `evidence_status=insufficient_sample`(首采只有 30 条原始视频/账号,刨去置顶/年轻的天然凑不满 30 这个目标,不是新 bug,等每日增量采集把样本攒大后会自然转为 `sufficient`)。

## 下一步该干嘛

- **业务表(观察池/爆款库/候选池等完整业务视图)仍未建**——当前只有 `scripts/core/business_data/` 这一个竞品账号+首采+基线/爆款的切片,不是完整业务层。切片本身现在验证得比较扎实了(端到端测试、执行器测试、README 都补齐了),下一步如果要继续业务开发,先看这个切片能不能直接扩展,不要另起炉灶。
- **本地监控面板**(`scripts/monitor/`,双击根目录 `启动监控面板.bat`)已就绪,可用来看 job/状态机/审计日志——业务数据面板还没做,等业务表长出来再说。
- **飞书集成**在上次 legacy removal 里被整体删除,还没重建,重建前先确认是否真的现在需要。
- production schedules 仍是 disabled 状态,GPT 切换未开始——这两项都不是当前 GOAL 的阻塞项,是未来用户主动触发的手动激活项(见 `PHASE_8_AUTHORIZATION_GATE_STATUS.yaml` 的 `future_user_initiated_activation`)。
