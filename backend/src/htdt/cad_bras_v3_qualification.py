"""#836 Actions 4–6 — BRAS v3 solver-qualification binding + report.

Action 4 binds the solver lanes *honestly*.  An imported BRAS v3 case
carries measured emitter/receiver positions, calibrated Pa impulse
responses and corpus references — but no sealed scene geometry, no
per-source :class:`DirectivityDataset` authority and no calibrated
source-strength authority.  Every native solver lane therefore fails
its own admission gates: the GA R150 lane compiles only sealed
closed-shell room policies (``exact_axis_aligned_closed_shoebox_v1`` /
``general_planar_closed_polyhedral_v1`` / portal multi-region), and the
R130 wave lane compiles closed rigid regions for project scenes only.
The binding produced here records ``produced_observables = ()`` per
lane plus the exact missing prerequisites — the capability gap — rather
than fabricating numbers through a lane that cannot lawfully run.

The one executable comparison lane is analytic: the
``analytic-direct-path`` provider predicts ``arrival_timing`` as the
free-field direct path ``distance / c`` between the *measured* emitter
and receiver positions bound to each measurement index.  That compares
two independent measured quantities (geometry vs onset) — an
import-consistency check, never solver qualification.  On the corpus
(RS1–RS4 verified) the residual is a stable sub-millisecond
measurement-chain latency.

Actions 5–6 evaluate every manifest observable under the sealed
qualification config and seal a report: ``pass``/``fail`` where a lane
emits predictions, ``unsupported``/``unobservable``/``missing``
elsewhere — never conflated with a numeric fail.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_registry import AuthorityRef
from .cad_benchmark import (
    BenchmarkCase,
    BenchmarkObservable,
    BenchmarkPoint,
    EvaluationProfile,
    evaluate_observable,
)
from .cad_bras_v3_importer import (
    BrasV3SceneImport,
    GeneralFirMeasurement,
)
from .cad_bras_v3_metrics import (
    BrasMetricEntry,
    BrasV3MetricManifest,
    MetricKind,
)
from .cad_external_benchmark_fixture import (
    AnalyticDirectPathProvider,
    FixtureRunMode,
    runtime_descriptor,
)
from .canonical_json import canonical_sha256 as _hash

BRAS_V3_QUALIFICATION_SCHEMA_VERSION = 1
BRAS_V3_QUALIFICATION_VERSION = 'bras-v3-qualification-1'

_SHA256 = r'^[0-9a-f]{64}$'

BrasV3ObservableVerdict = Literal[
    'pass', 'fail', 'missing', 'unobservable', 'unsupported',
]
"""Same vocabulary as the external-fixture layer (#948): ``missing`` =
a bound lane produced no prediction; ``unobservable`` = the measured
payload physically cannot observe it (band beyond Nyquist);
``unsupported`` = no bound lane can lawfully emit the kind."""

BrasV3RunStatus = Literal['pass', 'fail', 'incomplete', 'blocked']

BrasV3SolverLane = Literal['wave_r130', 'geometric_r150', 'hybrid']
"""Native solver lanes probed by the binding — the analytic direct-path
provider is a comparison lane, not a solver lane, and is never listed
here."""

ANALYTIC_LANE_ID = 'analytic-direct-path'

_PHASE_BEARING_KINDS = frozenset({'complex_transfer', 'impulse_window'})
_ABSOLUTE_LEVEL_TOLERANCE_UNITS = frozenset(
    {'pa', 'pa_s', 'pa·s', 'pascal', 'pa2', 'pa^2'}
)
_PASCAL_UNITS = frozenset({'pascal', 'pa'})

# Precise per-kind unlock statement used in ``unsupported`` reasons.
_KIND_UNLOCK = {
    'impulse_window': (
        'a wave-class solver lane emitting time-domain pressure would '
        'be required (impulse_window is a waveform comparison)'
    ),
    'decay_metric': (
        'a solver lane emitting band-filtered decay — or a wave lane '
        'whose IR output could be post-processed — would be required'
    ),
    'magnitude_fr': (
        'a solver lane with calibrated source-strength authority would '
        'be required (absolute level is not inferable from geometry)'
    ),
    'complex_transfer': (
        'a solver lane emitting coherent phase would be required'
    ),
}


class BrasV3QualificationError(Exception):
    """Fail-closed error for the qualification layer."""


# ---------------------------------------------------------------------------
# Sealed config — Action 4's "qualification config" artifact
# ---------------------------------------------------------------------------


class BrasV3QualificationConfig(BaseModel):
    """Sealed Action-4 qualification config.

    Declares — before any prediction exists — which member + measurement
    indexes the run binds, the onset convention shared with the metric
    manifest, the run mode, and the tolerance bound per observable kind.
    The config is a preregistration surface: tolerances are stated up
    front and can never be retrofitted to make verdicts pass.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = BRAS_V3_QUALIFICATION_SCHEMA_VERSION
    config_id: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    member_path: str = Field(min_length=1)
    measurement_indexes: tuple[int, ...] = Field(min_length=1)
    channel_index: int = Field(default=0, ge=0)
    onset_threshold: float = Field(gt=0.0, lt=1.0)
    run_mode: FixtureRunMode = 'preregistered_unfitted'
    informed_parameters: tuple[str, ...] = ()
    prior_unfitted_evidence_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    arrival_tolerance_s: float = Field(gt=0.0)
    decay_tolerance_s: float = Field(gt=0.0)
    magnitude_tolerance: float = Field(gt=0.0)
    magnitude_tolerance_unit: str = Field(min_length=1)
    impulse_window_tolerance: float = Field(gt=0.0)
    impulse_window_tolerance_unit: str = Field(min_length=1)
    speed_of_sound_m_per_s: float | None = Field(
        default=None, gt=0.0,
        description='None → temperature model / 343.0 m/s fallback',
    )
    evaluation_profile_id: str = Field(min_length=1)
    evaluation_profile_version: str = Field(min_length=1)
    tolerance_overrides: dict[str, float] = Field(default_factory=dict)
    config_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'config_id', 'config_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3QualificationConfig':
        if self.run_mode == 'informed_calibrated':
            if not self.informed_parameters:
                raise ValueError(
                    'informed_calibrated runs must name the tuned '
                    'parameters'
                )
            if self.prior_unfitted_evidence_sha256 is None:
                raise ValueError(
                    'informed_calibrated runs must pin the prior '
                    'unfitted evidence they calibrate against'
                )
        else:
            if self.informed_parameters:
                raise ValueError(
                    'preregistered_unfitted runs must not carry '
                    'informed parameters'
                )
            if self.prior_unfitted_evidence_sha256 is not None:
                raise ValueError(
                    'preregistered_unfitted runs must not pin prior '
                    'fitted evidence'
                )
        if len(set(self.measurement_indexes)) != len(
            self.measurement_indexes
        ):
            raise ValueError('duplicate measurement index')
        for observable_id, tolerance in self.tolerance_overrides.items():
            if not math.isfinite(float(tolerance)) or tolerance <= 0.0:
                raise ValueError(
                    f'tolerance override for {observable_id} must be '
                    'finite and positive'
                )
        if self.config_sha256 != _hash(self.identity_payload()):
            raise ValueError('qualification config sha mismatch')
        return self

    def evaluation_profile(self) -> EvaluationProfile:
        """The profile the generic evaluator sees (id + overrides)."""
        return EvaluationProfile(
            profile_id=self.evaluation_profile_id,
            version=self.evaluation_profile_version,
            tolerance_overrides=dict(self.tolerance_overrides),
        )


def build_bras_v3_qualification_config(
    *,
    member_path: str,
    measurement_indexes: Sequence[int],
    onset_threshold: float = 0.1,
    arrival_tolerance_s: float = 0.002,
    decay_tolerance_s: float = 0.05,
    magnitude_tolerance: float = 0.1,
    magnitude_tolerance_unit: str = 'pa',
    impulse_window_tolerance: float = 0.1,
    impulse_window_tolerance_unit: str = 'pa',
    channel_index: int = 0,
    run_mode: FixtureRunMode = 'preregistered_unfitted',
    informed_parameters: Sequence[str] = (),
    prior_unfitted_evidence_sha256: str | None = None,
    speed_of_sound_m_per_s: float | None = None,
    evaluation_profile_id: str = 'bras-v3-default',
    evaluation_profile_version: str = '1',
    tolerance_overrides: dict[str, float] | None = None,
    config_version: str = BRAS_V3_QUALIFICATION_VERSION,
) -> BrasV3QualificationConfig:
    """Seal a qualification config; every tolerance defaults to a
    declared bound the run must state even when no lane can produce the
    observable."""
    fields: dict[str, Any] = {
        'schema_version': BRAS_V3_QUALIFICATION_SCHEMA_VERSION,
        'config_id': 'pending',
        'config_version': config_version,
        'member_path': member_path,
        'measurement_indexes': tuple(int(i) for i in measurement_indexes),
        'channel_index': int(channel_index),
        'onset_threshold': float(onset_threshold),
        'run_mode': run_mode,
        'informed_parameters': tuple(informed_parameters),
        'prior_unfitted_evidence_sha256': prior_unfitted_evidence_sha256,
        'arrival_tolerance_s': float(arrival_tolerance_s),
        'decay_tolerance_s': float(decay_tolerance_s),
        'magnitude_tolerance': float(magnitude_tolerance),
        'magnitude_tolerance_unit': magnitude_tolerance_unit,
        'impulse_window_tolerance': float(impulse_window_tolerance),
        'impulse_window_tolerance_unit': impulse_window_tolerance_unit,
        'speed_of_sound_m_per_s': speed_of_sound_m_per_s,
        'evaluation_profile_id': evaluation_profile_id,
        'evaluation_profile_version': evaluation_profile_version,
        'tolerance_overrides': dict(tolerance_overrides or {}),
    }
    probe = BrasV3QualificationConfig.model_construct(
        **fields, config_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return BrasV3QualificationConfig(
        **{**fields, 'config_id': f'bqc-{digest[:12]}'},
        config_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Solver-lane binding — the Action-4 fail-closed artifact
# ---------------------------------------------------------------------------


class BrasV3SolverLaneBinding(BaseModel):
    """One native solver lane probed against the imported case.

    ``produced_observables`` is the honest output of the lane against
    this case — ``()`` whenever the lane is blocked.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    lane: BrasV3SolverLane
    verdict: Literal['capable', 'blocked']
    produced_observables: tuple[str, ...] = ()
    missing_prerequisites: tuple[str, ...] = ()
    limitation_reasons: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3SolverLaneBinding':
        if self.verdict == 'blocked' and self.produced_observables:
            raise ValueError(
                'a blocked lane cannot produce observables'
            )
        if self.verdict == 'blocked' and not (
            self.missing_prerequisites or self.limitation_reasons
        ):
            raise ValueError(
                'a blocked lane must record why it is blocked'
            )
        return self


class BrasV3SolverBinding(BaseModel):
    """Sealed solver-binding manifest for one imported scene.

    Records — per native solver lane — what the lane could lawfully
    emit for this case, with explicit limitation reasons when the
    answer is nothing.  ``produced_observables`` is the union across
    solver lanes; ``()`` is the honest record, not an omission.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    binding_id: str = Field(min_length=1)
    binding_version: str = Field(min_length=1)
    case_ref: AuthorityRef
    member_path: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)
    lanes: tuple[BrasV3SolverLaneBinding, ...] = Field(min_length=1)
    produced_observables: tuple[str, ...] = ()
    capability_gap: tuple[str, ...] = Field(min_length=1)
    binding_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'binding_id', 'binding_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3SolverBinding':
        union = tuple(
            sorted(
                {
                    observable_id
                    for lane in self.lanes
                    for observable_id in lane.produced_observables
                }
            )
        )
        if union != tuple(sorted(self.produced_observables)):
            raise ValueError(
                'produced_observables must equal the union across lanes'
            )
        if self.binding_sha256 != _hash(self.identity_payload()):
            raise ValueError('solver binding sha mismatch')
        return self


def _geometric_r150_blockers(case: BenchmarkCase) -> list[str]:
    """What the GA R150 lane requires that the imported case lacks."""
    blockers = [
        'sealed scene-geometry authority — case.geometry is None '
        '(the corpus publishes room geometry descriptively only; no '
        'sealed shell exists)',
        'per-source DirectivityDataset authority — the corpus ships no '
        'measured source directivity in an importable form and no '
        'SOFA→directivity adapter exists',
        'closed-shell room-policy boundary — GA room policies '
        '(exact_axis_aligned_closed_shoebox_v1 / '
        'general_planar_closed_polyhedral_v1 / portal multi-region) '
        'accept closed regions only',
        'region / portal / boundary-termination authority chain',
        'calibrated source-strength authority for level observables',
    ]
    return blockers


def _wave_r130_blockers(case: BenchmarkCase) -> list[str]:
    """What the PFFDTD wave lane requires that the imported case lacks."""
    return [
        'sealed closed-rigid-region geometry — case.geometry is None '
        '(the R100A/B compiler lane is closed-rigid-region only)',
        'a pinned PFFDTD execution binding for external benchmark '
        'scenes — the wave lane is bound to project scenes, not the '
        'corpus',
        'calibrated source-strength authority for level observables',
    ]


def bind_bras_v3_solver_lanes(
    case: BenchmarkCase,
    *,
    member_path: str,
    measurement_sha256: str,
    binding_version: str = BRAS_V3_QUALIFICATION_VERSION,
) -> BrasV3SolverBinding:
    """Action-4 binding: probe each native solver lane against what the
    imported case actually supplies and seal the verdicts.

    Lanes are probed, not asserted — the blocker list is derived from
    the case's fields; ``produced_observables`` stays ``()`` until a
    lane can lawfully run.
    """
    geometric_blockers = _geometric_r150_blockers(case)
    wave_blockers = _wave_r130_blockers(case)
    hybrid_blockers = sorted(
        set(geometric_blockers) | set(wave_blockers)
    )
    lanes = (
        BrasV3SolverLaneBinding(
            lane='geometric_r150',
            verdict='blocked',
            missing_prerequisites=tuple(geometric_blockers),
            limitation_reasons=(
                'deterministic GA execution input requires a sealed '
                'scene chain the imported case does not carry',
            ),
        ),
        BrasV3SolverLaneBinding(
            lane='wave_r130',
            verdict='blocked',
            missing_prerequisites=tuple(wave_blockers),
            limitation_reasons=(
                'the wave lane compiles closed rigid regions for '
                'project scenes only',
            ),
        ),
        BrasV3SolverLaneBinding(
            lane='hybrid',
            verdict='blocked',
            missing_prerequisites=tuple(hybrid_blockers),
            limitation_reasons=(
                'hybrid needs both constituent lanes — both blocked',
            ),
        ),
    )
    capability_gap = (
        'arrival_timing is the only observable a non-fabricating lane '
        'can emit — the analytic direct-path provider compares measured '
        'geometry (d/c) against the measured onset; it is an '
        'import-consistency check, not solver qualification',
        'impulse_window / decay_metric / magnitude_fr / '
        'complex_transfer references exist in the metric manifest but '
        'no solver lane can emit comparable predictions without sealed '
        'scene geometry, per-source directivity authority, and '
        'calibrated source strength',
    )
    fields: dict[str, Any] = {
        'binding_id': 'pending',
        'binding_version': binding_version,
        'case_ref': AuthorityRef(
            kind='benchmark_case',
            ref_id=case.benchmark_id,
            ref_sha256=case.semantic_sha256,
        ),
        'member_path': member_path,
        'measurement_sha256': measurement_sha256,
        'lanes': lanes,
        'produced_observables': (),
        'capability_gap': capability_gap,
    }
    probe = BrasV3SolverBinding.model_construct(
        **fields, binding_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return BrasV3SolverBinding(
        **{**fields, 'binding_id': f'bsb-{digest[:12]}'},
        binding_sha256=digest,
    )


# ---------------------------------------------------------------------------
# Per-observable verdicts — Action 5
# ---------------------------------------------------------------------------


class BrasV3ObservableResult(BaseModel):
    """One manifest observable evaluated under the declared tolerance."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable_id: str = Field(min_length=1)
    kind: MetricKind
    measurement_index: int = Field(ge=0)
    channel_index: int = Field(ge=0)
    lane: str | None = None
    verdict: BrasV3ObservableVerdict
    metric_id: str | None = None
    metric_version: str | None = None
    band_hz: tuple[float, float] | None = None
    window_s: tuple[float, float] | None = None
    tolerance: float | None = None
    tolerance_unit: str | None = None
    error: float | None = None
    reason: str | None = None


def _tolerance_for(
    config: BrasV3QualificationConfig, entry: BrasMetricEntry
) -> tuple[float, str]:
    """Declared tolerance + unit for one manifest entry; an explicit
    per-observable override wins the value, never the unit."""
    if entry.kind == 'arrival_timing':
        value, unit = config.arrival_tolerance_s, 's'
    elif entry.kind == 'decay_metric':
        value, unit = config.decay_tolerance_s, 's'
    elif entry.kind == 'magnitude_fr':
        value, unit = (
            config.magnitude_tolerance,
            config.magnitude_tolerance_unit,
        )
    elif entry.kind == 'impulse_window':
        value, unit = (
            config.impulse_window_tolerance,
            config.impulse_window_tolerance_unit,
        )
    else:  # pragma: no cover - MetricKind is closed
        raise BrasV3QualificationError(
            f'observable {entry.observable_id}: unknown kind '
            f'{entry.kind!r}'
        )
    override = config.tolerance_overrides.get(entry.observable_id)
    if override is not None:
        value = float(override)
    return value, unit


def _observable_for(
    entry: BrasMetricEntry, config: BrasV3QualificationConfig
) -> BenchmarkObservable:
    """Re-express a metric-manifest entry as a harness observable bound
    to the member's per-measurement point ids (``S{i}``/``R{i}``)."""
    tolerance, unit = _tolerance_for(config, entry)
    index = entry.measurement_index
    return BenchmarkObservable(
        observable_id=entry.observable_id,
        kind=entry.kind,
        source_id=f'S{index + 1}',
        receiver_id=f'R{index + 1}',
        valid_band_hz=entry.band_hz,
        window_s=entry.window_s,
        preprocessing=entry.derivation,
        metric_id=entry.metric_id,
        metric_version=entry.metric_version,
        tolerance=tolerance,
        tolerance_unit=unit,
        reference=dict(entry.reference),
    )


def _evaluate_decay_metric(
    reference: dict[str, Any],
    prediction: dict[str, Any],
    tolerance: float,
) -> tuple[BrasV3ObservableVerdict, float | None, str | None]:
    ref = reference.get('value_s')
    pred = prediction.get('value_s')
    if ref is None or pred is None:
        return 'missing', None, 'decay value missing'
    error = abs(float(pred) - float(ref))
    return ('pass' if error <= tolerance else 'fail'), error, None


def _evaluate_impulse_window(
    observable: BenchmarkObservable,
    reference: dict[str, Any],
    prediction: dict[str, Any],
    tolerance: float,
) -> tuple[BrasV3ObservableVerdict, float | None, str | None]:
    def _pairs(payload: dict[str, Any]) -> tuple[list[float], list[float]] | None:
        times = payload.get('time_s')
        values = payload.get('pressure')
        if (
            not isinstance(times, list)
            or not isinstance(values, list)
            or len(times) != len(values)
            or not times
        ):
            return None
        pairs = []
        for raw_t, raw_v in zip(times, values):
            t, v = float(raw_t), float(raw_v)
            if not (math.isfinite(t) and math.isfinite(v)):
                return None
            pairs.append((t, v))
        window = observable.window_s
        if window is not None:
            pairs = [p for p in pairs if window[0] <= p[0] <= window[1]]
        if not pairs:
            return None
        return [t for t, _ in pairs], [v for _, v in pairs]

    ref = _pairs(reference)
    pred = _pairs(prediction)
    if ref is None:
        return 'missing', None, 'reference impulse_window data missing'
    if pred is None or pred[0] != ref[0]:
        return 'missing', None, (
            'prediction impulse_window data missing or misaligned'
        )
    error = math.sqrt(
        sum((p - r) ** 2 for r, p in zip(ref[1], pred[1])) / len(ref[1])
    )
    return ('pass' if error <= tolerance else 'fail'), error, None


def _evaluate_magnitude_fr(
    reference: dict[str, Any],
    prediction: dict[str, Any],
    tolerance: float,
) -> tuple[BrasV3ObservableVerdict, float | None, str | None]:
    """BRAS v3 magnitude references are absolute pressures keyed
    ``frequency_hz`` + ``magnitude`` in the declared ``units`` — not the
    dB grids the canonical importer emits."""
    ref_f = reference.get('frequency_hz')
    ref_v = reference.get('magnitude')
    pred_f = prediction.get('frequency_hz')
    pred_v = prediction.get('magnitude')
    if (
        not isinstance(ref_f, list)
        or not isinstance(ref_v, list)
        or len(ref_f) != len(ref_v)
        or not ref_f
    ):
        return 'missing', None, 'reference magnitude_fr data missing'
    if (
        not isinstance(pred_f, list)
        or not isinstance(pred_v, list)
        or list(ref_f) != list(pred_f)
        or len(pred_v) != len(ref_v)
    ):
        return 'missing', None, (
            'prediction magnitude_fr data missing or misaligned'
        )
    error = math.sqrt(
        sum((float(p) - float(r)) ** 2 for r, p in zip(ref_v, pred_v))
        / len(ref_v)
    )
    return ('pass' if error <= tolerance else 'fail'), error, None


def evaluate_bras_v3_observable(
    entry: BrasMetricEntry,
    observable: BenchmarkObservable,
    prediction: dict[str, Any] | None,
    *,
    measurement: GeneralFirMeasurement,
    config: BrasV3QualificationConfig,
) -> BrasV3ObservableResult:
    """Eligibility-gated per-observable evaluation.

    Ordering is deliberate: physical unobservability (band beyond
    Nyquist) outranks authority gates (absolute-level units), which
    outrank lane capability (``unsupported``), which outranks a missing
    prediction.  Only then does a numeric pass/fail ever apply.
    """
    tolerance, unit = _tolerance_for(config, entry)
    base: dict[str, Any] = {
        'observable_id': entry.observable_id,
        'kind': entry.kind,
        'measurement_index': entry.measurement_index,
        'channel_index': entry.channel_index,
        'metric_id': entry.metric_id,
        'metric_version': entry.metric_version,
        'band_hz': entry.band_hz,
        'window_s': entry.window_s,
        'tolerance': tolerance,
        'tolerance_unit': unit,
    }

    def _result(
        verdict: BrasV3ObservableVerdict,
        *,
        lane: str | None = None,
        error: float | None = None,
        reason: str | None = None,
    ) -> BrasV3ObservableResult:
        return BrasV3ObservableResult(
            **base, lane=lane, verdict=verdict, error=error,
            reason=reason,
        )

    nyquist = measurement.sample_rate_hz / 2.0
    if entry.band_hz is not None and entry.band_hz[1] > nyquist:
        return _result(
            'unobservable',
            reason=(
                f'band {entry.band_hz[0]:.0f}–{entry.band_hz[1]:.0f} Hz '
                f'exceeds the measured IR Nyquist {nyquist:.1f} Hz'
            ),
        )
    if (
        unit.lower() in _ABSOLUTE_LEVEL_TOLERANCE_UNITS
        and measurement.data_ir_units.strip().lower()
        not in _PASCAL_UNITS
    ):
        return _result(
            'unsupported',
            reason=(
                f'absolute-level tolerance unit {unit!r} requires '
                f'pascal-calibrated data; member declares '
                f'Data.IR.Units={measurement.data_ir_units!r}'
            ),
        )
    if entry.kind != 'arrival_timing':
        unlock = _KIND_UNLOCK.get(entry.kind, 'no bound lane emits it')
        return _result(
            'unsupported',
            reason=(
                f'no bound lane emits {entry.kind} observables — all '
                'native solver lanes are blocked (see solver binding); '
                f'{unlock}'
            ),
        )
    if prediction is None:
        return _result(
            'missing',
            lane=ANALYTIC_LANE_ID,
            reason='analytic lane produced no prediction — the '
            'position binding is unresolved',
        )

    profile = config.evaluation_profile()
    delegated = evaluate_observable(observable, prediction, profile)
    mapping: dict[str, BrasV3ObservableVerdict] = {
        'PASS': 'pass',
        'FAIL': 'fail',
        'UNKNOWN': 'missing',
        'NOT_APPLICABLE': 'unsupported',
    }
    return _result(
        mapping[delegated.status],
        lane=ANALYTIC_LANE_ID,
        error=delegated.error,
        reason=delegated.reason,
    )


# ---------------------------------------------------------------------------
# Evidence — the sealed Action-4/5 run record
# ---------------------------------------------------------------------------


def _fold_verdicts(
    verdicts: Sequence[BrasV3ObservableVerdict],
) -> BrasV3RunStatus:
    if not verdicts:
        return 'blocked'
    if 'fail' in verdicts:
        return 'fail'
    if 'pass' in verdicts:
        return (
            'pass' if all(v == 'pass' for v in verdicts) else 'incomplete'
        )
    return 'blocked'


class BrasV3QualificationEvidence(BaseModel):
    """Sealed evidence for one ``scene x member x config`` run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = BRAS_V3_QUALIFICATION_SCHEMA_VERSION
    evidence_id: str = Field(min_length=1)
    case_ref: AuthorityRef
    run_case_sha256: str = Field(pattern=_SHA256)
    metric_manifest_ref: AuthorityRef
    solver_binding_ref: AuthorityRef
    member_path: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=_SHA256)
    run_mode: FixtureRunMode
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    provider_config_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    evaluation_profile_id: str = Field(min_length=1)
    evaluation_profile_version: str = Field(min_length=1)
    status: BrasV3RunStatus
    results: tuple[BrasV3ObservableResult, ...]
    verdict_counts: dict[str, int]
    runtime: dict[str, str]
    created_at_utc: str | None = Field(default=None, min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'evidence_id', 'evidence_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3QualificationEvidence':
        if self.case_ref.ref_sha256 is None:
            raise ValueError('case_ref must pin the case sha256')
        if self.metric_manifest_ref.ref_sha256 is None:
            raise ValueError('metric manifest ref must pin the sha256')
        if self.solver_binding_ref.ref_sha256 is None:
            raise ValueError('solver binding ref must pin the sha256')
        if self.evidence_sha256 != _hash(self.identity_payload()):
            raise ValueError('qualification evidence sha mismatch')
        return self


def _bound_measurement(
    scene_import: BrasV3SceneImport, member_path: str
) -> GeneralFirMeasurement:
    imported = [
        member
        for member in scene_import.sofa_members
        if member.disposition == 'imported'
    ]
    for index, member in enumerate(imported):
        if member.member_path == member_path:
            return scene_import.measurements[index]
    raise BrasV3QualificationError(
        f'member {member_path} is not an imported member of '
        f'{scene_import.scene_id}'
    )


def _run_case(
    case: BenchmarkCase,
    measurement: GeneralFirMeasurement,
    entries: Sequence[BrasMetricEntry],
    config: BrasV3QualificationConfig,
    member_path: str,
) -> BenchmarkCase:
    """Derive the sealed run case: the imported case re-bound to the
    *member's own* measured emitter/receiver positions plus the manifest
    observables.  Positions come from the corpus record — nothing is
    invented; the derived sha differs from the imported case's and both
    are recorded."""
    emit = measurement.emitter_positions_m
    recv = measurement.receiver_positions_m
    if (
        len(emit) != measurement.measurement_count
        or len(recv) != measurement.measurement_count
    ):
        raise BrasV3QualificationError(
            f'member {member_path} lacks per-measurement '
            'emitter/receiver positions — the analytic arrival lane '
            'cannot bind'
        )
    emitter_ids = measurement.emitter_ids or (None,) * len(emit)
    receiver_ids = measurement.receiver_ids or (None,) * len(recv)

    def _role(what: str, transducer_id: int | None) -> str:
        if transducer_id is None:
            return f'measured {what} position'
        return f'measured {what} position (transducer #{transducer_id})'

    sources = tuple(
        BenchmarkPoint(
            point_id=f'S{i + 1}',
            position_m=emit[i],
            role=_role('emitter', emitter_ids[i]),
        )
        for i in range(len(emit))
    )
    receivers = tuple(
        BenchmarkPoint(
            point_id=f'R{i + 1}',
            position_m=recv[i],
            role=_role('receiver', receiver_ids[i]),
        )
        for i in range(len(recv))
    )
    observables = tuple(_observable_for(entry, config) for entry in entries)
    preprocessing = (
        f'run case derived from the imported scene; sources/receivers '
        f're-bound to member {member_path} per-measurement positions; '
        'observables bound from the metric manifest'
    )
    probe = case.model_copy(
        update={
            'sources': sources,
            'receivers': receivers,
            'observables': observables,
            'preprocessing': preprocessing,
        }
    )
    fields = probe.model_dump(mode='python')
    fields.pop('semantic_sha256', None)
    return BenchmarkCase(
        **fields,
        semantic_sha256=_hash(probe.semantic_payload()),
    )


def run_bras_v3_qualification(
    scene_import: BrasV3SceneImport,
    manifest: BrasV3MetricManifest,
    config: BrasV3QualificationConfig,
    *,
    provider: AnalyticDirectPathProvider | None = None,
    created_at_utc: str | None = None,
) -> tuple[BrasV3QualificationEvidence, BrasV3SolverBinding]:
    """Action 4+5 execution: bind the solver lanes (fail-closed record),
    run the analytic arrival lane over the manifest's arrival
    observables, evaluate every manifest observable under the declared
    tolerances and seal the evidence.

    Returns ``(evidence, solver_binding)`` — the binding is produced
    deterministically here and its sha is pinned by the evidence, so a
    replay can rebuild it identically.

    Frozen-config checks: member path, measurement indexes, channel
    index, onset threshold and measurement hash must match the sealed
    manifest exactly — drift raises, never re-pins.
    """
    case = scene_import.case
    if manifest.member_path != config.member_path:
        raise BrasV3QualificationError(
            'config member_path does not match the metric manifest'
        )
    manifest_indexes = {
        entry.measurement_index for entry in manifest.observables
    }
    if set(config.measurement_indexes) != manifest_indexes:
        raise BrasV3QualificationError(
            'config measurement_indexes do not match the manifest: '
            f'{sorted(manifest_indexes)}'
        )
    thresholds = {
        entry.onset_threshold
        for entry in manifest.observables
        if entry.kind == 'arrival_timing'
    }
    if thresholds != {config.onset_threshold}:
        raise BrasV3QualificationError(
            f'config onset_threshold {config.onset_threshold} does not '
            f'match the manifest convention {sorted(thresholds)}'
        )
    measurement = _bound_measurement(scene_import, manifest.member_path)
    if measurement.measurement_sha256 != manifest.measurement_sha256:
        raise BrasV3QualificationError(
            'bound member IR hash does not match the metric manifest'
        )
    if not (0 <= config.channel_index < measurement.channel_count):
        raise BrasV3QualificationError(
            f'channel_index {config.channel_index} outside '
            f'{measurement.channel_count}'
        )
    if any(
        entry.channel_index != config.channel_index
        for entry in manifest.observables
    ):
        raise BrasV3QualificationError(
            'manifest channel indexes do not match the config'
        )
    for entry in manifest.observables:
        if entry.measurement_index >= measurement.measurement_count:
            raise BrasV3QualificationError(
                f'observable {entry.observable_id}: measurement index '
                f'{entry.measurement_index} outside '
                f'{measurement.measurement_count}'
            )

    binding = bind_bras_v3_solver_lanes(
        case,
        member_path=manifest.member_path,
        measurement_sha256=manifest.measurement_sha256,
    )
    run_case = _run_case(
        case,
        measurement,
        manifest.observables,
        config,
        manifest.member_path,
    )
    provider = provider or AnalyticDirectPathProvider(
        speed_of_sound_m_per_s=config.speed_of_sound_m_per_s
    )
    if provider.provider_id != ANALYTIC_LANE_ID:
        raise BrasV3QualificationError(
            f'provider {provider.provider_id!r} is not the declared '
            f'analytic lane {ANALYTIC_LANE_ID!r}'
        )
    predictions = provider.predict(run_case)
    observables = {
        observable.observable_id: observable
        for observable in run_case.observables
    }
    results = tuple(
        evaluate_bras_v3_observable(
            entry,
            observables[entry.observable_id],
            predictions.get(entry.observable_id),
            measurement=measurement,
            config=config,
        )
        for entry in manifest.observables
    )
    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    status = _fold_verdicts([result.verdict for result in results])

    fields: dict[str, Any] = {
        'schema_version': BRAS_V3_QUALIFICATION_SCHEMA_VERSION,
        'evidence_id': 'pending',
        'case_ref': AuthorityRef(
            kind='benchmark_case',
            ref_id=case.benchmark_id,
            ref_sha256=case.semantic_sha256,
        ),
        'run_case_sha256': run_case.semantic_sha256,
        'metric_manifest_ref': AuthorityRef(
            kind='bras_v3_metric_manifest',
            ref_id=manifest.manifest_id,
            ref_sha256=manifest.manifest_sha256,
        ),
        'solver_binding_ref': AuthorityRef(
            kind='bras_v3_solver_binding',
            ref_id=binding.binding_id,
            ref_sha256=binding.binding_sha256,
        ),
        'member_path': manifest.member_path,
        'measurement_sha256': manifest.measurement_sha256,
        'run_mode': config.run_mode,
        'provider_id': provider.provider_id,
        'provider_version': provider.provider_version,
        'provider_config_sha256': provider.config_sha256(),
        'evaluation_profile_id': config.evaluation_profile_id,
        'evaluation_profile_version': config.evaluation_profile_version,
        'status': status,
        'results': results,
        'verdict_counts': counts,
        'runtime': runtime_descriptor(),
        'created_at_utc': created_at_utc,
    }
    probe = BrasV3QualificationEvidence.model_construct(
        **fields, evidence_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    evidence = BrasV3QualificationEvidence(
        **{**fields, 'evidence_id': f'bqe-{digest[:12]}'},
        evidence_sha256=digest,
    )
    return evidence, binding


# ---------------------------------------------------------------------------
# Report — Action 6 surfaces
# ---------------------------------------------------------------------------


class BrasV3QualificationReport(BaseModel):
    """Sealed Action-6 report: the qualification story for one scene
    member across its evidence runs — verdicts, the solver-lane binding
    and the honest claim ceiling."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    report_version: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    member_path: str = Field(min_length=1)
    case_ref: AuthorityRef
    metric_manifest_ref: AuthorityRef
    solver_binding_ref: AuthorityRef
    solver_lanes: tuple[BrasV3SolverLaneBinding, ...]
    evidence_refs: tuple[AuthorityRef, ...] = Field(min_length=1)
    run_statuses: tuple[str, ...] = Field(min_length=1)
    results: tuple[BrasV3ObservableResult, ...]
    verdict_counts: dict[str, int]
    claim_ceiling: str = Field(min_length=1)
    capability_gap: tuple[str, ...] = Field(min_length=1)
    report_sha256: str = Field(pattern=_SHA256)

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'report_id', 'report_sha256'}
        )

    @model_validator(mode='after')
    def _check(self) -> 'BrasV3QualificationReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('qualification report sha mismatch')
        return self


def build_bras_v3_qualification_report(
    *,
    scene_import: BrasV3SceneImport,
    manifest: BrasV3MetricManifest,
    binding: BrasV3SolverBinding,
    evidences: Sequence[BrasV3QualificationEvidence],
    report_version: str = BRAS_V3_QUALIFICATION_VERSION,
) -> BrasV3QualificationReport:
    """Aggregate one scene's evidence runs into the sealed report."""
    if not evidences:
        raise BrasV3QualificationError(
            'a report requires at least one evidence run'
        )
    for evidence in evidences:
        if (
            evidence.solver_binding_ref.ref_sha256
            != binding.binding_sha256
        ):
            raise BrasV3QualificationError(
                f'evidence {evidence.evidence_id} pins a different '
                'solver binding'
            )
        if (
            evidence.metric_manifest_ref.ref_sha256
            != manifest.manifest_sha256
        ):
            raise BrasV3QualificationError(
                f'evidence {evidence.evidence_id} pins a different '
                'metric manifest'
            )
    results = tuple(
        result
        for evidence in evidences
        for result in evidence.results
    )
    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    solver_produced = tuple(
        observable_id
        for lane in binding.lanes
        for observable_id in lane.produced_observables
    )
    evaluated = counts.get('pass', 0) + counts.get('fail', 0)
    claim_ceiling = (
        'no solver-qualification claim: every native solver lane '
        f'produced {len(solver_produced)} observable(s); the '
        f'{evaluated} numerically evaluated verdict(s) come from the '
        'analytic direct-path lane and constitute import-consistency '
        'evidence only'
    )
    fields: dict[str, Any] = {
        'report_id': 'pending',
        'report_version': report_version,
        'scene_id': scene_import.scene_id,
        'member_path': manifest.member_path,
        'case_ref': AuthorityRef(
            kind='benchmark_case',
            ref_id=scene_import.case.benchmark_id,
            ref_sha256=scene_import.case.semantic_sha256,
        ),
        'metric_manifest_ref': AuthorityRef(
            kind='bras_v3_metric_manifest',
            ref_id=manifest.manifest_id,
            ref_sha256=manifest.manifest_sha256,
        ),
        'solver_binding_ref': AuthorityRef(
            kind='bras_v3_solver_binding',
            ref_id=binding.binding_id,
            ref_sha256=binding.binding_sha256,
        ),
        'solver_lanes': binding.lanes,
        'evidence_refs': tuple(
            AuthorityRef(
                kind='bras_v3_qualification_evidence',
                ref_id=evidence.evidence_id,
                ref_sha256=evidence.evidence_sha256,
            )
            for evidence in evidences
        ),
        'run_statuses': tuple(
            evidence.status for evidence in evidences
        ),
        'results': results,
        'verdict_counts': counts,
        'claim_ceiling': claim_ceiling,
        'capability_gap': binding.capability_gap,
    }
    probe = BrasV3QualificationReport.model_construct(
        **fields, report_sha256='0' * 64
    )
    digest = _hash(probe.identity_payload())
    return BrasV3QualificationReport(
        **{**fields, 'report_id': f'bqr-{digest[:12]}'},
        report_sha256=digest,
    )


def render_bras_v3_report_markdown(
    report: BrasV3QualificationReport,
) -> str:
    """Render the sealed report as human-readable markdown — the
    Action-6 report surface."""
    lines: list[str] = []
    lines.append(f'# BRAS v3 qualification report — {report.scene_id}')
    lines.append('')
    lines.append(
        f'- report: `{report.report_id}` (v{report.report_version})'
    )
    lines.append(f'- member: `{report.member_path}`')
    lines.append(
        f'- run status(es): '
        + ', '.join(f'`{status}`' for status in report.run_statuses)
    )
    lines.append(f'- claim ceiling: {report.claim_ceiling}')
    lines.append('')
    lines.append('## Solver lanes (Action 4 binding)')
    lines.append('')
    lines.append(
        '| lane | verdict | produced observables | missing prerequisites |'
    )
    lines.append('|---|---|---|---|')
    for lane in report.solver_lanes:
        produced = ', '.join(lane.produced_observables) or '—'
        missing = '<br>'.join(lane.missing_prerequisites)
        lines.append(
            f'| `{lane.lane}` | {lane.verdict} | {produced} | {missing} |'
        )
    lines.append('')
    lines.append('## Observable verdicts (Actions 4–5)')
    lines.append('')
    lines.append(
        '| observable | kind | verdict | error | tolerance | reason |'
    )
    lines.append('|---|---|---|---|---|---|')
    for result in report.results:
        error = '—' if result.error is None else f'{result.error:.6g}'
        tolerance = (
            '—'
            if result.tolerance is None
            else f'{result.tolerance:g} {result.tolerance_unit or ""}'
        )
        reason = result.reason or '—'
        lines.append(
            f'| `{result.observable_id}` | {result.kind} | '
            f'{result.verdict} | {error} | {tolerance} | {reason} |'
        )
    lines.append('')
    counts = ', '.join(
        f'{verdict}={count}'
        for verdict, count in sorted(report.verdict_counts.items())
    )
    lines.append(f'Verdict counts: {counts}')
    lines.append('')
    lines.append('## Capability gap')
    lines.append('')
    for gap in report.capability_gap:
        lines.append(f'- {gap}')
    lines.append('')
    lines.append('## Honesty notes')
    lines.append('')
    lines.append(
        '- `unsupported` / `unobservable` / `missing` verdicts are '
        'never conflated with a numeric `fail`.'
    )
    lines.append(
        '- The analytic direct-path lane compares measured geometry '
        'against the measured onset — it is not a solver and confers '
        'no solver-qualification level.'
    )
    lines.append(
        '- No room geometry, directivity or source-strength authority '
        'was fabricated anywhere in the pipeline.'
    )
    lines.append('')
    return '\n'.join(lines)


__all__ = [
    'ANALYTIC_LANE_ID',
    'BRAS_V3_QUALIFICATION_SCHEMA_VERSION',
    'BRAS_V3_QUALIFICATION_VERSION',
    'BrasV3ObservableResult',
    'BrasV3ObservableVerdict',
    'BrasV3QualificationConfig',
    'BrasV3QualificationError',
    'BrasV3QualificationEvidence',
    'BrasV3QualificationReport',
    'BrasV3RunStatus',
    'BrasV3SolverBinding',
    'BrasV3SolverLane',
    'BrasV3SolverLaneBinding',
    'bind_bras_v3_solver_lanes',
    'build_bras_v3_qualification_config',
    'build_bras_v3_qualification_report',
    'evaluate_bras_v3_observable',
    'render_bras_v3_report_markdown',
    'run_bras_v3_qualification',
]
