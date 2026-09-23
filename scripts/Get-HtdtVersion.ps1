# Returns the canonical HTDT application version from the single source of
# truth: htdt.__version__ in backend/src/htdt/__init__.py. pyproject.toml
# derives the Python package version from the same attribute.
param(
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}

$InitFile = Join-Path $RepoRoot "backend\src\htdt\__init__.py"
$Match = [regex]::Match(
    (Get-Content $InitFile -Raw),
    '(?m)^__version__\s*=\s*"([^"]+)"\s*$'
)
if (-not $Match.Success) {
    throw "Could not read canonical __version__ from $InitFile"
}
return $Match.Groups[1].Value
