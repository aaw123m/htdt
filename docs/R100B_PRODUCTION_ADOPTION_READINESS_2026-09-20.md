# R100B Production-Adoption Readiness — 2026-09-20

Issue: #101  
PR: #267  
Base main: `bcc48997a9b7cf5a9d567cb532b4671f6d581275`  
RDC usage: **0**

## Purpose

This record closes the remaining R100B **selection-readiness authority** gap without selecting a production wave solver.

The existing authorities remain authoritative:

- `benchmarks/acoustics/r100a_manifest.json`
- `benchmarks/acoustics/r100b_candidates.json`
- `benchmarks/acoustics/r100b_wave_adoption_profile.json`
- existing `htdt.acoustic_bakeoff` evidence/run types

This slice adds a candidate-wide audit that evaluates:

`candidate -> exact implementation -> required capability -> exact fixture/evidence -> hard-gate state -> Windows/license/resource state -> selection readiness`

No weighted score, candidate ranking, performance compensation, or default PFFDTD selection is introduced.

## Evaluated implementations

| Candidate | Role | Exact implementation | Selection scope |
|---|---|---|---|
| PFFDTD | wave primary evaluation | `bsxfun/pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5` | shipping candidate |
| MFEM | wave reference / alternative | `mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f` (v4.10) | shipping candidate |
| pyroomacoustics | geometric reference | `LCAV/pyroomacoustics@f02b01dd6609709e2089aefa5d1e59c91d3a0601` (v0.10.1) | reference only |

Reference-only candidates cannot become the low-band production wave solver.

## Candidate-wide gate semantics

The report uses four explicit audit states:

- **PASS** — exact current-authority evidence satisfies the required gate.
- **FAIL** — exact current-authority evidence explicitly failed, or immutable candidate metadata explicitly fails a hard gate such as redistribution/platform support.
- **BLOCKED** — required evidence/capability is missing, stale, unbound, unsupported for the required role, conflicting, or otherwise cannot be admitted as current evidence.
- **NOT_APPLICABLE** — the check is outside the selected production role. For the current low-band wave profile, the deferred geometric reflecting-obstacle fixture is reported this way.

A mandatory `NOT_APPLICABLE` is not treated as PASS.

### Mandatory production-wave checks

The audit exposes, at minimum:

- exact repository/ref/commit implementation identity;
- license and redistribution;
- Windows execution/packaging;
- CPU correctness baseline;
- rigid analytical/reference evidence;
- grid/mesh convergence;
- complex FR/phase capability;
- explicit impedance/reflection evidence;
- concave geometry evidence;
- Portal/region continuity;
- radiation/source capability where required by the current wave profile;
- resource-budget evidence;
- deterministic/reproducible execution;
- required fixture/capability coverage;
- current authority freshness.

Performance/resource evidence is evaluated only after the mandatory physics fixture path is qualified. A fast solver cannot offset a failed or missing physics gate.

## Exact binding and stale evidence

A selection-eligible evidence atom must bind to:

- current R100A manifest id and semantic hash;
- exact candidate source commit;
- exact candidate semantic hash;
- exact fixture or hard-gate target;
- typed existing R100B fixture/hard-gate evidence payload.

The candidate semantic hash is intentionally candidate-local. Adding a new unrelated candidate does not invalidate an existing candidate's evidence. Changing that candidate's implementation or candidate authority does.

Historical evidence is retained in:

`benchmarks/acoustics/r100b_production_adoption_evidence.json`

but stale or incompletely bound records are excluded from current selection input. Their original PASS/FAIL state remains visible for audit; negative evidence is not deleted or promoted to reference truth.

Notably retained history includes:

- PFFDTD rigid rectangular PASS summary from 2026-09-18;
- PFFDTD rectangular convergence FAIL history;
- PFFDTD impedance PASS history;
- PFFDTD concave FAIL history;
- MFEM concave / finite-record FAIL, including exact current R100A-4 artifact `10583118482`;
- MFEM Portal PASS history;
- MFEM radiation FAIL history;
- pyroomacoustics direct/first-reflection PASS history;
- pyroomacoustics stochastic non-converged FAIL history.

## Current readiness outcome

The checked-in evidence ledger deliberately does **not** fabricate current evidence from prose summaries or older artifacts.

Therefore the expected production-adoption result for the current authority is:

**NO_GO / additional evidence required**

Production solver selected: **no**.

The decisive reasons are hard-gate/capability/evidence completeness, not comparative speed:

- PFFDTD's current candidate declaration does not claim all low-band adoption capabilities, including Portal continuity and explicit radiation termination.
- MFEM's immutable finite-record artifact `10583118482` exactly matches current R100A-4 hash `a9d45a3d650f20747368dd5610a6a91f93cdad881dcb10fcac88cd9d17e211e7`, current candidate-manifest hash `8fda56df1087fd64f55cfd17e241e46d236b60e242d86c546650d8f9d4194707`, and the pinned MFEM commit. Its concave result is therefore admitted as a current **FAIL / non-converged**, not BLOCKED; the unqualified finest trace is still not reference truth. Other mandatory MFEM fixtures/platform gates remain unresolved or stale.
- pyroomacoustics remains reference-only and cannot satisfy the production-wave role.
- missing/stale evidence is BLOCKED, not fabricated as FAIL or PASS.

## Smallest selection-changing follow-up

A selection decision can change only after exact current-authority evidence is persisted.

The next bounded work is:

1. for PFFDTD, qualify any missing required capability before attempting the corresponding frozen fixture;
2. for MFEM, address the current exact concave FAIL and the radiation negative evidence with bounded experiments without relaxing R100A tolerances;
3. for any shipping candidate, persist exact current typed evidence for Windows packaging/execution, CPU baseline, reproducible authority, and every mandatory adoption fixture;
4. rerun only the missing/changed gate. Existing successful unrelated workflows are not a reason to rerun accepted evidence.

If every mandatory gate for one candidate becomes PASS, the machine report may return `READY`; that still does not silently select a solver. A separate explicit `BakeoffDecision(status='selected', ...)` remains required for actual adoption.

## Verification

Focused tests cover:

- fully qualified PASS;
- missing mandatory evidence;
- explicit FAIL;
- BLOCKED evidence;
- stale R100A authority;
- stale candidate implementation;
- wrong candidate/evidence binding;
- reference-only rejection;
- unsupported required capability;
- license/platform hard-gate failure;
- preservation of negative evidence;
- candidate-addition invariance;
- deterministic report identity;
- save/reopen ledger identity.

Dedicated bounded workflow:

`.github/workflows/r100b-production-adoption-readiness.yml`

The workflow runs only the focused readiness tests and deterministic report generation; it does not execute a large acoustic solver.
