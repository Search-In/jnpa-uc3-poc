<#
.SYNOPSIS
    UC1-001 - one-command cold start for the complete UC-1 stack.

.DESCRIPTION
    Postgres -> jnpa_v3_local -> migrations -> demo account -> FastAPI gateway
    :8000 -> UC-1 dashboard (poc_1) :5173, then verifies the stack end to end
    against real JNPA data.

    Safe to run repeatedly. Every step decides for itself whether there is work
    to do, so a second run reports SKIPPED where the first reported STARTED and
    never touches data that is already there.

    This script ORCHESTRATES; it does not reimplement. Migrations go through
    scripts/migrate.py and its core.schema_migrations ledger, the demo account
    through scripts/seed_auth_users.py, and the marine DDL (0038-0052) is left
    to the gateway's own ensure_*_schema boot path. Nothing here duplicates any
    of that.

.PARAMETER Down
    Stop the gateway and dashboard this script started. Leaves Postgres and the
    database untouched - it never drops data.

.PARAMETER StatusOnly
    Report what is running and answer the verification checks. Starts nothing,
    creates nothing, writes nothing.

.PARAMETER SkipFrontend
    Bring up Postgres + database + gateway only. For API work and for CI, where
    a Vite dev server is dead weight.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uc1-up.ps1

.NOTES
    Exit codes
        0  stack is up and every verification passed
        1  a component failed to start (the failing step prints why)
        2  the stack is up but a verification did not pass - see the summary
#>
[CmdletBinding()]
param(
    [switch]$Down,
    [switch]$StatusOnly,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunDir   = Join-Path $RepoRoot '.uc1-run'
$EnvFile  = Join-Path $RepoRoot '.env.uc1-local'
$ExampleFile = Join-Path $RepoRoot '.env.uc1-local.example'

# Collected as the run proceeds and printed as one table at the end, so the
# operator reads a single verdict instead of scrolling back through output.
$script:Summary = New-Object System.Collections.ArrayList
# Set by a verification that did not pass. Distinguishes "the stack is broken"
# (exit 1) from "the stack is up but an acceptance check failed" (exit 2) -
# they need very different responses from whoever ran this.
$script:VerifyFailed = $false

# ---------------------------------------------------------------- output
function Write-Step($text)  { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)    { Write-Host "    [OK]      $text" -ForegroundColor Green }
function Write-Skip($text)  { Write-Host "    [SKIPPED] $text" -ForegroundColor DarkGray }
function Write-Note($text)  { Write-Host "    $text" -ForegroundColor Gray }
function Write-Warn2($text) { Write-Host "    [WARN]    $text" -ForegroundColor Yellow }

function Add-Summary($component, $status, $detail) {
    [void]$script:Summary.Add([pscustomobject]@{
        Component = $component; Status = $status; Detail = $detail
    })
}

function Stop-Fail($message, $hint) {
    Write-Host ""
    Write-Host "FAILED: $message" -ForegroundColor Red
    if ($hint) { Write-Host "" ; Write-Host $hint -ForegroundColor Yellow }
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------- helpers

<#
  Poll until Probe returns $true. This is the only waiting primitive in the
  script: UC1-001 forbids sleeping a fixed number of seconds and hoping, and a
  readiness poll is also what makes a re-run fast (an already-healthy component
  satisfies its probe on the first attempt instead of costing a fixed delay).
#>
function Wait-For {
    param(
        [scriptblock]$Probe,
        [int]$TimeoutSec = 90,
        [string]$Label = 'component',
        [int]$IntervalMs = 750
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $spin = 0
    while ((Get-Date) -lt $deadline) {
        try { if (& $Probe) { if ($spin -gt 0) { Write-Host "" }; return $true } } catch { }
        Start-Sleep -Milliseconds $IntervalMs
        $spin++
        if ($spin % 4 -eq 0) { Write-Host "." -NoNewline -ForegroundColor DarkGray }
    }
    if ($spin -gt 0) { Write-Host "" }
    return $false
}

function Read-DotEnv([string]$path) {
    $map = @{}
    if (-not (Test-Path $path)) { return $map }
    foreach ($line in Get-Content $path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        $map[$k] = $v
    }
    return $map
}

function Get-Cfg($map, $key, $fallback) {
    if ($map.ContainsKey($key) -and $map[$key] -ne '') { return $map[$key] }
    return $fallback
}

# Locate the PostgreSQL client binaries. psql/pg_restore/pg_dump are not
# usually on PATH after a Windows installer run, so fall back to the highest
# versioned install under Program Files.
function Resolve-PgBin {
    $probe = Get-Command psql -ErrorAction SilentlyContinue
    if ($probe) { return (Split-Path -Parent $probe.Source) }
    $root = 'C:\Program Files\PostgreSQL'
    if (Test-Path $root) {
        $dirs = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                Sort-Object { [int]($_.Name -replace '\D', '0') } -Descending
        foreach ($d in $dirs) {
            $candidate = Join-Path $d.FullName 'bin\psql.exe'
            if (Test-Path $candidate) { return (Join-Path $d.FullName 'bin') }
        }
    }
    return $null
}

# A libpq URI with every component percent-encoded, so a password containing
# @ : / ? # cannot silently corrupt the DSN.
function New-Dsn($scheme, $user, $password, $hostName, $port, $db) {
    $u = [uri]::EscapeDataString($user)
    $p = [uri]::EscapeDataString($password)
    return "$scheme`://$u`:$p@$hostName`:$port/$db"
}

function Hide-Secret($dsn) { return ($dsn -replace '://([^:/@]+):[^@]*@', '://$1:****@') }

function Test-PortListening($portNumber) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $portNumber -ErrorAction SilentlyContinue
    return ($null -ne $c)
}

function Invoke-Api {
    param([string]$Url, [string]$Method = 'GET', $Body = $null, [string]$Token = $null, [int]$TimeoutSec = 30)
    $headers = @{}
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    # NOT $args - that is an automatic variable and splatting it silently
    # drops everything set here.
    $params = @{ Uri = $Url; Method = $Method; TimeoutSec = $TimeoutSec; UseBasicParsing = $true }
    if ($headers.Count -gt 0) { $params['Headers'] = $headers }
    if ($null -ne $Body) {
        $params['Body'] = ($Body | ConvertTo-Json -Compress)
        $params['ContentType'] = 'application/json'
    }
    $r = Invoke-WebRequest @params
    return ($r.Content | ConvertFrom-Json)
}

# psql -Atc against the local server. Returns trimmed stdout; throws on a
# non-zero exit so a connection failure never reads as an empty result.
function Invoke-Psql {
    param([string]$Database, [string]$Sql)
    $out = & $script:Psql -h $script:PgHost -p $script:PgPort -U $script:PgUser -d $Database -w -Atc $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("psql failed: " + ($out -join ' ')) }
    return ($out -join "`n").Trim()
}

# ============================================================================
# Configuration
# ============================================================================
if (-not (Test-Path $EnvFile)) {
    if (-not (Test-Path $ExampleFile)) {
        Stop-Fail "Neither .env.uc1-local nor .env.uc1-local.example exists in $RepoRoot." $null
    }
    Copy-Item $ExampleFile $EnvFile
    Stop-Fail "No .env.uc1-local - one has just been created from the example." @"
Open $EnvFile and set UC1_PG_PASSWORD (the local PostgreSQL superuser
password). Nothing else is required for a machine that already has
$((Read-DotEnv $ExampleFile)['UC1_DUMP_PATH']).

Then run this script again.
"@
}

$cfg = Read-DotEnv $EnvFile

$script:PgHost = Get-Cfg $cfg 'UC1_PG_HOST' '127.0.0.1'
$script:PgPort = Get-Cfg $cfg 'UC1_PG_PORT' '5432'
$script:PgUser = Get-Cfg $cfg 'UC1_PG_SUPERUSER' 'postgres'
$PgPassword    = Get-Cfg $cfg 'UC1_PG_PASSWORD' ''
$PgService     = Get-Cfg $cfg 'UC1_PG_SERVICE' ''
$DbName        = Get-Cfg $cfg 'UC1_DB_NAME' 'jnpa_v3_local'
$DumpRel       = Get-Cfg $cfg 'UC1_DUMP_PATH' 'backups/jnpa_v3_local.dump'
$SourceDsn     = Get-Cfg $cfg 'UC1_SOURCE_DSN' ''
$ExcludeData   = Get-Cfg $cfg 'UC1_DUMP_EXCLUDE_DATA' ''
$DemoUser      = Get-Cfg $cfg 'UC1_DEMO_USER' 'admin'
$DemoPassword  = Get-Cfg $cfg 'UC1_DEMO_PASSWORD' 'admin123'
$GatewayHost   = Get-Cfg $cfg 'UC1_GATEWAY_HOST' '127.0.0.1'
$GatewayPort   = [int](Get-Cfg $cfg 'UC1_GATEWAY_PORT' '8000')
$JwtSecret     = Get-Cfg $cfg 'UC1_AUTH_JWT_SECRET' ''
$Poc1Rel       = Get-Cfg $cfg 'UC1_POC1_DIR' '../jnpa_poc_1'
$WebPort       = [int](Get-Cfg $cfg 'UC1_WEB_PORT' '5173')

$DumpPath = if ([System.IO.Path]::IsPathRooted($DumpRel)) { $DumpRel } else { Join-Path $RepoRoot $DumpRel }
$Poc1Dir  = if ([System.IO.Path]::IsPathRooted($Poc1Rel)) { $Poc1Rel } else { (Join-Path $RepoRoot $Poc1Rel) }
$GatewayBase = "http://$GatewayHost`:$GatewayPort"

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$GatewayPidFile = Join-Path $RunDir 'gateway.pid'
$WebPidFile     = Join-Path $RunDir 'web.pid'
$GatewayLog     = Join-Path $RunDir 'gateway.log'
$WebLog         = Join-Path $RunDir 'web.log'

# ============================================================================
# -Down: stop what this script started
# ============================================================================
if ($Down) {
    Write-Step 'Stopping UC-1 stack'
    foreach ($pair in @(@{f=$WebPidFile;n='dashboard (5173)'}, @{f=$GatewayPidFile;n='gateway (8000)'})) {
        if (Test-Path $pair.f) {
            $procId = [int]((Get-Content $pair.f | Select-Object -First 1).Trim())
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($p) {
                # Kill the tree: uvicorn --reload and `npm run dev` both fork a
                # child that owns the socket, so stopping the parent alone
                # leaves the port bound and the next run reports a phantom
                # "already running".
                & taskkill /PID $procId /T /F 2>&1 | Out-Null
                Write-Ok "$($pair.n) stopped (pid $procId)"
            } else {
                Write-Skip "$($pair.n) was not running"
            }
            Remove-Item $pair.f -Force
        } else {
            Write-Skip "$($pair.n) - no pid file, nothing this script started"
        }
    }
    Write-Host ""
    Write-Host "Postgres and $DbName were left untouched." -ForegroundColor Gray
    Write-Host ""
    exit 0
}

Write-Host ""
Write-Host "UC1-001 - one-command cold start" -ForegroundColor White
Write-Host "repo: $RepoRoot"
Write-Host "poc_1: $Poc1Dir"
if ($StatusOnly) { Write-Host "mode: STATUS ONLY (nothing will be started or written)" -ForegroundColor Yellow }

# ============================================================================
# 1. Prerequisites
# ============================================================================
Write-Step '1/9  Prerequisites'

$script:PgBin = Resolve-PgBin
if (-not $script:PgBin) {
    Stop-Fail "PostgreSQL client tools not found." @"
psql/pg_restore are needed to create and load $DbName. Install the PostgreSQL
client (or the full server) and re-run. Looked on PATH and under
C:\Program Files\PostgreSQL\<version>\bin.
"@
}
$script:Psql   = Join-Path $script:PgBin 'psql.exe'
$PgRestore     = Join-Path $script:PgBin 'pg_restore.exe'
$PgDump        = Join-Path $script:PgBin 'pg_dump.exe'
$PgIsReady     = Join-Path $script:PgBin 'pg_isready.exe'
Write-Ok "PostgreSQL client: $script:PgBin"

$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sys) {
        Stop-Fail "No Python found." "Expected $VenvPython, or `python` on PATH (3.11+)."
    }
    $VenvPython = $sys.Source
    Write-Warn2 "Using system Python ($VenvPython) - .venv not found."
    Write-Note  "The gateway needs its dependencies installed there; if it fails to boot, create the venv first."
} else {
    Write-Ok "Python: $VenvPython"
}

# uvicorn must be importable, or the gateway step fails much later with a
# confusing traceback in a log file the operator has not been told about yet.
& $VenvPython -c "import uvicorn, fastapi" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-Fail "The Python environment cannot import uvicorn/fastapi." @"
Install the gateway dependencies into $VenvPython, e.g.

    $VenvPython -m pip install -e "$RepoRoot\shared" -e "$RepoRoot\gateway"
"@
}
Write-Ok 'uvicorn + fastapi importable'

# scripts/migrate.py connects with psycopg 3. It is not pulled in by the gateway
# package, so a venv built only for running the API is missing it - and without
# this check that surfaces as a failure in step 5, several minutes in.
& $VenvPython -c "import psycopg" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-Fail "The Python environment cannot import psycopg (needed by scripts/migrate.py)." @"
    $VenvPython -m pip install "psycopg[binary]"
"@
}
Write-Ok 'psycopg importable (migration runner)'

if (-not $SkipFrontend) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
    if (-not $npm) { Stop-Fail "npm not found - needed to run the UC-1 dashboard on $WebPort." "Install Node.js 20+." }
    $script:Npm = $npm.Source
    if (-not (Test-Path $Poc1Dir)) {
        Stop-Fail "poc_1 not found at $Poc1Dir." "Set UC1_POC1_DIR in $EnvFile to the jnpa_poc_1 checkout."
    }
    Write-Ok "npm: $script:Npm"
    Write-Ok "poc_1: $Poc1Dir"
}

if ($PgPassword -eq '' -and -not $StatusOnly) {
    Stop-Fail "UC1_PG_PASSWORD is empty in $EnvFile." @"
The superuser password for $($script:PgHost):$($script:PgPort) is required to create
and load $DbName. Set UC1_PG_PASSWORD and re-run.
"@
}
$env:PGPASSWORD = $PgPassword

# ============================================================================
# 2. PostgreSQL
# ============================================================================
Write-Step '2/9  PostgreSQL'

$pgUp = { & $PgIsReady -h $script:PgHost -p $script:PgPort -q 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }

if (& $pgUp) {
    Write-Skip "already accepting connections on $($script:PgHost):$($script:PgPort)"
    Add-Summary 'Postgres' 'ALREADY RUNNING' "$($script:PgHost):$($script:PgPort)"
} elseif ($StatusOnly) {
    Write-Warn2 "not accepting connections on $($script:PgHost):$($script:PgPort)"
    Add-Summary 'Postgres' 'DOWN' "$($script:PgHost):$($script:PgPort)"
    $script:VerifyFailed = $true
} else {
    if ($PgService -eq '') {
        Stop-Fail "PostgreSQL is not accepting connections on $($script:PgHost):$($script:PgPort)." @"
UC1_PG_SERVICE is blank, so this script will not try to start a service.
Start PostgreSQL yourself, or set UC1_PG_SERVICE in $EnvFile.
"@
    }
    $svc = Get-Service -Name $PgService -ErrorAction SilentlyContinue
    if (-not $svc) {
        Stop-Fail "No Windows service named '$PgService'." @"
Available PostgreSQL services:
$((Get-Service | Where-Object { $_.Name -like '*postgres*' } | ForEach-Object { '  ' + $_.Name + '  (' + $_.Status + ')' }) -join "`n")

Set UC1_PG_SERVICE in $EnvFile to the right one.
"@
    }
    Write-Note "starting service '$PgService'"
    Start-Service -Name $PgService
    if (-not (Wait-For -Probe $pgUp -TimeoutSec 60 -Label 'postgres')) {
        Stop-Fail "PostgreSQL service '$PgService' started but never accepted connections on $($script:PgHost):$($script:PgPort)." "Check the PostgreSQL server log."
    }
    Write-Ok "started and accepting connections"
    Add-Summary 'Postgres' 'STARTED' "service $PgService"
}

# Verify the credential and the server version now, against the maintenance
# database - before any step that would fail confusingly on a bad password.
if (& $pgUp) {
    try { $pgVersionNum = [int](Invoke-Psql -Database 'postgres' -Sql 'SHOW server_version_num;') }
    catch {
        Stop-Fail "Cannot authenticate to PostgreSQL as '$($script:PgUser)'." @"
$($_.Exception.Message)

Check UC1_PG_PASSWORD in $EnvFile.
"@
    }
    $pgMajor = [math]::Floor($pgVersionNum / 10000)
    if ($pgMajor -lt 16) {
        Stop-Fail "PostgreSQL $pgMajor is older than the 16 UC1-001 requires." "Upgrade, or point UC1_PG_HOST/UC1_PG_PORT at a 16+ server."
    }
    if ($pgMajor -ne 16) {
        Write-Warn2 "server is PostgreSQL $pgMajor; UC1-001 specifies 16. Accepted (>= 16), recorded as a deviation."
    } else {
        Write-Ok "PostgreSQL $pgMajor"
    }
}

# ============================================================================
# 3. Database
# ============================================================================
Write-Step "3/9  Database $DbName"

$dbExists = (Invoke-Psql -Database 'postgres' -Sql "SELECT 1 FROM pg_database WHERE datname = '$DbName';") -eq '1'

if ($dbExists) {
    Write-Skip "$DbName already exists"
    Add-Summary 'Database' 'EXISTS' $DbName
} elseif ($StatusOnly) {
    Write-Warn2 "$DbName does not exist"
    Add-Summary 'Database' 'MISSING' $DbName
    $script:VerifyFailed = $true
} else {
    Invoke-Psql -Database 'postgres' -Sql "CREATE DATABASE `"$DbName`" ENCODING 'UTF8';" | Out-Null
    Write-Ok "$DbName created"
    Add-Summary 'Database' 'CREATED' $DbName
}

$AppDsnLibpq   = New-Dsn 'postgresql' $script:PgUser $PgPassword $script:PgHost $script:PgPort $DbName
$AppDsnAsyncpg = New-Dsn 'postgresql+asyncpg' $script:PgUser $PgPassword $script:PgHost $script:PgPort $DbName

# ============================================================================
# 4. JNPA corpus
# ============================================================================
Write-Step '4/9  JNPA corpus'

# "Is the corpus loaded?" is answered by the vessel-call spine, because that is
# what the UC-1 dashboard and the turnaround KPI actually read. A database that
# has the schema but no calls is not loaded, whatever else is in it.
$callCount = 0
try { $callCount = [int](Invoke-Psql -Database $DbName -Sql "SELECT count(*) FROM core.vessel_call;") } catch { $callCount = -1 }

if ($callCount -gt 0) {
    Write-Skip "core.vessel_call already holds $callCount rows - not reloading"
    Add-Summary 'Corpus' 'ALREADY LOADED' "$callCount vessel calls"
} elseif ($StatusOnly) {
    Write-Warn2 'corpus not loaded'
    Add-Summary 'Corpus' 'MISSING' 'core.vessel_call empty or absent'
    $script:VerifyFailed = $true
} else {
    if (-not (Test-Path $DumpPath)) {
        if ($SourceDsn -eq '') {
            Stop-Fail "No corpus dump at $DumpPath, and UC1_SOURCE_DSN is blank." @"
Neither repository ships a UC-1 seed or fixture, so the corpus has to come from
an archive. Either:

  * copy the dump to $DumpPath  (how a demo machine is provisioned), or
  * set UC1_SOURCE_DSN in $EnvFile to the shared instance and re-run - this
    script will then produce the archive with pg_dump before restoring it.
"@
        }
        Write-Note "dump absent - creating it from the source instance (this transfers the corpus once)"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DumpPath) | Out-Null
        $dumpArgs = @('-d', $SourceDsn, '-Fc', '-Z6', '--no-owner', '--no-privileges', '-f', $DumpPath)
        foreach ($t in ($ExcludeData -split ',')) {
            $t = $t.Trim()
            if ($t -ne '') { $dumpArgs += @("--exclude-table-data=$t") }
        }
        & $PgDump @dumpArgs
        if ($LASTEXITCODE -ne 0) { Stop-Fail "pg_dump from the source instance failed (exit $LASTEXITCODE)." "Check UC1_SOURCE_DSN and network access to that host." }
        Write-Ok "dump written: $DumpPath ($([math]::Round((Get-Item $DumpPath).Length / 1MB, 1)) MB)"
    } else {
        Write-Note "restoring $DumpPath ($([math]::Round((Get-Item $DumpPath).Length / 1MB, 1)) MB)"
    }

    $restoreLog = Join-Path $RunDir 'pg_restore.log'
    # No --exit-on-error: a fresh database legitimately reports errors for
    # objects the archive cannot own (extensions, roles). The load is judged by
    # the row assertion below, not by pg_restore's exit code, and the full log
    # is kept so a genuine failure is diagnosable.
    & $PgRestore --no-owner --no-privileges --dbname $AppDsnLibpq $DumpPath 2> $restoreLog
    $restoreExit = $LASTEXITCODE

    try { $callCount = [int](Invoke-Psql -Database $DbName -Sql "SELECT count(*) FROM core.vessel_call;") } catch { $callCount = 0 }
    if ($callCount -le 0) {
        Stop-Fail "The restore left core.vessel_call empty (pg_restore exit $restoreExit)." @"
Last lines of $restoreLog

$((Get-Content $restoreLog -Tail 25) -join "`n")
"@
    }
    if ($restoreExit -ne 0) {
        Write-Warn2 "pg_restore exited $restoreExit; the corpus loaded anyway. Details: $restoreLog"
    }
    Write-Ok "corpus restored - $callCount vessel calls"
    Add-Summary 'Corpus' 'LOADED' "$callCount vessel calls"
}

# ============================================================================
# 5. Migrations
# ============================================================================
Write-Step '5/9  Migrations'

if ($StatusOnly) {
    & $VenvPython (Join-Path $RepoRoot 'scripts\migrate.py') --status --dsn $AppDsnLibpq 2>&1 | Select-Object -Last 12 | ForEach-Object { Write-Note $_ }
    Add-Summary 'Migrations' 'REPORTED' 'see --status output above'
} else {
    # v3 (0100..0127 + backfills), including the 0101 UC1-001 names. Idempotent
    # and ledgered by core.schema_migrations, so a restored database that
    # already carries them applies nothing.
    Push-Location $RepoRoot
    try {
        & $VenvPython 'scripts\migrate.py' --dsn $AppDsnLibpq
        if ($LASTEXITCODE -ne 0) { Stop-Fail "infra/postgres/v3 migrations failed." "Re-run `"$VenvPython scripts\migrate.py --status --dsn ...`" for the ledger state." }

        # 0036 + 0037 live in the OTHER directory, which has no runner of its
        # own and whose other files include backfills. --only applies exactly
        # these two through the same ledger - no second migration mechanism.
        & $VenvPython 'scripts\migrate.py' --dsn $AppDsnLibpq --dir 'infra/postgres/migrations' --only '0036,0037'
        if ($LASTEXITCODE -ne 0) { Stop-Fail "manual migrations 0036/0037 failed." $null }
    } finally { Pop-Location }

    Write-Ok 'v3 (incl. 0101) + manual 0036/0037 applied or already present'
    Write-Note 'marine 0038-0052: applied by the gateway at boot (gateway/marine_ext.py, JNPA_RUNTIME_DDL=1) - not duplicated here'
    Add-Summary 'Migrations' 'APPLIED' '0036, 0037, v3 incl. 0101'
}

# ============================================================================
# 6. Demo account
# ============================================================================
Write-Step "6/9  Demo account ($DemoUser)"

# The gateway env is assembled here and inherited by every child process below
# (seeder, uvicorn). A process env var outranks the .env.local entry of the
# same name, which is what keeps this run on the LOCAL database while
# .env.local continues to point the normal workflow at RDS.
if ($JwtSecret -eq '') {
    if ($StatusOnly) {
        $JwtSecret = 'status-only-placeholder'
    } else {
        $JwtSecret = (& $VenvPython -c "import secrets;print(secrets.token_hex(32))").Trim()
        Add-Content -Path $EnvFile -Value "`n# Generated by scripts/uc1-up.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm')."
        Add-Content -Path $EnvFile -Value "UC1_AUTH_JWT_SECRET=$JwtSecret"
        Write-Note 'generated AUTH_JWT_SECRET and saved it to .env.uc1-local'
    }
}

$env:POSTGRES_DSN       = $AppDsnAsyncpg
$env:RFID_POSTGRES_DSN  = $AppDsnLibpq
$env:APP_ENV            = 'development'
$env:JNPA_RUNTIME_DDL   = '1'
$env:AUTH_ENABLED       = 'true'
$env:AUTH_DEV_TOKENS    = 'false'
$env:AUTH_JWT_SECRET    = $JwtSecret
$env:ALLOW_FALLBACK     = 'true'
$env:JNPA_SYNC_ENABLED  = 'false'   # no external JNPA Port-Data polling on a demo box
$env:OTEL_SDK_DISABLED  = 'true'    # no Jaeger in this profile

if ($StatusOnly) {
    Write-Skip 'not seeding (status only)'
} else {
    $env:SEED_MUST_CHANGE_PASSWORD = 'false'
    Set-Item -Path "env:SEED_$($DemoUser.ToUpper())_PASSWORD" -Value $DemoPassword
    Push-Location $RepoRoot
    try {
        $seedOut = & $VenvPython 'scripts\seed_auth_users.py' --user $DemoUser --role ADMIN --full-name 'UC-1 Demo Administrator' --no-force-password-change 2>&1
        $seedExit = $LASTEXITCODE
    } finally { Pop-Location }
    if ($seedExit -ne 0) {
        Stop-Fail "Seeding the demo account failed." (($seedOut | Out-String))
    }
    if (($seedOut | Out-String) -match 'exists') {
        Write-Skip "$DemoUser already exists - password left alone"
        Add-Summary 'Demo account' 'EXISTS' $DemoUser
    } else {
        Write-Ok "$DemoUser created (role ADMIN)"
        Add-Summary 'Demo account' 'CREATED' $DemoUser
    }
}

# ============================================================================
# 7. Gateway
# ============================================================================
Write-Step "7/9  Gateway :$GatewayPort"

# "Is OUR gateway already up?" - a 200 from /healthz is not enough, because any
# process could own the port. The service name in the payload is the check.
$gatewayHealthy = {
    try {
        $h = Invoke-Api -Url "$GatewayBase/healthz" -TimeoutSec 5
        return ($h.service -eq 'jnpa-gateway')
    } catch { return $false }
}

if (& $gatewayHealthy) {
    Write-Skip "already serving on $GatewayBase"
    Add-Summary 'Gateway' 'ALREADY RUNNING' $GatewayBase
} elseif ($StatusOnly) {
    Write-Warn2 "not responding on $GatewayBase"
    Add-Summary 'Gateway' 'DOWN' $GatewayBase
    $script:VerifyFailed = $true
} else {
    if (Test-PortListening $GatewayPort) {
        Stop-Fail "Port $GatewayPort is in use but does not answer as the JNPA gateway." @"
Something else owns $GatewayPort. Identify it with

    Get-NetTCPConnection -State Listen -LocalPort $GatewayPort | Select OwningProcess

then stop it, or set UC1_GATEWAY_PORT in $EnvFile.
"@
    }
    $p = Start-Process -FilePath $VenvPython `
        -ArgumentList @('-m', 'uvicorn', 'gateway.main:app', '--host', $GatewayHost, '--port', "$GatewayPort") `
        -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $GatewayLog -RedirectStandardError "$GatewayLog.err"
    Set-Content -Path $GatewayPidFile -Value $p.Id
    Write-Note "uvicorn started (pid $($p.Id)), waiting for /healthz"

    if (-not (Wait-For -Probe $gatewayHealthy -TimeoutSec 120 -Label 'gateway')) {
        $tail = @()
        if (Test-Path "$GatewayLog.err") { $tail += (Get-Content "$GatewayLog.err" -Tail 30) }
        if (Test-Path $GatewayLog)       { $tail += (Get-Content $GatewayLog -Tail 20) }
        Stop-Fail "Gateway did not become healthy on $GatewayBase within 120s." @"
Last log lines ($GatewayLog):

$($tail -join "`n")
"@
    }
    Write-Ok "healthy on $GatewayBase"
    Add-Summary 'Gateway' 'STARTED' "$GatewayBase (pid $($p.Id))"
}

# ============================================================================
# 8. Dashboard
# ============================================================================
Write-Step "8/9  UC-1 dashboard :$WebPort"

$webUp = {
    foreach ($h in @('127.0.0.1', 'localhost')) {
        try {
            $r = Invoke-WebRequest -Uri "http://$h`:$WebPort/" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

if ($SkipFrontend) {
    Write-Skip '-SkipFrontend'
    Add-Summary 'Dashboard' 'SKIPPED' '-SkipFrontend'
} elseif (& $webUp) {
    Write-Skip "already serving on http://localhost:$WebPort"
    Add-Summary 'Dashboard' 'ALREADY RUNNING' "http://localhost:$WebPort"
} elseif ($StatusOnly) {
    Write-Warn2 "not responding on http://localhost:$WebPort"
    Add-Summary 'Dashboard' 'DOWN' "http://localhost:$WebPort"
    $script:VerifyFailed = $true
} else {
    if (-not (Test-Path (Join-Path $Poc1Dir 'node_modules'))) {
        Write-Note 'node_modules absent - running npm install (first run only, several minutes)'
        Push-Location $Poc1Dir
        try { & $script:Npm install } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Stop-Fail "npm install failed in $Poc1Dir." $null }
    }

    # Handed to Vite as REAL environment variables, which outrank poc_1/.env.
    # The demo credential therefore lives in exactly one file (.env.uc1-local)
    # and is never written into the frontend checkout.
    $env:VITE_GATEWAY_URL   = $GatewayBase
    $env:VITE_UC3_ENABLED   = 'true'
    $env:VITE_UC3_USERNAME  = $DemoUser
    $env:VITE_UC3_PASSWORD  = $DemoPassword

    $wp = Start-Process -FilePath $script:Npm -ArgumentList @('run', 'dev', '--', '--port', "$WebPort", '--strictPort') `
        -WorkingDirectory $Poc1Dir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $WebLog -RedirectStandardError "$WebLog.err"
    Set-Content -Path $WebPidFile -Value $wp.Id
    Write-Note "vite started (pid $($wp.Id)), waiting for http://localhost:$WebPort"

    if (-not (Wait-For -Probe $webUp -TimeoutSec 180 -Label 'dashboard')) {
        $tail = @()
        if (Test-Path "$WebLog.err") { $tail += (Get-Content "$WebLog.err" -Tail 30) }
        if (Test-Path $WebLog)       { $tail += (Get-Content $WebLog -Tail 20) }
        Stop-Fail "The dashboard did not answer on http://localhost:$WebPort within 180s." @"
Last log lines ($WebLog):

$($tail -join "`n")
"@
    }
    Write-Ok "serving on http://localhost:$WebPort"
    Add-Summary 'Dashboard' 'STARTED' "http://localhost:$WebPort (pid $($wp.Id))"
}

# ============================================================================
# 9. Verification
# ============================================================================
Write-Step '9/9  Verification'

# Login with the seeded account. Authentication is enforced (AUTH_ENABLED=true),
# so this both proves the demo credential works and yields the bearer the
# remaining checks need - the dashboard performs exactly this exchange.
$token = $null
try {
    $login = Invoke-Api -Url "$GatewayBase/api/auth/login" -Method POST -Body @{ username = $DemoUser; password = $DemoPassword }
    $token = $login.access_token
    Write-Ok "login $DemoUser -> role $($login.role), auth_enabled=$($login.auth_enabled)"
    Add-Summary 'Login' 'PASS' "$DemoUser / role $($login.role)"
} catch {
    Write-Warn2 "login failed: $($_.Exception.Message)"
    Add-Summary 'Login' 'FAIL' $_.Exception.Message
    $script:VerifyFailed = $true
}

# Real-data proof. /api/marine/calls/stats reads core.vessel_call directly and
# computes avg_turnaround_hours from the stored ATA/ATD actuals, so a plausible
# figure here IS the corpus talking, not a fixture.
if ($token) {
    try {
        $stats = Invoke-Api -Url "$GatewayBase/api/marine/calls/stats" -Token $token
        if ([int]$stats.total -le 0) { throw "core.vessel_call reports 0 calls" }
        Write-Ok "marine corpus: $($stats.total) vessel calls, $($stats.arrived) arrived, avg turnaround $($stats.avg_turnaround_hours) h"
        Add-Summary 'Real JNPA data' 'PASS' "$($stats.total) calls, avg TAT $($stats.avg_turnaround_hours) h"
    } catch {
        Write-Warn2 "marine corpus check failed: $($_.Exception.Message)"
        Add-Summary 'Real JNPA data' 'FAIL' $_.Exception.Message
        $script:VerifyFailed = $true
    }
}

# The UC1-001 acceptance endpoint. Reported honestly: a 404 is a MISSING
# BACKEND ROUTE, not a cold-start defect, and the script must not pass it off
# as either success or a stack failure.
$kpiUrl = "$GatewayBase/api/marine/kpis?window_days=30"
if ($token) {
    $kpiStatus = 0
    try {
        $kpis = Invoke-Api -Url $kpiUrl -Token $token
        $kpiStatus = 200
    } catch {
        if ($_.Exception.Response) { $kpiStatus = [int]$_.Exception.Response.StatusCode }
    }
    if ($kpiStatus -eq 200) {
        $tat = $kpis.kpis | Where-Object { $_.key -eq 'AVG_TAT' } | Select-Object -First 1
        if ($tat) {
            Write-Ok "GET /api/marine/kpis?window_days=30 -> AVG_TAT = $($tat.value) $($tat.unit), n = $($tat.n)"
            Add-Summary 'KPI API (UC1-001)' 'PASS' "AVG_TAT $($tat.value) $($tat.unit), n=$($tat.n)"
        } else {
            Write-Warn2 'KPI endpoint answered 200 but carried no AVG_TAT entry.'
            Add-Summary 'KPI API (UC1-001)' 'FAIL' 'no AVG_TAT in response'
            $script:VerifyFailed = $true
        }
    } elseif ($kpiStatus -eq 404) {
        Write-Warn2 'GET /api/marine/kpis?window_days=30 -> 404 NOT IMPLEMENTED'
        Write-Note  'This route does not exist in this gateway revision. The whole /api/marine/*'
        Write-Note  'dashboard family that poc_1 consumes (berths, berthing-plan, kpis,'
        Write-Note  'vessel-states, tides, pilotage-performance, arrivals-departures,'
        Write-Note  'calls/{id}/arrival-times, reference/kpi-baselines) is absent from the'
        Write-Note  'backend. That is a backend gap, NOT a cold-start failure - the stack'
        Write-Note  'above is up and serving the real corpus.'
        Add-Summary 'KPI API (UC1-001)' 'NOT IMPLEMENTED' '404 - backend route missing (separate ticket)'
        $script:VerifyFailed = $true
    } else {
        Write-Warn2 "GET /api/marine/kpis?window_days=30 -> HTTP $kpiStatus"
        Add-Summary 'KPI API (UC1-001)' 'FAIL' "HTTP $kpiStatus"
        $script:VerifyFailed = $true
    }
}

# ============================================================================
# Summary
# ============================================================================
Write-Host ""
Write-Host "----------------------------------------------------------------------"
$script:Summary | Format-Table -AutoSize | Out-String | Write-Host
Write-Host "----------------------------------------------------------------------"
Write-Host "  Dashboard   http://localhost:$WebPort"
Write-Host "  Gateway     $GatewayBase        (docs: $GatewayBase/docs)"
Write-Host "  Login       $DemoUser / $DemoPassword"
Write-Host "  Database    $(Hide-Secret $AppDsnLibpq)"
Write-Host "  Logs        $RunDir"
Write-Host "  Stop        powershell -ExecutionPolicy Bypass -File scripts\uc1-up.ps1 -Down"
Write-Host "----------------------------------------------------------------------"
Write-Host ""

if ($script:VerifyFailed) {
    Write-Host "Stack is UP; one or more verifications did not pass (see the table)." -ForegroundColor Yellow
    Write-Host ""
    exit 2
}
Write-Host "UC-1 stack is up and verified." -ForegroundColor Green
Write-Host ""
exit 0
