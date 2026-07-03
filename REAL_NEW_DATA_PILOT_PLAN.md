# Real New Data Pilot Plan

goal: `GOAL-V0.6.2-PRODUCTION-COMPLETION-01`

status: `FUTURE_USER_INITIATED_MANUAL`

This is a post-engineering operating manual for a future user-approved small pilot. It is not a current Phase 8 blocker. It does not create a live approval file, does not choose real URLs, does not start real collection, does not send Feishu messages and does not enable production schedules.

## Scope

The pilot must use only brand-new data collected after explicit approval. It must not read old `creation.db`, old Vault data, cold backups, archived legacy exports or historical platform snapshots.

## Fresh Data Sources

Allowed only after future explicit user approval:

- Newly selected competitor account URLs or platform IDs supplied for the pilot.
- Newly collected latest videos after the pilot starts.
- Newly fetched comments/details for videos discovered during the pilot.
- Newly generated clean-room workflow artifacts created from the pilot data.

Not allowed:

- Old local database rows.
- Old Obsidian material.
- Old `creation_assistant` archive data.
- Cached legacy crawler output.
- Cold backup data.
- Any default fixture used as real success evidence.

## Test Account And Range

Pilot range must be small and reversible:

- Domains: at most two formal domains for the first pilot.
- Competitor accounts: 1 to 2 newly approved accounts per domain.
- Collection window: new/latest posts only after pilot start.
- Candidate workflow count: maximum 3 controlled production tasks.
- Publication: no automatic publication.
- Feishu: no real send unless separately approved.
- Timers: no production scheduled task unless separately approved.

## Required Future User Approvals

The following operations require explicit approval before execution:

- Use of any real platform account, cookie, token or crawler credential.
- Starting real new platform collection.
- Fetching real comments/details/media.
- Running a real GPT regression or switching `business.primary` to GPT.
- Sending any real Feishu test message.
- Creating or enabling any production timer.
- Persisting pilot results to formal production tables beyond the approved clean-room pilot scope.

## Pilot Procedure

1. Confirm clean-room formal DB health before pilot.
2. Confirm `business.primary` binding and model snapshot policy.
3. Register the approved new data source as pilot-only input.
4. Collect only fresh post metadata within the approved range.
5. Run deterministic hit/baseline logic only through formal code paths.
6. For any selected source, run formal workflow through Hermes whitelist/Core/Scheduler/Input Assembly/Formal Skill Dispatcher/Materializer/Outbox.
7. Record every model, prompt, schema, input, experience and output snapshot.
8. Stop on first fail-closed production blocker; do not use fallback.
9. Produce a pilot evidence report with sanitized IDs and no secrets.
10. Destroy disposable test resources and confirm no residual containers/processes.

## Acceptance Criteria

- No old data read.
- No fallback or automatic downgrade.
- No direct Skill call from Hermes.
- No direct DB/shell/model call from Hermes.
- No production timer enabled.
- No Feishu send unless separately approved.
- New jobs freeze the active business model snapshot.
- Failed Provider, Adapter, Skill, Materializer or Outbox paths fail closed.
- Clean-room formal DB state is reported before and after the pilot.

## Stop Conditions

Stop immediately if any of the following is required:

- Real credential access not already approved.
- GPT switch or GPT real regression.
- Real platform collection.
- Real Feishu send.
- Production timer enablement.
- Any attempt to read old data because new data is absent.
- Any fallback/alternate path proposal.

## Future Manual Activation Inputs

These items are intentionally not required for the current engineering Goal to complete:

1. Explicit approval to start the small real new data pilot.
2. User-supplied new data sources/accounts for the pilot.
3. Explicit approval for any real Feishu test message.
4. Explicit approval for any production scheduled task enablement.
5. Explicit approval for any formal production writes beyond disposable/sanitized evidence.

Until those future inputs exist, the correct current state remains: no real collection has started, no real Feishu message has been sent, no production schedule has been enabled, and no old data may be used as pilot evidence.
