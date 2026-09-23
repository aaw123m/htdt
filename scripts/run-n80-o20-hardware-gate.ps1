param(
    [string]$RepoRoot = "",
    [string]$Branch = "feat/n80-o20-hardware-acceptance",
    [string]$ExpectedProductHead = "df630d686f4e0c1687f05427585c0af1ae7bcf79",
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
} else {
    $RepoRoot = (Resolve-Path $RepoRoot -ErrorAction Stop).Path
}
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$OriginalSha = $null
$OriginalBranch = ""
$GateFailed = $false
$RestoreFailed = $false

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& git -C $RepoRoot @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $exitCode - $($output -join [Environment]::NewLine)"
    }
    return @($output | ForEach-Object { [string]$_ })
}

function Get-GitFirstLine {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $lines = @(Invoke-Git -Arguments $Arguments)
    if ($lines.Count -lt 1) {
        throw "git $($Arguments -join ' ') returned no output."
    }
    return ([string]$lines[0]).Trim()
}

try {
    $OriginalSha = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
    $branchOutput = @(& git -C $RepoRoot symbolic-ref --short -q HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $branchOutput.Count -gt 0) {
        $OriginalBranch = ([string]$branchOutput[0]).Trim()
    }
    Write-Output "N80_O20_ORIGINAL_SHA=$OriginalSha"
    Write-Output "N80_O20_ORIGINAL_BRANCH=$OriginalBranch"

    $preStatus = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
    Write-Output "N80_O20_PRE_STATUS_COUNT=$($preStatus.Count)"
    if ($preStatus.Count -gt 0) {
        $preStatus | ForEach-Object { Write-Output "N80_O20_PRE_STATUS=$_" }
        throw "Refusing O20 hardware gate because the repository is not clean."
    }

    if ($PreflightOnly) {
        Invoke-Git @("fetch", "--dry-run", "origin", $Branch) | Out-Null
        Write-Output "N80_O20_PREFLIGHT_RESULT=PASS"
        return
    }

    if (-not (Test-Path $Python)) {
        throw "Missing $Python. Use the existing Windows Python 3.12 environment."
    }

    Invoke-Git @("fetch", "--prune", "origin", $Branch) | Out-Null
    $remoteRef = "origin/$Branch"
    $branchHead = Get-GitFirstLine -Arguments @("rev-parse", $remoteRef)
    Write-Output "N80_O20_BRANCH_HEAD=$branchHead"
    Write-Output "N80_O20_PRODUCT_HEAD=$ExpectedProductHead"

    & git -C $RepoRoot cat-file -e "$ExpectedProductHead^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head is unavailable after fetch: $ExpectedProductHead"
    }
    & git -C $RepoRoot merge-base --is-ancestor $ExpectedProductHead $remoteRef
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head $ExpectedProductHead is not an ancestor of $remoteRef."
    }

    $allowedAfterProduct = @(
        ".github/workflows/ci.yml",
        "docs/IMPLEMENTATION_STATUS.md",
        "docs/N80_DESIGN.md",
        "docs/N80_PROGRESS.md",
        "scripts/validate_n80_o20_roomsim_windows.py",
        "scripts/run-n80-o20-hardware-gate.ps1"
    )
    $changed = @(Invoke-Git @("diff", "--name-only", "$ExpectedProductHead..$remoteRef"))
    $unexpected = @($changed | Where-Object { $_ -and $_ -notin $allowedAfterProduct })
    if ($unexpected.Count -gt 0) {
        $unexpected | ForEach-Object { Write-Output "N80_O20_UNEXPECTED_AFTER_PRODUCT=$_" }
        throw "Product code changed after $ExpectedProductHead. Update ExpectedProductHead first."
    }

    Invoke-Git @("checkout", "--quiet", "--detach", $branchHead) | Out-Null
    $gateSha = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
    Write-Output "N80_O20_GATE_SHA=$gateSha"
    if ($gateSha -ne $branchHead) {
        throw "O20 hardware gate checkout mismatch."
    }

    $gateStatus = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
    if ($gateStatus.Count -gt 0) {
        throw "O20 hardware gate checkout is unexpectedly dirty."
    }

    & $Python (Join-Path $RepoRoot "scripts\validate_n80_o20_roomsim_windows.py")
    $gateExit = $LASTEXITCODE
    Write-Output "N80_O20_GATE_EXIT=$gateExit"
    $GateFailed = ($gateExit -ne 0)
} catch {
    $GateFailed = $true
    Write-Output "N80_O20_GATE_ERROR=$($_.Exception.Message)"
} finally {
    if ($null -ne $OriginalSha) {
        try {
            if ($OriginalBranch) {
                Invoke-Git @("checkout", "--quiet", $OriginalBranch) | Out-Null
            } else {
                Invoke-Git @("checkout", "--quiet", "--detach", $OriginalSha) | Out-Null
            }
            $restoredSha = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
            $postStatus = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
            Write-Output "N80_O20_RESTORED_SHA=$restoredSha"
            Write-Output "N80_O20_POST_STATUS_COUNT=$($postStatus.Count)"
            if ($postStatus.Count -gt 0) {
                $postStatus | ForEach-Object { Write-Output "N80_O20_POST_STATUS=$_" }
            }
            if ($restoredSha -ne $OriginalSha -or $postStatus.Count -gt 0) {
                $RestoreFailed = $true
                Write-Output "N80_O20_REPO_RESTORE_OK=False"
            } else {
                Write-Output "N80_O20_REPO_RESTORE_OK=True"
            }
        } catch {
            $RestoreFailed = $true
            Write-Output "N80_O20_REPO_RESTORE_ERROR=$($_.Exception.Message)"
        }
    }
}

if ($PreflightOnly) {
    exit $(if ($GateFailed -or $RestoreFailed) { 1 } else { 0 })
}

$passed = -not $GateFailed -and -not $RestoreFailed
Write-Output "N80_O20_HARDWARE_GATE_RESULT=$(if ($passed) { 'PASS' } else { 'FAIL' })"
exit $(if ($passed) { 0 } else { 1 })
