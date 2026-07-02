# Undocumented Legacy Behavior - GOAL-ALIGNMENT-01

Legacy behavior listed here is not production authority. It must be either documented as target behavior, explicitly changed, or covered by a user decision before migration code starts.

| id | behavior | classification | business risk |
|---|---|---|---|
| ULB-001 | scripts/llm/call.py shells out to `claude -p` while config names codex routes. | conflict | Can change provider/model quality and loses ModelGateway envelope. |
| ULB-002 | judge_hits supplements sparse 90-day baseline to baseline_min_samples instead of simply skipping. | conflict | Can promote/suppress hits differently for young accounts. |
| ULB-003 | daily_topics has TOP_N_NEW=10 and TOP_N_CANDIDATES=8 display caps. | undocumented_legacy | Changes what user sees in daily topic list. |
| ULB-004 | daily_topics applies CLUSTER_BOOST=0.3 to ranking. | undocumented_legacy | Changes candidate order. |
| ULB-005 | reduce.py uses MAX_PER_PLAY=3 and SECTION_CAP=1200. | undocumented_legacy | Changes example density and token budget. |
| ULB-006 | reduce.py removed hook/style dimensions from generated outputs. | undocumented_legacy | Changes what experience is produced. |
| ULB-007 | dna.py intentionally excludes golden sentences to avoid AI flavor backflow. | exact_match | Preserve as business taste boundary. |
| ULB-008 | ASR transcribe writes transcript_path and reverse_status directly. | conflict | Bypasses Core state ownership. |
| ULB-009 | Feishu listener launches subprocesses and writes/dispatches outside Core. | conflict | Can advance state or send messages without formal receipts. |
| ULB-010 | legacy research.py uses model WebSearch-style prompt path rather than formal provider materialization. | conflict | Breaks formal research provenance. |
| ULB-011 | language_fuel collectors persist raw items/insights directly. | undocumented_legacy | Useful source behavior but target needs adapter/materializer split. |
| ULB-012 | scripts/db/migrate_*.py target data/creation.db directly. | conflict | Forbidden for formal migration rehearsal. |
| ULB-013 | settings route names differ from actual formal ModelGateway routes. | ambiguous | Could change model tier if migrated mechanically. |
| ULB-014 | Current DB has no <20 or =20 baseline fixture. | fixture_gap | Need synthetic/archived fixture before proving sparse account semantics. |
| ULB-015 | Current DB has no duplicate platform_item_id fixture. | fixture_gap | Need synthetic duplicate fixture for target dedupe proof. |

## Minimum Decision Items

1. Sparse baseline semantics: keep legacy supplement-to-30 behavior or skip/flag underpowered accounts.
2. Topic-first preparation: add target behavior before migration or keep hit-anchored MVP temporarily.
3. Reduce output dimensions: confirm hook/style removal remains intended for V0.6.2.
4. Third-domain gate: accept synthetic/config-only placeholder until a second/third real domain exists.
