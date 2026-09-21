# O60R Owned-Room Hardware Evidence Gate

Tracking: Issue #83

## Purpose

O60R does not create or modify model-validation evidence. It audits a completed
owned-room Validation Campaign after the human physical measurement loop is
finished.

O70/O80 stay disabled unless this gate reports PASS for the exact persisted
campaign and current eligible ValidationRecord.

## Software harness

The software gate was merged to main by PR #85.

Main provides:

- `scripts/audit_o60_owned_room.py`
- `scripts/inventory_o60_owned_room.py`
- `scripts/run-o60-owned-room-gate.ps1`

The Python audit opens the source `cad-scenes.sqlite3` with SQLite
`mode=ro` and `query_only=ON`, takes a consistent backup into a temporary
database, and performs all repository validation on that temporary snapshot.

The source database is never opened writable by the audit.

On the temporary snapshot the harness:

1. loads the exact campaign id;
2. derives current campaign readiness;
3. resolves the current eligible O60 ValidationRecord for the SearchSpec;
4. requires the record campaign id/SHA to match;
5. deletes that ValidationRecord only from the temporary snapshot;
6. calls `CadModelValidationRepository.save()` with the immutable record,
   replaying the full save-time cross-evidence authority;
7. reopens the saved record and requires exact equality;
8. requires owned-room scope, evidence readiness, residual PASS, every trend /
   sensitivity / repeatability / separation gate PASS, every applicability
   check PASS, recommendation gate `eligible`, and no gate reasons.

The audit emits stable `O60R_...` key/value lines plus one compact
`O60R_AUDIT_JSON` record.

## Windows runner

The PowerShell runner:

- refuses a dirty worktree;
- fetches the exact remote gate branch;
- verifies the frozen O60E product head is an ancestor;
- rejects product-code changes after that head;
- detaches to the audited branch head;
- runs the read-only audit against an explicitly supplied data directory and
  campaign id;
- optionally writes the JSON report;
- restores the original branch/SHA and requires the repository to be clean.

The real command is intentionally not run by CI because it requires the user's
actual completed owned-room campaign data.

The runner defaults to `main` after PR #85 merge. It still freezes the O60E
product head and refuses unexpected product-code changes between that authority
and the audited main head.

`scripts/inventory_o60_owned_room.py` is a read-only operator preflight. It
uses the same mode=ro SQLite snapshot contract to list campaign ids, readiness,
candidate evidence and the current O70-entry ValidationRecord, and can query the
localhost REW API using GET-only endpoints. It does not create validation
evidence.

CI compiles both Python helpers, parses all PowerShell scripts, and runs the
runner with `-PreflightOnly`.

## Physical boundary

Before the real O60R gate can run successfully, a human must complete the
campaign measurement loop:

1. preregister the campaign before measurements;
2. apply/save each candidate SceneRevision;
3. save its Measurement Plan;
4. physically move the speakers/setup;
5. measure in REW;
6. import with `選択REW→Campaign実測`;
7. collect the preregistered repeatability measurements;
8. complete each Measurement Plan;
9. materialize the preregistered O30 objective evidence;
10. enter explicit geometry/band/routing applicability evidence — each PASS is
    bound to exact evaluator and source authority (automated re-derivation from
    scene/spec/batch/plan evidence, or a persisted immutable attestation);
11. build and save the campaign ValidationRecord.

No synthetic fixture, normal N60 import, or pre-campaign measurement may be used
to satisfy the real gate.

## RDC policy

RDC is not used while implementing or testing this software harness. When real
owned-room evidence exists, one consolidated RDC call should run the gate,
capture the printed ids/hashes/check results, and restore the original checkout.

The resulting acceptance record will be committed to GitHub before O70 work is
allowed to start.
## Windows SQLite handle closure

The first real read-only inventory preflight on the owned Windows machine exposed
a Windows-specific cleanup failure: repository methods using the sqlite3
connection context manager committed or rolled back transactions but did not
explicitly close the connection before the temporary snapshot directory was
removed. Windows therefore reported `WinError 32` for the snapshot database.

The O60R follow-up makes Search / RoomSim / Objective / ModelValidation
repository connections deterministic with `contextlib.closing`. A regression
test now runs the inventory CLI against an existing database so the Windows CI
exercises snapshot creation, repository initialization, and temporary-directory
cleanup rather than only the no-database fast path.

This changes SQLite connection lifetime only. It does not alter campaign,
prediction, validation, or recommendation semantics, and the source owned-room
database remains read-only during O60R inventory/audit.
## CI preflight versus real audit authority

O60R has two deliberately different checks:

- `-PreflightOnly` validates that the runner can resolve the frozen audited commit, that it remains an ancestor of the target branch, and that the audit harness is present. It **does not** reject later product-code development. Otherwise every O70/O80 software PR after O60R would be untestable by CI.
- the real owned-room invocation (without `-PreflightOnly`) still rejects unexpected product-code changes after `ExpectedProductHead`. Before the eventual physical campaign audit, that SHA must be explicitly advanced to the final software authority being measured.

This keeps CI useful during continued product development without weakening the real measurement gate or allowing a synthetic fixture to masquerade as audited owned-room evidence.

