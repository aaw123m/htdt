"""Pre-run compute estimate authority (issue #991).

Before an acoustic prediction or optimization run is committed, the
operator must see the compute story — solver, backend, fidelity axes,
predicted runtime/memory/storage, budget verdict — derived from the
ACTUAL job configuration against DECLARED cost models.

Honesty contract:

* Inputs inside a model's calibrated range yield a bounded estimate.
* Inputs beyond the calibrated range but inside the declared
  extrapolation bound yield a widened, explicitly-flagged estimate.
* Inputs outside the extrapolation bound yield ``None`` metrics — an
  honest UNKNOWN, never a fabricated point precision.
* Estimates are sealed records hash-pinned to the exact job plan
  (``PrerunJobPlan.plan_sha256`` covers every numeric driver), so an
  estimate can be audited against the config it described.
* Measured outcomes live only in ``ComputeObservation`` rows written by
  ``record_prerun_observation`` from real timing; cancelled/failed runs
  never mint observations, and historical observations never upgrade an
  estimate's confidence.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_compute_budget import (
    BudgetEvidenceMeans,
    BudgetVerdict,
    ComputeObservation,
    FidelityClaimVerdict,
    PredictedCost,
    SolverBudgetProfile,
    fidelity_cost_gate,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


_SHA256_PATTERN = r'^[0-9a-f]{64}$'

PrerunJobKind = Literal[
    'prediction_rectangular', 'prediction_provider', 'optimization_joint',
]
AxisRangeStatus = Literal[
    'in_range', 'extrapolated', 'out_of_range', 'unmodeled', 'missing',
]
EstimateConfidence = Literal[
    'calibrated', 'bounded_extrapolation', 'unknown',
]
RequiredAccelerator = Literal['none', 'gpu']

# Metric kinds the estimate covers. Aligned with cad_compute_budget's
# CostMetricKind; 'unknown' is never a dict key — a metric that cannot
# be estimated is simply ``None`` on the record.
_PRERUN_METRICS = ('runtime_s', 'peak_memory_bytes', 'storage_bytes')

# Soft advisory: predicted runtimes above this are flagged to the
# operator but never block execution.
LONG_RUNTIME_SOFT_LIMIT_S = 300.0

JOB_KIND_LABELS: dict[str, str] = {
    'prediction_rectangular': '予測（厳密矩形モード）',
    'prediction_provider': '予測（登録プロバイダー）',
    'optimization_joint': 'ジョイント最適化',
}
AXIS_LABELS: dict[str, str] = {
    'max_frequency_hz': '最大解析周波数(Hz)',
    'sound_speed_m_s': '音速(m/s)',
    'room_volume_m3': '室容積(m³)',
    'mode_grid_cells': 'モード探索格子数',
    'accepted_modes': '受理モード数(推定)',
    'speaker_count': 'スピーカー数',
    'receiver_count': '受音点数',
    'provider_sample_count': 'プロバイダー標本数',
    'candidate_count': '評価候補数',
    'physical_variable_count': '物理変数数',
    'dsp_variable_count': 'DSP変数数',
    'objective_count': '目標数',
}
AXIS_STATUS_LABELS: dict[str, str] = {
    'in_range': '校正範囲内',
    'extrapolated': '外挿',
    'out_of_range': '範囲外',
    'unmodeled': 'モデル未対応',
    'missing': '未供給',
}
CONFIDENCE_LABELS: dict[str, str] = {
    'calibrated': '校正済み',
    'bounded_extrapolation': '外挿（上限あり）',
    'unknown': '不明',
}
BLOCKING_REASON_LABELS: dict[str, str] = {
    'estimated_peak_memory_exceeds_device': '推定ピークメモリがデバイス容量を超過',
    'gpu_unavailable': '必要なGPUが利用不可',
    'invalid_plan': '実行計画が無効',
}
WARNING_LABELS: dict[str, str] = {
    'long_predicted_runtime': '推定実行時間が長い（上限が所定の目安を超過）',
    'bounded_extrapolation': '校正範囲外の外挿を含む推定',
    'estimate_unknown': 'コストモデル未対応のため推定不可',
    'memory_estimate_unknown': 'ピークメモリ推定なし（容量判定不可）',
    'gpu_capability_unknown': 'GPU能力が未検出のため判定不可',
    'non_rectangular_room': '非矩形の部屋 — モード走査は実行されません',
    'budget_limits_undeclared': '予算上限が未宣言のため予算判定は不明',
    'past_observations_present': '同一計画の過去実測あり（下に併記）',
}


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


class PrerunValueRange(BaseModel):
    """Declared [minimum, maximum] bound for one cost-model quantity."""

    model_config = ConfigDict(frozen=True)

    minimum: float = Field(ge=0)
    maximum: float = Field(ge=0)

    @model_validator(mode='after')
    def _ordered(self) -> 'PrerunValueRange':
        if self.maximum < self.minimum:
            raise ValueError('range maximum below minimum')
        return self


class PrerunJobPlan(BaseModel):
    """The exact compute-driving configuration of one pending run.

    ``plan_sha256`` seals every numeric driver plus solver identity and
    scene binding, so an estimate recorded against this plan cannot be
    reused for a different configuration without a visible hash change.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    job_kind: PrerunJobKind
    document_id: str = Field(min_length=1)
    scene_revision_id: str = ''
    scene_content_hash: str = ''
    solver_id: str = Field(min_length=1)
    solver_version: str = ''
    backend_label: str = 'cpu'
    #: The thing the run binds — receiver entity id or spec id.
    subject_id: str = ''
    #: Sealed sha of the bound subject (e.g. JointOptimizationSpec
    #: semantic_sha256); empty when the subject has no sealed form.
    subject_sha256: str = ''
    #: Numeric cost drivers actually bound by the run. Missing axes are
    #: absent — never filled with fabricated values.
    axes: dict[str, float] = Field(default_factory=dict)
    #: Non-numeric identity labels sealed into the plan (model lane,
    #: geometry kind, variant id, ...). Display + hash-binding only.
    bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def _validate(self) -> 'PrerunJobPlan':
        for name, value in self.axes.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'plan axis {name} must be finite and >= 0')
        if self.subject_sha256 and not (
            len(self.subject_sha256) == 64
            and all(c in '0123456789abcdef' for c in self.subject_sha256)
        ):
            raise ValueError('subject_sha256 must be 64-hex when set')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'PrerunJobPlan':
        return _seal(cls, kwargs, 'plan_id', 'plan_sha256', 'pplan')


class SolverCostModel(BaseModel):
    """Declared cost model for one job kind.

    ``calibrated_ranges`` bounds each axis the model was measured or
    derived over; ``*_terms`` map axis names (or ``'base'``) to per-unit
    cost ranges. A term whose axis is absent from the plan makes its
    metric UNKNOWN. An axis the model does not declare is reported
    ``unmodeled`` rather than silently costed.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    job_kind: PrerunJobKind
    basis: BudgetEvidenceMeans
    calibrated_ranges: dict[str, PrerunValueRange] = Field(
        default_factory=dict
    )
    runtime_terms_s: dict[str, PrerunValueRange] = Field(
        default_factory=dict
    )
    memory_terms_bytes: dict[str, PrerunValueRange] = Field(
        default_factory=dict
    )
    storage_terms_bytes: dict[str, PrerunValueRange] = Field(
        default_factory=dict
    )
    #: Inputs above calibrated_max * factor are out_of_range entirely.
    extrapolation_factor: float = Field(default=4.0, gt=1.0)
    required_accelerator: RequiredAccelerator = 'none'
    notes: str = ''

    @model_validator(mode='after')
    def _validate(self) -> 'SolverCostModel':
        for terms in (
            self.runtime_terms_s,
            self.memory_terms_bytes,
            self.storage_terms_bytes,
        ):
            for name in terms:
                if name == 'base':
                    continue
                if name not in self.calibrated_ranges:
                    raise ValueError(
                        f'cost term axis {name} lacks a calibrated range'
                    )
        if self.basis in ('observed_run',):
            raise ValueError(
                'declared cost models cannot claim observed_run basis; '
                'measured cost belongs to ComputeObservation'
            )
        return self


class EstimatedMetricRange(BaseModel):
    """One estimated metric as a range with its evidence basis."""

    model_config = ConfigDict(frozen=True)

    minimum: float = Field(ge=0)
    maximum: float = Field(ge=0)
    basis: BudgetEvidenceMeans
    extrapolated: bool = False
    note: str = ''

    @model_validator(mode='after')
    def _ordered(self) -> 'EstimatedMetricRange':
        if self.maximum < self.minimum:
            raise ValueError('estimate maximum below minimum')
        return self


class PrerunEstimate(BaseModel):
    """Sealed pre-run estimate, hash-pinned to the exact job plan."""

    model_config = ConfigDict(frozen=True)

    estimate_id: str
    estimate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    plan: PrerunJobPlan
    cost_model_id: str = Field(min_length=1)
    cost_model_version: str = Field(min_length=1)
    cost_model_basis: BudgetEvidenceMeans
    runtime_s: EstimatedMetricRange | None = None
    peak_memory_bytes: EstimatedMetricRange | None = None
    storage_bytes: EstimatedMetricRange | None = None
    confidence: EstimateConfidence
    axis_statuses: dict[str, AxisRangeStatus] = Field(default_factory=dict)
    out_of_range_reasons: tuple[str, ...] = ()
    budget_verdict: BudgetVerdict = 'budget_unknown'
    budget_reasons: tuple[str, ...] = ()
    fidelity_verdict: FidelityClaimVerdict = 'cost_evidence_missing'
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _validate(self) -> 'PrerunEstimate':
        if self.plan.document_id != self.document_id:
            raise ValueError('estimate document must match its plan')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'estimate_id', 'estimate_sha256'}
        )

    @classmethod
    def create(cls, **kwargs: Any) -> 'PrerunEstimate':
        return _seal(
            cls, kwargs, 'estimate_id', 'estimate_sha256', 'prest'
        )

    @property
    def executable(self) -> bool:
        """Soft warnings never block; only blocking reasons do."""
        return not self.blocking_reasons


# ----------------------------------------------------------------------
# Declared cost models. Constants measured on the Windows dev box
# (Python 3.13, CPU-only) 2026-10-09; ranges bracket the observed
# spread so the estimate is a bound, not a fake point value.
# ----------------------------------------------------------------------

RECTANGULAR_MODES_COST_MODEL = SolverCostModel(
    model_id='rectangular-modal-geometry',
    model_version='cost-2026-10-09-win313',
    job_kind='prediction_rectangular',
    basis='extrapolated_model',
    calibrated_ranges={
        # Measured: ~5us/cell enumeration, ~5.4us+~1KB per accepted mode.
        'mode_grid_cells': PrerunValueRange(minimum=0, maximum=2.0e5),
        'accepted_modes': PrerunValueRange(minimum=0, maximum=1.0e5),
        'speaker_count': PrerunValueRange(minimum=0, maximum=32),
    },
    runtime_terms_s={
        'base': PrerunValueRange(minimum=0.01, maximum=0.05),
        'mode_grid_cells': PrerunValueRange(minimum=3e-6, maximum=9e-6),
        'accepted_modes': PrerunValueRange(minimum=3e-6, maximum=1.5e-5),
        'speaker_count': PrerunValueRange(minimum=2e-4, maximum=3e-3),
    },
    memory_terms_bytes={
        'base': PrerunValueRange(minimum=2e6, maximum=8e6),
        'accepted_modes': PrerunValueRange(minimum=9e2, maximum=2.5e3),
        'speaker_count': PrerunValueRange(minimum=5e3, maximum=5e4),
    },
    storage_terms_bytes={
        'base': PrerunValueRange(minimum=1e3, maximum=1e4),
        'accepted_modes': PrerunValueRange(minimum=150, maximum=600),
        'speaker_count': PrerunValueRange(minimum=1e3, maximum=2e4),
    },
    notes=(
        'Calibrated 2026-10-09 on the Windows/Python3.13 dev box: '
        'rectangular_room_modes ~4.7-6.2us per grid cell, ~1KB per '
        'accepted mode record; persistence adds the same order again.'
    ),
)

PROVIDER_RESPONSE_COST_MODEL = SolverCostModel(
    model_id='provider-response-replay',
    model_version='cost-2026-10-09-win313',
    job_kind='prediction_provider',
    basis='extrapolated_model',
    calibrated_ranges={
        'provider_sample_count': PrerunValueRange(
            minimum=0, maximum=1.0e6
        ),
    },
    runtime_terms_s={
        'base': PrerunValueRange(minimum=0.005, maximum=0.1),
        'provider_sample_count': PrerunValueRange(
            minimum=1e-7, maximum=2e-6
        ),
    },
    memory_terms_bytes={
        'base': PrerunValueRange(minimum=1e6, maximum=4e6),
        'provider_sample_count': PrerunValueRange(
            minimum=40, maximum=400
        ),
    },
    storage_terms_bytes={
        'base': PrerunValueRange(minimum=1e3, maximum=1e4),
        'provider_sample_count': PrerunValueRange(
            minimum=20, maximum=200
        ),
    },
    notes=(
        'Provider lanes replay an exact persisted solver output: cost is '
        'band selection plus sealing of one result record.'
    ),
)

JOINT_EXECUTION_COST_MODEL = SolverCostModel(
    model_id='joint-optimization-execution',
    model_version='cost-2026-10-09-win313',
    job_kind='optimization_joint',
    basis='extrapolated_model',
    calibrated_ranges={
        # Measured ~0.19s per candidate (materialize + seal + persist,
        # real evaluator) on the Windows/Python3.13 dev box 2026-10-09.
        'candidate_count': PrerunValueRange(minimum=0, maximum=256),
        'dsp_variable_count': PrerunValueRange(minimum=0, maximum=16),
        'objective_count': PrerunValueRange(minimum=0, maximum=32),
    },
    runtime_terms_s={
        'base': PrerunValueRange(minimum=0.05, maximum=0.3),
        'candidate_count': PrerunValueRange(minimum=0.05, maximum=0.6),
    },
    memory_terms_bytes={
        'base': PrerunValueRange(minimum=5e6, maximum=3e7),
        'candidate_count': PrerunValueRange(minimum=2e4, maximum=5e5),
    },
    storage_terms_bytes={
        'base': PrerunValueRange(minimum=1e4, maximum=1e5),
        # variant + optional plan + candidate + evaluation bindings
        'candidate_count': PrerunValueRange(minimum=2e4, maximum=2e5),
    },
    notes=(
        'Per-candidate cost covers exact SystemVariant/CalibrationPlan '
        'materialization, re-validation, and sealed persistence.'
    ),
)

COST_MODELS: dict[str, SolverCostModel] = {
    model.job_kind: model
    for model in (
        RECTANGULAR_MODES_COST_MODEL,
        PROVIDER_RESPONSE_COST_MODEL,
        JOINT_EXECUTION_COST_MODEL,
    )
}


def cost_model_for(job_kind: str) -> SolverCostModel:
    model = COST_MODELS.get(job_kind)  # type: ignore[arg-type]
    if model is None:
        raise ValueError(f'no declared cost model for job kind {job_kind}')
    return model


# ----------------------------------------------------------------------
# Plan builders — extract the cost-driving axes from the real job config.
# ----------------------------------------------------------------------

def rectangular_mode_counts(
    width_m: float,
    depth_m: float,
    height_m: float,
    *,
    max_hz: float,
    sound_speed_m_s: float = 343.0,
) -> tuple[int, float]:
    """Exact mode-enumeration grid size + Weyl accepted-mode estimate.

    The enumeration grid size is computed with the same integer formula
    ``acoustics.rectangular_room_modes`` uses, so the dominant cost axis
    is exact — not sampled. The accepted-mode count uses the Weyl
    approximation (the exact count IS the enumeration being estimated).
    """
    cells = 1
    for dimension in (width_m, depth_m, height_m):
        cells *= max(
            1, math.floor((2.0 * max_hz * dimension) / sound_speed_m_s) + 1
        ) + 1
    volume = width_m * depth_m * height_m
    weyl_modes = (
        (4.0 * math.pi / 3.0)
        * volume
        * (max_hz / sound_speed_m_s) ** 3
    )
    return cells, weyl_modes


def plan_for_rectangular_prediction(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    receiver_entity_id: str,
    room_width_m: float | None,
    room_depth_m: float | None,
    room_height_m: float | None,
    speaker_count: int,
    max_mode_hz: float,
    sound_speed_m_s: float,
    solver_version: str,
) -> PrerunJobPlan:
    """Plan for the native rectangular-geometry prediction lane."""
    axes: dict[str, float] = {
        'max_frequency_hz': float(max_mode_hz),
        'sound_speed_m_s': float(sound_speed_m_s),
        'speaker_count': float(max(speaker_count, 0)),
        'receiver_count': 1.0,
    }
    if (
        room_width_m is not None
        and room_depth_m is not None
        and room_height_m is not None
    ):
        axes['room_volume_m3'] = room_width_m * room_depth_m * room_height_m
        cells, weyl_modes = rectangular_mode_counts(
            room_width_m,
            room_depth_m,
            room_height_m,
            max_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
        )
        axes['mode_grid_cells'] = float(cells)
        axes['accepted_modes'] = float(weyl_modes)
    return PrerunJobPlan.create(
        job_kind='prediction_rectangular',
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        solver_id='rectangular_geometry',
        solver_version=solver_version,
        backend_label='cpu',
        subject_id=receiver_entity_id,
        axes=axes,
        bindings={
            'geometry_kind': (
                'rectangular' if 'room_volume_m3' in axes else 'non_rectangular'
            ),
        },
    )


def plan_for_provider_prediction(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    receiver_entity_id: str,
    provider_id: str,
    provider_version: str,
    provider_semantic_sha256: str,
    max_mode_hz: float,
    provider_sample_count: int | None,
) -> PrerunJobPlan:
    """Plan for a persisted provider-response prediction lane (#938)."""
    axes: dict[str, float] = {
        'max_frequency_hz': float(max_mode_hz),
        'receiver_count': 1.0,
    }
    if provider_sample_count is not None:
        axes['provider_sample_count'] = float(
            max(provider_sample_count, 0)
        )
    return PrerunJobPlan.create(
        job_kind='prediction_provider',
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        solver_id=f'provider:{provider_id}',
        solver_version=provider_version,
        backend_label='cpu',
        subject_id=receiver_entity_id,
        subject_sha256=provider_semantic_sha256,
        axes=axes,
    )


def plan_for_joint_execution(
    *,
    spec_id: str,
    spec_sha256: str,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    decision_vector_count: int,
    candidate_budget: int,
    physical_variable_count: int,
    dsp_variable_count: int,
    objective_count: int,
    evaluator_version: str,
) -> PrerunJobPlan:
    """Plan for one persisted joint-optimization spec execution.

    ``candidate_count`` is the count the run will actually process —
    ``min(decision_vector_count, candidate_budget)`` — so the estimate
    reflects the immutable budget cap, not the theoretical grid size.
    """
    executed = max(
        0, min(int(decision_vector_count), int(candidate_budget))
    )
    return PrerunJobPlan.create(
        job_kind='optimization_joint',
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        solver_id='joint_optimization_execution',
        solver_version=evaluator_version,
        backend_label='cpu',
        subject_id=spec_id,
        subject_sha256=spec_sha256,
        axes={
            'candidate_count': float(executed),
            'physical_variable_count': float(
                max(physical_variable_count, 0)
            ),
            'dsp_variable_count': float(max(dsp_variable_count, 0)),
            'objective_count': float(max(objective_count, 0)),
        },
        bindings={
            'candidate_budget': str(int(candidate_budget)),
        },
    )


# ----------------------------------------------------------------------
# The estimator.
# ----------------------------------------------------------------------

def _axis_status(
    value: float, declared: PrerunValueRange | None, factor: float
) -> AxisRangeStatus:
    if declared is None:
        return 'unmodeled'
    if value <= declared.maximum:
        return 'in_range'
    if value <= declared.maximum * factor:
        return 'extrapolated'
    return 'out_of_range'


def _evaluate_terms(
    plan: PrerunJobPlan,
    model: SolverCostModel,
    terms: dict[str, PrerunValueRange],
    axis_statuses: dict[str, AxisRangeStatus],
    reasons: list[str],
    metric: str,
) -> EstimatedMetricRange | None:
    minimum = 0.0
    maximum = 0.0
    extrapolated = False
    unmodeled: list[str] = []
    for name, term in terms.items():
        if name == 'base':
            minimum += term.minimum
            maximum += term.maximum
            continue
        value = plan.axes.get(name)
        if value is None:
            axis_statuses[name] = 'missing'
            reasons.append(f'{metric}: axis {name} not supplied by plan')
            return None
        status = axis_statuses.get(name)
        if status == 'out_of_range':
            reasons.append(
                f'{metric}: axis {name}={value:g} outside calibrated '
                'range and extrapolation bound'
            )
            return None
        if status == 'extrapolated':
            extrapolated = True
        elif status == 'unmodeled':
            unmodeled.append(name)
        minimum += term.minimum * value
        axis_max = term.maximum * value
        if status == 'extrapolated':
            axis_max *= model.extrapolation_factor
        maximum += axis_max
    note = ''
    if unmodeled:
        note = 'unmodeled axes costed: ' + ','.join(sorted(unmodeled))
        extrapolated = True
    return EstimatedMetricRange(
        minimum=minimum,
        maximum=maximum,
        basis=model.basis,
        extrapolated=extrapolated,
        note=note,
    )


def estimate_prerun_cost(
    plan: PrerunJobPlan,
    model: SolverCostModel | None = None,
    *,
    budget_limits: dict[str, float] | None = None,
    device_memory_bytes: float | None = None,
    gpu_available: bool | None = None,
) -> PrerunEstimate:
    """Deterministic pre-run estimate for one exact job plan.

    ``budget_limits`` maps metric names (``runtime_s``,
    ``peak_memory_bytes``, ``storage_bytes``) to declared upper bounds —
    typically device capability. ``None``/empty → ``budget_unknown``.
    Limits keyed by metrics the estimate does not produce are
    ``incomparable``.
    """
    if model is None:
        model = cost_model_for(plan.job_kind)
    if model.job_kind != plan.job_kind:
        raise ValueError(
            f'cost model {model.model_id} does not cover '
            f'{plan.job_kind}'
        )

    axis_statuses: dict[str, AxisRangeStatus] = {}
    for name, value in plan.axes.items():
        axis_statuses[name] = _axis_status(
            value,
            model.calibrated_ranges.get(name),
            model.extrapolation_factor,
        )

    reasons: list[str] = []
    runtime = _evaluate_terms(
        plan, model, model.runtime_terms_s, axis_statuses, reasons,
        'runtime_s',
    )
    memory = _evaluate_terms(
        plan, model, model.memory_terms_bytes, axis_statuses, reasons,
        'peak_memory_bytes',
    )
    storage = _evaluate_terms(
        plan, model, model.storage_terms_bytes, axis_statuses, reasons,
        'storage_bytes',
    )
    metrics = {
        'runtime_s': runtime,
        'peak_memory_bytes': memory,
        'storage_bytes': storage,
    }

    # Only axes the model actually costs affect confidence — a context
    # axis the model never claims to drive (sound speed, receiver count)
    # reports 'unmodeled' for display without demoting the estimate.
    costed_axes = {
        name
        for terms in (
            model.runtime_terms_s,
            model.memory_terms_bytes,
            model.storage_terms_bytes,
        )
        for name in terms
        if name != 'base'
    }
    statuses = {
        axis_statuses[name]
        for name in costed_axes
        if name in axis_statuses
    }
    if any(metric is None for metric in metrics.values()):
        confidence: EstimateConfidence = 'unknown'
    elif statuses & {'extrapolated', 'unmodeled'}:
        confidence = 'bounded_extrapolation'
    else:
        confidence = 'calibrated'

    warnings: list[str] = []
    blocking: list[str] = []

    if model.required_accelerator == 'gpu':
        if gpu_available is False:
            blocking.append('gpu_unavailable')
        elif gpu_available is None:
            warnings.append('gpu_capability_unknown')

    if device_memory_bytes is not None:
        if memory is None:
            warnings.append('memory_estimate_unknown')
        elif memory.minimum > device_memory_bytes:
            blocking.append('estimated_peak_memory_exceeds_device')

    if confidence == 'bounded_extrapolation':
        warnings.append('bounded_extrapolation')
    if all(metric is None for metric in metrics.values()):
        warnings.append('estimate_unknown')
    if (
        runtime is not None
        and runtime.maximum > LONG_RUNTIME_SOFT_LIMIT_S
    ):
        warnings.append('long_predicted_runtime')
    if plan.bindings.get('geometry_kind') == 'non_rectangular':
        warnings.append('non_rectangular_room')

    # Budget verdict against declared limits only — never invented.
    budget_verdict: BudgetVerdict
    budget_reasons: list[str] = []
    known_limits = {
        key: value
        for key, value in (budget_limits or {}).items()
        if key in _PRERUN_METRICS
    }
    foreign_limits = set(budget_limits or ()) - set(_PRERUN_METRICS)
    if not budget_limits:
        budget_verdict = 'budget_unknown'
        budget_reasons.append('no_limits_declared')
        warnings.append('budget_limits_undeclared')
    elif not known_limits and foreign_limits:
        budget_verdict = 'incomparable'
        budget_reasons.append('limits_reference_unknown_metrics')
    else:
        over: list[str] = []
        unclear: list[str] = []
        for key, limit in known_limits.items():
            estimate = metrics[key]
            if estimate is None:
                unclear.append(f'unestimated_{key}')
            elif estimate.minimum > limit:
                over.append(f'predicted_over_{key}')
            elif estimate.maximum > limit:
                unclear.append(f'estimate_straddles_{key}')
        if foreign_limits:
            unclear.append('unmatched_limits')
        if over:
            budget_verdict = 'over_budget'
        elif unclear:
            budget_verdict = 'budget_unknown'
        else:
            budget_verdict = 'within_budget'
        budget_reasons.extend(over + unclear)

    # Reuse the #770 fidelity gate verbatim: predicted costs carry the
    # declared basis; unestimable metrics enter as means=unknown so a
    # silent omission can never read as cost evidence.
    predicted_costs: list[PredictedCost] = []
    for key, estimate in metrics.items():
        if estimate is None:
            predicted_costs.append(
                PredictedCost(
                    metric=key,  # type: ignore[arg-type]
                    value=0.0,
                    means='unknown',
                    notes='outside the cost model calibrated range',
                )
            )
        else:
            predicted_costs.append(
                PredictedCost(
                    metric=key,  # type: ignore[arg-type]
                    value=estimate.maximum,
                    means=estimate.basis,
                    notes='upper bound of estimated range',
                )
            )
    profile = SolverBudgetProfile.create(
        document_id=plan.document_id,
        fidelity_axes=dict(plan.axes),
        predicted_costs=tuple(predicted_costs),
        notes=(
            f'pre-run estimate profile: model={model.model_id} '
            f'{model.model_version}'
        ),
    )
    fidelity_verdict = fidelity_cost_gate(profile)

    return PrerunEstimate.create(
        document_id=plan.document_id,
        plan=plan,
        cost_model_id=model.model_id,
        cost_model_version=model.model_version,
        cost_model_basis=model.basis,
        runtime_s=runtime,
        peak_memory_bytes=memory,
        storage_bytes=storage,
        confidence=confidence,
        axis_statuses=axis_statuses,
        out_of_range_reasons=tuple(reasons),
        budget_verdict=budget_verdict,
        budget_reasons=tuple(budget_reasons),
        fidelity_verdict=fidelity_verdict,
        blocking_reasons=tuple(blocking),
        warnings=tuple(dict.fromkeys(warnings)),
    )


# ----------------------------------------------------------------------
# Post-run observation — real measurement only, never fabricated.
# ----------------------------------------------------------------------

def record_prerun_observation(
    *,
    document_id: str,
    estimate: PrerunEstimate,
    runtime_s: float,
    process_rss_bytes: float | None = None,
    hardware_label: str = '',
    observed_at_utc: str,
    run_ref: AuthorityRef | None = None,
    notes: str = '',
) -> ComputeObservation:
    """Mint a sealed observation from REAL measured run cost.

    ``runtime_s`` must come from an actual elapsed measurement of the
    completed run — callers record nothing on cancel/failure, so a
    non-positive duration is rejected rather than laundered. The process
    RSS sample is stored under its own key (``process_rss_bytes``): it is
    a completion-time sample, not a true peak, and never masquerades as
    ``peak_memory_bytes``.
    """
    if not math.isfinite(runtime_s) or runtime_s <= 0:
        raise ValueError(
            'observed runtime must be a positive measured value'
        )
    metrics: dict[str, float] = {'runtime_s': float(runtime_s)}
    if process_rss_bytes is not None:
        if not math.isfinite(process_rss_bytes) or process_rss_bytes <= 0:
            raise ValueError(
                'observed RSS must be a positive measured value'
            )
        metrics['process_rss_bytes'] = float(process_rss_bytes)
    estimate_ref = AuthorityRef(
        kind='prerun_estimate',
        ref_id=estimate.estimate_id,
        ref_sha256=estimate.estimate_sha256,
    )
    return ComputeObservation.create(
        document_id=document_id,
        run_ref=run_ref if run_ref is not None else estimate_ref,
        profile_ref=estimate_ref,
        metrics=metrics,
        hardware_label=hardware_label,
        observed_at_utc=observed_at_utc,
        notes=notes
        or (
            f'pre-run estimate {estimate.estimate_id}; rss is a '
            'completion-time process sample, not a true peak'
        ),
    )


def estimate_for_observation(
    observation: ComputeObservation,
) -> str | None:
    """The estimate id an observation binds, if it binds one."""
    for ref in (observation.run_ref, observation.profile_ref):
        if ref is not None and ref.kind == 'prerun_estimate':
            return ref.ref_id
    return None
