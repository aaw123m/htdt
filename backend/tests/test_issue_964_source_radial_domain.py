"""Issue #964: radial / far-field validity domain for source prediction.

Directivity evidence and the point-source direct-level model must declare
the source-to-receiver distance domain over which they are valid; seats or
consumers outside the declared domain fail closed to ``unsupported`` instead
of silently extrapolating a false-precision level.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_direct_level import (
    DirectLevelFrequencyBand,
    DistanceLevelAuthority,
    ReferenceInputCondition,
    SeatPopulation,
    build_playback_excitation_scenario,
    evaluate_direct_level,
)
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    RadialDomain,
    SensitivityReference,
    SplCapability,
    build_equipment_definition,
)
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
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
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository


NOW = '2026-09-26T00:00:00+00:00'


def _provenance(source_name: str, source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=source_name,
        source_version='2026-09-26',
        source_reference='issue-964-fixture',
        source_sha256=source_hash,
    )


def test_radial_domain_bounds_and_containment() -> None:
    domain = RadialDomain(minimum_m=0.5, maximum_m=4.0)
    assert not domain.contains(0.4)
    assert domain.contains(0.5)
    assert domain.contains(2.0)
    assert domain.contains(4.0)
    assert not domain.contains(4.1)

    far_field = RadialDomain(minimum_m=1.0)
    assert far_field.contains(100.0)
    assert not far_field.contains(0.9)

    with pytest.raises(ValueError):
        RadialDomain(minimum_m=2.0, maximum_m=1.0)
    with pytest.raises(ValueError):
        RadialDomain(minimum_m=0.0)


def _definition(
    *,
    definition_id: str,
    radial_domain: RadialDomain | None = None,
):
    provenance = _provenance(definition_id, 'f' * 64)
    domain = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        sensitivity=SensitivityReference(
            level_db_spl=88.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=domain,
            weighting=None,
            provenance=provenance,
        ),
        spl_capability=SplCapability(
            continuous_db_spl=105.0,
            peak_db_spl=111.0,
            reference_distance_m=1.0,
            valid_frequency_domain=domain,
            continuous_duration_s=60.0,
            peak_duration_s=0.1,
            provenance=provenance,
        ),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='custom',
            provenance=provenance,
            data_asset_sha256='e' * 64,
            valid_domain=DirectivityDomain(
                frequency=domain,
                horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
                vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
            ),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='issue-964-fixture',
                implementation_version='1',
                provenance=provenance,
            ),
            radial_domain=radial_domain,
        ),
    )


def test_unknown_directivity_cannot_claim_radial_domain() -> None:
    provenance = _provenance('unknown', 'd' * 64)
    with pytest.raises(ValueError):
        DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
            radial_domain=RadialDomain(minimum_m=1.0),
        )


def test_equipment_digest_stable_when_radial_domain_absent() -> None:
    """A capability without the domain dumps exactly as before the field
    existed — persisted equipment keeps its semantic identity."""
    definition = _definition(definition_id='no-domain-speaker')
    payload = definition.semantic_payload()
    assert 'radial_domain' not in payload['directivity']


def test_equipment_records_declared_radial_domain() -> None:
    domain = RadialDomain(minimum_m=0.7, maximum_m=6.0)
    definition = _definition(
        definition_id='domain-speaker',
        radial_domain=domain,
    )
    payload = definition.semantic_payload()
    assert payload['directivity']['radial_domain'] == domain.model_dump(
        mode='json'
    )


def _r110_fixture(tmp_path: Path, *, radial_domain: RadialDomain | None):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    speaker = SceneEntity(
        entity_id='fl',
        kind='speaker',
        name='Front left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=2.0, z_m=1.5),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
    )
    document = SceneDocument(
        document_id='issue-964-r110',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(speaker,),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    definition = _definition(
        definition_id='r110-radial-speaker',
        radial_domain=radial_domain,
    )
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    for evidence in build_equipment_manual_evidence(
        definition, actor='issue-964-fixture', recorded_at_utc=NOW
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)
    variant = build_system_variant(
        baseline=revision,
        name='R110 radial',
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
    return compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='fl',
        equipment_definition=definition,
    )


def test_r110_compiled_model_propagates_radial_domain(tmp_path: Path) -> None:
    domain = RadialDomain(minimum_m=0.8, maximum_m=8.0)
    model = _r110_fixture(tmp_path, radial_domain=domain)
    assert model.valid_radial_domain == domain
    assert model.radial_domain_authority == 'equipment_definition'


def test_r110_compiled_model_marks_domain_unavailable(tmp_path: Path) -> None:
    model = _r110_fixture(tmp_path, radial_domain=None)
    assert model.valid_radial_domain is None
    assert model.radial_domain_authority is None
    # Digest stability for pre-existing models: absent fields stay out of the
    # semantic payload.
    assert 'valid_radial_domain' not in model.semantic_payload()
    assert 'radial_domain_authority' not in model.semantic_payload()


def _level_fixture(tmp_path: Path, *, radial_domain: RadialDomain | None):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    speaker = SceneEntity(
        entity_id='speaker-fl',
        kind='speaker',
        name='Front Left',
        speaker_role='FL',
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
    )
    near = SceneEntity(
        entity_id='seat-near',
        kind='seat',
        name='seat-near',
        position=Position3(x_m=1.0, y_m=3.0, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(z_m=0.5),
    )
    far = SceneEntity(
        entity_id='seat-far',
        kind='seat',
        name='seat-far',
        position=Position3(x_m=1.0, y_m=5.0, z_m=0.5),
        size_m=Size3(x_m=0.6, y_m=0.8, z_m=1.0),
        acoustic_reference_offset_m=Offset3(z_m=0.5),
    )
    document = SceneDocument(
        document_id='issue-964-level',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(speaker, near, far),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    definition = _definition(
        definition_id='radial-level-speaker',
    )
    for evidence in build_equipment_manual_evidence(
        definition, actor='issue-964-fixture', recorded_at_utc=NOW
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)
    variant = build_system_variant(
        baseline=revision,
        name='Radial level',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=(
            EquipmentBindingRef(
                entity_id='speaker-fl',
                equipment_definition_id=definition.definition_id,
                equipment_definition_version=definition.version,
                equipment_definition_sha256=definition.semantic_sha256,
            ),
        ),
        created_at_utc=NOW,
    )
    scenario = build_playback_excitation_scenario(
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
        receiver_population=SeatPopulation(
            population_id='two-seat-fixture',
            seat_entity_ids=('seat-near', 'seat-far'),
        ),
        radial_domain=radial_domain,
    )
    return evaluate_direct_level(
        revision=revision,
        variant=variant,
        equipment_definition=definition,
        scenario=scenario,
    )


def test_direct_level_outside_radial_domain_fails_closed(
    tmp_path: Path,
) -> None:
    # seat-near is 2 m from the source, seat-far is 4 m.
    evaluation = _level_fixture(
        tmp_path,
        radial_domain=RadialDomain(minimum_m=1.0, maximum_m=3.0),
    )
    near, far = evaluation.seat_results
    assert near.distance_m == pytest.approx(2.0)
    assert near.direct_level.state == 'available'
    assert far.distance_m == pytest.approx(4.0)
    assert far.direct_level.state == 'unsupported'
    assert 'radial validity domain' in (far.direct_level.reason or '')
    assert far.target_margin.state == 'unsupported'
    assert far.continuous_headroom.state == 'unsupported'
    assert far.peak_headroom.state == 'unsupported'


def test_direct_level_inside_radial_domain_stays_available(
    tmp_path: Path,
) -> None:
    evaluation = _level_fixture(
        tmp_path,
        radial_domain=RadialDomain(minimum_m=0.5, maximum_m=10.0),
    )
    for seat in evaluation.seat_results:
        assert seat.direct_level.state == 'available'
        assert seat.continuous_headroom.state == 'available'


def test_direct_level_scenario_digest_stable_without_domain() -> None:
    """A scenario without a declared domain keeps the pre-#964 identity."""
    scenario = build_playback_excitation_scenario(
        source_entity_id='speaker-fl',
        channel_role_id='FL',
        reference_input=ReferenceInputCondition(
            input_quantity='voltage_v_rms',
            input_value=2.83,
        ),
        target_spl_db_spl=75.0,
        target_reference_condition='condition',
        continuous_reference_duration_s=60.0,
        peak_reference_duration_s=0.1,
        frequency_band=DirectLevelFrequencyBand(low_hz=100.0, high_hz=10000.0),
        weighting='unweighted',
        receiver_population=SeatPopulation(
            population_id='p', seat_entity_ids=('s1',)
        ),
    )
    payload = scenario.semantic_payload()
    assert 'radial_domain' not in payload['distance_authority']
    assert scenario.distance_authority == DistanceLevelAuthority()
