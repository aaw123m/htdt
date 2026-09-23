param(
    [string]$DataDir = "",
    [string]$DocumentId = "fixture-f1"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Missing .venv. Create a Python 3.12 venv and install backend first."
}

$Arguments = @("-m", "htdt.native_cad", "--document-id", $DocumentId)
if ($DataDir) {
    $Arguments += @("--data-dir", $DataDir)
}
& $Python @Arguments
exit $LASTEXITCODE
