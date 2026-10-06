"""Acoustic channel-identity / polarity verification authority (#621, REV57-AUD).

A correct renderer layout, DSP matrix and cable schedule do not prove that
the intended logical channel actually emerges from the intended physical
loudspeaker with correct polarity. L/R or side/rear swaps, mislabelled DSP
outputs, one polarity-reversed array member and DSP-masked wiring defects
all survive label-level review. This module owns the end-to-end acoustic
endpoint evidence:

- :class:`CadChannelIdentityChain` — the declared identity chain for one
  logical channel: renderer output → processor/DSP output → DAC/amplifier
  channel → physical cable path (#597) → loudspeaker instance → expected
  acoustic endpoint(s). Every hop keeps its own ``known`` / ``inferred`` /
  ``unknown`` state; a label is never silently upgraded to a verified hop.
- :class:`CadAcousticEndpointObservation` — one sealed observation of
  which physical speaker entities actually responded when the logical
  channel was stimulated, with its method and evidence class (manual
  listening confirmation is kept distinct from instrumented proof).
- :class:`CadChannelIdentityTest` — one sealed test binding an exact #608
  channel-identity stimulus and the observed endpoints to the chain.
- :class:`CadPolarityVerificationRecord` — per-channel polarity evidence
  with the five polarity layers kept strictly separate: physical wiring
  polarity (#597 truth), DSP polarity inversion, source-device internal
  polarity, measured acoustic relative polarity and frequency-dependent
  phase. A DSP inversion that makes the acoustic response align never
  rewrites a physically reversed wiring defect.
- :class:`CadChannelIdentityEvaluation` +
  :func:`evaluate_channel_identity` — the fail-closed per-channel verdict
  reconciling configured device map, physical path and acoustic endpoint
  observation into the #621 §12 state ladder, never a global
  "speaker check passed" flag.

Honesty rules baked in:

- Renderer fallback is not a dead speaker: when the bound render session
  intentionally emits nothing on the channel (e.g. a 5.1 fallback on a
  height channel of a 7.1.4 layout), the verdict is
  ``not_driven_by_renderer``, not ``acoustic_endpoint_mismatch``.
- Uncontrolled programme material cannot be the sole identity proof when
  an exact test stimulus exists: a ``program_material`` stimulus class
  caps the verdict at ``verified_with_limitations``.
- Phase-based polarity inference requires a bound #609 timing-capable
  measurement; without it the acoustic polarity verdict stays
  ``unknown``.
- Arrays are verified member-by-member: one active speaker never proves
  the whole array — missing expected members and unexpected active
  endpoints are distinct reconciliation states.
- Verification is state-bound: AVR/DSP resets, firmware/config restores,
  cable service, amplifier or speaker replacement, renderer topology
  changes and physical polarity repairs all stale the evidence.

Literature basis
----------------
- SMPTE RP 2096-1:2017 §7.3.4 — individual pink-noise playback through
  every playback channel including LFE to verify channel routing before
  calibration; §8.7.4 — surround phase/polarity consistency checks.
- Dolby DARDT / calibration guidance — speaker layout and position are
  explicit system inputs whose model predictions still require real-room
  confirmation (provider guidance informs workflow only; HTDT remains
  vendor-neutral).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


CHANNEL_IDENTITY_SCHEMA_VERSION = 'aud-channel-identity-1'
CHANNEL_IDENTITY_EVALUATION_VERSION = 'aud-channel-identity-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#621)
# ---------------------------------------------------------------------------

ChannelClass = Literal[
    'bed_channel',
    'object_test_channel',
    'lfe',
    'redirected_bass',
    'individual_sub_output',
    'height',
    'wide',
    'other',
    'unknown',
]
"""What the logical channel is. LFE, redirected bass and individual
subwoofer outputs are distinct classes (#621 §9) — a combined low-frequency
room response never proves an individual sub's identity."""

IdentityHopKind = Literal[
    'renderer_output',
    'processor_output',
    'dac_amplifier_channel',
    'physical_cable_path',
    'loudspeaker_instance',
    'acoustic_endpoint',
]
"""The ordered identity chain (#621 §1). Each hop is a distinct identity:
a renderer output label, a DSP output label, a cable path and a physical
speaker never collapse into one claim."""

IdentityHopState = Literal['known', 'inferred', 'unknown']
"""Per-hop certainty (#621 §1). An ``inferred`` hop names what the model
believes; only ``known`` carries direct evidence."""

IdentityStimulusClass = Literal[
    'channel_id_test_signal',
    'program_material',
    'unknown',
]
"""#621 §2 — an exact channel-identifying stimulus (check tone / ID signal /
slate) is the strong class; uncontrolled programme material can never be
the sole identity proof when an exact asset exists."""

EndpointObservationMethod = Literal[
    'operator_listening',
    'measurement_mic_level',
    'measurement_mic_ir',
    'near_speaker_mic',
    'array_localization',
    'device_telemetry',
    'other',
]
"""#621 §3 observation classes. ``operator_listening`` is valid manual
evidence but weaker than instrumented proof."""

EndpointEvidenceClass = Literal['manual', 'instrumented']
"""Derived evidence class — operator listening and unspecified 'other'
observations are manual; mic/array methods are instrumented."""

PolarityLayer = Literal[
    'physical_wiring',
    'dsp_inversion',
    'source_internal',
    'acoustic_relative',
    'frequency_dependent_phase',
]
"""#621 §5 — the five polarity quantities stay strictly separate. A
broadband phase trace is not wiring polarity; a DSP inversion is not a
physical repair."""

PolarityLayerState = Literal[
    'normal',
    'reversed',
    'inverted',
    'consistent',
    'inconsistent',
    'ambiguous',
    'unknown',
]

PolarityMethod = Literal[
    'impulse_sign',
    'dedicated_polarity_stimulus',
    'relative_transfer_function',
    'manufacturer_diagnostic',
    'manual_wiring_inspection',
    'other',
    'unknown',
]
"""#621 §6 — the exact method/profile must persist. Phase-based methods
(``relative_transfer_function``) compose #609 and stay ineligible when no
valid timing reference exists."""

ReconciliationState = Literal[
    'all_match',
    'device_map_mismatch',
    'physical_path_mismatch',
    'acoustic_endpoint_mismatch',
    'multiple_unexpected_endpoints',
    'insufficient_evidence',
]
"""#621 §12 — device map, physical path and acoustic endpoint are three
reconciled-but-separate layers; no layer silently overwrites another."""

IdentityInvalidationTrigger = Literal[
    'avr_dsp_reset',
    'firmware_config_change',
    'cable_port_service',
    'amplifier_replacement',
    'speaker_replacement',
    'renderer_topology_change',
    'physical_polarity_repair',
]
"""#621 §13 — events that stale identity/polarity evidence (#592/#596/
#597/#603/#595 compose here)."""

ChannelIdentityVerdict = Literal[
    'verified',
    'verified_with_limitations',
    'verified_compensated',
    'polarity_fault',
    'identity_mismatch',
    'not_driven_by_renderer',
    'stale',
    'insufficient_evidence',
]
"""Per-channel outcome. ``verified_compensated`` records that acoustic
evidence is consistent ONLY because a DSP inversion masks a documented
physical wiring defect — the defect is retained, never rewritten."""


# ---------------------------------------------------------------------------
# Embedded models
# ---------------------------------------------------------------------------


class CadIdentityHop(BaseModel):
    """One hop in the logical→physical identity chain.

    A hop is ``known`` only when it carries a ref or an explicit evidence
    label; ``inferred`` hops document what the model assumes;
    ``unknown`` hops are honest gaps. Known-by-label alone is kept — the
    evaluation still distinguishes declared routing from acoustically
    observed endpoints.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: IdentityHopKind
    state: IdentityHopState = 'unknown'
    ref: AuthorityRef | None = None
    """Exact pin to the hop's authority record — e.g. a #597 physical
    path for ``physical_cable_path``, a #569 installed instance for
    ``loudspeaker_instance``, a #603 render session output for
    ``renderer_output``."""
    label: str | None = Field(default=None, min_length=1)
    """Free-text identity (e.g. the AVR output name) when no sealed
    authority exists for the hop."""

    @model_validator(mode='after')
    def _check(self) -> 'CadIdentityHop':
        if self.state == 'known' and self.ref is None and self.label is None:
            raise ValueError(
                "a 'known' hop requires an authority ref or an evidence label"
            )
        if self.state == 'unknown' and self.ref is not None:
            raise ValueError(
                'an unknown hop cannot pin an authority ref — '
                'a ref implies something is known'
            )
        return self


class CadIdentityStimulusSpec(BaseModel):
    """Exact stimulus identity for one channel-identity test (#621 §2).

    Composes #608: the stimulus asset/generator is pinned by ref when it
    exists; the remaining fields record the exact signal and routing
    state the test ran under.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    stimulus_class: IdentityStimulusClass
    stimulus_ref: AuthorityRef | None = None
    """Pin to a #608 StimulusAssetEntry / generator spec."""
    digital_level_dbfs: float | None = None
    spectrum_descriptor: str | None = Field(default=None, min_length=1)
    """e.g. 'pink_noise_500_2k', 'swept_sine', 'id_sequence'."""
    channel_assignment: str | None = Field(default=None, min_length=1)
    """Slate / identifier assignment for the tested channel."""
    routing_mode: str | None = Field(default=None, min_length=1)
    """e.g. 'bitstream_passthrough', 'pcm_direct', 'upmixer_off'."""
    renderer_state: str | None = Field(default=None, min_length=1)
    """e.g. the renderer/listening mode in force during the test."""

    @model_validator(mode='after')
    def _check(self) -> 'CadIdentityStimulusSpec':
        if self.digital_level_dbfs is not None:
            _require_finite(
                self.digital_level_dbfs, 'stimulus digital_level_dbfs'
            )
        if self.stimulus_class == 'channel_id_test_signal' and (
            self.stimulus_ref is None and self.spectrum_descriptor is None
        ):
            raise ValueError(
                'a channel-id test signal requires a #608 stimulus pin or '
                'an explicit spectrum/generator descriptor'
            )
        if self.stimulus_ref is not None and (
            self.stimulus_ref.ref_sha256 is None
        ):
            raise ValueError('stimulus ref must pin its sha256')
        return self


class CadPolarityLayerState(BaseModel):
    """One polarity layer's observed state (#621 §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    layer: PolarityLayer
    state: PolarityLayerState
    method: PolarityMethod = 'unknown'
    evidence_note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'CadPolarityLayerState':
        if self.state in {'ambiguous', 'unknown'}:
            return self
        if (
            self.layer in {'acoustic_relative', 'frequency_dependent_phase'}
            and self.method == 'unknown'
        ):
            raise ValueError(
                'a definite acoustic polarity claim requires a declared '
                'measurement method'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


class CadChannelIdentityChain(BaseModel):
    """Declared logical→physical identity chain for one channel (#621 §1).

    The chain is a *declaration* — saving it never claims the channel was
    verified. Verification requires a :class:`CadChannelIdentityTest` plus
    acoustic endpoint observation.
    """

    model_config = ConfigDict(frozen=True)

    chain_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    logical_channel: str = Field(min_length=1)
    """Logical channel/role label — e.g. 'FL', 'TFL', 'LFE1'."""
    channel_class: ChannelClass = 'unknown'
    expected_speaker_entity_ids: tuple[str, ...] = ()
    """The physical speaker entities this logical channel should drive.
    Multiple entries model arrayed speakers (#621 §7) — every member must
    verify, one working speaker never proves the array."""
    hops: tuple[CadIdentityHop, ...] = ()
    physical_path_ref: AuthorityRef | None = None
    """Optional pin to the #597 physical path (verified wiring)."""
    render_session_ref: AuthorityRef | None = None
    """Optional pin to the #603 render session this chain was verified
    under — renderer fallback changes the verdict, so the session is part
    of the identity."""
    authority_version: str = Field(
        default=CHANNEL_IDENTITY_SCHEMA_VERSION, min_length=1
    )
    declared_at_utc: str = Field(min_length=1)
    chain_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'logical_channel': self.logical_channel,
            'channel_class': self.channel_class,
            'expected_speaker_entity_ids': list(
                self.expected_speaker_entity_ids
            ),
            'hops': [hop.model_dump(mode='json') for hop in self.hops],
            'physical_path_ref': (
                self.physical_path_ref.model_dump(mode='json')
                if self.physical_path_ref is not None
                else None
            ),
            'render_session_ref': (
                self.render_session_ref.model_dump(mode='json')
                if self.render_session_ref is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadChannelIdentityChain':
        _require_iso8601(self.declared_at_utc, 'chain declared_at_utc')
        if len(set(self.expected_speaker_entity_ids)) != len(
            self.expected_speaker_entity_ids
        ):
            raise ValueError('expected speaker entity ids must be unique')
        hop_kinds = [hop.kind for hop in self.hops]
        if len(hop_kinds) != len(set(hop_kinds)):
            raise ValueError('each identity hop kind may appear only once')
        if self.physical_path_ref is not None and (
            self.physical_path_ref.ref_sha256 is None
        ):
            raise ValueError('physical path ref must pin its sha256')
        if self.render_session_ref is not None:
            if self.render_session_ref.kind != 'render_session':
                raise ValueError(
                    "render_session_ref must pin a 'render_session' authority"
                )
            if self.render_session_ref.ref_sha256 is None:
                raise ValueError('render session ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.chain_sha256 != expected:
            raise ValueError('chain hash mismatch')
        if self.chain_id != _semantic_id('chain', expected):
            raise ValueError('chain id does not match its hash')
        return self


def chain_binding(chain: CadChannelIdentityChain) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_identity_chain',
        ref_id=chain.chain_id,
        ref_sha256=chain.chain_sha256,
    )


class CadAcousticEndpointObservation(BaseModel):
    """One observation of which physical speakers responded (#621 §3).

    The evidence class is derived from the method and never set manually:
    operator-listening and unclassified 'other' observations are ``manual``;
    microphone/array/telemetry methods are ``instrumented``.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    method: EndpointObservationMethod
    observed_speaker_entity_ids: tuple[str, ...] = ()
    """The physical speaker entities observed emitting signal — bound by
    exact Scene entity id, never by label alone (#621 §4)."""
    confidence: Literal['high', 'medium', 'low'] = 'medium'
    measurement_ref: AuthorityRef | None = None
    """Optional pin to the measurement record (IR trace, level meter
    capture, device telemetry snapshot)."""
    note: str | None = Field(default=None, min_length=1)
    observed_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_IDENTITY_SCHEMA_VERSION, min_length=1
    )
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'method': self.method,
            'observed_speaker_entity_ids': list(
                self.observed_speaker_entity_ids
            ),
            'confidence': self.confidence,
            'measurement_ref': (
                self.measurement_ref.model_dump(mode='json')
                if self.measurement_ref is not None
                else None
            ),
            'note': self.note,
            'observed_at_utc': self.observed_at_utc,
            'authority_version': self.authority_version,
        }

    @property
    def evidence_class(self) -> EndpointEvidenceClass:
        if self.method in {'operator_listening', 'other'}:
            return 'manual'
        return 'instrumented'

    @model_validator(mode='after')
    def _check(self) -> 'CadAcousticEndpointObservation':
        _require_iso8601(
            self.observed_at_utc, 'observation observed_at_utc'
        )
        if len(set(self.observed_speaker_entity_ids)) != len(
            self.observed_speaker_entity_ids
        ):
            raise ValueError('observed speaker entity ids must be unique')
        if self.measurement_ref is not None and (
            self.measurement_ref.ref_sha256 is None
        ):
            raise ValueError('measurement ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('endpoint observation hash mismatch')
        if self.observation_id != _semantic_id('chiobs', expected):
            raise ValueError('endpoint observation id does not match its hash')
        return self


def endpoint_observation_binding(
    observation: CadAcousticEndpointObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='acoustic_endpoint_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


class CadChannelIdentityTest(BaseModel):
    """One channel-identity test run (#621).

    Binds the declared chain to the exact stimulus and to every acoustic
    endpoint observation taken under it. The test is sealed against the
    chain sha — a chain edit invalidates earlier tests rather than
    silently inheriting them.
    """

    model_config = ConfigDict(frozen=True)

    test_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_ref: AuthorityRef
    stimulus: CadIdentityStimulusSpec
    observation_refs: tuple[AuthorityRef, ...] = ()
    """Every acoustic endpoint observation bound to this test run."""
    render_session_ref: AuthorityRef | None = None
    """Optional pin to the #603 render session active during the test —
    required for renderer-fallback awareness when the chain omits it."""
    tested_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_IDENTITY_SCHEMA_VERSION, min_length=1
    )
    test_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_ref': self.chain_ref.model_dump(mode='json'),
            'stimulus': self.stimulus.model_dump(mode='json'),
            'observation_refs': [
                ref.model_dump(mode='json') for ref in self.observation_refs
            ],
            'render_session_ref': (
                self.render_session_ref.model_dump(mode='json')
                if self.render_session_ref is not None
                else None
            ),
            'tested_at_utc': self.tested_at_utc,
            'authority_version': self.authority_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadChannelIdentityTest':
        _require_iso8601(self.tested_at_utc, 'test tested_at_utc')
        if self.chain_ref.kind != 'channel_identity_chain':
            raise ValueError(
                "chain_ref must pin a 'channel_identity_chain' authority"
            )
        if self.chain_ref.ref_sha256 is None:
            raise ValueError('chain ref must pin its sha256')
        for ref in self.observation_refs:
            if ref.kind != 'acoustic_endpoint_observation':
                raise ValueError(
                    'observation_refs must pin acoustic endpoint '
                    'observations'
                )
            if ref.ref_sha256 is None:
                raise ValueError('observation refs must pin their sha256')
        if self.render_session_ref is not None and (
            self.render_session_ref.kind != 'render_session'
        ):
            raise ValueError(
                "render_session_ref must pin a 'render_session' authority"
            )
        expected = _hash(self.identity_payload())
        if self.test_sha256 != expected:
            raise ValueError('channel identity test hash mismatch')
        if self.test_id != _semantic_id('chitest', expected):
            raise ValueError('channel identity test id does not match its hash')
        return self


def identity_test_binding(test: CadChannelIdentityTest) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_identity_test',
        ref_id=test.test_id,
        ref_sha256=test.test_sha256,
    )


class CadPolarityVerificationRecord(BaseModel):
    """Polarity verification for one channel/endpoint (#621 §5/§10).

    The five polarity layers are stored separately and never merged. When
    a physical reversal exists but a DSP inversion compensates it, the
    record keeps BOTH states — the wiring defect is retained for service
    recovery and the DSP workaround is documented, never used to mark the
    wiring physically correct.
    """

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_ref: AuthorityRef
    layers: tuple[CadPolarityLayerState, ...]
    """Independent states for wiring / DSP / source / acoustic / phase.
    Each layer appears at most once — the taxonomy is per-layer, not a
    combined verdict."""
    timebase_ref: AuthorityRef | None = None
    """#609 timing-capability pin — REQUIRED whenever a phase/relative-TF
    method is claimed for an acoustic layer (#621 §6)."""
    wiring_path_ref: AuthorityRef | None = None
    """Optional pin to the #597 physical path the wiring state reports."""
    policy_acceptance: Literal[
        'unreviewed', 'accepted', 'rejected', 'not_applicable'
    ] = 'unreviewed'
    """Project policy on accepting a DSP-compensated wiring defect —
    recorded, never auto-accepted (#621 §10)."""
    measured_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=CHANNEL_IDENTITY_SCHEMA_VERSION, min_length=1
    )
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_ref': self.chain_ref.model_dump(mode='json'),
            'layers': [layer.model_dump(mode='json') for layer in self.layers],
            'timebase_ref': (
                self.timebase_ref.model_dump(mode='json')
                if self.timebase_ref is not None
                else None
            ),
            'wiring_path_ref': (
                self.wiring_path_ref.model_dump(mode='json')
                if self.wiring_path_ref is not None
                else None
            ),
            'policy_acceptance': self.policy_acceptance,
            'measured_at_utc': self.measured_at_utc,
            'authority_version': self.authority_version,
        }

    def layer_state(self, layer: PolarityLayer) -> PolarityLayerState:
        for entry in self.layers:
            if entry.layer == layer:
                return entry.state
        return 'unknown'

    @model_validator(mode='after')
    def _check(self) -> 'CadPolarityVerificationRecord':
        _require_iso8601(self.measured_at_utc, 'record measured_at_utc')
        if not self.layers:
            raise ValueError(
                'a polarity record requires at least one layer state'
            )
        layer_names = [entry.layer for entry in self.layers]
        if len(layer_names) != len(set(layer_names)):
            raise ValueError('each polarity layer may appear only once')
        if self.chain_ref.kind != 'channel_identity_chain':
            raise ValueError(
                "chain_ref must pin a 'channel_identity_chain' authority"
            )
        if self.chain_ref.ref_sha256 is None:
            raise ValueError('chain ref must pin its sha256')
        phase_methods = {'relative_transfer_function'}
        if any(
            entry.method in phase_methods
            and entry.state not in {'ambiguous', 'unknown'}
            for entry in self.layers
        ) and self.timebase_ref is None:
            raise ValueError(
                'phase-based polarity claims require a bound #609 '
                'timebase capability — without valid timing the inference '
                'is ambiguous'
            )
        if self.timebase_ref is not None and (
            self.timebase_ref.kind != 'measurement_timebase'
        ):
            raise ValueError(
                "timebase_ref must pin a 'measurement_timebase' authority"
            )
        # #621 §10: a DSP workaround never rewrites the wiring truth. If a
        # physical layer claims 'normal' purely via DSP masking the model
        # cannot see it — but an explicit 'reversed' physical layer plus a
        # 'not_applicable'/'unreviewed' policy still validates; what is
        # forbidden is declaring the wiring corrected by DSP state.
        wiring = self.layer_state('physical_wiring')
        dsp = self.layer_state('dsp_inversion')
        acoustic = self.layer_state('acoustic_relative')
        if (
            wiring == 'reversed'
            and dsp == 'inverted'
            and acoustic == 'consistent'
            and self.policy_acceptance == 'accepted'
        ):
            # Allowed, but recorded explicitly — never auto-accepted.
            pass
        if wiring == 'normal' and dsp == 'inverted':
            # A DSP inversion on physically-normal wiring inverts the
            # acoustic output — the acoustic layer may not then claim
            # 'consistent' without evidence of the product.
            if acoustic == 'consistent':
                raise ValueError(
                    'physically normal wiring with a DSP inversion cannot '
                    'report a consistent acoustic polarity — the layers '
                    'contradict; measure the acoustic polarity directly'
                )
        expected = _hash(self.identity_payload())
        if self.record_sha256 != expected:
            raise ValueError('polarity record hash mismatch')
        if self.record_id != _semantic_id('chipol', expected):
            raise ValueError('polarity record id does not match its hash')
        return self


def polarity_record_binding(
    record: CadPolarityVerificationRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='polarity_verification_record',
        ref_id=record.record_id,
        ref_sha256=record.record_sha256,
    )


# ---------------------------------------------------------------------------
# Evaluation (#621 §12/§15)
# ---------------------------------------------------------------------------


class CadChannelIdentityEvaluation(BaseModel):
    """Sealed per-channel verdict (#621 §15).

    Reports every axis — expected/observed speakers, device-map vs
    physical-path vs acoustic reconciliation, each polarity layer and the
    evidence/staleness state. There is no composite ``speaker_check``
    boolean: a channel is commissioned per-channel, per-axis.
    """

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chain_ref: AuthorityRef
    test_refs: tuple[AuthorityRef, ...] = ()
    polarity_record_refs: tuple[AuthorityRef, ...] = ()
    logical_channel: str = Field(min_length=1)
    verdict: ChannelIdentityVerdict
    reconciliation: ReconciliationState
    physical_wiring_state: PolarityLayerState = 'unknown'
    dsp_polarity_state: PolarityLayerState = 'unknown'
    acoustic_polarity_state: PolarityLayerState = 'unknown'
    expected_speaker_entity_ids: tuple[str, ...] = ()
    observed_speaker_entity_ids: tuple[str, ...] = ()
    missing_speaker_entity_ids: tuple[str, ...] = ()
    unexpected_speaker_entity_ids: tuple[str, ...] = ()
    evidence_class: EndpointEvidenceClass | Literal['none'] = 'none'
    renderer_fallback: bool = False
    stale_triggers: tuple[IdentityInvalidationTrigger, ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'chain_ref': self.chain_ref.model_dump(mode='json'),
            'test_refs': [
                ref.model_dump(mode='json') for ref in self.test_refs
            ],
            'polarity_record_refs': [
                ref.model_dump(mode='json')
                for ref in self.polarity_record_refs
            ],
            'logical_channel': self.logical_channel,
            'verdict': self.verdict,
            'reconciliation': self.reconciliation,
            'physical_wiring_state': self.physical_wiring_state,
            'dsp_polarity_state': self.dsp_polarity_state,
            'acoustic_polarity_state': self.acoustic_polarity_state,
            'expected_speaker_entity_ids': list(
                self.expected_speaker_entity_ids
            ),
            'observed_speaker_entity_ids': list(
                self.observed_speaker_entity_ids
            ),
            'missing_speaker_entity_ids': list(
                self.missing_speaker_entity_ids
            ),
            'unexpected_speaker_entity_ids': list(
                self.unexpected_speaker_entity_ids
            ),
            'evidence_class': self.evidence_class,
            'renderer_fallback': self.renderer_fallback,
            'stale_triggers': list(self.stale_triggers),
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'CadChannelIdentityEvaluation':
        _require_iso8601(
            self.evaluated_at_utc, 'evaluation evaluated_at_utc'
        )
        if self.chain_ref.kind != 'channel_identity_chain':
            raise ValueError(
                "chain_ref must pin a 'channel_identity_chain' authority"
            )
        if self.chain_ref.ref_sha256 is None:
            raise ValueError('chain ref must pin its sha256')
        for ref in self.test_refs:
            if ref.kind != 'channel_identity_test':
                raise ValueError(
                    'test_refs must pin channel identity tests'
                )
            if ref.ref_sha256 is None:
                raise ValueError('test refs must pin their sha256')
        for ref in self.polarity_record_refs:
            if ref.kind != 'polarity_verification_record':
                raise ValueError(
                    'polarity_record_refs must pin polarity records'
                )
            if ref.ref_sha256 is None:
                raise ValueError('polarity refs must pin their sha256')
        expected = _hash(self.identity_payload())
        if self.evaluation_sha256 != expected:
            raise ValueError('identity evaluation hash mismatch')
        if self.evaluation_id != _semantic_id('chieval', expected):
            raise ValueError('evaluation id does not match its hash')
        return self


def identity_evaluation_binding(
    evaluation: CadChannelIdentityEvaluation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='channel_identity_evaluation',
        ref_id=evaluation.evaluation_id,
        ref_sha256=evaluation.evaluation_sha256,
    )


def evaluate_channel_identity(
    *,
    document_id: str,
    chain: CadChannelIdentityChain,
    tests: Sequence[CadChannelIdentityTest] = (),
    observations: Sequence[CadAcousticEndpointObservation] = (),
    polarity_records: Sequence[CadPolarityVerificationRecord] = (),
    render_fallback_active: bool = False,
    device_map_speaker_entity_ids: Sequence[str] | None = None,
    invalidation_triggers: Sequence[IdentityInvalidationTrigger] = (),
    evaluated_at_utc: str | None = None,
) -> CadChannelIdentityEvaluation:
    """Fail-closed channel-identity verdict (#621 §12/§15).

    Reconciles the declared chain, the device-map claim, the physical
    path claim and every bound acoustic endpoint observation. The
    renderer-fallback flag is an explicit input: when the bound render
    session intentionally emits nothing on this channel the verdict is
    ``not_driven_by_renderer`` rather than a misdiagnosed dead speaker
    (#621 §8).
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    reasons: list[str] = []
    limitations: list[str] = []

    # --- staleness first: changed configuration invalidates evidence ---
    stale = tuple(dict.fromkeys(invalidation_triggers))
    if stale:
        reasons.append(
            'invalidating change recorded: ' + ' / '.join(stale)
        )

    # --- bind tests to this exact chain --------------------------------
    bound_tests = [
        test
        for test in tests
        if test.chain_ref.ref_id == chain.chain_id
        and test.chain_ref.ref_sha256 == chain.chain_sha256
    ]
    bound_records = [
        record
        for record in polarity_records
        if record.chain_ref.ref_id == chain.chain_id
        and record.chain_ref.ref_sha256 == chain.chain_sha256
    ]
    if len(bound_tests) != len(tuple(tests)):
        reasons.append(
            'tests bound to a different chain revision were ignored — '
            'a chain edit stales earlier evidence'
        )

    # --- stimulus honesty ----------------------------------------------
    program_only = False
    if bound_tests:
        classes = {test.stimulus.stimulus_class for test in bound_tests}
        if 'program_material' in classes and (
            'channel_id_test_signal' not in classes
        ):
            program_only = True
            limitations.append(
                'identity evidence rests on uncontrolled programme '
                'material — an exact #608 channel-id stimulus is required '
                'for full verification'
            )
        if 'unknown' in classes and 'channel_id_test_signal' not in classes:
            program_only = True
            limitations.append('stimulus class unknown — evidence weakened')

    # --- gather observed endpoints -------------------------------------
    observation_ids = {
        ref.ref_id
        for test in bound_tests
        for ref in test.observation_refs
    }
    bound_observations = [
        obs for obs in observations if obs.observation_id in observation_ids
    ]
    observed = tuple(
        dict.fromkeys(
            entity
            for obs in bound_observations
            for entity in obs.observed_speaker_entity_ids
        )
    )
    if bound_observations:
        evidence_class: EndpointEvidenceClass | Literal['none'] = (
            'instrumented'
            if any(
                obs.evidence_class == 'instrumented'
                for obs in bound_observations
            )
            else 'manual'
        )
        if evidence_class == 'manual':
            limitations.append(
                'endpoint evidence is manual (listening/unspecified '
                'method) — instrumented observation is required for '
                'stronger claims'
            )
    else:
        evidence_class = 'none'

    expected = tuple(chain.expected_speaker_entity_ids)
    missing = tuple(e for e in expected if e not in observed)
    unexpected = tuple(e for e in observed if e not in expected)

    # --- physical polarity layers --------------------------------------
    physical_state: PolarityLayerState = 'unknown'
    dsp_state: PolarityLayerState = 'unknown'
    acoustic_state: PolarityLayerState = 'unknown'
    if bound_records:
        latest = bound_records[-1]
        physical_state = latest.layer_state('physical_wiring')
        dsp_state = latest.layer_state('dsp_inversion')
        acoustic_state = latest.layer_state('acoustic_relative')
        if latest.layer_state('frequency_dependent_phase') == 'ambiguous':
            limitations.append(
                'frequency-dependent phase is ambiguous — broadband phase '
                'is not wiring polarity (#621 §5)'
            )

    # --- renderer fallback awareness (#621 §8) --------------------------
    session_ref = chain.render_session_ref
    if session_ref is None:
        session_ref = next(
            (t.render_session_ref for t in bound_tests
             if t.render_session_ref is not None),
            None,
        )
    renderer_silent = render_fallback_active and not observed

    # --- reconciliation (#621 §12) --------------------------------------
    if stale:
        reconciliation: ReconciliationState = 'insufficient_evidence'
        verdict: ChannelIdentityVerdict = 'stale'
    elif renderer_silent:
        reconciliation = 'insufficient_evidence'
        verdict = 'not_driven_by_renderer'
        reasons.append(
            'render session fell back for this channel — an absent '
            'acoustic endpoint is intentional, not a dead speaker'
        )
    elif not bound_tests or not bound_observations:
        reconciliation = 'insufficient_evidence'
        verdict = 'insufficient_evidence'
        reasons.append('no bound endpoint observation')
    else:
        if unexpected:
            reconciliation = 'multiple_unexpected_endpoints'
        elif missing:
            reconciliation = 'acoustic_endpoint_mismatch'
        else:
            reconciliation = 'all_match'
        if (
            device_map_speaker_entity_ids is not None
            and reconciliation == 'all_match'
            and tuple(device_map_speaker_entity_ids) != expected
        ):
            reconciliation = 'device_map_mismatch'
        if unexpected:
            verdict = 'identity_mismatch'
            reasons.append(
                'unexpected acoustic endpoints active: '
                + ' / '.join(unexpected)
            )
        elif missing:
            verdict = 'identity_mismatch'
            reasons.append(
                'expected physical speakers silent: ' + ' / '.join(missing)
            )
        elif physical_state == 'reversed' and dsp_state == 'inverted':
            verdict = 'verified_compensated'
            reasons.append(
                'physical wiring defect retained — DSP inversion masks it '
                'acoustically; the wiring graph stays reversed'
            )
        elif physical_state == 'reversed':
            verdict = 'polarity_fault'
            reasons.append('physical wiring polarity reversed')
        elif acoustic_state == 'inconsistent':
            verdict = 'polarity_fault'
            reasons.append('acoustic relative polarity inconsistent')
        elif dsp_state == 'inverted' and acoustic_state == 'inconsistent':
            verdict = 'polarity_fault'
        elif program_only or evidence_class == 'manual':
            verdict = 'verified_with_limitations'
        else:
            verdict = 'verified'

    payload = {
        'document_id': document_id,
        'chain_ref': chain_binding(chain).model_dump(mode='json'),
        'test_refs': [
            identity_test_binding(test).model_dump(mode='json')
            for test in bound_tests
        ],
        'polarity_record_refs': [
            polarity_record_binding(record).model_dump(mode='json')
            for record in bound_records
        ],
        'logical_channel': chain.logical_channel,
        'verdict': verdict,
        'reconciliation': reconciliation,
        'physical_wiring_state': physical_state,
        'dsp_polarity_state': dsp_state,
        'acoustic_polarity_state': acoustic_state,
        'expected_speaker_entity_ids': list(expected),
        'observed_speaker_entity_ids': list(observed),
        'missing_speaker_entity_ids': list(missing),
        'unexpected_speaker_entity_ids': list(unexpected),
        'evidence_class': evidence_class,
        'renderer_fallback': bool(renderer_silent),
        'stale_triggers': list(stale),
        'reasons': reasons,
        'limitations': limitations,
        'evaluation_version': CHANNEL_IDENTITY_EVALUATION_VERSION,
        'evaluated_at_utc': evaluated_at_utc,
    }
    return _seal(
        CadChannelIdentityEvaluation,
        payload,
        'evaluation_id',
        'evaluation_sha256',
        'chieval',
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_channel_identity_chain(
    *,
    document_id: str,
    logical_channel: str,
    channel_class: ChannelClass = 'unknown',
    expected_speaker_entity_ids: Sequence[str] = (),
    hops: Sequence[CadIdentityHop] = (),
    physical_path_ref: AuthorityRef | None = None,
    render_session_ref: AuthorityRef | None = None,
    declared_at_utc: str | None = None,
) -> CadChannelIdentityChain:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        CadChannelIdentityChain,
        {
            'document_id': document_id,
            'logical_channel': logical_channel,
            'channel_class': channel_class,
            'expected_speaker_entity_ids': list(expected_speaker_entity_ids),
            'hops': [
                hop.model_dump(mode='json') if isinstance(hop, CadIdentityHop)
                else hop
                for hop in hops
            ],
            'physical_path_ref': (
                physical_path_ref.model_dump(mode='json')
                if physical_path_ref is not None
                else None
            ),
            'render_session_ref': (
                render_session_ref.model_dump(mode='json')
                if render_session_ref is not None
                else None
            ),
            'authority_version': CHANNEL_IDENTITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'chain_id',
        'chain_sha256',
        'chain',
    )


def build_endpoint_observation(
    *,
    document_id: str,
    method: EndpointObservationMethod,
    observed_speaker_entity_ids: Sequence[str] = (),
    confidence: Literal['high', 'medium', 'low'] = 'medium',
    measurement_ref: AuthorityRef | None = None,
    note: str | None = None,
    observed_at_utc: str | None = None,
) -> CadAcousticEndpointObservation:
    observed_at_utc = observed_at_utc or _utc_now()
    return _seal(
        CadAcousticEndpointObservation,
        {
            'document_id': document_id,
            'method': method,
            'observed_speaker_entity_ids': list(
                observed_speaker_entity_ids
            ),
            'confidence': confidence,
            'measurement_ref': (
                measurement_ref.model_dump(mode='json')
                if measurement_ref is not None
                else None
            ),
            'note': note,
            'observed_at_utc': observed_at_utc,
            'authority_version': CHANNEL_IDENTITY_SCHEMA_VERSION,
        },
        'observation_id',
        'observation_sha256',
        'chiobs',
    )


def build_channel_identity_test(
    *,
    document_id: str,
    chain: CadChannelIdentityChain,
    stimulus: CadIdentityStimulusSpec,
    observation_refs: Sequence[AuthorityRef] = (),
    render_session_ref: AuthorityRef | None = None,
    tested_at_utc: str | None = None,
) -> CadChannelIdentityTest:
    tested_at_utc = tested_at_utc or _utc_now()
    return _seal(
        CadChannelIdentityTest,
        {
            'document_id': document_id,
            'chain_ref': chain_binding(chain).model_dump(mode='json'),
            'stimulus': stimulus.model_dump(mode='json'),
            'observation_refs': [
                ref.model_dump(mode='json') for ref in observation_refs
            ],
            'render_session_ref': (
                render_session_ref.model_dump(mode='json')
                if render_session_ref is not None
                else None
            ),
            'tested_at_utc': tested_at_utc,
            'authority_version': CHANNEL_IDENTITY_SCHEMA_VERSION,
        },
        'test_id',
        'test_sha256',
        'chitest',
    )


def build_polarity_record(
    *,
    document_id: str,
    chain: CadChannelIdentityChain,
    layers: Sequence[CadPolarityLayerState],
    timebase_ref: AuthorityRef | None = None,
    wiring_path_ref: AuthorityRef | None = None,
    policy_acceptance: Literal[
        'unreviewed', 'accepted', 'rejected', 'not_applicable'
    ] = 'unreviewed',
    measured_at_utc: str | None = None,
) -> CadPolarityVerificationRecord:
    measured_at_utc = measured_at_utc or _utc_now()
    return _seal(
        CadPolarityVerificationRecord,
        {
            'document_id': document_id,
            'chain_ref': chain_binding(chain).model_dump(mode='json'),
            'layers': [
                layer.model_dump(mode='json')
                if isinstance(layer, CadPolarityLayerState)
                else layer
                for layer in layers
            ],
            'timebase_ref': (
                timebase_ref.model_dump(mode='json')
                if timebase_ref is not None
                else None
            ),
            'wiring_path_ref': (
                wiring_path_ref.model_dump(mode='json')
                if wiring_path_ref is not None
                else None
            ),
            'policy_acceptance': policy_acceptance,
            'measured_at_utc': measured_at_utc,
            'authority_version': CHANNEL_IDENTITY_SCHEMA_VERSION,
        },
        'record_id',
        'record_sha256',
        'chipol',
    )


__all__ = [
    'CadAcousticEndpointObservation',
    'CadChannelIdentityChain',
    'CadChannelIdentityEvaluation',
    'CadChannelIdentityTest',
    'CadIdentityHop',
    'CadIdentityStimulusSpec',
    'CadPolarityLayerState',
    'CadPolarityVerificationRecord',
    'CHANNEL_IDENTITY_EVALUATION_VERSION',
    'CHANNEL_IDENTITY_SCHEMA_VERSION',
    'ChannelClass',
    'ChannelIdentityVerdict',
    'EndpointEvidenceClass',
    'EndpointObservationMethod',
    'IdentityHopKind',
    'IdentityHopState',
    'IdentityInvalidationTrigger',
    'IdentityStimulusClass',
    'PolarityLayer',
    'PolarityLayerState',
    'PolarityMethod',
    'ReconciliationState',
    'build_channel_identity_chain',
    'build_channel_identity_test',
    'build_endpoint_observation',
    'build_polarity_record',
    'chain_binding',
    'endpoint_observation_binding',
    'evaluate_channel_identity',
    'identity_evaluation_binding',
    'identity_test_binding',
    'polarity_record_binding',
]
