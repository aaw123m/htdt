# Shared release-verification evidence binding (issue #833).
#
# Dot-source this file, then call Get-VerificationBlock to turn a
# release_verification_evidence.json (produced by
# scripts/run_release_verification.py --profile release) into the
# `verification` block of a release manifest (htdt-release-manifest/*).
#
# The block is deliberately fail-closed in both directions:
#   - evidence must exist, parse, carry schema htdt-release-verification/1,
#     report verdict=passed on a completed run with mode=release and
#     coverage=full, and name the same commit the package was built from;
#   - when evidence is absent or fails any of that, the manifest still
#     records the outcome — `unverified`/`rejected` — so a packaged
#     artifact can never silently look like a fully verified candidate.
#
# -RequireEvidence turns any non-verified outcome into a hard error.

function Get-VerificationBlock {
    param(
        [string]$EvidencePath = "",
        [string]$ExpectedCommitSha = "",
        [switch]$RequireEvidence
    )

    $result = [ordered]@{
        status              = "unverified"
        reason              = $null
        evidence_file       = $null
        evidence_sha256     = $null
        verdict             = $null
        profile             = $null
        coverage            = $null
        run_completed       = $null
        evidence_commit_sha = $null
        generated_at_utc    = $null
        manifest_sha256     = $null
    }

    $reject = {
        param([string]$why)
        $result.status = "rejected"
        $result.reason = $why
        if ($RequireEvidence) {
            throw "Verification evidence rejected: $why"
        }
        return $result
    }

    if (-not $EvidencePath) {
        $result.reason = "no verification evidence supplied"
        if ($RequireEvidence) {
            throw "Verification evidence required but -VerificationEvidence was not supplied"
        }
        return $result
    }
    if (-not (Test-Path $EvidencePath)) {
        return & $reject "evidence file not found: $EvidencePath"
    }

    $result.evidence_file = (Split-Path -Leaf $EvidencePath)
    $result.evidence_sha256 = (Get-FileHash -Algorithm SHA256 $EvidencePath).Hash.ToLowerInvariant()

    try {
        $Evidence = Get-Content $EvidencePath -Raw | ConvertFrom-Json
    } catch {
        return & $reject "evidence JSON did not parse: $($_.Exception.Message)"
    }
    if ($null -eq $Evidence) {
        return & $reject "evidence JSON is empty"
    }

    if ($Evidence.schema -ne "htdt-release-verification/1") {
        return & $reject "unexpected evidence schema '$($Evidence.schema)' (expected htdt-release-verification/1)"
    }
    $result.verdict = [string]$Evidence.verdict
    $result.profile = [string]$Evidence.profile
    $result.coverage = [string]$Evidence.coverage
    $result.run_completed = [bool]$Evidence.run_completed
    $result.generated_at_utc = [string]$Evidence.generated_at_utc
    if ($Evidence.manifest -and $Evidence.manifest.sha256) {
        $result.manifest_sha256 = [string]$Evidence.manifest.sha256
    }
    if ($Evidence.revision -and $Evidence.revision.commit_sha) {
        $result.evidence_commit_sha = [string]$Evidence.revision.commit_sha
    }

    if (-not $Evidence.run_completed) {
        return & $reject "verification run did not complete (interrupted runs are never release evidence)"
    }
    if ($Evidence.verdict -ne "passed") {
        return & $reject "verification verdict is '$($Evidence.verdict)', not 'passed'"
    }
    if ($Evidence.profile -ne "release") {
        return & $reject "verification profile is '$($Evidence.profile)' — release-candidate evidence requires the release profile"
    }
    if ($Evidence.coverage -ne "full") {
        return & $reject "verification coverage is '$($Evidence.coverage)' — release-candidate evidence requires coverage=full"
    }
    if ($ExpectedCommitSha -and $result.evidence_commit_sha -and
        $result.evidence_commit_sha -ne $ExpectedCommitSha) {
        return & $reject ("evidence was produced for commit {0} but the package was built from {1}" -f
            $result.evidence_commit_sha.Substring(0, [Math]::Min(12, $result.evidence_commit_sha.Length)),
            $ExpectedCommitSha.Substring(0, [Math]::Min(12, $ExpectedCommitSha.Length)))
    }
    if ($ExpectedCommitSha -and -not $result.evidence_commit_sha) {
        return & $reject "evidence records no commit_sha — cannot bind it to the packaged revision"
    }

    $result.status = "verified"
    $result.reason = $null
    return $result
}
