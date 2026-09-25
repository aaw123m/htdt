from __future__ import annotations

from hashlib import sha256
import json
from math import acos, atan2, degrees, isfinite, sqrt
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import Point

from .cad_orientation_constraints import (
    entity_collision_geometry_authority,
    entity_horizontal_footprint,
)
from .cad_repository import SceneRevision
from .cad_scene import (
    Direction3,
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    quaternion_to_matrix3,
    scene_content_hash,
)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .r120_geometry_compiler import ExactExternalAuthorityRef


VIDEO_GEOMETRY_SCHEMA_VERSION = 1
VIDEO_GEOMETRY_AUTHORITY_VERSION = 'video-geometry-1'
PROJECTOR_SPEC_SCHEMA_VERSION = 1
PROJECTOR_SPEC_AUTHORITY_VERSION = 'projector-spec-1'
PROJECTOR_SPEC_EVIDENCE_SCHEMA_VERSION = 1
PROJECTOR_SPEC_EVIDENCE_AUTHORITY_VERSION = 'projector-spec-evidence-1'

EvaluationStatus = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']
ProjectorSpecSourceKind = Literal['manufacturer', 'user_defined']
ProjectorSpecEvidenceKind = Literal[
    'manufacturer_document',
    'external_authority',
    'manual_record',
]
ProjectorSpecOpticalField = Literal[
    'horizontal_lens_shift',
    'lens_reference_offset_m',
    'optical_axis_local',
    'optical_zoom_ratio',
    'supported_aspect_ratios',
    'throw_ratio_max',
    'throw_ratio_min',
    'vertical_lens_shift',
]
# The complete optical datum set a specification claims. Evidence must attest
# every one of these fields exactly once (explicit nulls included) so a
# specification re-derives entirely from its bound evidence.
PROJECTOR_SPEC_EVIDENCED_FIELDS: tuple[ProjectorSpecOpticalField, ...] = (
    'horizontal_lens_shift',
    'lens_reference_offset_m',
    'optical_axis_local',
    'optical_zoom_ratio',
    'supported_aspect_ratios',
    'throw_ratio_max',
    'throw_ratio_min',
    'vertical_lens_shift',
)

_EPS = 1e-9


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


def _finite(value: float) -> float:
    value = float(value)
    if not isfinite(value):
        raise ValueError('value must be finite')
    return value


def _v(position: Position3) -> tuple[float, float, float]:
    return (float(position.x_m), float(position.y_m), float(position.z_m))


def _add(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(left[i] + right[i] for i in range(3))


def _sub(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(left[i] - right[i] for i in range(3))


def _scale(
    vector: tuple[float, float, float],
    scalar: float,
) -> tuple[float, float, float]:
    return tuple(value * scalar for value in vector)


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return sum(left[i] * right[i] for i in range(3))


def _norm(vector: tuple[float, float, float]) -> float:
    return sqrt(_dot(vector, vector))


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = _norm(vector)
    if length <= _EPS:
        raise ValueError('zero-length vector is not a valid geometry direction')
    return tuple(value / length for value in vector)


def _position(vector: tuple[float, float, float]) -> Position3:
    return Position3(x_m=vector[0], y_m=vector[1], z_m=vector[2])


def _world_direction(
    entity: SceneEntity,
    local: Direction3,
) -> tuple[float, float, float]:
    matrix = quaternion_to_matrix3(entity.orientation)
    source = (float(local.x), float(local.y), float(local.z))
    return _unit(tuple(
        sum(matrix[row][column] * source[column] for column in range(3))
        for row in range(3)
    ))


def _world_offset(
    entity: SceneEntity,
    offset: Offset3,
) -> tuple[float, float, float]:
    matrix = quaternion_to_matrix3(entity.orientation)
    local = (float(offset.x_m), float(offset.y_m), float(offset.z_m))
    rotated = tuple(
        sum(matrix[row][column] * local[column] for column in range(3))
        for row in range(3)
    )
    return _add(_v(entity.position), rotated)


def _angle_between_deg(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    dot = max(-1.0, min(1.0, _dot(_unit(left), _unit(right))))
    return degrees(acos(dot))


def _range_status(value: float, limits: 'AngleRange | None') -> EvaluationStatus:
    if limits is None:
        return 'UNKNOWN'
    return 'PASS' if limits.minimum_deg <= value <= limits.maximum_deg else 'FAIL'


def _combine_status(statuses: Sequence[EvaluationStatus]) -> EvaluationStatus:
    applicable = [status for status in statuses if status != 'NOT_APPLICABLE']
    if any(status == 'FAIL' for status in applicable):
        return 'FAIL'
    if any(status == 'UNKNOWN' for status in applicable):
        return 'UNKNOWN'
    if any(status == 'PASS' for status in applicable):
        return 'PASS'
    return 'NOT_APPLICABLE'


class AspectRatio(BaseModel):
    model_config = ConfigDict(frozen=True)

    width_units: int = Field(gt=0)
    height_units: int = Field(gt=0)
    label: str | None = Field(default=None, min_length=1)

    @property
    def value(self) -> float:
        return float(self.width_units) / float(self.height_units)


class LensShiftRange(BaseModel):
    """Image-center shift as a fraction of the full image dimension.

    Zero means the optical-axis intersection is at image center. Positive horizontal
    values move image center toward screen +X; positive vertical values move it toward
    screen +Z. Manufacturer percentages must be converted explicitly into this
    canonical convention by the importer/user.
    """

    model_config = ConfigDict(frozen=True)

    minimum_fraction: float
    maximum_fraction: float

    @field_validator('minimum_fraction', 'maximum_fraction')
    @classmethod
    def finite_fraction(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def valid_range(self) -> 'LensShiftRange':
        if self.minimum_fraction > self.maximum_fraction:
            raise ValueError('lens shift minimum must not exceed maximum')
        return self

    def contains(self, value: float) -> bool:
        return self.minimum_fraction <= value <= self.maximum_fraction


class ProjectorSpecEvidenceRef(BaseModel):
    """Typed reference to the exact persisted evidence authority.

    The reference pins both the evidence kind and the evidence record's
    self-hash, so a specification cannot silently re-point at a different
    evidence record — resolution is content-addressed and fails closed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evidence_kind: ProjectorSpecEvidenceKind
    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ProjectorSpecificationProvenance(BaseModel):
    """Declared source identity plus the typed evidence ref behind it.

    ``source_kind`` selects the admissible evidence kind: manufacturer data
    must resolve to a retained manufacturer document or a typed immutable
    external authority (never a manual record), while user-defined data must
    resolve to an explicit manual evidence record. For manufacturer data an
    exact ``source_sha256`` of the source document/data is required.
    """

    model_config = ConfigDict(frozen=True)

    source_kind: ProjectorSpecSourceKind
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    evidence: ProjectorSpecEvidenceRef

    @model_validator(mode='after')
    def valid_evidence_binding(self) -> 'ProjectorSpecificationProvenance':
        if self.source_kind == 'manufacturer':
            if self.source_sha256 is None:
                raise ValueError(
                    'manufacturer projector data requires an exact source_sha256'
                )
            if self.evidence.evidence_kind == 'manual_record':
                raise ValueError(
                    'manufacturer projector data must not use manual_record '
                    'evidence; requires manufacturer_document or '
                    'external_authority evidence'
                )
        elif self.evidence.evidence_kind != 'manual_record':
            raise ValueError(
                'user_defined projector data requires manual_record evidence'
            )
        return self


class ProjectorSpecFieldAssertion(BaseModel):
    """One optical datum attested by evidence: canonical value plus locator.

    ``value`` is normalized to canonical JSON at validation time so equality
    and hashing never depend on input container/number spellings. ``locator``
    is the exact position inside the bound evidence — a page/table/row or a
    JSON-pointer style path for extracted data, or the citation/basis note
    for manual entries.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    field: ProjectorSpecOpticalField
    value: Any
    locator: str = Field(min_length=1)

    @field_validator('value')
    @classmethod
    def canonical_json_value(cls, value: Any) -> Any:
        try:
            return json.loads(_canonical(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                'evidence field assertion value must be canonical JSON'
            ) from exc


class ProjectorSpecificationEvidence(BaseModel):
    """Typed immutable evidence authority behind a ProjectorSpecification.

    One record binds the exact source identity (manufacturer/model/document
    metadata) to the evidenced value and exact locator of every optical datum
    a specification claims. ``manufacturer_document`` evidence is bound to
    retained source bytes in the managed asset store; ``external_authority``
    evidence is the persisted typed authority when source bytes cannot be
    retained; ``manual_record`` evidence attests user-entered data with
    subject, fields/values, source citation, actor/time and evidence basis.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PROJECTOR_SPEC_EVIDENCE_SCHEMA_VERSION
    authority_version: Literal[
        'projector-spec-evidence-1'
    ] = PROJECTOR_SPEC_EVIDENCE_AUTHORITY_VERSION
    evidence_kind: ProjectorSpecEvidenceKind

    # Exact subject/source identity the evidence describes.
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    publisher: str = Field(min_length=1)
    document_title: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    source_uri: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    # Extraction authority — required for extracted (non-manual) evidence.
    extractor_id: str | None = Field(default=None, min_length=1)
    extractor_version: str | None = Field(default=None, min_length=1)

    # Attested optical values: canonical value + exact locator per field.
    field_assertions: tuple[ProjectorSpecFieldAssertion, ...] = Field(
        min_length=1
    )

    # Manual-evidence basis — required for manual_record evidence.
    actor: str | None = Field(default=None, min_length=1)
    recorded_at_utc: str | None = Field(default=None, min_length=1)
    source_citation: str | None = Field(default=None, min_length=1)
    evidence_basis: str | None = Field(default=None, min_length=1)

    evidence_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('field_assertions')
    @classmethod
    def canonical_field_assertions(
        cls,
        values: tuple[ProjectorSpecFieldAssertion, ...],
    ) -> tuple[ProjectorSpecFieldAssertion, ...]:
        return tuple(sorted(values, key=lambda item: item.field))

    @model_validator(mode='after')
    def valid_evidence(self) -> 'ProjectorSpecificationEvidence':
        fields = [item.field for item in self.field_assertions]
        if len(fields) != len(set(fields)):
            raise ValueError('evidence field assertions must be unique')
        if set(fields) != set(PROJECTOR_SPEC_EVIDENCED_FIELDS):
            raise ValueError(
                'evidence must attest every optical field exactly once'
            )
        manual_fields = (
            'actor',
            'recorded_at_utc',
            'source_citation',
            'evidence_basis',
        )
        extraction_fields = ('extractor_id', 'extractor_version')
        if self.evidence_kind == 'manual_record':
            for name in manual_fields:
                if getattr(self, name) is None:
                    raise ValueError(f'manual_record evidence requires {name}')
            for name in extraction_fields:
                if getattr(self, name) is not None:
                    raise ValueError(
                        f'manual_record evidence must not carry {name}'
                    )
        else:
            for name in ('manufacturer', 'model', 'source_sha256') + extraction_fields:
                if getattr(self, name) is None:
                    raise ValueError(
                        f'{self.evidence_kind} evidence requires {name}'
                    )
            for name in manual_fields:
                if getattr(self, name) is not None:
                    raise ValueError(
                        f'{self.evidence_kind} evidence must not carry {name}'
                    )
        if self.evidence_sha256 != _digest(self.semantic_payload()):
            raise ValueError(
                'ProjectorSpecificationEvidence semantic hash mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'evidence_sha256'})

    def attested_optical_payload(self) -> dict[str, Any]:
        """Canonical optical payload this evidence attests."""
        return {item.field: item.value for item in self.field_assertions}

    def ref(self) -> ProjectorSpecEvidenceRef:
        """The typed reference a specification embeds in its provenance."""
        return ProjectorSpecEvidenceRef(
            evidence_kind=self.evidence_kind,
            evidence_sha256=self.evidence_sha256,
        )


def projector_spec_optical_values(
    *,
    lens_reference_offset_m: Offset3,
    optical_axis_local: Direction3,
    throw_ratio_min: float,
    throw_ratio_max: float,
    optical_zoom_ratio: float | None = None,
    horizontal_lens_shift: LensShiftRange | None = None,
    vertical_lens_shift: LensShiftRange | None = None,
    supported_aspect_ratios: Sequence[AspectRatio] = (),
) -> dict[str, Any]:
    """Canonical optical payload shared by evidence attestation and spec
    re-derivation — the same shape ``ProjectorSpecification.optical_payload``
    produces for a built specification."""
    return {
        'horizontal_lens_shift': (
            None
            if horizontal_lens_shift is None
            else horizontal_lens_shift.model_dump(mode='json')
        ),
        'lens_reference_offset_m': lens_reference_offset_m.model_dump(mode='json'),
        'optical_axis_local': optical_axis_local.model_dump(mode='json'),
        'optical_zoom_ratio': optical_zoom_ratio,
        'supported_aspect_ratios': [
            item.model_dump(mode='json')
            for item in supported_aspect_ratios
        ],
        'throw_ratio_max': float(throw_ratio_max),
        'throw_ratio_min': float(throw_ratio_min),
        'vertical_lens_shift': (
            None
            if vertical_lens_shift is None
            else vertical_lens_shift.model_dump(mode='json')
        ),
    }


def build_projector_spec_field_assertions(
    *,
    optical_values: Mapping[str, Any],
    field_locators: Mapping[str, str],
) -> tuple[ProjectorSpecFieldAssertion, ...]:
    """Build the complete assertion set over the canonical optical payload.

    Every evidenced field needs both its canonical value and an exact locator
    inside the bound source evidence; partial attestation is rejected so a
    missing optical datum can never ride along as unevaluated provenance.
    """
    expected = set(PROJECTOR_SPEC_EVIDENCED_FIELDS)
    if set(optical_values) != expected:
        raise ValueError(
            'optical values must cover every evidenced field exactly once'
        )
    if set(field_locators) != expected:
        raise ValueError(
            'field locators must cover every evidenced field exactly once'
        )
    return tuple(
        ProjectorSpecFieldAssertion(
            field=field,
            value=optical_values[field],
            locator=field_locators[field],
        )
        for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
    )


def _build_evidence(**kwargs: Any) -> ProjectorSpecificationEvidence:
    """Construct a self-hashed evidence record without duplicating payload shape."""
    # Sort exactly like the ``canonical_field_assertions`` validator so the
    # probe digest already reflects the persisted canonical order.
    assertions = tuple(
        sorted(
            (
                ProjectorSpecFieldAssertion.model_validate(item)
                for item in kwargs['field_assertions']
            ),
            key=lambda item: item.field,
        )
    )
    probe = ProjectorSpecificationEvidence.model_construct(
        field_assertions=assertions,
        evidence_sha256='0' * 64,
        **{key: value for key, value in kwargs.items() if key != 'field_assertions'},
    )
    digest = _digest(probe.semantic_payload())
    return ProjectorSpecificationEvidence(
        field_assertions=assertions,
        evidence_sha256=digest,
        **{key: value for key, value in kwargs.items() if key != 'field_assertions'},
    )


def build_projector_spec_document_evidence(
    *,
    evidence_kind: Literal['manufacturer_document', 'external_authority'],
    manufacturer: str,
    model: str,
    publisher: str,
    document_title: str,
    document_version: str,
    reference: str,
    source_sha256: str,
    extractor_id: str,
    extractor_version: str,
    field_assertions: Sequence[ProjectorSpecFieldAssertion],
    source_uri: str | None = None,
) -> ProjectorSpecificationEvidence:
    """Build extracted-evidence authority for manufacturer-sourced data.

    ``manufacturer_document`` binds evidence to source bytes retained in the
    managed asset store; ``external_authority`` is the typed immutable
    authority persisted when the source bytes themselves cannot be retained.
    ``extractor_id``/``extractor_version`` pin the parser/extraction version
    and each assertion's ``locator`` pins the field/page/table position the
    datum was extracted from.
    """
    return _build_evidence(
        evidence_kind=evidence_kind,
        manufacturer=manufacturer,
        model=model,
        publisher=publisher,
        document_title=document_title,
        document_version=document_version,
        reference=reference,
        source_uri=source_uri,
        source_sha256=source_sha256,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        field_assertions=field_assertions,
        actor=None,
        recorded_at_utc=None,
        source_citation=None,
        evidence_basis=None,
    )


def build_projector_spec_manual_evidence(
    *,
    publisher: str,
    document_title: str,
    document_version: str,
    reference: str,
    field_assertions: Sequence[ProjectorSpecFieldAssertion],
    source_citation: str,
    actor: str,
    recorded_at_utc: str,
    evidence_basis: str,
    manufacturer: str | None = None,
    model: str | None = None,
    source_uri: str | None = None,
    source_sha256: str | None = None,
) -> ProjectorSpecificationEvidence:
    """Build the explicit typed manual evidence record for user-entered data.

    The record preserves subject identity, the attested fields/values with
    per-field locators, the source citation, the actor and time, and the
    evidence basis distinguishing user measurement/override from
    manufacturer-certified data.
    """
    return _build_evidence(
        evidence_kind='manual_record',
        manufacturer=manufacturer,
        model=model,
        publisher=publisher,
        document_title=document_title,
        document_version=document_version,
        reference=reference,
        source_uri=source_uri,
        source_sha256=source_sha256,
        extractor_id=None,
        extractor_version=None,
        field_assertions=field_assertions,
        actor=actor,
        recorded_at_utc=recorded_at_utc,
        source_citation=source_citation,
        evidence_basis=evidence_basis,
    )


class ProjectorSpecification(BaseModel):
    """Immutable optical/cabinet reference authority for one projector specification."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PROJECTOR_SPEC_SCHEMA_VERSION
    authority_version: Literal['projector-spec-1'] = PROJECTOR_SPEC_AUTHORITY_VERSION
    specification_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    provenance: ProjectorSpecificationProvenance
    lens_reference_offset_m: Offset3
    optical_axis_local: Direction3
    throw_ratio_min: float = Field(gt=0)
    throw_ratio_max: float = Field(gt=0)
    optical_zoom_ratio: float | None = Field(default=None, ge=1.0)
    horizontal_lens_shift: LensShiftRange | None = None
    vertical_lens_shift: LensShiftRange | None = None
    supported_aspect_ratios: tuple[AspectRatio, ...] = ()
    specification_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('throw_ratio_min', 'throw_ratio_max')
    @classmethod
    def finite_throw_ratio(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def valid_specification(self) -> 'ProjectorSpecification':
        if self.throw_ratio_min > self.throw_ratio_max:
            raise ValueError('throw ratio minimum must not exceed maximum')
        if self.provenance.source_kind == 'manufacturer':
            if self.manufacturer is None or self.model is None:
                raise ValueError('manufacturer projector data requires manufacturer and model')
        ratio_keys = [
            (item.width_units, item.height_units)
            for item in self.supported_aspect_ratios
        ]
        if len(ratio_keys) != len(set(ratio_keys)):
            raise ValueError('supported aspect ratios must be unique')
        if self.specification_sha256 != _digest(self.semantic_payload()):
            raise ValueError('ProjectorSpecification semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'specification_id': self.specification_id,
            'version': self.version,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'provenance': self.provenance.model_dump(mode='json'),
            'lens_reference_offset_m': self.lens_reference_offset_m.model_dump(mode='json'),
            'optical_axis_local': self.optical_axis_local.model_dump(mode='json'),
            'throw_ratio_min': self.throw_ratio_min,
            'throw_ratio_max': self.throw_ratio_max,
            'optical_zoom_ratio': self.optical_zoom_ratio,
            'horizontal_lens_shift': (
                None
                if self.horizontal_lens_shift is None
                else self.horizontal_lens_shift.model_dump(mode='json')
            ),
            'vertical_lens_shift': (
                None
                if self.vertical_lens_shift is None
                else self.vertical_lens_shift.model_dump(mode='json')
            ),
            'supported_aspect_ratios': [
                item.model_dump(mode='json')
                for item in self.supported_aspect_ratios
            ],
        }

    def optical_payload(self) -> dict[str, Any]:
        """Canonical optical data the bound evidence must attest in full.

        Every value here directly determines PASS/FAIL geometry feasibility,
        so the resolved evidence record must re-derive all of it — field
        presence alone is not equivalent provenance.
        """
        payload = self.semantic_payload()
        return {key: payload[key] for key in PROJECTOR_SPEC_EVIDENCED_FIELDS}


def verify_projector_specification_evidence(
    *,
    specification: ProjectorSpecification,
    evidence: ProjectorSpecificationEvidence,
) -> None:
    """Fail-closed re-resolution of a specification's typed evidence ref.

    The resolved evidence must be the exact record the provenance ref pins
    (kind + self-hash), must describe the same manufacturer/model and source
    document identity the provenance declares, and must re-derive every
    optical value the specification claims. Any divergence raises instead of
    letting unsupported optical values serve as authority.
    """
    provenance = specification.provenance
    ref = provenance.evidence
    if evidence.evidence_kind != ref.evidence_kind:
        raise ValueError(
            'projector specification evidence kind does not match provenance ref'
        )
    if evidence.evidence_sha256 != ref.evidence_sha256:
        raise ValueError(
            'projector specification evidence hash does not match provenance ref'
        )
    expected_identity = {
        'manufacturer': specification.manufacturer,
        'model': specification.model,
        'publisher': provenance.publisher,
        'document_title': provenance.document_title,
        'document_version': provenance.document_version,
        'reference': provenance.reference,
        'source_uri': provenance.source_uri,
        'source_sha256': provenance.source_sha256,
    }
    for name, wanted in expected_identity.items():
        if getattr(evidence, name) != wanted:
            raise ValueError(
                f'projector specification evidence {name} diverges from '
                'the specification provenance'
            )
    if evidence.attested_optical_payload() != specification.optical_payload():
        raise ValueError(
            'projector specification optical values do not re-derive from '
            'the resolved evidence record'
        )


def build_projector_specification(
    *,
    specification_id: str,
    version: str,
    provenance: ProjectorSpecificationProvenance,
    lens_reference_offset_m: Offset3,
    optical_axis_local: Direction3,
    throw_ratio_min: float,
    throw_ratio_max: float,
    manufacturer: str | None = None,
    model: str | None = None,
    optical_zoom_ratio: float | None = None,
    horizontal_lens_shift: LensShiftRange | None = None,
    vertical_lens_shift: LensShiftRange | None = None,
    supported_aspect_ratios: Sequence[AspectRatio] = (),
) -> ProjectorSpecification:
    ratios = tuple(supported_aspect_ratios)
    payload = {
        'schema_version': PROJECTOR_SPEC_SCHEMA_VERSION,
        'authority_version': PROJECTOR_SPEC_AUTHORITY_VERSION,
        'specification_id': specification_id,
        'version': version,
        'manufacturer': manufacturer,
        'model': model,
        'provenance': provenance.model_dump(mode='json'),
        'lens_reference_offset_m': lens_reference_offset_m.model_dump(mode='json'),
        'optical_axis_local': optical_axis_local.model_dump(mode='json'),
        'throw_ratio_min': float(throw_ratio_min),
        'throw_ratio_max': float(throw_ratio_max),
        'optical_zoom_ratio': optical_zoom_ratio,
        'horizontal_lens_shift': (
            None
            if horizontal_lens_shift is None
            else horizontal_lens_shift.model_dump(mode='json')
        ),
        'vertical_lens_shift': (
            None
            if vertical_lens_shift is None
            else vertical_lens_shift.model_dump(mode='json')
        ),
        'supported_aspect_ratios': [
            item.model_dump(mode='json')
            for item in ratios
        ],
    }
    return ProjectorSpecification(
        specification_id=specification_id,
        version=version,
        manufacturer=manufacturer,
        model=model,
        provenance=provenance,
        lens_reference_offset_m=lens_reference_offset_m,
        optical_axis_local=optical_axis_local,
        throw_ratio_min=throw_ratio_min,
        throw_ratio_max=throw_ratio_max,
        optical_zoom_ratio=optical_zoom_ratio,
        horizontal_lens_shift=horizontal_lens_shift,
        vertical_lens_shift=vertical_lens_shift,
        supported_aspect_ratios=ratios,
        specification_sha256=_digest(payload),
    )


class ScreenGeometryBinding(BaseModel):
    # extra='allow' quarantines the legacy v1 ``acoustically_transparent``
    # flag (#541): old payloads still parse and round-trip byte-identically,
    # but the flag is dead — it was never acoustic truth and is no longer
    # part of the request identity. Transfer authority is the
    # ``screen_transfer_ref`` to an AcousticScreenTransferAuthority. Any
    # *other* unknown extra field is rejected: semantic identity is a fixed
    # declared-field set, never whatever extras happen to arrive.
    model_config = ConfigDict(frozen=True, extra='allow')

    entity_id: str = Field(min_length=1)
    visible_width_m: float = Field(gt=0)
    visible_height_m: float = Field(gt=0)
    image_center_offset_local_m: Offset3 = Field(default_factory=Offset3)
    frame_clearance_m: float = Field(ge=0)
    screen_transfer_ref: 'ExactExternalAuthorityRef | None' = None

    @field_validator('visible_width_m', 'visible_height_m', 'frame_clearance_m')
    @classmethod
    def finite_metric(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def quarantined_extras_only(self) -> 'ScreenGeometryBinding':
        unknown = set(self.model_extra or ()) - {'acoustically_transparent'}
        if unknown:
            raise ValueError(
                'screen geometry binding carries unknown fields: '
                f'{sorted(unknown)}'
            )
        return self


def _screen_identity_payload(screen: ScreenGeometryBinding) -> dict[str, Any]:
    """Canonical screen identity: declared fields only.

    ``screen_transfer_ref`` joins identity only when present (a screen that
    never declared transfer evidence hashes identically to pre-#541 content);
    the quarantined legacy flag and any extras never join identity.
    """
    payload: dict[str, Any] = {
        'entity_id': screen.entity_id,
        'visible_width_m': screen.visible_width_m,
        'visible_height_m': screen.visible_height_m,
        'image_center_offset_local_m': (
            screen.image_center_offset_local_m.model_dump(mode='json')
        ),
        'frame_clearance_m': screen.frame_clearance_m,
    }
    if screen.screen_transfer_ref is not None:
        payload['screen_transfer_ref'] = screen.screen_transfer_ref.model_dump(
            mode='json'
        )
    return payload


def _legacy_v1_screen_payload(screen: ScreenGeometryBinding) -> dict[str, Any]:
    """Reconstruct the exact pre-#541 v1 screen dump for hash fallback.

    The old model had no ``screen_transfer_ref`` — it is never emitted here.
    Only the one recognized historical field ``acoustically_transparent`` is
    restored, verbatim from the quarantined extras (including an explicit
    null); every other field is already covered by the declared set.
    """
    payload: dict[str, Any] = {
        'entity_id': screen.entity_id,
        'visible_width_m': screen.visible_width_m,
        'visible_height_m': screen.visible_height_m,
        'image_center_offset_local_m': (
            screen.image_center_offset_local_m.model_dump(mode='json')
        ),
        'frame_clearance_m': screen.frame_clearance_m,
    }
    extras = screen.model_extra or {}
    # The v1 model declared the field (default None), so its dump always
    # carried the key — including an explicit null.
    payload['acoustically_transparent'] = extras.get('acoustically_transparent')
    return payload


class SeatGeometryBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    eye_reference_offset_local_m: Offset3
    head_center_offset_local_m: Offset3
    head_radius_m: float = Field(gt=0)
    riser_entity_id: str | None = Field(default=None, min_length=1)

    @field_validator('head_radius_m')
    @classmethod
    def finite_radius(cls, value: float) -> float:
        return _finite(value)


class AngleRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum_deg: float
    maximum_deg: float

    @field_validator('minimum_deg', 'maximum_deg')
    @classmethod
    def finite_angle(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def valid_range(self) -> 'AngleRange':
        if self.minimum_deg > self.maximum_deg:
            raise ValueError('angle minimum must not exceed maximum')
        return self


class SightlineSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1)
    horizontal_fraction: float = Field(ge=0, le=1)
    vertical_fraction: float = Field(ge=0, le=1)

    @field_validator('horizontal_fraction', 'vertical_fraction')
    @classmethod
    def finite_fraction(cls, value: float) -> float:
        return _finite(value)


class VideoGeometryPolicy(BaseModel):
    """Explicit user/profile thresholds; no cinema standard is implied by defaults."""

    model_config = ConfigDict(frozen=True)

    horizontal_viewing_angle_deg: AngleRange | None = None
    vertical_viewing_angle_deg: AngleRange | None = None
    center_elevation_angle_deg: AngleRange | None = None
    sightline_samples: tuple[SightlineSample, ...]
    sightline_clearance_m: float = Field(ge=0)
    riser_support_tolerance_m: float = Field(ge=0)
    max_optical_axis_deviation_deg: float = Field(ge=0, le=90)
    collision_clearance_m: float = Field(ge=0)

    @field_validator(
        'sightline_clearance_m',
        'riser_support_tolerance_m',
        'max_optical_axis_deviation_deg',
        'collision_clearance_m',
    )
    @classmethod
    def finite_policy_value(cls, value: float) -> float:
        return _finite(value)

    @model_validator(mode='after')
    def valid_policy(self) -> 'VideoGeometryPolicy':
        if not self.sightline_samples:
            raise ValueError('at least one sightline sample is required')
        sample_ids = [item.sample_id for item in self.sightline_samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError('sightline sample ids must be unique')
        return self


class VideoGeometryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = VIDEO_GEOMETRY_SCHEMA_VERSION
    authority_version: Literal['video-geometry-1'] = VIDEO_GEOMETRY_AUTHORITY_VERSION
    projector_entity_id: str = Field(min_length=1)
    projector_specification_id: str = Field(min_length=1)
    projector_specification_version: str = Field(min_length=1)
    projector_specification_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    screen: ScreenGeometryBinding
    seats: tuple[SeatGeometryBinding, ...]
    policy: VideoGeometryPolicy
    collision_entity_ids: tuple[str, ...]
    request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @field_validator('seats')
    @classmethod
    def canonical_seats(
        cls,
        values: tuple[SeatGeometryBinding, ...],
    ) -> tuple[SeatGeometryBinding, ...]:
        return tuple(sorted(values, key=lambda item: item.entity_id))

    @field_validator('collision_entity_ids')
    @classmethod
    def canonical_collision_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    @model_validator(mode='after')
    def valid_request(self) -> 'VideoGeometryRequest':
        seat_ids = [item.entity_id for item in self.seats]
        if len(seat_ids) != len(set(seat_ids)):
            raise ValueError('seat geometry bindings must be unique')
        if len(self.collision_entity_ids) != len(set(self.collision_entity_ids)):
            raise ValueError('collision entity ids must be unique')
        if self.request_sha256 != _digest(self.identity_payload()):
            # Pre-#541 payloads may embed the legacy acoustically_transparent
            # extra in the screen dump; accept that historical identity so
            # persisted requests still revalidate. The legacy dump never
            # carries screen_transfer_ref — that field did not exist.
            legacy = self.identity_payload()
            legacy['screen'] = _legacy_v1_screen_payload(self.screen)
            if self.request_sha256 == _digest(legacy):
                return self
            # Payloads serialized under the post-#541 model baked
            # 'screen_transfer_ref': null into identity; accept that
            # historical hash too so mid-era requests still revalidate.
            buggy = self.identity_payload()
            buggy['screen'] = {
                **buggy['screen'],
                'screen_transfer_ref': None,
            }
            if self.request_sha256 != _digest(buggy):
                raise ValueError('VideoGeometryRequest semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'projector_entity_id': self.projector_entity_id,
            'projector_specification_id': self.projector_specification_id,
            'projector_specification_version': self.projector_specification_version,
            'projector_specification_sha256': self.projector_specification_sha256,
            'screen': _screen_identity_payload(self.screen),
            'seats': [item.model_dump(mode='json') for item in self.seats],
            'policy': self.policy.model_dump(mode='json'),
            'collision_entity_ids': list(self.collision_entity_ids),
        }


def build_video_geometry_request(
    *,
    projector_entity_id: str,
    projector_specification: ProjectorSpecification,
    screen: ScreenGeometryBinding,
    seats: Sequence[SeatGeometryBinding],
    policy: VideoGeometryPolicy,
    collision_entity_ids: Sequence[str],
) -> VideoGeometryRequest:
    ordered_seats = tuple(sorted(tuple(seats), key=lambda item: item.entity_id))
    ordered_collision_ids = tuple(sorted(tuple(collision_entity_ids)))
    payload = {
        'schema_version': VIDEO_GEOMETRY_SCHEMA_VERSION,
        'authority_version': VIDEO_GEOMETRY_AUTHORITY_VERSION,
        'projector_entity_id': projector_entity_id,
        'projector_specification_id': projector_specification.specification_id,
        'projector_specification_version': projector_specification.version,
        'projector_specification_sha256': projector_specification.specification_sha256,
        # The quarantined legacy flag never joins request identity (#541).
        'screen': _screen_identity_payload(screen),
        'seats': [item.model_dump(mode='json') for item in ordered_seats],
        'policy': policy.model_dump(mode='json'),
        'collision_entity_ids': list(ordered_collision_ids),
    }
    return VideoGeometryRequest(
        projector_entity_id=projector_entity_id,
        projector_specification_id=projector_specification.specification_id,
        projector_specification_version=projector_specification.version,
        projector_specification_sha256=projector_specification.specification_sha256,
        screen=screen,
        seats=ordered_seats,
        policy=policy,
        collision_entity_ids=ordered_collision_ids,
        request_sha256=_digest(payload),
    )


class VideoGeometryTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    evaluated_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_variant_binding(self) -> 'VideoGeometryTarget':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('SystemVariant id/hash must be supplied together')
        return self


class ProjectionGeometryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: EvaluationStatus
    lens_position: Position3
    screen_image_center: Position3
    image_plane_corners: tuple[Position3, Position3, Position3, Position3]
    projection_cone_directions: tuple[Direction3, Direction3, Direction3, Direction3]
    optical_axis_intersection: Position3 | None
    throw_distance_m: float
    throw_ratio: float
    required_zoom_fraction: float | None
    required_horizontal_lens_shift_fraction: float | None
    required_vertical_lens_shift_fraction: float | None
    optical_axis_deviation_deg: float
    screen_aperture_status: EvaluationStatus
    projector_side_status: EvaluationStatus
    optical_axis_status: EvaluationStatus
    throw_ratio_status: EvaluationStatus
    horizontal_lens_shift_status: EvaluationStatus
    vertical_lens_shift_status: EvaluationStatus
    aspect_ratio_status: EvaluationStatus


class SeatViewingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    eye_position: Position3
    horizontal_viewing_angle_deg: float
    vertical_viewing_angle_deg: float
    center_elevation_angle_deg: float
    horizontal_status: EvaluationStatus
    vertical_status: EvaluationStatus
    center_elevation_status: EvaluationStatus


class SeatSightlineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    row_id: str = Field(min_length=1)
    status: EvaluationStatus
    blocking_seat_ids: tuple[str, ...]
    blocking_row_ids: tuple[str, ...]
    blocked_sample_ids: tuple[str, ...]
    minimum_head_ray_clearance_m: float | None


class RiserInteractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    seat_entity_id: str = Field(min_length=1)
    riser_entity_id: str | None = Field(default=None, min_length=1)
    status: EvaluationStatus
    seat_base_z_m: float
    riser_top_z_m: float | None
    support_gap_m: float | None
    horizontally_supported: bool | None


class CollisionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_a: str = Field(min_length=1)
    entity_b: str = Field(min_length=1)
    status: EvaluationStatus
    intersects_or_violates_clearance: bool
    # Issue #464: records whether the check consumed exact extruded body
    # geometry, the bounding envelope on both sides, or a mix. Absent in
    # pre-464 payloads; omitted from identity payloads while None.
    geometry_authority: (
        Literal[
            'exact_body_geometry',
            'bounding_envelope',
            'mixed_body_geometry',
            'envelope_unverified',
        ]
        | None
    ) = None


def _collision_payload(item: CollisionResult) -> dict[str, Any]:
    payload = item.model_dump(mode='json')
    if payload.get('geometry_authority') is None:
        payload.pop('geometry_authority', None)
    return payload


class VideoGeometryEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = VIDEO_GEOMETRY_SCHEMA_VERSION
    authority_version: Literal['video-geometry-1'] = VIDEO_GEOMETRY_AUTHORITY_VERSION
    evaluation_id: str = Field(min_length=1)
    target: VideoGeometryTarget
    request: VideoGeometryRequest
    projector_specification_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    projection: ProjectionGeometryResult
    viewing: tuple[SeatViewingResult, ...]
    sightlines: tuple[SeatSightlineResult, ...]
    risers: tuple[RiserInteractionResult, ...]
    collisions: tuple[CollisionResult, ...]
    geometry_status: EvaluationStatus
    screen_acoustic_effect_status: EvaluationStatus
    screen_acoustic_effect_reason: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'VideoGeometryEvaluation':
        digest = _digest(self.identity_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('VideoGeometryEvaluation semantic hash mismatch')
        if self.evaluation_id != 'vge-' + digest[:24]:
            raise ValueError('VideoGeometryEvaluation deterministic id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'target': self.target.model_dump(mode='json'),
            'request': self.request.model_dump(mode='json'),
            'projector_specification_sha256': self.projector_specification_sha256,
            'projection': self.projection.model_dump(mode='json'),
            'viewing': [item.model_dump(mode='json') for item in self.viewing],
            'sightlines': [item.model_dump(mode='json') for item in self.sightlines],
            'risers': [item.model_dump(mode='json') for item in self.risers],
            'collisions': [_collision_payload(item) for item in self.collisions],
            'geometry_status': self.geometry_status,
            'screen_acoustic_effect_status': self.screen_acoustic_effect_status,
            'screen_acoustic_effect_reason': self.screen_acoustic_effect_reason,
        }


def _plane_frame(
    entity: SceneEntity,
    *,
    width_m: float,
    height_m: float,
    center_offset_local_m: Offset3,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[Position3, Position3, Position3, Position3],
]:
    """World-space frame of a rectangular image aperture on an entity.

    Shared by projection screens and direct-view displays: the entity's
    local X axis is the aperture's right axis, local Y its normal, local Z
    its up axis — identical convention for both surface kinds.
    """
    if entity.size_m is None:
        raise ValueError(f'{entity.kind} entity requires physical size')
    matrix = quaternion_to_matrix3(entity.orientation)
    right = _unit((matrix[0][0], matrix[1][0], matrix[2][0]))
    normal = _unit((matrix[0][1], matrix[1][1], matrix[2][1]))
    up = _unit((matrix[0][2], matrix[1][2], matrix[2][2]))
    center = _world_offset(entity, center_offset_local_m)
    half_w = width_m * 0.5
    half_h = height_m * 0.5
    corners = tuple(
        _position(_add(_add(center, _scale(right, sx * half_w)), _scale(up, sz * half_h)))
        for sx, sz in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
    )
    return center, right, up, normal, corners  # type: ignore[return-value]


def _screen_frame(
    screen_entity: SceneEntity,
    binding: ScreenGeometryBinding,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[Position3, Position3, Position3, Position3],
]:
    if screen_entity.kind != 'screen':
        raise ValueError('screen binding must reference a screen SceneEntity')
    return _plane_frame(
        screen_entity,
        width_m=binding.visible_width_m,
        height_m=binding.visible_height_m,
        center_offset_local_m=binding.image_center_offset_local_m,
    )


def _screen_aperture_status(
    screen_entity: SceneEntity,
    binding: ScreenGeometryBinding,
) -> EvaluationStatus:
    assert screen_entity.size_m is not None
    offset = binding.image_center_offset_local_m
    within_width = (
        abs(offset.x_m) + binding.visible_width_m * 0.5
        <= screen_entity.size_m.x_m * 0.5 + _EPS
    )
    within_height = (
        abs(offset.z_m) + binding.visible_height_m * 0.5
        <= screen_entity.size_m.z_m * 0.5 + _EPS
    )
    within_depth = abs(offset.y_m) <= screen_entity.size_m.y_m * 0.5 + _EPS
    return 'PASS' if within_width and within_height and within_depth else 'FAIL'


def _projector_aspect_status(
    specification: ProjectorSpecification,
    screen: ScreenGeometryBinding,
) -> EvaluationStatus:
    if not specification.supported_aspect_ratios:
        return 'UNKNOWN'
    actual = screen.visible_width_m / screen.visible_height_m
    for ratio in specification.supported_aspect_ratios:
        if abs(actual - ratio.value) <= 1e-6:
            return 'PASS'
    return 'FAIL'


def _projection_result(
    *,
    projector_entity: SceneEntity,
    screen_entity: SceneEntity,
    specification: ProjectorSpecification,
    request: VideoGeometryRequest,
) -> ProjectionGeometryResult:
    if projector_entity.kind != 'projector':
        raise ValueError('projector binding must reference a projector SceneEntity')
    center, right, up, normal, corners = _screen_frame(screen_entity, request.screen)
    lens = _world_offset(projector_entity, specification.lens_reference_offset_m)
    optical_axis = _world_direction(projector_entity, specification.optical_axis_local)
    toward_screen = _scale(normal, -1.0)
    deviation = _angle_between_deg(optical_axis, toward_screen)
    optical_axis_status: EvaluationStatus = (
        'PASS'
        if deviation <= request.policy.max_optical_axis_deviation_deg + _EPS
        else 'FAIL'
    )

    signed_side_distance = _dot(_sub(lens, center), normal)
    projector_side_status: EvaluationStatus = 'PASS' if signed_side_distance > _EPS else 'FAIL'
    throw_distance = abs(signed_side_distance)
    throw_ratio = throw_distance / request.screen.visible_width_m
    throw_ratio_status: EvaluationStatus = (
        'PASS'
        if specification.throw_ratio_min - _EPS
        <= throw_ratio
        <= specification.throw_ratio_max + _EPS
        else 'FAIL'
    )
    ratio_span = specification.throw_ratio_max - specification.throw_ratio_min
    if abs(ratio_span) <= _EPS:
        required_zoom_fraction = 0.0 if throw_ratio_status == 'PASS' else None
    else:
        required_zoom_fraction = (
            throw_ratio - specification.throw_ratio_min
        ) / ratio_span

    denominator = _dot(optical_axis, normal)
    intersection: tuple[float, float, float] | None = None
    if abs(denominator) > _EPS:
        t = _dot(_sub(center, lens), normal) / denominator
        if t > _EPS:
            intersection = _add(lens, _scale(optical_axis, t))

    horizontal_shift: float | None = None
    vertical_shift: float | None = None
    if intersection is not None:
        image_center_delta = _sub(center, intersection)
        horizontal_shift = _dot(image_center_delta, right) / request.screen.visible_width_m
        vertical_shift = _dot(image_center_delta, up) / request.screen.visible_height_m

    if horizontal_shift is None:
        horizontal_status: EvaluationStatus = 'FAIL'
    elif specification.horizontal_lens_shift is None:
        horizontal_status = 'UNKNOWN'
    else:
        horizontal_status = (
            'PASS'
            if specification.horizontal_lens_shift.contains(horizontal_shift)
            else 'FAIL'
        )
    if vertical_shift is None:
        vertical_status: EvaluationStatus = 'FAIL'
    elif specification.vertical_lens_shift is None:
        vertical_status = 'UNKNOWN'
    else:
        vertical_status = (
            'PASS'
            if specification.vertical_lens_shift.contains(vertical_shift)
            else 'FAIL'
        )

    cone_directions = tuple(
        Direction3(x=direction[0], y=direction[1], z=direction[2])
        for direction in (
            _unit(_sub(_v(corner), lens))
            for corner in corners
        )
    )
    aperture_status = _screen_aperture_status(screen_entity, request.screen)
    aspect_status = _projector_aspect_status(specification, request.screen)
    status = _combine_status((
        aperture_status,
        projector_side_status,
        optical_axis_status,
        throw_ratio_status,
        horizontal_status,
        vertical_status,
        aspect_status,
    ))
    return ProjectionGeometryResult(
        status=status,
        lens_position=_position(lens),
        screen_image_center=_position(center),
        image_plane_corners=corners,
        projection_cone_directions=cone_directions,  # type: ignore[arg-type]
        optical_axis_intersection=None if intersection is None else _position(intersection),
        throw_distance_m=throw_distance,
        throw_ratio=throw_ratio,
        required_zoom_fraction=required_zoom_fraction,
        required_horizontal_lens_shift_fraction=horizontal_shift,
        required_vertical_lens_shift_fraction=vertical_shift,
        optical_axis_deviation_deg=deviation,
        screen_aperture_status=aperture_status,
        projector_side_status=projector_side_status,
        optical_axis_status=optical_axis_status,
        throw_ratio_status=throw_ratio_status,
        horizontal_lens_shift_status=horizontal_status,
        vertical_lens_shift_status=vertical_status,
        aspect_ratio_status=aspect_status,
    )


def _seat_eye(entity: SceneEntity, binding: SeatGeometryBinding) -> tuple[float, float, float]:
    if entity.kind != 'seat':
        raise ValueError('seat binding must reference a seat SceneEntity')
    return _world_offset(entity, binding.eye_reference_offset_local_m)


def _seat_head(entity: SceneEntity, binding: SeatGeometryBinding) -> tuple[float, float, float]:
    return _world_offset(entity, binding.head_center_offset_local_m)


def _screen_sample_point(
    *,
    center: tuple[float, float, float],
    right: tuple[float, float, float],
    up: tuple[float, float, float],
    width_m: float,
    height_m: float,
    sample: SightlineSample,
) -> tuple[float, float, float]:
    return _add(
        _add(
            center,
            _scale(right, (sample.horizontal_fraction - 0.5) * width_m),
        ),
        _scale(up, (sample.vertical_fraction - 0.5) * height_m),
    )


def _viewing_result(
    *,
    seat_entity: SceneEntity,
    binding: SeatGeometryBinding,
    center: tuple[float, float, float],
    right: tuple[float, float, float],
    up: tuple[float, float, float],
    width_m: float,
    height_m: float,
    policy: VideoGeometryPolicy,
) -> SeatViewingResult:
    eye = _seat_eye(seat_entity, binding)
    left_middle = _add(center, _scale(right, -width_m * 0.5))
    right_middle = _add(center, _scale(right, width_m * 0.5))
    bottom_middle = _add(center, _scale(up, -height_m * 0.5))
    top_middle = _add(center, _scale(up, height_m * 0.5))
    horizontal = _angle_between_deg(_sub(left_middle, eye), _sub(right_middle, eye))
    vertical = _angle_between_deg(_sub(bottom_middle, eye), _sub(top_middle, eye))
    center_delta = _sub(center, eye)
    center_elevation = degrees(atan2(
        center_delta[2],
        sqrt(center_delta[0] * center_delta[0] + center_delta[1] * center_delta[1]),
    ))
    return SeatViewingResult(
        seat_entity_id=binding.entity_id,
        row_id=binding.row_id,
        eye_position=_position(eye),
        horizontal_viewing_angle_deg=horizontal,
        vertical_viewing_angle_deg=vertical,
        center_elevation_angle_deg=center_elevation,
        horizontal_status=_range_status(horizontal, policy.horizontal_viewing_angle_deg),
        vertical_status=_range_status(vertical, policy.vertical_viewing_angle_deg),
        center_elevation_status=_range_status(
            center_elevation,
            policy.center_elevation_angle_deg,
        ),
    )


def _segment_sphere_clearance(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    center: tuple[float, float, float],
    radius: float,
) -> tuple[float, float]:
    direction = _sub(end, start)
    length_sq = _dot(direction, direction)
    if length_sq <= _EPS:
        raise ValueError('sightline segment has zero length')
    t = _dot(_sub(center, start), direction) / length_sq
    t_clamped = max(0.0, min(1.0, t))
    closest = _add(start, _scale(direction, t_clamped))
    return _norm(_sub(center, closest)) - radius, t


def _sightline_results(
    *,
    scene: SceneDocument,
    bindings: tuple[SeatGeometryBinding, ...],
    center: tuple[float, float, float],
    right: tuple[float, float, float],
    up: tuple[float, float, float],
    width_m: float,
    height_m: float,
    policy: VideoGeometryPolicy,
) -> tuple[SeatSightlineResult, ...]:
    by_id = {item.entity_id: item for item in bindings}
    eyes = {
        entity_id: _seat_eye(scene.entity(entity_id), binding)
        for entity_id, binding in by_id.items()
    }
    heads = {
        entity_id: _seat_head(scene.entity(entity_id), binding)
        for entity_id, binding in by_id.items()
    }
    results: list[SeatSightlineResult] = []
    for viewer in bindings:
        eye = eyes[viewer.entity_id]
        blocking_ids: set[str] = set()
        blocked_samples: set[str] = set()
        minimum_clearance: float | None = None
        for sample in policy.sightline_samples:
            target = _screen_sample_point(
                center=center,
                right=right,
                up=up,
                width_m=width_m,
                height_m=height_m,
                sample=sample,
            )
            for blocker in bindings:
                if blocker.entity_id == viewer.entity_id:
                    continue
                clearance, segment_t = _segment_sphere_clearance(
                    eye,
                    target,
                    heads[blocker.entity_id],
                    blocker.head_radius_m,
                )
                if segment_t <= _EPS or segment_t >= 1.0 - _EPS:
                    continue
                if minimum_clearance is None or clearance < minimum_clearance:
                    minimum_clearance = clearance
                if clearance + _EPS < policy.sightline_clearance_m:
                    blocking_ids.add(blocker.entity_id)
                    blocked_samples.add(sample.sample_id)
        ordered_blockers = tuple(sorted(blocking_ids))
        blocking_rows = tuple(sorted({by_id[item].row_id for item in ordered_blockers}))
        results.append(SeatSightlineResult(
            seat_entity_id=viewer.entity_id,
            row_id=viewer.row_id,
            status='PASS' if not ordered_blockers else 'FAIL',
            blocking_seat_ids=ordered_blockers,
            blocking_row_ids=blocking_rows,
            blocked_sample_ids=tuple(sorted(blocked_samples)),
            minimum_head_ray_clearance_m=minimum_clearance,
        ))
    return tuple(results)


def _entity_obb(
    entity: SceneEntity,
    *,
    extra_m: float = 0.0,
) -> tuple[
    tuple[float, float, float],
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    tuple[float, float, float],
]:
    if entity.size_m is None:
        raise ValueError(f'entity {entity.entity_id} has no physical size')
    matrix = quaternion_to_matrix3(entity.orientation)
    axes = (
        _unit((matrix[0][0], matrix[1][0], matrix[2][0])),
        _unit((matrix[0][1], matrix[1][1], matrix[2][1])),
        _unit((matrix[0][2], matrix[1][2], matrix[2][2])),
    )
    half = (
        entity.size_m.x_m * 0.5 + extra_m,
        entity.size_m.y_m * 0.5 + extra_m,
        entity.size_m.z_m * 0.5 + extra_m,
    )
    return _v(entity.position), axes, half


def _obb_corners(entity: SceneEntity) -> tuple[tuple[float, float, float], ...]:
    center, axes, half = _entity_obb(entity)
    corners: list[tuple[float, float, float]] = []
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                corners.append(_add(
                    _add(
                        _add(center, _scale(axes[0], sx * half[0])),
                        _scale(axes[1], sy * half[1]),
                    ),
                    _scale(axes[2], sz * half[2]),
                ))
    return tuple(corners)


def _obb_intersects(
    left: SceneEntity,
    right: SceneEntity,
    *,
    left_extra_m: float,
    right_extra_m: float,
) -> bool:
    a_center, a_axes, a_half = _entity_obb(left, extra_m=left_extra_m)
    b_center, b_axes, b_half = _entity_obb(right, extra_m=right_extra_m)
    rotation = [[_dot(a_axes[i], b_axes[j]) for j in range(3)] for i in range(3)]
    abs_rotation = [[abs(rotation[i][j]) + 1e-10 for j in range(3)] for i in range(3)]
    delta_world = _sub(b_center, a_center)
    translation = [_dot(delta_world, a_axes[i]) for i in range(3)]

    for i in range(3):
        radius_a = a_half[i]
        radius_b = sum(b_half[j] * abs_rotation[i][j] for j in range(3))
        if abs(translation[i]) > radius_a + radius_b:
            return False
    for j in range(3):
        radius_a = sum(a_half[i] * abs_rotation[i][j] for i in range(3))
        radius_b = b_half[j]
        projection = abs(sum(translation[i] * rotation[i][j] for i in range(3)))
        if projection > radius_a + radius_b:
            return False
    for i in range(3):
        for j in range(3):
            radius_a = (
                a_half[(i + 1) % 3] * abs_rotation[(i + 2) % 3][j]
                + a_half[(i + 2) % 3] * abs_rotation[(i + 1) % 3][j]
            )
            radius_b = (
                b_half[(j + 1) % 3] * abs_rotation[i][(j + 2) % 3]
                + b_half[(j + 2) % 3] * abs_rotation[i][(j + 1) % 3]
            )
            projection = abs(
                translation[(i + 2) % 3] * rotation[(i + 1) % 3][j]
                - translation[(i + 1) % 3] * rotation[(i + 2) % 3][j]
            )
            if projection > radius_a + radius_b:
                return False
    return True


def _entity_z_extent(entity: SceneEntity) -> tuple[float, float]:
    corners = _obb_corners(entity)
    z_values = [point[2] for point in corners]
    return min(z_values), max(z_values)


def _extruded_intersects(
    left: SceneEntity,
    right: SceneEntity,
    *,
    left_extra_m: float,
    right_extra_m: float,
) -> bool:
    """Extruded-body intersection: exact XY footprints plus Z-interval overlap.

    Each entity contributes the footprint authority it actually has: authored
    cylinder/polygon bodies give their exact upright XY footprint while
    box/mesh/tilted bodies contribute the bounding-envelope hull.
    ``*_extra_m`` inflates both the XY distance test and the Z interval, which
    mirrors the per-side OBB inflation used by the envelope path.
    """

    left_footprint = entity_horizontal_footprint(left)
    right_footprint = entity_horizontal_footprint(right)
    xy_gap = float(left_footprint.distance(right_footprint))
    left_z_min, left_z_max = _entity_z_extent(left)
    right_z_min, right_z_max = _entity_z_extent(right)
    z_overlap = (
        left_z_min - left_extra_m <= right_z_max + right_extra_m + _EPS
        and right_z_min - right_extra_m <= left_z_max + left_extra_m + _EPS
    )
    # Footprints that touch (gap 0) must still intersect even at zero
    # clearance, matching the SAT convention of the OBB envelope path.
    return z_overlap and xy_gap <= left_extra_m + right_extra_m + _EPS


def _collision_results(
    *,
    scene: SceneDocument,
    collision_entity_ids: tuple[str, ...],
    surface_entity_id: str,
    surface_frame_clearance_m: float,
    policy: VideoGeometryPolicy,
) -> tuple[CollisionResult, ...]:
    entities = [scene.entity(entity_id) for entity_id in collision_entity_ids]
    allowed_kinds = {'speaker', 'screen', 'projector', 'display'}
    invalid = [item.entity_id for item in entities if item.kind not in allowed_kinds]
    if invalid:
        raise ValueError(
            'collision evaluation accepts speaker/screen/projector/display entities only: '
            + ', '.join(sorted(invalid))
        )
    authorities = {
        entity.entity_id: entity_collision_geometry_authority(entity)
        for entity in entities
    }
    results: list[CollisionResult] = []
    for index, left in enumerate(entities):
        for right in entities[index + 1:]:
            half_clearance = policy.collision_clearance_m * 0.5
            left_extra = half_clearance
            right_extra = half_clearance
            if left.entity_id == surface_entity_id:
                left_extra += surface_frame_clearance_m
            if right.entity_id == surface_entity_id:
                right_extra += surface_frame_clearance_m
            left_authority = authorities[left.entity_id]
            right_authority = authorities[right.entity_id]
            exact_pair = (
                left_authority == 'exact_body_geometry'
                and right_authority == 'exact_body_geometry'
            )
            if left_authority == 'exact_body_geometry' or right_authority == 'exact_body_geometry':
                # At least one side authored an exact body: consume explicit
                # collision geometry, retaining the envelope on the other side.
                intersects = _extruded_intersects(
                    left,
                    right,
                    left_extra_m=left_extra,
                    right_extra_m=right_extra,
                )
            else:
                intersects = _obb_intersects(
                    left,
                    right,
                    left_extra_m=left_extra,
                    right_extra_m=right_extra,
                )
            results.append(CollisionResult(
                entity_a=left.entity_id,
                entity_b=right.entity_id,
                status='FAIL' if intersects else 'PASS',
                intersects_or_violates_clearance=intersects,
                geometry_authority=(
                    'exact_body_geometry'
                    if exact_pair
                    else (
                        'mixed_body_geometry'
                        if 'exact_body_geometry' in (left_authority, right_authority)
                        else (
                            # Issue #656: a mesh body that is not provably
                            # inside its size_m envelope must not masquerade
                            # as verified envelope clearance.
                            'envelope_unverified'
                            if 'envelope_unverified'
                            in (left_authority, right_authority)
                            else 'bounding_envelope'
                        )
                    )
                ),
            ))
    return tuple(results)


def _riser_results(
    *,
    scene: SceneDocument,
    bindings: tuple[SeatGeometryBinding, ...],
    policy: VideoGeometryPolicy,
) -> tuple[RiserInteractionResult, ...]:
    results: list[RiserInteractionResult] = []
    for binding in bindings:
        seat = scene.entity(binding.entity_id)
        seat_corners = _obb_corners(seat)
        seat_base = min(point[2] for point in seat_corners)
        if binding.riser_entity_id is None:
            results.append(RiserInteractionResult(
                seat_entity_id=binding.entity_id,
                riser_entity_id=None,
                status='NOT_APPLICABLE',
                seat_base_z_m=seat_base,
                riser_top_z_m=None,
                support_gap_m=None,
                horizontally_supported=None,
            ))
            continue
        riser = scene.entity(binding.riser_entity_id)
        if riser.kind != 'riser':
            raise ValueError('riser binding must reference a riser SceneEntity')
        riser_corners = _obb_corners(riser)
        riser_top = max(point[2] for point in riser_corners)
        # Issue #464: support uses the authored XY footprint when one exists
        # (an L-shaped riser must not "support" a seat over its missing
        # corner); the bounding-envelope hull is the fallback.
        riser_footprint = entity_horizontal_footprint(riser)
        seat_point = Point(float(seat.position.x_m), float(seat.position.y_m))
        horizontally_supported = bool(
            riser_footprint.covers(seat_point)
            or float(riser_footprint.distance(seat_point)) <= _EPS
        )
        gap = seat_base - riser_top
        supported = (
            horizontally_supported
            and abs(gap) <= policy.riser_support_tolerance_m + _EPS
        )
        results.append(RiserInteractionResult(
            seat_entity_id=binding.entity_id,
            riser_entity_id=binding.riser_entity_id,
            status='PASS' if supported else 'FAIL',
            seat_base_z_m=seat_base,
            riser_top_z_m=riser_top,
            support_gap_m=gap,
            horizontally_supported=horizontally_supported,
        ))
    return tuple(results)


def _validate_scene_bindings(
    *,
    scene: SceneDocument,
    request: VideoGeometryRequest,
) -> None:
    projector = scene.entity(request.projector_entity_id)
    if projector.kind != 'projector':
        raise ValueError('projector_entity_id must reference a projector entity')
    screen = scene.entity(request.screen.entity_id)
    if screen.kind != 'screen':
        raise ValueError('screen binding must reference a screen entity')
    for binding in request.seats:
        if scene.entity(binding.entity_id).kind != 'seat':
            raise ValueError('seat geometry binding must reference a seat entity')
        if binding.riser_entity_id is not None:
            if scene.entity(binding.riser_entity_id).kind != 'riser':
                raise ValueError('riser_entity_id must reference a riser entity')
    for entity_id in request.collision_entity_ids:
        scene.entity(entity_id)


def _target_and_scene(
    *,
    baseline: SceneRevision,
    variant: SystemVariant | None,
) -> tuple[VideoGeometryTarget, SceneDocument]:
    if variant is None:
        scene = baseline.document
        target = VideoGeometryTarget(
            document_id=baseline.document_id,
            scene_revision_id=baseline.revision_id,
            scene_content_hash=baseline.content_hash,
            system_variant_id=None,
            system_variant_sha256=None,
            evaluated_scene_content_hash=baseline.content_hash,
        )
        return target, scene
    scene = materialize_system_variant(baseline, variant)
    target = VideoGeometryTarget(
        document_id=baseline.document_id,
        scene_revision_id=baseline.revision_id,
        scene_content_hash=baseline.content_hash,
        system_variant_id=variant.variant_id,
        system_variant_sha256=variant.variant_sha256,
        evaluated_scene_content_hash=scene_content_hash(scene),
    )
    return target, scene


def _screen_acoustic_effect(
    screen: ScreenGeometryBinding,
    screen_transfers: Mapping[str, Any] | None,
) -> tuple[EvaluationStatus, str]:
    """Resolve the screen's acoustic effect from the bound transfer
    authority (#541). The legacy ``acoustically_transparent`` flag is dead
    data — it is never read as acoustic truth — and an unresolvable or
    unmeasured authority reports UNKNOWN, never a fabricated effect."""

    legacy_flag = (
        screen.model_extra.get('acoustically_transparent')
        if screen.model_extra
        else None
    )
    transfer = None
    if screen.screen_transfer_ref is not None and screen_transfers is not None:
        ref = screen.screen_transfer_ref
        transfer = screen_transfers.get(
            getattr(ref, 'authority_id', None) or ref.get('authority_id')
        )
    if screen.screen_transfer_ref is not None and transfer is None:
        return (
            'UNKNOWN',
            'a screen-transfer authority is bound but could not be resolved '
            '— no acoustic effect is assumed',
        )
    if transfer is None:
        if legacy_flag is not None:
            return (
                'UNKNOWN',
                'legacy acoustically_transparent flag present but ignored — '
                'bind an AcousticScreenTransferAuthority for acoustic truth',
            )
        return (
            'UNKNOWN',
            'no screen-transfer authority bound — the screen acoustic effect '
            'is unknown',
        )
    tier = transfer.capability_tier
    if tier == 'UNKNOWN':
        return (
            'UNKNOWN',
            f'transfer authority {transfer.transfer_id} declares no transfer '
            'capability',
        )
    if tier == 'AT_CLAIM':
        return (
            'UNKNOWN',
            f'transfer authority {transfer.transfer_id} is an unmeasured AT '
            'claim — not acoustic truth',
        )
    return (
        'PASS',
        f'screen transfer authority {transfer.transfer_id} bound '
        f'(tier {tier}) — evidence available for acoustic propagation models',
    )


def evaluate_video_geometry(
    *,
    baseline: SceneRevision,
    variant: SystemVariant | None,
    projector_specification: ProjectorSpecification,
    request: VideoGeometryRequest,
    screen_transfers: Mapping[str, Any] | None = None,
) -> VideoGeometryEvaluation:
    """Evaluate one exact SceneRevision/SystemVariant without mutating scene truth."""

    if (
        request.projector_specification_id
        != projector_specification.specification_id
        or request.projector_specification_version != projector_specification.version
        or request.projector_specification_sha256
        != projector_specification.specification_sha256
    ):
        raise ValueError('request projector specification binding mismatch')
    target, scene = _target_and_scene(baseline=baseline, variant=variant)
    _validate_scene_bindings(scene=scene, request=request)
    projector = scene.entity(request.projector_entity_id)
    screen_entity = scene.entity(request.screen.entity_id)
    projection = _projection_result(
        projector_entity=projector,
        screen_entity=screen_entity,
        specification=projector_specification,
        request=request,
    )
    center, right, up, _normal, _corners = _screen_frame(screen_entity, request.screen)
    viewing = tuple(
        _viewing_result(
            seat_entity=scene.entity(binding.entity_id),
            binding=binding,
            center=center,
            right=right,
            up=up,
            width_m=request.screen.visible_width_m,
            height_m=request.screen.visible_height_m,
            policy=request.policy,
        )
        for binding in request.seats
    )
    sightlines = _sightline_results(
        scene=scene,
        bindings=request.seats,
        center=center,
        right=right,
        up=up,
        width_m=request.screen.visible_width_m,
        height_m=request.screen.visible_height_m,
        policy=request.policy,
    )
    risers = _riser_results(
        scene=scene,
        bindings=request.seats,
        policy=request.policy,
    )
    collisions = _collision_results(
        scene=scene,
        collision_entity_ids=request.collision_entity_ids,
        surface_entity_id=request.screen.entity_id,
        surface_frame_clearance_m=request.screen.frame_clearance_m,
        policy=request.policy,
    )
    geometry_status = _combine_status((
        projection.status,
        *(
            status
            for item in viewing
            for status in (
                item.horizontal_status,
                item.vertical_status,
                item.center_elevation_status,
            )
        ),
        *(item.status for item in sightlines),
        *(item.status for item in risers),
        *(item.status for item in collisions),
    ))
    acoustic_status, acoustic_reason = _screen_acoustic_effect(
        request.screen, screen_transfers
    )

    identity = {
        'schema_version': VIDEO_GEOMETRY_SCHEMA_VERSION,
        'authority_version': VIDEO_GEOMETRY_AUTHORITY_VERSION,
        'target': target.model_dump(mode='json'),
        'request': request.model_dump(mode='json'),
        'projector_specification_sha256': projector_specification.specification_sha256,
        'projection': projection.model_dump(mode='json'),
        'viewing': [item.model_dump(mode='json') for item in viewing],
        'sightlines': [item.model_dump(mode='json') for item in sightlines],
        'risers': [item.model_dump(mode='json') for item in risers],
        'collisions': [_collision_payload(item) for item in collisions],
        'geometry_status': geometry_status,
        'screen_acoustic_effect_status': acoustic_status,
        'screen_acoustic_effect_reason': acoustic_reason,
    }
    digest = _digest(identity)
    return VideoGeometryEvaluation(
        evaluation_id='vge-' + digest[:24],
        target=target,
        request=request,
        projector_specification_sha256=projector_specification.specification_sha256,
        projection=projection,
        viewing=viewing,
        sightlines=sightlines,
        risers=risers,
        collisions=collisions,
        geometry_status=geometry_status,
        screen_acoustic_effect_status=acoustic_status,
        screen_acoustic_effect_reason=acoustic_reason,
        evaluation_sha256=digest,
    )
