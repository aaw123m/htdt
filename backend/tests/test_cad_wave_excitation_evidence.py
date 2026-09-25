from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_equipment import (
    DirectivityCapability,
    EquipmentDataProvenance,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Direction3, Offset3, Size3
from htdt.cad_source_response import (
    SourceResponseCondition,
    SourceResponseSample,
    build_source_response,
    source_response_wave_excitation_eligible,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_wave_excitation import (
    WAVE_EXCITATION_CONSTANT_MODEL_ID,
    WAVE_EXCITATION_CONSTANT_MODEL_VERSION,
    WAVE_EXCITATION_EVIDENCE_AUTHORITY_VERSION,
    WAVE_EXCITATION_TABLE_CONVERTER_ID,
    WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
    WAVE_EXCITATION_TABLE_SCHEMA,
    AcousticWaveExcitationAuthority,
    CadWaveExcitationRepository,
    ComplexVolumeVelocitySample,
    WaveExcitationAnalyticDerivation,
    WaveExcitationEvidenceAuthority,
    WaveExcitationEvidenceSubject,
    WaveExcitationManualDerivation,
    WaveExcitationSourceAssetDerivation,
    WaveExcitationSourceResponseDerivation,
    _digest,
    build_acoustic_wave_excitation_authority,
    build_wave_excitation_evidence_authority,
    derive_wave_excitation_evidence_from_source_response,
    replay_wave_excitation_source_derivation,
)
from htdt.native_backup import create_backup, restore_backup, validate_backup
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


NOW = '2026-10-01T00:00:00+00:00'
SOURCE_FILENAME = 'speaker-excitation.vv-table.json'
SOURCE_MEDIA_TYPE = 'application/json'
DEFAULT_PARAMETERS = {
    'frequency_unit': 'Hz',
    'value_unit': 'm3_s',
    'value_form': 'rectangular',
}
DEFAULT_ROWS = [
    {'frequency': 100.0, 'real': 1.0e-4, 'imag': 0.0},
    {'frequency': 200.0, 'real': 8.0e-5, 'imag': -2.0e-5},
]


def _source_bytes(rows: list[dict] | None = None) -> bytes:
    return json.dumps(
        {
            'schema': WAVE_EXCITATION_TABLE_SCHEMA,
            'rows': DEFAULT_ROWS if rows is None else rows,
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _definition(definition_id: str = 'evidence-fixture-speaker'):
    provenance = EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='fixture-equipment-identity',
        source_version='1',
        source_reference='issue409 evidence fixture',
        source_sha256='0' * 64,
    )
    return build_equipment_definition(
        definition_id=definition_id,
        version='1',
        identity_kind='user_defined',
        user_label=definition_id,
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=provenance,
        ),
    )


def _repositories(tmp_path: Path, *, db_name: str = 'cad.sqlite3'):
    scene_repository = SceneRepository(tmp_path / db_name)
    equipment_repository = CadEquipmentRepository(scene_repository)
    wave_repository = CadWaveExcitationRepository(
        scene_repository,
        equipment_repository=equipment_repository,
    )
    return scene_repository, equipment_repository, wave_repository


def _save_definition(equipment_repository, definition) -> None:
    """Persist manual evidence for every cited provenance, then the definition.

    ``save_definition`` requires each provenance claim to resolve to a
    retained ``EquipmentEvidenceAuthority``, so the fixture records the
    manual-entry authorities first.
    """
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='wave-excitation-test-fixture',
        recorded_at_utc=NOW,
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)


def _imported(
    definition,
    *,
    rows: list[dict] | None = None,
    parameters: dict | None = None,
    evidence_kind: str = 'measured',
    source_name: str = 'issue409 imported fixture',
):
    source_bytes = _source_bytes(rows)
    parameters = (
        dict(DEFAULT_PARAMETERS) if parameters is None else dict(parameters)
    )
    samples = replay_wave_excitation_source_derivation(
        source_bytes=source_bytes,
        converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
        converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
        conversion_parameters=parameters,
    )
    evidence = build_wave_excitation_evidence_authority(
        evidence_kind=evidence_kind,
        source_name=source_name,
        source_version='1',
        source_reference='issue409 retained source asset',
        derivation=WaveExcitationSourceAssetDerivation(
            source_asset_sha256=sha256(source_bytes).hexdigest(),
            converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
            converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
            conversion_parameters=parameters,
        ),
        subject=WaveExcitationEvidenceSubject(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_sha256=definition.semantic_sha256,
            samples=samples,
        ),
    )
    return source_bytes, evidence, samples


def _excitation(definition, evidence, samples):
    return build_acoustic_wave_excitation_authority(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_sha256=definition.semantic_sha256,
        samples=samples,
        interpolation=InterpolationProvenance(
            method='linear',
            implementation='issue409-fixture-linear',
            implementation_version='1',
            provenance=evidence.provenance,
        ),
        provenance=(evidence.provenance,),
        evidence=(evidence,),
        approximation_note='issue409 evidence replay fixture',
    )


def _manual_evidence(definition, samples, *, source_name='issue409 manual fixture'):
    return build_wave_excitation_evidence_authority(
        evidence_kind='user_defined',
        source_name=source_name,
        source_version='1',
        source_reference='manual entry record',
        derivation=WaveExcitationManualDerivation(
            author='fixture-author',
            authored_at_utc=NOW,
        ),
        subject=WaveExcitationEvidenceSubject(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_sha256=definition.semantic_sha256,
            samples=samples,
        ),
    )


def _reforged(excitation, **updates) -> AcousticWaveExcitationAuthority:
    """Self-consistent forgery: claimed semantics with a recomputed identity."""
    forged = excitation.model_copy(update=updates)
    digest = _digest(forged.semantic_payload())
    return AcousticWaveExcitationAuthority.model_validate(
        {
            **forged.model_dump(mode='python'),
            'semantic_sha256': digest,
            'excitation_id': f'acoustic-wave-excitation:{digest}',
        }
    )


def _asset_rows(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            'SELECT sha256, filename, relative_path, size_bytes '
            'FROM cad_measurement_assets ORDER BY sha256'
        ).fetchall()


def _metadata_rows(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            'SELECT source_asset_sha256, filename, media_type, declared_schema '
            'FROM cad_wave_excitation_source_assets ORDER BY source_asset_sha256'
        ).fetchall()


def _evidence_rows(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            'SELECT evidence_id, evidence_sha256, evidence_kind, source_sha256 '
            'FROM cad_wave_excitation_evidence_authorities ORDER BY seq'
        ).fetchall()


def test_imported_excitation_persists_reopens_and_replays(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)

    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
        media_type=SOURCE_MEDIA_TYPE,
        declared_schema=WAVE_EXCITATION_TABLE_SCHEMA,
    )
    repository.save_excitation(excitation)

    digest = evidence.derivation.source_asset_sha256
    asset_file = repository.assets_dir / digest
    assert asset_file.read_bytes() == source_bytes
    assert _asset_rows(repository.path) == [
        (
            digest,
            SOURCE_FILENAME,
            str(Path('measurement-assets') / digest),
            len(source_bytes),
        )
    ]
    assert _metadata_rows(repository.path) == [
        (digest, SOURCE_FILENAME, SOURCE_MEDIA_TYPE, WAVE_EXCITATION_TABLE_SCHEMA)
    ]
    assert _evidence_rows(repository.path) == [
        (
            evidence.evidence_id,
            evidence.evidence_sha256,
            'measured',
            digest,
        )
    ]

    assert repository.read_source_asset(digest) == source_bytes
    metadata = repository.get_source_metadata(digest)
    assert metadata is not None
    assert metadata.filename == SOURCE_FILENAME
    assert metadata.media_type == SOURCE_MEDIA_TYPE
    assert metadata.declared_schema == WAVE_EXCITATION_TABLE_SCHEMA
    assert metadata.size_bytes == len(source_bytes)

    assert repository.get_evidence(evidence.evidence_id) == evidence
    assert repository.resolve_evidence(evidence.as_external_ref()) == evidence
    assert repository.get_excitation(excitation.excitation_id) == excitation
    assert (
        repository.get_excitation_by_hash(excitation.semantic_sha256)
        == excitation
    )


def test_identical_evidence_and_source_asset_are_stored_once(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)

    for _ in range(2):
        repository.save_evidence(
            evidence,
            source_bytes=source_bytes,
            source_filename=SOURCE_FILENAME,
            media_type=SOURCE_MEDIA_TYPE,
            declared_schema=WAVE_EXCITATION_TABLE_SCHEMA,
        )
        repository.save_excitation(excitation)

    digest = evidence.derivation.source_asset_sha256
    assert [path.name for path in repository.assets_dir.iterdir()] == [digest]
    assert len(_asset_rows(repository.path)) == 1
    assert len(_metadata_rows(repository.path)) == 1
    assert len(_evidence_rows(repository.path)) == 1

    # A resave may bind the already-managed asset without resupplying bytes.
    assert repository.save_evidence(evidence) == evidence
    assert repository.save_excitation(excitation) == excitation


def test_save_evidence_without_source_bytes_requires_managed_asset(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)

    with pytest.raises(ValueError, match='not registered'):
        repository.save_evidence(evidence)
    assert _evidence_rows(repository.path) == []

    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )
    assert repository.get_evidence(evidence.evidence_id) == evidence


def test_source_bytes_must_match_evidence_digest(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, _samples = _imported(definition)

    with pytest.raises(ValueError, match='do not match'):
        repository.save_evidence(
            evidence,
            source_bytes=source_bytes + b' ',
            source_filename=SOURCE_FILENAME,
        )
    with pytest.raises(ValueError, match='filename is required'):
        repository.save_evidence(
            evidence,
            source_bytes=source_bytes,
        )
    assert _evidence_rows(repository.path) == []


def test_fabricated_source_sha_cannot_authorize_excitation(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )

    # A naked fabricated source hash with an invented evidence ref never
    # resolves to a retained authority.
    fabricated_claim = EquipmentDataProvenance(
        evidence_kind='measured',
        source_name='fabricated source',
        source_version='9',
        source_reference='invented measurement',
        source_sha256='b' * 64,
    )
    fabricated_ref = ExactExternalAuthorityRef(
        authority_id=f'wave-excitation-evidence:{"b" * 64}',
        authority_version=WAVE_EXCITATION_EVIDENCE_AUTHORITY_VERSION,
        semantic_hash_sha256='b' * 64,
    )
    forged = _reforged(
        excitation,
        provenance=(fabricated_claim,),
        source_evidence=(fabricated_ref,),
        interpolation=excitation.interpolation.model_copy(
            update={'provenance': fabricated_claim}
        ),
    )
    with pytest.raises(ValueError, match='does not resolve'):
        repository.save_excitation(forged)
    assert repository.get_excitation(forged.excitation_id) is None

    # Pointing at the real evidence while claiming different provenance fields
    # fails closed on the provenance/evidence mismatch.
    mislabeled = _reforged(
        excitation,
        provenance=(fabricated_claim,),
        source_evidence=(evidence.as_external_ref(),),
        interpolation=excitation.interpolation.model_copy(
            update={'provenance': fabricated_claim}
        ),
    )
    with pytest.raises(
        ValueError, match='does not match the retained evidence'
    ):
        repository.save_excitation(mislabeled)

    # An evidence authority cannot even be constructed when its provenance
    # hash does not commit to the retained source material.
    with pytest.raises(ValueError, match='does not commit'):
        WaveExcitationEvidenceAuthority(
            evidence_id=f'wave-excitation-evidence:{"c" * 64}',
            evidence_sha256='c' * 64,
            provenance=fabricated_claim,
            derivation=evidence.derivation,
            subject=evidence.subject,
        )


def test_missing_or_tampered_source_asset_fails_closed(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )
    repository.save_excitation(excitation)

    digest = evidence.derivation.source_asset_sha256
    asset_file = repository.assets_dir / digest
    # Same-length tampering isolates digest verification from size checks.
    asset_file.write_bytes(b'x' * len(source_bytes))
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_excitation(excitation.excitation_id)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_evidence(evidence.evidence_id)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.read_source_asset(digest)

    asset_file.unlink()
    with pytest.raises(ValueError, match='missing'):
        repository.get_excitation(excitation.excitation_id)
    with pytest.raises(ValueError, match='missing'):
        repository.get_evidence(evidence.evidence_id)


def test_persisted_excitation_must_replay_from_evidence(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )
    repository.save_excitation(excitation)

    # Swap in a self-consistent excitation claiming the same evidence while
    # carrying different samples: only derivation replay can prove the stored
    # samples do not derive from the retained evidence.
    divergent_samples = (
        samples[0],
        ComplexVolumeVelocitySample(
            frequency_hz=200.0,
            real_m3_s=9.0e-5,
            imag_m3_s=-2.0e-5,
        ),
    )
    forged = _reforged(excitation, samples=divergent_samples)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_acoustic_wave_excitations '
            'SET excitation_id=?, semantic_sha256=?, payload_json=? '
            'WHERE excitation_id=?',
            (
                forged.excitation_id,
                forged.semantic_sha256,
                forged.model_dump_json(),
                excitation.excitation_id,
            ),
        )
    with pytest.raises(ValueError, match='does not support the persisted'):
        repository.get_excitation(forged.excitation_id)


def test_unregistered_converter_or_model_fails_closed(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, _samples = _imported(definition)

    stale_derivation = WaveExcitationSourceAssetDerivation(
        source_asset_sha256=evidence.derivation.source_asset_sha256,
        converter_id=evidence.derivation.converter_id,
        converter_version='999',
        conversion_parameters=evidence.derivation.conversion_parameters,
    )
    stale_evidence = build_wave_excitation_evidence_authority(
        evidence_kind='measured',
        source_name='issue409 imported fixture',
        source_version='1',
        source_reference='issue409 retained source asset',
        derivation=stale_derivation,
        subject=evidence.subject,
    )
    with pytest.raises(
        ValueError, match='no registered wave-excitation source converter'
    ):
        repository.save_evidence(
            stale_evidence,
            source_bytes=source_bytes,
            source_filename=SOURCE_FILENAME,
        )

    stale_model = build_wave_excitation_evidence_authority(
        evidence_kind='analytic',
        source_name='issue409 analytic fixture',
        source_version='1',
        source_reference='analytic fixture',
        derivation=WaveExcitationAnalyticDerivation(
            model_id=WAVE_EXCITATION_CONSTANT_MODEL_ID,
            model_version='999',
            model_parameters={
                'frequencies_hz': [100.0, 200.0],
                'real_m3_s': 1.0e-4,
                'imag_m3_s': 0.0,
            },
        ),
        subject=evidence.subject,
    )
    with pytest.raises(
        ValueError, match='no registered wave-excitation analytic model'
    ):
        repository.save_evidence(stale_model)


def test_analytic_excitation_regenerates_and_reopens(tmp_path: Path) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)

    parameters = {
        'frequencies_hz': [100.0, 200.0],
        'real_m3_s': 1.0e-4,
        'imag_m3_s': -2.0e-5,
    }
    derivation = WaveExcitationAnalyticDerivation(
        model_id=WAVE_EXCITATION_CONSTANT_MODEL_ID,
        model_version=WAVE_EXCITATION_CONSTANT_MODEL_VERSION,
        model_parameters=parameters,
    )
    samples = (
        ComplexVolumeVelocitySample(
            frequency_hz=100.0, real_m3_s=1.0e-4, imag_m3_s=-2.0e-5
        ),
        ComplexVolumeVelocitySample(
            frequency_hz=200.0, real_m3_s=1.0e-4, imag_m3_s=-2.0e-5
        ),
    )
    evidence = build_wave_excitation_evidence_authority(
        evidence_kind='analytic',
        source_name='issue409 analytic fixture',
        source_version='1',
        source_reference='analytic piston-equivalent fixture',
        derivation=derivation,
        subject=WaveExcitationEvidenceSubject(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_sha256=definition.semantic_sha256,
            samples=samples,
        ),
    )
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(evidence)
    repository.save_excitation(excitation)

    # The analytic provenance hash commits to the exact model specification.
    assert evidence.provenance.source_sha256 == _digest(
        {
            'derivation': 'analytic_model',
            'model_id': WAVE_EXCITATION_CONSTANT_MODEL_ID,
            'model_version': WAVE_EXCITATION_CONSTANT_MODEL_VERSION,
            'model_parameters': parameters,
        }
    )
    assert repository.get_evidence(evidence.evidence_id) == evidence
    assert repository.get_excitation(excitation.excitation_id) == excitation

    # Analytic evidence carries no managed source asset.
    assert repository.read_source_asset(
        evidence.provenance.source_sha256
    ) is None

    # A subject the model cannot regenerate fails closed on save.
    wrong_subject_evidence = build_wave_excitation_evidence_authority(
        evidence_kind='analytic',
        source_name='issue409 analytic fixture',
        source_version='1',
        source_reference='analytic piston-equivalent fixture',
        derivation=derivation,
        subject=WaveExcitationEvidenceSubject(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_sha256=definition.semantic_sha256,
            samples=(
                ComplexVolumeVelocitySample(
                    frequency_hz=100.0, real_m3_s=2.0e-4, imag_m3_s=0.0
                ),
                ComplexVolumeVelocitySample(
                    frequency_hz=200.0, real_m3_s=2.0e-4, imag_m3_s=0.0
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match='does not replay'):
        repository.save_evidence(wrong_subject_evidence)


def test_manual_evidence_authority_retains_exact_values(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    samples = (
        ComplexVolumeVelocitySample(
            frequency_hz=40.0, real_m3_s=1.0e-4, imag_m3_s=0.0
        ),
        ComplexVolumeVelocitySample(
            frequency_hz=80.0, real_m3_s=0.0, imag_m3_s=1.0e-4
        ),
    )
    evidence = _manual_evidence(definition, samples)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(evidence)
    repository.save_excitation(excitation)

    # The manual provenance hash commits to author/time plus exact values —
    # it is the retained evidence, not a naked external hash.
    assert evidence.provenance.source_sha256 == _digest(
        {
            'derivation': 'manual',
            'author': 'fixture-author',
            'authored_at_utc': NOW,
            'subject': evidence.subject.model_dump(mode='json'),
        }
    )
    assert repository.resolve_evidence(
        evidence.as_external_ref()
    ) == evidence
    assert repository.get_excitation(excitation.excitation_id) == excitation

    # Manual evidence must not carry source bytes.
    with pytest.raises(ValueError, match='only valid for source-asset'):
        repository.save_evidence(evidence, source_bytes=b'payload')

    # A tampered evidence payload fails its own content address on read.
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_wave_excitation_evidence_authorities '
            'SET payload_json=? WHERE evidence_id=?',
            (
                evidence.model_copy(
                    update={
                        'derivation': WaveExcitationManualDerivation(
                            author='tampered-author',
                            authored_at_utc=NOW,
                        )
                    }
                ).model_dump_json(),
                evidence.evidence_id,
            ),
        )
    with pytest.raises(ValueError):
        repository.get_evidence(evidence.evidence_id)


def test_interpolation_provenance_resolves_to_retained_evidence(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )

    # An interpolation provenance naming a source that is not one of the
    # paired provenance claims cannot even form the authority.
    with pytest.raises(
        ValueError, match='interpolation provenance must resolve'
    ):
        _reforged(
            excitation,
            interpolation=excitation.interpolation.model_copy(
                update={
                    'provenance': EquipmentDataProvenance(
                        evidence_kind='measured',
                        source_name='unrelated source',
                        source_version='1',
                        source_reference='unrelated',
                        source_sha256='d' * 64,
                    )
                }
            ),
        )

    # A provenance claim whose paired ref resolves to evidence standing behind
    # different values fails closed at the excitation boundary.
    other_evidence = _manual_evidence(
        definition,
        (
            ComplexVolumeVelocitySample(
                frequency_hz=100.0, real_m3_s=5.0e-4, imag_m3_s=0.0
            ),
            ComplexVolumeVelocitySample(
                frequency_hz=200.0, real_m3_s=5.0e-4, imag_m3_s=0.0
            ),
        ),
        source_name='issue409 other fixture',
    )
    repository.save_evidence(other_evidence)
    mismatched = _reforged(
        excitation,
        provenance=(evidence.provenance,),
        source_evidence=(other_evidence.as_external_ref(),),
    )
    with pytest.raises(
        ValueError, match='does not match the retained evidence'
    ):
        repository.save_excitation(mismatched)


def test_wrong_equipment_subject_never_authorizes(tmp_path: Path) -> None:
    definition = _definition()
    other_definition = _definition('other-speaker')
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    _save_definition(equipment_repository, other_definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
    )

    # Evidence retained for one definition cannot back an excitation claiming
    # another, even if the excitation is internally self-consistent.
    forged = _reforged(
        excitation,
        definition_id=other_definition.definition_id,
        definition_sha256=other_definition.semantic_sha256,
    )
    with pytest.raises(
        ValueError, match='subject does not match'
    ):
        repository.save_excitation(forged)


def test_noncanonical_conversion_parameters_fail_closed() -> None:
    with pytest.raises(ValueError, match='canonical JSON'):
        WaveExcitationSourceAssetDerivation(
            source_asset_sha256='a' * 64,
            converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
            converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
            conversion_parameters={'calibration_scale': float('nan')},
        )


def test_table_converter_applies_pinned_unit_and_form_parameters() -> None:
    rows = [
        {'frequency': 0.1, 'magnitude': 100.0, 'phase_deg': 0.0},
        {'frequency': 0.2, 'magnitude': 50.0, 'phase_deg': -90.0},
    ]
    parameters = {
        'frequency_unit': 'kHz',
        'value_unit': 'cm3_s',
        'value_form': 'polar_deg',
        'calibration_scale': 2.0,
        'imaginary_sign': 'conjugate',
    }
    samples = replay_wave_excitation_source_derivation(
        source_bytes=_source_bytes(rows),
        converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
        converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
        conversion_parameters=parameters,
    )
    expected_real = 100.0 * 1e-6 * 2.0
    expected_imag = 50.0 * 1e-6 * 2.0  # -sin(-90°) after the conjugate flip
    assert samples[0].frequency_hz == 0.1 * 1000.0
    assert samples[0].real_m3_s == expected_real
    assert samples[0].imag_m3_s == 0.0
    assert samples[1].frequency_hz == 0.2 * 1000.0
    assert samples[1].imag_m3_s == expected_imag

    # Different pinned parameters derive different samples — the recorded
    # parameters are part of the evidence contract.
    rescaled = replay_wave_excitation_source_derivation(
        source_bytes=_source_bytes(rows),
        converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
        converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
        conversion_parameters={**parameters, 'value_unit': 'mm3_s'},
    )
    assert rescaled != samples

    with pytest.raises(ValueError, match='unknown wave-excitation conversion'):
        replay_wave_excitation_source_derivation(
            source_bytes=_source_bytes(rows),
            converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
            converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
            conversion_parameters={**parameters, 'invented': 1},
        )
    with pytest.raises(ValueError, match='missing wave-excitation conversion'):
        replay_wave_excitation_source_derivation(
            source_bytes=_source_bytes(rows),
            converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
            converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
            conversion_parameters={
                key: value
                for key, value in parameters.items()
                if key != 'value_unit'
            },
        )
    with pytest.raises(
        ValueError, match='unsupported wave-excitation source table schema'
    ):
        replay_wave_excitation_source_derivation(
            source_bytes=b'{"schema": "other", "rows": []}',
            converter_id=WAVE_EXCITATION_TABLE_CONVERTER_ID,
            converter_version=WAVE_EXCITATION_TABLE_CONVERTER_VERSION,
            conversion_parameters=parameters,
        )


def test_native_backup_preserves_excitation_source_assets(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(
        data_dir,
        db_name='cad-scenes.sqlite3',
    )
    _save_definition(equipment_repository, definition)
    source_bytes, evidence, samples = _imported(definition)
    excitation = _excitation(definition, evidence, samples)
    repository.save_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SOURCE_FILENAME,
        media_type=SOURCE_MEDIA_TYPE,
        declared_schema=WAVE_EXCITATION_TABLE_SCHEMA,
    )
    repository.save_excitation(excitation)

    backup_path = tmp_path / 'excitation.htdt-backup'
    manifest = create_backup(data_dir, backup_path)
    digest = evidence.derivation.source_asset_sha256
    asset_entry = next(
        entry
        for entry in manifest.files
        if entry.path == f'measurement-assets/{digest}'
    )
    assert asset_entry.sha256 == digest
    assert asset_entry.size_bytes == len(source_bytes)
    assert validate_backup(backup_path) == manifest

    restored_dir = tmp_path / 'restored'
    restore_backup(restored_dir, backup_path)
    reopened_scene = SceneRepository(restored_dir / 'cad-scenes.sqlite3')
    reopened = CadWaveExcitationRepository(
        reopened_scene,
        equipment_repository=CadEquipmentRepository(reopened_scene),
    )
    assert reopened.read_source_asset(digest) == source_bytes
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.get_excitation(excitation.excitation_id) == excitation
    assert reopened.get_source_metadata(digest) is not None


# ---------------------------------------------------------------------------
# #931 — Source-response → wave-excitation derivation chain
# ---------------------------------------------------------------------------


def _volume_velocity_condition() -> SourceResponseCondition:
    return SourceResponseCondition(
        input_quantity='voltage_v_rms',
        input_value=2.83,
        reference_distance_m=1.0,
        field_condition='free_field',
        mounting_condition='infinite baffle',
        on_axis_direction=Direction3(x=1.0, y=0.0, z=0.0),
        calibration='acoustic reference point measurement',
    )


def _volume_velocity_response(
    definition,
    *,
    tier='EXACT_VOLUME_VELOCITY',
    with_phase=True,
    with_semantics=True,
):
    samples = (
        SourceResponseSample(
            frequency_hz=100.0,
            volume_velocity_m3_s=1.0e-4,
            volume_velocity_phase_deg=0.0 if with_phase else None,
        ),
        SourceResponseSample(
            frequency_hz=200.0,
            volume_velocity_m3_s=8.0e-5,
            volume_velocity_phase_deg=14.0362 if with_phase else None,
        ),
    )
    return build_source_response(
        equipment_definition=definition,
        label='exact volume velocity sweep',
        capability_tier=tier,
        provenance='anechoic rig',
        condition=_volume_velocity_condition(),
        valid_frequency_domain=FrequencyDomain(
            minimum_hz=100.0, maximum_hz=200.0
        ),
        response_samples=samples,
        created_at_utc=NOW,
        phasor_convention='exp(-i*omega*t)' if with_semantics else None,
        acoustic_reference_semantics=(
            'equipment_acoustic_reference_point' if with_semantics else None
        ),
    )


def test_source_response_derivation_replays_to_complex_excitation(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    response = _volume_velocity_response(definition)
    repository.source_response_repository.save_response(response)

    evidence = derive_wave_excitation_evidence_from_source_response(
        response=response,
        evidence_kind='measured',
        source_name='issue931 rig import',
        source_version='1',
        source_reference='retained sealed response authority',
    )
    assert isinstance(
        evidence.derivation, WaveExcitationSourceResponseDerivation
    )
    assert evidence.derivation.source_response_id == response.response_id
    assert (
        evidence.derivation.source_response_sha256
        == response.semantic_sha256
    )
    # Replayability: subject samples are exactly the converter output.
    expected = replay_wave_excitation_source_derivation(
        source_bytes=response.model_dump_json().encode('utf-8'),
        converter_id=evidence.derivation.converter_id,
        converter_version=evidence.derivation.converter_version,
        conversion_parameters=evidence.derivation.conversion_parameters,
    )
    assert tuple(evidence.subject.samples) == tuple(expected)
    assert evidence.subject.samples[0].real_m3_s == pytest.approx(1.0e-4)
    assert evidence.subject.samples[0].imag_m3_s == pytest.approx(0.0)

    # No managed source bytes: the sealed response authority is the source.
    repository.save_evidence(evidence)
    excitation = _excitation(
        definition, evidence, evidence.subject.samples
    )
    repository.save_excitation(excitation)
    assert repository.get_evidence(evidence.evidence_id) == evidence
    assert repository.get_excitation(excitation.excitation_id) == excitation

    # Resave must replay identically (idempotent sealed authority).
    assert repository.save_evidence(evidence) == evidence


def test_source_response_derivation_refuses_ineligible_responses(
    tmp_path: Path,
) -> None:
    definition = _definition()

    # Relative magnitude / absolute SPL cannot establish volume velocity.
    for tier in ('RELATIVE_ON_AXIS_MAGNITUDE', 'ABSOLUTE_FREE_FIELD_SPL'):
        spl_response = build_source_response(
            equipment_definition=definition,
            label=f'{tier} fixture',
            capability_tier=tier,
            provenance='rig',
            condition=_volume_velocity_condition(),
            valid_frequency_domain=FrequencyDomain(
                minimum_hz=100.0, maximum_hz=200.0
            ),
            response_samples=(
                SourceResponseSample(frequency_hz=100.0, magnitude_db_spl=88.0),
                SourceResponseSample(frequency_hz=200.0, magnitude_db_spl=86.0),
            ),
            created_at_utc=NOW,
            phasor_convention='exp(-i*omega*t)',
            acoustic_reference_semantics='equipment_acoustic_reference_point',
        )
        assert source_response_wave_excitation_eligible(spl_response) is not None
        with pytest.raises(ValueError, match='not eligible'):
            derive_wave_excitation_evidence_from_source_response(
                response=spl_response,
                evidence_kind='measured',
                source_name='rig',
                source_version='1',
                source_reference='x',
            )

    # Magnitude-only volume velocity (no phase) is not complex excitation.
    no_phase = _volume_velocity_response(definition, with_phase=False)
    assert source_response_wave_excitation_eligible(no_phase) is not None
    with pytest.raises(ValueError, match='complex volume velocity'):
        derive_wave_excitation_evidence_from_source_response(
            response=no_phase,
            evidence_kind='measured',
            source_name='rig',
            source_version='1',
            source_reference='x',
        )

    # Undeclared phasor/reference semantics is not eligible either.
    no_semantics = _volume_velocity_response(definition, with_semantics=False)
    assert source_response_wave_excitation_eligible(no_semantics) is not None
    with pytest.raises(ValueError, match='not eligible'):
        derive_wave_excitation_evidence_from_source_response(
            response=no_semantics,
            evidence_kind='measured',
            source_name='rig',
            source_version='1',
            source_reference='x',
        )


def test_source_response_derivation_stales_when_response_edited(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    response = _volume_velocity_response(definition)
    repository.source_response_repository.save_response(response)
    evidence = derive_wave_excitation_evidence_from_source_response(
        response=response,
        evidence_kind='measured',
        source_name='rig',
        source_version='1',
        source_reference='x',
    )
    repository.save_evidence(evidence)

    # A replacement response authority has a different content hash: a
    # derivation pinned to it cannot replay from the retained record, so the
    # chain goes stale instead of silently rebinding to the old response.
    edited = _volume_velocity_response(definition)
    assert edited.semantic_sha256 != response.semantic_sha256
    edited_evidence = build_wave_excitation_evidence_authority(
        evidence_kind='measured',
        source_name='rig',
        source_version='1',
        source_reference='x',
        derivation=WaveExcitationSourceResponseDerivation(
            source_response_id=edited.response_id,
            source_response_version=edited.authority_version,
            source_response_sha256=edited.semantic_sha256,
            converter_id=evidence.derivation.converter_id,
            converter_version=evidence.derivation.converter_version,
            conversion_parameters=evidence.derivation.conversion_parameters,
        ),
        subject=evidence.subject,
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_evidence(edited_evidence)


def test_source_response_derivation_fails_closed_when_unpersisted(
    tmp_path: Path,
) -> None:
    definition = _definition()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_definition(equipment_repository, definition)
    response = _volume_velocity_response(definition)
    evidence = derive_wave_excitation_evidence_from_source_response(
        response=response,
        evidence_kind='measured',
        source_name='rig',
        source_version='1',
        source_reference='x',
    )
    # The response authority was never persisted: replay must fail closed.
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_evidence(evidence)
    # Source bytes are meaningless for a source-response derivation.
    with pytest.raises(
        ValueError, match='only valid for source-asset evidence'
    ):
        repository.save_evidence(evidence, source_bytes=b'{}')
