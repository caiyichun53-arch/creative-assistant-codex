# V0.6.2 RC1 External Environment Requirements

status: `REQUIREMENTS_ONLY_NO_SECRETS`

Do not commit credentials, tokens, cookies, browser profiles, API keys, `.env`
files with real values, exported provider configs or platform session material.

## Required Environment Classes

### Isolated Database

Required for every gate.

- SQLite option: disposable database under `data/validation/live_gates/v0.6.2/`
- PostgreSQL option: disposable database named with the pattern
  `creation_v062_live_gate_<gate>`
- Must not point at production.
- Must be backed up before each run.
- Must be archived with evidence after each run.

### Hermes Test Host

Required for:

- `GATE-HERMES-REAL-HOST`
- `GATE-SHADOW-E2E`
- `GATE-CONTINUOUS-FAULT-RECOVERY`

Needed material:

- non-production host endpoint or local host runner
- test actor identity
- event id/correlation id policy
- disposable reply channel binding
- validation-only auth material

Do not use production host secrets or production user identities.

### Feishu Test Tenant

Required for:

- `GATE-FEISHU-THIN-BINDING`
- `GATE-SHADOW-E2E`
- `GATE-CONTINUOUS-FAULT-RECOVERY` if Feishu dispatch is included

Needed material:

- Feishu test tenant
- test app id
- test app secret
- disposable test chat id
- event verification/signature material if inbound events are enabled
- bot permissions limited to the test chat

Known config keys from current legacy client source:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_CHAT_ID`

Store real values outside Git only. Logs must redact all values.

### Model Provider Test Project

Required for:

- `GATE-MODEL-PROVIDER`
- `GATE-SHADOW-E2E`
- `GATE-CONTINUOUS-FAULT-RECOVERY` if model calls are included

Needed material:

- non-production API key
- provider/project id
- allowed model name
- approved max requests
- approved token and cost cap
- timeout and retry policy

The live adapter must enter through `ModelGateway`; direct provider calls are
not acceptable evidence for the Core gate.

### ASR Local Runtime

Required for:

- `GATE-ASR`
- `GATE-SHADOW-E2E` if source-hit transcription is included

Needed material:

- `tools/asr/.venv`
- `ffmpeg`
- local SenseVoice model path
- local VAD model path
- controlled test media or disposable platform test video
- isolated validation workspace because current ASR script uses
  `data/creation.db`

Config example keys exist under `reverse_engine` in `config/settings.example.yaml`.

### Search Provider Test Project

Required for:

- `GATE-SEARCH-PROVIDER`
- `GATE-SHADOW-E2E`
- `GATE-CONTINUOUS-FAULT-RECOVERY` if formal research is included

Needed material:

- non-production search API key or approved test project
- approved query file with no private data
- user-agent/contact string if required by fetcher policy
- request and rate cap
- allowed-domain policy
- blocked-platform test samples

Formal research must reject video/audio/comment/video-analysis sources and
blocked platforms.

### External Collection Test Environment

Required for:

- `GATE-EXTERNAL-COLLECTOR-ADAPTER`
- `GATE-SHADOW-E2E` if platform collection is included

Needed material:

- disposable browser profile
- test platform account only if a login is unavoidable
- public sample URL or test competitor account
- isolated legacy-format SQLite database
- MediaCrawler runtime
- rate-limit stop policy

The collector gate is read-only from the platform perspective. Likes, follows,
comments, DMs and publishing are prohibited.

### Shadow Publication Sink

Required for:

- `GATE-SHADOW-E2E`

Needed material:

- explicit shadow-only sink name
- no platform publish credential
- trace marker that all final artifacts are shadow-only
- reviewer signoff step before any future promotion

No real publish operation is allowed in RC1 live-gate preparation.

## Current Executability

No external live gate should be executed in this preparation round.

The fixture/fake references already exist for:

- Hermes/Feishu fixture behavior: `python scripts/core/hermes/verify_goal_11.py`
- ModelGateway fixture behavior: `python scripts/core/model_gateway/verify_goal_07.py`
- Formal research fixture behavior: `python scripts/core/research/verify_goal_06.py`
- End-to-end staging fixture behavior: `python scripts/core/staging/verify_goal_12.py`

Those fixture commands are references only for this plan. They were not rerun in
this preparation round.

## Missing Before Live Execution

- explicit approval to call each external service
- isolated validation database for each gate
- test-only credentials and accounts
- live validation harness entries or approved manual invocation wrappers
- evidence directory creation policy
- secret redaction policy
- budget and rate caps for model/search/platform calls
- rollback owner for each external account
