"""External benchmark import & cross-solver validation harness (#875).

Turns legally usable external benchmark datasets into exact, replayable
HTDT validation cases and runs bounded per-observable comparisons:

- :class:`BenchmarkSourceAsset` — immutable identity of the source
  dataset/file: dataset name + version, origin URI, exact content hash,
  license/admission reference into the #834 ledger, and an explicit
  evidence class. An external *simulated* dataset is never promoted to
  independent measured evidence; the class is part of the asset's hash.
- :class:`BenchmarkCase` — versioned immutable normalized case: source
  asset ref, coordinate convention, geometry/sources/receivers/
  materials/environment as provided, sample rate/frequency grid/time
  origin, preprocessing, limitations, observable inventory, importer
  identity and semantic hash. Fields not supplied by the source stay
  ``None`` — the importer never invents geometry or material authority.
- Importers are pure transforms returning ``BenchmarkCase``;
  :func:`canonical_json_importer` parses the documented canonical JSON
  format and fails closed on ambiguous conventions (no coordinate
  convention, unparseable hash, missing observable inventory).
- :func:`evaluate_observable` produces per-observable results with the
  exact band/window, preprocessing, metric definition/version and
  tolerance — never one aggregate score; missing reference data or an
  unsupported observable kind is ``UNKNOWN``/``NOT_APPLICABLE``, never
  ``PASS``.
- :func:`run_benchmark` binds ``BenchmarkCase x PredictionProvider x
  EvaluationProfile`` into machine-readable
  :class:`BenchmarkValidationEvidence` carrying exact identity for
  replay: case hash, importer, provider id/version/config hash,
  evaluation profile, per-observable results. No PASS is produced from
  missing observables.
"""

from __future__ import annotations

import json
import math
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash




BENCHMARK_SCHEMA_VERSION: Literal[1] = 1
CANONICAL_JSON_FORMAT = 'htdt-benchmark-case-1'

BenchmarkEvidenceClass = Literal[
    'analytic',
    'independent_numerical',
    'external_simulated',
    'external_measured',
]
"""How independent the reference actually is — surfaced verbatim into
corpus/dashboard ingestion; an external simulated dataset is not
independent measured evidence."""

ObservableKind = Literal[
    'magnitude_fr',
    'complex_transfer',
    'arrival_timing',
    'impulse_window',
    'decay_metric',
]

BenchmarkResultStatus = Literal['PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE']


class BenchmarkSourceAsset(BaseModel):
    """Immutable identity + provenance of one admitted benchmark asset."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BENCHMARK_SCHEMA_VERSION
    asset_id: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    origin_uri: str | None = Field(default=None, min_length=1)
    content_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    license_id: str | None = Field(default=None, min_length=1)
    admission_ref: str | None = Field(
        default=None,
        min_length=1,
        description='reference into the #834 admission ledger',
    )
    evidence_class: BenchmarkEvidenceClass
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_asset(self) -> 'BenchmarkSourceAsset':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('BenchmarkSourceAsset semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'asset_id': self.asset_id,
            'dataset_name': self.dataset_name,
            'dataset_version': self.dataset_version,
            'origin_uri': self.origin_uri,
            'content_sha256': self.content_sha256,
            'license_id': self.license_id,
            'admission_ref': self.admission_ref,
            'evidence_class': self.evidence_class,
            'provenance': [
                item.model_dump(mode='json') for item in self.provenance
            ],
        }


class BenchmarkPoint(BaseModel):
    """One source or receiver point in the case's declared convention."""

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1)
    position_m: tuple[float, float, float] | None = None
    orientation_deg: tuple[float, float, float] | None = None
    role: str | None = Field(
        default=None,
        description='free-form role/excitation semantics as documented',
    )


class BenchmarkObservable(BaseModel):
    """One comparable observable with its exact evaluation window."""

    model_config = ConfigDict(frozen=True)

    observable_id: str = Field(min_length=1)
    kind: ObservableKind
    receiver_id: str | None = Field(default=None, min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    valid_band_hz: tuple[float, float] | None = None
    window_s: tuple[float, float] | None = None
    preprocessing: str | None = Field(default=None, min_length=1)
    metric_id: str = Field(min_length=1)
    metric_version: str = Field(min_length=1)
    tolerance: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    tolerance_unit: str | None = Field(default=None, min_length=1)
    reference: dict[str, Any]
    """Reference payload keyed by observable kind:

    - ``magnitude_fr``: ``frequency_hz``: [..], ``magnitude_db``: [..]
    - ``complex_transfer``: ``frequency_hz``: [..], ``real``: [..],
      ``imaginary``: [..]
    - ``arrival_timing``: ``arrival_s``: float
    - ``impulse_window``: ``time_s``: [..], ``pressure``: [..]
    - ``decay_metric``: ``value_s``: float (e.g. T20/T30 in seconds)
    """


class BenchmarkCase(BaseModel):
    """Versioned immutable normalized external benchmark case."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BENCHMARK_SCHEMA_VERSION
    benchmark_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    source_asset: BenchmarkSourceAsset
    coordinate_convention: str | None = Field(default=None, min_length=1)
    geometry: dict[str, Any] | None = None
    sources: tuple[BenchmarkPoint, ...] = ()
    receivers: tuple[BenchmarkPoint, ...] = ()
    materials: dict[str, Any] | None = None
    environment: dict[str, Any] | None = None
    sample_rate_hz: float | None = Field(default=None, gt=0.0)
    frequency_grid_hz: tuple[float, ...] | None = None
    time_origin_s: float | None = None
    preprocessing: str | None = Field(default=None, min_length=1)
    limitations: str | None = Field(default=None, min_length=1)
    observables: tuple[BenchmarkObservable, ...] = ()
    importer_id: str = Field(min_length=1)
    importer_version: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_case(self) -> 'BenchmarkCase':
        if self.semantic_sha256 != _hash(self.semantic_payload()):
            raise ValueError('BenchmarkCase semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'benchmark_id': self.benchmark_id,
            'version': self.version,
            'title': self.title,
            'source_asset': self.source_asset.semantic_payload(),
            'coordinate_convention': self.coordinate_convention,
            'geometry': self.geometry,
            'sources': [item.model_dump(mode='json') for item in self.sources],
            'receivers': [
                item.model_dump(mode='json') for item in self.receivers
            ],
            'materials': self.materials,
            'environment': self.environment,
            'sample_rate_hz': self.sample_rate_hz,
            'frequency_grid_hz': (
                None
                if self.frequency_grid_hz is None
                else list(self.frequency_grid_hz)
            ),
            'time_origin_s': self.time_origin_s,
            'preprocessing': self.preprocessing,
            'limitations': self.limitations,
            'observables': [
                item.model_dump(mode='json') for item in self.observables
            ],
            'importer_id': self.importer_id,
            'importer_version': self.importer_version,
        }

    def observable(self, observable_id: str) -> BenchmarkObservable | None:
        for observable in self.observables:
            if observable.observable_id == observable_id:
                return observable
        return None


class EvaluationProfile(BaseModel):
    """Bounded evaluation settings bound into the evidence identity."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    # Per-observable tolerance overrides by observable_id.
    tolerance_overrides: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode='after')
    def finite_overrides(self) -> 'EvaluationProfile':
        for observable_id, tolerance in self.tolerance_overrides.items():
            if not math.isfinite(float(tolerance)) or tolerance <= 0.0:
                raise ValueError(
                    f'tolerance override for {observable_id} must be finite and positive'
                )
        return self


class ObservableEvaluation(BaseModel):
    """Result of evaluating one observable against its reference."""

    model_config = ConfigDict(frozen=True)

    observable_id: str = Field(min_length=1)
    kind: ObservableKind | None = None
    status: BenchmarkResultStatus
    metric_id: str | None = None
    metric_version: str | None = None
    tolerance: float | None = None
    tolerance_unit: str | None = None
    valid_band_hz: tuple[float, float] | None = None
    window_s: tuple[float, float] | None = None
    error: float | None = None
    reason: str | None = None


class BenchmarkValidationEvidence(BaseModel):
    """Machine-readable evidence for one ``case x provider x profile``
    run — replayable from the recorded exact identities."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BENCHMARK_SCHEMA_VERSION
    evidence_id: str = Field(min_length=1)
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    benchmark_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    importer_id: str = Field(min_length=1)
    importer_version: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    provider_config_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    evaluation_profile_id: str = Field(min_length=1)
    evaluation_profile_version: str = Field(min_length=1)
    evidence_class: BenchmarkEvidenceClass
    status: BenchmarkResultStatus
    observable_evaluations: tuple[ObservableEvaluation, ...]
    unsupported_count: int = Field(ge=0)
    created_at_utc: str | None = Field(default=None, min_length=1)


class PredictionProvider(Protocol):
    """Solver-neutral prediction boundary — a provider supplies
    per-observable predictions for a normalized case."""

    provider_id: str
    provider_version: str

    def predict(self, case: BenchmarkCase) -> dict[str, Any]: ...

    def config_sha256(self) -> str | None: ...


class StaticReplayProvider:
    """Provider bound to stored predictions — supports exact replay of a
    recorded run and unit verification of the harness itself."""

    def __init__(
        self,
        predictions: dict[str, Any],
        *,
        provider_id: str = 'static-replay',
        provider_version: str = '1',
    ) -> None:
        self._predictions = dict(predictions)
        self.provider_id = provider_id
        self.provider_version = provider_version

    def predict(self, case: BenchmarkCase) -> dict[str, Any]:
        return dict(self._predictions)

    def config_sha256(self) -> str | None:
        return _hash(self._predictions)


def _band_values(
    observable: BenchmarkObservable,
    reference: dict[str, Any],
    key: str,
) -> tuple[list[float], list[float], BenchmarkResultStatus | None]:
    """Return (band-filtered frequencies, values) or a failure status."""
    frequencies = reference.get('frequency_hz')
    values = reference.get(key)
    if (
        not isinstance(frequencies, list)
        or not isinstance(values, list)
        or len(frequencies) != len(values)
        or not frequencies
    ):
        return [], [], 'UNKNOWN'
    pairs: list[tuple[float, float]] = []
    for raw_f, raw_v in zip(frequencies, values):
        try:
            f_value, v_value = float(raw_f), float(raw_v)
        except (TypeError, ValueError):
            return [], [], 'UNKNOWN'
        # A non-finite frequency must not silently drop a sample out of band.
        if not math.isfinite(f_value) or not math.isfinite(v_value):
            return [], [], 'UNKNOWN'
        pairs.append((f_value, v_value))
    band = observable.valid_band_hz
    pairs = [
        pair for pair in pairs
        if band is None or (band[0] <= pair[0] <= band[1])
    ]
    if not pairs:
        return [], [], 'UNKNOWN'
    return (
        [f for f, _ in pairs],
        [v for _, v in pairs],
        None,
    )


def _rms_error(reference: Sequence[float], predicted: Sequence[float]) -> float:
    return math.sqrt(
        sum((p - r) ** 2 for r, p in zip(reference, predicted)) / len(reference)
    )


def evaluate_observable(
    observable: BenchmarkObservable,
    prediction: dict[str, Any] | None,
    profile: EvaluationProfile | None = None,
) -> ObservableEvaluation:
    """Evaluate one observable against its reference — never aggregate.

    ``prediction`` carries the same payload shape as ``reference``. Missing
    reference/prediction data is ``UNKNOWN``; an observable kind the harness
    does not implement is ``NOT_APPLICABLE``.
    """
    tolerance = (
        profile.tolerance_overrides.get(observable.observable_id)
        if profile is not None
        else None
    )
    if tolerance is None:
        tolerance = observable.tolerance
    base = {
        'observable_id': observable.observable_id,
        'kind': observable.kind,
        'metric_id': observable.metric_id,
        'metric_version': observable.metric_version,
        'tolerance': tolerance,
        'tolerance_unit': observable.tolerance_unit,
        'valid_band_hz': observable.valid_band_hz,
        'window_s': observable.window_s,
    }

    def _result(
        status: BenchmarkResultStatus,
        *,
        error: float | None = None,
        reason: str | None = None,
    ) -> ObservableEvaluation:
        return ObservableEvaluation(
            **base, status=status, error=error, reason=reason
        )

    reference = observable.reference
    if prediction is None:
        return _result('UNKNOWN', reason='no prediction supplied')
    if tolerance is None:
        return _result('UNKNOWN', reason='no tolerance bound supplied')

    if observable.kind == 'magnitude_fr':
        ref_freqs, ref_values, failure = _band_values(
            observable, reference, 'magnitude_db'
        )
        if failure is not None:
            return _result('UNKNOWN', reason='reference magnitude_fr data missing')
        pred_freqs, pred_values, failure = _band_values(
            observable, prediction, 'magnitude_db'
        )
        # Pairing is positional: identical counts at different frequencies
        # would compare values against the wrong grid point.
        if failure is not None or pred_freqs != ref_freqs:
            return _result('UNKNOWN', reason='prediction magnitude_fr data missing or misaligned')
        error = _rms_error(ref_values, pred_values)
        return _result(
            'PASS' if error <= tolerance else 'FAIL', error=error
        )

    if observable.kind == 'complex_transfer':
        for key in ('real', 'imaginary'):
            ref_freqs, ref_values, failure = _band_values(observable, reference, key)
            if failure is not None:
                return _result('UNKNOWN', reason=f'reference {key} data missing')
            pred_freqs, pred_values, failure = _band_values(observable, prediction, key)
            if failure is not None or pred_freqs != ref_freqs:
                return _result(
                    'UNKNOWN',
                    reason=f'prediction {key} data missing or misaligned',
                )
            if key == 'real':
                ref_re, pred_re = ref_values, pred_values
            else:
                ref_im, pred_im = ref_values, pred_values
        error = _rms_error(
            [r * r + i * i for r, i in zip(ref_re, ref_im)],
            [p * p + i * i for p, i in zip(pred_re, pred_im)],
        )
        # complex error metric: rms of |z| differences is a documented
        # magnitude-domain comparison, not a phase claim
        error = _rms_error(
            [math.hypot(r, i) for r, i in zip(ref_re, ref_im)],
            [math.hypot(p, i) for p, i in zip(pred_re, pred_im)],
        )
        return _result(
            'PASS' if error <= tolerance else 'FAIL', error=error
        )

    if observable.kind == 'arrival_timing':
        ref_t = reference.get('arrival_s')
        pred_t = prediction.get('arrival_s')
        if ref_t is None or pred_t is None:
            return _result('UNKNOWN', reason='arrival time missing')
        error = abs(float(pred_t) - float(ref_t))
        return _result(
            'PASS' if error <= tolerance else 'FAIL', error=error
        )

    return _result(
        'NOT_APPLICABLE',
        reason=f'observable kind {observable.kind} has no evaluator yet',
    )


def _fold_status(statuses: Sequence[BenchmarkResultStatus]) -> BenchmarkResultStatus:
    if not statuses:
        return 'UNKNOWN'
    if 'FAIL' in statuses:
        return 'FAIL'
    evaluated = [s for s in statuses if s in ('PASS', 'FAIL')]
    if not evaluated:
        return 'UNKNOWN' if 'UNKNOWN' in statuses else 'NOT_APPLICABLE'
    if 'UNKNOWN' in statuses:
        return 'UNKNOWN'
    return 'PASS'


def run_benchmark(
    case: BenchmarkCase,
    provider: PredictionProvider,
    profile: EvaluationProfile,
    *,
    evidence_id: str = 'evidence-1',
    created_at_utc: str | None = None,
) -> BenchmarkValidationEvidence:
    """Run ``case x provider x profile`` into replayable evidence."""
    predictions = provider.predict(case)
    evaluations = tuple(
        evaluate_observable(observable, predictions.get(observable.observable_id), profile)
        for observable in case.observables
    )
    unsupported = sum(
        1 for e in evaluations if e.status == 'NOT_APPLICABLE'
    )
    return BenchmarkValidationEvidence(
        evidence_id=evidence_id,
        benchmark_id=case.benchmark_id,
        benchmark_version=case.version,
        benchmark_sha256=case.semantic_sha256,
        importer_id=case.importer_id,
        importer_version=case.importer_version,
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        provider_config_sha256=provider.config_sha256(),
        evaluation_profile_id=profile.profile_id,
        evaluation_profile_version=profile.version,
        evidence_class=case.source_asset.evidence_class,
        status=_fold_status([e.status for e in evaluations]),
        observable_evaluations=evaluations,
        unsupported_count=unsupported,
        created_at_utc=created_at_utc,
    )


# ----------------------------------------------------------------------
# Importers — pure transforms into BenchmarkCase; fail closed on ambiguity.


def _parse_triplet(
    raw: Any, *, what: str, field: str, point_id: str
) -> tuple[float, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError(f'{what} {point_id} has malformed {field}')
    vector = tuple(float(v) for v in raw)
    if not all(math.isfinite(v) for v in vector):
        raise ValueError(f'{what} {point_id} has non-finite {field}')
    return vector


def _parse_point(raw: Any, *, what: str) -> BenchmarkPoint:
    if not isinstance(raw, dict) or not raw.get('point_id'):
        raise ValueError(f'{what} requires point_id')
    point_id = str(raw['point_id'])
    return BenchmarkPoint(
        point_id=point_id,
        position_m=_parse_triplet(
            raw.get('position_m'),
            what=what,
            field='position_m',
            point_id=point_id,
        ),
        orientation_deg=_parse_triplet(
            raw.get('orientation_deg'),
            what=what,
            field='orientation_deg',
            point_id=point_id,
        ),
        role=raw.get('role'),
    )


def canonical_json_importer(
    payload: str | bytes | dict[str, Any],
    source_asset: BenchmarkSourceAsset,
    *,
    importer_version: str = '1',
) -> BenchmarkCase:
    """Parse the documented canonical JSON benchmark format into a case.

    The canonical format is what per-dataset adapters emit after their own
    normalization; it never guesses conventions:

    - ``format`` must equal ``htdt-benchmark-case-1``;
    - ``coordinate_convention`` is required when any geometry/position is
      supplied — ambiguous coordinates are rejected, not guessed;
    - at least one observable is required;
    - every observable requires ``kind``, ``metric_id``, ``metric_version``
      and a ``reference`` payload.
    """
    if isinstance(payload, (str, bytes)):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError(f'benchmark payload is not JSON: {error}')
    else:
        data = payload
    if not isinstance(data, dict):
        raise ValueError('benchmark payload must be a JSON object')
    if data.get('format') != CANONICAL_JSON_FORMAT:
        raise ValueError(
            f"unsupported benchmark format: {data.get('format')!r}"
        )
    if not data.get('benchmark_id') or not data.get('version'):
        raise ValueError('benchmark_id and version are required')

    coordinate_convention = data.get('coordinate_convention')
    has_positions = any(
        item.get('position_m') is not None
        for item in data.get('sources', []) + data.get('receivers', [])
    )
    if (data.get('geometry') is not None or has_positions) and (
        coordinate_convention is None
    ):
        raise ValueError(
            'geometry/position data without a coordinate convention is '
            'ambiguous — refusing to guess'
        )

    observables = []
    for index, raw in enumerate(data.get('observables', [])):
        if not isinstance(raw, dict):
            raise ValueError(f'observable #{index} is not an object')
        for required in ('observable_id', 'kind', 'metric_id', 'metric_version', 'reference'):
            if raw.get(required) is None:
                raise ValueError(
                    f'observable #{index} lacks required {required!r}'
                )
        observables.append(BenchmarkObservable(**raw))
    if not observables:
        raise ValueError('a benchmark case requires at least one observable')

    payload_for_hash = {
        'schema_version': BENCHMARK_SCHEMA_VERSION,
        'benchmark_id': data['benchmark_id'],
        'version': data['version'],
        'title': data.get('title'),
        'source_asset': source_asset.semantic_payload(),
        'coordinate_convention': coordinate_convention,
        'geometry': data.get('geometry'),
        'sources': [
            _parse_point(item, what='source').model_dump(mode='json')
            for item in data.get('sources', [])
        ],
        'receivers': [
            _parse_point(item, what='receiver').model_dump(mode='json')
            for item in data.get('receivers', [])
        ],
        'materials': data.get('materials'),
        'environment': data.get('environment'),
        'sample_rate_hz': (
            None
            if data.get('sample_rate_hz') is None
            else float(data['sample_rate_hz'])
        ),
        'frequency_grid_hz': (
            None
            if data.get('frequency_grid_hz') is None
            else [float(v) for v in data['frequency_grid_hz']]
        ),
        'time_origin_s': (
            None
            if data.get('time_origin_s') is None
            else float(data['time_origin_s'])
        ),
        'preprocessing': data.get('preprocessing'),
        'limitations': data.get('limitations'),
        'observables': [
            item.model_dump(mode='json') for item in observables
        ],
        'importer_id': CANONICAL_JSON_FORMAT,
        'importer_version': importer_version,
    }
    return BenchmarkCase(
        benchmark_id=data['benchmark_id'],
        version=data['version'],
        title=data.get('title'),
        source_asset=source_asset,
        coordinate_convention=coordinate_convention,
        geometry=data.get('geometry'),
        sources=tuple(
            _parse_point(item, what='source')
            for item in data.get('sources', [])
        ),
        receivers=tuple(
            _parse_point(item, what='receiver')
            for item in data.get('receivers', [])
        ),
        materials=data.get('materials'),
        environment=data.get('environment'),
        sample_rate_hz=data.get('sample_rate_hz'),
        frequency_grid_hz=(
            None
            if data.get('frequency_grid_hz') is None
            else tuple(float(v) for v in data['frequency_grid_hz'])
        ),
        time_origin_s=data.get('time_origin_s'),
        preprocessing=data.get('preprocessing'),
        limitations=data.get('limitations'),
        observables=tuple(observables),
        importer_id=CANONICAL_JSON_FORMAT,
        importer_version=importer_version,
        semantic_sha256=_hash(payload_for_hash),
    )


def analytic_benchmark_case(
    *,
    benchmark_id: str,
    version: str,
    source_asset: BenchmarkSourceAsset,
    observables: Sequence[BenchmarkObservable],
    coordinate_convention: str = 'analytic',
    title: str | None = None,
    sources: Sequence[BenchmarkPoint] = (),
    receivers: Sequence[BenchmarkPoint] = (),
    environment: dict[str, Any] | None = None,
    limitations: str | None = None,
    importer_version: str = '1',
) -> BenchmarkCase:
    """Build an analytic benchmark case (evidence class ``analytic``).

    Analytic references carry no geometry authority beyond the points and
    environment the caller supplies — the convention records that.
    """
    payload = {
        'format': CANONICAL_JSON_FORMAT,
        'benchmark_id': benchmark_id,
        'version': version,
        'title': title,
        'coordinate_convention': coordinate_convention,
        'sources': [p.model_dump(mode='json') for p in sources],
        'receivers': [p.model_dump(mode='json') for p in receivers],
        'environment': environment,
        'limitations': limitations,
        'observables': [
            o.model_dump(mode='json') for o in observables
        ],
    }
    return canonical_json_importer(
        payload, source_asset, importer_version=importer_version
    )


__all__ = [
    'BENCHMARK_SCHEMA_VERSION',
    'CANONICAL_JSON_FORMAT',
    'BenchmarkCase',
    'BenchmarkEvidenceClass',
    'BenchmarkObservable',
    'BenchmarkPoint',
    'BenchmarkResultStatus',
    'BenchmarkSourceAsset',
    'BenchmarkValidationEvidence',
    'EvaluationProfile',
    'ObservableEvaluation',
    'ObservableKind',
    'PredictionProvider',
    'StaticReplayProvider',
    'analytic_benchmark_case',
    'canonical_json_importer',
    'evaluate_observable',
    'run_benchmark',
]
