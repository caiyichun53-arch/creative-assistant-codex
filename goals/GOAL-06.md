# GOAL-06 - Formal Research Port and Materialization Baseline

Source: User approval after GOAL-05, plus the repository-local GOAL-06 anchors found in `BUILD_PLAN.md` and `MODULE_REUSE_MATRIX.yaml`.

Scope:
- Create the first formal research chain baseline for topic-first material preparation.
- Define SearchProvider, Fetcher and Extractor as Port/Adapter contracts.
- Keep real search providers, websites, models and external credentials outside the local core gate.
- Persist sources, fetched documents, extracted evidence and research artifacts through the existing Core/Persistence Materializer boundary.
- Prove with fake/replay-style local tests that formal research does not read video, audio, ASR, comments or video-analysis artifacts.
- Preserve traceability and immutable version history for formal research outputs.

Non-scope:
- No video-platform search or video-platform reads in formal research.
- No MediaCrawler, ASR, comment collection or reverse-DNA integration.
- No generic Agent Runtime.
- No Portable Skill Runtime, complete ModelGateway or GOAL-07 work.
- No Redis, Celery, Temporal, Kafka, vector database or generic DAG.
- No live external search gate as a local blocker.

Initial checkpoints:
1. Create GOAL-06 task/progress baseline from the repository-local GOAL-06 anchors.
2. Add formal research Port/Adapter contracts and a local fake/replay verification path.
3. Add topic-first research workflow enqueueing only after the formal research materialization contract is stable.
4. Add GOAL-06 closeout report after all local GOAL-06 gates pass.
