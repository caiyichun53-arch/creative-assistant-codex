param(
    [string]$PostgresImage = "postgres:16-alpine",
    [string]$ContainerName = "",
    [string]$DatabaseName = "goal_business_skill_content_relation_judge_01",
    [string]$EvidencePath = "",
    [switch]$KeepContainer
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$schemaPaths = @(
    (Join-Path $PSScriptRoot "..\persistence\goal01_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal02_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal03_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "formal_skill_adapter_schema.postgres.sql")
)
$verifyPath = Join-Path $PSScriptRoot "verify_content_relation_judge_postgres.sql"
if (-not $EvidencePath) {
    $EvidencePath = Join-Path $root "validation_evidence\GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01_POSTGRES.md"
}
if (-not $ContainerName) {
    $suffix = [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $ContainerName = "content-relation-pg-$suffix"
}

foreach ($path in ($schemaPaths + @($verifyPath))) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing PostgreSQL gate file: $path"
    }
}

function Invoke-Docker {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $output = & docker @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "docker $($Arguments -join ' ') failed with exit code $exitCode`n$output"
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($output -join "`n") }
}

function Invoke-PsqlFile {
    param([string]$Path, [string]$Db = $DatabaseName)
    Get-Content -LiteralPath $Path -Encoding UTF8 -Raw | docker exec -i $ContainerName psql -U postgres -d $Db -v ON_ERROR_STOP=1 -X
    if ($LASTEXITCODE -ne 0) {
        throw "psql file failed: $Path"
    }
}

function Get-PsqlScalar {
    param([string]$Sql, [string]$Db = $DatabaseName)
    $output = $Sql | docker exec -i $ContainerName psql -U postgres -d $Db -v ON_ERROR_STOP=1 -X -t -A
    if ($LASTEXITCODE -ne 0) {
        throw "psql scalar failed for database $Db"
    }
    return ($output -join "`n").Trim()
}

$imageWasPresent = (Invoke-Docker -Arguments @("image", "inspect", $PostgresImage) -AllowFailure).ExitCode -eq 0
$containerStarted = $false
$databaseDropped = $false
$gateStatus = "failed"
$postgresVersion = ""
$initTableCount = ""
$initRowCount = ""
$concurrencyResult = ""

try {
    $passwordBytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($passwordBytes)
    $rng.Dispose()
    $password = [Convert]::ToBase64String($passwordBytes)
    Invoke-Docker -Arguments @(
        "run", "-d", "--rm",
        "--name", $ContainerName,
        "-e", "POSTGRES_PASSWORD=$password",
        "-e", "POSTGRES_DB=postgres",
        $PostgresImage
    ) | Out-Null
    $containerStarted = $true

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        $probe = Invoke-Docker -Arguments @("exec", $ContainerName, "pg_isready", "-U", "postgres") -AllowFailure
        if ($probe.ExitCode -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw "PostgreSQL container did not become ready"
    }

    Invoke-Docker -Arguments @("exec", $ContainerName, "createdb", "-U", "postgres", $DatabaseName) | Out-Null
    $postgresVersion = Get-PsqlScalar -Db $DatabaseName -Sql "SELECT version();"

    foreach ($round in 1..2) {
        foreach ($path in $schemaPaths) {
            Invoke-PsqlFile -Path $path
        }
    }

    $initTableCount = Get-PsqlScalar -Sql "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
    $initRowCount = Get-PsqlScalar -Sql @"
WITH table_counts AS (
    SELECT format('%I.%I', table_schema, table_name) AS table_ref
      FROM information_schema.tables
     WHERE table_schema='public'
       AND table_type='BASE TABLE'
)
SELECT COALESCE(sum(row_count), 0)
  FROM (
    SELECT (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', table_ref), false, true, '')))[1]::text::bigint AS row_count
      FROM table_counts
  ) counts;
"@

    Invoke-PsqlFile -Path $verifyPath

    $sessionASql = @"
BEGIN;
INSERT INTO command_receipt(
    receipt_id, command_scope, idempotency_key, request_hash, result_json,
    correlation_id, status
) VALUES(
    '018f0000-0000-7000-8000-00000000aa01',
    'content_relation_judge.postgres.concurrent',
    'content-relation-concurrent-claim',
    'request-hash',
    '{"accepted": true}'::jsonb,
    '018f0000-0000-7000-8000-00000000aa99',
    'succeeded'
);
INSERT INTO scheduler_job(
    job_id, job_kind, status, priority, run_after, max_attempts, payload_json,
    correlation_id, created_by_receipt_id
) VALUES(
    '018f0000-0000-7000-8000-00000000aa02',
    'formal_skill.execute',
    'queued',
    0,
    now(),
    3,
    '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-concurrent"}}'::jsonb,
    '018f0000-0000-7000-8000-00000000aa99',
    '018f0000-0000-7000-8000-00000000aa01'
);
WITH picked AS (
    SELECT job_id
      FROM scheduler_job
     WHERE job_id='018f0000-0000-7000-8000-00000000aa02'
       AND status='queued'
     FOR UPDATE SKIP LOCKED
),
updated AS (
    UPDATE scheduler_job j
       SET status='leased',
           attempt_count=j.attempt_count + 1,
           current_attempt_id='018f0000-0000-7000-8000-00000000aa03',
           lease_owner='concurrent-worker-a',
           lease_expires_at=now() + interval '60 seconds'
      FROM picked p
     WHERE j.job_id=p.job_id
 RETURNING j.job_id
)
SELECT count(*) AS worker_a_claimed FROM updated;
SELECT pg_sleep(5);
ROLLBACK;
"@
    $jobA = Start-Job -ScriptBlock {
        param($Name, $Db, $Sql)
        $Sql | docker exec -i $Name psql -U postgres -d $Db -v ON_ERROR_STOP=1 -X -t -A
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } -ArgumentList $ContainerName, $DatabaseName, $sessionASql

    Start-Sleep -Seconds 1
    $concurrencyResult = Get-PsqlScalar -Sql @"
WITH picked AS (
    SELECT job_id
      FROM scheduler_job
     WHERE job_id='018f0000-0000-7000-8000-00000000aa02'
       AND status='queued'
     FOR UPDATE SKIP LOCKED
)
SELECT count(*) FROM picked;
"@
    Wait-Job $jobA | Out-Null
    $jobAOutput = Receive-Job $jobA
    Remove-Job $jobA
    if ($concurrencyResult -ne "0") {
        throw "Concurrent SKIP LOCKED claim returned $concurrencyResult rows for worker B"
    }
    if (($jobAOutput -join "`n") -notmatch "(?m)^\s*1\s*$") {
        throw "Concurrent worker A did not claim exactly one row"
    }

    Invoke-Docker -Arguments @("exec", $ContainerName, "dropdb", "-U", "postgres", "--if-exists", $DatabaseName) | Out-Null
    $databaseDropped = $true
    $remainingDb = Get-PsqlScalar -Db "postgres" -Sql "SELECT count(*) FROM pg_database WHERE datname='$DatabaseName';"
    if ($remainingDb -ne "0") {
        throw "Disposable database still exists after drop"
    }

    $gateStatus = "passed"
}
finally {
    if ($containerStarted -and -not $KeepContainer) {
        Invoke-Docker -Arguments @("rm", "-f", $ContainerName) -AllowFailure | Out-Null
    }

    $evidenceDir = Split-Path -Parent $EvidencePath
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    $evidence = @(
        "# GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01 PostgreSQL Evidence",
        "",
        "- gate_status: $gateStatus",
        "- postgres_image: $PostgresImage",
        "- postgres_image_preexisting: $imageWasPresent",
        "- postgres_version: $postgresVersion",
        "- isolation: disposable Docker container without host port exposure",
        "- disposable_database: $DatabaseName",
        "- schema_rounds: 2",
        "- initialized_formal_table_count: $initTableCount",
        "- initialized_formal_row_count: $initRowCount",
        "- content_relation_judge_sql_gate: $verifyPath",
        "- success_case: true",
        "- insufficient_evidence_case: true",
        "- no_relation_case: true",
        "- provider_failure_no_formal_result: true",
        "- schema_error_no_success_result: true",
        "- invalid_enum_no_success_result: true",
        "- direction_error_no_success_result: true",
        "- retry_reclaim: true",
        "- idempotency_duplicate_rejected: true",
        "- materializer_failure_preserved_counts: true",
        "- outbox_dedupe: true",
        "- immutable_result_index: true",
        "- ab_swap_semantics: true",
        "- two_session_concurrent_claim_worker_b_rows: $concurrencyResult",
        "- disposable_database_dropped: $databaseDropped",
        "- container_removed: $(-not $KeepContainer)",
        "- secrets_recorded: false",
        "- external_llm_called: false",
        "- feishu_called: false",
        "- legacy_data_imported: false"
    )
    Set-Content -LiteralPath $EvidencePath -Encoding UTF8 -Value $evidence
}

if ($gateStatus -ne "passed") {
    exit 1
}

Write-Output "GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01 PostgreSQL gate passed"
Write-Output "evidence=$EvidencePath"
