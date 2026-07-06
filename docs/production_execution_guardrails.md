# Production Execution Guardrails

This project treats real collection, real database writes, baselines, hit promotion, activation, and cleanup as controlled production actions.

## Required Sequence

1. Write an execution card before any production action.
2. Compare the business design with the script behavior before running.
3. Prove the rules with fixture data before touching real data.
4. Run the single approved entrypoint only.
5. Report business status, not implementation jargon.

## Execution Card

An execution card must state:

- business flow being executed
- design sources used
- input data
- expected output
- allowed writes
- excluded data
- insufficient-sample behavior
- rollback or isolation plan
- success criteria

## Competitor Registration Contract

For competitor account registration first crawl:

- registration stock videos are archived, not placed into the observation pool
- comments are not collected during first crawl
- pinned videos are excluded from baseline and hit judgement
- if an explicit pinned flag exists, use it
- if no pinned flag exists, compare the first four homepage videos; at most the first three can be pinned, and time-order inversion marks pinned videos
- videos younger than 7 days do not enter baseline or hit judgement
- the baseline target is 30 samples
- 30 is not a hard judgement gate
- the 90-day window is tried first
- if the 90-day usable sample count is below 10, expand the window
- if the expanded usable sample count is still below 10, skip that account with an explicit insufficient-sample status
- baseline is median/P90
- hit promotion threshold is `max(median * 3, P90)` when P90 is required
- first crawl must run baseline and hit judgement after stock ingestion

## Reporting

Reports must use business terms:

- accounts registered
- first-crawl videos
- baseline samples
- baselines generated
- hits promoted
- skipped accounts and reason
- excluded pinned / young / out-of-window videos

Avoid implementation terms unless debugging implementation directly.
