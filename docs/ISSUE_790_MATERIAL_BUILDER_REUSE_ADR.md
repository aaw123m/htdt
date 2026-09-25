# Material builder reuse-vs-implement ADR (#790 MAB10)

Status: accepted 2026-09-24
Scope: porous-absorber construction → derived impedance/reflection/absorption evidence (MAB10 slice only).

## Decision

Implement a thin, narrowly-scoped HTDT transfer-matrix module
(`htdt.cad_acoustic_construction`) using the published Miki (1990)
model instead of adopting `acoustipy` as a dependency for this slice.

## Candidates evaluated

### Candidate A — reuse `acoustipy` (MIT)

Positive findings:

- MIT license; redistribution-friendly.
- Implements TMM multilayer structures, Delany-Bazley/Miki/JCA-family
  porous models, air gaps and inverse identification workflows —
  credible prior art for later MAB30/JCA and §10 inverse fitting.

Blocking concerns for MAB10:

- API and dependency surface are far wider than MAB10 needs (full
  absorber object model, plotting, inverse solver); vendoring a subset
  loses upstream tracking, adopting it wholesale adds a heavyweight
  physics dependency for ~80 lines of arithmetic.
- Correctness, units and e^{jwt} sign conventions must be re-verified
  against primary literature anyway; the verification cost for MAB10 is
  the same as writing the formulas directly.
- HTDT requires per-point model-validity gating and content-addressed
  evidence — neither maps onto acoustipy's return values without a
  wrapper layer.
- Windows/PySide packaging footprint and numpy-version interaction need
  their own validation pass.

### Candidate B — thin HTDT implementation

- Miki's equations are two short published formulas; the impedance
  recurrence for a rigidly backed layer stack is standard transmission-
  line arithmetic.
- Independently validated in `tests/test_cad_acoustic_construction.py`
  against a hand-computed Miki/TMM reference point (100 mm porous,
  sigma = 10 kPa·s/m², 500 Hz → Z_in ≈ 441 − j221 Pa·s/m, α ≈ 0.94)
  and against closed-form medium values.
- Zero new dependencies; sign convention, validity gating and hashing
  conventions stay inside HTDT authority patterns.

## Consequences

- `acoustipy` remains the leading reuse candidate for MAB30+ (JCA
  parameter families) and §10 inverse identification; its ledger entry
  stays at `SOFTWARE_REUSE_BAKEOFF` pending that slice's own ADR.
- This decision is scoped to MAB10 only and does not establish a policy
  of reimplementing porous-acoustics mathematics.

## Validation record

- `backend/tests/test_cad_acoustic_construction.py`:
  `test_miki_medium_reference_point`,
  `test_rigid_backed_porous_absorption_reference`,
  `test_air_gap_increases_low_frequency_absorption`,
  `test_passivity_holds_across_grid`,
  `test_validity_gating_fail_closed_and_exploratory`,
  `test_resistivity_outside_miki_envelope`,
  `test_evidence_binds_construction_and_is_deterministic`.
