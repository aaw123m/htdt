# Issue #891 — Reference Theater self-test fixture + deterministic self-test lane

## Scope

A versioned, provenance-safe **Reference Theater**: a small real-shaped theater
project shipped with the application, plus a deterministic self-test lane that
opens the fixture and walks the full commissioning story — `open → authority →
analysis → compare → deploy → verify → export` — checking every step against
documented expected outcomes. The lane exists so a fresh install (or a CI job,
or a support call) can exercise the whole sealed-authority pipeline end to end
and get a sealed, replayable verdict instead of a smoke-screen pass.

The fixture is synthetic. It contains no private or customer data and no
external proprietary assets; every entity, measurement row, and equipment
definition was authored for this issue.

## Vocabulary

| Term | Meaning |
|---|---|
| `ReferenceTheaterPayload` | The fixture body: a `reference_project` `SceneDocument` plus declared equipment, material/source data, measurement evidence, optimization candidates, a simulated `deploy` HeadlessDeploymentSpec document, a real sealed `export` snapshot, and per-step `expected` outcomes. Format `htdt-reference-theater-1`, fixture version `rt-v1`. |
| `ReferenceTheaterManifest` | Sealed descriptor (`rtman-…`) pinning the payload by canonical content sha256, the expected scene content hash, the export semantic hash, and the fixture size class. `manifest_id`/`manifest_sha256` are the sealed pair. |
| `CadReferenceTheaterRun` | Sealed run record (`rtrun-…`) persisted to `cad_reference_theater_runs` (native schema v111). Carries the fixture version, manifest sha, scene sha, per-step verdicts, deploy honesty fields, and the rolled-up `outcome`/`verdict`. |
| Step verdicts | `verified`, `diverged`, `unverified`, `unauthorized`, `blocked`, `skipped`. Every lane step lands exactly one. |
| Run outcomes | `passed` (all steps verified), `failed` (diverged/unverified evidence), `unauthorized` (deploy apply requested without `--authorize-apply`), `blocked` (environment/evidence missing), `fixture_drift` (payload or materialized scene no longer matches the manifest pins). |

## Evidence model

- **Fail closed.** The payload is pinned twice: canonical content sha256 in
  the manifest (the corpus-manifest convention — semantic content, immune to
  checkout line-ending differences) and the manifest's own seal.
  `load_reference_theater_payload` validates format, fixture version, manifest
  seal, payload seal, payload content sha, scene sha, and export sha — any
  mismatch raises `ReferenceTheaterDriftError` before anything opens.
- **Drift at the scene layer.** `materialize_reference_theater` writes the
  fixture scene through `SceneRepository` once; reopening is a no-op only when
  the current head's `scene_content_hash` still equals the manifest pin. A
  divergent head under `doc-reference-theater-v1` is user drift and refuses to
  reopen — the fixture never silently adopts an edited document.
- **Expected outcomes live in the fixture**, not the lane. `expected.analysis`
  carries entity counts and room geometry facts; `expected.compare` names the
  preferred optimization candidate; `expected.verify` carries the applied
  channel gains and tolerance; `expected.deploy`/`expected.export` carry the
  remaining step outcomes. The lane compares observed facts against these and
  diverges when they disagree.
- **Deploy honesty.** The deploy step runs the real
  `DeploymentPipelineService` against `FakeAvrLanTransport` — a simulated
  `avr-lan` target — so the record stores `deploy_is_simulated=True` and
  `deploy_evidence_strength='machine_readback'` (the strongest evidence a
  simulated transport can honestly produce; see #878's evidence-strength
  vocabulary). Nothing in the lane claims a real device was deployed.
- **Export is a report, not a fantasy.** The export step writes
  `htdt-reference-theater-selftest-1` JSON containing the run record, per-step
  results, and the fixture's sealed export snapshot sha — evidence the lane
  ran, in a replayable artifact.

## Integration points

- **CLI**: `htdt selftest run --fixture-version rt-v1 [--authorize-apply OP]
  [--out PATH]` (verb `selftest.run`, sealed verb in `cad_headless_cli`).
  Outcome → exit lattice matches the other lanes: `succeeded`→0,
  `failed`→2, `unauthorized`→4, `blocked`→3, `fixture_drift`→2.
  Without `--authorize-apply` the lane still runs through the deploy preview
  and reports `unauthorized` — the apply gate is exercised, not bypassed.
  `records list --kind reference_theater_run` / `records export` read the
  sealed runs.
- **#886 first-run wizard**: `FirstRunWizardFacts.reference_theater_available`
  (probed behind the error boundary) surfaces an "リファレンスシアターを開く"
  offer on stage 1 while the project-room step is incomplete; accepting calls
  `materialize_reference_theater` and opens the document. The offer is intent
  wiring only — the wizard's flow is unchanged.
- **#867 benchmark hook**: `reference_theater_benchmark_entry()` exposes the
  versioned fixture (id, size class, scene/document shas) as a stable
  manifest entry for benchmark tooling — a manifest entry, not a new
  benchmark.
- **Authority audit**: `cad_reference_theater_runs` is a replay-canonical
  table — registered in `NATIVE_BASELINE_DDL`, `NATIVE_SCHEMA_TABLES`,
  `_ROW_BINDINGS`, `_TABLE_POLICY` via `_RepositoryChain` (`reference_theater`
  repository) and `_ReplayProbe` (`reference_theater_run` record kind); JA
  lifecycle label `リファレンスシアター検証`.

## What remains device-only

- The deploy lane is simulated by construction (`approved_remote_endpoints`
  allows only `rt-sim-avr-1`); no claim about a physical AVR is made.
- Measurement evidence and optimization candidates are declared fixture data —
  enough to exercise compare/verify logic, not a substitute for a measured
  room.
- The GUI offer button opens the project; driving the full 7-step lane still
  happens through the CLI or API.
