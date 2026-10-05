"""CEDIA/CTA-RP22 StandardsProfile authority (issue #579).

RP22 v1.2 (September 2023) defines four immersive-audio performance levels
through **21 engineering parameters** in Appendix A, grouped into spatial
resolution, dynamics and timbre. HTDT already evaluates the spatial/layout
subset through :mod:`cad_standards_profiles` (``rp22_spatial_profile``); this
module is the *profile authority* that declares all 21 parameters, binds
them to the exact external document identity registered in
:mod:`cad_external_standards` (``cedia-cta-rp22@v1.2``) and evaluates a
project parameter-by-parameter against a requested level.

Contract properties (issue #579):

- every parameter is a sealed declaration record: index, category, metric
  scope, exact level limits (or ``None`` where the standard writes ``N/A``),
  the recommended tier where the standard publishes one, definition,
  measurement semantics, source reference and the HTDT authority mapping —
  with an honest ``supported`` / ``supported_with_limitations`` /
  ``measurement_required`` / ``unsupported`` status;
- requested level, per-parameter achieved level and limiting parameters are
  kept distinct — a single average theater score can never hide a failed
  parameter (strict conformance = every applicable parameter meets the
  requested level);
- ``design_evaluation`` / ``as_built_evaluation`` /
  ``measured_commissioning_evaluation`` are distinct evaluation kinds: a
  design prediction never upgrades into a commissioning claim — evidence
  weaker than the evaluation kind resolves to ``insufficient_evidence``;
- dynamics parameters (12–14) record an explicit capability basis —
  ``nominal_spec_only`` sensitivity-plus-watts claims cannot satisfy the
  standard where compression/headroom is unverified;
- the SPL capability of the system is never conflated with a normal user
  listening level (CEDIA 2025/2026 reference-level clarification);
- a missing-value or unmapped parameter is UNKNOWN, never an implicit pass;
- the report matrix and the generated verification plan expose every
  per-parameter evidence state instead of an opaque verdict.

Licensing boundary: HTDT stores only the published parameter identities,
units, level limits and citations needed to evaluate — never the
recommended-practice text itself.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


RP22_PROFILE_AUTHORITY_VERSION = 'rp22-standards-profile-1'
RP22_EVALUATION_AUTHORITY_VERSION = 'rp22-evaluation-1'
RP22_MAPPING_VERSION = 'rp22-map-v1'
RP22_CALCULATION_VERSION = 'rp22-eval-v1'

_SHA256 = r'^[0-9a-f]{64}$'
_LEVELS = (1, 2, 3, 4)


RP22Category = Literal['spatial_resolution', 'dynamics', 'timbre']
"""The three parameter groups of RP22 Appendix A."""

RP22MetricScope = Literal[
    'seat_metric',
    'listening_area_metric',
    'room_metric',
    'system_capability_metric',
]
"""The aggregation scope a parameter is measured at (#579 §12). A
seat-metric failure is never averaged into a room-level pass."""

RP22MappingStatus = Literal[
    'supported',
    'supported_with_limitations',
    'measurement_required',
    'unsupported',
]
"""Whether an HTDT authority currently supplies the parameter value
(#579 §2). ``unsupported`` is an honest gap, never a silent pass."""

RP22EvidenceClass = Literal[
    'design_prediction',
    'as_built',
    'measured_commissioning',
    'unknown',
]
"""Evidence provenance of one observed parameter value (#579 §3)."""

RP22EvaluationKind = Literal[
    'design_evaluation',
    'as_built_evaluation',
    'measured_commissioning_evaluation',
]
"""The evaluation class being produced. A ``measured_commissioning``
evaluation only credits ``measured_commissioning`` evidence."""

RP22DynamicsBasis = Literal[
    'nominal_spec_only',
    'modelled_small_signal',
    'modelled_with_output_limits',
    'lab_measured_output',
    'in_room_measured_capability',
    'commissioned_verified',
]
"""SPL-capability evidence classes (#579 §7), ordered weakest→strongest."""

RP22LimitKind = Literal['minimum', 'maximum', 'boolean_allowed']

RP22ParameterVerdict = Literal[
    'met',
    'not_met',
    'insufficient_evidence',
    'not_applicable',
    'unsupported',
]

RP22ConformanceVerdict = Literal['met', 'not_met', 'indeterminate']

# Evidence-class strength per evaluation kind: an evaluation never credits
# evidence weaker than its own class (#579 §3).
_EVIDENCE_STRENGTH: dict[str, int] = {
    'unknown': 0,
    'design_prediction': 1,
    'as_built': 2,
    'measured_commissioning': 3,
}
_KIND_MIN_STRENGTH: dict[str, int] = {
    'design_evaluation': 1,
    'as_built_evaluation': 2,
    'measured_commissioning_evaluation': 3,
}

# Minimum capability basis a dynamics parameter (12–14) needs per
# evaluation kind (#579 §7): below this, compression/headroom evidence is
# unverified and the parameter is insufficient_evidence, not a pass.
_BASIS_RANK: dict[str, int] = {
    'nominal_spec_only': 0,
    'modelled_small_signal': 1,
    'modelled_with_output_limits': 2,
    'lab_measured_output': 3,
    'in_room_measured_capability': 4,
    'commissioned_verified': 5,
}
_KIND_MIN_BASIS_RANK: dict[str, int] = {
    'design_evaluation': 2,
    'as_built_evaluation': 3,
    'measured_commissioning_evaluation': 4,
}

_DYNAMICS_INDICES = frozenset({12, 13, 14})


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


# ---------------------------------------------------------------------------
# Parameter declaration (#579 §1, §2)
# ---------------------------------------------------------------------------


class RP22AlternativeLimit(BaseModel):
    """A conditional alternative limit the standard publishes inline
    (e.g. Parameter 2 ``15/13*`` — 13 discrete feeds only for an
    Auro-3D-specific room design)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    condition_tag: str = Field(min_length=1)
    levels: tuple[int, ...] = Field(min_length=1)
    minimum: float | None = None
    maximum: float | None = None
    boolean_allowed: bool | None = None
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'RP22AlternativeLimit':
        if not set(self.levels) <= set(_LEVELS):
            raise ValueError('alternative limit levels must be within 1..4')
        if len(set(self.levels)) != len(self.levels):
            raise ValueError('alternative limit levels must be unique')
        kinds = sum(
            value is not None
            for value in (self.minimum, self.maximum, self.boolean_allowed)
        )
        if kinds != 1:
            raise ValueError(
                'an alternative limit needs exactly one of '
                'minimum/maximum/boolean_allowed'
            )
        for value in (self.minimum, self.maximum):
            if value is not None and not isfinite(float(value)):
                raise ValueError('alternative limit must be finite')
        return self


class RP22ParameterSpec(BaseModel):
    """Sealed declaration of one RP22 Appendix A parameter.

    ``limits``/``recommended`` are 4-tuples indexed by level-1 … level-4;
    a ``None`` entry encodes the standard's literal ``N/A`` — the parameter
    does not apply at that level.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_index: int = Field(ge=1, le=21)
    parameter_id: str = Field(min_length=1)
    category: RP22Category
    scope: RP22MetricScope
    name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    limit_kind: RP22LimitKind
    limits: tuple[float | bool | None, float | bool | None,
                  float | bool | None, float | bool | None]
    recommended: tuple[float | None, float | None,
                       float | None, float | None] | None = None
    alternative_limits: tuple[RP22AlternativeLimit, ...] = ()
    strict_boundary: bool = False
    """True when the limit is a strict ``>``/``<`` (Parameter 1) rather
    than an inclusive bound."""
    definition: str = Field(min_length=1)
    measurement_semantics: str = ''
    htdt_authority: str = ''
    mapping_status: RP22MappingStatus
    mapping_limitations: str = ''
    required_inputs: tuple[str, ...] = ()
    source_reference: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'RP22ParameterSpec':
        if self.limit_kind == 'boolean_allowed':
            for level, value in enumerate(self.limits, start=1):
                if value is not None and not isinstance(value, bool):
                    raise ValueError(
                        f'level {level} limit of a boolean parameter must '
                        'be bool or None (N/A)'
                    )
        else:
            for level, value in enumerate(self.limits, start=1):
                if value is not None:
                    if isinstance(value, bool):
                        raise ValueError(
                            f'level {level} numeric limit cannot be bool'
                        )
                    if not isfinite(float(value)):
                        raise ValueError(
                            f'level {level} limit must be finite'
                        )
        if self.recommended is not None:
            for level, value in enumerate(self.recommended, start=1):
                if value is not None and not isfinite(float(value)):
                    raise ValueError(
                        f'level {level} recommended value must be finite'
                    )
        if len(set(self.required_inputs)) != len(self.required_inputs):
            raise ValueError('required_inputs must be unique')
        return self

    def limit_for_level(self, level: int) -> float | bool | None:
        if level not in _LEVELS:
            raise ValueError('RP22 performance level must be 1..4')
        return self.limits[level - 1]

    def recommended_for_level(self, level: int) -> float | None:
        if level not in _LEVELS:
            raise ValueError('RP22 performance level must be 1..4')
        if self.recommended is None:
            return None
        return self.recommended[level - 1]

    def applies_at_level(self, level: int) -> bool:
        return self.limit_for_level(level) is not None


class RP22StandardsProfile(BaseModel):
    """Sealed, versioned declaration of the full 21-parameter RP22 profile
    bound to one exact external document edition (#579 §1)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rp22-standards-profile-1'
    ] = RP22_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    standard_id: str = Field(min_length=1)
    edition: str = Field(min_length=1)
    registry_key: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    parameters: tuple[RP22ParameterSpec, ...]
    interpretation_notes: str = ''
    created_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={'profile_sha256'})

    @model_validator(mode='after')
    def _check(self) -> 'RP22StandardsProfile':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        indices = [param.parameter_index for param in self.parameters]
        if sorted(indices) != list(range(1, 22)):
            raise ValueError(
                'an RP22 profile must declare exactly parameters 1..21'
            )
        ids = [param.parameter_id for param in self.parameters]
        if len(ids) != len(set(ids)):
            raise ValueError('parameter ids must be unique')
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('RP22 profile hash mismatch')
        return self

    def parameter(self, parameter_id: str) -> RP22ParameterSpec | None:
        for param in self.parameters:
            if param.parameter_id == parameter_id:
                return param
        return None


def build_rp22_profile(
    *,
    profile_id: str,
    standard_id: str,
    edition: str,
    publisher: str,
    document_title: str,
    document_version: str,
    source_uri: str,
    parameters: tuple[RP22ParameterSpec, ...],
    mapping_version: str = RP22_MAPPING_VERSION,
    calculation_version: str = RP22_CALCULATION_VERSION,
    interpretation_notes: str = '',
    created_at_utc: str | None = None,
) -> RP22StandardsProfile:
    payload = canonicalize_payload(
        RP22StandardsProfile,
        dict(
            authority_version=RP22_PROFILE_AUTHORITY_VERSION,
            profile_id=profile_id,
            standard_id=standard_id,
            edition=edition,
            registry_key=f'{standard_id}@{edition}',
            publisher=publisher,
            document_title=document_title,
            document_version=document_version,
            source_uri=source_uri,
            mapping_version=mapping_version,
            calculation_version=calculation_version,
            parameters=tuple(parameters),
            interpretation_notes=interpretation_notes,
            created_at_utc=created_at_utc or _utc_now(),
            profile_sha256='0' * 64,
        ),
    )
    probe = RP22StandardsProfile.model_construct(**payload)
    return RP22StandardsProfile(
        **probe.model_dump(mode='python', exclude={'profile_sha256'}),
        profile_sha256=_hash(probe.semantic_payload()),
    )


def _spec(
    index: int,
    category: RP22Category,
    scope: RP22MetricScope,
    name: str,
    unit: str,
    limit_kind: RP22LimitKind,
    limits: tuple[Any, Any, Any, Any],
    *,
    recommended: tuple[Any, ...] | None = None,
    alternative_limits: tuple[RP22AlternativeLimit, ...] = (),
    strict_boundary: bool = False,
    definition: str,
    measurement_semantics: str = '',
    htdt_authority: str = '',
    mapping_status: RP22MappingStatus = 'supported',
    mapping_limitations: str = '',
    required_inputs: tuple[str, ...] = (),
    source_reference: str,
    note: str = '',
) -> RP22ParameterSpec:
    return RP22ParameterSpec(
        parameter_index=index,
        parameter_id=f'rp22.p{index:02d}',
        category=category,
        scope=scope,
        name=name,
        unit=unit,
        limit_kind=limit_kind,
        limits=limits,
        recommended=recommended,
        alternative_limits=alternative_limits,
        strict_boundary=strict_boundary,
        definition=definition,
        measurement_semantics=measurement_semantics,
        htdt_authority=htdt_authority,
        mapping_status=mapping_status,
        mapping_limitations=mapping_limitations,
        required_inputs=required_inputs,
        source_reference=source_reference,
        note=note,
    )


def rp22_v1_2_parameter_specs() -> tuple[RP22ParameterSpec, ...]:
    """All 21 RP22 v1.2 Appendix A parameters with exact level limits.

    Values transcribed from the published v1.2 table (Appendix A.1);
    ``None`` entries reproduce the standard's literal ``N/A``. Where the
    table publishes both a minimum (``Min.``) and a recommended (``Rec.``)
    tier, both are declared — recommended values are guidance targets,
    never the conformance boundary.
    """
    spatial = 'spatial_resolution'
    dynamics = 'dynamics'
    timbre = 'timbre'
    return (
        _spec(
            1, spatial, 'seat_metric',
            'Minimum distance between the listening area and the room '
            'walls (dsw, dbw)',
            'm', 'minimum', (0.5, 0.8, 1.2, 1.5),
            strict_boundary=True,
            definition=(
                'Distance from the centre of each listener\'s head to the '
                'nearest boundary wall or protruding speaker baffle; helps '
                'avoid speakers being too loud for some and too quiet for '
                'others while minimizing wall-boundary interference.'
            ),
            measurement_semantics=(
                'Per-seat geometric distance; evaluated individually for '
                'every seat.'
            ),
            htdt_authority='cad_standards_layout_observation + scene geometry',
            mapping_status='supported',
            required_inputs=('listener_head_to_nearest_room_boundary_m',),
            source_reference='Appendix A, Parameter 1; §4.1.4',
            note='Strict greater-than boundary per Appendix A.',
        ),
        _spec(
            2, spatial, 'room_metric',
            'Decoder/renderer capability and discretely rendered speaker '
            'configuration, excl. subwoofers',
            'count', 'minimum', (5, 11, 15, 15),
            alternative_limits=(
                RP22AlternativeLimit(
                    condition_tag='auro3d_room_design',
                    levels=(3, 4),
                    minimum=13.0,
                    note=(
                        'An Auro-3D-specific room design requires 13 '
                        'discrete feeds; for other formats the minimum is '
                        '15 at Levels 3 and 4.'
                    ),
                ),
            ),
            definition=(
                'Minimum number of discrete speaker feeds from the '
                'processor/OAR: all listener-level and upper discrete '
                'outputs, excluding LFE/subwoofers.'
            ),
            measurement_semantics=(
                'Count of discretely rendered speaker feeds the topology '
                'provides; subwoofer outputs never count toward it.'
            ),
            htdt_authority='speaker topology / device capability',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Requires the declared count of discretely rendered feeds; '
                'HTDT does not infer renderer capability from physical '
                'speaker count alone.'
            ),
            required_inputs=('discrete_rendered_speaker_count',),
            source_reference='Appendix A, Parameter 2; Appendix E.2',
        ),
        _spec(
            3, spatial, 'room_metric',
            'Number of screen wall speakers allowed outside of recommended '
            'zonal locations',
            'count', 'maximum', (0, 0, 0, 0),
            definition=(
                'Speaker locations are zones/areas — not strict angle '
                'numbers — resulting from multiple trade-offs; zero '
                'screen-wall speakers may sit outside the recommended '
                'zones.'
            ),
            measurement_semantics=(
                'Zonal-location evaluation per recommended-zone profile; '
                'angle-only checks are not a substitute.'
            ),
            htdt_authority='rp22-recommended-zone-evaluation-v1 + layout profile',
            mapping_status='supported',
            required_inputs=(
                'screen_wall_speakers_outside_recommended_zone_count',
            ),
            source_reference='Appendix A, Parameter 3; §5.5.4',
        ),
        _spec(
            4, spatial, 'seat_metric',
            'Maximum SPL difference between screen wall speakers',
            'dB', 'maximum', (6.0, 5.0, 4.0, 2.0),
            definition=(
                'For every seat individually, the maximum predicted SPL '
                'difference between any two screen wall speakers, '
                'normalized to the RSP (0 dB variation there), using '
                'anechoic propagation loss.'
            ),
            measurement_semantics=(
                'Anechoic-propagation prediction at each seat; measured '
                'verification uses banded in-room responses per seat.'
            ),
            htdt_authority='cad_direct_level / source output prediction + measurement',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Design evaluation covers anechoic propagation only; '
                'in-room contributions need measured evidence.'
            ),
            required_inputs=('screen_speaker_spl_difference_db',),
            source_reference='Appendix A, Parameter 4',
        ),
        _spec(
            5, spatial, 'seat_metric',
            'Maximum allowable horizontal angle between adjacent surround '
            'speakers',
            'deg', 'maximum', (None, 80.0, 60.0, 50.0),
            definition=(
                'Maximum horizontal angle between adjacent surround '
                'speakers at the seating location, so sound movement is '
                'smooth and localization accurate.'
            ),
            measurement_semantics='Per-seat angular evaluation.',
            htdt_authority='layout-angle-v1 + scene geometry',
            mapping_status='supported',
            required_inputs=('max_adjacent_surround_horizontal_angle_deg',),
            source_reference='Appendix A, Parameter 5; §5.6.2.1',
        ),
        _spec(
            6, spatial, 'seat_metric',
            'Maximum SPL difference between surround speakers',
            'dB', 'maximum', (10.0, 6.0, 4.0, 2.0),
            definition=(
                'For every seat individually, the maximum predicted SPL '
                'difference between any two listener-level surround '
                'speakers, normalized to the RSP, using anechoic '
                'propagation loss.'
            ),
            measurement_semantics=(
                'Anechoic-propagation prediction at each seat; measured '
                'verification uses banded in-room responses per seat.'
            ),
            htdt_authority='cad_direct_level / source output prediction + measurement',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Design evaluation covers anechoic propagation only; '
                'in-room contributions need measured evidence.'
            ),
            required_inputs=('surround_speaker_spl_difference_db',),
            source_reference='Appendix A, Parameter 6',
        ),
        _spec(
            7, spatial, 'room_metric',
            'Wide speakers (if implemented) maximum allowable horizontal '
            'deviation from median angle',
            'deg', 'maximum', (10.0, 7.0, 5.0, 2.0),
            definition=(
                'Maximum horizontal angular deviation allowed from the '
                'ideal median angular location for wide front speakers.'
            ),
            measurement_semantics='Signed horizontal deviation, absolute value.',
            htdt_authority='layout-angle-v1 + scene geometry',
            mapping_status='supported',
            required_inputs=('wide_horizontal_deviation_from_median_deg',),
            source_reference='Appendix A, Parameter 7; §5.7',
        ),
        _spec(
            8, spatial, 'room_metric',
            'Upfiring/elevation speakers allowed?',
            'boolean', 'boolean_allowed', (True, True, False, False),
            definition=(
                'Whether up-firing/elevation (e.g. "Atmos Enabled") '
                'speakers aimed at a reflective ceiling surface may be '
                'used to reproduce immersive content.'
            ),
            measurement_semantics=(
                'Declared rendering mode of the speaker topology.'
            ),
            htdt_authority='speaker-rendering-mode-v1 + speaker topology',
            mapping_status='supported',
            required_inputs=('uses_upfiring_elevation_speakers',),
            source_reference='Appendix A, Parameter 8',
            note=(
                'Levels 1 and 2 say such speakers are allowed — not '
                'required; only Levels 3 and 4 are evaluated as a '
                'prohibition.'
            ),
        ),
        _spec(
            9, spatial, 'seat_metric',
            'Maximum allowable vertical angle between adjacent (L/R rows '
            'of) upper speakers',
            'deg', 'maximum', (None, 80.0, 60.0, 50.0),
            definition=(
                'Maximum vertical angle between adjacent upper speakers at '
                'the seating location. Excludes top middle center '
                '("Voice of God") and height center speakers.'
            ),
            measurement_semantics='Per-seat angular evaluation.',
            htdt_authority='layout-angle-v1 + scene geometry',
            mapping_status='supported',
            required_inputs=('max_adjacent_upper_vertical_angle_deg',),
            source_reference='Appendix A, Parameter 9; §5.8.2',
        ),
        _spec(
            10, spatial, 'seat_metric',
            'Maximum SPL difference between upper speakers',
            'dB', 'maximum', (12.0, 8.0, 5.0, 2.0),
            definition=(
                'For every seat individually, the maximum predicted SPL '
                'difference between any two height/upper speakers, '
                'normalized to the RSP, using anechoic propagation loss.'
            ),
            measurement_semantics=(
                'Anechoic-propagation prediction at each seat; measured '
                'verification uses banded in-room responses per seat.'
            ),
            htdt_authority='cad_direct_level / source output prediction + measurement',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Design evaluation covers anechoic propagation only; '
                'in-room contributions need measured evidence.'
            ),
            required_inputs=('upper_speaker_spl_difference_db',),
            source_reference='Appendix A, Parameter 10',
        ),
        _spec(
            11, spatial, 'room_metric',
            'Number of surround/wide/upper speakers allowed outside of '
            'zonal recommendation locations',
            'count', 'maximum', (None, 0.0, 0.0, 0.0),
            definition=(
                'Zero surround, wide, and upper speakers may sit outside '
                'the designated zonal areas for their groups.'
            ),
            measurement_semantics=(
                'Zonal-location evaluation per recommended-zone profile.'
            ),
            htdt_authority='rp22-recommended-zone-evaluation-v1 + layout profile',
            mapping_status='supported',
            required_inputs=(
                'surround_wide_upper_speakers_outside_recommended_zone_count',
            ),
            source_reference='Appendix A, Parameter 11; §5.9.3',
        ),
        _spec(
            12, dynamics, 'system_capability_metric',
            'Screen speakers SPL capability at RSP (post calibration EQ, '
            'within assigned bandwidth) without clipping',
            'dB SPL (C)', 'minimum', (99.0, 102.0, 105.0, 108.0),
            recommended=(102.0, 105.0, 108.0, 111.0),
            definition=(
                'Recommended minimum long-term SPL capability at the '
                'Reference Seating Position according to AES75-2022 or '
                'ANSI-CTA-2034-A §8 — including allowance for bass '
                'contours and positive calibration-EQ gain.'
            ),
            measurement_semantics=(
                'System *capability*, not normal listening level: the '
                'headroom exists for peaks/reference playback; it does not '
                'prescribe everyday level. Evidence must include '
                'output-limit/compression information for credit.'
            ),
            htdt_authority='cad_usable_output_profiles + CTA-2034 evidence + amplifier headroom (#192)',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Capability claims need a modelled-with-limits or measured '
                'basis; a sensitivity-plus-amplifier-watts figure is not '
                'evaluatable.'
            ),
            required_inputs=('screen_speaker_spl_capability_db',),
            source_reference='Appendix A, Parameter 12; §11',
        ),
        _spec(
            13, dynamics, 'system_capability_metric',
            'Non-screen speakers SPL capability at RSP (post calibration '
            'EQ, within assigned bandwidth) without clipping (includes '
            'amplifier headroom)',
            'dB SPL (C)', 'minimum', (96.0, 99.0, 102.0, 105.0),
            recommended=(99.0, 102.0, 105.0, 108.0),
            definition=(
                'Recommended minimum long-term SPL capability at the RSP '
                'for non-screen channels, with the same AES75/CTA-2034 '
                'basis and headroom allowances as Parameter 12.'
            ),
            measurement_semantics=(
                'System capability, not normal listening level; includes '
                'amplifier headroom.'
            ),
            htdt_authority='cad_usable_output_profiles + CTA-2034 evidence + amplifier headroom (#192)',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Capability claims need a modelled-with-limits or measured '
                'basis.'
            ),
            required_inputs=('non_screen_speaker_spl_capability_db',),
            source_reference='Appendix A, Parameter 13; §11',
        ),
        _spec(
            14, dynamics, 'system_capability_metric',
            'LFE frequencies total SPL capability at RSP, plus bass '
            'management if used (post calibration EQ, within bass '
            'extension spec for the level) without clipping (includes '
            'amplifier headroom)',
            'dB SPL (C)', 'minimum', (109.0, 112.0, 115.0, 118.0),
            recommended=(114.0, 117.0, 120.0, 123.0),
            definition=(
                'Total system SPL capability at LFE frequencies for '
                'speakers and/or subwoofers; may include room/boundary '
                'gain and summing of multiple speakers/subwoofers acting '
                'as one virtual subwoofer.'
            ),
            measurement_semantics=(
                'Must compose the actual bass-management routing and '
                'subwoofer count — theoretical boundary/multi-sub gain is '
                'never counted twice.'
            ),
            htdt_authority='cad_bass_management_qualification (#574) + cad_usable_output_profiles + headroom (#224/#569)',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Requires the exact bass-management/multi-sub state; an '
                'unqualified summed figure is insufficient.'
            ),
            required_inputs=('lfe_spl_capability_db',),
            source_reference='Appendix A, Parameter 14; §11, Appendix B',
        ),
        _spec(
            15, dynamics, 'room_metric',
            'Background noise floor with all AV equipment and mechanical '
            'systems and building services switched on, at nominal '
            'operating temperatures',
            'NCB', 'maximum', (35.0, 26.0, 22.0, 18.0),
            recommended=(26.0, 22.0, 18.0, 15.0),
            definition=(
                'Level of general background noise discernible with all '
                'systems running (including HVAC) during regular operation '
                'of the entertainment space while no multimedia content '
                'plays.'
            ),
            measurement_semantics=(
                'NCB rating from octave-band levels under the operating '
                'room/device state — a silent/cold-room measurement is '
                'not evidence (operating-state snapshot composes with '
                '#573).'
            ),
            htdt_authority='room/noise measurement authority + state snapshot (#573)',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Requires an actual banded noise measurement under the '
                'operating state; HTDT does not predict NCB.'
            ),
            required_inputs=('background_noise_ncb_rating',),
            source_reference='Appendix A, Parameter 15; Appendix C.2',
        ),
        _spec(
            16, timbre, 'seat_metric',
            'Seat-to-seat frequency response variance across all screen '
            'wall speakers normalized to measured RSP response between '
            '500 Hz and 16 kHz (1 octave smoothing)',
            'dB', 'maximum', (5.0, 3.0, 1.5, 1.5),
            definition=(
                'How similar the experience and performance level is '
                'across multiple seats: per-seat response variance of '
                'screen-wall speakers relative to the RSP response over '
                '500 Hz–16 kHz with 1-octave smoothing.'
            ),
            measurement_semantics=(
                'Design prediction considers speaker alignment, off-axis '
                'response on horizontal and vertical axes, and room '
                'effects; commissioning uses measured per-seat responses.'
            ),
            htdt_authority='multi-seat optimization evidence (#266/#569) + measurement',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Prediction is a design input only; the conformance claim '
                'requires measured per-seat responses with the declared '
                '1-octave smoothing.'
            ),
            required_inputs=('seat_to_seat_fr_variance_screen_db',),
            source_reference='Appendix A, Parameter 16',
        ),
        _spec(
            17, timbre, 'seat_metric',
            'Seat-to-seat frequency response variance across all '
            'wide/surround/upper speakers normalized to measured RSP '
            'response between 500 Hz and 16 kHz (1 octave smoothing)',
            'dB', 'maximum', (None, None, 3.0, 1.5),
            definition=(
                'Per-seat response variance of wide, surround and upper '
                'speakers relative to the RSP response over 500 Hz–16 kHz '
                'with 1-octave smoothing.'
            ),
            measurement_semantics=(
                'Same per-seat methodology as Parameter 16, evaluated '
                'only at Levels 3 and 4.'
            ),
            htdt_authority='multi-seat optimization evidence (#266/#569) + measurement',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Requires measured per-seat responses with the declared '
                'smoothing.'
            ),
            required_inputs=('seat_to_seat_fr_variance_surround_upper_db',),
            source_reference='Appendix A, Parameter 17',
        ),
        _spec(
            18, timbre, 'room_metric',
            'In-room bass extension -3 dB cut off frequency point',
            'Hz', 'minimum', (35.0, 30.0, 20.0, 18.0),
            recommended=(30.0, 25.0, 18.0, 15.0),
            definition=(
                'In-room predicted -3 dB bass extension frequency with no '
                'perceptible distortion or audible mechanical resonances '
                '(e.g. rattles) at the Parameter 14 SPL — including '
                'speaker coupling, boundary and room gain.'
            ),
            measurement_semantics=(
                'Extension is claimed only together with the distortion/'
                'rattle-free condition at the specified SPL.'
            ),
            htdt_authority='bass qualification (#569/#571/#574) + usable output',
            mapping_status='supported_with_limitations',
            mapping_limitations=(
                'Design extension is predictable; the no-rattle/no-'
                'distortion condition at level SPL still requires '
                'verification evidence.'
            ),
            required_inputs=('bass_extension_hz',),
            source_reference='Appendix A, Parameter 18; Appendix B',
        ),
        _spec(
            19, timbre, 'seat_metric',
            'Frequency response below the room\'s transition frequency at '
            'the RSP relative to target curve (1/3 octave smoothing). '
            '"The Result"',
            'dB', 'maximum', (5.0, 4.0, 3.0, 2.0),
            definition=(
                'Smoothness of the frequency response at the RSP relative '
                'to a pre-determined target curve, below the room\'s '
                'transition frequency, with 1/3-octave smoothing.'
            ),
            measurement_semantics=(
                'Evaluated against a declared target profile (#588) with '
                'exact smoothing/banding — never a generic flatness '
                'score; conventional diffuse-field RT is not a '
                'modal-region substitute (composes with #571).'
            ),
            htdt_authority='response-target authority (#588) + measurement transform (#575) + transition-frequency semantics (#571)',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Needs a pinned target profile and measured RSP response '
                'below the declared transition frequency.'
            ),
            required_inputs=('lf_response_vs_target_db',),
            source_reference='Appendix A, Parameter 19; §3.4, Appendix B',
        ),
        _spec(
            20, timbre, 'seat_metric',
            'Seat-to-seat frequency response relative to measured RSP '
            'response below the room\'s transition frequency per seat '
            '(1/3 octave smoothing). "The Consistency"',
            'dB', 'maximum', (None, 4.0, 3.0, 2.0),
            definition=(
                'Per-seat frequency-response agreement with the measured '
                'RSP response below the room\'s transition frequency, '
                'with 1/3-octave smoothing.'
            ),
            measurement_semantics=(
                'Per-seat metric below the declared transition frequency; '
                'evaluated from Level 2 upward.'
            ),
            htdt_authority='response-target authority (#588) + measurement (#575) + transition-frequency semantics (#571)',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Requires measured per-seat responses below the '
                'transition frequency.'
            ),
            required_inputs=('seat_to_seat_lf_variance_db',),
            source_reference='Appendix A, Parameter 20; Appendix B',
        ),
        _spec(
            21, timbre, 'room_metric',
            'Level of early reflections relative to direct sound '
            '(0-15 ms, 1-8 kHz)',
            'dB', 'maximum', (None, -8.0, -10.0, -12.0),
            definition=(
                'Management of early reflections for an optimum balance '
                'of direct and reflected sound: early-reflection level '
                'relative to direct sound in the 0–15 ms window over '
                '1–8 kHz — a value at or below the limit means '
                'reflections sit at least that many dB under the direct '
                'sound.'
            ),
            measurement_semantics=(
                'Requires an exact direct/reference arrival and '
                'reflection-window definition; a statistical RT model is '
                'not reflection-level evidence (composes with #2/#564/'
                '#566).'
            ),
            htdt_authority='hybrid solver / reflection-path authority (#2/#566) + measurement',
            mapping_status='measurement_required',
            mapping_limitations=(
                'Solver support exists for design prediction; the '
                'conformance claim needs reflection-window measurement '
                'or solver evidence with declared uncertainty.'
            ),
            required_inputs=('early_reflection_level_db',),
            source_reference='Appendix A, Parameter 21; §7',
        ),
    )


def rp22_v1_2_profile(
    *, created_at_utc: str | None = None
) -> RP22StandardsProfile:
    """The built-in versioned RP22 v1.2 profile, bound to the external
    standards registry key ``cedia-cta-rp22@v1.2``."""
    return build_rp22_profile(
        profile_id='cedia-cta-rp22-v1.2-full',
        standard_id='cedia-cta-rp22',
        edition='v1.2',
        publisher='CEDIA / Consumer Technology Association (CTA)',
        document_title=(
            'CEDIA/CTA-RP22 Recommended Practice for Immersive Audio Design'
        ),
        document_version='v1.2, September 2023',
        source_uri=(
            'https://cedia.org/site/assets/files/6057/'
            'cedia-cta_rp22_v1_2_sept_2023.pdf'
        ),
        parameters=rp22_v1_2_parameter_specs(),
        interpretation_notes=(
            'Evaluates parameter declarations and published level limits '
            'only; HTDT stores no recommended-practice text and makes no '
            'CEDIA/CTA certification or endorsement claim. Where the '
            'standard publishes an N/A cell the parameter does not apply '
            'at that level. Where it publishes Min. and Rec. tiers, only '
            'the Min. tier is the conformance boundary. Parameters mapped '
            'measurement_required or unsupported are reported honestly '
            'rather than silently satisfied. SPL-capability parameters '
            'describe system headroom — never a prescribed everyday '
            'listening level (CEDIA reference-level clarification, '
            '2025-10/2026-01).'
        ),
        created_at_utc=created_at_utc,
    )


# ---------------------------------------------------------------------------
# Observations + evaluation (#579 §3, §4, §5, §12, §13)
# ---------------------------------------------------------------------------


class RP22ParameterObservation(BaseModel):
    """One observed value for one parameter, with its evidence class.

    For seat-scope parameters the value is the claimed aggregate across
    the declared ``seat_scope`` (e.g. the worst seat) — the record keeps
    which seats the claim covers; an empty scope on a seat metric is a
    declared limitation, not silent room coverage.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    value: float | bool | None = None
    evidence_class: RP22EvidenceClass = 'unknown'
    evidence_ref: str | None = None
    seat_scope: tuple[str, ...] = ()
    basis: RP22DynamicsBasis | None = None
    """SPL-capability evidence class — valid only on dynamics parameters
    12–14 (#579 §7)."""
    context_tag: str | None = None
    """Optional condition tag matching an alternative limit (e.g.
    ``auro3d_room_design`` for Parameter 2)."""
    note: str = ''

    @model_validator(mode='after')
    def _check(self) -> 'RP22ParameterObservation':
        if isinstance(self.value, bool):
            pass
        elif self.value is not None and not isfinite(float(self.value)):
            raise ValueError('observed value must be finite')
        if len(set(self.seat_scope)) != len(self.seat_scope):
            raise ValueError('seat_scope entries must be unique')
        return self


class RP22ParameterResult(BaseModel):
    """Per-parameter evaluation outcome: verdict at the requested level,
    the achieved level the evidence supports, and the limitation notes —
    never folded into a scalar score (#579 §4, §12)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    parameter_index: int = Field(ge=1, le=21)
    category: RP22Category
    scope: RP22MetricScope
    mapping_status: RP22MappingStatus
    observed_value: float | bool | None = None
    evidence_class: RP22EvidenceClass = 'unknown'
    basis: RP22DynamicsBasis | None = None
    requested_level: int = Field(ge=1, le=4)
    target_at_requested_level: float | bool | None = None
    recommended_at_requested_level: float | None = None
    verdict: RP22ParameterVerdict
    achieved_level: int | None = Field(default=None, ge=1, le=4)
    achieved_by_alternative: bool = False
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'RP22ParameterResult':
        if isinstance(self.observed_value, bool):
            pass
        elif self.observed_value is not None and not isfinite(
            float(self.observed_value)
        ):
            raise ValueError('observed value must be finite')
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError('limitations must be unique')
        return self


def _meets_limit(
    spec: RP22ParameterSpec,
    level: int,
    value: float | bool,
    *,
    context_tag: str | None = None,
) -> tuple[bool, bool]:
    """Compare an observed value to the level limit.

    Returns ``(met, used_alternative)``. ``limit_kind`` decides the
    comparison; ``strict_boundary`` applies the standard's strict ``>``.
    A matching ``context_tag`` substitutes the declared alternative limit.
    """
    raw_limit: float | bool | None = spec.limit_for_level(level)
    used_alternative = False
    if context_tag is not None:
        for alternative in spec.alternative_limits:
            if (
                alternative.condition_tag == context_tag
                and level in alternative.levels
            ):
                if alternative.minimum is not None:
                    raw_limit = alternative.minimum
                elif alternative.maximum is not None:
                    raw_limit = alternative.maximum
                else:
                    raw_limit = alternative.boolean_allowed
                used_alternative = True
                break
    if raw_limit is None:
        return False, used_alternative
    if spec.limit_kind == 'boolean_allowed':
        observed = bool(value)
        return (observed is False or bool(raw_limit)), used_alternative
    observed = float(value)
    limit = float(raw_limit)
    if spec.limit_kind == 'minimum':
        if spec.strict_boundary:
            return observed > limit, used_alternative
        return observed >= limit, used_alternative
    return observed <= limit, used_alternative


def _limitation(
    result: RP22ParameterResult, *notes: str
) -> RP22ParameterResult:
    return result.model_copy(
        update={'limitations': tuple(dict.fromkeys(result.limitations + notes))}
    )


def evaluate_rp22_parameter(
    spec: RP22ParameterSpec,
    observation: RP22ParameterObservation | None,
    *,
    requested_level: int,
    evaluation_kind: RP22EvaluationKind,
) -> RP22ParameterResult:
    """Evaluate one parameter against the requested level.

    Verdict vocabulary is fail-closed: missing/unmapped/weak evidence is
    ``insufficient_evidence`` or ``unsupported`` — never an implicit pass —
    and a parameter the standard marks ``N/A`` at the requested level is
    ``not_applicable``, not a hidden success.
    """
    base = dict(
        parameter_id=spec.parameter_id,
        parameter_index=spec.parameter_index,
        category=spec.category,
        scope=spec.scope,
        mapping_status=spec.mapping_status,
        requested_level=requested_level,
        target_at_requested_level=spec.limit_for_level(requested_level),
        recommended_at_requested_level=(
            spec.recommended_for_level(requested_level)
        ),
    )
    if not spec.applies_at_level(requested_level):
        return RP22ParameterResult(
            **base,
            verdict='not_applicable',
            limitations=('parameter_not_required_at_requested_level',),
        )
    if spec.mapping_status == 'unsupported':
        return RP22ParameterResult(
            **base,
            verdict='unsupported',
            limitations=('no_htdt_authority_mapping',),
        )
    if observation is None or observation.value is None:
        return RP22ParameterResult(
            **base,
            verdict='insufficient_evidence',
            limitations=('no_observation',),
        )
    if observation.basis is not None and spec.parameter_index not in (
        _DYNAMICS_INDICES
    ):
        return RP22ParameterResult(
            **base,
            observed_value=observation.value,
            evidence_class=observation.evidence_class,
            basis=observation.basis,
            verdict='insufficient_evidence',
            limitations=('capability_basis_on_non_dynamics_parameter',),
        )
    strength = _EVIDENCE_STRENGTH[observation.evidence_class]
    limitations: list[str] = []
    if strength < _KIND_MIN_STRENGTH[evaluation_kind]:
        return RP22ParameterResult(
            **base,
            observed_value=observation.value,
            evidence_class=observation.evidence_class,
            basis=observation.basis,
            verdict='insufficient_evidence',
            limitations=(
                f'evidence_class_{observation.evidence_class}_below_'
                f'{evaluation_kind}',
            ),
        )
    if spec.scope == 'seat_metric' and not observation.seat_scope:
        limitations.append('seat_coverage_undeclared')
    if spec.parameter_index in _DYNAMICS_INDICES:
        if observation.basis is None:
            return RP22ParameterResult(
                **base,
                observed_value=observation.value,
                evidence_class=observation.evidence_class,
                verdict='insufficient_evidence',
                limitations=tuple(
                    limitations + ['capability_basis_missing']
                ),
            )
        basis_rank = _BASIS_RANK[observation.basis]
        if basis_rank < _KIND_MIN_BASIS_RANK[evaluation_kind]:
            return RP22ParameterResult(
                **base,
                observed_value=observation.value,
                evidence_class=observation.evidence_class,
                basis=observation.basis,
                verdict='insufficient_evidence',
                limitations=tuple(
                    limitations
                    + [
                        f'capability_basis_{observation.basis}_below_'
                        f'{evaluation_kind}'
                    ]
                ),
            )
    met, used_alternative = _meets_limit(
        spec,
        requested_level,
        observation.value,
        context_tag=observation.context_tag,
    )
    achieved: int | None = None
    achieved_by_alternative = False
    for level in _LEVELS:
        if spec.limit_for_level(level) is None:
            continue
        level_met, level_alt = _meets_limit(
            spec,
            level,
            observation.value,
            context_tag=observation.context_tag,
        )
        if level_met:
            achieved = level
            achieved_by_alternative = level_alt
    if spec.mapping_status == 'measurement_required':
        limitations.append('measurement_evidence_required_for_claim')
    elif spec.mapping_status == 'supported_with_limitations':
        limitations.append('mapping_has_limitations')
    if observation.evidence_class == 'design_prediction':
        limitations.append('design_prediction_not_commissioning_proof')
    return RP22ParameterResult(
        **base,
        observed_value=observation.value,
        evidence_class=observation.evidence_class,
        basis=observation.basis,
        verdict='met' if met else 'not_met',
        achieved_level=achieved,
        achieved_by_alternative=achieved_by_alternative,
        limitations=tuple(dict.fromkeys(limitations)),
    )


class RP22Evaluation(BaseModel):
    """Sealed parameter-by-parameter evaluation of a project against a
    requested RP22 performance level (#579 §4, §14, §15)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'rp22-evaluation-1'
    ] = RP22_EVALUATION_AUTHORITY_VERSION
    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256)
    registry_key: str = Field(min_length=1)
    mapping_version: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    requested_level: int = Field(ge=1, le=4)
    evaluation_kind: RP22EvaluationKind
    results: tuple[RP22ParameterResult, ...]
    strict_conformance: RP22ConformanceVerdict
    limiting_parameter_ids: tuple[str, ...] = ()
    insufficient_parameter_ids: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'RP22Evaluation':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        ids = [result.parameter_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError('evaluation result parameter ids must be unique')
        if len(set(self.limiting_parameter_ids)) != len(
            self.limiting_parameter_ids
        ):
            raise ValueError('limiting parameter ids must be unique')
        if len(set(self.insufficient_parameter_ids)) != len(
            self.insufficient_parameter_ids
        ):
            raise ValueError('insufficient parameter ids must be unique')
        digest = _hash(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('RP22 evaluation hash mismatch')
        if self.evaluation_id != 'rp22ev-' + digest[:24]:
            raise ValueError('RP22 evaluation id mismatch')
        return self

    def result(self, parameter_id: str) -> RP22ParameterResult | None:
        for item in self.results:
            if item.parameter_id == parameter_id:
                return item
        return None


def evaluate_rp22_profile(
    profile: RP22StandardsProfile,
    *,
    document_id: str,
    requested_level: int,
    evaluation_kind: RP22EvaluationKind,
    observations: tuple[RP22ParameterObservation, ...],
    evaluated_at_utc: str | None = None,
) -> RP22Evaluation:
    """Evaluate every declared parameter and derive the strict
    requested-level verdict plus the limiting-parameter list."""
    if requested_level not in _LEVELS:
        raise ValueError('requested_level must be 1..4')
    seen: set[str] = set()
    for observation in observations:
        if observation.parameter_id in seen:
            raise ValueError(
                f'duplicate observation for {observation.parameter_id!r}'
            )
        if profile.parameter(observation.parameter_id) is None:
            raise ValueError(
                f'observation for undeclared parameter '
                f'{observation.parameter_id!r}'
            )
        seen.add(observation.parameter_id)
    by_id = {obs.parameter_id: obs for obs in observations}
    results = tuple(
        evaluate_rp22_parameter(
            spec,
            by_id.get(spec.parameter_id),
            requested_level=requested_level,
            evaluation_kind=evaluation_kind,
        )
        for spec in profile.parameters
    )
    failing = [
        result.parameter_id
        for result in results
        if result.verdict == 'not_met'
    ]
    unknown = [
        result.parameter_id
        for result in results
        if result.verdict in ('insufficient_evidence', 'unsupported')
    ]
    if failing:
        conformance: RP22ConformanceVerdict = 'not_met'
    elif unknown:
        conformance = 'indeterminate'
    else:
        conformance = 'met'
    payload = canonicalize_payload(
        RP22Evaluation,
        dict(
            authority_version=RP22_EVALUATION_AUTHORITY_VERSION,
            evaluation_id='',
            document_id=document_id,
            profile_id=profile.profile_id,
            profile_sha256=profile.profile_sha256,
            registry_key=profile.registry_key,
            mapping_version=profile.mapping_version,
            calculation_version=profile.calculation_version,
            requested_level=requested_level,
            evaluation_kind=evaluation_kind,
            results=results,
            strict_conformance=conformance,
            limiting_parameter_ids=tuple(failing),
            insufficient_parameter_ids=tuple(unknown),
            evaluated_at_utc=evaluated_at_utc or _utc_now(),
            evaluation_sha256='0' * 64,
        ),
    )
    probe = RP22Evaluation.model_construct(**payload)
    digest = _hash(probe.semantic_payload())
    return RP22Evaluation(
        **probe.model_dump(
            mode='python', exclude={'evaluation_sha256', 'evaluation_id'}
        ),
        evaluation_id='rp22ev-' + digest[:24],
        evaluation_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Verification plan + report (#579 §14, §15)
# ---------------------------------------------------------------------------


class RP22VerificationItem(BaseModel):
    """One missing piece of commissioning evidence the requested level
    still needs — generated per parameter, never a generic
    'run measurements' task."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_id: str = Field(min_length=1)
    parameter_index: int = Field(ge=1, le=21)
    category: RP22Category
    scope: RP22MetricScope
    required_evidence: str = Field(min_length=1)
    required_inputs: tuple[str, ...] = ()
    seat_scope_required: bool = False
    blocking_verdict: RP22ParameterVerdict
    note: str = ''


def rp22_verification_plan(
    profile: RP22StandardsProfile,
    evaluation: RP22Evaluation,
) -> tuple[RP22VerificationItem, ...]:
    """Derive the commissioning plan: every parameter that failed or is
    still unknown at the requested level, with the exact evidence needed."""
    if evaluation.profile_sha256 != profile.profile_sha256:
        raise ValueError(
            'verification plan requires an evaluation of this profile'
        )
    items: list[RP22VerificationItem] = []
    for result in evaluation.results:
        if result.verdict in ('met', 'not_applicable'):
            continue
        spec = profile.parameter(result.parameter_id)
        if spec is None:
            raise ValueError(
                f'evaluation references undeclared parameter '
                f'{result.parameter_id!r}'
            )
        if result.verdict == 'unsupported':
            required = (
                f'No HTDT authority mapping exists — provide an external '
                f'evidence path for {spec.name}'
            )
        elif spec.mapping_status == 'measurement_required':
            required = (
                f'Measured commissioning evidence for {spec.name}: '
                f'{spec.measurement_semantics or spec.definition}'
            )
        elif result.verdict == 'not_met':
            required = (
                f'Design change or remediation for {spec.name}: observed '
                f'{result.observed_value} vs requested Level '
                f'{result.requested_level} limit '
                f'{result.target_at_requested_level} {spec.unit}'
            )
        else:
            required = (
                f'Evidence for {spec.name} ({spec.measurement_semantics or spec.definition})'
            )
        items.append(
            RP22VerificationItem(
                parameter_id=spec.parameter_id,
                parameter_index=spec.parameter_index,
                category=spec.category,
                scope=spec.scope,
                required_evidence=required,
                required_inputs=spec.required_inputs,
                seat_scope_required=spec.scope == 'seat_metric',
                blocking_verdict=result.verdict,
            )
        )
    return tuple(items)


class RP22ReportRow(BaseModel):
    """One report-matrix row: parameter, requested-level target, observed
    value, evidence state and verdict (#579 §15)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    parameter_index: int = Field(ge=1, le=21)
    parameter_id: str
    category: RP22Category
    scope: RP22MetricScope
    name: str
    unit: str
    requested_level_target: float | bool | None
    recommended_level_target: float | None
    observed_value: float | bool | None
    evidence_class: RP22EvidenceClass
    basis: RP22DynamicsBasis | None
    verdict: RP22ParameterVerdict
    achieved_level: int | None
    limitations: tuple[str, ...] = ()


def rp22_report_rows(
    profile: RP22StandardsProfile,
    evaluation: RP22Evaluation,
) -> tuple[RP22ReportRow, ...]:
    """The parameter-by-parameter report matrix — every row keeps its own
    evidence and limitations; nothing is averaged away."""
    if evaluation.profile_sha256 != profile.profile_sha256:
        raise ValueError(
            'report rows require an evaluation of this profile'
        )
    rows: list[RP22ReportRow] = []
    for result in evaluation.results:
        spec = profile.parameter(result.parameter_id)
        if spec is None:
            raise ValueError(
                f'evaluation references undeclared parameter '
                f'{result.parameter_id!r}'
            )
        rows.append(
            RP22ReportRow(
                parameter_index=spec.parameter_index,
                parameter_id=spec.parameter_id,
                category=spec.category,
                scope=spec.scope,
                name=spec.name,
                unit=spec.unit,
                requested_level_target=result.target_at_requested_level,
                recommended_level_target=(
                    result.recommended_at_requested_level
                ),
                observed_value=result.observed_value,
                evidence_class=result.evidence_class,
                basis=result.basis,
                verdict=result.verdict,
                achieved_level=result.achieved_level,
                limitations=result.limitations,
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Layout-derivation bridge (#579 §2 integration)
# ---------------------------------------------------------------------------

# Criterion ids the scene-layout lane derives (predicted basis) mapped to
# the RP22 parameter they evidence. Criteria the lane cannot honestly
# derive — recommended-zone membership, rendering mode, SPL/headroom —
# produce no observation and stay insufficient_evidence.
_LAYOUT_DERIVED_PARAMETERS = {
    'rp22.p01.listener-boundary-distance': 'rp22.p01',
    'rp22.p05.max-adjacent-surround-horizontal-angle': 'rp22.p05',
    'rp22.p09.max-adjacent-upper-vertical-angle': 'rp22.p09',
}


def derive_rp22_design_observations(
    *,
    repository: Any,
    target: Any,
    document: Any,
    observed_at_utc: str | None = None,
) -> tuple[RP22ParameterObservation, ...]:
    """Derive design-evaluation observations for the geometry-evidenced
    RP22 parameters from the exact scene layout.

    Runs the existing ``rp22_spatial_profile`` criterion lane
    (``scene-layout-derivation-v1``, predicted basis) for Level 4 — the
    superset criterion set — and re-binds each derived criterion value as
    an ``RP22ParameterObservation`` with ``design_prediction`` evidence.
    Parameters the lane does not cover are never fabricated.
    """
    from .cad_standards_layout_observation import (
        derive_layout_observations,
    )
    from .cad_standards_profiles import rp22_spatial_profile

    observations = derive_layout_observations(
        repository=repository,
        profile=rp22_spatial_profile(4),
        target=target,
        document=document,
        observed_at_utc=observed_at_utc or _utc_now(),
    )
    results: list[RP22ParameterObservation] = []
    for observation in observations:
        parameter_id = _LAYOUT_DERIVED_PARAMETERS.get(
            observation.criterion_id
        )
        if parameter_id is None or observation.observed_value is None:
            continue
        results.append(
            RP22ParameterObservation(
                parameter_id=parameter_id,
                value=float(observation.observed_value),
                evidence_class='design_prediction',
                evidence_ref=(
                    ':'.join(
                        (
                            observation.evidence_refs[0].kind,
                            observation.evidence_refs[0].evidence_id,
                        )
                    )
                    if observation.evidence_refs
                    else None
                ),
                seat_scope=tuple(observation.entity_ids),
                note=(
                    'Derived from scene layout '
                    '(scene-layout-derivation-v1, predicted basis).'
                ),
            )
        )
    return tuple(results)


__all__ = [
    'RP22AlternativeLimit',
    'RP22Category',
    'RP22ConformanceVerdict',
    'RP22DynamicsBasis',
    'RP22Evaluation',
    'RP22EvaluationKind',
    'RP22EvidenceClass',
    'RP22LimitKind',
    'RP22MappingStatus',
    'RP22MetricScope',
    'RP22ParameterObservation',
    'RP22ParameterResult',
    'RP22ParameterSpec',
    'RP22ParameterVerdict',
    'RP22ReportRow',
    'RP22StandardsProfile',
    'RP22VerificationItem',
    'RP22_CALCULATION_VERSION',
    'RP22_EVALUATION_AUTHORITY_VERSION',
    'RP22_MAPPING_VERSION',
    'RP22_PROFILE_AUTHORITY_VERSION',
    'build_rp22_profile',
    'derive_rp22_design_observations',
    'evaluate_rp22_parameter',
    'evaluate_rp22_profile',
    'rp22_report_rows',
    'rp22_v1_2_parameter_specs',
    'rp22_v1_2_profile',
    'rp22_verification_plan',
]
