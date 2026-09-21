# R100B MFEM spatial h-refinement experiment

## Scope

This document records the Issue #101 / R100B selection-changing MFEM spatial convergence slice for `wave-rectangular-convergence-v1`. HTDT-Capture, R130D, R140, R150, R160, R170, UX, and the shared implementation roadmap/status documents are out of scope.

## Frozen authority

- task-start `main`: `9e6066259ec58c093e9a7587550ccf907f28402f`
- initial frozen-plan commit: `c181fb62993e5dcbff719feac02b013a0459d5d2`
- final pre-result plan identity commit: `e4ac01664603bbfd9e761ebd15d2e530896a337e`
- R100A manifest id: `htdt-issue-101-r100a-benchmark-authority`
- R100A semantic hash: `a9d45a3d650f20747368dd5610a6a91f93cdad881dcb10fcac88cd9d17e211e7`
- candidate: `mfem-v4.10-d964264`
- MFEM source commit: `d964264cdb9a13e94a201b6c236c7721e0c8765f`
- H1 order: 2
- uniform refinements: exact 0 / 1 / 2
- record: exact current R100A 2 s finite record
- frequency grid: mechanically derived current R100A 20..300 Hz / 1 Hz grid
- observable: finite-record complex `P_T(f)/Q_T(f)`
- temporal integrator: PR #289 GL2 / Padé [2/2] identity
- output sample rate: 12000 Hz
- substeps per output interval: exact 4
- adaptive stepping: false
- result-driven masks/exclusions: false
- geometry fitting / source-receiver shift / tolerance relaxation: false
- h3 / p-order retry: false

The temporal identity was strengthened after the initial plan-only commit but before any authoritative h0/h1/h2 numerical result was produced. The final pre-result content is therefore bound to the later plan identity commit above; the initial commit is retained for audit history.

## Implementation

The MFEM exporter builds the exact current R100A axis-aligned rectangular box and exports the H1 p2 semidiscrete mass/stiffness matrices plus source and receiver delta functionals for each predeclared refinement. Python then validates all non-spatial bindings, applies the same sparse SuperLU-backed fourth-order GL2 / Padé [2/2] propagation contract as PR #289, records the exact 95996 internal steps for the 2 s / 12000 Hz output record, computes direct scored-frequency DTFT `P_T/Q_T`, and evaluates current R100A monotonic complex-RMS convergence.

Each refinement records element/DOF counts, mass/stiffness nnz, assembly and factorization times, stepping and total solve time, peak RSS, disk/output size, internal-step count, residual evidence, semidiscrete identity, and transfer identity. Resource overflow is fail-closed; no result-driven retry is permitted.

Typed `BakeoffFixtureEvidence` is generated for the exact current authority binding. Admissibility is only reported PASS when the existing production-readiness resolver actually includes the new record in the target fixture check; constructing a syntactically valid record alone is not sufficient.

## Acceptance

The authority is current R100A `wave-rectangular-convergence-v1`:

- acceptance relation: `monotonic_convergence`
- final complex RMS absolute tolerance: 0.02
- final complex RMS relative tolerance: 0.02
- separate dB/phase gate: none

The report also records adjacent `h0 -> h1` and `h1 -> h2` complex RMS absolute/relative metrics. R100A monotonic convergence remains evaluated against the finest representation, so error reduction is assessed as `h0 vs h2` compared with `h1 vs h2`.

## Decision separation

The authoritative report keeps these independent:

- workflow execution
- spatial numerical convergence
- current R100A fixture tolerance
- resource suitability
- typed evidence admissibility
- candidate-wide readiness
- production solver selection

An experiment PASS is not production adoption. `production_solver_selected=false` is invariant.

## Evidence status

Authoritative numerical evidence is produced only by `.github/workflows/r100b-mfem-spatial-refinement.yml`. The final run id, artifact id/digest, refinement metrics, resources, readiness before/after, and final decision are appended after the successful authoritative workflow completes.
