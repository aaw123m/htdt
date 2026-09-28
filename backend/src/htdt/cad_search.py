from __future__ import annotations

import json
from collections import OrderedDict
from threading import Lock
from typing import Callable, Iterable, Iterator

from .cad_constraint_models import CadConstraintSet
from .cad_constraints import build_g10_constraint_request, scene_to_g10_context
from .cad_document import EditStateError, WorkingDocument
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import Position3, SceneDocument, scene_content_hash
from .cad_search_models import (
    CAD_SEARCH_SCHEMA_VERSION,
    CadCandidate,
    CadCandidateSetPage,
    CadLinkedSearchVariable,
    CadSearchAxis,
    CadSearchSpec,
    canonical_search_json,
    canonical_search_sha256,
    constraint_workspace_snapshot,
    new_search_spec_id,
    search_timestamp_utc,
)
from .placement_constraints import (
    ConstraintSetCreate,
    LinkedPlacementConstraint,
    validate_constraint_set_for_context,
)
from .search_space import (
    MAX_SEARCH_PAGE_SIZE,
    GridAxis,
    LinkedDerivation,
    SearchGenerationCancelled,
    SearchSpecCreate,
    generate_search_space,
    validate_search_spec,
)


def _linked_placement_constraint_payloads(
    linked_variables: Iterable[CadLinkedSearchVariable],
) -> list[dict]:
    return [
        LinkedPlacementConstraint(
            constraint_id=variable.constraint_id,
            kind='linked_placement',
            entity_a=variable.master_entity_id,
            entity_b=variable.slave_entity_id,
            relation=variable.relation,
            mirror_axis_x_m=variable.mirror_axis_x_m,
            tolerance_m=variable.tolerance_m,
        ).model_dump(mode='json')
        for variable in linked_variables
    ]


def _linked_derivations(
    linked_variables: Iterable[CadLinkedSearchVariable],
) -> list[LinkedDerivation]:
    return [
        LinkedDerivation(
            constraint_id=variable.constraint_id,
            master_entity_id=variable.master_entity_id,
        )
        for variable in linked_variables
    ]


def _constraint_engine_spec(
    revision: SceneRevision,
    constraint_set: CadConstraintSet,
    search_entity_ids: Iterable[str] = (),
    linked_variables: Iterable[CadLinkedSearchVariable] = (),
) -> tuple[dict, str]:
    context = scene_to_g10_context(revision.document)
    request = build_g10_constraint_request(
        revision.document,
        constraint_set,
        additional_entity_ids=search_entity_ids,
    )
    linked_payloads = _linked_placement_constraint_payloads(linked_variables)
    if linked_payloads:
        payload = request.model_dump(mode='json')
        existing = {item['constraint_id'] for item in payload['constraints']}
        for item in linked_payloads:
            if item['constraint_id'] in existing:
                raise ValueError(
                    'linked search variable constraint_id collides with a '
                    f"placement constraint: {item['constraint_id']}"
                )
        payload['constraints'].extend(linked_payloads)
        request = ConstraintSetCreate.model_validate(payload)
    stored = validate_constraint_set_for_context(request, context)
    return stored, canonical_search_sha256(stored)


def build_cad_search_spec(
    revision: SceneRevision,
    constraint_set: CadConstraintSet,
    axes: Iterable[CadSearchAxis],
    *,
    candidate_limit: int = 10_000,
    name: str | None = None,
    linked_variables: Iterable[CadLinkedSearchVariable] = (),
) -> tuple[CadSearchSpec, dict]:
    """Create an immutable native SearchSpec while reusing O10 only as an algorithm service.

    ``linked_variables`` declares explicit pair/group relations (mirror,
    matched axis, shared delta); each rule is compiled into the
    constraint-engine spec as a ``linked_placement`` constraint and into the
    O10 spec as a linked derivation, so slave axes do not expand the
    independent search dimensionality.
    """

    if constraint_set.document_id != revision.document_id:
        raise ValueError('SearchSpec constraint workspace belongs to another document')

    native_axes = tuple(axes)
    if not native_axes:
        raise ValueError('SearchSpec requires at least one axis')
    native_links = tuple(linked_variables)

    constraint_snapshot_json, constraint_workspace_hash = constraint_workspace_snapshot(constraint_set)
    engine_spec, engine_sha = _constraint_engine_spec(
        revision,
        constraint_set,
        {axis.entity_id for axis in native_axes}
        | {variable.master_entity_id for variable in native_links}
        | {variable.slave_entity_id for variable in native_links},
        native_links,
    )
    context = scene_to_g10_context(revision.document)
    synthetic_constraint_id = f'cad-constraints:{constraint_workspace_hash[:20]}'
    request = SearchSpecCreate(
        constraint_set_id=synthetic_constraint_id,
        name=name,
        axes=[GridAxis.model_validate(item.model_dump(mode='json')) for item in native_axes],
        linked_derivations=_linked_derivations(native_links),
        candidate_limit=candidate_limit,
    )
    o10_spec, estimate = validate_search_spec(
        request,
        context,
        context_id=f'cad-revision:{revision.revision_id}',
        constraint_set_id=synthetic_constraint_id,
        constraint_set_spec=engine_spec,
        constraint_set_spec_sha256=engine_sha,
    )
    ordered_axes = tuple(CadSearchAxis.model_validate(item) for item in o10_spec['axes'])
    provisional = CadSearchSpec.model_construct(
        schema_version=CAD_SEARCH_SCHEMA_VERSION,
        search_spec_id=new_search_spec_id(),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash=constraint_workspace_hash,
        constraint_snapshot_json=constraint_snapshot_json,
        constraint_engine_spec_json=canonical_search_json(engine_spec),
        constraint_engine_spec_sha256=engine_sha,
        axes=ordered_axes,
        linked_variables=tuple(
            sorted(native_links, key=lambda item: item.constraint_id)
        ),
        candidate_limit=candidate_limit,
        o10_spec_json=canonical_search_json(o10_spec),
        search_spec_sha256='0' * 64,
        name=name.strip() if name and name.strip() else None,
        created_at_utc=search_timestamp_utc(),
    )
    spec = CadSearchSpec(
        **provisional.model_dump(exclude={'search_spec_sha256'}),
        search_spec_sha256=canonical_search_sha256(provisional.identity_payload()),
    )
    return spec, estimate


def require_search_spec_authority(
    revision: SceneRevision,
    spec: CadSearchSpec,
) -> tuple[dict, dict]:
    """Replay the pinned SearchSpec compiler over declared native authority.

    The model validator alone cannot prove that the executable payloads are
    the canonical compilation of the spec's scene/constraint authority: a
    coherently rehashed row with altered ``o10_spec_json`` or
    ``constraint_engine_spec_json`` still validates. This shared validator
    resolves the exact SceneRevision binding, rechecks the constraint
    snapshot hash, then recompiles the G10 constraint-engine spec and the
    O10 search spec from the declared inputs and requires byte-exact
    canonical equality. It returns the replayed ``(engine_spec, o10_spec)``
    so callers execute the canonical payloads instead of trusting stored
    JSON. Used identically on save, on every authoritative repository read,
    and before candidate generation.
    """

    if revision.document_id != spec.document_id:
        raise ValueError('SearchSpec source revision belongs to another document')
    if revision.revision_id != spec.scene_revision_id:
        raise ValueError('SearchSpec source revision id mismatch')
    if revision.content_hash != spec.scene_content_hash:
        raise ValueError('SearchSpec source content hash mismatch')

    constraint_snapshot = CadConstraintSet.model_validate(
        json.loads(spec.constraint_snapshot_json)
    )
    _, snapshot_hash = constraint_workspace_snapshot(constraint_snapshot)
    if snapshot_hash != spec.constraint_workspace_hash:
        raise ValueError('SearchSpec constraint snapshot hash mismatch')
    if constraint_snapshot.document_id != spec.document_id:
        raise ValueError('SearchSpec constraint snapshot belongs to another document')

    engine_spec, engine_sha = _constraint_engine_spec(
        revision,
        constraint_snapshot,
        {axis.entity_id for axis in spec.axes}
        | {variable.master_entity_id for variable in spec.linked_variables}
        | {variable.slave_entity_id for variable in spec.linked_variables},
        spec.linked_variables,
    )
    if canonical_search_json(engine_spec) != spec.constraint_engine_spec_json:
        raise ValueError(
            'SearchSpec constraint engine spec is not the canonical '
            'compilation of its declared authority'
        )
    if engine_sha != spec.constraint_engine_spec_sha256:
        raise ValueError(
            'SearchSpec constraint engine spec hash is not the canonical '
            'compilation of its declared authority'
        )

    synthetic_constraint_id = f'cad-constraints:{spec.constraint_workspace_hash[:20]}'
    context = scene_to_g10_context(revision.document)
    request = SearchSpecCreate(
        constraint_set_id=synthetic_constraint_id,
        axes=[
            GridAxis.model_validate(item.model_dump(mode='json'))
            for item in spec.axes
        ],
        linked_derivations=_linked_derivations(spec.linked_variables),
        candidate_limit=spec.candidate_limit,
    )
    o10_spec, _estimate = validate_search_spec(
        request,
        context,
        context_id=f'cad-revision:{revision.revision_id}',
        constraint_set_id=synthetic_constraint_id,
        constraint_set_spec=engine_spec,
        constraint_set_spec_sha256=engine_sha,
    )
    if canonical_search_json(o10_spec) != spec.o10_spec_json:
        raise ValueError(
            'SearchSpec o10 spec is not the canonical compilation of its '
            'declared authority'
        )
    ordered_axes = tuple(
        CadSearchAxis.model_validate(item) for item in o10_spec['axes']
    )
    if ordered_axes != spec.axes:
        raise ValueError(
            'SearchSpec axes are not the canonical compilation of its '
            'declared authority'
        )
    return engine_spec, o10_spec


# Paging a SearchSpec always needs the full feasible set (the candidate-set
# sha256 covers every feasible candidate), so each page request previously
# rescanned the entire raw Cartesian product. ``search_spec_sha256`` pins
# every enumeration input — the spec axes/limit and both compiled payloads —
# and SearchSpec rows are immutable, so a complete enumeration is safely
# reusable across page calls. A small LRU bounds the retained sets.
_ENUMERATION_CACHE_MAX = 4
_enumeration_cache: OrderedDict[str, dict] = OrderedDict()
_enumeration_cache_lock = Lock()


def _cached_enumeration(
    scene_repository: SceneRepository,
    spec: CadSearchSpec,
    *,
    cancelled: Callable[[], bool] | None,
) -> dict:
    with _enumeration_cache_lock:
        cached = _enumeration_cache.get(spec.search_spec_sha256)
        if cached is not None:
            _enumeration_cache.move_to_end(spec.search_spec_sha256)
    if cached is not None:
        if cancelled is not None and cancelled():
            raise SearchGenerationCancelled('search generation cancelled')
        return cached

    source = scene_repository.get(spec.scene_revision_id)
    if source is None:
        raise ValueError('SearchSpec source revision no longer exists')
    engine_spec, o10_spec = require_search_spec_authority(source, spec)

    context = scene_to_g10_context(source.document)
    raw = generate_search_space(
        context,
        o10_spec,
        search_spec_sha256=spec.search_spec_sha256,
        constraint_set_spec=engine_spec,
        constraint_set_spec_sha256=spec.constraint_engine_spec_sha256,
        offset=0,
        limit=1,
        cancelled=cancelled,
    )
    entry = {
        key: raw[key]
        for key in (
            'candidate_set_sha256',
            'raw_candidate_count',
            'feasible_candidate_count',
            'rejected_candidate_count',
            'duplicate_candidate_count',
            'rejection_counts',
            'candidate_limit',
            'all_candidates',
        )
    }
    with _enumeration_cache_lock:
        _enumeration_cache[spec.search_spec_sha256] = entry
        _enumeration_cache.move_to_end(spec.search_spec_sha256)
        while len(_enumeration_cache) > _ENUMERATION_CACHE_MAX:
            _enumeration_cache.popitem(last=False)
    return entry


def generate_cad_candidates(
    scene_repository: SceneRepository,
    spec: CadSearchSpec,
    *,
    offset: int = 0,
    limit: int = 100,
    cancelled: Callable[[], bool] | None = None,
) -> CadCandidateSetPage:
    if offset < 0:
        raise ValueError('offset must be >= 0')
    if limit < 1 or limit > MAX_SEARCH_PAGE_SIZE:
        raise ValueError('limit must be between 1 and 500')
    source = scene_repository.get(spec.scene_revision_id)
    if source is None:
        raise ValueError('SearchSpec source revision no longer exists')
    require_search_spec_authority(source, spec)

    enumeration = _cached_enumeration(scene_repository, spec, cancelled=cancelled)
    candidates = tuple(
        CadCandidate.model_validate(item)
        for item in enumeration['all_candidates'][offset : offset + limit]
    )
    return CadCandidateSetPage(
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=enumeration['candidate_set_sha256'],
        raw_candidate_count=enumeration['raw_candidate_count'],
        feasible_candidate_count=enumeration['feasible_candidate_count'],
        rejected_candidate_count=enumeration['rejected_candidate_count'],
        duplicate_candidate_count=enumeration['duplicate_candidate_count'],
        rejection_counts=enumeration['rejection_counts'],
        offset=offset,
        limit=limit,
        candidates=candidates,
    )


def iter_cad_candidate_pages(
    scene_repository: SceneRepository,
    spec: CadSearchSpec,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[CadCandidateSetPage]:
    """Yield every page of one SearchSpec's canonical feasible candidate set.

    ``generate_search_space`` never returns more than ``MAX_SEARCH_PAGE_SIZE``
    candidates per call, so native authorities that must resolve the full set
    paginate deterministically at ``min(MAX_SEARCH_PAGE_SIZE,
    spec.candidate_limit)`` instead of choosing an unbounded page size that
    breaches the generator contract.

    Every yielded page is re-anchored to the same ``search_spec_sha256`` and
    ``candidate_set_sha256``; if the regenerated set identity drifts between
    pages the replay fails closed rather than mixing two enumerations. No
    candidate is skipped or duplicated at page boundaries: offsets advance by
    exactly the number of candidates each page returned, so the concatenation
    of yielded pages equals the canonical feasible enumeration.
    """

    page_limit = min(MAX_SEARCH_PAGE_SIZE, spec.candidate_limit)
    offset = 0
    candidate_set_sha256: str | None = None
    while True:
        page = generate_cad_candidates(
            scene_repository,
            spec,
            offset=offset,
            limit=page_limit,
            cancelled=cancelled,
        )
        if page.search_spec_sha256 != spec.search_spec_sha256:
            raise ValueError('candidate page lost SearchSpec authority')
        if candidate_set_sha256 is None:
            candidate_set_sha256 = page.candidate_set_sha256
        elif page.candidate_set_sha256 != candidate_set_sha256:
            raise ValueError('candidate-set identity changed between pages')
        yield page
        offset += len(page.candidates)
        if not page.candidates or offset >= page.feasible_candidate_count:
            return


def search_spec_current(
    spec: CadSearchSpec,
    current_revision: SceneRevision,
    current_constraint_set: CadConstraintSet,
) -> bool:
    if current_revision.document_id != spec.document_id:
        return False
    if current_revision.revision_id != spec.scene_revision_id:
        return False
    if current_revision.content_hash != spec.scene_content_hash:
        return False
    if current_constraint_set.document_id != spec.document_id:
        return False
    _, current_constraint_hash = constraint_workspace_snapshot(current_constraint_set)
    return current_constraint_hash == spec.constraint_workspace_hash


def search_spec_current_working(
    spec: CadSearchSpec,
    working: WorkingDocument,
    current_constraint_set: CadConstraintSet,
    *,
    current_document_id: str | None = None,
) -> bool:
    if current_document_id is not None and current_document_id != spec.document_id:
        return False
    if working.committed_document.document_id != spec.document_id:
        return False
    if working.source_revision_id != spec.scene_revision_id:
        return False
    if scene_content_hash(working.committed_document) != spec.scene_content_hash:
        return False
    if current_constraint_set.document_id != spec.document_id:
        return False
    _, current_constraint_hash = constraint_workspace_snapshot(current_constraint_set)
    return current_constraint_hash == spec.constraint_workspace_hash


def candidate_preview_document(document: SceneDocument, candidate: CadCandidate) -> SceneDocument:
    """Return a non-authoritative candidate preview without mutating WorkingDocument."""

    replacements = {}
    for entity_id, raw_position in candidate.positions.items():
        entity = document.entity(entity_id)
        position = Position3.model_validate(raw_position)
        replacements[entity_id] = entity.model_copy(update={'position': position})
    entities = tuple(replacements.get(entity.entity_id, entity) for entity in document.entities)
    return document.model_copy(update={'entities': entities})


def apply_candidate_positions(
    working: WorkingDocument,
    candidate: CadCandidate,
    *,
    spec: CadSearchSpec,
    current_constraint_set: CadConstraintSet,
    current_document_id: str | None = None,
) -> bool:
    """Apply candidate positions as one Undo command, after rechecking native authority."""

    if working.has_preview:
        raise EditStateError('cannot apply a candidate while an edit preview is active')
    if not search_spec_current_working(
        spec,
        working,
        current_constraint_set,
        current_document_id=current_document_id,
    ):
        raise ValueError('cannot apply candidate from a stale SearchSpec')
    if not candidate.positions:
        return False

    before = tuple(working.committed_document.entity(entity_id) for entity_id in sorted(candidate.positions))
    after = tuple(
        entity.model_copy(update={'position': Position3.model_validate(candidate.positions[entity.entity_id])})
        for entity in before
    )
    return working.transform_entities(before, after)
