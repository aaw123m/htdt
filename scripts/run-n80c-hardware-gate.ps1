param(
    [string]$RepoRoot = '',
    [string]$Branch = 'feat/n80-pareto-workspace',
    [string]$ExpectedProductHead = '2a891dbc1796d3cfdaebbe762d0d6e0d2636563f',
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
} else {
    $RepoRoot = (Resolve-Path $RepoRoot -ErrorAction Stop).Path
}
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$OriginalSha = $null
$OriginalBranch = ''
$GateFailed = $false
$RestoreFailed = $false

function Invoke-Git {
    param([Parameter(Mandatory=$true)][string[]]$Arguments)
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -C $RepoRoot @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) {
        throw ('git failed: ' + ($Arguments -join ' ') + ' / exit=' + $code + ' / ' + ($output -join [Environment]::NewLine))
    }
    return @($output | ForEach-Object { [string]$_ })
}

function Get-GitFirstLine {
    param([Parameter(Mandatory=$true)][string[]]$Arguments)
    $lines = @(Invoke-Git -Arguments $Arguments)
    if ($lines.Count -lt 1) { throw 'git returned no output' }
    return ([string]$lines[0]).Trim()
}

function Stop-N80cHarness {
    $items = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -match '^python(?:w)?\.exe$' -and $_.CommandLine -match 'validate_n80c_windows\.py'
    })
    foreach ($item in $items) { Stop-Process -Id $item.ProcessId -Force -ErrorAction Stop }
    Start-Sleep -Milliseconds 250
    $left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -match '^python(?:w)?\.exe$' -and $_.CommandLine -match 'validate_n80c_windows\.py'
    })
    if ($left.Count -gt 0) { throw 'N80c acceptance process remained after cleanup' }
}

try {
    $OriginalSha = Get-GitFirstLine -Arguments @('rev-parse','HEAD')
    $branchOut = @(& git -C $RepoRoot symbolic-ref --short -q HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $branchOut.Count -gt 0) { $OriginalBranch = ([string]$branchOut[0]).Trim() }
    Write-Output ('N80C_ORIGINAL_SHA=' + $OriginalSha)
    Write-Output ('N80C_ORIGINAL_BRANCH=' + $OriginalBranch)

    $pre = @(Invoke-Git @('status','--porcelain=v1','--untracked-files=all'))
    Write-Output ('N80C_PRE_STATUS_COUNT=' + $pre.Count)
    if ($pre.Count -gt 0) { throw 'Refusing N80c hardware gate because repository is not clean' }

    if ($PreflightOnly) {
        Invoke-Git @('fetch','--dry-run','origin',$Branch) | Out-Null
        Write-Output 'N80C_PREFLIGHT_RESULT=PASS'
        return
    }

    if (-not (Test-Path $Python)) { throw ('Missing Python environment: ' + $Python) }
    Stop-N80cHarness
    Invoke-Git @('fetch','--prune','origin',$Branch) | Out-Null
    $remoteRef = 'origin/' + $Branch
    $branchHead = Get-GitFirstLine -Arguments @('rev-parse',$remoteRef)
    Write-Output ('N80C_BRANCH_HEAD=' + $branchHead)
    Write-Output ('N80C_PRODUCT_HEAD=' + $ExpectedProductHead)

    & git -C $RepoRoot cat-file -e ($ExpectedProductHead + '^{commit}') 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Expected product head is unavailable after fetch' }
    & git -C $RepoRoot merge-base --is-ancestor $ExpectedProductHead $remoteRef
    if ($LASTEXITCODE -ne 0) { throw 'Expected product head is not an ancestor of remote branch' }

    $allowed = @(
        '.github/workflows/ci.yml',
        'docs/N80_PROGRESS.md',
        'docs/IMPLEMENTATION_STATUS.md',
        'docs/N80C_ACCEPTANCE_2026-09-18.md',
        'scripts/validate_n80c_windows.py',
        'scripts/run-n80c-hardware-gate.ps1'
    )
    $changed = @(Invoke-Git @('diff','--name-only',($ExpectedProductHead + '..' + $remoteRef)))
    $unexpected = @($changed | Where-Object { $_ -and $_ -notin $allowed })
    if ($unexpected.Count -gt 0) {
        $unexpected | ForEach-Object { Write-Output ('N80C_UNEXPECTED_AFTER_PRODUCT=' + $_) }
        throw 'Product code changed after expected product head'
    }

    Invoke-Git @('checkout','--quiet','--detach',$branchHead) | Out-Null
    Write-Output ('N80C_GATE_SHA=' + (Get-GitFirstLine -Arguments @('rev-parse','HEAD')))
    $gateStatus = @(Invoke-Git @('status','--porcelain=v1','--untracked-files=all'))
    if ($gateStatus.Count -gt 0) { throw 'N80c gate checkout is dirty' }

    try {
        $dpi = (Get-ItemProperty 'HKCU:\Control Panel\Desktop\WindowMetrics' -Name AppliedDPI -ErrorAction Stop).AppliedDPI
        Write-Output ('N80C_ENV_APPLIED_DPI=' + $dpi)
    } catch { Write-Output 'N80C_ENV_APPLIED_DPI=unavailable' }
    & $Python -c "import sys,PySide6,pyvista,vtk; print('N80C_ENV_PYTHON='+sys.version.split()[0]); print('N80C_ENV_PYSIDE6='+PySide6.__version__); print('N80C_ENV_PYVISTA='+pyvista.__version__); print('N80C_ENV_VTK='+vtk.vtkVersion.GetVTKVersion())"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to query Python environment' }

    & $Python (Join-Path $RepoRoot 'scripts\validate_n80c_windows.py')
    $acceptanceExit = $LASTEXITCODE
    Write-Output ('N80C_GATE_EXIT=' + $acceptanceExit)
    Stop-N80cHarness
    $GateFailed = ($acceptanceExit -ne 0)
} catch {
    $GateFailed = $true
    Write-Output ('N80C_GATE_ERROR=' + $_.Exception.Message)
} finally {
    if ($null -ne $OriginalSha) {
        try {
            Stop-N80cHarness
            if ($OriginalBranch) {
                Invoke-Git @('checkout','--quiet',$OriginalBranch) | Out-Null
            } else {
                Invoke-Git @('checkout','--quiet','--detach',$OriginalSha) | Out-Null
            }
            $restored = Get-GitFirstLine -Arguments @('rev-parse','HEAD')
            $post = @(Invoke-Git @('status','--porcelain=v1','--untracked-files=all'))
            Write-Output ('N80C_RESTORED_SHA=' + $restored)
            Write-Output ('N80C_POST_STATUS_COUNT=' + $post.Count)
            $ok = ($restored -eq $OriginalSha -and $post.Count -eq 0)
            Write-Output ('N80C_RESTORE_OK=' + $ok)
            if (-not $ok) { $RestoreFailed = $true }
        } catch {
            $RestoreFailed = $true
            Write-Output ('N80C_RESTORE_ERROR=' + $_.Exception.Message)
        }
    }
}

if ($PreflightOnly) { exit $(if ($GateFailed -or $RestoreFailed) { 1 } else { 0 }) }
$passed = (-not $GateFailed -and -not $RestoreFailed)
Write-Output ('N80C_HARDWARE_GATE_RESULT=' + $(if ($passed) { 'PASS' } else { 'FAIL' }))
exit $(if ($passed) { 0 } else { 1 })
