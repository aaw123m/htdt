"""Validation sample-dependence / benchmark-leakage authority
(#698, REV58-IDENT).

``N = frequency bins × seats × repeats`` is not an independent sample
count, and a holdout repeatedly inspected during development is no longer
an untouched test. This module is the statistical-inference /
benchmark-governance layer above #581's sampling geometry and #566's
physical benchmark definitions.

- :class:`CadValidationStatisticalDesign` — the sealed statistical
  design: the generalization claim, the independent sampling unit
  (§1), the explicit room→source→receiver→repeat→bin hierarchy (§3),
  raw vs independent/effective counts (§2) and the error-distribution
  scope (§7).
- :class:`CadDependenceModel` — the declared dependence structure
  (§4/§5): spatial-correlation class, frequency-bin coupling causes,
  common-calibration/processing dependencies and the resampling unit a
  bootstrap must use (§16). Different XYZ is never statistically
  independent by geometry alone (Kuster 2008).
- :class:`CadDatasetRoleAssignment` — a corpus's declared epistemic role
  (§8): calibration fit / model selection / threshold tuning /
  development validation / locked challenge / repeatability only. A
  dataset repeatedly consulted during solver development is
  ``development_validation`` even if no optimizer directly fitted it.
- :class:`CadBenchmarkExposureRecord` — the append-only holdout
  exposure ledger (§9): what was revealed, when, to which solver
  version, and which development decision followed. A heavily inspected
  benchmark never pretends to be untouched.
- :class:`CadChallengeQualification` +
  :func:`evaluate_validation_claim` — the sealed fail-closed verdict:
  pseudoreplication detection (frequency bins or repeats counted as
  independent units), split-unit vs claim mismatch (§11),
  winner's-curse separation of model-selection vs final-generalization
  scores (§12), locked-challenge eligibility and the versioned
  exposure lifecycle (§18).

Composition:

- #581 ``cad_spatial_campaign`` — owns the measurement geometry; this
  layer owns the inference semantics above it.
- #566 benchmark / #773 ``cad_validation_corpus`` — the corpora whose
  sample structure and epistemic role these records govern.
- #572/#604 — metrological and input/model uncertainty stay separate
  from sampling/dependence uncertainty (§15).
- #689 — parameter identifiability; a model-selection corpus can be
  honest validation yet the fitted parameters remain non-identified.
- #693 — operator/reproducibility governance.

Literature basis
----------------
- Dwork et al., *The reusable holdout: Preserving validity in adaptive
  data analysis*, Science 349(6248) (2015) — adaptive inspection of a
  holdout overfits the holdout itself; exposure must be ledgered and
  strong claims bounded accordingly.
- Hurlbert, *Pseudoreplication and the Design of Ecological Field
  Experiments*, Ecological Monographs 54(2) (1984) — many observations
  are not many independent experimental units; counting correlated
  observations as independent inflates sample size and overstates
  confidence.
- Kuster, *Spatial correlation and coherence in reverberant acoustic
  fields*, JASA 123(1) (2008) — spatial correlation in reverberant
  fields is frequency/field/directivity dependent; receiver spacing
  alone does not create independence.
- Johnson & Long, *The Probability Density of Spectral Estimates Based
  on Modified Periodogram Averages*, IEEE Trans. Signal Process. 47(5)
  (1999) — adjacent/smoothed spectral bins are statistically dependent;
  frequency-grid points are not replication units.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


VSD_AUTHORITY_SCHEMA_VERSION = 'vsd-validation-stats-1'
VSD_EVALUATION_VERSION = 'vsd-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


# ---------------------------------------------------------------------------
# Taxonomies (#698)
# ---------------------------------------------------------------------------

SamplingUnitKind = Literal[
    'room',
    'room_config',
    'source_position',
    'receiver_position',
    'source_receiver_pair',
    'measurement_session',
    'physical_device_instance',
    'installation_project',
    'benchmark_scene',
    'other_explicit',
]
"""#698 §1 — the unit a claim generalizes over. Deliberately no
``frequency_bin``/``repeat`` entry: spectral points and repeated captures
are nested *inside* replication units, never the unit itself."""

HierarchyLevelKind = Literal[
    'installation_project',
    'room',
    'room_config',
    'benchmark_scene',
    'measurement_session',
    'source_position',
    'receiver_position',
    'source_receiver_pair',
    'repeat_capture',
    'frequency_bin',
    'time_bin',
    'other_explicit',
]
"""Ordered outermost→innermost nesting levels (#698 §3)."""

GeneralizationClaim = Literal[
    'unseen_installations',
    'unseen_rooms',
    'unseen_room_configs',
    'across_benchmark_scenes',
    'unseen_sessions',
    'unseen_sources_within_room',
    'unseen_receivers_within_room',
    'within_pair_repeatability',
    'other_declared',
]
"""#698 §11 — the split unit must match the claim: training on seven
seats and calling the eighth an unseen-room example is a mismatch."""

DatasetRole = Literal[
    'calibration_parameter_fit',
    'model_selection_development',
    'threshold_policy_tuning',
    'development_validation',
    'locked_challenge_test',
    'repeatability_only',
]
"""#698 §8 — epistemic roles stay distinct; ``development_validation`` is
the honest label for a dataset repeatedly consulted during development
even when no optimizer fitted it."""

DependenceClass = Literal[
    'spatial_correlation',
    'frequency_bin_coupling',
    'repeat_capture',
    'common_calibration',
    'common_processing',
    'common_model_inputs',
    'other_declared',
]

SpatialCorrelationModel = Literal[
    'kuster_reverberant_family',
    'measured_covariance',
    'distance_declared_model',
    'none_declared',
    'unknown',
]

FrequencyCouplingCause = Literal[
    'finite_ir_window',
    'fft_spectral_leakage',
    'smoothing_banding',
    'interpolation_resampling',
    'modal_bandwidth',
    'common_calibration_curve',
    'common_deconvolution',
    'other_declared',
]

ExposureDecision = Literal[
    'solver_change',
    'threshold_change',
    'metric_selection',
    'model_selection',
    'debugging',
    'published_result',
    'declared_other',
]

CorpusStatus = Literal['untouched', 'exposed', 'superseded_for_version']

ValidationClaimState = Literal[
    'locked_challenge_eligible',
    'qualified_generalization',
    'qualified_with_limitations',
    'development_validation_only',
    'winner_selection_biased',
    'pseudoreplication_detected',
    'split_mismatch',
    'independence_unestablished',
    'insufficient_evidence',
]

#: Rank of each sampling unit — the split granularity must reach the
#: claim's minimum rank (#698 §11). ``other_explicit`` never auto-maps.
_UNIT_RANK: dict[str, int] = {
    'source_receiver_pair': 1,
    'receiver_position': 1,
    'source_position': 2,
    'measurement_session': 2,
    'physical_device_instance': 2,
    'room': 3,
    'room_config': 3,
    'benchmark_scene': 3,
    'installation_project': 4,
    'other_explicit': 0,
}

_CLAIM_MIN_RANK: dict[str, int] = {
    'within_pair_repeatability': 1,
    'unseen_receivers_within_room': 1,
    'unseen_sources_within_room': 2,
    'unseen_sessions': 2,
    'unseen_rooms': 3,
    'unseen_room_configs': 3,
    'across_benchmark_scenes': 3,
    'unseen_installations': 4,
    'other_declared': 0,
}

#: Hierarchy levels that are never independent units — counting them as
#: replication is pseudoreplication by construction.
_NEVER_INDEPENDENT_LEVELS: frozenset[str] = frozenset({
    'repeat_capture', 'frequency_bin', 'time_bin',
})


# ---------------------------------------------------------------------------
# Embedded blocks
# ---------------------------------------------------------------------------


class CadHierarchyLevel(BaseModel):
    """One nesting level of the statistical design (#698 §3)."""

    model_config = ConfigDict(frozen=True)

    level_kind: HierarchyLevelKind
    count: int = Field(ge=1)
    label: str | None = None


class CadCorpusExposureState(BaseModel):
    """A corpus's epistemic status inside a claim verdict (#698 §18)."""

    model_config = ConfigDict(frozen=True)

    corpus_ref: AuthorityRef
    role: DatasetRole | Literal['unassigned'] = 'unassigned'
    exposure_count: int = Field(ge=0)
    status: CorpusStatus = 'untouched'

    @model_validator(mode='after')
    def valid_state(self) -> 'CadCorpusExposureState':
        if self.corpus_ref.ref_sha256 is None:
            raise ValueError('corpus_ref must carry its sha256 pin')
        if self.status == 'exposed' and self.exposure_count == 0:
            raise ValueError(
                'an exposed corpus must record at least one exposure'
            )
        return self


# ---------------------------------------------------------------------------
# Sealed records
# ---------------------------------------------------------------------------


class CadValidationStatisticalDesign(BaseModel):
    """The sealed statistical design behind a validation claim
    (#698 §1–§7).

    The generalization claim, the independent sampling unit, the
    explicit observation hierarchy and the separated raw / independent /
    effective counts. ``independent_unit_count=None`` is honest
    "undeclared" — the evaluator then caps every claim.
    """

    model_config = ConfigDict(frozen=True)

    design_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    design_label: str = Field(min_length=1)
    generalization_claim: GeneralizationClaim
    claim_detail: str | None = None
    independent_unit: SamplingUnitKind
    hierarchy: tuple[CadHierarchyLevel, ...] = ()
    raw_observation_count: int = Field(ge=0)
    independent_unit_count: int | None = Field(default=None, ge=0)
    cluster_count: int | None = Field(default=None, ge=0)
    repeat_count: int = Field(default=0, ge=0)
    frequency_bin_count: int = Field(default=0, ge=0)
    effective_sample_size: float | None = None
    effective_sample_size_method: str | None = None
    error_distribution_scope: str | None = None
    corpus_refs: tuple[AuthorityRef, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    provenance_json: str = '{}'
    design_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_design(self) -> 'CadValidationStatisticalDesign':
        _require_iso8601(self.declared_at_utc, 'design declared_at_utc')
        if (
            self.independent_unit_count is not None
            and self.independent_unit_count
            > self.raw_observation_count
        ):
            raise ValueError(
                'independent_unit_count cannot exceed the raw '
                'observation count'
            )
        if self.effective_sample_size is not None:
            _require_finite(
                self.effective_sample_size, 'effective_sample_size'
            )
            if self.effective_sample_size <= 0:
                raise ValueError(
                    'effective sample size must be positive'
                )
            if not self.effective_sample_size_method:
                raise ValueError(
                    'an effective-sample-size estimate requires its '
                    'estimation method — a bare number is not justified '
                    '(#698 §2)'
                )
        for i, ref in enumerate(self.corpus_refs):
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'corpus_refs[{i}] must carry its sha256 pin'
                )
        kinds = [level.level_kind for level in self.hierarchy]
        if len(kinds) != len(set(kinds)):
            raise ValueError(
                'hierarchy level kinds must not repeat — name the '
                'exact nesting once'
            )
        expected = _hash(self.identity_payload())
        if self.design_sha256 != expected:
            raise ValueError('statistical design hash mismatch')
        if self.design_id != _semantic_id('vsdes', expected):
            raise ValueError(
                'statistical design id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'design_label': self.design_label,
            'generalization_claim': self.generalization_claim,
            'claim_detail': self.claim_detail,
            'independent_unit': self.independent_unit,
            'hierarchy': [h.model_dump(mode='json') for h in self.hierarchy],
            'raw_observation_count': self.raw_observation_count,
            'independent_unit_count': self.independent_unit_count,
            'cluster_count': self.cluster_count,
            'repeat_count': self.repeat_count,
            'frequency_bin_count': self.frequency_bin_count,
            'effective_sample_size': self.effective_sample_size,
            'effective_sample_size_method': self.effective_sample_size_method,
            'error_distribution_scope': self.error_distribution_scope,
            'corpus_refs': [
                r.model_dump(mode='json') for r in self.corpus_refs
            ],
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
            'provenance_json': self.provenance_json,
        }

    def independent_cap(self) -> int | None:
        """The largest defensible independent count under the declared
        hierarchy — the product of counts at or above the declared
        independent unit's level. Levels inner to the unit (repeats,
        bins) never add replication."""
        if not self.hierarchy:
            return None
        order = [level.level_kind for level in self.hierarchy]
        unit_kind = self.independent_unit
        if unit_kind == 'other_explicit':
            return None
        if unit_kind not in order:
            # The declared unit is not a hierarchy level — cap at the
            # product of all non-nested-innermost levels is unknowable;
            # callers get None and the evaluator stays honest.
            return None
        cap = 1
        for level in self.hierarchy:
            if level.level_kind in _NEVER_INDEPENDENT_LEVELS:
                continue
            cap *= level.count
            if level.level_kind == unit_kind:
                break
        return cap


def statistical_design_binding(
    design: CadValidationStatisticalDesign,
) -> AuthorityRef:
    return AuthorityRef(
        kind='validation_statistical_design',
        ref_id=design.design_id,
        ref_sha256=design.design_sha256,
    )


class CadDependenceModel(BaseModel):
    """The declared dependence structure of a validation corpus
    (#698 §4–§6/§16).

    Dependence classes, the spatial-correlation model *class* (a declared
    model is never universal physics), frequency-bin coupling causes and
    the resampling unit a bootstrap must draw. ``none_declared`` /
    ``unknown`` stay honest — they bound the claims, never vanish.
    """

    model_config = ConfigDict(frozen=True)

    dependence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    design_ref: AuthorityRef | None = None
    dependence_classes: tuple[DependenceClass, ...] = Field(min_length=1)
    spatial_correlation_model: SpatialCorrelationModel = 'unknown'
    spatial_model_detail: str | None = None
    frequency_coupling_causes: tuple[FrequencyCouplingCause, ...] = ()
    resampling_unit: SamplingUnitKind | None = None
    resampling_method: str | None = None
    notes: tuple[str, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    dependence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_dependence(self) -> 'CadDependenceModel':
        _require_iso8601(
            self.declared_at_utc, 'dependence declared_at_utc'
        )
        if self.design_ref is not None and (
            self.design_ref.ref_sha256 is None
        ):
            raise ValueError('design_ref must carry its sha256 pin')
        if self.resampling_method and self.resampling_unit is None:
            raise ValueError(
                'a resampling method requires its resampling unit — '
                'resample rooms, not bins, for new-room claims '
                '(#698 §16)'
            )
        if (
            self.spatial_correlation_model
            in ('kuster_reverberant_family', 'distance_declared_model',
                'measured_covariance')
            and not self.spatial_model_detail
        ):
            raise ValueError(
                'a declared spatial-correlation model must carry its '
                'parameterization/source detail'
            )
        expected = _hash(self.identity_payload())
        if self.dependence_sha256 != expected:
            raise ValueError('dependence model hash mismatch')
        if self.dependence_id != _semantic_id('vsdep', expected):
            raise ValueError(
                'dependence model id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'design_ref': (
                self.design_ref.model_dump(mode='json')
                if self.design_ref is not None
                else None
            ),
            'dependence_classes': list(self.dependence_classes),
            'spatial_correlation_model': self.spatial_correlation_model,
            'spatial_model_detail': self.spatial_model_detail,
            'frequency_coupling_causes': list(
                self.frequency_coupling_causes
            ),
            'resampling_unit': self.resampling_unit,
            'resampling_method': self.resampling_method,
            'notes': list(self.notes),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }


def dependence_model_binding(
    model: CadDependenceModel,
) -> AuthorityRef:
    return AuthorityRef(
        kind='validation_dependence_model',
        ref_id=model.dependence_id,
        ref_sha256=model.dependence_sha256,
    )


class CadDatasetRoleAssignment(BaseModel):
    """A corpus's declared epistemic role for one context (#698 §8)."""

    model_config = ConfigDict(frozen=True)

    assignment_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    corpus_ref: AuthorityRef
    role: DatasetRole
    context_label: str = Field(min_length=1)
    solver_version: str | None = None
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)
    assignment_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_assignment(self) -> 'CadDatasetRoleAssignment':
        _require_iso8601(
            self.declared_at_utc, 'assignment declared_at_utc'
        )
        if self.corpus_ref.ref_sha256 is None:
            raise ValueError('corpus_ref must carry its sha256 pin')
        expected = _hash(self.identity_payload())
        if self.assignment_sha256 != expected:
            raise ValueError('dataset role assignment hash mismatch')
        if self.assignment_id != _semantic_id('vsrole', expected):
            raise ValueError(
                'dataset role assignment id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'corpus_ref': self.corpus_ref.model_dump(mode='json'),
            'role': self.role,
            'context_label': self.context_label,
            'solver_version': self.solver_version,
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }


def dataset_role_binding(
    assignment: CadDatasetRoleAssignment,
) -> AuthorityRef:
    return AuthorityRef(
        kind='validation_dataset_role',
        ref_id=assignment.assignment_id,
        ref_sha256=assignment.assignment_sha256,
    )


class CadBenchmarkExposureRecord(BaseModel):
    """One append-only holdout-exposure ledger entry (#698 §9).

    What was revealed from a nominal holdout/benchmark, when, to which
    solver/model version, and which development decision followed.
    Recording is the mechanism: adaptivity stays visible and strong
    claims are bounded by it (Dwork et al.).
    """

    model_config = ConfigDict(frozen=True)

    exposure_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    corpus_ref: AuthorityRef
    metrics_revealed: tuple[str, ...] = Field(min_length=1)
    decision_class: ExposureDecision
    decision_detail: str | None = None
    solver_version: str | None = None
    authority_version: str = Field(min_length=1)
    exposed_at_utc: str = Field(min_length=1)
    exposure_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_exposure(self) -> 'CadBenchmarkExposureRecord':
        _require_iso8601(self.exposed_at_utc, 'exposed_at_utc')
        if self.corpus_ref.ref_sha256 is None:
            raise ValueError('corpus_ref must carry its sha256 pin')
        expected = _hash(self.identity_payload())
        if self.exposure_sha256 != expected:
            raise ValueError('benchmark exposure hash mismatch')
        if self.exposure_id != _semantic_id('vsexp', expected):
            raise ValueError(
                'benchmark exposure id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'corpus_ref': self.corpus_ref.model_dump(mode='json'),
            'metrics_revealed': list(self.metrics_revealed),
            'decision_class': self.decision_class,
            'decision_detail': self.decision_detail,
            'solver_version': self.solver_version,
            'authority_version': self.authority_version,
            'exposed_at_utc': self.exposed_at_utc,
        }


def benchmark_exposure_binding(
    exposure: CadBenchmarkExposureRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='benchmark_exposure',
        ref_id=exposure.exposure_id,
        ref_sha256=exposure.exposure_sha256,
    )


class CadChallengeQualification(BaseModel):
    """The sealed validation-claim verdict (#698).

    Binds the statistical design, dependence model, corpus roles and
    exposure ledger to the strongest claim the evidence supports —
    ``pseudoreplication_detected`` / ``split_mismatch`` /
    ``independence_unestablished`` / ``winner_selection_biased`` /
    ``development_validation_only`` / ``locked_challenge_eligible`` /
    ``qualified_*``. ``corpus_states`` carries each corpus's resulting
    epistemic status so a benchmark's role can change for a later solver
    version without deleting history.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    design_ref: AuthorityRef
    dependence_ref: AuthorityRef | None = None
    state: ValidationClaimState
    corpus_states: tuple[CadCorpusExposureState, ...] = ()
    independent_unit: SamplingUnitKind
    independent_unit_count: int | None = None
    raw_observation_count: int = Field(ge=0)
    frequency_bin_count: int = Field(default=0, ge=0)
    reasons: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_qualification(self) -> 'CadChallengeQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        for label, ref in (
            ('design_ref', self.design_ref),
            ('dependence_ref', self.dependence_ref),
        ):
            if ref is not None and ref.ref_sha256 is None:
                raise ValueError(f'{label} must carry its sha256 pin')
        if self.state == 'locked_challenge_eligible' and not any(
            c.status == 'untouched' and c.role == 'locked_challenge_test'
            for c in self.corpus_states
        ):
            raise ValueError(
                'locked_challenge_eligible requires at least one '
                'untouched locked-challenge corpus'
            )
        if self.independent_unit_count is not None and (
            self.independent_unit_count > self.raw_observation_count
        ):
            raise ValueError(
                'independent count cannot exceed raw count'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('challenge qualification hash mismatch')
        if self.qualification_id != _semantic_id('vsqual', expected):
            raise ValueError(
                'challenge qualification id does not match its hash'
            )
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'design_ref': self.design_ref.model_dump(mode='json'),
            'dependence_ref': (
                self.dependence_ref.model_dump(mode='json')
                if self.dependence_ref is not None
                else None
            ),
            'state': self.state,
            'corpus_states': [
                c.model_dump(mode='json') for c in self.corpus_states
            ],
            'independent_unit': self.independent_unit,
            'independent_unit_count': self.independent_unit_count,
            'raw_observation_count': self.raw_observation_count,
            'frequency_bin_count': self.frequency_bin_count,
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seal_model(model, payload: dict[str, Any], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_statistical_design(
    *,
    document_id: str,
    design_label: str,
    generalization_claim: GeneralizationClaim,
    independent_unit: SamplingUnitKind,
    raw_observation_count: int,
    claim_detail: str | None = None,
    hierarchy: tuple[CadHierarchyLevel, ...] | list[
        CadHierarchyLevel
    ] = (),
    independent_unit_count: int | None = None,
    cluster_count: int | None = None,
    repeat_count: int = 0,
    frequency_bin_count: int = 0,
    effective_sample_size: float | None = None,
    effective_sample_size_method: str | None = None,
    error_distribution_scope: str | None = None,
    corpus_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    declared_at_utc: str | None = None,
    provenance_json: str = '{}',
) -> CadValidationStatisticalDesign:
    """Seal a validation statistical design."""
    payload = dict(
        document_id=document_id,
        design_label=design_label,
        generalization_claim=generalization_claim,
        claim_detail=claim_detail,
        independent_unit=independent_unit,
        hierarchy=tuple(hierarchy),
        raw_observation_count=raw_observation_count,
        independent_unit_count=independent_unit_count,
        cluster_count=cluster_count,
        repeat_count=repeat_count,
        frequency_bin_count=frequency_bin_count,
        effective_sample_size=effective_sample_size,
        effective_sample_size_method=effective_sample_size_method,
        error_distribution_scope=error_distribution_scope,
        corpus_refs=tuple(corpus_refs),
        authority_version=VSD_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
        provenance_json=provenance_json,
    )
    return _seal_model(
        CadValidationStatisticalDesign, payload,
        'design_id', 'design_sha256', 'vsdes',
    )


def build_dependence_model(
    *,
    document_id: str,
    dependence_classes: tuple[DependenceClass, ...] | list[
        DependenceClass
    ],
    design_ref: AuthorityRef | CadValidationStatisticalDesign | None = None,
    spatial_correlation_model: SpatialCorrelationModel = 'unknown',
    spatial_model_detail: str | None = None,
    frequency_coupling_causes: tuple[FrequencyCouplingCause, ...] | list[
        FrequencyCouplingCause
    ] = (),
    resampling_unit: SamplingUnitKind | None = None,
    resampling_method: str | None = None,
    notes: tuple[str, ...] | list[str] = (),
    declared_at_utc: str | None = None,
) -> CadDependenceModel:
    """Seal a declared dependence structure."""
    if isinstance(design_ref, CadValidationStatisticalDesign):
        design_ref = statistical_design_binding(design_ref)
    payload = dict(
        document_id=document_id,
        design_ref=design_ref,
        dependence_classes=tuple(dependence_classes),
        spatial_correlation_model=spatial_correlation_model,
        spatial_model_detail=spatial_model_detail,
        frequency_coupling_causes=tuple(frequency_coupling_causes),
        resampling_unit=resampling_unit,
        resampling_method=resampling_method,
        notes=tuple(notes),
        authority_version=VSD_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadDependenceModel, payload,
        'dependence_id', 'dependence_sha256', 'vsdep',
    )


def build_dataset_role_assignment(
    *,
    document_id: str,
    corpus_ref: AuthorityRef,
    role: DatasetRole,
    context_label: str,
    solver_version: str | None = None,
    declared_at_utc: str | None = None,
) -> CadDatasetRoleAssignment:
    """Seal a corpus's epistemic role."""
    payload = dict(
        document_id=document_id,
        corpus_ref=corpus_ref,
        role=role,
        context_label=context_label,
        solver_version=solver_version,
        authority_version=VSD_AUTHORITY_SCHEMA_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        CadDatasetRoleAssignment, payload,
        'assignment_id', 'assignment_sha256', 'vsrole',
    )


def build_benchmark_exposure(
    *,
    document_id: str,
    corpus_ref: AuthorityRef,
    metrics_revealed: tuple[str, ...] | list[str],
    decision_class: ExposureDecision,
    decision_detail: str | None = None,
    solver_version: str | None = None,
    exposed_at_utc: str | None = None,
) -> CadBenchmarkExposureRecord:
    """Seal one holdout-exposure ledger entry."""
    payload = dict(
        document_id=document_id,
        corpus_ref=corpus_ref,
        metrics_revealed=tuple(metrics_revealed),
        decision_class=decision_class,
        decision_detail=decision_detail,
        solver_version=solver_version,
        authority_version=VSD_AUTHORITY_SCHEMA_VERSION,
        exposed_at_utc=exposed_at_utc or _utc_now(),
    )
    return _seal_model(
        CadBenchmarkExposureRecord, payload,
        'exposure_id', 'exposure_sha256', 'vsexp',
    )


def evaluate_validation_claim(
    *,
    document_id: str,
    design: CadValidationStatisticalDesign,
    dependence: CadDependenceModel | None = None,
    role_assignments: tuple[CadDatasetRoleAssignment, ...] | list[
        CadDatasetRoleAssignment
    ] = (),
    exposures: tuple[CadBenchmarkExposureRecord, ...] | list[
        CadBenchmarkExposureRecord
    ] = (),
    claim_corpus_refs: tuple[AuthorityRef, ...] | list[AuthorityRef] = (),
    evaluated_at_utc: str | None = None,
) -> CadChallengeQualification:
    """Fail-closed validation-claim verdict (#698).

    Derivation order:

    - undeclared independent count → ``insufficient_evidence``;
    - declared independent count above the hierarchy's defensible cap
      (repeats/bins counted as replication) →
      ``pseudoreplication_detected``;
    - split-unit granularity below the generalization claim →
      ``split_mismatch``;
    - the claim corpus carries selection/tuning roles or model-selection
      exposures → ``winner_selection_biased`` /
      ``development_validation_only`` — the same corpus cannot be an
      unbiased final test for the variant it selected;
    - spatial/frequency structure with no dependence model →
      ``independence_unestablished``;
    - an untouched ``locked_challenge_test`` corpus present →
      ``locked_challenge_eligible``;
    - residual unknowns (``other_explicit`` unit, undeclared spatial
      model, missing error-distribution scope) →
      ``qualified_with_limitations``;
    - otherwise ``qualified_generalization``.
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    reasons: list[str] = []

    role_by_corpus: dict[str, DatasetRole] = {}
    for assignment in role_assignments:
        role_by_corpus[assignment.corpus_ref.ref_id] = assignment.role
    exposure_count: dict[str, int] = {}
    selection_exposed: set[str] = set()
    for exposure in exposures:
        rid = exposure.corpus_ref.ref_id
        exposure_count[rid] = exposure_count.get(rid, 0) + 1
        if exposure.decision_class in (
            'model_selection', 'threshold_change', 'solver_change',
            'metric_selection',
        ):
            selection_exposed.add(rid)

    all_corpus_refs = list(design.corpus_refs) + [
        r for r in claim_corpus_refs if r not in design.corpus_refs
    ]
    corpus_states: list[CadCorpusExposureState] = []
    for ref in all_corpus_refs:
        count = exposure_count.get(ref.ref_id, 0)
        role = role_by_corpus.get(ref.ref_id, 'unassigned')
        status: CorpusStatus = 'untouched' if count == 0 else 'exposed'
        corpus_states.append(
            CadCorpusExposureState(
                corpus_ref=ref,
                role=role,
                exposure_count=count,
                status=status,
            )
        )

    claim_corpus_ids = {
        r.ref_id for r in (claim_corpus_refs or design.corpus_refs)
    }

    # --- independent count -------------------------------------------------
    if design.independent_unit_count is None or (
        design.independent_unit_count == 0
    ):
        state: ValidationClaimState = 'insufficient_evidence'
        reasons.append(
            'the independent-unit count is undeclared — a raw '
            'observation count is not a generalization sample size'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    cap = design.independent_cap()
    if cap is not None and design.independent_unit_count > cap:
        state = 'pseudoreplication_detected'
        reasons.append(
            f'independent_unit_count={design.independent_unit_count} '
            f'exceeds the hierarchy\'s defensible cap {cap} — nested '
            'repeats/bins were counted as independent evidence'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    # --- claim vs split granularity ----------------------------------------
    unit_rank = _UNIT_RANK.get(design.independent_unit, 0)
    claim_rank = _CLAIM_MIN_RANK.get(design.generalization_claim, 0)
    if design.independent_unit != 'other_explicit' and (
        design.generalization_claim != 'other_declared'
    ) and unit_rank < claim_rank:
        state = 'split_mismatch'
        reasons.append(
            f'split unit {design.independent_unit} cannot support '
            f'claim {design.generalization_claim} — the outer group '
            'must be at least the claim\'s granularity (#698 §11)'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    # --- corpus roles / exposure -------------------------------------------
    selection_roles = {
        'model_selection_development',
        'threshold_policy_tuning',
        'development_validation',
        'calibration_parameter_fit',
        'repeatability_only',
    }
    claim_states = [
        c for c in corpus_states if c.corpus_ref.ref_id in claim_corpus_ids
    ]
    if any(
        c.role in ('model_selection_development',
                   'threshold_policy_tuning')
        or (
            c.corpus_ref.ref_id in selection_exposed
            and c.exposure_count > 0
        )
        for c in claim_states
    ):
        state = 'winner_selection_biased'
        reasons.append(
            'the claim corpus drove model/threshold selection or was '
            'exposed to selection decisions — its score is not an '
            'unbiased final-generalization estimate (Dwork et al.)'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)
    if any(
        c.role in selection_roles or c.exposure_count > 0
        for c in claim_states
    ):
        state = 'development_validation_only'
        reasons.append(
            'the claim corpus is a development/fit corpus — it remains '
            'valid regression evidence but cannot headline a '
            'generalization claim'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    # --- dependence ----------------------------------------------------------
    has_spatial = any(
        level.level_kind in (
            'room', 'room_config', 'source_position',
            'receiver_position', 'source_receiver_pair',
            'benchmark_scene', 'installation_project',
        )
        for level in design.hierarchy
    )
    has_frequency = design.frequency_bin_count > 0 or any(
        level.level_kind == 'frequency_bin' for level in design.hierarchy
    )
    if dependence is None and (has_spatial or has_frequency):
        state = 'independence_unestablished'
        reasons.append(
            'spatial/frequency-correlated observations with no declared '
            'dependence model — different XYZ is not statistically '
            'independent by geometry alone (Kuster 2008)'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    # --- locked challenge ----------------------------------------------------
    if any(
        c.role == 'locked_challenge_test' and c.status == 'untouched'
        for c in corpus_states
    ):
        state = 'locked_challenge_eligible'
        reasons.append(
            'an untouched locked-challenge corpus can carry a strong '
            'release/scientific claim'
        )
        return _seal(design, dependence, state, corpus_states, reasons,
                     evaluated_at_utc, document_id)

    # --- limitations -----------------------------------------------------------
    limitations: list[str] = []
    if design.independent_unit == 'other_explicit' or (
        design.generalization_claim == 'other_declared'
    ):
        limitations.append(
            'an explicitly declared unit/claim carries custom '
            'generalization semantics'
        )
    if dependence is not None and (
        dependence.spatial_correlation_model in ('none_declared', 'unknown')
        and 'spatial_correlation' in dependence.dependence_classes
    ):
        limitations.append(
            'spatial dependence is declared but no correlation model '
            'was established'
        )
    if not design.error_distribution_scope:
        limitations.append(
            'error-distribution scope (which population a percentile '
            'summarizes) is undeclared (#698 §7)'
        )
    if limitations:
        reasons.extend(limitations)
        state = 'qualified_with_limitations'
        return _seal(
            design, dependence, state, corpus_states, reasons,
            evaluated_at_utc, document_id,
        )

    state = 'qualified_generalization'
    return _seal(
        design, dependence, state, corpus_states, reasons,
        evaluated_at_utc, document_id,
    )


def _seal(
    design: CadValidationStatisticalDesign,
    dependence: CadDependenceModel | None,
    state: ValidationClaimState,
    corpus_states: list[CadCorpusExposureState],
    reasons: list[str],
    evaluated_at_utc: str,
    document_id: str,
) -> CadChallengeQualification:
    payload = dict(
        document_id=document_id,
        design_ref=statistical_design_binding(design),
        dependence_ref=(
            dependence_model_binding(dependence)
            if dependence is not None
            else None
        ),
        state=state,
        corpus_states=tuple(corpus_states),
        independent_unit=design.independent_unit,
        independent_unit_count=design.independent_unit_count,
        raw_observation_count=design.raw_observation_count,
        frequency_bin_count=design.frequency_bin_count,
        reasons=tuple(reasons),
        evaluation_version=VSD_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        CadChallengeQualification, payload,
        'qualification_id', 'qualification_sha256', 'vsqual',
    )


__all__ = [
    'CadBenchmarkExposureRecord',
    'CadChallengeQualification',
    'CadCorpusExposureState',
    'CadDatasetRoleAssignment',
    'CadDependenceModel',
    'CadHierarchyLevel',
    'CadValidationStatisticalDesign',
    'CorpusStatus',
    'DatasetRole',
    'DependenceClass',
    'ExposureDecision',
    'FrequencyCouplingCause',
    'GeneralizationClaim',
    'HierarchyLevelKind',
    'SamplingUnitKind',
    'SpatialCorrelationModel',
    'VSD_AUTHORITY_SCHEMA_VERSION',
    'VSD_EVALUATION_VERSION',
    'ValidationClaimState',
    'benchmark_exposure_binding',
    'build_benchmark_exposure',
    'build_dataset_role_assignment',
    'build_dependence_model',
    'build_statistical_design',
    'dataset_role_binding',
    'dependence_model_binding',
    'evaluate_validation_claim',
    'statistical_design_binding',
]
