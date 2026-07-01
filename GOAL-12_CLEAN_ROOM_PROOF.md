# GOAL-12 Clean-room Proof

status: `GOAL-12_COMPLETE_WITH_EXTERNAL_LIVE_GATES`

validated_code_commit: `a9a28ec`

## Local-only Proof

- `scripts/core/staging/verify_goal_12.py` uses in-memory SQLite and temporary disposable filesystem directories.
- Fake SearchProvider, Fetcher, Extractor, ModelProvider and Feishu response dispatcher are local fixtures.
- No real Hermes, Feishu, platform, ASR, SearchProvider, model provider or production credential is used.
- No network side effect is required.

## Production Independence Proof

- GOAL-12 staging uses existing production handlers and materializers:
  - `CoreMaterializer`
  - `Goal03Scheduler`
  - `RuntimeHost`
  - `FormalResearchService`
  - `ModelGateway`
  - `PortableSkillRunner`
  - `ProductionVersionChainMaterializer`
  - `ExperimentMaterializer`
  - `CorrectionMaterializer`
  - `HermesCoreBridge`
- The verifier does not create business tables or new business states.
- Validation assets and candidate workspace are created under a temporary directory and deleted.
- After deletion, a fresh production probe starts and executes a Hermes-to-Core topic creation path.
- Formal skill loading reads only an allowlisted formal skill directory; candidate assets are not loaded.

## Backup and Restore Proof

- SQLite `backup()` copies the staging database into a disposable backup file.
- The backup is copied to a restored database file.
- The restored database is opened independently and required trace versions are verified:
  - script version
  - publication capture version
  - experiment result version
  - correction report version

## Replay and Fault Proof

- Publication capture replay returns the original version without duplicate side effects.
- Stale basis transition records a rejected Core command.
- Injected script receipt failure rolls back partial script side effects.
- Forced interruption is simulated by claiming a job, advancing `FakeClock` past the lease, recovering the expired lease and completing the retry path.
- Continuous-run fixture dispatches five deterministic outbox jobs.

## External Side Effects

- Real Hermes used: no.
- Real Feishu used: no.
- Real credentials used: no.
- Network side effects: none.
- Irreversible external operations: none.

## Remaining External Gates

- Real Hermes/Feishu live/shadow gate.
- Live provider/platform/model/ASR/SearchProvider shadow gate.
- Formal production-duration continuous-run gate.
