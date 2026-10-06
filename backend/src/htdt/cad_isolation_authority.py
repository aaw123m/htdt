"""Inter-room sound-isolation qualification authority (#576).

Layer on top of the #559 planning primitives in
:mod:`htdt.cad_sound_isolation`. Where #559 answers "what is a bounded
direct-path estimate over declared paths", this module records the *full
qualification story* for a home-theater -> adjacent-space pair:

- :class:`IsolationConstructionElement` — the evidenced separating or
  flanking element: construction build-up, lab TL / rating evidence with
  exact method + edition, valid frequency domain, and in-situ field
  evidence. A marketing construction name is never treated as lab data.
- :class:`InterRoomIsolationScenario` — the ordered source -> receiving
  pair with its declared transmission-path decomposition (direct
  partition, flanking wall/floor-ceiling, door, window, duct/HVAC,
  service penetration, open portal, structure-borne), the source
  playback/LFE stress profile, the construction lifecycle state, and the
  receiving-room criteria the qualification is judged against.
- :class:`InterRoomFieldMeasurement` — ISO 16283-1-style banded field
  evidence: per-band source/receiving/background levels, decay or
  absorption normalization reference, room volumes, opening state and the
  operating-state binding. Non-conformant captures stay evidence but are
  labeled diagnostic.
- :class:`IsolationCalibrationRecord` — declared predict <-> measure
  calibration: which measurements fit the model and which are held out.
  Fit and holdout sets may never overlap; a calibration is never treated
  as proof of generic validity.
- :class:`SoundIsolationQualification` — the fail-closed verdict:
  per-band evidence state (measured / modelled / declared / unknown), the
  low-frequency validity domain, the limiting paths, estimated receiving
  band levels *only* where a declared source spectrum and banded transfer
  evidence both exist, and criterion verdicts. A single-number rating is
  retained as a derived view only and never substitutes for banded
  evidence; an Rw/STC rating is never extrapolated into the
  below-validated domain.

Contract properties:

- declarations and estimates never promote themselves: a qualification can
  only reach ``field_measured`` / ``qualified_with_limitations`` lifecycle
  states when actual field measurement evidence is bound;
- any declared but unmodelled flanking / structure-borne / penetration
  path caps the affected band at ``declared`` or ``unknown`` evidence —
  the 'no transmission' claim is impossible by construction;
- bands below the evidence's valid domain are
  ``below_validated_domain`` — honest UNKNOWN, never extrapolated;
- predicted receiving levels require both sides declared (source spectrum
  *and* banded transfer) and stay ``modelled`` evidence;
- comparability: two qualifications over different scenario or evidence
  content are never silently equated.
"""

from __future__ import annotations

from math import isfinite, log10
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import EquipmentDataProvenance, FrequencyDomain
from .cad_sound_isolation import IsolationEvidenceTier
from .canonical_json import (
    canonical_sha256 as _digest,
    canonicalize_payload as _canon,
)


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

INTERROOM_ISOLATION_SCHEMA_VERSION = 1
INTERROOM_ISOLATION_AUTHORITY_VERSION = 'interroom-isolation-1'

#: Transmission-path taxonomy for the inter-room qualification. A scenario
#: must decompose its coupling into these path kinds; anything unresolved
#: stays ``unknown`` and keeps the verdict honest.
IsolationTransmissionPathKind = Literal[
    'direct_partition',
    'flanking_wall',
    'floor_ceiling_structure',
    'door',
    'window',
    'duct_hvac',
    'service_penetration',
    'open_portal',
    'structure_borne',
    'unknown',
]

#: Element/assembly construction classes. Declared so the UI can reason
#: about junction behaviour without pretending to model it.
IsolationConstructionClass = Literal[
    'single_leaf',
    'double_leaf_cavity',
    'double_leaf_decoupled',
    'masonry_heavy',
    'lightweight_framed',
    'composite',
    'unknown',
]

#: Construction lifecycle of the scenario — what phase of truth this is.
IsolationConstructionState = Literal[
    'design_prediction',
    'under_construction',
    'as_built_unverified',
    'field_measured',
    'qualified_with_limitations',
]

#: Low-frequency handling classes. ``standard_rating_domain`` is the
#: ~100-3150 Hz third-octave span ISO 717 ratings describe; evidence below
#: it must be explicitly measured or explicitly modelled, else the band is
#: ``below_validated_domain`` and stays UNKNOWN.
IsolationLowFrequencyDomain = Literal[
    'standard_rating_domain',
    'extended_low_frequency_measured',
    'extended_low_frequency_modelled',
    'below_validated_domain',
]

#: Per-band evidence state inside a qualification.
IsolationBandEvidenceState = Literal[
    'measured',
    'modelled',
    'declared',
    'unknown',
]

IsolationPathOpeningState = Literal[
    'open',
    'closed',
    'sealed',
    'unknown',
]

IsolationFieldMethod = Literal[
    'iso_16283_1',
    'iso_16283_1_low_frequency',
    'astm_e336',
    'informal',
    'other_versioned',
]

IsolationQuantityMetric = Literal[
    'standardized_level_difference_dnt',
    'normalized_level_difference_dn',
    'apparent_sound_reduction_index_rprime',
    'level_difference_d',
    'sound_reduction_index_r',
    'receiving_spl',
    'other_declared',
]

IsolationCriterionKind = Literal[
    'max_receiving_spl_band',
    'nighttime_room_criterion',
    'neighbor_property_limit',
    'relative_reduction_target',
    'standards_derived',
    'user_defined',
]

IsolationCriterionVerdict = Literal[
    'pass',
    'fail',
    'unknown',
    'unsupported',
]

IsolationSourceContentClass = Literal[
    'movie_lfe_stress',
    'music',
    'speech',
    'test_signal',
    'other',
]

#: Standard rating domain per ISO 717-1 / ISO 12354-1 detailed model.
STANDARD_RATING_DOMAIN_HZ = FrequencyDomain(minimum_hz=100.0, maximum_hz=3150.0)

#: ISO 16283-1 optional low-frequency extension bands.
LF_EXTENSION_BANDS_HZ = (50.0, 63.0, 80.0)

#: 1/3-octave evaluation grid this authority reports on.
ISOLATION_EVALUATION_BANDS_HZ = (
    50.0, 63.0, 80.0,
    100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0,
    630.0, 800.0, 1000.0, 1250.0, 1600.0, 2000.0, 2500.0, 3150.0,
    4000.0, 5000.0,
)


#: JA labels for UI display of the enum values.
ISOLATION_PATH_KIND_LABELS = {
    'direct_partition': '直接隔壁伝搬',
    'flanking_wall': '側壁フラッキング',
    'floor_ceiling_structure': '床・天井構造伝搬',
    'door': 'ドア経路',
    'window': '窓経路',
    'duct_hvac': 'ダクト・空調経路',
    'service_penetration': '配管・貫通部',
    'open_portal': '開口部',
    'structure_borne': '構造伝搬振動',
    'unknown': '不明経路',
}

ISOLATION_CONSTRUCTION_STATE_LABELS = {
    'design_prediction': '設計予測',
    'under_construction': '施工中',
    'as_built_unverified': '竣工・未検証',
    'field_measured': '実測済み',
    'qualified_with_limitations': '制限付き適合',
}

ISOLATION_LF_DOMAIN_LABELS = {
    'standard_rating_domain': '標準格付け域',
    'extended_low_frequency_measured': '拡張低域（実測）',
    'extended_low_frequency_modelled': '拡張低域（モデル）',
    'below_validated_domain': '検証域外低域',
}

ISOLATION_BAND_EVIDENCE_LABELS = {
    'measured': '実測',
    'modelled': 'モデル推定',
    'declared': '宣言値',
    'unknown': '不明',
}

ISOLATION_CRITERION_KIND_LABELS = {
    'max_receiving_spl_band': '受音室最大音圧（バンド）',
    'nighttime_room_criterion': '夜間室内基準',
    'neighbor_property_limit': '隣接敷地界限',
    'relative_reduction_target': '相対低減目標',
    'standards_derived': '規格由来基準',
    'user_defined': 'ユーザー定義',
}

ISOLATION_CRITERION_VERDICT_LABELS = {
    'pass': '適合',
    'fail': '不適合',
    'unknown': '不明',
    'unsupported': '判定不能',
}


def _require_finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


class IsolationElementEvidence(BaseModel):
    """One evidence bundle for a construction element.

    ``evidence_kind`` distinguishes lab TL spectra, lab single-number
    ratings, field measurements, declared values and generic labels.
    A ``generic_label`` (e.g. a marketing name like 'double stud wall')
    must never carry TL values — there is nothing honest to store.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_kind: Literal[
        'lab_tl_spectrum',
        'lab_rating',
        'field_measurement',
        'declared',
        'generic_label',
    ]
    standard_ref: str | None = None
    method_ref: str | None = None
    edition_or_revision: str | None = None
    #: Band TL values keyed by band label, e.g. {'125': 34.0}.
    tl_values_db: dict[str, float] | None = None
    single_number_rating: str | None = None
    frequency_domain: FrequencyDomain | None = None
    uncertainty_db: float | None = None
    provenance: EquipmentDataProvenance | None = None
    note: str | None = None

    @field_validator('tl_values_db')
    @classmethod
    def _tl_finite(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return value
        for key, item in value.items():
            _require_finite(item, f'tl_values_db[{key!r}]')
            if item < 0.0:
                raise ValueError('transmission loss must be >= 0 dB')
        return value

    @model_validator(mode='after')
    def _kind_consistency(self) -> 'IsolationElementEvidence':
        if self.evidence_kind == 'generic_label' and (
            self.tl_values_db is not None or self.single_number_rating is not None
        ):
            raise ValueError(
                'generic_label evidence cannot carry TL values or ratings'
            )
        if self.evidence_kind in ('lab_tl_spectrum', 'field_measurement') and (
            not self.tl_values_db and self.single_number_rating is None
        ):
            raise ValueError(
                'measured/lab evidence must carry band TL data or an explicit rating'
            )
        if self.uncertainty_db is not None:
            _require_finite(self.uncertainty_db, 'uncertainty_db')
        return self


class IsolationJunctionEvidence(BaseModel):
    """Junction/flanking coupling declaration between two elements.

    The authority does not compute vibration reduction indices — it
    records whether junction evidence exists and which standard it claims.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    junction_class: Literal[
        'rigid_t_junction',
        'rigid_x_junction',
        'rigid_corner',
        'decoupled_junction',
        'elastic_layer',
        'unknown',
    ] = 'unknown'
    evidence_kind: Literal[
        'lab_measured', 'standard_table', 'declared', 'unknown'
    ] = 'unknown'
    standard_ref: str | None = None
    vibration_reduction_index_db: float | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _kij_consistency(self) -> 'IsolationJunctionEvidence':
        if self.vibration_reduction_index_db is not None:
            _require_finite(
                self.vibration_reduction_index_db,
                'vibration_reduction_index_db',
            )
            if self.evidence_kind in ('declared', 'unknown'):
                raise ValueError(
                    'a numeric Kij requires lab_measured or standard_table evidence'
                )
        return self


class IsolationConstructionElement(BaseModel):
    """An evidenced building element participating in transmission.

    Content-sealed; ``element_id`` derives from the full payload so any
    change to the construction story is a different element.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    element_id: str
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    construction_class: IsolationConstructionClass = 'unknown'
    surface_mass_kg_m2: float | None = None
    layer_count: int | None = Field(default=None, ge=1)
    cavity_depth_mm: float | None = None
    stud_frame: Literal['none', 'steel', 'wood', 'double', 'unknown'] = 'unknown'
    resilient_decoupling: Literal[
        'none', 'resilient_channel', 'isolation_clips', 'independent_frame', 'unknown'
    ] = 'unknown'
    insulation_fill: Literal['none', 'mineral_fibre', 'other', 'unknown'] = 'unknown'
    surface_area_m2: float | None = None
    evidence: tuple[IsolationElementEvidence, ...] = ()
    junction_evidence: tuple[IsolationJunctionEvidence, ...] = ()
    provenance: EquipmentDataProvenance | None = None
    note: str | None = None
    element_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IsolationConstructionElement':
        expected = _digest(self.identity_payload())
        if self.element_sha256 != expected:
            raise ValueError('element_sha256 does not match content')
        if self.element_id != f'ise:{expected}':
            raise ValueError('element_id must be ise:<sha256>')
        if self.surface_mass_kg_m2 is not None:
            _require_finite(self.surface_mass_kg_m2, 'surface_mass_kg_m2')
        if self.cavity_depth_mm is not None:
            _require_finite(self.cavity_depth_mm, 'cavity_depth_mm')
        if self.surface_area_m2 is not None:
            _require_finite(self.surface_area_m2, 'surface_area_m2')
            if self.surface_area_m2 <= 0.0:
                raise ValueError('surface_area_m2 must be > 0')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('element_id', None)
        payload.pop('element_sha256', None)
        return payload

    @property
    def best_lab_domain(self) -> FrequencyDomain | None:
        """Union of lab/measured evidence frequency domains."""
        domain: FrequencyDomain | None = None
        for ev in self.evidence:
            if ev.evidence_kind in ('lab_tl_spectrum', 'field_measurement') and (
                ev.frequency_domain is not None
            ):
                if domain is None:
                    domain = ev.frequency_domain
                else:
                    domain = FrequencyDomain(
                        minimum_hz=min(
                            domain.minimum_hz, ev.frequency_domain.minimum_hz
                        ),
                        maximum_hz=max(
                            domain.maximum_hz, ev.frequency_domain.maximum_hz
                        ),
                    )
        return domain


class IsolationTransmissionPath(BaseModel):
    """One declared coupling path inside a scenario."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    path_id: str = Field(min_length=1)
    kind: IsolationTransmissionPathKind
    element_id: str | None = None
    element_sha256: str | None = None
    area_m2: float | None = None
    opening_state: IsolationPathOpeningState = 'closed'
    junction_ref: str | None = None
    bypasses_partition: bool = False
    note: str | None = None

    @model_validator(mode='after')
    def _refs(self) -> 'IsolationTransmissionPath':
        if self.area_m2 is not None:
            _require_finite(self.area_m2, 'area_m2')
            if self.area_m2 <= 0.0:
                raise ValueError('area_m2 must be > 0')
        if (self.element_id is None) != (self.element_sha256 is None):
            raise ValueError('element_id and element_sha256 must be set together')
        if self.kind == 'open_portal' and self.opening_state not in (
            'open', 'unknown'
        ):
            raise ValueError('an open_portal path must be open or unknown')
        return self


class IsolationSourceStressProfile(BaseModel):
    """What the theater is doing — the playback stress the qualification
    is judged against. ``band_levels_db`` is optional; without it no
    receiving-SPL estimate is possible."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    content_class: IsolationSourceContentClass = 'other'
    playback_level_db_spl: float | None = None
    band_levels_db: dict[str, float] | None = None
    lfe_active: bool = False
    subwoofer_output_level_db: float | None = None
    bass_management_state: Literal['on', 'off', 'unknown'] = 'unknown'
    channel_activity: tuple[str, ...] = ()
    eq_filter_state: str | None = None
    duration_s: float | None = None
    reference_offset_db: float | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'IsolationSourceStressProfile':
        for name in (
            'playback_level_db_spl',
            'subwoofer_output_level_db',
            'duration_s',
            'reference_offset_db',
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if self.band_levels_db is not None:
            for key, item in self.band_levels_db.items():
                _require_finite(item, f'band_levels_db[{key!r}]')
        return self


class ReceivingRoomCriterion(BaseModel):
    """A criterion the receiving room is judged against — a separate
    declaration from the transmission evidence itself."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    criterion_id: str = Field(min_length=1)
    kind: IsolationCriterionKind
    frequency_domain: FrequencyDomain | None = None
    limit_db: float | None = None
    source_ref: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'ReceivingRoomCriterion':
        if self.limit_db is not None:
            _require_finite(self.limit_db, 'limit_db')
        return self


class InterRoomIsolationScenario(BaseModel):
    """Ordered source -> receiving region pair with declared paths,
    stress profile and criteria. Content-sealed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    scenario_id: str
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    source_region_id: str = Field(min_length=1)
    receiving_region_id: str = Field(min_length=1)
    paths: tuple[IsolationTransmissionPath, ...] = ()
    source_profile: IsolationSourceStressProfile
    criteria: tuple[ReceivingRoomCriterion, ...] = ()
    construction_state: IsolationConstructionState = 'design_prediction'
    evaluation_bands_hz: tuple[float, ...] = ISOLATION_EVALUATION_BANDS_HZ
    provenance: EquipmentDataProvenance | None = None
    created_at_utc: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator('evaluation_bands_hz')
    @classmethod
    def _bands_finite(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        for band in value:
            _require_finite(band, 'evaluation_bands_hz')
            if band <= 0.0:
                raise ValueError('evaluation bands must be positive')
        return tuple(sorted(set(value)))

    @model_validator(mode='after')
    def _seal(self) -> 'InterRoomIsolationScenario':
        if self.source_region_id == self.receiving_region_id:
            raise ValueError('source and receiving regions must differ')
        path_ids = [path.path_id for path in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError('duplicate path_id')
        expected = _digest(self.identity_payload())
        if self.scenario_sha256 != expected:
            raise ValueError('scenario_sha256 does not match content')
        if self.scenario_id != f'irs:{expected}':
            raise ValueError('scenario_id must be irs:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('scenario_id', None)
        payload.pop('scenario_sha256', None)
        return payload


class IsolationBandEvidence(BaseModel):
    """Banded measured/decomposed evidence inside a field measurement.
    Metric identity is part of the band — metrics are never merged."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: float
    metric: IsolationQuantityMetric
    value_db: float
    source_level_db: float | None = None
    receiving_level_db: float | None = None
    background_level_db: float | None = None
    uncertainty_db: float | None = None

    @model_validator(mode='after')
    def _finite(self) -> 'IsolationBandEvidence':
        _require_finite(self.band_hz, 'band_hz')
        _require_finite(self.value_db, 'value_db')
        for name in (
            'source_level_db',
            'receiving_level_db',
            'background_level_db',
            'uncertainty_db',
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        return self


class IsolationNormalizationEvidence(BaseModel):
    """Decay/absorption normalization applied to the field levels."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    normalization: Literal[
        'reverberation_time', 'equivalent_absorption_area', 'none_declared'
    ]
    reference: Literal[
        'reference_rt_0p5s', 'reference_absorption_10m2', 'none'
    ] = 'none'
    receiving_rt_s: dict[str, float] | None = None
    receiving_absorption_m2: dict[str, float] | None = None

    @model_validator(mode='after')
    def _consistency(self) -> 'IsolationNormalizationEvidence':
        if self.normalization == 'none_declared' and (
            self.receiving_rt_s is not None
            or self.receiving_absorption_m2 is not None
        ):
            raise ValueError(
                'normalization=none_declared cannot carry RT/absorption data'
            )
        return self


class InterRoomFieldMeasurement(BaseModel):
    """ISO 16283-1-style banded field capture between the scenario pair.

    ``standard_conformant`` is computed, not declared: the record must
    carry the minimum evidence a real field survey needs (method
    identity, positions, background levels, normalization data, volumes)
    before its numbers can feed a qualified verdict. Non-conformant
    captures stay stored as diagnostic evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str
    document_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(min_length=1)
    method_profile: IsolationFieldMethod
    method_standard_ref: str | None = None
    method_edition: str | None = None
    source_position_ids: tuple[str, ...] = ()
    receiver_position_ids: tuple[str, ...] = ()
    bands: tuple[IsolationBandEvidence, ...] = ()
    normalization: IsolationNormalizationEvidence = IsolationNormalizationEvidence(
        normalization='none_declared'
    )
    source_room_volume_m3: float | None = None
    receiving_room_volume_m3: float | None = None
    partition_area_m2: float | None = None
    opening_state: IsolationPathOpeningState = 'unknown'
    operating_state_ref: str | None = None
    state_snapshot_id: str | None = None
    uncertainty_db: float | None = None
    instrument_refs: tuple[str, ...] = ()
    provenance: EquipmentDataProvenance | None = None
    measured_at_utc: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'InterRoomFieldMeasurement':
        expected = _digest(self.identity_payload())
        if self.measurement_sha256 != expected:
            raise ValueError('measurement_sha256 does not match content')
        if self.measurement_id != f'irm:{expected}':
            raise ValueError('measurement_id must be irm:<sha256>')
        for name in (
            'source_room_volume_m3',
            'receiving_room_volume_m3',
            'partition_area_m2',
            'uncertainty_db',
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        if self.method_profile != 'informal' and (
            self.method_standard_ref is None
        ):
            raise ValueError(
                'standardized methods require method_standard_ref'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('measurement_id', None)
        payload.pop('measurement_sha256', None)
        return payload

    @property
    def standard_conformant(self) -> bool:
        """Minimum-evidence check for a real field survey.

        A conformant capture has a versioned method, at least one source
        and receiver position, background levels on every measured band,
        a declared normalization basis, and both room volumes.
        """
        if self.method_profile in ('informal', 'other_versioned'):
            return False
        if not self.source_position_ids or not self.receiver_position_ids:
            return False
        if not self.bands:
            return False
        if any(band.background_level_db is None for band in self.bands):
            return False
        if self.normalization.normalization == 'none_declared':
            return False
        if (
            self.source_room_volume_m3 is None
            or self.receiving_room_volume_m3 is None
        ):
            return False
        return True

    def band_for(self, band_hz: float) -> IsolationBandEvidence | None:
        for band in self.bands:
            if band.band_hz == band_hz:
                return band
        return None


class IsolationCalibrationRecord(BaseModel):
    """Declared predict <-> measure calibration.

    ``fit_measurement_ids`` were used to tune the model;
    ``holdout_measurement_ids`` must be disjoint and are the only honest
    validation evidence. An empty holdout means the calibration has never
    been checked against unseen data.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    calibration_id: str
    document_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    fit_measurement_ids: tuple[str, ...] = ()
    holdout_measurement_ids: tuple[str, ...] = ()
    bounded_refinement: str = Field(min_length=1)
    outcome_note: str | None = None
    calibration_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'IsolationCalibrationRecord':
        overlap = set(self.fit_measurement_ids) & set(
            self.holdout_measurement_ids
        )
        if overlap:
            raise ValueError(
                'fit and holdout measurement sets must be disjoint'
            )
        expected = _digest(self.identity_payload())
        if self.calibration_sha256 != expected:
            raise ValueError('calibration_sha256 does not match content')
        if self.calibration_id != f'isc:{expected}':
            raise ValueError('calibration_id must be isc:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('calibration_id', None)
        payload.pop('calibration_sha256', None)
        return payload


class IsolationBandVerdict(BaseModel):
    """Per-band qualification row — the truth the UX lists."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: float
    evidence_state: IsolationBandEvidenceState
    lf_domain: IsolationLowFrequencyDomain
    level_difference_db: float | None = None
    estimated_receiving_spl_db: float | None = None
    limiting_path_ids: tuple[str, ...] = ()
    uncertainty_db: float | None = None
    note: str | None = None


class IsolationCriterionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    criterion_id: str
    verdict: IsolationCriterionVerdict
    evaluated_bands_hz: tuple[float, ...] = ()
    worst_margin_db: float | None = None
    note: str | None = None


class SoundIsolationQualification(BaseModel):
    """The qualification verdict for a scenario — the only place a
    'transmission achieved' claim may live, and it can only live here
    behind measured evidence."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    qualification_id: str
    document_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(min_length=1)
    measurement_ids: tuple[str, ...] = ()
    calibration_ids: tuple[str, ...] = ()
    lifecycle_state: IsolationConstructionState
    band_verdicts: tuple[IsolationBandVerdict, ...] = ()
    criterion_results: tuple[IsolationCriterionResult, ...] = ()
    derived_single_number: str | None = None
    unmodelled_path_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def _seal(self) -> 'SoundIsolationQualification':
        expected = _digest(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('qualification_sha256 does not match content')
        if self.qualification_id != f'siq:{expected}':
            raise ValueError('qualification_id must be siq:<sha256>')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode='json')
        payload.pop('qualification_id', None)
        payload.pop('qualification_sha256', None)
        return payload


def classify_band_lf_domain(
    band_hz: float,
    *,
    has_measured_evidence: bool,
    has_lf_model: bool,
) -> IsolationLowFrequencyDomain:
    """Assign the low-frequency validity class for a band.

    Inside the standard rating domain (>= 100 Hz) the band is
    ``standard_rating_domain`` regardless of provenance. Below it the
    band is extended-measured only with real measured evidence covering
    that band, extended-modelled only with an explicit LF model
    declaration, else below_validated_domain — never extrapolated.
    """
    if STANDARD_RATING_DOMAIN_HZ.contains(band_hz):
        return 'standard_rating_domain'
    if has_measured_evidence:
        return 'extended_low_frequency_measured'
    if has_lf_model:
        return 'extended_low_frequency_modelled'
    return 'below_validated_domain'


def evaluate_isolation_qualification(
    scenario: InterRoomIsolationScenario,
    *,
    measurements: Sequence[InterRoomFieldMeasurement] = (),
    calibrations: Sequence[IsolationCalibrationRecord] = (),
    lf_modelled_bands_hz: Sequence[float] = (),
    evaluated_at_utc: str,
) -> SoundIsolationQualification:
    """Fail-closed qualification of an inter-room scenario.

    Rules:

    - per-band evidence state is ``measured`` only when a standard-
      conformant measurement covers the band; ``modelled`` when an LF /
      explicit model declaration covers it; ``declared`` when the band is
      inside the standard domain but no conformant evidence exists;
      ``unknown`` otherwise;
    - bands below 100 Hz without measured/modelled coverage are
      ``below_validated_domain`` and ``unknown``;
    - estimated receiving SPL = declared source band level minus the
      measured level difference, emitted only when both exist for the
      band — never from a single-number rating;
    - a declared door/window/duct/penetration/open-portal/structure/
      unknown path whose element is not carried into the evaluation is a
      limiting path and caps the band at ``declared``;
    - the lifecycle can only exceed ``as_built_unverified`` when at least
      one conformant measurement is bound; ``qualified_with_limitations``
      additionally requires every criterion evaluated.
    """
    conformant = [m for m in measurements if m.standard_conformant]
    measurement_ids = tuple(m.measurement_id for m in measurements)
    lf_modelled = set(lf_modelled_bands_hz)

    # Paths that bypass the partition or carry an unresolved kind limit
    # every band they touch until evidence carries them explicitly.
    unmodelled_paths = tuple(
        path.path_id
        for path in scenario.paths
        if path.kind in (
            'flanking_wall',
            'floor_ceiling_structure',
            'duct_hvac',
            'service_penetration',
            'open_portal',
            'structure_borne',
            'unknown',
        )
        or path.bypasses_partition
        or (path.opening_state == 'open' and path.kind != 'direct_partition')
    )
    has_unmodelled = bool(unmodelled_paths)

    source_bands = scenario.source_profile.band_levels_db or {}

    band_verdicts: list[IsolationBandVerdict] = []
    for band_hz in scenario.evaluation_bands_hz:
        measured_band: IsolationBandEvidence | None = None
        uncertainty: float | None = None
        for measurement in conformant:
            candidate = measurement.band_for(band_hz)
            if candidate is not None:
                measured_band = candidate
                uncertainty = candidate.uncertainty_db or measurement.uncertainty_db
                break
        has_measured = measured_band is not None
        has_model = band_hz in lf_modelled
        lf_domain = classify_band_lf_domain(
            band_hz,
            has_measured_evidence=has_measured,
            has_lf_model=has_model,
        )
        if has_measured:
            state: IsolationBandEvidenceState = 'measured'
        elif has_model:
            state = 'modelled'
        elif lf_domain == 'standard_rating_domain':
            state = 'declared'
        else:
            state = 'unknown'
        # A declared-but-unmodelled bypass/flanking path can dominate a
        # *modelled* estimate — demote it to 'declared'. A conformant field
        # measurement already captures the combined real transmission
        # (including flanking), so 'measured' bands stay measured; the
        # limiting paths remain listed as attribution diagnostics.
        if has_unmodelled and state == 'modelled':
            state = 'declared'
        # ``level_difference_db`` only carries an actual level-difference
        # quantity — an absolute receiving SPL or an undeclared metric has
        # no place under that name.
        level_difference = (
            measured_band.value_db
            if measured_band is not None
            and measured_band.metric
            in (
                'standardized_level_difference_dnt',
                'normalized_level_difference_dn',
                'level_difference_d',
                'apparent_sound_reduction_index_rprime',
                'sound_reduction_index_r',
            )
            else None
        )
        source_level = source_bands.get(_band_key(band_hz))
        estimated_spl: float | None = None
        if (
            measured_band is not None
            and measured_band.metric == 'receiving_spl'
        ):
            estimated_spl = measured_band.value_db
        elif (
            source_level is not None
            and level_difference is not None
            and measured_band is not None
            and measured_band.metric
            in (
                'standardized_level_difference_dnt',
                'normalized_level_difference_dn',
                'level_difference_d',
            )
        ):
            estimated_spl = source_level - level_difference
        limiting = unmodelled_paths if has_unmodelled else ()
        band_verdicts.append(
            IsolationBandVerdict(
                band_hz=band_hz,
                evidence_state=state,
                lf_domain=lf_domain,
                level_difference_db=level_difference,
                estimated_receiving_spl_db=estimated_spl,
                limiting_path_ids=limiting,
                uncertainty_db=uncertainty,
            )
        )

    criterion_results: list[IsolationCriterionResult] = []
    for criterion in scenario.criteria:
        bands = (
            criterion.frequency_domain and
            tuple(
                v for v in band_verdicts
                if criterion.frequency_domain.contains(v.band_hz)
            )
        ) or tuple(band_verdicts)
        if criterion.limit_db is None:
            criterion_results.append(
                IsolationCriterionResult(
                    criterion_id=criterion.criterion_id,
                    verdict='unsupported',
                    note='no numeric limit declared',
                )
            )
            continue
        if criterion.kind == 'relative_reduction_target':
            # A reduction target caps the measured level difference, not
            # the receiving SPL — comparing limit against estimated SPL
            # would mix quantities.
            usable = [
                v for v in bands
                if v.level_difference_db is not None
                and v.evidence_state == 'measured'
            ]
            if not usable:
                criterion_results.append(
                    IsolationCriterionResult(
                        criterion_id=criterion.criterion_id,
                        verdict='unknown',
                        note='no measured level-difference evidence '
                        'in domain',
                    )
                )
                continue
            worst = min(
                (v.level_difference_db or 0.0) - criterion.limit_db
                for v in usable
            )
            criterion_results.append(
                IsolationCriterionResult(
                    criterion_id=criterion.criterion_id,
                    verdict='pass' if worst >= 0.0 else 'fail',
                    evaluated_bands_hz=tuple(v.band_hz for v in usable),
                    worst_margin_db=worst,
                )
            )
            continue
        usable = [
            v for v in bands
            if v.estimated_receiving_spl_db is not None
            and v.evidence_state == 'measured'
        ]
        if not usable:
            criterion_results.append(
                IsolationCriterionResult(
                    criterion_id=criterion.criterion_id,
                    verdict='unknown',
                    note='no measured receiving-level estimate in domain',
                )
            )
            continue
        worst = min(
            criterion.limit_db - (v.estimated_receiving_spl_db or 0.0)
            for v in usable
        )
        criterion_results.append(
            IsolationCriterionResult(
                criterion_id=criterion.criterion_id,
                verdict='pass' if worst >= 0.0 else 'fail',
                evaluated_bands_hz=tuple(v.band_hz for v in usable),
                worst_margin_db=worst,
            )
        )

    limitations: list[str] = []
    if has_unmodelled:
        limitations.append(
            'declared_paths_unmodelled:' + ','.join(unmodelled_paths)
        )
    if not conformant:
        limitations.append('no_standard_conformant_field_measurement')
    if any(
        v.lf_domain == 'below_validated_domain' for v in band_verdicts
    ):
        limitations.append('low_frequency_below_validated_domain')
    if any(
        r.verdict in ('unknown', 'unsupported') for r in criterion_results
    ):
        limitations.append('criteria_not_fully_evaluated')

    lifecycle = scenario.construction_state
    if lifecycle in ('field_measured', 'qualified_with_limitations'):
        if not conformant:
            # The scenario claimed measured truth but none is bound —
            # fail closed to as-built-unverified.
            lifecycle = 'as_built_unverified'
            limitations.append('claimed_measurement_not_bound')
        elif lifecycle == 'qualified_with_limitations' and any(
            r.verdict in ('unknown', 'unsupported')
            for r in criterion_results
        ):
            lifecycle = 'field_measured'
            limitations.append('qualification_requires_all_criteria')

    probe = SoundIsolationQualification.model_construct(
        **_canon(
            SoundIsolationQualification,
            dict(
                qualification_id='',
                document_id=scenario.document_id,
                scenario_id=scenario.scenario_id,
                scenario_sha256=scenario.scenario_sha256,
                measurement_ids=measurement_ids,
                calibration_ids=tuple(
                    c.calibration_id for c in calibrations
                ),
                lifecycle_state=lifecycle,
                band_verdicts=tuple(band_verdicts),
                criterion_results=tuple(criterion_results),
                unmodelled_path_ids=unmodelled_paths,
                limitations=tuple(dict.fromkeys(limitations)),
                evaluated_at_utc=evaluated_at_utc,
                qualification_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return SoundIsolationQualification(
        **probe.model_dump(
            exclude={'qualification_id', 'qualification_sha256'}
        ),
        qualification_id=f'siq:{sha}',
        qualification_sha256=sha,
    )


def _band_key(band_hz: float) -> str:
    value = int(band_hz)
    return str(value) if float(value) == band_hz else str(band_hz)


def build_isolation_element(
    *,
    document_id: str,
    label: str,
    construction_class: IsolationConstructionClass = 'unknown',
    evidence: Sequence[IsolationElementEvidence] = (),
    **fields: Any,
) -> IsolationConstructionElement:
    probe = IsolationConstructionElement.model_construct(
        **_canon(
            IsolationConstructionElement,
            dict(
                element_id='',
                document_id=document_id,
                label=label,
                construction_class=construction_class,
                evidence=tuple(evidence),
                element_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IsolationConstructionElement(
        **probe.model_dump(exclude={'element_id', 'element_sha256'}),
        element_id=f'ise:{sha}',
        element_sha256=sha,
    )


def build_interroom_scenario(
    *,
    document_id: str,
    label: str,
    source_region_id: str,
    receiving_region_id: str,
    source_profile: IsolationSourceStressProfile,
    paths: Sequence[IsolationTransmissionPath] = (),
    criteria: Sequence[ReceivingRoomCriterion] = (),
    construction_state: IsolationConstructionState = 'design_prediction',
    created_at_utc: str,
    **fields: Any,
) -> InterRoomIsolationScenario:
    probe = InterRoomIsolationScenario.model_construct(
        **_canon(
            InterRoomIsolationScenario,
            dict(
                scenario_id='',
                document_id=document_id,
                label=label,
                source_region_id=source_region_id,
                receiving_region_id=receiving_region_id,
                paths=tuple(paths),
                source_profile=source_profile,
                criteria=tuple(criteria),
                construction_state=construction_state,
                created_at_utc=created_at_utc,
                scenario_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return InterRoomIsolationScenario(
        **probe.model_dump(exclude={'scenario_id', 'scenario_sha256'}),
        scenario_id=f'irs:{sha}',
        scenario_sha256=sha,
    )


def build_field_measurement(
    *,
    document_id: str,
    scenario: InterRoomIsolationScenario,
    method_profile: IsolationFieldMethod,
    bands: Sequence[IsolationBandEvidence] = (),
    measured_at_utc: str,
    **fields: Any,
) -> InterRoomFieldMeasurement:
    probe = InterRoomFieldMeasurement.model_construct(
        **_canon(
            InterRoomFieldMeasurement,
            dict(
                measurement_id='',
                document_id=document_id,
                scenario_id=scenario.scenario_id,
                scenario_sha256=scenario.scenario_sha256,
                method_profile=method_profile,
                bands=tuple(bands),
                measured_at_utc=measured_at_utc,
                measurement_sha256='',
                **fields,
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return InterRoomFieldMeasurement(
        **probe.model_dump(exclude={'measurement_id', 'measurement_sha256'}),
        measurement_id=f'irm:{sha}',
        measurement_sha256=sha,
    )


def build_calibration_record(
    *,
    document_id: str,
    scenario_id: str,
    model_ref: str,
    bounded_refinement: str,
    fit_measurement_ids: Sequence[str] = (),
    holdout_measurement_ids: Sequence[str] = (),
    outcome_note: str | None = None,
) -> IsolationCalibrationRecord:
    probe = IsolationCalibrationRecord.model_construct(
        **_canon(
            IsolationCalibrationRecord,
            dict(
                calibration_id='',
                document_id=document_id,
                scenario_id=scenario_id,
                model_ref=model_ref,
                fit_measurement_ids=tuple(fit_measurement_ids),
                holdout_measurement_ids=tuple(holdout_measurement_ids),
                bounded_refinement=bounded_refinement,
                outcome_note=outcome_note,
                calibration_sha256='',
            ),
        )
    )
    sha = _digest(probe.identity_payload())
    return IsolationCalibrationRecord(
        **probe.model_dump(exclude={'calibration_id', 'calibration_sha256'}),
        calibration_id=f'isc:{sha}',
        calibration_sha256=sha,
    )


__all__ = [
    'INTERROOM_ISOLATION_AUTHORITY_VERSION',
    'INTERROOM_ISOLATION_SCHEMA_VERSION',
    'ISOLATION_EVALUATION_BANDS_HZ',
    'ISOLATION_BAND_EVIDENCE_LABELS',
    'ISOLATION_CONSTRUCTION_STATE_LABELS',
    'ISOLATION_CRITERION_KIND_LABELS',
    'ISOLATION_CRITERION_VERDICT_LABELS',
    'ISOLATION_LF_DOMAIN_LABELS',
    'ISOLATION_PATH_KIND_LABELS',
    'LF_EXTENSION_BANDS_HZ',
    'STANDARD_RATING_DOMAIN_HZ',
    'InterRoomFieldMeasurement',
    'InterRoomIsolationScenario',
    'IsolationBandEvidence',
    'IsolationBandEvidenceState',
    'IsolationBandVerdict',
    'IsolationCalibrationRecord',
    'IsolationConstructionClass',
    'IsolationConstructionElement',
    'IsolationConstructionState',
    'IsolationCriterionKind',
    'IsolationCriterionResult',
    'IsolationCriterionVerdict',
    'IsolationElementEvidence',
    'IsolationFieldMethod',
    'IsolationJunctionEvidence',
    'IsolationLowFrequencyDomain',
    'IsolationNormalizationEvidence',
    'IsolationPathOpeningState',
    'IsolationQuantityMetric',
    'IsolationSourceStressProfile',
    'IsolationTransmissionPath',
    'IsolationTransmissionPathKind',
    'ReceivingRoomCriterion',
    'SoundIsolationQualification',
    'build_calibration_record',
    'build_field_measurement',
    'build_interroom_scenario',
    'build_isolation_element',
    'classify_band_lf_domain',
    'evaluate_isolation_qualification',
]
