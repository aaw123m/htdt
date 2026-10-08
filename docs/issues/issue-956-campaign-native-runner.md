# Issue #956 — campaign UI drives the native automated runner end-to-end

REV72. The measurement campaign page now executes the registered plan
with the HTDT-native sweep engine (#869) through the automated campaign
runner (#875): one click materializes the plan, one explicit arm
approval starts output, same-position entries all run unattended, the
runner pauses only for physical mic moves, and cancel/pause/resume are
journaled on the sealed event log so a restart resumes exactly.

## What landed

- `backend/src/htdt/cad_campaign_native.py` — the domain slice:
  - `materialize_native_campaign_plan` converts a #529
    `MeasurementRunnerPlan` into the canonical `mcplan-` execution plan
    (position = measurement target, channel = role+speaker-set binding,
    purpose→`MeasurementRole`, `campaign_ref`/`scene_ref` pinned). It
    fails closed (`NativePlanConversionError`) on unknown positions,
    missing/empty routings, gapped or non-uniform repeats, and any plan
    whose materialized queue does not cover the runner matrix exactly —
    nothing is invented or dropped.
  - `resolve_cell_assignments` resolves each queue entry to exactly one
    runner cell; entries with zero or several candidate cells park in
    `ambiguous` for manual review instead of being guessed.
  - `build_native_preflight` is read-only: enumerated devices, every
    declared routing, level + policy ceiling, position/gate list,
    calibration state, SIMULATED flag, and a real engine PRECHECK per
    distinct routing. Any blocked reason disarms the operator dialog.
  - `NativeCampaignDrive` wraps `CadCampaignExecutionRepository.runner_for`
    — rebuilding from the persisted journal IS the restart-resume path.
  - `cell_native_labels` renders per-cell state honestly: a SIMULATED
    bound cell says `完了（品質 ...）SIMULATED`, ambiguous cells say
    `要レビュー`, and runner-cell status is never promoted to a measured
    verdict.
- `measurement_workflow.py` — controller: `materialize_native_campaign`
  (deterministic `mcplan-` per runner plan + routing + stimulus — the
  same inputs re-attach the existing journal), `native_preflight`,
  `native_campaign_drive`, `native_campaign_status`,
  `native_campaign_for_runner_plan`.
- `measurement_page_workspace.py` — a "HTDTネイティブ自動実行" card on the
  campaign page: 自動実行を準備 → read-only preflight dialog → explicit
  アーム承認 button (disabled while PRECHECK is blocked) → unattended
  drive; position-move prompt with a Japanese confirmation button;
  pause/resume/cancel; progress + remaining + missing-evidence lines;
  SIMULATED banner; the cell table gains a ネイティブ実行 column. The
  REW delegated path and the manual per-cell flow are untouched.
- Arm contract: the provider echoes exactly the routing+level the
  operator approved; an engine presenting different parameters is
  refused (`arm_confirmation_unavailable`) — spec or saved settings
  never substitute for the approval click.

## Test evidence (`backend/tests/test_issue_956_campaign_native.py`, 17 tests)

- Exact queue↔cell matrix mapping; deterministic `mcplan-`; fail-closed
  conversion (missing routing, unknown target); ambiguous cells parked.
- Preflight lists everything; WASAPI stub blocks arming (fail-closed).
- E2E: one arm → same-position entries auto-run → stop only at position
  move → resume → `campaign_completed`; every `mcrun-` binds a real
  `swrun-` + raw assets reopenable; SIMULATED labels on cells.
- Restart resumes from the journal with zero re-recordings
  (`factory.calls` proves only remaining entries run).
- Clipping retry at reduced level (policy-gated), timeout/truncation
  terminal-honest, routing drift blocks until operator resume,
  pause/cancel journaled, unarmed run emits nothing.
- Workspace E2E: preflight dialog arm button clicked once → position
  gates only → completed; `_commit_campaign_cell`/`_skip_campaign_cell`
  are patched to fail if invoked (per-cell clicks = 0); runner cells
  remain `not_started` — fake evidence never promotes them.

## Remaining gates

- Real WASAPI backend + physical mic moves — the existing #869/#875
  physical gates; the fake-backend E2E is the scripted seam only.

Refs #956, #875, #869, #876.
