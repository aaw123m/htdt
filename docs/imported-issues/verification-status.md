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
| 11 | 810 | 72 | Owned-room validation evidence gate: require measurement quality, disposition and hardware evidence before claimed validation | — | open queue |
| 12 | 811 | 73 | Windows in-place update acceptance: test old installed HTDT → new installer, schema migration and data preservation | — | open queue |
| 13 | 812 | 74 | SystemVariant validation lifecycle disconnect: measured proposals can never resolve back to the scene they tested | — | open queue |
| 14 | 817 | 79 | Audio/theater authority batch integration: persist and productize BassManagement, theater modes and listening profiles | — | open queue |
| 15 | 818 | 80 | Video authority batch integration: persist PresentationProfile, Photometric/HDR and projection modes | — | open queue |
| 16 | 839 | 95 | Measurement lifecycle enforcement regression: excluded/corrected evidence can bypass disposition rules | — | open queue |
| 17 | 844 | 100 | Measurement eligibility integration regression: Calibration, O60 and O100G bypass eligibility checks | — | open queue |
| 18 | 876 | 129 | Interactive SBIR & Reflection Diagnosis UX: turn exact path authority into an explorable diagnosis surface | — | open queue |
| 19 | 886 | 133 | Feature authority batch integration: persist and productize acoustic targets, isolation and listening modes | — | open queue |
| 20 | 887 | 134 | IA v2 project-secondary integration: expose decisions, installation, commissioning and reports surfaces | — | open queue |
| 21 | 920 | 155 | CI red on main: authority audit coverage_gap for htdt_project_* tables | — | open queue |
| 22 | 933 | 162 | Treatment-aware wave/hybrid gap: R130 rejects all non-empty treatment boundary options | — | open queue |
| 23 | 938 | 164 | Room prediction execution regression: closed #457 still leaves wave providers non-runnable and hybrid permanently unsupported | — | open queue |
| 24 | 940 | 166 | Screen-transfer solver integration regression: AcousticScreenTransferAuthority never reaches solver input | — | open queue |
| 25 | 945 | 169 | Joint optimization execution regression: closed #524 Native Optimize stops at spec authoring | — | open queue |
| 26 | 953 | 177 | 3D Acoustic Field Explorer product regression: closed #517 has backend authority but no Native explorer workflow | — | open queue |
| 27 | 964 | 187 | Source applicability gap: directivity and point-source prediction have no radial or band coverage claims | — | open queue |
| 28 | 966 | 189 | R130 source-model gap: wave solver collapses every loudspeaker to a point monopole | — | open queue |
| 29 | 968 | 191 | Speaker clearance frame bug: front/rear/side and port checks use world axes instead of speaker-local axes | — | open queue |
| 30 | 990 | 213 | Spatial-impression diagnostics: derive IACC, lateral-energy and envelopment evidence from computed fields | — | open queue |
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

Start at queue row 11 (orig 810 / GH#72). Method per item: read the
closing commit from the issue's events, confirm it is an ancestor of
`origin/main`, run its regression tests, and grep the claimed surface —
then mark the row verified-fixed / still-present / invalid here and
comment on the issue.
