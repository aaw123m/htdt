"""Issue #414 — typed, replayable evidence authorities behind EquipmentDefinition.

An ``EquipmentDataProvenance`` claim (evidence kind + source name/version/
reference + ``source_sha256``) is only a claim. These tests pin the O100C
contract that every cited claim must resolve to a retained immutable
``EquipmentEvidenceAuthority`` bound to the exact definition SHA-256, whose
recorded subject reproduces the definition's normalized field-group values —
re-resolved and re-derived on save and on every authoritative read.
"""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
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
)
from htdt.cad_equipment_evidence import (
    EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
    EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
    EQUIPMENT_EVIDENCE_JSON_SCHEMA,
    EquipmentEvidenceAuthority,
    EquipmentEvidenceSubject,
    EquipmentSensitivitySubject,
    EquipmentUncertaintySubject,
    ExternalEquipmentAuthority,
    ManagedEquipmentSourceAsset,
    build_equipment_evidence_authority,
    build_equipment_manual_evidence,
    equipment_evidence_subject,
    equipment_field_group_subjects,
    replay_equipment_evidence_extraction,
)
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, RoomPrism, SceneDocument, Size3
from htdt.native_backup import create_backup, restore_backup, validate_backup


NOW = '2026-09-19T12:00:00+00:00'


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _provenance(
    evidence_kind: str = 'manufacturer',
    source_name: str = 'Example Audio datasheet',
    source_hash: str = 'a' * 64,
) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version='2026-09-19',
        source_reference='datasheet section 4.2',
        source_sha256=source_hash,
    )


def _domain() -> DirectivityDomain:
    return DirectivityDomain(
        frequency=FrequencyDomain(minimum_hz=80.0, maximum_hz=18000.0),
        horizontal=AngleDomain(minimum_deg=-90.0, maximum_deg=90.0),
        vertical=AngleDomain(minimum_deg=-45.0, maximum_deg=45.0),
    )


def _definition(
    *,
    definition_id: str = 'example-audio-monitor-x',
    version: str = 'datasheet-2026-09',
    source_sha256: str = 'a' * 64,
    sensitivity_db: float = 88.0,
    continuous_spl: float = 105.0,
):
    """Manufacturer definition where one provenance claim covers every field."""
    provenance = _provenance(source_hash=source_sha256)
    return build_equipment_definition(
        definition_id=definition_id,
        version=version,
        identity_kind='manufacturer',
        manufacturer='Example Audio',
        model='Monitor X',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.22, y_m=0.31, z_m=0.38),
        acoustic_reference_point_m=Offset3(x_m=0.0, y_m=0.04, z_m=0.08),
        mounting=MountingMetadata(mounting_modes=('stand', 'shelf')),
        port=PortMetadata(port_type='rear', minimum_clearance_m=0.15),
        clearance=ClearanceMetadata(rear_m=0.15, side_m=0.05),
        sensitivity=SensitivityReference(
            level_db_spl=sensitivity_db,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=100.0,
                maximum_hz=10000.0,
            ),
            provenance=provenance,
        ),
        spl_capability=SplCapability(
            continuous_db_spl=continuous_spl,
            peak_db_spl=continuous_spl + 6.0,
            reference_distance_m=1.0,
            declared_headroom_db=6.0,
            headroom_reference_level_db_spl=continuous_spl,
            provenance=provenance,
        ),
        directivity=DirectivityCapability(
            tier='magnitude_only',
            data_format='clf',
            provenance=provenance,
            data_asset_sha256='e' * 64,
            valid_domain=_domain(),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='source-declared-grid-linear',
                implementation_version='1',
                provenance=provenance,
            ),
        ),
        uncertainty=(
            EquipmentUncertainty(
                quantity='sensitivity',
                unit='dB',
                model='bounded',
                lower=-1.0,
                upper=1.0,
                provenance=provenance,
            ),
        ),
    )


def _repository(tmp_path: Path, *, db_name: str = 'cad.sqlite3'):
    scene_repository = SceneRepository(tmp_path / db_name)
    scene_repository.save(
        SceneDocument(
            document_id='equipment-evidence-fixture',
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
            entities=(),
        ),
        parent_revision_id=None,
    )
    return CadEquipmentRepository(scene_repository)


def _save_manual(repository: CadEquipmentRepository, definition) -> None:
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='evidence-test-fixture',
        recorded_at_utc=NOW,
    ):
        repository.save_evidence(evidence)
    repository.save_definition(definition)


def _managed_source(definition, provenance):
    """Retained normalized-JSON source bytes for the whole definition subject."""
    subject = equipment_evidence_subject(definition)
    source_bytes = _canonical(
        {
            'schema': EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            'subject': subject.model_dump(mode='json'),
        }
    ).encode('utf-8')
    evidence = build_equipment_evidence_authority(
        definition=definition,
        provenance=provenance,
        subject=subject,
        managed_asset=ManagedEquipmentSourceAsset(
            source_asset_sha256=sha256(source_bytes).hexdigest(),
            size_bytes=len(source_bytes),
            extractor_id=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
            extractor_version=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
            filename='monitor-x.evidence.json',
            media_type='application/json',
            declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            datum_locator='datasheet page 4, table 2',
        ),
    )
    return evidence, source_bytes


def _managed_definition(tmp_path: Path):
    """Definition + retained managed source evidence bound to its exact bytes."""
    # The retained source document carries the normalized subject, so the
    # provenance source hash can only be fixed once the values are: build the
    # definition with a placeholder hash, derive the subject, then rebuild the
    # same definition citing the real content-addressed source bytes.
    draft = _definition()
    subject = equipment_evidence_subject(draft)
    source_bytes = _canonical(
        {
            'schema': EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            'subject': subject.model_dump(mode='json'),
        }
    ).encode('utf-8')
    digest = sha256(source_bytes).hexdigest()
    definition = _definition(source_sha256=digest)
    assert equipment_evidence_subject(definition) == subject
    evidence = build_equipment_evidence_authority(
        definition=definition,
        provenance=definition.provenance[0],
        subject=subject,
        managed_asset=ManagedEquipmentSourceAsset(
            source_asset_sha256=digest,
            size_bytes=len(source_bytes),
            extractor_id=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
            extractor_version=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
            filename='monitor-x.evidence.json',
            media_type='application/json',
            declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            datum_locator='datasheet page 4, table 2',
        ),
    )
    repository = _repository(tmp_path)
    return repository, definition, evidence, source_bytes


def test_fabricated_source_sha_cannot_authorize_capability_claims(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    definition = _definition(source_sha256='f' * 64)

    # A self-consistent definition with an invented source SHA is still just a
    # claim: no evidence resolves, so save and every capability gate fail.
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(definition)
    assert repository.get_definition(
        definition.definition_id, definition.version
    ) is None
    assert repository.get_definition_by_hash(definition.semantic_sha256) is None

    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.evaluate_capability(definition, 'sensitivity_reference')
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.evaluate_capability(definition, 'continuous_spl')
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.capability_matrix(definition)


def test_manual_evidence_resolves_every_sourced_field_group(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    authorities = build_equipment_manual_evidence(
        definition,
        actor='evidence-test-fixture',
        recorded_at_utc=NOW,
        confidence='datasheet transcription',
    )
    assert len(authorities) == 1
    evidence = authorities[0]
    assert evidence.authority_kind == 'manual_entry'
    assert evidence.manual is not None
    assert evidence.manual.actor == 'evidence-test-fixture'
    assert evidence.equipment_definition_sha256 == definition.semantic_sha256

    repository.save_evidence(evidence)
    repository.save_definition(definition)

    resolved = repository.resolve_definition_evidence(definition)
    assert resolved.equipment_definition_sha256 == definition.semantic_sha256
    assert resolved.authorities == (evidence,)
    supported = {ref.field_group for ref in resolved.field_evidence}
    assert supported == set(equipment_field_group_subjects(definition))
    assert all(
        ref.evidence_id == evidence.evidence_id
        and ref.evidence_sha256 == evidence.evidence_sha256
        for ref in resolved.field_evidence
    )

    # Authoritative reads re-resolve and re-derive the same records.
    reopened = CadEquipmentRepository(SceneRepository(repository.path))
    assert reopened.get_definition(
        definition.definition_id, definition.version
    ) == definition
    assert reopened.get_definition_by_hash(
        definition.semantic_sha256
    ) == definition
    assert reopened.list_definitions() == (definition,)
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.list_evidence_for_definition(
        definition.semantic_sha256
    ) == (evidence,)
    assert (
        reopened.evaluate_capability(definition, 'sensitivity_reference').decision
        == 'SUPPORTED'
    )
    assert (
        reopened.evaluate_capability(definition, 'continuous_spl').decision
        == 'SUPPORTED'
    )


def test_evidence_subject_divergence_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    provenance = definition.provenance[0]

    # Evidence whose recorded subject asserts a different sensitivity can be
    # built and persisted before the definition exists — but it can never
    # authorize the definition it is bound to.
    divergent_subject = EquipmentEvidenceSubject(
        sensitivity_reference=EquipmentSensitivitySubject(
            level_db_spl=99.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
        )
    )
    divergent = build_equipment_evidence_authority(
        definition=definition,
        provenance=provenance,
        subject=divergent_subject,
        manual=build_equipment_manual_evidence(
            definition,
            actor='evidence-test-fixture',
            recorded_at_utc=NOW,
        )[0].manual,
    )
    repository.save_evidence(divergent)
    with pytest.raises(
        ValueError, match='diverges from the bound EquipmentDefinition'
    ):
        repository.save_definition(definition)
    assert (
        repository.get_definition(definition.definition_id, definition.version)
        is None
    )

    # Once the definition is persisted, a divergent subject is rejected at
    # evidence-save time too (fresh store: the divergent row above already
    # occupies this definition's provenance slot).
    repository = _repository(tmp_path / 'persisted')
    _save_manual(repository, definition)
    with pytest.raises(
        ValueError, match='diverges from the bound EquipmentDefinition'
    ):
        repository.save_evidence(
            build_equipment_evidence_authority(
                definition=definition,
                provenance=provenance,
                subject=divergent_subject,
                manual=build_equipment_manual_evidence(
                    definition,
                    actor='evidence-test-fixture',
                    recorded_at_utc=NOW,
                )[0].manual,
            )
        )


def test_evidence_cannot_be_retargeted_across_definitions(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _definition()
    _save_manual(repository, first)

    # A second definition citing an identical provenance claim still has no
    # evidence of its own: retained evidence is bound to one exact definition
    # SHA-256, so the lookup for this definition misses.
    second = _definition(
        definition_id='example-audio-monitor-y',
        version='datasheet-2026-10',
    )
    assert second.semantic_sha256 != first.semantic_sha256
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(second)
    assert repository.list_evidence_for_definition(second.semantic_sha256) == ()

    # Evidence bound to a persisted definition hash but naming a different
    # id/version is rejected outright.
    base = repository.list_evidence_for_definition(first.semantic_sha256)[0]
    payload = base.model_dump(mode='json')
    payload['equipment_definition_id'] = 'example-audio-monitor-y'
    core = {
        key: value
        for key, value in payload.items()
        if key not in {'evidence_id', 'evidence_sha256'}
    }
    forged_digest = _digest(core)
    payload['evidence_sha256'] = forged_digest
    payload['evidence_id'] = f'equipment-evidence:{forged_digest}'
    forged = EquipmentEvidenceAuthority.model_validate(payload)
    with pytest.raises(
        ValueError, match='equipment evidence definition identity mismatch'
    ):
        repository.save_evidence(forged)

    # A second authority for an already-covered provenance claim fails closed.
    replacement = build_equipment_evidence_authority(
        definition=first,
        provenance=first.provenance[0],
        subject=equipment_evidence_subject(first),
        external=ExternalEquipmentAuthority(
            authority_uri='https://example.invalid/datasheet.pdf',
            locator='page 4, table 2',
        ),
    )
    with pytest.raises(ValueError, match='already retained'):
        repository.save_evidence(replacement)


def test_evidence_must_be_cited_by_the_bound_definition() -> None:
    definition = _definition()
    foreign = _provenance(
        evidence_kind='measured', source_name='Unrelated lab', source_hash='9' * 64
    )
    with pytest.raises(ValueError, match='not cited by the bound'):
        build_equipment_evidence_authority(
            definition=definition,
            provenance=foreign,
            subject=equipment_evidence_subject(
                definition, groups=('sensitivity_reference',)
            ),
            manual=build_equipment_manual_evidence(
                definition,
                actor='evidence-test-fixture',
                recorded_at_utc=NOW,
            )[0].manual,
        )


def test_missing_evidence_fails_closed_on_every_authoritative_read(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    _save_manual(repository, definition)
    evidence = repository.list_evidence_for_definition(
        definition.semantic_sha256
    )[0]

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_equipment_evidence_authorities WHERE evidence_id=?',
            (evidence.evidence_id,),
        )

    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.get_definition(definition.definition_id, definition.version)
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.get_definition_by_hash(definition.semantic_sha256)
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.list_definitions()
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.resolve_definition_evidence(definition)
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.evaluate_capability(definition, 'sensitivity_reference')
    # Even an idempotent re-save re-resolves instead of succeeding silently.
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(definition)


def test_tampered_evidence_payload_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    _save_manual(repository, definition)
    evidence = repository.list_evidence_for_definition(
        definition.semantic_sha256
    )[0]

    payload = evidence.model_dump(mode='json')
    payload['subject']['sensitivity_reference']['level_db_spl'] = 99.0
    payload['evidence_id'] = evidence.evidence_id
    payload['evidence_sha256'] = evidence.evidence_sha256
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_equipment_evidence_authorities SET payload_json=? '
            'WHERE evidence_id=?',
            (json.dumps(payload), evidence.evidence_id),
        )

    with pytest.raises(ValueError):
        repository.get_evidence(evidence.evidence_id)
    with pytest.raises(ValueError):
        repository.get_definition(definition.definition_id, definition.version)


def test_field_group_coverage_is_enforced_per_claim(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    provenance = definition.provenance[0]
    manual = build_equipment_manual_evidence(
        definition, actor='evidence-test-fixture', recorded_at_utc=NOW
    )[0].manual

    # Definition-level provenance whose evidence only supports identity leaves
    # the structural groups unevidenced.
    identity_only = build_equipment_evidence_authority(
        definition=definition,
        provenance=provenance,
        subject=equipment_evidence_subject(definition, groups=('identity',)),
        manual=manual,
    )
    repository.save_evidence(identity_only)
    with pytest.raises(
        ValueError, match="field group 'cabinet_geometry'"
    ):
        repository.save_definition(definition)


def test_managed_source_asset_replays_subject_on_every_read(
    tmp_path: Path,
) -> None:
    repository, definition, evidence, source_bytes = _managed_definition(tmp_path)

    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename='monitor-x.evidence.json',
        media_type='application/json',
        declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
    )
    repository.save_definition(definition)

    digest = evidence.managed_asset.source_asset_sha256
    assert repository.read_source_asset(digest) == source_bytes
    assert (repository.assets_dir / digest).read_bytes() == source_bytes

    reopened = CadEquipmentRepository(SceneRepository(repository.path))
    assert reopened.get_definition(
        definition.definition_id, definition.version
    ) == definition
    assert reopened.resolve_definition_evidence(definition).authorities == (
        evidence,
    )

    # Tampered retained bytes fail closed on every authoritative read.
    (repository.assets_dir / digest).write_bytes(b'0' * len(source_bytes))
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        reopened.get_definition(definition.definition_id, definition.version)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        reopened.get_evidence(evidence.evidence_id)


def test_managed_source_must_replay_its_recorded_subject(
    tmp_path: Path,
) -> None:
    repository, definition, evidence, source_bytes = _managed_definition(tmp_path)

    # The bound managed asset hash must equal the cited provenance source hash.
    other_bytes = _canonical(
        {
            'schema': EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            'subject': equipment_evidence_subject(
                _definition(sensitivity_db=91.0)
            ).model_dump(mode='json'),
        }
    ).encode('utf-8')
    with pytest.raises(
        ValueError, match='does not match the provenance source_sha256'
    ):
        build_equipment_evidence_authority(
            definition=definition,
            provenance=definition.provenance[0],
            subject=evidence.subject,
            managed_asset=ManagedEquipmentSourceAsset(
                source_asset_sha256=sha256(other_bytes).hexdigest(),
                size_bytes=len(other_bytes),
                extractor_id=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
                extractor_version=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
                filename='monitor-x.evidence.json',
                declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            ),
        )

    # Retained bytes whose replayed subject does not equal the recorded
    # subject are rejected before anything is committed: the managed file is
    # installed but the evidence row never lands, so the definition still has
    # no resolvable evidence.
    mismatched_subject = EquipmentEvidenceSubject(
        sensitivity_reference=EquipmentSensitivitySubject(
            level_db_spl=97.0,
            input_quantity='voltage_v_rms',
            input_value=2.83,
            distance_m=1.0,
        )
    )
    mismatched_doc = _canonical(
        {
            'schema': EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            'subject': mismatched_subject.model_dump(mode='json'),
        }
    ).encode('utf-8')
    mismatched_digest = sha256(mismatched_doc).hexdigest()
    mismatched_definition = _definition(
        definition_id='example-audio-monitor-w',
        version='datasheet-2026-12',
        source_sha256=mismatched_digest,
    )
    forged = build_equipment_evidence_authority(
        definition=mismatched_definition,
        provenance=mismatched_definition.provenance[0],
        subject=equipment_evidence_subject(mismatched_definition),
        managed_asset=ManagedEquipmentSourceAsset(
            source_asset_sha256=mismatched_digest,
            size_bytes=len(mismatched_doc),
            extractor_id=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
            extractor_version=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
            filename='monitor-w.evidence.json',
            declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
        ),
    )
    with pytest.raises(ValueError, match='does not replay'):
        repository.save_evidence(
            forged,
            source_bytes=mismatched_doc,
            source_filename='monitor-w.evidence.json',
            declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
        )
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(mismatched_definition)

    # Source bytes that do not even hash to the bound digest are rejected.
    with pytest.raises(ValueError, match='do not match'):
        repository.save_evidence(
            evidence,
            source_bytes=b'not the source',
            source_filename='monitor-x.evidence.json',
        )

    # Unregistered extractor ids can never replay.
    with pytest.raises(
        ValueError, match='no supported equipment evidence extractor'
    ):
        replay_equipment_evidence_extraction(
            source_bytes=source_bytes,
            extractor_id='unregistered-extractor',
            extractor_version='1',
        )


def test_external_source_authority_roundtrip(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    provenance = definition.provenance[0]
    evidence = build_equipment_evidence_authority(
        definition=definition,
        provenance=provenance,
        subject=equipment_evidence_subject(definition),
        external=ExternalEquipmentAuthority(
            authority_uri='https://example.invalid/datasheet-2026-09.pdf',
            locator='page 4, table 2, row sensitivity',
            retrieved_at_utc=NOW,
            note='manufacturer-hosted datasheet snapshot',
        ),
    )
    repository.save_evidence(evidence)
    repository.save_definition(definition)

    reopened = CadEquipmentRepository(SceneRepository(repository.path))
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.get_definition(
        definition.definition_id, definition.version
    ) == definition

    # External/manual records cannot smuggle raw source bytes.
    with pytest.raises(
        ValueError, match='cannot carry raw source bytes'
    ):
        repository.save_evidence(
            build_equipment_evidence_authority(
                definition=definition,
                provenance=provenance,
                subject=equipment_evidence_subject(
                    definition, groups=('identity',)
                ),
                external=ExternalEquipmentAuthority(
                    authority_uri='https://example.invalid/other.pdf',
                    locator='page 1',
                ),
            ),
            source_bytes=b'raw bytes',
            source_filename='other.pdf',
        )


def test_evidence_authority_identity_is_content_addressed() -> None:
    definition = _definition()
    evidence = build_equipment_manual_evidence(
        definition, actor='evidence-test-fixture', recorded_at_utc=NOW
    )[0]
    assert evidence.evidence_id == f'equipment-evidence:{evidence.evidence_sha256}'

    # Any retargeted field invalidates the self-hash.
    payload = evidence.model_dump(mode='json')
    payload['manual']['actor'] = 'someone-else'
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        EquipmentEvidenceAuthority.model_validate(payload)

    payload = evidence.model_dump(mode='json')
    payload['evidence_id'] = 'equipment-evidence:' + '0' * 64
    with pytest.raises(ValidationError, match='id mismatch'):
        EquipmentEvidenceAuthority.model_validate(payload)


def test_uncertainty_items_resolve_per_provenance_claim(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first_claim = _provenance(source_hash='a' * 64)
    second_claim = _provenance(
        evidence_kind='measured',
        source_name='Independent lab report',
        source_hash='b' * 64,
    )
    definition = build_equipment_definition(
        definition_id='example-audio-monitor-x',
        version='datasheet-2026-09',
        identity_kind='manufacturer',
        manufacturer='Example Audio',
        model='Monitor X',
        provenance=(first_claim, second_claim),
        cabinet_envelope_m=Size3(x_m=0.22, y_m=0.31, z_m=0.38),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown', data_format='unknown', provenance=first_claim
        ),
        uncertainty=(
            EquipmentUncertainty(
                quantity='sensitivity',
                unit='dB',
                model='bounded',
                lower=-1.0,
                upper=1.0,
                provenance=first_claim,
            ),
            EquipmentUncertainty(
                quantity='max_spl',
                unit='dB',
                model='standard_uncertainty',
                standard_uncertainty=0.5,
                provenance=second_claim,
            ),
        ),
    )
    authorities = build_equipment_manual_evidence(
        definition, actor='evidence-test-fixture', recorded_at_utc=NOW
    )
    assert len(authorities) == 2
    by_provenance = {
        item.provenance.source_name: item for item in authorities
    }
    first_evidence = by_provenance['Example Audio datasheet']
    second_evidence = by_provenance['Independent lab report']
    assert 'uncertainty' in first_evidence.subject.supported_groups()
    assert 'uncertainty' in second_evidence.subject.supported_groups()
    assert [item.quantity for item in first_evidence.subject.uncertainty] == [
        'sensitivity'
    ]
    assert [item.quantity for item in second_evidence.subject.uncertainty] == [
        'max_spl'
    ]

    # Evidence covering a different uncertainty item cannot support this one.
    wrong_item = EquipmentEvidenceSubject(
        identity=equipment_evidence_subject(definition).identity,
        uncertainty=(
            EquipmentUncertaintySubject(
                quantity='sensitivity',
                unit='dB',
                model='bounded',
                lower=-1.0,
                upper=1.0,
            ),
        ),
    )
    incomplete = build_equipment_evidence_authority(
        definition=definition,
        provenance=second_claim,
        subject=wrong_item,
        manual=second_evidence.manual,
    )
    repository.save_evidence(first_evidence)
    repository.save_evidence(incomplete)
    with pytest.raises(
        ValueError, match='not supported by the retained evidence'
    ):
        repository.save_definition(definition)


def test_native_backup_retains_equipment_evidence_for_replay(
    tmp_path: Path,
) -> None:
    # The canonical backup database name is required by native_backup.
    data_dir = tmp_path / 'data'
    scene_repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    scene_repository.save(
        SceneDocument(
            document_id='equipment-evidence-fixture',
            room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
            entities=(),
        ),
        parent_revision_id=None,
    )
    repository = CadEquipmentRepository(scene_repository)
    draft = _definition()
    subject = equipment_evidence_subject(draft)
    source_bytes = _canonical(
        {
            'schema': EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            'subject': subject.model_dump(mode='json'),
        }
    ).encode('utf-8')
    digest = sha256(source_bytes).hexdigest()
    definition = _definition(source_sha256=digest)
    evidence = build_equipment_evidence_authority(
        definition=definition,
        provenance=definition.provenance[0],
        subject=subject,
        managed_asset=ManagedEquipmentSourceAsset(
            source_asset_sha256=digest,
            size_bytes=len(source_bytes),
            extractor_id=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_ID,
            extractor_version=EQUIPMENT_EVIDENCE_JSON_EXTRACTOR_VERSION,
            filename='monitor-x.evidence.json',
            media_type='application/json',
            declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
            datum_locator='datasheet page 4, table 2',
        ),
    )
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename='monitor-x.evidence.json',
        media_type='application/json',
        declared_schema=EQUIPMENT_EVIDENCE_JSON_SCHEMA,
    )
    repository.save_definition(definition)

    backup_path = tmp_path / 'equipment.htdt-backup'
    manifest = create_backup(data_dir, backup_path)
    asset_entry = next(
        entry
        for entry in manifest.files
        if entry.path == f'measurement-assets/{digest}'
    )
    assert asset_entry.sha256 == digest
    assert validate_backup(backup_path) == manifest

    restored_dir = tmp_path / 'restored'
    restore_backup(restored_dir, backup_path)
    reopened = CadEquipmentRepository(
        SceneRepository(restored_dir / 'cad-scenes.sqlite3')
    )
    assert reopened.get_definition(
        definition.definition_id, definition.version
    ) == definition
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.read_source_asset(digest) == source_bytes
    assert reopened.resolve_definition_evidence(definition).authorities == (
        evidence,
    )


def test_save_evidence_idempotent_and_column_integrity(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    definition = _definition()
    evidence = build_equipment_manual_evidence(
        definition, actor='evidence-test-fixture', recorded_at_utc=NOW
    )[0]
    assert repository.save_evidence(evidence) == evidence
    assert repository.save_evidence(evidence) == evidence

    # Column drift against the self-hashed payload fails closed.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_equipment_evidence_authorities '
            'SET subject_sha256=? WHERE evidence_id=?',
            ('0' * 64, evidence.evidence_id),
        )
    with pytest.raises(ValueError, match='column mismatch'):
        repository.get_evidence(evidence.evidence_id)


def test_definition_semantic_identity_unchanged_by_evidence(
    tmp_path: Path,
) -> None:
    # Evidence resolution never mutates the definition's semantic identity.
    repository = _repository(tmp_path)
    definition = _definition()
    _save_manual(repository, definition)
    persisted = repository.get_definition(
        definition.definition_id, definition.version
    )
    assert persisted == definition
    assert persisted.semantic_sha256 == definition.semantic_sha256
