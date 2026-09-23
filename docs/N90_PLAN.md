# N90 Stable Windows Release plan

Tracking: Issue #77  
Base: main `b0b56497255425b5b343c6f5f52763d11dbf5ee6`

## Scope

N90 completes the CAD-first stable personal Windows release without waiting for O70/O80. O70 remains gated by real owned-room O60 evidence.

### N90a — data backup / restore authority

The managed native data root is the existing `default_data_dir()`:

- default: `%LOCALAPPDATA%\HomeTheaterDigitalTwin`;
- SQLite authority: `cad-scenes.sqlite3`;
- immutable N60 raw assets: `measurement-assets/<sha256>`.

Backup format `.htdt-backup` is a ZIP container with a canonical versioned manifest. A backup contains:

- a consistent SQLite snapshot created with SQLite's backup API, not a raw live-DB copy;
- every raw asset referenced by `cad_measurement_assets`;
- SHA-256 and byte size for every archived file;
- backup schema version, application version and timestamp;
- a manifest identity SHA over canonical metadata.

Validation rejects:

- duplicate/absolute/`..` archive members;
- symlink-like ZIP entries;
- unexpected members;
- file size/hash mismatch;
- corrupt SQLite or foreign-key violations;
- missing or mismatched content-addressed measurement assets.

Restore validates and stages the entire archive before changing live data. Existing data is first written to a sibling pre-restore backup archive. Live DB/assets are then swapped with rollback-on-failure semantics. A running app holding the Windows DB file naturally prevents the rename rather than partially overwriting it.

### N90b — packaged maintenance CLI / reproducible package

`HTDT.exe` / `python -m htdt.native_cad` exposes maintenance commands before QApplication creation:

- `--backup <path>`;
- `--restore <path>`;
- `--version`.

The PyInstaller package remains onedir and Python 3.12 x64. Build uses the committed Windows dependency lock as the install source rather than resolving a new transitive graph for every package build.

### N90c — installer / update / uninstall

Use a per-user Windows installer with a stable AppId and no administrator requirement.

- install under `%LOCALAPPDATA%\Programs\Home Theater Digital Twin`;
- user data remains under `%LOCALAPPDATA%\HomeTheaterDigitalTwin`;
- rerunning a newer installer updates the program in place;
- uninstall removes app files/shortcuts but never silently deletes user data;
- signing is optional for the personal build and not treated as a correctness gate.

### N90d — product CI / legacy retirement

Native app/package is the product authority. Browser/FastAPI compatibility is not a release requirement.

- package/release workflow builds the Windows artifact from committed sources/lock;
- packaged CLI smoke checks run without development venv;
- frozen browser source may remain for rollback/history, but frontend build is not allowed to dictate native release correctness after the migration slice is documented.

### N90e — A15

Final owned-Windows gate performs one consolidated session:

1. clean install;
2. create/save native data;
3. create backup;
4. install newer build over existing build;
5. verify data remains;
6. restore backup and reopen;
7. uninstall;
8. verify program removed and user data retained;
9. reinstall and verify retained data opens.

RDC is reserved for this final installer/UI gate only. CI handles backup corruption, restore rollback, package build and installer construction first.

## Safety / authority

- Restore never treats a ZIP manifest as trusted merely because it parses.
- No automatic deletion of user data.
- No O70 automatic recommendation is enabled by N90.
- No cloud updater/service or enterprise deployment framework.
- No compatibility obligation for the frozen browser UI.

## Implementation sequence

1. backup/restore domain + focused invariant tests;
2. packaged maintenance CLI;
3. reproducible PyInstaller build;
4. per-user installer and package workflow;
5. legacy native-release CI cleanup;
6. A15 harness/preflight;
7. one consolidated owned-Windows A15 gate;
8. acceptance/status docs and merge.


## Completion result — 2026-09-18

N90 implementation and A15 are complete on PR #79.

- stable product version: `0.1.0`
- product/artifact head: `968a9461435ac37138ddd15526140c06613fccb8`
- final A15 gate head: `3ee2fb91b4976d7b0cac7b13718222cd6e359b76`
- CI #458 / run `35299355927`: PASS
- Windows Release Artifact #23 / run `35299355977`: PASS
- A15 harness-fix CI #459 / run `35300374434`: PASS
- owned-Windows A15: PASS
- detailed evidence: [N90 Windows acceptance](N90_ACCEPTANCE_2026-09-18.md)

The stable installer was accepted as an in-place update from `0.1.0.dev0` to `0.1.0`. Backup/restore, GUI reopen, uninstall data retention, reinstall/reopen and final repository cleanup all passed.

O70/O80 remain intentionally disabled because the independent owned-room O60 evidence gate is not yet satisfied.
