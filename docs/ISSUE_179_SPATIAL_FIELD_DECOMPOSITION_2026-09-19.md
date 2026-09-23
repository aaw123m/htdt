# Issue #179 — Solver-neutral spatial incident/reflected field decomposition

Date: 2026-09-19

## Scope

This slice establishes a solver-independent mathematical authority for spatial
incident/reflected complex-pressure decomposition. It deliberately does **not**
select PFFDTD, MFEM, or any other backend as truth.

The first supported method is intentionally narrow:

- planar reference boundary;
- locally one-dimensional field;
- normal incidence only;
- exactly two complex-pressure samples;
- both samples on the same line normal to the boundary;
- exact sample positions and exact inward distances;
- exact common frequency grid;
- exact sound-speed authority;
- exact Fourier/phasor authority and pressure normalization.

General 3-D wave separation, arbitrary-angle incidence, beamforming and diffuse
field decomposition are outside this authority and remain **UNSUPPORTED**.

Implementation:

- `backend/src/htdt/acoustic_spatial_decomposition.py`
- `backend/tests/test_acoustic_spatial_decomposition.py`

## Mathematical definition

### Existing R100 Fourier authority

Current R100 authority is already explicit:

- `NumericalComparisonContract.fourier_sign = exp(-i*omega*t)`;
- finite-record R100A-4 analysis uses
  `dt_weighted_sum` with kernel `exp(+i*2*pi*f*n*dt)`.

This decomposition authority therefore follows the existing R100 convention
instead of introducing the opposite convention.

Canonical time dependence:

```text
exp(-j * omega * t)
```

The analysis transform that produces compatible complex pressure is:

```text
exp(+j * omega * t)
```

A solver/backend using another convention must perform an **explicit, separately
identified conversion** before its pressure evidence can be consumed. The
decomposition algorithm never applies an implicit conjugation or sign flip.

### Boundary normal and inward distance

The reference boundary plane is represented by:

- one exact point `x0`;
- one exact unit **surface outward normal** `n_out`.

The canonical acoustic-domain inward direction is:

```text
n_in = -n_out
```

The boundary is `d = 0`. Inward distance is positive into the acoustic domain:

```text
d >= 0
x(d) = x0 + d * n_in
```

Both pressure samples must lie on this exact normal line. A sample on the wrong
side of the boundary, a lateral offset, or an inconsistent stored distance is
rejected as outside the supported method domain.

### Incident/reflected convention

For frequency `f` and exact sound speed `c`:

```text
k = 2*pi*f/c
```

Under the canonical `exp(-j*omega*t)` time convention:

```text
p(d) = I * exp(-j*k*d) + R * exp(+j*k*d)
```

where:

- `I` is the complex pressure amplitude propagating **toward** the boundary,
  therefore toward decreasing `d`;
- `R` is the complex pressure amplitude propagating **away from** the boundary,
  therefore toward increasing `d`.

This sign convention is part of the spec identity.

## Two-point solve

For exact distances `d1` and `d2` and complex pressures `p1` and `p2`:

```text
[ p1 ]   [ exp(-j*k*d1)  exp(+j*k*d1) ] [ I ]
[ p2 ] = [ exp(-j*k*d2)  exp(+j*k*d2) ] [ R ]
```

The implementation solves this exact 2 x 2 complex system directly. It records
the determinant and the matrix 2-norm condition number. No epsilon is inserted
into the matrix, determinant, incident amplitude, or denominator.

For an AVAILABLE frequency it records:

- incident complex pressure `I`;
- reflected complex pressure `R`;
- reflection coefficient `R/I`;
- reconstructed total pressure at both sample locations;
- maximum absolute reconstruction residual;
- determinant and condition-number evidence;
- exact threshold authority.

## Conditioning and fail-closed behavior

The v1 conditioning policy is itself stored in the spec identity:

- policy id:
  `htdt.planar-normal-incidence-two-point-conditioning`;
- policy version: `1`;
- minimum `|det(A)| = 1e-8`;
- maximum 2-norm condition number `1e8`;
- incident magnitude floor `1e-12`;
- sample geometry tolerance `1e-9 m`.

The matrix entries are unit-magnitude phasors, so the determinant threshold is
dimensionless. The incident floor is interpreted in the exact pressure
normalization carried by the spec.

Per-frequency states are:

- `AVAILABLE`: matrix is accepted and `|I|` is above the incident floor;
- `BLOCKED / SINGULAR`: determinant threshold failed;
- `BLOCKED / ILL_CONDITIONED`: condition-number threshold failed;
- `BLOCKED / INCIDENT_UNDEFINED`: `I` is zero/near-zero, so `R/I` is
  undefined;
- `UNSUPPORTED / UNSUPPORTED_CONVENTION`: pressure evidence does not use the
  canonical phasor/analysis convention and no explicit conversion authority was
  supplied.

An infinite condition number is stored explicitly as an infinity flag with no
fabricated finite numeric substitute.

## Authority and provenance model

### SpatialFieldDecompositionSpec

The immutable/versioned spec carries:

- deterministic spec id and SHA-256;
- method id/version and narrow valid-domain declaration;
- exact fixture and/or prediction authority;
- exact raw complex-pressure evidence ref;
- exact timing/Fourier authority ref;
- exact normalization authority ref;
- exact sound-speed authority ref;
- pressure-evidence source classification;
- exact solver/backend provenance when the input is solver-derived;
- boundary plane point;
- outward normal;
- canonical inward coordinate semantics;
- exact sample positions/distances;
- exact frequency grid;
- exact sound speed;
- canonical and evidence phasor conventions;
- analysis kernel;
- pressure normalization;
- exact conditioning policy/version/thresholds.

Solver-derived evidence is rejected if exact solver/backend provenance is absent.

### SpatialFieldDecompositionResult

The immutable result binds exactly to the spec id/hash and raw pressure evidence
id/hash. Each frequency stores its own conditioning/status/reason and numerical
output only when permitted by the state.

A result hash changes when any exact mathematical, geometric, numerical,
provenance or raw-pressure input changes.

## Analytic synthetic verification

The focused fixture is mathematical adapter verification only. It is not PFFDTD
physics validation and cannot become an independent solver reference.

The tests cover:

1. pure incident field;
2. known non-zero reflection;
3. phase-bearing reflection;
4. multiple frequencies;
5. changed sample spacing and changed identity;
6. half-wavelength singular spacing, blocked without epsilon substitution;
7. zero and near-zero incident pressure, with `R/I` blocked;
8. normal and Fourier convention identity plus no implicit conversion;
9. same input producing the same evidence/spec/result hashes;
10. explicit reconstruction residual and reconstructed sample pressures.

GitHub Actions is the execution authority for the focused tests. Final run status
is recorded on the pull request.

## PFFDTD bridge audit

### Frozen normal-incidence impedance fixture

The current frozen `wave-normal-incidence-impedance-v1` authority has:

- source: `(4, 2, 1) m`;
- **one** receiver: `(3, 2, 1) m`;
- receiver timing reference: `source_t0`;
- frequency grid: exactly 100 / 200 / 300 Hz;
- sound speed: exactly 343 m/s;
- R100 Fourier sign: `exp(-i*omega*t)`.

The required second spatial complex-pressure sample is absent.

The existing PFFDTD impedance workflow calls PFFDTD's native
`compute_Rf_from_DEF()` and records its boundary/reflection-function output.
It explicitly declares `spatial_fdtd_reflection_validated = false`. That
reflection-function evidence is **not** a pair of spatial total-pressure samples
and is not relabeled as such here.

Therefore no current spatial two-point decomposition is performed for this
fixture.

### Existing concave PFFDTD evidence

The current exact-concave PFFDTD harness also records one receiver trace per
level. It does not provide the required two co-referenced pressure samples for
this decomposition authority.

Its existing numerical result remains **FAIL / non-converged**. This PR does not
change the result, tolerances, frozen fixture semantics, or independent-reference
qualification.

### Bridge status

**Current bridge: BLOCKED.**

Missing current evidence authority:

- a second exact spatial pressure sample on the same normal line;
- exact two-sample evidence identity binding those locations to one common
  timing/Fourier/normalization authority.

Data that already exists (frequency grid, sound speed and R100 Fourier
convention) is not sufficient to manufacture the missing spatial evidence.

This slice does not:

- modify the frozen R100A fixture to add a receiver;
- promote failed MFEM output to truth;
- use PFFDTD as its own independent reference;
- relabel native reflection coefficient output as spatial pressure evidence.

A future evidence-acquisition slice may bind to this authority only if it
captures the required two spatial pressure samples without changing the frozen
fixture meaning.

## Issue #179 status

The previously missing solver-neutral spatial decomposition contract is now
implemented.

The **spatial reflected-field evaluation itself remains BLOCKED** because the
current frozen PFFDTD evidence does not contain the required two spatial
complex-pressure samples. This is a data-acquisition limitation, not a reason to
fabricate a decomposition value.

Issue #179 can be closed as an authority/evidence investigation once this PR is
merged: its remaining mathematical authority is implemented and the unavailable
current spatial evidence is explicitly recorded as BLOCKED rather than forced to
PASS. Any future acquisition of two-point PFFDTD pressure evidence should be a
new, separately scoped evidence task.

## Non-goals preserved

No implementation here includes:

- general 3-D wave separation;
- arbitrary-angle incidence;
- beamforming;
- diffuse-field decomposition;
- production solver adoption;
- PFFDTD accuracy changes;
- MFEM re-adoption;
- tolerance relaxation;
- R110;
- GUI;
- O90/O100.

RDC used: **0**.
