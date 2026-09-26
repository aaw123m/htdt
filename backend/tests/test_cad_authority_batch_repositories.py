"""Persistence tests for the #817/#818 authority-batch repositories.

Covers the signal-path, lighting, tactile, usable-output, photometric, and
colorimetry stores: append-only identity, row-vs-payload integrity checks,
explicit current-selection records, and scoped selections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from htdt.cad_colorimetry import (
    build_video_color_measurement_set,
    build_video_color_target_profile,
)
from htdt.cad_colorimetry_repository import (
    CadColorimetryRepository,
    ColorimetryConflictError,
    ColorimetryIntegrityError,
)
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_lighting import build_lighting_scene
from htdt.cad_lighting_repository import (
    CadLightingRepository,
    LightingConflictError,
    LightingIntegrityError,
)
from htdt.cad_photometric import (
    build_ambient_reflectance_profile,
    build_projector_image_performance_profile,
    build_screen_optical_profile,
)
from htdt.cad_photometric_repository import (
    CadPhotometricRepository,
    PhotometricConflictError,
    PhotometricIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_signal_path import (
    SignalPathEdge,
    SignalPathNode,
    SignalPort,
    build_av_signal_path,
)
from htdt.cad_signal_path_repository import (
    CadSignalPathRepository,
    SignalPathConflictError,
    SignalPathIntegrityError,
)
from htdt.cad_tactile import (
    build_tactile_actuator_definition,
    build_tactile_processing_profile,
)
from htdt.cad_tactile_repository import (
    CadTactileRepository,
    TactileConflictError,
    TactileIntegrityError,
)
from htdt.cad_usable_output import build_source_usable_output_profile
from htdt.cad_usable_output_repository import (
    CadUsableOutputRepository,
    UsableOutputConflictError,
    UsableOutputIntegrityError,
)


def _provenance() -> tuple[EquipmentDataProvenance, ...]:
    return (
        EquipmentDataProvenance(
            evidence_kind='manufacturer',
            source_name='Example Co.',
            source_version='1',
            source_reference='published defaults',
            source_sha256='a' * 64,
        ),
    )


def _signal_path(version: str, label: str = 'main'):
    return build_av_signal_path(
        path_id='path-main',
        version=version,
        nodes=(
            SignalPathNode(
                node_id='src',
                kind='source',
                ports=(
                    SignalPort(
                        port_id='out1',
                        direction='out',
                        medium='hdmi',
                    ),
                ),
            ),
            SignalPathNode(
                node_id='avr',
                kind='avr',
                ports=(
                    SignalPort(
                        port_id='in1',
                        direction='in',
                        medium='hdmi',
                    ),
                ),
            ),
        ),
        edges=(
            SignalPathEdge(
                edge_id='e1',
                from_node_id='src',
                from_port_id='out1',
                to_node_id='avr',
                to_port_id='in1',
                medium='hdmi',
            ),
        ),
        label=label,
        provenance=_provenance(),
    )


def _lighting_scene(version: str, label: str = 'movie'):
    return build_lighting_scene(
        scene_id='scene-movie',
        version=version,
        label=label,
        provenance=_provenance(),
    )


def _actuator(version: str, model: str | None = 'ex-1'):
    return build_tactile_actuator_definition(
        definition_id='act-seat-1',
        version=version,
        tech_class='voice_coil',
        manufacturer='Tactile Co.' if model is not None else None,
        model=model,
        provenance=_provenance(),
    )


def _tactile_profile(version: str, gain_db: float | None = 0.0):
    return build_tactile_processing_profile(
        profile_id='tactile-main',
        version=version,
        source_bus='lfe',
        gain_db=gain_db,
        provenance=_provenance(),
    )


def _usable_output(version: str, excitation: str | None = 'sweep'):
    return build_source_usable_output_profile(
        profile_id='usable-src-1',
        version=version,
        equipment_definition_id='eq-def-1',
        equipment_definition_version='3',
        equipment_definition_sha256='b' * 64,
        excitation_method=excitation,
        provenance=_provenance(),
    )


def _photometric_profile(version: str, label: str | None = 'bright'):
    return build_projector_image_performance_profile(
        profile_id='proj-perf-1',
        version=version,
        label=label,
        projector_specification_id='pj-spec-1',
        projector_specification_version='2',
        projector_specification_sha256='c' * 64,
        provenance=_provenance(),
    )


def _screen_optical(version: str, gain: float | None = 1.0):
    return build_screen_optical_profile(
        profile_id='screen-opt-1',
        version=version,
        nominal_gain=gain,
        provenance=_provenance(),
    )


def _ambient_reflectance(version: str):
    return build_ambient_reflectance_profile(
        profile_id='ambient-1',
        version=version,
        surface_kind='projection',
        screen_optical_profile_id='screen-opt-1',
        screen_optical_profile_version='1',
        screen_optical_profile_sha256='d' * 64,
        diffuse_reflectance_fraction=0.02,
        evidence_kind='measured_reflectance',
        provenance=_provenance(),
    )


def _color_target(version: str, peak: float | None = 1000.0):
    return build_video_color_target_profile(
        target_id='target-hdr',
        version=version,
        eotf='pq_st2084',
        peak_luminance_cd_m2=peak,
        provenance=_provenance(),
    )


def _measurement_set(set_id: str = 'ms-1', meter: str = 'i1 Pro'):
    return build_video_color_measurement_set(
        measurement_set_id=set_id,
        measured_at_utc='2026-01-01T00:00:00+00:00',
        surface_entity_id='screen-1',
        meter=meter,
        samples=(),
        provenance=_provenance(),
    )


# ----------------------------------------------------------------------
# Signal paths


def test_signal_path_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadSignalPathRepository(SceneRepository(path))
    path_model = _signal_path('1')
    repo.save_path(path_model, document_id='doc-1')

    assert repo.get_path('doc-1', 'path-main', '1') == path_model
    assert repo.get_path_by_hash(path_model.path_sha256) == path_model

    reopened = CadSignalPathRepository(SceneRepository(path))
    assert reopened.get_path('doc-1', 'path-main', '1') == path_model
    assert reopened.list_paths('doc-1') == (path_model,)
    assert reopened.list_paths('doc-2') == ()


def test_signal_path_append_only_identity(tmp_path: Path) -> None:
    repo = CadSignalPathRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    path_model = _signal_path('1')
    repo.save_path(path_model, document_id='doc-1')
    repo.save_path(path_model, document_id='doc-1')  # identical: no-op
    assert len(repo.list_paths('doc-1')) == 1

    divergent = _signal_path('1', label='renamed')
    assert divergent.path_sha256 != path_model.path_sha256
    with pytest.raises(SignalPathConflictError):
        repo.save_path(divergent, document_id='doc-1')


def test_signal_path_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadSignalPathRepository(SceneRepository(path))
    repo.save_path(_signal_path('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_signal_paths SET path_sha256=? WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(SignalPathIntegrityError):
        repo.get_path('doc-1', 'path-main', '1')


def test_signal_path_current_selection_is_explicit(tmp_path: Path) -> None:
    repo = CadSignalPathRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    v1 = _signal_path('1')
    v2 = _signal_path('2', label='v2')
    repo.save_path(v1, document_id='doc-1')
    repo.save_path(v2, document_id='doc-1')

    assert repo.current_selection('doc-1') is None
    with pytest.raises(SignalPathIntegrityError):
        repo.select_path('doc-1', _signal_path('9'))

    sel1 = repo.select_path('doc-1', v1)
    assert repo.current_path('doc-1') == v1
    sel2 = repo.select_path('doc-1', v2)
    assert repo.current_selection('doc-1') == sel2
    assert repo.current_path('doc-1') == v2
    assert repo.list_selections('doc-1') == (sel1, sel2)


# ----------------------------------------------------------------------
# Lighting scenes


def test_lighting_scene_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadLightingRepository(SceneRepository(path))
    scene = _lighting_scene('1')
    repo.save_scene(scene, document_id='doc-1')

    assert repo.get_scene('doc-1', 'scene-movie', '1') == scene
    assert repo.get_scene_by_hash(scene.scene_sha256) == scene
    reopened = CadLightingRepository(SceneRepository(path))
    assert reopened.list_scenes('doc-1') == (scene,)


def test_lighting_scene_append_only_identity(tmp_path: Path) -> None:
    repo = CadLightingRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    scene = _lighting_scene('1')
    repo.save_scene(scene, document_id='doc-1')
    repo.save_scene(scene, document_id='doc-1')
    assert len(repo.list_scenes('doc-1')) == 1

    divergent = _lighting_scene('1', label='sports')
    with pytest.raises(LightingConflictError):
        repo.save_scene(divergent, document_id='doc-1')


def test_lighting_scene_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadLightingRepository(SceneRepository(path))
    repo.save_scene(_lighting_scene('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_lighting_scenes SET scene_sha256=? '
            'WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(LightingIntegrityError):
        repo.get_scene('doc-1', 'scene-movie', '1')


def test_lighting_selection_requires_persisted_scene(tmp_path: Path) -> None:
    repo = CadLightingRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    with pytest.raises(LightingIntegrityError):
        repo.select_scene('doc-1', _lighting_scene('1'))


def test_lighting_current_selection_history(tmp_path: Path) -> None:
    repo = CadLightingRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    v1 = _lighting_scene('1')
    v2 = _lighting_scene('2', label='intermission')
    repo.save_scene(v1, document_id='doc-1')
    repo.save_scene(v2, document_id='doc-1')

    sel1 = repo.select_scene('doc-1', v1)
    sel2 = repo.select_scene('doc-1', v2)
    assert repo.current_selection('doc-1') == sel2
    assert repo.current_scene('doc-1') == v2
    assert repo.list_selections('doc-1') == (sel1, sel2)


# ----------------------------------------------------------------------
# Tactile


def test_tactile_actuator_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadTactileRepository(SceneRepository(path))
    definition = _actuator('1')
    repo.save_actuator_definition(definition, document_id='doc-1')

    assert (
        repo.get_actuator_definition('doc-1', 'act-seat-1', '1') == definition
    )
    reopened = CadTactileRepository(SceneRepository(path))
    assert reopened.list_actuator_definitions('doc-1') == (definition,)


def test_tactile_actuator_append_only_identity(tmp_path: Path) -> None:
    repo = CadTactileRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    definition = _actuator('1')
    repo.save_actuator_definition(definition, document_id='doc-1')
    repo.save_actuator_definition(definition, document_id='doc-1')
    assert len(repo.list_actuator_definitions('doc-1')) == 1

    divergent = _actuator('1', model='ex-2')
    with pytest.raises(TactileConflictError):
        repo.save_actuator_definition(divergent, document_id='doc-1')


def test_tactile_profile_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadTactileRepository(SceneRepository(path))
    repo.save_profile(_tactile_profile('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_tactile_processing_profiles SET profile_sha256=? '
            'WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(TactileIntegrityError):
        repo.get_profile('doc-1', 'tactile-main', '1')


def test_tactile_current_profile_selection(tmp_path: Path) -> None:
    repo = CadTactileRepository(SceneRepository(tmp_path / 'cad.sqlite3'))
    v1 = _tactile_profile('1')
    v2 = _tactile_profile('2', gain_db=-3.0)
    repo.save_profile(v1, document_id='doc-1')
    repo.save_profile(v2, document_id='doc-1')

    with pytest.raises(TactileIntegrityError):
        repo.select_profile('doc-1', _tactile_profile('9'))

    sel1 = repo.select_profile('doc-1', v1)
    assert repo.current_profile('doc-1') == v1
    sel2 = repo.select_profile('doc-1', v2)
    assert repo.current_selection('doc-1') == sel2
    assert repo.list_selections('doc-1') == (sel1, sel2)


# ----------------------------------------------------------------------
# Usable output


def test_usable_output_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadUsableOutputRepository(SceneRepository(path))
    profile = _usable_output('1')
    repo.save_profile(profile, document_id='doc-1')

    assert repo.get_profile('doc-1', 'usable-src-1', '1') == profile
    assert repo.get_profile_by_hash(profile.profile_sha256) == profile
    reopened = CadUsableOutputRepository(SceneRepository(path))
    assert reopened.list_profiles('doc-1') == (profile,)
    assert reopened.list_profiles('doc-1', 'eq-def-1') == (profile,)
    assert reopened.list_profiles('doc-1', 'eq-def-9') == ()


def test_usable_output_append_only_identity(tmp_path: Path) -> None:
    repo = CadUsableOutputRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _usable_output('1')
    repo.save_profile(profile, document_id='doc-1')
    repo.save_profile(profile, document_id='doc-1')
    assert len(repo.list_profiles('doc-1')) == 1

    divergent = _usable_output('1', excitation='pink')
    with pytest.raises(UsableOutputConflictError):
        repo.save_profile(divergent, document_id='doc-1')


def test_usable_output_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadUsableOutputRepository(SceneRepository(path))
    repo.save_profile(_usable_output('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_usable_output_profiles SET equipment_definition_id=? '
            'WHERE document_id=?',
            ('eq-def-9', 'doc-1'),
        )
    with pytest.raises(UsableOutputIntegrityError):
        repo.get_profile('doc-1', 'usable-src-1', '1')


def test_usable_output_selections_scoped_per_equipment(
    tmp_path: Path,
) -> None:
    repo = CadUsableOutputRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _usable_output('1')
    repo.save_profile(profile, document_id='doc-1')

    with pytest.raises(UsableOutputIntegrityError):
        repo.select_profile('doc-1', 'eq-def-1', _usable_output('9'))

    sel = repo.select_profile('doc-1', 'eq-def-1', profile)
    assert sel.equipment_definition_id == 'eq-def-1'
    assert repo.current_selection('doc-1', 'eq-def-1') == sel
    assert repo.current_profile('doc-1', 'eq-def-1') == profile
    assert repo.current_selection('doc-1', 'eq-def-2') is None


# ----------------------------------------------------------------------
# Photometric / screen optical / ambient reflectance


def test_photometric_profile_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPhotometricRepository(SceneRepository(path))
    profile = _photometric_profile('1')
    repo.save_photometric_profile(profile, document_id='doc-1')

    assert (
        repo.get_photometric_profile('doc-1', 'proj-perf-1', '1') == profile
    )
    assert (
        repo.get_photometric_profile_by_hash(profile.profile_sha256)
        == profile
    )
    reopened = CadPhotometricRepository(SceneRepository(path))
    assert reopened.list_photometric_profiles('doc-1') == (profile,)


def test_photometric_profile_append_only_identity(tmp_path: Path) -> None:
    repo = CadPhotometricRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _photometric_profile('1')
    repo.save_photometric_profile(profile, document_id='doc-1')
    repo.save_photometric_profile(profile, document_id='doc-1')
    assert len(repo.list_photometric_profiles('doc-1')) == 1

    divergent = _photometric_profile('1', label='eco')
    with pytest.raises(PhotometricConflictError):
        repo.save_photometric_profile(divergent, document_id='doc-1')


def test_photometric_current_selection(tmp_path: Path) -> None:
    repo = CadPhotometricRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    v1 = _photometric_profile('1')
    v2 = _photometric_profile('2', label='eco')
    repo.save_photometric_profile(v1, document_id='doc-1')
    repo.save_photometric_profile(v2, document_id='doc-1')

    assert repo.current_photometric_selection('doc-1') is None
    with pytest.raises(PhotometricIntegrityError):
        repo.select_photometric_profile('doc-1', _photometric_profile('9'))

    sel1 = repo.select_photometric_profile('doc-1', v1)
    assert repo.current_photometric_profile('doc-1') == v1
    sel2 = repo.select_photometric_profile('doc-1', v2)
    assert repo.current_photometric_selection('doc-1') == sel2
    assert repo.list_photometric_selections('doc-1') == (sel1, sel2)


def test_screen_optical_profile_and_scoped_selection(tmp_path: Path) -> None:
    repo = CadPhotometricRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _screen_optical('1')
    repo.save_screen_optical_profile(profile, document_id='doc-1')

    assert (
        repo.get_screen_optical_profile('doc-1', 'screen-opt-1', '1')
        == profile
    )
    assert (
        repo.get_screen_optical_profile_by_hash(profile.profile_sha256)
        == profile
    )

    with pytest.raises(PhotometricIntegrityError):
        repo.select_screen_optical_profile(
            'doc-1', 'screen-1', _screen_optical('9')
        )

    sel = repo.select_screen_optical_profile('doc-1', 'screen-1', profile)
    assert sel.screen_entity_id == 'screen-1'
    assert (
        repo.current_screen_optical_profile('doc-1', 'screen-1') == profile
    )
    assert repo.current_screen_optical_selection('doc-1', 'screen-2') is None


def test_screen_optical_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPhotometricRepository(SceneRepository(path))
    repo.save_screen_optical_profile(_screen_optical('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_screen_optical_profiles SET profile_sha256=? '
            'WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(PhotometricIntegrityError):
        repo.get_screen_optical_profile('doc-1', 'screen-opt-1', '1')


def test_ambient_reflectance_profile_save_get(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadPhotometricRepository(SceneRepository(path))
    profile = _ambient_reflectance('1')
    repo.save_ambient_reflectance_profile(profile, document_id='doc-1')

    assert (
        repo.get_ambient_reflectance_profile('doc-1', 'ambient-1', '1')
        == profile
    )
    reopened = CadPhotometricRepository(SceneRepository(path))
    assert reopened.list_ambient_reflectance_profiles('doc-1') == (profile,)

    divergent = build_ambient_reflectance_profile(
        profile_id='ambient-1',
        version='1',
        surface_kind='projection',
        diffuse_reflectance_fraction=0.5,
        evidence_kind='user_declared',
    )
    with pytest.raises(PhotometricConflictError):
        reopened.save_ambient_reflectance_profile(
            divergent, document_id='doc-1'
        )


# ----------------------------------------------------------------------
# Colorimetry


def test_color_target_save_get_reopen(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadColorimetryRepository(SceneRepository(path))
    profile = _color_target('1')
    repo.save_target_profile(profile, document_id='doc-1')

    assert repo.get_target_profile('doc-1', 'target-hdr', '1') == profile
    assert repo.get_target_profile_by_hash(profile.target_sha256) == profile
    reopened = CadColorimetryRepository(SceneRepository(path))
    assert reopened.list_target_profiles('doc-1') == (profile,)


def test_color_target_append_only_identity(tmp_path: Path) -> None:
    repo = CadColorimetryRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    profile = _color_target('1')
    repo.save_target_profile(profile, document_id='doc-1')
    repo.save_target_profile(profile, document_id='doc-1')
    assert len(repo.list_target_profiles('doc-1')) == 1

    divergent = _color_target('1', peak=400.0)
    with pytest.raises(ColorimetryConflictError):
        repo.save_target_profile(divergent, document_id='doc-1')


def test_color_target_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadColorimetryRepository(SceneRepository(path))
    repo.save_target_profile(_color_target('1'), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_color_target_profiles SET target_sha256=? '
            'WHERE document_id=?',
            ('f' * 64, 'doc-1'),
        )
    with pytest.raises(ColorimetryIntegrityError):
        repo.get_target_profile('doc-1', 'target-hdr', '1')


def test_color_target_selection_requires_persisted_profile(
    tmp_path: Path,
) -> None:
    repo = CadColorimetryRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    with pytest.raises(ColorimetryIntegrityError):
        repo.select_target_profile('doc-1', _color_target('1'))


def test_color_target_current_selection_history(tmp_path: Path) -> None:
    repo = CadColorimetryRepository(
        SceneRepository(tmp_path / 'cad.sqlite3')
    )
    v1 = _color_target('1')
    v2 = _color_target('2', peak=400.0)
    repo.save_target_profile(v1, document_id='doc-1')
    repo.save_target_profile(v2, document_id='doc-1')

    sel1 = repo.select_target_profile('doc-1', v1)
    assert repo.current_target_profile('doc-1') == v1
    sel2 = repo.select_target_profile('doc-1', v2)
    assert repo.current_selection('doc-1') == sel2
    assert repo.current_target_profile('doc-1') == v2
    assert repo.list_selections('doc-1') == (sel1, sel2)


def test_measurement_set_save_get_and_surface_index(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadColorimetryRepository(SceneRepository(path))
    measurement_set = _measurement_set()
    repo.save_measurement_set(measurement_set, document_id='doc-1')

    assert (
        repo.get_measurement_set('doc-1', 'ms-1') == measurement_set
    )
    reopened = CadColorimetryRepository(SceneRepository(path))
    assert reopened.list_measurement_sets('doc-1') == (measurement_set,)
    assert (
        reopened.list_measurement_sets('doc-1', 'screen-1')
        == (measurement_set,)
    )
    assert reopened.list_measurement_sets('doc-1', 'screen-9') == ()

    repo.save_measurement_set(measurement_set, document_id='doc-1')  # no-op
    assert len(repo.list_measurement_sets('doc-1')) == 1

    divergent = _measurement_set(meter='i1 Pro 2')
    with pytest.raises(ColorimetryConflictError):
        repo.save_measurement_set(divergent, document_id='doc-1')


def test_measurement_set_row_vs_payload_integrity(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'
    repo = CadColorimetryRepository(SceneRepository(path))
    repo.save_measurement_set(_measurement_set(), document_id='doc-1')

    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE cad_color_measurement_sets SET surface_entity_id=? '
            'WHERE document_id=?',
            ('screen-9', 'doc-1'),
        )
    with pytest.raises(ColorimetryIntegrityError):
        repo.get_measurement_set('doc-1', 'ms-1')
