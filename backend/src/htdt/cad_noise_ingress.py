"""External noise ingress / façade isolation authority (#781).

A room's internal acoustic quality is bounded from outside: road, rail,
aircraft, mechanical plant and neighboring noise enter through the
façade — walls, glazing, doors, roof, vents, ducts, penetrations and
flanking junctions. Field façade-sound-insulation measurement is
governed by ISO 16283-3:2016 (validated domain ≥ 50 Hz for level
difference); ISO/CD 16283-3 Ed.2 is a draft and may back only
research-flagged records; whole-building prediction composes with ISO
12354-3:2017 and rating with ISO 717-1:2020. This module keeps the
layers separate: source scenario → façade transmission model →
measurement → indoor qualification. Claims fail closed: a design-time
envelope model never produces a 'qualified' verdict, and sub-50 Hz
claims never ride the standard method domain.

Basis: issue #781 scope; ISO 16283-3:2016; ISO 12354-3:2017; ISO
717-1:2020; WHO Environmental Noise Guidelines 2018 (context only —
health targets live with #602/#580). Interior noise criteria compose
with #580 room-noise metrics; façade construction refs compose with
#620 assemblies.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


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
        **{sha_field: digest, id_field: _semantic_id(prefix, digest)},
    )


def _require_refs(*refs: AuthorityRef) -> None:
    for ref in refs:
        if ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


ExternalSourceKind = Literal[
    'road_traffic', 'rail', 'aircraft', 'outdoor_hvac',
    'generator_plant', 'neighbor_external', 'construction_intermittent',
    'project_defined', 'unknown',
]

FacadeElementKind = Literal[
    'wall_assembly', 'window_glazing', 'exterior_door', 'roof_ceiling',
    'vent_louver', 'duct_opening', 'service_penetration',
    'junction_flanking', 'other_external_surface',
]

IngressPathKind = Literal[
    'direct_facade_element', 'window_door', 'roof', 'flanking_junction',
    'vent_duct', 'leak_penetration', 'opening', 'structureborne_external',
    'unknown',
]

IngressDomainState = Literal[
    'standard_method_domain', 'extended_lf_diagnostic',
    'extended_lf_modelled', 'below_validated_domain',
]

EnvelopeState = Literal[
    'design_envelope', 'installed_envelope', 'field_observed_state',
    'qualified_state',
]

OpeningState = Literal['open', 'partial', 'closed', 'unknown']

INGRESS_LABELS: dict[str, str] = {
    'ingress_qualified': '適合（実測裏付け）',
    'ingress_qualified_limited': '限定条件付き適合',
    'ingress_fails_criterion': 'プロジェクト基準不適合',
    'design_model_only': '設計モデルのみ（実測なし）',
    'below_validated_domain': '検証域を下回る帯域の主張',
    'stale_after_envelope_change': '包絡変更により陳腐化',
    'insufficient_evidence': '証拠不足',
    'unknown': '不明',
}


class NoiseBand(BaseModel):
    """One band level: center frequency + level in dB. Used for source
    spectra, element transmission loss, and indoor results alike —
    always banded, never a single bare 'dB'."""

    model_config = ConfigDict(frozen=True)

    center_hz: float
    level_db: float

    @model_validator(mode='after')
    def _validate(self) -> 'NoiseBand':
        if self.center_hz <= 0.0:
            raise ValueError('center_hz must be > 0')
        return self


class ExternalNoiseIngressScenario(BaseModel):
    """The declared external noise source case (enis- prefix). Pins the
    source kind, a banded spectrum, time statistic, incidence geometry
    and operating state so an indoor claim is always evaluated against a
    specific outside condition — never a generic 'quiet street'."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    scenario_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    source_kind: ExternalSourceKind
    spectrum_bands: tuple[NoiseBand, ...]
    time_statistic: Literal[
        'la_eq', 'l_max', 'l_den', 'l_night', 'percentile',
        'event_max', 'other', 'unknown',
    ] = 'unknown'
    duration_s: float | None = None
    source_direction_deg: float | None = None
    facade_incidence: Literal[
        'normal', 'oblique', 'grazing', 'unknown',
    ] = 'unknown'
    source_position_ref: AuthorityRef | None = None
    source_operating_state: str | None = None
    source_evidence_ref: AuthorityRef | None = None
    meteorological_notes: str | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ExternalNoiseIngressScenario':
        if not self.spectrum_bands:
            raise ValueError('spectrum_bands must not be empty')
        for ref in (self.source_position_ref,
                    self.source_evidence_ref):
            if ref is not None:
                _require_refs(ref)
        if self.duration_s is not None and self.duration_s <= 0.0:
            raise ValueError('duration_s must be > 0')
        if self.source_direction_deg is not None and not (
                0.0 <= self.source_direction_deg < 360.0):
            raise ValueError('source_direction_deg must be in [0, 360)')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'scenario_id', 'scenario_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ExternalNoiseIngressScenario':
        return _seal(
            cls, payload, 'scenario_id', 'scenario_sha256', 'enis')


class FacadeElement(BaseModel):
    """One façade element's transmission description inside a
    FacadeTransmissionModel — banded reduction with the method and
    standard that produced it, plus installation state."""

    model_config = ConfigDict(frozen=True)

    element_kind: FacadeElementKind
    label: str
    area_m2: float | None = None
    construction_ref: AuthorityRef | None = None
    banded_transmission: tuple[NoiseBand, ...] = ()
    transmission_method: Literal[
        'lab_rating', 'field_measurement', 'prediction',
        'manufacturer_declaration', 'unknown',
    ] = 'unknown'
    standard_references: tuple[str, ...] = ()
    incidence: Literal['normal', 'diffuse', 'oblique', 'unknown'] \
        = 'unknown'
    installation_state: Literal[
        'as_designed', 'as_built', 'deteriorated', 'unknown',
    ] = 'unknown'
    uncertainty_db: float | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'FacadeElement':
        if not self.label.strip():
            raise ValueError('element label must not be empty')
        if self.area_m2 is not None and self.area_m2 <= 0.0:
            raise ValueError('area_m2 must be > 0')
        if self.construction_ref is not None:
            _require_refs(self.construction_ref)
        for pinned in self.standard_references:
            if '@' not in pinned:
                raise ValueError(
                    'standard_references must pin exact editions '
                    '(standard_id@edition)')
        return self


class FacadeTransmissionModel(BaseModel):
    """The envelope model that carries outside noise inward (ftm-
    prefix). Enumerates the façade elements and ingress paths actually
    in play, pins the lifecycle state of the envelope evidence (design
    vs installed vs field-observed vs qualified) and snapshots the
    opening states the model assumed."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    model_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    envelope_state: EnvelopeState = 'design_envelope'
    elements: tuple[FacadeElement, ...]
    ingress_paths: tuple[IngressPathKind, ...]
    window_state: OpeningState = 'unknown'
    door_state: OpeningState = 'unknown'
    vent_state: OpeningState = 'unknown'
    hvac_state: Literal['off', 'on', 'unknown'] = 'unknown'
    as_built_ref: AuthorityRef | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'FacadeTransmissionModel':
        if not self.elements:
            raise ValueError('elements must not be empty')
        if not self.ingress_paths:
            raise ValueError('ingress_paths must not be empty')
        if self.envelope_state in (
                'field_observed_state', 'qualified_state') \
                and self.as_built_ref is None:
            raise ValueError(
                'field_observed/qualified envelope states require an '
                'as_built_ref')
        if self.as_built_ref is not None:
            _require_refs(self.as_built_ref)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'model_id', 'model_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'FacadeTransmissionModel':
        return _seal(cls, payload, 'model_id', 'model_sha256', 'ftm')


class ExternalNoiseIngressMeasurement(BaseModel):
    """One ingress measurement record (enim- prefix): façade insulation,
    indoor receiving level, or background level under the declared
    scenario. Sub-50 Hz bands are legal to record but can never carry
    the 'standard_method_domain' flag — ISO 16283-3:2016 validates level
    difference from 50 Hz up; below that the record is diagnostic or
    modelled only."""

    model_config = ConfigDict(frozen=True)

    measurement_id: str
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scenario_ref: AuthorityRef
    model_ref: AuthorityRef
    measurement_class: Literal[
        'facade_insulation', 'indoor_receiving', 'background_noise',
    ]
    method_standard: str | None = None
    method_status: Literal[
        'current_standard', 'draft_research_only', 'other', 'unknown',
    ] = 'unknown'
    result_bands: tuple[NoiseBand, ...]
    domain_state: IngressDomainState = 'standard_method_domain'
    window_state: OpeningState = 'unknown'
    door_state: OpeningState = 'unknown'
    vent_state: OpeningState = 'unknown'
    receiver_positions: tuple[str, ...] = ()
    uncertainty_db: float | None = None
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'ExternalNoiseIngressMeasurement':
        _require_refs(self.scenario_ref, self.model_ref)
        if not self.result_bands:
            raise ValueError('result_bands must not be empty')
        if self.method_standard is not None \
                and '@' not in self.method_standard:
            raise ValueError(
                'method_standard must pin an exact edition '
                '(standard_id@edition)')
        if any(band.center_hz < 50.0 for band in self.result_bands) \
                and self.domain_state == 'standard_method_domain':
            raise ValueError(
                'sub-50 Hz bands cannot claim the ISO 16283-3 standard '
                'method domain — use an extended-lf or '
                'below_validated_domain state')
        if self.method_status == 'draft_research_only' \
                and self.domain_state == 'standard_method_domain':
            raise ValueError(
                'a draft method (e.g. ISO/CD 16283-3 Ed.2) cannot back '
                'a standard_method_domain claim')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'measurement_id', 'measurement_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'ExternalNoiseIngressMeasurement':
        return _seal(
            cls, payload, 'measurement_id', 'measurement_sha256',
            'enim')


class IndoorNoiseIngressQualification(BaseModel):
    """The verdict record for one scenario + envelope combination
    (iniq- prefix). Binds predicted bands, measured bands, the dominant
    limiting path, composed uncertainty and the project criterion the
    result is judged against (e.g. a #580 room-noise profile ref).
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    scenario_ref: AuthorityRef
    model_ref: AuthorityRef
    measurement_refs: tuple[AuthorityRef, ...] = ()
    predicted_bands: tuple[NoiseBand, ...] = ()
    main_limiting_path: IngressPathKind | None = None
    uncertainty_db: float | None = None
    criterion_ref: AuthorityRef | None = None
    criterion_description: str | None = None
    verdict: Literal[
        'qualified', 'qualified_with_limitations',
        'marginal_uncertainty', 'fails_project_criterion',
        'insufficient_evidence', 'below_validated_domain',
        'stale_after_envelope_change',
    ]
    staling_ref: AuthorityRef | None = None
    limitation_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'IndoorNoiseIngressQualification':
        _require_refs(self.scenario_ref, self.model_ref)
        for ref in self.measurement_refs:
            _require_refs(ref)
        for ref in (self.criterion_ref, self.staling_ref):
            if ref is not None:
                _require_refs(ref)
        if self.verdict in ('qualified', 'qualified_with_limitations'):
            if not self.measurement_refs and not self.predicted_bands:
                raise ValueError(
                    'a qualified verdict requires bound measurements '
                    'or predicted bands')
        if self.verdict == 'qualified_with_limitations' \
                and not self.limitation_reasons:
            raise ValueError(
                'qualified_with_limitations requires '
                'limitation_reasons')
        if self.verdict == 'marginal_uncertainty' \
                and self.uncertainty_db is None:
            raise ValueError(
                'marginal_uncertainty requires uncertainty_db')
        if self.verdict == 'fails_project_criterion' \
                and self.criterion_ref is None \
                and not self.criterion_description:
            raise ValueError(
                'fails_project_criterion requires the criterion')
        if self.verdict == 'stale_after_envelope_change' \
                and self.staling_ref is None:
            raise ValueError(
                'stale_after_envelope_change requires staling_ref')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(
        cls, **payload: Any
    ) -> 'IndoorNoiseIngressQualification':
        return _seal(
            cls, payload, 'qualification_id',
            'qualification_sha256', 'iniq')


def evaluate_ingress_claim(
    scenario: ExternalNoiseIngressScenario | None,
    model: FacadeTransmissionModel | None,
    measurements: tuple[ExternalNoiseIngressMeasurement, ...],
    qualification: IndoorNoiseIngressQualification | None,
) -> tuple[str, str]:
    """Fail-closed external-noise-ingress claim gate (#781).

    A qualified verdict requires: declared scenario + envelope model,
    no measurement sitting below its validated domain on the standard
    flag, and either field-observed/qualified envelope state or bound
    measurements. A design-time envelope alone is design_model_only —
    never 'qualified'.
    """
    if scenario is None:
        return ('insufficient_evidence', 'no_ingress_scenario')
    if model is None or not model.elements:
        return ('insufficient_evidence', 'no_facade_model')
    for meas in measurements:
        if meas.domain_state == 'below_validated_domain':
            return ('below_validated_domain',
                    'measurement:' + meas.measurement_id)
    if qualification is None:
        return ('insufficient_evidence', 'no_qualification_record')
    if qualification.verdict == 'stale_after_envelope_change':
        return ('stale_after_envelope_change',
                'staled_by_envelope_change')
    if qualification.verdict == 'below_validated_domain':
        return ('below_validated_domain',
                'qualification:below_validated_domain')
    if qualification.verdict == 'insufficient_evidence':
        return ('insufficient_evidence',
                'qualification:insufficient_evidence')
    if model.envelope_state not in (
            'field_observed_state', 'qualified_state') \
            and not qualification.measurement_refs:
        return ('design_model_only',
                'envelope model without field measurements')
    if qualification.verdict == 'fails_project_criterion':
        return ('ingress_fails_criterion',
                'project_criterion_not_met')
    if qualification.verdict == 'marginal_uncertainty':
        return ('ingress_qualified_limited',
                'marginal_uncertainty:' + str(
                    qualification.uncertainty_db))
    if qualification.verdict == 'qualified_with_limitations':
        return ('ingress_qualified_limited',
                'limitations:' + ','.join(
                    qualification.limitation_reasons))
    return ('ingress_qualified',
            'qualification:' + qualification.qualification_id)
