# Competitive Product Research — HTDT 2026-09-19

> Scope: product/UX/capability research only. No implementation in this pass.
> Canonical implementation order remains `docs/IMPLEMENTATION_ROADMAP.md` until a separate plan-review PR promotes any proposal below.
> RDC used: 0.

## 1. Executive conclusion

HTDT already has an unusually strong foundation in three areas:

1. immutable SceneRevision / evidence / provenance;
2. prediction → candidate → measurement plan → holdout validation;
3. planned arbitrary-room hybrid wave + geometrical acoustics solver with fail-closed capability gates.

The largest product gap is **not another solver**. It is the missing continuity between:

```
real room capture
→ theater layout
→ equipment/source definition
→ standards/performance targets
→ acoustic treatment
→ predicted performance
→ guided measurement
→ calibration/handoff
→ variant comparison
→ install/report
→ as-built validation
```

The strongest competing products each cover a different part of this chain:

- Home Theater Maestro (HTM): end-to-end private-cinema workflow and highly integrated UX.
- Home Cinema Design (HCD) / The Cinema Designer (TCD): standards-driven design, equipment data, client/install deliverables.
- Treble: hybrid wave + geometrical acoustics, material/source modeling, auralization and comparison.
- ODEON / CATT / EASE: mature geometry QA, source/directivity handling, spatial maps, reflection analysis and auralization.
- REW: measurement, alignment, EQ and analysis authority.
- Multi-Sub Optimizer (MSO): multi-seat low-frequency optimization of delay/gain/filter parameters.
- Dirac / Trinnov / Genelec GLM: guided calibration, multipoint measurement, timing/phase alignment, target constraints, active control and usable result presentation.

The recommended HTDT direction is therefore:

> **Do not imitate one competitor. Combine HTM/HCD workflow completeness, Treble/ODEON physics workflows, REW/MSO measurement/optimization discipline, and HTDT's stronger evidence/provenance model.**

## 2. Competitor lessons

### 2.1 Home Theater Maestro — workflow completeness

HTM currently exposes a very broad private-cinema workflow:

- non-rectangular rooms, attics, sloped ceilings, beams, risers and open spaces;
- up to 32 channels, multiple immersive formats, per-speaker/group aiming and 3D coverage;
- subwoofer layouts, placement comparison and an optimizer;
- projector throw/zoom/lens-shift and screen/viewing geometry;
- layer-by-layer acoustic treatment, absorber/diffuser design and treatment placement;
- room modes, RT60, first reflections, SBIR, ETC, per-seat maps;
- REW / Audyssey import, waterfall, measured RT60, group delay, symmetry and predicted-vs-measured checks;
- target curves / PEQ suggestions and calibration export;
- compliance, cable plans, order lists and PDF reporting;
- 3D import/export, project variants, restore points, search and guided onboarding.

The main HTDT lesson is not to reproduce every feature. It is that a home-theater tool becomes easy to understand when geometry, speakers, seats, acoustics, measurement and deliverables are all part of one project and each phase has a clear outcome.

References:
- https://hometheatermaestro.fr/en/features.html
- https://hometheatermaestro.fr/en/index.html
- https://hometheatermaestro.fr/en/guides.html

### 2.2 Home Cinema Design / The Cinema Designer — standards and deliverables

HCD is useful because it makes every calculated design number traceable to a published reference. It exposes:

- CEDIA/CTA RP22-oriented performance criteria;
- speaker position and angle compliance;
- sound-pressure capability and seat-to-seat variation;
- CLF/CF2 loudspeaker directivity data;
- viewing angle, brightness and sightline design;
- equipment catalogue provenance;
- bill of materials / quotation / documentation.

A particularly useful design principle is **"where the documents are silent, so is the tool."** HTDT's evidence-first architecture is compatible with this and can go further by versioning the standard/criterion used for each result.

TCD reinforces the value of:
- fast technical room design;
- equipment/manufacturer data;
- client-ready documentation;
- exported 3D CAD.

References:
- https://homecinema.design/
- https://homecinema.design/dieu-khoan
- https://thecinemadesigner.com/
- https://thecinemadesigner.com/about-tcd/

### 2.3 CEDIA / Dolby — explicit design criteria

CEDIA RP22 defines four performance levels and 21 parameters rather than vague "good/better/best" labels. Public CEDIA material explicitly mentions speaker layout, SPL capability, seat-to-seat variation and engineering verification.

Dolby publishes placement guidance and room-design tools for immersive layouts.

HTDT should model standards as **versioned evaluation profiles**, not hard-coded permanent truth.

References:
- https://cedia.org/smart-home-professionals/advocacy/standards-best-practices/immersive-audio-design-excellence/
- https://professionalsupport.dolby.com/s/article/Dolby-Atmos-Home-Entertainment-Studio-Technical-Guidelines
- https://professionalsupport.dolby.com/s/article/How-to-Design-a-Dolby-Atmos-Mix-Room

### 2.4 Treble — hybrid physics, source/material authority and auralization

Treble is the closest public reference for the architecture already planned in Issue #101:

- wave-based low/mid-frequency solver + geometrical acoustics;
- explicit hybridization;
- custom source directivity and CLF import;
- material import from absorption, impedance or reflection data;
- material construction builder;
- binaural / Ambisonic auralization;
- comparison of multiple simulations.

Relevant lesson: physical-model sophistication must be paired with **usable source/material import, geometry preparation, comparison and listening workflows**.

References:
- https://docs.treble.tech/intro
- https://docs.treble.tech/user-guide/simulations/simulation_settings
- https://docs.treble.tech/hybridization
- https://docs.treble.tech/user-guide/simulations/adding_sources_and_receivers/adding_and_editing_sources
- https://docs.treble.tech/treble-sdk/materials/import-material
- https://docs.treble.tech/user-guide/auralizer/auralizing_your_space

### 2.5 ODEON / CATT / EASE — geometry QA and spatial analysis

ODEON exposes several mature workflows that HTDT should treat as product references:

- broad CAD/3D import;
- automatic detection of warped/overlapping surfaces;
- hole/lost-ray diagnostics;
- patch/repair tools;
- direct-sound and room-acoustic color maps;
- first/second-order reflector coverage;
- dynamic ray display;
- source directivity;
- auralization.

CATT emphasizes full room impulse responses, multiple GA algorithms, optional diffraction and real-time walkthrough auralization.

EASE reinforces the value of:
- 3D model import/creation;
- standardized loudspeaker databases;
- material management;
- receiver mapping;
- ray-tracing / reflection analysis;
- auralization.

References:
- https://odeon.dk/product/features/
- https://catt.se/TUCT/TUCToverview.html
- https://catt.se/auralization.htm
- https://afmg.jtesori.com/all_products/ease-5_top/ease-5_daihyou

### 2.6 REW — measurement and alignment authority

REW remains the natural measurement authority rather than something HTDT should replace.

Relevant functions:
- alignment tool with gain/delay/polarity and phase/impulse modes;
- EQ targets and room curves;
- distortion and time/frequency analysis;
- API access to measurements/alignment.

HTDT should orchestrate, validate and bind REW evidence to the digital twin rather than building a second measurement engine.

References:
- https://www.roomeqwizard.com/help/help/html/api.html
- https://www.roomeqwizard.com/help/help_en-GB/html/graph_allspl.html
- https://www.roomeqwizard.com/help/help_en-GB/html/eqsettings.html

### 2.7 Multi-Sub Optimizer — multi-seat low-frequency optimization

MSO demonstrates an important capability that HTDT's spatial optimization should eventually cover:

- optimize multiple listening positions;
- reduce seat-to-seat variation;
- optimize delay, gain, polarity, all-pass and PEQ;
- trade off MLP flatness, spatial consistency and SPL;
- use target curves;
- export filter parameters;
- inspect multiple alternative solutions.

HTDT's Pareto model is a better fit than collapsing this into one opaque score. The missing piece is to expand the decision variables beyond physical position into **signal-path parameters** when the measurement and hardware authority permits it.

References:
- https://www.andyc.diy-audio-engineering.org/mso/html/
- https://www.andyc.diy-audio-engineering.org/mso/html/reference-manual/optim_options.html
- https://www.andyc.diy-audio-engineering.org/mso/html/tech-topics/error-calculations.html

### 2.8 Dirac / Trinnov / Genelec GLM — guided calibration and active control

Dirac:
- room correction;
- timing/phase correction;
- multi-subwoofer spatial bass optimization;
- ART multi-speaker active control / decay management.

Trinnov:
- 3D speaker localization;
- multipoint measurement;
- amplitude/phase/impulse/group-delay views;
- target curves and correction excursion/safety limits;
- early-reflection vs late-energy processing;
- WaveForming / multiple-source low-frequency control.

Genelec GLM:
- guided calibration;
- unlimited measurement positions;
- level, time-of-flight and room-response correction;
- subwoofer phase alignment;
- accessible acoustic reports.

The important lesson is **guided calibration with explicit limits and before/after evidence**, not that HTDT should become a real-time DSP platform immediately.

References:
- https://www.dirac.com/products/room-correction
- https://www.dirac.com/products/bass-control
- https://www.dirac.com/products/art
- https://www.trinnov.com/en/technologies/active-acoustics/optimizer/
- https://www.trinnov.com/en/technologies/active-acoustics/waveforming/
- https://www.genelec.com/glm

## 3. High-priority additions to HTDT

### A. Acoustic geometry import + semantic repair assistant

**Priority: very high**

Current HTDT CAD is useful for manual modeling but does not yet offer the import/repair workflow that mature acoustics tools rely on.

Add:

- OBJ/GLB first; IFC/STEP/3DM only later if justified;
- phone-scan / photogrammetry import as raw visual geometry;
- automatic detection of open boundaries, duplicate/overlapping faces, inverted normals, non-manifold edges, tiny/sliver surfaces and thin features below acoustic resolution;
- watertightness/readiness analysis for the wave solver;
- ray-leak / portal diagnostics for GA;
- guided repair suggestions;
- explicit conversion from **raw visual mesh → semantic acoustic geometry**.

Do **not** let an arbitrary imported mesh become solver authority directly.

Keep:
- original import asset/hash;
- repair actions;
- semantic surface IDs;
- acoustic compiler version;
- approximation warnings.

This should integrate with R120, not become an independent mesh truth.

### B. Rich room construction primitives

**Priority: very high**

Issue #101 handles arbitrary 3D solver geometry, but the editor still needs user-friendly semantics for real private rooms:

- sloped ceilings;
- beams / soffits;
- risers / tiered platforms;
- partial-height partitions;
- alcoves;
- open-plan adjacent regions;
- screen wall / baffle wall;
- doors/openings with explicit neighboring region or termination.

This is more valuable than adding generic free-form CAD operations.

### C. EquipmentDefinition + Directivity import authority

**Priority: very high**

R110 already plans source-directivity capability tiers. It should be made usable with import adapters and equipment records.

Add:
- CLF/CF2 import;
- SOFA/AES69 where applicable;
- CTA-2034 / spinorama-like summary import for magnitude-only use;
- raw polar/orbit import;
- user-defined analytic source fallback;
- manufacturer/data-source/version/hash;
- measured vs inferred vs approximate badges;
- angular coverage / interpolation / valid band;
- sensitivity/reference level;
- physical cabinet dimensions;
- optional max SPL / power / distortion-related data when documented;
- mounting context and port/clearance metadata.

Do not build a huge manually maintained internet product catalogue first. Build the authority/import layer, then curate only the equipment actually used.

### D. Coverage + SPL/headroom + worst-seat performance

**Priority: very high**

Current HTDT optimization is acoustically sophisticated, but a home-theater design also needs to answer:

- Is every seat inside useful directivity?
- Is target SPL/headroom achievable at the worst seat?
- Which seat is limiting the design?
- Is seat-to-seat spread acceptable?
- Does aim improve one seat while harming another?

Add:
- per-seat directivity loss;
- coverage map;
- direct/direct+validated-room level distinction;
- peak/continuous capability where source data supports it;
- amplifier/headroom constraints where data exists;
- worst-seat and percentile objectives;
- explicit "unknown" when equipment data is insufficient.

Do not replace Pareto with one "cinema score".

### E. StandardsProfile / compliance evaluator

**Priority: high**

Add a versioned evaluation layer for:
- CEDIA/CTA RP22;
- Dolby layout guidance;
- DTS:X / Auro-3D where public/usable criteria exist;
- future HTDT custom criteria.

Each criterion should store:
- standard/profile;
- version;
- clause/reference;
- required inputs;
- observed value;
- pass / fail / unknown / not-applicable;
- whether result is design-predicted or measurement-verified.

Important: standards must not mutate old evidence. A saved project evaluated under one revision stays reproducible; a newer profile creates a new evaluation.

### F. AcousticTreatment as a first-class entity

**Priority: high**

Current material authority is physically careful, but the product needs a user-facing treatment model.

Add:
- absorber panel;
- porous absorber + air gap;
- membrane/panel absorber;
- perforated/slotted absorber;
- bass trap;
- diffuser / scattering element;
- hybrid absorber/diffuser;
- acoustic screen fabric / stretched fabric;
- treatment placement/orientation/coverage.

Separate:
- surface base construction;
- attached treatment;
- furniture equivalent absorption;
- source-path transmission loss.

A treatment assembly should retain:
- layer stack;
- thickness/air gap/density/perforation parameters;
- model used;
- measured/inferred status;
- valid band;
- uncertainty;
- build dimensions.

Then allow:
- predicted before/after;
- installed/not-installed state;
- treatment quantity/cut list;
- treatment-location optimization after physics gates pass.

### G. Guided measurement session + MeasurementQualityReport

**Priority: high**

HTDT already has MeasurementPlan and REW integration, but the user workflow should become more like a guided calibration system.

Add:
- measurement sequence by speaker/seat;
- 3D mic-position guidance;
- automatic mapping proposal from REW names;
- clipping check;
- SNR/noise-floor check;
- truncation / usable IR window;
- usable frequency band;
- timing-reference capability;
- polarity confidence;
- calibration-file provenance;
- retake recommendation;
- completion state per planned measurement.

The quality report should gate downstream claims. A low-quality measurement is not merely a warning; it should disable unsupported phase/decay/calibration operations.

### H. CalibrationPlan and device-neutral export

**Priority: high, after measurement capability is stable**

Do not initially build a competing Dirac/YPAO/Trinnov real-time correction engine.

Instead produce a device-neutral plan:

- channel level;
- distance/delay;
- polarity;
- crossover;
- target curve;
- PEQ;
- optional all-pass where authority exists;
- max boost/cut and safety limits;
- predicted response after settings;
- expected uncertainty;
- measurement verification checklist.

Export adapters can then target:
- REW-compatible filters;
- generic biquads;
- miniDSP-like formats;
- human-readable AVR settings;
- future device-specific adapters only when stable/legal.

This is the cleanest bridge from HTDT's optimization to the real system.

### I. Named design variants / comparison sets

**Priority: high**

SceneRevision already gives HTDT a stronger base than many competitors, but the product should expose a user-facing concept of named alternatives.

Examples:
- "Current";
- "Seat 20 cm forward";
- "Speaker toe-in";
- "Treatment A";
- "Treatment B";
- "Candidate 07".

A comparison set should preserve:
- exact SceneRevision;
- model/evidence;
- plots;
- camera/view preset;
- key objective deltas;
- measurement status.

This is different from Undo history and different from optimizer candidates. It is a decision-making object.

### J. Design / As-built / Measured state distinction

**Priority: high**

HTDT should explicitly distinguish:
- design intent;
- installed/as-built geometry and equipment;
- measured state;
- proposed variant.

This makes the digital twin more credible after construction.

Useful behavior:
- mark planned vs measured positions;
- store installation deviations;
- show when a measured state no longer matches the design revision;
- support "update as-built from measured dimensions" without rewriting old evidence.

### K. Robust optimization / tolerance analysis

**Status: promoted to canonical O90 plan / high-value differentiator**

Detailed authority: [O90 Robust Optimization](O90_ROBUST_OPTIMIZATION.md)

Competitors generally present exact geometry/equipment as if installation were exact. HCD itself notes that installation variance and equipment tolerances cause real measurements to diverge.

O90 formally supports a staged path to uncertainty/tolerance envelopes:

- speaker position ±x cm;
- aim ±x°;
- seat location range;
- material uncertainty;
- source sensitivity/directivity uncertainty;
- temperature/environment range.

Optimization can then rank candidates by:
- nominal performance;
- worst-case / percentile performance;
- sensitivity to realistic installation error.

This is a stronger engineering feature than another opaque "best placement" score.

## 4. Medium-priority additions

### L. Auralization

After R160 produces validated spatial/hybrid impulse-response capability:

- convolve user-selected anechoic program material;
- binaural output via versioned HRTF/SOFA;
- receiver/head orientation;
- A/B variants;
- explicit valid-band/provenance;
- never synthesize missing coherent phase.

This would make prediction differences perceptually accessible, but must remain downstream of solver validity.

### M. Projector / screen / sightline geometry

HTDT already models screen/furniture but not projector optics.

Useful additions:
- projector entity;
- throw ratio;
- zoom;
- lens shift;
- projection cone;
- image size/aspect ratio;
- vertical/horizontal viewing angle;
- seat sightlines;
- screen frame clearance;
- acoustically-transparent screen effects;
- speaker/screen collision constraints.

This belongs in the digital twin because it directly constrains speaker and seat geometry.

### N. Build/install output

For personal use, avoid CRM/quotation complexity, but add:
- top/front/side dimension sheets;
- speaker coordinates and angles;
- measurement-point coordinates;
- treatment dimensions;
- cable-length estimates;
- equipment/treatment list;
- project summary PDF/HTML;
- CSV export for angles/settings.

This converts the digital twin into something usable during installation.

### O. Background-noise / noise-floor performance

Do not simulate HVAC from first principles.

Instead:
- import/measure background noise;
- optional NC/NCB-style evaluation;
- projector/PC/AVR fan-noise notes;
- use measured noise floor to determine usable dynamic range and measurement SNR.

### P. Interactive SBIR / reflection diagnosis

Once R150 path authority exists:
- select speaker + boundary;
- drag position and show path distance / expected notch / arrival delay;
- first-reflection target zones;
- treatment panel useful-size/coverage;
- explain when a null cannot be fixed by EQ.

This is a high-value user-facing interpretation of the solver rather than another raw graph.

## 5. Future / research-only capabilities

### Q. Joint position + DSP optimization

Extend O-series decision variables from geometry into:
- subwoofer delay;
- gain;
- polarity;
- PEQ;
- all-pass;
- crossover.

Maintain separate objectives:
- MLP target error;
- seat-to-seat variation;
- SPL/headroom;
- filter complexity;
- boost/excursion penalty;
- robustness.

MSO is the reference product for this class of optimization.

### R. Multi-speaker active control / MIMO

Dirac ART and Trinnov WaveForming show the value of using multiple sources cooperatively to control modal decay.

This is a valid future research track, but **not an immediate HTDT product feature**. It depends on:
- validated complex transfer matrices;
- enough independently controllable channels;
- real hardware constraints;
- low-latency DSP execution;
- safety/headroom models.

Keep architecture open for it, but do not let it distract R100/R180.

## 6. Existing HTDT functions that should be corrected or expanded

### 6.1 Measurement workspace: change from "import and plot" to "measurement campaign"

Current functionality centers on import/FR/A-B. The target should be:
- plan;
- measure;
- quality-check;
- assign;
- compare;
- retake;
- validate.

### 6.2 Optimization: expose worst-seat / robustness, not only candidate metrics

Keep objective vectors and Pareto. Add:
- worst-seat;
- percentile;
- sensitivity;
- robustness/tolerance;
- signal-path decision variables later.

Never replace this with a single opaque recommendation score.

### 6.3 Prediction UI: prioritize explanation over solver detail

Normal view should answer:
- what changed;
- where;
- why;
- how reliable;
- what action could improve it.

Solver/backend/mesh/hash remain Advanced/provenance.

### 6.4 Material UI: split "surface material" from "treatment assembly"

A wall finish, a sofa, a porous panel and a membrane trap are not the same semantic object even if all ultimately affect absorption.

### 6.5 Speaker model: split five independent concerns

Keep separate:
1. physical cabinet;
2. acoustic source/reference point;
3. directivity;
4. mounting context/baffle/port clearance;
5. signal path / crossover / delay / EQ.

Do not let one "speaker preset" hide all five.

### 6.6 Geometry model: visual mesh is not acoustic geometry

Imported/rendered meshes need semantic conversion and solver-readiness diagnostics.

### 6.7 Results: make scenario comparison a first-class workflow

The main decision unit should be "A vs B vs measured", not an isolated result graph.

### 6.8 Overview: extend readiness to the whole lifecycle

In addition to current blockers, Overview should eventually identify:
- geometry not solver-ready;
- source directivity missing;
- equipment SPL data missing;
- measurement quality insufficient;
- standard criterion unknown;
- design differs from as-built;
- prediction not validated;
- treatment not installed;
- calibration not re-measured.

## 7. Features HTDT should deliberately *not* prioritize

### Full video color calibration

HTM includes ColorHCFR workflows, but this is a separate measurement domain. Screen/projector geometry is relevant; full SDR/HDR color calibration should wait until the audio digital twin is mature.

### CRM / quoting / client management

TCD/HCD are installer-business tools. HTDT is currently a personal engineering application. BOM/report output is useful; CRM/client billing is not.

### Cloud collaboration / cross-platform mobile

HTM/HCD/Treble pursue multi-platform/cloud collaboration. HTDT's local Windows architecture is not a product weakness for the current use case. Do not add cloud complexity without a concrete need.

### Huge vendor catalogue

The important requirement is a robust EquipmentDefinition/import authority. A large continuously maintained commercial catalogue would create data-governance work unrelated to core physics.

### Proprietary calibration file rewriting

Audyssey/other vendor export can be useful, but should not become a dependency. Prefer device-neutral CalibrationPlan + stable adapters.

### Real-time active correction engine now

Do not attempt to reproduce Dirac/Trinnov before the forward model, measurement capability and hardware execution are validated.

## 8. Proposed priority order

### P0 — fix product foundations before adding more visible complexity

1. finish Issue #118 UX shell;
2. MeasurementQualityReport + guided measurement campaign;
3. EquipmentDefinition/directivity import authority;
4. acoustic geometry readiness/repair diagnostics;
5. named comparison sets;
6. StandardsProfile authority.

### P1 — make the digital twin useful for design decisions

7. coverage + SPL/headroom + worst-seat analysis;
8. first-class acoustic treatment assemblies;
9. richer room primitives + 3D import path;
10. design/as-built/measured state model;
11. calibration plan + device-neutral export;
12. robust/tolerance optimization — **promoted to O90 canonical milestone**.

### P2 — make it complete as a home-theater design tool

13. projector/screen/sightline geometry;
14. installation/report output;
15. background-noise evaluation;
16. interactive SBIR/reflection workflow.

### P3 — after R160/R180 evidence exists

17. auralization;
18. joint position + DSP optimization;
19. active multi-speaker / MIMO research.

## 9. Architectural guardrails

All new features should preserve existing HTDT strengths:

- no single hidden "quality score";
- no measurement result detached from SceneRevision/AcquisitionContext;
- no inferred directivity/material/impedance presented as measured;
- no standards criterion without source/version;
- no calibration suggestion outside device/headroom constraints;
- no imported mesh silently treated as solver-ready;
- no auralization outside valid coherent/spatial IR capability;
- no optimized candidate promoted to owned-room recommendation without measured validation;
- no GUI expansion by adding permanent docks to the old shell.

## 10. Product positioning after these changes

The target identity should be:

> **HTDT = home-theater CAD digital twin + standards-aware system design + verified hybrid acoustics + guided measurement/calibration + robust multi-objective optimization + evidence/provenance.**

That is materially different from:
- HTM: broad and polished end-to-end workshop;
- Treble/ODEON: general acoustic simulation;
- REW/MSO: measurement/DSP optimization;
- Dirac/Trinnov: correction processors;
- TCD/HCD: professional design/proposal tools.

The differentiator is not "more features than HTM." It is **a closed, evidence-bearing loop from a physical room model to validated decisions, while remaining understandable to a home-theater user.**
