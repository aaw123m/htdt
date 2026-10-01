"""Issue #524 Optimize-side context for #174 joint placement+DSP search.

Read-side context only: it resolves the exact persisted authorities a
``JointOptimizationSpec`` must bind (baseline SceneRevision, base
SystemVariant, physical/extended search authority, CalibrationPlan,
MeasurementQualityReport, O90 RobustnessSpec) so the native Optimize UI can
create a canonical spec without users typing internal ids or hashes.

Nothing here mutates the scene, device settings, or DSP state — a created
spec is still only a solver-neutral orchestration contract and the
CalibrationPlan remains the DSP settings authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Sequence

from .cad_calibration import CadCalibrationPlan
from .cad_calibration_repository import CadCalibrationRepository
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_joint_execution import (
    JointExecutionResult,
    assess_joint_spec_staleness,
    run_joint_execution,
)
from .cad_joint_optimization import (
    JointDspVariable,
    JointEvaluatorIdentity,
    JointOptimizationSpec,
    JointRobustnessSpecRef,
    JointRobustnessVariableMapping,
    build_joint_optimization_spec,
    physical_variables_from_authority,
)
from .cad_joint_optimization_repository import CadJointOptimizationRepository
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    gate_measurement_claim,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_search_models import CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .cad_system_variant import SystemVariant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_robustness_repository import CadRobustnessRepository
from .optimization_objectives import ObjectiveDefinition
from .optimization_robustness import RobustnessSpec

DspParameterName = Literal[
    'gain_db',
    'peq_frequency_hz',
    'peq_q',
    'peq_gain_db',
    'crossover_frequency_hz',
    'crossover_order',
    'delay_s',
    'polarity',
]

JointSearchMode = Literal['placement_only', 'dsp_only', 'joint']

# Declared magnitude-authority band for DSP variables authored in the UI.
# Low-frequency integration band; the same value is used for both the
# capability gate and the JointDspVariable.required_band_hz field.
DEFAULT_MAGNITUDE_BAND_HZ: tuple[float, float] = (20.0, 200.0)

_DSP_PARAMETERS: tuple[DspParameterName, ...] = (
    'gain_db',
    'peq_frequency_hz',
    'peq_q',
    'peq_gain_db',
    'crossover_frequency_hz',
    'crossover_order',
    'delay_s',
    'polarity',
)

_DSP_LABEL_JA: dict[str, str] = {
    'gain_db': 'ゲイン',
    'peq_frequency_hz': 'PEQ 周波数',
    'peq_q': 'PEQ Q',
    'peq_gain_db': 'PEQ ゲイン',
    'crossover_frequency_hz': 'クロスオーバー周波数',
    'crossover_order': 'クロスオーバー次数',
    'delay_s': 'ディレイ',
    'polarity': '極性',
}

_DSP_CLAIM_JA: dict[str, str] = {
    'gain_db': '振幅応答（周波数帯指定）',
    'peq_frequency_hz': '振幅応答（周波数帯指定）',
    'peq_q': '振幅応答（周波数帯指定）',
    'peq_gain_db': '振幅応答（周波数帯指定）',
    'crossover_frequency_hz': '振幅応答（周波数帯指定）',
    'crossover_order': '振幅応答（周波数帯指定）',
    'delay_s': '共通タイミング',
    'polarity': '極性',
}


@dataclass(frozen=True, slots=True)
class JointBaseline:
    """Resolved authority set for one baseline; all ids/sha256 are exact."""

    scene_revision: SceneRevision
    base_variant: SystemVariant
    physical_search_spec: CadSearchSpec
    extended_search_spec: object | None
    calibration_plan: CadCalibrationPlan | None
    quality_report: CadMeasurementQualityReport | None
    robustness_spec: RobustnessSpec | None
    objective_definitions: tuple[ObjectiveDefinition, ...]


@dataclass(frozen=True, slots=True)
class DspVariableOption:
    """Capability-gated availability of one DSP decision variable."""

    parameter: DspParameterName
    label_ja: str
    required_claim_ja: str
    enabled: bool
    decision: str
    reason_ja: str
    device_limit_note_ja: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateBudgetEstimate:
    physical_candidate_count: int
    dsp_candidate_count: int
    combined_candidate_count: int
    within_budget: bool
    candidate_budget: int


def _o90_parameter(entity_kind: str, parameter: str) -> str | None:
    """Map a joint physical parameter onto the O90 uncertainty-axis domain.

    O90 names Cartesian axes per element kind ('speaker_x_m',
    'listener_x_m'); orientation parameters already share the namespace.
    """
    if parameter in ('aim_yaw_deg', 'aim_pitch_deg', 'body_yaw_deg'):
        return parameter
    if not parameter.endswith('_m'):
        return None
    prefix = (
        'listener'
        if entity_kind in ('seat', 'measurement_point', 'listener')
        else 'speaker'
    )
    return f'{prefix}_{parameter}'


def _claim_of(parameter: DspParameterName) -> str:
    if parameter == 'delay_s':
        return 'common_timing'
    if parameter == 'polarity':
        return 'polarity'
    return 'magnitude_response'


def _device_limit_note_ja(
    parameter: DspParameterName,
    plan: CadCalibrationPlan | None,
) -> str | None:
    if plan is None:
        return None
    constraints = plan.device_constraints
    notes: list[str] = []
    if parameter == 'gain_db':
        if constraints.min_gain_db is not None and constraints.max_gain_db is not None:
            notes.append(
                f'デバイス範囲 {constraints.min_gain_db:g}〜{constraints.max_gain_db:g} dB'
            )
        if constraints.channel_gain_resolution_db is not None:
            notes.append(f'分解能 {constraints.channel_gain_resolution_db:g} dB')
    elif parameter == 'delay_s':
        if constraints.max_delay_s is not None:
            notes.append(f'最大 {constraints.max_delay_s:g} s')
        if constraints.delay_resolution_s is not None:
            notes.append(f'分解能 {constraints.delay_resolution_s:g} s')
    elif parameter in ('peq_q',):
        if constraints.q_resolution is not None:
            notes.append(f'Q分解能 {constraints.q_resolution:g}')
    elif parameter in ('peq_frequency_hz', 'crossover_frequency_hz'):
        if constraints.frequency_resolution_hz is not None:
            notes.append(f'周波数分解能 {constraints.frequency_resolution_hz:g} Hz')
    elif parameter == 'peq_gain_db':
        if constraints.filter_gain_resolution_db is not None:
            notes.append(f'ゲイン分解能 {constraints.filter_gain_resolution_db:g} dB')
    elif parameter == 'crossover_order':
        if constraints.supported_crossover_orders:
            orders = ' / '.join(str(item) for item in constraints.supported_crossover_orders)
            notes.append(f'対応次数 {orders}')
    return '；'.join(notes) if notes else None


class JointOptimizationContext:
    """Resolves and persists joint-optimization authorities for Optimize UI."""

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        *,
        objective_repository: CadObjectiveRepository | None = None,
        objective_input_resolvers: dict | None = None,
        objective_vector_evaluators: dict | None = None,
    ) -> None:
        self.repository = repository
        self.document_id = document_id
        self.variant_repository = CadSystemVariantRepository(repository)
        self.search_repository = CadSearchRepository(repository)
        self.extended_repository = CadExtendedSearchRepository(
            self.search_repository,
        )
        self.measurement_repository = CadMeasurementRepository(repository)
        self.quality_repository = CadMeasurementQualityRepository(
            self.measurement_repository,
        )
        self.calibration_repository = CadCalibrationRepository(
            scene_repository=repository,
            system_variant_repository=self.variant_repository,
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
        )
        self.objective_repository = (
            objective_repository
            if objective_repository is not None
            else CadObjectiveRepository(
                repository,
                self.search_repository,
                measurement_repository=self.measurement_repository,
                input_resolvers=objective_input_resolvers,
                vector_evaluators=objective_vector_evaluators,
            )
        )
        self.robustness_repository = CadRobustnessRepository(
            scene_repository=repository,
            search_repository=self.search_repository,
            objective_repository=self.objective_repository,
            extended_search_repository=self.extended_repository,
        )
        self._joint_repository: CadJointOptimizationRepository | None = None

    @property
    def joint_repository(self) -> CadJointOptimizationRepository:
        if self._joint_repository is None:
            self._joint_repository = CadJointOptimizationRepository(
                scene_repository=self.repository,
                system_variant_repository=self.variant_repository,
                calibration_repository=self.calibration_repository,
                search_repository=self.search_repository,
                extended_search_repository=self.extended_repository,
                robustness_repository=self.robustness_repository,
            )
        return self._joint_repository

    def list_specs(self) -> tuple[JointOptimizationSpec, ...]:
        return self.joint_repository.list_specs(self.document_id)

    # ------------------------------------------------------------------
    # Baseline resolution
    # ------------------------------------------------------------------

    def resolve_baseline(self) -> JointBaseline | None:
        head = self.repository.current_head(self.document_id)
        if head is None:
            return None

        variants = [
            item
            for item in self.variant_repository.list_variants(self.document_id)
            if item.baseline_revision_id == head.revision_id
            and item.baseline_content_hash == head.content_hash
        ]
        if not variants:
            return None

        plans = [
            item
            for item in self.calibration_repository.list_plans(self.document_id)
            if item.scene_revision_id == head.revision_id
            and item.scene_content_hash == head.content_hash
            and item.support_state == 'SUPPORTED'
        ]
        planned_ids = {item.system_variant_id for item in plans}
        planned_variants = [
            item for item in variants if item.variant_id in planned_ids
        ]
        # A supported CalibrationPlan names its exact base variant; prefer it
        # so the DSP authority chain stays consistent.
        base_variant = (
            planned_variants[-1] if planned_variants else variants[-1]
        )
        plans = [
            item
            for item in plans
            if item.system_variant_id == base_variant.variant_id
            and item.system_variant_sha256 == base_variant.variant_sha256
        ]
        base_plan = plans[-1] if plans else None

        specs = [
            item
            for item in self.search_repository.list_specs(self.document_id)
            if item.scene_revision_id == head.revision_id
            and item.scene_content_hash == head.content_hash
        ]
        if not specs:
            return None
        physical_search_spec = specs[-1]

        extended_items = self.extended_repository.list_for_base_search(
            physical_search_spec.search_spec_id
        )
        extended_search_spec = extended_items[-1] if extended_items else None

        quality_report = None
        if base_plan is not None:
            candidate = self.quality_repository.latest_report(
                base_plan.source_measurement_id
            )
            if (
                candidate is not None
                and candidate.report_id
                == base_plan.measurement_quality_report_id
                and candidate.report_sha256
                == base_plan.measurement_quality_report_sha256
            ):
                quality_report = candidate

        robustness_spec = self._latest_robustness_spec(
            head=head,
            search_spec=physical_search_spec,
        )
        objective_definitions = self._objective_definitions(
            physical_search_spec.search_spec_id
        )
        return JointBaseline(
            scene_revision=head,
            base_variant=base_variant,
            physical_search_spec=physical_search_spec,
            extended_search_spec=extended_search_spec,
            calibration_plan=base_plan,
            quality_report=quality_report,
            robustness_spec=robustness_spec,
            objective_definitions=objective_definitions,
        )

    def _latest_robustness_spec(
        self,
        *,
        head: SceneRevision,
        search_spec: CadSearchSpec,
    ) -> RobustnessSpec | None:
        rows = self.robustness_repository.list_specs_for_search(
            document_id=self.document_id,
            scene_revision_id=head.revision_id,
            search_spec_id=search_spec.search_spec_id,
        )
        return rows[-1] if rows else None

    def _objective_definitions(
        self,
        search_spec_id: str,
    ) -> tuple[ObjectiveDefinition, ...]:
        """Reuse the persisted evaluation's explicit definitions.

        Joint spec objectives must be ObjectiveDefinition records, so we take
        them from the most recent canonical objective evaluation bound to the
        same physical search authority rather than synthesizing new ids.
        Metrics without an explicit definition fall back to the marked
        ``ObjectiveDefinition.legacy`` form — still versioned, still exact.
        """
        evaluations = self.objective_repository.latest_evaluations_by_candidate(
            search_spec_id
        )
        definitions: list[ObjectiveDefinition] = []
        seen: set[str] = set()
        for evaluation in evaluations:
            for metric in evaluation.vector.metrics:
                if metric.objective_id in seen:
                    continue
                seen.add(metric.objective_id)
                definition = metric.definition
                if definition is None:
                    definition = ObjectiveDefinition.legacy(
                        objective_id=metric.objective_id,
                        unit=metric.unit,
                        direction=metric.direction,
                    )
                definitions.append(definition)
        return tuple(definitions)

    # ------------------------------------------------------------------
    # DSP variable gating (measurement capability + device limits)
    # ------------------------------------------------------------------

    def dsp_variable_options(
        self,
        baseline: JointBaseline,
    ) -> tuple[DspVariableOption, ...]:
        plan = baseline.calibration_plan
        report = baseline.quality_report
        options: list[DspVariableOption] = []
        for parameter in _DSP_PARAMETERS:
            claim = _claim_of(parameter)
            label = _DSP_LABEL_JA[parameter]
            required_ja = _DSP_CLAIM_JA[parameter]
            device_note = _device_limit_note_ja(parameter, plan)
            if plan is None:
                options.append(
                    DspVariableOption(
                        parameter=parameter,
                        label_ja=label,
                        required_claim_ja=required_ja,
                        enabled=False,
                        decision='BLOCKED',
                        reason_ja='対応する校正プランがありません。',
                        device_limit_note_ja=device_note,
                    )
                )
                continue
            if report is None:
                options.append(
                    DspVariableOption(
                        parameter=parameter,
                        label_ja=label,
                        required_claim_ja=required_ja,
                        enabled=False,
                        decision='UNKNOWN',
                        reason_ja='測定品質レポートが未保存のため有効化できません。',
                        device_limit_note_ja=device_note,
                    )
                )
                continue
            capability = gate_measurement_claim(
                report,
                claim,
                required_band_hz=(
                    DEFAULT_MAGNITUDE_BAND_HZ
                    if claim == 'magnitude_response'
                    else None
                ),
            )
            enabled = capability.decision == 'ALLOWED'
            reason = '' if enabled else '；'.join(capability.reasons)
            options.append(
                DspVariableOption(
                    parameter=parameter,
                    label_ja=label,
                    required_claim_ja=required_ja,
                    enabled=enabled,
                    decision=capability.decision,
                    reason_ja=reason or '有効',
                    device_limit_note_ja=device_note,
                )
            )
        return tuple(options)

    # ------------------------------------------------------------------
    # Candidate-count preflight
    # ------------------------------------------------------------------

    def estimate_candidates(
        self,
        baseline: JointBaseline,
        *,
        mode: JointSearchMode,
        dsp_variables: Sequence[JointDspVariable],
        candidate_budget: int,
    ) -> CandidateBudgetEstimate:
        physical_variables = physical_variables_from_authority(
            baseline.physical_search_spec,
            baseline.extended_search_spec,  # type: ignore[arg-type]
        )
        physical_count = 1
        if mode in ('placement_only', 'joint'):
            for variable in physical_variables:
                span = variable.maximum - variable.minimum
                steps = int(span / variable.step) + 1 if variable.step > 0 else 1
                physical_count *= max(steps, 1)
        else:
            physical_count = 1

        dsp_count = 1
        if mode in ('dsp_only', 'joint'):
            for variable in dsp_variables:
                if variable.parameter == 'polarity':
                    dsp_count *= max(len(variable.allowed_values), 1)
                    continue
                if (
                    variable.minimum is None
                    or variable.maximum is None
                    or variable.step is None
                    or variable.step <= 0
                ):
                    dsp_count *= 1
                    continue
                span = variable.maximum - variable.minimum
                dsp_count *= max(int(span / variable.step) + 1, 1)

        combined = physical_count * dsp_count
        return CandidateBudgetEstimate(
            physical_candidate_count=physical_count,
            dsp_candidate_count=dsp_count,
            combined_candidate_count=combined,
            within_budget=combined <= candidate_budget,
            candidate_budget=candidate_budget,
        )

    # ------------------------------------------------------------------
    # Spec creation (exact authorities only; never infers DSP authority)
    # ------------------------------------------------------------------

    def create_spec(
        self,
        *,
        baseline: JointBaseline,
        mode: JointSearchMode,
        dsp_variables: Sequence[JointDspVariable],
        candidate_budget: int,
        created_at_utc: str | None = None,
    ) -> JointOptimizationSpec:
        if baseline.robustness_spec is None:
            raise ValueError(
                'joint optimization requires an existing O90 RobustnessSpec '
                'binding the physical search authority'
            )
        if not baseline.objective_definitions:
            raise ValueError(
                'joint optimization requires persisted objective definitions '
                'from the baseline physical search'
            )
        if mode in ('dsp_only', 'joint') and not dsp_variables:
            raise ValueError('DSP mode requires at least one DSP variable')

        effective_dsp: tuple[JointDspVariable, ...] = (
            tuple(dsp_variables) if mode in ('dsp_only', 'joint') else ()
        )
        effective_plan = (
            baseline.calibration_plan if effective_dsp else None
        )
        effective_report = (
            baseline.quality_report if effective_dsp else None
        )

        mappings: list[JointRobustnessVariableMapping] = []
        physical = physical_variables_from_authority(
            baseline.physical_search_spec,
            baseline.extended_search_spec,  # type: ignore[arg-type]
        )
        entity_kinds = {
            entity.entity_id: entity.kind
            for entity in baseline.scene_revision.document.entities
        }
        axis_lookup = {
            (axis.entity_id, axis.parameter): axis.axis_id
            for axis in baseline.robustness_spec.axes
        }
        for variable in physical:
            o90_parameter = _o90_parameter(
                entity_kinds.get(variable.entity_id, ''),
                variable.parameter,
            )
            if o90_parameter is None:
                continue
            axis_id = axis_lookup.get((variable.entity_id, o90_parameter))
            if axis_id is not None:
                mappings.append(
                    JointRobustnessVariableMapping(
                        joint_variable_id=variable.variable_id,
                        o90_axis_id=axis_id,
                    )
                )
        if not mappings:
            raise ValueError(
                'O90 robustness mapping requires at least one joint physical '
                'variable bound to an existing uncertainty axis'
            )
        robustness_ref = JointRobustnessSpecRef(
            robustness_spec_id=baseline.robustness_spec.robustness_spec_id,
            robustness_spec_sha256=baseline.robustness_spec.robustness_spec_sha256,
            variable_mapping=tuple(mappings),
        )

        evaluator = JointEvaluatorIdentity(
            evaluator_id=(
                f'joint-evaluator:{baseline.robustness_spec.model_id}:'
                f'{baseline.robustness_spec.model_version}'
            ),
            evaluator_version='joint-evaluation-1',
            model_id=baseline.robustness_spec.model_id,
            model_version=baseline.robustness_spec.model_version,
            fidelity=baseline.robustness_spec.fidelity,
            evidence_scope='validated_model',
        )

        spec = build_joint_optimization_spec(
            scene_revision=baseline.scene_revision,
            base_system_variant=baseline.base_variant,
            physical_search_spec=baseline.physical_search_spec,
            extended_search_spec=baseline.extended_search_spec,  # type: ignore[arg-type]
            base_calibration_plan=effective_plan,
            measurement_quality_report=effective_report,
            dsp_variables=effective_dsp,
            objectives=baseline.objective_definitions,
            robustness=robustness_ref,
            evaluator=evaluator,
            candidate_budget=candidate_budget,
            created_at_utc=(
                created_at_utc
                or datetime.now(timezone.utc).isoformat(timespec='seconds')
            ),
        )
        self.joint_repository.save_spec(spec)
        return spec

    # ------------------------------------------------------------------
    # Spec execution (#945): the same exact baseline authorities the spec
    # was authored against drive the bounded canonical execution pass.
    # ------------------------------------------------------------------

    def assess_spec_staleness(
        self,
        spec_id: str,
    ) -> tuple[str, ...]:
        """Typed staleness between a persisted spec and the live baseline.

        An unresolved baseline reports ``('baseline_unresolved',)`` so the UI
        can gate execution without conflating missing authorities with
        authority drift.
        """

        spec = self.joint_repository.get_spec(spec_id)
        if spec is None:
            raise ValueError(
                'joint optimization spec is not persisted: ' + spec_id
            )
        baseline = self.resolve_baseline()
        if baseline is None:
            return ('baseline_unresolved',)
        return assess_joint_spec_staleness(
            spec=spec,
            baseline=baseline.scene_revision,
            base_variant=baseline.base_variant,
            base_plan=(
                baseline.calibration_plan
                if spec.dsp_authority is not None
                else None
            ),
        )

    def execute_spec(
        self,
        spec_id: str,
        *,
        created_at_utc: str | None = None,
        is_cancelled=None,
        on_progress=None,
    ) -> JointExecutionResult:
        """Run one persisted spec end to end inside its immutable budget.

        The baseline is re-resolved at call time, so authority drift fails
        closed inside ``run_joint_execution`` before any candidate is
        persisted. No numeric evaluator is injected: eligible candidates
        receive the canonical unsupported objective vector — execution
        materializes exact SystemVariant/CalibrationPlan candidates and
        persists every binding without fabricating prediction numbers.
        """

        spec = self.joint_repository.get_spec(spec_id)
        if spec is None:
            raise ValueError(
                'joint optimization spec is not persisted: ' + spec_id
            )
        baseline = self.resolve_baseline()
        if baseline is None:
            raise ValueError(
                'joint execution requires a resolved baseline: current '
                'SceneRevision, base SystemVariant, and physical search spec'
            )
        return run_joint_execution(
            repository=self.joint_repository,
            spec=spec,
            baseline=baseline.scene_revision,
            base_variant=baseline.base_variant,
            system_variant_repository=self.variant_repository,
            calibration_repository=self.calibration_repository,
            base_plan=(
                baseline.calibration_plan
                if spec.dsp_authority is not None
                else None
            ),
            quality_report=(
                baseline.quality_report
                if spec.dsp_authority is not None
                else None
            ),
            created_at_utc=(
                created_at_utc
                or datetime.now(timezone.utc).isoformat(timespec='seconds')
            ),
            is_cancelled=is_cancelled,
            on_progress=on_progress,
        )

