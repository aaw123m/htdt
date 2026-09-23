# Software-wide review and hardening — 2026-09-18

## Scope

This review covered the current native product path on `main` after O70/O80
software completion:

- native startup and process/data-directory ownership;
- SQLite repositories and immutable authority;
- native backup/restore;
- O10–O80 optimization boundaries;
- Windows package/installer CI;
- README/canonical documentation consistency;
- maintainability of the native GUI composition.

The review deliberately did not fabricate owned-room evidence. Issue #83 remains
the authority for physical O60R validation.

## Findings fixed in this hardening slice

### H1 — native data directory had no process-level ownership

The legacy browser launcher had instance locking, but the supported
`htdt.native_cad` path did not use it. Two native GUIs, or a GUI and a restore/
synthetic-seed process, could therefore share the same `cad-scenes.sqlite3`.

Fix:

- `native_cad.py` now acquires the existing OS-level `SingleInstanceGuard`
  for the selected data directory before GUI, backup, restore, or synthetic seed;
- contention fails before repository/application mutation;
- the guard is released in a `finally` block;
- a meaningful maintenance-CLI test covers fail-closed contention.

### H2 — native DB had no central schema/downgrade authority

The legacy browser DB has a schema version, but the native
`cad-scenes.sqlite3` previously depended only on distributed
`CREATE TABLE IF NOT EXISTS` / ad-hoc column migrations.

Fix:

- native schema authority starts at `NATIVE_SCHEMA_VERSION = 1`;
- pre-versioned 0.1.0 native DBs are adopted only when their tables look native
  and SQLite integrity/foreign-key checks pass;
- migration application is transactional and recorded in
  `native_schema_migrations`;
- a DB written by a newer native schema is rejected rather than downgraded;
- native backup validation also rejects an explicitly newer schema;
- tests cover new DB versioning, legacy adoption, unrelated DB rejection, and
  newer-schema rejection.

This v1 migration is intentionally metadata-only. It does not rewrite immutable
Scene/O10–O80 payloads.

### H3 — native restore could allocate an entire ZIP member in memory

Path traversal, symlink, SHA-256 and SQLite checks already existed, but restore
used `archive.read()` for each member and had no native archive expansion
budget.

Fix:

- archive byte limit;
- member byte limit;
- total expanded byte limit;
- member-count limit;
- manifest byte limit;
- excessive compression-ratio rejection;
- streaming extraction with incremental SHA-256 instead of whole-member memory
  allocation;
- regression test for bounded expansion.

The limits are deliberately generous for personal REW assets; they are resource
safety bounds, not normal workflow quotas.

### H4 — two native repositories did not deterministically close SQLite handles

`CadConstraintRepository` and `CadPredictionRepository` still relied on
`sqlite3.Connection.__exit__`, which commits/rolls back but does not itself
provide the explicit close discipline used by the hardened native repositories.

Fix:

- both repositories now use `contextlib.closing(...), connection`;
- constraint connections also enable `PRAGMA foreign_keys=ON`;
- constraint repository participates in native schema compatibility checks.

This keeps the Windows handle lifecycle consistent with the O60R fix that
previously addressed real-machine SQLite file-handle retention.

### H5 — README understated O70/O80 completion

Fix:

- README now states that N05–N90 and O10–O80 software paths are implemented;
- `development_synthetic` and production-owned-room boundaries are explicit;
- native schema, process lock, and bounded restore behavior are documented.

### H6 — O80 “toe-in” wording overclaimed physical semantics

The implemented `aim_yaw_deg` changes acoustic `aim_xyz`, while
`SceneEntity.orientation` remains the cabinet/body pose. Presenting this as
full physical cabinet toe-in could make users assume orientation-dependent
footprint/clearance had been re-evaluated.

Fix:

- native Extended Search UI and canonical O70/O80 documentation now call the
  implemented parameter **acoustic aim yaw**;
- physical cabinet toe-in is explicitly a separate future extension requiring
  body-yaw change plus hard-constraint re-evaluation;
- documentation also states that current O70 GP features are O10 XYZ axes and
  do not adapt over O80 extended parameters.

## Reviewed and retained

No authority bypass was found that lets synthetic evidence become owned-room
evidence. The following are retained:

- immutable SceneRevision/SearchSpec/candidate-set/evaluation/ValidationRecord
  binding;
- exact O60 campaign authority for production recommendation;
- separate `development_synthetic` and `production_owned_room` scopes;
- REW Room Simulator prohibition for directional/aim capability;
- O80 exact SearchSpec/candidate-set/model validation replay;
- Windows package/installer/data-retention workflow.

## Non-blocking architecture follow-ups

These are not defects in the accepted O10–O80 software authority, but should be
handled before large new feature growth:

1. split the ~141 KB `OptimizationWorkspaceWindow` into focused controllers/
   dock components while preserving domain/repository contracts;
2. if desired, design an Adaptive Extended Search that includes O80 parameters
   in the surrogate feature vector rather than implying the current O70 does so;
3. implement physical cabinet toe-in only with orientation-aware hard-constraint
   evaluation;
4. separate legacy browser-only dependencies from the native runtime dependency
   surface when the rollback path can be isolated cleanly;
5. add narrowly scoped static analysis after existing code is made clean under a
   chosen rule set;
6. choose a repository/distribution license explicitly rather than having the
   software select legal terms automatically.

## Verification

GitHub Actions is the verification authority for this slice. No RDC call is
required unless GitHub Actions exposes a Windows-runtime failure that cannot be
resolved from CI evidence.
