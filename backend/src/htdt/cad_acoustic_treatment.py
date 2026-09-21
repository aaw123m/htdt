from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_benchmark import AcousticMaterial
from .cad_repository import SceneRevision
from .cad_scene import Position3, Quaternion4
from .cad_system_variant import SystemVariant, materialize_system_variant
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .semantic_geometry import SemanticSurface


ACOUSTIC_TREATMENT_SCHEMA_VERSION = 2
ACOUSTIC_TREATMENT_AUTHORITY_VERSION = 'acoustic-treatment-2'
TREATMENT_EVIDENCE_AUTHORITY_VERSION = 'treatment-evidence-1'
TREATMENT_SURFACE_BINDING_EVALUATOR_ID = 'htdt.acoustic_treatment.semantic_surface_binding'
TREATMENT_SURFACE_BINDING_EVALUATOR_VERSION = '1'
TREATMENT_SURFACE_AUTHORITY_VERSION = 'r120-semantic-surface-host-1'

TreatmentType = Literal[
    'porous_absorber',
    'absorber_with_air_gap',
    'membrane_panel_absorber',
    'perforated_slotted_absorber',
    'bass_trap',
    'diffuser_scattering_element',
    'hybrid',
]
TreatmentLifecycle = Literal['proposed', 'installed']
TreatmentEvidenceBasis = Literal['measured', 'inferred', 'modelled']
CapabilityState = Literal['SUPPORTED', 'UNKNOWN']
PredictionReadiness = Literal['UNKNOWN']
SystemVariantRelation = Literal['proposal_baseline', 'design_source']
HostSurfaceSemanticClass = Literal['room_boundary', 'object_surface', 'unknown']
SurfaceBindingState = Literal[
    'exact',
    'unbound',
    'legacy_unverified',
    'scene_revision_missing',
    'wrong_scene_revision',
    'scene_authority_mismatch',
    'semantic_geometry_missing',
    'surface_removed',
    'surface_authority_mismatch',
    'stale_scene_revision',
    'stale_semantic_geometry',
]
SurfaceLifecycleState = Literal[
    'unavailable',
    'stable_same_authority',
    'stable_authority_changed',
    'removed',
]
HostSemanticPolicy = Literal['semantic_class_does_not_gate_placement_authority']

TreatmentSourceKind = Literal[
    'manufacturer',
    'measurement',
    'literature',
    'user_defined',
    'analytic_model',
    'inference',
]

# Source kinds that claim externally sourced data. They require an exact
# source_sha256 and resolve to a retained TreatmentEvidenceAuthority whose
# normalized subject must support the persisted values.
EXTERNAL_TREATMENT_SOURCE_KINDS = frozenset({'manufacturer', 'measurement', 'literature'})
# Source kinds that claim model-generated data. They resolve to a retained
# TreatmentEvidenceAuthority carrying an exact TreatmentModelBasis.
MODEL_TREATMENT_SOURCE_KINDS = frozenset({'analytic_model', 'inference'})

# Which provenance source kinds may honestly back each evidence basis. A
# 'measured' basis can never be claimed from a bare user_defined record, and a
# 'modelled' basis can never be claimed from measurement/manufacturer data.
EVIDENCE_BASIS_SOURCE_KINDS: dict[str, frozenset[str]] = {
    'measured': EXTERNAL_TREATMENT_SOURCE_KINDS,
    'inferred': frozenset(
        {'manufacturer', 'measurement', 'literature', 'analytic_model', 'inference'}
    ),
    'modelled': MODEL_TREATMENT_SOURCE_KINDS,
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


class TreatmentProvenance(BaseModel):
    """Versioned source claim bound to an exact resolvable evidence authority.

    The claim is only authoritative when `source_authority` resolves to a
    persisted `TreatmentEvidenceAuthority` whose retained source fields and
    normalized subject match the claimed values exactly.
    """

    model_config = ConfigDict(frozen=True)

    source_kind: TreatmentSourceKind
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    reference: str | None = Field(default=None, min_length=1)
    source_authority: ExactExternalAuthorityRef

    @model_validator(mode='after')
    def exact_source_claim(self) -> 'TreatmentProvenance':
        if (
            self.source_kind in EXTERNAL_TREATMENT_SOURCE_KINDS
            and self.source_sha256 is None
        ):
            raise ValueError(
                'manufacturer/measurement/literature provenance requires an '
                'exact source_sha256'
            )
        return self


class TreatmentDimensions(BaseModel):
    """Manufacturing/cut-list dimensions. Air gap is intentionally separate."""

    model_config = ConfigDict(frozen=True)

    width_m: float = Field(gt=0.0)
    height_m: float = Field(gt=0.0)
    thickness_m: float = Field(gt=0.0)

    @model_validator(mode='after')
    def finite_dimensions(self) -> 'TreatmentDimensions':
        if not all(isfinite(float(value)) for value in (self.width_m, self.height_m, self.thickness_m)):
            raise ValueError('treatment dimensions must be finite')
        return self


class TreatmentLayer(BaseModel):
    """Physical assembly layer; this is not a room base-construction material assignment."""

    model_config = ConfigDict(frozen=True)

    layer_id: str = Field(min_length=1)
    material_name: str = Field(min_length=1)
    thickness_m: float = Field(gt=0.0)
    density_kg_m3: float | None = Field(default=None, gt=0.0)
    airflow_resistivity_pa_s_m2: float | None = Field(default=None, gt=0.0)
    surface_density_kg_m2: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def finite_layer_values(self) -> 'TreatmentLayer':
        values = (
            self.thickness_m,
            self.density_kg_m3,
            self.airflow_resistivity_pa_s_m2,
            self.surface_density_kg_m2,
        )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('treatment layer values must be finite')
        return self


class TreatmentPhysicalParameters(BaseModel):
    """Explicit treatment-model inputs that must not be hidden in free-form metadata."""

    model_config = ConfigDict(frozen=True)

    bulk_density_kg_m3: float | None = Field(default=None, gt=0.0)
    airflow_resistivity_pa_s_m2: float | None = Field(default=None, gt=0.0)
    membrane_surface_density_kg_m2: float | None = Field(default=None, gt=0.0)
    perforation_open_area_ratio: float | None = Field(default=None, gt=0.0, lt=1.0)
    slot_open_area_ratio: float | None = Field(default=None, gt=0.0, lt=1.0)
    cavity_depth_m: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def finite_parameters(self) -> 'TreatmentPhysicalParameters':
        if any(
            value is not None and not isfinite(float(value))
            for value in self.model_dump(mode='python').values()
        ):
            raise ValueError('treatment physical parameters must be finite')
        return self


class TreatmentFrequencyBand(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_hz: float = Field(gt=0.0)
    max_hz: float = Field(gt=0.0)

    @model_validator(mode='after')
    def ordered_band(self) -> 'TreatmentFrequencyBand':
        if not isfinite(float(self.min_hz)) or not isfinite(float(self.max_hz)):
            raise ValueError('treatment frequency band must be finite')
        if self.max_hz <= self.min_hz:
            raise ValueError('treatment frequency band max_hz must exceed min_hz')
        return self


class TreatmentUncertainty(BaseModel):
    """Uncertainty remains explicit even when no quantitative model is available."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['quantified', 'unknown']
    value: float | None = Field(default=None, ge=0.0)
    unit: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def complete_uncertainty(self) -> 'TreatmentUncertainty':
        if self.kind == 'quantified':
            if self.value is None or self.unit is None:
                raise ValueError('quantified uncertainty requires value and unit')
            if not isfinite(float(self.value)):
                raise ValueError('uncertainty value must be finite')
        elif self.value is not None or self.unit is not None:
            raise ValueError('unknown uncertainty cannot carry a quantitative value/unit')
        return self


class TreatmentAcousticModelSubject(BaseModel):
    """Normalized acoustic-model values an evidence authority supports.

    The acoustic model's own provenance is deliberately excluded: the evidence
    authority carries the source identity, so binding it here would be circular.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    evidence_basis: TreatmentEvidenceBasis
    valid_frequency_band: TreatmentFrequencyBand
    uncertainty: TreatmentUncertainty
    material: AcousticMaterial


class TreatmentEvidenceSubject(BaseModel):
    """Exact normalized treatment data a persisted evidence authority supports.

    The subject binds the treatment id/version plus every production-significant
    normalized field group, so evidence retained for one treatment/version can
    never silently authorize another treatment's values.
    """

    model_config = ConfigDict(frozen=True)

    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    treatment_type: TreatmentType
    dimensions: TreatmentDimensions
    air_gap_m: float = Field(ge=0.0)
    layers: tuple[TreatmentLayer, ...]
    parameters: TreatmentPhysicalParameters = Field(
        default_factory=TreatmentPhysicalParameters
    )
    acoustic_model: TreatmentAcousticModelSubject | None = None

    @model_validator(mode='after')
    def finite_subject(self) -> 'TreatmentEvidenceSubject':
        if not isfinite(float(self.air_gap_m)):
            raise ValueError('treatment evidence subject air gap must be finite')
        return self


class TreatmentModelBasis(BaseModel):
    """Exact generative-model authority for analytic_model/inference evidence.

    Exact model id/version plus canonical parameters identify the conversion
    that produced the normalized subject; assumed vs derived quantities are
    explicit rather than implied.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    assumed_quantities: tuple[str, ...] = ()
    derived_quantities: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_model_basis(self) -> 'TreatmentModelBasis':
        try:
            _canonical(self.parameters)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                'treatment model basis parameters must be canonical JSON'
            ) from exc
        quantities = (*self.assumed_quantities, *self.derived_quantities)
        if any(not str(item).strip() for item in quantities):
            raise ValueError('treatment model basis quantities must be non-empty')
        overlap = set(self.assumed_quantities) & set(self.derived_quantities)
        if overlap:
            raise ValueError(
                'treatment model basis quantities cannot be both assumed and '
                f'derived: {sorted(overlap)}'
            )
        return self


class TreatmentEvidenceAuthority(BaseModel):
    """Immutable content-addressed evidence record behind a provenance claim.

    For manufacturer/measurement/literature data this retains the exact source
    identity (including the raw source SHA-256) and the normalized extraction
    it supports. For analytic_model/inference data it retains the exact model
    basis. For user_defined data it is the explicit immutable manual evidence
    authority rather than implied external provenance.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'treatment-evidence-1'
    ] = TREATMENT_EVIDENCE_AUTHORITY_VERSION
    evidence_id: str = Field(pattern=r'^treatment-evidence:[0-9a-f]{64}$')
    source_kind: TreatmentSourceKind
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    reference: str | None = Field(default=None, min_length=1)
    extraction_id: str = Field(min_length=1)
    extraction_version: str = Field(min_length=1)
    model_basis: TreatmentModelBasis | None = None
    subject: TreatmentEvidenceSubject
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evidence(self) -> 'TreatmentEvidenceAuthority':
        if self.source_kind in EXTERNAL_TREATMENT_SOURCE_KINDS:
            if self.source_sha256 is None:
                raise ValueError(
                    'manufacturer/measurement/literature treatment evidence '
                    'requires an exact source_sha256'
                )
            if self.model_basis is not None:
                raise ValueError(
                    'externally sourced treatment evidence cannot carry a model basis'
                )
        elif self.source_kind in MODEL_TREATMENT_SOURCE_KINDS:
            if self.model_basis is None:
                raise ValueError(
                    'analytic_model/inference treatment evidence requires an '
                    'exact model basis'
                )
        elif self.model_basis is not None:
            raise ValueError('user_defined treatment evidence cannot carry a model basis')
        expected = _digest(self.identity_payload())
        if self.evidence_sha256 != expected:
            raise ValueError('TreatmentEvidenceAuthority semantic hash mismatch')
        if self.evidence_id != f'treatment-evidence:{expected}':
            raise ValueError('TreatmentEvidenceAuthority id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'evidence_id', 'evidence_sha256'},
        )

    def subject_sha256(self) -> str:
        return _digest(self.subject.model_dump(mode='json'))

    def as_external_ref(self) -> ExactExternalAuthorityRef:
        return ExactExternalAuthorityRef(
            authority_id=self.evidence_id,
            authority_version=self.authority_version,
            semantic_hash_sha256=self.evidence_sha256,
        )

    def as_provenance(self) -> 'TreatmentProvenance':
        """Provenance claim exactly matching this retained evidence authority."""
        return TreatmentProvenance(
            source_kind=self.source_kind,
            source_id=self.source_id,
            source_version=self.source_version,
            source_sha256=self.source_sha256,
            reference=self.reference,
            source_authority=self.as_external_ref(),
        )


class TreatmentAcousticModel(BaseModel):
    """Treatment-level acoustic capability using the existing R110/R100 material split."""

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    evidence_basis: TreatmentEvidenceBasis
    valid_frequency_band: TreatmentFrequencyBand
    uncertainty: TreatmentUncertainty
    provenance: TreatmentProvenance
    material: AcousticMaterial

    @model_validator(mode='after')
    def has_declared_capability(self) -> 'TreatmentAcousticModel':
        if (
            self.material.wave_model == 'unsupported'
            and self.material.geometric_model == 'unsupported'
        ):
            raise ValueError(
                'unsupported treatment physics must be represented by no acoustic_model, not a fake model'
            )
        allowed = EVIDENCE_BASIS_SOURCE_KINDS[self.evidence_basis]
        if self.provenance.source_kind not in allowed:
            raise ValueError(
                f"evidence_basis '{self.evidence_basis}' cannot be claimed from "
                f"source_kind '{self.provenance.source_kind}'"
            )
        return self


class AcousticTreatmentDefinition(BaseModel):
    """Immutable/versioned first-class attached treatment definition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = ACOUSTIC_TREATMENT_SCHEMA_VERSION
    authority_version: Literal['acoustic-treatment-2'] = ACOUSTIC_TREATMENT_AUTHORITY_VERSION
    authority_role: Literal['attached_acoustic_treatment'] = 'attached_acoustic_treatment'
    definition_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    treatment_type: TreatmentType
    provenance: TreatmentProvenance
    dimensions: TreatmentDimensions
    air_gap_m: float = Field(default=0.0, ge=0.0)
    layers: tuple[TreatmentLayer, ...]
    parameters: TreatmentPhysicalParameters = Field(default_factory=TreatmentPhysicalParameters)
    acoustic_model: TreatmentAcousticModel | None = None
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_definition(self) -> 'AcousticTreatmentDefinition':
        if not isfinite(float(self.air_gap_m)):
            raise ValueError('air gap must be finite')
        layer_ids = [item.layer_id for item in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError('treatment layer ids must be unique')
        if self.treatment_type == 'absorber_with_air_gap' and self.air_gap_m <= 0.0:
            raise ValueError('absorber_with_air_gap requires a positive air gap')
        if self.definition_sha256 != _digest(self.identity_payload()):
            raise ValueError('AcousticTreatmentDefinition semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'authority_role': self.authority_role,
            'definition_id': self.definition_id,
            'version': self.version,
            'name': self.name,
            'treatment_type': self.treatment_type,
            'provenance': self.provenance.model_dump(mode='json'),
            'dimensions': self.dimensions.model_dump(mode='json'),
            'air_gap_m': self.air_gap_m,
            'layers': [item.model_dump(mode='json') for item in self.layers],
            'parameters': self.parameters.model_dump(mode='json'),
            'acoustic_model': (
                None if self.acoustic_model is None
                else self.acoustic_model.model_dump(mode='json')
            ),
        }


class TreatmentCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    width_m: float = Field(gt=0.0)
    height_m: float = Field(gt=0.0)
    host_surface_fraction: float | None = Field(default=None, gt=0.0, le=1.0)

    @model_validator(mode='after')
    def finite_coverage(self) -> 'TreatmentCoverage':
        if not all(isfinite(float(value)) for value in (self.width_m, self.height_m)):
            raise ValueError('treatment coverage must be finite')
        if self.host_surface_fraction is not None and not isfinite(float(self.host_surface_fraction)):
            raise ValueError('host surface coverage fraction must be finite')
        return self


class AcousticTreatmentPlacement(BaseModel):
    """Immutable treatment instance/placement; it never edits the Scene base material."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = ACOUSTIC_TREATMENT_SCHEMA_VERSION
    authority_version: Literal['acoustic-treatment-2'] = ACOUSTIC_TREATMENT_AUTHORITY_VERSION
    authority_role: Literal['attached_acoustic_treatment'] = 'attached_acoustic_treatment'
    instance_id: str = Field(min_length=1)
    placement_version: int = Field(ge=1)
    lifecycle: TreatmentLifecycle
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    system_variant_relation: SystemVariantRelation | None = None
    position: Position3
    orientation: Quaternion4 = Field(default_factory=Quaternion4)
    coverage: TreatmentCoverage
    host_surface_id: str | None = Field(default=None, min_length=1)
    host_surface_authority_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    previous_placement_version: int | None = Field(default=None, ge=1)
    previous_placement_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    placement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_placement(self) -> 'AcousticTreatmentPlacement':
        variant_fields = (
            self.system_variant_id,
            self.system_variant_sha256,
            self.system_variant_relation,
        )
        if any(value is None for value in variant_fields) and any(
            value is not None for value in variant_fields
        ):
            raise ValueError('SystemVariant id/hash/relation must be supplied together')
        surface_fields = (self.host_surface_id, self.host_surface_authority_sha256)
        if (surface_fields[0] is None) != (surface_fields[1] is None):
            raise ValueError('host surface id/hash must be supplied together')
        previous_fields = (self.previous_placement_version, self.previous_placement_sha256)
        if (previous_fields[0] is None) != (previous_fields[1] is None):
            raise ValueError('previous placement version/hash must be supplied together')
        if self.placement_version == 1 and self.previous_placement_version is not None:
            raise ValueError('first treatment placement version cannot have previous lineage')
        if self.placement_version > 1:
            if self.previous_placement_version != self.placement_version - 1:
                raise ValueError('treatment placement lineage must reference the immediately prior version')
        if self.lifecycle == 'installed' and self.placement_version < 2:
            raise ValueError('installed treatment placement requires prior proposed lineage')
        if self.lifecycle == 'proposed' and self.system_variant_relation == 'design_source':
            raise ValueError('proposed treatment placement must use proposal_baseline relation')
        if self.placement_sha256 != _digest(self.identity_payload()):
            raise ValueError('AcousticTreatmentPlacement semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'authority_role': self.authority_role,
            'instance_id': self.instance_id,
            'placement_version': self.placement_version,
            'lifecycle': self.lifecycle,
            'definition_id': self.definition_id,
            'definition_version': self.definition_version,
            'definition_sha256': self.definition_sha256,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'system_variant_relation': self.system_variant_relation,
            'position': self.position.model_dump(mode='json'),
            'orientation': self.orientation.model_dump(mode='json'),
            'coverage': self.coverage.model_dump(mode='json'),
            'host_surface_id': self.host_surface_id,
            'host_surface_authority_sha256': self.host_surface_authority_sha256,
            'previous_placement_version': self.previous_placement_version,
            'previous_placement_sha256': self.previous_placement_sha256,
        }


class TreatmentPredictionCapability(BaseModel):
    """Physics availability is separate from placement and solver/compiler readiness."""

    model_config = ConfigDict(frozen=True)

    definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_basis: TreatmentEvidenceBasis | None = None
    valid_frequency_band: TreatmentFrequencyBand | None = None
    uncertainty: TreatmentUncertainty
    wave_material_capability: CapabilityState
    geometric_material_capability: CapabilityState
    solver_prediction_readiness: PredictionReadiness = 'UNKNOWN'
    reasons: tuple[str, ...]


class TreatmentSurfaceBindingEvaluation(BaseModel):
    """Deterministic evaluation of one placement against exact R120 surface authority."""

    model_config = ConfigDict(frozen=True)

    evaluator_id: Literal['htdt.acoustic_treatment.semantic_surface_binding'] = (
        TREATMENT_SURFACE_BINDING_EVALUATOR_ID
    )
    evaluator_version: Literal['1'] = TREATMENT_SURFACE_BINDING_EVALUATOR_VERSION
    placement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bound_scene_revision_id: str
    bound_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluated_scene_revision_id: str | None
    evaluated_scene_content_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    binding_state: SurfaceBindingState
    bound_authority_valid: bool
    placement_authority_valid: bool
    host_surface_id: str | None
    expected_host_surface_authority_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    bound_semantic_geometry_id: str | None = Field(
        default=None,
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$',
    )
    bound_semantic_geometry_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    evaluated_semantic_geometry_id: str | None = Field(
        default=None,
        pattern=r'^semantic-acoustic-geometry:[0-9a-f]{64}$',
    )
    evaluated_semantic_geometry_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    actual_host_surface_authority_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    actual_host_surface_semantic_class: HostSurfaceSemanticClass | None = None
    host_surface_lifecycle: SurfaceLifecycleState
    host_surface_semantics_known: bool
    host_semantic_policy: HostSemanticPolicy = 'semantic_class_does_not_gate_placement_authority'
    solver_prediction_readiness: PredictionReadiness = 'UNKNOWN'
    reasons: tuple[str, ...]
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation_identity(self) -> 'TreatmentSurfaceBindingEvaluation':
        if self.evaluation_sha256 != _digest(self.identity_payload()):
            raise ValueError('TreatmentSurfaceBindingEvaluation semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'evaluation_sha256'})


def semantic_surface_host_authority_sha256(surface: SemanticSurface) -> str:
    """Hash only the stable SemanticSurface authority, not the whole geometry snapshot."""

    return _digest(
        {
            'authority_version': TREATMENT_SURFACE_AUTHORITY_VERSION,
            'surface': surface.model_dump(mode='json'),
        }
    )


def _surface_in_revision(
    revision: SceneRevision,
    surface_id: str,
) -> SemanticSurface | None:
    geometry = revision.document.r120_semantic_geometry
    if geometry is None:
        return None
    matches = [surface for surface in geometry.surfaces if surface.surface_id == surface_id]
    if len(matches) > 1:
        raise ValueError('R120 semantic geometry contains duplicate SemanticSurface ids')
    return None if not matches else matches[0]


def _resolve_host_surface_binding(
    revision: SceneRevision,
    host_surface_id: str | None,
    host_surface_authority_sha256: str | None,
) -> tuple[str | None, str | None]:
    if host_surface_id is None:
        if host_surface_authority_sha256 is not None:
            raise ValueError('host surface id/hash must be supplied together')
        return None, None

    geometry = revision.document.r120_semantic_geometry
    if geometry is None:
        if host_surface_authority_sha256 is None:
            raise ValueError(
                'legacy host surface binding without R120 semantic geometry requires an explicit authority hash'
            )
        return host_surface_id, host_surface_authority_sha256

    surface = _surface_in_revision(revision, host_surface_id)
    if surface is None:
        raise ValueError('host SemanticSurface does not exist in the exact SceneRevision geometry')
    actual_sha256 = semantic_surface_host_authority_sha256(surface)
    if (
        host_surface_authority_sha256 is not None
        and host_surface_authority_sha256 != actual_sha256
    ):
        raise ValueError('host SemanticSurface authority hash mismatch')
    return host_surface_id, actual_sha256


def _make_surface_binding_evaluation(**payload: Any) -> TreatmentSurfaceBindingEvaluation:
    identity = {
        'evaluator_id': TREATMENT_SURFACE_BINDING_EVALUATOR_ID,
        'evaluator_version': TREATMENT_SURFACE_BINDING_EVALUATOR_VERSION,
        **payload,
    }
    return TreatmentSurfaceBindingEvaluation(
        **identity,
        evaluation_sha256=_digest(identity),
    )


def evaluate_treatment_surface_binding(
    placement: AcousticTreatmentPlacement,
    *,
    bound_revision: SceneRevision | None,
    evaluated_revision: SceneRevision | None,
    evaluated_revision_id: str | None,
) -> TreatmentSurfaceBindingEvaluation:
    """Evaluate exact placement binding and surface lifecycle without promoting prediction capability."""

    common: dict[str, Any] = {
        'placement_sha256': placement.placement_sha256,
        'bound_scene_revision_id': placement.scene_revision_id,
        'bound_scene_content_hash': placement.scene_content_hash,
        'evaluated_scene_revision_id': evaluated_revision_id,
        'evaluated_scene_content_hash': (
            None if evaluated_revision is None else evaluated_revision.content_hash
        ),
        'host_surface_id': placement.host_surface_id,
        'expected_host_surface_authority_sha256': placement.host_surface_authority_sha256,
        'bound_semantic_geometry_id': None,
        'bound_semantic_geometry_sha256': None,
        'evaluated_semantic_geometry_id': None,
        'evaluated_semantic_geometry_sha256': None,
        'actual_host_surface_authority_sha256': None,
        'actual_host_surface_semantic_class': None,
        'host_surface_lifecycle': 'unavailable',
        'host_surface_semantics_known': False,
        'host_semantic_policy': 'semantic_class_does_not_gate_placement_authority',
        'solver_prediction_readiness': 'UNKNOWN',
    }

    if placement.host_surface_id is None:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='unbound',
            bound_authority_valid=True,
            placement_authority_valid=True,
            reasons=(
                'placement has no host surface binding to evaluate',
                'unbound placement does not establish solver prediction readiness',
            ),
        )

    if bound_revision is None:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='scene_revision_missing',
            bound_authority_valid=False,
            placement_authority_valid=False,
            reasons=('bound SceneRevision does not exist',),
        )

    if (
        bound_revision.revision_id != placement.scene_revision_id
        or bound_revision.document_id != placement.document_id
        or bound_revision.content_hash != placement.scene_content_hash
    ):
        return _make_surface_binding_evaluation(
            **common,
            binding_state='scene_authority_mismatch',
            bound_authority_valid=False,
            placement_authority_valid=False,
            reasons=('placement does not match the exact bound SceneRevision authority',),
        )

    bound_geometry = bound_revision.document.r120_semantic_geometry
    if bound_geometry is None:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='legacy_unverified',
            bound_authority_valid=False,
            placement_authority_valid=False,
            reasons=(
                'bound SceneRevision has no R120 semantic geometry; legacy host id/hash cannot be promoted to exact authority',
            ),
        )

    common['bound_semantic_geometry_id'] = bound_geometry.geometry_id
    common['bound_semantic_geometry_sha256'] = bound_geometry.semantic_hash_sha256
    bound_surface = _surface_in_revision(bound_revision, placement.host_surface_id)
    if bound_surface is None:
        common['host_surface_lifecycle'] = 'removed'
        return _make_surface_binding_evaluation(
            **common,
            binding_state='surface_removed',
            bound_authority_valid=False,
            placement_authority_valid=False,
            reasons=('bound SemanticSurface is absent from the exact bound geometry',),
        )

    bound_surface_sha256 = semantic_surface_host_authority_sha256(bound_surface)
    common['actual_host_surface_authority_sha256'] = bound_surface_sha256
    common['actual_host_surface_semantic_class'] = bound_surface.semantic_class
    common['host_surface_semantics_known'] = bound_surface.semantic_class != 'unknown'
    common['host_surface_lifecycle'] = (
        'stable_same_authority'
        if bound_surface_sha256 == placement.host_surface_authority_sha256
        else 'stable_authority_changed'
    )
    if bound_surface_sha256 != placement.host_surface_authority_sha256:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='surface_authority_mismatch',
            bound_authority_valid=False,
            placement_authority_valid=False,
            reasons=('bound SemanticSurface id exists but its exact authority hash does not match',),
        )

    if evaluated_revision is None:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='scene_revision_missing',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('evaluated SceneRevision does not exist',),
        )

    if evaluated_revision.document_id != placement.document_id:
        if evaluated_revision.document.r120_semantic_geometry is not None:
            common['evaluated_semantic_geometry_id'] = (
                evaluated_revision.document.r120_semantic_geometry.geometry_id
            )
            common['evaluated_semantic_geometry_sha256'] = (
                evaluated_revision.document.r120_semantic_geometry.semantic_hash_sha256
            )
        return _make_surface_binding_evaluation(
            **common,
            binding_state='wrong_scene_revision',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('evaluated SceneRevision belongs to another SceneDocument',),
        )

    evaluated_geometry = evaluated_revision.document.r120_semantic_geometry
    if evaluated_geometry is not None:
        common['evaluated_semantic_geometry_id'] = evaluated_geometry.geometry_id
        common['evaluated_semantic_geometry_sha256'] = evaluated_geometry.semantic_hash_sha256
        evaluated_surface = _surface_in_revision(evaluated_revision, placement.host_surface_id)
        if evaluated_surface is None:
            common['host_surface_lifecycle'] = 'removed'
            common['actual_host_surface_authority_sha256'] = None
            common['actual_host_surface_semantic_class'] = None
            common['host_surface_semantics_known'] = False
        else:
            evaluated_surface_sha256 = semantic_surface_host_authority_sha256(evaluated_surface)
            common['actual_host_surface_authority_sha256'] = evaluated_surface_sha256
            common['actual_host_surface_semantic_class'] = evaluated_surface.semantic_class
            common['host_surface_semantics_known'] = evaluated_surface.semantic_class != 'unknown'
            common['host_surface_lifecycle'] = (
                'stable_same_authority'
                if evaluated_surface_sha256 == placement.host_surface_authority_sha256
                else 'stable_authority_changed'
            )
    else:
        common['host_surface_lifecycle'] = 'unavailable'
        common['actual_host_surface_authority_sha256'] = None
        common['actual_host_surface_semantic_class'] = None
        common['host_surface_semantics_known'] = False

    if evaluated_revision.revision_id != placement.scene_revision_id:
        state: SurfaceBindingState = 'stale_scene_revision'
        if (
            evaluated_geometry is not None
            and evaluated_geometry.semantic_hash_sha256 != bound_geometry.semantic_hash_sha256
        ):
            state = 'stale_semantic_geometry'
        reasons = [
            'placement remains bound to its immutable original SceneRevision and is stale for the evaluated revision'
        ]
        if common['host_surface_lifecycle'] == 'stable_same_authority':
            reasons.append(
                'stable SemanticSurface id and authority are traceable across revisions, but exact placement binding is still stale'
            )
        elif common['host_surface_lifecycle'] == 'stable_authority_changed':
            reasons.append(
                'stable SemanticSurface id is traceable, but its surface authority changed'
            )
        elif common['host_surface_lifecycle'] == 'removed':
            reasons.append('bound SemanticSurface was removed from the evaluated geometry')
        return _make_surface_binding_evaluation(
            **common,
            binding_state=state,
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=tuple(reasons),
        )

    if evaluated_revision.content_hash != placement.scene_content_hash:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='scene_authority_mismatch',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('evaluated SceneRevision content hash does not match the placement authority',),
        )

    if evaluated_geometry is None:
        return _make_surface_binding_evaluation(
            **common,
            binding_state='semantic_geometry_missing',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('exact SceneRevision no longer resolves an R120 semantic geometry authority',),
        )

    if common['host_surface_lifecycle'] == 'removed':
        return _make_surface_binding_evaluation(
            **common,
            binding_state='surface_removed',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('bound SemanticSurface is absent from the evaluated exact geometry',),
        )

    if common['host_surface_lifecycle'] == 'stable_authority_changed':
        return _make_surface_binding_evaluation(
            **common,
            binding_state='surface_authority_mismatch',
            bound_authority_valid=True,
            placement_authority_valid=False,
            reasons=('SemanticSurface id matches but exact host authority hash does not',),
        )

    reasons = [
        'placement, SceneRevision, R120 semantic geometry, SemanticSurface id, and host surface hash match exactly'
    ]
    if not common['host_surface_semantics_known']:
        reasons.append(
            'SemanticSurface class is unknown; placement authority is valid but prediction capability is not implied'
        )
    else:
        reasons.append(
            'room_boundary/object_surface classification is placement metadata only; solver prediction readiness remains UNKNOWN'
        )
    return _make_surface_binding_evaluation(
        **common,
        binding_state='exact',
        bound_authority_valid=True,
        placement_authority_valid=True,
        reasons=tuple(reasons),
    )


def build_treatment_evidence_authority(
    *,
    source_kind: TreatmentSourceKind,
    source_id: str,
    source_version: str,
    source_sha256: str | None = None,
    reference: str | None = None,
    extraction_id: str,
    extraction_version: str,
    model_basis: TreatmentModelBasis | None = None,
    subject: TreatmentEvidenceSubject,
) -> TreatmentEvidenceAuthority:
    """Build a deterministic content-addressed treatment evidence authority.

    The returned authority must be persisted through
    `CadAcousticTreatmentRepository.save_evidence` before a definition whose
    provenance references it can be saved.
    """

    payload = {
        'authority_version': TREATMENT_EVIDENCE_AUTHORITY_VERSION,
        'source_kind': source_kind,
        'source_id': source_id,
        'source_version': source_version,
        'source_sha256': source_sha256,
        'reference': reference,
        'extraction_id': extraction_id,
        'extraction_version': extraction_version,
        'model_basis': None if model_basis is None else model_basis.model_dump(mode='json'),
        'subject': subject.model_dump(mode='json'),
    }
    digest = _digest(payload)
    return TreatmentEvidenceAuthority(
        evidence_id=f'treatment-evidence:{digest}',
        evidence_sha256=digest,
        **payload,
    )


def acoustic_model_evidence_subject(
    model: TreatmentAcousticModel,
) -> TreatmentAcousticModelSubject:
    """Normalized acoustic-model subject (model provenance excluded)."""

    return TreatmentAcousticModelSubject(
        model_id=model.model_id,
        model_version=model.model_version,
        evidence_basis=model.evidence_basis,
        valid_frequency_band=model.valid_frequency_band,
        uncertainty=model.uncertainty,
        material=model.material,
    )


def definition_evidence_subject(
    definition: AcousticTreatmentDefinition,
) -> TreatmentEvidenceSubject:
    """Exact normalized subject a persisted evidence authority must match.

    Covers every production-significant normalized field group: physical
    dimensions/air gap, layer stack, explicit physical parameters, and the
    normalized acoustic model (material, band, uncertainty, evidence basis)
    when one is attached.
    """

    model = definition.acoustic_model
    return TreatmentEvidenceSubject(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        treatment_type=definition.treatment_type,
        dimensions=definition.dimensions,
        air_gap_m=definition.air_gap_m,
        layers=definition.layers,
        parameters=definition.parameters,
        acoustic_model=(
            None
            if model is None
            else acoustic_model_evidence_subject(model)
        ),
    )


def build_acoustic_treatment_definition(
    *,
    definition_id: str,
    version: str,
    name: str,
    treatment_type: TreatmentType,
    provenance: TreatmentProvenance,
    dimensions: TreatmentDimensions,
    air_gap_m: float = 0.0,
    layers: Sequence[TreatmentLayer] = (),
    parameters: TreatmentPhysicalParameters | None = None,
    acoustic_model: TreatmentAcousticModel | None = None,
) -> AcousticTreatmentDefinition:
    """Build an immutable definition with a deterministic semantic identity."""

    air_gap_m = float(air_gap_m)
    identity = {
        'schema_version': ACOUSTIC_TREATMENT_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_TREATMENT_AUTHORITY_VERSION,
        'authority_role': 'attached_acoustic_treatment',
        'definition_id': definition_id,
        'version': version,
        'name': name,
        'treatment_type': treatment_type,
        'provenance': provenance.model_dump(mode='json'),
        'dimensions': dimensions.model_dump(mode='json'),
        'air_gap_m': air_gap_m,
        'layers': [item.model_dump(mode='json') for item in layers],
        'parameters': (parameters or TreatmentPhysicalParameters()).model_dump(mode='json'),
        'acoustic_model': None if acoustic_model is None else acoustic_model.model_dump(mode='json'),
    }
    return AcousticTreatmentDefinition(
        definition_id=definition_id,
        version=version,
        name=name,
        treatment_type=treatment_type,
        provenance=provenance,
        dimensions=dimensions,
        air_gap_m=air_gap_m,
        layers=tuple(layers),
        parameters=parameters or TreatmentPhysicalParameters(),
        acoustic_model=acoustic_model,
        definition_sha256=_digest(identity),
    )


def evaluate_treatment_prediction_capability(
    definition: AcousticTreatmentDefinition,
) -> TreatmentPredictionCapability:
    """Fail closed: model data may be known while solver readiness remains UNKNOWN."""

    model = definition.acoustic_model
    if model is None:
        return TreatmentPredictionCapability(
            definition_sha256=definition.definition_sha256,
            uncertainty=TreatmentUncertainty(
                kind='unknown',
                note='no treatment-level acoustic model authority is attached',
            ),
            wave_material_capability='UNKNOWN',
            geometric_material_capability='UNKNOWN',
            reasons=(
                'unsupported treatment physics is UNKNOWN; placement alone establishes no prediction capability',
                'no treatment solver/compiler binding exists in this foundation slice',
            ),
        )

    wave = 'SUPPORTED' if model.material.wave_model != 'unsupported' else 'UNKNOWN'
    geometric = 'SUPPORTED' if model.material.geometric_model != 'unsupported' else 'UNKNOWN'
    reasons = [
        'wave material capability is explicit in the reused AcousticMaterial authority'
        if wave == 'SUPPORTED'
        else 'wave material capability is not available; no impedance is inferred from absorption',
        'geometric material capability is explicit in the reused AcousticMaterial authority'
        if geometric == 'SUPPORTED'
        else 'geometric absorption/scattering capability is not available',
        'treatment solver/compiler binding is deferred, so prediction readiness remains UNKNOWN',
    ]
    return TreatmentPredictionCapability(
        definition_sha256=definition.definition_sha256,
        evidence_basis=model.evidence_basis,
        valid_frequency_band=model.valid_frequency_band,
        uncertainty=model.uncertainty,
        wave_material_capability=wave,
        geometric_material_capability=geometric,
        reasons=tuple(reasons),
    )


def build_treatment_placement(
    *,
    definition: AcousticTreatmentDefinition,
    revision: SceneRevision,
    instance_id: str,
    lifecycle: Literal['proposed'] = 'proposed',
    position: Position3,
    coverage: TreatmentCoverage,
    orientation: Quaternion4 | None = None,
    system_variant: SystemVariant | None = None,
    host_surface_id: str | None = None,
    host_surface_authority_sha256: str | None = None,
) -> AcousticTreatmentPlacement:
    """Create the first immutable placement version against an exact scene/variant."""

    if lifecycle != 'proposed':
        raise ValueError('first treatment placement version must be proposed')
    relation: SystemVariantRelation | None = None
    if system_variant is not None:
        if (
            system_variant.document_id != revision.document_id
            or system_variant.baseline_revision_id != revision.revision_id
            or system_variant.baseline_content_hash != revision.content_hash
        ):
            raise ValueError('proposed treatment SystemVariant baseline authority mismatch')
        materialize_system_variant(revision, system_variant)
        relation = 'proposal_baseline'

    host_surface_id, host_surface_authority_sha256 = _resolve_host_surface_binding(
        revision,
        host_surface_id,
        host_surface_authority_sha256,
    )
    identity = {
        'schema_version': ACOUSTIC_TREATMENT_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_TREATMENT_AUTHORITY_VERSION,
        'authority_role': 'attached_acoustic_treatment',
        'instance_id': instance_id,
        'placement_version': 1,
        'lifecycle': lifecycle,
        'definition_id': definition.definition_id,
        'definition_version': definition.version,
        'definition_sha256': definition.definition_sha256,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'system_variant_id': None if system_variant is None else system_variant.variant_id,
        'system_variant_sha256': None if system_variant is None else system_variant.variant_sha256,
        'system_variant_relation': relation,
        'position': position.model_dump(mode='json'),
        'orientation': (orientation or Quaternion4()).model_dump(mode='json'),
        'coverage': coverage.model_dump(mode='json'),
        'host_surface_id': host_surface_id,
        'host_surface_authority_sha256': host_surface_authority_sha256,
        'previous_placement_version': None,
        'previous_placement_sha256': None,
    }
    return AcousticTreatmentPlacement(
        instance_id=instance_id,
        placement_version=1,
        lifecycle=lifecycle,
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_sha256=definition.definition_sha256,
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if system_variant is None else system_variant.variant_id,
        system_variant_sha256=None if system_variant is None else system_variant.variant_sha256,
        system_variant_relation=relation,
        position=position,
        orientation=orientation or Quaternion4(),
        coverage=coverage,
        host_surface_id=host_surface_id,
        host_surface_authority_sha256=host_surface_authority_sha256,
        placement_sha256=_digest(identity),
    )


def revise_treatment_placement(
    previous: AcousticTreatmentPlacement,
    *,
    revision: SceneRevision,
    lifecycle: TreatmentLifecycle | None = None,
    position: Position3 | None = None,
    coverage: TreatmentCoverage | None = None,
    orientation: Quaternion4 | None = None,
    system_variant: SystemVariant | None = None,
    host_surface_id: str | None = None,
    host_surface_authority_sha256: str | None = None,
) -> AcousticTreatmentPlacement:
    """Create the next immutable placement version, preserving exact lineage."""

    if previous.lifecycle == 'installed':
        raise ValueError('installed treatment placement is terminal and cannot be revised')
    next_lifecycle = previous.lifecycle if lifecycle is None else lifecycle
    if next_lifecycle not in {'proposed', 'installed'}:
        raise ValueError('unsupported treatment lifecycle transition')
    if revision.document_id != previous.document_id:
        raise ValueError('treatment placement lineage cannot move to another document')

    relation: SystemVariantRelation | None = None
    if system_variant is not None:
        if system_variant.document_id != revision.document_id:
            raise ValueError('treatment placement SystemVariant belongs to another document')
        if next_lifecycle == 'proposed':
            if (
                system_variant.baseline_revision_id != revision.revision_id
                or system_variant.baseline_content_hash != revision.content_hash
            ):
                raise ValueError('proposed treatment SystemVariant baseline authority mismatch')
            materialize_system_variant(revision, system_variant)
            relation = 'proposal_baseline'
        else:
            relation = 'design_source'
    elif next_lifecycle == 'proposed' and previous.system_variant_id is not None:
        raise ValueError('revised proposed placement must receive its bound SystemVariant authority')

    if host_surface_id is None and host_surface_authority_sha256 is None:
        next_surface_id = previous.host_surface_id
        requested_surface_hash = (
            previous.host_surface_authority_sha256
            if revision.document.r120_semantic_geometry is None
            else None
        )
    else:
        next_surface_id = host_surface_id
        requested_surface_hash = host_surface_authority_sha256

    next_surface_id, next_surface_hash = _resolve_host_surface_binding(
        revision,
        next_surface_id,
        requested_surface_hash,
    )
    next_orientation = previous.orientation if orientation is None else orientation
    next_position = previous.position if position is None else position
    next_coverage = previous.coverage if coverage is None else coverage
    next_version = previous.placement_version + 1

    identity = {
        'schema_version': ACOUSTIC_TREATMENT_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_TREATMENT_AUTHORITY_VERSION,
        'authority_role': 'attached_acoustic_treatment',
        'instance_id': previous.instance_id,
        'placement_version': next_version,
        'lifecycle': next_lifecycle,
        'definition_id': previous.definition_id,
        'definition_version': previous.definition_version,
        'definition_sha256': previous.definition_sha256,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'system_variant_id': None if system_variant is None else system_variant.variant_id,
        'system_variant_sha256': None if system_variant is None else system_variant.variant_sha256,
        'system_variant_relation': relation,
        'position': next_position.model_dump(mode='json'),
        'orientation': next_orientation.model_dump(mode='json'),
        'coverage': next_coverage.model_dump(mode='json'),
        'host_surface_id': next_surface_id,
        'host_surface_authority_sha256': next_surface_hash,
        'previous_placement_version': previous.placement_version,
        'previous_placement_sha256': previous.placement_sha256,
    }
    return AcousticTreatmentPlacement(
        instance_id=previous.instance_id,
        placement_version=next_version,
        lifecycle=next_lifecycle,
        definition_id=previous.definition_id,
        definition_version=previous.definition_version,
        definition_sha256=previous.definition_sha256,
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None if system_variant is None else system_variant.variant_id,
        system_variant_sha256=None if system_variant is None else system_variant.variant_sha256,
        system_variant_relation=relation,
        position=next_position,
        orientation=next_orientation,
        coverage=next_coverage,
        host_surface_id=next_surface_id,
        host_surface_authority_sha256=next_surface_hash,
        previous_placement_version=previous.placement_version,
        previous_placement_sha256=previous.placement_sha256,
        placement_sha256=_digest(identity),
    )
