# Issue #836 Action 2-6 remainder — native solver prediction binding for BRAS RS8_01a

## Status

**Investigation complete. Docs-only outcome: no additional fixture observable is evaluable by an HTDT-native solver today without new machinery or new physics.**

Review date: 2026-10-09

Related:
- #836 external qualification program (Action 5: R150 external qualification)
- #948 BRAS external measurement harness (landed in PR #1036)
- #809 external qualification for R130/R150
- `docs/issues/issue-948-bras-external-measurement-harness.md` (fixture authority, honest limits)

This document records the required solver-binding investigation for the imported
`bras-rs8-01a` sealed fixture: which declared observable points can be fed by an
existing native solver output **without new physics**, which cannot, and the
precise blockers. Per the dispatch rules, nothing was fabricated: verdict
vocabulary is unchanged, no thresholds were weakened, preregistered vs informed
separation is untouched, and honest `unobservable`/`unsupported`/`missing`
states remain exactly that.

## What exists today (landed in #1036)

`backend/src/htdt/cad_external_benchmark_fixture.py` binds the sealed
`ExternalBenchmarkFixture` (`fixture_sha256=7bd2acf6…`), imports the BRAS
RS8_01a SRIR SOFA (`RS8_RIRs_01a.sofa`, member sha `d92118d9…`), and evaluates
one preregistered observable:

- `rs8-01a-m150-direct-arrival` — `arrival_timing`, S1→R151, reference
  `arrival_s=0.007891156462585034` (derivation `peak`), tolerance 0.0003 s —
  **pass** via `AnalyticDirectPathProvider` under
  `spec_id=rs8-01a-m150-arrival` (`run_mode=preregistered_unfitted`,
  `solver_path=analytic-direct-path` rev73).

That analytic provider is a measurement-side reference model (`arrival_s =
distance / c` on pinned positions), **not** an HTDT solver. The Action 2-6
remainder asked for solver-side predictions so *additional* declared
observables could be evaluated.

## Fixture scene recap (what a solver would have to run)

- Anechoic / free-field environment per dataset comment (`temperature_c=20.0`).
- Source S1 (index 0) at (4.0, 0, 1.35); 351 receiver positions (R151 = index 150
  at (1.35, 0, 1.35)); `unit_semantics=pascal_calibrated`,
  `phase_authority=coherent_phase`, `valid_band_hz=[100, 4000]`.
- Geometry: single curved finite reflector plate
  (`RS8_CurvedReflector_solid.stl`, closed solid STL — free-standing object,
  not a room boundary).
- Bound payloads: `2_Source_descriptions.zip` (`Genelec8331A_1x1_64442.sofa`
  measured source directivity — advisory pin, not consumed) and
  `3_Surface_descriptions.zip` (`mat_MDF25mmC_*` absorption/scattering/complex
  impedance CSVs — consumed as `boundary_material` pin).

All four payload files were fetched locally under
`scripts/fetch_external_corpus.py` during this investigation and verified
against the manifest SHA-256 pins (4/4 verified); the fixture's consumed pins
are unchanged by this document.

## Seams investigated (canonical entry points only)

| Seam | Entry point | Verdict for this fixture |
|---|---|---|
| R150 deterministic GA | `execute_deterministic_ga` + `compile_deterministic_ga_execution_input` in `cad_geometric_acoustics_adapter.py` | **Blocked** — requires the full sealed scene-authority chain (`AcousticSceneSnapshot`, `AcousticPredictionRequest`, READY `AcousticSolverDispatchBinding`, adapter descriptor, `R120CompiledGeometry` from `compile_r120_geometry` over a `SceneRevision`/`SemanticAcousticGeometry`, region/portal/boundary-termination authorities). Room policies are closed-shell only (`exact_axis_aligned_closed_shoebox_v1`, `general_planar_closed_polyhedral_v1` ≤2nd order, `general_planar_multi_region_portal_v1`); an anechoic free-field scene has no closed manifold boundary shell to express. The curved reflector is at most an *occluder* (blocks, never reflects); non-planar surfaces are rejected for specular. Per-source `DirectivityDataset` is mandatory and raises if absent. |
| R130 PFFDTD lane | `acoustic_pffdtd_adapter.py` | **Not a lane for this scene** — bound to the R100A/B closed-rigid single-region fixture compiler and a pinned upstream `pffdtd` checkout (not provisioned); RS8_01a is an open free-field scene, not a closed rigid region. |
| Low-band / app prediction | `room_prediction.py`, `LowBandPredictionProvider`s | Requires the full snapshot/persistence scene stack over an HTDT project scene — not expressible from fixture positions alone. |
| Direct-level evaluation | `cad_direct_level.py` | Consumer seat-SPL authority (`DistanceLevelAuthority`, `PlaybackExcitationScenario`, headroom). Outputs dB SPL per seat — not a transfer/impulse-domain observable; wrong authority for `pascal_calibrated` fixture quantities. |
| Legacy acoustics helpers | `acoustics.py` (`first_order_reflections`, `analyze_rectangular_context`) | Rectangular-room-only analytic helpers; no free-field/plate support. |
| Source directivity import | `cad_directivity.py` `NormalizedDirectivityJsonV1` + `cad_directivity_admission.py` | **Adapter absent** — `NormalizedDirectivityJsonV1.parse` explicitly notes "native CLF/CF2/SOFA/AES69 require dedicated adapters". The BRAS `Genelec8331A_1x1_64442.sofa` cannot become a sealed `DirectivityDataset` (which additionally requires an `EquipmentDefinition` binding, provenance, and a complete freq×angle lattice) without a new SOFA adapter. The Aalto corpus in `cad_directivity_admission` does contain a calibrated *Genelec 8331A* SOFA, but it is a different measurement of the same loudspeaker model — using it as the BRAS source authority would be an input-authority substitution, not the pinned payload. |
| Plate material import | `3_Surface_descriptions.zip` CSVs → `GeometricMaterialAuthority`/`AcousticMaterial` | CSV→authority binding machinery exists in principle, but the plate can never reach GA specular evaluation (non-planar free-standing surface), so a material binding would feed nothing. |

## Per-observable-kind verdict

| Observable kind | Evaluable today? | Blocker |
|---|---|---|
| `arrival_timing` (direct path, M150 and any unobstructed receiver) | Yes — **already evaluated** via the analytic provider (pass). A GA direct-path run would emit the identical physical quantity (`propagation_delay_s = length/c`). | GA binding blocked anyway (above); zero net-new evaluable observables. For receivers *behind* the plate, GA occlusion could honestly report "no direct path" where the analytic provider still returns d/c — but reaching that requires the full fabricated scene chain plus the absent SOFA→directivity adapter. |
| `arrival_timing` (plate reflection) | No — `unsupported` | GA specular needs planar room-boundary surfaces inside a closed shell; the reflector is curved and free-standing → `UNSUPPORTED_GEOMETRY`. Diffraction/scattering around the finite plate is outside deterministic GA entirely. |
| `magnitude_fr` / level | No — `unsupported` | GA band quantity is `relative_energy_transport_per_m2` (1/d² spreading × directivity energy) — relative, **not** calibrated Pa; the fixture is `pascal_calibrated`. Absolute level would need a calibrated source-strength/excitation authority that the fixture does not bind. |
| `complex_transfer` | No — `unsupported` | GA emits `coherent_phase=UNAVAILABLE_NOT_SYNTHESIZED`; the fixture declares `phase_authority=coherent_phase`. |
| `impulse_window` / `decay_metric` | No — `missing`/`unsupported` | No native solver emits an impulse-domain output for this scene (GA paths carry arrivals + relative band energies only; synthesizing an impulse response from them would be invented physics). |
| SPL coverage-style observables | n/a | Not a declared fixture observable kind; `cad_direct_level` authority does not apply. |

## Net result

**No additional fixture observable becomes evaluable today.** The single
evaluated metric remains the #1036 arrival-time pass. The verdict vocabulary
and the sealed fixture/spec/evidence artifacts are unchanged; the honest
outcome is this documented blocker list rather than a fabricated binding.

Note on scope: the *existing* analytic provider could in principle be asked to
predict `arrival_timing` for the other 350 receiver rows, but that is not
solver evidence and is deliberately not claimed here — it adds no new
capability and risks inflating pass counts without new physics.

## What unlocks each blocked observable (prerequisite roadmap)

Ordered by leverage; each item is a separable, canonical addition — none exists
today:

1. **SOFA/AES69 → `NormalizedDirectivityJsonV1` adapter** for
   `cad_directivity.py` (admitting the pinned `Genelec8331A_1x1_64442.sofa`
   into a sealed `DirectivityDataset` with an honest `EquipmentDefinition`
   binding). Prerequisite for *any* GA run: `execute_deterministic_ga`
   rejects a source without a directivity dataset.
2. **Fixture→sealed-scene geometry importer** able to express an
   anechoic/free-field scene (or a closed-shell approximation honestly
   declared as such) in `SemanticAcousticGeometry`/`SceneRevision` terms so
   `compile_r120_geometry` + `compile_deterministic_ga_execution_input` can
   bind it. Today no canonical lane models a boundary-free acoustic region.
3. **Planar-facet specular support for finite free-standing surfaces**
   (panel-as-reflector, not room-boundary) in deterministic GA — would unlock
   the RS8 plate-reflection arrival at the plate's planar approximation;
   curvature handling is the declared RS8 campaign question itself.
   Diffraction remains out of GA scope.
4. **Calibrated source-strength authority** (excitation/sensitivity binding)
   feeding absolute level — required before `relative_energy_transport_per_m2`
   can be compared against `pascal_calibrated` measured quantities without
   conflation.
5. **Wave-solver lane for this scene class** (PFFDTD or equivalent) with a
   free-field/open-domain compile path — the only route to `complex_transfer`,
   `impulse_window`, `decay_metric`, and curvature-true RS8 reflections.

Until at least (1) and (2) exist, the R150 external-qualification step for
RS8_01a (issue #836 Action 5, item 7 "RS8 faceting/curvature campaign") stays
blocked at the direct-arrival verdict already on record.
