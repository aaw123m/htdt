from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import product
import json
from math import atan2, cos, degrees, isfinite, radians, sin, sqrt
from typing import Any, Callable, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_adaptive_planner import production_validation_ready
from .cad_constraint_models import CadConstraintSet
from .cad_document import EditStateError, WorkingDocument
from .cad_model_validation import CadModelValidationRecord
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import (
    Direction3,
    Position3,
    SceneDocument,
    quaternion_to_euler_deg,
    rotate_orientation_world,
)
from .cad_search import (
    candidate_preview_document,
    iter_cad_candidate_pages,
    search_spec_current_working,
)
from .cad_search_models import CadCandidate, CadSearchSpec


EXTENDED_SEARCH_SCHEMA_VERSION = 1
EXTENDED_SEARCH_ALGORITHM_VERSION = 'extended-grid-1'
EXTENDED_SEARCH_SYSTEM_MAX_CANDIDATES = 50_000
ExtendedParameter = Literal['aim_yaw_deg', 'body_yaw_deg']
ExtendedEvidenceScope = Literal['synthetic_fixture', 'owned_room']


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


def _grid_decimal(value: float) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'invalid extended-search grid value: {value}') from exc


def _grid_values(low: float, high: float, step: float) -> tuple[float, ...]:
    low_d, high_d, step_d = (
        _grid_decimal(low),
        _grid_decimal(high),
        _grid_decimal(step),
    )
    values: list[float] = []
    index = 0
    while True:
        value = low_d + step_d * index
        if value > high_d:
            break
        values.append(float(value))
        index += 1
        if index > EXTENDED_SEARCH_SYSTEM_MAX_CANDIDATES:
            raise ValueError('extended-search axis exceeds the system candidate limit')
    if not values:
        raise ValueError('extended-search axis produced no values')
    return tuple(values)


class CadExtendedModelCapability(BaseModel):
    """Explicit model capability gate for parameters not covered by base O10."""

    model_config = ConfigDict(frozen=True)

    capability_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    evidence_scope: ExtendedEvidenceScope
    supported_parameters: tuple[ExtendedParameter, ...] = Field(min_length=1)
    validation_id: str | None = Field(default=None, min_length=1)
    detail: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_capability(self) -> 'CadExtendedModelCapability':
        if len(self.supported_parameters) != len(set(self.supported_parameters)):
            raise ValueError('extended capability parameters must be unique')
        if self.evidence_scope == 'owned_room' and self.validation_id is None:
            raise ValueError('owned-room extended capability requires ValidationRecord')
        if self.evidence_scope == 'synthetic_fixture' and self.validation_id is not None:
            raise ValueError('synthetic extended capability must not claim owned-room validation')
        if (
            {'aim_yaw_deg', 'body_yaw_deg'}.intersection(self.supported_parameters)
            and self.model_id in {'rew-room-simulator', 'rew-roomsim'}
        ):
            raise ValueError(
                'REW Room Simulator does not model speaker acoustic direction; '
                'aim/body yaw capability is forbidden'
            )
        if self.capability_sha256 != _digest(self.identity_payload()):
            raise ValueError('extended capability identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'model_id': self.model_id,
            'model_version': self.model_version,
            'evidence_scope': self.evidence_scope,
            'supported_parameters': list(self.supported_parameters),
            'validation_id': self.validation_id,
            'detail': self.detail,
        }


class CadExtendedSearchAxis(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    parameter: ExtendedParameter = 'aim_yaw_deg'
    min_value: float
    max_value: float
    step: float = Field(gt=0.0)

    @model_validator(mode='after')
    def valid_axis(self) -> 'CadExtendedSearchAxis':
        values = (self.min_value, self.max_value, self.step)
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('extended-search axis values must be finite')
        if self.max_value < self.min_value:
            raise ValueError('extended-search axis max must be >= min')
        if self.min_value < -180.0 or self.max_value > 180.0:
            raise ValueError('extended yaw parameters must remain within -180..180 degrees')
        return self


class CadExtendedSearchSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = EXTENDED_SEARCH_SCHEMA_VERSION
    extended_search_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    base_search_spec_id: str = Field(min_length=1)
    base_search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    capability_id: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    axes: tuple[CadExtendedSearchAxis, ...] = Field(min_length=1)
    candidate_limit: int = Field(ge=1, le=EXTENDED_SEARCH_SYSTEM_MAX_CANDIDATES)
    algorithm_version: Literal['extended-grid-1'] = EXTENDED_SEARCH_ALGORITHM_VERSION
    created_at_utc: str = Field(min_length=1)
    extended_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadExtendedSearchSpec':
        keys = [(item.entity_id, item.parameter) for item in self.axes]
        if len(keys) != len(set(keys)):
            raise ValueError('extended-search axes must be unique')
        if self.extended_search_sha256 != _digest(self.identity_payload()):
            raise ValueError('extended-search spec identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'base_search_spec_id': self.base_search_spec_id,
            'base_search_spec_sha256': self.base_search_spec_sha256,
            'base_candidate_set_sha256': self.base_candidate_set_sha256,
            'capability_id': self.capability_id,
            'capability_sha256': self.capability_sha256,
            'axes': [item.model_dump(mode='json') for item in self.axes],
            'candidate_limit': self.candidate_limit,
            'algorithm_version': self.algorithm_version,
        }


class CadExtendedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    base_candidate_id: str = Field(min_length=1)
    raw_index: int = Field(ge=0)
    feasible_index: int = Field(ge=0)
    positions: dict[str, dict[str, float]]
    aim_yaw_deg: dict[str, float] = Field(default_factory=dict)
    body_yaw_deg: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid_payload(self) -> 'CadExtendedCandidate':
        for entity_id, position in self.positions.items():
            if not entity_id or set(position) != {'x_m', 'y_m', 'z_m'}:
                raise ValueError('extended candidate position payload is invalid')
            if not all(isfinite(float(value)) for value in position.values()):
                raise ValueError('extended candidate positions must be finite')
        if not self.aim_yaw_deg and not self.body_yaw_deg:
            raise ValueError('extended candidate requires at least one yaw override')
        for mapping, label in (
            (self.aim_yaw_deg, 'aim yaw'),
            (self.body_yaw_deg, 'body yaw'),
        ):
            for entity_id, value in mapping.items():
                if (
                    not entity_id
                    or not isfinite(float(value))
                    or not -180 <= float(value) <= 180
                ):
                    raise ValueError(f'extended candidate {label} is invalid')
        return self


class CadExtendedCandidateSetPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    extended_search_id: str = Field(min_length=1)
    extended_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_candidate_count: int = Field(ge=1)
    feasible_candidate_count: int = Field(ge=0)
    rejected_candidate_count: int = Field(default=0, ge=0)
    rejection_counts: dict[str, int] = Field(default_factory=dict)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    candidates: tuple[CadExtendedCandidate, ...]


def build_extended_model_capability(
    *,
    model_id: str,
    model_version: str,
    evidence_scope: ExtendedEvidenceScope,
    supported_parameters: Sequence[ExtendedParameter],
    detail: str,
    validation: CadModelValidationRecord | None = None,
    created_at_utc: str,
) -> CadExtendedModelCapability:
    parameters = tuple(dict.fromkeys(supported_parameters))
    if not parameters:
        raise ValueError('extended capability requires supported parameters')
    validation_id = None
    if evidence_scope == 'owned_room':
        if validation is None or not production_validation_ready(validation):
            raise ValueError(
                'owned-room extended capability requires an eligible O60 ValidationRecord'
            )
        if validation.model_id != model_id or validation.model_version != model_version:
            raise ValueError('extended capability model does not match ValidationRecord')
        validation_id = validation.validation_id
    elif validation is not None:
        raise ValueError('synthetic extended capability must not reference ValidationRecord')

    identity = {
        'model_id': model_id,
        'model_version': model_version,
        'evidence_scope': evidence_scope,
        'supported_parameters': list(parameters),
        'validation_id': validation_id,
        'detail': detail,
    }
    return CadExtendedModelCapability(
        capability_id=str(uuid4()),
        model_id=model_id,
        model_version=model_version,
        evidence_scope=evidence_scope,
        supported_parameters=parameters,
        validation_id=validation_id,
        detail=detail,
        created_at_utc=created_at_utc,
        capability_sha256=_digest(identity),
    )


def _source_speaker_with_aim(
    revision: SceneRevision,
    entity_id: str,
):
    entity = revision.document.entity(entity_id)
    if entity.kind != 'speaker':
        raise ValueError(f'extended aim axis requires a speaker: {entity_id}')
    if entity.aim_xyz is None:
        raise ValueError(
            f'extended aim axis requires explicit speaker aim before search: {entity_id}'
        )
    horizontal = sqrt(entity.aim_xyz.x * entity.aim_xyz.x + entity.aim_xyz.y * entity.aim_xyz.y)
    if horizontal <= 1e-9:
        raise ValueError(f'extended aim axis cannot rotate a vertical-only aim: {entity_id}')
    return entity


def build_extended_search_spec(
    *,
    source_revision: SceneRevision,
    base_spec: CadSearchSpec,
    base_candidate_set_sha256: str,
    base_candidate_count: int,
    capability: CadExtendedModelCapability,
    axes: Sequence[CadExtendedSearchAxis],
    candidate_limit: int = 50_000,
    created_at_utc: str,
) -> CadExtendedSearchSpec:
    if (
        base_spec.document_id != source_revision.document_id
        or base_spec.scene_revision_id != source_revision.revision_id
        or base_spec.scene_content_hash != source_revision.content_hash
    ):
        raise ValueError('extended search base SearchSpec source authority mismatch')
    ordered = tuple(sorted(axes, key=lambda item: (item.entity_id, item.parameter)))
    if not ordered:
        raise ValueError('extended search requires at least one axis')
    for axis in ordered:
        if axis.parameter not in capability.supported_parameters:
            raise ValueError(
                f'extended model capability does not support {axis.parameter}'
            )
        _source_speaker_with_aim(source_revision, axis.entity_id)

    raw_count = int(base_candidate_count)
    for axis in ordered:
        raw_count *= len(_grid_values(axis.min_value, axis.max_value, axis.step))
        if raw_count > candidate_limit:
            raise ValueError(
                f'extended raw candidate estimate {raw_count} exceeds '
                f'candidate_limit {candidate_limit}'
            )
    if raw_count < 1:
        raise ValueError('extended search requires at least one base candidate')

    identity = {
        'schema_version': 1,
        'document_id': source_revision.document_id,
        'base_search_spec_id': base_spec.search_spec_id,
        'base_search_spec_sha256': base_spec.search_spec_sha256,
        'base_candidate_set_sha256': base_candidate_set_sha256,
        'capability_id': capability.capability_id,
        'capability_sha256': capability.capability_sha256,
        'axes': [item.model_dump(mode='json') for item in ordered],
        'candidate_limit': int(candidate_limit),
        'algorithm_version': EXTENDED_SEARCH_ALGORITHM_VERSION,
    }
    return CadExtendedSearchSpec(
        extended_search_id=str(uuid4()),
        document_id=source_revision.document_id,
        base_search_spec_id=base_spec.search_spec_id,
        base_search_spec_sha256=base_spec.search_spec_sha256,
        base_candidate_set_sha256=base_candidate_set_sha256,
        capability_id=capability.capability_id,
        capability_sha256=capability.capability_sha256,
        axes=ordered,
        candidate_limit=int(candidate_limit),
        algorithm_version=EXTENDED_SEARCH_ALGORITHM_VERSION,
        created_at_utc=created_at_utc,
        extended_search_sha256=_digest(identity),
    )


def _all_base_candidates(
    scene_repository: SceneRepository,
    base_spec: CadSearchSpec,
    expected_candidate_set_sha256: str,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[CadCandidate, ...]:
    result: list[CadCandidate] = []
    for page in iter_cad_candidate_pages(
        scene_repository,
        base_spec,
        cancelled=cancelled,
    ):
        if page.candidate_set_sha256 != expected_candidate_set_sha256:
            raise ValueError('extended search base candidate-set authority mismatch')
        result.extend(page.candidates)
    if not result:
        raise ValueError('extended search base SearchSpec has no feasible candidates')
    return tuple(result)


def _candidate_id(
    spec: CadExtendedSearchSpec,
    base_candidate_id: str,
    positions: dict[str, dict[str, float]],
    aim_yaw_deg: dict[str, float],
    body_yaw_deg: dict[str, float],
) -> str:
    payload = {
        'extended_search_sha256': spec.extended_search_sha256,
        'base_candidate_id': base_candidate_id,
        'positions': positions,
        'aim_yaw_deg': aim_yaw_deg,
    }
    # Preserve candidate IDs for pre-O80P aim-only Extended Search specs.
    if body_yaw_deg:
        payload['body_yaw_deg'] = body_yaw_deg
    return 'ec-' + _digest(payload)[:20]


def generate_extended_candidates(
    scene_repository: SceneRepository,
    base_spec: CadSearchSpec,
    spec: CadExtendedSearchSpec,
    *,
    offset: int = 0,
    limit: int = 100,
    cancelled: Callable[[], bool] | None = None,
) -> CadExtendedCandidateSetPage:
    if offset < 0:
        raise ValueError('extended search offset must be >= 0')
    if limit < 1 or limit > 500:
        raise ValueError('extended search limit must be between 1 and 500')
    if (
        base_spec.search_spec_id != spec.base_search_spec_id
        or base_spec.search_spec_sha256 != spec.base_search_spec_sha256
        or base_spec.document_id != spec.document_id
    ):
        raise ValueError('extended search base SearchSpec authority mismatch')

    base_candidates = _all_base_candidates(
        scene_repository,
        base_spec,
        spec.base_candidate_set_sha256,
        cancelled=cancelled,
    )
    value_lists = [
        _grid_values(axis.min_value, axis.max_value, axis.step)
        for axis in spec.axes
    ]
    raw_count = len(base_candidates)
    for values in value_lists:
        raw_count *= len(values)
    if raw_count > spec.candidate_limit:
        raise ValueError('extended search raw count exceeds immutable candidate limit')

    source = scene_repository.get(base_spec.scene_revision_id)
    if source is None:
        raise ValueError('extended search source SceneRevision no longer exists')
    constraint_set = CadConstraintSet.model_validate(
        json.loads(base_spec.constraint_snapshot_json)
    )

    ids: list[str] = []
    page_candidates: list[CadExtendedCandidate] = []
    rejection_counts: dict[str, int] = {}
    feasible_index = 0
    raw_index = 0
    for base_candidate in base_candidates:
        for combination in product(*value_lists):
            if cancelled is not None and cancelled():
                raise RuntimeError('extended search generation cancelled')
            aim_map: dict[str, float] = {}
            body_map: dict[str, float] = {}
            for axis, value in zip(spec.axes, combination, strict=True):
                target = (
                    aim_map if axis.parameter == 'aim_yaw_deg' else body_map
                )
                target[axis.entity_id] = round(float(value), 12)

            candidate_id = _candidate_id(
                spec,
                base_candidate.candidate_id,
                base_candidate.positions,
                aim_map,
                body_map,
            )
            candidate = CadExtendedCandidate(
                candidate_id=candidate_id,
                base_candidate_id=base_candidate.candidate_id,
                raw_index=raw_index,
                feasible_index=feasible_index,
                positions=base_candidate.positions,
                aim_yaw_deg=aim_map,
                body_yaw_deg=body_map,
            )
            if body_map:
                preview = extended_candidate_preview_document(
                    source.document,
                    candidate,
                )
                rejections = orientation_constraint_rejections(
                    preview,
                    constraint_set,
                    changed_entity_ids=body_map,
                )
                if rejections:
                    for constraint_id in rejections:
                        rejection_counts[constraint_id] = (
                            rejection_counts.get(constraint_id, 0) + 1
                        )
                    raw_index += 1
                    continue

            ids.append(candidate_id)
            if offset <= feasible_index < offset + limit:
                page_candidates.append(candidate)
            feasible_index += 1
            raw_index += 1

    return CadExtendedCandidateSetPage(
        extended_search_id=spec.extended_search_id,
        extended_search_sha256=spec.extended_search_sha256,
        candidate_set_sha256=_digest(ids),
        raw_candidate_count=raw_count,
        feasible_candidate_count=feasible_index,
        rejected_candidate_count=raw_count - feasible_index,
        rejection_counts=dict(sorted(rejection_counts.items())),
        offset=offset,
        limit=limit,
        candidates=tuple(page_candidates),
    )


def aim_horizontal_yaw_deg(direction: Direction3) -> float:
    horizontal = sqrt(direction.x * direction.x + direction.y * direction.y)
    if horizontal <= 1e-9:
        raise ValueError('vertical-only aim has no horizontal yaw')
    return degrees(atan2(direction.x, direction.y))


def direction_with_horizontal_yaw(
    direction: Direction3,
    yaw_deg: float,
) -> Direction3:
    horizontal = sqrt(direction.x * direction.x + direction.y * direction.y)
    if horizontal <= 1e-9:
        raise ValueError('vertical-only aim cannot receive horizontal yaw')
    angle = radians(float(yaw_deg))
    return Direction3(
        x=horizontal * sin(angle),
        y=horizontal * cos(angle),
        z=float(direction.z),
    )


def body_horizontal_yaw_deg(entity) -> float:
    return float(quaternion_to_euler_deg(entity.orientation)[0])


def _wrapped_delta_deg(target_deg: float, current_deg: float) -> float:
    return (float(target_deg) - float(current_deg) + 180.0) % 360.0 - 180.0


def _apply_body_yaw(entity, target_yaw_deg: float):
    if entity.kind != 'speaker' or entity.aim_xyz is None:
        raise ValueError('physical toe-in requires an explicit-aim speaker')
    delta = _wrapped_delta_deg(target_yaw_deg, body_horizontal_yaw_deg(entity))
    current_aim_yaw = aim_horizontal_yaw_deg(entity.aim_xyz)
    return entity.model_copy(update={
        'orientation': rotate_orientation_world(
            entity.orientation,
            'z',
            delta,
        ),
        'aim_xyz': direction_with_horizontal_yaw(
            entity.aim_xyz,
            current_aim_yaw + delta,
        ),
    })


def extended_candidate_preview_document(
    document: SceneDocument,
    candidate: CadExtendedCandidate,
) -> SceneDocument:
    base = CadCandidate(
        candidate_id=candidate.base_candidate_id,
        raw_index=candidate.raw_index,
        feasible_index=candidate.feasible_index,
        positions=candidate.positions,
    )
    preview = candidate_preview_document(document, base)
    replacements = {}

    for entity_id, yaw_deg in candidate.body_yaw_deg.items():
        entity = replacements.get(entity_id, preview.entity(entity_id))
        replacements[entity_id] = _apply_body_yaw(entity, yaw_deg)

    for entity_id, yaw_deg in candidate.aim_yaw_deg.items():
        entity = replacements.get(entity_id, preview.entity(entity_id))
        if entity.kind != 'speaker' or entity.aim_xyz is None:
            raise ValueError(
                f'extended candidate aim target lacks explicit speaker aim: {entity_id}'
            )
        replacements[entity_id] = entity.model_copy(update={
            'aim_xyz': direction_with_horizontal_yaw(entity.aim_xyz, yaw_deg),
        })
    entities = tuple(
        replacements.get(entity.entity_id, entity)
        for entity in preview.entities
    )
    return preview.model_copy(update={'entities': entities})


def apply_extended_candidate(
    working: WorkingDocument,
    candidate: CadExtendedCandidate,
    *,
    extended_spec: CadExtendedSearchSpec,
    base_spec: CadSearchSpec,
    current_constraint_set,
    current_document_id: str | None = None,
) -> bool:
    if working.has_preview:
        raise EditStateError('cannot apply an extended candidate during edit preview')
    if not search_spec_current_working(
        base_spec,
        working,
        current_constraint_set,
        current_document_id=current_document_id,
    ):
        raise ValueError('cannot apply extended candidate from stale base SearchSpec')
    if (
        base_spec.search_spec_id != extended_spec.base_search_spec_id
        or base_spec.search_spec_sha256 != extended_spec.base_search_spec_sha256
    ):
        raise ValueError('extended candidate base SearchSpec mismatch')
    expected_id = _candidate_id(
        extended_spec,
        candidate.base_candidate_id,
        candidate.positions,
        candidate.aim_yaw_deg,
        candidate.body_yaw_deg,
    )
    if expected_id != candidate.candidate_id:
        raise ValueError('extended candidate identity mismatch')

    candidate_document = extended_candidate_preview_document(
        working.committed_document,
        candidate,
    )
    if candidate.body_yaw_deg:
        rejections = orientation_constraint_rejections(
            candidate_document,
            current_constraint_set,
            changed_entity_ids=candidate.body_yaw_deg,
        )
        if rejections:
            raise ValueError(
                'physical toe-in violates hard constraints: '
                + ', '.join(rejections)
            )

    touched = sorted(
        set(candidate.positions)
        | set(candidate.aim_yaw_deg)
        | set(candidate.body_yaw_deg)
    )
    before = tuple(
        working.committed_document.entity(entity_id)
        for entity_id in touched
    )
    after = tuple(
        candidate_document.entity(entity_id)
        for entity_id in touched
    )
    return working.transform_entities(before, after)
