# N90 Windows acceptance — 2026-09-18

Tracking: Issue #77 / PR #79  
Milestone: N90 — Stable Windows release  
Canonical gate: A15 in `docs/CAD_EDITOR_ACCEPTANCE.md`

## Result

**PASS**

N90 product code was packaged as stable version `0.1.0`, then accepted on the owned Windows machine with an actual per-user installer update from the earlier `0.1.0.dev0` package.

The final owned-Windows gate used only a dedicated temporary program/data root. It did not modify the normal HTDT user-data root.

## Accepted heads and CI

- stable product/artifact head: `968a9461435ac37138ddd15526140c06613fccb8`
- final A15 gate head: `3ee2fb91b4976d7b0cac7b13718222cd6e359b76`
- product CI: run `35299355927` / CI #458 — PASS
- Windows Release Artifact: run `35299355977` / #23 — PASS
- gate-harness fix CI: run `35300374434` / CI #459 — PASS

The only change after the stable product artifact head was acceptance-harness cleanup: wait for Inno Setup's uninstaller self-removal before the harness performs fallback cleanup. No product binary/data implementation changed between the stable artifact and the final gate head.

## Installer evidence

Baseline:

- workflow run: `35297276617` / Windows Release Artifact #8
- source head: `3be739a11602dddb2a48e1a58fdeac3e9ea5dcaf`
- file: `HTDT-Setup-0.1.0.dev0.exe`
- installer SHA-256: `871f6f55f52fd7581c930e2fbb133f6ca03fcf3abaddf6cfef8d5d3659c1cf36`
- GitHub artifact ZIP digest: `sha256:afbc4c9c007c13485063d1a428766cf3a17933b83a095fc06777db509ba7b961`

Stable update:

- workflow run: `35299355977` / Windows Release Artifact #23
- source head: `968a9461435ac37138ddd15526140c06613fccb8`
- file: `HTDT-Setup-0.1.0.exe`
- installer SHA-256: `81f23f7fdf545ae06a194a54b02bf17f4c890126ec37efb14f2f4131d3496aa4`
- GitHub installer artifact ZIP digest: `sha256:0f8c2c38543349ed8c90d7721e91ddd3b7abe963753d0914a268eaae09081cd7`
- GitHub native-package artifact ZIP digest: `sha256:3c30d87ef8319c532c2041093861a5b99e4a571c49d96fa27115c0475e31a092`

## Owned-Windows environment

- Windows 11 Pro build 26200
- AMD Ryzen 7 8845HS with Radeon 780M Graphics
- RAM: 31.31 GiB
- AppliedDPI: 192 (200%)
- repository before gate: detached `5ede848e8e0b0967a50c04c83ff679a649ca439b`, clean

## A15 evidence

Final rerun:

```text
N90_RERUN_DOWNLOADS=115363622,115352849
N90_RERUN_GATE_HEAD=3ee2fb91b4976d7b0cac7b13718222cd6e359b76
N90_PRE_STATUS_COUNT=0
N90_GATE_SHA=3ee2fb91b4976d7b0cac7b13718222cd6e359b76
A15_BASELINE_GUI_SEEDED True
A15_BASELINE_REVISION 82c9bc5c-c49a-4747-ae10-6ab19b595e95
A15_BACKUP_CREATED True
A15_UPDATE_PRESERVED_DATA True
A15_RESTORE_EXACT True
A15_REOPEN_AFTER_RESTORE True
A15_UNINSTALL_RETAINED_USER_DATA True
A15_REINSTALL_OPENED_RETAINED_DATA True
A15_FINAL_DATA_RETENTION True
N90_A15_WINDOWS_RESULT PASS
N90_GATE_EXIT=0
N90_RESTORED_SHA=3ee2fb91b4976d7b0cac7b13718222cd6e359b76
N90_POST_STATUS_COUNT=0
N90_RESTORE_OK=True
N90_HARDWARE_GATE_RESULT=PASS
N90_RERUN_GATE_EXIT=0
N90_RERUN_RESTORED_SHA=5ede848e8e0b0967a50c04c83ff679a649ca439b
N90_RERUN_POST_STATUS_COUNT=0
N90_RERUN_RESTORE_OK=True
N90_FINAL_A15_RESULT=PASS
```

The gate exercised:

1. silent per-user install of `0.1.0.dev0`;
2. packaged native GUI launch and seed/save into a dedicated temporary data root;
3. packaged `--backup`;
4. in-place install of stable `0.1.0` with the same AppId;
5. exact SceneRevision/content retention across update;
6. packaged `--restore`;
7. native GUI reopen after restore;
8. uninstall with user data retained;
9. stable reinstall;
10. native GUI reopen from retained data;
11. final uninstall with user data retained;
12. repository checkout restored to the original detached SHA and clean state.

## Backup/restore coverage outside A15

A15 is complemented by the N90 focused Windows CI invariants:

- SQLite backup API is used instead of copying a live database file.
- manifest/member SHA-256 and sizes are verified.
- path traversal, duplicate/unexpected archive members and symlink-like entries are rejected.
- SQLite integrity and foreign keys are checked before restore.
- content-addressed N60 measurement assets are included and verified.
- tampered assets are rejected.
- failed restore leaves current managed data intact.
- a failure during the managed-data swap rolls back the original database/assets.
- Windows-style measurement-asset relative paths are normalized into portable archive paths.

## Initial gate false negative

The first owned-Windows run reached all product assertions through `A15_FINAL_DATA_RETENTION True`, but the acceptance harness immediately invoked the Inno uninstaller a second time while `unins000.exe` was still self-cleaning. That second cleanup invocation returned exit code 1.

Commit `3ee2fb91b4976d7b0cac7b13718222cd6e359b76` changed only the harness to wait for Inno self-removal. CI #459 passed, and the complete A15 gate was rerun to the PASS result above.

## Release contract accepted

- Native Qt/CAD is the product authority.
- Stable personal Windows version is `0.1.0`.
- Install root and user-data root are separate.
- Uninstall does not silently delete user data.
- Backup/restore executes before QApplication creation.
- Package build consumes the committed Windows dependency lock.
- Windows installer uses a stable AppId and per-user install.
- Frontend/browser build is no longer a native release gate.
- The personal build is unsigned; code signing is not a correctness requirement for this release.
- O70/O80 remain disabled until genuine owned-room O60 validation evidence satisfies the model gate.
