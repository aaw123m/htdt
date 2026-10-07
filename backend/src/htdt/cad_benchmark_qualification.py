"""External-benchmark qualification authority (#809).

``cad_benchmark`` already turns an admitted external dataset into an exact,
replayable case and produces per-observable evidence. This module owns the
layer #809 adds on top: *what a benchmark run is allowed to claim*.

- :class:`BenchmarkSceneMapping` — sealed binding between a corpus scene
  (pinned by :class:`~.cad_authority_resolver.AuthorityRef` to the admitted
  ``BenchmarkSourceAsset``) and the HTDT phenomenon/solver path it is meant
  to qualify, plus the declared applicability domain (band, boundary-model
  family, surface-curvature class — the BRAS RS8 curved-surface lane) and
  the observable inventory the scene is claimed to exercise.
- :class:`BenchmarkPreregistration` — sealed run specification frozen
  *before* any reference comparison: case hash, provider identity and
  config hash, evaluation profile, declared observables and the planned
  convergence axes. ``run_mode`` separates ``preregistered_unfitted`` from
  ``informed_calibrated``; an informed run must link the prior unfitted
  evidence it builds on and name exactly which parameters were tuned —
  it can never stand in as a blind predictive result.
- :func:`evaluate_qualification` — fail-closed verdict derivation.
  Frozen-configuration violations raise; every non-passing conclusion is a
  named verdict (``INSUFFICIENT_REFERENCE_QUALITY``,
  ``INSUFFICIENT_INPUT_AUTHORITY``, ``NUMERICAL_NONCONVERGENCE``,
  ``OUTSIDE_APPLICABILITY``, ``UNSUPPORTED_OBSERVABLE``), never a forced
  PASS/FAIL and never an aggregate score.
- :class:`BenchmarkQualification` — the sealed verdict record. Its
  ``level_attained`` is capped at ``EXTERNAL_BENCHMARK_VALIDATED`` and only
  for ``external_measured`` evidence; analytic/independent-numerical
  corpora cap at ``NUMERICALLY_VERIFIED``. Nothing here can produce
  ``OWNED_ROOM_VALIDATED`` or ``PRODUCTION_RECOMMENDATION_ELIGIBLE`` — those
  stay behind the owned-room campaign gate (#801).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .acoustic_validation_envelope import ConvergenceStatus
from .cad_authority_resolver import AuthorityRef
from .cad_benchmark import (
    BenchmarkResultStatus,
    BenchmarkValidationEvidence,
)
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


QUALIFICATION_SCHEMA_VERSION: Literal[1] = 1

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

BenchmarkSolverPath = Literal['wave_r130', 'geometric_r150', 'hybrid']

SurfaceCurvatureClass = Literal[
    'planar',
    'curved',
    'edge_diffraction',
    'mixed',
]
"""BRAS RS8 lane: curved/edge-diffraction claims are qualified separately —
planar qualification never covers curvature."""

BenchmarkRunMode = Literal['preregistered_unfitted', 'informed_calibrated']
"""#809 §3: unfitted runs freeze configuration before seeing the reference;
informed runs used the reference to tune inputs and must declare it."""

BenchmarkVerdict = Literal[
    'PASS_WITHIN_DOMAIN',
    'FAIL',
    'INSUFFICIENT_REFERENCE_QUALITY',
    'INSUFFICIENT_INPUT_AUTHORITY',
    'NUMERICAL_NONCONVERGENCE',
    'OUTSIDE_APPLICABILITY',
    'UNSUPPORTED_OBSERVABLE',
]
"""#809 §8: every conclusion is named; PASS is always domain-limited."""

QualificationLevel = Literal[
    'CAN_REPRESENT',
    'NUMERICALLY_VERIFIED',
    'EXTERNAL_BENCHMARK_VALIDATED',
    'OWNED_ROOM_VALIDATED',
    'PRODUCTION_RECOMMENDATION_ELIGIBLE',
]
"""#809 §6 ladder. This module can only ever emit the first three —
capability rows stay representation, not validation."""

_MAX_EXTERNAL_LEVEL: QualificationLevel = 'EXTERNAL_BENCHMARK_VALIDATED'
_LEVEL_ORDER: tuple[QualificationLevel, ...] = (
    'CAN_REPRESENT',
    'NUMERICALLY_VERIFIED',
    'EXTERNAL_BENCHMARK_VALIDATED',
    'OWNED_ROOM_VALIDATED',
    'PRODUCTION_RECOMMENDATION_ELIGIBLE',
)


class QualificationIntegrityError(ValueError):
    """A sealed record's identity fields disagree with its payload."""


class FrozenConfigurationError(ValueError):
    """A qualification run diverged from its preregistration."""


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


def _require_pinned(ref: AuthorityRef, role: str) -> None:
    if ref.ref_sha256 is None:
        raise ValueError(f'{role} reference must pin its sha256')


def _require_band(band: tuple[float, float], role: str) -> tuple[float, float]:
    low, high = float(band[0]), float(band[1])
    if not (low > 0.0 and high > low):
        raise ValueError(f'{role} must satisfy 0 < low < high')
    return (low, high)


def _band_within(
    inner: tuple[float, float] | None,
    outer: tuple[float, float],
) -> bool:
    if inner is None:
        return True
    return outer[0] <= inner[0] and inner[1] <= outer[1]


class BenchmarkSceneMapping(BaseModel):
    """Sealed binding: corpus scene → phenomenon/solver path + domain."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = QUALIFICATION_SCHEMA_VERSION
    mapping_id: str = Field(min_length=1)
    mapping_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    asset_ref: AuthorityRef
    corpus_scene_id: str = Field(min_length=1)
    solver_path: BenchmarkSolverPath
    phenomenon_id: str = Field(min_length=1)
    observable_ids: tuple[str, ...] = Field(min_length=1)
    applicability_band_hz: tuple[float, float]
    boundary_model_family: str = Field(min_length=1)
    curvature_class: SurfaceCurvatureClass
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'BenchmarkSceneMapping':
        _require_pinned(self.asset_ref, 'benchmark asset')
        _require_band(self.applicability_band_hz, 'applicability_band_hz')
        if len(set(self.observable_ids)) != len(self.observable_ids):
            raise ValueError('observable_ids must be unique')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={
            'mapping_id', 'mapping_sha256'})

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        asset_ref: AuthorityRef,
        corpus_scene_id: str,
        solver_path: BenchmarkSolverPath,
        phenomenon_id: str,
        observable_ids: tuple[str, ...] | list[str],
        applicability_band_hz: tuple[float, float],
        boundary_model_family: str,
        curvature_class: SurfaceCurvatureClass,
        rationale: str,
    ) -> 'BenchmarkSceneMapping':
        return _seal(
            cls,
            {
                'document_id': document_id,
                'asset_ref': asset_ref,
                'corpus_scene_id': corpus_scene_id,
                'solver_path': solver_path,
                'phenomenon_id': phenomenon_id,
                'observable_ids': tuple(observable_ids),
                'applicability_band_hz': applicability_band_hz,
                'boundary_model_family': boundary_model_family,
                'curvature_class': curvature_class,
                'rationale': rationale,
            },
            'mapping_id', 'mapping_sha256', 'bmap',
        )


class BenchmarkPreregistration(BaseModel):
    """Sealed frozen run spec — created before reference comparison."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = QUALIFICATION_SCHEMA_VERSION
    preregistration_id: str = Field(min_length=1)
    preregistration_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    mapping_ref: AuthorityRef
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    benchmark_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    provider_config_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    evaluation_profile_id: str = Field(min_length=1)
    evaluation_profile_version: str = Field(min_length=1)
    declared_observable_ids: tuple[str, ...] = Field(min_length=1)
    convergence_axes_planned: tuple[str, ...] = ()
    run_mode: BenchmarkRunMode
    informed_parameters: tuple[str, ...] = ()
    prior_unfitted_evidence_ref: AuthorityRef | None = None
    created_at_utc: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _check(self) -> 'BenchmarkPreregistration':
        _require_pinned(self.mapping_ref, 'scene mapping')
        if len(set(self.declared_observable_ids)) != len(
                self.declared_observable_ids):
            raise ValueError('declared_observable_ids must be unique')
        if self.run_mode == 'informed_calibrated':
            if self.prior_unfitted_evidence_ref is None:
                raise ValueError(
                    'informed_calibrated runs must link the prior unfitted '
                    'evidence they build on (#809 §3)')
            _require_pinned(
                self.prior_unfitted_evidence_ref, 'prior unfitted evidence')
            if not self.informed_parameters:
                raise ValueError(
                    'informed_calibrated runs must name the tuned parameters')
        else:
            if self.informed_parameters:
                raise ValueError(
                    'unfitted preregistrations cannot carry informed '
                    'parameters')
            if self.prior_unfitted_evidence_ref is not None:
                raise ValueError(
                    'unfitted preregistrations cannot link prior unfitted '
                    'evidence')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={
            'preregistration_id', 'preregistration_sha256'})

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        mapping_ref: AuthorityRef,
        benchmark_id: str,
        benchmark_version: str,
        benchmark_sha256: str,
        provider_id: str,
        provider_version: str,
        provider_config_sha256: str | None,
        evaluation_profile_id: str,
        evaluation_profile_version: str,
        declared_observable_ids: tuple[str, ...] | list[str],
        convergence_axes_planned: tuple[str, ...] | list[str] = (),
        run_mode: BenchmarkRunMode = 'preregistered_unfitted',
        informed_parameters: tuple[str, ...] | list[str] = (),
        prior_unfitted_evidence_ref: AuthorityRef | None = None,
        created_at_utc: str | None = None,
    ) -> 'BenchmarkPreregistration':
        return _seal(
            cls,
            {
                'document_id': document_id,
                'mapping_ref': mapping_ref,
                'benchmark_id': benchmark_id,
                'benchmark_version': benchmark_version,
                'benchmark_sha256': benchmark_sha256,
                'provider_id': provider_id,
                'provider_version': provider_version,
                'provider_config_sha256': provider_config_sha256,
                'evaluation_profile_id': evaluation_profile_id,
                'evaluation_profile_version': evaluation_profile_version,
                'declared_observable_ids': tuple(declared_observable_ids),
                'convergence_axes_planned': tuple(convergence_axes_planned),
                'run_mode': run_mode,
                'informed_parameters': tuple(informed_parameters),
                'prior_unfitted_evidence_ref': prior_unfitted_evidence_ref,
                'created_at_utc': created_at_utc,
            },
            'preregistration_id', 'preregistration_sha256', 'bpreg',
        )


def _check_frozen_configuration(
    prereg: BenchmarkPreregistration,
    evidence: BenchmarkValidationEvidence,
) -> None:
    """The run being qualified must be exactly the preregistered one."""
    if evidence.benchmark_sha256 != prereg.benchmark_sha256:
        raise FrozenConfigurationError(
            'evidence benchmark hash differs from the preregistered case')
    if evidence.benchmark_id != prereg.benchmark_id:
        raise FrozenConfigurationError(
            'evidence benchmark id differs from the preregistered case')
    if evidence.benchmark_version != prereg.benchmark_version:
        raise FrozenConfigurationError(
            'evidence benchmark version differs from the preregistered case')
    if evidence.provider_id != prereg.provider_id:
        raise FrozenConfigurationError(
            'provider id differs from the preregistered solver identity')
    if evidence.provider_version != prereg.provider_version:
        raise FrozenConfigurationError(
            'provider version differs from the preregistered solver identity')
    if evidence.provider_config_sha256 != prereg.provider_config_sha256:
        raise FrozenConfigurationError(
            'provider config hash differs from the preregistered solver '
            'configuration — a tuned run is a different applicability '
            'identity')
    if evidence.evaluation_profile_id != prereg.evaluation_profile_id:
        raise FrozenConfigurationError(
            'evaluation profile id differs from the preregistration')
    if evidence.evaluation_profile_version != prereg.evaluation_profile_version:
        raise FrozenConfigurationError(
            'evaluation profile version differs from the preregistration')


def evaluate_qualification(
    mapping: BenchmarkSceneMapping,
    prereg: BenchmarkPreregistration,
    evidence: BenchmarkValidationEvidence,
    *,
    convergence: ConvergenceStatus,
    convergence_axes_tested: tuple[str, ...] | list[str],
    input_authority_complete: bool,
    reference_quality_ok: bool,
    verdict_band_hz: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Fail-closed verdict derivation for one benchmark run.

    Returns the payload for :meth:`BenchmarkQualification.create`; the
    caller seals and persists it. Order of checks is deliberate: identity
    first (frozen configuration), then coverage of the declared domain and
    observables, then numerical and input authority, then per-observable
    outcomes. No path can emit PASS without each gate.
    """
    _check_frozen_configuration(prereg, evidence)
    if prereg.mapping_ref.ref_id != mapping.mapping_id:
        raise FrozenConfigurationError(
            'preregistration mapping ref does not resolve to this mapping')
    if prereg.mapping_ref.ref_sha256 != mapping.mapping_sha256:
        raise FrozenConfigurationError(
            'preregistration was made against a different mapping revision')

    declared = set(mapping.observable_ids)
    prereg_declared = set(prereg.declared_observable_ids)
    if not prereg_declared <= declared:
        raise FrozenConfigurationError(
            'preregistration declares observables outside the scene mapping')

    observed = {
        result.observable_id: result
        for result in evidence.observable_evaluations
    }

    statuses: dict[str, BenchmarkResultStatus] = {
        observable_id: (
            observed[observable_id].status
            if observable_id in observed else 'UNKNOWN'
        )
        for observable_id in prereg.declared_observable_ids
    }

    missing = [oid for oid in prereg.declared_observable_ids
               if oid not in observed]
    if evidence.status == 'NOT_APPLICABLE' or (
            missing and all(s != 'PASS' for s in statuses.values())):
        verdict: BenchmarkVerdict = 'UNSUPPORTED_OBSERVABLE'
        reason = (
            'the case/provider cannot produce the declared observables'
        )
    elif convergence != 'CONVERGED_WITHIN_TESTED_RANGE':
        verdict = 'NUMERICAL_NONCONVERGENCE'
        reason = (
            f'convergence status {convergence!r}; numerical evidence is '
            'required independently of experimental match (#809 §5)')
    elif set(prereg.convergence_axes_planned) - set(
            convergence_axes_tested):
        verdict = 'NUMERICAL_NONCONVERGENCE'
        reason = 'planned convergence axes were not all tested'
    elif not reference_quality_ok:
        verdict = 'INSUFFICIENT_REFERENCE_QUALITY'
        reason = 'the admitted reference does not support the claim'
    elif not input_authority_complete:
        verdict = 'INSUFFICIENT_INPUT_AUTHORITY'
        reason = (
            'geometry/boundary/source/receiver input authority is '
            'incomplete for this scene')
    else:
        effective_band = verdict_band_hz or mapping.applicability_band_hz
        if not _band_within(
                effective_band, mapping.applicability_band_hz):
            verdict = 'OUTSIDE_APPLICABILITY'
            reason = (
                'the claimed band exceeds the mapping applicability domain')
        elif 'FAIL' in statuses.values() or evidence.status == 'FAIL':
            verdict = 'FAIL'
            reason = 'at least one declared observable failed its metric'
        elif 'UNKNOWN' in statuses.values() or evidence.status == 'UNKNOWN':
            verdict = 'INSUFFICIENT_REFERENCE_QUALITY'
            reason = (
                'declared observables have no usable reference data')
        else:
            verdict = 'PASS_WITHIN_DOMAIN'
            reason = (
                'all declared observables pass inside the declared domain')

    band = (
        verdict_band_hz
        if verdict_band_hz is not None
        else mapping.applicability_band_hz
    )
    if verdict == 'PASS_WITHIN_DOMAIN':
        if evidence.evidence_class == 'external_measured':
            level: QualificationLevel = _MAX_EXTERNAL_LEVEL
        else:
            # Analytic / numerical corpora are code-verification lanes —
            # never independent measured validation (#809 §1.1).
            level = 'NUMERICALLY_VERIFIED'
    else:
        level = 'CAN_REPRESENT'

    return {
        'document_id': mapping.document_id,
        'mapping_ref': AuthorityRef(
            kind='benchmark_scene_mapping',
            ref_id=mapping.mapping_id,
            ref_sha256=mapping.mapping_sha256,
        ),
        'preregistration_ref': AuthorityRef(
            kind='benchmark_preregistration',
            ref_id=prereg.preregistration_id,
            ref_sha256=prereg.preregistration_sha256,
        ),
        'evidence_ref': AuthorityRef(
            kind='benchmark_validation_evidence',
            ref_id=evidence.evidence_id,
            ref_sha256=None,
        ),
        'verdict': verdict,
        'verdict_reason': reason,
        'verdict_band_hz': band,
        'boundary_model_family': mapping.boundary_model_family,
        'curvature_class': mapping.curvature_class,
        'level_attained': level,
        'run_mode': prereg.run_mode,
        'predictive': prereg.run_mode == 'preregistered_unfitted',
        'observable_statuses': statuses,
        'convergence': convergence,
        'convergence_axes_tested': tuple(convergence_axes_tested),
    }


class BenchmarkQualification(BaseModel):
    """Sealed verdict record for one qualified benchmark run."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = QUALIFICATION_SCHEMA_VERSION
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    mapping_ref: AuthorityRef
    preregistration_ref: AuthorityRef
    evidence_ref: AuthorityRef
    verdict: BenchmarkVerdict
    verdict_reason: str = Field(min_length=1)
    verdict_band_hz: tuple[float, float]
    boundary_model_family: str = Field(min_length=1)
    curvature_class: SurfaceCurvatureClass
    level_attained: QualificationLevel
    run_mode: BenchmarkRunMode
    predictive: bool
    observable_statuses: dict[str, BenchmarkResultStatus]
    convergence: ConvergenceStatus
    convergence_axes_tested: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'BenchmarkQualification':
        _require_pinned(self.mapping_ref, 'scene mapping')
        _require_pinned(self.preregistration_ref, 'preregistration')
        _require_band(self.verdict_band_hz, 'verdict_band_hz')
        if self.predictive and self.run_mode != 'preregistered_unfitted':
            raise ValueError(
                'only unfitted preregistered runs are predictive evidence')
        if self.level_attained in (
                'OWNED_ROOM_VALIDATED', 'PRODUCTION_RECOMMENDATION_ELIGIBLE'):
            raise ValueError(
                'benchmark qualification can never emit owned-room or '
                'production-recommendation levels (#801 gate)')
        if (self.verdict == 'PASS_WITHIN_DOMAIN'
                and self.level_attained == 'CAN_REPRESENT'):
            raise ValueError(
                'a passing verdict must attain at least '
                'NUMERICALLY_VERIFIED')
        if (self.verdict != 'PASS_WITHIN_DOMAIN'
                and self.level_attained != 'CAN_REPRESENT'):
            raise ValueError(
                'a non-passing verdict stays at CAN_REPRESENT — capability '
                'is not validation (#809 §6)')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='python', exclude={
            'qualification_id', 'qualification_sha256'})

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        mapping_ref: AuthorityRef,
        preregistration_ref: AuthorityRef,
        evidence_ref: AuthorityRef,
        verdict: BenchmarkVerdict,
        verdict_reason: str,
        verdict_band_hz: tuple[float, float],
        boundary_model_family: str,
        curvature_class: SurfaceCurvatureClass,
        level_attained: QualificationLevel,
        run_mode: BenchmarkRunMode,
        predictive: bool,
        observable_statuses: dict[str, BenchmarkResultStatus],
        convergence: ConvergenceStatus,
        convergence_axes_tested: tuple[str, ...] | list[str] = (),
    ) -> 'BenchmarkQualification':
        return _seal(
            cls,
            {
                'document_id': document_id,
                'mapping_ref': mapping_ref,
                'preregistration_ref': preregistration_ref,
                'evidence_ref': evidence_ref,
                'verdict': verdict,
                'verdict_reason': verdict_reason,
                'verdict_band_hz': verdict_band_hz,
                'boundary_model_family': boundary_model_family,
                'curvature_class': curvature_class,
                'level_attained': level_attained,
                'run_mode': run_mode,
                'predictive': predictive,
                'observable_statuses': dict(observable_statuses),
                'convergence': convergence,
                'convergence_axes_tested': tuple(convergence_axes_tested),
            },
            'qualification_id', 'qualification_sha256', 'bqual',
        )


__all__ = [
    'QUALIFICATION_SCHEMA_VERSION',
    'BenchmarkSolverPath',
    'SurfaceCurvatureClass',
    'BenchmarkRunMode',
    'BenchmarkVerdict',
    'QualificationLevel',
    'BenchmarkSceneMapping',
    'BenchmarkPreregistration',
    'BenchmarkQualification',
    'QualificationIntegrityError',
    'FrozenConfigurationError',
    'evaluate_qualification',
]
