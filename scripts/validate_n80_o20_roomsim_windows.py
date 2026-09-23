from __future__ import annotations

import argparse
from dataclasses import asdict
import gc
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim import CadRoomSimBinding, CadRoomSimSourceBinding
from htdt.cad_roomsim_batch_runner import build_cad_roomsim_batch_spec, run_cad_roomsim_batch
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_scene import Offset3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_search import build_cad_search_spec, generate_cad_candidates
from htdt.cad_search_models import CadSearchAxis
from htdt.cad_search_repository import CadSearchRepository
from htdt.rew_api import DEFAULT_REW_API_URL
from htdt.rew_roomsim_batch import RewRoomSimControlClient, roomsim_state_sha256


MOVE_M = 0.01


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


def roomsim_openapi_subset(document: dict[str, Any]) -> dict[str, Any]:
    paths = document.get('paths')
    if not isinstance(paths, dict):
        raise RuntimeError('REW OpenAPI document does not contain paths')
    selected = {
        path: value
        for path, value in paths.items()
        if isinstance(path, str) and path.startswith('/roomsim')
    }
    if not selected:
        raise RuntimeError('REW OpenAPI document contains no /roomsim paths')
    return {
        'openapi': document.get('openapi'),
        'info': document.get('info'),
        'roomsim_paths': selected,
        'components': document.get('components') if isinstance(document.get('components'), dict) else {},
    }


def method_summary(subset: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for path, item in subset['roomsim_paths'].items():
        if not isinstance(item, dict):
            continue
        methods = tuple(
            sorted(
                key.upper()
                for key in item
                if isinstance(key, str) and key.lower() in {'get', 'post', 'put', 'delete', 'patch'}
            )
        )
        result[str(path)] = methods
    return result


def moved_x(x_m: float, room_width_m: float) -> float:
    if x_m + MOVE_M <= room_width_m:
        return round(x_m + MOVE_M, 12)
    if x_m - MOVE_M >= 0.0:
        return round(x_m - MOVE_M, 12)
    raise RuntimeError('Room Simulator head X cannot be moved by 1 cm within the room bounds')


def live_scene(snapshot, source_name: str) -> SceneDocument:
    room = snapshot.room_size
    source = snapshot.sources[source_name]['position_htdt']
    head = snapshot.head_position_htdt
    return SceneDocument(
        document_id='n80-o20-live-acceptance',
        schema_version=2,
        room=RoomPrism(
            width_m=float(room['width']),
            depth_m=float(room['length']),
            height_m=float(room['height']),
        ),
        entities=(
            SceneEntity(
                entity_id='speaker-live',
                kind='speaker',
                name=f'REW {source_name}',
                speaker_role=source_name,
                position=Position3(
                    x_m=float(source['x_m']),
                    y_m=float(source['y_m']),
                    z_m=float(source['z_m']),
                ),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
            ),
            SceneEntity(
                entity_id='point-live',
                kind='measurement_point',
                name='REW Main',
                position=Position3(
                    x_m=float(head['x_m']),
                    y_m=float(head['y_m']),
                    z_m=float(head['z_m']),
                ),
            ),
        ),
    )


def run_gate(base_url: str) -> bool:
    client = RewRoomSimControlClient(base_url, timeout_s=5.0)
    version_payload = client._get_json('/version')
    document = client._get_json('/doc.json')
    if not isinstance(version_payload, dict) or not isinstance(document, dict):
        raise RuntimeError('Unexpected REW metadata payload')

    subset = roomsim_openapi_subset(document)
    methods = method_summary(subset)
    print('N80_O20_REW_VERSION', version_payload.get('message'), flush=True)
    print('N80_O20_OPENAPI_VERSION', subset.get('openapi'), flush=True)
    print('N80_O20_OPENAPI_ROOMSIM_SHA256', canonical_sha256(subset), flush=True)
    print('N80_O20_ROOMSIM_PATH_COUNT', len(methods), flush=True)
    for path in sorted(methods):
        print('N80_O20_ROOMSIM_PATH', path, ','.join(methods[path]), flush=True)

    baseline = client.get_roomsim_snapshot()
    if baseline.room_size.get('unit') != 'metres':
        raise RuntimeError('Room Simulator room size is not in metres')
    if not baseline.active_sources:
        raise RuntimeError('Room Simulator has no active source for source-specific acceptance')

    source_name = sorted(baseline.active_sources)[0]
    if source_name not in baseline.sources:
        raise RuntimeError(f'Active Room Simulator source has no position snapshot: {source_name}')
    print('N80_O20_SOURCE', source_name, flush=True)

    baseline_state_sha = roomsim_state_sha256(baseline)
    baseline_response = client.get_roomsim_frequency_response(
        mic_position='Main',
        source_name=source_name,
    )
    baseline_response_sha = canonical_sha256(asdict(baseline_response))
    print('N80_O20_BASELINE_STATE_SHA256', baseline_state_sha, flush=True)
    print('N80_O20_BASELINE_FR_SHA256', baseline_response_sha, flush=True)
    print('N80_O20_BASELINE_FR_POINTS', len(baseline_response.frequency_hz), flush=True)

    target_x = moved_x(float(baseline.head_position_htdt['x_m']), float(baseline.room_size['width']))
    print('N80_O20_HEAD_X_BEFORE_M', baseline.head_position_htdt['x_m'], flush=True)
    print('N80_O20_HEAD_X_TARGET_M', target_x, flush=True)

    with tempfile.TemporaryDirectory(prefix='htdt-n80-o20-') as temp:
        scene_repository = SceneRepository(Path(temp) / 'acceptance.sqlite3')
        revision = scene_repository.save(live_scene(baseline, source_name), parent_revision_id=None).revision

        constraint_set = CadConstraintSet(document_id=revision.document_id, constraints=())
        search_spec, estimate = build_cad_search_spec(
            revision,
            constraint_set,
            (
                CadSearchAxis(
                    entity_id='point-live',
                    axis='x',
                    min_m=target_x,
                    max_m=target_x,
                    step_m=MOVE_M,
                ),
            ),
            candidate_limit=1,
            name='N80 O20 owned-Windows acceptance',
        )
        if estimate['raw_candidate_count'] != 1:
            raise RuntimeError(f'Expected exactly one raw candidate, got {estimate!r}')

        search_repository = CadSearchRepository(scene_repository)
        search_repository.save(search_spec)
        candidate_page = generate_cad_candidates(
            scene_repository,
            search_spec,
            offset=0,
            limit=1,
        )
        if candidate_page.feasible_candidate_count != 1 or len(candidate_page.candidates) != 1:
            raise RuntimeError(
                'Expected exactly one feasible live Room Simulator acceptance candidate'
            )

        binding = CadRoomSimBinding(
            receiver_entity_id='point-live',
            sources=(
                CadRoomSimSourceBinding(
                    entity_id='speaker-live',
                    rew_source_name=source_name,
                ),
            ),
            response_source_name=source_name,
        )
        batch_spec = build_cad_roomsim_batch_spec(
            revision,
            search_spec,
            candidate_set_sha256=candidate_page.candidate_set_sha256,
            candidates=candidate_page.candidates,
            binding=binding,
        )
        result_repository = CadRoomSimRepository(scene_repository, search_repository)
        outcome = run_cad_roomsim_batch(
            result_repository,
            client,
            batch_spec,
        )
        if outcome.failed_candidate_id is not None or outcome.cancelled:
            raise RuntimeError(f'Live O20 batch did not complete cleanly: {outcome!r}')
        if len(outcome.completed_candidate_ids) != 1:
            raise RuntimeError(f'Live O20 batch did not complete exactly one candidate: {outcome!r}')

        attempts = result_repository.list_attempts(batch_spec.batch_run_id)
        if len(attempts) != 1 or attempts[0].status != 'completed':
            raise RuntimeError('Live O20 batch did not persist exactly one completed attempt')
        attempt = attempts[0]
        if attempt.model_version != baseline.rew_version:
            raise RuntimeError('Persisted REW version differs from live baseline version')
        if attempt.pre_state_sha256 != baseline_state_sha:
            raise RuntimeError('Persisted pre-state hash differs from live baseline')
        if attempt.restored_state_sha256 != baseline_state_sha:
            raise RuntimeError('Persisted restored-state hash differs from live baseline')
        if attempt.applied_state_sha256 == baseline_state_sha:
            raise RuntimeError('Candidate apply did not change the Room Simulator state hash')
        if attempt.response_sha256 is None:
            raise RuntimeError('Completed live attempt has no response hash')
        print('N80_O20_BATCH_SPEC_SHA256', batch_spec.batch_spec_sha256, flush=True)
        print('N80_O20_ATTEMPT_SHA256', attempt.attempt_sha256, flush=True)
        print('N80_O20_APPLIED_STATE_SHA256', attempt.applied_state_sha256, flush=True)
        print('N80_O20_CANDIDATE_FR_SHA256', attempt.response_sha256, flush=True)

        # Verify live restore before the temporary persistence database is torn down.
        # On Windows, sqlite finalizers may otherwise run after TemporaryDirectory
        # starts deletion and turn a successful Room Simulator transaction into a
        # harness-only WinError 32.
        restored = client.get_roomsim_snapshot()
        restored_state_sha = roomsim_state_sha256(restored)
        restored_response = client.get_roomsim_frequency_response(
            mic_position='Main',
            source_name=source_name,
        )
        restored_response_sha = canonical_sha256(asdict(restored_response))
        candidate_response_sha = attempt.response_sha256

        # Drop repository/result references and collect sqlite finalizers before
        # TemporaryDirectory removes acceptance.sqlite3.
        del attempts, attempt, result_repository, search_repository, scene_repository
        gc.collect()

    state_restored = restored_state_sha == baseline_state_sha
    fr_restored = restored_response_sha == baseline_response_sha
    candidate_fr_changed = candidate_response_sha != baseline_response_sha
    print('N80_O20_RESTORED_STATE_SHA256', restored_state_sha, flush=True)
    print('N80_O20_RESTORED_FR_SHA256', restored_response_sha, flush=True)
    print('N80_O20_STATE_RESTORE_MATCH', state_restored, flush=True)
    print('N80_O20_FR_RESTORE_MATCH', fr_restored, flush=True)
    print('N80_O20_CANDIDATE_FR_CHANGED', candidate_fr_changed, flush=True)

    passed = (
        state_restored
        and fr_restored
        and candidate_fr_changed
        and len(baseline_response.frequency_hz) > 0
    )
    print('N80_O20_LIVE_GATE_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Run N80 O20 live REW Room Simulator reversible-write acceptance.'
    )
    parser.add_argument('--base-url', default=DEFAULT_REW_API_URL)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print('N80_O20_LIVE_GATE_RESULT FAIL', flush=True)
        print('N80_O20_GATE_ERROR=This acceptance requires Windows.', flush=True)
        return 1
    try:
        return 0 if run_gate(args.base_url) else 1
    except Exception as exc:
        print(f'N80_O20_GATE_ERROR={type(exc).__name__}: {exc}', flush=True)
        print('N80_O20_LIVE_GATE_RESULT FAIL', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
