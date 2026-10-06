"""Tactile / seat-vibration authority (#612, REV57-INST).

Mechanical vibration at the listener is a separate modality from
acoustic bass: subwoofer SPL and shaker amplifier watts are not a
proxy for felt seat acceleration. This module proves — or honestly
fails to prove — what acceleration actually appears at each seat or
contact surface:

- :class:`CadTactilePath` — the exact mechanical chain: bass source →
  DSP/routing → amplifier → transducer instance → mounted seat /
  platform / contact surface. Tactile routing stays distinct from
  acoustic LFE/redirection even when both derive from one signal.
- :class:`CadTactileVibrationMeasurement` — measured acceleration /
  velocity / displacement at a named contact point: sensor identity
  and traceability, axis, occupied/empty/recline/contact state,
  uncertainty, drive reference, level and optional transfer
  spectrum. A phone accelerometer stays ``diagnostic`` evidence —
  never ``traceable`` — unless bound calibration (#611) supports it.
- :class:`CadTactileProfile` — a tactile target or exposure/comfort
  profile. Preference targets and safety/exposure limits stay
  independent; no universal ``ideal tactile curve`` is created.
  ISO 2631-1:1997 + Amd 1:2010 is the current production profile;
  ISO/FDIS 2631-1 Ed.3 stays ``draft_research_only`` until published
  (lifecycle via #599). ISO 2631-2:2026 building vibration is a
  separate domain.
- :class:`CadTactileVibrationQualification` +
  :func:`evaluate_tactile_vibration` — per-seat fail-closed verdict:
  is tactile vibration measured rather than inferred, is the
  occupancy state represented, is audio–tactile timing measured
  (composing #608/#609), are acoustic side effects routed to #589 and
  building coupling kept an independent outcome.

Honesty rules baked in:

- Transducer input voltage/watts never read as seat-surface
  acceleration — the drive→seat transfer must be measured.
- An empty-seat EQ never silently represents occupied-seat
  performance; the occupancy state is part of the measurement
  identity.
- One sensor position never silently represents the whole seat — pan,
  backrest and armrest are separate contact points.
- Desired seat vibration and unwanted building/adjacent-room
  vibration stay independent outcomes; a tactile pass never hides a
  structural coupling failure (#576/#229).
- A measured acceleration result never produces a medical or safety
  claim — profile evaluations stay profile-specific.
- Aggregation never hides a dead seat: per-seat results are retained
  and the verdict reflects the worst per-seat state.

Literature basis
----------------
- ISO 2631-1:1997 + Amd 1:2010 — current published whole-body
  vibration measurement/evaluation profile (confirmed 2021).
- ISO/FDIS 2631-1 Edition 3 — under FDIS approval (2026-09-24);
  research-only until formally published.
- ISO 2631-2:2026 — vibration in buildings, 1–80 Hz; separate domain
  from whole-body entertainment comfort.
- Siedenburg et al., Sci. Rep. 14:7764 (2024) — congruent chair-
  delivered vibrotactile stimulation increases musical engagement;
  establishes perceptual relevance, not a universal target curve.
- Merchel et al. / ``Auditory-Tactile Experience of Music`` (Springer
  2018) — body-related transfer functions vary with occupant and
  posture beyond tactile JNDs; drive level is not felt acceleration.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


TAC_SCHEMA_VERSION = 'tacvib-1'
TAC_EVALUATION_VERSION = 'tacvib-eval-1'

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
# Taxonomies (#612)
# ---------------------------------------------------------------------------

TactilePathNodeKind = Literal[
    'bass_signal_source',
    'routing_dsp',
    'crossover_band_pass',
    'level_delay_stage',
    'amplifier',
    'transducer',
    'seat_platform',
    'contact_surface',
]
"""Node classes of one tactile drive path — kept distinct from the
acoustic LFE/redirection path even when both derive from the same
bass signal."""

ContentMappingKind = Literal[
    'lfe_derived',
    'bass_managed_sum',
    'selected_frequency_band',
    'content_object_track',
    'custom_tactile_track',
    'other',
    'unknown',
]
"""How content maps onto the tactile channel. LFE is never assumed to
be copied verbatim to tactile transducers."""

OccupancyState = Literal[
    'empty_seat',
    'occupied_generic',
    'occupied_measured',
    'unknown',
]
"""Occupant/contact state — part of the tactile transfer identity. An
empty-seat measurement is not occupied-seat truth."""

VibrationQuantity = Literal[
    'acceleration',
    'velocity',
    'displacement',
]
"""Measured mechanical quantity (issue §3)."""

VibrationAxis = Literal['x', 'y', 'z', 'vector_magnitude', 'other', 'unknown']

IntegrationMetric = Literal[
    'rms',
    'peak',
    'peak_to_peak',
    'weighted_rms',
    'vibration_dose_value',
    'other',
    'unknown',
]

SensorEvidenceClass = Literal[
    'traceable_calibrated',
    'manufacturer_rated',
    'diagnostic_relative',
    'unknown',
]
"""Sensor evidence class: a phone accelerometer stays
``diagnostic_relative`` unless bound calibration (#611) supports an
absolute claim."""

TactileProfileKind = Literal[
    'project_defined',
    'user_preference',
    'research_profile',
    'device_manufacturer_profile',
    'iso_2631_1_1997_amd1_current',
    'iso_2631_1_ed3_fdis_draft_research_only',
    'iso_2631_2_2026_building',
    'exposure_comfort_limit',
    'unknown',
]
"""Tactile profile classes — preference targets and safety/exposure
limits are independent kinds, never merged (issue §7/§12)."""

PathAxisState = Literal[
    'measured',
    'partially_measured',
    'inferred_only',
    'unmeasured',
]

TactileVerdict = Literal[
    'qualified',
    'qualified_with_limitations',
    'research_only',
    'insufficient_evidence',
    'failed',
]


# ---------------------------------------------------------------------------
# Tactile path declaration
# ---------------------------------------------------------------------------


class CadTactilePathNode(BaseModel):
    """One element of the tactile drive chain."""

    model_config = ConfigDict(frozen=True)

    node_id: str = Field(min_length=1)
    node_kind: TactilePathNodeKind
    label: str
    device_ref: AuthorityRef | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def valid_node(self) -> 'CadTactilePathNode':
        if self.device_ref is not None and (
            self.device_ref.ref_sha256 is None
        ):
            raise ValueError('device refs must pin their sha256')
        return self


class CadTactilePathLeg(BaseModel):
    """One directed link between tactile path nodes."""

    model_config = ConfigDict(frozen=True)

    leg_id: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    content_mapping: ContentMappingKind = 'unknown'
    filter_description: str | None = None
    gain_db: float | None = None
    delay_ms: float | None = None
    polarity_reversed: bool | None = None

    @model_validator(mode='after')
    def valid_leg(self) -> 'CadTactilePathLeg':
        if self.gain_db is not None:
            _require_finite(self.gain_db, 'leg gain_db')
        if self.delay_ms is not None:
            _require_finite(self.delay_ms, 'leg delay_ms')
            if self.delay_ms < 0:
                raise ValueError('leg delay_ms must be non-negative')
        return self


class CadTactilePath(BaseModel):
    """Sealed source→seat tactile drive path for one seat/platform.

    Declares the mechanical chain end-to-end so the evaluation can
    prove the path is complete before trusting any measurement bound
    to it. Composes with #638 actuator evidence via device refs and
    with #593 electrical limits externally — the path itself never
    claims an electrical capability.
    """

    model_config = ConfigDict(frozen=True)

    path_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    label: str
    seat_ref: str | None = None
    seat_label: str | None = None
    nodes: tuple[CadTactilePathNode, ...]
    legs: tuple[CadTactilePathLeg, ...]
    actuator_binding_ref: AuthorityRef | None = None
    acoustic_source_ref: AuthorityRef | None = None
    structure_coupling_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    path_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_path(self) -> 'CadTactilePath':
        _require_iso8601(self.declared_at_utc, 'path declared_at_utc')
        if not self.nodes:
            raise ValueError('a tactile path requires nodes')
        node_ids = {n.node_id for n in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError('duplicate tactile path node ids')
        kinds = {n.node_kind for n in self.nodes}
        if 'transducer' not in kinds:
            raise ValueError(
                'a tactile path requires a transducer node — a seat '
                'vibration claim without a declared actuator is '
                'unbound'
            )
        if 'contact_surface' not in kinds and (
            'seat_platform' not in kinds
        ):
            raise ValueError(
                'a tactile path requires a seat/platform or contact '
                'surface node — the mechanical endpoint must be '
                'declared'
            )
        for leg in self.legs:
            for end, label in (
                (leg.from_node_id, 'from_node_id'),
                (leg.to_node_id, 'to_node_id'),
            ):
                if end not in node_ids:
                    raise ValueError(
                        f'leg {leg.leg_id} {label} references an '
                        'undeclared node'
                    )
        for ref, label in (
            (self.actuator_binding_ref, 'actuator_binding_ref'),
            (self.acoustic_source_ref, 'acoustic_source_ref'),
            (self.structure_coupling_ref, 'structure_coupling_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.path_sha256 != expected:
            raise ValueError('tactile path hash mismatch')
        if self.path_id != _semantic_id('tvpath', expected):
            raise ValueError('path id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'label': self.label,
            'seat_ref': self.seat_ref,
            'seat_label': self.seat_label,
            'nodes': [n.model_dump(mode='json') for n in self.nodes],
            'legs': [l.model_dump(mode='json') for l in self.legs],
            'actuator_binding_ref': (
                self.actuator_binding_ref.model_dump(mode='json')
                if self.actuator_binding_ref is not None else None
            ),
            'acoustic_source_ref': (
                self.acoustic_source_ref.model_dump(mode='json')
                if self.acoustic_source_ref is not None else None
            ),
            'structure_coupling_ref': (
                self.structure_coupling_ref.model_dump(mode='json')
                if self.structure_coupling_ref is not None else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def tactile_path_binding(path: CadTactilePath) -> AuthorityRef:
    return AuthorityRef(
        kind='tactile_path',
        ref_id=path.path_id,
        ref_sha256=path.path_sha256,
    )


# ---------------------------------------------------------------------------
# Vibration measurement
# ---------------------------------------------------------------------------


class CadTactileVibrationMeasurement(BaseModel):
    """One measured vibration value at a seat contact point.

    Binds sensor identity + evidence class, axis, contact point,
    occupancy state, drive reference and uncertainty. ``transfer``
    fields carry the drive→seat frequency response when measured
    (issue §6): the electronic input response of a resonant shaker
    never reads as the seat response.
    """

    model_config = ConfigDict(frozen=True)

    measurement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path_ref: AuthorityRef
    quantity: VibrationQuantity = 'acceleration'
    axis: VibrationAxis = 'unknown'
    contact_point: str = Field(min_length=1)
    occupancy_state: OccupancyState = 'unknown'
    recline_deg: float | None = None
    contact_note: str | None = None
    value: float | None = None
    unit: str | None = None
    metric: IntegrationMetric = 'unknown'
    uncertainty: float | None = None
    frequency_hz: float | None = None
    spectrum_json: str | None = None
    transfer_json: str | None = None
    drive_level_db: float | None = None
    duration_s: float | None = None
    sensor_ref: AuthorityRef | None = None
    sensor_evidence_class: SensorEvidenceClass = 'unknown'
    stimulus_ref: AuthorityRef | None = None
    timebase_ref: AuthorityRef | None = None
    tactile_delay_ms: float | None = None
    acoustic_side_effect_refs: tuple[AuthorityRef, ...] = ()
    measured_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_measurement(self) -> 'CadTactileVibrationMeasurement':
        _require_iso8601(
            self.measured_at_utc, 'measurement measured_at_utc'
        )
        if self.path_ref.ref_sha256 is None:
            raise ValueError('measurements must pin the path sha256')
        if self.value is not None:
            _require_finite(self.value, 'measured value')
            if self.unit is None:
                raise ValueError(
                    'a measured value requires its unit — bare '
                    'numbers are not vibration evidence'
                )
        if self.metric == 'weighted_rms' and self.unit is None:
            raise ValueError(
                'a weighted quantity must name its weighting in unit'
            )
        for label, v in (
            ('uncertainty', self.uncertainty),
            ('frequency_hz', self.frequency_hz),
            ('drive_level_db', self.drive_level_db),
            ('duration_s', self.duration_s),
            ('tactile_delay_ms', self.tactile_delay_ms),
            ('recline_deg', self.recline_deg),
        ):
            if v is not None:
                _require_finite(v, label)
        for ref, label in (
            (self.sensor_ref, 'sensor_ref'),
            (self.stimulus_ref, 'stimulus_ref'),
            (self.timebase_ref, 'timebase_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        if self.sensor_evidence_class == 'traceable_calibrated' and (
            self.sensor_ref is None
        ):
            raise ValueError(
                'a traceable-calibrated claim must bind the sensor '
                'calibration record (#611)'
            )
        for ref in self.acoustic_side_effect_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'acoustic side-effect refs must pin their sha256'
                )
        expected = _hash(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('vibration measurement hash mismatch')
        if self.measurement_id != _semantic_id('tvmeas', expected):
            raise ValueError('measurement id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'path_ref': self.path_ref.model_dump(mode='json'),
            'quantity': self.quantity,
            'axis': self.axis,
            'contact_point': self.contact_point,
            'occupancy_state': self.occupancy_state,
            'recline_deg': self.recline_deg,
            'contact_note': self.contact_note,
            'value': self.value,
            'unit': self.unit,
            'metric': self.metric,
            'uncertainty': self.uncertainty,
            'frequency_hz': self.frequency_hz,
            'spectrum_json': self.spectrum_json,
            'transfer_json': self.transfer_json,
            'drive_level_db': self.drive_level_db,
            'duration_s': self.duration_s,
            'sensor_ref': (
                self.sensor_ref.model_dump(mode='json')
                if self.sensor_ref is not None else None
            ),
            'sensor_evidence_class': self.sensor_evidence_class,
            'stimulus_ref': (
                self.stimulus_ref.model_dump(mode='json')
                if self.stimulus_ref is not None else None
            ),
            'timebase_ref': (
                self.timebase_ref.model_dump(mode='json')
                if self.timebase_ref is not None else None
            ),
            'tactile_delay_ms': self.tactile_delay_ms,
            'acoustic_side_effect_refs': [
                r.model_dump(mode='json')
                for r in self.acoustic_side_effect_refs
            ],
            'measured_at_utc': self.measured_at_utc,
            'provenance_json': self.provenance_json,
        }


def tactile_measurement_binding(
    measurement: CadTactileVibrationMeasurement,
) -> AuthorityRef:
    return AuthorityRef(
        kind='tactile_vibration_measurement',
        ref_id=measurement.measurement_id,
        ref_sha256=measurement.measurement_sha256,
    )


# ---------------------------------------------------------------------------
# Tactile profile
# ---------------------------------------------------------------------------


class CadTactileProfile(BaseModel):
    """A tactile target or exposure/comfort profile.

    Preference targets and ISO exposure/comfort profiles are separate
    kinds. ``iso_2631_1_ed3_fdis_draft_research_only`` carries no
    production targets — it is declared research-only until published
    (lifecycle through #599). Profiles never produce medical or
    safety claims.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_kind: TactileProfileKind
    label: str
    description: str | None = None
    target_value: float | None = None
    target_unit: str | None = None
    limit_value: float | None = None
    limit_unit: str | None = None
    applies_to_occupancy: tuple[OccupancyState, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CadTactileProfile':
        _require_iso8601(
            self.declared_at_utc, 'profile declared_at_utc'
        )
        for label, v in (
            ('target_value', self.target_value),
            ('limit_value', self.limit_value),
        ):
            if v is not None:
                _require_finite(v, label)
        if self.profile_kind == (
            'iso_2631_1_ed3_fdis_draft_research_only'
        ) and (
            self.target_value is not None
            or self.limit_value is not None
        ):
            raise ValueError(
                'ISO/FDIS 2631-1 Ed.3 is research-only until published '
                '— it cannot carry production targets or limits'
            )
        if self.profile_kind in (
            'iso_2631_1_1997_amd1_current',
            'iso_2631_2_2026_building',
            'exposure_comfort_limit',
        ) and self.target_value is not None:
            raise ValueError(
                'exposure/comfort profiles carry limits, not '
                'entertainment targets — keep preference and safety '
                'independent'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('tactile profile hash mismatch')
        if self.profile_id != _semantic_id('tvprof', expected):
            raise ValueError('profile id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_kind': self.profile_kind,
            'label': self.label,
            'description': self.description,
            'target_value': self.target_value,
            'target_unit': self.target_unit,
            'limit_value': self.limit_value,
            'limit_unit': self.limit_unit,
            'applies_to_occupancy': list(self.applies_to_occupancy),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def tactile_profile_binding(
    profile: CadTactileProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='tactile_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


class CadTactileVibrationQualification(BaseModel):
    """Sealed per-seat tactile-vibration verdict.

    Independent axis states stay explicit: whether the seat response
    is measured (not inferred), whether occupancy is represented,
    whether audio–tactile timing is physically measured, and whether
    acoustic side effects and building coupling were evaluated —
    each an independent outcome.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path_ref: AuthorityRef
    profile_ref: AuthorityRef | None = None
    transfer_state: PathAxisState
    occupancy_state: OccupancyState
    timing_state: Literal[
        'physically_measured',
        'dsp_setting_only',
        'unmeasured',
    ]
    acoustic_side_effect_state: Literal[
        'evaluated_clean',
        'evaluated_flagged',
        'not_evaluated',
    ]
    building_coupling_state: Literal[
        'evaluated_acceptable',
        'evaluated_excessive',
        'not_evaluated',
    ]
    profile_verdict: Literal[
        'within_profile',
        'exceeds_profile',
        'not_evaluated',
        'research_only',
    ]
    verdict: TactileVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadTactileVibrationQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.path_ref.ref_sha256 is None:
            raise ValueError(
                'qualifications must pin the tactile path sha256'
            )
        if self.profile_ref is not None and (
            self.profile_ref.ref_sha256 is None
        ):
            raise ValueError('profile_ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('tactile qualification hash mismatch')
        if self.qualification_id != _semantic_id('tvqual', expected):
            raise ValueError(
                'qualification id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'path_ref': self.path_ref.model_dump(mode='json'),
            'profile_ref': (
                self.profile_ref.model_dump(mode='json')
                if self.profile_ref is not None else None
            ),
            'transfer_state': self.transfer_state,
            'occupancy_state': self.occupancy_state,
            'timing_state': self.timing_state,
            'acoustic_side_effect_state': self.acoustic_side_effect_state,
            'building_coupling_state': self.building_coupling_state,
            'profile_verdict': self.profile_verdict,
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def tactile_qualification_binding(
    qualification: CadTactileVibrationQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='tactile_vibration_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


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


def build_tactile_path(**kwargs: Any) -> CadTactilePath:
    """Seal one tactile drive-path declaration."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadTactilePath, dict(kwargs),
        'path_id', 'path_sha256', 'tvpath',
    )


def build_vibration_measurement(
    *,
    document_id: str,
    path: CadTactilePath | AuthorityRef,
    contact_point: str,
    **kwargs: Any,
) -> CadTactileVibrationMeasurement:
    """Seal one vibration measurement at a seat contact point."""
    path_ref = (
        tactile_path_binding(path)
        if isinstance(path, CadTactilePath)
        else path
    )
    payload = dict(
        document_id=document_id,
        path_ref=path_ref,
        contact_point=contact_point,
        **kwargs,
    )
    payload.setdefault('measured_at_utc', _utc_now())
    return _seal_model(
        CadTactileVibrationMeasurement, payload,
        'measurement_id', 'measurement_sha256', 'tvmeas',
    )


def build_tactile_profile(**kwargs: Any) -> CadTactileProfile:
    """Seal one tactile target/exposure profile."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadTactileProfile, dict(kwargs),
        'profile_id', 'profile_sha256', 'tvprof',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_tactile_vibration(
    *,
    document_id: str,
    path: CadTactilePath,
    measurements: tuple[CadTactileVibrationMeasurement, ...] = (),
    profile: CadTactileProfile | None = None,
    acoustic_side_effect_evaluated: bool = False,
    acoustic_side_effect_flagged: bool = False,
    building_coupling_evaluated: bool = False,
    building_coupling_excessive: bool = False,
    evaluated_at_utc: str | None = None,
) -> CadTactileVibrationQualification:
    """Fail-closed per-seat tactile-vibration evaluation.

    A seat is only ``qualified`` when the drive→seat transfer is
    measured (never inferred from amplifier watts or transducer
    input response), the occupancy state is represented, and acoustic
    side effects plus building coupling were evaluated. Any flagged
    side effect or excessive building coupling fails independently —
    a good seat response never hides a structural problem.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    for m in measurements:
        if (
            m.path_ref.ref_id != path.path_id
            or m.path_ref.ref_sha256 != path.path_sha256
        ):
            raise ValueError(
                'a bound measurement references a different path'
            )
    if profile is not None and (
        profile.profile_kind
        == 'iso_2631_1_ed3_fdis_draft_research_only'
    ):
        reasons.append(
            'ISO/FDIS 2631-1 Ed.3 bound — research-only until '
            'formally published (#599 lifecycle)'
        )

    # --- transfer axis -------------------------------------------------
    measured = [
        m for m in measurements
        if m.value is not None or m.spectrum_json is not None
    ]
    with_transfer = [m for m in measured if m.transfer_json is not None]
    if not measured:
        transfer_state: PathAxisState = 'unmeasured'
        reasons.append(
            'no seat vibration measurement bound — transducer watts '
            'or electronic input response are not seat acceleration'
        )
    elif with_transfer:
        transfer_state = 'measured'
    elif len(measured) == len(measurements):
        transfer_state = 'partially_measured'
        reasons.append(
            'levels measured but no drive→seat transfer spectrum — '
            'the path response is only partially proven'
        )
    else:
        transfer_state = 'partially_measured'

    if measured:
        classes = {m.sensor_evidence_class for m in measured}
        if classes == {'diagnostic_relative'} or (
            'diagnostic_relative' in classes
            and 'traceable_calibrated' not in classes
        ):
            reasons.append(
                'sensor evidence is diagnostic/relative only — phone '
                'accelerometer readings are not traceable absolute '
                'vibration'
            )
        elif 'unknown' in classes and (
            'traceable_calibrated' not in classes
        ):
            reasons.append(
                'sensor evidence class unknown — absolute level '
                'claims stay limited'
            )

    # --- occupancy -----------------------------------------------------
    occupancy_states = {m.occupancy_state for m in measurements}
    occupancy: OccupancyState
    if 'occupied_measured' in occupancy_states:
        occupancy = 'occupied_measured'
    elif 'occupied_generic' in occupancy_states:
        occupancy = 'occupied_generic'
    elif occupancy_states == {'empty_seat'}:
        occupancy = 'empty_seat'
        reasons.append(
            'only empty-seat measurements bound — empty-seat EQ is '
            'not exact occupied-seat performance'
        )
    elif 'unknown' in occupancy_states or not occupancy_states:
        occupancy = 'unknown'
    else:
        occupancy = 'occupied_generic'

    # --- timing --------------------------------------------------------
    timed = [m for m in measurements if m.tactile_delay_ms is not None]
    if timed and all(
        m.timebase_ref is not None for m in timed
    ):
        timing_state = 'physically_measured'
    elif timed:
        timing_state = 'dsp_setting_only'
        reasons.append(
            'tactile delay values exist without a bound timebase — '
            'a DSP delay setting is not proof of physical '
            'synchronization (#608/#609)'
        )
    else:
        timing_state = 'unmeasured'
        reasons.append('audio–tactile timing not measured')

    # --- side effects --------------------------------------------------
    if acoustic_side_effect_flagged:
        acoustic_state = 'evaluated_flagged'
        reasons.append(
            'tactile hardware produces unwanted acoustic/mechanical '
            'noise — routed to the rattle authority (#589)'
        )
    elif acoustic_side_effect_evaluated:
        acoustic_state = 'evaluated_clean'
    else:
        acoustic_state = 'not_evaluated'

    if building_coupling_excessive:
        coupling_state = 'evaluated_excessive'
        reasons.append(
            'tactile system couples unwanted vibration into the '
            'building structure — desired seat vibration and unwanted '
            'building vibration stay independent (#229/#576)'
        )
    elif building_coupling_evaluated:
        coupling_state = 'evaluated_acceptable'
    else:
        coupling_state = 'not_evaluated'

    # --- profile -------------------------------------------------------
    if profile is None:
        profile_verdict = 'not_evaluated'
    elif profile.profile_kind == (
        'iso_2631_1_ed3_fdis_draft_research_only'
    ):
        profile_verdict = 'research_only'
    elif profile.limit_value is not None and measured:
        accel_values = [
            m.value for m in measured
            if m.quantity == 'acceleration' and m.value is not None
        ]
        if accel_values and max(accel_values) > profile.limit_value:
            profile_verdict = 'exceeds_profile'
            reasons.append(
                'measured seat acceleration exceeds the bound '
                'profile limit — a profile-specific result, not a '
                'medical claim'
            )
        elif accel_values:
            profile_verdict = 'within_profile'
        else:
            profile_verdict = 'not_evaluated'
    else:
        profile_verdict = 'not_evaluated'

    # --- verdict -------------------------------------------------------
    if acoustic_state == 'evaluated_flagged' or (
        coupling_state == 'evaluated_excessive'
    ):
        verdict: TactileVerdict = 'failed'
    elif profile_verdict == 'exceeds_profile':
        verdict = 'failed'
    elif profile_verdict == 'research_only':
        verdict = 'research_only'
    elif transfer_state in ('unmeasured', 'inferred_only'):
        verdict = 'insufficient_evidence'
    elif (
        transfer_state == 'measured'
        and occupancy in ('occupied_measured', 'occupied_generic')
        and timing_state == 'physically_measured'
        and acoustic_state == 'evaluated_clean'
        and coupling_state != 'not_evaluated'
    ):
        verdict = 'qualified'
    else:
        verdict = 'qualified_with_limitations'
        if occupancy == 'unknown':
            reasons.append(
                'occupancy state unrepresented — seat transfer '
                'identity incomplete'
            )
        if coupling_state == 'not_evaluated':
            reasons.append(
                'building/structure coupling not evaluated'
            )
        if acoustic_state == 'not_evaluated':
            reasons.append('acoustic side effects not evaluated')

    payload = dict(
        document_id=document_id,
        path_ref=tactile_path_binding(path),
        profile_ref=(
            tactile_profile_binding(profile)
            if profile is not None else None
        ),
        transfer_state=transfer_state,
        occupancy_state=occupancy,
        timing_state=timing_state,
        acoustic_side_effect_state=acoustic_state,
        building_coupling_state=coupling_state,
        profile_verdict=profile_verdict,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=TAC_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadTactileVibrationQualification, payload,
        'qualification_id', 'qualification_sha256', 'tvqual',
    )


__all__ = [
    'TAC_EVALUATION_VERSION',
    'TAC_SCHEMA_VERSION',
    'CadTactilePath',
    'CadTactilePathLeg',
    'CadTactilePathNode',
    'CadTactileProfile',
    'CadTactileVibrationMeasurement',
    'CadTactileVibrationQualification',
    'ContentMappingKind',
    'IntegrationMetric',
    'OccupancyState',
    'PathAxisState',
    'SensorEvidenceClass',
    'TactilePathNodeKind',
    'TactileProfileKind',
    'TactileVerdict',
    'VibrationAxis',
    'VibrationQuantity',
    'build_tactile_path',
    'build_tactile_profile',
    'build_vibration_measurement',
    'evaluate_tactile_vibration',
    'tactile_measurement_binding',
    'tactile_path_binding',
    'tactile_profile_binding',
    'tactile_qualification_binding',
]
