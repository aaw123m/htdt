from __future__ import annotations

import argparse
import gc
from math import cos, pi, sin
from pathlib import Path
import sys
import tempfile
from time import perf_counter

import numpy as np
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.analysis_markers import render_analysis_marker_cloud
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Offset3, Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.native_cad import TheaterEditorWindow
from htdt.prediction_workspace import PredictionWorkspaceWindow

from validate_n40_windows import foreground, pump

if sys.platform != 'win32':
    raise SystemExit('This benchmark requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

FIXTURE_ID = 'fixture-f5-n70'
MARKER_COUNT = 10_000
EDITABLE_OBJECT_COUNT = 50


def make_f5_scene() -> SceneDocument:
    entities: list[SceneEntity] = [
        SceneEntity(
            entity_id='speaker-fl',
            kind='speaker',
            name='Front Left',
            speaker_role='FL',
            position=Position3(x_m=1.3, y_m=0.8, z_m=1.0),
            size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            acoustic_reference_offset_m=Offset3(),
        ),
        SceneEntity(
            entity_id='speaker-fr',
            kind='speaker',
            name='Front Right',
            speaker_role='FR',
            position=Position3(x_m=4.7, y_m=0.8, z_m=1.0),
            size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
            acoustic_reference_offset_m=Offset3(),
        ),
        SceneEntity(
            entity_id='point-mlp',
            kind='measurement_point',
            name='MLP',
            position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
        ),
    ]
    for index in range(EDITABLE_OBJECT_COUNT):
        column = index % 10
        row = index // 10
        entities.append(
            SceneEntity(
                entity_id=f'furniture-{index:02d}',
                kind='furniture',
                name=f'F5 furniture {index + 1}',
                position=Position3(
                    x_m=0.35 + column * 0.57,
                    y_m=1.25 + row * 0.48,
                    z_m=0.18,
                ),
                size_m=Size3(x_m=0.18, y_m=0.18, z_m=0.36),
            )
        )
    return SceneDocument(
        document_id=FIXTURE_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=tuple(entities),
    )


def make_marker_points() -> np.ndarray:
    x, y = np.meshgrid(
        np.linspace(0.05, 5.95, 100),
        np.linspace(0.05, 3.95, 100),
    )
    z = 0.05 + (np.arange(MARKER_COUNT, dtype=float) % 7.0) * 0.018
    return np.column_stack((x.ravel(), y.ravel(), z))


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = int(round((len(ordered) - 1) * percentile_value))
    return float(ordered[max(0, min(len(ordered) - 1, rank))])


def actor_is_pickable(actor: object) -> bool:
    getter = getattr(actor, 'GetPickable', None)
    if callable(getter):
        return bool(getter())
    value = getattr(actor, 'pickable', None)
    return bool(value) if value is not None else True


def orbit_frame(window: PredictionWorkspaceWindow, frame_index: int, total: int) -> None:
    angle = 2.0 * pi * float(frame_index) / float(total)
    focal = (3.0, -2.0, 1.1)
    radius = 7.5
    position = (
        focal[0] + radius * cos(angle),
        focal[1] + radius * sin(angle),
        4.2,
    )
    window.viewport.camera_position = [position, focal, (0.0, 0.0, 1.0)]
    window.viewport.render()


def run_f5(app: QApplication, root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(make_f5_scene(), parent_revision_id=None)
    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    try:
        product_ok = isinstance(window, PredictionWorkspaceWindow)
        editable_count = sum(
            1
            for entity in window.working.committed_document.entities
            if entity.kind == 'furniture'
        ) if window.working is not None else 0
        print('F5_PRODUCT_COMPOSITION', product_ok, flush=True)
        print('F5_EDITABLE_OBJECT_COUNT', editable_count, flush=True)
        if not product_ok or editable_count != EDITABLE_OBJECT_COUNT:
            return False

        points = make_marker_points()
        before_actor_count = len(window.viewport.renderer.actors)
        started = perf_counter()
        rendered = render_analysis_marker_cloud(
            window.viewport,
            points,
            actor_name='f5-analysis-marker-cloud',
            point_size=5.0,
            render=True,
        )
        first_render_ms = (perf_counter() - started) * 1000.0
        pump(app, 0.10)
        after_actor_count = len(window.viewport.renderer.actors)
        actor_delta = after_actor_count - before_actor_count
        non_pickable = not actor_is_pickable(rendered.actor)
        structural_ok = (
            rendered.point_count == MARKER_COUNT
            and actor_delta == 1
            and non_pickable
        )
        print('F5_MARKER_COUNT', rendered.point_count, flush=True)
        print('F5_MARKER_ACTOR_DELTA', actor_delta, flush=True)
        print('F5_MARKER_NON_PICKABLE', non_pickable, flush=True)
        print('F5_FIRST_RENDER_MS', f'{first_render_ms:.3f}', flush=True)
        if not structural_ok:
            return False

        for index in range(6):
            orbit_frame(window, index, 40)
            app.processEvents()

        frame_ms: list[float] = []
        for index in range(40):
            frame_started = perf_counter()
            orbit_frame(window, index, 40)
            app.processEvents()
            frame_ms.append((perf_counter() - frame_started) * 1000.0)

        median_ms = percentile(frame_ms, 0.50)
        p95_ms = percentile(frame_ms, 0.95)
        max_ms = max(frame_ms)
        print('F5_ORBIT_FRAME_P50_MS', f'{median_ms:.3f}', flush=True)
        print('F5_ORBIT_FRAME_P95_MS', f'{p95_ms:.3f}', flush=True)
        print('F5_ORBIT_FRAME_MAX_MS', f'{max_ms:.3f}', flush=True)
        print('F5_PERFORMANCE_BUDGET_STATUS', 'MEASURE_ONLY_NO_PRESET_BUDGET', flush=True)
        return True
    finally:
        window.close()
        pump(app, 0.65)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Benchmark N70 F5 bulk analysis-marker rendering on Windows.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if args.keep_data is not None:
        base = args.keep_data
        base.mkdir(parents=True, exist_ok=True)
        passed = run_f5(app, base)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n70-f5-', ignore_cleanup_errors=True) as temp:
            passed = run_f5(app, Path(temp))
    print('F5_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
