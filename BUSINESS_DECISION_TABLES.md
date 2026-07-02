# Business Decision Tables - GOAL-ALIGNMENT-01

These tables are decision fixtures for migration. `Decision` describes the target business result; conflicts are explicit and must not be hidden inside implementation.

## Account Baseline Enablement

| Case | Inputs | Decision | State | Notes |
|---|---|---|---|---|
| Normal account | >=30 settled samples in rolling window | Compute median/P90 baseline | append baseline version | exact legacy evidence exists |
| Sparse recent window | <30 recent but historical stock exists | Conflict: legacy supplements to 30; target decision required | mark baseline method | affects hit threshold |
| Underpowered account | <20 usable samples | Do not silently judge; require evidence flag or skip | no promotion | current DB lacks fixture |
| New account first crawl | stock snapshot only | Archive videos and seed historical baseline | competitor_videos archived | not watching |

## Hit Judgement

| Case | Inputs | Decision | State | Notes |
|---|---|---|---|---|
| High performer | like_count >= max(median*3, P90) | promote hit | hit + source promoted | exact legacy math |
| Normal performer | between median and threshold | keep as baseline/control | no hit | fixture required |
| Low performer | below median/control | keep as archived/watching by age | no hit | fixture required |
| Missing baseline | no valid baseline | skip and record gap | no hit | prevents false promotion |
| Duplicate source | same competitor/platform_item_id | update existing source | no duplicate hit | target idempotency key required |

## Video Discovery And Follow-Up Timing

| Case | Inputs | Decision | State | Notes |
|---|---|---|---|---|
| First crawl stock | last_crawled_at empty | archive, do not observe | status=archived | includes young stock videos |
| Daily new video T+0..T+7 | publish_time within observe window | watch and snapshot | status=watching + video_checks | deterministic |
| Watching aged >7d | publish_time older than observe_days | archive to baseline material | status=archived | no LLM |
| publish_time missing | discovered by crawler but no publish time | keep source with uncertainty | needs target flag | avoid wrong T+n |

## Comment Collection Stage

| Case | Inputs | Decision | State | Notes |
|---|---|---|---|---|
| Promoted hit with fresh detail | comments available | filter, dedupe, store top comments | hit_comments | reverse-prep only |
| No comments | empty/blocked comments | DNA may proceed with explicit no-comment marker | no hit_comments | no fabricated resonance |
| Duplicate comment_id | same hit/comment_id | ignore duplicate | stable comment set | current table uses INSERT OR IGNORE |
| Formal research | comment evidence requested | reject as formal evidence | no formal source | structural boundary |

## Video Learning vs Formal Research Boundary

| Case | Video/DNA | Formal source | Decision |
|---|---|---|---|
| Source hit inspires angle | yes | optional | allowed as creative/reverse evidence |
| Factual claim needs support | no | yes | formal research required |
| Search wants Douyin/video platform | yes | no | rejected in FormalResearchService |
| Topic hard to research | tempting video fallback | absent | still no video platform path |

## Deep Analysis Eligibility

| Case | transcript | cluster | comments | Decision |
|---|---|---|---|---|
| representative hit | exists | null or self | any | eligible |
| duplicate/non-representative | exists | points to another hit | any | not batch-eligible |
| no transcript | missing | any | any | prep first |
| audit freeze | pending DNA | any | any | do not run batch during alignment |

## Experience Generation / Candidate / Publish

| Case | Evidence | Decision | State |
|---|---|---|---|
| Single DNA | one hit | source artifact only | not global rule |
| Multiple DNA reduce | domain sample set | domain route/examples | projection with source hashes |
| One human diff | edited draft | candidate example | not automatic rule |
| Day7 own result | publication data | validate/promote experience | long-cycle evidence |

## Skill Failure And Retry

| Case | Failure | Decision | State |
|---|---|---|---|
| deterministic checker failure | invalid input | fail command, no state advance | receipt/error |
| model provider failure | provider error | record envelope failure, no artifact current pointer | retry according to job policy |
| worker interruption | lease expires | recover and retry idempotently | attempt history |
| adapter partial output | malformed payload | reject before materialization | no business state write |

## Hermes Human Confirmation Boundary

| Case | Actor command | Decision |
|---|---|---|
| create/prepare shadow material | explicit test actor | allowed in validation only |
| select topic | user confirmation required | advances selected state |
| ready_to_publish/publish | explicit user confirmation required | no silent publish |
| external Feishu send | test chat/gate only | production send blocked until gate |

## Multi-Domain Config Selection

| Case | Domain config | Examples | Decision |
|---|---|---|---|
| existing 泛科普 | present | present/legacy | run with domain metadata |
| second real domain | absent in current DB | absent | fixture gap |
| third placeholder | synthetic/config-only | empty buckets | must prove no code fork |
| missing examples | empty | none | degrade gracefully, no cross-domain injection |
