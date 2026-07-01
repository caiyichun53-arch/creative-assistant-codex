# V0.6.2 RC1 External Live Gate Plan

status: `PLAN_ONLY_NO_LIVE_EXECUTION`

baseline:

- release branch: `release/v0.6.2-rc1`
- validation branch: `validation/v0.6.2-live-gates`
- RC tag: `v0.6.2-rc1`
- GOAL-12 status: `GOAL-12_COMPLETE_WITH_EXTERNAL_LIVE_GATES`

This is not GOAL-13. This plan does not modify business behavior, migrations,
tests or production configuration. It defines how to prepare and run external
live gates after explicit approval, test accounts and isolated environments are
available.

## Global Safety Rules

- Do not connect to a production database.
- Do not use production accounts or production credentials.
- Do not publish real content.
- Do not mutate platform production data.
- Do not start the full production deployment.
- Do not submit or store secrets in the repository.
- Do not delete old code or perform LEGACY retirement work.
- Run every gate against an isolated validation database and disposable logs.
- Stop the gate immediately on unexpected external writes, credential leakage,
  idempotency conflict, uncontrolled retry loops or writes outside the evidence
  directory.

## Evidence Convention

Use this evidence root for all future live-gate runs:

`validation_artifacts/v0.6.2-live-gates/<gate_id>/<YYYYMMDD-HHMMSS>/`

Each run should save:

- `run_manifest.json`
- sanitized environment summary with secret values redacted
- command transcript
- adapter request/response summaries with tokens redacted
- isolated database backup or checksum
- rollback log

## Gates

### GATE-HERMES-REAL-HOST

- gate_id: `GATE-HERMES-REAL-HOST`
- 验证目标: prove a real host invocation can enter Hermes, map to Core, create
  or transition state, enqueue a response and replay safely without direct Core
  writes outside the Hermes boundary.
- 当前相关Adapter或入口:
  - `scripts/core/hermes/goal11_host_binding.py`
  - `HermesInboundMessage`
  - `HermesCoreBridge`
  - `FeishuThinBinding`
  - `FeishuResponseDispatcher`
  - `RuntimeHost` job kind `outbox.dispatch`
- 所需测试账号、凭证或环境:
  - non-production host endpoint or local host runner
  - test actor identity
  - isolated SQLite or disposable PostgreSQL database
  - disposable reply channel
- 是否已有配置样例: partial. `config/settings.example.yaml` covers local paths
  and adapter categories, but no real Hermes host live configuration is present.
- 使用的隔离数据库: `data/validation/live_gates/v0.6.2/hermes_host.sqlite` or
  disposable database `creation_v062_live_gate_hermes`.
- 可能产生的外部副作用:
  - host callback receipt
  - reply outbox dispatch to a test channel if paired with Feishu
  - audit/log records in the isolated database
- 必须禁止的操作:
  - production Core database writes
  - live user/channel replies
  - command types beyond approved create/transition shadow commands
  - bypassing `HermesCoreBridge` to call Core directly
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_hermes_host_gate.py --db <isolated-db> --host test`
  - Existing fixture reference: `python scripts/core/hermes/verify_goal_11.py`
- fixture/fake阶段已验证的内容:
  - Feishu binding maps only to Hermes message.
  - duplicate events replay without duplicate state or response.
  - fake response dispatch retries without duplicate send.
  - GOAL-12 staging exercised Hermes to Core to response outbox.
- live阶段仍需验证的内容:
  - real host envelope shape
  - external event id stability
  - auth boundary and actor mapping
  - response dispatch behavior against a disposable channel
  - clock, retry and idempotency behavior outside fake fixtures
- 通过条件:
  - exactly one Core command receipt per unique external event
  - replay returns the original result
  - response outbox is created once and dispatched once
  - all evidence is captured under the gate evidence root
- 失败停止条件:
  - any write to production database
  - duplicate command receipt for the same idempotency key
  - unexpected host command type
  - response to non-test channel
- 回滚方式:
  - revoke test host token
  - stop the validation runner
  - archive isolated database and logs
  - discard isolated database after evidence review
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-HERMES-REAL-HOST/`
- 是否必须在正式上线前完成: yes

### GATE-FEISHU-THIN-BINDING

- gate_id: `GATE-FEISHU-THIN-BINDING`
- 验证目标: prove the Feishu thin binding receives a test event, creates a
  Hermes message, sends a response only to a disposable test chat and preserves
  idempotency.
- 当前相关Adapter或入口:
  - `scripts/core/hermes/goal11_host_binding.py`
  - `FeishuBindingEvent`
  - `FeishuThinBinding`
  - `FeishuResponseDispatcher`
  - legacy low-level client source: `scripts/feishu/client.py`
  - legacy push source: `scripts/feishu/push.py`
- 所需测试账号、凭证或环境:
  - Feishu test tenant
  - test app id and app secret
  - test bot added to a disposable chat
  - test event verification token/signature material if the host receives events
  - isolated database
- 是否已有配置样例: partial. `.env` keys are implied by `scripts/feishu/client.py`
  (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`), but no secret values
  may be committed.
- 使用的隔离数据库: `data/validation/live_gates/v0.6.2/feishu_binding.sqlite`
  or disposable database `creation_v062_live_gate_feishu`.
- 可能产生的外部副作用:
  - one or more messages in a disposable Feishu chat
  - Feishu API token issuance
  - Feishu app event delivery logs
- 必须禁止的操作:
  - sending to production chats
  - using production Feishu app credentials
  - modifying Feishu documents, sheets or official production data
  - relying on global `lark-cli` profiles for production identity
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_feishu_binding_gate.py --db <isolated-db> --chat <test-chat-id>`
  - Existing fixture reference: `python scripts/core/hermes/verify_goal_11.py`
- fixture/fake阶段已验证的内容:
  - binding validation for required `event_id`, `chat_id`, `sender_id` and command fields
  - duplicate event replay
  - fake response retry and idempotent send receipt
- live阶段仍需验证的内容:
  - Feishu event payload normalization
  - tenant token acquisition with test app credentials
  - API send behavior and returned message ids
  - event retry behavior from Feishu
  - permission scopes for message receive and reply
- 通过条件:
  - one disposable test event produces one Hermes/Core receipt
  - one response reaches only the test chat
  - repeated delivery replays without duplicate state or duplicate reply
  - all secret values are redacted from evidence
- 失败停止条件:
  - message delivered to a non-test chat
  - token or secret printed to logs
  - Feishu permission error that would require broad production scopes
  - duplicate reply on retry
- 回滚方式:
  - remove bot from test chat
  - revoke/rotate test app secret
  - disable event subscription
  - delete only disposable validation data after evidence capture
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-FEISHU-THIN-BINDING/`
- 是否必须在正式上线前完成: yes

### GATE-MODEL-PROVIDER

- gate_id: `GATE-MODEL-PROVIDER`
- 验证目标: prove a real non-production model route can run through
  `ModelGateway`, record a model run envelope, capture usage/cost metadata and
  stop safely on provider errors.
- 当前相关Adapter或入口:
  - `scripts/core/model_gateway/goal07_model_gateway.py`
  - `ModelGateway`
  - `ModelRoute`
  - `ModelProvider`
  - `ModelRunMaterializer`
  - `PortableSkillRunner`
- 所需测试账号、凭证或环境:
  - non-production model provider API key or test billing project
  - allowed low-risk test model
  - strict token and cost cap
  - isolated database
- 是否已有配置样例: partial. `config/settings.example.yaml` defines logical LLM
  aliases and routes, but V0.6.2 Core has no committed live provider adapter.
- 使用的隔离数据库: `data/validation/live_gates/v0.6.2/model_provider.sqlite`
  or disposable database `creation_v062_live_gate_model`.
- 可能产生的外部副作用:
  - billable model request
  - provider request logs
  - provider safety or abuse monitoring event
- 必须禁止的操作:
  - production API keys
  - prompts containing private user data or secrets
  - uncapped batch execution
  - model outputs being written as approved production content
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_model_provider_gate.py --db <isolated-db> --route <test-route> --max-requests 1`
  - Existing fixture reference: `python scripts/core/model_gateway/verify_goal_07.py`
- fixture/fake阶段已验证的内容:
  - route/provider lookup
  - provider mismatch rejection
  - failure envelope persistence
  - timeout handling
  - portable skill execution through the gateway contract
- live阶段仍需验证的内容:
  - real provider auth
  - provider request id capture
  - token usage and cost accounting
  - timeout behavior with real latency
  - redaction of prompts and responses in logs
- 通过条件:
  - one test request succeeds through `ModelGateway`
  - envelope records status, route, model, provider request id, usage and cost
  - failure path is demonstrated with a harmless invalid route or revoked test key
  - cost stays inside the approved cap
- 失败停止条件:
  - cost cap exceeded
  - provider key appears in logs
  - direct provider call bypasses `ModelGateway`
  - output is promoted to production artifacts
- 回滚方式:
  - revoke/rotate test provider key
  - delete provider-side test project if needed
  - archive isolated DB and command transcript
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-MODEL-PROVIDER/`
- 是否必须在正式上线前完成: yes

### GATE-ASR

- gate_id: `GATE-ASR`
- 验证目标: prove the local ASR route can transcribe controlled media through
  the existing deterministic ASR path and write only to isolated validation
  storage.
- 当前相关Adapter或入口:
  - `tools/asr/transcribe.py`
  - `tools/asr/asr_test.py`
  - `tools/asr/reverse_prep_worker.py`
  - settings keys under `reverse_engine`
  - legacy adapter source `scripts/collect/common.py::fetch_fresh_aweme`
- 所需测试账号、凭证或环境:
  - local ASR virtual environment
  - local SenseVoice and VAD model paths
  - ffmpeg
  - controlled test media or disposable platform test video
  - isolated copy of `data/creation.db` containing only test hit rows
- 是否已有配置样例: yes, partial. `config/settings.example.yaml` includes
  `reverse_engine.models_root`, `asr_model`, `vad_model` and
  `min_transcript_chars`.
- 使用的隔离数据库: isolated workspace copy with
  `data/validation/live_gates/v0.6.2/asr/creation.db`. The current ASR script
  hardcodes `data/creation.db`, so the live gate must run in a disposable
  validation workspace or use an explicitly approved validation harness.
- 可能产生的外部副作用:
  - if a platform URL is used, MediaCrawler may open a browser session and fetch
    fresh media/comment detail
  - local transcript files
  - local temporary media files
  - writes to the isolated `hits` rows
- 必须禁止的操作:
  - production database writes
  - bulk `--all-pending` against real hit rows
  - using real creator/private media without approval
  - retaining downloaded media after the run
- 推荐执行命令或入口:
  - Future live harness entry: `tools/asr/.venv/Scripts/python.exe scripts/validation/live_gates/run_asr_gate.py --isolated-workspace <path>`
  - Existing direct entry, only inside isolated workspace: `tools/asr/.venv/Scripts/python.exe tools/asr/transcribe.py --hit <test-hit-id>`
- fixture/fake阶段已验证的内容:
  - GOAL-12 did not exercise real ASR.
  - Legacy ASR path exists and is classified as adapter source, not formal Core state authority.
  - Local project checks have historically treated ASR venv availability as a runtime readiness item.
- live阶段仍需验证的内容:
  - ASR venv import readiness
  - model path resolution
  - ffmpeg extraction
  - transcript length/quality gate
  - no residual media files
  - no writes outside isolated validation DB/transcript directory
- 通过条件:
  - controlled sample transcribes above `min_transcript_chars`
  - transcript path is written only in isolated DB
  - temporary mp4/wav files are removed
  - run log records model paths as paths only, not secrets
- 失败停止条件:
  - script targets real `data/creation.db`
  - model download attempts unexpected network access
  - media download pulls non-test content
  - transcript is too short or empty
- 回滚方式:
  - delete disposable workspace after archiving evidence
  - remove local temporary media
  - restore isolated DB from pre-run copy
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-ASR/`
- 是否必须在正式上线前完成: yes

### GATE-SEARCH-PROVIDER

- gate_id: `GATE-SEARCH-PROVIDER`
- 验证目标: prove a real SearchProvider/Fetcher/Extractor chain can run formal
  topic-first research against allowed web sources and persist traceable
  evidence without using blocked video-platform sources.
- 当前相关Adapter或入口:
  - `scripts/core/research/goal06_formal_research.py`
  - `SearchProvider`
  - `ResearchFetcher`
  - `ResearchExtractor`
  - `FormalResearchService`
  - `make_formal_research_runtime_handler`
- 所需测试账号、凭证或环境:
  - non-production search API key or approved search provider test project
  - allowed fetcher user-agent
  - extractor runtime
  - isolated database
  - test query list with no private data
- 是否已有配置样例: no committed live provider config. GOAL-06 currently defines
  the protocol and fake fixture implementation.
- 使用的隔离数据库:
  `data/validation/live_gates/v0.6.2/search_provider.sqlite` or disposable
  database `creation_v062_live_gate_search`.
- 可能产生的外部副作用:
  - search API usage/billing
  - web page fetch logs
  - rate-limit events
- 必须禁止的操作:
  - blocked source types: video, audio, ASR, comment, video analysis
  - blocked platforms: Douyin, TikTok, Bilibili, Xiaohongshu, Kuaishou
  - scraping behind login or paywalls
  - writing fetched data into production stores
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_search_provider_gate.py --db <isolated-db> --query-file <test-queries>`
  - Existing fixture reference: `python scripts/core/research/verify_goal_06.py`
- fixture/fake阶段已验证的内容:
  - query validation
  - blocked source/platform rejection
  - source/fetch/evidence trace materialization
  - workflow entry for topic-first research
- live阶段仍需验证的内容:
  - real provider auth and rate limits
  - fetcher timeout/retry behavior
  - extractor output quality and locator accuracy
  - blocked platform enforcement with real result data
- 通过条件:
  - at least one allowed source is searched, fetched and extracted
  - all persisted source/fetch/evidence versions are linked
  - blocked URL sample is rejected
  - no production stores are touched
- 失败停止条件:
  - blocked platform enters persisted evidence
  - provider key appears in logs
  - fetcher follows login-only or private pages
  - result count/rate exceeds approved cap
- 回滚方式:
  - revoke/rotate test search key
  - archive isolated DB and provider usage summary
  - delete disposable fetched raw cache after evidence review
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-SEARCH-PROVIDER/`
- 是否必须在正式上线前完成: yes

### GATE-EXTERNAL-COLLECTOR-ADAPTER

- gate_id: `GATE-EXTERNAL-COLLECTOR-ADAPTER`
- 验证目标: prove external platform collection can run only as an adapter source
  in an isolated validation store, without direct authority over formal
  V0.6.2 Core state.
- 当前相关Adapter或入口:
  - `scripts/collect/register_competitor.py`
  - `scripts/collect/crawl_competitors.py`
  - `scripts/collect/common.py`
  - `vendor/MediaCrawler`
  - language/material collectors listed in `LEGACY_RETIREMENT_REPORT.md`
- 所需测试账号、凭证或环境:
  - disposable platform browsing profile
  - test competitor account or public sample URL
  - isolated legacy-format SQLite database
  - MediaCrawler runtime and browser profile
- 是否已有配置样例: partial. `config/settings.example.yaml` and legacy config
  examples exist, but live account/session material must stay local and uncommitted.
- 使用的隔离数据库:
  `data/validation/live_gates/v0.6.2/external_collector/creation.db`.
- 可能产生的外部副作用:
  - platform page requests
  - browser login/session activity
  - rate-limit or anti-bot events
  - local raw JSONL archive files
- 必须禁止的操作:
  - likes, follows, comments, DMs or publishing
  - production account login
  - production Core writes
  - deleting legacy code during validation
  - collecting private or non-test material
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_external_collector_gate.py --isolated-workspace <path> --sample-url <url>`
  - Existing legacy entry, only inside isolated workspace: `python scripts/collect/crawl_competitors.py --mode daily`
- fixture/fake阶段已验证的内容:
  - GOAL-12 Core does not import legacy collector modules.
  - Retirement audit classifies collector families as adapter source, not
    safe-to-remove or production Core entrypoints.
- live阶段仍需验证的内容:
  - MediaCrawler runtime readiness
  - browser profile isolation
  - rate-limit behavior
  - data normalization into adapter output
  - no direct formal Core writes
- 通过条件:
  - one controlled public sample is collected into isolated raw/legacy store
  - no formal Core database receives writes
  - adapter output is reproducible enough for a future wrapper contract
  - browser/session artifacts stay under disposable validation paths
- 失败停止条件:
  - platform account mutation occurs
  - production browser profile is used
  - formal Core database is opened for writing
  - rate-limit/captcha indicates unsafe continuation
- 回滚方式:
  - close browser and delete disposable profile
  - delete isolated raw archives after evidence capture if approved
  - revoke test session
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-EXTERNAL-COLLECTOR-ADAPTER/`
- 是否必须在正式上线前完成: yes

### GATE-SHADOW-E2E

- gate_id: `GATE-SHADOW-E2E`
- 验证目标: run one full external-shadow path with test inputs and real
  providers/adapters, while keeping all outputs in shadow storage and preventing
  publication or production data mutation.
- 当前相关Adapter或入口:
  - `scripts/core/staging/verify_goal_12.py` as fixture reference
  - `HermesCoreBridge`
  - `RuntimeHost`
  - `FormalResearchService`
  - `ModelGateway`
  - `ProductionVersionChainMaterializer`
  - candidate external adapters after their individual gates pass
- 所需测试账号、凭证或环境:
  - isolated database
  - disposable Hermes/Feishu test channel
  - test model provider route
  - approved SearchProvider test key
  - optional ASR/collector test fixtures
  - explicit shadow-mode publication sink
- 是否已有配置样例: partial. GOAL-12 staging fixture exists; no committed live
  shadow harness exists.
- 使用的隔离数据库:
  `data/validation/live_gates/v0.6.2/shadow_e2e.sqlite` or disposable database
  `creation_v062_live_gate_shadow`.
- 可能产生的外部副作用:
  - test Feishu messages
  - search/model API usage
  - platform read-only requests if collector/ASR URL path is included
  - local shadow artifacts
- 必须禁止的操作:
  - real publish
  - production account/channel writes
  - preference promotion into durable production experience
  - deleting validation or legacy assets before review
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_shadow_e2e_gate.py --db <isolated-db> --shadow-only`
  - Existing fixture reference: `python scripts/core/staging/verify_goal_12.py`
- fixture/fake阶段已验证的内容:
  - end-to-end staging path materialized from Hermes to Core, research,
    model, production chain, review, approval, publication capture, experiment,
    proposal and correction.
  - replay, stale-basis rejection, fault rollback, backup/restore, clean-room
    deletion, forced interruption/resume and continuous-run fixture passed.
- live阶段仍需验证的内容:
  - real external providers can be substituted without breaking contracts
  - shadow publication capture cannot publish externally
  - evidence and logs remain complete across provider calls
  - live failures stop before promotion
- 通过条件:
  - one shadow topic path completes with real external test providers
  - no production data or platform content is modified
  - every external call is linked to trace evidence
  - final artifact remains marked shadow-only
- 失败停止条件:
  - any real publish attempt
  - unapproved external service call
  - missing trace link for a provider result
  - preference or experience promotion outside validation store
- 回滚方式:
  - disable all test credentials
  - archive isolated DB and logs
  - remove shadow artifacts only after review approval
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-SHADOW-E2E/`
- 是否必须在正式上线前完成: yes

### GATE-CONTINUOUS-FAULT-RECOVERY

- gate_id: `GATE-CONTINUOUS-FAULT-RECOVERY`
- 验证目标: prove the live validation runtime can operate continuously with
  bounded retries, recover expired leases, preserve idempotency and stop safely
  under injected external failures.
- 当前相关Adapter或入口:
  - `scripts/core/scheduler/goal03_scheduler.py`
  - `scripts/core/runtime/goal04_runtime_host.py`
  - `RuntimeHost.run_once`
  - `RuntimeHost.run_batch`
  - GOAL-12 forced interruption and continuous-run fixture
- 所需测试账号、凭证或环境:
  - isolated database
  - validation worker identity
  - disposable test credentials for providers used in the run
  - controlled fault injection switches at provider/test account layer
  - agreed run duration and max cost/request budget
- 是否已有配置样例: partial. Scheduler/runtime fixtures exist; no production-duration
  live validation config is committed.
- 使用的隔离数据库:
  `data/validation/live_gates/v0.6.2/continuous_fault_recovery.sqlite` or
  disposable database `creation_v062_live_gate_continuous`.
- 可能产生的外部副作用:
  - repeated test-channel messages
  - repeated test provider calls
  - provider rate-limit events
  - local logs and database growth
- 必须禁止的操作:
  - full production deployment
  - unbounded loops
  - real production credentials
  - automatic promotion of generated output
  - destructive cleanup while the run is active
- 推荐执行命令或入口:
  - Future live harness entry: `python scripts/validation/live_gates/run_continuous_fault_recovery_gate.py --db <isolated-db> --duration-minutes <n> --max-external-calls <n>`
  - Existing fixture reference: `python scripts/core/staging/verify_goal_12.py`
- fixture/fake阶段已验证的内容:
  - expired lease recovery with `FakeClock`
  - five deterministic outbox jobs through continuous-run fixture
  - handler failure retry path
  - clean-room deletion after validation assets are removed
- live阶段仍需验证的内容:
  - real wall-clock lease expiry
  - retry behavior under provider/network failures
  - external rate-limit handling
  - worker restart/resume from isolated database
  - bounded continuous run under real process supervision
- 通过条件:
  - run completes agreed duration or job count
  - all retries remain within max attempts
  - no duplicate external side effects after restart/retry
  - stop command halts worker without corrupting isolated DB
- 失败停止条件:
  - unbounded retry loop
  - duplicate external message or provider action
  - cost/request cap exceeded
  - worker writes outside isolated database/log root
- 回滚方式:
  - stop validation worker
  - revoke/rotate test credentials if a provider call escaped bounds
  - restore isolated DB from pre-run backup
  - archive logs for review
- 证据和日志保存位置:
  - `validation_artifacts/v0.6.2-live-gates/GATE-CONTINUOUS-FAULT-RECOVERY/`
- 是否必须在正式上线前完成: yes
