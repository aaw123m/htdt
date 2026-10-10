# R130D instantaneous impulse: negative original-contract continuation

The original instantaneous-input, rectangular sampled-pressure contract remains
**SELF_CONVERGENCE_FAILED**. A new physical Dirac / weak-observer candidate also
fails its preregistered independent refinement gate. Neither result is a fix or
a release qualification. The already qualified finite-band profile is unchanged.

## Original data and first correction

All five original source, voxel and raw-wave SHA hashes are checked during a
read-only archive replay. Original q[0]=1 and all subsequent samples zero,
40/80 Hz bins, eight-node weights and pressure endpoint stencils are unchanged.
Original adjacent complex errors reproduce as
1.246927070 / 0.761304772 / 0.367367293 / 0.958742341 (limit 0.20).

Weak observation of the same native archived potential reduces these only to
0.987080002 / 0.648492400 / 0.509212953 / 0.804492728. Observation alone does not
repair the staircase spatial operator.

On the exact-roof SEM system with the original eight physical-node functionals,
an exact continuous impulse and a C2 quintic endpoint test (1 ms at the start,
10 ms at the end) give 0.009275373 / 0.004878474 / 0.003810967 / 0.006508643.
All four threshold comparisons pass, but strict decrease does not. The native
terminal time is (Nt-1)*dt. Fixing it to exactly 250 ms lowers the maximum to
0.002897156, but still does not give strict decrease. No PASS is assigned.

All ten exact-time arms (two clocks, terminal ramps 20/10/5/2.5/1.25 ms) and
five Newmark interpolant arms are published, including failures. Narrowing the
ramp increases sensitivity. The zero-ramp limit is **NOT_ESTABLISHED**.

## New physical Dirac candidate, changed contract

The forcing is still instantaneous, q(t)=A delta(t), with a velocity jump
v(0+)=c^2 A M^-1 b and no later forcing; A=dt corresponds to discrete q0 and
cancels in P/Q. It is not the Gaussian drive. The point functional is evaluated
at the fixed physical coordinates, replacing the original moving eight-node
approximation. The SEM basis and pressure observer also change.

For mass-normalized modes, phi_e=A c^2 coupling_e sin(omega_e t)/omega_e.
The weak pressure transfer is rho c^2 sum coupling_e times the integral of
cos(omega_e t) w(t) exp(i Omega t). The rigid zero mode is retained. The endpoint
window w is integrated analytically using the Beta(3,3) ramp derivative transform.
All modes are retained; there is no source lowpass, source broadening, damping,
phase fit, or discarded frequency bin. This is a C2 finite-order weak observation
experiment, not a general distributional convergence theorem.

The exact-time SEM PPW28/32/36/40/44 adjacent complex errors are
0.001272470 / 0.000382696 / 0.000382955 / 0.000129923. They pass the new explicit
2% complex, 4% magnitude and 2 degree spatial tolerances, which do not assert
strict monotonicity. Newmark time errors at 1000/2000/4000/8000 steps are
0.121165995 / 0.045942890 / 0.011724471 / 0.002633236; final orders 1.9703/2.1546.

Independently assembled SHA-pinned MFEM P2 r2/r3/r4 systems are driven by the
same physical instantaneous velocity jump and observed by integration by parts
of potential against the window derivative, on 8000 intervals. No Gaussian
output is reused. Every true PCG residual satisfies the 1e-8 cap.

MFEM r3-to-r4 errors are **6.0922% complex / 18.8049% magnitude / 7.2799 degrees**,
failing the preregistered **3% / 5% / 3 degrees** fine-pair limits. The r4-versus-
SEM cross-check is **0.52805% / 1.88353% / 0.17724 degrees**, and passes its
independent cross-check bounds. A good cross-check does not override the failed
refinement test: overall **FAIL_DIRAC_WEAK_OBSERVER**. The plan is not relaxed.

## Record endpoint sensitivity witness

A separate analytic diagnostic enumerates non-corner x/y-wall image paths,
with z remaining near 2 m, below the roof. A path of length
sqrt(65^2+56^2)=85.7962703152 m arrives at **249.9891326201 ms**,
10.8674 microseconds before the nominal endpoint. The last native sample times
are 249.866268089 / 249.995012045 / 249.934931532 / 249.886867122 /
249.978626450 ms. Thus this arrival is before the last sample for PPW32 and
after it for the other four. This identifies sensitivity of endpoint evaluation;
it is not the claim that this single family causes all observed native error.
The half-open DTFT record is [0,Nt*dt); its endpoint differs from the last sample.
The final one-sided pressure stencil operates at that last sample, so both
times are recorded explicitly rather than conflated with a common exact T.

The x/y image sum excludes floor/roof interactions and diffraction and is
**not the complete sloped-room Green function**. The independent free-space
counterexample similarly proves that stability does not guarantee convergence
of this sharp observer; it does not prove impossibility for every discretization.

## Reproduction and validation

Use NumPy 1.26.4 / SciPy 1.14.1 / h5py and the existing Python 3.12 runtime,
with OPENBLAS_NUM_THREADS=4 and OMP_NUM_THREADS=4.

```
python scripts/run_r130d_weak_impulse_observer.py
python scripts/run_r130d_weak_impulse_newmark.py
python scripts/run_r130d_archived_native_weak_impulse.py --original-sims-root PATH
python scripts/run_r130d_physical_dirac_weak.py --stage all
python scripts/r130d_dirac_weak_cli.py --output-dir RESULTS
```

The cached eigensystems reproduce the frozen original SEM sampled-impulse
results before new scores are calculated. The CLI checks complete spatial-cache
hashes, publishes the actual FAIL status and keeps the legacy FAILED status.
All 28 relevant tests pass, including independent quadrature/recurrence checks,
free-space distribution pairing, and independent recomputation of negative
and positive evidence gates. A passing test suite is not a passing model.

Preregistered JSON plan hashes precede their new numeric executions. No native
PFFDTD reruns or GitHub Actions were initiated, and no archived wave was edited.
Measured-room / BRAS validation remains NOT_VALIDATED. Draft PR #118 and
Issue #53 remain open. The finite-band profile retains its previous qualification.

Background primary sources: [NIST distribution derivatives](https://dlmf.nist.gov/1.16)
and [Symes et al., weak convergence of singular wave sources](https://par.nsf.gov/servlets/purl/10301929).
These motivate separate observer checks; they are not acceptance certificates.
