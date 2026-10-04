"""REV47-ISS2: executable acceptance for the new r100a-5 fixture classes.

The r100a-5 manifest adds two fixture classes: occlusion/edge honesty
(occluded geometry must produce typed rejections, never fabricated
diffraction traversal) and scattering redistribution (scattering must
change the energy result — a candidate that ignores scattering fails).

The underlying behaviours already have dedicated suites; these tests pin
the fixture-contract invariants themselves and the honesty property that
diffracted arrivals cannot be fabricated by any path the engine emits.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from htdt.acoustic_benchmark import (
    AcousticBenchmarkManifest,
    AcousticMaterial,
    GeometricAcousticBand,
)
from htdt.cad_geometric_acoustics_adapter import (
    GeometricMaterialAuthority,
)
from htdt.cad_scene import Position3
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef

from test_cad_geometric_acoustics_adapter import (  # noqa: E402  (shared fixtures)
    _execute,
    _fixture,
)
from test_cad_late_field_energy import (  # noqa: E402  (shared helpers)
    _contributions,
    _execute_late,
    _rejections,
)


MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / 'benchmarks'
    / 'acoustics'
    / 'r100a_manifest.json'
)


def _fixture_by_id(manifest: AcousticBenchmarkManifest, fixture_id: str):
    match = [
        item for item in manifest.fixtures if item.fixture_id == fixture_id
    ]
    assert len(match) == 1
    return match[0]


def _material(*, scattering_500: float, scattering_1000: float):
    material = AcousticMaterial(
        material_id='fixture-wall',
        provenance='fixture exact GA material',
        version='1',
        wave_model='unsupported',
        geometric_model='banded',
        geometric_bands=(
            GeometricAcousticBand(
                center_hz=500.0,
                absorption=0.2,
                scattering=scattering_500,
            ),
            GeometricAcousticBand(
                center_hz=1000.0,
                absorption=0.3,
                scattering=scattering_1000,
            ),
        ),
    )
    payload = material.model_dump(mode='json')
    ref = ExactExternalAuthorityRef(
        authority_id='fixture-material:wall',
        authority_version=material.version,
        semantic_hash_sha256=sha256(
            json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
    )
    return GeometricMaterialAuthority(authority_ref=ref, material=material)


def test_r100a5_manifest_declares_new_fixture_classes() -> None:
    manifest = AcousticBenchmarkManifest.model_validate(
        json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    )
    occlusion = _fixture_by_id(manifest, 'geometric-occlusion-edge-v1')
    scattering = _fixture_by_id(
        manifest, 'geometric-scattering-redistribution-v1'
    )

    assert occlusion.benchmark_role == 'geometric'
    assert occlusion.required_capabilities == ('geometric_specular',)
    assert {item.kind for item in occlusion.observables} == {
        'direct_path_length_m',
        'energy_decay_db',
    }
    assert scattering.required_capabilities == (
        'geometric_specular',
        'geometric_scattering',
        'stochastic_rays',
    )
    assert scattering.random_seed == 20261004
    assert {item.kind for item in scattering.observables} == {'energy_decay_db'}


def test_occluded_candidates_fail_closed_never_fabricated(
    tmp_path: Path,
) -> None:
    """Occlusion/edge acceptance: a tetra occluder must yield typed
    rejections for every blocked candidate, and no emitted path may claim
    a traversal type outside the declared {direct, specular_reflection}
    set — diffraction arrivals cannot be fabricated by silence or by a
    fake path type."""
    fx = _fixture(tmp_path, occluder=True)
    artifact = _execute(fx)

    # Occluded direct path: retained as a typed rejection, not a path.
    assert not any(item.path_type == 'direct' for item in artifact.paths)
    assert any(
        item.path_type == 'direct' and item.decision == 'BLOCKED_VISIBILITY'
        for item in artifact.rejected_candidates
    )
    # Every rejected candidate is an inspectable record: typed decision
    # plus a reason — the honesty contract is the rejection, not silence.
    assert artifact.rejected_candidates
    assert all(
        item.decision and item.reason
        for item in artifact.rejected_candidates
    )
    # No emitted path may claim a traversal type outside the declared
    # {direct, specular_reflection} set — diffracted arrivals cannot be
    # fabricated through a fake path type.
    assert {item.path_type for item in artifact.paths} <= {
        'direct',
        'specular_reflection',
    }


def test_late_field_occlusion_rejections_are_typed(
    tmp_path: Path,
) -> None:
    # Receiver raised above the tetra occluder: the floor patch
    # centroid's emergent segment crosses the occluder body.
    fx = _fixture(
        tmp_path,
        occluder=True,
        receiver_position=Position3(x_m=2.0, y_m=1.5, z_m=1.8),
    )
    artifact = _execute_late(fx)
    blocked = _rejections(artifact, 'BLOCKED_VISIBILITY')
    assert blocked
    blocked_surfaces = {
        item.interaction_key for item in blocked
    }
    contribution_surfaces = {
        item.interaction_surface_id for item in artifact.contributions
    }
    assert blocked_surfaces.isdisjoint(contribution_surfaces)


def test_scattering_redistribution_changes_the_energy_result(
    tmp_path: Path,
) -> None:
    """Scattering acceptance: the same scene with scattering=0 must
    produce strictly less redistributed late energy than scattering>0 —
    a candidate that ignores scattering fails this fixture."""
    fx_zero = _fixture(
        tmp_path / 'zero',
        material=_material(scattering_500=0.0, scattering_1000=0.0),
    )
    fx_high = _fixture(
        tmp_path / 'high',
        material=_material(scattering_500=0.4, scattering_1000=0.5),
    )

    artifact_zero = _execute_late(fx_zero)
    artifact_high = _execute_late(fx_high)

    def scattered_energy(artifact) -> float:
        total = 0.0
        for contribution in _contributions(artifact, 'surface_scattering'):
            for band in contribution.bands:
                total += band.late_energy_upper_bound_per_m2
        return total

    zero_energy = scattered_energy(artifact_zero)
    high_energy = scattered_energy(artifact_high)

    assert zero_energy == 0.0
    assert high_energy > 0.0
    assert high_energy > zero_energy


def test_scattering_execution_is_repeatable_under_same_seed(
    tmp_path: Path,
) -> None:
    """The fixture pins seed-repeatability: identical configuration and
    geometry must produce identical redistributed energy."""
    material = _material(scattering_500=0.4, scattering_1000=0.5)
    first = _execute_late(_fixture(tmp_path / 'a', material=material))
    second = _execute_late(_fixture(tmp_path / 'b', material=material))

    first_bands = [
        band.late_energy_upper_bound_per_m2
        for contribution in _contributions(first, 'surface_scattering')
        for band in contribution.bands
    ]
    second_bands = [
        band.late_energy_upper_bound_per_m2
        for contribution in _contributions(second, 'surface_scattering')
        for band in contribution.bands
    ]
    assert len(first_bands) == len(second_bands)
    assert all(a == b for a, b in zip(first_bands, second_bands))
