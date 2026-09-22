from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from htdt.cad_equipment import (
    AngleDomain,
    ClearanceMetadata,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    EquipmentUncertainty,
    FrequencyDomain,
    InterpolationProvenance,
    MountingMetadata,
    PortMetadata,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
    evaluate_equipment_capability,
)
from htdt.cad_equipment_catalog import EquipmentCatalogSnapshot
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
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    ProposedEntitySpec,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-19T12:00:00+00:00'


def _provenance(
    evidence_kind: str,
    source_name: str,
    source_hash: str,
) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version='2026-09-19',
        source_reference='fixture-section-1',
        source_sha256=source_hash,
    )


def _domain() -> DirectivityDomain:
    return DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=80.0, maximum_hz=18000.0),
        horizontal=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
        vertical=AngleDomain(minimum_deg=-45.0, maximum_deg=45.0),
    )


def _manufacturer_complex():
    manufacturer = _provenance('manufacturer', 'Example Audio datasheet', 'a' * 64)
    measured = _provenance('measured', 'Example Audio AES69 export', 'b' * 64)
    directivity = DirectivityCapability(
        tier='complex',
        data_format='sofa_aes69',
        provenance=measured,
        data_asset_sha256='c' * 64,
        valid_domain=_domain(),
        interpolation=InterpolationProvenance(
            method='spherical',
            implementation='htdt-r110-compatible-reference',
            implementation_version='1',
            provenance=measured,
        ),
        coherent_phase=True,
        phase_reference='acoustic_reference_point / source t0',
    )
    return build_equipment_definition(
        definition_id='example-audio-monitor-x',
        version='datasheet-2026-09',
        identity_kind='manufacturer',
        manufacturer='Example Audio',
        model='Monitor X',
        provenance=(manufacturer, measured),
        cabinet_envelope_m=Size3(x_m=0.22, y_m=0.31, z_m=0.38),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.04, z_m=0.08),
        mounting=MountingMetadata(mounting_modes=('stand', 'shelf')),
        port=PortMetadata(port_type='rear', minimum_clearance_m=0.15),
        clearance=ClearanceMetadata(rear_m=0.15, side_m=0.05),
        sensitivity=SensitivityReference(
            level_db_spl=88.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=10000.0,
            ),
            provenance=manufacturer,
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
            declared_headroom_db=6.0,
            headroom_reference_level_db_spl=105.0,
            provenance=manufacturer,
        ),
        directivity=directivity,
        uncertainty=(
            EquipmentUncertainty(
                quantity='sensitivity',
                unit='dB',
                model='bounded',
                lower=-1.0,
                upper=1.0,
                provenance=manufacturer,
            ),
        ),
    )


def _magnitude_only():
    manufacturer = _provenance('manufacturer', 'Example Audio CLF export', 'd' * 64)
    return build_equipment_definition(
        definition_id='example-audio-magnitude-only',
        version='clf-1',
        identity_kind='manufacturer',
        manufacturer='Example Audio',
        model='Magnitude Only',
        provenance=(manufacturer,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.34),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='clf',
            provenance=manufacturer,
            data_asset_sha256='e' * 64,
            valid_domain=_domain(),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='source-declared-grid-linear',
                implementation_version='1',
                provenance=manufacturer,
            ),
        ),
    )


def _user_defined_unknown():
    user = _provenance('user_defined', 'HTDT user equipment record', 'f' * 64)
    return build_equipment_definition(
        definition_id='user-speaker-1',
        version='1',
        identity_kind='user_defined',
        user_label='DIY surround',
        provenance=(user,),
        cabinet_envelope_m=Size3(x_m=0.18, y_m=0.20, z_m=0.28),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=user,
        ),
    )


def _speaker(entity_id: str, role: str, x_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=role,
        speaker_role=role,
        position=Position3(x_m=x_m, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
    )


def _save_equipment(
    repository: CadEquipmentRepository,
    definition,
) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='equipment-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _baseline(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='o100c-fixture',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(_speaker('fl', 'FL', 1.0),),
    )
    baseline = scene_repository.save(
        document,
        parent_revision_id=None,
    ).revision
    return scene_repository, baseline


def test_representative_manufacturer_and_user_definitions_persist_deterministically(
    tmp_path: Path,
) -> None:
    scene_repository, _baseline_revision = _baseline(tmp_path)
    repository = CadEquipmentRepository(scene_repository)
    manufacturer = _manufacturer_complex()
    user_defined = _user_defined_unknown()

    _save_equipment(repository, manufacturer)
    _save_equipment(repository, user_defined)

    reopened = CadEquipmentRepository(scene_repository)
    persisted_manufacturer = reopened.get_definition(
        manufacturer.definition_id,
        manufacturer.version,
    )
    persisted_user = reopened.get_definition(
        user_defined.definition_id,
        user_defined.version,
    )

    assert persisted_manufacturer == manufacturer
    assert persisted_user == user_defined
    assert persisted_manufacturer.semantic_sha256 == manufacturer.semantic_sha256
    assert persisted_user.semantic_sha256 == user_defined.semantic_sha256

    changed = build_equipment_definition(
        definition_id=manufacturer.definition_id,
        version=manufacturer.version,
        identity_kind='manufacturer',
        manufacturer=manufacturer.manufacturer,
        model=manufacturer.model,
        provenance=manufacturer.provenance,
        cabinet_envelope_m=Size3(x_m=0.23, y_m=0.31, z_m=0.38),
        acoustic_reference_point_m=manufacturer.acoustic_reference_point_m,
        directivity=manufacturer.directivity,
    )
    with pytest.raises(
        ValueError,
        match='id/version already exists with different semantics',
    ):
        reopened.save_definition(changed)


def test_unknown_magnitude_and_complex_capability_tiers_fail_closed() -> None:
    unknown = _user_defined_unknown()
    magnitude = _magnitude_only()
    complex_definition = _manufacturer_complex()

    assert evaluate_equipment_capability(
        unknown,
        'directivity_summary',
    ).decision == 'UNSUPPORTED'
    assert evaluate_equipment_capability(
        magnitude,
        'directivity_magnitude',
        frequency_hz=1000.0,
        horizontal_angle_deg=30.0,
        vertical_angle_deg=0.0,
    ).decision == 'SUPPORTED'
    assert evaluate_equipment_capability(
        magnitude,
        'directivity_complex',
    ).decision == 'UNSUPPORTED'
    assert evaluate_equipment_capability(
        magnitude,
        'coherent_phase',
    ).decision == 'UNSUPPORTED'
    assert evaluate_equipment_capability(
        magnitude,
        'directivity_magnitude',
        frequency_hz=30.0,
    ).decision == 'UNSUPPORTED'
    assert evaluate_equipment_capability(
        complex_definition,
        'directivity_complex',
        frequency_hz=1000.0,
    ).decision == 'SUPPORTED'
    assert evaluate_equipment_capability(
        complex_definition,
        'coherent_phase',
    ).decision == 'SUPPORTED'


def test_magnitude_only_directivity_cannot_fabricate_coherent_phase() -> None:
    provenance = _provenance('manufacturer', 'Magnitude fixture', '1' * 64)
    with pytest.raises(
        ValidationError,
        match='cannot claim coherent phase',
    ):
        DirectivityCapability(
            tier='magnitude_only',
            data_format='polar_table',
            provenance=provenance,
            data_asset_sha256='2' * 64,
            valid_domain=_domain(),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='phase-rejection-fixture',
                implementation_version='1',
                provenance=provenance,
            ),
            coherent_phase=True,
            phase_reference='fabricated-reference',
        )


def test_system_variant_binds_exact_persisted_equipment_definition(
    tmp_path: Path,
) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    definition = _magnitude_only()
    _save_equipment(equipment_repository, definition)

    proposed = ProposedEntitySpec(
        spec_id='proposal-sl',
        entity=_speaker('sl', 'SL', 0.5),
        role_binding_id='SL',
    )
    binding = EquipmentBindingRef(
        entity_id='sl',
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )
    variant = build_system_variant(
        baseline=baseline,
        name='Add SL with exact equipment',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(proposed,),
        equipment_bindings=(binding,),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)

    reopened_variant = variant_repository.get_variant(variant.variant_id)
    assert reopened_variant == variant
    assert reopened_variant.equipment_bindings == (binding,)

    resolved = equipment_repository.resolve_variant_bindings(variant.variant_id)
    assert len(resolved) == 1
    assert resolved[0].entity_id == 'sl'
    assert resolved[0].definition == definition
    assert resolved[0].variant_sha256 == variant.variant_sha256
    assert equipment_repository.definition_for_variant_entity(
        variant.variant_id,
        'sl',
    ) == definition


def test_variant_identity_changes_with_equipment_choice(tmp_path: Path) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    first = _magnitude_only()
    second = _manufacturer_complex()
    proposal = ProposedEntitySpec(
        spec_id='proposal-sl',
        entity=_speaker('sl', 'SL', 0.5),
        role_binding_id='SL',
    )
    common = dict(
        baseline=baseline,
        name='Equipment alternative',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(proposal,),
        created_at_utc=NOW,
    )
    first_variant = build_system_variant(
        **common,
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='sl',
                equipment_definition_id=first.definition_id,
                equipment_definition_version=first.version,
                equipment_definition_sha256=first.semantic_sha256,
            ),
        ),
    )
    second_variant = build_system_variant(
        **common,
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='sl',
                equipment_definition_id=second.definition_id,
                equipment_definition_version=second.version,
                equipment_definition_sha256=second.semantic_sha256,
            ),
        ),
    )

    assert first_variant.variant_sha256 != second_variant.variant_sha256


def test_variant_persistence_rejects_unpersisted_equipment_binding(
    tmp_path: Path,
) -> None:
    scene_repository, baseline = _baseline(tmp_path)
    variant_repository = CadSystemVariantRepository(scene_repository)
    CadEquipmentRepository(scene_repository, variant_repository)

    proposal = ProposedEntitySpec(
        spec_id='proposal-sl',
        entity=_speaker('sl', 'SL', 0.5),
        role_binding_id='SL',
    )
    variant = build_system_variant(
        baseline=baseline,
        name='Dangling equipment ref',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(proposal,),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='sl',
                equipment_definition_id='missing-equipment',
                equipment_definition_version='1',
                equipment_definition_sha256='9' * 64,
            ),
        ),
        created_at_utc=NOW,
    )

    with pytest.raises(ValueError, match='unpersisted definition'):
        variant_repository.save_variant(variant)
    assert variant_repository.get_variant(variant.variant_id) is None


def _equipment_bound_variant(tmp_path: Path):
    scene_repository, baseline = _baseline(tmp_path)
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    definition = _magnitude_only()
    _save_equipment(equipment_repository, definition)
    variant = build_system_variant(
        baseline=baseline,
        name='Add SL with exact equipment',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker('sl', 'SL', 0.5),
                role_binding_id='SL',
            ),
        ),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='sl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    return scene_repository, variant_repository, definition, variant


def test_variant_read_rejects_dangling_equipment_definition(
    tmp_path: Path,
) -> None:
    scene_repository, variant_repository, definition, variant = (
        _equipment_bound_variant(tmp_path)
    )
    assert variant_repository.get_variant(variant.variant_id) == variant

    # Direct tamper simulates an authority that cannot be re-resolved after
    # reopen. SQLite foreign keys are intentionally not enabled on this raw
    # connection.
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'DELETE FROM cad_equipment_definitions WHERE semantic_sha256=?',
            (definition.semantic_sha256,),
        )

    with pytest.raises(ValueError, match='unpersisted definition'):
        variant_repository.get_variant(variant.variant_id)
    with pytest.raises(ValueError, match='unpersisted definition'):
        variant_repository.list_variants('o100c-fixture')


def test_variant_read_rejects_equipment_definition_identity_mismatch(
    tmp_path: Path,
) -> None:
    scene_repository, variant_repository, definition, variant = (
        _equipment_bound_variant(tmp_path)
    )
    assert variant_repository.get_variant(variant.variant_id) == variant

    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'UPDATE cad_equipment_definitions SET version=? '
            'WHERE semantic_sha256=?',
            ('rewritten-version', definition.semantic_sha256),
        )

    with pytest.raises(
        ValueError, match='definition identity mismatch'
    ):
        variant_repository.get_variant(variant.variant_id)
    with pytest.raises(
        ValueError, match='definition identity mismatch'
    ):
        variant_repository.list_variants('o100c-fixture')


def test_equipment_catalog_snapshot_is_deterministic_exact_reference_surface(
    tmp_path: Path,
) -> None:
    scene_repository, _baseline_revision = _baseline(tmp_path)
    repository = CadEquipmentRepository(scene_repository)
    first = _manufacturer_complex()
    second = _user_defined_unknown()

    _save_equipment(repository, second)
    _save_equipment(repository, first)

    snapshot = repository.catalog_snapshot()
    reopened = EquipmentCatalogSnapshot.model_validate_json(
        snapshot.canonical_bytes()
    )

    assert reopened == snapshot
    assert {
        (
            item.definition_id,
            item.version,
            item.semantic_sha256,
        )
        for item in snapshot.definitions
    } == {
        (
            first.definition_id,
            first.version,
            first.semantic_sha256,
        ),
        (
            second.definition_id,
            second.version,
            second.semantic_sha256,
        ),
    }
    assert snapshot.canonical_bytes() == repository.catalog_snapshot().canonical_bytes()
