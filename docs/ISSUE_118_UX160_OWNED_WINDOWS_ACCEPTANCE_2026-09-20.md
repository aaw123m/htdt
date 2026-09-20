# Issue #118 UX160 — owned-Windows first-use / visual acceptance

> Status: **IN_PROGRESS**  
> This document is the canonical evidence record for the UX160 gate. Items are not marked PASS until they are observed on the owned Windows machine.

## Scope and guardrails

- Repository: `bolph71656-ai/Home-Theater-Digital-Twin`
- HTDT-Capture: **not changed**
- Base main at start: `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96`
- Tested commit: **PENDING**
- Acceptance branch: `issue-118-ux160-owned-windows-acceptance`
- Workflow-shell default promotion: **BLOCKED until UX160 PASS**
- R-series and unrelated O-series backend work: **out of scope**
- RDC code editing: **forbidden**

## Preflight

| Check | Result | Evidence |
|---|---|---|
| Latest main resolved | PASS | `c77b8b50d7e4f9706c08e64a1daa7a7a51e8ed96` |
| Normal CI on latest main | PASS | CI run [35516523459](https://github.com/bolph71656-ai/Home-Theater-Digital-Twin/actions/runs/35516523459) |
| Windows packaging/release for tested commit | NOT_TESTED | Run after tested commit is fixed |
| Installer/install-uninstall smoke | NOT_TESTED | Run after tested commit is fixed |
| Workflow shell tests | NOT_TESTED | Repository-native tests |
| Room / Measurements / Optimize / command registry / theme tests | NOT_TESTED | Repository-native tests |

## Test identity

- Tested commit: **PENDING**
- Build/artifact identity: **PENDING**
- Windows edition/build: **PENDING**
- Python / Qt / PyVista / VTK: **PENDING**
- CPU/GPU/driver/RAM: **PENDING**
- Renderer: **PENDING**
- RDC sessions: **0** (preflight only)

## Fixture and launch contract

- Primary fixture: **PENDING repository fixture selection**
- First-use path: L-shaped room → 3.0.2 → REW import → prediction comparison → candidate review
- Launch command: **PENDING artifact and data directory**
- Evidence naming: `ux160-<workspace>-<resolution>-<scale>-<sequence>.png`
- Required representative screens:
  - Overview
  - Room
  - Measurements
  - Optimize
- DPI matrix:
  - 1280×800 @100%
  - 1440×900 @100%
  - 1440×900 @150%
  - 1440×900 @200% (if feasible)

## Acceptance matrix

| Gate | Result | Notes / evidence |
|---|---|---|
| UX-A01 Overview discoverability | NOT_TESTED | |
| UX-A02 Ctrl+K command reachability | NOT_TESTED | |
| UX-A03 Room workspace / Inspector | NOT_TESTED | |
| UX-A04 Measurements flow | NOT_TESTED | |
| UX-A05 Optimize flow | NOT_TESTED | |
| UX-A06 visual/DPI layout | NOT_TESTED | |
| UX-A07 state consistency and stale fence | NOT_TESTED | |
| UX-A08 first-use L字室 → 3.0.2 → REW → comparison → candidate | NOT_TESTED | |
| UX-A09 Room visual readability/focus modes | NOT_TESTED | |
| UX-A10 interaction feedback/focus/motion | NOT_TESTED | |
| UX-A11 scientific visualization | NOT_TESTED | |
| UX-A12 CAD gestures/shortcuts | NOT_TESTED | |
| UX-A13 Japanese-first UI | NOT_TESTED | |
| UX-A14 shortcut-free CAD discoverability | NOT_TESTED | |
| UX-A15 O90D workflow regression | NOT_TESTED | |
| UX-A16 O100G workflow regression | NOT_TESTED | |

## Findings

### Visual findings

- PENDING

### First-use findings

- PENDING

### Discovered defects

- PENDING

### Applied fixes

- PENDING

### Retest

- PENDING

### Unresolved concerns

- UX160 is not yet accepted.
- Workflow shell must remain opt-in until the gate is complete.

## Final decision

**UX160: NOT_YET_DETERMINED**

Default launcher promotion: **NOT_PERFORMED**  
Final merge SHA: **PENDING**  
Issue #118 close: **NOT_PERFORMED**
