param(
    [string]$OutputDir = "",
    # Interpreter that seeds the build venv. Defaults to the current `python`
    # on PATH so the caller controls the toolchain; the script never
    # resolves the py launcher itself.
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "dist-native"
}
$WorkRoot = Join-Path $RepoRoot ".tmp\n05-package"
$BuildVenv = Join-Path $WorkRoot "venv"

if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
} elseif (-not (Test-Path $PythonExe)) {
    # A bare command name (e.g. `-PythonExe python`) resolves through PATH too.
    $Resolved = (Get-Command $PythonExe -ErrorAction SilentlyContinue).Source
    if ($Resolved) {
        $PythonExe = $Resolved
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "No Python interpreter found. Supply -PythonExe or put the pinned CPython 3.12 interpreter on PATH."
}

# The lock freezes a CPython 3.12 / Windows x64 wheel closure; refuse any other
# toolchain instead of producing a subtly different environment.
$InterpreterInfo = & $PythonExe -c "import platform, sys; print(platform.python_version()); print(sys.version_info[:2] == (3, 12)); print(platform.machine())"
if ($LASTEXITCODE -ne 0) {
    throw "Could not query interpreter version via $PythonExe (exit code $LASTEXITCODE)"
}
$PythonVersion = $InterpreterInfo[0]
if ($InterpreterInfo[1] -ne "True") {
    throw "Build interpreter must be CPython 3.12.x (the locked wheel closure targets it); got $PythonVersion from $PythonExe"
}
if ($InterpreterInfo[2] -ne "AMD64") {
    throw "Build interpreter must be Windows x64 (the locked wheel closure targets it); got architecture '$($InterpreterInfo[2])' from $PythonExe"
}
Write-Host "Build interpreter: $PythonExe ($PythonVersion)"

# Fail fast if the hash-pinned lock has drifted from backend/pyproject.toml;
# this is a pure consistency check and needs no venv.
& $PythonExe (Join-Path $RepoRoot "scripts\check_dependency_lock.py")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency lock consistency check failed with exit code $LASTEXITCODE"
}

try {
    if (Test-Path $WorkRoot) {
        Remove-Item -Recurse -Force $WorkRoot
    }
    New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
    & $PythonExe -m venv $BuildVenv
    $Python = Join-Path $BuildVenv "Scripts\python.exe"
    $LockFile = Join-Path $RepoRoot "backend\requirements-n05-windows.lock"
    # Hash-pinned lock: pip verifies the SHA-256 of every downloaded artifact,
    # so the packaged closure is exactly the one recorded in the repository.
    & $Python -m pip install --disable-pip-version-check --require-hashes -r $LockFile
    if ($LASTEXITCODE -ne 0) {
        throw "Locked dependency install failed with exit code $LASTEXITCODE"
    }
    # Assert the build venv now holds every locked distribution at exactly
    # the locked version — nothing may be silently moved by the install.
    & $Python (Join-Path $RepoRoot "scripts\check_dependency_lock.py") --verify-installed
    if ($LASTEXITCODE -ne 0) {
        throw "Post-install dependency lock verification failed with exit code $LASTEXITCODE"
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
    $PipVersion = (& $Python -c "import pip; print(pip.__version__)") | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) {
        $PipVersion = $null
    }
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
        python_version   = $PythonVersion
        pip_version      = $(if ($PipVersion) { $PipVersion } else { $null })
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -Path $BuildInfoFile -Encoding utf8

    # No --clean: the build workpath is already wiped above, and PyInstaller's
    # content-keyed config-dir cache is safe to keep across runs — it skips
    # re-analysing an unchanged dependency graph.
    & $Python -m PyInstaller `
        --noconfirm `
        --onedir `
        --windowed `
        --name HTDT `
        --icon $ExecutableIcon `
        --add-data "$RuntimeIcon;htdt_branding" `
        --add-data "$BuildInfoFile;htdt_build" `
        --paths "$RepoRoot\backend\src" `
        --collect-submodules htdt `
        --collect-data htdt `
        --collect-all pyvista `
        --collect-all pyvistaqt `
        --copy-metadata numpy `
        --copy-metadata h5py `
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
