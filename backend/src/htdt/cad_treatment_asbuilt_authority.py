"""As-built acoustic-treatment qualification authority (#631,
REV57-INST).

Laboratory material data and a CAD treatment object do not prove the
installed absorber/diffuser/scatterer has the acoustic build-up, area,
backing, air gap, facing, orientation or position the prediction
assumed. This module proves — or honestly fails to prove — the
design-vs-as-built match per parameter:

- :class:`CadTreatmentInstallSpec` — the designed treatment identity:
  product/material evidence ref, intended acoustic role, bound
  surface/scene element, pose, dimensions/area, thickness, air gap,
  backing, facing, mounting type and coverage fraction. The CAD object
  is design intent, not field truth.
- :class:`CadTreatmentAsBuiltObservation` — per-parameter as-built
  evidence: thickness, air gap, area, facing, orientation and
  placement each carry their own
  :data:`AsBuiltEvidenceState` — a photo proves presence, never a
  hidden cavity depth.
- :class:`CadTreatmentInspection` — the immutable field-inspection
  record (measured dimensions, mount/gap observations, product
  labels, damage, inaccessible parameters, operator/instrument refs).
- :class:`CadTreatmentQualification` +
  :func:`evaluate_treatment_asbuilt` — the fail-closed
  design-vs-as-built comparison: per-parameter
  match/deviation/unknown and a joint verdict; a treatment change
  stales affected predictions rather than silently keeping them
  ``validated``.

Honesty rules baked in:

- ISO 354 / ISO 11654 / ISO 20189 evidence stays bound to its lab
  mounting/specimen semantics — a catalogue rating never reads as
  installed truth, and a single-number rating (αw/NRC class) never
  claims full frequency-dependent solver equivalence (#596
  substitution composition).
- ``HIDDEN_UNVERIFIED`` / ``UNKNOWN`` states never promote to
  observed values — a concealed air gap is not a zero air gap.
- Diffuser/scatterer orientation is first-class: rotating a
  directional device invalidates the intended scattering plane.
- Before/after room measurements report ``consistent`` /
  ``inconsistent`` with the expected treatment effect — a measured
  delta never uniquely identifies the hidden material model (#564 /
  #604 boundary).
- A site-built ``bass trap`` contractor note is not a physical
  boundary model — custom assemblies carry construction layers or
  stay limited.

Literature basis
----------------
- ISO 354:2003 (confirmed 2024) — laboratory absorption under defined
  mounting/specimen conditions; design evidence, not field proof.
- ISO 11654:1997 (confirmed 2023) — single-number absorber rating from
  laboratory data; never a full frequency-dependent solver boundary.
- ISO 20189:2018 (confirmed 2024) — single-object/screens evidence for
  room-acoustic calculation; never an infinite-surface coefficient.
- CEDIA/CTA-RP22 v1.2 — absorber thickness and rear air space change
  low-frequency performance; diffuser orientation/placement matters.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


TAI_SCHEMA_VERSION = 'tai-1'
TAI_EVALUATION_VERSION = 'tai-eval-1'

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
# Taxonomies (#631)
# ---------------------------------------------------------------------------

TreatmentClass = Literal[
    'porous_absorber',
    'membrane_panel_absorber',
    'perforated_slotted_resonant',
    'helmholtz_resonant',
    'diffuser',
    'scatterer',
    'hybrid_absorption_diffusion',
    'curtain_soft_finish',
    'carpet_rug',
    'bass_trap_corner_buildup',
    'custom_built_in_assembly',
    'other',
    'unknown',
]
"""Treatment taxonomy — porous-layer semantics never apply to
resonant devices by category convenience."""

LabEvidenceClass = Literal[
    'iso_354_specimen',
    'iso_11654_rating',
    'iso_20189_single_object',
    'manufacturer_declared',
    'engineering_model',
    'none',
    'unknown',
]
"""Laboratory/material evidence classes — each keeps its mounting/
specimen semantics; none of them is installed truth."""

AsBuiltEvidenceState = Literal[
    'field_measured',
    'field_observed',
    'installer_documented',
    'manufacturer_build_spec',
    'design_only',
    'inferred_from_geometry',
    'hidden_unverified',
    'unknown',
]
"""Per-parameter evidence state. An installation photo can prove
presence/position while the hidden cavity depth stays
``hidden_unverified``."""

AcousticRole = Literal[
    'early_reflection_control',
    'front_wall_baffle',
    'rear_wall_absorption',
    'rear_wall_scattering',
    'ceiling_cloud',
    'modal_region_control',
    'flutter_control',
    'general_decay',
    'other',
    'unknown',
]

ParameterVerdict = Literal['match', 'deviation', 'unknown']

AsBuiltVerdict = Literal[
    'qualified_as_built',
    'qualified_with_limitations',
    'prediction_stale',
    'incompatible',
    'insufficient_evidence',
]

BeforeAfterResult = Literal[
    'consistent_with_expected',
    'inconsistent_with_expected',
    'inconclusive',
    'not_performed',
]


_TREATMENT_PARAMETERS = (
    'thickness',
    'air_gap',
    'area',
    'facing',
    'orientation',
    'placement',
    'backing',
)
"""Parameters compared between spec and as-built."""


# ---------------------------------------------------------------------------
# Designed treatment spec
# ---------------------------------------------------------------------------


class CadTreatmentInstallSpec(BaseModel):
    """The designed treatment / build-up identity for one instance.

    Binds the product/material evidence (with its lab class and exact
    mounting/specimen semantics), intended acoustic role, scene
    surface, geometry and every build-up parameter the prediction
    relied on.
    """

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    treatment_class: TreatmentClass = 'unknown'
    product_identity: str | None = None
    material_evidence_ref: AuthorityRef | None = None
    lab_evidence_class: LabEvidenceClass = 'unknown'
    lab_mounting_condition: str | None = None
    acoustic_role: AcousticRole = 'unknown'
    surface_ref: str | None = None
    scene_element_ref: AuthorityRef | None = None
    pose_json: str | None = None
    width_m: float | None = None
    height_m: float | None = None
    area_m2: float | None = None
    thickness_m: float | None = None
    air_gap_m: float | None = None
    backing: str | None = None
    facing: str | None = None
    mounting_type: str | None = None
    coverage_fraction: float | None = None
    construction_layers_json: str | None = None
    predicted_boundary_ref: AuthorityRef | None = None
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_spec(self) -> 'CadTreatmentInstallSpec':
        _require_iso8601(self.declared_at_utc, 'spec declared_at_utc')
        for ref, label in (
            (self.material_evidence_ref, 'material_evidence_ref'),
            (self.scene_element_ref, 'scene_element_ref'),
            (self.predicted_boundary_ref, 'predicted_boundary_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        for label, value in (
            ('width_m', self.width_m),
            ('height_m', self.height_m),
            ('area_m2', self.area_m2),
            ('thickness_m', self.thickness_m),
            ('air_gap_m', self.air_gap_m),
            ('coverage_fraction', self.coverage_fraction),
        ):
            if value is not None:
                _require_finite(value, f'spec {label}')
                if value < 0:
                    raise ValueError(f'spec {label} must be non-negative')
        if self.lab_evidence_class in (
            'iso_354_specimen',
            'iso_11654_rating',
            'iso_20189_single_object',
        ) and self.lab_mounting_condition is None:
            raise ValueError(
                'laboratory evidence requires its mounting/specimen '
                'condition — a rating detached from its test mounting '
                'is not importable evidence'
            )
        if self.treatment_class == 'custom_built_in_assembly' and (
            self.construction_layers_json is None
        ):
            raise ValueError(
                'a site-built assembly must declare its construction '
                'layers — a contractor note is not a boundary model'
            )
        expected = _hash(self.identity_payload())
        if self.spec_sha256 != expected:
            raise ValueError('treatment spec hash mismatch')
        if self.spec_id != _semantic_id('taispec', expected):
            raise ValueError('spec id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'treatment_class': self.treatment_class,
            'product_identity': self.product_identity,
            'material_evidence_ref': (
                self.material_evidence_ref.model_dump(mode='json')
                if self.material_evidence_ref is not None else None
            ),
            'lab_evidence_class': self.lab_evidence_class,
            'lab_mounting_condition': self.lab_mounting_condition,
            'acoustic_role': self.acoustic_role,
            'surface_ref': self.surface_ref,
            'scene_element_ref': (
                self.scene_element_ref.model_dump(mode='json')
                if self.scene_element_ref is not None else None
            ),
            'pose_json': self.pose_json,
            'width_m': self.width_m,
            'height_m': self.height_m,
            'area_m2': self.area_m2,
            'thickness_m': self.thickness_m,
            'air_gap_m': self.air_gap_m,
            'backing': self.backing,
            'facing': self.facing,
            'mounting_type': self.mounting_type,
            'coverage_fraction': self.coverage_fraction,
            'construction_layers_json': self.construction_layers_json,
            'predicted_boundary_ref': (
                self.predicted_boundary_ref.model_dump(mode='json')
                if self.predicted_boundary_ref is not None else None
            ),
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }


def treatment_spec_binding(
    spec: CadTreatmentInstallSpec,
) -> AuthorityRef:
    return AuthorityRef(
        kind='treatment_install_spec',
        ref_id=spec.spec_id,
        ref_sha256=spec.spec_sha256,
    )


# ---------------------------------------------------------------------------
# As-built observation
# ---------------------------------------------------------------------------


class CadParameterObservation(BaseModel):
    """One parameter's as-built evidence.

    ``state`` never promotes beyond what the evidence observes —
    ``field_observed`` presence is not ``field_measured`` dimension,
    and hidden construction stays ``hidden_unverified``.
    """

    model_config = ConfigDict(frozen=True)

    parameter: str = Field(min_length=1)
    state: AsBuiltEvidenceState = 'unknown'
    observed_value: str | None = None
    observed_numeric: float | None = None
    instrument_ref: AuthorityRef | None = None
    method: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_parameter(self) -> 'CadParameterObservation':
        if self.observed_numeric is not None:
            _require_finite(
                self.observed_numeric, f'{self.parameter} observed'
            )
        if self.instrument_ref is not None and (
            self.instrument_ref.ref_sha256 is None
        ):
            raise ValueError('instrument_ref must pin its sha256')
        if self.state == 'field_measured' and (
            self.observed_numeric is None
            and self.observed_value is None
        ):
            raise ValueError(
                'a field_measured state requires the measured value'
            )
        return self


class CadTreatmentAsBuiltObservation(BaseModel):
    """The observed as-built state of one installed treatment
    instance, per parameter."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    spec_ref: AuthorityRef
    installed_product_identity: str | None = None
    parameters: tuple[CadParameterObservation, ...] = ()
    substituted: bool | None = None
    substitution_evidence: str | None = None
    observed_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadTreatmentAsBuiltObservation':
        _require_iso8601(
            self.observed_at_utc, 'observation observed_at_utc'
        )
        if self.spec_ref.ref_sha256 is None:
            raise ValueError('observations must pin the spec sha256')
        seen = [p.parameter for p in self.parameters]
        if len(seen) != len(set(seen)):
            raise ValueError('duplicate parameter observations')
        if self.substituted and self.substitution_evidence is None:
            raise ValueError(
                'a declared substitution requires its evidence — '
                'equal single-number ratings are not equivalence'
            )
        expected = _hash(self.identity_payload())
        if self.observation_sha256 != expected:
            raise ValueError('as-built observation hash mismatch')
        if self.observation_id != _semantic_id('taiobs', expected):
            raise ValueError('observation id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'spec_ref': self.spec_ref.model_dump(mode='json'),
            'installed_product_identity': self.installed_product_identity,
            'parameters': [
                p.model_dump(mode='json') for p in self.parameters
            ],
            'substituted': self.substituted,
            'substitution_evidence': self.substitution_evidence,
            'observed_at_utc': self.observed_at_utc,
            'provenance_json': self.provenance_json,
        }

    def parameter_state(self, parameter: str) -> AsBuiltEvidenceState:
        for p in self.parameters:
            if p.parameter == parameter:
                return p.state
        return 'unknown'

    def parameter_numeric(self, parameter: str) -> float | None:
        for p in self.parameters:
            if p.parameter == parameter:
                return p.observed_numeric
        return None


def asbuilt_observation_binding(
    observation: CadTreatmentAsBuiltObservation,
) -> AuthorityRef:
    return AuthorityRef(
        kind='treatment_asbuilt_observation',
        ref_id=observation.observation_id,
        ref_sha256=observation.observation_sha256,
    )


# ---------------------------------------------------------------------------
# Inspection record
# ---------------------------------------------------------------------------


class CadTreatmentInspection(BaseModel):
    """The immutable field-inspection record for one installation
    pass. Photos/dimensions/labels are evidence artifacts referenced
    by id/hash — inaccessible parameters stay named, never inferred."""

    model_config = ConfigDict(frozen=True)

    inspection_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_refs: tuple[AuthorityRef, ...] = ()
    photo_artifact_refs: tuple[str, ...] = ()
    measured_dimensions_json: str | None = None
    product_label_evidence: str | None = None
    visible_damage: str | None = None
    hidden_parameters: tuple[str, ...] = ()
    operator: str | None = None
    instrument_ref: AuthorityRef | None = None
    inspected_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    inspection_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_inspection(self) -> 'CadTreatmentInspection':
        _require_iso8601(
            self.inspected_at_utc, 'inspection inspected_at_utc'
        )
        for ref in self.observation_refs:
            if ref.ref_sha256 is None:
                raise ValueError(
                    'observation refs must pin their sha256'
                )
        if self.instrument_ref is not None and (
            self.instrument_ref.ref_sha256 is None
        ):
            raise ValueError('instrument_ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.inspection_sha256 != expected:
            raise ValueError('inspection hash mismatch')
        if self.inspection_id != _semantic_id('taiinsp', expected):
            raise ValueError('inspection id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'observation_refs': [
                r.model_dump(mode='json')
                for r in self.observation_refs
            ],
            'photo_artifact_refs': list(self.photo_artifact_refs),
            'measured_dimensions_json': self.measured_dimensions_json,
            'product_label_evidence': self.product_label_evidence,
            'visible_damage': self.visible_damage,
            'hidden_parameters': list(self.hidden_parameters),
            'operator': self.operator,
            'instrument_ref': (
                self.instrument_ref.model_dump(mode='json')
                if self.instrument_ref is not None else None
            ),
            'inspected_at_utc': self.inspected_at_utc,
            'provenance_json': self.provenance_json,
        }


def treatment_inspection_binding(
    inspection: CadTreatmentInspection,
) -> AuthorityRef:
    return AuthorityRef(
        kind='treatment_inspection',
        ref_id=inspection.inspection_id,
        ref_sha256=inspection.inspection_sha256,
    )


# ---------------------------------------------------------------------------
# Qualification verdict
# ---------------------------------------------------------------------------


class CadTreatmentQualification(BaseModel):
    """Sealed design-vs-as-built verdict for one treatment instance.

    Per-parameter match/deviation/unknown stay explicit; the verdict
    never collapses ``panel present`` into ``boundary as designed``.
    ``prediction_validity`` carries what the comparison does to the
    solver boundary the prediction relied on.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    spec_ref: AuthorityRef
    observation_ref: AuthorityRef | None = None
    inspection_ref: AuthorityRef | None = None
    parameter_verdicts_json: str = '{}'
    prediction_validity: Literal[
        'remains_eligible',
        'limited',
        'stale',
        'unknown',
    ]
    before_after_result: BeforeAfterResult
    verdict: AsBuiltVerdict
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadTreatmentQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.spec_ref.ref_sha256 is None:
            raise ValueError('qualifications must pin the spec sha256')
        for ref, label in (
            (self.observation_ref, 'observation_ref'),
            (self.inspection_ref, 'inspection_ref'),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('treatment qualification hash mismatch')
        if self.qualification_id != _semantic_id('taieval', expected):
            raise ValueError('qualification id does not match its hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'spec_ref': self.spec_ref.model_dump(mode='json'),
            'observation_ref': (
                self.observation_ref.model_dump(mode='json')
                if self.observation_ref is not None else None
            ),
            'inspection_ref': (
                self.inspection_ref.model_dump(mode='json')
                if self.inspection_ref is not None else None
            ),
            'parameter_verdicts_json': self.parameter_verdicts_json,
            'prediction_validity': self.prediction_validity,
            'before_after_result': self.before_after_result,
            'verdict': self.verdict,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


def treatment_qualification_binding(
    qualification: CadTreatmentQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='treatment_qualification',
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


def build_install_spec(**kwargs: Any) -> CadTreatmentInstallSpec:
    """Seal one designed treatment spec."""
    kwargs.setdefault('declared_at_utc', _utc_now())
    return _seal_model(
        CadTreatmentInstallSpec, dict(kwargs),
        'spec_id', 'spec_sha256', 'taispec',
    )


def build_asbuilt_observation(
    *,
    document_id: str,
    spec: CadTreatmentInstallSpec | AuthorityRef,
    installed_product_identity: str | None = None,
    parameters: tuple[CadParameterObservation, ...] = (),
    substituted: bool | None = None,
    substitution_evidence: str | None = None,
    observed_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadTreatmentAsBuiltObservation:
    """Seal one as-built observation."""
    spec_ref = (
        treatment_spec_binding(spec)
        if isinstance(spec, CadTreatmentInstallSpec)
        else spec
    )
    payload = dict(
        document_id=document_id,
        spec_ref=spec_ref,
        installed_product_identity=installed_product_identity,
        parameters=parameters,
        substituted=substituted,
        substitution_evidence=substitution_evidence,
        observed_at_utc=observed_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadTreatmentAsBuiltObservation, payload,
        'observation_id', 'observation_sha256', 'taiobs',
    )


def build_inspection(
    *,
    document_id: str,
    observations: tuple[CadTreatmentAsBuiltObservation | AuthorityRef,
                        ...] = (),
    photo_artifact_refs: tuple[str, ...] = (),
    measured_dimensions_json: str | None = None,
    product_label_evidence: str | None = None,
    visible_damage: str | None = None,
    hidden_parameters: tuple[str, ...] = (),
    operator: str | None = None,
    instrument_ref: AuthorityRef | None = None,
    inspected_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadTreatmentInspection:
    """Seal one field-inspection record."""
    observation_refs = tuple(
        asbuilt_observation_binding(o)
        if isinstance(o, CadTreatmentAsBuiltObservation)
        else o
        for o in observations
    )
    payload = dict(
        document_id=document_id,
        observation_refs=observation_refs,
        photo_artifact_refs=photo_artifact_refs,
        measured_dimensions_json=measured_dimensions_json,
        product_label_evidence=product_label_evidence,
        visible_damage=visible_damage,
        hidden_parameters=hidden_parameters,
        operator=operator,
        instrument_ref=instrument_ref,
        inspected_at_utc=inspected_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadTreatmentInspection, payload,
        'inspection_id', 'inspection_sha256', 'taiinsp',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

import json as _json


def evaluate_treatment_asbuilt(
    *,
    document_id: str,
    spec: CadTreatmentInstallSpec,
    observation: CadTreatmentAsBuiltObservation | None = None,
    inspection: CadTreatmentInspection | None = None,
    before_after_result: BeforeAfterResult = 'not_performed',
    evaluated_at_utc: str | None = None,
) -> CadTreatmentQualification:
    """Fail-closed design-vs-as-built comparison.

    Each treatment parameter is compared independently: a field-
    measured/observed/documented value equal to the spec reads
    ``match``; a differing value reads ``deviation``; hidden,
    design-only or unobserved parameters read ``unknown``. Any
    deviation on a build-up parameter (thickness, air gap, facing,
    backing, area) marks the assumed boundary ``stale`` — the old
    prediction never survives a changed build-up.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    if observation is not None and (
        observation.spec_ref.ref_id != spec.spec_id
        or observation.spec_ref.ref_sha256 != spec.spec_sha256
    ):
        raise ValueError(
            'as-built observation binds a different spec'
        )
    if inspection is not None:
        for ref in inspection.observation_refs:
            if observation is not None and (
                ref.ref_id != observation.observation_id
            ):
                raise ValueError(
                    'inspection references an unbound observation'
                )

    verdicts: dict[str, ParameterVerdict] = {}
    numeric_spec = {
        'thickness': spec.thickness_m,
        'air_gap': spec.air_gap_m,
        'area': spec.area_m2,
    }
    text_spec = {
        'facing': spec.facing,
        'backing': spec.backing,
        'orientation': spec.pose_json,
        'placement': spec.surface_ref,
    }
    if observation is None:
        for p in _TREATMENT_PARAMETERS:
            verdicts[p] = 'unknown'
        reasons.append('no as-built observation bound')
    else:
        for p in _TREATMENT_PARAMETERS:
            state = observation.parameter_state(p)
            spec_value = numeric_spec.get(p)
            text_value = text_spec.get(p)
            if state in ('hidden_unverified', 'unknown', 'design_only'):
                verdicts[p] = 'unknown'
                if state == 'hidden_unverified':
                    reasons.append(
                        f'{p}: concealed in the installed assembly — '
                        'UNKNOWN, not assumed to match design'
                    )
                continue
            observed_num = observation.parameter_numeric(p)
            observed = next(
                (o for o in observation.parameters
                 if o.parameter == p),
                None,
            )
            observed_text = (
                observed.observed_value if observed is not None else None
            )
            if spec_value is not None and observed_num is not None:
                verdicts[p] = (
                    'match' if abs(observed_num - spec_value) < 1e-9
                    else 'deviation'
                )
            elif text_value is not None and observed_text is not None:
                verdicts[p] = (
                    'match' if observed_text == text_value
                    else 'deviation'
                )
            elif spec_value is None and text_value is None:
                verdicts[p] = 'match' if state in (
                    'field_measured', 'field_observed',
                    'installer_documented',
                ) else 'unknown'
            else:
                verdicts[p] = 'unknown'
        if observation.substituted:
            verdicts['product_identity'] = 'deviation'
            reasons.append(
                'installed product is a substitution — equal '
                'single-number rating does not inherit solver '
                'equivalence (#596 composition)'
            )

    deviations = [p for p, v in verdicts.items() if v == 'deviation']
    unknowns = [p for p, v in verdicts.items() if v == 'unknown']

    if deviations:
        prediction_validity = 'stale'
        reasons.append(
            'build-up deviations invalidate the assumed boundary: '
            + ', '.join(deviations)
        )
    elif unknowns:
        prediction_validity = 'limited'
        reasons.append(
            'parameters not verified as-built: ' + ', '.join(unknowns)
        )
    else:
        prediction_validity = 'remains_eligible'

    if observation is None:
        verdict: AsBuiltVerdict = 'insufficient_evidence'
    elif deviations:
        verdict = 'prediction_stale'
        if spec.predicted_boundary_ref is not None:
            reasons.append(
                'the pinned predicted-boundary ref must be re-derived '
                'before its result is used'
            )
    elif spec.lab_evidence_class in ('none', 'unknown'):
        verdict = 'qualified_with_limitations'
        reasons.append(
            'no laboratory/material evidence bound — the boundary '
            'model rests on declaration only'
        )
    elif unknowns:
        verdict = 'qualified_with_limitations'
    else:
        verdict = 'qualified_as_built'

    if before_after_result == 'inconsistent_with_expected':
        verdict = 'incompatible'
        reasons.append(
            'before/after room measurement is inconsistent with the '
            'expected treatment effect — the installation does not '
            'behave like the design'
        )
    elif before_after_result == 'consistent_with_expected':
        reasons.append(
            'before/after measurement is consistent with the expected '
            'effect (observed room change — not a unique material '
            'identification)'
        )

    payload = dict(
        document_id=document_id,
        spec_ref=treatment_spec_binding(spec),
        observation_ref=(
            asbuilt_observation_binding(observation)
            if observation is not None else None
        ),
        inspection_ref=(
            treatment_inspection_binding(inspection)
            if inspection is not None else None
        ),
        parameter_verdicts_json=_json.dumps(
            verdicts, sort_keys=True
        ),
        prediction_validity=prediction_validity,
        before_after_result=before_after_result,
        verdict=verdict,
        reasons=tuple(reasons),
        evaluation_version=TAI_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadTreatmentQualification, payload,
        'qualification_id', 'qualification_sha256', 'taieval',
    )


__all__ = [
    'TAI_EVALUATION_VERSION',
    'TAI_SCHEMA_VERSION',
    'AcousticRole',
    'AsBuiltEvidenceState',
    'AsBuiltVerdict',
    'BeforeAfterResult',
    'CadParameterObservation',
    'CadTreatmentAsBuiltObservation',
    'CadTreatmentInspection',
    'CadTreatmentInstallSpec',
    'CadTreatmentQualification',
    'LabEvidenceClass',
    'ParameterVerdict',
    'TreatmentClass',
    'asbuilt_observation_binding',
    'build_asbuilt_observation',
    'build_inspection',
    'build_install_spec',
    'evaluate_treatment_asbuilt',
    'treatment_inspection_binding',
    'treatment_qualification_binding',
    'treatment_spec_binding',
]
