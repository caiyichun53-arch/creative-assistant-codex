param(
    [string]$DatabaseUrl = $env:DATABASE_URL,
    [string]$PsqlPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Stop-GateInput($Message) {
    [Console]::Error.WriteLine($Message)
    exit 2
}

$schemaPaths = @(
    (Join-Path $PSScriptRoot "..\persistence\goal01_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal02_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "..\persistence\goal03_schema.postgres.sql"),
    (Join-Path $PSScriptRoot "goal_runtime_vertical_slice_schema.postgres.sql")
)
$verifyPath = Join-Path $PSScriptRoot "verify_runtime_vertical_slice_postgres.sql"

foreach ($path in ($schemaPaths + @($verifyPath))) {
    if (-not (Test-Path -LiteralPath $path)) {
        Stop-GateInput "Missing PostgreSQL gate file: $path"
    }
}

if (-not $PsqlPath) {
    $cmd = Get-Command psql -ErrorAction SilentlyContinue
    if ($cmd) {
        $PsqlPath = $cmd.Source
    }
}
if (-not $PsqlPath) {
    Stop-GateInput "psql was not found. Install PostgreSQL client tools or pass -PsqlPath. Docker is not used by this gate."
}
if (-not $DatabaseUrl) {
    Stop-GateInput "DatabaseUrl is required. Pass -DatabaseUrl or set DATABASE_URL to a disposable PostgreSQL database."
}

if ($DryRun) {
    Write-Output "GOAL-RUNTIME-VERTICAL-SLICE-01 PostgreSQL gate dry run"
    Write-Output "psql=$PsqlPath"
    Write-Output "database_url_configured=true"
    foreach ($path in $schemaPaths) { Write-Output "schema=$path" }
    Write-Output "verify=$verifyPath"
    exit 0
}

Write-Output "GOAL-RUNTIME-VERTICAL-SLICE-01 PostgreSQL gate started"
foreach ($path in $schemaPaths) {
    & $PsqlPath $DatabaseUrl -v ON_ERROR_STOP=1 -f $path
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $PsqlPath $DatabaseUrl -v ON_ERROR_STOP=1 -f $verifyPath
exit $LASTEXITCODE
