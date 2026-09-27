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
  version cannot diverge from `__version__` (`test_release_identity.py`
  asserts this wiring).
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
   written by `scripts/build-native.ps1` with the commit SHA, an optional
   CI build id (`GITHUB_RUN_ID` when present), the dirty flag and the
   SHA-256 of `backend/requirements-n05-windows.lock`.
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
SHA-256, the build toolchain (`toolchain.python` / `toolchain.pip`), CI run
context when present and a UTC timestamp. The manifest is the artifact a
release must ship alongside the installer so the binary's provenance is
verifiable against the checked-out source.

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
- `scripts/build-native.ps1` is the packaging gate: it checks lock
  consistency (`check_dependency_lock.py`) before creating the build venv,
  installs under `--require-hashes`, and then runs
  `check_dependency_lock.py --verify-installed` to prove the build venv
  holds exactly the locked versions. For a release, run the full backend
  test suite first in an environment holding exactly the locked closure,
  so the shipped bits are the tested bits.
- `python scripts/check_dependency_lock.py` verifies the lock stays
  consistent with `pyproject.toml` (`build-native.ps1` and
  `backend/tests/test_dependency_lock.py` enforce it). After re-pinning,
  refresh digests with `--refresh-hashes`.

### Intentional dev-vs-shipped differences

Local development installs the backend editable (`run-local.ps1` runs
`pip install -e .\backend`), so day-to-day work floats on freshly resolved
transitive dependencies and upstream drift surfaces early. The shipped
environment is the locked closure above; `build-native.ps1` installs and
verifies that exact closure when packaging, so keep the lock current
rather than assuming a locally-green change ships an identical
dependency set.

## Test verification

- `backend/tests/test_release_identity.py` fails if `pyproject.toml` stops
  deriving the package version from `htdt.__version__` or if the Inno Setup
  fallback diverges.
- `backend/tests/test_dependency_lock.py` fails if the lock loses hash
  pinning or diverges from `pyproject.toml`, or if `build-native.ps1`
  stops enforcing the locked closure (interpreter pinning,
  `--require-hashes`, lock consistency check, `--verify-installed`). If
  `.github/workflows` ever returns, it also re-checks that every workflow
  action is pinned to a commit SHA.
- `HTDT.exe --version`, the installer filename and the manifest must all
  agree on the `<version>+g<sha8>[.dirty]` display version derived from
  `build_info.json`.
