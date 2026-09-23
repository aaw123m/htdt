# Issue #169 O100D — amplifier / electrical playback-chain headroom

Status: implemented on \`issue-169-o100d-amplifier-electrical-headroom\`.

This slice adds an explicit amplifier/output-device electrical capability authority. It is
intentionally separate from the existing speaker-side direct/equipment-derived acoustic
SPL/headroom authority.

## Authority boundary

The existing \`cad_direct_level.py\` authority answers speaker-side acoustic questions such
as direct SPL, target SPL margin, and evidenced continuous/peak speaker SPL capability.

This slice answers a different question:

\`\`\`
playback source
-> exact amplifier/output channel
-> exact speaker load/reference
-> explicit electrical headroom
\`\`\`

An amplifier electrical value is not treated as speaker acoustic headroom. Conversely, a
speaker continuous/peak SPL declaration is not treated as amplifier output capability.
Only when the existing speaker sensitivity/reference condition and the new electrical
authority are exactly compatible may the evaluator derive an
amplifier-constrained direct SPL ceiling. That derived ceiling still uses the existing
direct free-field reference semantics: no room gain, reflections, excursion model,
distortion model, thermal model, or multi-channel acoustic summation is added.

## AmplifierOutputCapability

\`AmplifierOutputCapability\` is immutable, versioned, and deterministic-hash identified.
Its semantic identity includes:

- capability ID/version;
- manufacturer/model or user-defined identity;
- exact output/channel ID;
- evidence/provenance;
- explicit resistive reference load plus the load domain for which the declaration is
  valid;
- optional continuous voltage or power capability;
- optional peak voltage or power capability;
- the corresponding continuous/peak duration;
- optional evidenced gain and reference input;
- clipping/reference definition;
- valid frequency band;
- weighting;
- exact simultaneous-channel-count condition;
- whether a multi-channel declaration has explicit shared-supply evidence;
- uncertainty;
- declared missing/unsupported fields.

A multi-channel capability with no explicit shared-supply evidence is rejected at model
construction. A single-channel declaration is not promoted to simultaneous multi-channel
performance.

## Speaker electrical load authority

The existing \`EquipmentDefinition\` intentionally remains an acoustic/source authority;
this slice does not add a guessed nominal impedance to it.

Where electrical load semantics are needed, the evaluator consumes a separate
\`SpeakerElectricalLoadAuthority\` that binds to the exact
\`EquipmentDefinition\` ID/version/hash and carries:

- an explicit resistance/reference value;
- its valid frequency band;
- provenance;
- one of two semantics:
  - \`exact_resistive_reference\`;
  - \`nominal_impedance_only\`.

\`nominal_impedance_only\` is deliberately insufficient for amplifier load-domain
qualification or V<->W conversion. An “8 ohm / 100 W” amplifier declaration therefore does
not imply that arbitrary speakers can receive 100 W.

If no speaker electrical-load authority exists, load-dependent evaluation remains
unsupported rather than inventing an impedance.

## Voltage / power conversion

Same-quantity comparison remains in its declared quantity. Cross-quantity conversion is
allowed only for an \`exact_resistive_reference\` load:

- \`P = V^2 / R\`
- \`V = sqrt(P * R)\`

No frequency-dependent impedance curve is inferred. A nominal impedance value cannot
upgrade into an exact load model.

## PlaybackChainScenario

\`PlaybackChainScenario\` binds the evaluation to:

- exact SceneRevision ID/content hash;
- exact SystemVariant ID/semantic hash;
- exact source EquipmentDefinition ID/version/hash;
- exact AmplifierOutputCapability ID/version/hash;
- optional exact SpeakerElectricalLoadAuthority ID/version/hash;
- exact source -> role -> amplifier-output routing;
- requested input condition;
- requested output quantity;
- requested continuous and peak output values;
- frequency band and weighting;
- continuous and peak durations;
- target SPL/reference condition and direct acoustic target distance;
- continuous/peak target mode;
- exact simultaneous output set.

The scenario has both a full semantic hash and a comparison hash. The comparison identity
contains the physical conditions that must match for objective comparison while allowing
different SystemVariant/amplifier candidates to be compared under the same test
conditions.

Different load condition, duration, simultaneous-channel count, frequency band, weighting,
or voltage-vs-power quantity yields a different comparison authority. Those values are not
silently compared.

## Evaluation results

\`PlaybackChainEvaluation\` retains independently:

- continuous electrical margin;
- peak electrical margin;
- continuous amplifier-constrained direct SPL ceiling;
- peak amplifier-constrained direct SPL ceiling;
- continuous speaker acoustic SPL ceiling;
- peak speaker acoustic SPL ceiling;
- amplifier-constrained target margin;
- continuous limiting component;
- peak limiting component;
- exact missing/unsupported reasons.

Electrical margin is the evidenced amplifier capability minus the requested output in the
scenario's requested electrical quantity. The unit therefore remains \`V RMS\` or \`W\`;
it is not relabeled as dB.

The limiting component is \`amplifier\`, \`speaker\`, \`equal\`, or \`unknown\`. A limiting
component is named only when both the amplifier-constrained acoustic ceiling and the
speaker acoustic capability ceiling are available on compatible semantics. Missing
sensitivity, incompatible load, missing duration, unsupported band, and similar gaps keep
the limiter unknown.

## Acoustic SPL connection

The amplifier-constrained direct SPL ceiling is calculated only when all required
authorities align:

1. amplifier electrical capability is valid for the exact load, band, duration, weighting,
   and simultaneous-channel condition;
2. the speaker sensitivity/reference condition exists;
3. the sensitivity band/weighting supports the scenario;
4. amplifier output quantity can be matched to the sensitivity input quantity directly or
   through an exact resistive-load conversion.

If those conditions do not hold, electrical headroom may remain available when its own
requirements are satisfied while the acoustic ceiling is independently
missing/unsupported. No new electro-acoustic efficiency assumption is introduced.

## ObjectiveDefinition

This slice emits three independent objectives through the existing direction-aware
objective authority:

| Objective | Unit | Direction |
| --- | --- | --- |
| \`o100d.electrical_headroom.continuous\` | \`V RMS\` or \`W\` | maximize |
| \`o100d.electrical_headroom.peak\` | \`V RMS\` or \`W\` | maximize |
| \`o100d.amplifier_constrained_target_margin\` | \`dB\` | maximize |

Voltage and power objectives therefore cannot share an ObjectiveDefinition identity, and
electrical absolute margins are not mixed with dB acoustic margins. No aggregate or hidden
system score is added.

## Persistence

\`CadAmplifierHeadroomRepository\` provides append-only native SQLite persistence for:

- amplifier output capabilities;
- speaker electrical-load authorities;
- playback-chain scenarios;
- playback-chain evaluations.

On save and reopen it revalidates the exact SceneRevision, SystemVariant,
EquipmentDefinition, amplifier capability, speaker load, and routing/output identity.
Authority IDs that already exist with different semantics are rejected.

## Focused verification

\`backend/tests/test_cad_amplifier_headroom.py\` covers the requested fixture set:

- voltage capability;
- power capability;
- compatible load;
- incompatible load;
- continuous limit;
- peak limit;
- duration mismatch;
- single-channel capability;
- refusal to promote single-channel data to multi-channel operation;
- explicit shared-supply multi-channel authority;
- amplifier-constrained target margin;
- amplifier-limited state;
- speaker-limited state when exact acoustic comparison is valid;
- unknown limiter when the acoustic conversion evidence is absent;
- voltage/power conversion refusal for nominal-only load semantics;
- maximize ObjectiveDefinition behavior and unit separation;
- comparison ineligibility through distinct load/duration/channel/quantity identities;
- append-only save/reopen with exact authority revalidation.

## Non-goals

This slice does not implement speaker impedance-curve estimation, amplifier distortion
simulation, amplifier/speaker thermal models, excursion models, coherent multi-channel
acoustic summation, room gain, a product catalogue, hidden system scoring, coverage
changes, topology-comparison changes, or common roadmap/status edits.
