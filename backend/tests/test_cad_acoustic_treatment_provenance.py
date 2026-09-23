from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.acoustic_benchmark import AcousticMaterial, GeometricAcousticBand
from htdt.cad_acoustic_treatment import (
    TreatmentAcousticModel,
    TreatmentAcousticModelSubject,
    TreatmentDimensions,
    TreatmentEvidenceAuthority,
    TreatmentEvidenceSubject,
    TreatmentFrequencyBand,
    TreatmentLayer,
    TreatmentModelBasis,
    TreatmentProvenance,
    TreatmentUncertainty,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
)
from htdt.cad_acoustic_treatment_repository import CadAcousticTreatmentRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, SceneDocument
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _repository(tmp_path: Path) -> CadAcousticTreatmentRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(
        SceneDocument(
            document_id='treatment-provenance-fixture',
            room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
            entities=(),
        ),
        parent_revision_id=None,
    )
    return CadAcousticTreatmentRepository(scene_repository)


def _dimensions() -> TreatmentDimensions:
    return TreatmentDimensions(width_m=0.6, height_m=1.2, thickness_m=0.1)


def _layers() -> tuple[TreatmentLayer, ...]:
    return (
        TreatmentLayer(
            layer_id='core',
            material_name='mineral wool',
            thickness_m=0.1,
            density_kg_m3=48.0,
        ),
    )


def _material() -> AcousticMaterial:
    return AcousticMaterial(
        material_id='provenance-fixture-material',
        provenance='Issue #421 test fixture',
        version='1',
        wave_model='unsupported',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(center_hz=500.0, absorption=0.9, scattering=0.05),
        ),
    )


def _model_subject() -> TreatmentAcousticModelSubject:
    return TreatmentAcousticModelSubject(
        model_id='provenance-fixture-model',
        model_version='1',
        evidence_basis='measured',
        valid_frequency_band=TreatmentFrequencyBand(min_hz=100.0, max_hz=4000.0),
        uncertainty=TreatmentUncertainty(kind='unknown'),
        material=_material(),
    )


def _subject(
    *,
    definition_id: str = 'panel-provenance',
    definition_version: str = '1.0',
    with_model: bool = True,
) -> TreatmentEvidenceSubject:
    return TreatmentEvidenceSubject(
        definition_id=definition_id,
        definition_version=definition_version,
        treatment_type='porous_absorber',
        dimensions=_dimensions(),
        air_gap_m=0.0,
        layers=_layers(),
        acoustic_model=_model_subject() if with_model else None,
    )


def _evidence(**overrides):
    kwargs = {
        'source_kind': 'measurement',
        'source_id': 'lab-report-4417',
        'source_version': '2026-09-01',
        'source_sha256': '2' * 64,
        'reference': 'Issue #421 fixture measurement report',
        'extraction_id': 'fixture-extraction',
        'extraction_version': '1',
        'subject': _subject(),
    }
    kwargs.update(overrides)
    return build_treatment_evidence_authority(**kwargs)


def _definition(
    provenance: TreatmentProvenance,
    *,
    definition_id: str = 'panel-provenance',
    version: str = '1.0',
    with_model: bool = True,
    model_provenance: TreatmentProvenance | None = None,
    model_subject: TreatmentAcousticModelSubject | None = None,
):
    acoustic_model = None
    if with_model:
        subject = _model_subject() if model_subject is None else model_subject
        acoustic_model = TreatmentAcousticModel(
            model_id=subject.model_id,
            model_version=subject.model_version,
            evidence_basis=subject.evidence_basis,
            valid_frequency_band=subject.valid_frequency_band,
            uncertainty=subject.uncertainty,
            provenance=provenance if model_provenance is None else model_provenance,
            material=subject.material,
        )
    return build_acoustic_treatment_definition(
        definition_id=definition_id,
        version=version,
        name='provenance fixture panel',
        treatment_type='porous_absorber',
        provenance=provenance,
        dimensions=_dimensions(),
        air_gap_m=0.0,
        layers=_layers(),
        acoustic_model=acoustic_model,
    )


def test_external_source_kinds_require_exact_source_sha256() -> None:
    evidence = _evidence()
    for kind in ('manufacturer', 'measurement', 'literature'):
        with pytest.raises(ValidationError, match='source_sha256'):
            TreatmentProvenance(
                source_kind=kind,
                source_id='source-1',
                source_version='1',
                source_authority=evidence.as_external_ref(),
            )
        with pytest.raises(ValidationError, match='source_sha256'):
            _evidence(source_kind=kind, source_sha256=None)


def test_provenance_requires_exact_authority_ref() -> None:
    with pytest.raises(ValidationError):
        TreatmentProvenance(
            source_kind='measurement',
            source_id='lab-report-4417',
            source_version='2026-09-01',
            source_sha256='2' * 64,
        )


def test_dangling_provenance_authority_rejected_on_save(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    definition = _definition(evidence.as_provenance())

    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(definition)


def test_fabricated_authority_ref_rejected_on_save(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provenance = TreatmentProvenance(
        source_kind='measurement',
        source_id='lab-report-4417',
        source_version='2026-09-01',
        source_sha256='2' * 64,
        source_authority=ExactExternalAuthorityRef(
            authority_id='treatment-evidence:' + '9' * 64,
            authority_version='treatment-evidence-1',
            semantic_hash_sha256='9' * 64,
        ),
    )
    definition = _definition(provenance)

    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.save_definition(definition)


def test_provenance_claim_mismatch_against_retained_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)

    mismatched = TreatmentProvenance(
        source_kind='measurement',
        source_id='different-source-id',
        source_version='2026-09-01',
        source_sha256='2' * 64,
        source_authority=evidence.as_external_ref(),
    )
    with pytest.raises(
        ValueError,
        match='does not match the retained evidence authority',
    ):
        repository.save_definition(_definition(mismatched))

    wrong_hash = TreatmentProvenance(
        source_kind='measurement',
        source_id='lab-report-4417',
        source_version='2026-09-01',
        source_sha256='3' * 64,
        source_authority=evidence.as_external_ref(),
    )
    with pytest.raises(
        ValueError,
        match='does not match the retained evidence authority',
    ):
        repository.save_definition(_definition(wrong_hash))


def test_evidence_subject_must_support_normalized_definition(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)

    other_subject_evidence = _evidence(
        source_id='other-definition-evidence',
        subject=_subject(definition_id='other-panel'),
    )
    repository.save_evidence(other_subject_evidence)
    wrong_definition = _definition(
        other_subject_evidence.as_provenance(),
        model_provenance=evidence.as_provenance(),
    )
    with pytest.raises(ValueError, match='does not support the normalized'):
        repository.save_definition(wrong_definition)

    physical_only = _evidence(
        source_id='physical-only-evidence',
        subject=_subject(with_model=False),
    )
    repository.save_evidence(physical_only)
    model_without_support = _definition(
        physical_only.as_provenance(),
        model_provenance=physical_only.as_provenance(),
    )
    with pytest.raises(ValueError, match='does not support the normalized'):
        repository.save_definition(model_without_support)


def test_honest_definition_roundtrip_is_unchanged(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)
    definition = _definition(evidence.as_provenance())

    persisted = repository.save_definition(definition)
    assert persisted == definition

    reopened = CadAcousticTreatmentRepository(SceneRepository(repository.path))
    assert reopened.get_definition(definition.definition_id, definition.version) == definition
    assert reopened.list_definition_versions(definition.definition_id) == (definition,)
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.resolve_evidence(evidence.as_external_ref()) == evidence


def test_deleted_evidence_fails_closed_on_read(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)
    definition = _definition(evidence.as_provenance())
    repository.save_definition(definition)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_treatment_evidence_authorities WHERE evidence_id=?',
            (evidence.evidence_id,),
        )

    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.get_definition(definition.definition_id, definition.version)
    with pytest.raises(ValueError, match='does not resolve to retained evidence'):
        repository.list_definition_versions(definition.definition_id)


def test_tampered_evidence_payload_fails_closed_on_read(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)
    definition = _definition(evidence.as_provenance())
    repository.save_definition(definition)

    payload = evidence.model_dump(mode='json')
    payload['source_id'] = 'tampered-source'
    forged = dict(payload)
    forged['evidence_id'] = evidence.evidence_id
    forged['evidence_sha256'] = evidence.evidence_sha256

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_treatment_evidence_authorities SET payload_json=? '
            'WHERE evidence_id=?',
            (json.dumps(forged), evidence.evidence_id),
        )

    with pytest.raises(ValueError):
        repository.get_evidence(evidence.evidence_id)
    with pytest.raises(ValueError):
        repository.get_definition(definition.definition_id, definition.version)


def test_user_defined_evidence_is_explicit_manual_authority(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence(
        source_kind='user_defined',
        source_id='manual-panel-declaration',
        source_sha256=None,
        subject=_subject(with_model=False),
    )
    assert evidence.source_sha256 is None
    repository.save_evidence(evidence)
    definition = _definition(evidence.as_provenance(), with_model=False)
    repository.save_definition(definition)
    assert (
        repository.get_definition(definition.definition_id, definition.version)
        == definition
    )


def test_measured_basis_cannot_be_claimed_from_user_defined_source() -> None:
    evidence = _evidence(
        source_kind='user_defined',
        source_id='manual-panel-declaration',
        source_sha256=None,
        subject=_subject(with_model=False),
    )
    subject = _model_subject()
    with pytest.raises(ValidationError, match='cannot be claimed from'):
        TreatmentAcousticModel(
            model_id=subject.model_id,
            model_version=subject.model_version,
            evidence_basis='measured',
            valid_frequency_band=subject.valid_frequency_band,
            uncertainty=subject.uncertainty,
            provenance=evidence.as_provenance(),
            material=subject.material,
        )


def test_analytic_model_evidence_requires_exact_model_basis() -> None:
    with pytest.raises(ValidationError, match='model basis'):
        _evidence(
            source_kind='analytic_model',
            source_id='delany-bazley-fixture',
            source_sha256=None,
            subject=_subject(with_model=False),
        )

    basis = TreatmentModelBasis(
        model_id='delany-bazley',
        model_version='1970-1',
        parameters={'airflow_resistivity_pa_s_m2': 12000.0, 'thickness_m': 0.1},
        assumed_quantities=('airflow_resistivity_pa_s_m2', 'thickness_m'),
        derived_quantities=('geometric_bands',),
    )
    evidence = _evidence(
        source_kind='analytic_model',
        source_id='delany-bazley-fixture',
        source_version='1',
        source_sha256=None,
        model_basis=basis,
        subject=_subject(with_model=False),
    )
    assert evidence.model_basis == basis


def test_modelled_evidence_basis_roundtrip(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    basis = TreatmentModelBasis(
        model_id='delany-bazley',
        model_version='1970-1',
        parameters={'airflow_resistivity_pa_s_m2': 12000.0},
        assumed_quantities=('airflow_resistivity_pa_s_m2',),
        derived_quantities=('geometric_bands',),
    )
    model_subject = TreatmentAcousticModelSubject(
        model_id='modelled-model',
        model_version='1',
        evidence_basis='modelled',
        valid_frequency_band=TreatmentFrequencyBand(min_hz=100.0, max_hz=4000.0),
        uncertainty=TreatmentUncertainty(kind='unknown'),
        material=_material(),
    )
    subject = TreatmentEvidenceSubject(
        definition_id='modelled-panel',
        definition_version='1.0',
        treatment_type='porous_absorber',
        dimensions=_dimensions(),
        air_gap_m=0.0,
        layers=_layers(),
        acoustic_model=model_subject,
    )
    evidence = _evidence(
        source_kind='analytic_model',
        source_id='delany-bazley-fixture',
        source_sha256=None,
        model_basis=basis,
        subject=subject,
    )
    repository.save_evidence(evidence)
    definition = _definition(
        evidence.as_provenance(),
        definition_id='modelled-panel',
        model_subject=model_subject,
    )
    repository.save_definition(definition)
    assert (
        repository.get_definition(definition.definition_id, definition.version)
        == definition
    )


def test_model_basis_rejects_assumed_derived_overlap() -> None:
    with pytest.raises(ValidationError, match='assumed and derived'):
        TreatmentModelBasis(
            model_id='fixture-model',
            model_version='1',
            assumed_quantities=('density',),
            derived_quantities=('density',),
        )


def test_managed_source_asset_tampering_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    raw = b'fixture lab report bytes'
    digest = repository.save_source_asset(
        filename='lab-report-4417.csv',
        data=raw,
    )
    evidence = _evidence(source_sha256=digest)
    repository.save_evidence(evidence)
    definition = _definition(evidence.as_provenance())
    repository.save_definition(definition)

    asset_path = repository.assets_dir / digest
    asset_path.write_bytes(b'fixture lab report byt3s')

    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_evidence(evidence.evidence_id)
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        repository.get_definition(definition.definition_id, definition.version)


def test_evidence_id_reuse_with_different_semantics_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    evidence = _evidence()
    repository.save_evidence(evidence)

    payload = evidence.model_dump(mode='python')
    payload['reference'] = 'different reference, same id'
    with pytest.raises(ValidationError):
        TreatmentEvidenceAuthority.model_validate(payload)
