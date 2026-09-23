param(
    [string]$RepoRoot = "",
    [string]$Branch = "feat/n90-stable-release",
    [string]$ExpectedHead = "",
    [string]$BaselineInstaller = "",
    [string]$UpdateInstaller = "",
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)
    & git -C $RepoRoot @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$Harness = Join-Path $RepoRoot "scripts\validate_n90_a15_windows.py"
if (-not (Test-Path $Harness)) {
    throw "Missing N90 A15 harness: $Harness"
}

if ($PreflightOnly) {
    & git -C $RepoRoot ls-remote --exit-code origin "refs/heads/$Branch" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "N90 branch is not available on origin: $Branch"
    }
    Write-Host "N90_A15_PREFLIGHT_RESULT=PASS"
    exit 0
}

if (-not $ExpectedHead) {
    throw "-ExpectedHead is required for the real A15 gate"
}
if (-not $BaselineInstaller -or -not $UpdateInstaller) {
    throw "-BaselineInstaller and -UpdateInstaller are required for the real A15 gate"
}
$BaselineInstaller = (Resolve-Path $BaselineInstaller).Path
$UpdateInstaller = (Resolve-Path $UpdateInstaller).Path

$StatusBefore = @(& git -C $RepoRoot status --porcelain=v1)
if ($StatusBefore.Count -ne 0) {
    throw "Refusing N90 A15 gate on a dirty repository"
}

$OriginalSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not read original HEAD" }
$OriginalBranchOutput = @(& git -C $RepoRoot symbolic-ref --short -q HEAD)
$OriginalBranch = if ($OriginalBranchOutput.Count -gt 0) {
    ([string]$OriginalBranchOutput[0]).Trim()
} else {
    ""
}
$WorkRoot = Join-Path $env:TEMP ("htdt-n90-a15-" + [guid]::NewGuid().ToString("N"))

Write-Host "N90_ORIGINAL_SHA=$OriginalSha"
Write-Host "N90_ORIGINAL_BRANCH=$OriginalBranch"
Write-Host "N90_PRE_STATUS_COUNT=$($StatusBefore.Count)"

$GateExit = 1
try {
    Invoke-Git fetch origin $Branch
    $RemoteRef = "refs/remotes/origin/$Branch"
    $GateHead = (& git -C $RepoRoot rev-parse $RemoteRef).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve N90 gate head" }
    if ($GateHead -ne $ExpectedHead) {
        throw "N90 branch moved: expected $ExpectedHead, found $GateHead"
    }
    Write-Host "N90_GATE_SHA=$GateHead"

    Invoke-Git checkout --detach $GateHead

    $HarnessArgs = @(
        $Harness,
        "--baseline-installer", $BaselineInstaller,
        "--update-installer", $UpdateInstaller,
        "--work-root", $WorkRoot
    )
    & py -3.12 @HarnessArgs
    $GateExit = $LASTEXITCODE
    Write-Host "N90_GATE_EXIT=$GateExit"
    if ($GateExit -ne 0) {
        throw "N90 A15 harness failed with exit code $GateExit"
    }
}
finally {
    if (Test-Path $WorkRoot) {
        Remove-Item -Recurse -Force $WorkRoot -ErrorAction SilentlyContinue
    }
    if ($OriginalBranch) {
        & git -C $RepoRoot checkout $OriginalBranch | Out-Null
    } else {
        & git -C $RepoRoot checkout --detach $OriginalSha | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to restore original checkout"
    }
}

$RestoredSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
$StatusAfter = @(& git -C $RepoRoot status --porcelain=v1)
$RestoreOk = ($RestoredSha -eq $OriginalSha -and $StatusAfter.Count -eq 0)

Write-Host "N90_RESTORED_SHA=$RestoredSha"
Write-Host "N90_POST_STATUS_COUNT=$($StatusAfter.Count)"
Write-Host "N90_RESTORE_OK=$RestoreOk"

if ($GateExit -ne 0 -or -not $RestoreOk) {
    Write-Host "N90_HARDWARE_GATE_RESULT=FAIL"
    exit 1
}
Write-Host "N90_HARDWARE_GATE_RESULT=PASS"
