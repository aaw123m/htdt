"""#809 external-benchmark qualification authority tests."""

from __future__ import annotations

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_benchmark import (
    BenchmarkValidationEvidence,
    ObservableEvaluation,
)
from htdt.cad_benchmark_qualification import (
    BenchmarkPreregistration,
    BenchmarkQualification,
    BenchmarkSceneMapping,
    FrozenConfigurationError,
    evaluate_qualification,
)
from htdt.cad_benchmark_qualification_repository import (
    CadBenchmarkQualificationRepository,
    QualificationConflictError,
    QualificationIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema


DOC = 'doc-809'

_ASSET = AuthorityRef(
    kind='benchmark_source_asset',
    ref_id='asset-bras-rs1',
    ref_sha256='a' * 64,
)


def _mapping(**over) -> BenchmarkSceneMapping:
    kwargs = dict(
        document_id=DOC,
        asset_ref=_ASSET,
        corpus_scene_id='BRAS-RS1-SC1',
        solver_path='wave_r130',
        phenomenon_id='single_reflection',
        observable_ids=('obs_mag', 'obs_arrival'),
        applicability_band_hz=(20.0, 500.0),
        boundary_model_family='locally_reacting_impedance',
        curvature_class='planar',
        rationale='rigid wall single reflection',
    )
    kwargs.update(over)
    return BenchmarkSceneMapping.create(**kwargs)


def _evidence(**over) -> BenchmarkValidationEvidence:
    kwargs = dict(
        evidence_id='bev-1',
        benchmark_id='bras-rs1',
        benchmark_version='v3',
        benchmark_sha256='b' * 64,
        importer_id='canonical-json',
        importer_version='1',
        provider_id='pffdtd',
        provider_version='0.9',
        provider_config_sha256='c' * 64,
        evaluation_profile_id='profile-a',
        evaluation_profile_version='1',
        evidence_class='external_measured',
        status='PASS',
        observable_evaluations=(
            ObservableEvaluation(
                observable_id='obs_mag', kind='magnitude_fr',
                status='PASS', metric_id='rms_db', error=0.4),
            ObservableEvaluation(
                observable_id='obs_arrival', kind='arrival_timing',
                status='PASS', metric_id='tof_ms', error=0.2),
        ),
        unsupported_count=0,
    )
    kwargs.update(over)
    return BenchmarkValidationEvidence(**kwargs)


def _prereg(**over) -> BenchmarkPreregistration:
    kwargs = dict(
        document_id=DOC,
        mapping_ref=AuthorityRef(
            kind='benchmark_scene_mapping',
            ref_id=_mapping().mapping_id,
            ref_sha256=_mapping().mapping_sha256,
        ),
        benchmark_id='bras-rs1',
        benchmark_version='v3',
        benchmark_sha256='b' * 64,
        provider_id='pffdtd',
        provider_version='0.9',
        provider_config_sha256='c' * 64,
        evaluation_profile_id='profile-a',
        evaluation_profile_version='1',
        declared_observable_ids=('obs_mag', 'obs_arrival'),
        convergence_axes_planned=('grid_dx', 'timestep'),
        run_mode='preregistered_unfitted',
    )
    kwargs.update(over)
    return BenchmarkPreregistration.create(**kwargs)


def _payload(**over):
    kwargs = dict(
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx', 'timestep'),
        input_authority_complete=True,
        reference_quality_ok=True,
    )
    kwargs.update(over)
    return evaluate_qualification(
        _mapping(), _prereg(), _evidence(), **kwargs)


def test_unfitted_measured_pass_reaches_external_validated() -> None:
    payload = _payload()
    assert payload['verdict'] == 'PASS_WITHIN_DOMAIN'
    assert payload['level_attained'] == 'EXTERNAL_BENCHMARK_VALIDATED'
    assert payload['predictive'] is True
    q = BenchmarkQualification.create(**payload)
    assert q.qualification_id.startswith('bqual-')


def test_analytic_corpus_caps_at_numerically_verified() -> None:
    payload = evaluate_qualification(
        _mapping(), _prereg(),
        _evidence(evidence_class='analytic'),
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx', 'timestep'),
        input_authority_complete=True,
        reference_quality_ok=True,
    )
    assert payload['verdict'] == 'PASS_WITHIN_DOMAIN'
    assert payload['level_attained'] == 'NUMERICALLY_VERIFIED'


def test_informed_run_requires_unfitted_link_and_params() -> None:
    prior = AuthorityRef(
        kind='benchmark_validation_evidence',
        ref_id='bev-0', ref_sha256='d' * 64)
    with pytest.raises(ValueError, match='prior unfitted'):
        _prereg(run_mode='informed_calibrated',
                informed_parameters=('absorption',))
    with pytest.raises(ValueError, match='tuned parameters'):
        _prereg(run_mode='informed_calibrated',
                prior_unfitted_evidence_ref=prior)
    prereg = _prereg(
        run_mode='informed_calibrated',
        informed_parameters=('absorption',),
        prior_unfitted_evidence_ref=prior)
    payload = evaluate_qualification(
        _mapping(), prereg, _evidence(),
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx', 'timestep'),
        input_authority_complete=True,
        reference_quality_ok=True)
    assert payload['run_mode'] == 'informed_calibrated'
    assert payload['predictive'] is False
    q = BenchmarkQualification.create(**payload)
    assert q.run_mode == 'informed_calibrated'


def test_frozen_configuration_violations_raise() -> None:
    with pytest.raises(FrozenConfigurationError, match='config hash'):
        evaluate_qualification(
            _mapping(), _prereg(),
            _evidence(provider_config_sha256='e' * 64),
            convergence='CONVERGED_WITHIN_TESTED_RANGE',
            convergence_axes_tested=('grid_dx', 'timestep'),
            input_authority_complete=True, reference_quality_ok=True)
    with pytest.raises(FrozenConfigurationError, match='provider version'):
        evaluate_qualification(
            _mapping(), _prereg(),
            _evidence(provider_version='0.10'),
            convergence='CONVERGED_WITHIN_TESTED_RANGE',
            convergence_axes_tested=('grid_dx', 'timestep'),
            input_authority_complete=True, reference_quality_ok=True)


def test_missing_convergence_is_numerical_nonconvergence() -> None:
    for status in ('NOT_CONVERGED', 'INSUFFICIENT_EVIDENCE'):
        payload = _payload(convergence=status)
        assert payload['verdict'] == 'NUMERICAL_NONCONVERGENCE'
        assert payload['level_attained'] == 'CAN_REPRESENT'


def test_untested_planned_axis_blocks_pass() -> None:
    payload = _payload(convergence_axes_tested=('grid_dx',))
    assert payload['verdict'] == 'NUMERICAL_NONCONVERGENCE'


def test_reference_and_input_gates() -> None:
    assert _payload(reference_quality_ok=False)['verdict'] == (
        'INSUFFICIENT_REFERENCE_QUALITY')
    assert _payload(input_authority_complete=False)['verdict'] == (
        'INSUFFICIENT_INPUT_AUTHORITY')


def test_band_outside_mapping_domain_is_outside_applicability() -> None:
    payload = _payload(verdict_band_hz=(20.0, 2000.0))
    assert payload['verdict'] == 'OUTSIDE_APPLICABILITY'


def test_missing_declared_observable_is_unsupported() -> None:
    payload = evaluate_qualification(
        _mapping(), _prereg(),
        _evidence(
            status='UNKNOWN',
            observable_evaluations=(),
            unsupported_count=2),
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx', 'timestep'),
        input_authority_complete=True, reference_quality_ok=True)
    assert payload['verdict'] == 'UNSUPPORTED_OBSERVABLE'


def test_failed_observable_is_fail() -> None:
    payload = evaluate_qualification(
        _mapping(), _prereg(),
        _evidence(status='FAIL', observable_evaluations=(
            ObservableEvaluation(
                observable_id='obs_mag', status='PASS'),
            ObservableEvaluation(
                observable_id='obs_arrival', status='FAIL',
                reason='5 dB over tolerance'),
        )),
        convergence='CONVERGED_WITHIN_TESTED_RANGE',
        convergence_axes_tested=('grid_dx', 'timestep'),
        input_authority_complete=True, reference_quality_ok=True)
    assert payload['verdict'] == 'FAIL'
    assert payload['observable_statuses']['obs_arrival'] == 'FAIL'


def test_qualification_never_reaches_production_levels() -> None:
    payload = _payload()
    payload['level_attained'] = 'PRODUCTION_RECOMMENDATION_ELIGIBLE'
    with pytest.raises(ValueError, match='owned-room'):
        BenchmarkQualification.create(**payload)


def test_mapping_requires_pinned_asset() -> None:
    with pytest.raises(ValueError, match='pin its sha256'):
        _mapping(asset_ref=AuthorityRef(
            kind='benchmark_source_asset', ref_id='x'))


def test_prereg_observables_must_stay_within_mapping() -> None:
    prereg = _prereg(declared_observable_ids=('obs_mag', 'obs_extra'))
    with pytest.raises(FrozenConfigurationError, match='outside'):
        evaluate_qualification(
            _mapping(), prereg, _evidence(),
            convergence='CONVERGED_WITHIN_TESTED_RANGE',
            convergence_axes_tested=('grid_dx', 'timestep'),
            input_authority_complete=True, reference_quality_ok=True)


def _repo(tmp_path):
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadBenchmarkQualificationRepository(SceneRepository(db))


def test_repository_roundtrip_and_append_only(tmp_path) -> None:
    repo = _repo(tmp_path)
    mapping = _mapping()
    prereg = _prereg()
    qual = BenchmarkQualification.create(**_payload())
    repo.scene_mappings.save(mapping)
    repo.preregistrations.save(prereg)
    repo.qualifications.save(qual)
    assert repo.get_mapping(mapping.mapping_id) == mapping
    assert repo.get_preregistration(
        prereg.preregistration_id) == prereg
    assert repo.get_qualification(
        qual.qualification_id) == qual
    assert repo.scene_mappings.list(DOC) == (mapping,)
    # Idempotent re-save is fine; a different payload under the same id is not.
    repo.scene_mappings.save(mapping)
    tampered = mapping.model_copy(update={
        'mapping_sha256': 'f' * 64})
    with pytest.raises(QualificationIntegrityError):
        repo.scene_mappings.save(tampered)


def test_repository_rejects_payload_column_tamper(tmp_path) -> None:
    repo = _repo(tmp_path)
    mapping = _mapping()
    repo.scene_mappings.save(mapping)
    with repo._connect() as connection:
        connection.execute(
            'UPDATE cad_benchmark_scene_mappings SET solver_path=? '
            'WHERE mapping_id=?',
            ('geometric_r150', mapping.mapping_id))
    with pytest.raises(QualificationIntegrityError, match='solver_path'):
        repo.get_mapping(mapping.mapping_id)


def test_conflicting_id_rejected(tmp_path) -> None:
    repo = _repo(tmp_path)
    mapping = _mapping()
    repo.scene_mappings.save(mapping)
    other = _mapping(corpus_scene_id='BRAS-RS1-SC2')
    forged = other.model_copy(update={'mapping_id': mapping.mapping_id})
    with pytest.raises(QualificationIntegrityError):
        repo.scene_mappings.save(forged)
