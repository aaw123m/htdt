"""REV47-ISS2: declared per-solver-path capability manifest.

Pins the deterministic, serializable, auditable capability record: every
phenomenon declared exactly once, bands bounded by the adapter authority,
and UNSUPPORTED physics (diffraction at minimum) recorded with reasons —
never silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_acoustic_solver_adapter import (
    build_acoustic_solver_adapter_descriptor,
)
from htdt.cad_acoustic_solver_dispatch_repository import (
    CadAcousticSolverDispatchRepository,
)
from htdt.cad_equipment import FrequencyDomain
from htdt.cad_repository import SceneRepository
from htdt.cad_solver_capability_manifest import (
    SOLVER_PATH_PHENOMENA,
    SolverCapabilityManifest,
    SolverCapabilityRow,
    build_solver_capability_manifest,
)
from htdt.r120_geometry_compiler import ExactExternalAuthorityRef


def _ref(label: str) -> ExactExternalAuthorityRef:
    return ExactExternalAuthorityRef(
        authority_id=f'test-authority:{label}',
        authority_version='1',
        semantic_hash_sha256='ab' * 32,
    )


BAND = FrequencyDomain(minimum_hz=20.0, maximum_hz=20000.0)
SUB_BAND = FrequencyDomain(minimum_hz=40.0, maximum_hz=8000.0)
OUTER_BAND = FrequencyDomain(minimum_hz=10.0, maximum_hz=30000.0)


def _descriptor(**overrides):
    kwargs = {
        'adapter_id': 'htdt.deterministic-ga',
        'adapter_version': '1',
        'model_solver_role_id': 'geometric-solver',
        'acoustic_domain': 'geometric',
        'solver_implementation_ref': _ref('ga-implementation'),
        'solver_configuration_schema_ref': _ref('ga-config-schema'),
        'supported_snapshot_schema_versions': (1,),
        'supported_observables': ('deterministic_paths',),
        'valid_frequency_domain': BAND,
    }
    kwargs.update(overrides)
    return build_acoustic_solver_adapter_descriptor(**kwargs)


def _rows(**states) -> tuple[SolverCapabilityRow, ...]:
    """One row per phenomenon; each state keyword overrides the default
    UNSUPPORTED-with-reason row so callers can be explicit."""
    rows = []
    for phenomenon in SOLVER_PATH_PHENOMENA:
        state = states.get(phenomenon, 'UNSUPPORTED')
        if state == 'SUPPORTED':
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='SUPPORTED',
                    valid_frequency_domain=SUB_BAND,
                )
            )
        elif state == 'BOUNDED':
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='BOUNDED',
                    valid_frequency_domain=SUB_BAND,
                    bound_description=f'{phenomenon} bounded lane',
                    reasons=('bounded by declared limits',),
                )
            )
        else:
            rows.append(
                SolverCapabilityRow(
                    phenomenon=phenomenon,
                    state='UNSUPPORTED',
                    reasons=(f'{phenomenon} not modeled by this path',),
                )
            )
    return tuple(rows)


def _dispatch_repository(tmp_path: Path) -> CadAcousticSolverDispatchRepository:
    return CadAcousticSolverDispatchRepository(
        SceneRepository(tmp_path / 'cad.sqlite3'),
        external_authority_resolver=lambda ref: ref,
        fidelity_policy_resolver=lambda ref: None,
    )


def test_manifest_declares_every_phenomenon_fail_closed() -> None:
    descriptor = _descriptor()
    manifest = build_solver_capability_manifest(
        descriptor=descriptor,
        rows=_rows(
            direct_sound='SUPPORTED',
            specular_reflection='BOUNDED',
            edge_diffraction='UNSUPPORTED',
        ),
    )

    assert {row.phenomenon for row in manifest.rows} == set(
        SOLVER_PATH_PHENOMENA
    )
    assert manifest.row_for('edge_diffraction').state == 'UNSUPPORTED'
    assert manifest.row_for('edge_diffraction').reasons
    assert manifest.row_for('direct_sound').state == 'SUPPORTED'
    assert manifest.row_for('specular_reflection').state == 'BOUNDED'
    assert manifest.row_for('specular_reflection').bound_description
    assert manifest.manifest_id.startswith('solver-capability-manifest:')
    assert len(manifest.semantic_sha256) == 64
    # Rows are canonicalized to phenomenon order for a deterministic record.
    assert [row.phenomenon for row in manifest.rows] == sorted(
        SOLVER_PATH_PHENOMENA
    )


def test_manifest_rejects_undeclared_phenomenon() -> None:
    descriptor = _descriptor()
    rows = [
        row
        for row in _rows()
        if row.phenomenon != 'edge_diffraction'
    ]
    with pytest.raises(ValueError, match='every phenomenon exactly once'):
        build_solver_capability_manifest(descriptor=descriptor, rows=rows)


def test_manifest_rejects_band_outside_adapter_domain() -> None:
    descriptor = _descriptor()
    rows = _rows(direct_sound='SUPPORTED')
    rows = tuple(
        SolverCapabilityRow(
            phenomenon=row.phenomenon,
            state=row.state,
            valid_frequency_domain=(
                OUTER_BAND if row.phenomenon == 'direct_sound' else row.valid_frequency_domain
            ),
            bound_description=row.bound_description,
            reasons=row.reasons,
        )
        for row in rows
    )
    with pytest.raises(ValueError, match='outside the adapter'):
        build_solver_capability_manifest(descriptor=descriptor, rows=rows)


def test_row_states_enforce_their_contracts() -> None:
    with pytest.raises(ValueError, match='at least one reason'):
        SolverCapabilityRow(
            phenomenon='edge_diffraction',
            state='UNSUPPORTED',
        )
    with pytest.raises(ValueError, match='cannot declare a valid band'):
        SolverCapabilityRow(
            phenomenon='edge_diffraction',
            state='UNSUPPORTED',
            valid_frequency_domain=SUB_BAND,
            reasons=('not modeled',),
        )
    with pytest.raises(ValueError, match='requires a valid'):
        SolverCapabilityRow(
            phenomenon='direct_sound',
            state='SUPPORTED',
        )
    with pytest.raises(ValueError, match='bound description'):
        SolverCapabilityRow(
            phenomenon='edge_diffraction',
            state='BOUNDED',
            valid_frequency_domain=SUB_BAND,
        )


def test_manifest_persists_bound_to_persisted_descriptor(
    tmp_path: Path,
) -> None:
    repository = _dispatch_repository(tmp_path)
    descriptor = repository.save_descriptor(_descriptor())
    manifest = build_solver_capability_manifest(
        descriptor=descriptor,
        rows=_rows(direct_sound='SUPPORTED', specular_reflection='BOUNDED'),
    )

    saved = repository.save_capability_manifest(manifest)
    assert saved == manifest

    reopened = _dispatch_repository(tmp_path)
    loaded = reopened.get_capability_manifest(manifest.manifest_id)
    assert loaded == manifest
    listed = reopened.list_capability_manifests(
        adapter_descriptor_id=descriptor.descriptor_id
    )
    assert listed == (manifest,)


def test_manifest_save_requires_persisted_descriptor(tmp_path: Path) -> None:
    repository = _dispatch_repository(tmp_path)
    manifest = build_solver_capability_manifest(
        descriptor=_descriptor(),
        rows=_rows(),
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_capability_manifest(manifest)


def test_manifest_save_rejects_descriptor_drift(tmp_path: Path) -> None:
    repository = _dispatch_repository(tmp_path)
    repository.save_descriptor(_descriptor())
    drifted = build_solver_capability_manifest(
        descriptor=_descriptor(adapter_version='2'),
        rows=_rows(),
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_capability_manifest(drifted)


def test_manifest_id_is_content_bound() -> None:
    manifest = build_solver_capability_manifest(
        descriptor=_descriptor(),
        rows=_rows(direct_sound='SUPPORTED'),
    )
    payload = manifest.model_dump(mode='python')
    payload['adapter_version'] = '9'
    with pytest.raises(ValueError, match='hash mismatch'):
        SolverCapabilityManifest.model_validate(payload)
