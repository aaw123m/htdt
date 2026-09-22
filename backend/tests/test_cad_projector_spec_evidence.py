from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

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
from htdt.cad_video_geometry import (
    PROJECTOR_SPEC_EVIDENCED_FIELDS,
    AngleRange,
    AspectRatio,
    LensShiftRange,
    ProjectorSpecification,
    ProjectorSpecificationEvidence,
    ProjectorSpecificationProvenance,
    ProjectorSpecFieldAssertion,
    ScreenGeometryBinding,
    SeatGeometryBinding,
    SightlineSample,
    VideoGeometryPolicy,
    build_projector_spec_document_evidence,
    build_projector_spec_field_assertions,
    build_projector_spec_manual_evidence,
    build_projector_specification,
    build_video_geometry_request,
    evaluate_video_geometry,
    projector_spec_optical_values,
    verify_projector_specification_evidence,
)
from htdt.cad_video_geometry_repository import CadVideoGeometryRepository
from htdt.native_backup import create_backup, restore_backup, validate_backup


NOW = '2026-09-19T07:30:00+00:00'
SPEC_SOURCE_FILENAME = 'example-projector-p-spec.json'
SPEC_SOURCE_MEDIA_TYPE = 'application/json'
EXTRACTOR_ID = 'htdt-test-spec-table'
EXTRACTOR_VERSION = '1.0'


def _optical_values(**overrides) -> dict:
    values = projector_spec_optical_values(
        lens_reference_offset_m=Offset3(x_m=0.0, y_m=-0.25, z_m=0.0),
        optical_axis_local=Direction3(x=0.0, y=-1.0, z=0.0),
        throw_ratio_min=1.2,
        throw_ratio_max=1.6,
        optical_zoom_ratio=1.33,
        horizontal_lens_shift=LensShiftRange(
            minimum_fraction=-0.25,
            maximum_fraction=0.25,
        ),
        vertical_lens_shift=LensShiftRange(
            minimum_fraction=-0.65,
            maximum_fraction=0.65,
        ),
        supported_aspect_ratios=(AspectRatio(width_units=16, height_units=9),),
    )
    values.update(overrides)
    return values


def _locators() -> dict:
    return {
        field: f'datasheet p.4, optical table, row "{field}"'
        for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
    }


def _assertions(**overrides) -> tuple[ProjectorSpecFieldAssertion, ...]:
    return build_projector_spec_field_assertions(
        optical_values=_optical_values(**overrides),
        field_locators=_locators(),
    )


def _source_bytes(**overrides) -> bytes:
    return json.dumps(
        {
            'schema': 'example.projector-spec-sheet.v1',
            'manufacturer': 'Example Projection Co.',
            'model': 'P',
            'document_version': '2026.1',
            'optical': _optical_values(**overrides),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def _document_evidence(
    *,
    evidence_kind: str = 'manufacturer_document',
    manufacturer: str = 'Example Projection Co.',
    model: str = 'P',
    document_version: str = '2026.1',
    source_sha256: str | None = None,
    assertions: tuple[ProjectorSpecFieldAssertion, ...] | None = None,
    optical_overrides: dict | None = None,
) -> ProjectorSpecificationEvidence:
    return build_projector_spec_document_evidence(
        evidence_kind=evidence_kind,
        manufacturer=manufacturer,
        model=model,
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version=document_version,
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=(
            source_sha256
            if source_sha256 is not None
            else sha256(_source_bytes(**(optical_overrides or {}))).hexdigest()
        ),
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        field_assertions=(
            assertions
            if assertions is not None
            else _assertions(**(optical_overrides or {}))
        ),
    )


def _spec(
    evidence: ProjectorSpecificationEvidence,
    *,
    specification_id: str = 'example-projector-p',
    source_sha256: str | None = None,
    optical_overrides: dict | None = None,
) -> ProjectorSpecification:
    optical = _optical_values(**(optical_overrides or {}))
    provenance = ProjectorSpecificationProvenance(
        source_kind='manufacturer',
        publisher='Example Projection Co.',
        document_title='Model P Optical Installation Specification',
        document_version='2026.1',
        reference='Throw and lens-shift table',
        source_uri='https://example.invalid/projector-p/spec',
        source_sha256=(
            source_sha256 if source_sha256 is not None else evidence.source_sha256
        ),
        evidence=evidence.ref(),
    )
    return build_projector_specification(
        specification_id=specification_id,
        version='2026.1',
        manufacturer='Example Projection Co.',
        model='P',
        provenance=provenance,
        lens_reference_offset_m=Offset3(
            **{k: v for k, v in optical['lens_reference_offset_m'].items()}
        ),
        optical_axis_local=Direction3(
            x=optical['optical_axis_local']['x'],
            y=optical['optical_axis_local']['y'],
            z=optical['optical_axis_local']['z'],
        ),
        throw_ratio_min=optical['throw_ratio_min'],
        throw_ratio_max=optical['throw_ratio_max'],
        optical_zoom_ratio=optical['optical_zoom_ratio'],
        horizontal_lens_shift=(
            None
            if optical['horizontal_lens_shift'] is None
            else LensShiftRange(**optical['horizontal_lens_shift'])
        ),
        vertical_lens_shift=(
            None
            if optical['vertical_lens_shift'] is None
            else LensShiftRange(**optical['vertical_lens_shift'])
        ),
        supported_aspect_ratios=tuple(
            AspectRatio(**item) for item in optical['supported_aspect_ratios']
        ),
    )


def _manual_evidence(
    *,
    manufacturer: str | None = None,
    model: str | None = None,
    optical_overrides: dict | None = None,
) -> ProjectorSpecificationEvidence:
    return build_projector_spec_manual_evidence(
        manufacturer=manufacturer,
        model=model,
        publisher='HTDT user',
        document_title='Measured placement envelope',
        document_version='1',
        reference='manual entry',
        field_assertions=build_projector_spec_field_assertions(
            optical_values=_optical_values(**(optical_overrides or {})),
            field_locators={
                field: 'site measurement worksheet, field row'
                for field in PROJECTOR_SPEC_EVIDENCED_FIELDS
            },
        ),
        source_citation='site measurement worksheet 2026-09-19',
        actor='installer-a',
        recorded_at_utc=NOW,
        evidence_basis='user_measurement',
    )


_EVIDENCE_SUBJECT = object()


def _manual_spec(
    evidence: ProjectorSpecificationEvidence,
    *,
    specification_id: str = 'custom-projector-envelope',
    manufacturer=_EVIDENCE_SUBJECT,
    model=_EVIDENCE_SUBJECT,
    optical_overrides: dict | None = None,
) -> ProjectorSpecification:
    optical = _optical_values(**(optical_overrides or {}))
    provenance = ProjectorSpecificationProvenance(
        source_kind='user_defined',
        publisher='HTDT user',
        document_title='Measured placement envelope',
        document_version='1',
        reference='manual entry',
        evidence=evidence.ref(),
    )
    return build_projector_specification(
        specification_id=specification_id,
        version='1',
        manufacturer=(
            evidence.manufacturer if manufacturer is _EVIDENCE_SUBJECT else manufacturer
        ),
        model=evidence.model if model is _EVIDENCE_SUBJECT else model,
        provenance=provenance,
        lens_reference_offset_m=Offset3(
            **{k: v for k, v in optical['lens_reference_offset_m'].items()}
        ),
        optical_axis_local=Direction3(
            x=optical['optical_axis_local']['x'],
            y=optical['optical_axis_local']['y'],
            z=optical['optical_axis_local']['z'],
        ),
        throw_ratio_min=optical['throw_ratio_min'],
        throw_ratio_max=optical['throw_ratio_max'],
        optical_zoom_ratio=optical['optical_zoom_ratio'],
        horizontal_lens_shift=(
            None
            if optical['horizontal_lens_shift'] is None
            else LensShiftRange(**optical['horizontal_lens_shift'])
        ),
        vertical_lens_shift=(
            None
            if optical['vertical_lens_shift'] is None
            else LensShiftRange(**optical['vertical_lens_shift'])
        ),
        supported_aspect_ratios=tuple(
            AspectRatio(**item) for item in optical['supported_aspect_ratios']
        ),
    )


def _repository(tmp_path: Path, *, db_name: str = 'cad.sqlite3') -> tuple:
    scene_repository = SceneRepository(tmp_path / db_name)
    repository = CadVideoGeometryRepository(scene_repository)
    return scene_repository, repository


def _save_document_spec(
    repository, specification, evidence
) -> ProjectorSpecification:
    return repository.save_projector_specification(
        specification,
        evidence=evidence,
        source_bytes=_source_bytes(),
        source_filename=SPEC_SOURCE_FILENAME,
        media_type=SPEC_SOURCE_MEDIA_TYPE,
    )


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='evidence-fixture',
        room=RoomPrism(width_m=6.0, depth_m=5.0, height_m=3.0),
        entities=(
            SceneEntity(
                entity_id='screen-main',
                kind='screen',
                name='Main Screen',
                position=Position3(x_m=3.0, y_m=0.2, z_m=1.5),
                size_m=Size3(x_m=3.2, y_m=0.1, z_m=1.8),
            ),
            SceneEntity(
                entity_id='projector-main',
                kind='projector',
                name='Projector',
                position=Position3(x_m=3.0, y_m=4.3, z_m=2.25),
                size_m=Size3(x_m=0.5, y_m=0.5, z_m=0.2),
            ),
            SceneEntity(
                entity_id='seat-front',
                kind='seat',
                name='Front',
                position=Position3(x_m=3.0, y_m=2.2, z_m=0.5),
                size_m=Size3(x_m=0.8, y_m=0.8, z_m=1.0),
            ),
        ),
    )


def _request(specification: ProjectorSpecification):
    return build_video_geometry_request(
        projector_entity_id='projector-main',
        projector_specification=specification,
        screen=ScreenGeometryBinding(
            entity_id='screen-main',
            visible_width_m=8.0 / 3.0,
            visible_height_m=1.5,
            frame_clearance_m=0.05,
            acoustically_transparent=True,
        ),
        seats=(
            SeatGeometryBinding(
                entity_id='seat-front',
                row_id='row-front',
                eye_reference_offset_local_m=Offset3(z_m=0.65),
                head_center_offset_local_m=Offset3(z_m=0.65),
                head_radius_m=0.16,
            ),
        ),
        policy=VideoGeometryPolicy(
            horizontal_viewing_angle_deg=AngleRange(
                minimum_deg=20.0,
                maximum_deg=80.0,
            ),
            sightline_samples=(
                SightlineSample(
                    sample_id='bottom-center',
                    horizontal_fraction=0.5,
                    vertical_fraction=0.0,
                ),
            ),
            sightline_clearance_m=0.03,
            riser_support_tolerance_m=0.005,
            max_optical_axis_deviation_deg=0.1,
            collision_clearance_m=0.02,
        ),
        collision_entity_ids=('screen-main', 'projector-main'),
    )


def test_provenance_requires_typed_evidence_ref() -> None:
    with pytest.raises(ValidationError):
        ProjectorSpecificationProvenance(
            source_kind='manufacturer',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            source_sha256='a' * 64,
        )


def test_manufacturer_provenance_requires_exact_source_hash_and_document_evidence() -> None:
    evidence = _document_evidence()
    with pytest.raises(ValidationError, match='exact source_sha256'):
        ProjectorSpecificationProvenance(
            source_kind='manufacturer',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            evidence=evidence.ref(),
        )
    manual = _manual_evidence()
    with pytest.raises(ValidationError, match='manual_record'):
        ProjectorSpecificationProvenance(
            source_kind='manufacturer',
            publisher='HTDT user',
            document_title='Measured placement envelope',
            document_version='1',
            reference='manual entry',
            source_sha256='a' * 64,
            evidence=manual.ref(),
        )
    with pytest.raises(ValidationError, match='manual_record evidence'):
        ProjectorSpecificationProvenance(
            source_kind='user_defined',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            evidence=evidence.ref(),
        )


def test_evidence_record_requires_complete_optical_attestation() -> None:
    partial = _assertions()[:-1]
    with pytest.raises(ValidationError, match='every optical field'):
        build_projector_spec_document_evidence(
            evidence_kind='manufacturer_document',
            manufacturer='Example Projection Co.',
            model='P',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            source_sha256='a' * 64,
            extractor_id=EXTRACTOR_ID,
            extractor_version=EXTRACTOR_VERSION,
            field_assertions=partial,
        )
    with pytest.raises(ValueError, match='every evidenced field'):
        build_projector_spec_field_assertions(
            optical_values={
                key: value
                for key, value in _optical_values().items()
                if key != 'throw_ratio_min'
            },
            field_locators=_locators(),
        )
    with pytest.raises(ValidationError, match='canonical JSON'):
        ProjectorSpecFieldAssertion(
            field='throw_ratio_min',
            value=float('nan'),
            locator='datasheet p.4',
        )


def test_evidence_kind_contracts_are_enforced() -> None:
    # Extracted (non-manual) evidence requires extraction authority and an
    # exact source hash; manual fields are forbidden on it.
    with pytest.raises(ValidationError, match='requires extractor_id'):
        ProjectorSpecificationEvidence(
            evidence_kind='manufacturer_document',
            manufacturer='Example Projection Co.',
            model='P',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            source_sha256='a' * 64,
            extractor_version=EXTRACTOR_VERSION,
            field_assertions=_assertions(),
            evidence_sha256='0' * 64,
        )
    with pytest.raises(ValidationError, match='requires source_sha256'):
        ProjectorSpecificationEvidence(
            evidence_kind='external_authority',
            manufacturer='Example Projection Co.',
            model='P',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            extractor_id=EXTRACTOR_ID,
            extractor_version=EXTRACTOR_VERSION,
            field_assertions=_assertions(),
            evidence_sha256='0' * 64,
        )
    with pytest.raises(ValidationError, match='must not carry actor'):
        ProjectorSpecificationEvidence(
            evidence_kind='manufacturer_document',
            manufacturer='Example Projection Co.',
            model='P',
            publisher='Example Projection Co.',
            document_title='Model P Optical Installation Specification',
            document_version='2026.1',
            reference='Throw and lens-shift table',
            source_sha256='a' * 64,
            extractor_id=EXTRACTOR_ID,
            extractor_version=EXTRACTOR_VERSION,
            field_assertions=_assertions(),
            actor='installer-a',
            evidence_sha256='0' * 64,
        )


def test_manual_evidence_requires_actor_time_citation_and_basis() -> None:
    for missing, match in (
        ('actor', 'requires actor'),
        ('recorded_at_utc', 'requires recorded_at_utc'),
        ('source_citation', 'requires source_citation'),
        ('evidence_basis', 'requires evidence_basis'),
    ):
        fields = {
            'actor': 'installer-a',
            'recorded_at_utc': NOW,
            'source_citation': 'citation',
            'evidence_basis': 'user_measurement',
        }
        fields.pop(missing)
        with pytest.raises(ValidationError, match=match):
            ProjectorSpecificationEvidence(
                evidence_kind='manual_record',
                manufacturer=None,
                model=None,
                publisher='HTDT user',
                document_title='Measured placement envelope',
                document_version='1',
                reference='manual entry',
                field_assertions=_assertions(),
                evidence_sha256='0' * 64,
                **fields,
            )
    # Manual evidence must not masquerade as extracted data.
    with pytest.raises(ValidationError, match='must not carry extractor_id'):
        ProjectorSpecificationEvidence(
            evidence_kind='manual_record',
            manufacturer=None,
            model=None,
            publisher='HTDT user',
            document_title='Measured placement envelope',
            document_version='1',
            reference='manual entry',
            extractor_id=EXTRACTOR_ID,
            field_assertions=_assertions(),
            actor='installer-a',
            recorded_at_utc=NOW,
            source_citation='citation',
            evidence_basis='user_measurement',
            evidence_sha256='0' * 64,
        )


def test_evidence_self_hash_is_enforced() -> None:
    evidence = _document_evidence()
    payload = evidence.model_dump(mode='json')
    payload['evidence_sha256'] = '0' * 64
    with pytest.raises(ValidationError, match='semantic hash mismatch'):
        ProjectorSpecificationEvidence.model_validate(payload)


def test_manufacturer_specification_without_resolvable_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    specification = _spec(evidence)

    # The typed ref points at evidence that was never persisted.
    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.save_projector_specification(specification)
    assert repository.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) is None

    # Supplying source bytes/metadata without the evidence record is rejected.
    with pytest.raises(ValueError, match='matching evidence record'):
        repository.save_projector_specification(
            specification,
            source_bytes=_source_bytes(),
            source_filename=SPEC_SOURCE_FILENAME,
        )


def test_fabricated_source_sha_and_bytes_are_rejected(tmp_path: Path) -> None:
    _scene_repository, repository = _repository(tmp_path)

    # Fabricated source hash: bytes do not hash to the declared digest.
    evidence = _document_evidence()
    with pytest.raises(ValueError, match='do not match'):
        repository.save_projector_spec_evidence(
            evidence,
            source_bytes=_source_bytes() + b' ',
            source_filename=SPEC_SOURCE_FILENAME,
        )

    # Fabricated source hash: no bytes and no registered managed asset.
    fabricated = _document_evidence(source_sha256='f' * 64)
    with pytest.raises(ValueError, match='retained source bytes'):
        repository.save_projector_spec_evidence(fabricated)

    # A specification ref to evidence that was never persisted is rejected.
    specification = _spec(fabricated)
    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.save_projector_specification(
            specification,
            evidence=None,
        )


def test_document_evidence_persists_retained_source_bytes(tmp_path: Path) -> None:
    _scene_repository, repository = _repository(tmp_path)
    source_bytes = _source_bytes()
    evidence = _document_evidence()
    digest = evidence.source_sha256

    persisted = repository.save_projector_spec_evidence(
        evidence,
        source_bytes=source_bytes,
        source_filename=SPEC_SOURCE_FILENAME,
        media_type=SPEC_SOURCE_MEDIA_TYPE,
    )
    assert persisted == evidence
    assert (repository.assets_dir / digest).read_bytes() == source_bytes
    assert repository.read_projector_spec_source_asset(digest) == source_bytes

    metadata = repository.get_projector_spec_source_metadata(digest)
    assert metadata is not None
    assert metadata.filename == SPEC_SOURCE_FILENAME
    assert metadata.media_type == SPEC_SOURCE_MEDIA_TYPE
    assert metadata.size_bytes == len(source_bytes)
    assert metadata.evidence_sha256 == evidence.evidence_sha256

    resolved = repository.get_projector_spec_evidence(evidence.evidence_sha256)
    assert resolved == evidence
    locators = {
        item.field: item.locator for item in resolved.field_assertions
    }
    assert 'throw_ratio_min' in locators
    assert 'p.4' in locators['throw_ratio_min']

    # Evidence is append-only and idempotent.
    assert (
        repository.save_projector_spec_evidence(
            evidence,
            source_bytes=source_bytes,
            source_filename=SPEC_SOURCE_FILENAME,
        )
        == evidence
    )


def test_optical_values_must_rederive_from_evidence(tmp_path: Path) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    repository.save_projector_spec_evidence(
        evidence,
        source_bytes=_source_bytes(),
        source_filename=SPEC_SOURCE_FILENAME,
    )

    # A specification can name the real evidence record yet claim arbitrary
    # favorable optical values: re-derivation fails closed.
    falsified = _spec(evidence, optical_overrides={'throw_ratio_max': 9.9})
    with pytest.raises(ValueError, match='do not re-derive'):
        repository.save_projector_specification(falsified)

    with pytest.raises(ValueError, match='do not re-derive'):
        verify_projector_specification_evidence(
            specification=falsified,
            evidence=evidence,
        )


def test_evidence_for_other_manufacturer_model_or_version_is_rejected(
    tmp_path: Path,
) -> None:
    _scene_repository, repository = _repository(tmp_path)
    other = _document_evidence(
        manufacturer='Other Projection Co.',
        model='Q',
        document_version='9.9',
    )
    repository.save_projector_spec_evidence(
        other,
        source_bytes=_source_bytes(),
        source_filename=SPEC_SOURCE_FILENAME,
    )
    # The spec claims Example/P document identity but references evidence
    # that describes a different manufacturer/model/version.
    specification = _spec(other)
    with pytest.raises(ValueError, match='diverges from the specification provenance'):
        repository.save_projector_specification(specification)

    # Supplied evidence must be the exact record the provenance ref pins.
    canonical = _document_evidence()
    honest = _spec(canonical)
    with pytest.raises(ValueError, match='does not match the specification provenance ref'):
        repository.save_projector_specification(
            honest,
            evidence=other,
            source_bytes=_source_bytes(),
            source_filename=SPEC_SOURCE_FILENAME,
        )


def test_persisted_specification_roundtrips_with_verified_evidence(
    tmp_path: Path,
) -> None:
    scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)

    assert repository.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) == specification
    assert repository.get_projector_specification_by_hash(
        specification.specification_sha256
    ) == specification
    assert repository.list_projector_specifications() == (specification,)

    # Idempotent re-save still re-resolves evidence.
    assert (
        _save_document_spec(repository, specification, evidence)
        == specification
    )

    reopened = CadVideoGeometryRepository(
        SceneRepository(scene_repository.path)
    )
    assert reopened.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) == specification


def test_missing_or_tampered_source_asset_fails_closed(tmp_path: Path) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)
    digest = evidence.source_sha256
    asset_file = repository.assets_dir / digest

    # Same-length tampering isolates digest verification from size checks.
    asset_file.write_bytes(b'x' * asset_file.stat().st_size)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_projector_specification(
            specification.specification_id,
            specification.version,
        )
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.read_projector_spec_source_asset(digest)

    asset_file.unlink()
    with pytest.raises(ValueError, match='missing'):
        repository.get_projector_specification(
            specification.specification_id,
            specification.version,
        )


def test_deleted_evidence_record_fails_closed(tmp_path: Path) -> None:
    scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)

    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_projector_spec_evidence WHERE evidence_sha256=?',
            (evidence.evidence_sha256,),
        )

    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.get_projector_specification(
            specification.specification_id,
            specification.version,
        )
    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.list_projector_specifications()
    # Even an idempotent-looking re-save re-resolves evidence first.
    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.save_projector_specification(specification)


def test_divergent_persisted_evidence_payload_fails_closed(tmp_path: Path) -> None:
    scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)

    # Replace the persisted payload with a different internally-consistent
    # evidence record under the same evidence_sha256 key. It keeps the same
    # bound source digest so asset verification still resolves, isolating
    # the typed-ref hash check.
    divergent = _document_evidence(
        source_sha256=evidence.source_sha256,
        optical_overrides={'throw_ratio_max': 1.7},
    )
    assert divergent.evidence_sha256 != evidence.evidence_sha256
    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_projector_spec_evidence SET payload_json=? '
            'WHERE evidence_sha256=?',
            (divergent.model_dump_json(), evidence.evidence_sha256),
        )

    with pytest.raises(ValueError, match='does not match provenance ref'):
        repository.get_projector_specification(
            specification.specification_id,
            specification.version,
        )


def test_external_authority_evidence_resolves_without_retained_bytes(
    tmp_path: Path,
) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _document_evidence(evidence_kind='external_authority')
    specification = _spec(evidence, specification_id='example-projector-p-ext')

    # No retained bytes: the typed immutable record itself is the authority.
    assert repository.save_projector_spec_evidence(evidence) == evidence
    assert repository.read_projector_spec_source_asset(
        evidence.source_sha256
    ) is None
    assert repository.save_projector_specification(specification) == specification
    assert repository.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) == specification


def test_user_defined_specification_uses_explicit_manual_evidence(
    tmp_path: Path,
) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _manual_evidence()
    specification = _manual_spec(evidence)

    assert repository.save_projector_spec_evidence(evidence) == evidence
    assert repository.save_projector_specification(specification) == specification
    persisted = repository.get_projector_specification(
        specification.specification_id,
        specification.version,
    )
    assert persisted == specification
    resolved = repository.get_projector_spec_evidence(evidence.evidence_sha256)
    assert resolved is not None
    assert resolved.evidence_kind == 'manual_record'
    assert resolved.actor == 'installer-a'
    assert resolved.recorded_at_utc == NOW
    assert resolved.source_citation == 'site measurement worksheet 2026-09-19'
    assert resolved.evidence_basis == 'user_measurement'

    # A user-defined spec cannot lean on manufacturer-document evidence.
    document_evidence = _document_evidence()
    repository.save_projector_spec_evidence(
        document_evidence,
        source_bytes=_source_bytes(),
        source_filename=SPEC_SOURCE_FILENAME,
    )
    with pytest.raises(ValidationError, match='manual_record evidence'):
        _manual_spec(
            document_evidence,
            specification_id='custom-projector-other',
        )


def test_manual_evidence_subject_must_match_specification(tmp_path: Path) -> None:
    _scene_repository, repository = _repository(tmp_path)
    evidence = _manual_evidence(manufacturer='Example', model='P9')
    specification = _manual_spec(
        evidence,
        specification_id='custom-labeled',
    )
    repository.save_projector_spec_evidence(evidence)
    assert repository.save_projector_specification(specification) == specification

    # A spec that claims a different manufacturer/model while pointing at
    # evidence for another subject is rejected by the identity check.
    mismatched = _manual_spec(
        evidence,
        specification_id='custom-labeled-2',
        manufacturer='Different',
        model='Z',
    )
    with pytest.raises(
        ValueError,
        match='diverges from the specification provenance',
    ):
        repository.save_projector_specification(mismatched)


def test_evaluation_replay_is_unchanged_once_spec_authority_resolves(
    tmp_path: Path,
) -> None:
    scene_repository, repository = _repository(tmp_path)
    baseline = scene_repository.save(_scene(), parent_revision_id=None).revision
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)

    request = _request(specification)
    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=None,
        projector_specification=specification,
        request=request,
    )
    assert repository.save_evaluation(evaluation) == evaluation
    assert repository.get_evaluation(evaluation.evaluation_id) == evaluation
    assert repository.list_evaluations_for_revision(baseline.revision_id) == (
        evaluation,
    )


def test_evaluation_reads_fail_closed_when_spec_evidence_disappears(
    tmp_path: Path,
) -> None:
    scene_repository, repository = _repository(tmp_path)
    baseline = scene_repository.save(_scene(), parent_revision_id=None).revision
    evidence = _document_evidence()
    specification = _spec(evidence)
    _save_document_spec(repository, specification, evidence)
    evaluation = evaluate_video_geometry(
        baseline=baseline,
        variant=None,
        projector_specification=specification,
        request=_request(specification),
    )
    repository.save_evaluation(evaluation)

    with closing(sqlite3.connect(scene_repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('DELETE FROM cad_projector_spec_evidence')

    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.get_evaluation(evaluation.evaluation_id)
    with pytest.raises(ValueError, match='unpersisted evidence'):
        repository.list_evaluations_for_revision(baseline.revision_id)


def test_native_backup_preserves_managed_projector_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    scene_repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository = CadVideoGeometryRepository(scene_repository)
    evidence = _document_evidence()
    specification = _spec(evidence)
    source_bytes = _source_bytes()
    repository.save_projector_specification(
        specification,
        evidence=evidence,
        source_bytes=source_bytes,
        source_filename=SPEC_SOURCE_FILENAME,
        media_type=SPEC_SOURCE_MEDIA_TYPE,
    )

    backup_path = tmp_path / 'projector-evidence.htdt-backup'
    manifest = create_backup(data_dir, backup_path)
    digest = evidence.source_sha256
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
    reopened = CadVideoGeometryRepository(
        SceneRepository(restored_dir / 'cad-scenes.sqlite3')
    )
    assert reopened.read_projector_spec_source_asset(digest) == source_bytes
    assert reopened.get_projector_spec_evidence(
        evidence.evidence_sha256
    ) == evidence
    assert reopened.get_projector_specification(
        specification.specification_id,
        specification.version,
    ) == specification
