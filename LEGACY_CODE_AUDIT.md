# LEGACY_CODE_AUDIT

## 1. Scope and status

- Repository: `caiyichun53-arch/creative-assistant-codex`
- Frozen source commit: `a8787a439fd281b2d8cec7a7ffe2606426bebab4`
- Audit branch: `audit/goal-00-v0.6.2`
- Design authority: V0.6.2
- Scope: GOAL-00 only
- Business-code changes: none
- Audit result: static audit complete; runtime validation blocked

The repository is neither an empty shell nor a V0.6.2-compliant implementation. It contains valuable deterministic algorithms, acquisition know-how, structured validators, data relationships and content assets. Its production authority and runtime model are nevertheless incompatible with V0.6.2.

The correct conclusion is therefore:

- do not keep everything;
- do not rebuild everything;
- preserve algorithms, data, fixtures/records and adapter capability;
- replace only the foundational authority/runtime paths that cannot be made safe by adaptation or wrapping.

## 2. Current architecture found

### 2.1 State and database

The legacy governance and schema identify SQLite/files as the data truth source. Raw `sqlite3` calls are spread across collectors, analysis, topic flow, content utilities, Feishu listener and semantic/learning scripts.

Current writes include:

- account/video collection and metric updates;
- hit promotion and video status changes;
- topic insertion, selection, rejection, decay and expiry;
- draft/diff registration;
- comment, language-fuel and experience writes;
- Feishu-triggered status changes;
- vault/example/method output.

There is no authoritative Core Command Envelope or Materializer transaction boundary. This is a structural V0.6.2 conflict.

### 2.2 Runtime and host

The current runtime is a Windows-local script system:

- Windows Task Scheduler invokes `scripts/run_daily.py`;
- subprocesses execute collection, hit detection and topic flow;
- a fire-and-forget ASR/reverse-prep process is launched;
- Feishu listener directly handles commands and writes state;
- Codex/Claude agents and CLIs are part of the content production path;
- ignored local folders supply MediaCrawler, data, vault, settings and models.

V0.6.2 instead requires Hermes as production Host, Core commands as the only state-changing interface and Codex as development-only.

### 2.3 Database and time models

The existing SQLite schema contains useful historical structures:

- accounts and competitor accounts;
- competitor videos and observation snapshots;
- baselines and hits;
- topics and candidate view;
- drafts and diffs;
- self-owned tracking;
- hit comments;
- NetEase and language-fuel tables;
- content atoms and language-texture observations.

However:

- accounts embed a single domain rather than using account-domain bindings;
- platform-video uniqueness and immutable snapshots are not represented in the frozen form;
- the video state model is `watching/promoted/archived`, not the formal historical/transition/new plus D0-D7 model;
- self-owned tracking uses integer day offsets rather than exact P+ target and actual ages;
- root/version/current-pointer patterns, UUIDv7, receipts, audit and outbox are absent.

The schema must be retained as migration evidence, not adopted as the target schema.

### 2.4 Baseline and candidate logic

`scripts/analyze/judge_hits.py` contains reusable median/P90 calculations and account-relative comparison. It also directly writes hit/video state and uses a settled-video sample that does not implement the frozen mature-history, rough D7 and formal D baseline families.

The statistical helpers are reusable. The state writes, sample qualification and candidate timing require adaptation.

### 2.5 Semantic model execution

`config/settings.example.yaml` declares a model route table, but `scripts/llm/call.py` invokes `claude -p` directly. The current path does not emit the V0.6.2 Run Envelope:

- model/provider profile version and hash;
- Prompt, Skill, Binding and config version/hash;
- frozen input/output hashes;
- usage, cost and timing;
- provider raw ID and normalized error;
- correlation/causation IDs.

This cannot be preserved as ModelGateway merely by renaming it.

### 2.6 Formal research

Two different generations exist:

1. `scripts/research/research.py` reads a current topic and possibly a competitor transcript, gives the model CLI WebSearch, and overwrites `research.md`.
2. `.agents/skills/research-collector/**` creates structured research plans/packs and includes validators.

The first must leave the formal production path because it mixes video/ASR material into formal research and lacks SearchProvider/Fetcher snapshots and claim/evidence traceability.

The second is a strong adaptation candidate for SearchProvider/Fetcher plus Portable Skill execution.

### 2.7 Content versions

The repository has useful draft version naming and AI-human diff capture. It also has violations that cannot remain:

- a polish flow instructs overwriting `draft_v1.md`;
- fixed paths such as `research.md` are reused;
- the draft table carries `draft/approved/published` status in one object family;
- actual spoken/published content is not separated from the approved draft;
- current rows and paths are used instead of concrete immutable revisions.

### 2.8 Learning and Skill governance

The code already distinguishes some raw comments, insights, atoms, DNA map outputs and reduce outputs. These assets are worth retaining.

The publication boundary is not compliant:

- reduce/upgrade scripts can write methods/examples directly;
- formal and candidate content are not structurally isolated;
- the formal Skill repository is not demonstrably read-only;
- fixed regression, ablation, compatibility, provenance and Clean-room gates are not enforced before publication.

Existing learned artifacts must be imported as legacy candidates or explicitly reviewed legacy artifacts, not silently accepted as current formal Skills.

## 3. Prohibited-item audit

| Check | Finding | Consequence |
|---|---|---|
| Feishu direct connection replacing Hermes | Present | Direct listener must leave production path; retain only command/card concepts. |
| Self-built Agent Runtime | Partly present | Codex/Claude writer/editor workflow duplicates Host/Core/Job/Skill responsibilities. |
| Codex as production host/executor | Present | Production route must move to Hermes + Core/Runner/ModelGateway. |
| Adapter/Skill/Host direct database writes | Present | All formal writes must move behind Core/Materializer. |
| Formal Skill silent modification/publication | Present risk | Learning outputs must become isolated candidates pending release gates. |
| Formal research includes video/ASR/comments | Present | Legacy `research.py` must be replaced. |
| In-place overwrite of artifacts | Present | Immutable root/revision model is required. |
| Current-only references | Present | Input Assembly must freeze exact versions/snapshots/hashes. |
| Approved and published content conflated | Present | Approved draft and actual published artifact require separate objects. |
| Missing model/Prompt/config/input/edit trace | Present | Full Run Envelope and human-edit evidence refs are required. |
| Separate validation and production logic | Unproven/high risk | No integrated suite demonstrates same-handler validation. |
| Production depends on removable validation assets | Not proven | Clean-room proof is absent; runtime does depend on hidden local assets. |
| Old code changes frozen business boundaries | Risk present | Old SQLite/direct-Feishu/single-domain rules cannot override V0.6.2. |
| Unnecessary Redis/Celery/Temporal/vector DB | Not found | Do not introduce them during migration. |
| Unnecessary multi-Agent system | Present in legacy workflow | Extract useful prompts/roles; remove production agent-team dependence. |

## 4. Module decision summary

The proposed `MODULE_REUSE_MATRIX.yaml` contains 29 auditable modules:

- `keep`: 2
- `adapt`: 15
- `wrap`: 4
- `replace`: 8

The matrix is proposed, not approved.

### Keep

1. `comment_noise_filter`
2. `banned_word_checker`

These are narrow deterministic utilities. They still require regression fixtures and versioned policy/config.

### Wrap

1. `mediacrawler_adapter`
2. `netease_adapter`
3. `asr_pipeline`
4. `external_language_collectors`

These retain external execution and normalization capability, but lose all business-state authority.

### Adapt

The main adaptation assets are:

- legacy SQLite schema and migration history as read-only migration source;
- account/domain concepts;
- video/snapshot fields;
- comment storage and sample logic;
- baseline statistics and candidate concepts;
- embedding clustering;
- language-fuel atoms and evidence links;
- structured research collector and validators;
- topic/content planning contracts;
- draft/diff/review assets;
- experience/evidence/P+ materials;
- Portable Skill content;
- Obsidian formats;
- existing validation probes.

### Replace

The following production authorities must be replaced:

1. dependency/deployment path as a hidden Windows-local runtime;
2. absence of Core API/Materializer and distributed direct state writes;
3. legacy formal research path;
4. CLI-based model runner as ModelGateway;
5. direct Feishu host path instead of Hermes;
6. Codex/Claude production and multi-agent runtime;
7. Task Scheduler/subprocess path as durable Job/Worker/Scheduler;
8. missing correction/receipt/audit/outbox/idempotency foundation.

`replace` here means replace the forbidden authority. It does not authorize deletion of useful inner algorithms, data, prompts, adapters or migration evidence.

## 5. Directly reusable assets

### Deterministic code

- comment filtering;
- banned-word checking;
- field/int normalization;
- safe-title/file-name handling;
- median/P90 helpers;
- selected ranking and deduplication utilities.

### Adapter knowledge

- MediaCrawler JSONL mapping and fresh signed-link fetching;
- browser mutual exclusion and raw archiving;
- NetEase track/comment mapping;
- local ASR preparation and quality checks;
- Douban and other target-specific parsing.

### Structured contracts and validators

- research plan/pack contracts;
- topic output validators;
- original/source-derived/spinoff distinctions;
- full vs writer-safe material packs;
- stage-specific content review concepts.

### Data and historical evidence

- SQLite schema and numbered migration scripts;
- legacy IDs, source URLs and raw payloads;
- topic/draft/comment relations;
- content hashes where present;
- local database, logs, vault and vendor revisions once safely exported.

## 6. High-risk migration items

### 6.1 SQLite to PostgreSQL

Before any implementation:

- export the real schema and table counts;
- run SQLite foreign-key and integrity checks on a copy;
- hash the source database and relevant file assets;
- map each row to a migration receipt;
- preserve the SQLite source read-only;
- define reconciliation and rollback queries.

No legacy migration script may be rewritten to make the history look cleaner.

### 6.2 D/P and baseline semantics

Legacy video checks cannot be mechanically renamed into formal D/P snapshots. Discovery time, publish-time trust, completeness and late-discovery eligibility must be reconstructed only where evidence exists. Unknown facts must remain unknown.

### 6.3 Approved/published history

Legacy files may not prove which model/config produced a draft, whether it was overwritten, what was approved or what was actually spoken. Migration must retain uncertainty rather than invent provenance.

### 6.4 Skill and experience assets

Existing vault methods/examples are not automatically formal under R07. They need origin metadata and candidate/review status before publication.

### 6.5 Ignored MediaCrawler/vendor assets

The repository documents local MediaCrawler patches but does not contain the vendor checkout or exact patch state. Those must be exported and pinned before wrapping the adapter.

## 7. First review loop — over-rewrite check

Every proposed replacement was challenged against `adapt` and `wrap`.

Results:

- MediaCrawler, NetEase, ASR and external collectors remain `wrap`, not replace.
- SQLite schema remains `adapt` as migration evidence, even though PostgreSQL becomes authoritative.
- Portable Skills, content workflow, experience assets and Obsidian remain `adapt`.
- Direct Feishu, Codex/Claude production execution, CLI model runner, non-durable scheduler and missing Core/trace authority remain `replace`, because a cosmetic wrapper would preserve forbidden ownership.

Conclusion: there is no basis for an indiscriminate rewrite.

## 8. Second review loop — false reuse and overdesign check

The opposite failure mode was also checked.

Findings:

- direct SQL cannot be retained behind renamed folders;
- direct Feishu cannot remain host through a thin wrapper;
- `claude -p` cannot become ModelGateway without the formal provider/Run contracts;
- no Redis, Celery, Temporal, vector database or generic DAG is required;
- no new domain-specific core states/tables are justified in GOAL-00;
- domain differences must stay in versioned configuration/foundation packages;
- data migration and uncertainty are mandatory parts of the cutover;
- Portable Skill, Host Binding, Runner, ModelGateway, Core and Materializer must remain separate.

Conclusion: the proposed matrix avoids both code-cleanliness rewriting and architecture-preserving reuse.

## 9. Cross-layer ownership check

| Layer | Required owner | Legacy finding | Decision |
|---|---|---|---|
| External acquisition | Port/Adapter | Also writes SQLite/files/vault | Wrap and remove writes |
| Frozen input selection | Core | Current rows/files chosen by scripts | Adapt into Input Assembly |
| Host conversion | Host Binding | Direct Feishu acts and chooses | Replace host path |
| Semantic generation | Portable Skill/Runner | Host paths and side effects leak into skills | Adapt packages |
| Model selection | ModelGateway | Direct CLI runner | Replace |
| State/pointers/audit | Core/Materializer | Distributed SQL/file writes | Replace authority |
| Schedule/recovery | Job/attempt worker | Task Scheduler + Popen | Replace |
| Knowledge projection | Materializer | Collectors/LLM scripts write vault | Adapt |
| Development | Codex | Mixed with production runtime | Keep development role only |

## 10. Runtime and completion blockers

GOAL-00 cannot be marked complete because:

1. the user's local dirty/untracked Git state is inaccessible;
2. the remote repository is a sanitized source upload, not the complete runnable environment;
3. vendor code, local settings, data, vault and models are absent;
4. there is no authoritative dependency manifest;
5. the actual SQLite database/migration state is unavailable;
6. no tracked unit/integration/fixture/replay/FakeClock/fault suite exists;
7. the audit environment could not clone and execute the project due DNS failure;
8. the reuse matrix has not been approved.

No business code, migration or data was changed. No credentials or real external operations were used. GOAL-01 must not start.
