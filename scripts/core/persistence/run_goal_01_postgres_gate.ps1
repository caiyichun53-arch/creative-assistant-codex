param(
    [string]$DatabaseUrl = $env:DATABASE_URL,
    [string]$PsqlPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$schemaPath = Join-Path $PSScriptRoot "goal01_schema.postgres.sql"
$verifyPath = Join-Path $PSScriptRoot "verify_goal_01_postgres.sql"

function Stop-GateInput {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
    exit 2
}

if (-not (Test-Path -LiteralPath $schemaPath)) {
    Stop-GateInput "Missing PostgreSQL schema: $schemaPath"
}

if (-not (Test-Path -LiteralPath $verifyPath)) {
    Stop-GateInput "Missing PostgreSQL verification SQL: $verifyPath"
}

if ([string]::IsNullOrWhiteSpace($PsqlPath)) {
    $psqlCommand = Get-Command psql -ErrorAction SilentlyContinue
    if ($psqlCommand) {
        $PsqlPath = $psqlCommand.Source
    }
}

if ([string]::IsNullOrWhiteSpace($PsqlPath) -or -not (Test-Path -LiteralPath $PsqlPath)) {
    Stop-GateInput "psql was not found. Install PostgreSQL client tools or pass -PsqlPath. Docker is not used by this gate."
}

if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    Stop-GateInput "DatabaseUrl is required. Pass -DatabaseUrl or set DATABASE_URL to a disposable PostgreSQL database."
}

$arguments = @(
    $DatabaseUrl,
    "-v", "ON_ERROR_STOP=1",
    "-f", $schemaPath,
    "-f", $verifyPath
)

if ($DryRun) {
    Write-Output "GOAL-01 PostgreSQL gate dry run"
    Write-Output "workspace=$root"
    Write-Output "psql=$PsqlPath"
    Write-Output "schema=$schemaPath"
    Write-Output "verify=$verifyPath"
    exit 0
}

Write-Output "GOAL-01 PostgreSQL gate started"
& $PsqlPath @arguments
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    [Console]::Error.WriteLine("GOAL-01 PostgreSQL gate failed with exit code $exitCode")
    exit $exitCode
}

Write-Output "GOAL-01 PostgreSQL gate passed"
