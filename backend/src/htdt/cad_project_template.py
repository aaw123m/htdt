"""Project templates and theater starters (#614).

A :class:`ProjectTemplate` captures *topology intent* — which speaker roles
and listening layout a project should start from, which standards profiles
it aims at, and the measurement-workflow shape — without copying a single
piece of project evidence. Materializing a template produces a fresh
project document (new ``document_id`` identity per #607) seeded with the
declared layout intent; room geometry stays absent until measured or
entered, and no measurement/Capture/serial/prediction data is carried over.

Contract (per the issue):

- topology is *intent*, not truth — geometry guides produced by a template
  remain pending until materialized in the project;
- built-in starters contain no manufacturer data, no room evidence, and no
  display assumptions — a TV-room starter declares a direct-view display
  intent (or stays ``not_configured``); it never fabricates a projector or
  a passive screen;
- editing a template never mutates projects already created from it;
  ``ProjectTemplateInstantiation`` rows record which exact template
  version seeded each document;
- saving a document as a template previews exactly which content is
  included/excluded before persisting.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import (
    Direction3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
)
from .cad_standards_profiles import dolby_atmos_home_5_1_2_profile
from .project_lifecycle import ProjectLibrary


TEMPLATE_SCHEMA_VERSION = 1
TEMPLATE_AUTHORITY_VERSION = 'project-template-1'

ProjectTemplateKind = Literal['builtin', 'user']

#: Display intent in the design brief — never a fabricated device.
TemplateDisplayIntent = Literal['direct_view', 'projection', 'not_configured']
TEMPLATE_DISPLAY_INTENTS: frozenset[str] = frozenset(
    {'direct_view', 'projection', 'not_configured'}
)

#: Coarse project kind — controls which workspaces the starter emphasizes.
TemplateProjectKind = Literal['dedicated_theater', 'living_tv', 'custom']
TEMPLATE_PROJECT_KINDS: frozenset[str] = frozenset(
    {'dedicated_theater', 'living_tv', 'custom'}
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class TemplateAuthorityRef(BaseModel):
    """Exact reference to a standards/profile authority a template targets.

    ``semantic_hash_sha256`` pins the baseline semantics the template was
    authored against (#795): instantiation resolves the live authority and
    reports 'missing' or 'hash_conflict' instead of silently substituting
    whatever happens to be installed. ``coverage`` is declared, honest
    coverage — ``'partial'`` means the template deliberately extends beyond
    what this authority proves.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)  # e.g. 'standards_profile'
    ref_id: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)
    semantic_hash_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    coverage: Literal['full', 'partial'] = 'full'


class TemplateSpeakerSpec(BaseModel):
    """One declared speaker position *intent* — a nominal angle/distance
    guide, not claimed geometry."""

    model_config = ConfigDict(frozen=True)

    speaker_role: str = Field(min_length=1)
    name: str = Field(min_length=1)
    nominal_azimuth_deg: float
    nominal_elevation_deg: float = 0.0
    nominal_distance_m: float = Field(default=2.5, gt=0.0)


class TemplateDesignBrief(BaseModel):
    """The intent-facing brief carried by a template — no evidence."""

    model_config = ConfigDict(frozen=True)

    project_kind: TemplateProjectKind = 'dedicated_theater'
    display_intent: TemplateDisplayIntent = 'not_configured'
    audio_only: bool = False
    notes: str | None = None


class TemplateMeasurementSpec(BaseModel):
    """Measurement-workflow intent: which roles to measure and how many
    listening points a typical campaign should cover."""

    model_config = ConfigDict(frozen=True)

    channel_roles: tuple[str, ...] = ()
    measurement_point_count: int = Field(default=1, ge=0)
    notes: str | None = None

    @model_validator(mode='after')
    def valid_spec(self) -> 'TemplateMeasurementSpec':
        if len(set(self.channel_roles)) != len(self.channel_roles):
            raise ValueError('channel_roles must be unique')
        return self


class ProjectTemplate(BaseModel):
    """Immutable starter: topology + design brief + measurement intent."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = TEMPLATE_SCHEMA_VERSION
    authority_version: Literal['project-template-1'] = TEMPLATE_AUTHORITY_VERSION
    template_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ProjectTemplateKind
    description: str | None = None
    speaker_specs: tuple[TemplateSpeakerSpec, ...] = ()
    design_brief: TemplateDesignBrief = TemplateDesignBrief()
    measurement_spec: TemplateMeasurementSpec | None = None
    #: Exact standards/profile refs the template aims at (#600 vocabulary).
    target_refs: tuple[TemplateAuthorityRef, ...] = ()
    template_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_template(self) -> 'ProjectTemplate':
        roles = [spec.speaker_role for spec in self.speaker_specs]
        if len(set(roles)) != len(roles):
            raise ValueError('speaker roles must be unique')
        if self.template_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectTemplate hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'template_id': self.template_id,
            'version': self.version,
            'name': self.name,
            'kind': self.kind,
            'description': self.description,
            'speaker_specs': [
                spec.model_dump(mode='json') for spec in self.speaker_specs
            ],
            'design_brief': self.design_brief.model_dump(mode='json'),
            'measurement_spec': (
                None
                if self.measurement_spec is None
                else self.measurement_spec.model_dump(mode='json')
            ),
            'target_refs': [
                ref.model_dump(mode='json') for ref in self.target_refs
            ],
        }


def build_project_template(
    *,
    template_id: str,
    version: str,
    name: str,
    kind: ProjectTemplateKind,
    speaker_specs: tuple[TemplateSpeakerSpec, ...] = (),
    design_brief: TemplateDesignBrief | None = None,
    measurement_spec: TemplateMeasurementSpec | None = None,
    target_refs: tuple[TemplateAuthorityRef, ...] = (),
    description: str | None = None,
) -> ProjectTemplate:
    payload: dict[str, Any] = {
        'template_id': template_id,
        'version': version,
        'name': name,
        'kind': kind,
        'description': description,
        'speaker_specs': tuple(speaker_specs),
        'design_brief': (
            design_brief if design_brief is not None else TemplateDesignBrief()
        ),
        'measurement_spec': measurement_spec,
        'target_refs': tuple(target_refs),
    }
    provisional = ProjectTemplate.model_construct(
        **payload, template_sha256='0' * 64
    )
    return ProjectTemplate(
        **payload,
        template_sha256=_hash(provisional.semantic_payload()),
    )


class TemplateSavePreview(BaseModel):
    """Exactly what ``save_document_as_template`` would include/exclude."""

    model_config = ConfigDict(frozen=True)

    included_entity_ids: tuple[str, ...]
    excluded_entity_ids: tuple[str, ...]
    included_fields: tuple[str, ...]
    excluded_fields: tuple[str, ...]


#: Speaker kinds a template may carry — equipment/furniture/measurement
#: points are project evidence and never enter a template.
_TEMPLATE_SPEAKER_KINDS: frozenset[str] = frozenset({'speaker'})

_EXCLUDED_FROM_TEMPLATE: tuple[str, ...] = (
    'measurements',
    'capture_inbox',
    'equipment_bindings',
    'serials',
    'predictions',
    'calibrations',
    'installations',
    'action_items',
    'decisions',
)


def preview_document_as_template(
    document: SceneDocument,
) -> TemplateSavePreview:
    """Preview of save-as-template: speakers' layout intent only."""

    included = tuple(
        entity.entity_id
        for entity in document.entities
        if entity.kind in _TEMPLATE_SPEAKER_KINDS
    )
    excluded = tuple(
        entity.entity_id
        for entity in document.entities
        if entity.kind not in _TEMPLATE_SPEAKER_KINDS
    )
    return TemplateSavePreview(
        included_entity_ids=included,
        excluded_entity_ids=excluded,
        included_fields=('speaker_role', 'name', 'position', 'aim'),
        excluded_fields=_EXCLUDED_FROM_TEMPLATE,
    )


def _listening_center(document: SceneDocument) -> tuple[float, float, float]:
    """Center the layout intent on the MLP if present, else the speaker mean."""

    points = [
        entity
        for entity in document.entities
        if entity.kind == 'measurement_point'
    ]
    if points:
        origin = points[0].position
        return (origin.x_m, origin.y_m, origin.z_m)
    speakers = [entity for entity in document.entities if entity.kind == 'speaker']
    if not speakers:
        return (0.0, 0.0, 0.0)
    count = len(speakers)
    return (
        sum(e.position.x_m for e in speakers) / count,
        sum(e.position.y_m for e in speakers) / count,
        sum(e.position.z_m for e in speakers) / count,
    )


def _intent_spec(entity: SceneEntity, center: tuple[float, float, float]) -> TemplateSpeakerSpec:
    """Re-express an actual entity as nominal azimuth/elevation/distance intent."""

    import math

    dx = entity.position.x_m - center[0]
    # +y is rear in the scene frame; front-relative intent uses -dy.
    dy = -(entity.position.y_m - center[1])
    dz = entity.position.z_m - center[2]
    distance = math.sqrt(dx * dx + dy * dy)
    azimuth = math.degrees(math.atan2(dx, dy)) if distance > 0 else 0.0
    total = math.sqrt(dx * dx + dy * dy + dz * dz)
    elevation = (
        math.degrees(math.asin(dz / total)) if total > 0 else 0.0
    )
    return TemplateSpeakerSpec(
        speaker_role=entity.speaker_role or entity.entity_id,
        name=entity.name or entity.entity_id,
        nominal_azimuth_deg=azimuth,
        nominal_elevation_deg=elevation,
        nominal_distance_m=total if total > 0 else 1.0,
    )


def save_document_as_template(
    document: SceneDocument,
    *,
    template_id: str,
    version: str,
    name: str,
    description: str | None = None,
    design_brief: TemplateDesignBrief | None = None,
    measurement_spec: TemplateMeasurementSpec | None = None,
    target_refs: tuple[TemplateAuthorityRef, ...] = (),
) -> ProjectTemplate:
    """Capture a document's speaker layout as a user template.

    Copies *positions as nominal intent* — the role/name/relative geometry —
    and nothing else: no measurements, bindings, capture items, serials or
    predictions can ever travel into a template.
    """

    center = _listening_center(document)
    speaker_specs = tuple(
        _intent_spec(entity, center)
        for entity in document.entities
        if entity.kind in _TEMPLATE_SPEAKER_KINDS
    )
    return build_project_template(
        template_id=template_id,
        version=version,
        name=name,
        kind='user',
        speaker_specs=speaker_specs,
        design_brief=design_brief,
        measurement_spec=measurement_spec,
        target_refs=target_refs,
        description=description,
    )


def _nominal_position(spec: TemplateSpeakerSpec) -> Position3:
    """Intent-frame position: MLP at origin, x right / y front / z up."""

    import math

    azimuth = math.radians(spec.nominal_azimuth_deg)
    elevation = math.radians(spec.nominal_elevation_deg)
    distance = spec.nominal_distance_m
    return Position3(
        x_m=distance * math.sin(azimuth) * math.cos(elevation),
        y_m=-distance * math.cos(azimuth) * math.cos(elevation),
        z_m=1.1 + distance * math.sin(elevation),
    )


def materialize_template_scene(
    template: ProjectTemplate,
    *,
    document_id: str,
) -> SceneDocument:
    """Seed a fresh project document from template intent.

    #795: a new project carries *no physical entities* — the layout intent
    lives inside the template and the instantiation record until the
    operator explicitly materializes it on a concrete room via
    :func:`materialize_template_layout`. ``room`` stays ``None``: a
    template never invents room evidence.
    """

    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=None,
        entities=(),
    )


def materialize_template_layout(
    template: ProjectTemplate,
    *,
    listener_position: Position3,
) -> tuple[SceneEntity, ...]:
    """Explicit layout materialization onto a concrete room (#795).

    This is an operator action, not a creation-time seed: the caller
    declares the listening position (the project MLP intent) and receives
    speaker entities at the template's nominal positions with aims pointed
    at that listener — never a fabricated measurement point, never a
    constant aim vector. The caller assigns the entities into the scene
    only when the document's room is concrete.
    """

    def _aim(position: Position3) -> Direction3:
        dx = listener_position.x_m - position.x_m
        dy = listener_position.y_m - position.y_m
        dz = listener_position.z_m - position.z_m
        length = (dx * dx + dy * dy + dz * dz) ** 0.5
        if length < 1e-9:
            # Coincident with the listener: aim toward the screen (-Y).
            return Direction3(x=0.0, y=-1.0, z=0.0)
        return Direction3(x=dx / length, y=dy / length, z=dz / length)

    return tuple(
        SceneEntity(
            entity_id=f'spk-{uuid4().hex[:12]}',
            kind='speaker',
            name=spec.name,
            speaker_role=spec.speaker_role,
            position=_nominal_position(spec),
            size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.5),
            aim_xyz=_aim(_nominal_position(spec)),
        )
        for spec in template.speaker_specs
    )


class ProjectTemplateInstantiation(BaseModel):
    """Provenance record: which exact template seeded which document."""

    model_config = ConfigDict(frozen=True)

    instantiation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    #: Project Library identity the document was registered under (#795).
    project_id: str | None = None
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    template_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: Baseline refs that did not resolve at instantiation time (#795).
    unresolved_refs: tuple[TemplateAuthorityRef, ...] = ()
    created_at_utc: str = Field(min_length=1)
    instantiation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_instantiation(self) -> 'ProjectTemplateInstantiation':
        if self.instantiation_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectTemplateInstantiation hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'instantiation_id': self.instantiation_id,
            'document_id': self.document_id,
            'project_id': self.project_id,
            'template_id': self.template_id,
            'template_version': self.template_version,
            'template_sha256': self.template_sha256,
            'unresolved_refs': [
                ref.model_dump(mode='json') for ref in self.unresolved_refs
            ],
            'created_at_utc': self.created_at_utc,
        }


class ResolvedTargetRef(BaseModel):
    """Instantiation-time resolution of one baseline authority ref (#795)."""

    model_config = ConfigDict(frozen=True)

    ref: TemplateAuthorityRef
    status: Literal['resolved', 'missing', 'hash_conflict']
    detail: str | None = None


def resolve_template_target_refs(
    template: ProjectTemplate,
    standards_repository,
) -> tuple[ResolvedTargetRef, ...]:
    """Resolve a template's baseline refs against canonical authorities."""

    resolved: list[ResolvedTargetRef] = []
    for ref in template.target_refs:
        status: Literal['resolved', 'missing', 'hash_conflict'] = 'resolved'
        detail: str | None = None
        if ref.kind != 'standards_profile' or standards_repository is None:
            status, detail = 'missing', f'no canonical source for kind {ref.kind}'
        else:
            profile = standards_repository.get_profile(ref.ref_id, ref.version)
            if profile is None:
                status, detail = 'missing', 'authority not installed'
            elif (
                ref.semantic_hash_sha256 is not None
                and profile.profile_semantic_hash != ref.semantic_hash_sha256
            ):
                status = 'hash_conflict'
                detail = 'installed authority semantics differ from the template pin'
        resolved.append(
            ResolvedTargetRef(ref=ref, status=status, detail=detail)
        )
    return tuple(resolved)


def create_project_from_template(
    scene_repository,
    template: ProjectTemplate,
    *,
    library: ProjectLibrary,
    display_name: str = '',
    document_id: str | None = None,
    created_at_utc: str,
    standards_repository=None,
    instantiation_repository=None,
) -> tuple[str, ProjectTemplateInstantiation]:
    """Create a project through the canonical project lifecycle (#795).

    Project identity comes from the Project Library — ``project_id`` is
    allocated there and recorded on the instantiation provenance, so a
    template-seeded project is a real project, not an orphaned document.
    Baseline refs resolve against the standards repository and unresolved
    ones land on the instantiation record; the scene seeds intent only
    (no entities — layout materialization is an explicit later action).
    """

    document_id = document_id or str(uuid4())
    if scene_repository.latest(document_id) is not None:
        raise ValueError(f'document_id {document_id} already exists')
    record = library.register_project(document_id, display_name=display_name)
    scene = materialize_template_scene(template, document_id=document_id)
    scene_repository.save(scene, parent_revision_id=None)
    resolutions = resolve_template_target_refs(template, standards_repository)
    unresolved = tuple(
        item.ref for item in resolutions if item.status != 'resolved'
    )
    payload: dict[str, Any] = {
        'instantiation_id': str(uuid4()),
        'document_id': document_id,
        'project_id': record.project_id,
        'template_id': template.template_id,
        'template_version': template.version,
        'template_sha256': template.template_sha256,
        'unresolved_refs': unresolved,
        'created_at_utc': created_at_utc,
    }
    provisional = ProjectTemplateInstantiation.model_construct(
        **payload, instantiation_sha256='0' * 64
    )
    instantiation = ProjectTemplateInstantiation(
        **payload,
        instantiation_sha256=_hash(provisional.semantic_payload()),
    )
    if instantiation_repository is not None:
        instantiation_repository.save_instantiation(instantiation)
    return document_id, instantiation


# -- built-in starters ------------------------------------------------------


def _dolby_5_1_2_ref() -> TemplateAuthorityRef:
    """Pinned reference to the canonical Dolby 5.1.2 profile (#795).

    Coverage is honestly ``'partial'``: the 5.1.2 profile proves bed-layer
    nominal angles only — the 4 top channels and extra surrounds in the
    5.1.4/7.1.4 starters extend beyond it by declaration.
    """

    profile = dolby_atmos_home_5_1_2_profile()
    return TemplateAuthorityRef(
        kind='standards_profile',
        ref_id=profile.profile_id,
        version=profile.version,
        semantic_hash_sha256=profile.profile_semantic_hash,
        coverage='partial',
    )


def _speaker(role: str, name: str, azimuth: float, elevation: float = 0.0) -> TemplateSpeakerSpec:
    return TemplateSpeakerSpec(
        speaker_role=role,
        name=name,
        nominal_azimuth_deg=azimuth,
        nominal_elevation_deg=elevation,
    )


def theater_5_1_4_template() -> ProjectTemplate:
    """Built-in 5.1.4 dedicated-theater starter (Dolby nominal angles)."""

    return build_project_template(
        template_id='builtin-theater-5.1.4',
        version='1',
        name='Theater 5.1.4',
        kind='builtin',
        description='5 bed channels + sub + 4 height channels, nominal Dolby layout intent.',
        speaker_specs=(
            _speaker('FL', 'Front Left', -30.0),
            _speaker('C', 'Center', 0.0),
            _speaker('FR', 'Front Right', 30.0),
            _speaker('SL', 'Surround Left', -110.0),
            _speaker('SR', 'Surround Right', 110.0),
            _speaker('TFL', 'Top Front Left', -45.0, 45.0),
            _speaker('TFR', 'Top Front Right', 45.0, 45.0),
            _speaker('TRL', 'Top Rear Left', -135.0, 45.0),
            _speaker('TRR', 'Top Rear Right', 135.0, 45.0),
            _speaker('LFE', 'Subwoofer', 0.0),
        ),
        design_brief=TemplateDesignBrief(project_kind='dedicated_theater'),
        measurement_spec=TemplateMeasurementSpec(
            channel_roles=(
                'FL', 'C', 'FR', 'SL', 'SR',
                'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
            ),
            measurement_point_count=3,
        ),
        target_refs=(
            _dolby_5_1_2_ref(),
        ),
    )


def theater_7_1_4_template() -> ProjectTemplate:
    """Built-in 7.1.4 dedicated-theater starter."""

    return build_project_template(
        template_id='builtin-theater-7.1.4',
        version='1',
        name='Theater 7.1.4',
        kind='builtin',
        description='7 bed channels + sub + 4 height channels, nominal Dolby layout intent.',
        speaker_specs=(
            _speaker('FL', 'Front Left', -30.0),
            _speaker('C', 'Center', 0.0),
            _speaker('FR', 'Front Right', 30.0),
            _speaker('SL', 'Surround Left', -110.0),
            _speaker('SR', 'Surround Right', 110.0),
            _speaker('SBL', 'Surround Back Left', -150.0),
            _speaker('SBR', 'Surround Back Right', 150.0),
            _speaker('TFL', 'Top Front Left', -45.0, 45.0),
            _speaker('TFR', 'Top Front Right', 45.0, 45.0),
            _speaker('TRL', 'Top Rear Left', -135.0, 45.0),
            _speaker('TRR', 'Top Rear Right', 135.0, 45.0),
            _speaker('LFE', 'Subwoofer', 0.0),
        ),
        design_brief=TemplateDesignBrief(project_kind='dedicated_theater'),
        measurement_spec=TemplateMeasurementSpec(
            channel_roles=(
                'FL', 'C', 'FR', 'SL', 'SR', 'SBL', 'SBR',
                'TFL', 'TFR', 'TRL', 'TRR', 'LFE',
            ),
            measurement_point_count=3,
        ),
        target_refs=(
            _dolby_5_1_2_ref(),
        ),
    )


def tv_room_starter_template() -> ProjectTemplate:
    """Built-in TV-room starter — direct-view display intent only.

    Per the issue contract this starter never fabricates a projector or a
    passive/acoustically-transparent screen: it declares a direct-view
    display intent and leaves screen geometry pending.
    """

    return build_project_template(
        template_id='builtin-tv-room',
        version='1',
        name='TV Room',
        kind='builtin',
        description='Living-room TV starter with a direct-view display intent.',
        speaker_specs=(
            _speaker('FL', 'Front Left', -30.0),
            _speaker('C', 'Center', 0.0),
            _speaker('FR', 'Front Right', 30.0),
        ),
        design_brief=TemplateDesignBrief(
            project_kind='living_tv',
            display_intent='direct_view',
        ),
        measurement_spec=TemplateMeasurementSpec(
            channel_roles=('FL', 'C', 'FR'),
            measurement_point_count=1,
        ),
    )


def builtin_project_templates() -> tuple[ProjectTemplate, ...]:
    """Built-in starters — intent only, no evidence, no devices."""

    return (
        theater_5_1_4_template(),
        theater_7_1_4_template(),
        tv_room_starter_template(),
    )


__all__ = [
    'TEMPLATE_AUTHORITY_VERSION',
    'TEMPLATE_DISPLAY_INTENTS',
    'TEMPLATE_PROJECT_KINDS',
    'TEMPLATE_SCHEMA_VERSION',
    'ProjectTemplate',
    'ProjectTemplateInstantiation',
    'ProjectTemplateKind',
    'ResolvedTargetRef',
    'TemplateAuthorityRef',
    'TemplateDesignBrief',
    'TemplateDisplayIntent',
    'TemplateMeasurementSpec',
    'TemplateProjectKind',
    'TemplateSavePreview',
    'TemplateSpeakerSpec',
    'builtin_project_templates',
    'create_project_from_template',
    'materialize_template_layout',
    'materialize_template_scene',
    'preview_document_as_template',
    'resolve_template_target_refs',
    'save_document_as_template',
    'theater_5_1_4_template',
    'theater_7_1_4_template',
    'tv_room_starter_template',
]
