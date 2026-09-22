from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from htdt.cad_directivity import (
    NORMALIZED_JSON_DIRECTIVITY_ADAPTER,
    DirectivityCoordinateConvention,
    DirectivityNormalization,
    DirectivitySample,
    build_directivity_dataset,
)
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    SensitivityReference,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_r110_source import (
    compile_r110_source_model,
    require_r110_source_capability,
)
from htdt.cad_r110_source_repository import CadR110SourceRepository
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
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-19T13:00:00+00:00'


def _save_equipment(repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _provenance(source_sha256: str, name: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='measured',
        source_name=name,
        source_version='2026-09-19',
        source_reference=f'{name}-fixture',
        source_sha256=source_sha256,
    )


def _domain() -> DirectivityDomain:
    return DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
        horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
        vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
    )


def _definition(
    *,
    definition_id: str,
    tier: str,
    source_asset_sha256: str,
    with_sensitivity: bool = True,
):
    provenance = _provenance(source_asset_sha256, definition_id)
    if tier == 'unknown':
        directivity = DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        )
    else:
        directivity = DirectivityCapability(
            tier=tier,
            data_format=(
                'cta2034_summary'
                if tier == 'polar_summary'
                else 'custom'
            ),
            provenance=provenance,
            data_asset_sha256=source_asset_sha256,
            valid_domain=_domain(),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='r110-test-grid-linear',
                implementation_version='1',
                provenance=provenance,
            ),
            coherent_phase=(tier == 'complex'),
            phase_reference=(
                'acoustic_reference_point/source-t0'
                if tier == 'complex'
                else None
            ),
        )
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.1, z_m=0.0),
        sensitivity=(
            SensitivityReference(
                level_db_spl=88.0,
                input_quantity='voltage_v_rms',
                input_value=2.83,
                distance_m=1.0,
                valid_frequency_domain=FrequencyDomain(
                    minimum_hz=500.0,
                    maximum_hz=1000.0,
                ),
                provenance=provenance,
            )
            if with_sensitivity
            else None
        ),
        directivity=directivity,
    )


def _dataset(definition, *, kind: str):
    provenance = definition.directivity.provenance
    samples = []
    for frequency_hz in (500.0, 1000.0):
        for horizontal_angle_deg in (-30.0, 0.0, 30.0):
            magnitude_db = 0.0 if horizontal_angle_deg == 0.0 else -6.0
            samples.append(
                DirectivitySample(
                    frequency_hz=frequency_hz,
                    horizontal_angle_deg=horizontal_angle_deg,
                    vertical_angle_deg=0.0,
                    magnitude_db=magnitude_db,
                    phase_deg=(
                        horizontal_angle_deg / 3.0
                        if kind == 'complex'
                        else None
                    ),
                )
            )
    return build_directivity_dataset(
        dataset_id=f'{definition.definition_id}-dataset',
        version='1',
        definition=definition,
        source_asset_sha256=definition.directivity.data_asset_sha256,
        source_format='custom',
        parser_id='r110-fixture-parser',
        parser_version='1',
        adapter_id='r110-fixture-adapter',
        adapter_version='1',
        evidence_kind='measured',
        source_provenance=provenance,
        kind=kind,
        coordinate_convention=DirectivityCoordinateConvention(
            angle_semantics='horizontal_vertical',
            horizontal_wrap='none',
        ),
        normalization=DirectivityNormalization(
            source_magnitude_unit='db',
            reference='on_axis_per_frequency',
        ),
        frequencies_hz=(500.0, 1000.0),
        horizontal_angles_deg=(-30.0, 0.0, 30.0),
        vertical_angles_deg=(0.0,),
        samples=tuple(samples),
        interpolation=definition.directivity.interpolation,
        phase_reference=(
            definition.directivity.phase_reference
            if kind == 'complex'
            else None
        ),
    )


def _persisted_directivity(definition_id: str, kind: str):
    """Build real imported source bytes, a bound definition and the parsed
    dataset so repository persistence can verify and replay the source."""
    samples = []
    for frequency_hz in (500.0, 1000.0):
        for horizontal_angle_deg in (-30.0, 0.0, 30.0):
            samples.append(
                {
                    'frequency_hz': frequency_hz,
                    'horizontal_angle_deg': horizontal_angle_deg,
                    'vertical_angle_deg': 0.0,
                    'magnitude': (
                        0.0 if horizontal_angle_deg == 0.0 else -6.0
                    ),
                    'phase_deg': (
                        horizontal_angle_deg / 3.0
                        if kind == 'complex'
                        else None
                    ),
                }
            )
    payload = {
        'schema': 'htdt.normalized-directivity.v1',
        'dataset_id': f'{definition_id}-dataset',
        'version': '1',
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': definition_id,
        'source_version': '2026-09-19',
        'source_reference': f'{definition_id}-fixture',
        'kind': kind,
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
        'phase_reference': (
            'acoustic_reference_point/source-t0'
            if kind == 'complex'
            else None
        ),
        'interpolation_method': 'linear',
        'interpolation_implementation': 'r110-test-grid-linear',
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-30.0, 0.0, 30.0],
        'vertical_angles_deg': [0.0],
        'samples': samples,
    }
    source_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    definition = _definition(
        definition_id=definition_id,
        tier=kind,
        source_asset_sha256=sha256(source_bytes).hexdigest(),
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


def _speaker(
    *,
    entity_id: str = 'fl',
    x_m: float = 1.0,
    aim: Direction3 | None = Direction3(x=1.0, y=0.0, z=0.0),
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name='Front left',
        speaker_role='FL',
        position=Position3(x_m=x_m, y_m=2.0, z_m=1.5),
        orientation=quaternion_from_euler_deg(
            yaw_deg=90.0,
            pitch_deg=0.0,
            roll_deg=0.0,
        ),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        aim_xyz=aim,
    )


def _baseline(scene_repository: SceneRepository, *, x_m: float = 1.0):
    document = SceneDocument(
        document_id='r110-source-fixture',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(_speaker(x_m=x_m),),
    )
    return scene_repository.save(
        document,
        parent_revision_id=None,
    ).revision


def _variant(baseline, definition):
    return build_system_variant(
        baseline=baseline,
        name=f'R110 {definition.definition_id}',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )


def _compile(baseline, definition, dataset=None):
    variant = _variant(baseline, definition)
    return compile_r110_source_model(
        scene_revision=baseline,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
        directivity_dataset=dataset,
    )


def test_magnitude_only_speaker_compiles_for_geometric_directivity_only(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='magnitude-speaker',
        tier='magnitude_only',
        source_asset_sha256='a' * 64,
    )
    dataset = _dataset(definition, kind='magnitude_only')

    model = _compile(baseline, definition, dataset)

    assert (
        model.use_case_states.geometric_directivity
        == 'SUPPORTED_FOR_GEOMETRIC_DIRECTIVITY'
    )
    assert model.use_case_states.complex_directivity == 'UNSUPPORTED'
    assert model.capability('magnitude_directivity').decision == 'SUPPORTED'
    assert model.capability('complex_directivity').decision == 'UNSUPPORTED'
    assert model.coherent_phase_available is False


def test_complex_directivity_requires_and_preserves_exact_phase_authority(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='complex-speaker',
        tier='complex',
        source_asset_sha256='b' * 64,
    )
    dataset = _dataset(definition, kind='complex')

    model = _compile(baseline, definition, dataset)

    assert (
        model.use_case_states.complex_directivity
        == 'SUPPORTED_FOR_COMPLEX_DIRECTIVITY'
    )
    assert model.capability('coherent_phase').decision == 'SUPPORTED'
    assert model.coherent_phase_available is True
    assert model.phase_reference == 'acoustic_reference_point/source-t0'
    assert model.directivity_dataset_sha256 == dataset.semantic_sha256


def test_unknown_directivity_is_unsupported(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='unknown-speaker',
        tier='unknown',
        source_asset_sha256='c' * 64,
    )

    model = _compile(baseline, definition)

    assert model.use_case_states.geometric_directivity == 'UNSUPPORTED'
    assert model.use_case_states.complex_directivity == 'UNSUPPORTED'
    assert model.capability('magnitude_directivity').decision == 'UNSUPPORTED'
    assert any(
        'directivity capability is unknown' in reason
        for reason in model.unsupported_reasons
    )


def test_polar_summary_is_not_promoted_to_full_angular_field(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='polar-summary-speaker',
        tier='polar_summary',
        source_asset_sha256='d' * 64,
    )

    model = _compile(baseline, definition)

    assert model.use_case_states.geometric_directivity == 'UNSUPPORTED'
    assert model.capability('magnitude_directivity').decision == 'UNSUPPORTED'
    assert {
        item.kind for item in model.approximation_metadata
    } >= {'polar_summary_not_expanded'}


def test_exact_source_pose_and_equipment_reference_point_are_compiled(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='pose-speaker',
        tier='magnitude_only',
        source_asset_sha256='e' * 64,
    )
    dataset = _dataset(definition, kind='magnitude_only')

    model = _compile(baseline, definition, dataset)

    assert model.source_entity_position == Position3(
        x_m=1.0,
        y_m=2.0,
        z_m=1.5,
    )
    assert model.source_acoustic_reference_world_position.x_m == pytest.approx(0.9)
    assert model.source_acoustic_reference_world_position.y_m == pytest.approx(2.0)
    assert model.source_acoustic_reference_world_position.z_m == pytest.approx(1.5)
    assert model.source_reference_axis_world == Direction3(x=1.0, y=0.0, z=0.0)
    assert model.source_reference_axis_authority == 'scene-entity-aim-xyz-v1'


def test_scene_system_variant_mismatch_is_rejected(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='scene-mismatch',
        tier='magnitude_only',
        source_asset_sha256='f' * 64,
    )
    variant = _variant(baseline, definition)
    changed_document = baseline.document.model_copy(
        update={'entities': (_speaker(x_m=1.25),)}
    )
    changed = scene_repository.save(
        changed_document,
        parent_revision_id=baseline.revision_id,
    ).revision

    with pytest.raises(
        ValueError,
        match='SceneRevision/SystemVariant revision mismatch',
    ):
        compile_r110_source_model(
            scene_revision=changed,
            system_variant=variant,
            source_entity_id='fl',
            equipment_definition=definition,
        )


def test_equipment_definition_mismatch_is_rejected(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    bound = _definition(
        definition_id='bound-equipment',
        tier='magnitude_only',
        source_asset_sha256='1' * 64,
    )
    other = _definition(
        definition_id='other-equipment',
        tier='magnitude_only',
        source_asset_sha256='2' * 64,
    )
    variant = _variant(baseline, bound)

    with pytest.raises(
        ValueError,
        match='does not match exact SystemVariant binding',
    ):
        compile_r110_source_model(
            scene_revision=baseline,
            system_variant=variant,
            source_entity_id='fl',
            equipment_definition=other,
        )


def test_directivity_dataset_mismatch_is_rejected(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    bound = _definition(
        definition_id='bound-dataset-equipment',
        tier='magnitude_only',
        source_asset_sha256='3' * 64,
    )
    other = _definition(
        definition_id='other-dataset-equipment',
        tier='magnitude_only',
        source_asset_sha256='4' * 64,
    )
    wrong_dataset = _dataset(other, kind='magnitude_only')
    variant = _variant(baseline, bound)

    with pytest.raises(
        ValueError,
        match='EquipmentDefinition hash mismatch',
    ):
        compile_r110_source_model(
            scene_revision=baseline,
            system_variant=variant,
            source_entity_id='fl',
            equipment_definition=bound,
            directivity_dataset=wrong_dataset,
        )


def test_magnitude_only_coherent_request_is_rejected(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='no-coherent-upgrade',
        tier='magnitude_only',
        source_asset_sha256='5' * 64,
    )
    dataset = _dataset(definition, kind='magnitude_only')
    model = _compile(baseline, definition, dataset)

    with pytest.raises(ValueError, match='coherent_phase is UNSUPPORTED'):
        require_r110_source_capability(model, 'coherent_phase')


def test_complex_directivity_does_not_create_wave_excitation_normalization(
    tmp_path: Path,
) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='complex-no-wave-transfer',
        tier='complex',
        source_asset_sha256='6' * 64,
        with_sensitivity=True,
    )
    dataset = _dataset(definition, kind='complex')
    model = _compile(baseline, definition, dataset)

    assert (
        model.use_case_states.complex_directivity
        == 'SUPPORTED_FOR_COMPLEX_DIRECTIVITY'
    )
    assert model.capability('electrical_sensitivity_reference').decision == 'SUPPORTED'
    assert model.electrical_sensitivity_reference is not None
    assert model.electrical_sensitivity_reference.input_quantity == 'voltage_v_rms'
    assert model.electrical_sensitivity_reference.input_value == pytest.approx(2.83)
    assert (
        model.capability('acoustic_wave_excitation_normalization').decision
        == 'BLOCKED'
    )
    assert (
        model.use_case_states.wave_excitation
        == 'BLOCKED_FOR_WAVE_EXCITATION'
    )
    assert model.normalization.wave_excitation_normalization == 'UNKNOWN'
    assert model.normalization.electrical_to_acoustic_transfer_authority is None


def test_compile_is_deterministic(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    definition = _definition(
        definition_id='deterministic-speaker',
        tier='complex',
        source_asset_sha256='7' * 64,
    )
    dataset = _dataset(definition, kind='complex')
    variant = _variant(baseline, definition)

    first = compile_r110_source_model(
        scene_revision=baseline,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
        directivity_dataset=dataset,
    )
    second = compile_r110_source_model(
        scene_revision=baseline,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
        directivity_dataset=dataset,
    )

    assert first == second
    assert first.semantic_sha256 == second.semantic_sha256


def test_save_reopen_reresolves_all_exact_authorities(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = _baseline(scene_repository)
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    source_bytes, definition, dataset = _persisted_directivity(
        'persisted-r110-speaker',
        'complex',
    )
    _save_equipment(equipment_repository, definition)
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename='persisted-r110-speaker.normalized.json',
        media_type='application/json',
        declared_schema='htdt.normalized-directivity.v1',
    )
    variant = _variant(baseline, definition)
    variant_repository.save_variant(variant)

    repository = CadR110SourceRepository(
        scene_repository,
        variant_repository,
        equipment_repository,
        directivity_repository,
    )
    model = repository.compile_for_variant_source(
        system_variant_id=variant.variant_id,
        source_entity_id='fl',
        directivity_dataset_sha256=dataset.semantic_sha256,
    )
    repository.save_model(model)

    reopened_scene = SceneRepository(tmp_path / 'cad.sqlite3')
    reopened_variant = CadSystemVariantRepository(reopened_scene)
    reopened_equipment = CadEquipmentRepository(
        reopened_scene,
        reopened_variant,
    )
    reopened_directivity = CadDirectivityRepository(
        reopened_scene,
        reopened_equipment,
    )
    reopened_repository = CadR110SourceRepository(
        reopened_scene,
        reopened_variant,
        reopened_equipment,
        reopened_directivity,
    )
    reopened = reopened_repository.get_model(model.semantic_sha256)

    assert reopened == model
    assert reopened is not None
    assert reopened.scene_revision_id == baseline.revision_id
    assert reopened.system_variant_sha256 == variant.variant_sha256
    assert reopened.equipment_definition_sha256 == definition.semantic_sha256
    assert reopened.directivity_dataset_sha256 == dataset.semantic_sha256
