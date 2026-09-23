param(
    [string]$RepoRoot = "",
    [string]$Branch = "main",
    [string]$ExpectedProductHead = "252647a7c4757c420151049a7f729d8157f5243b",
    [string]$DataDir = "",
    [string]$CampaignId = "",
    [string]$ValidationId = "",
    [string]$ReportJson = "",
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
} else {
    $RepoRoot = (Resolve-Path $RepoRoot -ErrorAction Stop).Path
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$AuditScript = Join-Path $RepoRoot "scripts\audit_o60_owned_room.py"
$OriginalSha = $null
$OriginalBranch = ""
$GateFailed = $false
$RestoreFailed = $false

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& git -C $RepoRoot @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) {
        throw ("git " + ($Arguments -join " ") + " failed with exit code " + $code + ": " + ($output -join [Environment]::NewLine))
    }
    return @($output | ForEach-Object { [string]$_ })
}

function Get-GitFirstLine {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $lines = @(Invoke-Git -Arguments $Arguments)
    if ($lines.Count -lt 1) {
        throw "git $($Arguments -join ' ') returned no output"
    }
    return ([string]$lines[0]).Trim()
}

try {
    $OriginalSha = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
    $branchOut = @(& git -C $RepoRoot symbolic-ref --short -q HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $branchOut.Count -gt 0) {
        $OriginalBranch = ([string]$branchOut[0]).Trim()
    }
    Write-Output "O60R_ORIGINAL_SHA=$OriginalSha"
    Write-Output "O60R_ORIGINAL_BRANCH=$OriginalBranch"

    $pre = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
    Write-Output "O60R_PRE_STATUS_COUNT=$($pre.Count)"
    if ($pre.Count -gt 0) {
        $pre | ForEach-Object { Write-Output "O60R_PRE_STATUS=$_" }
        throw "Refusing O60R gate because repository is not clean"
    }

    Invoke-Git @("fetch", "--prune", "origin", $Branch) | Out-Null
    $remoteRef = "origin/$Branch"

    $isShallow = Get-GitFirstLine -Arguments @("rev-parse", "--is-shallow-repository")
    if ($isShallow -eq "true") {
        Write-Output "O60R_HISTORY_SHALLOW=True"
        Invoke-Git @("fetch", "--no-tags", "--unshallow", "origin", $Branch) | Out-Null
    } else {
        Write-Output "O60R_HISTORY_SHALLOW=False"
    }

    $branchHead = Get-GitFirstLine -Arguments @("rev-parse", $remoteRef)
    Write-Output "O60R_BRANCH_HEAD=$branchHead"
    Write-Output "O60R_PRODUCT_HEAD=$ExpectedProductHead"

    & git -C $RepoRoot cat-file -e ($ExpectedProductHead + "^{commit}") 2>$null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Git @("fetch", "--no-tags", "origin", $ExpectedProductHead) | Out-Null
    }
    & git -C $RepoRoot cat-file -e ($ExpectedProductHead + "^{commit}") 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head is unavailable after explicit fetch: $ExpectedProductHead"
    }
    & git -C $RepoRoot merge-base --is-ancestor $ExpectedProductHead $remoteRef
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head is not an ancestor of $remoteRef"
    }

    if ($PreflightOnly) {
        if (-not (Test-Path $AuditScript)) {
            throw "Missing O60R audit script: $AuditScript"
        }
        Write-Output "O60R_PREFLIGHT_AUTHORITY=$ExpectedProductHead"
        Write-Output "O60R_PREFLIGHT_RESULT=PASS"
        return
    }

    $allowedAfterProduct = @(
        ".github/workflows/ci.yml",
        "backend/tests/test_o60r_audit.py",
        "docs/IMPLEMENTATION_STATUS.md",
        "docs/O60R_PROGRESS.md",
        "scripts/audit_o60_owned_room.py",
        "scripts/inventory_o60_owned_room.py",
        "scripts/run-o60-owned-room-gate.ps1"
    )
    $changed = @(Invoke-Git @("diff", "--name-only", ($ExpectedProductHead + ".." + $remoteRef)))
    $unexpected = @($changed | Where-Object { $_ -and $_ -notin $allowedAfterProduct })
    if ($unexpected.Count -gt 0) {
        $unexpected | ForEach-Object { Write-Output "O60R_UNEXPECTED_AFTER_PRODUCT=$_" }
        throw "Product code changed after expected audited product head"
    }

    if (-not (Test-Path $Python)) {
        throw "Missing Python environment: $Python"
    }
    if (-not $DataDir) {
        throw "-DataDir is required for the real O60R gate"
    }
    if (-not $CampaignId) {
        throw "-CampaignId is required for the real O60R gate"
    }
    $DataDir = (Resolve-Path $DataDir -ErrorAction Stop).Path
    if ($ReportJson) {
        $ReportJson = [System.IO.Path]::GetFullPath($ReportJson)
    }

    Invoke-Git @("checkout", "--quiet", "--detach", $branchHead) | Out-Null
    $gateSha = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
    Write-Output "O60R_GATE_SHA=$gateSha"
    if ($gateSha -ne $branchHead) {
        throw "O60R gate checkout mismatch"
    }

    $gateStatus = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
    if ($gateStatus.Count -gt 0) {
        throw "O60R gate checkout is unexpectedly dirty"
    }

    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        Write-Output "O60R_ENV_OS=$($os.Caption) $($os.Version) build $($os.BuildNumber)"
    } catch {
        Write-Output "O60R_ENV_OS=unavailable:$($_.Exception.Message)"
    }
    & $Python -c "import sys; print('O60R_ENV_PYTHON=' + sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query Python environment"
    }

    $auditArgs = @(
        $AuditScript,
        "--data-dir", $DataDir,
        "--campaign-id", $CampaignId
    )
    if ($ValidationId) {
        $auditArgs += @("--validation-id", $ValidationId)
    }
    if ($ReportJson) {
        $auditArgs += @("--report-json", $ReportJson)
    }

    & $Python @auditArgs
    $auditExit = $LASTEXITCODE
    Write-Output "O60R_AUDIT_EXIT=$auditExit"
    $GateFailed = ($auditExit -ne 0)
} catch {
    $GateFailed = $true
    Write-Output "O60R_GATE_ERROR=$($_.Exception.Message)"
} finally {
    if ($null -ne $OriginalSha) {
        try {
            if ($OriginalBranch) {
                Invoke-Git @("checkout", "--quiet", $OriginalBranch) | Out-Null
            } else {
                Invoke-Git @("checkout", "--quiet", "--detach", $OriginalSha) | Out-Null
            }
            $restored = Get-GitFirstLine -Arguments @("rev-parse", "HEAD")
            $post = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all"))
            Write-Output "O60R_RESTORED_SHA=$restored"
            Write-Output "O60R_POST_STATUS_COUNT=$($post.Count)"
            $restoreOk = ($restored -eq $OriginalSha -and $post.Count -eq 0)
            Write-Output "O60R_RESTORE_OK=$restoreOk"
            if (-not $restoreOk) {
                $RestoreFailed = $true
            }
        } catch {
            $RestoreFailed = $true
            Write-Output "O60R_RESTORE_ERROR=$($_.Exception.Message)"
        }
    }
}

if ($PreflightOnly) {
    exit $(if ($GateFailed -or $RestoreFailed) { 1 } else { 0 })
}

$passed = (-not $GateFailed -and -not $RestoreFailed)
Write-Output "O60R_HARDWARE_GATE_RESULT=$(if ($passed) { 'PASS' } else { 'FAIL' })"
exit $(if ($passed) { 0 } else { 1 })
