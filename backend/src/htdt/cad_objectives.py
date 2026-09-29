from __future__ import annotations

from typing import Any, Sequence

from .cad_objective_models import (
    CadObjectiveEvaluation,
    CadObjectiveInputRef,
    CadParetoEvaluationRef,
    CadParetoSet,
    canonical_objective_json,
    canonical_objective_sha256,
    new_evaluation_id,
    new_pareto_set_id,
    objective_timestamp_utc,
)
from .cad_repository import SceneRevision
from .cad_search_models import CadSearchSpec
from .optimization_objectives import ObjectiveVector
from .pareto import pareto_front


def build_objective_evaluation(
    revision: SceneRevision,
    search_spec: CadSearchSpec,
    candidate_id: str,
    vector: ObjectiveVector,
    *,
    evaluation_spec: Any,
    input_refs: Sequence[CadObjectiveInputRef],
) -> CadObjectiveEvaluation:
    """Assemble one immutable O30 evaluation payload.

    This is the low-level assembler: it binds the evaluation to the exact
    SceneRevision/SearchSpec and canonicalizes caller-supplied provenance.
    It does not resolve candidate membership or evidence — that authority
    replay runs inside ``CadObjectiveRepository.save_evaluation`` and on
    every authoritative read (see ``cad_objective_authority``).
    """

    if search_spec.document_id != revision.document_id:
        raise ValueError('objective SearchSpec belongs to another document')
    if search_spec.scene_revision_id != revision.revision_id:
        raise ValueError('objective SearchSpec belongs to another SceneRevision')
    if search_spec.scene_content_hash != revision.content_hash:
        raise ValueError('objective SearchSpec content hash mismatch')
    if vector.candidate_id != candidate_id:
        raise ValueError('objective vector candidate_id mismatch')
    if not input_refs:
        raise ValueError('objective evaluation requires at least one evidence/input reference')

    ordered_refs = tuple(sorted(
        input_refs,
        key=lambda ref: (ref.evidence_class, ref.source_kind, ref.source_id),
    ))
    evaluation_spec_json = canonical_objective_json(evaluation_spec)
    payload = {
        'schema_version': 1,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'search_spec_id': search_spec.search_spec_id,
        'search_spec_sha256': search_spec.search_spec_sha256,
        'candidate_id': candidate_id,
        'input_refs': [
            ref.model_dump(mode='json', exclude_none=True)
            for ref in ordered_refs
        ],
        'evaluation_spec': evaluation_spec,
        'vector': vector.identity_payload(),
    }
    return CadObjectiveEvaluation(
        evaluation_id=new_evaluation_id(),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        search_spec_id=search_spec.search_spec_id,
        search_spec_sha256=search_spec.search_spec_sha256,
        candidate_id=candidate_id,
        input_refs=ordered_refs,
        evaluation_spec_json=evaluation_spec_json,
        evaluation_spec_sha256=canonical_objective_sha256(evaluation_spec),
        vector=vector,
        evaluation_sha256=canonical_objective_sha256(payload),
        created_at_utc=objective_timestamp_utc(),
    )


def build_pareto_set(
    evaluations: Sequence[CadObjectiveEvaluation],
    objective_ids: Sequence[str],
) -> CadParetoSet:
    if not evaluations:
        raise ValueError('Pareto set requires at least one objective evaluation')

    first = evaluations[0]
    for evaluation in evaluations:
        if (
            evaluation.document_id != first.document_id
            or evaluation.scene_revision_id != first.scene_revision_id
            or evaluation.scene_content_hash != first.scene_content_hash
            or evaluation.search_spec_id != first.search_spec_id
            or evaluation.search_spec_sha256 != first.search_spec_sha256
            or evaluation.evaluation_spec_sha256 != first.evaluation_spec_sha256
        ):
            raise ValueError(
                'Pareto evaluations must share one SceneRevision, SearchSpec '
                'and evaluation spec'
            )

    selected = tuple(objective_ids)
    result = pareto_front(tuple(item.vector for item in evaluations), selected)
    refs = tuple(
        CadParetoEvaluationRef(
            evaluation_id=item.evaluation_id,
            evaluation_sha256=item.evaluation_sha256,
            candidate_id=item.candidate_id,
        )
        for item in evaluations
    )
    payload = {
        'schema_version': 1,
        'document_id': first.document_id,
        'scene_revision_id': first.scene_revision_id,
        'scene_content_hash': first.scene_content_hash,
        'search_spec_id': first.search_spec_id,
        'search_spec_sha256': first.search_spec_sha256,
        'evaluations': [ref.model_dump(mode='json') for ref in refs],
        'objective_ids': list(selected),
        'result': result.model_dump(mode='json'),
    }
    return CadParetoSet(
        pareto_set_id=new_pareto_set_id(),
        document_id=first.document_id,
        scene_revision_id=first.scene_revision_id,
        scene_content_hash=first.scene_content_hash,
        search_spec_id=first.search_spec_id,
        search_spec_sha256=first.search_spec_sha256,
        evaluations=refs,
        objective_ids=selected,
        result=result,
        pareto_sha256=canonical_objective_sha256(payload),
        created_at_utc=objective_timestamp_utc(),
    )
