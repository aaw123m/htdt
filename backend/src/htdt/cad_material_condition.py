"""Acoustic-material environmental / aging applicability authority (issue #776).

Measured or manufacturer absorption/impedance data describe a specimen
in a particular state. Installed porous/fibrous treatments can change
with moisture content, humidity exposure, thermal aging, compression,
contamination and structural aging — and the effect is
material-specific, never one universal correction factor. This module
records specimen/installed condition state and material-specific
durability evidence, then evaluates whether the original acoustic
evidence still applies.

Basis (research cited in #776):
Ando & Kosaka, *Effect of humidity on sound absorption of porous
materials*, Applied Acoustics 3(3), 1970 — RH can be an applicability
variable; does not justify a universal modern-material correction.
*Experimental assessment of the water content influence on
thermo-acoustic performance of building insulation materials*,
Construction and Building Materials (2018) — water-content effects are
material dependent. Yang et al., *A Study on the Acoustic Durability of
Sound-Absorbing Porous Materials*, ICSV 2023 — accelerated thermal
aging changed polyurethane-foam absorption and airflow resistivity.
*Durability of recycled and natural fibrous acoustic materials
undergoing long-term indoor ageing* (2026) — counter-evidence: many
fibrous materials largely retained acoustic properties; natural/mixed
families more sensitive than synthetic. ISO 16544:2012 is referenced
only as conditioning context (equilibrium moisture at specified
temperature/RH) and is not an acoustic-performance standard;
ISO 354:2003 test conditions belong to the original measurement
profile.

Hard rules carried into code:

* room RH/temperature is never a universal acoustic correction;
* room RH is never inferred as material moisture content without
  evidence (hygroscopic equilibrium is material and history
  dependent);
* a state-dependent acoustic model is eligible only when
  product/material-specific or validated class evidence exists —
  otherwise the verdict is a limitation, not a fabricated coefficient;
* property changes stay quantity-specific — one changed absorption
  coefficient never relabels every transport parameter;
* accelerated aging is not a known number of service years unless a
  validated mapping is explicitly declared.

Composition: #570 keeps quantity compatibility, #615 consumes
condition-specific parameters without overwriting the canonical
new-material record, #631 supplies installation identity, #704 owns
resonant-treatment models (porous aging assumptions are not applied to
resonant devices by category), #2/#161 keep room-air physics separate
from material-condition physics, #595 consumes reinspection verdicts,
and #719 may treat material degradation only as a hypothesis — never an
automatic calibration target.
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


def _require_refs(*refs: AuthorityRef | None) -> None:
    for ref in refs:
        if ref is not None and ref.ref_sha256 is None:
            raise ValueError(f'{ref.kind} reference must pin its sha256')


# ---------------------------------------------------------------------------
# Taxonomies (#776)


MaterialConditionKind = Literal[
    'new_as_tested',
    'conditioned',
    'installed_dry',
    'moisture_exposed',
    'wet_water_damaged',
    'thermally_aged',
    'mechanically_compressed',
    'contaminated_dust_loaded',
    'uv_weather_aged',
    'unknown',
]

ConditionContext = Literal[
    'source_specimen',
    'installed',
]

DurabilityEvidenceClass = Literal[
    'long_term_field_measurement',
    'accelerated_aging_test',
    'controlled_climate_exposure',
    'before_after_lab_test',
    'manufacturer_durability_claim',
    'in_situ_remeasurement',
    'literature_material_class',
    'unknown',
]

AffectedQuantity = Literal[
    'normal_incidence_absorption',
    'diffuse_absorption',
    'complex_impedance',
    'airflow_resistivity',
    'thickness_density',
    'porosity_transport',
    'resonant_tuning',
    'physical_integrity',
]

ChangeDirection = Literal[
    'increase',
    'decrease',
    'negligible_within_uncertainty',
    'mixed',
    'unknown',
]

ApplicabilityVerdict = Literal[
    'directly_applicable',
    'applicable_within_declared_domain',
    'applicable_with_unquantified_environmental_limitation',
    'remeasurement_recommended',
    'insufficient_durability_evidence',
    'unknown_applicability',
]

ReinspectionTrigger = Literal[
    'water_leak',
    'hvac_humidity_incident',
    'visible_sag_compression',
    'treatment_replacement',
    'cleaning_contamination',
    'service_interval_review',
]

ReinspectionVerdict = Literal[
    'remeasurement_recommended',
    'inspection_recommended',
    'monitor',
    'not_required',
]

TriState = Literal['observed', 'not_observed', 'unknown']

CavityKind = Literal['enclosed', 'open', 'partial', 'unknown']

MoistureMethod = Literal[
    'gravimetric',
    'dielectric',
    'calcium_carbide',
    'inferred_reported',
    'unknown',
]


CONDITION_LABELS: dict[str, str] = {
    'new_as_tested': '新品/測定時状態',
    'conditioned': '調湿済み',
    'installed_dry': '設置済み・乾燥',
    'moisture_exposed': '湿気曝露',
    'wet_water_damaged': '湿潤/水損',
    'thermally_aged': '熱劣化',
    'mechanically_compressed': '機械的圧縮',
    'contaminated_dust_loaded': '汚染/粉塵蓄積',
    'uv_weather_aged': 'UV/風雨劣化',
    'unknown': '不明',
}
CONTEXT_LABELS: dict[str, str] = {
    'source_specimen': '出所試料',
    'installed': '設置済み',
}
EVIDENCE_CLASS_LABELS: dict[str, str] = {
    'long_term_field_measurement': '長期実測',
    'accelerated_aging_test': '加速劣化試験',
    'controlled_climate_exposure': '管理気候曝露',
    'before_after_lab_test': '前後ラボ試験',
    'manufacturer_durability_claim': 'メーカー耐久性主張',
    'in_situ_remeasurement': '現場再測定',
    'literature_material_class': '文献上の材料分類',
    'unknown': '不明',
}
QUANTITY_LABELS: dict[str, str] = {
    'normal_incidence_absorption': '垂直入射吸音率',
    'diffuse_absorption': '拡散/残響室吸音率',
    'complex_impedance': '複素インピーダンス',
    'airflow_resistivity': '通気抵抗率',
    'thickness_density': '厚さ/密度',
    'porosity_transport': '孔隙率/輸送パラメータ',
    'resonant_tuning': '共振処理の同調',
    'physical_integrity': '物理的完全性',
}
DIRECTION_LABELS: dict[str, str] = {
    'increase': '増加',
    'decrease': '減少',
    'negligible_within_uncertainty': '不確かさ内でほぼ無変化',
    'mixed': '混在',
    'unknown': '不明',
}
VERDICT_LABELS: dict[str, str] = {
    'directly_applicable': '直接適用可能',
    'applicable_within_declared_domain': '宣言範囲内で適用可能',
    'applicable_with_unquantified_environmental_limitation':
        '定量化されない環境制約付きで適用可能',
    'remeasurement_recommended': '再測定推奨',
    'insufficient_durability_evidence': '耐久性証拠不足',
    'unknown_applicability': '適用性不明',
}
TRIGGER_LABELS: dict[str, str] = {
    'water_leak': '水漏れ',
    'hvac_humidity_incident': '空調故障/高湿インシデント',
    'visible_sag_compression': '目視の垂下/圧縮',
    'treatment_replacement': '処理材の交換',
    'cleaning_contamination': '大清掃/汚染',
    'service_interval_review': '定期レビュー',
}
REINSPECTION_LABELS: dict[str, str] = {
    'remeasurement_recommended': '再測定推奨',
    'inspection_recommended': '点検推奨',
    'monitor': '経過観察',
    'not_required': '不要',
}
TRISTATE_LABELS: dict[str, str] = {
    'observed': '確認あり',
    'not_observed': '確認なし',
    'unknown': '不明',
}
CAVITY_LABELS: dict[str, str] = {
    'enclosed': '密閉',
    'open': '開放',
    'partial': '部分的',
    'unknown': '不明',
}
MOISTURE_METHOD_LABELS: dict[str, str] = {
    'gravimetric': '重量法',
    'dielectric': '誘電法',
    'calcium_carbide': '炭化カルシウム法',
    'inferred_reported': '報告値',
    'unknown': '不明',
}


# ---------------------------------------------------------------------------
# Records


class AcousticMaterialConditionState(BaseModel):
    """Physical state of a material — specimen or installed (#776 §1–§3).

    State is an observation descriptor, never an automatic acoustic
    modifier. Specimen records preserve conditioning/age where known —
    unknown fields stay unset rather than invented; installed records
    preserve exposure history — a visually intact panel is not proof
    its transport/acoustic parameters are unchanged.
    """

    model_config = ConfigDict(frozen=True)

    condition_id: str
    condition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_ref: AuthorityRef
    context: ConditionContext
    condition_state: MaterialConditionKind
    # Specimen fields (context == 'source_specimen')
    specimen_age_days: int | None = Field(default=None, ge=0)
    conditioning_temperature_c: float | None = None
    conditioning_rh_percent: float | None = Field(
        default=None, ge=0, le=100
    )
    moisture_content_percent: float | None = Field(default=None, ge=0)
    moisture_content_method: MoistureMethod | None = None
    density_kg_m3: float | None = Field(default=None, gt=0)
    thickness_m: float | None = Field(default=None, gt=0)
    compression_ratio: float | None = Field(default=None, gt=0)
    facing_backing_state: str = ''
    storage_history: str = ''
    test_method: str = ''
    test_date_utc: str = ''
    source_provenance: str = ''
    # Installed fields (context == 'installed')
    installation_ref: AuthorityRef | None = None
    installed_at_utc: str = ''
    cavity_kind: CavityKind | None = None
    moisture_event_refs: tuple[AuthorityRef, ...] = ()
    hvac_exposure: str = ''
    contamination_observed: TriState | None = None
    damage_observed: TriState | None = None
    facing_changed: TriState | None = None
    inspection_refs: tuple[AuthorityRef, ...] = ()
    # Shared
    observed_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'AcousticMaterialConditionState':
        _require_refs(
            self.material_ref,
            self.installation_ref,
            *self.moisture_event_refs,
            *self.inspection_refs,
        )
        specimen_fields = {
            'specimen_age_days': self.specimen_age_days,
            'conditioning_temperature_c': self.conditioning_temperature_c,
            'conditioning_rh_percent': self.conditioning_rh_percent,
            'moisture_content_percent': self.moisture_content_percent,
            'moisture_content_method': self.moisture_content_method,
            'density_kg_m3': self.density_kg_m3,
            'thickness_m': self.thickness_m,
            'compression_ratio': self.compression_ratio,
            'facing_backing_state': self.facing_backing_state,
            'storage_history': self.storage_history,
            'test_method': self.test_method,
            'test_date_utc': self.test_date_utc,
            'source_provenance': self.source_provenance,
        }
        installed_fields = {
            'installation_ref': self.installation_ref,
            'installed_at_utc': self.installed_at_utc,
            'cavity_kind': self.cavity_kind,
            'moisture_event_refs': self.moisture_event_refs,
            'hvac_exposure': self.hvac_exposure,
            'contamination_observed': self.contamination_observed,
            'damage_observed': self.damage_observed,
            'facing_changed': self.facing_changed,
            'inspection_refs': self.inspection_refs,
        }
        if self.context == 'source_specimen':
            if any(v not in (None, '', ()) for v in installed_fields.values()):
                raise ValueError(
                    'installed-only fields are not valid on a '
                    'source_specimen state'
                )
        else:
            if any(v not in (None, '', ()) for v in specimen_fields.values()):
                raise ValueError(
                    'specimen-only fields are not valid on an '
                    'installed state'
                )
        if self.condition_state == 'conditioned' and (
            self.conditioning_temperature_c is None
            and self.conditioning_rh_percent is None
            and self.moisture_content_percent is None
        ):
            raise ValueError(
                'a conditioned state must record at least one '
                'conditioning fact — do not claim conditioning without it'
            )
        if (
            self.moisture_content_percent is not None
            and self.moisture_content_method is None
        ):
            raise ValueError(
                'moisture_content_percent requires a measurement method'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'condition_id', 'condition_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'AcousticMaterialConditionState':
        return _seal(
            cls, kwargs, 'condition_id', 'condition_sha256', 'amcs'
        )


class MaterialDurabilityEvidence(BaseModel):
    """Material/class-specific durability evidence (#776 §6, §7).

    Evidence classes stay distinct: a long-term field measurement, an
    accelerated test, a controlled-climate exposure, a before/after lab
    test, a manufacturer claim, an in-situ remeasurement and a
    literature class claim are never merged into one durability scalar.
    ``service_year_mapping_validated`` is the only way an accelerated
    result may be read as service years — default False means the
    evidence remains its own class.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_family_ref: AuthorityRef
    evidence_class: DurabilityEvidenceClass
    affected_quantities: tuple[AffectedQuantity, ...] = ()
    change_direction: ChangeDirection = 'unknown'
    magnitude_descriptor: str = ''
    material_family_label: str = ''
    # Exposure domain the evidence was produced under
    temperature_c: float | None = None
    rh_percent: float | None = Field(default=None, ge=0, le=100)
    duration_hours: float | None = Field(default=None, ge=0)
    service_year_mapping_validated: bool = False
    method_source: str = ''
    observed_at_utc: str = Field(min_length=1)
    note: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'MaterialDurabilityEvidence':
        _require_refs(self.material_family_ref)
        bounded = (
            self.temperature_c is not None
            or self.rh_percent is not None
            or self.duration_hours is not None
        )
        if self.evidence_class in (
            'accelerated_aging_test',
            'controlled_climate_exposure',
        ) and not bounded:
            raise ValueError(
                'exposure-domain evidence must bound at least one of '
                'temperature/rh/duration'
            )
        if self.evidence_class in (
            'accelerated_aging_test',
            'controlled_climate_exposure',
            'before_after_lab_test',
        ) and not self.affected_quantities:
            raise ValueError(
                'test evidence must name the quantities it measured — '
                'changes stay quantity-specific'
            )
        if self.evidence_class == 'long_term_field_measurement' and (
            self.duration_hours is None
        ):
            raise ValueError(
                'long-term field evidence must record its duration'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'MaterialDurabilityEvidence':
        return _seal(
            cls, kwargs, 'evidence_id', 'evidence_sha256', 'mdev'
        )


class MaterialEvidenceApplicability(BaseModel):
    """Whether source evidence still applies to the installed state
    (#776 §4, §11).

    Produced by :func:`evaluate_material_applicability`. When a
    condition-specific model was consumed, ``consumed_refs`` pins the
    exact evidence and parameters used — condition/evidence identity
    participates in prediction identity.
    """

    model_config = ConfigDict(frozen=True)

    applicability_id: str
    applicability_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    material_ref: AuthorityRef
    specimen_state_ref: AuthorityRef | None = None
    installed_state_ref: AuthorityRef | None = None
    verdict: ApplicabilityVerdict
    evidence_refs: tuple[AuthorityRef, ...] = ()
    matched_evidence_refs: tuple[AuthorityRef, ...] = ()
    consumed_refs: tuple[AuthorityRef, ...] = ()
    basis_note: str = ''
    assessed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'MaterialEvidenceApplicability':
        _require_refs(
            self.material_ref,
            self.specimen_state_ref,
            self.installed_state_ref,
            *self.evidence_refs,
            *self.matched_evidence_refs,
            *self.consumed_refs,
        )
        if self.verdict == 'directly_applicable' and self.consumed_refs:
            raise ValueError(
                'directly_applicable means the original evidence was '
                'used as-is — it must not claim condition-specific '
                'consumption'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'applicability_id', 'applicability_sha256'},
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'MaterialEvidenceApplicability':
        return _seal(
            cls,
            kwargs,
            'applicability_id',
            'applicability_sha256',
            'mapa',
        )


class ReinspectionAssessment(BaseModel):
    """Drift-triggered material reinspection verdict (#776 §12).

    Known events — water leak, HVAC/humidity incident, visible
    sag/compression, treatment replacement, major cleaning, policy
    service interval — can stale material evidence and request
    re-inspection or remeasurement. No universal calendar expiration is
    implied.
    """

    model_config = ConfigDict(frozen=True)

    assessment_id: str
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str
    installed_state_ref: AuthorityRef
    trigger: ReinspectionTrigger
    verdict: ReinspectionVerdict
    basis_refs: tuple[AuthorityRef, ...] = ()
    basis_note: str = ''
    assessed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'ReinspectionAssessment':
        _require_refs(self.installed_state_ref, *self.basis_refs)
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'assessment_id', 'assessment_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'ReinspectionAssessment':
        return _seal(
            cls, kwargs, 'assessment_id', 'assessment_sha256', 'risp'
        )


# ---------------------------------------------------------------------------
# Evaluation


# Which evidence classes can bound which installed condition kinds.
# Manufacturer claims and literature class claims never bound a
# degraded state — they are claims, not coverage.
_EVIDENCE_STATE_COVERAGE: dict[str, frozenset[str]] = {
    'installed_dry': frozenset({
        'controlled_climate_exposure',
        'before_after_lab_test',
        'in_situ_remeasurement',
    }),
    'moisture_exposed': frozenset({
        'controlled_climate_exposure',
        'before_after_lab_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
    'wet_water_damaged': frozenset({
        'before_after_lab_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
    'thermally_aged': frozenset({
        'accelerated_aging_test',
        'before_after_lab_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
    'mechanically_compressed': frozenset({
        'before_after_lab_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
    'contaminated_dust_loaded': frozenset({
        'before_after_lab_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
    'uv_weather_aged': frozenset({
        'accelerated_aging_test',
        'long_term_field_measurement',
        'in_situ_remeasurement',
    }),
}


def _state_ref(
    state: AcousticMaterialConditionState | None,
) -> AuthorityRef | None:
    if state is None:
        return None
    return AuthorityRef(
        kind='material_condition_state',
        ref_id=state.condition_id,
        ref_sha256=state.condition_sha256,
    )


def _evidence_ref(
    evidence: MaterialDurabilityEvidence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='material_durability_evidence',
        ref_id=evidence.evidence_id,
        ref_sha256=evidence.evidence_sha256,
    )


def _covers_state(
    evidence: MaterialDurabilityEvidence,
    installed: AcousticMaterialConditionState,
    material_ref: AuthorityRef,
) -> bool:
    if evidence.material_family_ref.ref_id not in (
        material_ref.ref_id,
        installed.material_ref.ref_id,
    ):
        return False
    covering = _EVIDENCE_STATE_COVERAGE.get(
        installed.condition_state, frozenset()
    )
    return evidence.evidence_class in covering


def evaluate_material_applicability(
    material_ref: AuthorityRef,
    specimen: AcousticMaterialConditionState | None,
    installed: AcousticMaterialConditionState | None,
    evidence: tuple[MaterialDurabilityEvidence, ...] = (),
    *,
    consumed_refs: tuple[AuthorityRef, ...] = (),
    assessed_at_utc: str,
    basis_note: str = '',
) -> MaterialEvidenceApplicability:
    """Judge whether source material evidence applies as installed.

    Never produces a corrected coefficient: the verdict ladder ends in
    limitation/remediation words, and condition-specific consumption is
    only recorded through ``consumed_refs`` the caller actually used
    (e.g. a #615 aged-state parameter set).
    """
    _require_refs(material_ref, *consumed_refs)
    document_id = (
        installed.document_id if installed is not None
        else specimen.document_id if specimen is not None
        else ''
    )

    def _record(verdict: ApplicabilityVerdict, matched=()) -> (
        MaterialEvidenceApplicability
    ):
        return MaterialEvidenceApplicability.create(
            document_id=document_id,
            material_ref=material_ref,
            specimen_state_ref=_state_ref(specimen),
            installed_state_ref=_state_ref(installed),
            verdict=verdict,
            evidence_refs=tuple(_evidence_ref(e) for e in evidence),
            matched_evidence_refs=matched,
            consumed_refs=consumed_refs,
            basis_note=basis_note,
            assessed_at_utc=assessed_at_utc,
        )

    if specimen is None and installed is None:
        return _record('insufficient_durability_evidence')

    if specimen is None:
        return _record('unknown_applicability')

    if installed is None:
        # Source state known, installed state never observed: the
        # original curve is usable only with an explicit limitation.
        return _record(
            'applicable_with_unquantified_environmental_limitation'
        )

    if installed.condition_state == 'unknown':
        return _record(
            'applicable_with_unquantified_environmental_limitation'
        )

    if specimen.condition_state == 'unknown':
        return _record('insufficient_durability_evidence')

    dry_family = {'new_as_tested', 'installed_dry'}
    if (
        specimen.condition_state in dry_family
        and installed.condition_state in dry_family
    ):
        return _record('directly_applicable')

    degraded = (
        installed.condition_state in _EVIDENCE_STATE_COVERAGE
        and installed.condition_state != 'installed_dry'
    )
    if not degraded:
        # e.g. conditioned specimen vs installed_dry: same physical
        # family but the conditioning changed the specimen — bound by
        # evidence or declare the limitation.
        matched = tuple(
            _evidence_ref(e) for e in evidence
            if _covers_state(e, installed, material_ref)
        )
        if matched:
            return _record('applicable_within_declared_domain', matched)
        return _record(
            'applicable_with_unquantified_environmental_limitation'
        )

    matched = tuple(
        _evidence_ref(e) for e in evidence
        if _covers_state(e, installed, material_ref)
    )
    if not evidence:
        return _record('insufficient_durability_evidence')
    if matched:
        # Material-specific evidence bounds the state change; whether a
        # condition-specific model was consumed is recorded separately
        # in consumed_refs so prediction identity stays honest.
        return _record('applicable_within_declared_domain', matched)
    return _record('remeasurement_recommended')


def evaluate_reinspection_need(
    installed: AcousticMaterialConditionState,
    *,
    basis_refs: tuple[AuthorityRef, ...] = (),
    assessed_at_utc: str,
) -> ReinspectionAssessment:
    """Map an installed-state observation to a reinspection verdict
    (#776 §12)."""
    state = installed.condition_state
    if state == 'wet_water_damaged' or installed.moisture_event_refs:
        trigger: ReinspectionTrigger = 'water_leak'
        verdict: ReinspectionVerdict = 'remeasurement_recommended'
    elif state == 'moisture_exposed':
        trigger = 'hvac_humidity_incident'
        verdict = 'remeasurement_recommended'
    elif state in ('mechanically_compressed',):
        trigger = 'visible_sag_compression'
        verdict = 'inspection_recommended'
    elif state == 'contaminated_dust_loaded' or (
        installed.contamination_observed == 'observed'
    ):
        trigger = 'cleaning_contamination'
        verdict = 'inspection_recommended'
    elif state in ('thermally_aged', 'uv_weather_aged'):
        trigger = 'service_interval_review'
        verdict = 'monitor'
    else:
        trigger = 'service_interval_review'
        verdict = 'not_required'
    return ReinspectionAssessment.create(
        document_id=installed.document_id,
        installed_state_ref=_state_ref(installed),
        trigger=trigger,
        verdict=verdict,
        basis_refs=basis_refs,
        assessed_at_utc=assessed_at_utc,
    )
