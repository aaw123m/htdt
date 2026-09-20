# R130B Candidate Explicit-Impedance Execution — 2026-09-20

## Scope

R130B extends the existing R130A bounded PFFDTD candidate execution from explicit
`rigid_zero_normal_velocity` boundaries to one independently checked,
explicit physical-impedance subset.

The vertical slice is:

`AcousticSceneSnapshot exact surface boundary refs`
→ `AcousticMaterial specific_impedance_table`
→ `R100B exact Zn/DEF mapping`
→ `pinned PFFDTD material HDF5 + one-sided room-interior material surface`
→ `bounded PFFDTD numerical execution`
→ `immutable complex-pressure artifact/result envelope`.

This is not R130C and it is not a production-solver adoption decision.

## Exact supported impedance domain

R130B is deliberately narrower than the general HTDT material model.

Supported:

- physical quantity: **specific acoustic impedance**;
- unit: **Pa·s/m** (`Pa*s/m` in serialized authority);
- existing `AcousticMaterial.wave_model == specific_impedance_table`;
- explicit resistance/reactance samples on the **exact candidate output frequency grid**;
- finite positive resistance;
- exactly zero reactance;
- exactly frequency-independent resistance across the requested grid;
- explicit valid frequency domain matching the material table endpoints;
- exact density and sound-speed external authority refs;
- exact PFFDTD mapping authority/version;
- exact material and boundary provenance.

Fail-closed / unsupported:

- scalar/statistical absorption as impedance;
- missing phase synthesis;
- reactive impedance;
- frequency-varying resistance;
- arbitrary frequency-dependent/causal fitting;
- unsupported boundary quantities/models;
- missing or modified material/mapping/environment authorities;
- mapping-version mismatch;
- silent fallback to rigid.

Reactive and causal frequency-dependent work remains R130C.

## R100B authority reuse

The physical mapping is not reimplemented in R130B.

`acoustic_pffdtd_impedance_adapter.py` now exposes the same R100B-authorized
frequency-independent, purely resistive mapping as a reusable function. The
existing R100B fixture wrapper calls that function, and R130B calls the same
function from candidate input compilation.

For resistance `Z`, density `rho`, and sound speed `c`:

- `Z_n = Z / (rho*c)`;
- `Y_n = 1/Z_n`;
- PFFDTD `DEF = [[0, Z_n, 0]]`.

No absorption-to-reflection or absorption-to-impedance helper participates.

The pinned PFFDTD implementation remains:

`bsxfun/pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5`.

## Actual PFFDTD boundary representation

R130A previously passed `mat_files_dict={}`, so every surface was rigid.

For an R130B impedance surface the executor now:

1. resolves exact snapshot material/boundary refs;
2. validates `AcousticMaterial` and the R100B-supported impedance subset;
3. resolves exact density, sound-speed and PFFDTD mapping refs;
4. writes PFFDTD's frequency-independent material HDF5 with the pinned
   `write_freq_ind_mat_from_Zn()`;
5. reopens that HDF5 and requires byte-semantic DEF equality with the compiled
   R100B mapping;
6. passes the material filename through `mat_files_dict` to `sim_setup()`;
7. assigns the non-rigid surface to PFFDTD `side=1` after the existing closed
   shell compiler has normalized triangle winding outward.

The `side=1` step is required by pinned PFFDTD: for outward-oriented room
surfaces it activates the negative-normal/back side, i.e. the room-interior
side. `side=0` would be treated as rigid and is therefore prohibited for the
impedance surface.

## Execution identity and stale behavior

Rigid-only execution keeps the R130A authority/compiler versions and omits the
new optional impedance field from semantic serialization. This intentionally
preserves the pre-R130B rigid semantic identity shape.

An impedance execution uses:

- `r130b-candidate-wave-input-1`;
- `htdt.r130b.pffdtd_candidate_impedance_input_compiler` version 2;
- PFFDTD candidate adapter version 2.

The execution-input semantic payload includes exact surface id, material
id/version/hash, boundary id/version/hash, physical impedance, unit/capability,
valid domain, material/boundary provenance, density/sound-speed refs and values,
normalized impedance/DEF, and exact mapping id/version/hash.

Changing the material hash or physical impedance changes the execution-input
semantic identity. There is no implicit candidate result cache; result envelopes
remain bound to their exact dispatch/request/execution chain.

## Immutable artifact provenance

For impedance runs, the complex-pressure artifact and execution provenance
include a `boundary_authority` / `boundary_execution` section with:

- `material_boundary_configuration_sha256`;
- exact serialized boundary bindings;
- PFFDTD material group;
- exact material HDF5 SHA-256;
- exact DEF;
- exact mapping authority ref.

The raw PFFDTD `sim_outs.h5` SHA-256 remains tracked as in R130A.

## Independent normal-incidence verification

The R130B evidence runner uses the same physical R100B canonical relationship
without calling the candidate reflection implementation for the expected value:

`R_expected = (Z - rho*c) / (Z + rho*c)`.

For the bounded fixture:

- `rho = 1.2 kg/m^3`;
- `c = 343.0 m/s`;
- `rho*c = 411.6 Pa*s/m`;
- `Z = 823.2 + j0 Pa*s/m = 2 rho*c`;
- `R_expected = 1/3 + j0`;
- expected magnitude = `1/3`;
- expected phase = `0 deg`.

The candidate side is the pinned PFFDTD `compute_Rf_from_DEF()` evaluated from
the exact DEF used by R130B. The evidence gate checks magnitude and phase using
the frozen R100B tolerances (magnitude absolute tolerance 0.01, phase tolerance
1 degree).

This is an independent closed-form reference for the **boundary mapping/native
reflection function**. It is not a claim that the spatial FDTD total-pressure
field has been incident/reflected decomposed. That decomposition remains
unavailable for the frozen fixture and is not synthesized here.

## Focused acceptance

The dedicated R130B workflow runs only focused R100B/R130B contract tests before
the numerical candidate run. The existing R130A workflow is also triggered by
the shared executor changes and serves as the rigid regression gate.

Required evidence:

- rigid R130A behavior remains PASS;
- explicit impedance uses an actual PFFDTD non-rigid material file;
- material/mapping identity is present in execution identity;
- changed impedance/material identity invalidates the old execution identity;
- unsupported/scalar, reactive, and frequency-dependent quantities fail closed;
- missing/modified material authority fails closed;
- mapping mismatch fails closed;
- normal-incidence magnitude and phase pass the independent reference gate;
- artifact/result provenance traces exact boundary authority;
- exact reopen/tamper rejection remains intact.

GitHub Actions run identifiers and measured evidence are appended after the PR
gate executes.

## Accepted code-head evidence

Accepted implementation head before documentation-only updates:
`41f2a636dbfdde899943a1d8ff9c123e483ec528`.

### R130B actual impedance execution

GitHub Actions **R130B Candidate Explicit Impedance Execution** run
`35490513468` completed **PASS**.

- evidence artifact: `r130b-candidate-impedance-execution`;
- artifact id: `10598962121`;
- artifact ZIP digest:
  `sha256:e58d0bc7f1e7d9a7d1d9981022433ff819d81da563de1e32f9a6306a96e86162`;
- exact candidate input SHA-256:
  `e6cb5edae0b46ac8d6f39d394c304e1728c3cd488eed7c6b7014e6863b8389d7`;
- result SHA-256:
  `ba396d1a0b5a32ae644a773c34b777be7aa901907463fa98c70fc888bfe85d3f`;
- raw PFFDTD `sim_outs.h5` SHA-256:
  `1ae504af4e6f71bd8363a54c384420c355903b89f6083a76546a28c3bcb6487c`;
- material HDF5 SHA-256:
  `5c2c023a83e099ec644467e1d1d983de99a4c6005fc7c25c1593316b01830343`;
- exact packed DEF: `[[0, 2, 0]]`;
- active PFFDTD impedance boundary nodes: **64**;
- physical impedance: `823.2 + j0 Pa*s/m` (float serialization
  `823.1999999999999`);
- exact `rho*c`: `411.6 Pa*s/m` (float serialization
  `411.59999999999997`);
- 40 / 80 Hz candidate native-reflection values:
  `1/3 + j0`;
- maximum complex-reflection error against the independent closed form: **0**;
- maximum reflection-magnitude error: **0**;
- maximum reflection-phase error: **0 deg**;
- missing/modified material authority, mapping version mismatch,
  reactive impedance, frequency-dependent impedance and unsupported/scalar
  input were all rejected in the same evidence run;
- save/reopen exact re-resolution, missing-artifact rejection and
  modified-artifact rejection all passed;
- `production_solver_selected=false`;
- `r130b_numerical_acceptance_completed=false`;
- `owned_room_evidence=false`;
- `rdc_calls=0`.

### R130A rigid regression

GitHub Actions **R130A Candidate Wave Execution** run `35490513423`
completed **PASS** on the same code head.

- evidence artifact id: `10598682669`;
- artifact ZIP digest:
  `sha256:4da203279e19f685e43c78166a78bb829f233dab6aa61af42bc7622805410a48`;
- candidate input SHA-256:
  `6c96902447ebe27ec0491d0c7357076c2c6facc420269926d017774efb2b0c16`;
- result SHA-256:
  `dec0e92dbe41e2e9221b2522a3785afc83b2d1a7f8ed2246c56ebd0a0f242431`;
- raw solver asset SHA-256:
  `32eb07d29b036183ff612fd15d0189ee51db730436ad27b3f6d5cd3fc54803cb`;
- exact save/reopen and fail-closed artifact semantics remained PASS.

### R100B authority regression

GitHub Actions **R100B PFFDTD Impedance Reflection** run
`35490513459` completed **PASS** on the same code head.

- evidence artifact id: `10599330950`;
- artifact ZIP digest:
  `sha256:6051f22f8d3ae1e17de0d8c47c69540fe3240920e87fb49aa52c364914bf58fa`;
- canonical 100 / 200 / 300 Hz reflection:
  `0.33333333333333337 + j0`, magnitude
  `0.33333333333333337`, phase `0 deg` at every sample.

This verifies that extracting the reusable R100B mapping authority did not
change the existing canonical impedance/reflection gate.

## Explicit non-claims

The following remain false:

- `production_solver_selected=false`;
- `r130b_numerical_acceptance_completed=false`;
- `owned_room_evidence=false`.

The conservative R130B acceptance flag remains false because this slice proves
actual bounded impedance execution and the independently checked native
normal-incidence boundary mapping, but does not add a separately qualified
spatial incident/reflected FDTD decomposition or production validation.

## Remaining gates

R130C retains:

- causal frequency-dependent boundary models;
- reactive impedance support where an exact backend representation is justified;
- any explicit fitting/realization policy and its independent validation.

R180 retains owned-room / real-system validation evidence.

Production solver selection remains a separate decision after the required
candidate-wide physics, numerical and real-room evidence gates.

## RDC

RDC calls for this software implementation and automated acceptance: **0**.
