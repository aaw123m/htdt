param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "dist-native"
}
$WorkRoot = Join-Path $RepoRoot ".tmp\n05-package"
$BuildVenv = Join-Path $WorkRoot "venv"

try {
    if (Test-Path $WorkRoot) {
        Remove-Item -Recurse -Force $WorkRoot
    }
    New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
    py -3.12 -m venv $BuildVenv
    $Python = Join-Path $BuildVenv "Scripts\python.exe"
    $LockFile = Join-Path $RepoRoot "backend\requirements-n05-windows.lock"
    & $Python -m pip install --disable-pip-version-check -r $LockFile
    if ($LASTEXITCODE -ne 0) {
        throw "Locked dependency install failed with exit code $LASTEXITCODE"
    }

    $BrandingSource = Join-Path $RepoRoot "assets\branding\HTDT-AppIcon-source.jpg"
    $BrandingOutput = Join-Path $WorkRoot "branding"
    & $Python (Join-Path $RepoRoot "scripts\prepare_branding_assets.py") `
        --source $BrandingSource `
        --output-dir $BrandingOutput
    if ($LASTEXITCODE -ne 0) {
        throw "HTDT branding asset generation failed with exit code $LASTEXITCODE"
    }
    $RuntimeIcon = Join-Path $BrandingOutput "HTDT.png"
    $ExecutableIcon = Join-Path $BrandingOutput "HTDT.ico"
    if (-not (Test-Path $RuntimeIcon) -or -not (Test-Path $ExecutableIcon)) {
        throw "HTDT branding asset generation did not produce the expected files"
    }

    & $Python -m pip install --disable-pip-version-check --no-deps --no-build-isolation "$RepoRoot\backend"
    if ($LASTEXITCODE -ne 0) {
        throw "HTDT package install failed with exit code $LASTEXITCODE"
    }

    # Embed the exact build/source identity in the package so the packaged
    # executable, the installer and backup manifests all report the same
    # display version ("<version>+g<sha8>[.dirty]"). The canonical version is
    # htdt.__version__; the commit SHA comes from the checked-out source.
    $AppVersion = & (Join-Path $PSScriptRoot "Get-HtdtVersion.ps1")
    $CommitSha = ""
    $Dirty = $false
    if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $RepoRoot ".git"))) {
        $CommitSha = & git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1
        if ($LASTEXITCODE -ne 0) {
            $CommitSha = ""
        }
        if ($CommitSha) {
            $Status = & git -C $RepoRoot status --porcelain 2>$null
            $Dirty = ($LASTEXITCODE -eq 0) -and [bool]$Status
        }
    }
    if (-not $CommitSha -and $env:GITHUB_SHA) {
        $CommitSha = $env:GITHUB_SHA
    }
    $LockSha256 = (Get-FileHash -Algorithm SHA256 $LockFile).Hash.ToLowerInvariant()
    $BuildInfoDir = Join-Path $WorkRoot "build-info"
    New-Item -ItemType Directory -Force -Path $BuildInfoDir | Out-Null
    $BuildInfoFile = Join-Path $BuildInfoDir "build_info.json"
    [ordered]@{
        version          = $AppVersion
        commit_sha       = $(if ($CommitSha) { $CommitSha } else { $null })
        build_id         = $(if ($env:GITHUB_RUN_ID) { $env:GITHUB_RUN_ID } else { $null })
        dirty            = $Dirty
        source           = "packaged"
        lock_sha256      = $LockSha256
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -Path $BuildInfoFile -Encoding utf8

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name HTDT `
        --icon $ExecutableIcon `
        --add-data "$RuntimeIcon;htdt_branding" `
        --add-data "$BuildInfoFile;htdt_build" `
        --paths "$RepoRoot\backend\src" `
        --collect-all pyvista `
        --collect-all pyvistaqt `
        --distpath $OutputDir `
        --workpath (Join-Path $WorkRoot "build") `
        --specpath $WorkRoot `
        (Join-Path $RepoRoot "scripts\native_entry.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $PackageDir = Join-Path $OutputDir "HTDT"
    Copy-Item -Force $ExecutableIcon (Join-Path $PackageDir "HTDT.ico")
    Write-Host "Built native package: $(Join-Path $PackageDir 'HTDT.exe')"
}
finally {
    if (Test-Path $WorkRoot) {
        Remove-Item -Recurse -Force $WorkRoot
    }
}
