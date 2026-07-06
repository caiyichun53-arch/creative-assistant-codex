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

$ python -m unittest tests.validation.test_legacy_removal_gate
Ran 2 tests in 0.291s
OK

$ python -m unittest <255 个已知测试模块列表,见本文件 git 历史或直接问上一个执行者要清单>
Ran 255 tests in 15.302s
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

## 下一步该干嘛

- **业务表(观察池/爆款库/候选池等完整业务视图)仍未建**——当前只有 `scripts/core/business_data/` 这一个竞品账号+首采+基线/爆款的切片,不是完整业务层。切片本身现在验证得比较扎实了(端到端测试、执行器测试、README 都补齐了),下一步如果要继续业务开发,先看这个切片能不能直接扩展,不要另起炉灶。
- **本地监控面板**(`scripts/monitor/`,双击根目录 `启动监控面板.bat`)已就绪,可用来看 job/状态机/审计日志——业务数据面板还没做,等业务表长出来再说。
- **飞书集成**在上次 legacy removal 里被整体删除,还没重建,重建前先确认是否真的现在需要。
- production schedules 仍是 disabled 状态,GPT 切换未开始——这两项都不是当前 GOAL 的阻塞项,是未来用户主动触发的手动激活项(见 `PHASE_8_AUTHORIZATION_GATE_STATUS.yaml` 的 `future_user_initiated_activation`)。
