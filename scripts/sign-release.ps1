<#
.SYNOPSIS
    Optional Authenticode signing stage for HTDT release artifacts (#849).

.DESCRIPTION
    Signs the installer / primary HTDT executables with Windows signtool and
    emits a signature-report.json per artifact that release_signature.py
    merges into the release manifest as a `publisher_signature` block —
    separate from software_verification. Signing is publisher identity,
    never a correctness gate.

    Secret isolation: pass signing capability via the machine certificate
    store (-Thumbprint) or a PFX + $env:HTDT_SIGN_PFX_PASSWORD. Nothing is
    written to the repo, artifacts, or reports.

    Unsigned builds stay valid: without signing capability the report
    records status='unsigned' (or 'unverifiable' when signtool is absent).
    -RequireSigning makes any non-signed_verified outcome fatal.

.EXAMPLE
    .\sign-release.ps1 -Artifacts dist-installer\HTDT-Setup-1.2.3.exe `
        -Thumbprint AAAA... -TimestampUrl http://timestamp.digicert.com

.EXAMPLE
    .\sign-release.ps1 -Artifacts dist-installer\HTDT-Setup-1.2.3.exe -RequireSigning
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]] $Artifacts,

    # Certificate thumbprint in the machine/user store (preferred).
    [string] $Thumbprint = '',

    # PFX path alternative — password via $env:HTDT_SIGN_PFX_PASSWORD only.
    [string] $PfxPath = '',

    [string] $TimestampUrl = 'http://timestamp.digicert.com',

    [string] $ReportDir = '',

    # Fail unless every artifact ends signed_verified.
    [switch] $RequireSigning
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
$capability = [ordered]@{
    signtool_present = [bool]$signtool
    cert_source      = $null
}
if ($Thumbprint) {
    $capability.cert_source = 'store_thumbprint'
} elseif ($PfxPath) {
    if (-not (Test-Path $PfxPath)) { throw "PFX not found: $PfxPath" }
    $capability.cert_source = 'pfx_file'
    if (-not $env:HTDT_SIGN_PFX_PASSWORD) {
        throw 'HTDT_SIGN_PFX_PASSWORD env var is required with -PfxPath'
    }
}

$reports = @()
$hadFailure = $false

foreach ($artifact in $Artifacts) {
    if (-not (Test-Path $artifact)) {
        throw "Artifact missing: $artifact"
    }
    $artifactPath = (Resolve-Path $artifact).Path
    $preSha = (Get-FileHash -Algorithm SHA256 $artifactPath).Hash.ToLowerInvariant()
    $report = [ordered]@{
        file              = Split-Path -Leaf $artifactPath
        status            = 'unsigned'
        pre_sign_sha256   = $preSha
        signed_sha256     = ''
        signer_subject    = $null
        signer_thumbprint = $null
        timestamped       = $false
        timestamp_url     = $null
    }

    if (-not $capability.signtool_present) {
        $report.status = 'unverifiable'
        Write-Warning "signtool.exe not found — $artifactPath stays unsigned"
    } elseif (-not $capability.cert_source) {
        Write-Warning "No signing capability configured — $artifactPath stays unsigned"
    } else {
        $signArgs = @('sign', '/fd', 'sha256')
        if ($Thumbprint) {
            $signArgs += @('/sha1', $Thumbprint)
        } else {
            $signArgs += @('/f', $PfxPath, '/p', $env:HTDT_SIGN_PFX_PASSWORD)
        }
        if ($TimestampUrl) {
            $signArgs += @('/tr', $TimestampUrl, '/td', 'sha256')
        }
        $signArgs += $artifactPath
        & $signtool.Source @signArgs | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $report.status = 'signing_failed'
            $hadFailure = $true
            Write-Warning "signtool sign failed for $artifactPath (exit $LASTEXITCODE)"
        } else {
            & $signtool.Source verify /pa $artifactPath | Out-Null
            if ($LASTEXITCODE -ne 0) {
                $report.status = 'unverifiable'
                $hadFailure = $true
                Write-Warning "signtool verify failed for $artifactPath"
            } else {
                $sig = Get-AuthenticodeSignature $artifactPath
                $report.status = 'signed_verified'
                $report.signed_sha256 = (Get-FileHash -Algorithm SHA256 $artifactPath).Hash.ToLowerInvariant()
                $report.signer_subject = $sig.SignerCertificate.Subject
                $report.signer_thumbprint = $sig.SignerCertificate.Thumbprint
                $report.timestamped = [bool]($sig.TimeStamperCertificate)
                $report.timestamp_url = $TimestampUrl
                Write-Host "signed_verified: $artifactPath"
            }
        }
    }

    $reports += ,@($artifactPath, $report)
    $reportPath = if ($ReportDir) {
        New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
        Join-Path $ReportDir "$(Split-Path -Leaf $artifactPath).signature.json"
    } else {
        "$artifactPath.signature.json"
    }
    $report | ConvertTo-Json -Depth 4 | Set-Content -Path $reportPath -Encoding utf8
    Write-Host "signature report: $reportPath"
}

if ($RequireSigning) {
    $bad = $reports | Where-Object { $_[1].status -ne 'signed_verified' }
    if ($bad -or $hadFailure) {
        throw 'Signature-required release: not all artifacts are signed_verified'
    }
}
exit 0
