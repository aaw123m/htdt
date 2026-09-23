# Issue #118 UX160 — owned-Windows first-use / visual acceptance

> Status: **BLOCKED — UX160 is not accepted**

この文書はUX160のcanonical evidenceです。確認していない項目はPASSにしていません。HTDT-Captureは変更していません。

## Scope and identity

- Repository: bolph71656-ai/Home-Theater-Digital-Twin
- Base main at start: c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96
- Tested code commit: ff8431cc2e32c1da07fbffacff7c2e0c8c5bbd2e
- Acceptance branch: issue-118-ux160-owned-windows-acceptance
- Workflow shell default promotion: **NOT PERFORMED**
- HTDT-Capture: **NOT CHANGED**
- RDC: 1 preparation/build session before the user redirected the work to local Windows; RDC acceptance sessions: 0

## Windows environment

- Windows Product: Windows 10 Pro, build 26200, x64
- Physical display: 2880×1800
- Observed logical UI at current display scaling: 1440×900, 200%
- GPU: AMD Radeon 780M Graphics, driver 32.0.13032.11
- RAM: 33,618,251,776 bytes reported by Windows
- Python: 3.12.10
- Qt/PySide6: 6.11.2
- PyVista: 0.49.0
- VTK: 9.7.0
- Renderer: VTK/PyVista Qt OpenGL path; renderer string was not separately captured

## Fixture and launch

- Room fixture: an L-shaped room drawn with six real viewport clicks and saved through Ctrl+S
- System fixture: existing 3.0.2, shown as FL / C / FR
- REW fixture: backend/tests/fixtures/basic_rew.txt
- Launch command: .venv\Scripts\python.exe -m htdt.native_cad --workflow-shell --data-dir <isolated data dir> --document-id fixture-f1
- Evidence images: docs/evidence/ux160/2026-09-20/

## Preflight and automated verification

| Check | Result | Evidence |
|---|---|---|
| Latest main resolved | PASS | c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96 |
| Normal CI on latest main | PASS | [run 35516523459](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35516523459) |
| PR normal CI on tested code/evidence | PASS | [run 35521488433](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35521488433) |
| Focused UX/workspace tests | PASS | 58 passed before fixes; post-fix focused suites passed (33, 20, 19, 15 as applicable) |
| Full backend tests | PASS | 1093 passed, 1 skipped, 4 warnings |
| CLI help / Python compile / PowerShell parse / O60R preflight | PASS | local Windows preflight 2026-09-21 |
| Native package build | PASS | .tmp/ux160-package-20260921/HTDT/HTDT.exe created |
| Packaged executable smoke | BLOCKED | packaged process shows “DLL load failed while importing QtGui: 指定されたプロシージャが見つかりません。” |
| Installer/install-uninstall smoke | PASS | included in Windows Release Artifact run 35521488422 |
| GitHub Windows Release Artifact on tested commit | PASS | [run 35521488422](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35521488422) |

## Acceptance matrix

| Gate | Result | Evidence / reason |
|---|---|---|
| UX-A01 Overview discoverability | PASS | Overview exposed 部屋を作成 and actionable state; representative image linked below |
| UX-A02 Ctrl+K command reachability | NOT_TESTED | Full command-palette tour was not completed |
| UX-A03 Room workspace / Inspector | BLOCKED | Room viewport and L-shaped editing observed; full selection/Inspector contract not completed |
| UX-A04 Measurements flow | BLOCKED | REW import → assignment → quality completed; comparison correctly blocked because no prediction was saved |
| UX-A05 Optimize flow | BLOCKED | page structure observed; candidate generation disabled with reason, no valid search spec/candidate available |
| UX-A06 visual/DPI layout | BLOCKED | 1440×900 logical at 200% verified; 100% and 150% matrix runs were not completed |
| UX-A07 state consistency / stale fence | NOT_TESTED | partial Room → Measurements → Optimize → Overview navigation only |
| UX-A08 L字室 → 3.0.2 → REW → comparison → candidate | BLOCKED | rectangular prediction model rejects the L-shaped room; no solver/backend change allowed in UX160 |
| UX-A09 Room visual readability/focus modes | BLOCKED | base geometry readability observed; all focus modes and selection contrast not completed |
| UX-A10 interaction feedback/focus/motion | NOT_TESTED | full hover/press/drag/focus sequence not completed |
| UX-A11 scientific visualization | BLOCKED | FR plot readability observed; heatmap/waterfall coverage not completed |
| UX-A12 CAD gestures/shortcuts | BLOCKED | RMB context and Ctrl+S observed; full M/R/F/Home/Esc/Enter and mouse matrix not completed |
| UX-A13 Japanese-first UI | BLOCKED | inspected screens were corrected; full Overview/Room/Measurements/Optimize/Settings tour not completed |
| UX-A14 shortcut-free CAD discoverability | BLOCKED | visible CAD hint and context menu verified; full pan/orbit/zoom execution not completed |
| UX-A15 O90D workflow regression | NOT_TESTED | separate robustness evidence gate |
| UX-A16 O100G workflow regression | BLOCKED | current FL/C/FR and proposal form observed; equipment definition was unavailable, so proposal/candidate/apply was not run |

## DPI matrix

- 1440×900 logical at current 200% display scaling: **observed** for Overview, Room, Measurements, Optimize
- 1280×800 @100%: NOT_TESTED
- 1440×900 @100%: NOT_TESTED
- 1440×900 @150%: NOT_TESTED

## Visual and first-use findings

- Overview presented next actions and did not require internal IDs.
- Room initially lacked a visible pan/orbit/zoom/context hint. The fix added a Japanese hint that remains visible in compact layout.
- O100G placement panel initially expanded horizontally at 200% and clipped text. The fix forces the panel and tree columns to fit the viewport and leaves only vertical scrolling.
- O100G normal copy exposed internal names and English terms. The fix moved raw details behind existing advanced/provenance surfaces and localized the visible copy.
- Measurements quality showed explicit measured/phase capability states. Comparison showed a clear reason when prediction data was absent.
- Optimize candidate generation was disabled without a reason. The fix displays the required setup/refresh reason beside the disabled action.
- An unsupported L-shaped prediction was previously treated as current by Overview. The fix keeps it out of optimization readiness and links to room-shape review.

Representative evidence:

- [Overview with unsupported prediction and actionable reason](evidence/ux160/2026-09-20/ux160-overview-unsupported-prediction-2880x1800-200.png)
- [Saved L-shaped Room](evidence/ux160/2026-09-20/ux160-room-lshape-saved-2880x1800-200.png)
- [Room CAD navigation hint](evidence/ux160/2026-09-20/ux160-room-navigation-hint-2880x1800-200.png)
- [Measurements quality](evidence/ux160/2026-09-20/ux160-measurement-quality-2880x1800-200.png)
- [Measurements comparison blocked with reason](evidence/ux160/2026-09-20/ux160-measurement-comparison-blocked-2880x1800-200.png)
- [Optimize disabled action with reason](evidence/ux160/2026-09-20/ux160-optimize-candidate-disabled-reason-2880x1800-200.png)
- [O100G panel before width/copy fix](evidence/ux160/2026-09-20/ux160-room-302-placement-2880x1800-200.png)
- [O100G panel after width/copy fix](evidence/ux160/2026-09-20/ux160-room-302-placement-final-japanese-2880x1800-200.png)

## Discovered defects and applied fixes

1. Room CAD gestures were not discoverable without prior shortcut knowledge. Added visible Japanese pan/orbit/zoom/RMB hint and a focused compact-layout test.
2. O100G Room panel overflowed horizontally at 200% DPI. Made the panel/tree columns shrink to the viewport, disabled horizontal scrolling, and kept vertical scrolling.
3. O100G normal copy exposed O100 authority, Scene truth, placement candidate, and English field labels. Localized the visible copy; raw provenance remains advanced.
4. Unsupported prediction for the current room incorrectly unlocked optimization readiness. Overview now reports the unsupported geometry and links to Room review.
5. Optimize candidate generation had no visible disabled reason. Added a reason label for missing/stale search setup.
6. Measurements screens exposed unknown channel roles and repository/authority implementation names. Added Japanese channel-role labels and user-facing comparison copy.

## Retest

- Full backend suite after all fixes: **1093 passed, 1 skipped**
- Focused Room/Optimize/Overview/Measurements/Standards suites: **PASS**
- Local Windows 200% visual retest: navigation hint, O100G panel width/copy, Overview unsupported-prediction fence, and Optimize disabled reason verified
- Packaged executable: **BLOCKED** by QtGui DLL load failure described above

## Unresolved concerns

- UX160 cannot be marked PASS because the full DPI matrix, command palette, all mouse/focus/VTK gestures, repeated stale-fence navigation, O90D, and O100G apply path were not completed.
- L-shaped prediction remains unsupported by the current rectangular geometry model. This is outside UX160 and no solver/backend implementation was added.
- The locally built package does not pass executable startup smoke on this Windows host; installer validation is therefore not claimed.
- Workflow shell remains opt-in via --workflow-shell. Default launcher promotion was deliberately not performed.

## Final decision

**UX160: BLOCKED / NOT ACCEPTED**

Default launcher promotion: **NOT PERFORMED**  
Final merge SHA: **PENDING**  
Issue #118 close: **NOT PERFORMED**
