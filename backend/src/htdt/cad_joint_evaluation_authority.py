"""Issue #390 joint evaluation authority replay.

A persisted ``JointCandidateEvaluationBinding`` is authoritative only when its
declared evidence replays exactly: every ``input_refs`` entry resolves through
a typed resolver to the exact persisted authority it names — the evaluated
JointCandidate, the parent JointOptimizationSpec and its baseline
SceneRevision, the candidate's physical SystemVariant or CalibrationPlan, the
spec's pinned measurement/dataset/quality authorities, or the search/O90
authorities — and the resolved semantic hash must equal the declared
``source_sha256`` exactly. Evidence bound to another candidate, variant,
plan, or baseline fails closed, as do ``measured``/``predicted`` refs whose
``source_kind`` has no registered resolver; ``derived``/``hypothesis`` refs
without a resolver stay declared-only claims bound by the binding hash.

The stored ``ObjectiveVector`` is never trusted as caller-supplied numbers:
the evaluator pinned by the spec's ``JointEvaluatorIdentity`` must be
registered under its ``evaluator_id`` and must reproduce the vector — every
metric's definition, unit, direction, state and value — from the resolved
inputs. When evaluator execution is intentionally external, an evaluator may
resolve a persisted, content-addressed evaluation-result record through the
input bindings and return that record's vector, so the binding ties the
joint record to the exact result authority instead of free-form metrics.

Input resolvers and vector evaluators are explicit registries injected into
``CadJointOptimizationRepository``; the same replay runs on save and on
every authoritative read, so disappeared or tampered evidence fails closed.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple

from .cad_calibration import CadCalibrationPlan
from .cad_joint_optimization import (
    JointCandidate,
    JointCandidateEvaluationBinding,
    JointDspAuthorityRef,
    JointEvaluationInputRef,
    JointOptimizationSpec,
)
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    dataset_sha256,
    measurement_sha256,
)
from .cad_repository import SceneRevision
from .cad_system_variant import SystemVariant
from .optimization_objectives import ObjectiveVector


class ResolvedJointEvaluationInput(NamedTuple):
    """One joint input reference resolved against exact persisted authority."""

    ref: JointEvaluationInputRef
    # Semantic hash of the exact evidence consumed. Joint input refs always
    # declare ``source_sha256``, so resolution must reproduce it exactly.
    source_sha256: str
    # The resolved upstream record for evaluators that consume typed
    # evidence (for example a content-addressed evaluation-result payload).
    authority: Any = None


class JointEvaluationAuthorityContext(NamedTuple):
    """Everything a joint input resolver or vector evaluator may rely on.

    ``calibration_plan``/``measurement_quality_report`` are populated only
    for candidates carrying an exact DSP CalibrationPlan authority.
    ``inputs`` is populated with the resolved input tuple before the pinned
    vector evaluator is invoked. A vector evaluator must derive its
    ``ObjectiveVector`` from the resolved inputs and pinned spec/candidate
    authority — never echo ``evaluation.objective_vector``.
    """

    evaluation: JointCandidateEvaluationBinding
    spec: JointOptimizationSpec
    candidate: JointCandidate
    scene_revision: SceneRevision
    physical_variant: SystemVariant
    calibration_plan: CadCalibrationPlan | None
    measurement_quality_report: CadMeasurementQualityReport | None
    scene_repository: Any = None
    system_variant_repository: Any = None
    calibration_repository: Any = None
    search_repository: Any = None
    extended_search_repository: Any = None
    robustness_repository: Any = None
    inputs: tuple[ResolvedJointEvaluationInput, ...] = ()


JointEvaluationInputResolver = Callable[
    [JointEvaluationAuthorityContext, JointEvaluationInputRef],
    ResolvedJointEvaluationInput,
]
JointObjectiveVectorEvaluator = Callable[
    [JointEvaluationAuthorityContext],
    ObjectiveVector,
]

# Evidence classes that may stay declared-only when their source kind has no
# registered resolver: derived inputs are recomputed from pinned candidate/spec
# authority and hypothesis inputs are explicit caller-attested claims. Measured
# and predicted inputs must always resolve to exact persisted evidence.
JOINT_DECLARED_ONLY_EVIDENCE_CLASSES = frozenset({'derived', 'hypothesis'})


def _require_evidence_class(
    ref: JointEvaluationInputRef,
    expected: str,
) -> None:
    if ref.evidence_class != expected:
        raise ValueError(
            f'{ref.source_kind} joint evaluation input must be {expected} '
            'evidence'
        )


def _resolve_joint_candidate(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    candidate = context.candidate
    if ref.source_id != candidate.candidate_id:
        raise ValueError(
            'joint_candidate input must name the exact evaluated candidate'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=candidate.candidate_sha256,
        authority=candidate,
    )


def _resolve_joint_optimization_spec(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    if ref.source_id != context.spec.spec_id:
        raise ValueError(
            'joint_optimization_spec input must name the exact parent spec'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=context.spec.semantic_sha256,
        authority=context.spec,
    )


def _resolve_scene_revision(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    revision = context.scene_revision
    if (
        ref.source_id != revision.revision_id
        or ref.source_id != context.spec.scene_revision_id
    ):
        raise ValueError(
            'scene_revision input must name the exact spec baseline '
            'SceneRevision'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=revision.content_hash,
        authority=revision,
    )


def _resolve_system_variant(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    variant = context.physical_variant
    candidate = context.candidate
    if (
        ref.source_id != candidate.physical_system_variant_id
        or ref.source_id != variant.variant_id
    ):
        raise ValueError(
            'system_variant input must name the exact candidate physical '
            'SystemVariant'
        )
    spec = context.spec
    if (
        variant.document_id != spec.document_id
        or variant.baseline_revision_id != spec.scene_revision_id
        or variant.baseline_content_hash != spec.scene_content_hash
    ):
        raise ValueError(
            'system_variant input is not bound to the spec baseline'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=variant.variant_sha256,
        authority=variant,
    )


def _resolve_calibration_plan(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    candidate = context.candidate
    plan = context.calibration_plan
    if candidate.calibration_candidate is None or plan is None:
        raise ValueError(
            'calibration_plan input requires the candidate to carry exact '
            'CalibrationPlan authority'
        )
    if (
        ref.source_id != candidate.calibration_candidate.plan_id
        or ref.source_id != plan.plan_id
    ):
        raise ValueError(
            'calibration_plan input must name the exact candidate '
            'CalibrationPlan'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=plan.plan_semantic_sha256,
        authority=plan,
    )


def _dsp_authority(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> JointDspAuthorityRef:
    authority = context.spec.dsp_authority
    if authority is None:
        raise ValueError(
            f'{ref.source_kind} joint evaluation input requires the spec to '
            'carry exact DSP authority'
        )
    return authority


def _resolve_cad_measurement(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'measured')
    authority = _dsp_authority(context, ref)
    if ref.source_id != authority.source_measurement_id:
        raise ValueError(
            'cad_measurement input must name the exact DSP source Measurement'
        )
    record = context.calibration_repository.measurement_repository.get_measurement(
        ref.source_id
    )
    if record is None:
        raise ValueError('cad_measurement input evidence does not exist')
    spec = context.spec
    if (
        record.document_id != spec.document_id
        or record.scene_revision_id != spec.scene_revision_id
        or record.scene_content_hash != spec.scene_content_hash
    ):
        raise ValueError(
            'cad_measurement input is not bound to the spec baseline'
        )
    if measurement_sha256(record) != authority.source_measurement_sha256:
        raise ValueError(
            'cad_measurement input does not match the DSP authority hash'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=measurement_sha256(record),
        authority=record,
    )


def _resolve_cad_measurement_dataset(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'measured')
    authority = _dsp_authority(context, ref)
    if ref.source_id != authority.source_dataset_id:
        raise ValueError(
            'cad_measurement_dataset input must name the exact DSP source '
            'dataset'
        )
    dataset = context.calibration_repository.measurement_repository.get_dataset(
        ref.source_id
    )
    if dataset is None:
        raise ValueError('cad_measurement_dataset input evidence does not exist')
    if dataset.measurement_id != authority.source_measurement_id:
        raise ValueError(
            'cad_measurement_dataset input does not belong to the DSP source '
            'measurement'
        )
    resolved_sha256 = dataset_sha256(dataset)
    if resolved_sha256 != authority.source_dataset_sha256:
        raise ValueError(
            'cad_measurement_dataset input does not match the DSP authority '
            'hash'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=resolved_sha256,
        authority=dataset,
    )


def _resolve_measurement_quality_report(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'measured')
    authority = _dsp_authority(context, ref)
    if ref.source_id != authority.measurement_quality_report_id:
        raise ValueError(
            'measurement_quality_report input must name the exact DSP '
            'quality report'
        )
    report = context.calibration_repository.quality_repository.get_report(
        ref.source_id
    )
    if report is None:
        raise ValueError(
            'measurement_quality_report input evidence does not exist'
        )
    if (
        report.measurement_id != authority.source_measurement_id
        or report.dataset_id != authority.source_dataset_id
    ):
        raise ValueError(
            'measurement_quality_report input does not bind the DSP source '
            'evidence'
        )
    if report.report_sha256 != authority.measurement_quality_report_sha256:
        raise ValueError(
            'measurement_quality_report input does not match the DSP '
            'authority hash'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=report.report_sha256,
        authority=report,
    )


def _resolve_physical_search_spec(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    spec = context.spec
    if ref.source_id != spec.physical_search_spec_id:
        raise ValueError(
            'physical_search_spec input must name the exact base SearchSpec'
        )
    search_spec = context.search_repository.get(ref.source_id)
    if search_spec is None:
        raise ValueError('physical_search_spec input evidence does not exist')
    if (
        search_spec.document_id != spec.document_id
        or search_spec.scene_revision_id != spec.scene_revision_id
        or search_spec.scene_content_hash != spec.scene_content_hash
    ):
        raise ValueError(
            'physical_search_spec input is not bound to the spec baseline'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=search_spec.search_spec_sha256,
        authority=search_spec,
    )


def _resolve_extended_search_spec(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    spec = context.spec
    if spec.extended_search_spec_id is None:
        raise ValueError(
            'extended_search_spec input requires the spec to carry extended '
            'search authority'
        )
    if ref.source_id != spec.extended_search_spec_id:
        raise ValueError(
            'extended_search_spec input must name the exact extended '
            'SearchSpec'
        )
    extended = context.extended_search_repository.get_spec(ref.source_id)
    if extended is None:
        raise ValueError('extended_search_spec input evidence does not exist')
    if (
        extended.extended_search_sha256 != spec.extended_search_spec_sha256
        or extended.base_search_spec_id != spec.physical_search_spec_id
        or extended.base_search_spec_sha256 != spec.physical_search_spec_sha256
    ):
        raise ValueError(
            'extended_search_spec input does not bind the exact base '
            'physical search'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=extended.extended_search_sha256,
        authority=extended,
    )


def _resolve_robustness_spec(
    context: JointEvaluationAuthorityContext,
    ref: JointEvaluationInputRef,
) -> ResolvedJointEvaluationInput:
    _require_evidence_class(ref, 'derived')
    spec = context.spec
    if ref.source_id != spec.robustness.robustness_spec_id:
        raise ValueError(
            'robustness_spec input must name the exact O90 RobustnessSpec'
        )
    try:
        robustness_spec = context.robustness_repository.get_spec(ref.source_id)
    except KeyError as exc:
        raise ValueError(
            'robustness_spec input evidence does not exist'
        ) from exc
    if (
        robustness_spec.robustness_spec_sha256
        != spec.robustness.robustness_spec_sha256
        or robustness_spec.document_id != spec.document_id
        or robustness_spec.scene_revision_id != spec.scene_revision_id
        or robustness_spec.scene_content_hash != spec.scene_content_hash
        or robustness_spec.search_spec_id != spec.physical_search_spec_id
        or robustness_spec.search_spec_sha256 != spec.physical_search_spec_sha256
    ):
        raise ValueError(
            'robustness_spec input does not bind the spec baseline and '
            'physical search authority'
        )
    return ResolvedJointEvaluationInput(
        ref=ref,
        source_sha256=robustness_spec.robustness_spec_sha256,
        authority=robustness_spec,
    )


JOINT_EVALUATION_INPUT_RESOLVERS: dict[str, JointEvaluationInputResolver] = {
    'joint_candidate': _resolve_joint_candidate,
    'joint_optimization_spec': _resolve_joint_optimization_spec,
    'scene_revision': _resolve_scene_revision,
    'system_variant': _resolve_system_variant,
    'calibration_plan': _resolve_calibration_plan,
    'cad_measurement': _resolve_cad_measurement,
    'cad_measurement_dataset': _resolve_cad_measurement_dataset,
    'measurement_quality_report': _resolve_measurement_quality_report,
    'physical_search_spec': _resolve_physical_search_spec,
    'extended_search_spec': _resolve_extended_search_spec,
    'robustness_spec': _resolve_robustness_spec,
}
