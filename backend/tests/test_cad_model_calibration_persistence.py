"""#948: durable calibration lifecycle — persistence, exact target
resolution, materialized calibrated model, repository-derived holdout."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_acoustic_snapshot import (
    AcousticSceneReadiness,
    AcousticSceneSnapshot,
    AcousticSceneSourceBinding,
    AcousticReceiverBinding,
    ObservableReadiness,
    SnapshotEnvironmentAuthorityRef,
    SurfaceBoundaryConfiguration,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_model_calibration import (
    CalibrationEvidenceRef,
    CalibrationObjectiveSpec,
    CalibrationOptimizerSpec,
    CalibrationParameterDefinition,
    build_model_calibration_spec,
    freeze_calibrated_model,
    materialize_calibrated_model,
    resolve_calibration_targets,
)
from htdt.cad_model_calibration_repository import (
    CadModelCalibrationRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _digest(payload) -> str:
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
    ).hexdigest()


def _ref(label: str, version: str = '1') -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'authority:{label}',
        authority_version=version,
        semantic_hash_sha256=_hash(label),
    )


SURFACE_ID = 'semantic-surface:' + _hash('surface-1')


def _snapshot(revision) -> AcousticSceneSnapshot:
    source = AcousticSceneSourceBinding(
        source_entity_id='source-1',
        source_entity_sha256=_hash('source-entity'),
        equipment_definition_id='equipment-1',
        equipment_definition_version='1',
        equipment_definition_sha256=_hash('equipment'),
        r110_compiled_source_sha256=_hash('r110'),
        source_reference_point=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        source_reference_semantics='acoustic_reference_point',
        source_axis=Direction3(x=1.0, y=0.0, z=0.0),
        source_axis_semantics='explicit_acoustic_axis',
        directivity_capability='complex',
        geometric_directivity_state='AVAILABLE',
        complex_directivity_state='AVAILABLE',
        wave_excitation_state='AVAILABLE',
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=20.0, maximum_hz=500.0
        ),
    )
    receiver = AcousticReceiverBinding(
        receiver_id='receiver-1',
        entity_id='receiver-1-entity',
        world_position=Position3(x_m=2.5, y_m=2.5, z_m=1.0),
        acoustic_reference_semantics='scene_acoustic_reference_position',
        requested_output_capabilities=('complex_pressure',),
    )
    environment = SnapshotEnvironmentAuthorityRef(
        authority=_ref('environment'),
        sound_speed_m_s=343.0,
        sound_speed_source_authority=_ref('sound-speed'),
        temperature_c=20.0,
        temperature_source_authority=_ref('temperature'),
    )
    readiness = AcousticSceneReadiness(
        geometry_ready=True,
        geometric_directivity_ready=True,
        wave_source_ready=True,
        wave_boundary_ready=True,
        geometric_boundary_ready=True,
        environment_ready=True,
        receiver_ready=True,
        requested_observable_ready=True,
        observable_readiness=(
            ObservableReadiness(
                observable='complex_pressure',
                state='READY',
                reasons=(),
            ),
        ),
    )
    probe = AcousticSceneSnapshot.model_construct(
        schema_version=3,
        authority_version='3',
        compiler_id='htdt.acoustic_scene_snapshot',
        compiler_version='3',
        snapshot_id=f'acoustic-scene-snapshot:{_hash("placeholder")}',
        semantic_sha256=_hash('placeholder'),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        system_variant_id=None,
        system_variant_sha256=None,
        semantic_geometry_id=(
            f'semantic-acoustic-geometry:{_hash("semantic-geometry")}'
        ),
        semantic_geometry_sha256=_hash('semantic-geometry'),
        r120_compiled_geometry_id=(
            f'r120-compiled-geometry:{_hash("r120-geometry")}'
        ),
        r120_compiled_geometry_sha256=_hash('r120-geometry'),
        compiled_topology_sha256=_hash('topology'),
        geometric_tolerance_m=1.0e-6,
        approximation_error_bound_m=0.0,
        approximation_error_status='EXACT',
        maximum_dropped_feature_extent_m=0.0,
        acoustic_region_authority_ref=None,
        portal_authority_ref=None,
        boundary_termination_authority_ref=None,
        surface_boundary_configuration=(
            SurfaceBoundaryConfiguration(
                source_surface_id=SURFACE_ID,
                material_authority=_ref('surface-material'),
            ),
        ),
        material_boundary_configuration_sha256=_digest(
            [
                SurfaceBoundaryConfiguration(
                    source_surface_id=SURFACE_ID,
                    material_authority=_ref('surface-material'),
                ).model_dump(mode='json')
            ]
        ),
        treatment_boundary_bindings=(),
        wave_source_excitation_bindings=(),
        sources=(source,),
        receivers=(receiver,),
        environment=environment,
        valid_frequency_domain=None,
        valid_frequency_domain_authority_ref=None,
        requested_frequency_domain=FrequencyDomain(
            minimum_hz=40.0, maximum_hz=80.0
        ),
        requested_observables=('complex_pressure',),
        readiness=readiness,
        unresolved_conditions=(),
    )
    digest = _digest(probe.semantic_payload())
    payload = probe.model_dump(mode='python')
    payload['snapshot_id'] = f'acoustic-scene-snapshot:{digest}'
    payload['semantic_sha256'] = digest
    return AcousticSceneSnapshot.model_validate(payload)


def _revision(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='calibration-fixture',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='source-1',
                kind='speaker',
                name='Source',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='receiver-1-entity',
                kind='measurement_point',
                name='Receiver',
                position=Position3(x_m=2.5, y_m=2.5, z_m=1.0),
            ),
        ),
    )
    return repository, repository.save(
        document, parent_revision_id=None
    ).revision


def _spec(snapshot, **overrides):
    kwargs = dict(
        baseline_snapshot_id=snapshot.snapshot_id,
        baseline_snapshot_sha256=snapshot.semantic_sha256,
        solver_id='solver-1',
        solver_version='1',
        calibration_evidence=(
            CalibrationEvidenceRef(
                evidence_kind='campaign',
                evidence_id='campaign:dev',
                evidence_sha256=_hash('dev-campaign'),
            ),
        ),
        parameters=(
            CalibrationParameterDefinition(
                parameter_id='alpha',
                target_kind='boundary_surface_material',
                target_id=SURFACE_ID,
                quantity='absorption',
                unit='1',
                model_family='allpass_alpha',
                role='fitted',
                lower_bound=0.0,
                upper_bound=1.0,
            ),
            CalibrationParameterDefinition(
                parameter_id='temp',
                target_kind='environment',
                target_id='temperature_c',
                quantity='temperature_c',
                unit='degC',
                model_family='air',
                role='fixed',
                fixed_value=20.0,
            ),
        ),
        objective=CalibrationObjectiveSpec(
            observable='transfer_magnitude_db',
            frequency_domain=FrequencyDomain(
                minimum_hz=20.0, maximum_hz=200.0
            ),
        ),
        optimizer=CalibrationOptimizerSpec(max_evaluations=16),
        holdout_campaign_ref=ExactExternalAuthorityRef(
            authority_id='campaign:holdout',
            authority_version='1',
            semantic_hash_sha256=_hash('holdout-campaign'),
        ),
    )
    kwargs.update(overrides)
    return build_model_calibration_spec(**kwargs)


def test_targets_resolve_against_baseline(tmp_path: Path) -> None:
    _repo, revision = _revision(tmp_path)
    snapshot = _snapshot(revision)
    spec = _spec(snapshot)
    resolved = resolve_calibration_targets(spec, snapshot)
    by_id = {item.parameter_id: item for item in resolved}
    assert by_id['alpha'].resolved_identity == f'surface:{SURFACE_ID}'
    assert (
        by_id['alpha'].resolved_authority_ref
        is not None
    )
    assert by_id['temp'].resolved_identity == 'environment:temperature_c'
    assert by_id['temp'].resolved_authority_ref is not None


def test_target_resolution_fails_closed(tmp_path: Path) -> None:
    _repo, revision = _revision(tmp_path)
    snapshot = _snapshot(revision)
    bad_surface = _spec(
        snapshot,
        parameters=(
            CalibrationParameterDefinition(
                parameter_id='alpha',
                target_kind='boundary_surface_material',
                target_id='semantic-surface:' + _hash('ghost'),
                quantity='absorption',
                unit='1',
                model_family='allpass_alpha',
                role='fitted',
                lower_bound=0.0,
                upper_bound=1.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match='not bound'):
        resolve_calibration_targets(bad_surface, snapshot)

    bad_quantity = _spec(
        snapshot,
        parameters=(
            CalibrationParameterDefinition(
                parameter_id='alpha',
                target_kind='boundary_surface_material',
                target_id=SURFACE_ID,
                quantity='membrane_impedance_phase',
                unit='1',
                model_family='x',
                role='fitted',
                lower_bound=0.0,
                upper_bound=1.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match='illegal quantity'):
        resolve_calibration_targets(bad_quantity, snapshot)


def test_materialization_and_freeze_binding(tmp_path: Path) -> None:
    _repo, revision = _revision(tmp_path)
    snapshot = _snapshot(revision)
    spec = _spec(snapshot)
    from htdt.cad_model_calibration import run_model_calibration

    class _Evaluator:
        def evaluate(self, parameter_values):
            return (abs(parameter_values['alpha'] - 0.4),)

    result = run_model_calibration(spec, _Evaluator())
    model = materialize_calibrated_model(spec, result, snapshot)
    assert model.materialized_model_id.startswith(
        'materialized-calibrated-model:'
    )
    assert model.calibrated_model_sha256 == result.calibrated_model_sha256
    values = {item.parameter_id: item.value for item in model.overrides}
    assert values['alpha'] == pytest.approx(
        dict(result.fitted_values)['alpha']
    )
    assert values['temp'] == pytest.approx(20.0)

    freeze = freeze_calibrated_model(
        result,
        spec,
        normalization_policy_sha256=_hash('norm'),
        materialized_model=model,
    )
    assert freeze.materialized_model_ref is not None
    assert (
        freeze.materialized_model_ref.authority_id
        == model.materialized_model_id
    )


def test_repository_lifecycle_and_holdout_history(tmp_path: Path) -> None:
    scene_repo, revision = _revision(tmp_path)
    repository = CadModelCalibrationRepository(scene_repo)
    snapshot = _snapshot(revision)
    spec = _spec(snapshot)
    repository.save_spec(spec)
    assert repository.get_spec(spec.spec_id) == spec

    from htdt.cad_model_calibration import run_model_calibration

    class _Evaluator:
        def evaluate(self, parameter_values):
            return (abs(parameter_values['alpha'] - 0.4),)

    result = run_model_calibration(spec, _Evaluator())
    repository.save_result(result)
    assert repository.get_result(result.result_id) == result

    model = materialize_calibrated_model(spec, result, snapshot)
    repository.save_model(model)
    assert repository.get_model(model.materialized_model_id) == model

    freeze = freeze_calibrated_model(
        result,
        spec,
        normalization_policy_sha256=_hash('norm'),
        materialized_model=model,
    )
    repository.save_freeze(freeze)
    assert repository.get_freeze(freeze.freeze_id) == freeze

    # Record that the holdout campaign was already consumed for development;
    # the verdict must then derive 'reused' from history, not the caller.
    holdout_ref = spec.holdout_campaign_ref
    repository.record_evidence_consumption(
        campaign_ref=holdout_ref,
        consumption_kind='development',
        freeze_id=None,
    )
    record = repository.evaluate_persisted_holdout(
        freeze,
        spec,
        consumed_campaign_ref=holdout_ref,
    )
    assert record.verdict == 'holdout_reused_for_development'

    # A fresh repository (different db) sees clean history.
    scene_repo2, revision2 = _revision(tmp_path / 'fresh')
    _ = revision2
    repository2 = CadModelCalibrationRepository(scene_repo2)
    repository2.save_spec(spec)
    repository2.save_result(result)
    repository2.save_model(model)
    repository2.save_freeze(freeze)
    record2 = repository2.evaluate_persisted_holdout(
        freeze,
        spec,
        consumed_campaign_ref=holdout_ref,
    )
    assert record2.verdict == 'independent_holdout'

    # Holdout against an unfrozen model fails closed.
    scene_repo3, _ = _revision(tmp_path / 'unfrozen')
    repository3 = CadModelCalibrationRepository(scene_repo3)
    with pytest.raises(ValueError, match='unfrozen'):
        repository3.evaluate_persisted_holdout(
            freeze,
            spec,
            consumed_campaign_ref=holdout_ref,
        )
