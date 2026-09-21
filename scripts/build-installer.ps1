param(
    [string]$PackageDir = "",
    [string]$OutputDir = "",
    [string]$IsccPath = "",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not $PackageDir) {
    $PackageDir = Join-Path $RepoRoot "dist-native\HTDT"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "dist-installer"
}
if (-not (Test-Path (Join-Path $PackageDir "HTDT.exe"))) {
    throw "Native package not found: $(Join-Path $PackageDir 'HTDT.exe')"
}

# Canonical application version (single source: htdt.__version__, which is
# also what pyproject.toml derives the Python package version from).
if (-not $Version) {
    $Version = & (Join-Path $PSScriptRoot "Get-HtdtVersion.ps1")
}

# The installer reports the identity of the bits it actually packages: the
# build-info manifest embedded by build-native.ps1 is exactly what the
# packaged HTDT.exe --version reports, so both sides agree by construction.
$BuildInfoFile = Join-Path $PackageDir "_internal\htdt_build\build_info.json"
$CommitSha = $null
$Dirty = $false
$LockSha256 = $null
if (Test-Path $BuildInfoFile) {
    $PackageBuild = Get-Content $BuildInfoFile -Raw | ConvertFrom-Json
    if ($PackageBuild.version -and $PackageBuild.version -ne $Version) {
        throw "Packaged build version '$($PackageBuild.version)' diverges from canonical version '$Version'"
    }
    if ($PackageBuild.commit_sha) {
        $CommitSha = [string]$PackageBuild.commit_sha
    }
    $Dirty = [bool]$PackageBuild.dirty
    if ($PackageBuild.lock_sha256) {
        $LockSha256 = [string]$PackageBuild.lock_sha256
    }
} else {
    Write-Warning "Package has no embedded build info ($BuildInfoFile); the installer will carry the plain canonical version."
}

# Display version: "<version>+g<sha8>[.dirty]" — identical to HTDT.exe
# --version output. Two different source commits can no longer publish
# indistinguishable HTDT-Setup-<version>.exe artifacts.
$AppVersion = $Version
if ($CommitSha) {
    $ShortSha = $CommitSha.Substring(0, [Math]::Min(8, $CommitSha.Length))
    $AppVersion = "$Version+g$ShortSha"
    if ($Dirty) {
        $AppVersion = "$AppVersion.dirty"
    }
}

if (-not $IsccPath) {
    $Candidates = @(@(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path $_) })
    if ($Candidates.Count -eq 0) {
        $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($Command) {
            $IsccPath = $Command.Source
        } else {
            throw "ISCC.exe not found. Install Inno Setup before building the installer."
        }
    } else {
        $IsccPath = $Candidates[0]
    }
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$PackageFull = (Resolve-Path $PackageDir).Path
$InstallerScript = Join-Path $RepoRoot "installer\HTDT.iss"

& $IsccPath `
    "/DAppVersion=$AppVersion" `
    "/DSourceDir=$PackageFull" `
    "/O$OutputDir" `
    $InstallerScript

if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

$Expected = Join-Path $OutputDir "HTDT-Setup-$AppVersion.exe"
if (-not (Test-Path $Expected)) {
    throw "Installer output missing: $Expected"
}

# Release/build manifest: exact source SHA, lock hash and installer digest
# travel next to the artifact instead of an external manual note.
if (-not $LockSha256) {
    $LockFile = Join-Path $RepoRoot "backend\requirements-n05-windows.lock"
    if (Test-Path $LockFile) {
        $LockSha256 = (Get-FileHash -Algorithm SHA256 $LockFile).Hash.ToLowerInvariant()
    }
}
$InstallerSha256 = (Get-FileHash -Algorithm SHA256 $Expected).Hash.ToLowerInvariant()
$Manifest = [ordered]@{
    schema              = "htdt-release-manifest/1"
    application_version = $Version
    display_version     = $AppVersion
    commit_sha          = $CommitSha
    dirty               = $Dirty
    build_id            = $(if ($env:GITHUB_RUN_ID) { $env:GITHUB_RUN_ID } else { $null })
    installer           = [ordered]@{
        file   = Split-Path -Leaf $Expected
        sha256 = $InstallerSha256
    }
    lock_file           = [ordered]@{
        path   = "backend/requirements-n05-windows.lock"
        sha256 = $LockSha256
    }
    github              = [ordered]@{
        workflow    = $env:GITHUB_WORKFLOW
        run_id      = $env:GITHUB_RUN_ID
        run_number  = $env:GITHUB_RUN_NUMBER
        ref         = $env:GITHUB_REF
        sha         = $env:GITHUB_SHA
    }
    generated_at_utc    = (Get-Date).ToUniversalTime().ToString("o")
}
$ManifestPath = Join-Path $OutputDir "HTDT-Setup-$AppVersion.manifest.json"
$Manifest | ConvertTo-Json -Depth 6 | Set-Content -Path $ManifestPath -Encoding utf8

Write-Host "Built installer: $Expected"
Write-Host "Release manifest: $ManifestPath"
