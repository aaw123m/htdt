"""Real-Project Acoustic Validation Corpus (#773).

One owned-room campaign is evidence for one room; mature acoustic products
accumulate reproducible evidence across many geometries, sources, placements
and measurement conditions. ``ValidationCorpusEntry`` is the immutable
manifest that turns selected owned-room evidence into a stable,
reproducible corpus — cumulative engineering knowledge instead of isolated
project history.

Contracts this module enforces:

- **Exact identity only.** An entry pins the preserved artifacts by exact
  ids and semantic hashes (SceneRevision + content hash, SystemVariant +
  sha256, measurement evidence digests, acquisition context). It never
  resolves "latest" evidence at read time.
- **Calibration vs holdout is preserved.** Every entry carries the original
  preregistered split role; ``ineligible`` evidence stays inspectable but
  can never silently enter pass metrics.
- **Observable-scoped ground truth.** ``observables`` declares what the
  measurement actually observed (e.g. magnitude FR without common timing
  cannot claim ``complex_transfer`` ground truth) — downstream evaluation
  must not infer unmeasured quantities.
- **Uncertainty stays UNKNOWN when absent.** ``CorpusUncertaintyRecord``
  entries adopt GUM/VIM vocabulary where real evidence supplies it; no
  generic ± value is ever fabricated for legacy data.
- **Privacy is explicit.** ``license_classification`` plus the recorded
  ``anonymization_version`` mark whether an entry is internal-only or
  exported through the anonymization pipeline — private project bundles
  are never silently made shareable.

``ValidationBenchmarkSpec`` (#773 §8) is the exact predeclared evaluation
binding for a corpus run: provider/model, numerical settings, observable,
masks, source/receiver subsets, metric, tolerance and aggregation
semantics — never one aggregate "solver accuracy %".
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload


CORPUS_SCHEMA_VERSION = 1
CORPUS_AUTHORITY_VERSION = 'validation-corpus-1'
BENCHMARK_SCHEMA_VERSION = 1
BENCHMARK_AUTHORITY_VERSION = 'validation-benchmark-spec-1'


CorpusSplitRole = Literal[
    'calibration',
    'holdout',
    'repeatability',
    'sensitivity',
    'stress',
    'ineligible',
]
CORPUS_SPLIT_ROLES: frozenset[str] = frozenset(
    {
        'calibration',
        'holdout',
        'repeatability',
        'sensitivity',
        'stress',
        'ineligible',
    }
)

GeometryDimension = Literal[
    'rectangular',
    'concave',
    'sloped_ceiling',
    'openings',
    'coupled_region',
    'furniture_obstacles',
    'treatment_present',
    'multi_row_seating',
    'other',
]
GEOMETRY_DIMENSIONS: frozenset[str] = frozenset(
    {
        'rectangular',
        'concave',
        'sloped_ceiling',
        'openings',
        'coupled_region',
        'furniture_obstacles',
        'treatment_present',
        'multi_row_seating',
        'other',
    }
)

CorpusObservable = Literal[
    'magnitude_fr',
    'relative_phase',
    'complex_transfer',
    'arrival_timing',
    'impulse_response',
    'decay',
    'seat_trend',
    'candidate_ranking',
    'spl_level',
]
CORPUS_OBSERVABLES: frozenset[str] = frozenset(
    {
        'magnitude_fr',
        'relative_phase',
        'complex_transfer',
        'arrival_timing',
        'impulse_response',
        'decay',
        'seat_trend',
        'candidate_ranking',
        'spl_level',
    }
)

UncertaintySourceKind = Literal[
    'repeatability',
    'instrument_calibration',
    'receiver_pose',
    'source_pose',
    'environment',
    'timing_reference',
    'processing_model',
    'other',
]

CorpusLicenseClassification = Literal[
    'internal',
    'shareable_anonymized',
    'public',
    'restricted',
]

SourceTopologyKind = Literal[
    'single_source',
    'stereo_pair',
    'multichannel',
    'multi_sub',
    'unknown',
]

BassManagementKind = Literal['none', 'shared', 'independent', 'unknown']

CorpusDspState = Literal['raw', 'calibrated', 'unknown']






class CorpusUncertaintyRecord(BaseModel):
    """One recorded uncertainty contribution in GUM/VIM vocabulary (#773).

    ``standard_uncertainty`` is ``None`` when the source evidence does not
    justify a quantitative value — UNKNOWN is never rewritten to zero.
    ``coverage_factor``/``confidence_semantics`` record how an interval was
    reported when a certificate or dataset supplies it.
    """

    model_config = ConfigDict(frozen=True)

    source: UncertaintySourceKind
    distribution: str | None = None
    standard_uncertainty: float | None = Field(default=None, ge=0.0)
    unit: str | None = None
    coverage_factor: float | None = Field(default=None, gt=0.0)
    confidence_semantics: str | None = None
    repeat_count: int | None = Field(default=None, ge=1)
    notes: str | None = None


class CorpusSourceTopology(BaseModel):
    """Source/system diversity classification for one corpus entry (#773 §5).

    Classification, not scoring: a solver validated on
    ``single_source``/``raw`` cases is not thereby validated for
    directional mains or shared-subwoofer topologies.
    """

    model_config = ConfigDict(frozen=True)

    topology: SourceTopologyKind = 'unknown'
    source_count: int = Field(default=1, ge=1)
    subwoofer_count: int = Field(default=0, ge=0)
    simultaneous_active_sources: int = Field(default=1, ge=1)
    bass_management: BassManagementKind = 'unknown'
    dsp_state: CorpusDspState = 'unknown'
    placement_class: str | None = None
    directivity_capability: str | None = None


class CorpusTransformation(BaseModel):
    """One recorded derivation applied to raw evidence (#773 §7).

    Normalized fixtures stored alongside an entry must stay traceable to
    the preserved originals: every resample/window/normalize/truncate/
    calibration step is named here in application order.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        'resample',
        'window',
        'normalize',
        'truncate',
        'calibration_correction',
        'mask',
        'other',
    ]
    description: str = Field(min_length=1)
    parameters_json: str | None = None


class ValidationCorpusEntry(BaseModel):
    """Immutable manifest binding one piece of owned-room evidence (#773 §1).

    A corpus entry is never "one REW file": it pins the exact preserved
    artifacts it was produced from, the split role it plays, the observable
    scope it can be ground truth for, and its license/privacy class.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[CORPUS_SCHEMA_VERSION] = CORPUS_SCHEMA_VERSION
    authority_version: Literal[
        'validation-corpus-1'
    ] = CORPUS_AUTHORITY_VERSION
    corpus_entry_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    #: Identifies which anonymization/export pipeline version produced this
    #: entry's project-facing fields; ``None`` = purely internal identity.
    anonymization_version: str | None = None

    geometry_classes: tuple[GeometryDimension, ...] = ()
    source_topology: CorpusSourceTopology | None = None

    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: Exported canonical geometry identity when the corpus references an
    #: anonymized geometry export rather than the project revision.
    canonical_geometry_identity: str | None = None
    system_variant_id: str = Field(min_length=1)
    system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_model_identities: tuple[str, ...] = ()
    receiver_identity: str = Field(min_length=1)
    acquisition_context_id: str | None = None
    acquisition_context_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    measurement_plan_id: str | None = None
    measurement_campaign_id: str | None = None
    #: Exact digests of the preserved measurement evidence this entry wraps.
    evidence_sha256s: tuple[str, ...] = Field(min_length=1)

    split_role: CorpusSplitRole
    provider_id: str = Field(min_length=1)
    provider_model_version: str = Field(min_length=1)

    observables: tuple[CorpusObservable, ...] = Field(min_length=1)
    frequency_mask_hz: tuple[float, float] | None = None
    time_mask_s: tuple[float, float] | None = None
    metric_definition_ids: tuple[str, ...] = ()

    license_classification: CorpusLicenseClassification = 'internal'
    uncertainty_records: tuple[CorpusUncertaintyRecord, ...] = ()
    transformations: tuple[CorpusTransformation, ...] = ()
    notes: str | None = None
    created_at_utc: str = Field(min_length=1)
    corpus_entry_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_entry(self) -> 'ValidationCorpusEntry':
        masks = (
            ('frequency_mask_hz', self.frequency_mask_hz),
            ('time_mask_s', self.time_mask_s),
        )
        for name, mask in masks:
            if mask is not None and len(mask) != 2:
                raise ValueError(f'{name} must be a (low, high) pair')
            if mask is not None and not mask[0] < mask[1]:
                raise ValueError(f'{name} must be strictly increasing')
        digests = [item for item in self.evidence_sha256s]
        if any(
            len(item) != 64 or any(c not in '0123456789abcdef' for c in item)
            for item in digests
        ):
            raise ValueError('evidence_sha256s must be sha256 digests')
        if len(digests) != len(set(digests)):
            raise ValueError('evidence_sha256s must be unique')
        if len(self.observables) != len(set(self.observables)):
            raise ValueError('observables must be unique')
        if len(self.geometry_classes) != len(set(self.geometry_classes)):
            raise ValueError('geometry_classes must be unique')
        context_pair = (
            self.acquisition_context_id is not None,
            self.acquisition_context_sha256 is not None,
        )
        if context_pair[0] != context_pair[1]:
            raise ValueError('acquisition context id/hash supplied together')
        if (
            self.split_role == 'ineligible'
            and self.license_classification == 'public'
        ):
            raise ValueError(
                'ineligible evidence cannot be exported as public corpus truth'
            )
        if self.corpus_entry_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ValidationCorpusEntry hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'corpus_entry_id': self.corpus_entry_id,
            'document_id': self.document_id,
            'anonymization_version': self.anonymization_version,
            'geometry_classes': list(self.geometry_classes),
            'source_topology': (
                self.source_topology.model_dump(mode='json')
                if self.source_topology is not None
                else None
            ),
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'canonical_geometry_identity': self.canonical_geometry_identity,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'source_model_identities': list(self.source_model_identities),
            'receiver_identity': self.receiver_identity,
            'acquisition_context_id': self.acquisition_context_id,
            'acquisition_context_sha256': self.acquisition_context_sha256,
            'measurement_plan_id': self.measurement_plan_id,
            'measurement_campaign_id': self.measurement_campaign_id,
            'evidence_sha256s': list(self.evidence_sha256s),
            'split_role': self.split_role,
            'provider_id': self.provider_id,
            'provider_model_version': self.provider_model_version,
            'observables': list(self.observables),
            'frequency_mask_hz': (
                list(self.frequency_mask_hz)
                if self.frequency_mask_hz is not None
                else None
            ),
            'time_mask_s': (
                list(self.time_mask_s) if self.time_mask_s is not None else None
            ),
            'metric_definition_ids': list(self.metric_definition_ids),
            'license_classification': self.license_classification,
            'uncertainty_records': [
                item.model_dump(mode='json')
                for item in self.uncertainty_records
            ],
            'transformations': [
                item.model_dump(mode='json') for item in self.transformations
            ],
            'notes': self.notes,
            'created_at_utc': self.created_at_utc,
        }


BenchmarkObservable = CorpusObservable

BenchmarkMetric = Literal[
    'log_magnitude_residual',
    'complex_transfer_residual',
    'modal_peak_frequency_error',
    'arrival_time_error',
    'candidate_rank_agreement',
    'seat_variation_trend',
    'holdout_delta',
    'other',
]

BenchmarkAggregation = Literal[
    'per_case',
    'median',
    'distribution',
    'regression_delta',
]


class ValidationBenchmarkSpec(BaseModel):
    """Exact predeclared evaluation binding for one corpus run (#773 §8).

    Every corpus evaluation must resolve against a spec persisted before
    the run — metric, tolerance and aggregation are declared up front so
    results are never retrofitted into a favourable summary.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[
        BENCHMARK_SCHEMA_VERSION
    ] = BENCHMARK_SCHEMA_VERSION
    authority_version: Literal[
        'validation-benchmark-spec-1'
    ] = BENCHMARK_AUTHORITY_VERSION
    benchmark_spec_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_model_version: str = Field(min_length=1)
    numerical_settings_json: str = Field(min_length=2)
    observable: BenchmarkObservable
    frequency_mask_hz: tuple[float, float] | None = None
    time_mask_s: tuple[float, float] | None = None
    source_subset: tuple[str, ...] = ()
    receiver_subset: tuple[str, ...] = ()
    metric: BenchmarkMetric
    metric_version: str = Field(min_length=1)
    tolerance_json: str = Field(min_length=2)
    aggregation: BenchmarkAggregation = 'per_case'
    created_at_utc: str = Field(min_length=1)
    benchmark_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_spec(self) -> 'ValidationBenchmarkSpec':
        masks = (
            ('frequency_mask_hz', self.frequency_mask_hz),
            ('time_mask_s', self.time_mask_s),
        )
        for name, mask in masks:
            if mask is not None and len(mask) != 2:
                raise ValueError(f'{name} must be a (low, high) pair')
            if mask is not None and not mask[0] < mask[1]:
                raise ValueError(f'{name} must be strictly increasing')
        try:
            json.loads(self.numerical_settings_json)
            json.loads(self.tolerance_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f'malformed benchmark JSON payload: {exc}')
        if len(self.source_subset) != len(set(self.source_subset)):
            raise ValueError('source_subset must be unique')
        if len(self.receiver_subset) != len(set(self.receiver_subset)):
            raise ValueError('receiver_subset must be unique')
        if self.benchmark_spec_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ValidationBenchmarkSpec hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'benchmark_spec_id': self.benchmark_spec_id,
            'provider_id': self.provider_id,
            'provider_model_version': self.provider_model_version,
            'numerical_settings_json': json.loads(
                self.numerical_settings_json
            ),
            'observable': self.observable,
            'frequency_mask_hz': (
                list(self.frequency_mask_hz)
                if self.frequency_mask_hz is not None
                else None
            ),
            'time_mask_s': (
                list(self.time_mask_s) if self.time_mask_s is not None else None
            ),
            'source_subset': list(self.source_subset),
            'receiver_subset': list(self.receiver_subset),
            'metric': self.metric,
            'metric_version': self.metric_version,
            'tolerance_json': json.loads(self.tolerance_json),
            'aggregation': self.aggregation,
            'created_at_utc': self.created_at_utc,
        }


def build_corpus_entry(
    *,
    document_id: str,
    split_role: CorpusSplitRole,
    scene_revision_id: str,
    scene_content_hash: str,
    system_variant_id: str,
    system_variant_sha256: str,
    receiver_identity: str,
    evidence_sha256s: tuple[str, ...],
    provider_id: str,
    provider_model_version: str,
    observables: tuple[CorpusObservable, ...],
    created_at_utc: str,
    corpus_entry_id: str | None = None,
    anonymization_version: str | None = None,
    geometry_classes: tuple[GeometryDimension, ...] = (),
    source_topology: CorpusSourceTopology | None = None,
    canonical_geometry_identity: str | None = None,
    source_model_identities: tuple[str, ...] = (),
    acquisition_context_id: str | None = None,
    acquisition_context_sha256: str | None = None,
    measurement_plan_id: str | None = None,
    measurement_campaign_id: str | None = None,
    frequency_mask_hz: tuple[float, float] | None = None,
    time_mask_s: tuple[float, float] | None = None,
    metric_definition_ids: tuple[str, ...] = (),
    license_classification: CorpusLicenseClassification = 'internal',
    uncertainty_records: tuple[CorpusUncertaintyRecord, ...] = (),
    transformations: tuple[CorpusTransformation, ...] = (),
    notes: str | None = None,
) -> ValidationCorpusEntry:
    """Assemble an immutable corpus-entry manifest (#773 §1).

    ``corpus_entry_id`` defaults to a fresh UUID; the semantic sha is the
    canonical identity — it binds every field including the id, so an
    identical rebuild produces the identical digest.
    """

    entry_id = corpus_entry_id or str(uuid4())
    payload: dict[str, Any] = {
        'corpus_entry_id': entry_id,
        'document_id': document_id,
        'anonymization_version': anonymization_version,
        'geometry_classes': geometry_classes,
        'source_topology': source_topology,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'canonical_geometry_identity': canonical_geometry_identity,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'source_model_identities': source_model_identities,
        'receiver_identity': receiver_identity,
        'acquisition_context_id': acquisition_context_id,
        'acquisition_context_sha256': acquisition_context_sha256,
        'measurement_plan_id': measurement_plan_id,
        'measurement_campaign_id': measurement_campaign_id,
        'evidence_sha256s': evidence_sha256s,
        'split_role': split_role,
        'provider_id': provider_id,
        'provider_model_version': provider_model_version,
        'observables': observables,
        'frequency_mask_hz': frequency_mask_hz,
        'time_mask_s': time_mask_s,
        'metric_definition_ids': metric_definition_ids,
        'license_classification': license_classification,
        'uncertainty_records': uncertainty_records,
        'transformations': transformations,
        'notes': notes,
        'created_at_utc': created_at_utc,
    }
    provisional = ValidationCorpusEntry.model_construct(**canonicalize_payload(ValidationCorpusEntry, dict(
        **payload,
        corpus_entry_sha256='0' * 64,
    )))
    return ValidationCorpusEntry(
        **payload,
        corpus_entry_sha256=_hash(provisional.semantic_payload()),
    )


def build_benchmark_spec(
    *,
    provider_id: str,
    provider_model_version: str,
    numerical_settings_json: str,
    observable: BenchmarkObservable,
    metric: BenchmarkMetric,
    metric_version: str,
    tolerance_json: str,
    created_at_utc: str,
    benchmark_spec_id: str | None = None,
    frequency_mask_hz: tuple[float, float] | None = None,
    time_mask_s: tuple[float, float] | None = None,
    source_subset: tuple[str, ...] = (),
    receiver_subset: tuple[str, ...] = (),
    aggregation: BenchmarkAggregation = 'per_case',
) -> ValidationBenchmarkSpec:
    spec_id = benchmark_spec_id or str(uuid4())
    payload: dict[str, Any] = {
        'benchmark_spec_id': spec_id,
        'provider_id': provider_id,
        'provider_model_version': provider_model_version,
        'numerical_settings_json': numerical_settings_json,
        'observable': observable,
        'frequency_mask_hz': frequency_mask_hz,
        'time_mask_s': time_mask_s,
        'source_subset': source_subset,
        'receiver_subset': receiver_subset,
        'metric': metric,
        'metric_version': metric_version,
        'tolerance_json': tolerance_json,
        'aggregation': aggregation,
        'created_at_utc': created_at_utc,
    }
    provisional = ValidationBenchmarkSpec.model_construct(**canonicalize_payload(ValidationBenchmarkSpec, dict(
        **payload,
        benchmark_spec_sha256='0' * 64,
    )))
    return ValidationBenchmarkSpec(
        **payload,
        benchmark_spec_sha256=_hash(provisional.semantic_payload()),
    )


def split_role_is_scorable(role: CorpusSplitRole) -> bool:
    """Whether a split role may enter pass metrics (#773 §2).

    ``ineligible`` evidence remains inspectable for diagnostics but never
    counts as pass/fail ground truth.
    """

    return role != 'ineligible'


__all__ = [
    'BENCHMARK_AUTHORITY_VERSION',
    'BENCHMARK_SCHEMA_VERSION',
    'CORPUS_AUTHORITY_VERSION',
    'CORPUS_OBSERVABLES',
    'CORPUS_SCHEMA_VERSION',
    'CORPUS_SPLIT_ROLES',
    'CorpusLicenseClassification',
    'CorpusObservable',
    'CorpusSourceTopology',
    'CorpusSplitRole',
    'CorpusTransformation',
    'CorpusUncertaintyRecord',
    'GEOMETRY_DIMENSIONS',
    'GeometryDimension',
    'UncertaintySourceKind',
    'ValidationBenchmarkSpec',
    'ValidationCorpusEntry',
    'build_benchmark_spec',
    'build_corpus_entry',
    'split_role_is_scorable',
]
