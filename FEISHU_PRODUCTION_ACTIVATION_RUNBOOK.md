# Feishu Production Activation Runbook

goal: `GOAL-V0.6.2-PRODUCTION-COMPLETION-01`

status: `FUTURE_USER_INITIATED_MANUAL`

This runbook is for future production activation after the engineering Goal is complete. It does not authorize Codex to modify Hermes credentials, send real Feishu messages, start real collection, or enable production schedules during the current Goal.

## Preconditions

- The user has confirmed the Hermes/Feishu app configuration outside this repository.
- Any required Feishu app ID, app secret, chat ID, user IDs and event verification token are supplied by the user through local ignored configuration only.
- No credential values are copied into tracked files or reports.
- `business.primary` remains a single active binding with no fallback.
- Production scheduled tasks remain disabled until the user explicitly enables them.

## When Event Verification Token Is Needed

The event verification token is needed only for future live inbound Feishu event validation. It is not required for the current engineering readiness check, and it must not be guessed, printed, or stored in tracked files.

## Controlled Test Procedure

1. Confirm the active Hermes profile and Feishu app are the intended future production target.
2. Confirm `CreationAssistant_Daily` and `CreationAssistant_Listener` are disabled before the test starts.
3. Run only the approved whitelist Tool path: Feishu -> Hermes -> whitelist Tool -> Core API -> Job/Worker -> formal workflow -> Outbox.
4. Use a small controlled task and verify the resulting status through the whitelist query actions.
5. Confirm the whitelist Tool exposes only approved actions:
   - `create_controlled_task`
   - `query_task_status`
   - `query_task_result`
   - `query_failure_reason`
   - `cancel_task`
   - `query_human_confirmation_items`
   - `query_business_model_binding_summary`
6. Confirm no shell, SQL, direct Skill call, direct model call, direct Feishu send, or fallback enablement is accepted through the Tool payload.
7. Confirm Outbox remains the only result-send boundary.

## Listener Operations

- Keep the Listener disabled by default after engineering handoff.
- Enable the Listener only after the user explicitly approves live Feishu activation.
- Disable the Listener immediately after a controlled test if production operation has not been separately approved.
- Do not bypass Hermes or Core API with shell commands, SQL commands, old push scripts, or direct Feishu send scripts.

## Production Schedule Operations

- Keep `CreationAssistant_Daily` disabled until the user explicitly approves production scheduling.
- Keep `CreationAssistant_Listener` disabled until the user explicitly approves listener activation.
- Do not enable any automatic collection, periodic model task, or batch Feishu send as part of the current engineering Goal.

## Rollback

1. Disable the Listener task.
2. Disable the Daily task.
3. Stop any test listener process started for the controlled test.
4. Confirm no real collection job is running.
5. Confirm no fallback route was created.
6. Record a sanitized activation note without secrets.

## Forbidden Shortcuts

- Do not use shell commands to execute production business work outside the whitelist Tool path.
- Do not use SQL to mutate workflow state directly.
- Do not call formal Skills directly from Hermes.
- Do not call the business model directly from Hermes.
- Do not send Feishu messages outside Outbox.
- Do not read old data, cold backups, or legacy outputs to make a live test appear successful.
