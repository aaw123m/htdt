"""Backend tests for the authority-expansion batch:

- #476 equipment binding semantics + R110 reconciliation
- #505 LayoutProfile + CurrentSystemTopology
- #540 speaker installation/mounting/port context
- #544 frequency-resolved electrical load
- #569 installed equipment instance authority
- #608 shared library upgrade workflow
- #646 line-level gain structure
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_amplifier_headroom import (
    AmplifierChannelCountCondition,
    AmplifierLoadDomain,
    AuthorityRef,
    ElectricalValue,
    build_amplifier_output_capability,
)
from htdt.cad_amplifier_headroom_repository import (
    CadAmplifierHeadroomRepository,
)
from htdt.cad_direct_level import DirectLevelFrequencyBand
from htdt.cad_equipment import (
    ClearanceMetadata,
    DirectivityCapability,
    EquipmentDataProvenance,
    FrequencyDomain,
    MountingMetadata,
    PortMetadata,
    build_equipment_definition,
)
from htdt.cad_equipment_binding import (
    build_equipment_binding_semantics,
    reconcile_speaker_binding,
)
from htdt.cad_equipment_binding_repository import CadEquipmentBindingRepository
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_instance import (
    InstalledDefinitionRef,
    InstalledEquipmentInstance,
    _digest,
    build_installed_definition_binding,
    build_installed_equipment_instance,
    installed_instance_from_capture_identity,
)
from htdt.cad_equipment_instance_repository import (
    CadInstalledEquipmentRepository,
)
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_gain_structure import (
    GainStructureOperatingState,
    LineLevelValue,
    build_gain_structure_scenario,
    build_line_level_stage_capability,
    dbfs_to_dbv,
    dbv_to_dbfs,
    evaluate_gain_structure,
    vrms_to_dbv,
)
from htdt.cad_gain_structure_repository import CadGainStructureRepository
from htdt.cad_installation_context import (
    MeasuredClearances,
    build_installation_context,
    evaluate_installation_context,
    installation_constraint_results,
    installation_preflight_violations,
)
from htdt.cad_installation_context_repository import (
    CadInstallationContextRepository,
)
from htdt.cad_layout_profile import (
    LayoutRole,
    TopologyBindingItem,
    build_current_system_topology,
    build_layout_profile,
)
from htdt.cad_layout_profile_repository import (
    CadCurrentTopologyRepository,
    CadLayoutProfileRepository,
)
from htdt.cad_library_upgrade import (
    adoption_required_fields,
    build_upgrade_adoption,
    diff_equipment_definitions,
    rebase_equipment_bindings,
)
from htdt.cad_library_upgrade_repository import CadLibraryUpgradeRepository
from htdt.cad_r110_source import compile_r110_source_model
from htdt.cad_r110_source_repository import CadR110SourceRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_speaker_impedance import (
    ImpedanceSample,
    build_amplifier_electrical_limit,
    build_speaker_impedance_authority,
    evaluate_frequency_resolved_load,
    minimum_impedance_out_of_domain,
)
from htdt.cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    build_system_variant,
)
from htdt.cad_system_variant_repository import CadSystemVariantRepository
from htdt.system_expansion_workflow import (
    ProposalSpeakerInput,
    SystemExpansionWorkflowService,
)


NOW = '2026-09-20T00:00:00+00:00'
DOCUMENT_ID = 'authority-expansion-fixture'
DOMAIN = FrequencyDomain(minimum_hz=100.0, maximum_hz=10000.0)


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-09-20',
        source_reference='authority-expansion-fixture',
        source_sha256=digit * 64,
    )


def _equipment(
    definition_id: str = 'speaker-a',
    digit: str = '1',
    version: str = '1',
    envelope: Size3 | None = None,
    reference: Offset3 | None = None,
    mounting: MountingMetadata | None = None,
    port: PortMetadata | None = None,
    clearance: ClearanceMetadata | None = None,
):
    provenance = _provenance(definition_id, digit)
    return build_equipment_definition(
        definition_id=definition_id,
        version=version,
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=envelope or Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=reference or Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
        mounting=mounting,
        port=port,
        clearance=clearance,
    )


def _speaker(
    entity_id: str = 'speaker-fl',
    role: str = 'FL',
    position: Position3 | None = None,
    size: Size3 | None = None,
    offset: Offset3 | None = None,
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name='Front Left',
        speaker_role=role,
        position=position or Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        size_m=size or Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_offset_m=offset,
    )


def _save_equipment(repository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='authority-expansion-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _repositories(tmp_path: Path, entities=()):
    database = tmp_path / 'cad.sqlite3'
    scene_repository = SceneRepository(database)
    revision = scene_repository.save(
        SceneDocument(
            document_id=DOCUMENT_ID,
            schema_version=2,
            room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
            entities=entities or (_speaker(),),
        ),
        parent_revision_id=None,
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository,
        variant_repository,
    )
    return (
        scene_repository,
        revision,
        variant_repository,
        equipment_repository,
    )


def _persist_variant(variant_repository, revision, definition, name='v1'):
    variant = build_system_variant(
        baseline=revision,
        name=name,
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
    variant_repository.save_variant(variant)
    return variant


# ---------------------------------------------------------------------------
# #476 — speaker equipment binding semantics
# ---------------------------------------------------------------------------


def test_binding_semantics_reconcile_and_r110(tmp_path: Path) -> None:
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(
            tmp_path,
            entities=(
                _speaker(offset=Offset3(x_m=0.05, y_m=0.0, z_m=0.0)),
            ),
        )
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    variant = _persist_variant(variant_repository, revision, definition)
    entity = revision.document.entity('speaker-fl')

    # Without an explicit binding record a stale scene offset fails closed.
    with pytest.raises(ValueError, match='acoustic reference'):
        compile_r110_source_model(
            scene_revision=revision,
            system_variant=variant,
            source_entity_id='speaker-fl',
            equipment_definition=definition,
        )

    binding_repo = CadEquipmentBindingRepository(
        scene_repository,
        equipment_repository,
    )
    semantics = build_equipment_binding_semantics(
        binding_id='bind-fl-1',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=definition,
        body_geometry_authority='equipment_nominal',
        acoustic_reference_authority='equipment_derived',
        provenance=(_provenance('binding', '2'),),
        created_at_utc=NOW,
    )
    binding_repo.save_binding(semantics)
    assert (
        binding_repo.get_binding_for_entity(DOCUMENT_ID, 'speaker-fl')
        == semantics
    )

    # Explicit equipment-derived authority: the stale scene offset no longer
    # blocks compilation, and the binding is pinned in the compiled model.
    compiled = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
        binding_semantics=semantics,
    )
    assert (
        compiled.equipment_binding_semantics_sha256
        == semantics.semantic_sha256
    )

    # A scene-explicit override still fails closed — never silently replaced.
    scene_owned = build_equipment_binding_semantics(
        binding_id='bind-fl-2',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=definition,
        body_geometry_authority='observed_asbuilt',
        acoustic_reference_authority='scene_explicit',
        provenance=(_provenance('binding', '3'),),
        created_at_utc=NOW,
    )
    binding_repo.save_binding(scene_owned)
    with pytest.raises(ValueError, match='scene-explicit'):
        compile_r110_source_model(
            scene_revision=revision,
            system_variant=variant,
            source_entity_id='speaker-fl',
            equipment_definition=definition,
            binding_semantics=scene_owned,
        )

    # Reconcile: equipment envelope/reference are adopted; the placement
    # anchor (position) is never rewritten.
    updated = reconcile_speaker_binding(
        document=revision.document,
        entity_id='speaker-fl',
        equipment_definition=definition,
        semantics=semantics,
    )
    updated_entity = updated.entity('speaker-fl')
    assert updated_entity.size_m == definition.cabinet_envelope_m
    assert (
        updated_entity.acoustic_reference_offset_m
        == definition.acoustic_reference_point_m
    )
    assert updated_entity.position == entity.position
    assert updated_entity.orientation == entity.orientation


# ---------------------------------------------------------------------------
# #505 — LayoutProfile + CurrentSystemTopology
# ---------------------------------------------------------------------------


def _profile() -> object:
    return build_layout_profile(
        profile_id='theater-51',
        version='1',
        name='5.1 Reference',
        roles=(
            LayoutRole(role_id='FL', display_name='Front Left', paired_role_id='FR'),
            LayoutRole(role_id='FR', display_name='Front Right', paired_role_id='FL'),
            LayoutRole(role_id='C', display_name='Center'),
        ),
        channel_order=('FL', 'C', 'FR'),
        provenance=(_provenance('profile', '4'),),
    )


def test_layout_profile_and_current_topology(tmp_path: Path) -> None:
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    profile = _profile()
    profile_repo = CadLayoutProfileRepository(scene_repository)
    profile_repo.save_profile(profile)
    assert (
        profile_repo.get_profile_by_hash(profile.semantic_sha256) == profile
    )

    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    topology_repo = CadCurrentTopologyRepository(
        scene_repository,
        profile_repo,
        equipment_repository=equipment_repository,
    )
    topology = build_current_system_topology(
        document=revision.document,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        layout_profile=profile,
        bindings=(
            TopologyBindingItem(
                entity_id='speaker-fl',
                role_id='FL',
                equipment=AuthorityRef(
                    authority_id=definition.definition_id,
                    version=definition.version,
                    semantic_sha256=definition.semantic_sha256,
                ),
            ),
        ),
        provenance=(_provenance('topology', '5'),),
        created_at_utc=NOW,
    )
    topology_repo.save_topology(topology)
    assert topology_repo.current_topology(DOCUMENT_ID) == topology

    # Coverage fails closed: an undeclared role cannot be bound.
    with pytest.raises(ValueError, match='not declared'):
        build_current_system_topology(
            document=revision.document,
            scene_revision_id=revision.revision_id,
            scene_content_hash=revision.content_hash,
            layout_profile=profile,
            bindings=(
                TopologyBindingItem(
                    entity_id='speaker-fl',
                    role_id='SL',
                    equipment=AuthorityRef(
                        authority_id=definition.definition_id,
                        version=definition.version,
                        semantic_sha256=definition.semantic_sha256,
                    ),
                ),
            ),
            provenance=(_provenance('topology', '5'),),
            created_at_utc=NOW,
        )


def test_topology_proposal_stamps_layout_profile(tmp_path: Path) -> None:
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    profile = _profile()
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    service = SystemExpansionWorkflowService(scene_repository, DOCUMENT_ID)
    result = service.create_topology_proposal(
        proposal_name='add center',
        speakers=(
            ProposalSpeakerInput(
                role_id='C',
                equipment_sha256=definition.semantic_sha256,
                zone_name='front wall',
                min_x_m=2.5,
                max_x_m=3.5,
                min_y_m=0.2,
                max_y_m=0.5,
                min_z_m=0.9,
                max_z_m=1.1,
                step_m=0.5,
            ),
        ),
        layout_profile=profile,
    )
    template = variant_repository.get_variant(result.template_variant_id)
    bindings = {
        item.role_id: item for item in template.role_bindings
    }
    assert bindings['C'].layout_profile_id == 'theater-51'
    assert bindings['C'].layout_profile_version == '1'
    assert bindings['FL'].layout_profile_id == 'theater-51'
    assert bindings['C'].display_name == 'Center'

    # A role outside the declared profile vocabulary fails closed.
    with pytest.raises(ValueError, match='LayoutProfile'):
        service.create_topology_proposal(
            proposal_name='bad role',
            speakers=(
                ProposalSpeakerInput(
                    role_id='TOP',
                    equipment_sha256=definition.semantic_sha256,
                    zone_name='ceiling',
                    min_x_m=2.5,
                    max_x_m=3.5,
                    min_y_m=0.2,
                    max_y_m=0.5,
                    min_z_m=0.9,
                    max_z_m=1.1,
                    step_m=0.5,
                ),
            ),
            layout_profile=profile,
        )


# ---------------------------------------------------------------------------
# #540 — speaker mounting/port installation context
# ---------------------------------------------------------------------------


def test_installation_context_evaluation(tmp_path: Path) -> None:
    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment(
        mounting=MountingMetadata(mounting_modes=('free_standing',)),
        port=PortMetadata(port_type='rear', minimum_clearance_m=0.4),
        clearance=ClearanceMetadata(rear_m=0.4),
    )
    _save_equipment(equipment_repository, definition)
    context_repo = CadInstallationContextRepository(
        scene_repository,
        equipment_repository,
    )
    context = build_installation_context(
        context_id='ctx-fl-1',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=definition,
        selected_mounting_mode='free_standing',
        provenance=(_provenance('install', '6'),),
        created_at_utc=NOW,
    )
    context_repo.save_context(context)

    entity = revision.document.entity('speaker-fl')
    evaluation = evaluate_installation_context(
        document=revision.document,
        entity=entity,
        equipment_definition=definition,
        context=context,
    )
    # Entity sits 1.0 m from the -y wall and ~4.875 m from the +y wall;
    # a 0.4 m rear clearance passes from scene geometry alone.
    states = {check.check: check.state for check in evaluation.checks}
    assert states['mounting_mode'] == 'PASS'
    assert states['clearance_rear'] == 'PASS'
    assert states['port_clearance'] == 'PASS'
    assert evaluation.mounting_compatible is True
    assert not installation_preflight_violations(evaluation)

    # Unsupported mounting mode fails with an exact reason in the shared
    # constraint-result channel.
    bad_context = build_installation_context(
        context_id='ctx-fl-2',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=definition,
        selected_mounting_mode='in_wall',
        provenance=(_provenance('install', '7'),),
        created_at_utc=NOW,
    )
    bad = evaluate_installation_context(
        document=revision.document,
        entity=entity,
        equipment_definition=definition,
        context=bad_context,
    )
    assert bad.mounting_compatible is False
    assert installation_preflight_violations(bad)
    results = installation_constraint_results(bad)
    assert all(item.kind == 'equipment_installation' for item in results)
    assert any(
        item.reason_code == 'equipment_installation.failed'
        for item in results
    )

    # Measured evidence overrides scene geometry: a measured 0.2 m rear
    # clearance fails the 0.4 m requirement explicitly.
    measured_context = build_installation_context(
        context_id='ctx-fl-3',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_definition=definition,
        selected_mounting_mode='free_standing',
        measured_clearances=MeasuredClearances(rear_m=0.2),
        provenance=(_provenance('install', '8'),),
        created_at_utc=NOW,
    )
    measured_eval = evaluate_installation_context(
        document=revision.document,
        entity=entity,
        equipment_definition=definition,
        context=measured_context,
    )
    rear = next(
        check for check in measured_eval.checks
        if check.check == 'clearance_rear'
    )
    assert rear.state == 'FAIL'
    assert rear.actual_m == pytest.approx(0.2)


def test_installation_context_via_r110(tmp_path: Path) -> None:
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    definition = _equipment(
        mounting=MountingMetadata(mounting_modes=('free_standing',)),
    )
    _save_equipment(equipment_repository, definition)
    variant = _persist_variant(variant_repository, revision, definition)
    context_repo = CadInstallationContextRepository(
        scene_repository,
        equipment_repository,
    )
    context = context_repo.save_context(
        build_installation_context(
            context_id='ctx-fl-1',
            document_id=DOCUMENT_ID,
            entity_id='speaker-fl',
            equipment_definition=definition,
            selected_mounting_mode='free_standing',
            directivity_applicability='unknown',
            provenance=(_provenance('install', '9'),),
            created_at_utc=NOW,
        )
    )
    compiled = compile_r110_source_model(
        scene_revision=revision,
        system_variant=variant,
        source_entity_id='speaker-fl',
        equipment_definition=definition,
        installation_context=context,
    )
    assert compiled.installation_context_sha256 == context.semantic_sha256
    # Context is known and geometry checks pass — the recorded capability is
    # the truthful one, not an overclaim of acoustic modeling.
    assert compiled.installation_capability == (
        'context_known_geometry_checked'
    )

    # Repository compile-for-variant picks the persisted context up.
    source_repo = CadR110SourceRepository(scene_repository)
    recompiled = source_repo.compile_for_variant_source(
        system_variant_id=variant.variant_id,
        source_entity_id='speaker-fl',
    )
    assert recompiled == compiled
    saved = source_repo.save_model(compiled)
    assert source_repo.get_model(saved.semantic_sha256) == compiled


# ---------------------------------------------------------------------------
# #544 — frequency-resolved electrical load
# ---------------------------------------------------------------------------


def _amplifier():
    provenance = _provenance('amp-a', 'b')
    return build_amplifier_output_capability(
        capability_id='amp-a',
        version='1',
        identity_kind='user_defined',
        user_label='amp-a',
        output_id='amp-a-out',
        provenance=(provenance,),
        supported_load=AmplifierLoadDomain(
            reference_load_ohm=8.0,
            minimum_load_ohm=4.0,
            maximum_load_ohm=16.0,
        ),
        clipping_reference_definition='rated sine clipping at 1 kHz',
        valid_frequency_band=DOMAIN,
        weighting='none',
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=2,
            shared_supply_evidence=True,
            condition_description='stereo shared supply',
        ),
        continuous_capability=ElectricalValue(
            quantity='voltage_v_rms',
            value=28.0,
        ),
        continuous_duration_s=60.0,
    )


def test_frequency_resolved_load(tmp_path: Path) -> None:
    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    amplifier = _amplifier()

    impedance = build_speaker_impedance_authority(
        impedance_id='imp-a',
        version='1',
        equipment_definition=definition,
        tier='complex_curve',
        nominal_impedance_ohm=8.0,
        minimum_impedance_ohm=4.0,
        minimum_frequency_hz=1000.0,
        samples=(
            ImpedanceSample(frequency_hz=100.0, real_ohm=8.0, imag_ohm=0.0),
            ImpedanceSample(
                frequency_hz=1000.0,
                magnitude_ohm=4.0,
                phase_deg=-30.0,
            ),
            ImpedanceSample(
                frequency_hz=10000.0, real_ohm=12.0, imag_ohm=4.0
            ),
        ),
        interpolation='linear',
        valid_frequency_domain=DOMAIN,
        provenance=_provenance('imp-a', 'c'),
    )
    limit = build_amplifier_electrical_limit(
        limit_id='limit-a',
        version='1',
        amplifier_capability=amplifier,
        channel_count_condition=AmplifierChannelCountCondition(
            simultaneous_channel_count=2,
            shared_supply_evidence=True,
            condition_description='stereo shared supply',
        ),
        valid_frequency_domain=DOMAIN,
        provenance=_provenance('limit-a', 'd'),
        rms_current_ceiling_a=8.0,
        minimum_magnitude_load_ohm=3.0,
    )
    band = DirectLevelFrequencyBand(low_hz=100.0, high_hz=10000.0)
    evaluation = evaluate_frequency_resolved_load(
        impedance=impedance,
        amplifier_capability=amplifier,
        frequency_band=band,
        required_voltage_v_rms=10.0,
        amplifier_limit=limit,
    )
    assert evaluation.state == 'available'
    # Worst current demand is at the minimum |Z| = 4 ohm sample.
    assert evaluation.minimum_magnitude_ohm.value == pytest.approx(4.0)
    assert evaluation.worst_current_demand_a.value == pytest.approx(2.5)
    assert evaluation.load_domain_check == 'within_evidenced_domain'
    # 2.5 A against an 8 A ceiling -> headroom > 0.
    assert evaluation.current_headroom_db.value > 0.0

    # Below the impedance authority's domain, load checks stay explicit.
    below = evaluate_frequency_resolved_load(
        impedance=impedance,
        amplifier_capability=amplifier,
        frequency_band=DirectLevelFrequencyBand(low_hz=20.0, high_hz=80.0),
        required_voltage_v_rms=10.0,
        amplifier_limit=limit,
    )
    assert below.state in ('partial', 'unsupported')
    # Fully below the evidenced sample span: load cannot be resolved.
    assert below.load_domain_check == 'unavailable'
    assert not minimum_impedance_out_of_domain(amplifier, 4.0)
    assert minimum_impedance_out_of_domain(amplifier, 3.0)

    repository = CadAmplifierHeadroomRepository(
        scene_repository,
        CadSystemVariantRepository(scene_repository),
        equipment_repository,
    )
    repository.save_amplifier_capability(amplifier)
    repository.save_speaker_impedance(impedance)
    repository.save_amplifier_limit(limit)
    repository.save_frequency_resolved_evaluation(evaluation)
    assert (
        repository.get_frequency_resolved_evaluation(evaluation.evaluation_id)
        == evaluation
    )


# ---------------------------------------------------------------------------
# #569 — installed equipment instance authority
# ---------------------------------------------------------------------------


def test_installed_equipment_instance(tmp_path: Path) -> None:
    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    repo = CadInstalledEquipmentRepository(
        scene_repository, equipment_repository
    )

    instance = build_installed_equipment_instance(
        instance_id='inst-fl-001',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        equipment_definition=definition,
        serial_number='SN-0001',
        scene_entity_id='speaker-fl',
        installed_at_utc=NOW,
        provenance=(_provenance('install', 'e'),),
        created_at_utc=NOW,
    )
    repo.save_instance(instance)
    assert repo.get_instance('inst-fl-001') == instance
    assert repo.effective_state('inst-fl-001') == 'current'
    assert repo.list_instances(DOCUMENT_ID) == (instance,)

    # Instance identity survives a definition upgrade through append-only
    # bindings — the instance record itself is immutable.
    successor = build_installed_equipment_instance(
        instance_id='inst-fl-002',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        equipment_definition=definition,
        serial_number='SN-0002',
        scene_entity_id='speaker-fl',
        provenance=(_provenance('install', 'f'),),
        created_at_utc=NOW,
    )
    repo.save_instance(successor)
    repo.replace_instance(
        replacement_id='repl-1',
        removed_instance_id='inst-fl-001',
        installed_instance=successor,
        replaced_at_utc=NOW,
        provenance=(_provenance('install', 'f'),),
    )
    assert repo.effective_state('inst-fl-001') == 'replaced'
    assert repo.effective_state('inst-fl-002') == 'current'
    assert repo.list_instances(DOCUMENT_ID) == (successor,)
    assert repo.list_instances(DOCUMENT_ID, include_removed=True) == (
        instance,
        successor,
    )

    # Unresolved units still require manual identity evidence.
    with pytest.raises(ValueError, match='unresolved installed equipment'):
        build_installed_equipment_instance(
            instance_id='inst-x',
            document_id=DOCUMENT_ID,
            equipment_class='amplifier',
            provenance=(_provenance('install', 'e'),),
            created_at_utc=NOW,
        )

    # Capture identity rows map onto instances exactly.
    captured = installed_instance_from_capture_identity(
        instance_id='inst-cap-1',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_id=definition.definition_id,
        equipment_version=definition.version,
        equipment_hash=definition.semantic_sha256,
        serial_or_asset_tag='SN-0009',
        recorded_at_utc=NOW,
        equipment_class='speaker',
        equipment_definition=definition,
        provenance=(_provenance('capture', 'f'),),
        created_at_utc=NOW,
    )
    assert captured.serial_number == 'SN-0009'
    assert captured.scene_entity_id == 'speaker-fl'
    assert captured.is_catalog_resolved


def test_installed_instance_resolution_integrity(tmp_path: Path) -> None:
    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    repo = CadInstalledEquipmentRepository(
        scene_repository, equipment_repository
    )

    # A fabricated exact-resolution claim cannot be persisted.
    fabricated = build_installed_equipment_instance(
        instance_id='inst-fabricated',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        equipment_definition=_equipment(definition_id='ghost', digit='9'),
        scene_entity_id='speaker-fl',
        provenance=(_provenance('install', '0'),),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='unpersisted'):
        repo.save_instance(fabricated)

    # Neither can a binding to a fabricated definition.
    real = build_installed_equipment_instance(
        instance_id='inst-real',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        equipment_definition=definition,
        scene_entity_id='speaker-fl',
        provenance=(_provenance('install', 'b'),),
        created_at_utc=NOW,
    )
    repo.save_instance(real)
    with pytest.raises(ValueError, match='unpersisted'):
        repo.save_binding(
            build_installed_definition_binding(
                binding_id='bind-fake',
                instance_id='inst-real',
                equipment_definition=_equipment(
                    definition_id='ghost', digit='9'
                ),
                bound_at_utc=NOW,
                provenance=(_provenance('bind', 'c'),),
            )
        )

    # Exact resolution round-trips and reports typed state.
    resolution = repo.resolve_instance_definition('inst-real')
    assert resolution.status == 'RESOLVED_EXACT'
    assert resolution.definition == definition

    binding = build_installed_definition_binding(
        binding_id='bind-real',
        instance_id='inst-real',
        equipment_definition=definition,
        bound_at_utc=NOW,
        provenance=(_provenance('bind', 'd'),),
    )
    repo.save_binding(binding)
    resolution = repo.resolve_instance_definition('inst-real')
    assert resolution.status == 'RESOLVED_EXACT'
    assert resolution.binding == binding

    # Capture identity without a local definition is preserved as external
    # evidence — never as a resolution claim.
    captured = installed_instance_from_capture_identity(
        instance_id='inst-cap-ext',
        document_id=DOCUMENT_ID,
        entity_id='speaker-fl',
        equipment_id='some-external-model',
        equipment_version='7',
        equipment_hash='5' * 64,
        serial_or_asset_tag='SN-ext',
        recorded_at_utc=NOW,
        equipment_class='speaker',
        provenance=(_provenance('capture', 'e'),),
        created_at_utc=NOW,
    )
    assert not captured.is_catalog_resolved
    assert captured.has_observed_identity
    repo.save_instance(captured)
    resolution = repo.resolve_instance_definition('inst-cap-ext')
    assert resolution.status == 'EXTERNAL_UNRESOLVED'

    # A later explicit binding resolves it exactly without rewriting the
    # capture evidence.
    repo.save_binding(
        build_installed_definition_binding(
            binding_id='bind-cap-ext',
            instance_id='inst-cap-ext',
            equipment_definition=definition,
            bound_at_utc=NOW,
            provenance=(_provenance('bind', 'f'),),
        )
    )
    resolution = repo.resolve_instance_definition('inst-cap-ext')
    assert resolution.status == 'RESOLVED_EXACT'
    assert repo.get_instance('inst-cap-ext').has_observed_identity

    # usages_for_definition consumes only the in-effect exact resolution
    upgrade_repo = CadLibraryUpgradeRepository(
        scene_repository, equipment_repository
    )
    usage_ids = {
        u.authority_id
        for u in upgrade_repo.usages_for_definition(
            definition.semantic_sha256
        )
        if u.binding_kind == 'installed_instance'
    }
    assert 'inst-real' in usage_ids
    assert 'inst-cap-ext' in usage_ids


def test_installed_resolution_missing_and_conflict(tmp_path: Path) -> None:
    import sqlite3

    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    definition = _equipment()
    _save_equipment(equipment_repository, definition)
    repo = CadInstalledEquipmentRepository(
        scene_repository, equipment_repository
    )

    # Simulate legacy/foreign rows persisted before resolution enforcement:
    # insert fabricated payloads directly, bypassing save validation.
    def _insert(instance) -> None:
        with sqlite3.connect(scene_repository.path) as connection:
            connection.execute(
                'INSERT INTO cad_installed_equipment_instances('
                'instance_id, document_id, equipment_class, state, '
                'payload_json, recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?)',
                (
                    instance.instance_id,
                    instance.document_id,
                    instance.equipment_class,
                    instance.state,
                    instance.model_dump_json(),
                    instance.created_at_utc,
                ),
            )

    missing = build_installed_equipment_instance(
        instance_id='inst-missing',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        equipment_definition=_equipment(definition_id='gone', digit='8'),
        scene_entity_id='speaker-fl',
        provenance=(_provenance('install', '7'),),
        created_at_utc=NOW,
    )
    _insert(missing)
    resolution = repo.resolve_instance_definition('inst-missing')
    assert resolution.status == 'MISSING_LOCAL_DEFINITION'

    # A definition with the claimed hash persists but id/version differ —
    # a corrupted/rewritten row: construct a hash-consistent instance whose
    # ref points at the persisted hash under a divergent identity.
    conflict = build_installed_equipment_instance(
        instance_id='inst-conflict',
        document_id=DOCUMENT_ID,
        equipment_class='speaker',
        scene_entity_id='speaker-fl',
        provenance=(_provenance('install', '8'),),
        created_at_utc=NOW,
        user_label='conflicting-unit',
    )
    divergent_ref = InstalledDefinitionRef(
        equipment_definition_id='different-id',
        equipment_definition_version='99',
        equipment_definition_sha256=definition.semantic_sha256,
    )
    payload = conflict.semantic_payload()
    payload['definition_ref'] = divergent_ref.model_dump(mode='json')
    data = conflict.model_dump(mode='python')
    data['definition_ref'] = divergent_ref
    data['semantic_sha256'] = _digest(payload)
    _insert(InstalledEquipmentInstance.model_validate(data))
    resolution = repo.resolve_instance_definition('inst-conflict')
    assert resolution.status == 'CONFLICT'

    # A plain manual-identity unit is unresolved rather than external.
    plain = build_installed_equipment_instance(
        instance_id='inst-plain',
        document_id=DOCUMENT_ID,
        equipment_class='amplifier',
        manufacturer='m',
        model='amp-x',
        provenance=(_provenance('install', '9'),),
        created_at_utc=NOW,
    )
    repo.save_instance(plain)
    resolution = repo.resolve_instance_definition('inst-plain')
    assert resolution.status == 'UNRESOLVED'


def test_installed_row_payload_mismatch_fails_closed(tmp_path: Path) -> None:
    import sqlite3

    scene_repository, revision, _, equipment_repository = _repositories(
        tmp_path
    )
    repo = CadInstalledEquipmentRepository(
        scene_repository, equipment_repository
    )
    instance = build_installed_equipment_instance(
        instance_id='inst-integrity',
        document_id=DOCUMENT_ID,
        equipment_class='amplifier',
        manufacturer='m',
        model='amp-x',
        provenance=(_provenance('install', '0'),),
        created_at_utc=NOW,
    )
    repo.save_instance(instance)
    with sqlite3.connect(scene_repository.path) as connection:
        connection.execute(
            'UPDATE cad_installed_equipment_instances SET document_id=? '
            'WHERE instance_id=?',
            ('tampered-doc', instance.instance_id),
        )
    with pytest.raises(ValueError, match='row/payload mismatch'):
        repo.get_instance(instance.instance_id)


# ---------------------------------------------------------------------------
# #608 — shared library upgrade workflow
# ---------------------------------------------------------------------------


def test_library_upgrade_workflow(tmp_path: Path) -> None:
    scene_repository, revision, variant_repository, equipment_repository = (
        _repositories(tmp_path)
    )
    old_definition = _equipment(version='1', digit='1')
    _save_equipment(equipment_repository, old_definition)
    variant = _persist_variant(variant_repository, revision, old_definition)

    new_definition = _equipment(
        version='2',
        digit='a',
        envelope=Size3(x_m=0.22, y_m=0.27, z_m=0.40),
    )
    _save_equipment(equipment_repository, new_definition)

    repo = CadLibraryUpgradeRepository(scene_repository, equipment_repository)
    upgrade = diff_equipment_definitions(
        old_definition,
        new_definition,
        provenance=(_provenance('upgrade', '1'),),
        created_at_utc=NOW,
    )
    changed = {item.field for item in upgrade.changes}
    assert 'cabinet_envelope_m' in changed
    assert 'user_label' in changed or 'version' not in changed
    repo.save_upgrade(upgrade)

    usages = repo.usages_for_definition(old_definition.semantic_sha256)
    kinds = {item.binding_kind for item in usages}
    assert 'system_variant_equipment' in kinds

    # Label-only diffs do not force rederivation.
    relabeled = _equipment(version='1b', digit='1')
    _save_equipment(equipment_repository, relabeled)
    label_only = diff_equipment_definitions(
        old_definition,
        relabeled,
        provenance=(_provenance('upgrade', '1'),),
        created_at_utc=NOW,
    )
    assert adoption_required_fields(label_only) == ()

    # Adoption is an explicit per-document record; bindings rebase into a NEW
    # variant, never in place.
    rebased = rebase_equipment_bindings(variant.equipment_bindings, upgrade)
    assert rebased[0].equipment_definition_sha256 == (
        new_definition.semantic_sha256
    )
    rebased_variant = build_system_variant(
        baseline=revision,
        name='v2',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='Front Left'),
        ),
        proposed_entities=(),
        equipment_bindings=rebased,
        created_at_utc=NOW,
    )
    variant_repository.save_variant(rebased_variant)

    adoption = build_upgrade_adoption(
        upgrade=upgrade,
        document_id=DOCUMENT_ID,
        decision='adopted',
        rationale='envelope correction adopted for current layout',
        rebased_authority_sha256=(rebased_variant.variant_sha256,),
        provenance=(_provenance('upgrade', '2'),),
        created_at_utc=NOW,
    )
    repo.save_adoption(adoption)
    assert repo.adoptions_for_document(DOCUMENT_ID) == (adoption,)


# ---------------------------------------------------------------------------
# #646 — line-level gain structure
# ---------------------------------------------------------------------------


def _stage(
    stage_id: str,
    stage_kind: str,
    input_domain: str,
    output_domain: str,
    *,
    gain_db: float | None = 0.0,
    max_in: LineLevelValue | None = None,
    max_out: LineLevelValue | None = None,
    full_scale_v_rms: float | None = None,
    digit: str = '1',
):
    return build_line_level_stage_capability(
        stage_id=stage_id,
        version='1',
        identity_kind='user_defined',
        user_label=stage_id,
        port_id=f'{stage_id}-io',
        stage_kind=stage_kind,
        topology='balanced',
        input_domain=input_domain,
        output_domain=output_domain,
        full_scale_v_rms=full_scale_v_rms,
        gain_db=gain_db,
        maximum_input_level=max_in,
        maximum_output_level=max_out,
        provenance=(_provenance(stage_id, digit),),
    )


def test_gain_structure_chain(tmp_path: Path) -> None:
    scene_repository, revision, _, _ = _repositories(tmp_path)
    repo = CadGainStructureRepository(scene_repository)

    # DAC/source analog out -> DSP input (analog->digital converter) ->
    # DSP output (digital->analog) -> amplifier input.
    stages = (
        _stage(
            'source-out',
            'source_output',
            'analog_v_rms',
            'analog_v_rms',
            max_out=LineLevelValue(domain='analog_v_rms', value=5.0),
            digit='1',
        ),
        _stage(
            'dsp-in',
            'level_converter',
            'analog_v_rms',
            'digital_dbfs',
            full_scale_v_rms=2.0,
            max_in=LineLevelValue(domain='analog_v_rms', value=2.0),
            max_out=LineLevelValue(domain='digital_dbfs', value=0.0),
            digit='2',
        ),
        _stage(
            'dsp-out',
            'level_converter',
            'digital_dbfs',
            'analog_v_rms',
            full_scale_v_rms=4.0,
            max_in=LineLevelValue(domain='digital_dbfs', value=0.0),
            max_out=LineLevelValue(domain='analog_v_rms', value=4.0),
            digit='3',
        ),
        _stage(
            'amp-in',
            'amplifier_input',
            'analog_v_rms',
            'analog_v_rms',
            gain_db=0.0,
            max_in=LineLevelValue(domain='analog_v_rms', value=4.5),
            digit='4',
        ),
    )
    for stage in stages:
        repo.save_stage(stage)

    # 0.5 V RMS input (-6.02 dBV) -> -12.04 dBFS at 2 V full scale.
    assert dbv_to_dbfs(vrms_to_dbv(0.5), 2.0) == pytest.approx(-12.0412, abs=1e-3)
    assert dbfs_to_dbv(-12.0412, 2.0) == pytest.approx(-6.02, abs=1e-2)

    scenario = build_gain_structure_scenario(
        document_id=DOCUMENT_ID,
        stages=stages,
        head_input=LineLevelValue(
            domain='analog_v_rms',
            value=0.5,
        ),
        required_output=LineLevelValue(
            domain='analog_v_rms',
            value=0.5,
        ),
        operating_state=GainStructureOperatingState(
            master_volume_db=0.0,
            channel_trim_db=0.0,
            stage_gain_db=(),
        ),
    )
    repo.save_scenario(scenario)
    evaluation = evaluate_gain_structure(scenario=scenario, stages=stages)
    assert evaluation.state == 'available'
    assert len(evaluation.stage_results) == 4
    dsp_in = evaluation.stage_results[1]
    assert dsp_in.input_level_db == pytest.approx(-6.0206, abs=1e-3)
    assert dsp_in.output_level_db == pytest.approx(-12.0412, abs=1e-3)
    repo.save_evaluation(evaluation)
    assert (
        repo.get_evaluation(evaluation.evaluation_id) == evaluation
    )

    # Clipping: input above the DSP input maximum reports 'clipped'.
    hot = build_gain_structure_scenario(
        document_id=DOCUMENT_ID,
        stages=stages,
        head_input=LineLevelValue(
            domain='analog_v_rms',
            value=4.0,
        ),
        required_output=LineLevelValue(
            domain='analog_v_rms',
            value=4.0,
        ),
    )
    hot_eval = evaluate_gain_structure(scenario=hot, stages=stages)
    # A clipped stage invalidates the downstream chain honestly.
    assert hot_eval.state == 'unsupported'
    assert hot_eval.stage_results[1].state == 'clipped'
    assert hot_eval.limiting_stage_id == 'dsp-in'
