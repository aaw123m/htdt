"""Room-acoustic measurement-source / excitation authority (#668,
REV58-MEASCHAIN).

A room-acoustic metric measured with a standardized approximately
omnidirectional source is *not the same measurand* as an impulse response
measured through the installed L/C/R/surround loudspeaker. When the
excitation/source semantics are lost, values can look numerically
compatible while describing different physical systems.

This module makes the excitation source a sealed, fail-closed authority:

- :class:`CadExcitationSourceProfile` — the declared source: exact device
  / model / instance, source type (dodecahedron / standardized omni /
  directional loudspeaker / installed channel / subwoofer / impulse
  source), directivity dataset pin, orientation and position, drive path
  and DSP/EQ state, output-level capability, the #608 stimulus pin, and a
  per-band omnidirectionality capability bound to evidence — never
  inferred from a dodecahedron-shaped enclosure alone.
- :class:`CadSourceOrientationCapture` — one immutable capture per source
  orientation; rotation/averaging procedures retain every acquisition and
  pin the #575 aggregation transform instead of relabeling the average as
  one physical IR.
- :class:`CadMeasurementSourceQualification` — the fail-closed verdict:
  measurand class, per-purpose eligibility (standardized room
  characterization vs installed-system diagnostics vs Strength-G vs
  simulation comparison), the level/dynamic-range gate, and the
  simulation source-compatibility verdict.

Composition:

- #608 supplies stimulus identity; #609 timebase governs timing; #695
  chain linearity governs capture validity; #611 governs instrument
  calibration — none of them prove the source's *directivity*.
- #213 owns directional/binaural *receiver* semantics; this authority is
  the excitation side. A correct HATS/figure-8 capture never compensates
  for an incompatible excitation source.
- #135 owns absolute Strength-G reference semantics; this authority gates
  the source side: arbitrary installed-speaker normalization or unknown
  source power blocks G.
- #564/#566 prediction↔measurement comparison consumes the
  ``sim_comparison`` verdict — models compare only under compatible
  source models; room materials never absorb source-model mismatch.
- #581 campaign design can request standardized room-characterization and
  installed-channel campaigns as separate measurands.

Honesty rules baked into the models and the evaluator:

- A dodecahedron enclosure is never proof of omnidirectionality — the
  omni capability requires measured/lab evidence per frequency band.
- An installed-channel IR remains first-class playback evidence but never
  silently answers "the room's standardized C80/T30/G".
- Source class participates in measurement identity — equal mic
  positions do not equate unequal sources.
- Averaging multiple orientations preserves every raw acquisition plus
  the exact aggregation rule; a subwoofer is not an omni standard source
  merely because wavelengths are long.
- ISO/DIS 3382-1 Ed.2 stays ``RESEARCH_ONLY`` — method profiles must
  never be silently re-read under draft requirements.

Literature / standards basis
----------------------------
- ISO 3382-1:2009 (current; `to be revised`): the measurement source must
  be as close to omnidirectional as practicable and provide enough
  level/dynamic range for valid decay measurement.
- ISO/DIS 3382-1 Edition 2 (DIS stage since 2026-09-07): draft only —
  tracked as research evidence, never normative.
- ISO 3382-2:2008 (reverberation time in ordinary rooms): an installed
  home-theater loudspeaker does not automatically qualify as the
  standardized excitation source.
- Applied Acoustics source-directivity studies (S0003682X07001508,
  S0003682X06002209, S0003682X17300580): source directivity changes
  measured T30/C80; practical dodecahedrons become directional above
  their useful omni range and rotation affects results.
- MDPI Applied Sciences 9(18):3705 (2019) review of omni-source
  alternatives; DOI 10.3397/IN_2025_1076868 (2025): measured-dodecahedron
  vs ideal-omni simulation shows close average RT but larger C80
  differences above 2 kHz — approximation validity is band-limited.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


EXCITATION_SCHEMA_VERSION = 'measchain-src-1'
EXCITATION_EVALUATION_VERSION = 'measchain-src-eval-1'

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


def _require_sha(value: str | None, label: str) -> None:
    if value is None:
        return
    if len(value) != 64:
        raise ValueError(f'{label} must be 64 hex characters')
    int(value, 16)


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#668)
# ---------------------------------------------------------------------------

MeasurandClass = Literal[
    'room_response_standardized_omni_source',
    'room_response_approx_omni_source',
    'installed_loudspeaker_transfer',
    'installed_channel_system_response',
    'reference_source_transfer',
    'external_unknown_source',
]

#: Measurand classes that describe the room under (approximately)
#: standardized excitation — eligible in principle for ISO-style room
#: metrics; the other classes are system-evidence measurands.
ROOM_CHARACTERIZATION_CLASSES: frozenset[MeasurandClass] = frozenset(
    {
        'room_response_standardized_omni_source',
        'room_response_approx_omni_source',
    }
)

ExcitationSourceType = Literal[
    'dodecahedron_omni',
    'standardized_omni_source',
    'directional_loudspeaker',
    'installed_loudspeaker_channel',
    'subwoofer',
    'impulse_source',
    'unknown',
]

OmniCapabilityState = Literal[
    'omni_profile_verified',
    'omni_within_profile_band',
    'limited_directivity',
    'directional_source',
    'unknown_directivity',
]

#: Omni states that can support standardized-room claims in-band.
_OMNI_CAPABLE_STATES: frozenset[OmniCapabilityState] = frozenset(
    {'omni_profile_verified', 'omni_within_profile_band'}
)

OmniEvidenceBasis = Literal[
    'laboratory_measurement',
    'manufacturer_balloon_data',
    'standard_conformance_test',
    'field_verification',
    'assumed_from_geometry',
    'unknown',
]

#: Evidence that can ever carry an omni claim — ``assumed_from_geometry``
#: and ``unknown`` never do (a dodecahedron shape alone is not evidence).
_STRONG_OMNI_BASES: frozenset[OmniEvidenceBasis] = frozenset(
    {
        'laboratory_measurement',
        'manufacturer_balloon_data',
        'standard_conformance_test',
        'field_verification',
    }
)

SourcePurposeClass = Literal[
    'standardized_room_characterization',
    'installed_system_diagnostics',
    'spatial_impression_measurement',
    'strength_g_measurement',
    'simulation_validation_comparison',
]

SourceEligibilityState = Literal[
    'eligible',
    'eligible_with_source_limitation',
    'directivity_out_of_profile',
    'insufficient_source_level',
    'wrong_source_class',
    'source_state_unknown',
]

SimComparisonVerdict = Literal[
    'comparable',
    'comparable_within_validated_band',
    'wrong_source_model',
    'insufficient_evidence',
]

LevelGateState = Literal[
    'sufficient',
    'insufficient',
    'unknown',
]

AggregationRole = Literal[
    'raw_orientation',
    'aggregate_member',
    'aggregate_result',
    'not_aggregated',
]


# ---------------------------------------------------------------------------
# Embedded blocks
# ---------------------------------------------------------------------------


class CadOmniBandCapability(BaseModel):
    """Omnidirectionality capability for one frequency band.

    ``state`` says how close to omnidirectional the source is inside
    ``band_low_hz``–``band_high_hz``; ``evidence_basis`` says why we
    believe it. A dodecahedron-shaped enclosure with no measured or lab
    evidence stays ``unknown_directivity`` — never inferred.
    """

    model_config = ConfigDict(frozen=True)

    band_low_hz: float
    band_high_hz: float
    state: OmniCapabilityState
    evidence_basis: OmniEvidenceBasis
    method_or_standard: str | None = None
    directivity_dataset_ref: AuthorityRef | None = None
    max_deviation_db: float | None = None
    uncertainty_db: float | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'CadOmniBandCapability':
        for label, value in (
            ('band_low_hz', self.band_low_hz),
            ('band_high_hz', self.band_high_hz),
            ('max_deviation_db', self.max_deviation_db),
            ('uncertainty_db', self.uncertainty_db),
        ):
            if value is not None:
                _require_finite(value, f'omni band {label}')
        if self.band_low_hz <= 0 or self.band_high_hz <= 0:
            raise ValueError('omni band edges must be positive')
        if self.band_high_hz <= self.band_low_hz:
            raise ValueError('omni band must be ascending')
        if self.uncertainty_db is not None and self.uncertainty_db < 0:
            raise ValueError('omni band uncertainty must be >= 0')
        if (
            self.state in _OMNI_CAPABLE_STATES
            and self.evidence_basis not in _STRONG_OMNI_BASES
        ):
            raise ValueError(
                f'omni state {self.state} requires measured/lab/standard '
                'evidence — geometry alone never proves omnidirectionality'
            )
        if self.directivity_dataset_ref is not None and (
            self.directivity_dataset_ref.ref_sha256 is None
        ):
            raise ValueError(
                'the directivity dataset pin must carry its sha256'
            )
        return self

    def covers_frequency_hz(self, hz: float) -> bool:
        return self.band_low_hz <= hz <= self.band_high_hz


class CadSourceLevelCapability(BaseModel):
    """Source output / dynamic-range evidence for decay measurement.

    ``achieved_decay_range_db`` is the measured SNR/decay span the source
    delivered in situ; ``limiter_state`` keeps protection electronics
    explicit — pushing a source past safe limits to hit a target dynamic
    range is never an option.
    """

    model_config = ConfigDict(frozen=True)

    max_test_level_db: float | None = None
    limiter_state: Literal[
        'none_declared', 'engaged', 'armed_disengaged', 'unknown'
    ] = 'unknown'
    achieved_decay_range_db: float | None = None
    snr_margin_db: float | None = None
    nonlinear_evidence: str | None = None

    @model_validator(mode='after')
    def valid_level(self) -> 'CadSourceLevelCapability':
        for label, value in (
            ('max_test_level_db', self.max_test_level_db),
            ('achieved_decay_range_db', self.achieved_decay_range_db),
            ('snr_margin_db', self.snr_margin_db),
        ):
            if value is not None:
                _require_finite(value, f'source level {label}')
        if self.achieved_decay_range_db is not None and (
            self.achieved_decay_range_db < 0
        ):
            raise ValueError('achieved_decay_range_db must be >= 0')
        return self


class CadSourcePose(BaseModel):
    """Exact physical source pose — position and orientation together.

    A practical omni source becomes directional at high frequency, so the
    orientation is part of measurement identity; a rotation/averaging
    procedure retains every orientation instead of collapsing to one
    scalar.
    """

    model_config = ConfigDict(frozen=True)

    x_m: float | None = None
    y_m: float | None = None
    z_m: float | None = None
    height_reference: str | None = None
    azimuth_deg: float | None = None
    tilt_deg: float | None = None
    orientation_label: str | None = None
    pose_evidence: str | None = None

    @model_validator(mode='after')
    def valid_pose(self) -> 'CadSourcePose':
        for label, value in (
            ('x_m', self.x_m),
            ('y_m', self.y_m),
            ('z_m', self.z_m),
            ('azimuth_deg', self.azimuth_deg),
            ('tilt_deg', self.tilt_deg),
        ):
            if value is not None:
                _require_finite(value, f'source pose {label}')
        return self


# ---------------------------------------------------------------------------
# Sealed authorities
# ---------------------------------------------------------------------------


class CadExcitationSourceProfile(BaseModel):
    """Sealed room-acoustic excitation-source profile.

    The source class participates in measurement identity: ``source_type``
    plus the evidenced omni band capabilities decide which measurand
    family the capture can support. ``measurand_class`` records what the
    measurement *claims* — the qualification verdict decides whether the
    claim is supportable.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    source_type: ExcitationSourceType
    measurand_class: MeasurandClass
    device_model: str | None = None
    instance_id: str | None = None
    source_geometry: str | None = None
    directivity_dataset_ref: AuthorityRef | None = None
    omni_capabilities: tuple[CadOmniBandCapability, ...] = ()
    pose: CadSourcePose | None = None
    drive_path: str | None = None
    dsp_eq_state: str | None = None
    level_capability: CadSourceLevelCapability | None = None
    stimulus_ref: AuthorityRef | None = None
    rotation_procedure: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadExcitationSourceProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        for label, ref in (
            ('directivity_dataset_ref', self.directivity_dataset_ref),
            ('stimulus_ref', self.stimulus_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256')
        # Source class vs claimed measurand coherence is checked at the
        # model boundary for the *impossible* combinations only — the
        # qualification verdict handles the graded cases.
        if (
            self.measurand_class == 'room_response_standardized_omni_source'
            and self.source_type == 'installed_loudspeaker_channel'
        ):
            raise ValueError(
                'an installed loudspeaker channel cannot claim the '
                'standardized omni-source measurand — use '
                'installed_channel_system_response or '
                'installed_loudspeaker_transfer'
            )
        if (
            self.measurand_class
            in ('installed_loudspeaker_transfer',
                'installed_channel_system_response')
            and self.source_type
            in ('dodecahedron_omni', 'standardized_omni_source')
        ):
            raise ValueError(
                'a standardized/omni source cannot claim the installed '
                'system measurand — keep the measurement families '
                'distinct'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('source profile hash mismatch')
        if self.profile_id != _semantic_id('srcpro', expected):
            raise ValueError('source profile id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_label': self.source_label,
            'source_type': self.source_type,
            'measurand_class': self.measurand_class,
            'device_model': self.device_model,
            'instance_id': self.instance_id,
            'source_geometry': self.source_geometry,
            'directivity_dataset_ref': (
                self.directivity_dataset_ref.model_dump(mode='json')
                if self.directivity_dataset_ref is not None
                else None
            ),
            'omni_capabilities': [
                band.model_dump(mode='json')
                for band in self.omni_capabilities
            ],
            'pose': (
                self.pose.model_dump(mode='json')
                if self.pose is not None
                else None
            ),
            'drive_path': self.drive_path,
            'dsp_eq_state': self.dsp_eq_state,
            'level_capability': (
                self.level_capability.model_dump(mode='json')
                if self.level_capability is not None
                else None
            ),
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None
                else None
            ),
            'rotation_procedure': self.rotation_procedure,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def omni_capability_at(
        self, hz: float
    ) -> CadOmniBandCapability | None:
        for band in self.omni_capabilities:
            if band.covers_frequency_hz(hz):
                return band
        return None


def excitation_profile_binding(
    profile: CadExcitationSourceProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='excitation_source_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class CadSourceOrientationCapture(BaseModel):
    """One immutable per-orientation source capture.

    Where a measurement rotates the source and averages, every raw
    acquisition is retained with its own pose and artifact digest; the
    aggregate is a #575 derived artifact pinned via ``transform_ref`` —
    never a silent relabel of the average as one direct physical IR.
    """

    model_config = ConfigDict(frozen=True)

    capture_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_ref: AuthorityRef
    pose: CadSourcePose
    aggregation_role: AggregationRole = 'raw_orientation'
    acquisition_ref: AuthorityRef | None = None
    capture_artifact_sha256: str | None = None
    transform_ref: AuthorityRef | None = None
    aggregate_of: tuple[str, ...] = ()
    captured_at_utc: str | None = None
    declared_at_utc: str = Field(min_length=1)
    capture_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_capture(self) -> 'CadSourceOrientationCapture':
        _require_iso8601(
            self.declared_at_utc, 'orientation capture declared_at_utc'
        )
        if self.captured_at_utc is not None:
            _require_iso8601(self.captured_at_utc, 'captured_at_utc')
        for label, ref in (
            ('acquisition_ref', self.acquisition_ref),
            ('transform_ref', self.transform_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256')
        _require_sha(
            self.capture_artifact_sha256, 'capture_artifact_sha256'
        )
        if self.aggregation_role == 'aggregate_result' and (
            self.transform_ref is None
        ):
            raise ValueError(
                'an aggregate_result must pin the #575 transform that '
                'produced it — the averaging rule is first-class '
                'provenance'
            )
        expected = _hash(self.identity_payload())
        if self.capture_sha256 != expected:
            raise ValueError('orientation capture hash mismatch')
        if self.capture_id != _semantic_id('srcori', expected):
            raise ValueError(
                'orientation capture id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_ref': self.source_ref.model_dump(mode='json'),
            'pose': self.pose.model_dump(mode='json'),
            'aggregation_role': self.aggregation_role,
            'acquisition_ref': (
                self.acquisition_ref.model_dump(mode='json')
                if self.acquisition_ref is not None
                else None
            ),
            'capture_artifact_sha256': self.capture_artifact_sha256,
            'transform_ref': (
                self.transform_ref.model_dump(mode='json')
                if self.transform_ref is not None
                else None
            ),
            'aggregate_of': list(self.aggregate_of),
            'captured_at_utc': self.captured_at_utc,
            'declared_at_utc': self.declared_at_utc,
        }


def orientation_capture_binding(
    capture: CadSourceOrientationCapture,
) -> AuthorityRef:
    return AuthorityRef(
        kind='source_orientation_capture',
        ref_id=capture.capture_id,
        ref_sha256=capture.capture_sha256,
    )


class CadMeasurementSourceQualification(BaseModel):
    """Sealed fail-closed verdict for one source × purpose × band.

    Every requested purpose reports an explicit eligibility state — a
    standardized-looking number never buries source incompatibility in a
    note. ``strength_g_gate`` keeps the absolute-strength boundary visible
    separately from the purpose verdicts.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_ref: AuthorityRef
    requested_band_low_hz: float | None = None
    requested_band_high_hz: float | None = None
    eligibilities: tuple[tuple[SourcePurposeClass, SourceEligibilityState], ...]
    strength_g_gate: Literal['eligible', 'ineligible', 'unknown']
    sim_comparison: SimComparisonVerdict | None = None
    level_gate: LevelGateState
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadMeasurementSourceQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.source_ref.ref_sha256 is None:
            raise ValueError('qualification must pin the source sha256')
        if not self.eligibilities:
            raise ValueError(
                'a source qualification must report at least one purpose '
                'eligibility'
            )
        purposes = [purpose for purpose, _ in self.eligibilities]
        if len(purposes) != len(set(purposes)):
            raise ValueError('duplicate purpose entries')
        for label, value in (
            ('requested_band_low_hz', self.requested_band_low_hz),
            ('requested_band_high_hz', self.requested_band_high_hz),
        ):
            if value is not None:
                _require_finite(value, f'qualification {label}')
        if (
            self.requested_band_low_hz is not None
            and self.requested_band_high_hz is not None
            and self.requested_band_high_hz <= self.requested_band_low_hz
        ):
            raise ValueError('requested band must be ascending')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('source qualification hash mismatch')
        if self.qualification_id != _semantic_id('srcqual', expected):
            raise ValueError(
                'source qualification id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'source_ref': self.source_ref.model_dump(mode='json'),
            'requested_band_low_hz': self.requested_band_low_hz,
            'requested_band_high_hz': self.requested_band_high_hz,
            'eligibilities': [
                list(item) for item in self.eligibilities
            ],
            'strength_g_gate': self.strength_g_gate,
            'sim_comparison': self.sim_comparison,
            'level_gate': self.level_gate,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    def eligibility_state(
        self, purpose: SourcePurposeClass
    ) -> SourceEligibilityState:
        return dict(self.eligibilities)[purpose]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_excitation_profile(
    *,
    document_id: str,
    source_label: str,
    source_type: ExcitationSourceType,
    measurand_class: MeasurandClass,
    device_model: str | None = None,
    instance_id: str | None = None,
    source_geometry: str | None = None,
    directivity_dataset_ref: AuthorityRef | None = None,
    omni_capabilities: tuple[CadOmniBandCapability, ...]
    | list[CadOmniBandCapability] = (),
    pose: CadSourcePose | None = None,
    drive_path: str | None = None,
    dsp_eq_state: str | None = None,
    level_capability: CadSourceLevelCapability | None = None,
    stimulus_ref: AuthorityRef | None = None,
    rotation_procedure: str | None = None,
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadExcitationSourceProfile:
    """Seal a room-acoustic excitation-source profile."""
    payload = dict(
        document_id=document_id,
        source_label=source_label,
        source_type=source_type,
        measurand_class=measurand_class,
        device_model=device_model,
        instance_id=instance_id,
        source_geometry=source_geometry,
        directivity_dataset_ref=directivity_dataset_ref,
        omni_capabilities=tuple(omni_capabilities),
        pose=pose,
        drive_path=drive_path,
        dsp_eq_state=dsp_eq_state,
        level_capability=level_capability,
        stimulus_ref=stimulus_ref,
        rotation_procedure=rotation_procedure,
        authority_version=EXCITATION_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadExcitationSourceProfile, payload,
        'profile_id', 'profile_sha256', 'srcpro',
    )


def build_orientation_capture(
    *,
    document_id: str,
    source_ref: AuthorityRef | CadExcitationSourceProfile,
    pose: CadSourcePose,
    aggregation_role: AggregationRole = 'raw_orientation',
    acquisition_ref: AuthorityRef | None = None,
    capture_artifact_sha256: str | None = None,
    transform_ref: AuthorityRef | None = None,
    aggregate_of: tuple[str, ...] | list[str] = (),
    captured_at_utc: str | None = None,
    declared_at_utc: str | None = None,
) -> CadSourceOrientationCapture:
    """Seal one immutable per-orientation capture."""
    if isinstance(source_ref, CadExcitationSourceProfile):
        source_ref = excitation_profile_binding(source_ref)
    payload = dict(
        document_id=document_id,
        source_ref=source_ref,
        pose=pose,
        aggregation_role=aggregation_role,
        acquisition_ref=acquisition_ref,
        capture_artifact_sha256=capture_artifact_sha256,
        transform_ref=transform_ref,
        aggregate_of=tuple(aggregate_of),
        captured_at_utc=captured_at_utc,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadSourceOrientationCapture, payload,
        'capture_id', 'capture_sha256', 'srcori',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _omni_state_for_request(
    profile: CadExcitationSourceProfile,
    band_low_hz: float | None,
    band_high_hz: float | None,
) -> tuple[OmniCapabilityState | None, list[str]]:
    """The worst omni capability state covering the requested band edges."""
    reasons: list[str] = []
    edges = [
        edge
        for edge in (band_low_hz, band_high_hz)
        if edge is not None
    ]
    if not edges:
        if not profile.omni_capabilities:
            return None, reasons
        # Whole declared envelope applies to an unbounded request — the
        # weakest band governs the envelope claim.
        states = [b.state for b in profile.omni_capabilities]
        order = [
            'omni_profile_verified',
            'omni_within_profile_band',
            'limited_directivity',
            'directional_source',
            'unknown_directivity',
        ]
        return max(states, key=lambda s: order.index(s)), reasons
    states: list[OmniCapabilityState] = []
    for edge in edges:
        band = profile.omni_capability_at(edge)
        if band is None:
            states.append('unknown_directivity')
            reasons.append(
                f'no declared directivity capability covers {edge} Hz — '
                'coverage outside the evidenced band is unknown'
            )
        else:
            states.append(band.state)
    order = [
        'omni_profile_verified',
        'omni_within_profile_band',
        'limited_directivity',
        'directional_source',
        'unknown_directivity',
    ]
    return max(states, key=lambda s: order.index(s)), reasons


def evaluate_source_qualification(
    *,
    document_id: str,
    profile: CadExcitationSourceProfile,
    purposes: tuple[SourcePurposeClass, ...] | list[SourcePurposeClass],
    requested_band_low_hz: float | None = None,
    requested_band_high_hz: float | None = None,
    simulated_source_model: Literal[
        'ideal_omni',
        'measured_directivity_matched',
        'exact_installed_source_model',
        'unknown',
    ] = 'unknown',
    evaluated_at_utc: str | None = None,
) -> CadMeasurementSourceQualification:
    """Fail-closed source-eligibility verdict for the requested purposes.

    ``simulated_source_model`` feeds the #564/#566 comparison verdict:
    measured dodecahedron vs simulated ideal omni is comparable only
    inside the evidenced omni band; an installed channel vs ideal omni is
    a wrong source model for direct residual.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []
    eligibilities: dict[SourcePurposeClass, SourceEligibilityState] = {}

    omni_state, omni_reasons = _omni_state_for_request(
        profile, requested_band_low_hz, requested_band_high_hz
    )
    reasons.extend(omni_reasons)

    room_class = profile.measurand_class in ROOM_CHARACTERIZATION_CLASSES
    level = profile.level_capability
    limiter_engaged = (
        level is not None and level.limiter_state == 'engaged'
    )

    # --- level gate ----------------------------------------------------------
    if level is None or (
        level.achieved_decay_range_db is None
        and level.max_test_level_db is None
    ):
        level_gate: LevelGateState = 'unknown'
        reasons.append(
            'no source output/dynamic-range evidence — decay metrics '
            'cannot confirm sufficient level'
        )
    elif limiter_engaged:
        level_gate = 'insufficient'
        reasons.append(
            'source limiter engaged — output was driven past linear '
            'limits'
        )
    else:
        level_gate = 'sufficient'

    # --- per-purpose eligibility ----------------------------------------------
    for purpose in purposes:
        if purpose == 'installed_system_diagnostics':
            # Installed-channel measurement stays first-class evidence —
            # it answers playback-system questions. An omni source also
            # legitimately serves diagnostics.
            if profile.measurand_class == 'external_unknown_source':
                eligibilities[purpose] = 'source_state_unknown'
            else:
                eligibilities[purpose] = 'eligible'

        elif purpose == 'standardized_room_characterization':
            if not room_class:
                eligibilities[purpose] = 'wrong_source_class'
                reasons.append(
                    f'{purpose}: measurand {profile.measurand_class} is '
                    'not a standardized omni-source room response — an '
                    'installed-channel IR never silently answers C80/T30/G'
                )
            elif omni_state in _OMNI_CAPABLE_STATES:
                if level_gate == 'insufficient':
                    eligibilities[purpose] = 'insufficient_source_level'
                elif profile.source_type == 'subwoofer':
                    eligibilities[purpose] = (
                        'eligible_with_source_limitation'
                    )
                    reasons.append(
                        'subwoofer excitation: position/boundary loading '
                        'and modal participation remain part of the '
                        'measurand — never automatic standardized status'
                    )
                else:
                    eligibilities[purpose] = 'eligible'
            elif omni_state == 'limited_directivity':
                eligibilities[purpose] = 'directivity_out_of_profile'
                reasons.append(
                    f'{purpose}: source directivity is limited inside '
                    'the requested band'
                )
            elif omni_state == 'directional_source':
                eligibilities[purpose] = 'wrong_source_class'
                reasons.append(
                    f'{purpose}: directional source cannot carry a '
                    'standardized omni-room response'
                )
            else:
                eligibilities[purpose] = 'source_state_unknown'
                reasons.append(
                    f'{purpose}: no omni evidence covers the requested '
                    'band — a dodecahedron shape alone proves nothing'
                )

        elif purpose == 'strength_g_measurement':
            # G requires an absolute reference to the same source under
            # the specified/free-field semantics — arbitrary installed
            # normalization is insufficient.
            if not room_class and (
                profile.measurand_class != 'reference_source_transfer'
            ):
                eligibilities[purpose] = 'wrong_source_class'
                reasons.append(
                    'Strength G requires a standardized/reference source '
                    'transfer — installed-channel IRs do not carry it'
                )
            elif level_gate == 'insufficient':
                # G is an absolute-level metric — a source driven into
                # its limiter cannot carry it regardless of omni proof.
                eligibilities[purpose] = 'insufficient_source_level'
                reasons.append(
                    f'{purpose}: source limiter engaged — output was '
                    'driven past linear limits'
                )
            elif omni_state == 'omni_profile_verified' and (
                level_gate == 'sufficient'
            ):
                eligibilities[purpose] = 'eligible'
            elif omni_state == 'omni_profile_verified':
                # Verified omni but no level evidence — the absolute
                # reference is unconfirmable, never a wrong class.
                eligibilities[purpose] = 'source_state_unknown'
                reasons.append(
                    f'{purpose}: no source output/dynamic-range '
                    'evidence — cannot confirm the absolute level '
                    'reference'
                )
            elif omni_state == 'omni_within_profile_band':
                eligibilities[purpose] = 'eligible_with_source_limitation'
                reasons.append(
                    'Strength G restricted to the verified omni band'
                )
            elif omni_state is None:
                eligibilities[purpose] = 'source_state_unknown'
            else:
                eligibilities[purpose] = 'wrong_source_class'

        elif purpose == 'spatial_impression_measurement':
            # Source side of the source+receiver pair (#213 owns the
            # receiver): needs room-class excitation; a wrong source never
            # gets rescued by a correct receiver.
            if not room_class:
                eligibilities[purpose] = 'wrong_source_class'
            elif omni_state in _OMNI_CAPABLE_STATES:
                eligibilities[purpose] = 'eligible'
            elif omni_state == 'limited_directivity':
                eligibilities[purpose] = 'eligible_with_source_limitation'
            elif omni_state == 'directional_source':
                eligibilities[purpose] = 'wrong_source_class'
            else:
                eligibilities[purpose] = 'source_state_unknown'

        else:  # simulation_validation_comparison
            eligibilities[purpose] = 'eligible'

    # --- simulation comparison verdict ---------------------------------------
    sim: SimComparisonVerdict | None
    if 'simulation_validation_comparison' not in eligibilities:
        sim = None
    elif simulated_source_model == 'unknown':
        sim = 'insufficient_evidence'
        reasons.append(
            'no simulated source model declared — cannot establish '
            'source-model compatibility'
        )
    elif profile.measurand_class in ROOM_CHARACTERIZATION_CLASSES:
        if simulated_source_model == 'measured_directivity_matched':
            sim = 'comparable'
        elif simulated_source_model == 'ideal_omni':
            if omni_state == 'omni_profile_verified':
                sim = 'comparable'
            elif omni_state == 'omni_within_profile_band':
                sim = 'comparable_within_validated_band'
                reasons.append(
                    'measured omni-class source vs simulated ideal omni: '
                    'comparable only inside the evidenced omni band'
                )
            else:
                sim = 'insufficient_evidence'
        elif simulated_source_model == 'exact_installed_source_model':
            sim = 'wrong_source_model'
        else:
            sim = 'insufficient_evidence'
    elif profile.measurand_class in (
        'installed_loudspeaker_transfer',
        'installed_channel_system_response',
    ):
        if simulated_source_model == 'exact_installed_source_model':
            sim = 'comparable'
        elif simulated_source_model == 'ideal_omni':
            sim = 'wrong_source_model'
            reasons.append(
                'installed loudspeaker vs simulated ideal omni is a '
                'wrong source model for a direct residual — never '
                'calibrate room materials to absorb the mismatch'
            )
        else:
            sim = 'insufficient_evidence'
    else:
        sim = 'insufficient_evidence'

    # --- Strength-G gate ----------------------------------------------------
    g_state = eligibilities.get('strength_g_measurement')
    if g_state == 'eligible':
        strength_g: Literal['eligible', 'ineligible', 'unknown'] = (
            'eligible'
        )
    elif g_state in (None, 'source_state_unknown'):
        strength_g = 'unknown'
    else:
        strength_g = 'ineligible'

    payload = dict(
        document_id=document_id,
        source_ref=excitation_profile_binding(profile),
        requested_band_low_hz=requested_band_low_hz,
        requested_band_high_hz=requested_band_high_hz,
        eligibilities=tuple(
            (purpose, eligibilities[purpose]) for purpose in purposes
        ),
        strength_g_gate=strength_g,
        sim_comparison=sim,
        level_gate=level_gate,
        reasons=tuple(reasons),
        evaluation_version=EXCITATION_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadMeasurementSourceQualification, payload,
        'qualification_id', 'qualification_sha256', 'srcqual',
    )


__all__ = [
    'AggregationRole',
    'CadExcitationSourceProfile',
    'CadMeasurementSourceQualification',
    'CadOmniBandCapability',
    'CadSourceLevelCapability',
    'CadSourceOrientationCapture',
    'CadSourcePose',
    'EXCITATION_EVALUATION_VERSION',
    'EXCITATION_SCHEMA_VERSION',
    'ExcitationSourceType',
    'LevelGateState',
    'MeasurandClass',
    'OmniCapabilityState',
    'OmniEvidenceBasis',
    'ROOM_CHARACTERIZATION_CLASSES',
    'SimComparisonVerdict',
    'SourceEligibilityState',
    'SourcePurposeClass',
    'build_excitation_profile',
    'build_orientation_capture',
    'evaluate_source_qualification',
    'excitation_profile_binding',
    'orientation_capture_binding',
]
