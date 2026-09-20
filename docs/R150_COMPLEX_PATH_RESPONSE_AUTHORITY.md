# R150 frequency-dependent complex path response authority

Status: **implementation in progress**

Issue: #101  
Base main: `49901a6b46ad889bd06b34e4482a5b6121545d1d`  
RDC usage: **0**  
HTDT-Capture changes: **0**

## Scope

This vertical slice adds a solver-neutral frequency-domain response authority above the existing immutable `DeterministicPathArtifact`. The existing path artifact remains geometry-only evidence. This layer is per-path only and does not sum paths or perform R160 wave+GA composition.

## Required physical contract

The authority will explicitly bind physical quantity, unit, source normalization, phasor convention, time origin, environment, valid frequency band/grid, geometric path length, propagation phase, geometric spreading, source capability, ordered surface reflection transfer, ordered Portal transmission transfer, and resulting response capability.

Target comparison quantity for coherent responses is `Pa/(m3/s)` under an explicitly selected analytic point-volume-velocity monopole capability. Unknown or magnitude-only source authority is never promoted to coherent complex capability.

## Fail-closed rules

No complex response is synthesized from:
- unknown source directivity;
- magnitude-only directivity by assuming zero phase;
- scalar absorption by inferring reflection phase;
- missing reflection authority;
- an open Portal by assuming unity transmission;
- missing/stale Portal transfer;
- out-of-band dependencies;
- zero/invalid path length;
- scattering/diffraction/stochastic paths.

Unsupported is not represented as zero response.

## Non-goals

No path summation, broadband room-response synthesis, R160 numerical stitching, scattering, diffraction, stochastic rays, late reverberation, automatic material inference, automatic Portal-transfer inference, or production solver adoption.

## Verification plan

Focused invariant fixtures will cover:
- analytic free-field direct;
- rigid planar image-source first reflection;
- explicit complex-impedance reflection;
- ordered two-surface coefficient product;
- capability rejection for magnitude-only/absorption-only/out-of-band/missing Portal authority;
- explicit ordered multi-Portal transmission product;
- dependency identity change and stale persistence rejection.

## R160 handoff

R160 numerical composition remains a separate follow-up after this authority is merged. This PR will only produce a physically normalized, dependency-bound per-path complex-response contract suitable for that later composition step.
