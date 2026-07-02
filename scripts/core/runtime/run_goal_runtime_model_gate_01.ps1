param(
    [string]$PostgresImage = "postgres:16-alpine",
    [string]$ContainerName = "",
    [string]$DatabaseName = "goal_runtime_model_gate_01",
    [string]$ConfigPath = "",
    [string]$EnvFile = "",
    [switch]$KeepContainer
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $root "config\live_gates.yaml"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $root ".env.live-gates"
}
if (-not $ContainerName) {
    $suffix = [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $ContainerName = "goal-runtime-model-gate-01-$suffix"
}

$schemaPaths = @(
    (Join-Path $PSScriptRoot "..\persistence\goal01_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal02_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal03_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "goal_runtime_vertical_slice_schema.postgres.sql")
)

foreach ($path in ($schemaPaths + @($ConfigPath, $EnvFile))) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing required file: $path"
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

function Invoke-PsqlText {
    param([string]$Sql, [string]$Db = $DatabaseName)
    $Sql | docker exec -i $ContainerName psql -U postgres -d $Db -v ON_ERROR_STOP=1 -X
    if ($LASTEXITCODE -ne 0) {
        throw "psql command failed for database $Db"
    }
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

$containerStarted = $false
$databaseDropped = $false
$containerRemoved = $false
$postgresVersion = ""
$initTableCount = ""
$initRowCount = ""
$gateStatus = "failed"

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

    Invoke-PsqlText -Sql @"
INSERT INTO command_receipt(
    receipt_id, command_scope, idempotency_key, request_hash, result_json,
    correlation_id, status
) VALUES(
    '018f0000-0000-7000-8000-0000000a0001',
    'runtime_probe.model_gate.preflight',
    'goal-runtime-model-gate-01-pg-preflight',
    'request-hash',
    '{"accepted": true}'::jsonb,
    '018f0000-0000-7000-8000-0000000a0002',
    'succeeded'
);
"@

    python (Join-Path $PSScriptRoot "run_goal_runtime_model_gate_01.py") --config $ConfigPath --env-file $EnvFile --environment validation
    if ($LASTEXITCODE -ne 0) {
        throw "Python live model gate failed with exit code $LASTEXITCODE"
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
        $containerRemoved = $true
    }

    $statusPath = Join-Path $root "MODEL_GATE_STATUS.yaml"
    if (Test-Path -LiteralPath $statusPath) {
        Add-Content -LiteralPath $statusPath -Encoding UTF8 -Value @"
postgres_test_environment:
  gate_status: $gateStatus
  postgres_image: $PostgresImage
  postgres_version: "$postgresVersion"
  disposable_database: $DatabaseName
  schema_rounds: 2
  initialized_formal_table_count: $initTableCount
  initialized_formal_row_count: $initRowCount
  disposable_database_dropped: $databaseDropped
  container_removed: $containerRemoved
"@
    }
    $reportPath = Join-Path $root "GOAL-RUNTIME-MODEL-GATE-01_VALIDATION_REPORT.md"
    if (Test-Path -LiteralPath $reportPath) {
        Add-Content -LiteralPath $reportPath -Encoding UTF8 -Value @"
## PostgreSQL Test Environment
- gate_status: $gateStatus
- postgres_image: $PostgresImage
- postgres_version: $postgresVersion
- disposable_database: $DatabaseName
- schema_rounds: 2
- initialized_formal_table_count: $initTableCount
- initialized_formal_row_count: $initRowCount
- disposable_database_dropped: $databaseDropped
- container_removed: $containerRemoved

"@
    }
}

if ($gateStatus -ne "passed") {
    exit 1
}

Write-Output "GOAL-RUNTIME-MODEL-GATE-01 gate passed"
