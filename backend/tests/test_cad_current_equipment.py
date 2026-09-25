"""Issue #984: application-aware current equipment authority.

Applying a SystemVariant promotes its exact EquipmentDefinition bindings
into current-state authority: the resolver walks
SceneRevision -> SystemVariantApplication ancestry -> originating variant
binding -> EquipmentDefinition, with explicit EquipmentBindingSemantics
rebinds overriding only for revisions created after the rebind.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from htdt.cad_current_equipment import (
    current_equipment_projection,
    resolve_current_equipment_binding,
)
from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_binding import build_equipment_binding_semantics
from htdt.cad_equipment_binding_repository import CadEquipmentBindingRepository
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_r110_source import compile_r110_source_model
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


DOCUMENT_ID = 'o100a-equipment-fixture'
NOW = '2026-09-19T00:00:00+00:00'


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


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id=DOCUMENT_ID,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            _speaker('fl', 'FL', 1.2),
            _speaker('fr', 'FR', 4.8),
            SceneEntity(
                entity_id='mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.2, z_m=1.1),
            ),
        ),
    )


def _provenance(source_hash: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='HTDT user equipment record',
        source_version='1',
        source_reference='fixture',
        source_sha256=source_hash,
    )


def _equipment(definition_id: str, source_hash: str):
    provenance = _provenance(source_hash)
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=f'Fixture speaker {definition_id}',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _binding_ref(entity_id: str, definition) -> EquipmentBindingRef:
    return EquipmentBindingRef(
        entity_id=entity_id,
        equipment_definition_id=definition.definition_id,
        equipment_definition_version=definition.version,
        equipment_definition_sha256=definition.semantic_sha256,
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    baseline = scene_repository.save(
        _scene(), parent_revision_id=None
    ).revision
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(scene_repository)
    binding_repository = CadEquipmentBindingRepository(
        scene_repository, equipment_repository
    )
    model_a = _equipment('fixture-model-a', 'a' * 64)
    model_b = _equipment('fixture-model-b', 'b' * 64)
    for definition in (model_a, model_b):
        for evidence in build_equipment_manual_evidence(
            definition,
            actor='equipment-test-fixture',
            recorded_at_utc=NOW,
        ):
            equipment_repository.save_evidence(evidence)
        equipment_repository.save_definition(definition)
    return (
        scene_repository,
        baseline,
        variant_repository,
        equipment_repository,
        binding_repository,
        model_a,
        model_b,
    )


def _bound_variant(baseline, model_a, model_b):
    return build_system_variant(
        baseline=baseline,
        name='Proposed rewire',
        role_bindings=(
            ChannelRoleBinding(role_id='FL', display_name='FL'),
            ChannelRoleBinding(role_id='FR', display_name='FR'),
            ChannelRoleBinding(role_id='SL', display_name='SL'),
        ),
        proposed_entities=(
            ProposedEntitySpec(
                spec_id='proposal-sl',
                entity=_speaker('sl', 'SL', 0.6),
                role_binding_id='SL',
            ),
        ),
        equipment_bindings=(
            _binding_ref('fl', model_a),
            _binding_ref('sl', model_b),
        ),
        created_at_utc=NOW,
    )


def _resolve(repos, revision_id, entity_id):
    (
        scene_repository,
        _baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        _model_a,
        _model_b,
    ) = repos
    return resolve_current_equipment_binding(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        revision_id=revision_id,
        entity_id=entity_id,
        binding_repository=binding_repository,
    )


def test_apply_promotes_variant_binding_to_current_authority(
    tmp_path: Path,
) -> None:
    repos = _fixture(tmp_path)
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = repos
    variant = _bound_variant(baseline, model_a, model_b)
    variant_repository.save_variant(variant)

    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    bound = _resolve(repos, applied.revision_id, 'fl')
    assert bound is not None
    assert bound.resolution == 'variant_application'
    assert bound.equipment_definition_id == 'fixture-model-a'
    assert bound.equipment_definition_sha256 == model_a.semantic_sha256
    assert bound.application_id == application.application_id
    assert bound.variant_sha256 == variant.variant_sha256
    assert bound.effective_at_utc == application.selected_at_utc

    added = _resolve(repos, applied.revision_id, 'sl')
    assert added is not None
    assert added.resolution == 'variant_application'
    assert added.equipment_definition_id == 'fixture-model-b'

    # Unbound speaker inherits nothing; non-speaker resolves nothing.
    assert _resolve(repos, applied.revision_id, 'fr') is None
    assert _resolve(repos, applied.revision_id, 'mlp') is None

    # Reopen the project: resolution survives on persisted state alone.
    reopened_scene = SceneRepository(scene_repository.path)
    reopened_variants = CadSystemVariantRepository(reopened_scene)
    reopened_bindings = CadEquipmentBindingRepository(
        reopened_scene,
        CadEquipmentRepository(reopened_scene),
    )
    reopened = resolve_current_equipment_binding(
        scene_repository=reopened_scene,
        variant_repository=reopened_variants,
        revision_id=applied.revision_id,
        entity_id='fl',
        binding_repository=reopened_bindings,
    )
    assert reopened == bound

    # Apply wrote no binding-semantics rows: promotion is resolver-derived.
    assert reopened_bindings.latest_binding_for_current_entity(
        DOCUMENT_ID, 'fl'
    ) is None


def test_descendant_inherits_and_descendant_add_resolves_explicit(
    tmp_path: Path,
) -> None:
    repos = _fixture(tmp_path)
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = repos
    variant = _bound_variant(baseline, model_a, model_b)
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    descendant = scene_repository.save(
        applied.document.model_copy(
            update={
                'entities': applied.document.entities
                + (_speaker('sr', 'SR', 5.4),)
            }
        ),
        parent_revision_id=applied.revision_id,
    ).revision

    inherited = _resolve(repos, descendant.revision_id, 'fl')
    assert inherited is not None
    assert inherited.resolution == 'variant_application'
    assert inherited.equipment_definition_id == 'fixture-model-a'

    # Entity added after the apply has no variant-derived candidate.
    assert _resolve(repos, descendant.revision_id, 'sr') is None

    # An explicit binding record covers the newly added entity.
    binding_repository.save_binding(
        build_equipment_binding_semantics(
            binding_id='binding-sr-1',
            document_id=DOCUMENT_ID,
            entity_id='sr',
            equipment_definition=model_b,
            body_geometry_authority='equipment_nominal',
            acoustic_reference_authority='equipment_derived',
            provenance=(_provenance('c' * 64),),
            created_at_utc='2026-09-21T00:00:00+00:00',
        )
    )
    explicit = _resolve(repos, descendant.revision_id, 'sr')
    assert explicit is not None
    assert explicit.resolution == 'explicit_binding'
    assert explicit.binding_id == 'binding-sr-1'
    assert explicit.equipment_definition_id == 'fixture-model-b'


def test_explicit_rebind_after_apply_wins_only_for_later_revisions(
    tmp_path: Path,
) -> None:
    repos = _fixture(tmp_path)
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = repos
    variant = _bound_variant(baseline, model_a, model_b)
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    # Rebind 'fl' to model B strictly after the applied revision's
    # created_at_utc (Windows clock granularity is ~15ms — spin past the
    # tick boundary so ordering is deterministic).
    rebind_time = datetime.now(timezone.utc).isoformat()
    while rebind_time <= applied.created_at_utc:
        rebind_time = datetime.now(timezone.utc).isoformat()
    binding_repository.save_binding(
        build_equipment_binding_semantics(
            binding_id='binding-fl-rebind',
            document_id=DOCUMENT_ID,
            entity_id='fl',
            equipment_definition=model_b,
            body_geometry_authority='equipment_nominal',
            acoustic_reference_authority='equipment_derived',
            provenance=(_provenance('d' * 64),),
            created_at_utc=rebind_time,
        )
    )

    # Historical replay at the applied revision: the rebind did not exist
    # yet — the variant binding still wins there.
    replay = _resolve(repos, applied.revision_id, 'fl')
    assert replay is not None
    assert replay.resolution == 'variant_application'
    assert replay.equipment_definition_id == 'fixture-model-a'

    time.sleep(0.05)
    descendant = scene_repository.save(
        applied.document.model_copy(
            update={
                'entities': tuple(
                    entity.model_copy(
                        update={
                            'position': entity.position.model_copy(
                                update={'x_m': 1.25}
                            )
                        }
                    )
                    if entity.entity_id == 'fl'
                    else entity
                    for entity in applied.document.entities
                )
            }
        ),
        parent_revision_id=applied.revision_id,
    ).revision
    current = _resolve(repos, descendant.revision_id, 'fl')
    assert current is not None
    assert current.resolution == 'explicit_binding'
    assert current.equipment_definition_id == 'fixture-model-b'
    assert current.binding_id == 'binding-fl-rebind'


def test_apply_overrides_older_explicit_binding(tmp_path: Path) -> None:
    repos = _fixture(tmp_path)
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = repos
    # The baseline 'fl' had an explicit binding to model B.
    binding_repository.save_binding(
        build_equipment_binding_semantics(
            binding_id='binding-fl-old',
            document_id=DOCUMENT_ID,
            entity_id='fl',
            equipment_definition=model_b,
            body_geometry_authority='equipment_nominal',
            acoustic_reference_authority='equipment_derived',
            provenance=(_provenance('e' * 64),),
            created_at_utc='2026-09-18T00:00:00+00:00',
        )
    )
    variant = _bound_variant(baseline, model_a, model_b)
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    # The applied variant's model-A binding overrides the older record.
    resolved = _resolve(repos, applied.revision_id, 'fl')
    assert resolved is not None
    assert resolved.resolution == 'variant_application'
    assert resolved.equipment_definition_id == 'fixture-model-a'

    # 'fr' was not bound by the variant: it inherits the baseline's prior
    # current binding — here there is none for 'fr'.
    assert _resolve(repos, applied.revision_id, 'fr') is None


def test_removed_and_unbound_speakers_have_no_current_equipment(
    tmp_path: Path,
) -> None:
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = _fixture(tmp_path)
    variant = build_system_variant(
        baseline=baseline,
        name='Remove FR',
        role_bindings=(ChannelRoleBinding(role_id='FL', display_name='FL'),),
        proposed_entities=(),
        remove_entity_ids=('fr',),
        equipment_bindings=(_binding_ref('fl', model_a),),
        created_at_utc=NOW,
    )
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    assert _resolve(
        (
            scene_repository,
            baseline,
            variant_repository,
            _equipment_repository,
            binding_repository,
            model_a,
            model_b,
        ),
        applied.revision_id,
        'fr',
    ) is None


def test_current_equipment_projection_compiles_applied_state(
    tmp_path: Path,
) -> None:
    repos = _fixture(tmp_path)
    (
        scene_repository,
        baseline,
        variant_repository,
        _equipment_repository,
        binding_repository,
        model_a,
        model_b,
    ) = repos
    variant = _bound_variant(baseline, model_a, model_b)
    variant_repository.save_variant(variant)
    application = variant_repository.apply_variant(
        variant.variant_id,
        selected_by='fixture-selection',
        selected_at_utc='2026-09-20T00:01:00+00:00',
    )
    applied = scene_repository.get(application.applied_revision_id)

    projection = current_equipment_projection(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        revision_id=applied.revision_id,
        binding_repository=binding_repository,
    )
    assert projection.baseline_revision_id == applied.revision_id
    assert projection.baseline_content_hash == applied.content_hash
    assert projection.diff == ()
    assert {
        item.entity_id: item.equipment_definition_id
        for item in projection.equipment_bindings
    } == {'fl': 'fixture-model-a', 'sl': 'fixture-model-b'}

    # Deterministic: the same projection derives the same exact identity.
    again = current_equipment_projection(
        scene_repository=scene_repository,
        variant_repository=variant_repository,
        revision_id=applied.revision_id,
        binding_repository=binding_repository,
    )
    assert again == projection

    # The applied current state compiles through the exact R110 path — the
    # applied revision is not pretending to be the old variant baseline.
    compiled = compile_r110_source_model(
        scene_revision=applied,
        system_variant=projection,
        source_entity_id='fl',
        equipment_definition=model_a,
    )
    assert compiled.system_variant_id == projection.variant_id
    assert compiled.system_variant_sha256 == projection.variant_sha256

    # An unbound speaker fails closed rather than compiling with generic
    # equipment authority.
    with pytest.raises(ValueError, match='exactly one'):
        compile_r110_source_model(
            scene_revision=applied,
            system_variant=projection,
            source_entity_id='fr',
            equipment_definition=model_a,
        )
