param(
    [string]$RepoRoot = "",
    [string]$Branch = "feat/n60-measurement-workspace",
    [string]$ExpectedProductHead = "8d4bfcd4af7589ab51c1363407ed2d94d1f7ea79",
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
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -C $RepoRoot @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)"
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

function Get-N60HarnessProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $PID -and
                $_.Name -match '^python(?:w)?\.exe$' -and
                $_.CommandLine -match 'validate_n60_a1[23]_windows\.py'
            }
    )
}

function Stop-N60HarnessProcesses {
    $processes = Get-N60HarnessProcesses
    if ($processes.Count -eq 0) {
        Write-Output "N60_RESIDUAL_PROCESSES=none"
        return
    }

    foreach ($process in $processes) {
        Write-Output "N60_RESIDUAL_STOP pid=$($process.ProcessId) command=$($process.CommandLine)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    }
    Start-Sleep -Milliseconds 350

    $remaining = Get-N60HarnessProcesses
    if ($remaining.Count -gt 0) {
        throw "N60 acceptance harness process remains after cleanup: $($remaining.ProcessId -join ',')"
    }
    Write-Output "N60_RESIDUAL_PROCESSES=cleared"
}

function Write-N60Environment {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        Write-Output "N60_ENV_OS=$($os.Caption) $($os.Version) build $($os.BuildNumber)"
    } catch {
        Write-Output "N60_ENV_OS=unavailable:$($_.Exception.Message)"
    }

    try {
        $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1
        Write-Output "N60_ENV_CPU=$($cpu.Name)"
    } catch {
        Write-Output "N60_ENV_CPU=unavailable:$($_.Exception.Message)"
    }

    try {
        $gpu = Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object { $_.CurrentHorizontalResolution -and $_.CurrentVerticalResolution } |
            Select-Object -First 1
        if ($null -ne $gpu) {
            Write-Output "N60_ENV_GPU=$($gpu.Name) driver=$($gpu.DriverVersion) display=$($gpu.CurrentHorizontalResolution)x$($gpu.CurrentVerticalResolution)"
        }
    } catch {
        Write-Output "N60_ENV_GPU=unavailable:$($_.Exception.Message)"
    }

    try {
        $dpi = (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics' -Name AppliedDPI -ErrorAction Stop).AppliedDPI
        Write-Output "N60_ENV_APPLIED_DPI=$dpi"
    } catch {
        Write-Output "N60_ENV_APPLIED_DPI=unavailable"
    }

    & $Python -c "import sys, PySide6, pyvista, vtk, pyqtgraph; print('N60_ENV_PYTHON=' + sys.version.split()[0]); print('N60_ENV_PYSIDE6=' + PySide6.__version__); print('N60_ENV_PYVISTA=' + pyvista.__version__); print('N60_ENV_VTK=' + vtk.vtkVersion.GetVTKVersion()); print('N60_ENV_PYQTGRAPH=' + pyqtgraph.__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query Python package versions."
    }
}

function Invoke-N60Harness {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][ref]$ExitCode
    )

    Write-Output "N60_GATE_${Label}_BEGIN"
    & $Python (Join-Path $RepoRoot "scripts\$ScriptName")
    $code = $LASTEXITCODE
    $ExitCode.Value = $code
    Write-Output "N60_GATE_${Label}_EXIT=$code"
}

try {
    $OriginalSha = Get-GitFirstLine -Arguments @('rev-parse', 'HEAD')
    $branchOutput = @(& git -C $RepoRoot symbolic-ref --short -q HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $branchOutput.Count -gt 0) {
        $OriginalBranch = ([string]$branchOutput[0]).Trim()
    }
    Write-Output "N60_ORIGINAL_SHA=$OriginalSha"
    Write-Output "N60_ORIGINAL_BRANCH=$OriginalBranch"

    $preStatus = @(Invoke-Git @('status', '--porcelain=v1', '--untracked-files=all'))
    Write-Output "N60_PRE_STATUS_COUNT=$($preStatus.Count)"
    if ($preStatus.Count -gt 0) {
        $preStatus | ForEach-Object { Write-Output "N60_PRE_STATUS=$_" }
        throw "Refusing hardware gate because the repository is not clean."
    }

    if ($PreflightOnly) {
        Invoke-Git @('fetch', '--dry-run', 'origin', $Branch) | Out-Null
        Write-Output "N60_PREFLIGHT_RESULT=PASS"
        return
    }

    if (-not (Test-Path $Python)) {
        throw "Missing $Python. Use the existing Windows Python 3.12 environment before running the hardware gate."
    }

    Stop-N60HarnessProcesses

    Invoke-Git @('fetch', '--prune', 'origin', $Branch) | Out-Null
    $remoteRef = "origin/$Branch"
    $branchHead = Get-GitFirstLine -Arguments @('rev-parse', $remoteRef)
    Write-Output "N60_BRANCH_HEAD=$branchHead"
    Write-Output "N60_PRODUCT_HEAD=$ExpectedProductHead"

    & git -C $RepoRoot cat-file -e "${ExpectedProductHead}^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head is unavailable after fetch: $ExpectedProductHead"
    }

    & git -C $RepoRoot merge-base --is-ancestor $ExpectedProductHead $remoteRef
    if ($LASTEXITCODE -ne 0) {
        throw "Expected product head $ExpectedProductHead is not an ancestor of $remoteRef."
    }

    $allowedAfterProduct = @(
        '.github/workflows/ci.yml',
        'docs/N60_PROGRESS.md',
        'scripts/run-n60-hardware-gate.ps1'
    )
    $changedSinceProduct = @(Invoke-Git @('diff', '--name-only', "$ExpectedProductHead..$remoteRef"))
    $unexpectedChanges = @($changedSinceProduct | Where-Object { $_ -and $_ -notin $allowedAfterProduct })
    if ($unexpectedChanges.Count -gt 0) {
        $unexpectedChanges | ForEach-Object { Write-Output "N60_UNEXPECTED_AFTER_PRODUCT=$_" }
        throw "Product code changed after $ExpectedProductHead. Update ExpectedProductHead before hardware acceptance."
    }

    Invoke-Git @('checkout', '--quiet', '--detach', $ExpectedProductHead) | Out-Null
    $gateSha = Get-GitFirstLine -Arguments @('rev-parse', 'HEAD')
    Write-Output "N60_GATE_SHA=$gateSha"
    if ($gateSha -ne $ExpectedProductHead) {
        throw "Hardware gate checkout mismatch: expected $ExpectedProductHead, got $gateSha"
    }

    $gateStatus = @(Invoke-Git @('status', '--porcelain=v1', '--untracked-files=all'))
    if ($gateStatus.Count -gt 0) {
        throw "Hardware gate checkout is unexpectedly dirty."
    }

    Write-N60Environment

    $a12Exit = -1
    Invoke-N60Harness -ScriptName 'validate_n60_a12_windows.py' -Label 'A12' -ExitCode ([ref]$a12Exit)
    Stop-N60HarnessProcesses
    $a13Exit = -1
    Invoke-N60Harness -ScriptName 'validate_n60_a13_windows.py' -Label 'A13' -ExitCode ([ref]$a13Exit)
    Stop-N60HarnessProcesses

    $GateFailed = ($a12Exit -ne 0 -or $a13Exit -ne 0)
    Write-Output "N60_GATE_A12_EXIT=$a12Exit"
    Write-Output "N60_GATE_A13_EXIT=$a13Exit"
} catch {
    $GateFailed = $true
    Write-Output "N60_GATE_ERROR=$($_.Exception.Message)"
} finally {
    if ($null -ne $OriginalSha) {
        try {
            Stop-N60HarnessProcesses
            if ($OriginalBranch) {
                Invoke-Git @('checkout', '--quiet', $OriginalBranch) | Out-Null
            } else {
                Invoke-Git @('checkout', '--quiet', '--detach', $OriginalSha) | Out-Null
            }

            $restoredSha = Get-GitFirstLine -Arguments @('rev-parse', 'HEAD')
            $postStatus = @(Invoke-Git @('status', '--porcelain=v1', '--untracked-files=all'))
            Write-Output "N60_RESTORED_SHA=$restoredSha"
            Write-Output "N60_POST_STATUS_COUNT=$($postStatus.Count)"
            if ($postStatus.Count -gt 0) {
                $postStatus | ForEach-Object { Write-Output "N60_POST_STATUS=$_" }
            }
            if ($restoredSha -ne $OriginalSha -or $postStatus.Count -gt 0) {
                $RestoreFailed = $true
                Write-Output "N60_RESTORE_OK=False"
            } else {
                Write-Output "N60_RESTORE_OK=True"
            }
        } catch {
            $RestoreFailed = $true
            Write-Output "N60_RESTORE_ERROR=$($_.Exception.Message)"
        }
    }
}

if ($PreflightOnly) {
    exit $(if ($GateFailed -or $RestoreFailed) { 1 } else { 0 })
}

$passed = -not $GateFailed -and -not $RestoreFailed
Write-Output "N60_HARDWARE_GATE_RESULT=$(if ($passed) { 'PASS' } else { 'FAIL' })"
exit $(if ($passed) { 0 } else { 1 })
