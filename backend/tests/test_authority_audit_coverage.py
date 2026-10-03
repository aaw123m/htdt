"""#845: audit coverage registry — explicit modes, completeness invariant,
canonical replay adapters for design-lifecycle / installed-equipment /
measurement-disposition families, and per-family forged fixtures.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

import pytest

from htdt.cad_design_checkpoint import (
    CheckpointRestoreRecord,
    build_design_checkpoint,
)
from htdt.cad_design_comparison import build_alternative, build_comparison_set
from htdt.cad_equipment import EquipmentDataProvenance
from htdt.cad_equipment_instance import (
    build_installed_equipment_instance,
    build_installed_equipment_replacement,
)
from htdt.cad_equipment_instance_repository import (
    CadInstalledEquipmentRepository,
)
from htdt.cad_measurement_disposition import (
    build_measurement_correction,
    build_measurement_disposition,
)
from htdt.cad_operating_preset import (
    bind_preset_measurements,
    build_operating_preset,
    record_applied_preset_state,
)
from htdt.cad_operating_preset_repository import CadOperatingPresetRepository
from htdt.cad_system_health import (
    HealthCheckItem,
    build_health_baseline,
    build_health_check_plan,
    run_health_check,
)
from htdt.cad_system_health_repository import CadSystemHealthRepository
from htdt.cad_design_checkpoint_repository import (
    CadDesignCheckpointRepository,
)
from htdt.cad_design_comparison_repository import (
    CadDesignComparisonRepository,
)
from htdt.cad_measurement_runner_repository import (
    CadMeasurementRunnerRepository,
)
from htdt.cad_model_calibration_repository import (
    CadModelCalibrationRepository,
)
from htdt.cad_repository import (
    AuthoringConstraintRevision,
    SceneRepository,
)
from htdt.cad_synthetic_demo import (
    SYNTHETIC_DEMO_DOCUMENT_ID,
    seed_synthetic_optimization_demo,
)
from htdt.native_authority_audit import (
    AuthorityAuditError,
    _STRUCTURAL_ONLY_TABLES,
    _NON_AUTHORITY_TABLES,
    assert_native_authority_graph,
    audit_native_authority_graph,
    audit_table_modes,
)

NOW = '2026-09-24T00:00:00+00:00'
_DOC = SYNTHETIC_DEMO_DOCUMENT_ID


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    return data_dir


def _seeded(tmp_path: Path) -> tuple[Path, SceneRepository]:
    data_dir = _data_dir(tmp_path)
    scene = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    seed_synthetic_optimization_demo(scene)
    return data_dir, scene


def _empty_db(tmp_path: Path) -> tuple[Path, SceneRepository]:
    data_dir = _data_dir(tmp_path)
    scene = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    return data_dir, scene


def _connect(data_dir: Path):
    return closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3'))


def _insert(data_dir: Path, table: str, **columns) -> None:
    names = ','.join(columns)
    marks = ','.join('?' for _ in columns)
    with _connect(data_dir) as connection, connection:
        connection.execute(
            f'INSERT INTO {table}({names}) VALUES ({marks})',
            tuple(columns.values()),
        )


def _failures(report, authority: str) -> list[str]:
    return [
        diagnostic.message
        for diagnostic in report.diagnostics
        if diagnostic.authority == authority
    ]


def _provenance() -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='manual',
        source_version='1',
        source_reference='manual-entry',
        source_sha256='0' * 64,
    )


def test_registry_covers_every_persisted_table() -> None:
    """Completeness invariant: every CREATE TABLE the codebase can make is
    registered to an explicit audit coverage mode."""
    declared: set[str] = set()
    src = Path(__file__).resolve().parent.parent / 'src' / 'htdt'
    for path in src.glob('*.py'):
        declared.update(
            re.findall(
                r'CREATE TABLE IF NOT EXISTS\s+([a-z_0-9]+)',
                path.read_text(encoding='utf-8', errors='replace'),
            )
        )
    modes = audit_table_modes()
    missing = declared - set(modes)
    assert not missing, f'tables lacking audit registration: {sorted(missing)}'
    for table, rationale in _STRUCTURAL_ONLY_TABLES.items():
        assert rationale, f'{table} registered without rationale'
    for table, rationale in _NON_AUTHORITY_TABLES.items():
        assert rationale, f'{table} registered without rationale'


def test_synthetic_unregistered_table_fails_audit(tmp_path: Path) -> None:
    """A persistent table without a coverage registration fails closed."""
    data_dir, _scene = _seeded(tmp_path)
    with _connect(data_dir) as connection, connection:
        connection.execute(
            'CREATE TABLE synthetic_unregistered_authority ('
            'payload_json TEXT NOT NULL)'
        )
        connection.execute(
            'INSERT INTO synthetic_unregistered_authority(payload_json) '
            "VALUES ('{}')"
        )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    gaps = [
        d
        for d in report.diagnostics
        if d.failure_class == 'coverage_gap'
        and d.authority == 'synthetic_unregistered_authority'
    ]
    assert gaps, 'synthetic table must produce a coverage_gap diagnostic'
    with pytest.raises(AuthorityAuditError):
        assert_native_authority_graph(data_dir / 'cad-scenes.sqlite3')


def test_report_coverage_counts_by_mode(tmp_path: Path) -> None:
    data_dir, _scene = _seeded(tmp_path)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert report.ok, report.summary()
    coverage = report.coverage_summary()
    assert set(coverage) == {
        'replay_canonical',
        'evidence_bytes',
        'structural_only',
        'non_authority',
    }
    assert coverage['replay_canonical'] > 0
    labels = {label for label, _mode, _count in report.coverage}
    # sqlite_sequence may or may not exist depending on AUTOINCREMENT use;
    # when present it must be registered as non_authority.
    assert all(
        mode == 'non_authority'
        for label, mode, _count in report.coverage
        if label == 'non_authority:sqlite_sequence'
        or label.endswith(':sqlite_sequence')
    )
    assert 'non_authority:scene_recovery_snapshots' in labels or any(
        label.startswith('non_authority:') for label in labels
    )


def test_forged_disposition_binding_missing_measurement_fails(
    tmp_path: Path,
) -> None:
    data_dir, _scene = _seeded(tmp_path)
    forged = build_measurement_disposition(
        document_id=_DOC,
        measurement_id='m-never-measured',
        disposition='active',
        reason='forged but self-consistent',
    )
    _insert(
        data_dir,
        'cad_measurement_dispositions',
        disposition_id=forged.disposition_id,
        document_id=forged.document_id,
        measurement_id=forged.measurement_id,
        disposition=forged.disposition,
        correction_id=None,
        disposition_sha256=forged.disposition_sha256,
        created_at_utc=forged.created_at_utc,
        payload_json=forged.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'measurement_disposition')


def test_forged_correction_binding_missing_measurement_fails(
    tmp_path: Path,
) -> None:
    data_dir, _scene = _seeded(tmp_path)
    forged = build_measurement_correction(
        document_id=_DOC,
        measurement_id='m-never-measured',
        dataset_id='d-never',
        dataset_sha256='0' * 64,
        channel_role='direct',
        reason='forged but self-consistent',
    )
    _insert(
        data_dir,
        'cad_measurement_corrections',
        correction_id=forged.correction_id,
        document_id=forged.document_id,
        measurement_id=forged.measurement_id,
        dataset_id=forged.dataset_id,
        dataset_sha256=forged.dataset_sha256,
        correction_sha256=forged.correction_sha256,
        created_at_utc=forged.created_at_utc,
        payload_json=forged.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'measurement_correction')


def test_forged_preset_binding_missing_preset_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    head = scene.current_head(_DOC)
    assert head is not None
    CadOperatingPresetRepository(scene)
    preset = build_operating_preset(
        document_id=_DOC,
        name='unsaved preset',
        scene_revision_id=head.revision_id,
        scene_content_hash=head.content_hash,
        created_at_utc=NOW,
    )
    binding = bind_preset_measurements(
        preset, measurement_ids=('m-never',), bound_at_utc=NOW
    )
    _insert(
        data_dir,
        'cad_preset_measurement_bindings',
        binding_id=binding.binding_id,
        document_id=binding.document_id,
        preset_id=binding.preset_id,
        preset_sha256=binding.preset_sha256,
        bound_at_utc=binding.bound_at_utc,
        payload_json=binding.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'preset_measurement_binding')


def test_forged_applied_preset_mismatch_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    head = scene.current_head(_DOC)
    assert head is not None
    preset = build_operating_preset(
        document_id=_DOC,
        name='saved preset',
        scene_revision_id=head.revision_id,
        scene_content_hash=head.content_hash,
        created_at_utc=NOW,
    )
    CadOperatingPresetRepository(scene).save_preset(preset)
    applied = record_applied_preset_state(preset, confirmed_at_utc=NOW)
    _insert(
        data_dir,
        'cad_applied_preset_states',
        applied_id=applied.applied_id,
        document_id=applied.document_id,
        preset_id='different-preset',
        preset_sha256=applied.preset_sha256,
        confirmed_at_utc=applied.confirmed_at_utc,
        payload_json=applied.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'applied_preset_state')


def test_forged_checkpoint_restore_missing_checkpoint_fails(
    tmp_path: Path,
) -> None:
    data_dir, scene = _seeded(tmp_path)
    head = scene.current_head(_DOC)
    assert head is not None
    CadDesignCheckpointRepository(scene)
    checkpoint = build_design_checkpoint(
        document_id=_DOC,
        title='never saved',
        scene_revision=head,
        created_at_utc=NOW,
    )
    from htdt.cad_design_checkpoint import _hash

    provisional = CheckpointRestoreRecord.model_construct(
        restore_id='restore-forged',
        document_id=_DOC,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        applied_components=(),
        new_scene_revision_id=None,
        new_constraint_sha256=None,
        created_at_utc=NOW,
        restore_sha256='0' * 64,
    )
    record = CheckpointRestoreRecord(
        **{
            **provisional.model_dump(mode='python'),
            'restore_sha256': _hash(provisional.semantic_payload()),
        }
    )
    _insert(
        data_dir,
        'cad_checkpoint_restores',
        restore_id=record.restore_id,
        document_id=record.document_id,
        checkpoint_id=record.checkpoint_id,
        restore_sha256=record.restore_sha256,
        created_at_utc=record.created_at_utc,
        payload_json=record.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'checkpoint_restore')


def test_forged_health_run_missing_plan_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    head = scene.current_head(_DOC)
    assert head is not None
    health = CadSystemHealthRepository(scene)
    baseline = build_health_baseline(
        document_id=_DOC,
        name='baseline',
        scene_revision_id=head.revision_id,
        scene_content_hash=head.content_hash,
        created_at_utc=NOW,
    )
    health.save_baseline(baseline)
    plan = build_health_check_plan(
        baseline,
        checks=(
            HealthCheckItem(
                check_id='check-1',
                domain='settings',
                description='settings check',
            ),
        ),
        created_at_utc=NOW,
    )
    health.save_plan(plan)
    run = run_health_check(plan, baseline, created_at_utc=NOW)
    _insert(
        data_dir,
        'cad_health_check_runs',
        run_id=run.run_id,
        document_id=run.document_id,
        plan_id='missing-plan',
        run_sha256=run.run_sha256,
        created_at_utc=run.created_at_utc,
        payload_json=run.model_dump_json(),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'health_check_run')


def test_branched_installed_equipment_lineage_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    installed = CadInstalledEquipmentRepository(scene)

    def _unit(name: str):
        return build_installed_equipment_instance(
            instance_id=name,
            document_id=_DOC,
            equipment_class='avr',
            provenance=(_provenance(),),
            created_at_utc=NOW,
            manufacturer='m',
            model=name,
        )

    for name in ('unit-a', 'unit-b', 'unit-c'):
        installed.save_instance(_unit(name))
    first = build_installed_equipment_replacement(
        replacement_id='rep-1',
        document_id=_DOC,
        removed_instance_id='unit-a',
        installed_instance_id='unit-b',
        replaced_at_utc=NOW,
        provenance=(_provenance(),),
    )
    installed.save_replacement(first)
    forged = build_installed_equipment_replacement(
        replacement_id='rep-2',
        document_id=_DOC,
        removed_instance_id='unit-a',
        installed_instance_id='unit-c',
        replaced_at_utc=NOW,
        provenance=(_provenance(),),
    )
    # The unique predecessor index (#842) rejects the forged second branch at
    # the schema level — the audit's read-side check stays as a backstop.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(
            data_dir,
            'cad_installed_equipment_replacements',
            replacement_id=forged.replacement_id,
            document_id=forged.document_id,
            removed_instance_id=forged.removed_instance_id,
            installed_instance_id=forged.installed_instance_id,
            payload_json=forged.model_dump_json(),
            recorded_at_utc=NOW,
        )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert report.ok


def test_branched_design_comparison_lineage_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    head = scene.current_head(_DOC)
    assert head is not None
    comparisons = CadDesignComparisonRepository(scene)
    alternative = build_alternative(
        label='A', scene_revision=head, created_at_utc=NOW
    )
    first = build_comparison_set(
        document_id=_DOC,
        name='set-1',
        alternatives=(alternative,),
        created_at_utc=NOW,
    )
    comparisons.save_set(first)
    forged = build_comparison_set(
        document_id=_DOC,
        name='set-2-branch',
        alternatives=(alternative,),
        supersedes=first,
        created_at_utc=NOW,
    )
    _insert(
        data_dir,
        'cad_design_comparison_sets',
        set_id=forged.set_id,
        document_id=forged.document_id,
        revision=forged.revision,
        supersedes_set_id=forged.supersedes_set_id,
        set_sha256=forged.set_sha256,
        created_at_utc=forged.created_at_utc,
        payload_json=forged.model_dump_json(),
    )
    second = build_comparison_set(
        document_id=_DOC,
        name='set-2-legit',
        alternatives=(alternative,),
        supersedes=first,
        created_at_utc=NOW,
    )
    comparisons.save_set(second)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'design_comparison_set')


def test_branched_authoring_constraint_lineage_fails(tmp_path: Path) -> None:
    data_dir, scene = _seeded(tmp_path)
    scene.save_authoring_constraints(_DOC, {'constraints': [{'id': 'c1'}]})
    scene.save_authoring_constraints(_DOC, {'constraints': [{'id': 'c2'}]})
    lineage = scene.list_authoring_constraint_revisions(_DOC)
    assert len(lineage) == 2
    # Forge a sibling of the existing successor: target a revision that
    # already has a successor so the row branches the lineage regardless
    # of listing order — created_at_utc ties fall back to UUID ordering
    # on coarse-clock platforms (Windows timer granularity), which made
    # ``lineage[-1]`` nondeterministically resolve to the head revision.
    parent = next(
        record
        for record in lineage
        if any(
            successor.supersedes_id == record.constraint_revision_id
            for successor in lineage
        )
    )
    forged = AuthoringConstraintRevision.build(
        document_id=_DOC,
        payload={'constraints': [{'id': 'forged-branch'}]},
        supersedes_id=parent.constraint_revision_id,
        scene_revision_id=parent.scene_revision_id,
    )
    _insert(
        data_dir,
        'authoring_constraint_revisions',
        constraint_revision_id=forged.constraint_revision_id,
        document_id=forged.document_id,
        supersedes_id=forged.supersedes_id,
        scene_revision_id=forged.scene_revision_id,
        payload_json=json.dumps(forged.payload),
        constraint_revision_sha256=forged.constraint_revision_sha256,
        created_at_utc=forged.created_at_utc,
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'authoring_constraint_revision')


def test_forged_scene_head_row_fails(tmp_path: Path) -> None:
    data_dir, _scene = _seeded(tmp_path)
    _insert(
        data_dir,
        'scene_document_heads',
        document_id='ghost-document',
        head_revision_id='ghost-revision',
        updated_at_utc=NOW,
        generation=1,
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'scene_document_head')


def test_forged_calibration_evidence_event_missing_freeze_fails(
    tmp_path: Path,
) -> None:
    data_dir, scene = _seeded(tmp_path)
    CadModelCalibrationRepository(scene)
    _insert(
        data_dir,
        'cad_calibration_evidence_events',
        campaign_id='campaign:forged',
        campaign_sha256='0' * 64,
        consumption_kind='holdout',
        freeze_id='calibrated-model-freeze:' + '0' * 64,
        record_id=None,
        recorded_at_utc=NOW,
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'calibration_evidence_event')


def test_forged_runner_event_missing_run_fails(tmp_path: Path) -> None:
    data_dir, scene = _empty_db(tmp_path)
    CadMeasurementRunnerRepository(scene)
    forged = {
        'event_id': 'evt-1',
        'run_id': 'run-missing',
        'cell_index': 0,
        'status': 'staged',
        'measurement_id': None,
        'dataset_id': None,
        'dataset_sha256': None,
        'supersedes_measurement_id': None,
        'reason': '',
        'created_at': NOW,
    }
    _insert(
        data_dir,
        'cad_measurement_runner_events',
        event_id=forged['event_id'],
        run_id=forged['run_id'],
        cell_index=forged['cell_index'],
        status=forged['status'],
        created_at_utc=forged['created_at'],
        payload_json=json.dumps(forged),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')

    assert not report.ok
    assert _failures(report, 'measurement_runner_event')
