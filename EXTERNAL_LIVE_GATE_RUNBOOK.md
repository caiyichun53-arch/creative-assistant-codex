# V0.6.2 External Live Gate Runbook

status: `RUNBOOK_READY_NO_LIVE_EXECUTION`

This runbook is for the `validation/v0.6.2-live-gates` branch. It is not a new
Goal and does not create GOAL-13.

## Never Do

- Never use production accounts or production credentials.
- Never connect to a production database.
- Never publish real content.
- Never mutate platform production data.
- Never start the full production deployment.
- Never commit `.env.live-gates`, browser profiles, cookies, sessions, test
  media, external raw responses or secret-bearing logs.
- Never run all live gates automatically. The harness has no run-all live mode.

## Files

- Harness: `scripts/validation/live_gates.py`
- Config example: `config/live_gates.example.yaml`
- Env example: `.env.live-gates.example`
- Local env file: `.env.live-gates` (ignored)
- Status summary: `EXTERNAL_LIVE_GATE_STATUS.yaml`
- Generated report: `EXTERNAL_LIVE_GATE_VALIDATION_REPORT.md`
- Local evidence root: `validation_evidence/` (run output ignored)

## Standard Commands

List gates:

```powershell
python scripts/validation/live_gates.py list
```

Run preflight:

```powershell
python scripts/validation/live_gates.py preflight
```

Run all dry-runs without external calls:

```powershell
python scripts/validation/live_gates.py dry-run
```

Run one dry-run:

```powershell
python scripts/validation/live_gates.py dry-run --gate GATE-MODEL-PROVIDER
```

Attempt one live gate only after test credentials and approval exist:

```powershell
python scripts/validation/live_gates.py run --gate GATE-FEISHU-THIN-BINDING --environment validation --live-confirm
```

Generate report:

```powershell
python scripts/validation/live_gates.py report
```

## Gate Preparation

### GATE-HERMES-REAL-HOST

Prepare a non-production Hermes host endpoint, validation token, test actor and
isolated database. Pass only if one external event produces one Core receipt,
one response outbox and a replay returns the original result. Stop and roll back
by revoking the test token and archiving the isolated database.

### GATE-FEISHU-THIN-BINDING

Prepare a Feishu test tenant, test app, test secret, disposable chat and event
verification material. Put values in `.env.live-gates`. Pass only if one test
event creates one Hermes/Core receipt and replies only to the test chat. Stop on
any message to a non-test chat or any secret in logs.

### GATE-MODEL-PROVIDER

Prepare a non-production API key, test project, allowed model and strict cost
cap. Pass only if the provider is called through `ModelGateway` and the model
run envelope records provider request id, usage and cost. Stop if the cap is
exceeded or a key appears in logs.

### GATE-ASR

Prepare local ASR venv, model paths, ffmpeg, controlled test media and an
isolated validation workspace. Pass only if the transcript is written to the
isolated DB and temporary media is removed. Stop if the command points at
`data/creation.db`.

### GATE-SEARCH-PROVIDER

Prepare a search test key, query file and allowed fetch policy. Pass only if an
allowed source is searched, fetched and extracted while blocked video platforms
are rejected. Stop if a blocked platform enters evidence.

### GATE-EXTERNAL-COLLECTOR-ADAPTER

Prepare a disposable browser profile, public sample URL and isolated
legacy-format DB. Pass only if collection writes only to isolated adapter output
and does not touch formal Core state. Stop on any platform mutation, captcha or
production profile use.

### GATE-SHADOW-E2E

Prepare all individual test providers plus a shadow-only publication sink. Pass
only if one full shadow topic path completes without production writes or real
publishing. Stop on any publish attempt or missing trace link.

### GATE-CONTINUOUS-FAULT-RECOVERY

Prepare isolated DB, validation worker identity, test credentials and approved
duration/request caps. Pass only if retries stay bounded, leases recover and
restart does not duplicate external effects. Stop on unbounded retry or cap
breach.

## Credential Placement

Copy `.env.live-gates.example` to `.env.live-gates` and replace placeholders
only on the local machine. Keep all real values outside Git.

## Stop And Roll Back

1. Stop the validation command or worker.
2. Revoke or rotate the relevant test credential.
3. Archive the isolated DB and `validation_evidence/<gate_id>/...` manifest.
4. Delete only disposable validation workspaces after evidence review.
5. Do not delete legacy code or RC artifacts as part of rollback.
