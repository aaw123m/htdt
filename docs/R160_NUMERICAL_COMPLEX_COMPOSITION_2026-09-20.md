# R160 numerical complex hybrid composition — 2026-09-20

Issue #101 vertical slice. Base authority: `main@f7ba4d8b30605b60da914c5e497870b104dfc7fa`.

## Scope

This task advances the existing typed/bounded R160 authority to an exact-authority-bound numerical complex transfer composition. HTDT-Capture, R100B MFEM experiment code, R130D geometry-adapter code, and canonical status/roadmap documents are out of scope.

## Convention comparison discovered before implementation

R130 candidate complex-pressure artifacts are emitted as Cartesian complex pressure in Pa with phasor convention `exp(-i*omega*t)` and analysis Fourier kernel `exp(+i*omega*t)`. The artifact pressure is the finite-record `P/Q` transfer multiplied by the exact `AcousticWaveExcitationAuthority Q(f)`.

R150 path responses are `complex_acoustic_pressure_per_volume_velocity` in `Pa/(m3/s)`, normalized to unit volume velocity, with phasor convention `exp(+i*omega*t)`, source time origin `source_t0`, and explicit complex propagation/reflection/Portal transfer authority.

Therefore R130 pressure cannot be numerically stitched directly to R150 path transfer. The supported numerical slice must first divide R130 pressure by the exact complex volume velocity at the same exact frequency sample, then convert the opposite phasor representation by complex conjugation. Zero/missing Q(f), frequency-grid mismatch, unrecognized conventions, or stale authority must fail closed.

## Planned bounded numerical authority

The first slice uses exact shared frequency samples only. R150 high-band response is a coherent complex sum only when every required path artifact is `COMPLEX_SUPPORTED`. No magnitude-only phase synthesis, implicit unity Portal/reflection transfer, nearest-neighbor resampling, or incoherent magnitude sum is allowed.

Transition overlap uses a versioned complementary blend, `H = w_low H_wave + w_high H_ga`, with `w_low + w_high = 1` at every output bin. The composition identity binds endpoints, weight law, exact grid, normalization authority, exact R130 result/artifact/excitation/input identities, and exact R150 response identities.

## Non-claims

This slice is not production solver adoption, optimal-crossover selection, full-broadband validation, diffraction completeness, late-reverberation completeness, owned-room validation, or R180 completion.

RDC usage: 0.
