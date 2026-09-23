param(
    [switch]$NoBrowser,
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host 'Creating Python 3.12 virtual environment...'
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python 3.12 virtual environment.' }
}

Write-Host 'Ensuring backend dependencies are installed...'
& $Python -m pip install -e '.\backend'
if ($LASTEXITCODE -ne 0) { throw 'Backend installation failed.' }

if (-not $SkipFrontendBuild) {
    Push-Location (Join-Path $RepoRoot 'frontend')
    try {
        if (-not (Test-Path 'node_modules')) {
            Write-Host 'Installing frontend dependencies...'
            & npm ci
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation from lockfile failed.' }
        }
        Write-Host 'Building frontend...'
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    }
    finally {
        Pop-Location
    }
}

$LaunchArgs = @('-m', 'htdt')
if ($NoBrowser) {
    $LaunchArgs += '--no-browser'
}

Write-Host 'Starting Home Theater Digital Twin. Press Ctrl+C to stop.'
& $Python @LaunchArgs
exit $LASTEXITCODE
