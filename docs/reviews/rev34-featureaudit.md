# REV34-FEATUREAUDIT — implementable feature gaps + ranked candidates

Scope: re-audit of `docs/IMPLEMENTATION_ROADMAP.md`, `docs/IMPLEMENTATION_STATUS.md`,
open GitHub issues on ka0923s-a11y/HTDT, `docs/imported-issues/` skeleton issues,
and `git log` TODO markers — after REV26 (#453) had classified the roadmap
remainder as gated. Method: every unfinished item was classified as
**implemented**, **already-fixed**, **gated** (hardware / external resource /
product decision / campaign-scale), or **implementable now**.

## Implemented this session

| Item | What shipped | Verification |
|---|---|---|
| #475 + #476 — RoomPlan lineage byte counts | `raw_byte_count` / `processed_byte_count` in `roomplan/captured-room-metadata.json` were validated only as nonnegative. Supplied counts now must equal the selected manifest payload's declared `bytes`; a processed count without a selected processed payload rejects; emitters omitting the optional counts stay accepted. `capture_reference.py::_load_roomplan_capture_metadata` | 4 new regressions on the Swift-authored fixture (raw mismatch, processed mismatch, absent counts accepted, dangling processed count rejected) + full capture suite green |

## Already fixed on main (documented, no change forced)

| Item | Evidence |
|---|---|
| #471 — TLS credential bounds before SSL parsing | `capture_receiver.py::_ensure_certificate` bounds both credential files via `read_file_bounded` before `load_cert_chain`; torn-pair regeneration preserved (merge `ed9c9f69`) |
| #472 — venv subprocess lock test | `test_runtime_instance.py` passes the environment's `purelib`/`platlib` to the `_base_executable` child via `site.addsitedir`; real lock-holder termination and OS-release coverage intact (merge `ed9c9f69`) |

Both issues remain open on GitHub pending their Controller's close-out; the
code-level fixes they describe are verified present.

## Ranked feature-candidate list

Effort is in sessions (≈ one Devin session each). "Gate" is what blocks
merging/claiming, not what blocks starting.

| Rank | Candidate | Effort | Value | Gate |
|---|---|---|---|---|
| 1 | R150 next bounded capability slice — pick one: arbitrary-order (bounded) specular reflection chains, stochastic-ray receiver estimator with seed/convergence evidence, or fuller late-decay contributions. Each historical slice was one PR (#292/#430/#432 pattern). | 1 | Advances arbitrary-room acoustics toward R160 union coverage; every slice is exact-authority and testable. | None for a bounded slice; production validation stays a separate claim. |
| 2 | R130D next predeclared diagnostic — spatial/voxel-staircase representation, mode/bin sensitivity, or source/receiver discretization, separated as hash-bound diagnostics (PR #295 pattern). | 1 | Progresses `SELF_CONVERGENCE_FAILED` → cross-solver unblocking; pure software instrumentation. | Diagnostic only; canonical contract unchanged. |
| 3 | Imported-skeleton residual verification — ~250 regression issues imported 2026-09-24; REV waves consumed most. Verify in batches of ~10 against main; each survivor is its own small-medium fix. | 1 per batch | Correctness hardening; honest close-out of stale trackers. | None. |
| 4 | UX140 legacy `QMainWindow` adapter removal — retire the `--legacy-ui` inheritance path once workflow mounts are sole owners. | 1–2 | Kills the dual-path maintenance hazard that already shipped bugs (REV33 phantom edits). | Coordinate with parallel UX lanes touching the same files. |
| 5 | 旧 wall/opening・高度 geometry editing 完全移植 — remaining advanced wall/opening edit parity between legacy and workflow paths. | 2–3 | Completes Room workspace feature parity. | Needs scoping pass to enumerate what is actually unported. |
| 6 | Standards: wire O100D SPL/headroom objectives into criteria evaluation. | 1 | First non-UNKNOWN standards verdicts beyond layout-derived criteria. | SPL/headroom input is not layout-derived — partially gated on prediction/measurement authority existing for the scene. |
| 7 | R160 evidence-driven automatic crossover for unequal grids. | 1–2 | Removes the manual opt-in `cartesian_linear_v1` boundary. | Bounded composition authority exists; production claim stays separate. |

## Too large for one session — decompositions (what ships first)

- **R150 → production path**: (a) arbitrary-order bounded reflections slice → (b) stochastic estimator + convergence evidence → (c) full late-decay model → (d) production validation campaign. Ship (a) first; each lands independently.
- **R100B candidate-wide adoption**: (a) pin the missing hard gates + external benchmark fixture list → (b) geometry benchmark evidence (L-room, obstacle) → (c) external measured benchmark (e.g. BRAS) per observable → (d) adoption ADR. Only (a) is software; (b)–(d) are campaign-scale.
- **UX140 legacy removal**: (a) inventory every `--legacy-ui`-only surface and add workflow-path tests for each → (b) migrate stragglers → (c) delete the legacy window + adapter. (a) ships first as tests-only.
- **Issue #8 golden-path acceptance**: decomposes into per-workspace scriptable acceptance cases; depends on UX160's real-machine gate.

## Gated (not startable as software)

| Item | Gate |
|---|---|
| R140 real GPU executor + CPU/GPU equivalence evidence | No GPU on any runner; `gpu_validation_state=NOT_VALIDATED`. |
| R180A/B, O60R (#1/#83), O90E production gate, #773 corpus | Owned-room hardware measurement campaigns. |
| UX160 owned-Windows visual acceptance | Runs on the user's owned Windows machine (DPI matrix, gestures, first-use), not a VM. |
| DTS:X built-in criteria | No explicit public criteria available to license-check. |
| Auro horizontal-azimuth source sign inconsistency | Product decision: correcting a published table silently is disallowed; needs user call. |
| #8/#131/#132 acceptance epics | Multi-session campaigns composed of the above. |
| #34 competitive-gap umbrella | Sub-items map to the R-series/UX items above. |
