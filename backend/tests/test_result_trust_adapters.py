"""#774 — Result Trust adapter boundary: freshness/scope derive from authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadMeasurementRecord
from htdt.cad_model_validation import CadModelValidationRecord, CadValidationPair
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)
from htdt.result_trust import (
    EvidenceClass,
    FreshnessState,
    ValidationScope,
)
from htdt.result_trust_adapters import (
    trust_for_measurement,
    trust_for_prediction,
    trust_for_recommendation,
    trust_for_validation,
)


def _scene(document_id: str, *, shift: float = 0.0) -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=10.0, y_m=20.0),
            RoomVertex(vertex_id='b', x_m=16.0, y_m=20.0),
            RoomVertex(vertex_id='c', x_m=16.0, y_m=24.0),
            RoomVertex(vertex_id='d', x_m=10.0, y_m=24.0),
        ),
        height_m=2.4,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=11.4 + shift, y_m=20.8, z_m=1.05),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=13.0, y_m=23.0, z_m=1.1),
            ),
        ),
    )


def _saved_revision(tmp_path: Path, document_id: str = 'trust-doc'):
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(document_id), parent_revision_id=None).revision
    return repository, revision


def _prediction(repository, revision):
    modes, _reflections = analyze_native_rectangular_geometry(
        revision, 'point-mlp'
    )
    return modes


def _validation(
    revision,
    *,
    evidence_scope: str = 'synthetic_fixture',
    recommendation_gate: str = 'disabled',
) -> CadModelValidationRecord:
    """Fixture validation record for adapter tests.

    ``model_construct`` skips the O60 gate validators on purpose: the
    adapter contract under test only reads the canonical identity, scope and
    recommendation gate — building a fully gate-eligible owned-room record
    would require an entire measurement campaign.
    """
    hex64 = 'a' * 64
    owned = evidence_scope == 'owned_room'
    return CadModelValidationRecord.model_construct(
        validation_id='val-1',
        document_id=revision.document_id,
        search_spec_id='spec-1',
        search_spec_sha256=hex64,
        candidate_set_sha256='b' * 64,
        campaign_id='camp-1' if owned else None,
        campaign_sha256='c' * 64 if owned else None,
        campaign_registration_id=(
            'o60-validation-campaign-registration:' + 'd' * 64 if owned else None
        ),
        campaign_registration_sha256='e' * 64 if owned else None,
        model_id='model-1',
        model_version='1.0.0',
        evidence_scope=evidence_scope,
        requested_band_hz=(20.0, 120.0),
        pairs=(
            CadValidationPair(
                candidate_id='cand-1',
                split='calibration',
                prediction_source_id='pred-1',
                measurement_id='meas-1',
                rms_difference_db=1.0,
            ),
        ),
        residual_gate='pass',
        max_holdout_rms_db=2.0,
        recommendation_gate=recommendation_gate,
        gate_reasons=(),
        created_at_utc='2026-01-01T00:00:00+00:00',
        validation_sha256='f' * 64,
    )


def _measurement(
    revision,
    *,
    document_id: str | None = None,
    scene_revision_id: str | None = None,
    scene_content_hash: str | None = None,
    evidence_type: str = 'measured',
) -> CadMeasurementRecord:
    return CadMeasurementRecord(
        measurement_id='meas-1',
        document_id=document_id or revision.document_id,
        scene_revision_id=scene_revision_id or revision.revision_id,
        scene_content_hash=scene_content_hash or revision.content_hash,
        measurement_entity_id='point-mlp',
        measurement_position=Position3(x_m=13.0, y_m=23.0, z_m=1.1),
        evidence_type=evidence_type,
        imported_at='2026-01-01T00:00:00+00:00',
        source_kind='rew_api',
    )


def test_prediction_bound_to_current_head_is_proven_current(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)

    summary = trust_for_prediction(prediction, scene_repository=repository)

    assert summary.evidence_class == EvidenceClass.PREDICTED
    assert summary.freshness == FreshnessState.CURRENT
    assert summary.validation_scope == ValidationScope.UNVALIDATED
    assert summary.invalidating_dependencies == ()


def test_prediction_is_stale_after_head_advances_and_names_the_head(
    tmp_path: Path,
) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    new_revision = repository.save(
        _scene(revision.document_id, shift=0.2),
        parent_revision_id=revision.revision_id,
    ).revision

    summary = trust_for_prediction(prediction, scene_repository=repository)

    assert summary.freshness == FreshnessState.STALE
    assert summary.invalidating_dependencies == (
        f'scene_head:{new_revision.revision_id}',
    )


def test_prediction_with_deleted_source_revision_is_incomplete(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    new_revision = repository.save(
        _scene(revision.document_id, shift=0.2),
        parent_revision_id=revision.revision_id,
    ).revision
    # Rewrite the pinned revision id so the bound authority is unresolvable —
    # head mismatch alone is stale; a missing pinned revision is incomplete.
    gone = prediction.model_copy(
        update={'scene_revision_id': 'rev-that-was-deleted'}
    )

    summary = trust_for_prediction(gone, scene_repository=repository)

    assert summary.freshness == FreshnessState.INCOMPLETE_DEPENDENCY
    assert new_revision.revision_id != gone.scene_revision_id


def test_prediction_for_unknown_document_head_is_incomplete(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    other = prediction.model_copy(update={'document_id': 'never-saved-doc'})

    summary = trust_for_prediction(other, scene_repository=repository)

    assert summary.freshness == FreshnessState.INCOMPLETE_DEPENDENCY


def test_synthetic_validation_maps_to_synthetic_scope(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    validation = _validation(
        revision,
        evidence_scope='synthetic_fixture',
    ).model_copy(
        update={
            'model_id': prediction.model_id,
            'model_version': prediction.model_version,
        }
    )

    summary = trust_for_prediction(
        prediction, scene_repository=repository, validation=validation
    )

    assert summary.validation_scope == ValidationScope.SYNTHETIC_FIXTURE
    assert summary.provenance_ref == 'model-validation:val-1'


def test_owned_room_validation_renders_strong_scope_with_provenance(
    tmp_path: Path,
) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    validation = _validation(
        revision,
        evidence_scope='owned_room',
        recommendation_gate='eligible',
    ).model_copy(
        update={
            'model_id': prediction.model_id,
            'model_version': prediction.model_version,
        }
    )

    summary = trust_for_prediction(
        prediction, scene_repository=repository, validation=validation
    )

    # Strongest scope an adapter can emit — still not production-qualified.
    assert summary.validation_scope == ValidationScope.OWNED_ROOM_VALIDATED
    assert summary.provenance_ref == 'model-validation:val-1'


def test_adapters_never_emit_production_qualified(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    validation = _validation(
        revision,
        evidence_scope='owned_room',
        recommendation_gate='eligible',
    ).model_copy(
        update={
            'model_id': prediction.model_id,
            'model_version': prediction.model_version,
        }
    )

    for summary in (
        trust_for_prediction(
            prediction, scene_repository=repository, validation=validation
        ),
        trust_for_recommendation(validation, underlying=None),
        trust_for_validation(validation),
    ):
        assert summary.validation_scope != ValidationScope.PRODUCTION_QUALIFIED


def test_disabled_recommendation_gate_cannot_lend_validation_scope(
    tmp_path: Path,
) -> None:
    repository, revision = _saved_revision(tmp_path)
    validation = _validation(
        revision, evidence_scope='owned_room', recommendation_gate='disabled'
    )

    summary = trust_for_recommendation(validation)

    # The gate is authoritative: a disabled recommendation may not render
    # the validation's strong scope — it renders as an unvalidated proposal.
    assert summary.validation_scope == ValidationScope.UNVALIDATED
    assert summary.evidence_class == EvidenceClass.PROPOSED


def test_historical_measurement_stays_historical_not_stale(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    measurement = _measurement(revision)
    repository.save(
        _scene(revision.document_id, shift=0.5),
        parent_revision_id=revision.revision_id,
    )

    current_view = trust_for_measurement(measurement, scene_repository=repository)
    history_view = trust_for_measurement(
        measurement, scene_repository=repository, historical=True
    )

    assert current_view.evidence_class == EvidenceClass.MEASURED
    assert current_view.freshness == FreshnessState.STALE
    assert history_view.freshness == FreshnessState.HISTORICAL
    assert history_view.invalidating_dependencies == ()


def test_validation_with_mismatched_model_fails_closed(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    validation = _validation(revision)  # model_id='model-1' != prediction's

    with pytest.raises(ValueError):
        trust_for_prediction(
            prediction, scene_repository=repository, validation=validation
        )


def test_validation_freshness_is_unknown_without_underlying(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)
    prediction = _prediction(repository, revision)
    validation = _validation(revision).model_copy(
        update={
            'model_id': prediction.model_id,
            'model_version': prediction.model_version,
        }
    )

    standalone = trust_for_validation(validation)
    bound = trust_for_validation(
        validation,
        underlying=trust_for_prediction(prediction, scene_repository=repository),
    )

    assert standalone.freshness == FreshnessState.UNKNOWN
    assert bound.freshness == FreshnessState.CURRENT


def test_measurement_evidence_type_derives_evidence_class(tmp_path: Path) -> None:
    repository, revision = _saved_revision(tmp_path)

    assert (
        trust_for_measurement(
            _measurement(revision, evidence_type='measured'),
            scene_repository=repository,
        ).evidence_class
        == EvidenceClass.MEASURED
    )
    assert (
        trust_for_measurement(
            _measurement(revision, evidence_type='predicted'),
            scene_repository=repository,
        ).evidence_class
        == EvidenceClass.PREDICTED
    )
