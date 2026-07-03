# Business Model Switch To GPT Plan

goal: `GOAL-V0.6.2-PRODUCTION-COMPLETION-01`

status: `AUTHORIZATION_REQUIRED`

This plan is a gate document only. It does not switch `business.primary`, does not call GPT, and does not create any fallback path.

## Current Binding

- Current unique business model binding: `business.primary`
- Current approved live provider path: `provider_alias=hermes`, `live_model_port=HermesModelProviderAdapter`
- Current model reference source: `MODEL_PROVIDER_MODEL`
- Current approved business model class for development/live Provider validation: Mimo
- Active binding count: exactly one
- Fallback policy: disabled; on failure, fail closed
- Hermes chat model: independent from business model binding and must not affect `business.primary`

## GPT Configuration Method

The switch must be a single authorized change to the model reference behind `business.primary`; Skill code and workflow code must not hard-code GPT model names.

Required configuration changes after approval:

- Set the approved GPT model reference through the same model reference mechanism currently used by `business.primary`.
- Create a new `model_config_version` for the GPT binding.
- Record the config hash, route registry version, operator, timestamp and approval reference.
- Keep all formal Skill logical routes unchanged, for example `business.content_classify`, `business.script_generate` and `business.experience_revision_propose`.
- Do not add a backup model list.
- Do not keep Mimo as an automatic fallback.

## Isolated GPT Precheck

The precheck must run before switching `business.primary`.

Required checks:

- One isolated GPT provider connectivity probe, using a synthetic payload only.
- No old data, no production data, no real platform collection and no Feishu send.
- Validate that tools, memory, messaging, nested orchestration and file/terminal side effects remain disabled.
- Validate that failure produces a failed run envelope and does not call Mimo, DeepSeek, Codex CLI, Claude CLI or any legacy model script.
- Validate cost/usage metadata shape without printing secrets.

## Minimal GPT Regression Matrix

Run only after explicit approval.

Required matrix:

- `content_classify`: success, insufficient information, invalid output fail-closed
- `content_relation_judge`: success, insufficient evidence, invalid direction fail-closed
- `source_to_topic`: success and needs-review/no-valid-result
- `content_plan`: hook + outline success and invalid subcall fail-closed
- `script_generate`: success and empty draft fail-closed
- `script_review`: review, polish and AI-flavor branches
- `experiment_review`: supported/refuted/inconclusive experience refs
- `experience_revision_propose`: candidate-only proposal, illegal publish rejected

Acceptance:

- Same formal Skill schemas as current Mimo path.
- Same prompts and prompt hashes unless a separate prompt migration is approved.
- Same Input Assembly, experience context and upstream artifact rules.
- Same Materializer and Outbox path.
- No fallback or automatic downgrade.

## Prompt And Schema Consistency

- Do not change prompt assets during the model switch unless explicitly approved as a separate prompt migration.
- All input and output schemas remain the current formal Skill schemas.
- Invalid GPT output must fail closed at the Adapter/schema boundary.
- Existing jobs keep their frozen Skill, prompt, schema, input and model snapshots.

## Experience Reference Consistency

- ExperienceContext selection remains deterministic.
- Published matching experiences only.
- Candidate, revoked, wrong-domain and unresolved-conflict experiences remain rejected.
- Retry must reuse the same frozen experience snapshot.
- `experience_usage` must reference only frozen experience versions.

## Model Config Version Upgrade

The GPT switch must create a new versioned binding:

- `model_config_version`: new value
- `config_hash`: derived from the approved GPT binding metadata
- `provider_alias`: unchanged unless separately approved
- `active_binding_count`: exactly one after switch
- Previous Mimo binding: inactive history only

## Atomic Switch

The switch must be atomic:

1. Record approval reference.
2. Prepare GPT binding metadata.
3. Run isolated GPT precheck.
4. Run minimal GPT regression matrix.
5. Mark current Mimo binding inactive.
6. Mark GPT binding active as the only `business.primary`.
7. Verify new job creation freezes the GPT binding snapshot.
8. Verify existing jobs still reference their original Mimo snapshot.

## New Jobs Only

- The switch affects only jobs created after the active binding changes.
- Running jobs do not switch.
- Queued jobs with frozen Mimo snapshots do not switch.
- Retry of an existing job must keep the original model snapshot.

## No Mimo Fallback

- Mimo must not remain configured as backup.
- GPT failure must fail closed.
- Manual future switch from GPT to Mimo, DeepSeek or another model requires a new authorization and regression gate.

## Generic Manual Switch Flow

The same flow applies to any future approved model:

1. Authorize target model and provider.
2. Run isolated provider precheck.
3. Run minimal formal Skill regression.
4. Create a new `model_config_version`.
5. Atomically activate exactly one `business.primary`.
6. Keep previous model as inactive history only.
7. Verify no fallback and no existing job mutation.

## Authorization Questions

Phase 8 must stop here until the user approves:

1. Whether to switch the unique business model from Mimo to GPT.
2. Whether to execute the minimal GPT real regression.
3. Whether any GPT provider credential/config value may be used.

