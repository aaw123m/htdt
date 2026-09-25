"""Backend tests for the authority-integrity batch (S8):

- #840 measurement target lineage derivation re-validation
- #841 exact equipment-binding refs + split resolvers
- #842 atomic single-transaction instance replacement
- #848 routing-profile scope + wiring-check evidence resolution
- #854 exact instance/binding definition refs + entity-kind compat
- #857 typed electrical load observations
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htdt.cad_amplifier_headroom import AuthorityRef
from htdt.cad_applicability import (
    APPLICABILITY_ROUTING_EVALUATOR_ID,
    ApplicabilityAuthorityContext,
    evaluate_applicability,
)
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_binding import (
    EquipmentBindingSemantics,
    _digest,
    build_equipment_binding_semantics,
)
from htdt.cad_equipment_binding_repository import CadEquipmentBindingRepository
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_instance import (
    InstalledEquipmentInstance,
    build_installed_equipment_instance,
    build_installed_equipment_replacement,
    installed_instance_from_capture_identity,
)
from htdt.cad_equipment_instance_repository import (
    CadInstalledEquipmentRepository,
)
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_listener_pose import (
    CadListenerPoseRepository,
    listener_pose_for_seat,
    pose_acoustic_reference_position,
)
from htdt.cad_measurement_authorities import (
    build_electrical_load_observation,
    build_routing_profile,
    build_wiring_check,
    derive_load_result,
    routing_profile_binding,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import build_acquisition_context
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_targets import build_measurement_target_lineage
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)


NOW = '2026-09-20T00:00:00+00:00'
DOCUMENT_ID = 'fixture-f1'


def _f1_repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    return (
        scene_repository,
        revision,
        measurement_repository,
        quality_repository,
    )


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
):
    """Persist one measured FR evidence record + dataset + raw asset."""
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        level_reference='unknown',
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return record


def _seat_entity(entity_id='seat-1') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name='Sofa',
        position=Position3(x_m=3.0, y_m=2.6, z_m=0.5),
        size_m=Size3(x_m=2.0, y_m=0.9, z_m=1.0),
        acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.6),
    )


def _point_entity(entity_id: str, position: Position3) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='measurement_point',
        name=entity_id,
        position=position,
    )


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='2026-09-20',
        source_reference='authority-integrity-fixture',
        source_sha256=digit * 64,
    )


def _equipment(definition_id='speaker-a', digit='1', version='1'):
    provenance = _provenance(definition_id, digit)
    return build_equipment_definition(
        definition_id=definition_id,
        version=version,
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.20, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _persist_definition(equipment_repository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='authority-integrity-fixture',
        recorded_at_utc=NOW,
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)


def _binding(
    binding_id: str,
    entity_id: str,
    definition,
    *,
    document_id=DOCUMENT_ID,
):
    return build_equipment_binding_semantics(
        binding_id=binding_id,
        document_id=document_id,
        entity_id=entity_id,
        equipment_definition=definition,
        body_geometry_authority='equipment_nominal',
        acoustic_reference_authority='equipment_derived',
        provenance=(_provenance(binding_id, '2'),),
        created_at_utc=NOW,
    )


def _profile(revision, *, document_id=DOCUMENT_ID, **kwargs):
    return build_routing_profile(
        profile_name='main-theater',
        document_id=document_id,
        scene_revision_id=revision.revision_id,
        entries=(
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'front_left',
                'expected_speaker_ids': ('speaker-fl',),
                'observed_speaker_ids': ('speaker-fl',),
                'verification': 'verified',
                'verified_at_utc': NOW,
            },
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FR',
                'hardware_channel_index': 1,
                'logical_role': 'front_right',
                'expected_speaker_ids': ('speaker-fr',),
                'observed_speaker_ids': ('speaker-fr',),
                'verification': 'verified',
                'verified_at_utc': NOW,
            },
        ),
        **kwargs,
    )


def _wiring_check(revision=None, **kwargs):
    kwargs.setdefault('check_kind', 'continuity')
    kwargs.setdefault('method', 'DCR probe')
    kwargs.setdefault('result', 'PASS')
    kwargs.setdefault('measured_at_utc', NOW)
    if kwargs['result'] == 'PASS':
        kwargs.setdefault('evidence_refs', ('manual:dcr-probe-verified',))
    kwargs.setdefault('document_id', DOCUMENT_ID)
    # A check pins the exact scene revision it was verified against.
    kwargs.setdefault(
        'scene_revision_id',
        revision.revision_id if revision is not None else 'rev-synthetic',
    )
    kwargs.setdefault(
        'scene_revision_sha256',
        revision.content_hash if revision is not None else '0' * 64,
    )
    return build_wiring_check(**kwargs)


def _instance(
    instance_id: str,
    *,
    document_id=DOCUMENT_ID,
    equipment_class='speaker',
    scene_entity_id='speaker-fl',
    equipment_definition=None,
):
    return build_installed_equipment_instance(
        instance_id=instance_id,
        document_id=document_id,
        equipment_class=equipment_class,
        equipment_definition=equipment_definition,
        manufacturer='m',
        model='x',
        user_label=instance_id,
        scene_entity_id=scene_entity_id,
        installed_at_utc=NOW,
        provenance=(_provenance(instance_id, '9'),),
        created_at_utc=NOW,
    )


# ---------------------------------------------------------------------------
# #840 measurement target lineage derivation


def _seat_point_scene(tmp_path: Path):
    (
        scene_repository,
        base_revision,
        measurement_repository,
        quality_repository,
    ) = _f1_repositories(tmp_path)
    seat = _seat_entity()
    seat_position = Position3(
        x_m=seat.position.x_m,
        y_m=seat.position.y_m,
        z_m=seat.position.z_m + seat.acoustic_reference_offset_m.z_m,
    )
    # The seat must pre-exist in the creation revision's parent — the
    # lineage contract resolves the source seat at the source revision,
    # never at the revision that created the point.
    source_document = base_revision.document.model_copy(
        update={
            'entities': (
                *base_revision.document.entities,
                seat,
            )
        }
    )
    source_revision = scene_repository.save(
        source_document,
        parent_revision_id=base_revision.revision_id,
    ).revision
    document = source_revision.document.model_copy(
        update={
            'entities': (
                *source_revision.document.entities,
                _point_entity('point-seat-derived', seat_position),
            )
        }
    )
    revision = scene_repository.save(
        document,
        parent_revision_id=source_revision.revision_id,
    ).revision
    return scene_repository, revision, quality_repository, seat_position


def test_target_lineage_seat_derivation_roundtrip(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-seat-derived',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=seat_position,
    )
    quality_repository.save_target_lineage(lineage)
    assert (
        quality_repository.get_target_lineage('point-seat-derived')
        == lineage
    )
    assert lineage in quality_repository.list_target_lineages(DOCUMENT_ID)


def test_target_lineage_rejects_unresolvable_creation_revision(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-seat-derived',
        source_seat_id='seat-1',
        creation_revision_id='not-a-revision',
        initial_position=seat_position,
    )
    with pytest.raises(ValueError, match='creation revision'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_rejects_wrong_document(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id='other-document',
        measurement_point_id='point-seat-derived',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=seat_position,
    )
    with pytest.raises(ValueError, match='document'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_rejects_mismatched_point_position(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-seat-derived',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=Position3(
            x_m=seat_position.x_m, y_m=seat_position.y_m, z_m=9.9
        ),
    )
    with pytest.raises(ValueError, match='initial position'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_rejects_non_seat_source(tmp_path):
    scene_repository, base_revision, _, quality_repository = (
        _f1_repositories(tmp_path)
    )
    # The point is created in a child revision so the source (parent)
    # revision resolves 'speaker-fl' — a speaker, never a seat.
    document = base_revision.document.model_copy(
        update={
            'entities': (
                *base_revision.document.entities,
                _point_entity(
                    'point-child', Position3(x_m=1.0, y_m=1.0, z_m=1.0)
                ),
            )
        }
    )
    revision = scene_repository.save(
        document, parent_revision_id=base_revision.revision_id
    ).revision
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-child',
        source_seat_id='speaker-fl',  # a speaker, never a seat
        creation_revision_id=revision.revision_id,
        initial_position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
    )
    with pytest.raises(ValueError, match='seat'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_rejects_non_measurement_point_target(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='speaker-fl',  # a speaker entity
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=seat_position,
    )
    with pytest.raises(ValueError, match='measurement'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_rejects_seat_without_acoustic_reference(tmp_path):
    scene_repository, base_revision, _, quality_repository = (
        _f1_repositories(tmp_path)
    )
    bare_seat = SceneEntity(
        entity_id='seat-bare',
        kind='seat',
        name='Stool',
        position=Position3(x_m=1.0, y_m=1.0, z_m=0.4),
        size_m=Size3(x_m=0.6, y_m=0.5, z_m=0.5),
    )
    source_document = base_revision.document.model_copy(
        update={
            'entities': (
                *base_revision.document.entities,
                bare_seat,
            )
        }
    )
    source_revision = scene_repository.save(
        source_document, parent_revision_id=base_revision.revision_id
    ).revision
    document = source_revision.document.model_copy(
        update={
            'entities': (
                *source_revision.document.entities,
                _point_entity(
                    'point-bare', Position3(x_m=1.0, y_m=1.0, z_m=0.4)
                ),
            )
        }
    )
    revision = scene_repository.save(
        document, parent_revision_id=source_revision.revision_id
    ).revision
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-bare',
        source_seat_id='seat-bare',
        creation_revision_id=revision.revision_id,
        initial_position=Position3(x_m=1.0, y_m=1.0, z_m=0.4),
    )
    with pytest.raises(ValueError, match='acoustic'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_pose_path_roundtrip(tmp_path):
    scene_repository, base_revision, _, quality_repository = (
        _f1_repositories(tmp_path)
    )
    seat = _seat_entity()
    pose = listener_pose_for_seat(
        seat,
        label='lean-back',
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.5),
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.55),
        provenance='authority-integrity-fixture',
    )
    pose_repository = CadListenerPoseRepository(quality_repository.path)
    pose_repository.save_pose(pose)
    position = pose_acoustic_reference_position(seat, pose)
    source_document = base_revision.document.model_copy(
        update={
            'entities': (
                *base_revision.document.entities,
                seat,
            )
        }
    )
    source_revision = scene_repository.save(
        source_document, parent_revision_id=base_revision.revision_id
    ).revision
    document = source_revision.document.model_copy(
        update={
            'entities': (
                *source_revision.document.entities,
                _point_entity('point-pose', position),
            )
        }
    )
    revision = scene_repository.save(
        document, parent_revision_id=source_revision.revision_id
    ).revision
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-pose',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=position,
        source_pose_ref=pose.authority_ref(),
    )
    quality_repository.save_target_lineage(lineage)
    assert quality_repository.get_target_lineage('point-pose') == lineage


def test_target_lineage_rejects_unresolvable_pose_ref(tmp_path):
    scene_repository, base_revision, _, quality_repository = (
        _f1_repositories(tmp_path)
    )
    seat = _seat_entity()
    pose = listener_pose_for_seat(
        seat,
        label='lean-back',
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.5),
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.55),
        provenance='authority-integrity-fixture',
    )
    # The ref names a pose that was never persisted.
    ref = pose.authority_ref()
    position = Position3(x_m=3.0, y_m=2.6, z_m=1.1)
    source_document = base_revision.document.model_copy(
        update={
            'entities': (
                *base_revision.document.entities,
                seat,
            )
        }
    )
    source_revision = scene_repository.save(
        source_document, parent_revision_id=base_revision.revision_id
    ).revision
    document = source_revision.document.model_copy(
        update={
            'entities': (
                *source_revision.document.entities,
                _point_entity('point-pose', position),
            )
        }
    )
    revision = scene_repository.save(
        document, parent_revision_id=source_revision.revision_id
    ).revision
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-pose',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=position,
        source_pose_ref=ref,
    )
    with pytest.raises(ValueError, match='pose'):
        quality_repository.save_target_lineage(lineage)


def test_target_lineage_read_rejects_tampered_row(tmp_path):
    _, revision, quality_repository, seat_position = _seat_point_scene(
        tmp_path
    )
    lineage = build_measurement_target_lineage(
        document_id=DOCUMENT_ID,
        measurement_point_id='point-seat-derived',
        source_seat_id='seat-1',
        creation_revision_id=revision.revision_id,
        initial_position=seat_position,
    )
    quality_repository.save_target_lineage(lineage)
    # Corrupt a denormalized column directly; the fail-closed read must
    # notice row and payload disagreeing.
    with sqlite3.connect(quality_repository.path) as connection:
        connection.execute(
            'UPDATE cad_measurement_target_lineages SET document_id=? '
            'WHERE target_lineage_id=?',
            ('forged-document', lineage.target_lineage_id),
        )
    with pytest.raises(ValueError, match='disagrees'):
        quality_repository.get_target_lineage('point-seat-derived')


# ---------------------------------------------------------------------------
# #841 exact binding authority + split resolvers


def _binding_repositories(tmp_path: Path):
    scene_repository, revision, _, _ = _f1_repositories(tmp_path)
    equipment_repository = CadEquipmentRepository(scene_repository)
    binding_repository = CadEquipmentBindingRepository(
        scene_repository, equipment_repository
    )
    return scene_repository, revision, equipment_repository, binding_repository


def test_binding_save_and_split_resolvers(tmp_path):
    _, _, equipment_repository, binding_repository = _binding_repositories(
        tmp_path
    )
    definition = _equipment('speaker-a', '2')
    _persist_definition(equipment_repository, definition)
    binding = _binding('bind-fl', 'speaker-fl', definition)
    assert binding_repository.save_binding(binding) == binding
    assert binding_repository.get_binding(binding.binding_id) == binding
    assert (
        binding_repository.latest_binding_for_current_entity(
            DOCUMENT_ID, 'speaker-fl'
        )
        == binding
    )
    assert (
        binding_repository.binding_by_exact_hash(binding.semantic_sha256)
        == binding
    )
    # Deprecated aliases keep working for older call sites.
    assert (
        binding_repository.get_binding_for_entity(DOCUMENT_ID, 'speaker-fl')
        == binding
    )
    assert (
        binding_repository.get_binding_by_hash(binding.semantic_sha256)
        == binding
    )


def test_binding_rejects_unpersisted_definition(tmp_path):
    _, _, equipment_repository, binding_repository = _binding_repositories(
        tmp_path
    )
    definition = _equipment('speaker-a', '2')  # never persisted
    binding = _binding('bind-fl', 'speaker-fl', definition)
    with pytest.raises(ValueError, match='unpersisted'):
        binding_repository.save_binding(binding)


def test_binding_rejects_mismatched_authority_identity(tmp_path):
    _, _, equipment_repository, binding_repository = _binding_repositories(
        tmp_path
    )
    persisted = _equipment('speaker-a', '2')
    _persist_definition(equipment_repository, persisted)
    # Forge a ref pinning the persisted definition's hash but declaring a
    # different id/version — the hash stays consistent with the record.
    forged_ref = AuthorityRef(
        authority_id='speaker-b',
        version='9',
        semantic_sha256=persisted.semantic_sha256,
    )
    base = _binding('bind-fl', 'speaker-fl', persisted)
    forged = EquipmentBindingSemantics(
        binding_id=base.binding_id,
        document_id=base.document_id,
        entity_id=base.entity_id,
        equipment=forged_ref,
        body_geometry_authority=base.body_geometry_authority,
        acoustic_reference_authority=base.acoustic_reference_authority,
        provenance=base.provenance,
        created_at_utc=base.created_at_utc,
        semantic_sha256=_digest(
            {
                'schema_version': base.schema_version,
                'authority_version': base.authority_version,
                'binding_id': base.binding_id,
                'document_id': base.document_id,
                'entity_id': base.entity_id,
                'equipment': forged_ref.model_dump(mode='json'),
                'body_geometry_authority': base.body_geometry_authority,
                'acoustic_reference_authority': (
                    base.acoustic_reference_authority
                ),
                'provenance': [
                    item.model_dump(mode='json')
                    for item in base.provenance
                ],
                'created_at_utc': base.created_at_utc,
            }
        ),
    )
    with pytest.raises(ValueError, match='different EquipmentDefinition'):
        binding_repository.save_binding(forged)


def test_binding_rejects_non_speaker_entity(tmp_path):
    _, _, equipment_repository, binding_repository = _binding_repositories(
        tmp_path
    )
    definition = _equipment('speaker-a', '2')
    _persist_definition(equipment_repository, definition)
    binding = _binding('bind-fl', 'point-mlp', definition)
    with pytest.raises(ValueError, match='speaker'):
        binding_repository.save_binding(binding)


def test_binding_read_rejects_tampered_row(tmp_path):
    _, _, equipment_repository, binding_repository = _binding_repositories(
        tmp_path
    )
    definition = _equipment('speaker-a', '2')
    _persist_definition(equipment_repository, definition)
    binding = _binding('bind-fl', 'speaker-fl', definition)
    binding_repository.save_binding(binding)
    with sqlite3.connect(binding_repository.path) as connection:
        connection.execute(
            'UPDATE cad_equipment_binding_semantics SET entity_id=? '
            'WHERE binding_id=?',
            ('speaker-c', binding.binding_id),
        )
    with pytest.raises(ValueError, match='disagrees'):
        binding_repository.get_binding(binding.binding_id)


# ---------------------------------------------------------------------------
# #842 atomic replacement + #854 exact instance authority


def _instance_repositories(tmp_path: Path):
    scene_repository, revision, _, _ = _f1_repositories(tmp_path)
    equipment_repository = CadEquipmentRepository(scene_repository)
    instance_repository = CadInstalledEquipmentRepository(
        scene_repository, equipment_repository
    )
    return (
        scene_repository,
        revision,
        equipment_repository,
        instance_repository,
    )


def _persisted_instance_setup(tmp_path: Path):
    (
        scene_repository,
        revision,
        equipment_repository,
        instance_repository,
    ) = _instance_repositories(tmp_path)
    definition = _equipment('speaker-a', '2')
    _persist_definition(equipment_repository, definition)
    return instance_repository, definition


def test_instance_save_and_replacement_roundtrip(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    old = _instance('inst-old', equipment_definition=definition)
    instance_repository.save_instance(old)
    new = _instance('inst-new', equipment_definition=definition)
    replacement = instance_repository.replace_instance(
        replacement_id='rep-1',
        removed_instance_id='inst-old',
        installed_instance=new,
        replaced_at_utc=NOW,
        rationale='tweeter failure',
        provenance=(_provenance('rep-1', '5'),),
    )
    assert instance_repository.get_instance('inst-new') == new
    assert instance_repository.list_replacements(DOCUMENT_ID) == (
        replacement,
    )
    assert instance_repository.effective_state('inst-old') == 'replaced'
    assert instance_repository.effective_state('inst-new') == 'current'


def test_replace_instance_is_atomic_on_contract_failure(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    new = _instance('inst-new', equipment_definition=definition)
    # The predecessor does not exist: the contract recheck must abort the
    # whole transaction so the successor is never persisted.
    with pytest.raises(ValueError, match='unknown installed'):
        instance_repository.replace_instance(
            replacement_id='rep-1',
            removed_instance_id='inst-missing',
            installed_instance=new,
            replaced_at_utc=NOW,
            provenance=(_provenance('rep-1', '5'),),
        )
    assert instance_repository.get_instance('inst-new') is None
    assert instance_repository.list_replacements(DOCUMENT_ID) == ()


def test_replacement_rejects_second_successor_for_predecessor(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    for instance_id in ('inst-a', 'inst-b', 'inst-c'):
        instance_repository.save_instance(
            _instance(instance_id, equipment_definition=definition)
        )
    instance_repository.save_replacement(
        build_installed_equipment_replacement(
            replacement_id='rep-1',
            document_id=DOCUMENT_ID,
            removed_instance_id='inst-a',
            installed_instance_id='inst-b',
            replaced_at_utc=NOW,
            provenance=(_provenance('rep-1', '5'),),
        )
    )
    # inst-a is already replaced — a second outgoing edge fails the
    # contract recheck; the UNIQUE(removed_instance_id) index backstops it
    # for racing writers.
    with pytest.raises(ValueError):
        instance_repository.save_replacement(
            build_installed_equipment_replacement(
                replacement_id='rep-2',
                document_id=DOCUMENT_ID,
                removed_instance_id='inst-a',
                installed_instance_id='inst-c',
                replaced_at_utc=NOW,
                provenance=(_provenance('rep-2', '5'),),
            )
        )
    assert len(instance_repository.list_replacements(DOCUMENT_ID)) == 1


def test_replacement_rejects_reused_successor(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    for instance_id in ('inst-a', 'inst-b', 'inst-c'):
        instance_repository.save_instance(
            _instance(instance_id, equipment_definition=definition)
        )
    instance_repository.save_replacement(
        build_installed_equipment_replacement(
            replacement_id='rep-1',
            document_id=DOCUMENT_ID,
            removed_instance_id='inst-a',
            installed_instance_id='inst-c',
            replaced_at_utc=NOW,
            provenance=(_provenance('rep-1', '5'),),
        )
    )
    # inst-c already absorbed a replacement — it cannot absorb a second.
    with pytest.raises(ValueError, match='successor'):
        instance_repository.save_replacement(
            build_installed_equipment_replacement(
                replacement_id='rep-2',
                document_id=DOCUMENT_ID,
                removed_instance_id='inst-b',
                installed_instance_id='inst-c',
                replaced_at_utc=NOW,
                provenance=(_provenance('rep-2', '5'),),
            )
        )
    assert len(instance_repository.list_replacements(DOCUMENT_ID)) == 1


def test_instance_rejects_unpersisted_definition_ref(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    unpersisted = _equipment('speaker-b', '3')
    instance = _instance('inst-x', equipment_definition=unpersisted)
    with pytest.raises(ValueError, match='unpersisted'):
        instance_repository.save_instance(instance)


def test_instance_rejects_mismatched_definition_identity(tmp_path):
    instance_repository, persisted = _persisted_instance_setup(tmp_path)
    # Same hash, different declared identity — must fail the exact-ref
    # identity check.
    payload = _instance(
        'inst-x', equipment_definition=persisted
    ).semantic_payload()
    payload['definition_ref'] = {
        'equipment_definition_id': 'speaker-b',
        'equipment_definition_version': '9',
        'equipment_definition_sha256': persisted.semantic_sha256,
    }
    payload['semantic_sha256'] = _digest(payload)
    instance = InstalledEquipmentInstance.model_validate(payload)
    with pytest.raises(ValueError, match='different EquipmentDefinition'):
        instance_repository.save_instance(instance)


def test_instance_entity_binding_respects_equipment_class(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    # A speaker instance cannot bind a measurement point.
    with pytest.raises(ValueError, match='kind'):
        instance_repository.save_instance(
            _instance(
                'inst-point',
                equipment_class='speaker',
                scene_entity_id='point-mlp',
                equipment_definition=definition,
            )
        )
    # And it cannot bind a nonexistent entity either.
    with pytest.raises(ValueError, match='not present'):
        instance_repository.save_instance(
            _instance(
                'inst-ghost',
                equipment_class='speaker',
                scene_entity_id='speaker-ghost',
                equipment_definition=definition,
            )
        )
    # Rack-only inventory keeps scene_entity_id=None and persists fine.
    rack = _instance(
        'inst-rack', scene_entity_id=None, equipment_definition=definition
    )
    instance_repository.save_instance(rack)
    assert instance_repository.get_instance('inst-rack') == rack


def test_instance_read_rejects_tampered_row(tmp_path):
    instance_repository, definition = _persisted_instance_setup(tmp_path)
    instance = _instance('inst-a', equipment_definition=definition)
    instance_repository.save_instance(instance)
    with sqlite3.connect(instance_repository.path) as connection:
        connection.execute(
            'UPDATE cad_installed_equipment_instances SET document_id=? '
            'WHERE instance_id=?',
            ('forged-document', 'inst-a'),
        )
    with pytest.raises(ValueError, match='disagrees'):
        instance_repository.get_instance('inst-a')


# ---------------------------------------------------------------------------
# #848 routing profile scope + wiring check resolution


def test_routing_profile_requires_scope(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    unscoped = build_routing_profile(
        profile_name='unscoped',
        entries=(
            {
                'output_device_label': 'dev',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
            },
        ),
    )
    with pytest.raises(ValueError, match='scope'):
        quality_repository.save_routing_profile(unscoped)


def test_routing_profile_rejects_wrong_document_scope(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    profile = _profile(revision, document_id='other-document')
    with pytest.raises(ValueError, match='unresolvable'):
        quality_repository.save_routing_profile(profile)


def test_routing_profile_rejects_non_speaker_ref(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    profile = _profile(revision)
    bad = build_routing_profile(
        profile_name='bad-scope',
        document_id=DOCUMENT_ID,
        scene_revision_id=revision.revision_id,
        entries=(
            {
                'output_device_label': 'dev',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'expected_speaker_ids': ('point-mlp',),  # measurement point
            },
        ),
    )
    with pytest.raises(ValueError, match='speaker'):
        quality_repository.save_routing_profile(bad)
    quality_repository.save_routing_profile(profile)  # sane still passes


def test_acquisition_context_resolves_routing_profile(tmp_path):
    _, revision, measurement_repository, quality_repository = (
        _f1_repositories(tmp_path)
    )
    _save_measurement(measurement_repository, revision, 'meas-ctx')
    profile = _profile(revision)
    quality_repository.save_routing_profile(profile)
    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=('meas-ctx',),
        routing_profile=routing_profile_binding(profile),
        created_at_utc=NOW,
    )
    quality_repository.save_acquisition_context(context)
    assert (
        quality_repository.get_acquisition_context(
            context.acquisition_context_id
        )
        == context
    )


def test_acquisition_context_rejects_bad_profile_binding(tmp_path):
    _, revision, measurement_repository, quality_repository = (
        _f1_repositories(tmp_path)
    )
    _save_measurement(measurement_repository, revision, 'meas-ctx')
    profile = _profile(revision)
    quality_repository.save_routing_profile(profile)
    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=('meas-ctx',),
        routing_profile=routing_profile_binding(profile).model_copy(
            update={'routing_profile_sha256': '0' * 64}
        ),
        created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match='routing profile'):
        quality_repository.save_acquisition_context(context)


def test_wiring_check_pass_requires_evidence(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    with pytest.raises(ValueError, match='evidence'):
        check = _wiring_check(
            revision,
            expected_speaker_ids=('speaker-fl',),
            evidence_refs=(),
            operator=None,
        )
        quality_repository.save_wiring_check(check)


def test_wiring_check_manual_attestation_passes(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    check = _wiring_check(
        revision,
        expected_speaker_ids=('speaker-fl',))
    quality_repository.save_wiring_check(check)
    assert (
        quality_repository.get_wiring_check(check.check_id) == check
    )


def test_wiring_check_rejects_unknown_document(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    check = _wiring_check(
        revision,
        document_id='other-document',
        expected_speaker_ids=(),
    )
    with pytest.raises(ValueError, match='document'):
        quality_repository.save_wiring_check(check)


def test_wiring_check_rejects_non_speaker_ref(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    check = _wiring_check(
        revision,
        expected_speaker_ids=('point-mlp',))
    with pytest.raises(ValueError, match='speaker'):
        quality_repository.save_wiring_check(check)


def test_wiring_check_rejects_unresolvable_evidence(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    check = _wiring_check(
        revision,
        expected_speaker_ids=('speaker-fl',),
        evidence_refs=('no-such-measurement-id',),
    )
    with pytest.raises(ValueError, match='evidence'):
        quality_repository.save_wiring_check(check)


def test_wiring_check_measurement_evidence_resolves(tmp_path):
    _, revision, measurement_repository, quality_repository = (
        _f1_repositories(tmp_path)
    )
    _save_measurement(measurement_repository, revision, 'meas-evidence')
    check = _wiring_check(
        revision,
        expected_speaker_ids=('speaker-fl',),
        evidence_refs=('meas-evidence',),
    )
    quality_repository.save_wiring_check(check)
    assert (
        quality_repository.get_wiring_check(check.check_id) == check
    )


def test_wiring_check_routing_pass_requires_profile_binding(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    with pytest.raises(ValueError, match='routing'):
        check = _wiring_check(
            revision,
            check_kind='routing',
            expected_speaker_ids=('speaker-fl',),
            evidence_refs=('manual:verified-continuity',),
        )
        quality_repository.save_wiring_check(check)


def test_wiring_check_routing_pass_with_binding(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    profile = _profile(revision)
    quality_repository.save_routing_profile(profile)
    check = _wiring_check(
        revision,
        check_kind='routing',
        expected_speaker_ids=('speaker-fl',),
        routing_profile_ref=routing_profile_binding(profile),
    )
    quality_repository.save_wiring_check(check)


def test_applicability_routing_requires_exact_profile_pin(tmp_path):
    """#848: bare routing_evidence='verified' never satisfies routing."""

    def measurement(provenance: dict) -> SimpleNamespace:
        return SimpleNamespace(
            measurement_id='meas-1',
            evidence_type='measured',
            routing_evidence='verified',
            provenance_json=json.dumps(provenance),
        )

    def context(measurements):
        return ApplicabilityAuthorityContext(
            document_id=DOCUMENT_ID,
            search_spec_id='spec-1',
            search_spec_sha256='a' * 64,
            candidate_set_sha256='b' * 64,
            model_id='model',
            model_version='1',
            evidence_scope='document',
            campaign_id=None,
            requested_band_hz=(20.0, 20000.0),
            search_spec=None,
            scene_revision=None,
            prediction_batches=(),
            measurement_plans=(
                SimpleNamespace(
                    plan_id='plan-1', plan_sha256='d' * 64
                ),
            ),
            measurements=measurements,
            pair_responses=(),
            attestation_repository=None,
        )

    # Coarse 'verified' alone does not pass.
    check = evaluate_applicability(
        context((measurement({}),)),
        code='routing',
        evaluator_id=APPLICABILITY_ROUTING_EVALUATOR_ID,
    )
    assert check.passed is False
    # An exact id+hash profile pin in provenance does.
    check = evaluate_applicability(
        context(
            (
                measurement(
                    {
                        'routing_profile': {
                            'routing_profile_id': 'profile-1',
                            'routing_profile_sha256': 'c' * 64,
                        }
                    }
                ),
            )
        ),
        code='routing',
        evaluator_id=APPLICABILITY_ROUTING_EVALUATOR_ID,
    )
    assert check.passed is True


# ---------------------------------------------------------------------------
# #857 typed electrical load observations


def test_dcr_observation_cannot_masquerade_as_impedance():
    with pytest.raises(ValidationError):
        build_electrical_load_observation(
            quantity_kind='dcr',
            value_ohm=6.4,
            test_condition='frequency',  # a DCR reading is never a curve
            test_frequency_hz=100.0,
            measured_at_utc=NOW,
        )
    with pytest.raises(ValidationError):
        build_electrical_load_observation(
            quantity_kind='impedance_magnitude',
            value_ohm=8.0,
            test_condition='dc',  # impedance needs a frequency
            measured_at_utc=NOW,
        )
    with pytest.raises(ValidationError):
        build_electrical_load_observation(
            quantity_kind='dcr',
            value_ohm=6.4,
            test_condition='dc',
            test_frequency_hz=50.0,  # DC observations carry no frequency
            measured_at_utc=NOW,
        )


def test_load_observation_expected_range_is_pairwise():
    with pytest.raises(ValidationError):
        build_electrical_load_observation(
            quantity_kind='dcr',
            value_ohm=6.4,
            test_condition='dc',
            expected_min_ohm=5.0,
            measured_at_utc=NOW,
        )
    with pytest.raises(ValidationError):
        build_electrical_load_observation(
            quantity_kind='dcr',
            value_ohm=6.4,
            test_condition='dc',
            expected_min_ohm=8.0,
            expected_max_ohm=5.0,
            measured_at_utc=NOW,
        )


def test_derive_load_result():
    in_range = build_electrical_load_observation(
        quantity_kind='dcr',
        value_ohm=6.4,
        test_condition='dc',
        expected_min_ohm=5.0,
        expected_max_ohm=8.0,
        expected_source='datasheet nominal',
        measured_at_utc=NOW,
    )
    assert derive_load_result(in_range) == 'PASS'
    out_of_range = build_electrical_load_observation(
        quantity_kind='impedance_magnitude',
        value_ohm=3.2,
        test_condition='frequency',
        test_frequency_hz=80.0,
        expected_min_ohm=6.0,
        expected_max_ohm=8.0,
        measured_at_utc=NOW,
    )
    assert derive_load_result(out_of_range) == 'FAIL'
    unscoped = build_electrical_load_observation(
        quantity_kind='dcr',
        value_ohm=6.4,
        test_condition='dc',
        measured_at_utc=NOW,
    )
    assert derive_load_result(unscoped) == 'UNKNOWN'


def test_load_check_result_must_match_derived(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    observation = build_electrical_load_observation(
        quantity_kind='dcr',
        value_ohm=3.2,
        test_condition='dc',
        expected_min_ohm=6.0,
        expected_max_ohm=8.0,
        measured_at_utc=NOW,
    )
    # derive_load_result(observation) == 'FAIL': a claimed PASS cannot
    # contradict the bound quantitative evidence.
    check = _wiring_check(
        revision,
        check_kind='load',
        expected_speaker_ids=('speaker-fl',),
        evidence_refs=('manual:dcr-probe',),
        load_observation=observation,
    )
    with pytest.raises(ValueError, match='derived'):
        quality_repository.save_wiring_check(check)


def test_load_check_pass_with_in_range_observation(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    observation = build_electrical_load_observation(
        quantity_kind='dcr',
        value_ohm=6.4,
        test_condition='dc',
        expected_min_ohm=5.0,
        expected_max_ohm=8.0,
        expected_source='datasheet',
        measured_at_utc=NOW,
    )
    check = _wiring_check(
        revision,
        check_kind='load',
        expected_speaker_ids=('speaker-fl',),
        evidence_refs=('manual:dcr-probe',),
        load_observation=observation,
    )
    quality_repository.save_wiring_check(check)
    assert (
        quality_repository.get_wiring_check(check.check_id) == check
    )


def test_load_check_pass_requires_observation(tmp_path):
    _, revision, _, quality_repository = _f1_repositories(tmp_path)
    check = _wiring_check(
        revision,
        check_kind='load',
        expected_speaker_ids=('speaker-fl',),
        evidence_refs=('manual:dcr-probe',),
    )
    with pytest.raises(ValueError, match='observation'):
        quality_repository.save_wiring_check(check)
