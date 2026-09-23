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
SHA-256, the build toolchain (`toolchain.python` / `toolchain.pip`), GitHub
run context and a UTC timestamp. The `windows-release` workflow uploads it
with the installer artifact and verifies it against the checked-out source.

## Dependency closure and reproducibility

The single authority for the third-party Python closure that ships inside
`HTDT.exe` and the installer is the hash-pinned lock
`backend/requirements-n05-windows.lock`. Every pin is `==`-exact and carries
`--hash=sha256` digests for all released files, so installs run under
`pip install --require-hashes` and every downloaded artifact is digest-verified.

- `scripts/build-native.ps1` creates its build venv from an explicitly
  supplied interpreter (`-PythonExe`, defaulting to `python` on PATH — never
  `py -3.12`), requires CPython 3.12 on Windows x64, installs the lock under
  `--require-hashes`, then installs HTDT with `--no-deps`. The interpreter
  version and lock SHA-256 are embedded in `build_info.json`.
- `windows-release.yml` pins the release toolchain (`python-version:
  "3.12.10"`, matching the lock header) and runs the full backend test suite
  in a clean venv holding exactly the hash-verified locked closure plus the
  pinned `backend[dev]` test harness; `check_dependency_lock.py
  --verify-installed` then proves no locked package moved. The package job
  therefore proves the shipped closure passed the release gate.
- `python scripts/check_dependency_lock.py` verifies the lock stays
  consistent with `pyproject.toml` (`ci.yml` and
  `backend/tests/test_dependency_lock.py` enforce it). After re-pinning,
  refresh digests with `--refresh-hashes`.
- All GitHub Actions are pinned to full commit SHAs (`# vX.Y.Z` comments keep
  the readable version) and workflows declare least-privilege
  `permissions: contents: read`.

### Intentional dev-vs-shipped differences

`ci.yml` deliberately floats on the latest CPython 3.12.x with freshly
resolved transitive dependencies (`-e ".\backend[dev]"`), so upstream drift
surfaces early in ordinary PRs. The shipped environment is the locked
closure above; the release workflow tests that exact closure before
packaging, so a CI-green commit cannot ship an untested dependency set.

## CI verification

- `backend/tests/test_release_identity.py` fails CI if `pyproject.toml` stops
  deriving the package version from `htdt.__version__` or if the Inno Setup
  fallback diverges.
- `backend/tests/test_dependency_lock.py` fails CI if the lock loses hash
  pinning or diverges from `pyproject.toml`, or if any workflow action is
  not pinned to a commit SHA.
- The `Verify release identity` step in `windows-release.yml` fails the build
  if the packaged `build_info.json`, the checked-out `HEAD`, the pinned
  Python toolchain or the installer filename/manifest disagree.
