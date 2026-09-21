# Release identity and versioning

HTDT produces Windows installer artifacts. Every artifact must identify the
exact source that produced it; two different commits must never publish
indistinguishable `HTDT-Setup-<version>.exe` binaries.

## Canonical version (single source of truth)

`backend/src/htdt/__init__.py` declares the one canonical application version:

```python
__version__ = "0.2.0.dev0"
```

Everything else derives from it — there is no second maintained copy:

- `backend/pyproject.toml` sets `dynamic = ["version"]` and reads the package
  version via `setuptools` `attr = "htdt.__version__"`, so the Python package
  version cannot diverge from `__version__` (CI tests assert this wiring).
- `scripts/Get-HtdtVersion.ps1` parses `__init__.py` and is used by
  `build-native.ps1` / `build-installer.ps1`.
- `installer/HTDT.iss` carries a fallback `#define AppVersion` that must track
  the canonical value (asserted by `test_release_identity.py`); real builds
  always pass `/DAppVersion` explicitly.
- `HTDT.exe --version`, `QApplication.applicationVersion`, `/api/health`, and
  backup manifests all read `htdt.__version__` (decorated with the build
  identity where applicable — see below).

## Development vs stable identity

- Accepted stable release: **`0.1.0`**.
- After a stable release, `main` moves to the next development version —
  currently **`0.2.0.dev0`** (same convention as the pre-release `0.1.0.dev0`
  that preceded stable `0.1.0`).
- To cut a stable release: set `__version__` to the bare `X.Y.Z`, build and
  accept the artifact, then immediately move `main` to `X.(Y+1).0.dev0`
  (or `X.Y.(Z+1).dev0` for a patch line).

## Build identity

The *display version* identifies the producing build:

```
<version>                    e.g. 0.2.0.dev0        (identity unknown)
<version>+g<sha8>            e.g. 0.2.0.dev0+g9e606625
<version>+g<sha8>.dirty      uncommitted changes present at build time
```

`htdt.build_info.get_build_info()` resolves the identity in this order:

1. `HTDT_BUILD_INFO` env var → JSON file (explicit override).
2. `sys._MEIPASS/htdt_build/build_info.json` inside a PyInstaller package —
   written by `scripts/build-native.ps1` with the commit SHA, GitHub run id,
   dirty flag and the SHA-256 of `backend/requirements-n05-windows.lock`.
3. `git` metadata of the source checkout (`git rev-parse HEAD`,
   `git status --porcelain`).
4. Otherwise the plain canonical version.

The display version is surfaced consistently by:

- `HTDT.exe --version` (`%(prog)s <display_version>`);
- installer `AppVersion` and filename `HTDT-Setup-<display_version>.exe` —
  `build-installer.ps1` derives it from the packaged `build_info.json`, so it
  matches the executable by construction;
- backup manifest `build.display_version` / `build.commit_sha` provenance.

## Release manifest

`build-installer.ps1` writes `HTDT-Setup-<display_version>.manifest.json`
next to the installer, recording `application_version`, `display_version`,
`commit_sha`, `dirty`, `build_id`, installer SHA-256, the dependency lock
SHA-256, GitHub run context and a UTC timestamp. The `windows-release`
workflow uploads it with the installer artifact and verifies it against the
checked-out source.

## CI verification

- `backend/tests/test_release_identity.py` fails CI if `pyproject.toml` stops
  deriving the package version from `htdt.__version__` or if the Inno Setup
  fallback diverges.
- The `Verify release identity` step in `windows-release.yml` fails the build
  if the packaged `build_info.json`, the checked-out `HEAD` and the installer
  filename/manifest disagree.
