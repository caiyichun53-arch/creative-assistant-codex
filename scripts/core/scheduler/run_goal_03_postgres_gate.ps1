param(
    [string]$DatabaseUrl = $env:DATABASE_URL,
    [string]$PsqlPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
$goal01SchemaPath = Join-Path $PSScriptRoot "..\persistence\goal01_schema.postgres.sql"
$goal02SchemaPath = Join-Path $PSScriptRoot "..\persistence\goal02_schema.postgres.sql"
$goal03SchemaPath = Join-Path $PSScriptRoot "..\persistence\goal03_schema.postgres.sql"
$verifyPath = Join-Path $PSScriptRoot "verify_goal_03_postgres.sql"

function Stop-GateInput {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
    exit 2
}

foreach ($path in @($goal01SchemaPath, $goal02SchemaPath, $goal03SchemaPath, $verifyPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        Stop-GateInput "Missing PostgreSQL gate file: $path"
    }
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
    "-f", $goal01SchemaPath,
    "-f", $goal02SchemaPath,
    "-f", $goal03SchemaPath,
    "-f", $verifyPath
)

if ($DryRun) {
    Write-Output "GOAL-03 PostgreSQL gate dry run"
    Write-Output "workspace=$root"
    Write-Output "psql=$PsqlPath"
    Write-Output "goal01_schema=$goal01SchemaPath"
    Write-Output "goal02_schema=$goal02SchemaPath"
    Write-Output "goal03_schema=$goal03SchemaPath"
    Write-Output "verify=$verifyPath"
    exit 0
}

Write-Output "GOAL-03 PostgreSQL gate started"
& $PsqlPath @arguments
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    [Console]::Error.WriteLine("GOAL-03 PostgreSQL gate failed with exit code $exitCode")
    exit $exitCode
}

Write-Output "GOAL-03 PostgreSQL gate passed"
