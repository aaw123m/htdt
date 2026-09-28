# Round 11 — tail-item implementation round

Scope: the remaining named deferrals from `round10-optimizer.md`,
`round10-ux.md`, `round10-dataj.md`, `round9-report.md`, and
`round10-fresh.md`. Each item was re-verified against current code before
implementation; where docs had drifted, the code won. Branch
`devin/rev11-tail`.

Method: implementation + new regression tests
(`backend/tests/test_cad_auralization_leg.py`,
`backend/tests/test_qt_smoke_round11.py`, one added case in
`test_cad_objective_repository.py`), then the full `backend/tests` suite
(`pytest -q -n 4`, `QT_QPA_PLATFORM=offscreen`).

## Verdict table

| # | Item | Verdict |
|---|------|---------|
| 1 | Pareto double replay (round10-optimizer deferred #9) | FIXED — shared `validated` authority memo across the Optimize refresh lane |
| 2 | Ctrl+Z/Ctrl+Y in Optimize workspace (deferred #11) | FIXED — `CommandShortcutBinder` on the workspace, the Room pattern applied to `_make_optimization` |
| 3 | Campaign-evidence synchronous lanes | RESOLVED AS DOCUMENTED — loops are bounded; bound stated below, no workers needed |
| 4 | Auralization WAV leg (round10-dataj) | IMPLEMENTED for the measured leg — all three prerequisites + repository + materialization; the predicted leg stays an honest non-claim |
| 5 | Per-command `on_error` | STILL DEFERRED — no real consumer exists |
| 6 | ~30 untested Qt modules | PARTIAL — offscreen smoke tests added for the highest-traffic untested modules |
| 7 | Topology-lane per-page rescan | STILL DEFERRED — conditional perf item, separate authority contract |

## 1. Pareto double replay — FIXED

The Optimize Pareto refresh ran three independent authority lanes over the
same immutable evaluations: `latest_evaluations_by_candidate`,
`find_pareto_set_by_sha`, and `save_pareto_set` each re-resolved every
evaluation's candidate-set scan, input-authority replay and vector
reproduction. `cad_objective_repository` now accepts an optional
caller-supplied memo, `validated: dict[tuple[str, str], _EvaluationAuthority]`
keyed on the sealed `(evaluation_id, evaluation_sha256)` pair:

- The memo consults **and** populates inside `_validated_evaluation`; the
  row-existence SELECT plus all row-column/payload equality checks still
  run on every call — a deleted or rewritten row fails closed whether or
  not the memo is warm. Only authority *recomputation* is skipped.
- Threaded through `get_evaluation`, `get_evaluations`,
  `list_evaluations`, `latest_evaluations_by_candidate`,
  `_require_pareto_authority`, `_validated_pareto_set`, `save_pareto_set`,
  `find_pareto_set_by_sha`, `get_pareto_set`, `list_pareto_sets`.
  `_require_pareto_authority` additionally shares one `scans`/`batches`
  derivation across the member evaluations it walks.
- Both Optimize refresh lanes (`optimization_workflow_controller`,
  `optimization_workspace`) create one `validated` dict per refresh and
  pass it to all three calls, matching the `authorities` precedent in
  `cad_joint_optimization_repository`.

Regression test:
`test_cad_objective_repository.py::test_pareto_refresh_lane_replays_each_evaluation_authority_once`
counts `_require_evaluation_authority` invocations — with the shared memo
each sealed evaluation resolves exactly once across listing + find + save;
without it the save lane resolves them all again.

## 2. Ctrl+Z/Ctrl+Y in Optimize — FIXED

`_make_optimization` now creates a `CommandShortcutBinder` parented to the
Optimize workspace widget over `("project.save", "edit.undo", "edit.redo")`
with `WidgetWithChildrenShortcut` — the exact `CadInputController` pattern.
Consequences:

- `Ctrl+S`/`Ctrl+Z`/`Ctrl+Y` fire only while focus is inside the Optimize
  widget tree, so the Room mount's binder over the same command ids never
  conflicts — scoping, not enable/disable races.
- FOCUS_SAFE semantics are preserved: `edit.undo`/`edit.redo` shortcuts are
  disabled while a text/numeric editor inside the workspace owns focus,
  so Ctrl+Z still edits the focused field rather than the document.
- `project.save` is a GLOBAL shortcut: it fires while a form field still
  owns focus, before `editingFinished` commits the value. The bound
  executor now runs `flush_focused_text_editor(workspace)` first — the
  Optimize-side counterpart of the Room mount's `commit_pending_editor`.
- `optimization.compare_candidates` declares no shortcut so it stays
  registry-bound only.

Regression test:
`test_qt_smoke_round11.py::test_optimization_mount_binds_undo_redo_shortcuts`
builds the mount through `_make_optimization`, asserts `Ctrl+S`/`Ctrl+Z`/
`Ctrl+Y` QShortcuts exist on the workspace widget, and that emitting
`Ctrl+Z` reaches `controller.undo`. The pre-existing UX140 contract test
(`test_ux140_workflow_application_binds_commands_without_legacy_qactions`)
still passes unchanged — bind/unbind semantics are untouched.

## 3. Campaign-evidence synchronous lanes — bounded, documented

Verified against current code rather than moved to workers:

- `materialize_objective_evidence` iterates `campaign.candidates` — the
  campaign's preregistered assignment list — with bounded repository gets
  and append-only `save_evaluation` calls. `save_evaluation`'s
  candidate-membership scan rides `_cached_enumeration` (the spec-keyed
  LRU in `cad_search`), so each save costs one page-materialization pass,
  no solver math, no re-enumeration.
- `build_validation_record` → `readiness()` shares one
  batches/plans/evaluations listing already; the loop is the same bounded
  candidate set.
- `complete_measurement_plan` iterates the user-selected `measurement_ids`
  (indexed gets only).

Bound: O(preregistered campaign candidates) and O(selected measurements)
respectively — both lists are user-authored and realistically tens, not
thousands, of rows. A worker + cancel surface would be dead UI weight; the
round10 doc itself reached this conclusion and it holds. Revisit if
campaigns grow past a few hundred evidence rows.

## 4. Auralization WAV leg — IMPLEMENTED (measured leg)

All three named prerequisites exist and compose honestly — the round10
doc's "no producers" claim was stale for the measured path:

- **`cad_auralization_service.resolve_impulse_authority`** seals a
  persisted `CadImpulseResponseDataset` as the `ImpulseAuthorityRef`
  (`kind='measured'`, `artifact_sha256=dataset_sha256`, `decoded_pcm_sha256`
  over the float64 samples the renderer consumes). The REW importer derives
  `1/dt` rates from decimal timestamps, so the integer lane accepts the
  nominal rate within 1e-5 relative tolerance — a genuinely fractional
  rate (44100.5) fails closed. `absolute_amplitude_authority` requires
  `amplitude_reference='full_scale'` + `calibration_state='calibrated'` +
  unnormalized — anything weaker renders relative level.
- **`decode_dry_program_wav` + `register_dry_program_asset`** are the
  reverse adaptation of `encode_wav_pcm_s16le`: mono uncompressed integer
  PCM only (8/16/24/32-bit; µ-law/float/multichannel rejected — downmixing
  is a product decision), decoded to float64 ±1.0, bytes installed
  content-addressed via `ManagedAssetStore.ensure_installed`, with
  `program_level_authority='unknown'` (imported WAV carries no level
  claim).
- **`build_measured_render_spec`** pins the spec from the persisted
  measurement record: `document_id`/`scene_revision_id`/
  `scene_content_hash` from `get_measurement`'s authority read (which
  re-verifies the entity + reference position), the record itself as the
  source-scenario authority (`source_scenario_sha256 =
  measurement_sha256(record)` — it seals channel_role/source_speaker_ids/
  routing_evidence), and `receiver_id = receiver_entity_id =
  measurement_entity_id` matching the scene binding convention.
  `system_variant` stays unpinned — measurements bind the exact revision.
- **`CadAuralizationRepository`** mirrors `CadIRAnalysisRepository`:
  append-only `save_render_spec`/`get_render_spec`/
  `find_render_spec_by_sha`, `save_artifact(artifact, wav_bytes)` requiring
  the exact persisted spec (`spec_semantic_sha256` match) and
  `sha256(wav) == output_asset_sha256`, managed-asset WAV storage under
  `measurement-assets`, `read_artifact_wav` via self-verifying
  `read_verified`. `materialize_measured_auralization` renders + seals +
  persists in one lane.
- **Persistence**: `cad_auralization_render_specs` +
  `cad_auralization_artifacts` added to `NATIVE_BASELINE_DDL` (all
  migrations replay the tuple), registered in `_UNBOUND_PAYLOAD_TABLES`
  and `_TABLE_POLICY` as `STRUCTURAL_ONLY` (canonical payload read path —
  same class as the IR analysis tables).
- **The predicted leg stays unimplemented**: no producer of a predicted
  `ImpulseAuthorityRef` exists in the codebase (the acoustic solver lane
  has no IR artifact output), so `kind='predicted'` remains a contract
  the schema supports but no service fabricates — honest non-claim.

Tests: `test_cad_auralization_leg.py` (8 tests) — WAV decode roundtrip
against `encode_wav_pcm_s16le`, stereo/non-WAV rejection, managed-asset
installation, absolute-authority gating, fractional-rate fail-closed, and
an end-to-end measured leg: persisted measurement + IR dataset → spec →
materialize → persisted artifact with self-verified WAV bytes; append-only
and byte-mismatch failures verified.

## 5. Per-command `on_error` — still deferred

Re-checked: `CommandRegistry.execute` routes every failure through
`self._error_handler`, and the shell installs the retry-with-same-command
handler (`on_retry=lambda: self.registry.execute(definition.command_id)`).
No command exists that needs a *different* failure surface (e.g.
retry-with-different-args); the deferral stands for the documented reason —
speculative API until a real consumer appears.

## 6. Untested Qt modules — smoke coverage added

`test_qt_smoke_round11.py` (9 tests) covers instantiation + primary-action
wiring for the highest-traffic previously-untested modules:

- `SeatPriorityPanel` — member tree + MLP preset button click.
- `DeliverablesDialog` over the real `DeliverablesCatalogService` —
  project scope label + every rendered 書き出し button reaches `on_command`.
- `CaptureReceiverPanel` + `PairingDialog` over the real
  `CaptureReceiverController`/`ApplicationPreferenceStore` — enable combo,
  port controls, scope combo, QR-offer button.
- `MaterialDialog`, `TreatmentDefinitionDialog`, `SurfaceMaterialPanel` +
  `RoomTreatmentPanel` over `RoomWorkspaceController`, and
  `RoomAcousticsTabs.refresh` reaching all three child panels.
- `SystemExpansionRoomPanel` over the real workflow service.
- `OptimizationWorkflowWorkspace.select_section` over all six
  `OPTIMIZATION_PAGE_IDS` — exercises every optimizer page's construction
  path and its controllers transitively.
- The `_make_optimization` shortcut binder (item 2).

Editor-chain modules (`RoomEditorWindow` → `MeasurementWorkspaceWindow`)
and the optimizer page controllers were already covered transitively by
`test_measurement_workspace_layout` / `test_optimization_workflow_workspace`;
the remaining untested modules are lower-traffic dialogs and legacy
widgets — same standing deferral as prior rounds.

## 7. Topology per-page rescan — still deferred

`_all_o10_candidates` (`cad_topology_search.py:856`) does call
`generate_search_space` per page rather than riding `_cached_enumeration`
— but it enumerates a *rewritten* constraint spec
(`_o10_prefilter_constraint_spec`) pinned to `spec.search_sha256`, a
different cache authority than the CAD lane's `search_spec_sha256`. A
correct LRU would need its own key derivation over the rewritten spec —
a separate contract, and the deferral itself is conditional ("when it
next hurts"). Documented, not implemented.

## Files changed

- `backend/src/htdt/cad_objective_repository.py` — `validated` memo.
- `backend/src/htdt/optimization_workflow_controller.py`,
  `backend/src/htdt/optimization_workspace.py` — memo wiring.
- `backend/src/htdt/workflow_application.py` — Optimize
  `CommandShortcutBinder` + save-time editor flush.
- `backend/src/htdt/cad_auralization_service.py` (new) — measured-leg
  resolver/provider/spec composer/materializer.
- `backend/src/htdt/cad_auralization_repository.py` (new) — spec/artifact
  persistence.
- `backend/src/htdt/cad_schema_ddl.py` — two tables.
- `backend/src/htdt/native_row_integrity.py` — `_UNBOUND_PAYLOAD_TABLES`.
- `backend/src/htdt/native_authority_audit.py` — `_TABLE_POLICY`.
- `backend/tests/test_cad_objective_repository.py` — memo regression.
- `backend/tests/test_cad_auralization_leg.py` (new) — 8 tests.
- `backend/tests/test_qt_smoke_round11.py` (new) — 9 tests.
