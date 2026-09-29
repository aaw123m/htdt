from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_directivity import NORMALIZED_JSON_DIRECTIVITY_ADAPTER
from htdt.cad_directivity_import import replay_directivity_import
from htdt.cad_directivity_repository import CadDirectivityRepository
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_evidence import build_equipment_manual_evidence
from htdt.cad_equipment_repository import CadEquipmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Size3
from htdt.native_backup import create_backup, restore_backup, validate_backup


NORMALIZED_SCHEMA = 'htdt.normalized-directivity.v1'
NORMALIZED_FILENAME = 'speaker-directivity.normalized.json'
NORMALIZED_MEDIA_TYPE = 'application/json'


def _source_payload(
    *,
    dataset_id: str = 'issue357-dataset',
    version: str = '1',
    kind: str = 'magnitude_only',
    interpolation_implementation: str = 'issue357-grid-linear',
    off_axis_db: float = -6.0,
    source_name: str = 'Issue 357 source fixture',
) -> dict:
    samples = []
    for frequency_hz in (500.0, 1000.0):
        for horizontal_angle_deg in (-30.0, 0.0, 30.0):
            samples.append(
                {
                    'frequency_hz': frequency_hz,
                    'horizontal_angle_deg': horizontal_angle_deg,
                    'vertical_angle_deg': 0.0,
                    'magnitude': (
                        0.0 if horizontal_angle_deg == 0.0 else off_axis_db
                    ),
                    'phase_deg': (
                        horizontal_angle_deg / 3.0
                        if kind == 'complex'
                        else None
                    ),
                }
            )
    return {
        'schema': NORMALIZED_SCHEMA,
        'dataset_id': dataset_id,
        'version': version,
        'source_format': 'custom',
        'evidence_kind': 'measured',
        'source_name': source_name,
        'source_version': '1',
        'source_reference': 'issue357-focused-fixture',
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
        'interpolation_implementation': interpolation_implementation,
        'interpolation_version': '1',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-30.0, 0.0, 30.0],
        'vertical_angles_deg': [0.0],
        'samples': samples,
    }


def _source_bytes(**kwargs) -> bytes:
    return json.dumps(
        _source_payload(**kwargs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _definition(
    source_bytes: bytes,
    *,
    definition_id: str,
    kind: str,
):
    source = json.loads(source_bytes)
    digest = sha256(source_bytes).hexdigest()
    provenance = EquipmentDataProvenance(
        evidence_kind=source['evidence_kind'],
        source_name=source['source_name'],
        source_version=source['source_version'],
        source_reference=source['source_reference'],
        source_sha256=digest,
    )
    domain = DirectivityDomain(
        frequency=FrequencyDomain(
            minimum_hz=min(source['frequencies_hz']),
            maximum_hz=max(source['frequencies_hz']),
        ),
        horizontal=AngleDomain(
            minimum_deg=min(source['horizontal_angles_deg']),
            maximum_deg=max(source['horizontal_angles_deg']),
        ),
        vertical=AngleDomain(
            minimum_deg=min(source['vertical_angles_deg']),
            maximum_deg=max(source['vertical_angles_deg']),
        ),
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
            tier=kind,
            data_format=source['source_format'],
            provenance=provenance,
            data_asset_sha256=digest,
            valid_domain=domain,
            interpolation=InterpolationProvenance(
                method=source['interpolation_method'],
                implementation=source['interpolation_implementation'],
                implementation_version=source['interpolation_version'],
                provenance=provenance,
            ),
            coherent_phase=(kind == 'complex'),
            phase_reference=source['phase_reference'],
        ),
    )


def _imported(
    *,
    dataset_id: str = 'issue357-dataset',
    definition_id: str = 'issue357-speaker',
    kind: str = 'magnitude_only',
    interpolation_implementation: str = 'issue357-grid-linear',
    source_name: str = 'Issue 357 source fixture',
):
    source_bytes = _source_bytes(
        dataset_id=dataset_id,
        kind=kind,
        interpolation_implementation=interpolation_implementation,
        source_name=source_name,
    )
    definition = _definition(
        source_bytes,
        definition_id=definition_id,
        kind=kind,
    )
    dataset = NORMALIZED_JSON_DIRECTIVITY_ADAPTER.parse(
        source_bytes,
        definition,
    )
    return source_bytes, definition, dataset


def _save_equipment(equipment_repository, definition) -> None:
    """Persist explicit manual evidence for every cited provenance, then save."""
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='issue357-fixture',
        recorded_at_utc='2026-01-01T00:00:00+00:00',
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)


def _repositories(tmp_path: Path, *, db_name: str = 'cad.sqlite3'):
    scene_repository = SceneRepository(tmp_path / db_name)
    equipment_repository = CadEquipmentRepository(scene_repository)
    directivity_repository = CadDirectivityRepository(
        scene_repository,
        equipment_repository,
    )
    return scene_repository, equipment_repository, directivity_repository


def _save(directivity_repository, dataset, source_bytes) -> None:
    directivity_repository.save_dataset(
        dataset,
        source_bytes=source_bytes,
        source_filename=NORMALIZED_FILENAME,
        media_type=NORMALIZED_MEDIA_TYPE,
        declared_schema=NORMALIZED_SCHEMA,
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
            'SELECT source_asset_sha256, filename, media_type, source_format, '
            'declared_schema FROM cad_directivity_source_assets '
            'ORDER BY source_asset_sha256'
        ).fetchall()


def test_persisted_dataset_reopens_exact_source_bytes(tmp_path: Path) -> None:
    source_bytes, definition, dataset = _imported()
    scene_repository, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    digest = dataset.source_asset_sha256
    asset_file = repository.assets_dir / digest
    assert asset_file.read_bytes() == source_bytes
    assert sha256(asset_file.read_bytes()).hexdigest() == digest

    # The managed registry and the directivity metadata each record the
    # asset once; the immutable byte identity stays separate from metadata.
    assert _asset_rows(repository.path) == [
        (
            digest,
            NORMALIZED_FILENAME,
            f'measurement-assets/{digest}',
            len(source_bytes),
        )
    ]
    assert _metadata_rows(repository.path) == [
        (
            digest,
            NORMALIZED_FILENAME,
            NORMALIZED_MEDIA_TYPE,
            'custom',
            NORMALIZED_SCHEMA,
        )
    ]
    metadata = repository.get_source_metadata(digest)
    assert metadata is not None
    assert metadata.filename == NORMALIZED_FILENAME
    assert metadata.media_type == NORMALIZED_MEDIA_TYPE
    assert metadata.source_format == 'custom'
    assert metadata.declared_schema == NORMALIZED_SCHEMA
    assert metadata.size_bytes == len(source_bytes)

    assert repository.read_source_asset(digest) == source_bytes
    assert repository.get_dataset(dataset.dataset_id, dataset.version) == dataset
    assert repository.get_dataset_by_hash(dataset.semantic_sha256) == dataset
    assert repository.list_datasets_for_definition(
        definition.semantic_sha256
    ) == (dataset,)


def test_identical_source_bytes_are_stored_once(tmp_path: Path) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    # Re-saving the same evidence reuses the one installed asset and one
    # registry/metadata row — identical bytes are never duplicated.
    _save(repository, dataset, source_bytes)
    repository.save_dataset(dataset)

    digest = dataset.source_asset_sha256
    assert [path.name for path in repository.assets_dir.iterdir()] == [digest]
    assert len(_asset_rows(repository.path)) == 1
    assert len(_metadata_rows(repository.path)) == 1
    assert repository.read_source_asset(digest) == source_bytes


def test_already_installed_identical_digest_is_a_dedup_hit(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)

    # An identical digest file already installed by another managed-asset
    # writer is adopted instead of rewritten; only its registry rows are
    # added.
    target = repository.assets_dir / dataset.source_asset_sha256
    target.write_bytes(source_bytes)
    _save(repository, dataset, source_bytes)

    assert [path.name for path in repository.assets_dir.iterdir()] == [
        dataset.source_asset_sha256
    ]
    assert len(_asset_rows(repository.path)) == 1
    assert repository.read_source_asset(
        dataset.source_asset_sha256
    ) == source_bytes


def test_resave_binds_existing_managed_asset_without_bytes(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    # A later save may bind to the already-managed asset without resupplying
    # the bytes; the file and replay still verify before the write commits.
    assert repository.save_dataset(dataset) == dataset


def test_save_without_source_or_managed_asset_fails_closed(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)

    with pytest.raises(ValueError, match='not registered'):
        repository.save_dataset(dataset)
    assert repository.read_source_asset(dataset.source_asset_sha256) is None

    with pytest.raises(ValueError, match='do not match'):
        repository.save_dataset(
            dataset,
            source_bytes=source_bytes + b' ',
            source_filename=NORMALIZED_FILENAME,
        )
    assert repository.read_source_asset(dataset.source_asset_sha256) is None


def test_missing_or_tampered_source_asset_fails_closed(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    asset_file = repository.assets_dir / dataset.source_asset_sha256
    # Same-length tampering isolates digest verification from size checks.
    asset_file.write_bytes(b'x' * len(source_bytes))
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_dataset(dataset.dataset_id, dataset.version)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.read_source_asset(dataset.source_asset_sha256)

    asset_file.unlink()
    with pytest.raises(ValueError, match='missing'):
        repository.get_dataset(dataset.dataset_id, dataset.version)
    with pytest.raises(ValueError, match='missing'):
        repository.read_source_asset(dataset.source_asset_sha256)


def test_recorded_adapter_replays_persisted_dataset(tmp_path: Path) -> None:
    source_bytes, definition, dataset = _imported(kind='complex')
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    reopened = repository.read_source_asset(dataset.source_asset_sha256)
    assert reopened == source_bytes
    replayed = replay_directivity_import(
        source_bytes=reopened,
        equipment_definition=definition,
        adapter_id=dataset.adapter_id,
        adapter_version=dataset.adapter_version,
    )
    assert replayed == dataset
    assert replayed.semantic_sha256 == dataset.semantic_sha256

    # An adapter version the pinned registry no longer resolves fails
    # closed instead of approximating the import transformation.
    with pytest.raises(ValueError, match='no supported directivity adapter'):
        replay_directivity_import(
            source_bytes=reopened,
            equipment_definition=definition,
            adapter_id=dataset.adapter_id,
            adapter_version='999',
        )


def test_dataset_that_does_not_replay_from_source_fails_closed(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    # Replace the persisted payload with a different internally-consistent
    # dataset that claims the same identity, binding and source hash: only
    # adapter replay can prove the stored samples do not derive from the
    # retained bytes.
    from htdt.cad_directivity import build_directivity_dataset
    from htdt.cad_equipment import EquipmentDataProvenance

    digest = dataset.source_asset_sha256
    other = build_directivity_dataset(
        dataset_id=dataset.dataset_id,
        version=dataset.version,
        definition=definition,
        source_asset_sha256=digest,
        source_format=dataset.source_format,
        parser_id=dataset.parser_id,
        parser_version=dataset.parser_version,
        adapter_id=dataset.adapter_id,
        adapter_version=dataset.adapter_version,
        evidence_kind=dataset.evidence_kind,
        source_provenance=EquipmentDataProvenance(
            evidence_kind=dataset.evidence_kind,
            source_name='Replay divergence fixture',
            source_version='1',
            source_reference='issue357-focused-fixture',
            source_sha256=digest,
        ),
        kind=dataset.kind,
        coordinate_convention=dataset.coordinate_convention,
        normalization=dataset.normalization,
        frequencies_hz=dataset.frequencies_hz,
        horizontal_angles_deg=dataset.horizontal_angles_deg,
        vertical_angles_deg=dataset.vertical_angles_deg,
        samples=tuple(
            sample.model_copy(
                update={
                    'magnitude_db': (
                        sample.magnitude_db - 1.0
                        if sample.horizontal_angle_deg != 0.0
                        else sample.magnitude_db
                    )
                }
            )
            for sample in dataset.samples
        ),
        interpolation=dataset.interpolation,
    )
    assert other.semantic_sha256 != dataset.semantic_sha256

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_directivity_datasets '
            'SET payload_json=?, semantic_sha256=? '
            'WHERE dataset_id=? AND version=?',
            (
                other.model_dump_json(),
                other.semantic_sha256,
                dataset.dataset_id,
                dataset.version,
            ),
        )

    with pytest.raises(ValueError, match='does not replay'):
        repository.get_dataset(dataset.dataset_id, dataset.version)


def test_conflicting_dataset_identity_still_fails_closed(
    tmp_path: Path,
) -> None:
    source_bytes, definition, dataset = _imported()
    other_bytes, other_definition, other_dataset = _imported(
        dataset_id=dataset.dataset_id,
        definition_id='other-speaker',
        interpolation_implementation='issue357-grid-linear-b',
        source_name='Other source fixture',
    )
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    _save_equipment(equipment_repository, other_definition)
    _save(repository, dataset, source_bytes)

    with pytest.raises(
        ValueError,
        match='already exists with different semantics',
    ):
        _save(repository, other_dataset, other_bytes)


def test_foreign_bytes_at_digest_path_fail_closed(tmp_path: Path) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)

    target = repository.assets_dir / dataset.source_asset_sha256
    target.write_bytes(b'foreign payload at the digest path')
    with pytest.raises(ValueError, match='hash collision'):
        _save(repository, dataset, source_bytes)
    assert repository.get_dataset(dataset.dataset_id, dataset.version) is None


def test_interrupted_asset_write_never_leaves_poisoned_digest_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(tmp_path)
    _save_equipment(equipment_repository, definition)
    digest = dataset.source_asset_sha256
    target = repository.assets_dir / digest

    def crashed_fsync(descriptor: int) -> None:
        raise OSError('simulated crash before durable install')

    monkeypatch.setattr(os, 'fsync', crashed_fsync)
    with pytest.raises(OSError, match='simulated crash'):
        _save(repository, dataset, source_bytes)

    assert not target.exists()
    leftovers = [
        path
        for path in repository.assets_dir.iterdir()
        if path.name.startswith('.asset-') or path.name.endswith('.tmp')
    ]
    assert leftovers == []
    assert _asset_rows(repository.path) == []
    assert _metadata_rows(repository.path) == []

    monkeypatch.undo()
    _save(repository, dataset, source_bytes)
    assert target.read_bytes() == source_bytes
    assert repository.get_dataset(dataset.dataset_id, dataset.version) == dataset


def test_native_backup_preserves_and_verifies_source_assets(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    source_bytes, definition, dataset = _imported()
    _scene, equipment_repository, repository = _repositories(
        data_dir,
        db_name='cad-scenes.sqlite3',
    )
    _save_equipment(equipment_repository, definition)
    _save(repository, dataset, source_bytes)

    backup_path = tmp_path / 'directivity.htdt-backup'
    manifest = create_backup(data_dir, backup_path)
    digest = dataset.source_asset_sha256
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
    reopened = CadDirectivityRepository(
        reopened_scene,
        CadEquipmentRepository(reopened_scene),
    )
    assert reopened.read_source_asset(digest) == source_bytes
    assert reopened.get_dataset(dataset.dataset_id, dataset.version) == dataset
    assert reopened.get_source_metadata(digest) is not None


def test_analytic_directivity_stays_asset_free_by_design(
    tmp_path: Path,
) -> None:
    provenance = EquipmentDataProvenance(
        evidence_kind='analytic',
        source_name='Issue 357 analytic fixture',
        source_version='1',
        source_reference='analytic-model-fixture',
        source_sha256='0' * 64,
    )
    definition = build_equipment_definition(
        definition_id='analytic-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='Analytic speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.25, z_m=0.35),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='analytic',
            data_format='analytic_model',
            provenance=provenance,
            data_asset_sha256=None,
            valid_domain=DirectivityDomain(
                frequency=FrequencyDomain(
                    minimum_hz=500.0,
                    maximum_hz=1000.0,
                ),
                horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
                vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
            ),
            interpolation=InterpolationProvenance(
                method='linear',
                implementation='issue357-grid-linear',
                implementation_version='1',
                provenance=provenance,
            ),
            analytic_model='piston-in-baffle',
        ),
    )
    assert definition.directivity.data_asset_sha256 is None

    # No DirectivityDataset can be constructed against an analytic
    # capability: the binding requires an exact imported source hash and the
    # analytic tier forbids one, so no asset can ever be claimed or stored.
    from htdt.cad_directivity import (
        DirectivityCoordinateConvention,
        DirectivityNormalization,
        DirectivitySample,
        build_directivity_dataset,
    )

    with pytest.raises(ValueError, match='source asset hash'):
        build_directivity_dataset(
            dataset_id='analytic-dataset',
            version='1',
            definition=definition,
            source_asset_sha256='a' * 64,
            source_format='analytic_model',
            parser_id='analytic-fixture',
            parser_version='1',
            adapter_id='analytic-fixture',
            adapter_version='1',
            evidence_kind='analytic',
            source_provenance=provenance,
            kind='magnitude_only',
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
            samples=(
                DirectivitySample(
                    frequency_hz=frequency_hz,
                    horizontal_angle_deg=horizontal_angle_deg,
                    vertical_angle_deg=0.0,
                    magnitude_db=(
                        0.0 if horizontal_angle_deg == 0.0 else -6.0
                    ),
                )
                for frequency_hz in (500.0, 1000.0)
                for horizontal_angle_deg in (-30.0, 0.0, 30.0)
            ),
            interpolation=definition.directivity.interpolation,
        )

    _scene, _equipment_repository, repository = _repositories(tmp_path)
    assert _asset_rows(repository.path) == []
    assert _metadata_rows(repository.path) == []
    assert not repository.assets_dir.exists() or not any(
        repository.assets_dir.iterdir()
    )
