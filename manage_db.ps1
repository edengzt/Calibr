# Simple database management script for users without Docker
param (
    [Parameter(Mandatory=$true)]
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action
)

$CondaEnv = "kalshi"
$DataDir = "pgdata"
$LogFile = "pgdata/postgres.log"

# Check if conda environment is available
$env_exists = conda env list | Select-String -Pattern $CondaEnv
if (-not $env_exists) {
    Write-Error "Conda environment '$CondaEnv' not found. Please run 'conda env create -f environment.yml' first."
    exit 1
}

switch ($Action) {
    "start" {
        # Check if already running
        $pg_proc = Get-Process -Name postgres -ErrorAction SilentlyContinue
        if ($pg_proc) {
            Write-Host "PostgreSQL is already running." -ForegroundColor Green
            exit 0
        }

        # Initialize data dir if not exists
        if (-not (Test-Path $DataDir)) {
            Write-Host "Initializing database cluster in '$DataDir'..." -ForegroundColor Cyan
            conda run -n $CondaEnv initdb -D $DataDir -U postgres --auth=trust --no-instructions
        }

        Write-Host "Starting PostgreSQL server..." -ForegroundColor Cyan
        # Start using pg_ctl in background
        conda run -n $CondaEnv pg_ctl -D $DataDir -l $LogFile -W start
        
        # Wait a moment for startup
        Start-Sleep -Seconds 2
        $pg_proc = Get-Process -Name postgres -ErrorAction SilentlyContinue
        if ($pg_proc) {
            Write-Host "PostgreSQL started successfully." -ForegroundColor Green
        } else {
            Write-Error "PostgreSQL failed to start. Check '$LogFile' for details."
        }
    }
    "stop" {
        Write-Host "Stopping PostgreSQL server..." -ForegroundColor Cyan
        conda run -n $CondaEnv pg_ctl -D $DataDir -W stop
        Write-Host "PostgreSQL stopped." -ForegroundColor Green
    }
    "status" {
        $pg_proc = Get-Process -Name postgres -ErrorAction SilentlyContinue
        if ($pg_proc) {
            Write-Host "PostgreSQL is RUNNING." -ForegroundColor Green
            Get-Process -Name postgres | Select-Object Id, NPM, PM, WS, CPU | Format-Table
        } else {
            Write-Host "PostgreSQL is STOPPED." -ForegroundColor Yellow
        }
    }
    "restart" {
        & $MyInvocation.MyCommand.Path -Action "stop"
        Start-Sleep -Seconds 1
        & $MyInvocation.MyCommand.Path -Action "start"
    }
}
