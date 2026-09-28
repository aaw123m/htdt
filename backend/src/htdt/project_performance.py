"""Representative Project Performance Budget (#663).

One bounded authority for:

- **Representative project classes** (P-small / P-large / P-stress) and the
  deterministic fixture generator that builds them *only* through normal
  production save paths — never by inserting arbitrary rows for volume.
- **Interaction classes** and the declared per-operation budget table.
- **A data-path benchmark** measuring the user-visible operations that are
  meaningful headlessly (project open, history list, revision compare,
  save, measurement enumeration, inbox summary, backup create).
- **A single optional instrumentation journal** (`record_perf_event`) so
  high-level operations can report phase timing without ad-hoc prints.

GUI timing (first render, frame pacing, selection latency) is measured on
declared reference Windows hardware, not here — see docs/IMPLEMENTATION_STATUS
and benchmarks/projects/ for recorded baselines.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import logging
import os
import platform
import time
from pathlib import Path
from uuid import uuid4

from .cad_measurement_models import CadFrequencyResponseDataset
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from .cad_repository import SceneRepository
from .cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from .export_io import write_text_atomic
from .native_backup import DATABASE_NAME, create_backup
from .managed_assets import MANAGED_ASSETS_DIRNAME
from .clock import utc_now_iso as _utc_now


_LOGGER = logging.getLogger('htdt.native')

GENERATOR_VERSION = 1


class ProjectClass(str, Enum):
    P_SMALL = 'p-small'
    P_LARGE = 'p-large'
    P_STRESS = 'p-stress'


class InteractionClass(str, Enum):
    INSTANT = 'instant_interaction'
    SHORT_FOREGROUND = 'short_foreground'
    LONG_OPERATION = 'long_operation'


@dataclass(frozen=True)
class ProjectClassSpec:
    """Scale dimensions of one representative project class."""

    project_class: ProjectClass
    revision_count: int
    entity_count: int
    campaign_count: int
    samples_per_dataset: int
    checkpoint_interval: int


#: P-stress is deliberately larger than the supported comfort target; it
#: exists to surface asymptotic failures, not to claim guaranteed
#: interactive performance.
PROJECT_CLASS_SPECS: dict[ProjectClass, ProjectClassSpec] = {
    ProjectClass.P_SMALL: ProjectClassSpec(
        project_class=ProjectClass.P_SMALL,
        revision_count=10,
        entity_count=14,
        campaign_count=3,
        samples_per_dataset=32,
        checkpoint_interval=5,
    ),
    ProjectClass.P_LARGE: ProjectClassSpec(
        project_class=ProjectClass.P_LARGE,
        revision_count=200,
        entity_count=60,
        campaign_count=12,
        samples_per_dataset=128,
        checkpoint_interval=25,
    ),
    ProjectClass.P_STRESS: ProjectClassSpec(
        project_class=ProjectClass.P_STRESS,
        revision_count=600,
        entity_count=120,
        campaign_count=24,
        samples_per_dataset=256,
        checkpoint_interval=50,
    ),
}


@dataclass(frozen=True)
class OperationBudget:
    """Declared budget for one user-visible operation.

    ``budget_ms=None`` marks a long operation whose contract is not a
    wall-clock cap but threading: it must run off the GUI thread, show
    progress, and be cancellable where transaction semantics allow.
    Concrete millisecond budgets are tightened only after a baseline is
    recorded on declared reference hardware — they are not portability
    claims.
    """

    interaction_class: InteractionClass
    budget_ms: int | None
    requires_background_progress: bool = False
    cancellable_expected: bool = False


#: Frozen per-operation budgets keyed by operation name.
OPERATION_BUDGETS: dict[str, OperationBudget] = {
    # Application/project
    'open_project': OperationBudget(InteractionClass.SHORT_FOREGROUND, 2000),
    'switch_projects': OperationBudget(InteractionClass.SHORT_FOREGROUND, 2000),
    # History
    'history_list': OperationBudget(InteractionClass.SHORT_FOREGROUND, 800),
    'revision_compare': OperationBudget(InteractionClass.SHORT_FOREGROUND, 800),
    'save_revision': OperationBudget(InteractionClass.SHORT_FOREGROUND, 1200),
    'create_checkpoint': OperationBudget(InteractionClass.INSTANT, 300),
    # Room (declared; timed on reference hardware)
    'select_object': OperationBudget(InteractionClass.INSTANT, 100),
    'context_switch': OperationBudget(InteractionClass.INSTANT, 100),
    'visibility_toggle': OperationBudget(InteractionClass.INSTANT, 100),
    # Measurements
    'measurement_enumerate': OperationBudget(InteractionClass.INSTANT, 500),
    # Capture/authority
    'inbox_summary': OperationBudget(InteractionClass.INSTANT, 500),
    # Project lifecycle
    'backup_create': OperationBudget(
        InteractionClass.LONG_OPERATION,
        None,
        requires_background_progress=True,
        cancellable_expected=True,
    ),
}


@dataclass(frozen=True)
class OperationMeasurement:
    operation: str
    interaction_class: InteractionClass
    duration_ms: float
    work_metrics: dict[str, int | float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectFixtureManifest:
    generator_version: int
    project_class: str
    document_id: str
    revision_count: int
    entity_count: int
    measurement_count: int
    checkpoint_count: int
    asset_count: int
    asset_bytes: int
    database_bytes: int
    built_at_utc: str


@dataclass(frozen=True)
class ProjectBenchmarkReport:
    generator_version: int
    project_class: str
    document_id: str
    machine: dict[str, str | int | None]
    measurements: tuple[OperationMeasurement, ...]
    violations: tuple[str, ...]
    measured_at_utc: str


def machine_profile() -> dict[str, str | int | None]:
    """Reference-hardware description for one recorded measurement run."""

    ram_bytes: int | None = None
    try:
        ram_bytes = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, OSError, ValueError):
        pass
    return {
        'platform': platform.platform(),
        'machine': platform.machine(),
        'processor': platform.processor() or None,
        'python': platform.python_version(),
        'cpu_count': os.cpu_count(),
        'ram_bytes': ram_bytes,
    }


# ----------------------------------------------------------------------
# instrumentation journal — the single optional timing layer (#663 §6)
# ----------------------------------------------------------------------

def _perf_journal_path(data_dir: Path) -> Path:
    return Path(data_dir) / 'diagnostics' / 'perf-events.jsonl'


def record_perf_event(
    data_dir: Path,
    *,
    operation: str,
    duration_ms: float,
    work_metrics: dict[str, int | float | str] | None = None,
    document_id: str | None = None,
) -> None:
    """Append one bounded timing record to the diagnostics journal.

    Best-effort: instrumentation failure never aborts the operation it
    measures.
    """

    try:
        path = _perf_journal_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'event_id': uuid4().hex,
            'operation': operation,
            'duration_ms': round(duration_ms, 3),
            'work_metrics': work_metrics or {},
            'document_id': document_id,
            'recorded_at_utc': _utc_now(),
        }
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        with open(path, 'ab') as destination, open(tmp, 'rb') as source:
            destination.write(source.read())
        tmp.unlink(missing_ok=True)
    except OSError:
        _LOGGER.debug('perf event journal write failed', exc_info=True)


@contextmanager
def perf_timer(
    data_dir: Path,
    operation: str,
    *,
    work_metrics: dict[str, int | float | str] | None = None,
    document_id: str | None = None,
):
    """Time one high-level operation into the perf journal."""

    start = time.perf_counter()
    yield
    record_perf_event(
        data_dir,
        operation=operation,
        duration_ms=(time.perf_counter() - start) * 1000.0,
        work_metrics=work_metrics,
        document_id=document_id,
    )


# ----------------------------------------------------------------------
# deterministic fixture generator (#663 §1-§2)
# ----------------------------------------------------------------------

_FIXTURE_KINDS = (
    'speaker',
    'seat',
    'screen',
    'projector',
    'av_equipment',
    'riser',
    'furniture',
)
_SPEAKER_ROLES = ('FL', 'FR', 'C', 'SL', 'SR', 'SBL', 'SBR', 'SW1', 'SW2')


def _fixture_entity(index: int) -> SceneEntity:
    """One plausible entity; deterministic from the index, no randomness."""

    kind = _FIXTURE_KINDS[index % len(_FIXTURE_KINDS)]
    lane = index // len(_FIXTURE_KINDS)
    speaker_count = index // len(_FIXTURE_KINDS)
    return SceneEntity(
        entity_id=f'entity-{index:03d}',
        kind=kind,
        name=f'{kind} {lane + 1}',
        speaker_role=(
            _SPEAKER_ROLES[speaker_count]
            if kind == 'speaker' and speaker_count < len(_SPEAKER_ROLES)
            else (
                f'UNASSIGNED-{speaker_count}'
                if kind == 'speaker'
                else None
            )
        ),
        position=Position3(
            x_m=0.4 + (index % 7) * 0.55,
            y_m=0.6 + (index % 5) * 0.8,
            z_m=0.2 + (index % 3) * 0.35,
        ),
        size_m=Size3(
            x_m=0.25 + (index % 4) * 0.05,
            y_m=0.3 + (index % 3) * 0.07,
            z_m=0.4 + (index % 5) * 0.06,
        ),
        aim_xyz=None,
    )


def _base_fixture_scene(document_id: str, entity_count: int) -> SceneDocument:
    # One standalone measurement point: the fixture's measurements bind to
    # it exactly like a user's imported sweep at the listening position.
    mlp = SceneEntity(
        entity_id='point-mlp',
        kind='measurement_point',
        name='MLP',
        position=Position3(x_m=3.1, y_m=2.4, z_m=1.1),
        size_m=None,
        aim_xyz=None,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.2, depth_m=4.8, height_m=2.5),
        entities=(mlp, *(_fixture_entity(i) for i in range(entity_count))),
    )


def _mutate_scene(scene: SceneDocument, generation: int) -> SceneDocument:
    """One deterministic small edit — the kind of change a user makes."""

    index = generation % len(scene.entities)
    entities = list(scene.entities)
    entity = entities[index]
    entities[index] = entity.model_copy(
        update={
            'position': Position3(
                x_m=entity.position.x_m + 0.05,
                y_m=entity.position.y_m,
                z_m=entity.position.z_m,
            )
        }
    )
    return scene.model_copy(update={'entities': tuple(entities)})


def _fixture_measurement(
    revision,
    index: int,
    sample_count: int,
) -> tuple[object, CadFrequencyResponseDataset, bytes]:
    """One valid declared FR measurement via the real importer path."""

    frequency_hz = tuple(20.0 + 20.0 * step for step in range(sample_count))
    level_db = tuple(
        68.0 + ((index + step) % 5) * 0.5 for step in range(sample_count)
    )
    raw = declared_fr_raw(
        frequency_hz=frequency_hz,
        level_db=level_db,
        phase_status='absent',
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=f'fixture-measurement-{index:04d}',
        evidence_type='measured',
        imported_at='2026-09-20T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'fixture-dataset-{index:04d}',
        measurement_id=record.measurement_id,
        frequency_hz=frequency_hz,
        level_db=level_db,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    return record, dataset, raw


def build_representative_project(
    data_dir: Path,
    project_class: ProjectClass,
    *,
    document_id: str | None = None,
) -> ProjectFixtureManifest:
    """Build one deterministic representative project in ``data_dir``.

    Every row and file is produced through normal production authority
    paths — ``SceneRepository.save``, ``set_revision_label`` and
    ``CadMeasurementRepository.save`` — so the fixture exercises the same
    write/verify/index work a real user's project does.
    """

    data_dir = Path(data_dir)
    spec = PROJECT_CLASS_SPECS[project_class]
    document_id = document_id or f'fixture-{project_class.value}'

    repository = SceneRepository(data_dir / DATABASE_NAME)
    measurements = CadMeasurementRepository(repository)

    scene = _base_fixture_scene(document_id, spec.entity_count)
    saved = repository.save(scene, parent_revision_id=None)
    parent_id = saved.revision.revision_id
    for generation in range(1, spec.revision_count):
        scene = _mutate_scene(scene, generation)
        saved = repository.save(scene, parent_revision_id=parent_id)
        parent_id = saved.revision.revision_id
    head = repository.current_head(document_id)
    assert head is not None

    checkpoint_count = 0
    for summary in repository.list_revision_summaries(document_id)[
        :: spec.checkpoint_interval
    ]:
        repository.set_revision_label(
            summary.revision_id,
            label=f'checkpoint-{checkpoint_count + 1:03d}',
            note='representative project fixture checkpoint',
        )
        checkpoint_count += 1

    for index in range(spec.campaign_count):
        record, dataset, raw = _fixture_measurement(
            head,
            index,
            spec.samples_per_dataset,
        )
        measurements.save(
            record,
            dataset,
            raw_filename=f'fixture-{index:04d}.json',
            raw_bytes=raw,
        )

    asset_count = 0
    asset_bytes = 0
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME
    if assets_root.is_dir():
        for candidate in assets_root.iterdir():
            if candidate.is_file():
                asset_count += 1
                asset_bytes += candidate.stat().st_size

    manifest = ProjectFixtureManifest(
        generator_version=GENERATOR_VERSION,
        project_class=project_class.value,
        document_id=document_id,
        revision_count=spec.revision_count,
        entity_count=spec.entity_count,
        measurement_count=spec.campaign_count,
        checkpoint_count=checkpoint_count,
        asset_count=asset_count,
        asset_bytes=asset_bytes,
        database_bytes=(data_dir / DATABASE_NAME).stat().st_size,
        built_at_utc=_utc_now(),
    )
    _LOGGER.info('representative project built: %r', manifest)
    return manifest


# ----------------------------------------------------------------------
# data-path benchmark (#663 §3)
# ----------------------------------------------------------------------

def _timed(
    operation: str,
    fn,
    work_metrics: dict[str, int | float | str] | None = None,
) -> OperationMeasurement:
    start = time.perf_counter()
    result = fn()
    duration_ms = (time.perf_counter() - start) * 1000.0
    metrics = dict(work_metrics or {})
    if isinstance(result, int):
        metrics.setdefault('rows', result)
    return OperationMeasurement(
        operation=operation,
        interaction_class=OPERATION_BUDGETS[operation].interaction_class,
        duration_ms=duration_ms,
        work_metrics=metrics,
    )


def measure_project_operations(
    data_dir: Path,
    document_id: str,
    *,
    report_dir: Path | None = None,
) -> ProjectBenchmarkReport:
    """Time the headlessly measurable user-visible operations.

    Measured operations cover the Application/project, Save/history,
    Measurements, Capture/authority and Project lifecycle rows of the
    issue's table; Room/viewport timing is declared for the owned-Windows
    acceptance pass and is intentionally absent here.
    """

    data_dir = Path(data_dir)
    repository = SceneRepository(data_dir / DATABASE_NAME)
    measurements = CadMeasurementRepository(repository)

    ops: list[OperationMeasurement] = []

    head = repository.current_head(document_id)
    ops.append(
        _timed(
            'open_project',
            lambda: repository.current_head(document_id),
            {
                'document_payload_bytes': len(
                    repository.current_head(document_id).document.model_dump_json()
                )
                if head is not None
                else 0
            },
        )
    )

    summaries = repository.list_revision_summaries(document_id)
    ops.append(
        _timed(
            'history_list',
            lambda: len(repository.list_revision_summaries(document_id)),
            {'revisions': len(summaries)},
        )
    )

    def _compare_head_with_predecessor() -> int:
        if len(summaries) < 2:
            return 0
        head_rev = repository.get(summaries[-1].revision_id)
        prev_rev = repository.get(summaries[-2].revision_id)
        assert head_rev is not None and prev_rev is not None
        return 1 if head_rev.content_hash != prev_rev.content_hash else 0

    ops.append(_timed('revision_compare', _compare_head_with_predecessor))

    if head is not None:
        edited = _mutate_scene(head.document, 999_983)
        ops.append(
            _timed(
                'save_revision',
                lambda: repository.save(
                    edited, parent_revision_id=head.revision_id
                ),
            )
        )
        head = repository.current_head(document_id)

    ops.append(
        _timed(
            'create_checkpoint',
            lambda: repository.set_revision_label(
                head.revision_id, label='benchmark-checkpoint'
            ).revision_id
            if head is not None
            else None,
        )
    )

    ops.append(
        _timed(
            'measurement_enumerate',
            lambda: len(measurements.list_measurements(document_id)),
        )
    )

    # Inbox summary reads whatever staged items exist; the fixture does
    # not fabricate capture deliveries (CaptureInbox.stage requires a
    # real ingestion lineage), so 0 items is an honest result.
    try:
        from .capture_inbox import CaptureInboxRepository

        inbox = CaptureInboxRepository(repository)
        ops.append(
            _timed('inbox_summary', lambda: len(inbox.list_items()))
        )
    except Exception:
        ops.append(
            OperationMeasurement(
                operation='inbox_summary',
                interaction_class=OPERATION_BUDGETS[
                    'inbox_summary'
                ].interaction_class,
                duration_ms=0.0,
                work_metrics={'skipped': 'capture inbox unavailable'},
            )
        )

    if report_dir is not None:
        backup_dir = Path(report_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f'perf-backup-{uuid4().hex[:8]}.htdt-backup'
        try:
            ops.append(
                _timed(
                    'backup_create',
                    lambda: create_backup(data_dir, backup_path),
                    {'archive_bytes': backup_path.stat().st_size}
                    if backup_path.is_file()
                    else {},
                )
            )
        finally:
            backup_path.unlink(missing_ok=True)

    violations = tuple(
        violation
        for measurement in ops
        if (violation := _budget_violation(measurement)) is not None
    )
    report = ProjectBenchmarkReport(
        generator_version=GENERATOR_VERSION,
        project_class=document_id.removeprefix('fixture-'),
        document_id=document_id,
        machine=machine_profile(),
        measurements=tuple(ops),
        violations=violations,
        measured_at_utc=_utc_now(),
    )
    for measurement in ops:
        record_perf_event(
            data_dir,
            operation=measurement.operation,
            duration_ms=measurement.duration_ms,
            work_metrics=dict(measurement.work_metrics),
            document_id=document_id,
        )
    return report


def _budget_violation(measurement: OperationMeasurement) -> str | None:
    budget = OPERATION_BUDGETS.get(measurement.operation)
    if budget is None or budget.budget_ms is None:
        return None
    if measurement.duration_ms > budget.budget_ms:
        return (
            f'{measurement.operation}: {measurement.duration_ms:.0f}ms '
            f'exceeds {budget.budget_ms}ms {budget.interaction_class.value} '
            'budget'
        )
    return None


def report_to_dict(report: ProjectBenchmarkReport) -> dict:
    return {
        'generator_version': report.generator_version,
        'project_class': report.project_class,
        'document_id': report.document_id,
        'machine': report.machine,
        'measured_at_utc': report.measured_at_utc,
        'violations': list(report.violations),
        'operations': [
            {
                'operation': m.operation,
                'interaction_class': m.interaction_class.value,
                'duration_ms': round(m.duration_ms, 2),
                'work_metrics': m.work_metrics,
                'budget_ms': OPERATION_BUDGETS.get(
                    m.operation, OperationBudget(
                        InteractionClass.SHORT_FOREGROUND, None
                    )
                ).budget_ms,
            }
            for m in report.measurements
        ],
    }


def manifest_to_dict(manifest: ProjectFixtureManifest) -> dict:
    return {
        'generator_version': manifest.generator_version,
        'project_class': manifest.project_class,
        'document_id': manifest.document_id,
        'revision_count': manifest.revision_count,
        'entity_count': manifest.entity_count,
        'measurement_count': manifest.measurement_count,
        'checkpoint_count': manifest.checkpoint_count,
        'asset_count': manifest.asset_count,
        'asset_bytes': manifest.asset_bytes,
        'database_bytes': manifest.database_bytes,
        'built_at_utc': manifest.built_at_utc,
    }


def write_benchmark_artifacts(
    manifest: ProjectFixtureManifest,
    report: ProjectBenchmarkReport,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Persist generator summary + measured report under benchmarks/."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / f'{manifest.project_class}-manifest.json'
    report_path = output_dir / f'{manifest.project_class}-baseline.json'
    write_text_atomic(
        manifest_path,
        json.dumps(manifest_to_dict(manifest), indent=2, allow_nan=False) + '\n',
    )
    write_text_atomic(
        report_path,
        json.dumps(report_to_dict(report), indent=2, allow_nan=False) + '\n',
    )
    return manifest_path, report_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description='Build a representative project and measure the '
        'data-path performance budget (#663)'
    )
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument(
        '--project-class',
        choices=[cls.value for cls in ProjectClass],
        required=True,
    )
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument(
        '--no-measure', action='store_true', help='build the fixture only'
    )
    args = parser.parse_args(argv)

    project_class = ProjectClass(args.project_class)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_representative_project(args.data_dir, project_class)
    print(json.dumps(manifest_to_dict(manifest), indent=2))
    if args.no_measure:
        return 0
    report = measure_project_operations(
        args.data_dir,
        manifest.document_id,
        report_dir=args.data_dir,
    )
    print(json.dumps(report_to_dict(report), indent=2))
    if args.output_dir is not None:
        manifest_path, report_path = write_benchmark_artifacts(
            manifest, report, args.output_dir
        )
        print(f'wrote {manifest_path}')
        print(f'wrote {report_path}')
    return 1 if report.violations else 0


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = [
    'GENERATOR_VERSION',
    'InteractionClass',
    'OPERATION_BUDGETS',
    'OperationBudget',
    'OperationMeasurement',
    'ProjectBenchmarkReport',
    'ProjectClass',
    'ProjectClassSpec',
    'ProjectFixtureManifest',
    'PROJECT_CLASS_SPECS',
    'build_representative_project',
    'machine_profile',
    'manifest_to_dict',
    'measure_project_operations',
    'perf_timer',
    'record_perf_event',
    'report_to_dict',
    'write_benchmark_artifacts',
]
