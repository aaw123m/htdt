#!/usr/bin/env python3
"""Golden Path software preflight (#877).

Drives a bounded, deterministic software-only slice of the ordinary-user
Golden Path through the product's service/repository orchestration seams —
never direct database writes, never the implicit ``htdt-synthetic-*`` demo
document, never an internal document/UUID handed in from outside.

Exercised end to end on a fresh temporary data root:

    create project -> select via startup resolution (no document id)
    -> author room scene -> bind equipment -> search spec + candidates
    -> development RoomSim prediction -> proposal applied to a revision
    -> verification MeasurementPlan -> synthetic fixture measurement
    -> authority audit -> close + reopen checkpoint
    -> backup -> restore into a clean root -> reopen + lineage equality
    -> blocker-path checks (measurement readiness on a bare context)
    -> machine-readable trace (authority ids/hashes, evidence classes,
       blockers, explicit non-claims)

Fixture evidence is labelled ``synthetic_fixture`` end to end; the trace
records ``final_non_claims`` so the run can never be mistaken for
owned-room/physical acceptance (#723 keeps that gate).
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend' / 'src'))

from htdt import __version__  # noqa: E402
from htdt.cad_constraint_models import CadConstraintSet  # noqa: E402
from htdt.cad_equipment import (  # noqa: E402
    DirectivityCapability,
    EquipmentDataProvenance,
    build_equipment_definition,
)
from htdt.cad_equipment_instance import (  # noqa: E402
    build_installed_equipment_instance,
)
from htdt.cad_equipment_instance_repository import (  # noqa: E402
    CadInstalledEquipmentRepository,
)
from htdt.cad_equipment_repository import CadEquipmentRepository  # noqa: E402
from htdt.cad_equipment_evidence import (  # noqa: E402
    build_equipment_manual_evidence,
)
from htdt.cad_measurement_loop import (  # noqa: E402
    build_measurement_plan,
    complete_measurement_plan,
)
from htdt.cad_measurement_models import (  # noqa: E402
    CadFrequencyResponseDataset,
)
from htdt.cad_measurement_repository import (  # noqa: E402
    CadMeasurementRepository,
)
from htdt.cad_measurements import (  # noqa: E402
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.cad_roomsim import (  # noqa: E402
    CadRoomSimBinding,
    CadRoomSimSourceBinding,
)
from htdt.cad_roomsim_batch_runner import (  # noqa: E402
    build_cad_roomsim_batch_spec,
)
from htdt.cad_roomsim_repository import CadRoomSimRepository  # noqa: E402
from htdt.cad_scene import (  # noqa: E402
    Direction3,
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search import (  # noqa: E402
    build_cad_search_spec,
    candidate_preview_document,
    generate_cad_candidates,
)
from htdt.cad_search_models import CadSearchAxis  # noqa: E402
from htdt.cad_search_repository import CadSearchRepository  # noqa: E402
from htdt.cad_synthetic_demo import (  # noqa: E402
    SYNTHETIC_MODEL_VERSION,
    _completed_attempt,
)
from htdt.cad_system_variant_repository import (  # noqa: E402
    CadSystemVariantRepository,
)
from htdt.native_authority_audit import (  # noqa: E402
    AuthorityAuditError,
    assert_native_authority_graph,
)
from htdt.native_backup import create_backup, restore_backup  # noqa: E402
from htdt.project_library_repository import (  # noqa: E402
    ProjectLibraryRepository,
)
from htdt.readiness import evaluate_measurement_readiness  # noqa: E402

SCENE_DB = 'cad-scenes.sqlite3'
EVIDENCE_CLASS = 'synthetic_fixture'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance(name: str, digit: str) -> EquipmentDataProvenance:
    return EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name=name,
        source_version='preflight-1',
        source_reference='golden-path-preflight',
        source_sha256=digit * 64,
    )


class GoldenPathTrace:
    """Machine-readable Golden Path trace — the CI artifact."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.blockers: list[dict[str, Any]] = []
        self.non_claims: list[str] = [
            'not owned-room validated — no physical room evidence exists',
            'no production-qualified solver exercised — development RoomSim'
            ' fixture only',
            'no real device state verified — no microphone, AVR, or'
            ' measurement hardware involved',
            'no visual/DPI/mouse acceptance — GUI surface intentionally'
            ' untouched',
            'fixture measurements are synthetic_fixture class — never'
            ' production evidence',
        ]

    def step(
        self,
        name: str,
        status: str,
        *,
        authorities: dict[str, Any] | None = None,
        evidence_class: str | None = None,
        detail: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {'step': name, 'status': status}
        if authorities:
            entry['authorities'] = authorities
        if evidence_class:
            entry['evidence_class'] = evidence_class
        if detail:
            entry['detail'] = detail
        self.steps.append(entry)
        label = f'{name}: {status}'
        print(f'[preflight] {label}')

    def to_json(self, *, environment: dict[str, Any]) -> str:
        return json.dumps(
            {
                'format': 'htdt-golden-path-preflight-1',
                'environment': environment,
                'steps': self.steps,
                'blockers': self.blockers,
                'final_non_claims': self.non_claims,
            },
            indent=2,
            sort_keys=True,
        )


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(
            room_id='preflight-room',
            width_m=5.0,
            depth_m=4.0,
            height_m=2.4,
        ),
        entities=(
            SceneEntity(
                entity_id='preflight-fl',
                kind='speaker',
                name='Preflight FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                acoustic_reference_offset_m=Offset3(),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
            SceneEntity(
                entity_id='preflight-mlp',
                kind='measurement_point',
                name='Preflight MLP',
                position=Position3(x_m=2.5, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _open_repositories(data_dir: Path):
    scene_repository = SceneRepository(data_dir / SCENE_DB)
    library = ProjectLibraryRepository(scene_repository)
    return scene_repository, library


def _audit(data_dir: Path, trace: GoldenPathTrace, step: str) -> None:
    report = assert_native_authority_graph(data_dir / SCENE_DB)
    trace.step(
        step,
        'PASS',
        authorities={
            'checked_tables': len(report.checked),
            'coverage_rows': len(report.coverage),
        },
        detail='authority graph audit clean',
    )


def run_preflight(work_dir: Path, trace: GoldenPathTrace) -> int:
    """Run the scenario; returns process exit code."""
    data_dir = work_dir / 'data-main'
    restore_dir = work_dir / 'data-restored'
    backup_path = work_dir / 'preflight.htdt-backup'
    data_dir.mkdir(parents=True)

    scene_repository, library = _open_repositories(data_dir)

    # --- project creation + startup selection (no document knowledge) ---
    entry = library.create_project(
        'Golden Path Preflight',
        description='software-only CI preflight project',
    )
    resolved = library.resolve_startup_document(None)
    if resolved.document_id != entry.document_id:
        raise RuntimeError(
            'startup resolution did not select the only project'
        )
    if len(library.list_projects()) != 1:
        raise RuntimeError('unexpected pre-seeded projects in fresh root')
    document_id = resolved.document_id
    trace.step(
        'create_project',
        'PASS',
        authorities={'project_id': entry.project_id},
        detail='startup resolution selected the only project without a '
        'document id',
    )

    # --- room/scene authoring ---
    source = scene_repository.save(
        _scene(document_id), parent_revision_id=None
    ).revision
    trace.step(
        'author_room',
        'PASS',
        authorities={'scene_revision_id': source.revision_id},
        detail='5.0x4.0x2.4m room, speaker + measurement point',
    )

    # --- equipment binding ---
    variant_repository = CadSystemVariantRepository(scene_repository)
    equipment_repository = CadEquipmentRepository(
        scene_repository, variant_repository
    )
    definition = build_equipment_definition(
        definition_id='preflight-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='Preflight Speaker',
        provenance=(_provenance('preflight-equipment', '2'),),
        cabinet_envelope_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='unknown',
            data_format='unknown',
            provenance=_provenance('preflight-equipment', '2'),
        ),
    )
    for evidence in build_equipment_manual_evidence(
        definition,
        actor='golden-path-preflight',
        recorded_at_utc=_utc_now(),
    ):
        equipment_repository.save_evidence(evidence)
    equipment_repository.save_definition(definition)
    # Post-#819 the repository takes the catalog to verify definition
    # claims; pre-#819 it is scene-only — probe so the lane runs on both.
    try:
        installed = CadInstalledEquipmentRepository(
            scene_repository, equipment_repository
        )
    except TypeError:
        installed = CadInstalledEquipmentRepository(scene_repository)
    instance = build_installed_equipment_instance(
        instance_id='preflight-inst-fl',
        document_id=document_id,
        equipment_class='speaker',
        equipment_definition=definition,
        serial_number='PF-SN-001',
        scene_entity_id='preflight-fl',
        installed_at_utc=_utc_now(),
        provenance=(_provenance('preflight-install', '3'),),
        created_at_utc=_utc_now(),
    )
    installed.save_instance(instance)
    trace.step(
        'bind_equipment',
        'PASS',
        authorities={
            'equipment_definition_sha256': definition.semantic_sha256,
            'installed_instance_id': instance.instance_id,
        },
        detail='definition + installed instance bound to speaker entity',
    )

    # --- search spec + candidates (optimization intake) ---
    search_repository = CadSearchRepository(scene_repository)
    search_spec, _estimate = build_cad_search_spec(
        source,
        CadConstraintSet(document_id=document_id, constraints=()),
        (
            CadSearchAxis(
                entity_id='preflight-fl',
                axis='x',
                min_m=1.2,
                max_m=2.2,
                step_m=0.5,
            ),
        ),
        candidate_limit=20,
        name='Golden Path preflight sweep',
    )
    search_repository.save(search_spec)
    page = generate_cad_candidates(
        scene_repository, search_spec, limit=20
    )
    if not page.candidates:
        raise RuntimeError('search produced no candidates')
    trace.step(
        'search_candidates',
        'PASS',
        authorities={
            'search_spec_id': search_spec.search_spec_id,
            'candidate_set_sha256': page.candidate_set_sha256,
        },
        detail=f'{len(page.candidates)} candidates',
    )

    # --- eligible development prediction (synthetic RoomSim lane) ---
    roomsim_repository = CadRoomSimRepository(
        scene_repository, search_repository
    )
    binding = CadRoomSimBinding(
        receiver_entity_id='preflight-mlp',
        sources=(
            CadRoomSimSourceBinding(
                entity_id='preflight-fl', rew_source_name='Left'
            ),
        ),
        response_source_name='Left',
    )
    batch = build_cad_roomsim_batch_spec(
        source,
        search_spec,
        candidate_set_sha256=page.candidate_set_sha256,
        candidates=page.candidates,
        binding=binding,
    )
    roomsim_repository.save_batch_spec(batch)
    request_sha = {
        request.candidate_id: request.request_sha256
        for request in batch.requests
    }
    for index, candidate in enumerate(page.candidates):
        roomsim_repository.save_attempt(
            _completed_attempt(
                batch_run_id=batch.batch_run_id,
                candidate_id=candidate.candidate_id,
                request_sha256=request_sha[candidate.candidate_id],
                index=index,
            )
        )
    trace.step(
        'development_prediction',
        'PASS',
        authorities={
            'batch_run_id': batch.batch_run_id,
            'attempts': len(page.candidates),
        },
        evidence_class='simulated_dev_fixture',
        detail=(
            f'{SYNTHETIC_MODEL_VERSION} — development lane only, not a'
            ' production-qualified solver'
        ),
    )

    # --- proposal applied to a revision + verification plan ---
    candidate = page.candidates[0]
    applied = scene_repository.save_detached_revision(
        candidate_preview_document(source.document, candidate),
        parent_revision_id=source.revision_id,
        reason='golden_path_preflight_applied_proposal',
    ).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    plan = build_measurement_plan(
        scene_repository,
        search_repository,
        search_spec_id=search_spec.search_spec_id,
        candidate_id=candidate.candidate_id,
        applied_scene_revision_id=applied.revision_id,
    )
    measurement_repository.save_measurement_plan(plan)
    trace.step(
        'proposal_to_revision',
        'PASS',
        authorities={
            'applied_revision_id': applied.revision_id,
            'measurement_plan_id': plan.plan_id,
            'verification_candidate': candidate.candidate_id,
        },
        detail='candidate applied as detached revision; measurement plan'
        ' bound to it',
    )

    # --- explicit synthetic fixture measurement, completed plan ---
    frequency_hz = (20.0, 40.0, 80.0, 160.0)
    level_db = (79.0, 80.0, 78.0, 79.5)
    processing = {
        'classification': 'htdt_synthetic_measurement_fixture',
        'evidence_scope': EVIDENCE_CLASS,
        'physical_measurement': False,
        'synthetic_fixture': True,
    }
    raw = declared_fr_raw(
        frequency_hz=list(frequency_hz),
        level_db=list(level_db),
        phase_deg=None,
        phase_status='absent',
        level_reference='synthetic_fixture',
        smoothing='None',
        processing=processing,
    )
    record = measurement_record_for_revision(
        applied,
        'preflight-mlp',
        measurement_id='preflight-measurement-1',
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('preflight-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        captured_at=_utc_now(),
        imported_at=_utc_now(),
        source_kind='unknown',
        quality_status=EVIDENCE_CLASS,
        quality_reasons=('not_physical_measurement',),
        quality_source='golden_path_preflight',
        provenance={
            'validation_scope': EVIDENCE_CLASS,
            'synthetic_fixture': True,
            'physical_measurement': False,
            'generator': 'golden_path_preflight',
        },
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='preflight-dataset-1',
        measurement_id='preflight-measurement-1',
        frequency_hz=frequency_hz,
        level_db=level_db,
        phase_deg=None,
        phase_status='absent',
        level_reference='synthetic_fixture',
        smoothing='None',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record,
        dataset,
        raw_filename='preflight-measurement-1.json',
        raw_bytes=raw,
    )
    completed_plan = complete_measurement_plan(
        plan, measurement_repository, ('preflight-measurement-1',)
    )
    measurement_repository.save_measurement_plan(completed_plan)
    trace.step(
        'fixture_measurement',
        'PASS',
        authorities={
            'measurement_id': 'preflight-measurement-1',
            'dataset_sha256': dataset.source_sha256,
            'plan_status': completed_plan.status,
        },
        evidence_class=EVIDENCE_CLASS,
        detail='declared-importer fixture bound to verification plan',
    )

    _audit(data_dir, trace, 'authority_audit')

    # --- persistence checkpoint: close, reopen, verify lineage ---
    del (
        scene_repository,
        library,
        search_repository,
        roomsim_repository,
        measurement_repository,
        equipment_repository,
        installed,
        variant_repository,
    )
    scene_repository, library = _open_repositories(data_dir)
    resolved = library.resolve_startup_document(None)
    head = scene_repository.current_head(document_id)
    latest = scene_repository.latest(document_id)
    if resolved.document_id != document_id:
        raise RuntimeError('reopen resolved a different project')
    if len(library.list_projects()) != 1:
        raise RuntimeError(
            'reopen shows additional projects — synthetic re-seed?'
        )
    if head is None or head.revision_id != source.revision_id:
        raise RuntimeError('document head moved unexpectedly')
    trace.step(
        'persistence_checkpoint',
        'PASS',
        authorities={
            'head_revision_id': head.revision_id,
            'latest_revision_id': (
                latest.revision_id if latest is not None else None
            ),
        },
        detail='reopen resolved same project/document; head unchanged; '
        'exactly one project',
    )
    _audit(data_dir, trace, 'post_reopen_audit')

    # --- backup / restore into a clean root / reopen ---
    manifest = create_backup(data_dir, backup_path)
    restore_backup(restore_dir, backup_path)
    restored_scene, restored_library = _open_repositories(restore_dir)
    restored_entry = restored_library.resolve_startup_document(None)
    restored_head = restored_scene.current_head(document_id)
    restored_plan_db = CadMeasurementRepository(restored_scene)
    restored_plans = restored_plan_db.latest_measurement_plans(
        search_spec.search_spec_id
    )
    restored_plan = next(
        (item for item in restored_plans if item.plan_id == plan.plan_id),
        None,
    )
    if restored_entry.document_id != document_id:
        raise RuntimeError('restored root resolved a different document')
    if restored_head is None or restored_head.revision_id != (
        source.revision_id
    ):
        raise RuntimeError('restored lineage diverged from source head')
    if restored_plan is None or restored_plan.status != (
        completed_plan.status
    ):
        raise RuntimeError('restored measurement plan lost its state')
    trace.step(
        'backup_restore_reopen',
        'PASS',
        authorities={
            'backup_schema': manifest.schema_version,
            'restored_document_id': restored_entry.document_id,
            'restored_head': restored_head.revision_id,
            'restored_plan_status': restored_plan.status,
        },
        detail='backup archive restored into a clean root with identical'
        ' project/revision/plan lineage',
    )

    # --- blocker paths: stable reason keys + next actions ---
    readiness = evaluate_measurement_readiness(
        {},
        {},
        [],
        context_id='preflight-empty-context',
    )
    if readiness['machine_ready'] or readiness['status'] != 'blocked':
        raise RuntimeError(
            'empty context unexpectedly reported measurement-ready'
        )
    expected_blockers = {
        'context_microphone_present',
        'rew_audio_ready',
        'rew_input_endpoint_ready',
    }
    failed = set(readiness['failed_check_keys'])
    missing = expected_blockers - failed
    if missing:
        raise RuntimeError(f'expected blocker keys absent: {missing}')
    trace.blockers.append(
        {
            'path': 'measurement_readiness_empty_context',
            'status': readiness['status'],
            'failed_check_keys': readiness['failed_check_keys'],
            'next_actions': readiness['manual_confirmation_required'],
            'classification': readiness['classification'],
        }
    )
    trace.step(
        'blocker_paths',
        'PASS',
        authorities={
            'failed_check_keys': readiness['failed_check_keys'],
        },
        detail='device/mic capability gaps return stable reason keys and'
        ' manual next actions',
    )

    trace.step(
        'final_non_claims',
        'RECORDED',
        detail='; '.join(trace.non_claims),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Golden Path software preflight (#877)'
    )
    parser.add_argument(
        '--work-dir',
        type=Path,
        default=None,
        help='scratch root for the run (default: a fresh temp dir)',
    )
    parser.add_argument(
        '--trace',
        type=Path,
        default=None,
        help='trace JSON output path (default: <work-dir>/golden-path-trace.json)',
    )
    parser.add_argument(
        '--clean-work-dir-on-success',
        action='store_true',
        help='delete the run artifacts inside the work dir on success '
        '(the trace JSON is kept; failure always preserves everything)',
    )
    args = parser.parse_args(argv)

    work_dir = args.work_dir or Path(
        tempfile.mkdtemp(prefix='htdt-golden-path-')
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    trace_path = args.trace or (work_dir / 'golden-path-trace.json')
    trace = GoldenPathTrace()
    environment = {
        'python': platform.python_version(),
        'platform': platform.platform(),
        'htdt_version': __version__,
        'work_dir': str(work_dir),
    }
    try:
        code = run_preflight(work_dir, trace)
    except AuthorityAuditError as error:
        code = 1
        trace.step('authority_audit', 'FAIL', detail=str(error))
    except Exception:
        code = 1
        trace.step(
            'fatal',
            'FAIL',
            detail=traceback.format_exc(limit=8),
        )
    trace_path.write_text(trace.to_json(environment=environment))
    print(f'[preflight] trace written: {trace_path}')
    # The trace is written inside the work dir, so a temporary work dir is
    # never auto-deleted: deleting it on failure would discard the trace
    # (and audit artifacts) that exist precisely to debug that failure.
    if code == 0 and args.clean_work_dir_on_success:
        for artifact in ('data-main', 'data-restored', 'preflight.htdt-backup'):
            target = work_dir / artifact
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        print(f'[preflight] cleaned run artifacts in {work_dir}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
