# R120B explicit polyhedral acoustic geometry implementation record

Date: 2026-09-20
Base authority: `main@a8491db973c6fcefc66e259903ca668936f63a2e`
Branch: `agent/r120b-polyhedral-geometry-20260920-1740`

## Bounded scope

This record tracks the solver-neutral R120B slice for explicit general-3D polyhedral acoustic geometry beyond a simple RoomPrism. The target authority includes planar polygon surfaces with arbitrary orientation, closed polyhedral air volumes, stepped floors/ceilings, sloped ceilings, deterministic triangulation where required, explicit per-surface material identity, topology diagnostics, and explicit bounded tessellation/error metadata for curved-source approximations.

The semantic path remains explicit:

`raw/imported geometry -> semantic acoustic geometry -> topology validation -> compiled representation`

Visual mesh data is not promoted directly to solver-ready geometry.

## Required diagnostics

The implementation is fail-closed for invalid closed volume, inconsistent orientation, duplicate or zero-area faces, detectable self-intersection, non-manifold edges, invalid holes, region overlap, missing material references, and invalid Portal/surface relationships. No geometry-changing repair is authorized unless an existing explicit bounded repair authority already applies.

## Solver readiness

Wave and geometric-acoustics readiness are evaluated independently. GA support never implies wave support.

## Exclusions

This task does not modify HTDT-Capture, the R130 numerical executor, R150 portal/adapter propagation code, `docs/IMPLEMENTATION_STATUS.md`, or `docs/IMPLEMENTATION_ROADMAP.md`. It does not claim production wave numerical validation or owned-room evidence.

## Verification

Implementation, focused invariant tests, save/reopen identity checks, prism regression, and relevant GitHub Actions results will be recorded here before completion.
