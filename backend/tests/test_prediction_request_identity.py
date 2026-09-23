from __future__ import annotations

from pathlib import Path

from htdt.cad_prediction_request import rectangular_geometry_request_identity
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='prediction-request',
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.2, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.3, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def test_request_identity_matches_adapter_output_exactly(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision

    identity = rectangular_geometry_request_identity(
        revision,
        'point-mlp',
        max_mode_hz=140.0,
        sound_speed_m_s=342.5,
    )
    results = analyze_native_rectangular_geometry(
        revision,
        'point-mlp',
        max_mode_hz=140.0,
        sound_speed_m_s=342.5,
        constraint_workspace_hash='1' * 64,
    )

    assert identity.geometry_compatibility == 'exact_for_model_geometry'
    assert all(item.model_id == identity.model_id for item in results)
    assert all(item.model_version == identity.model_version for item in results)
    assert all(item.parameters_json == identity.parameters_json for item in results)
    assert all(item.input_snapshot_json == identity.input_snapshot_json for item in results)
    assert all(item.input_hash == identity.input_hash for item in results)
