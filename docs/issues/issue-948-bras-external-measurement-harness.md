# Issue #948 — BRAS external measurement import + metric + deterministic replay harness

## What landed

`backend/src/htdt/cad_external_benchmark_fixture.py` — the common foundation for
external-measurement benchmark qualification, callable from #939's RS8 executor:

- `ExternalBenchmarkFixture` (sealed, extra-forbid): binds license id/family,
  attribution note, redistribution permission (auto-derived; `permitted` only for
  open license families), origin URI, scene config, coordinate convention, unit
  semantics (`pascal_calibrated`/`normalized`/`unknown`), phase authority
  (`coherent_phase`/`magnitude_only`/`unknown`), valid band + basis, declared
  source/receiver points, boundary-material bindings, and per-file pins
  (consumed = fail-closed import gate; bound = advisory) with zip-member
  sha256/size pins. Licensed payloads are verified, never vendored.
- `read_sofa_measurement` — SOFA `SingleRoomSRIR` reader (flat `Data.IR` or
  nested `Data`/`IR`), `M×R×N` semantics (per-measurement source/listener
  positions, per-channel receivers), units preserved verbatim (dataset `Units`
  attr or publisher `Comment` declaration — no guessing), coordinate/convention
  checks, finite-sample check, content sha256.
- `verify_fixture_payloads` / `assert_fixture_payloads_verified` — per-pin
  receipts (`verified`/`missing`/`size_mismatch`/`md5_mismatch`/`sha256_mismatch`/
  `member_missing`/`member_sha256_mismatch`/`member_size_mismatch`/`unverifiable`/
  `outside_admission`); any consumed pin not `verified` aborts import.
- `import_fixture_case` — produces a sealed `BenchmarkSourceAsset`
  (measured provenance) + sealed `BenchmarkCase` with all measured positions,
  limitations carrying redistribution/unit/phase/band notes.
- `evaluate_fixture_observable` — fail-closed gate order: disjoint band →
  `unobservable`; semantic blockers (magnitude-only phase authority, non-pascal
  units vs absolute-level tolerance, unknown units vs amplitude-sensitive kinds)
  → `unsupported`; no prediction/tolerance → `missing`; evaluated → `pass`/`fail`.
  Missing/unobservable/unsupported never fold into numeric FAIL. Calibrated Pa is
  never conflated with normalized amplitude; wave phase is never claimed for
  phase-unknown sources.
- `FixtureRunSpec` (sealed) — pins solver path/revision, mesh resolution, seed,
  provider id/version/config sha256, evaluation profile, runtime descriptor
  (python/numpy/h5py/platform), and run mode. `preregistered_unfitted` rejects
  informed parameters; `informed_calibrated` requires informed parameters +
  prior unfitted evidence pin — preregistered vs informed never mix.
- `run_fixture` → sealed `FixtureRunEvidence`; frozen-config drift raises
  `FixtureIntegrityError` (fixture/provider/config). Status fold: fail if any
  observable fails, pass iff all pass, else incomplete, empty → blocked.
- `replay_fixture` → `FixtureReplayReport`: `blocked` on runtime drift,
  `reproduced` iff evidence sha256 identical, else `diverged`.
- Providers: `AnalyticDirectPathProvider` (free-field d/c arrival predictions),
  `StaticFixtureReplayProvider` (replays recorded predictions).

`scripts/run_external_benchmark_fixture.py` — CLI subcommands
`verify`/`import`/`plan`/`run`/`replay`; fail-closed exit codes
(2=integrity/import failure, 3=non-pass run, 4=non-reproduced replay).

## Evidence

Synthetic suite: `backend/tests/test_issue_948_bras_fixture_harness.py`
(35 tests) — tamper/unit/band/coordinate mismatches fail closed; sealed
determinism; run-mode gating; deterministic replay reproduced/diverged/blocked.

**Real licensed case (BRAS RS8_01a, CC BY-SA 4.0)**: publisher-sha256-verified
`1_Scene_descriptions.zip` + `3_Surface_descriptions.zip` consumed;
`RS8_RIRs_01a.sofa` (351×1×10001 @44.1 kHz, declared Pascal units) imported
fail-closed; `S1→R151` direct-path arrival observable — measured peak
7.891 ms vs analytic 7.726 ms (err 0.165 ms < 0.3 ms) → **pass**;
replay → **reproduced** (evidence `56d87b2f…`, bit-identical sha256).
Artifacts: `backend/fixtures/bras-rs8-01a/` (fixture, spec, observables,
imported case, evidence). Re-run:

```
python scripts/run_external_benchmark_fixture.py verify \
  --corpus-root <corpus> --fixture backend/fixtures/bras-rs8-01a/fixture.json
python scripts/run_external_benchmark_fixture.py run \
  --corpus-root <corpus> --fixture backend/fixtures/bras-rs8-01a/fixture.json \
  --spec backend/fixtures/bras-rs8-01a/spec.json \
  --observables backend/fixtures/bras-rs8-01a/observables.json \
  --provider analytic-direct --speed-of-sound 343.0
python scripts/run_external_benchmark_fixture.py replay ... \
  --evidence backend/fixtures/bras-rs8-01a/evidence.json
```

## Honest limits

- `Documentation.pdf` + `2_Source_descriptions.zip` are bound (advisory) pins;
  the 776 MB source-descriptions archive was not fetched → receipt `missing`
  (advisory only, does not block).
- The analytic provider predicts only `arrival_timing`; phase-bearing and
  amplitude observables are `unsupported` until a real solver (e.g. #939's
  RS8 executor or R130D/PFFDTD) supplies predictions.
- No physical PASS is claimed: the single pass is a geometry-vs-measurement
  arrival check, not a solver validation.
