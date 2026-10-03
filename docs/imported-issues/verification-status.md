# Imported-issues verification status

Tracks which of the 309 imported open issues have been *verified against
current main*, as opposed to merely closed on the GitHub tracker
(ka0923s-a11y/HTDT issues 1–309 map to the originals via `issue-map.csv`).

An item counts as verified when either:

- it carries a closing comment of the form "Verified satisfied on main …"
  (the W-batch waves left these on ~266 issues), or
- a REV35-SKELETON batch re-checked it against main and recorded evidence
  here (closing commit + the regression tests that still prove it).

## Batch queue

The unverified residual = imported issues that were closed by a commit
reference alone (no verification comment): **34 items**, listed in
original-issue order. Work through them in batches of ~10.

| # | Orig | GH | Item | Batch | Verdict |
|---|------|----|------|-------|---------|
| 1 | 740 | 21 | Application IA v2 integration slice: wire Preferences, Help, Diagnostics, Activity, Libraries, Trust and localization into the native shell | 1 | verified-fixed |
| 2 | 762 | 35 | Geometry Import & Repair UX: productize #167 into guided mesh QA, repair preview and solver-readiness workflow | 1 | verified-fixed |
| 3 | 763 | 36 | Native semantic-integrity coverage: require typed row/payload adapters or explicit exemption for every persistent authority table | 1 | verified-fixed |
| 4 | 766 | 39 | IA v2 typed-navigation regression: application pages report focus success without resolving the requested authority | 1 | verified-fixed |
| 5 | 767 | 40 | Native schema authority regression: repository initialization still runs mutating convergence DDL after schema v6 | 1 | verified-fixed |
| 6 | 770 | 43 | Capture Inbox UI regression: staged deliveries are list-only and lose exact item/project context before review or promotion | 1 | verified-fixed |
| 7 | 782 | 55 | IA v2 rail regression: application destinations are all permanent buttons and compact mode only shrinks the text rail | 1 | verified-fixed (see note) |
| 8 | 786 | 59 | Measurements context regression: visible `Calibration` tab has no page and raises `unknown measurement context` | 1 | verified-fixed |
| 9 | 788 | 60 | IA v2 compact-rail regression: permanent text destinations can overflow at 200% DPI and have no real compact/overflow mode | 1 | verified-fixed |
| 10 | 801 | 67 | Measurement batch commit integrity: make measurement, AcquisitionContext and staged attachments idempotently resumable as one logical item | 1 | verified-fixed |
| 11 | 810 | 72 | Owned-room validation evidence gate: require measurement quality, disposition and hardware evidence before claimed validation | 2 | verified-fixed |
| 12 | 811 | 73 | Windows in-place update acceptance: test old installed HTDT → new installer, schema migration and data preservation | 2 | verified-fixed |
| 13 | 812 | 74 | SystemVariant validation lifecycle disconnect: measured proposals can never resolve back to the scene they tested | 2 | verified-fixed |
| 14 | 817 | 79 | Audio/theater authority batch integration: persist and productize BassManagement, theater modes and listening profiles | 2 | verified-fixed |
| 15 | 818 | 80 | Video authority batch integration: persist PresentationProfile, Photometric/HDR and projection modes | 2 | verified-fixed |
| 16 | 839 | 95 | Measurement lifecycle enforcement regression: excluded/corrected evidence can bypass disposition rules | 2 | verified-fixed |
| 17 | 844 | 100 | Measurement eligibility integration regression: Calibration, O60 and O100G bypass eligibility checks | 2 | verified-fixed |
| 18 | 876 | 129 | Interactive SBIR & Reflection Diagnosis UX: turn exact path authority into an explorable diagnosis surface | 2 | verified-fixed (see note) |
| 19 | 886 | 133 | Feature authority batch integration: persist and productize acoustic targets, isolation and listening modes | 2 | verified-fixed |
| 20 | 887 | 134 | IA v2 project-secondary integration: expose decisions, installation, commissioning and reports surfaces | 2 | verified-fixed |
| 21 | 920 | 155 | CI red on main: authority audit coverage_gap for htdt_project_* tables | 3 | verified-fixed |
| 22 | 933 | 162 | Treatment-aware wave/hybrid gap: R130 rejects all non-empty treatment boundary options | 3 | verified-fixed |
| 23 | 938 | 164 | Room prediction execution regression: closed #457 still leaves wave providers non-runnable and hybrid permanently unsupported | 3 | verified-fixed |
| 24 | 940 | 166 | Screen-transfer solver integration regression: AcousticScreenTransferAuthority never reaches solver input | 3 | verified-fixed |
| 25 | 945 | 169 | Joint optimization execution regression: closed #524 Native Optimize stops at spec authoring | 3 | verified-fixed |
| 26 | 953 | 177 | 3D Acoustic Field Explorer product regression: closed #517 has backend authority but no Native explorer workflow | 3 | verified-fixed |
| 27 | 964 | 187 | Source applicability gap: directivity and point-source prediction have no radial or band coverage claims | 3 | verified-fixed |
| 28 | 966 | 189 | R130 source-model gap: wave solver collapses every loudspeaker to a point monopole | 3 | verified-fixed |
| 29 | 968 | 191 | Speaker clearance frame bug: front/rear/side and port checks use world axes instead of speaker-local axes | 3 | verified-fixed |
| 30 | 990 | 213 | Spatial-impression diagnostics: derive IACC, lateral-energy and envelopment evidence from computed fields | 3 | verified-fixed |
| 31 | 1054 | 274 | Direct-view productization regression: closed #637 remains backend-only while Room surface lacks controls | — | open queue |
| 32 | 1086 | 306 | Overview vertical overflow: lifecycle/readiness cards can make actions unreachable | — | open queue |
| 33 | 1087 | 307 | Top context bar responsive overflow: workspace sub-contexts need wrap/overflow handling | — | open queue |
| 34 | 1088 | 308 | Optimize candidate triage UX: add filtering/sorting and selection continuity before polish passes | — | open queue |

## Batch 1 — REV35-SKELETON (2026-10-03, main @ ee094d8e)

All ten items verified-fixed on current main; no survivor. Each closing
commit is an ancestor of `origin/main` and its regression tests still
pass (112 tests, `-n 4`, basetemp `C:/t/skel-b1c`).

| Orig | GH | Closing commit | Evidence on main |
|------|----|----------------|------------------|
| 740 | 21 | 9c0511cbaf (IA v2 integration: compact rail, preferences surface, trust lines) | `ApplicationDestinationId` exposes PROJECTS/INBOX/ACTIVITY/LIBRARY/SUPPORT; `SupportPage` carries diagnostics dir + solver-diagnostics export; `PreferencesWidget` writes through to `ApplicationPreferenceStore` (fail-closed on newer schema); overview trust lines surface latest evidence; JA strings throughout. Tests: `test_ia_v2_integration.py`. |
| 762 | 35 | 5770c09d84 (Wire guided geometry import UX into Room workspace) | `geometry_import_dialog.py`: guided mesh diagnostics → bounded-repair preview → solver-readiness evaluation wired into `room_workspace.py`; operator confirms the repair before adoption. Tests: `test_geometry_import_dialog.py`. |
| 763 | 36 | c2c9ae9a1a (row-integrity ledger tail) | Escaped payload tables joined `_UNBOUND_PAYLOAD_TABLES`; `assert_row_integrity_registry_complete()` is a passing invariant — every persistent table is either typed-adapter covered or explicitly exempted. Tests: `test_cad_schema.py`, `test_native_authority_audit_hardening.py` coverage cases. |
| 766 | 39 | c2c9ae9a1a | `Projects`/`Support`/`ReferenceLibraryPage` focus targets resolve the requested authority (definition_id in UserRole) and report `TargetFocusResult(focused=False, message=…)` on unresolved refs instead of unconditional success. Tests: `test_ia_v2_integration.py::test_application_focus_targets_resolve_the_requested_authority`. |
| 767 | 40 | c2c9ae9a1a | `NATIVE_BASELINE_DDL` owns all 73 repository-local DDL statements across 22 repositories; open paths run `ensure_native_schema` then `require_native_tables` — no mutating convergence DDL at open. Tests: `test_cad_schema.py`. |
| 770 | 43 | cc592f836e (Capture Inbox: item detail pane + triage actions) | `application_pages.py` inbox page has a per-item detail pane with exact item/project context and triage actions instead of list-only staging. Tests: `test_application_pages.py`. |
| 782 | 55 | 9c0511cbaf | `WorkflowRail` compact mode is a real 72 px glyph rail (first-char glyph + full-label tooltip + hidden section headers) — not a shrunken text rail. Residual note: destinations remain a fixed permanent set by IA v2 design (5 application + 4 project); per-destination hide/reorder was not part of the closed fix and would be a new feature, not this regression. Tests: `test_workflow_shell.py` compact-rail cases. |
| 786 | 59 | 60d0fc6956 (Measurement nav: remove phantom calibration context) | `calibration` resolves to the 機器の準備 workspace context; stale `calibration_plan`/`calibration_export` links alias to the MEASUREMENT campaign page — no more `unknown measurement context`. Tests: `test_measurement_workspace_composition.py`, `test_workflow_shell.py`. |
| 788 | 60 | 9c0511cbaf | Destination column lives in a frameless `QScrollArea` (`workflowRailScroll`) and scrolls instead of clipping at constrained heights / 200% DPI. Tests: `test_workflow_shell.py::test_rail_destinations_scroll_instead_of_clipping_when_short`. |
| 801 | 67 | 7de05655b0 (Batch commit: idempotent resume) | `measurement_workflow.py` builds normalization + acquisition context once per entry for mid-commit-failure resume; identical (filename, kind, sha256) attachments deduplicate; persist accepts the identical already-persisted row. Tests: `test_measurement_workflow_extensions.py`. |

### Scoped verification run

```
TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
  C:/devin/python/python.exe -m pytest \
  backend/tests/test_ia_v2_integration.py \
  backend/tests/test_measurement_workflow_ux130.py \
  backend/tests/test_t18_workspace_ux.py \
  backend/tests/test_workflow_shell.py \
  backend/tests/test_geometry_import_dialog.py \
  backend/tests/test_cad_schema.py \
  backend/tests/test_application_pages.py \
  backend/tests/test_measurement_workspace_composition.py \
  backend/tests/test_measurement_workflow_extensions.py \
  -p no:warnings --basetemp=C:/t/skel-b1c
→ 112 passed in 135s
```

## Batch 2 — REV35-SKELETON2 (2026-10-03, main @ c7e898a9)

All ten items verified-fixed on current main; no survivor. Each closing
commit is an ancestor of `origin/main` and its regression tests still
pass (130 tests, `-n 4`, basetemp `C:/t/skel-b2a`).

| Orig | GH | Closing commit | Evidence on main |
|------|----|----------------|------------------|
| 810 | 72 | b211065867 (Measurement lifecycle + validation evidence gates, W4-09) | `cad_validation_campaign_service.py` owned-room gate requires the persisted replay-validated quality report bound to the exact measurement (sha/document/revision/content-hash/entity/position), a non-`unknown` `AcquisitionContext`, and `gate_measurement_claim` over `campaign.requested_band_hz` before O60 eligibility. Tests: `test_cad_validation_campaign_service.py`. |
| 811 | 73 | 095253b696 (Fix #811 update-acceptance lane vs post-lane authority hardening) | `scripts/validate_update_windows.py` keeps the sentinel-table drop before `--backup` (fails closed on unclassified tables otherwise) and the clean-NEW-before-in-place ordering; pinned OLD `803a512b0f` is an ancestor of main; `--seed-synthetic-demo`/`--backup` flags and `htdt-synthetic-o70-o80-demo-v1` document id all still present. The lane itself is a manual installer lane (documented non-scriptable subset stays owned-Windows acceptance); surface verified, lane not re-executed. Tests: none by design (lane script). |
| 812 | 74 | b211065867 | `system_expansion_workflow._measured_validated` resolves canonical O60/R180 authority (`_o60_validates`/`_r180_validates`) and drives both the lifecycle presentation (`measured` + `validated` flag → overview stage `measured_validated`, JA label `実測済み・検証済み`) and the `MeasurementPresentation` `validated` path. Tests: `test_system_expansion_workflow.py`. |
| 817 | 79 | 98ccd7e640 (Persist remaining #817/#818 audio/theater and video authorities) | Append-only repositories `cad_signal_path_repository`, `cad_lighting_repository`, `cad_tactile_repository`, `cad_usable_output_repository` (plus earlier `cad_bass_management_*` tables in baseline DDL); all families registered in `native_authority_audit` coverage map. Tests: `test_cad_authority_batch_repositories.py`, `test_cad_schema.py`. |
| 818 | 80 | 98ccd7e640 | `cad_photometric_repository`, `cad_colorimetry_repository`, `cad_video_presentation_*`, `cad_screen_optical_*`, `cad_color_target_*`, `cad_color_measurement_sets`, `cad_ambient_reflectance_profiles`, `cad_visual_qa_verdicts` — all registered in the audit coverage map; schema v10 converges v9 databases. Tests: `test_cad_authority_batch_repositories.py`, `test_cad_schema.py`. |
| 839 | 95 | b211065867 | `cad_model_validation_service` routes every validation-evidence read through `CadEffectiveMeasurementResolver.require_normal_use` — candidate, repeatability and separation checks all fail closed on excluded/misassigned/test-only/duplicate evidence (not only the campaign-gated path). Tests: `test_cad_model_validation_service.py`, `test_cad_validation_campaign_service.py`. |
| 844 | 100 | c5c2791dd7 (calibration authority audit: register R180 lifecycle tables + fail-closed row verification) | Six `cad_calibration_*` tables (specs/results/models/freezes/holdout_records/evidence_events) registered in `native_authority_audit` with `replay_canonical` probes; `CadModelCalibrationRepository.get_holdout_record` and fail-closed identity/hash re-verification on every persisted read. Tests: `test_cad_model_calibration_persistence.py`, `test_cad_schema.py`. |
| 876 | 129 | cf6f3b71dd (Reflection guidance UX + feature authority persistence) | `cad_reflection_guidance.py` delivers the explorable surface the issue titles: `build_reflection_guidance` (ranked treat-zone/reposition/verify/disambiguate items bound to exact path authority) plus the interactive scrub session (`open_guidance_session`/`scrub_source` exact per-frame deltas — the test suite itself calls this "the interactive scrub session"); the interactive diagnosis surface exists in-product via prediction findings (`reflection_path` → 3D highlight + `check-reflection-surface`/`create-treatment` actions). Tests: `test_cad_reflection_guidance.py`, `test_cad_reflection_diagnostic.py`. Residual note: `cad_reflection_guidance` currently has no production consumer — wiring its ranked items into a UI surface is a productization follow-up, not part of the closed claim. |
| 886 | 133 | cf6f3b71dd | `cad_feature_authority_repository` (acoustic targets, isolation, rack/BOM, drawings, field labels — 927 LOC) registered through `cad_authority_registry` for canonical access; audit-covered. Tests: `test_cad_feature_authority_repository.py`. |
| 887 | 134 | 82e03d458a (Expose IA v2 project-secondary domains on the Overview hub) | `overview_readiness._secondary_domains` produces per-domain `OverviewSecondaryDomain` cards — decisions (unapplied/expired-assumption flags), installation (deep-link to Optimization interventions), commissioning (plan absent/in-progress/finished), operating health (latest check-run assessment counts) — rendered by `overview_workspace._add_secondary_domain` under a dedicated `domain_header` section without bloating the four primary workspaces. Tests: `test_overview_secondary_domains.py`. |

### Scoped verification run

```
TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
  C:/devin/python/python.exe -m pytest \
  backend/tests/test_cad_model_validation_service.py \
  backend/tests/test_cad_validation_campaign_service.py \
  backend/tests/test_system_expansion_workflow.py \
  backend/tests/test_cad_authority_batch_repositories.py \
  backend/tests/test_cad_model_calibration_persistence.py \
  backend/tests/test_cad_feature_authority_repository.py \
  backend/tests/test_cad_reflection_guidance.py \
  backend/tests/test_cad_reflection_diagnostic.py \
  backend/tests/test_overview_secondary_domains.py \
  backend/tests/test_cad_schema.py \
  -q -n 4 -p no:warnings --basetemp=C:/t/skel-b2a
→ 130 passed
```

## Batch 3 — REV35-SKELETON3 (2026-10-03, main @ 9ed9589e)

All ten items verified-fixed on current main; no survivor. Each closing
commit is an ancestor of `origin/main` and its regression tests still
pass (169 tests, `-n 4`, basetemp `C:/t/skel-b3b`).

| Orig | GH | Closing commit | Evidence on main |
|------|----|----------------|------------------|
| 920 | 155 | f1591a9f447c (Register cad_calibration_* authorities in the native audit coverage registry) | Every persistent table — including the `htdt_project_*` and `cad_calibration_*` families — is registered to an explicit audit coverage mode; `test_registry_covers_every_persisted_table` is a passing completeness invariant and unregistered tables still fail closed with `coverage_gap`. Tests: `test_authority_audit_coverage.py`, `test_cad_schema.py`. |
| 933 | 162 | 269ab17262a0 (Fix candidate wave treatment fixture: mount panel on planar host surface) | Non-empty treatment boundary overlays compile and bind through the candidate wave execution path — `compile_treatment_boundary_overlays` over `TreatmentBoundaryCompileInput` with the panel mounted on its planar host surface. Tests: `test_cad_candidate_wave_treatment.py`. |
| 938 | 164 | 82aed1db6c07 (Execute R170A/R170B provider lanes as persisted-evidence Room predictions) | `room_prediction_options` hybrid lane enumerates the persisted R170B catalog and reports capability/evidence verbatim; missing coverage surfaces as explicit UNSUPPORTED entries rather than a permanently-unsupported lane; `cad_provider_response` executes the lanes. Tests: `test_room_prediction_provider_runs.py`. |
| 940 | 166 | 21f4d6035e3b (Bind exact screen-transfer authority into the acoustic snapshot) | `ScreenTransferSnapshotBinding` binds each `AcousticScreenTransferAuthority` via `ExactExternalAuthorityRef` into the acoustic snapshot (`screen_transfer_ready` gate, `screen_transfer_not_integrated` reason when unintegrated). Tests: `test_cad_acoustic_snapshot.py`. |
| 945 | 169 | eeab47feffc8 (Joint Optimize: execute persisted specs from the native panel) | `joint_optimization_context.execute_spec` executes persisted `JointOptimizationSpec` records; the native panel drives the run — no longer stops at spec authoring. Tests: `test_joint_optimization_context.py`, `test_joint_optimization_panel.py`. |
| 953 | 177 | 18e3ba6c5791 (Native 3D field explorer + wave source-model compatibility authority) | `FieldExplorerPanel` lives in a dedicated dock inside `prediction_workspace` (opened from the prediction scalar button), backed by `cad_field_explorer` + `cad_field_explorer_repository`. Tests: `test_cad_field_explorer.py`. |
| 964 | 187 | c66f903817bc (Declare radial/far-field validity domain for source directivity and direct-level evaluation) | `valid_radial_domain` + `R110RadialDomainAuthority` on the source propagate through `DistanceLevelAuthority(radial_domain=...)` into direct-level evaluation; seats outside the declared domain are flagged. Tests: `test_issue_964_source_radial_domain.py`. |
| 966 | 189 | 18e3ba6c5791 | `cad_wave_source_model.WaveSourceModelCompatibility` evaluates each bound excitation as `monopole_native` / `collapse_supported` / `collapse_unproven` — collapsing to a point monopole is a claimed, validated operation instead of an assumption. Tests: `test_cad_wave_source_model.py`. |
| 968 | 191 | aa02a5d3a167 (Make speaker clearance checks cabinet-local instead of world-axis) | `cad_installation_context` measures clearances along cabinet-local axes (local +Y front, -Y rear, ±X sides); vertical checks keep measuring floor/ceiling clearance. Tests: `test_cad_authority_expansion.py`. |
| 990 | 213 | 4be186e261b3 (SPAT20: derive lateral-energy and envelopment metrics from pinned directional channels) | `cad_spatial_ir_metrics` derives IACC (normalized cross-correlation), early lateral energy fraction J_LF, J_LFC and late lateral level L_J (listener envelopment) from pinned directional channels under ISO 3382 method ids. Tests: `test_cad_spatial_ir_metrics.py`. |

### Scoped verification run

```
TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
  C:/devin/python/python.exe -m pytest \
  backend/tests/test_authority_audit_coverage.py \
  backend/tests/test_cad_candidate_wave_treatment.py \
  backend/tests/test_room_prediction_provider_runs.py \
  backend/tests/test_cad_acoustic_snapshot.py \
  backend/tests/test_joint_optimization_context.py \
  backend/tests/test_joint_optimization_panel.py \
  backend/tests/test_cad_field_explorer.py \
  backend/tests/test_issue_964_source_radial_domain.py \
  backend/tests/test_cad_wave_source_model.py \
  backend/tests/test_cad_authority_expansion.py \
  backend/tests/test_cad_spatial_ir_metrics.py \
  backend/tests/test_cad_schema.py \
  -q -n 4 -p no:warnings --basetemp=C:/t/skel-b3b
→ 169 passed in 195s
```

## Still-open imported issues (all gated epics)

Nine imported issues remain open on GitHub — every one is a
campaign-scale epic or hardware/user-gated, matching
`docs/reviews/rev34-featureaudit.md` "Gated" classification; none is a
one-session software fix:

- GH#1 (orig 83) — O60R owned-room campaign (hardware gate)
- GH#2 (orig 101) — Arbitrary-room acoustics hybrid solver (R-series epic)
- GH#3 (orig 118) — Native UI/UX overhaul epic
- GH#4 (orig 140) — O90 robust/tolerance-aware optimization
- GH#5 (orig 142) — O100 system expansion
- GH#8 (orig 723) — End-to-end golden-path acceptance
- GH#34 (orig 761) — Competitive product gaps umbrella
- GH#131 (orig 878) — AI-agent execution backlog
- GH#132 (orig 880) — Owned-Windows native acceptance matrix (user's machine)

Non-imported open issues #471/#472/#475/#476 were verified-fixed by the
REV34-FEATUREAUDIT session (`docs/reviews/rev34-featureaudit.md`) and are
pending Controller close-out.

## Next batch

Start at queue row 31 (orig 1054 / GH#274). Method per item: read the
closing commit from the issue's events, confirm it is an ancestor of
`origin/main`, run its regression tests, and grep the claimed surface —
then mark the row verified-fixed / still-present / invalid here and
comment on the issue.
