"""#757 — semantic audit coverage for the #718 hardening families.

Every corrupt fixture below is *structurally* valid — the row parses, its
self-hash verifies — and fails only when the audit replays it through the
owning repository's persisted-record verification.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_design_checkpoint import (
    build_design_checkpoint,
    snapshot_constraint_workspace,
)
from htdt.cad_design_checkpoint_repository import CadDesignCheckpointRepository
from htdt.cad_design_comparison import (
    ComparisonEvidenceRef,
    build_alternative,
    build_comparison_set,
)
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_constraint_repository import CadConstraintRepository
from htdt.cad_operating_preset import (
    PresetComponentRef,
    bind_preset_measurements,
    build_operating_preset,
)
from htdt.cad_operating_preset_repository import CadOperatingPresetRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_synthetic_demo import seed_synthetic_optimization_demo
from htdt.cad_system_health import (
    HealthAuthorityRef,
    HealthCheckItem,
    build_health_baseline,
    build_health_check_plan,
)
from htdt.cad_system_health_repository import CadSystemHealthRepository
from htdt.native_authority_audit import (
    AuthorityAuditError,
    assert_native_authority_graph,
    audit_native_authority_graph,
)
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.native_backup import create_backup
from htdt.project_lifecycle import ProjectLibrary

NOW = '2026-09-22T00:00:00+00:00'


def _seeded(tmp_path: Path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    scene_repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    result = seed_synthetic_optimization_demo(scene_repository)
    # Instantiate the #718-family repositories so their tables exist even
    # when a test only inserts forged rows.
    CadOperatingPresetRepository(scene_repository)
    CadSystemHealthRepository(scene_repository)
    CadDesignCheckpointRepository(scene_repository)
    CadDesignComparisonRepository(scene_repository)
    ProjectLibrary(data_dir / 'cad-scenes.sqlite3')
    return data_dir, scene_repository, result


def _raw_insert(data_dir: Path, table: str, columns: tuple[str, ...], row: tuple):
    database = data_dir / 'cad-scenes.sqlite3'
    placeholders = ', '.join('?' for _ in columns)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            f'INSERT INTO {table} ({", ".join(columns)}) '
            f'VALUES ({placeholders})',
            row,
        )


def _insert_preset_row(data_dir: Path, preset) -> None:
    _raw_insert(
        data_dir,
        'cad_operating_presets',
        (
            'preset_id',
            'document_id',
            'category',
            'preset_sha256',
            'created_at_utc',
            'payload_json',
        ),
        (
            preset.preset_id,
            preset.document_id,
            preset.category,
            preset.preset_sha256,
            preset.created_at_utc,
            preset.model_dump_json(),
        ),
    )


def _insert_baseline_row(data_dir: Path, baseline) -> None:
    _raw_insert(
        data_dir,
        'cad_health_baselines',
        (
            'baseline_id',
            'document_id',
            'baseline_sha256',
            'created_at_utc',
            'payload_json',
        ),
        (
            baseline.baseline_id,
            baseline.document_id,
            baseline.baseline_sha256,
            baseline.created_at_utc,
            baseline.model_dump_json(),
        ),
    )


def _insert_plan_row(data_dir: Path, plan) -> None:
    _raw_insert(
        data_dir,
        'cad_health_check_plans',
        (
            'plan_id',
            'document_id',
            'baseline_id',
            'baseline_sha256',
            'plan_sha256',
            'created_at_utc',
            'payload_json',
        ),
        (
            plan.plan_id,
            plan.document_id,
            plan.baseline_id,
            plan.baseline_sha256,
            plan.plan_sha256,
            plan.created_at_utc,
            plan.model_dump_json(),
        ),
    )


def _insert_checkpoint_row(data_dir: Path, checkpoint) -> None:
    _raw_insert(
        data_dir,
        'cad_design_checkpoints',
        (
            'checkpoint_id',
            'document_id',
            'checkpoint_sha256',
            'created_at_utc',
            'payload_json',
        ),
        (
            checkpoint.checkpoint_id,
            checkpoint.document_id,
            checkpoint.checkpoint_sha256,
            checkpoint.created_at_utc,
            checkpoint.model_dump_json(),
        ),
    )


def _insert_comparison_row(data_dir: Path, comparison_set) -> None:
    _raw_insert(
        data_dir,
        'cad_design_comparison_sets',
        (
            'set_id',
            'document_id',
            'revision',
            'supersedes_set_id',
            'set_sha256',
            'created_at_utc',
            'payload_json',
        ),
        (
            comparison_set.set_id,
            comparison_set.document_id,
            comparison_set.revision,
            comparison_set.supersedes_set_id,
            comparison_set.set_sha256,
            comparison_set.created_at_utc,
            comparison_set.model_dump_json(),
        ),
    )


def test_audit_accepts_clean_hardening_rows(tmp_path: Path):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)

    presets = CadOperatingPresetRepository(scene_repository)
    preset = build_operating_preset(
        document_id=revision.document_id,
        name='Movie Night',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        created_at_utc=NOW,
    )
    presets.save_preset(preset)

    health = CadSystemHealthRepository(scene_repository)
    baseline = build_health_baseline(
        document_id=revision.document_id,
        name='Baseline',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        created_at_utc=NOW,
    )
    health.save_baseline(baseline)
    health.save_plan(
        build_health_check_plan(
            baseline,
            checks=(
                HealthCheckItem(
                    check_id='chk-1',
                    domain='acoustic',
                    description='level drift',
                ),
            ),
            created_at_utc=NOW,
        )
    )

    checkpoints = CadDesignCheckpointRepository(scene_repository)
    constraint_repo = CadConstraintRepository(data_dir / 'cad-scenes.sqlite3')
    constraint_repo.save(CadConstraintSet(document_id=revision.document_id))
    snapshot = snapshot_constraint_workspace(
        constraint_repo.load(revision.document_id),
        created_at_utc=NOW,
        source_updated_at_utc=NOW,
    )
    checkpoints.save_snapshot(snapshot)
    checkpoints.save_checkpoint(
        build_design_checkpoint(
            document_id=revision.document_id,
            title='Design freeze',
            scene_revision=revision,
            constraint_snapshot=snapshot,
            created_at_utc=NOW,
        )
    )

    comparisons = CadDesignComparisonRepository(scene_repository)
    comparisons.save_set(
        build_comparison_set(
            document_id=revision.document_id,
            name='A vs A',
            alternatives=(
                build_alternative(
                    label='A',
                    scene_revision=revision,
                    created_at_utc=NOW,
                ),
            ),
            created_at_utc=NOW,
        )
    )

    ProjectLibrary(data_dir / 'cad-scenes.sqlite3').register_project(
        revision.document_id, display_name='Seeded demo'
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert report.ok, report.summary()
    checked = dict(report.checked)
    assert checked['operating_preset'] == 1
    assert checked['health_baseline'] == 1
    assert checked['health_check_plan'] == 1
    assert checked['design_checkpoint'] == 1
    assert checked['design_comparison_set'] == 1
    assert checked['project_registry'] >= 1


def test_audit_rejects_preset_with_nonexistent_component_ref(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    preset = build_operating_preset(
        document_id=revision.document_id,
        name='Forged preset',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        component_refs=(
            PresetComponentRef(
                kind='system_variant',
                ref_id='ghost-variant',
                ref_sha256='a' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    # Forged via direct insert — the save path never saw the ghost ref.
    _insert_preset_row(data_dir, preset)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'operating_preset'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_preset_binding_to_missing_measurement(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    presets = CadOperatingPresetRepository(scene_repository)
    preset = build_operating_preset(
        document_id=revision.document_id,
        name='Preset',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        created_at_utc=NOW,
    )
    presets.save_preset(preset)
    binding = bind_preset_measurements(
        preset, measurement_ids=('ghost-measurement',), bound_at_utc=NOW
    )
    _raw_insert(
        data_dir,
        'cad_preset_measurement_bindings',
        (
            'binding_id',
            'document_id',
            'preset_id',
            'preset_sha256',
            'bound_at_utc',
            'payload_json',
        ),
        (
            binding.binding_id,
            binding.document_id,
            binding.preset_id,
            binding.preset_sha256,
            binding.bound_at_utc,
            binding.model_dump_json(),
        ),
    )

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'preset_measurement_binding'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_health_baseline_with_forged_preset_ref(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    baseline = build_health_baseline(
        document_id=revision.document_id,
        name='Forged baseline',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        operating_preset_id='ghost-preset',
        operating_preset_sha256='b' * 64,
        created_at_utc=NOW,
    )
    _insert_baseline_row(data_dir, baseline)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'health_baseline'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_health_baseline_with_forged_source_ref(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    baseline = build_health_baseline(
        document_id=revision.document_id,
        name='Forged assessment source',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        source_refs=(
            HealthAuthorityRef(
                kind='validation',
                ref_id='ghost-validation',
                ref_sha256='c' * 64,
            ),
        ),
        created_at_utc=NOW,
    )
    _insert_baseline_row(data_dir, baseline)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'health_baseline'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_cross_document_health_plan(tmp_path: Path):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    baseline = build_health_baseline(
        document_id=revision.document_id,
        name='Baseline',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        created_at_utc=NOW,
    )
    _insert_baseline_row(data_dir, baseline)
    plan = build_health_check_plan(
        baseline,
        checks=(
            HealthCheckItem(
                check_id='chk-1',
                domain='acoustic',
                description='drift',
            ),
        ),
        created_at_utc=NOW,
    )
    # Forge: keep the correct baseline hash but relabel the plan into
    # another document's scope.
    forged = plan.model_copy(update={'document_id': 'doc-elsewhere'})
    _insert_plan_row(data_dir, forged)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'health_check_plan'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_checkpoint_scene_document_mismatch(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    checkpoint = build_design_checkpoint(
        document_id=revision.document_id,
        title='Forged checkpoint',
        scene_revision=revision,
        constraint_snapshot=None,
        created_at_utc=NOW,
    )
    forged_payload = json.loads(checkpoint.model_dump_json())
    forged_payload['document_id'] = 'doc-elsewhere'
    forged = type(checkpoint).model_construct(
        **{
            key: value
            for key, value in forged_payload.items()
            if key != 'checkpoint_sha256'
        },
        checkpoint_sha256=checkpoint.checkpoint_sha256,
    )
    _insert_checkpoint_row(data_dir, forged)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'design_checkpoint'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_checkpoint_scene_hash_mismatch(tmp_path: Path):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision = scene_repository.get(result.source_revision_id)
    checkpoint = build_design_checkpoint(
        document_id=revision.document_id,
        title='Forged checkpoint',
        scene_revision=revision,
        constraint_snapshot=None,
        created_at_utc=NOW,
    )
    forged_payload = json.loads(checkpoint.model_dump_json())
    forged_payload['scene_content_hash'] = '0' * 64
    forged = type(checkpoint).model_construct(
        **{
            key: value
            for key, value in forged_payload.items()
            if key != 'checkpoint_sha256'
        },
        checkpoint_sha256=checkpoint.checkpoint_sha256,
    )
    _insert_checkpoint_row(data_dir, forged)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'design_checkpoint'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_comparison_foreign_scene_and_evidence(
    tmp_path: Path,
):
    data_dir, scene_repository, result = _seeded(tmp_path)
    revision_a = scene_repository.get(result.source_revision_id)

    # A second document: the alternative pins a foreign scene revision and
    # evidence that lives in another project.
    scene_b = SceneDocument(
        document_id='doc-b',
        room=RoomPrism(width_m=7.0, depth_m=5.0, height_m=2.5),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )
    revision_b = scene_repository.save(
        scene_b, parent_revision_id=None
    ).revision
    checkpoints = CadDesignCheckpointRepository(scene_repository)
    foreign_checkpoint = build_design_checkpoint(
        document_id='doc-b',
        title='Foreign checkpoint',
        scene_revision=revision_b,
        constraint_snapshot=None,
        created_at_utc=NOW,
    )
    checkpoints.save_checkpoint(foreign_checkpoint)

    forged = build_comparison_set(
        document_id=revision_a.document_id,
        name='Forged set',
        alternatives=(
            build_alternative(
                label='Foreign scene',
                scene_revision=revision_b,
                evidence_refs=(
                    ComparisonEvidenceRef(
                        kind='design_checkpoint',
                        ref_id=foreign_checkpoint.checkpoint_id,
                        ref_sha256=foreign_checkpoint.checkpoint_sha256,
                    ),
                ),
                created_at_utc=NOW,
            ),
        ),
        created_at_utc=NOW,
    )
    _insert_comparison_row(data_dir, forged)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert not report.ok
    assert any(
        diagnostic.authority == 'design_comparison_set'
        for diagnostic in report.diagnostics
    )


def test_audit_reports_every_table_exactly_once(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded(tmp_path)
    database = data_dir / 'cad-scenes.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'CREATE TABLE rogue_payloads ('
            'row_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)'
        )
        connection.execute(
            "INSERT INTO rogue_payloads VALUES ('r-1', '{}')"
        )

    report = audit_native_authority_graph(database)
    assert not report.ok
    assert 'rogue_payloads' in report.unclassified_tables
    counts = report.coverage_counts
    assert counts['unclassified_tables'] == 1
    assert counts['replayed_records'] > 0

    with pytest.raises(AuthorityAuditError, match='unclassified'):
        assert_native_authority_graph(database)


def test_unclassified_table_blocks_backup(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded(tmp_path)
    database = data_dir / 'cad-scenes.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'CREATE TABLE rogue_payloads ('
            'row_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)'
        )
    archive = tmp_path / 'must-not-exist.htdt-backup'
    with pytest.raises(AuthorityAuditError):
        create_backup(data_dir, archive)
    assert not archive.exists()


def test_audit_rejects_registry_clone_cycle(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded(tmp_path)
    database = data_dir / 'cad-scenes.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        for project_id, cloned_from in (
            ('p-a', 'p-b'),
            ('p-b', 'p-a'),
        ):
            connection.execute(
                'INSERT INTO htdt_project_documents('
                'project_id, document_id, display_name, archived, '
                'cloned_from_project_id, created_at_utc, updated_at_utc, '
                'archived_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    project_id,
                    f'doc-{project_id}',
                    project_id,
                    0,
                    cloned_from,
                    NOW,
                    NOW,
                    None,
                ),
            )

    report = audit_native_authority_graph(database)
    assert not report.ok
    assert any(
        diagnostic.authority == 'project_registry'
        for diagnostic in report.diagnostics
    )


def test_audit_rejects_registry_unknown_clone_source(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded(tmp_path)
    database = data_dir / 'cad-scenes.sqlite3'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, archived, '
            'cloned_from_project_id, created_at_utc, updated_at_utc, '
            'archived_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                'p-orphan',
                'doc-orphan',
                'orphan',
                0,
                'p-ghost',
                NOW,
                NOW,
                None,
            ),
        )

    report = audit_native_authority_graph(database)
    assert not report.ok
    diagnostics = [
        diagnostic
        for diagnostic in report.diagnostics
        if diagnostic.authority == 'project_registry'
    ]
    assert diagnostics
    assert 'unknown source' in diagnostics[0].message


def test_audit_covers_gui_created_tables(tmp_path: Path):
    """A real app launch creates repository tables the test fixtures do
    not (the Overview mount builds the topology-search repository).
    Regression cover for the cutover regression found in review: the
    coverage registry must classify every one of them."""
    from htdt.cad_system_variant_repository import (
        CadSystemVariantRepository,
    )
    from htdt.cad_topology_search_repository import (
        CadTopologySearchRepository,
    )

    data_dir, scene_repository, _result = _seeded(tmp_path)
    CadTopologySearchRepository(
        CadSystemVariantRepository(scene_repository)
    )
    report = audit_native_authority_graph(
        data_dir / 'cad-scenes.sqlite3'
    )
    assert report.ok, report.summary()
    assert report.coverage_counts['unclassified_tables'] == 0


def test_audit_accepts_valid_clone_lineage(tmp_path: Path):
    data_dir, _scene_repository, _result = _seeded(tmp_path)
    library = ProjectLibrary(data_dir / 'cad-scenes.sqlite3')
    parent = library.register_project(
        'doc-parent', display_name='Parent'
    )
    child = library.register_project(
        'doc-child',
        display_name='Child',
        cloned_from_project_id=parent.project_id,
    )
    # Retire the descendant, then the parent — the child's clone lineage
    # still resolves through the surviving registry row.
    library.archive_project(child.project_id)
    library.delete_project(
        child.project_id,
        expected_plan=library.plan_project_deletion(child.project_id),
    )
    library.archive_project(parent.project_id)
    plan = library.plan_project_deletion(parent.project_id)
    library.delete_project(parent.project_id, expected_plan=plan)

    report = audit_native_authority_graph(data_dir / 'cad-scenes.sqlite3')
    assert report.ok, report.summary()
    checked = dict(report.checked)
    assert checked['project_registry'] == 0  # both rows deleted with the projects
    assert checked['project_tombstone'] >= 2
