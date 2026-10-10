
from math import isfinite

import numpy as np

from ..domain.acoustic_benchmark import AcousticMaterial
from .acoustic_pffdtd_adapter import acoustic_position_array
from ..domain.cad_candidate_wave_contracts import (
    PFFDTD_IMPEDANCE_MAPPING_ID,
    PFFDTD_IMPEDANCE_MAPPING_VERSION,
)


def pffdtd_impedance_mapping_authority_payload() -> dict[str, object]:
    """Canonical exact mapping authority shared by R100B and R130B."""

    return {
        'authority_kind': 'pffdtd_impedance_mapping',
        'mapping_id': PFFDTD_IMPEDANCE_MAPPING_ID,
        'mapping_version': PFFDTD_IMPEDANCE_MAPPING_VERSION,
        'physical_quantity_type': 'specific_acoustic_impedance',
        'pffdtd_representation': 'normalized_DEF',
        'mapping': 'exact_frequency_independent_resistive_Z_to_DEF',
        'reactive_impedance_supported': False,
        'frequency_dependent_impedance_supported': False,
        'scalar_absorption_conversion': False,
    }

def compile_frequency_independent_resistive_impedance_boundary(
    *,
    material: AcousticMaterial,
    frequencies_hz: tuple[float, ...],
    density_kg_m3: float,
    sound_speed_m_s: float,
) -> dict[str, object]:
    """Compile the exact R100B-authorized PFFDTD impedance subset.

    This is the single reusable authority for the R100B/R130B mapping. It
    deliberately accepts only an explicit specific-impedance table whose
    samples exactly match the requested frequency grid and whose impedance is
    finite, positive, frequency-independent and purely resistive. No scalar
    absorption or table fitting participates.
    """

    material = AcousticMaterial.model_validate(material)
    frequencies_hz = tuple(float(value) for value in frequencies_hz)
    if (
        not frequencies_hz
        or frequencies_hz != tuple(sorted(set(frequencies_hz)))
        or any(not isfinite(value) or value <= 0.0 for value in frequencies_hz)
    ):
        raise ValueError(
            'PFFDTD direct impedance adapter requires finite positive unique sorted frequencies'
        )
    if material.wave_model != 'specific_impedance_table':
        raise ValueError(
            f'PFFDTD impedance adapter refuses unsupported wave material {material.material_id}'
        )

    table = material.specific_impedance
    table_frequencies = tuple(float(item.frequency_hz) for item in table)
    if table_frequencies != frequencies_hz:
        raise ValueError(
            'PFFDTD direct impedance adapter requires impedance samples on the exact '
            'requested frequency grid'
        )

    reactances = tuple(float(item.reactance_pa_s_m) for item in table)
    if any(value != 0.0 for value in reactances):
        raise ValueError(
            'PFFDTD direct impedance adapter refuses reactive impedance authority; '
            'no table-to-DEF fitting policy is authorized'
        )

    resistances = tuple(float(item.resistance_pa_s_m) for item in table)
    if not resistances or any(
        not isfinite(value) or value <= 0.0 for value in resistances
    ):
        raise ValueError(
            'PFFDTD direct impedance adapter requires finite positive resistance'
        )
    resistance = resistances[0]
    if any(value != resistance for value in resistances[1:]):
        raise ValueError(
            'PFFDTD direct impedance adapter refuses frequency-varying resistance; '
            'no table-to-DEF fitting policy is authorized'
        )

    density = float(density_kg_m3)
    sound_speed = float(sound_speed_m_s)
    characteristic_impedance = density * sound_speed
    if (
        not isfinite(density)
        or density <= 0.0
        or not isfinite(sound_speed)
        or sound_speed <= 0.0
        or not isfinite(characteristic_impedance)
        or characteristic_impedance <= 0.0
    ):
        raise ValueError('density/sound-speed authority produces invalid rho*c')

    normalized_impedance = resistance / characteristic_impedance
    if not isfinite(normalized_impedance) or normalized_impedance <= 0.0:
        raise ValueError('normalized PFFDTD impedance must be finite and positive')
    normalized_admittance = 1.0 / normalized_impedance
    coefficients = np.asarray(
        [[0.0, normalized_impedance, 0.0]],
        dtype=np.float64,
    )

    return {
        'material_id': material.material_id,
        'material_version': material.version,
        'material_provenance': material.provenance,
        'frequencies_hz': frequencies_hz,
        'physical_resistance_pa_s_m': resistance,
        'physical_reactance_pa_s_m': 0.0,
        'density_kg_m3': density,
        'sound_speed_m_s': sound_speed,
        'characteristic_impedance_pa_s_m': characteristic_impedance,
        'normalized_impedance': normalized_impedance,
        'normalized_admittance': normalized_admittance,
        'def_coefficients': coefficients,
        'mapping': 'exact_frequency_independent_resistive_Z_to_DEF',
        'mapping_id': PFFDTD_IMPEDANCE_MAPPING_ID,
        'mapping_version': PFFDTD_IMPEDANCE_MAPPING_VERSION,
    }

def compile_impedance_fixture_boundary(fixture) -> dict[str, object]:
    """Map the canonical R100B fixture through the reusable exact subset."""

    if fixture.comparison.frequency_grid.kind != 'explicit':
        raise ValueError(
            'PFFDTD impedance adapter requires an explicit R100A frequency grid'
        )
    frequencies_hz = tuple(
        float(value) for value in fixture.comparison.frequency_grid.values_hz
    )
    if not frequencies_hz:
        raise ValueError(
            'PFFDTD impedance adapter requires non-empty fixture frequencies'
        )

    boundary_by_id = {item.boundary_id: item for item in fixture.boundaries}
    material_by_id = {item.material_id: item for item in fixture.materials}
    impedance_material_ids: set[str] = set()
    has_rigid = False
    for face in fixture.regions[0].faces:
        boundary = boundary_by_id.get(face.boundary_id)
        if boundary is None:
            raise ValueError(
                f'unknown boundary on face {face.face_id}: {face.boundary_id}'
            )
        material = material_by_id.get(boundary.material_id)
        if material is None:
            raise ValueError(
                f'unknown material on boundary {boundary.boundary_id}'
            )
        if material.wave_model == 'rigid':
            has_rigid = True
        elif material.wave_model == 'specific_impedance_table':
            impedance_material_ids.add(material.material_id)
        else:
            raise ValueError(
                f'PFFDTD impedance adapter refuses unsupported wave material {material.material_id}'
            )

    if not has_rigid:
        raise ValueError(
            'PFFDTD impedance fixture requires explicit rigid companion boundaries'
        )
    if len(impedance_material_ids) != 1:
        raise ValueError(
            'PFFDTD impedance fixture requires exactly one explicit impedance material'
        )

    material = material_by_id[next(iter(impedance_material_ids))]
    return compile_frequency_independent_resistive_impedance_boundary(
        material=material,
        frequencies_hz=frequencies_hz,
        density_kg_m3=float(fixture.environment.density_kg_m3),
        sound_speed_m_s=float(fixture.environment.sound_speed_m_s),
    )

def compile_impedance_fixture_model(fixture) -> dict[str, object]:
    """Compile the canonical closed impedance fixture into PFFDTD material groups."""

    if fixture.portals or fixture.terminations or fixture.obstacles:
        raise ValueError('PFFDTD impedance fixture compiler expects one closed obstacle-free region')
    if len(fixture.regions) != 1 or len(fixture.sources) != 1 or len(fixture.receivers) != 1:
        raise ValueError('PFFDTD impedance fixture compiler expects one region/source/receiver')

    boundary_info = compile_impedance_fixture_boundary(fixture)
    impedance_material_id = str(boundary_info['material_id'])
    boundary_by_id = {item.boundary_id: item for item in fixture.boundaries}
    material_by_id = {item.material_id: item for item in fixture.materials}
    region = fixture.regions[0]
    vertex_by_id = {
        vertex.vertex_id: acoustic_position_array(vertex.position)
        for vertex in region.vertices
    }
    all_points = np.asarray(list(vertex_by_id.values()), dtype=np.float64)
    region_centroid = all_points.mean(axis=0)

    faces_by_group: dict[str, list[object]] = {}
    for face in region.faces:
        boundary = boundary_by_id.get(face.boundary_id)
        if boundary is None:
            raise ValueError(f'unknown boundary on face {face.face_id}: {face.boundary_id}')
        material = material_by_id.get(boundary.material_id)
        if material is None:
            raise ValueError(f'unknown material on boundary {boundary.boundary_id}')
        if material.wave_model == 'rigid':
            group = '_RIGID'
        elif material.material_id == impedance_material_id:
            group = impedance_material_id
        else:
            raise ValueError(f'unexpected impedance fixture material {material.material_id}')
        faces_by_group.setdefault(group, []).append(face)

    if set(faces_by_group) != {'_RIGID', impedance_material_id}:
        raise ValueError('PFFDTD impedance fixture compiler requires rigid and impedance groups')

    mats_hash: dict[str, object] = {}
    for group, faces in faces_by_group.items():
        local_vertex_ids: list[str] = []
        for face in faces:
            for vertex_id in face.vertex_ids:
                if vertex_id not in local_vertex_ids:
                    local_vertex_ids.append(vertex_id)
        local_index = {vertex_id: index for index, vertex_id in enumerate(local_vertex_ids)}
        local_points = np.asarray(
            [vertex_by_id[vertex_id] for vertex_id in local_vertex_ids],
            dtype=np.float64,
        )

        triangles: list[list[int]] = []
        for face in faces:
            face_points = np.asarray(
                [vertex_by_id[item] for item in face.vertex_ids],
                dtype=np.float64,
            )
            face_centroid = face_points.mean(axis=0)
            for offset in range(1, len(face.vertex_ids) - 1):
                tri_ids = [
                    face.vertex_ids[0],
                    face.vertex_ids[offset],
                    face.vertex_ids[offset + 1],
                ]
                tri_points = np.asarray(
                    [vertex_by_id[item] for item in tri_ids],
                    dtype=np.float64,
                )
                normal = np.cross(
                    tri_points[1] - tri_points[0],
                    tri_points[2] - tri_points[0],
                )
                if float(np.dot(normal, face_centroid - region_centroid)) < 0.0:
                    tri_ids = [tri_ids[0], tri_ids[2], tri_ids[1]]
                triangles.append([local_index[item] for item in tri_ids])

        mats_hash[group] = {
            'tris': triangles,
            'pts': local_points.tolist(),
            'color': [220, 220, 220] if group == '_RIGID' else [180, 190, 220],
            'sides': [0] * len(triangles),
        }

    return {
        'mats_hash': mats_hash,
        'sources': [
            {
                'xyz': acoustic_position_array(fixture.sources[0].position).tolist(),
                'name': fixture.sources[0].source_id,
            }
        ],
        'receivers': [
            {
                'xyz': acoustic_position_array(fixture.receivers[0].position).tolist(),
                'name': fixture.receivers[0].receiver_id,
            }
        ],
        'export_datetime': 'R100B deterministic impedance fixture compiler',
    }
