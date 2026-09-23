from __future__ import annotations

from hashlib import sha256
import json

import pytest

from htdt.cad_geometric_acoustics_adapter import (
    BoundaryMaterialContribution,
    DeterministicAcousticPath,
    DeterministicPathBandQuantity,
    SourceDirectivityContribution,
)
from htdt.cad_reflection_diagnostic import (
    build_interference_hypothesis,
    build_reflection_diagnostic_request,
    first_reflection_zones,
    match_etc_peaks,
    mirror_source_across_plane,
    preview_reflection_geometry,
)
from htdt.cad_scene import Direction3, Position3
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _hash(label: str) -> str:
    return sha256(label.encode('utf-8')).hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _authority(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test:{label}',
        authority_version='1',
        semantic_hash_sha256=_hash(label),
    )


SOLVER_REF = _authority('solver')


def _band(surface_id: str | None = None) -> DeterministicPathBandQuantity:
    return DeterministicPathBandQuantity(
        center_hz=100.0,
        spreading_factor_per_m2=1.0,
        source_directivity=SourceDirectivityContribution(
            dataset_id='dataset-1',
            dataset_version='1',
            dataset_semantic_sha256=_hash('dataset'),
            evaluation_semantic_sha256=_hash('eval'),
            frequency_hz=100.0,
            horizontal_angle_deg=0.0,
            vertical_angle_deg=0.0,
            magnitude_db=0.0,
            magnitude_linear=1.0,
            energy_factor=1.0,
        ),
        boundary_material=(
            None
            if surface_id is None
            else BoundaryMaterialContribution(
                source_surface_id=surface_id,
                material_authority=_authority('material'),
                frequency_hz=100.0,
                absorption=0.1,
                scattering=0.0,
                specular_energy_factor=0.9,
            )
        ),
        relative_energy_transport_per_m2=1.0,
    )


def _path(
    *,
    path_type: str,
    length_m: float,
    delay_s: float,
    surface_id: str | None = None,
    point: Position3 | None = None,
) -> DeterministicAcousticPath:
    kwargs = dict(
        source_entity_id='source-1',
        receiver_id='seat-1',
        receiver_entity_id='entity-seat',
        path_type=path_type,
        ordered_interaction_surface_ids=(
            () if surface_id is None else (surface_id,)
        ),
        ordered_interaction_points=(
            () if point is None else (point,)
        ),
        geometric_path_length_m=length_m,
        propagation_delay_s=delay_s,
        departure_direction=Direction3(x=1.0, y=0.0, z=0.0),
        arrival_direction=Direction3(x=-1.0, y=0.0, z=0.0),
        bands=(_band(surface_id),),
        solver_implementation_ref=SOLVER_REF,
    )
    probe = DeterministicAcousticPath.model_construct(
        path_id='deterministic-acoustic-path:' + '0' * 64,
        semantic_sha256='0' * 64,
        **kwargs,
    )
    digest = _digest(probe.semantic_payload())
    return DeterministicAcousticPath(
        path_id=f'deterministic-acoustic-path:{digest}',
        semantic_sha256=digest,
        **kwargs,
    )


def _request(direct: DeterministicAcousticPath, reflection: DeterministicAcousticPath):
    return build_reflection_diagnostic_request(
        scene_revision_id='rev-1',
        scene_content_hash=_hash('scene'),
        source_entity_id='source-1',
        receiver_id='seat-1',
        receiver_entity_id='entity-seat',
        direct_path_id=direct.path_id,
        reflection_path_id=reflection.path_id,
        speed_of_sound_m_s=343.0,
        solver_implementation_ref=SOLVER_REF,
    )


def _pair(
    direct_len: float = 5.0,
    reflection_len: float = 7.0,
):
    direct = _path(
        path_type='direct',
        length_m=direct_len,
        delay_s=direct_len / 343.0,
    )
    reflection = _path(
        path_type='specular_reflection',
        length_m=reflection_len,
        delay_s=reflection_len / 343.0,
        surface_id='wall-left',
        point=Position3(x_m=0.0, y_m=2.0, z_m=1.0),
    )
    return direct, reflection, _request(direct, reflection)


def test_request_identity_and_pair_validation() -> None:
    direct, reflection, request = _pair()
    assert request.request_id.startswith('reflection-diagnostic-request:')
    other = _path(path_type='direct', length_m=4.0, delay_s=4.0 / 343.0)
    with pytest.raises(ValueError, match='direct path'):
        build_interference_hypothesis(request, other, reflection)


def test_hypothesis_reports_delay_delta_and_labels() -> None:
    direct, reflection, request = _pair(direct_len=5.0, reflection_len=7.0)
    hypothesis = build_interference_hypothesis(request, direct, reflection)
    assert hypothesis.excess_path_length_m == pytest.approx(2.0)
    assert hypothesis.excess_delay_s == pytest.approx(2.0 / 343.0)
    assert hypothesis.phase_semantics == 'coherent_phase_unavailable_not_synthesized'
    assert not hypothesis.comb_notch_hz  # gated without phase authority


def test_comb_frequencies_only_with_phase_authority() -> None:
    direct, reflection, request = _pair(direct_len=5.0, reflection_len=6.0)
    hypothesis = build_interference_hypothesis(
        request, direct, reflection, phase_authority_supplied=True,
        max_notch_hz=300.0,
    )
    # delta L = 1.0 m → notches at c*(2n+1)/2 = 171.5, 514.5(>300 cut)
    assert hypothesis.comb_notch_hz == pytest.approx((171.5,))
    assert hypothesis.phase_semantics == 'phase_authority_supplied'


def test_mirror_and_preview_geometry() -> None:
    mirrored = mirror_source_across_plane(
        Position3(x_m=1.0, y_m=2.0, z_m=1.0),
        plane_point=Position3(x_m=0.0, y_m=0.0, z_m=0.0),
        plane_normal=(1.0, 0.0, 0.0),
    )
    assert mirrored.x_m == pytest.approx(-1.0)
    assert mirrored.y_m == pytest.approx(2.0)

    _, _, request = _pair()
    preview = preview_reflection_geometry(
        request,
        candidate_source=Position3(x_m=1.0, y_m=2.0, z_m=1.0),
        receiver_position=Position3(x_m=1.0, y_m=0.0, z_m=1.0),
        plane_point=Position3(x_m=3.0, y_m=0.0, z_m=0.0),
        plane_normal=(1.0, 0.0, 0.0),
    )
    # mirror x = 5 → segment to receiver crosses plane x=3
    assert preview.reflection_point.x_m == pytest.approx(3.0)
    direct = 2.0
    specular = preview.specular_length_m
    assert specular > direct
    assert preview.excess_path_length_m == pytest.approx(specular - direct)
    assert preview.excess_delay_s == pytest.approx(
        (specular - direct) / 343.0
    )


def test_first_reflection_zones_group_per_surface() -> None:
    direct = _path(path_type='direct', length_m=5.0, delay_s=5.0 / 343.0)
    r1 = _path(
        path_type='specular_reflection',
        length_m=6.0,
        delay_s=6.0 / 343.0,
        surface_id='wall-left',
        point=Position3(x_m=0.0, y_m=1.0, z_m=1.0),
    )
    r2 = _path(
        path_type='specular_reflection',
        length_m=6.5,
        delay_s=6.5 / 343.0,
        surface_id='wall-left',
        point=Position3(x_m=0.0, y_m=3.0, z_m=1.0),
    )
    request = _request(direct, r1)
    zones = first_reflection_zones(request, (direct, r1, r2))
    assert len(zones) == 1
    assert zones[0].surface_id == 'wall-left'
    assert len(zones[0].boundary_points) == 2
    assert zones[0].centroid.y_m == pytest.approx(2.0)


def test_etc_match_unique_ambiguous_unsupported() -> None:
    direct, reflection, request = _pair(direct_len=5.0, reflection_len=7.0)
    # second reflection at nearly the same delay
    reflection2 = _path(
        path_type='specular_reflection',
        length_m=7.0,
        delay_s=7.0 / 343.0,
        surface_id='wall-right',
        point=Position3(x_m=5.0, y_m=2.0, z_m=1.0),
    )
    peaks = (
        5.0 / 343.0 + 1e-4,          # matches direct within 2 ms
        7.0 / 343.0,                 # matches two reflections -> ambiguous
        0.1,                         # unsupported
    )
    matches = match_etc_peaks(
        request, (direct, reflection, reflection2), peaks
    )
    assert matches[0].verdict == 'match'
    assert matches[0].matched_path_id == direct.path_id
    assert matches[1].verdict == 'ambiguous'
    assert matches[2].verdict == 'unsupported'
