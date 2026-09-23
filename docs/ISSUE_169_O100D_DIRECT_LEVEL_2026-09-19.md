# Issue #169 O100D — direct/equipment-derived SPL and headroom slice

Status: implemented on `issue-169-o100d-direct-level`.

This slice intentionally excludes the coverage/directivity-map work. It consumes the
merged O100C `EquipmentDefinition` authority and the merged direction-aware
`ObjectiveDefinition` authority without changing either schema.

## Direct versus room-assisted level

Every result in this slice is explicitly
`direct_equipment_derived_no_room_gain_no_reflections`.

The evaluator uses only:

- the exact source position and equipment acoustic reference point;
- the exact receiver/seat acoustic reference position;
- an evidenced sensitivity/reference level or evidenced continuous/peak SPL capability;
- an explicit playback/excitation scenario;
- the versioned distance and input-normalization authorities below.

It does **not** add room gain, reflected energy, boundary loading, directivity/off-axis
loss, or room-solver output. These values are therefore not a substitute for a future
validated room-assisted SPL quantity.

Multi-channel coherent summation is not inferred. A scenario selects exactly one source
entity/channel role and declares `single_channel_no_coherent_sum`. Per-channel SPL
values are never arithmetically added to manufacture a system SPL value.

## Playback/excitation authority

`PlaybackExcitationScenario` is immutable and semantic-hash identified. Its identity
includes:

- source entity and channel role;
- reference input quantity and value: RMS voltage or power;
- target SPL and an explicit target reference-condition description;
- separate continuous and peak reference durations;
- frequency band;
- weighting;
- exact receiver population identity and ordered seat IDs;
- receiver-reference semantics;
- aggregation semantics;
- direct-level semantics;
- distance model ID/version/equation;
- input-normalization model ID/version/equations.

Changing any of these changes the scenario SHA-256 and therefore the formal
`ObjectiveDefinition` comparison identity. Results generated under different excitation
or reference conditions do not silently compare.

Voltage and power references are not converted into each other. A sensitivity expressed
at a voltage reference can be evaluated only under a voltage scenario, and a power
reference only under a power scenario, unless a future authority supplies the missing
electrical conversion evidence.

## Versioned physical equations

The direct-distance authority is:

- model: `free-field-spherical-pressure-decay`
- version: `20log10-distance-ratio-1`
- equation:
  `level_at_r = level_at_ref + 20 log10(reference_distance_m / r_m)`

The matched-reference input authority is:

- model: `matched-reference-input-scaling`
- version: `voltage20-power10-log-ratio-1`
- voltage:
  `delta_dB = 20 log10(V_rms / V_reference_rms)`
- power:
  `delta_dB = 10 log10(P_W / P_reference_W)`

These assumptions are first-class scenario data. They are not hidden inside an
unversioned inverse-square implementation.

## Per-seat direct level and target margin

For each explicitly identified seat, the receiver position must come from the existing
scene acoustic-reference authority. The implementation does not invent an ear offset
from seat geometry.

When sensitivity evidence is eligible, per-seat direct SPL is the sensitivity reference
level plus the matched-input adjustment plus the versioned distance adjustment.

Target margin is:

`direct/equipment-derived SPL at seat - target SPL`

The aggregate worst-seat direct level and worst-seat target margin are the minimum
available value across the exact seat population. Seat-to-seat direct-level spread is the
maximum minus minimum per-seat direct level and requires at least two seats.

## Continuous and peak headroom

Continuous and peak capability are kept separate.

For each mode, the evaluator requires:

- the matching O100C continuous or peak SPL datum;
- an explicit capability reference distance;
- an explicit valid frequency domain;
- an explicit duration matching the scenario's corresponding duration;
- a weighting condition that is actually supported by the available O100C capability
  evidence.

The per-seat capability is distance-normalized from the O100C reference distance.
Headroom margin is then:

`direct/equipment-derived capability at seat - target SPL`

A peak value is never substituted for continuous capability, and continuous capability is
never substituted for peak. The existing O100C declared-headroom field is not used to
invent a missing continuous or peak SPL datum.

## Frequency and weighting semantics

A requested frequency band must be wholly inside the relevant O100C valid-frequency
domain. A request outside that domain is `unsupported`.

If an evidenced sensitivity has no weighting declaration, this slice accepts only the
explicit `unweighted` scenario. If it has a weighting declaration, that declaration must
match the scenario.

O100C `SplCapability` currently has no separate weighting field. This slice therefore
uses it only for `unweighted` scenarios rather than inventing weighting provenance.

## Missing and unsupported results

No missing capability is replaced by 0, `-inf`, `+inf`, or another optimization
sentinel.

Representative fail-closed states include:

- absent sensitivity/reference evidence -> `missing` direct SPL and target margin;
- absent continuous/peak SPL datum -> corresponding headroom is `missing`;
- absent valid-frequency authority -> `missing`;
- frequency band outside the valid domain -> `unsupported`;
- voltage/power reference-kind mismatch -> `unsupported`;
- weighting mismatch -> `unsupported`;
- duration mismatch -> `unsupported`;
- missing/non-seat receiver or missing explicit seat acoustic reference -> `unsupported`.

Worst-seat aggregate values fail closed when any member of the exact population is
missing or unsupported.

## ObjectiveDefinition conversion

The physical O100D evidence is converted to the existing #200 objective authority.
There is no hidden loss transform and no aggregate cinema score.

The slice emits:

| Objective | Direction |
| --- | --- |
| `o100d.direct_level.worst_seat_db_spl` | maximize |
| `o100d.direct_level.seat_to_seat_spread_db` | minimize |
| `o100d.target_margin.worst_seat_db` | maximize |
| `o100d.continuous_headroom.worst_seat_db` | maximize |
| `o100d.peak_headroom.worst_seat_db` | maximize |

The objective comparison-model version is the exact playback-scenario SHA-256. This makes
different input conditions, target conditions, bands, weighting, seat populations, or
formula authorities comparison-incompatible by construction.

Existing Pareto and O90 direction handling is reused without modification. In particular,
the merged #200 O90 authority already defines sampled-worst for maximize objectives as the
lowest scored physical value.

## Exact authority binding and persistence

`DirectLevelEvaluation` binds and hashes:

- exact `SceneRevision` ID/content hash;
- exact `SystemVariant` ID/semantic hash;
- exact O100C `EquipmentDefinition` ID/version/semantic hash;
- exact `PlaybackExcitationScenario`;
- source acoustic-reference position;
- ordered per-seat results;
- aggregate results.

`CadDirectLevelRepository` stores playback scenarios and evaluations append-only in the
native CAD database and revalidates the SceneRevision, SystemVariant, equipment binding,
and EquipmentDefinition on save/reopen.

## Percentiles

This slice does not emit seat percentiles. The population identity is explicit, but no
probability/weighting authority beyond an equal, unweighted finite seat set has been
introduced. A percentile quantity will be added only when its population, ordering,
weighting/probability semantics, missing-seat policy, and percentile definition are
explicit.

## Focused verification

`backend/tests/test_cad_direct_level.py` covers:

- evidenced sensitivity;
- evidenced continuous/peak capability;
- capability-missing equipment;
- an exact two-seat population;
- different direct distances producing different per-seat levels;
- target margin;
- continuous and peak headroom;
- worst-seat and seat-to-seat spread;
- direction-aware maximize Pareto use;
- capability-missing comparison ineligibility;
- valid-frequency-domain rejection;
- voltage versus power excitation/reference comparison refusal;
- deterministic evaluation identity;
- native persistence/reopen.

The existing #200
`test_o90a_maximize_sampled_worst_uses_low_side_and_round_trips` remains the regression
for O90 sampled-worst direction semantics. Existing minimize-only objective regression
tests remain unchanged.

## Deferred

The following remain outside this slice:

- directivity dataset evaluation and off-axis/directivity loss;
- coverage fraction/map and coverage worst-seat objectives;
- room-assisted or reflected-energy SPL;
- solver-backed validated room gain;
- coherent or incoherent multi-channel system summation;
- amplifier/electrical impedance conversion authority;
- percentile-style seat objectives;
- any hidden aggregate score.

Those items require their own evidence authorities and are not inferred from the direct
equipment-derived values implemented here.
