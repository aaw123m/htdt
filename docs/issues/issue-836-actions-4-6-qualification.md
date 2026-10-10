# Issue #836 Actions 4–6 — BRAS v3 qualification binding, evaluation, report

Actions 1–3 landed the corpus admission manifest, the GeneralFIR importer
(`cad_bras_v3_importer.py`) and the sealed measured-reference manifest
(`cad_bras_v3_metrics.py`). This doc covers the remaining gates: solver-side
predictions (Action 4), tolerance/comparison (Action 5) and report surfaces
(Action 6).

## Action 4 — solver-prediction binding: honest fail-closed

**Investigation verdict: no existing solver lane can emit observables for
an imported BRAS v3 scene.** The import yields `case.geometry = None` by
design — the corpus publishes room geometry descriptively (PDF/photos),
not as a sealed shell — and ships no per-source directivity or calibrated
source-strength authority:

- `geometric_r150` — `compile_deterministic_ga_execution_input` requires a
  sealed scene-geometry chain, a closed-shell room policy
  (`exact_axis_aligned_closed_shoebox_v1` /
  `general_planar_closed_polyhedral_v1` / portal multi-region), per-source
  `DirectivityDataset` authority, and the region/portal/boundary
  termination chain. All are absent.
- `wave_r130` — the R100A/B compiler lane is bound to closed-rigid-region
  *project* scenes; the corpus is an external benchmark with no pinned
  PFFDTD execution binding.
- `hybrid` — union of both blockers.

`bind_bras_v3_solver_lanes` therefore lands the honest record: a sealed
`BrasV3SolverBinding` (`bsb-…`) with `produced_observables = ()` — never
a fabricated prediction — and each lane's `missing_prerequisites`
enumerated verbatim plus a `capability_gap` statement. Downstream
non-arrival observable kinds are folded to `unsupported` verdicts citing
this binding, so the claim surface can never exceed what the lanes
actually emit.

The only prediction lane that emits without fabricating physics is the
existing `AnalyticDirectPathProvider` (`analytic-direct-path`): it
predicts `arrival_s = d/c` from the *measured* per-measurement
transducer positions. Against the real corpus this is an
import-consistency check, not solver qualification — RS4 onCenter
residuals are +0.10–0.16 ms, RS1 ±0.05–0.28 ms, a stable sub-ms
measurement-chain latency, and the report's claim ceiling says so.

## Importer fixes folded in

`read_sofa_generalfir` mis-read BRAS v3 transducer positions, which are
stored `(I|E|R, 3, M)` — coordinate axis before measurement axis, i.e.
the *columns* are positions. `_positions` now transposes that layout
(plain `(N, 3)` arrays stay row-major); with correct pairing the
arrival residuals collapsed from ±6–19 ms to sub-ms. Per-measurement
`EmitterID`/`ReceiverID` indices are now captured on
`GeneralFirMeasurement` as provenance.

## Action 5 — tolerance/comparison

`run_bras_v3_qualification` executes a frozen-config run mirroring the
fixture-runner pattern: the sealed `BrasV3QualificationConfig` (`bqc-…`)
pins member, measurement index set, channel, onset threshold, run mode,
evaluation profile and per-kind tolerances (arrival 2 ms default).
The run re-derives a `run_case` — sources/receivers rebound to the
member's per-measurement positions — and fails closed on any
config↔manifest↔import disagreement (member path, index set, onset
threshold, measurement sha, entry bounds).

`evaluate_bras_v3_observable` gates before comparing: band beyond
Nyquist → `unobservable`; absolute-level unit / non-Pascal authority →
`unsupported`; non-arrival kind while every solver lane is blocked →
`unsupported` (with the unlock condition named); no prediction →
`missing`; otherwise `evaluate_observable` maps to `pass`/`fail`.
`unsupported`/`unobservable`/`missing` are never conflated with a
numeric `fail`.

## Action 6 — report surfaces

`BrasV3QualificationReport` (`bqr-…`) aggregates evidence(s) that pin the
same binding+manifest, exposes `verdict_counts`, and writes a
`claim_ceiling` string derived from what actually produced verdicts.
`render_bras_v3_report_markdown` emits the lane table, per-observable
verdicts, the capability gap, and honesty notes.

CLI: `scripts/run_bras_v3_qualification.py` — `bind` / `run` / `report`
against a local corpus root, fail-closed, UTF-8 safe.

## What remains physically unverifiable

T20/decay, impulse-window level and magnitude-FR references exist in the
metric manifest but **cannot** be qualified by any current lane — the
binding records exactly why. Landing them requires sealed scene
geometry, per-source directivity authority, calibrated source strength,
and an execution binding for external scenes — future work, not this
issue.
