# Round 17 — Windows platform correctness

Scope: every suspicion was verified against real Windows semantics on the
Windows box, not just by reading code. Categories: ENCODING (open()
without encoding= on text paths), PATHS (MAX_PATH, drive-relative/UNC
names, hardcoded separators, case handling), FILE LOCKING (Windows
mandatory locks vs POSIX rename/replace assumptions), ENV VARS
(TMP/TEMP, HOME vs USERPROFILE), LINE ENDINGS (CRLF round-trips),
SYMLINKS/JUNCTIONS (resolve() vs reparse points), PROCESS SPAWNING
(shell strings, console flashing, DLL search). Python 3.12.10, Windows
Server 2022, pytest `-n 4`, `QT_QPA_PLATFORM=offscreen`.
Branch `devin/rev17-plat`.

## Verified issues and fixes

### 1. `DirectorySource` — SYMLINKS/JUNCTIONS: directory junctions walked into targets outside the bundle root

`backend/src/htdt/capture_bundle.py`

`DirectorySource.list_files()` rejected `is_symlink()` dirs and files,
but Windows directory junctions and mount points are reparse points
that `Path.is_symlink()` never reports — verified empirically:
`is_symlink()` False, `os.walk(followlinks=False)` still descends into
the junction target, and `target.resolve()` silently redirects
out-of-root. A bundle directory containing a junction pointed at e.g.
`C:\Windows\System32` therefore leaked arbitrary host files through
`read_bytes()`, and `list_files()` enumerated the target as if it were
bundle content.

**Fix**: `list_files()` rejects `candidate.is_symlink() or
candidate.is_junction()` for both dirs and files, and `read_bytes()`
proves `target.resolve().relative_to(self.root)` before serving a byte —
a path routed through a junction now raises `payload escapes bundle
root`. Verified live: `mklink /J` junction inside a bundle root → listing
raises `symlink or junction directory forbidden`, `read_bytes('junc/
secret.txt')` raises `payload escapes bundle root`, `manifest.json` still
served.

### 2. `_validate_archive_member` — PATHS: rooted and drive-relative member names escaped the staging tree

`backend/src/htdt/migration_guard.py`

Pre-migration backup extraction checked `member_path.is_absolute() or
'..' in member_path.parts`. On Windows `is_absolute()` is False for
root-relative names (`/x`, `\x` — no drive), UNC names (`\\srv\share\x`
seen as a UNC `root`+`drive` only when backslashes survive), and
drive-relative names (`C:x` — drive without root). `staging /
member.filename` resolves each of those outside the staging root
(verified: `Path('/evil').is_absolute()` False; `Path('C:x').is_absolute()`
False; `C:/staging / '/evil'` → `C:\evil`; `C:/staging / 'C:x'` lands in
the drive's *current* directory context). Python's `zipfile` normalizes
backslashes to `/` on write, but hostile archives produced by
Explorer/WinRAR/.NET carry literal `\` members that bypass a `/`-only
parts check.

**Fix**: reject members where `is_absolute() or drive or root or
'\\' in member.filename or '..' in member_path.parts`. Verified against a
real zip: `/evil.txt`, `\evil.txt`, `\\srv\share\x`, `c:\evil.txt`,
`C:relative.txt`, `..\evil.txt`, `../x` all raise `Unsafe path in
pre-migration backup`; plain `assets/x.txt` still restores.

### 3. `str(relative_to(...))` asset paths — PATHS: backslash separators persisted in `relative_path` columns

`backend/src/htdt/database.py`, `cad_acoustic_treatment_repository.py`,
`cad_directivity_repository.py`, `cad_equipment_repository.py`,
`cad_measurement_quality_repository.py`,
`cad_measurement_repository.py`, `cad_video_geometry_repository.py`,
`cad_wave_excitation.py`

Twelve sites stored `str(path.relative_to(root))` into SQLite
`relative_path` columns. On Windows `str()` emits `dir\file.ext`
backslash separators — mixing `\` into a column that everywhere else
assembles POSIX-style paths (`joinpath`, `PurePosixPath`) and that any
future import/compare would need to normalize. Two codebases
(manifest/JSON paths vs. DB paths) disagreed in representation.

**Fix**: `.as_posix()` on all twelve sites. Regression test
`test_managed_asset_relative_path_uses_posix_separators` saves a real
source asset through `CadAcousticTreatmentRepository` and asserts the
stored `relative_path` contains `/` and no `\`.

### 4. `generate_self_signed_cert` — PROCESS SPAWNING: openssl console flash in the packaged windowed app

`backend/src/htdt/capture_receiver.py`

`subprocess.run(command, ...)` spawns `openssl.exe` without
`CREATE_NO_WINDOW`. The packaged app runs windowed (no console of its
own), so on Windows every cert generation flashes a console window at
the operator — the documented Windows-only subprocess bug class. All
other spawns in the codebase were already list-form with no `shell=True`.

**Fix**: `creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt'
else 0` — POSIX ignores the flag entirely.

## Clean areas checked

- **ENCODING** — all `open()`/`read_text`/`write_text` on text paths
  already carry `encoding=`; JSON/CSV/report writers explicit. No fix.
- **FILE LOCKING** — `export_io.write_text_atomic`/`write_bytes_atomic`
  write binary then `os.replace` from a dir-colocated `mkstemp` file —
  correct Windows atomicity; `_RelocationLock`/`InstanceLock` use
  `msvcrt.locking` byte-0 locks; `installation_handoff` swap rollback
  accounts for rename semantics. No fix.
- **ENV VARS** — `tempfile` honours TMP/TEMP; no bare `HOME` use on
  paths; `file_dialog` fallback tempdir only reached when unconfigured.
- **LINE ENDINGS** — CSV exports LF-normalized; `raw_mesh` PLY parser
  accepts CRLF/LF/CR terminators.
- **UNC/MAX_PATH** — `main.py` SPA route check already tests
  `PureWindowsPath.drive` + `resolve()`+`is_relative_to`; `managed_assets`
  uses resolve+`normcase` for case-insensitive roots.
- **Junction deletes** — verified `shutil.rmtree` removes the junction
  itself without deleting target contents; `_safe_data_path` catches
  junction escapes via `resolve()` in backup paths (fail-closed).

## Deferred

- **MAX_PATH (>260 chars)** — app cannot fix a host without long-path
  support (manifest/opt-in); deep project dirs remain a user-visible
  Windows limit. Not a code defect.
- **`native_upgrade._directory_size` junction overcount** — junction
  targets double-counted on size preflight; benign and conservative
  (over- rather than under-estimates free space need).

## Tests

`backend/tests/test_review_round17_platform.py` — 10 tests, all
reproduced live on Windows: junction rejection in list_files and
read_bytes (real `mklink /J` junction), parametrized escape member names
through a real rollback archive, direct validator backslash pinning, and
POSIX `relative_path` storage through the real repository save path.
Scoped suite (capture bundle, migration guard, security round 2,
native backup, persisted data, asset contract, project bundle, round 14
bundle, capture receiver, CAD acoustic treatment): 241 passed, 1
skipped.
