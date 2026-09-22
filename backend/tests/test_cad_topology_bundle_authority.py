from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_coverage import (
    build_coverage_evaluation_scenario,
    coverage_objective_definitions,
    coverage_objective_vector,
    evaluate_coverage,
)
from htdt.cad_coverage_repository import CadCoverageRepository
from htdt.cad_direct_level import (
    DirectLevelFrequencyBand,
    ReferenceInputCondition,
    SeatPopulation,
    build_playback_excitation_scenario,
    direct_level_objective_definitions,
    direct_level_objective_vector,
    evaluate_direct_level,
)
from htdt.cad_direct_level_repository import CadDirectLevelRepository
from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)
from htdt.cad_standards import build_user_standards_profile
from htdt.cad_standards_repository import CadStandardsRepository
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.cad_topology_comparison import (
    ExactAuthorityRef,
    ObjectiveEvidenceBinding,
    ResolvedObjectiveAuthority,
    VariantEvaluationBundle,
    build_system_topology_comparison_spec,
    build_variant_evaluation_bundle,
    compared_system_variant,
    coverage_evaluation_ref,
    direct_level_evaluation_ref,
    evaluate_topology_comparison,
)
from htdt.cad_topology_comparison_repository import (
    CadTopologyComparisonRepository,
)
from htdt.optimization_objectives import ObjectiveMetric, ObjectiveVector


NOW = '2026-09-22T10:00:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


DOCUMENT_ID = 'o100d-bundle-authority-fixture'


def _provenance(source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='O100D bundle authority fixture',
        source_version='2026-09-22',
        source_reference='issue-385-bundle-authority-fixture',
        source_sha256=source_hash,
    )


def _authority():
    source_payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': 'bundle-authority-speaker-directivity',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'user_defined',
        'source_name': 'O100D bundle authority fixture',
        'source_version': '2026-09-22',
        'source_reference': 'issue-385-bundle-authority-fixture',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'normalized_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
            'reference_level_db': None,
            'conversion_version': 'pressure-amplitude-db20-v1',
        },
        'phase_reference': None,
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-60.0, 0.0, 60.0],
        'vertical_angles_deg': [0.0],
        'samples': [
            {
                'frequency_hz': frequency_hz,
                'horizontal_angle_deg': horizontal_angle_deg,
                'vertical_angle_deg': 0.0,
                'magnitude': (
                    0.0
                    if horizontal_angle_deg == 0.0
                    else (-6.0 if frequency_hz == 500.0 else -12.0)
                ),
                'phase_deg': None,
            }
            for frequency_hz in (500.0, 1000.0)
            for horizontal_angle_deg in (-60.0, 0.0, 60.0)
        ],
    }
    source_bytes = json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    source_hash = sha256(source_bytes).hexdigest()
    provenance = _provenance(source_hash)
    domain = FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0)
    directivity_domain = DirectivityDomain(
        frequency=domain,
        horizontal=AngleDomain(minimum_deg=-60.0, maximum_deg=60.0),
        vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
    )
    definition = build_equipment_definition(
        definition_id='bundle-authority-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='bundle-authority-speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.20),
        sensitivity=SensitivityReference(
            level_db_spl=88.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=10000.0,
            ),
            weighting=None,
            provenance=provenance,
        ),
        spl_capability=SplCapability(
            continuous_db_spl=105.0,
            peak_db_spl=111.0,
            reference_distance_m=1.0,
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=10000.0,
            ),
            continuous_duration_s=60.0,
            peak_duration_s=0.1,
            provenance=provenance,
        ),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256=source_hash,
            valid_domain=directivity_domain,
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='htdt-grid-linear',
                implementation_version='1',
                provenance=provenance,
            ),
        ),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(source_bytes, definition)
    return source_bytes, definition, dataset


def _speaker() -> SceneEntity:
    return SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=0.0, y_m=0.0, z_m=1.0),
        orientation=quaternion_from_euler_deg(
            yaw_deg=0.0,
            pitch_deg=0.0,
            roll_deg=0.0,
        ),
        size_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _seat(entity_id: str, *, x_m: float, y_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
        acoustic_reference_offset_m=Offset3(),
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
            entities=(
                _speaker(),
                _seat('seat-on', x_m=0.20, y_m=2.0),
                _seat('seat-off', x_m=2.20, y_m=2.0),
            ),
        ),
        parent_revision_id=None,
    ).revision

    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    source_bytes, definition, dataset = _authority()
    _save_equipment(equipment_repository, definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename='bundle-authority-speaker.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )

    binding = EquipmentBindingRef(
        entity_id='speaker-fl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )
    variants = tuple(
        build_system_variant(
            baseline=revision,
            name=name,
            role_bindings=(
                ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ),
            proposed_entities=(),
            equipment_bindings=(binding,),
            created_at_utc=f'2026-09-22T10:{index:02d}:00+00:00',
        )
        for index, name in enumerate(('Current authority', 'Proposed authority'))
    )
    for variant in variants:
        variant_repository.save_variant(variant)

    standards_repository = CadStandardsRepository(
        scene_repository,
        variant_repository,
    )
    profile = build_user_standards_profile(
        profile_id='issue-385-bundle-authority-profile',
        version='1',
        name='Issue 385 bundle authority fixture',
        criteria=(),
    )
    standards_repository.save_profile(profile)

    coverage_repository = CadCoverageRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    direct_level_repository = CadDirectLevelRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
    )

    seats = SeatPopulation(
        population_id='bundle-authority-seats',
        seat_entity_ids=('seat-on', 'seat-off'),
    )
    coverage_scenario = build_coverage_evaluation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        receiver_population=seats,
        directivity_dataset=dataset,
        equipment_definition=definition,
        evaluation_frequencies_hz=(500.0, 1000.0),
        frequency_aggregation_semantics='worst_over_requested_frequencies',
        coverage_threshold_db=-6.0,
    )
    coverage_repository.save_scenario(coverage_scenario)
    direct_level_scenario = build_playback_excitation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        reference_input=ReferenceInputCondition(
            input_quantity='voltage_v_rms',
            input_value=2.83,
        ),
        target_spl_db_spl=75.0,
        target_reference_condition='single-channel direct target at each seat',
        continuous_reference_duration_s=60.0,
        peak_reference_duration_s=0.1,
        frequency_band=DirectLevelFrequencyBand(low_hz=100.0, high_hz=10000.0),
        weighting='unweighted',
        receiver_population=seats,
    )
    direct_level_repository.save_scenario(direct_level_scenario)

    coverage_evaluations = {}
    direct_level_evaluations = {}
    for variant in variants:
        coverage_evaluation = evaluate_coverage(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            directivity_dataset=dataset,
            scenario=coverage_scenario,
        )
        coverage_repository.save_evaluation(coverage_evaluation)
        coverage_evaluations[variant.variant_id] = coverage_evaluation
        direct_evaluation = evaluate_direct_level(
            revision=revision,
            variant=variant,
            equipment_definition=definition,
            scenario=direct_level_scenario,
        )
        direct_level_repository.save_evaluation(direct_evaluation)
        direct_level_evaluations[variant.variant_id] = direct_evaluation

    repository = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        coverage_repository=coverage_repository,
        direct_level_repository=direct_level_repository,
    )
    return (
        repository,
        revision,
        profile,
        variants,
        coverage_scenario,
        direct_level_scenario,
        coverage_evaluations,
        direct_level_evaluations,
    )


def _spec(profile, revision, variants, coverage_scenario, direct_level_scenario):
    return build_system_topology_comparison_spec(
        name='bundle authority comparison',
        baseline=revision,
        candidate_variants=(
            compared_system_variant(
                variants[0],
                role='current',
                comparison_label='current',
            ),
            compared_system_variant(
                variants[1],
                role='proposed',
                comparison_label='proposed',
            ),
        ),
        required_objectives=(
            coverage_objective_definitions(coverage_scenario)[0],
            direct_level_objective_definitions(direct_level_scenario)[0],
        ),
        optional_objectives=(),
        standards_profile=profile,
    )


def _bundle(
    spec,
    variant,
    coverage_evaluation,
    direct_level_evaluation,
    *,
    coverage_metric: ObjectiveMetric | None = None,
    direct_metric: ObjectiveMetric | None = None,
) -> VariantEvaluationBundle:
    coverage_ref = coverage_evaluation_ref(coverage_evaluation)
    direct_ref = direct_level_evaluation_ref(direct_level_evaluation)
    canonical_coverage = coverage_objective_vector(coverage_evaluation)
    canonical_direct = direct_level_objective_vector(direct_level_evaluation)
    coverage_objective_id = coverage_objective_definitions(
        coverage_evaluation.scenario
    )[0].objective_id
    direct_objective_id = direct_level_objective_definitions(
        direct_level_evaluation.scenario
    )[0].objective_id
    if coverage_metric is None:
        coverage_metric = canonical_coverage.metric(coverage_objective_id)
    if direct_metric is None:
        direct_metric = canonical_direct.metric(direct_objective_id)
    evidence = (
        ObjectiveEvidenceBinding(
            objective_id=coverage_metric.objective_id,
            source_authority_kind=coverage_ref.authority_kind,
            source_authority_id=coverage_ref.authority_id,
            source_semantic_sha256=coverage_ref.semantic_sha256,
        ),
        ObjectiveEvidenceBinding(
            objective_id=direct_metric.objective_id,
            source_authority_kind=direct_ref.authority_kind,
            source_authority_id=direct_ref.authority_id,
            source_semantic_sha256=direct_ref.semantic_sha256,
        ),
    )
    return build_variant_evaluation_bundle(
        spec=spec,
        variant=variant,
        objective_vector=ObjectiveVector(
            candidate_id=variant.variant_id,
            metrics=(coverage_metric, direct_metric),
        ),
        objective_evidence=evidence,
        coverage_evaluation=coverage_ref,
        direct_level_evaluation=direct_ref,
    )


def _insert_bundle(database: Path, bundle: VariantEvaluationBundle) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            INSERT INTO cad_topology_comparison_bundles(
                bundle_id, bundle_sha256, comparison_id,
                variant_id, variant_sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                bundle.bundle_id,
                bundle.bundle_sha256,
                bundle.comparison_id,
                bundle.variant_id,
                bundle.variant_sha256,
                bundle.model_dump_json(),
            ),
        )


def test_real_evaluation_refs_require_canonical_metric_values(
    tmp_path: Path,
) -> None:
    (
        repository,
        revision,
        profile,
        variants,
        coverage_scenario,
        direct_level_scenario,
        coverage_evaluations,
        direct_level_evaluations,
    ) = _fixture(tmp_path)
    spec = _spec(
        profile,
        revision,
        variants,
        coverage_scenario,
        direct_level_scenario,
    )
    repository.save_spec(spec)

    bundles = tuple(
        _bundle(
            spec,
            variant,
            coverage_evaluations[variant.variant_id],
            direct_level_evaluations[variant.variant_id],
        )
        for variant in variants
    )
    for bundle in bundles:
        assert repository.save_bundle(bundle) == bundle
        assert repository.get_bundle(bundle.bundle_id) == bundle

    evaluation = evaluate_topology_comparison(
        spec=spec,
        bundles=bundles,
        created_at_utc='2026-09-22T11:00:00+00:00',
    )
    repository.save_evaluation(evaluation)
    assert all(item.state == 'ELIGIBLE' for item in evaluation.eligibility)
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation

    # A real exact ref does not authorize an arbitrary caller-supplied value:
    # a self-hash-valid bundle whose coverage objective value differs from the
    # canonical CoverageEvaluation output is rejected at save.
    canonical = coverage_objective_vector(
        coverage_evaluations[variants[1].variant_id]
    )
    coverage_objective_id = coverage_objective_definitions(
        coverage_scenario
    )[0].objective_id
    canonical_metric = canonical.metric(coverage_objective_id)
    assert canonical_metric.value is not None
    value = float(canonical_metric.value)
    tampered_metric = canonical_metric.model_copy(
        update={'value': value - 0.05 if value > 0.5 else value + 0.05}
    )
    tampered = _bundle(
        spec,
        variants[1],
        coverage_evaluations[variants[1].variant_id],
        direct_level_evaluations[variants[1].variant_id],
        coverage_metric=tampered_metric,
    )
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.save_bundle(tampered)

    # The same tampering is rejected for the direct-level bound metric.
    canonical_direct = direct_level_objective_vector(
        direct_level_evaluations[variants[1].variant_id]
    )
    direct_objective_id = direct_level_objective_definitions(
        direct_level_scenario
    )[0].objective_id
    direct_metric = canonical_direct.metric(direct_objective_id)
    assert direct_metric.value is not None
    tampered = _bundle(
        spec,
        variants[1],
        coverage_evaluations[variants[1].variant_id],
        direct_level_evaluations[variants[1].variant_id],
        direct_metric=direct_metric.model_copy(
            update={'value': direct_metric.value + 1.0}
        ),
    )
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.save_bundle(tampered)


def test_metric_definition_unit_and_direction_cannot_drift_from_authority(
    tmp_path: Path,
) -> None:
    (
        repository,
        revision,
        profile,
        variants,
        coverage_scenario,
        direct_level_scenario,
        coverage_evaluations,
        direct_level_evaluations,
    ) = _fixture(tmp_path)
    spec = _spec(
        profile,
        revision,
        variants,
        coverage_scenario,
        direct_level_scenario,
    )
    repository.save_spec(spec)

    variant = variants[1]
    coverage_evaluation = coverage_evaluations[variant.variant_id]
    direct_evaluation = direct_level_evaluations[variant.variant_id]
    canonical_metric = coverage_objective_vector(coverage_evaluation).metric(
        coverage_objective_definitions(coverage_scenario)[0].objective_id
    )

    # Unit drift inside the metric definition is rejected even though the
    # numeric value stays canonical.
    wrong_unit_definition = canonical_metric.definition.model_copy(
        update={'unit': 'dB'}
    )
    wrong_unit_metric = canonical_metric.model_copy(
        update={'unit': 'dB', 'definition': wrong_unit_definition}
    )
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.save_bundle(
            _bundle(
                spec,
                variant,
                coverage_evaluation,
                direct_evaluation,
                coverage_metric=wrong_unit_metric,
            )
        )

    # Direction drift is rejected the same way.
    wrong_direction_definition = canonical_metric.definition.model_copy(
        update={'direction': 'minimize'}
    )
    wrong_direction_metric = canonical_metric.model_copy(
        update={
            'direction': 'minimize',
            'definition': wrong_direction_definition,
        }
    )
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.save_bundle(
            _bundle(
                spec,
                variant,
                coverage_evaluation,
                direct_evaluation,
                coverage_metric=wrong_direction_metric,
            )
        )

    # A binding cannot attribute a metric to a real ref that never produced
    # that objective: the canonical source has exactly one metric per
    # objective id and unknown ids fail closed.
    coverage_ref = coverage_evaluation_ref(coverage_evaluation)
    direct_ref = direct_level_evaluation_ref(direct_evaluation)
    direct_metric = direct_level_objective_vector(direct_evaluation).metric(
        direct_level_objective_definitions(direct_level_scenario)[0].objective_id
    )
    misplaced = build_variant_evaluation_bundle(
        spec=spec,
        variant=variant,
        objective_vector=ObjectiveVector(
            candidate_id=variant.variant_id,
            metrics=(direct_metric,),
        ),
        objective_evidence=(
            ObjectiveEvidenceBinding(
                objective_id=direct_metric.objective_id,
                source_authority_kind=coverage_ref.authority_kind,
                source_authority_id=coverage_ref.authority_id,
                source_semantic_sha256=coverage_ref.semantic_sha256,
            ),
        ),
        coverage_evaluation=coverage_ref,
        direct_level_evaluation=direct_ref,
    )
    with pytest.raises(ValueError, match='objective evidence is not canonical'):
        repository.save_bundle(misplaced)


def test_read_side_replays_metric_binding_validation(tmp_path: Path) -> None:
    (
        repository,
        revision,
        profile,
        variants,
        coverage_scenario,
        direct_level_scenario,
        coverage_evaluations,
        direct_level_evaluations,
    ) = _fixture(tmp_path)
    spec = _spec(
        profile,
        revision,
        variants,
        coverage_scenario,
        direct_level_scenario,
    )
    repository.save_spec(spec)
    bundle = _bundle(
        spec,
        variants[0],
        coverage_evaluations[variants[0].variant_id],
        direct_level_evaluations[variants[0].variant_id],
    )
    repository.save_bundle(bundle)
    assert repository.get_bundle(bundle.bundle_id) == bundle

    # Simulate a self-hash-valid payload inserted outside the repository: the
    # read path re-derives canonical metrics and fails closed.
    canonical_metric = coverage_objective_vector(
        coverage_evaluations[variants[0].variant_id]
    ).metric(coverage_objective_definitions(coverage_scenario)[0].objective_id)
    assert canonical_metric.value is not None
    value = float(canonical_metric.value)
    forged = _bundle(
        spec,
        variants[0],
        coverage_evaluations[variants[0].variant_id],
        direct_level_evaluations[variants[0].variant_id],
        coverage_metric=canonical_metric.model_copy(
            update={'value': value - 0.25 if value > 0.5 else value + 0.25}
        ),
    )
    _insert_bundle(repository.path, forged)
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.get_bundle(forged.bundle_id)
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.list_bundles(spec.comparison_id)


def _external_fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
            entities=(_speaker(),),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    variants = tuple(
        build_system_variant(
            baseline=revision,
            name=name,
            role_bindings=(
                ChannelRoleBinding(role_id='FL', display_name='Front Left'),
            ),
            proposed_entities=(),
            created_at_utc=f'2026-09-22T10:{index:02d}:00+00:00',
        )
        for index, name in enumerate(
            (
                'External current',
                'External proposed',
                'External evidence-only proposed',
            )
        )
    )
    for variant in variants:
        variant_repository.save_variant(variant)
    standards_repository = CadStandardsRepository(
        scene_repository,
        variant_repository,
    )
    profile = build_user_standards_profile(
        profile_id='issue-385-external-authority-profile',
        version='1',
        name='Issue 385 external authority fixture',
        criteria=(),
    )
    standards_repository.save_profile(profile)
    return scene_repository, revision, variant_repository, standards_repository, profile, variants


def test_extension_refs_require_canonical_objective_metrics(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        revision,
        variant_repository,
        standards_repository,
        profile,
        variants,
    ) = _external_fixture(tmp_path)

    definition = direct_level_objective_definitions(
        build_playback_excitation_scenario(
            source_entity_id='speaker-fl',
            channel_role_id='FL',
            reference_input=ReferenceInputCondition(
                input_quantity='voltage_v_rms',
                input_value=2.83,
            ),
            target_spl_db_spl=75.0,
            target_reference_condition='fixture target',
            continuous_reference_duration_s=60.0,
            peak_reference_duration_s=0.1,
            frequency_band=DirectLevelFrequencyBand(
                low_hz=100.0,
                high_hz=10000.0,
            ),
            weighting='unweighted',
            receiver_population=SeatPopulation(
                population_id='external-seats',
                seat_entity_ids=('seat-on',),
            ),
        )
    )[0]

    spec = build_system_topology_comparison_spec(
        name='external authority comparison',
        baseline=revision,
        candidate_variants=(
            compared_system_variant(
                variants[0],
                role='current',
                comparison_label='current',
            ),
            compared_system_variant(
                variants[1],
                role='proposed',
                comparison_label='proposed',
            ),
            compared_system_variant(
                variants[2],
                role='proposed',
                comparison_label='evidence-only proposed',
            ),
        ),
        required_objectives=(definition,),
        optional_objectives=(),
        standards_profile=profile,
    )

    def _ref(variant) -> ExactAuthorityRef:
        return ExactAuthorityRef(
            authority_kind='fixture_prediction',
            authority_id=f'fixture-prediction:{variant.variant_id}',
            authority_version='fixture-v1',
            semantic_sha256=sha256(
                f'prediction:{variant.variant_id}'.encode('utf-8')
            ).hexdigest(),
            evaluator_id='fixture-evaluator',
            evaluator_version='1',
            model_id=definition.comparison_model_id,
            model_version=definition.comparison_model_version,
            fidelity='fixture-fidelity',
        )

    def _canonical_metric(variant, value: float) -> ObjectiveMetric:
        return ObjectiveMetric(
            objective_id=definition.objective_id,
            value=value,
            unit=definition.unit,
            direction=definition.direction,
            state='available',
            definition=definition,
        )

    resolved: dict[str, ExactAuthorityRef | ResolvedObjectiveAuthority] = {}
    for index, variant in enumerate(variants):
        ref = _ref(variant)
        resolved[ref.authority_id] = ResolvedObjectiveAuthority(
            ref=ref,
            objective_vector=ObjectiveVector(
                candidate_id=variant.variant_id,
                metrics=(_canonical_metric(variant, 90.0 + index),),
            ),
        )

    repository = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        external_resolvers={'fixture_prediction': resolved.get},
    )
    repository.save_spec(spec)

    def _external_bundle(
        variant,
        metric: ObjectiveMetric,
        *,
        ref: ExactAuthorityRef | None = None,
    ) -> VariantEvaluationBundle:
        source = ref if ref is not None else _ref(variant)
        return build_variant_evaluation_bundle(
            spec=spec,
            variant=variant,
            objective_vector=ObjectiveVector(
                candidate_id=variant.variant_id,
                metrics=(metric,),
            ),
            objective_evidence=(
                ObjectiveEvidenceBinding(
                    objective_id=metric.objective_id,
                    source_authority_kind=source.authority_kind,
                    source_authority_id=source.authority_id,
                    source_semantic_sha256=source.semantic_sha256,
                ),
            ),
            fr_prediction_refs=(source,),
        )

    bundles = tuple(
        _external_bundle(variant, _canonical_metric(variant, 90.0 + index))
        for index, variant in enumerate(variants[:2])
    )
    for bundle in bundles:
        assert repository.save_bundle(bundle) == bundle
        assert repository.get_bundle(bundle.bundle_id) == bundle

    # Changing the reported value while keeping the real exact ref fails.
    tampered = _external_bundle(
        variants[0],
        _canonical_metric(variants[0], 91.5),
    )
    with pytest.raises(ValueError, match='objective metric mismatch'):
        repository.save_bundle(tampered)

    # An opaque ref alone is not sufficient evidence for a caller number.
    resolved[_ref(variants[0]).authority_id] = _ref(variants[0])
    with pytest.raises(ValueError, match='objective evidence is not canonical'):
        repository.save_bundle(bundles[0])

    # A resolved vector scoped to a different candidate fails closed.
    resolved[_ref(variants[0]).authority_id] = ResolvedObjectiveAuthority(
        ref=_ref(variants[0]),
        objective_vector=ObjectiveVector(
            candidate_id=variants[1].variant_id,
            metrics=(_canonical_metric(variants[0], 90.0),),
        ),
    )
    with pytest.raises(ValueError, match='ObjectiveVector variant mismatch'):
        repository.save_bundle(bundles[0])

    # Refs that bind no objective metrics still resolve as opaque evidence.
    resolved['fixture-note'] = ExactAuthorityRef(
        authority_kind='fixture_evidence',
        authority_id='fixture-note',
        authority_version='fixture-v1',
        semantic_sha256=sha256(b'fixture-note').hexdigest(),
    )
    evidence_ref = resolved['fixture-note']
    assert isinstance(evidence_ref, ExactAuthorityRef)
    note_resolvers = dict(resolved)
    repository = CadTopologyComparisonRepository(
        scene_repository=scene_repository,
        system_variant_repository=variant_repository,
        standards_repository=standards_repository,
        external_resolvers={
            'fixture_prediction': resolved.get,
            'fixture_evidence': note_resolvers.get,
        },
    )
    evidence_only = build_variant_evaluation_bundle(
        spec=spec,
        variant=variants[2],
        objective_vector=ObjectiveVector(
            candidate_id=variants[2].variant_id,
            metrics=(_canonical_metric(variants[2], 92.0),),
        ),
        objective_evidence=(
            ObjectiveEvidenceBinding(
                objective_id=definition.objective_id,
                source_authority_kind=_ref(variants[2]).authority_kind,
                source_authority_id=_ref(variants[2]).authority_id,
                source_semantic_sha256=_ref(variants[2]).semantic_sha256,
            ),
        ),
        fr_prediction_refs=(_ref(variants[2]),),
        installation_evidence_refs=(evidence_ref,),
    )
    assert repository.save_bundle(evidence_only) == evidence_only
    assert repository.get_bundle(evidence_only.bundle_id) == evidence_only
