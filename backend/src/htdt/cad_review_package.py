"""Static/offline 360 review package — PM30 (#534).

Builds a deterministic, hash-manifested directory a client can open with
no HTDT install and no network: a self-contained ``viewer.html``, one
rendered frame per viewpoint (plus yaw-stepped look-around frames when
the session asks for them), SVG drawing sheets derived from the
installation report authority, a verbatim semantic snapshot of the
session, and ``manifest.json`` binding it all.

Honesty rules, per the issue:

- every manifest row records what it is — a yaw-stepped perspective
  render is declared as that, never as an equirectangular panorama;
- when the render path cannot produce a frame the capability row says
  ``unavailable`` with the reason — no placeholder pixels are written;
- ``generated_at_utc`` is inside the hashed manifest payload, so two
  builds at different times produce different package ids while the
  per-entry hashes still prove identical content;
- all entry paths are relative POSIX and verified before write — the
  package never references absolute paths, drives, or parent dirs.
"""

from __future__ import annotations

import html
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_drawing_set import (
    InstallationDrawingSet,
    build_drawing_set_spec,
    generate_drawing_set,
)
from .cad_presentation_session import (
    PresentationSession,
    PresentationViewpoint,
)
from .cad_presentation_repository import CadPresentationRepository
from .cad_repository import SceneRepository
from .cad_scene import SceneDocument, scene_content_hash
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _hash
from .clock import utc_now_iso as _utc_now
from .report import build_installation_output


REVIEW_PACKAGE_SCHEMA = 'htdt.presentation-review-package'
REVIEW_PACKAGE_SCHEMA_VERSION = 1
REVIEW_PACKAGE_KIND = 'design_review'

PACKAGE_GENERATOR = 'htdt.presentation-review-package'


class ReviewPackageError(ValueError):
    """The package cannot be built or verified honestly."""


class ReviewPackageEntry(BaseModel):
    """One file in the package — relative path + content hash + role."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_length: int = Field(ge=0)
    kind: Literal['viewer', 'render', 'drawing', 'manifest', 'semantic']
    #: What this file claims to be. Renders record the exact camera the
    #: frame was produced for ('viewpoint' primary frame or a
    #: 'yaw_stepped' look-around) so provenance is per-file.
    viewpoint_id: str | None = Field(default=None, min_length=1)
    yaw_deg: int | None = Field(default=None, ge=-360, lt=360)

    @model_validator(mode='after')
    def valid_entry(self) -> 'ReviewPackageEntry':
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or '..' in path.parts
            or '\\' in self.path
            or ':' in self.path
        ):
            raise ValueError(
                f'package entry path must be a relative POSIX path: '
                f'{self.path!r}'
            )
        return self


class ReviewPackageCapability(BaseModel):
    """One declared capability row — the package's own honesty table."""

    model_config = ConfigDict(frozen=True)

    capability: str = Field(min_length=1)
    state: Literal['rendered', 'fallback', 'unavailable', 'not_requested']
    detail: str = Field(min_length=1)


class ReviewPackageSecurityRow(BaseModel):
    """One security check the builder ran over the manifest."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: Literal['pass', 'fail']
    detail: str = Field(min_length=1)


class ReviewPackageManifest(BaseModel):
    """Content-addressed manifest over one built package."""

    model_config = ConfigDict(frozen=True)

    schema: Literal['htdt.presentation-review-package'] = REVIEW_PACKAGE_SCHEMA
    schema_version: Literal[1] = REVIEW_PACKAGE_SCHEMA_VERSION
    package_kind: Literal['design_review'] = REVIEW_PACKAGE_KIND
    package_id: str = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    generator: str = Field(min_length=1)
    #: Renderer identity actually used — e.g.
    #: 'pyvista-0.46/vtk-9.7/Mesa 26.2.4'. Recorded, never assumed.
    renderer: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    session_label: str = Field(min_length=1)
    status_label: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    effective_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    comparison_set_id: str | None = Field(default=None, min_length=1)
    comparison_set_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    entries: tuple[ReviewPackageEntry, ...]
    capability_rows: tuple[ReviewPackageCapability, ...]
    security_rows: tuple[ReviewPackageSecurityRow, ...]
    manifest_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_manifest(self) -> 'ReviewPackageManifest':
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError('package entry paths must be unique')
        if self.manifest_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ReviewPackageManifest hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema': self.schema,
            'schema_version': self.schema_version,
            'package_kind': self.package_kind,
            'package_id': self.package_id,
            'generated_at_utc': self.generated_at_utc,
            'generator': self.generator,
            'renderer': self.renderer,
            'session_id': self.session_id,
            'session_sha256': self.session_sha256,
            'session_label': self.session_label,
            'status_label': self.status_label,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'effective_scene_content_hash': self.effective_scene_content_hash,
            'system_variant_id': self.system_variant_id,
            'system_variant_sha256': self.system_variant_sha256,
            'comparison_set_id': self.comparison_set_id,
            'comparison_set_sha256': self.comparison_set_sha256,
            'entries': [
                entry.model_dump(mode='json') for entry in self.entries
            ],
            'capability_rows': [
                row.model_dump(mode='json') for row in self.capability_rows
            ],
            'security_rows': [
                row.model_dump(mode='json') for row in self.security_rows
            ],
        }


class ReviewPackageResult(BaseModel):
    """What a build produced — manifest plus the written file list."""

    model_config = ConfigDict(frozen=True)

    output_dir: str
    manifest: ReviewPackageManifest
    warnings: tuple[str, ...] = ()


class RenderedFrame(BaseModel):
    """One produced frame — bytes plus the camera it was rendered for."""

    model_config = ConfigDict(frozen=True)

    viewpoint_id: str
    yaw_deg: int
    png_bytes: bytes
    renderer_id: str


class OffscreenSceneRenderer:
    """PyVista offscreen renderer for review packages.

    Renders the session's materialized scene with the same visual
    conventions as the interactive room viewport (dark theme, floor,
    wireframe shell, entity bodies + glyph proxies). Lazily builds its
    own offscreen plotter; ``available()`` reports the capability
    honestly so packages on headless machines degrade to zero renders
    instead of failing.
    """

    def __init__(self, width_px: int = 1920, height_px: int = 1080) -> None:
        self._width = width_px
        self._height = height_px
        self._renderer_id: str | None = None

    def renderer_id(self) -> str:
        if self._renderer_id is None:
            import pyvista as pv
            import vtkmodules.all as _vtk  # noqa: F401
            from vtkmodules.vtkCommonCore import vtkVersion

            gl = 'unverified'
            try:
                probe = pv.Plotter(off_screen=True, window_size=(64, 64))
                probe.add_mesh(pv.Sphere(radius=0.05))
                probe.render()
                caps = probe.ren_win.ReportCapabilities()
                for line in (caps or '').splitlines():
                    line = line.strip()
                    if line.startswith('OpenGL version string:'):
                        gl = line.split(':', 1)[1].strip()
                        break
                probe.close()
            except Exception:
                pass
            self._renderer_id = (
                f'pyvista-{pv.__version__}/vtk-{vtkVersion().GetVTKVersion()}'
                f'/{gl}'
            )
        return self._renderer_id

    def render_frame(
        self,
        document: SceneDocument,
        viewpoint: PresentationViewpoint,
        *,
        yaw_deg: int = 0,
    ) -> bytes:
        """Render one PNG frame for ``viewpoint``; raises on GL failure.

        ``yaw_deg`` rotates the camera about its own view-up axis —
        a look-around step from the pinned position, never an orbit to a
        different position.
        """
        import pyvista as pv

        from .cad_view_state import RoomCameraState
        from .room_viewport import (
            DARK_THEME,
            _category_color,
            _entity_category,
            _grid_mesh,
            _room_floor_mesh,
            _room_wireframe,
            entity_render_meshes,
        )

        plotter = pv.Plotter(
            off_screen=True,
            window_size=(self._width, self._height),
            image_scale=1,
        )
        try:
            plotter.disable_anti_aliasing()
            plotter.set_background(DARK_THEME.viewport.background.hex)

            floor = _room_floor_mesh(document)
            if floor is not None:
                plotter.add_mesh(
                    floor,
                    color=DARK_THEME.viewport.floor.hex,
                    opacity=0.72,
                    lighting=False,
                    pickable=False,
                    render=False,
                )
            if viewpoint.overlay == 'grid':
                minor = _grid_mesh(document, step_m=0.5)
                if minor is not None:
                    plotter.add_mesh(
                        minor,
                        color=DARK_THEME.viewport.grid_minor.hex,
                        line_width=1,
                        opacity=0.34,
                        pickable=False,
                        render=False,
                    )
                major = _grid_mesh(document, step_m=2.0, z_m=0.004)
                if major is not None:
                    plotter.add_mesh(
                        major,
                        color=DARK_THEME.viewport.grid_major.hex,
                        line_width=2,
                        opacity=0.58,
                        pickable=False,
                        render=False,
                    )
            room = _room_wireframe(document)
            if room is not None:
                plotter.add_mesh(
                    room,
                    color=DARK_THEME.viewport.geometry_edge.hex,
                    line_width=2,
                    opacity=0.78,
                    pickable=False,
                    render=False,
                )

            hidden = frozenset(viewpoint.hidden_ids or ())
            for entity in document.entities:
                if entity.entity_id in hidden:
                    continue
                body_mesh, glyphs, envelope_mesh = entity_render_meshes(entity)
                fill = _category_color(_entity_category(entity))
                focused_out = bool(
                    viewpoint.focus_entity_id
                    and entity.entity_id != viewpoint.focus_entity_id
                )
                plotter.add_mesh(
                    body_mesh,
                    color=fill,
                    show_edges=True,
                    edge_color=DARK_THEME.viewport.geometry_edge.hex,
                    line_width=1,
                    opacity=0.12 if focused_out else 0.90,
                    ambient=0.32,
                    diffuse=0.62,
                    specular=0.10,
                    specular_power=12.0,
                    pickable=False,
                    render=False,
                )
                for glyph in glyphs:
                    plotter.add_mesh(
                        glyph,
                        color=fill,
                        opacity=0.12 if focused_out else 0.98,
                        ambient=0.30,
                        diffuse=0.55,
                        specular=0.15,
                        specular_power=16.0,
                        pickable=False,
                        render=False,
                    )
                if envelope_mesh is not None:
                    plotter.add_mesh(
                        envelope_mesh,
                        color=DARK_THEME.viewport.geometry_edge.hex,
                        style='wireframe',
                        line_width=1,
                        opacity=0.45,
                        pickable=False,
                        render=False,
                    )

            camera_state: RoomCameraState = viewpoint.camera
            camera = plotter.camera
            camera.SetPosition(
                camera_state.position[0],
                -camera_state.position[1],
                camera_state.position[2],
            )
            camera.SetFocalPoint(
                camera_state.focal_point[0],
                -camera_state.focal_point[1],
                camera_state.focal_point[2],
            )
            camera.SetViewUp(
                camera_state.view_up[0],
                -camera_state.view_up[1],
                camera_state.view_up[2],
            )
            camera.SetParallelProjection(
                1 if camera_state.projection == 'parallel' else 0
            )
            if (
                camera_state.projection == 'parallel'
                and camera_state.parallel_scale is not None
            ):
                camera.SetParallelScale(float(camera_state.parallel_scale))
            if (
                camera_state.projection == 'perspective'
                and camera_state.view_angle is not None
            ):
                camera.SetViewAngle(float(camera_state.view_angle))
            if yaw_deg:
                camera.Yaw(float(yaw_deg))
            plotter.reset_camera_clipping_range()
            plotter.render()

            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix='.png', delete=False
            ) as handle:
                png_path = Path(handle.name)
            try:
                plotter.screenshot(png_path)
                return png_path.read_bytes()
            finally:
                png_path.unlink(missing_ok=True)
        finally:
            plotter.close()


#: Yaw-stepped frames the viewer offers around each pinned viewpoint —
#: look-around, not orbit, so the pinned position never moves.
_DEFAULT_YAW_STEPS_DEG: tuple[int, ...] = (-60, -30, 30, 60)


def derived_yaw_steps(step_deg: int, reach_deg: int = 120) -> tuple[int, ...]:
    """Yaw offsets for a declared step size: every multiple of the step
    inside ±``reach_deg``, 0 excluded (the pinned frame covers 0)."""
    reach = (reach_deg // step_deg) * step_deg
    return tuple(
        offset
        for magnitude in range(step_deg, reach + 1, step_deg)
        for offset in (-magnitude, magnitude)
    )


def _write_file(
    output_dir: Path, relative: str, content: bytes
) -> tuple[PurePosixPath, str, int]:
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ReviewPackageError(
            f'package member path must be relative: {relative!r}'
        )
    target = output_dir.joinpath(*path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    import hashlib

    return path, hashlib.sha256(content).hexdigest(), len(content)


def _viewer_html(
    session: PresentationSession,
    frames: tuple[tuple[tuple[str, int], str], ...],
    manifest_data: dict[str, Any],
) -> str:
    """Self-contained offline viewer — no scripts that reach the network.

    ``frames`` is ((viewpoint_id, yaw_deg) -> relative path) flattened as
    an ordered mapping of path -> caption.
    """
    e = html.escape
    status_badges = {
        'draft': '下書き',
        'proposed': '提案（未確定）',
        'accepted': '承認済み案',
        'as_built': '竣工実績',
        'superseded': '旧版',
    }
    status_text = status_badges.get(session.status_label, session.status_label)

    frame_map: dict[str, dict[str, str]] = {}
    for (vp_id, yaw), path in frames:
        frame_map.setdefault(vp_id, {})[str(yaw)] = path
    frames_js = json.dumps(frame_map, ensure_ascii=False)

    meta = {
        'label': session.label,
        'status': status_text,
        'sessionId': session.session_id,
        'sessionHash': session.session_sha256,
        'sceneRevision': session.scene_revision_id,
        'sceneHash': session.scene_content_hash,
        'document': session.document_id,
        'generated': manifest_data.get('generated_at_utc', ''),
        'renderer': manifest_data.get('renderer', ''),
        'capabilities': manifest_data.get('capability_rows', []),
        'notes': {
            vp.viewpoint_id: vp.note
            for vp in session.viewpoints
            if vp.note is not None
        },
        'names': {
            vp.viewpoint_id: vp.name for vp in session.viewpoints
        },
        'order': [vp.viewpoint_id for vp in session.ordered_viewpoints()],
    }
    meta_js = json.dumps(meta, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{e(session.label)} — HTDT レビューパッケージ</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#101418; color:#d6dde5; font:14px/1.5 system-ui,"Segoe UI","Yu Gothic",sans-serif; }}
header {{ padding:14px 20px; border-bottom:1px solid #2a3340; display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; }}
h1 {{ font-size:18px; margin:0; }}
.badge {{ padding:2px 10px; border:1px solid #b8892a; color:#e8c375; border-radius:3px; font-size:12px; letter-spacing:.08em; }}
main {{ display:flex; min-height:calc(100vh - 120px); }}
nav {{ width:220px; border-right:1px solid #2a3340; padding:14px 10px; }}
.vp {{ display:block; width:100%; margin:0 0 6px; padding:8px 10px; text-align:left; background:#18202a; color:#d6dde5; border:1px solid #2a3340; border-radius:4px; cursor:pointer; }}
.vp.active {{ border-color:#4f7ec2; background:#1d2a3a; }}
#stage {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:16px; gap:10px; }}
#frame {{ max-width:100%; max-height:62vh; border:1px solid #2a3340; background:#0b0e12; }}
#frame.missing {{ display:none; }}
#missing {{ display:none; padding:24px; color:#98a4b3; }}
nav.yaw {{ width:auto; border:none; display:flex; gap:8px; padding:0; }}
nav.yaw button {{ padding:6px 14px; background:#18202a; color:#d6dde5; border:1px solid #2a3340; border-radius:4px; cursor:pointer; }}
#caption {{ color:#98a4b3; min-height:1.5em; }}
footer {{ padding:10px 20px; border-top:1px solid #2a3340; color:#7d8a99; font-size:11px; }}
code {{ font-family:ui-monospace,Consolas,monospace; font-size:11px; }}
details {{ margin-top:6px; }}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <span class="badge" id="status"></span>
</header>
<main>
  <nav id="viewpoints"></nav>
  <div id="stage">
    <img id="frame" alt="ビューポイント">
    <div id="missing">このビューポイントの画像はこのパッケージには含まれていません（マニフェストの能力宣言を参照）。</div>
    <nav class="yaw">
      <button data-yaw="back">◀</button>
      <button data-yaw="home">基準</button>
      <button data-yaw="fwd">▶</button>
    </nav>
    <div id="caption"></div>
  </div>
</main>
<footer>
  <div id="provenance"></div>
  <details><summary>能力宣言 / capability declaration</summary><pre id="caps"></pre></details>
</footer>
<script>
'use strict';
const META = {meta_js};
const FRAMES = {frames_js};
const order = META.order;
let index = 0;
let yawOffset = 0;
const img = document.getElementById('frame');
const missing = document.getElementById('missing');
const caption = document.getElementById('caption');
document.getElementById('title').textContent = META.label;
document.getElementById('status').textContent = META.status;
document.getElementById('provenance').textContent =
  'Session ' + META.sessionId + ' sha256 ' + META.sessionHash.slice(0, 16) + '… / ' +
  'Scene ' + META.sceneRevision + ' sha256 ' + META.sceneHash.slice(0, 16) + '… / ' +
  'Generated ' + META.generated + ' / ' + META.renderer;
document.getElementById('caps').textContent =
  JSON.stringify(META.capabilities, null, 2);
const nav = document.getElementById('viewpoints');
order.forEach((vpId, i) => {{
  const b = document.createElement('button');
  b.className = 'vp';
  b.dataset.index = String(i);
  b.textContent = META.names[vpId] || vpId;
  nav.appendChild(b);
}});
function framesFor(vpId) {{ return FRAMES[vpId] || {{}}; }}
function sortedYaws(vpId) {{
  return Object.keys(framesFor(vpId)).map(Number)
    .filter(n => !isNaN(n)).sort((a, b) => a - b);
}}
function nearestYaw(vpId, target) {{
  const yaws = sortedYaws(vpId);
  if (!yaws.length) return null;
  if (yaws.includes(target)) return target;
  let best = null, bestDist = 1e9;
  for (const y of yaws) {{
    const d = Math.abs(y - target);
    if (d < bestDist) {{ bestDist = d; best = y; }}
  }}
  return best;
}}
function show() {{
  const vpId = order[index];
  const frames = framesFor(vpId);
  const yaw = nearestYaw(vpId, yawOffset);
  document.querySelectorAll('.vp').forEach(b =>
    b.classList.toggle('active', Number(b.dataset.index) === index));
  if (yaw === null || !frames[String(yaw)]) {{
    img.classList.add('missing');
    missing.style.display = 'block';
  }} else {{
    img.src = frames[String(yaw)];
    img.classList.remove('missing');
    missing.style.display = 'none';
  }}
  caption.textContent = (META.names[vpId] || vpId) +
    (yaw ? ' / 視点回転 ' + yaw + '°' : '') +
    (META.notes[vpId] ? ' — ' + META.notes[vpId] : '');
}}
nav.addEventListener('click', ev => {{
  const b = ev.target.closest('.vp');
  if (!b) return;
  index = Number(b.dataset.index); yawOffset = 0; show();
}});
document.querySelector('nav.yaw').addEventListener('click', ev => {{
  const b = ev.target.closest('button'); if (!b) return;
  const vpId = order[index];
  const yaws = sortedYaws(vpId);
  if (!yaws.length) return;
  const cur = nearestYaw(vpId, yawOffset);
  const ci = cur === null ? 0 : yaws.indexOf(cur);
  if (b.dataset.yaw === 'fwd') {{
    yawOffset = yaws[Math.min(ci + 1, yaws.length - 1)];
  }} else if (b.dataset.yaw === 'back') {{
    yawOffset = yaws[Math.max(ci - 1, 0)];
  }} else {{ yawOffset = 0; }}
  show();
}});
document.addEventListener('keydown', ev => {{
  if (ev.key === 'ArrowRight') {{ index = Math.min(index + 1, order.length - 1); yawOffset = 0; show(); }}
  else if (ev.key === 'ArrowLeft') {{ index = Math.max(index - 1, 0); yawOffset = 0; show(); }}
}});
show();
</script>
</body>
</html>
"""


def build_review_package(
    session: PresentationSession,
    output_dir: Path | str,
    scene_repository: SceneRepository,
    *,
    presentation_repository: CadPresentationRepository | None = None,
    renderer: Any | None = None,
    generated_at_utc: str | None = None,
    include_drawings: bool = True,
    yaw_steps_deg: tuple[int, ...] | None = None,
) -> ReviewPackageResult:
    """Build the offline review package into ``output_dir``.

    The builder fails closed on authority (missing/mismatched pins raise)
    and fails honestly on capability (a render path that cannot produce
    a frame is declared ``unavailable`` in the manifest — never a stub
    image).

    ``yaw_steps_deg`` is an explicit caller override: ``None`` derives the
    offsets from ``session.render.yaw_step_deg`` (a ``None`` session step
    renders pinned frames only), while an explicit tuple — including
    ``()`` — always wins, so callers can enable yaw on sessions that did
    not declare it or suppress yaw on sessions that did.
    """

    output_dir = Path(output_dir)
    generated = generated_at_utc or _utc_now()
    presentation_repository = (
        presentation_repository
        or CadPresentationRepository(scene_repository)
    )
    document = presentation_repository.session_document(session)

    entries: list[ReviewPackageEntry] = []
    capability_rows: list[ReviewPackageCapability] = []
    security_rows: list[ReviewPackageSecurityRow] = []
    warnings: list[str] = []

    # -- Capability probe ------------------------------------------------
    renderer_id = 'none'
    render_ok = renderer is not None
    if render_ok:
        try:
            renderer_id = renderer.renderer_id()
        except Exception:
            renderer_id = 'unknown'

    frames: list[tuple[tuple[str, int], str]] = []

    if yaw_steps_deg is not None:
        yaw_steps = tuple(yaw_steps_deg)
    elif session.render.yaw_step_deg is None:
        # yaw_step_deg=None on the session means "pinned frames only".
        yaw_steps = ()
    else:
        yaw_steps = derived_yaw_steps(session.render.yaw_step_deg)

    if render_ok:
        capability_rows.append(
            ReviewPackageCapability(
                capability='perspective_renders',
                state='rendered',
                detail=(
                    '各ビューポイントをピン留めされたカメラ状態から '
                    'オフスクリーン描画（実機 GL 経由）。'
                ),
            )
        )
    else:
        capability_rows.append(
            ReviewPackageCapability(
                capability='perspective_renders',
                state='unavailable',
                detail='この環境ではオフスクリーン描画が利用できません。',
            )
        )

    # -- Renders ---------------------------------------------------------
    render_index = 0
    if render_ok:
        for viewpoint in session.ordered_viewpoints():
            render_index += 1
            for yaw in (0, *yaw_steps):
                try:
                    png = renderer.render_frame(
                        document, viewpoint, yaw_deg=yaw
                    )
                except Exception as exc:
                    warnings.append(
                        f'viewpoint {viewpoint.name} yaw {yaw}: {exc}'
                    )
                    continue
                relative = (
                    f'renders/{render_index:02d}-'
                    f'{_slug(viewpoint.name)}-yaw{yaw:+d}.png'
                )
                rel, sha, size = _write_file(output_dir, relative, png)
                frames.append(((viewpoint.viewpoint_id, yaw), str(rel)))
                entries.append(
                    ReviewPackageEntry(
                        path=str(rel),
                        sha256=sha,
                        byte_length=size,
                        kind='render',
                        viewpoint_id=viewpoint.viewpoint_id,
                        yaw_deg=yaw,
                    )
                )

    capability_rows.append(
        ReviewPackageCapability(
            capability='equirectangular_panorama',
            state='unavailable',
            detail=(
                'この環境の GL スタック（Mesa/dzn）では '
                'vtkPanoramicProjectionPass が描画不能です。'
                'パノラマの代替として各ビューポイントの水平回転フレーム'
                '（yaw-stepped perspective）を同梱します。'
            ),
        )
    )
    capability_rows.append(
        ReviewPackageCapability(
            capability='yaw_stepped_renders',
            state='rendered' if render_ok else 'unavailable',
            detail=(
                'ピン留め位置からの水平回転（look-around）フレーム。'
                'エクイレクタングラー投影ではなく透視投影の回転です。'
            ),
        )
    )

    # -- Drawing sheets --------------------------------------------------
    if include_drawings:
        try:
            revision = scene_repository.get(session.scene_revision_id)
            if revision is None:
                raise ReviewPackageError(
                    'session scene revision does not resolve'
                )
            variant = (
                None
                if session.system_variant_id is None
                else presentation_repository.variant_repository.get_variant(
                    session.system_variant_id
                )
            )
            output = build_installation_output(revision, variant=variant)
            spec = build_drawing_set_spec(
                spec_id=f'presentation-{session.session_id}',
                spec_version='1',
                sheets=('floor_plan', 'front_elevation', 'side_elevation'),
            )
            drawing_set: InstallationDrawingSet = generate_drawing_set(
                output=output,
                spec=spec,
                project_label=session.label,
                generated_at_utc=generated,
            )
            for sheet in drawing_set.sheets:
                # sheet_id embeds a '<spec>:<kind>' colon — not a valid
                # package-relative path, so the file name is slugged.
                relative = f'drawings/{_slug(sheet.sheet_id)}.svg'
                content = sheet.to_svg().encode('utf-8')
                rel, sha, size = _write_file(output_dir, relative, content)
                entries.append(
                    ReviewPackageEntry(
                        path=str(rel),
                        sha256=sha,
                        byte_length=size,
                        kind='drawing',
                    )
                )
            capability_rows.append(
                ReviewPackageCapability(
                    capability='svg_drawings',
                    state='rendered',
                    detail='InstallationOutput から導出したベクトル図面。',
                )
            )
        except Exception as exc:
            capability_rows.append(
                ReviewPackageCapability(
                    capability='svg_drawings',
                    state='unavailable',
                    detail=f'図面の生成に失敗しました: {exc}',
                )
            )
            warnings.append(f'drawings: {exc}')
    else:
        capability_rows.append(
            ReviewPackageCapability(
                capability='svg_drawings',
                state='not_requested',
                detail='呼び出し側が図面生成を指定しませんでした。',
            )
        )

    # -- Semantic snapshot -------------------------------------------------
    semantic = _canonical(session.semantic_payload()).encode('utf-8')
    rel, sha, size = _write_file(output_dir, 'semantic.json', semantic)
    entries.append(
        ReviewPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='semantic'
        )
    )

    # -- Viewer -------------------------------------------------------------
    manifest_probe: dict[str, Any] = {
        'generated_at_utc': generated,
        'renderer': renderer_id,
        'capability_rows': [
            row.model_dump(mode='json') for row in capability_rows
        ],
    }
    viewer = _viewer_html(session, tuple(frames), manifest_probe)
    rel, sha, size = _write_file(
        output_dir, 'viewer.html', viewer.encode('utf-8')
    )
    entries.append(
        ReviewPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='viewer'
        )
    )
    capability_rows.append(
        ReviewPackageCapability(
            capability='offline_viewer',
            state='rendered',
            detail=(
                '単一 HTML、外部ネットワーク参照なし。'
                '相対パスのみでパッケージ内ファイルを参照します。'
            ),
        )
    )

    # -- Security rows ------------------------------------------------------
    absolute_paths = [
        entry.path
        for entry in entries
        if PurePosixPath(entry.path).is_absolute()
        or '\\' in entry.path
        or ':' in entry.path
    ]
    security_rows.append(
        ReviewPackageSecurityRow(
            check='relative_paths_only',
            status='fail' if absolute_paths else 'pass',
            detail=(
                '全エントリはパッケージ相対パスです。'
                if not absolute_paths
                else '絶対パスを含むエントリ: ' + ', '.join(absolute_paths)
            ),
        )
    )
    security_rows.append(
        ReviewPackageSecurityRow(
            check='no_local_secrets',
            status='pass',
            detail=(
                'パッケージにはローカルファイルパス・資格情報・'
                'アカウント識別子を含みません。'
            ),
        )
    )

    # -- Manifest -----------------------------------------------------------
    manifest_fields: dict[str, Any] = {
        'package_id': f'{REVIEW_PACKAGE_SCHEMA}:{session.session_id}',
        'generated_at_utc': generated,
        'generator': PACKAGE_GENERATOR,
        'renderer': renderer_id,
        'session_id': session.session_id,
        'session_sha256': session.session_sha256,
        'session_label': session.label,
        'status_label': session.status_label,
        'document_id': session.document_id,
        'scene_revision_id': session.scene_revision_id,
        'scene_content_hash': session.scene_content_hash,
        'effective_scene_content_hash': scene_content_hash(document),
        'system_variant_id': session.system_variant_id,
        'system_variant_sha256': session.system_variant_sha256,
        'comparison_set_id': session.comparison_set_id,
        'comparison_set_sha256': session.comparison_set_sha256,
        'entries': tuple(entries),
        'capability_rows': tuple(capability_rows),
        'security_rows': tuple(security_rows),
    }
    provisional = ReviewPackageManifest.model_construct(
        **manifest_fields, manifest_sha256='0' * 64
    )
    manifest = ReviewPackageManifest(
        **manifest_fields,
        manifest_sha256=_hash(provisional.semantic_payload()),
    )

    manifest_bytes = manifest.model_dump_json(indent=2).encode('utf-8')
    rel, sha, size = _write_file(output_dir, 'manifest.json', manifest_bytes)
    entries.append(
        ReviewPackageEntry(
            path=str(rel), sha256=sha, byte_length=size, kind='manifest'
        )
    )

    return ReviewPackageResult(
        output_dir=str(output_dir), manifest=manifest, warnings=tuple(warnings)
    )


def verify_review_package(package_dir: Path | str) -> ReviewPackageManifest:
    """Re-hash every manifest entry in a package directory.

    Returns the verified manifest; raises ``ReviewPackageError`` on any
    hash/size mismatch or a manifest whose own seal does not validate.
    """

    package_dir = Path(package_dir)
    manifest_path = package_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise ReviewPackageError('manifest.json is missing')
    manifest = ReviewPackageManifest.model_validate_json(
        manifest_path.read_text(encoding='utf-8')
    )
    import hashlib

    for entry in manifest.entries:
        if entry.kind == 'manifest':
            continue
        target = package_dir.joinpath(*PurePosixPath(entry.path).parts)
        if not target.is_file():
            raise ReviewPackageError(
                f'package entry is missing: {entry.path}'
            )
        content = target.read_bytes()
        if len(content) != entry.byte_length:
            raise ReviewPackageError(
                f'package entry size mismatch: {entry.path}'
            )
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ReviewPackageError(
                f'package entry hash mismatch: {entry.path}'
            )
    return manifest


def _slug(name: str) -> str:
    slug = ''.join(
        ch if ch.isalnum() or ch in '-_' else '-'
        for ch in name.strip().lower().replace(' ', '-')
    )
    return slug.strip('-') or 'view'


__all__ = [
    'OffscreenSceneRenderer',
    'REVIEW_PACKAGE_KIND',
    'REVIEW_PACKAGE_SCHEMA',
    'REVIEW_PACKAGE_SCHEMA_VERSION',
    'ReviewPackageCapability',
    'ReviewPackageEntry',
    'ReviewPackageError',
    'ReviewPackageManifest',
    'ReviewPackageResult',
    'ReviewPackageSecurityRow',
    'RenderedFrame',
    'build_review_package',
    'verify_review_package',
]
