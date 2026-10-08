"""Room-viewport field overlay controller (#999 / REV73).

Holds the armed 3D-field display request and resolves it against LIVE
authority on every frame: the session is re-fetched and re-checked against
the document's current head each resolve, so a scene edit or a project
switch makes a stale overlay disappear instead of painting an old field
over the new geometry.

No Qt imports here — the workspace owns the widgets; this controller owns
request state + request generation so a late callback cannot resurrect a
superseded display.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_field_explorer import FieldExplorerSession, explorer_probe
from .cad_field_explorer_repository import CadFieldExplorerRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_spatial_field import FieldQuantity
from .field_volume_visual_adapter import (
    FieldDisplayBlocked,
    FieldDisplayView,
    FieldOverlayScene,
    FieldSliceItem,
    build_field_display_view,
    build_slice_item,
    iso_values_for,
    nearest_slice_index,
)


@dataclass(frozen=True, slots=True)
class FieldOverlay3DRequest:
    """What the user asked the 3D overlay to show (panel-agnostic)."""

    session_id: str
    quantity: FieldQuantity
    axis_plane: str  # 'xy' | 'xz' | 'yz'
    coordinate_m: float
    iso_enabled: bool = False
    iso_fraction: float = 0.5
    volume_enabled: bool = False
    probe_enabled: bool = False


@dataclass(frozen=True, slots=True)
class FieldOverlayResolution:
    """Outcome of resolving the armed request for one frame."""

    scene: FieldOverlayScene | None
    blocked_reason: str | None
    probe_readout: str | None


_PHASE_MASK_MIN_PA = 1.0e-9


class RoomFieldOverlayController:
    """Armed-request state + resolve() for the acoustics render pass."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.field_repository = CadFieldExplorerRepository(scene_repository)
        self._request: FieldOverlay3DRequest | None = None
        self._probe_value = None
        self._view_cache_key: tuple | None = None
        self._view_cache: FieldDisplayView | None = None
        self.generation = 0

    @property
    def armed(self) -> bool:
        return self._request is not None

    @property
    def probe_armed(self) -> bool:
        return self._request is not None and self._request.probe_enabled

    @property
    def request(self) -> FieldOverlay3DRequest | None:
        return self._request

    def set_request(self, request: FieldOverlay3DRequest) -> None:
        if self._request != request:
            self._request = request
            self.generation += 1
            self._probe_value = None

    def clear(self) -> None:
        if self._request is not None or self._probe_value is not None:
            self.generation += 1
        self._request = None
        self._probe_value = None

    def disarm_probe(self) -> None:
        if self._request is not None and self._request.probe_enabled:
            self._request = FieldOverlay3DRequest(
                session_id=self._request.session_id,
                quantity=self._request.quantity,
                axis_plane=self._request.axis_plane,
                coordinate_m=self._request.coordinate_m,
                iso_enabled=self._request.iso_enabled,
                iso_fraction=self._request.iso_fraction,
                volume_enabled=self._request.volume_enabled,
                probe_enabled=False,
            )
            self.generation += 1

    def _session(self) -> FieldExplorerSession | None:
        if self._request is None:
            return None
        return self.field_repository.get(self._request.session_id)

    def _view(self, session: FieldExplorerSession) -> FieldDisplayView:
        head = self.scene_repository.current_head(session.document_id)
        key = (
            session.semantic_sha256,
            self._request.quantity,
            None if head is None else head.revision_id,
        )
        if key != self._view_cache_key:
            self._view_cache = build_field_display_view(
                session,
                quantity=self._request.quantity,
                head=head,
                phase_mask_min_magnitude_pa=_PHASE_MASK_MIN_PA,
            )
            self._view_cache_key = key
        return self._view_cache

    def resolve(self) -> FieldOverlayResolution:
        """Current display scene, or a fail-closed block reason."""

        request = self._request
        if request is None:
            return FieldOverlayResolution(None, None, None)
        session = self._session()
        if session is None:
            return FieldOverlayResolution(
                None, '音場セッションが見つかりません', None
            )
        try:
            view = self._view(session)
        except FieldDisplayBlocked as exc:
            return FieldOverlayResolution(
                None, f'3D音場を表示できません · {"; ".join(exc.reasons)}', None
            )
        index = nearest_slice_index(view, request.axis_plane, request.coordinate_m)
        slices = (build_slice_item(view, request.axis_plane, index),)
        iso_values: tuple[float, ...] = ()
        iso_note = ''
        if request.iso_enabled:
            try:
                iso_values = iso_values_for(view, request.iso_fraction)
            except FieldDisplayBlocked as exc:
                iso_note = f'iso: none ({"; ".join(exc.reasons)})'
        probe = self._probe_value
        probe_readout = None
        if probe is not None:
            probe_readout = (
                f'プローブ {probe.value:.4g} {probe.unit} '
                f'({probe.sampled_position.x_m:.3f}, '
                f'{probe.sampled_position.y_m:.3f}, '
                f'{probe.sampled_position.z_m:.3f}) m · {probe.sample_state}'
            )
        # The render-space plane of the single display slice — the probe's
        # fallback surface when no pickable actor is under the cursor.
        fixed_axis = {'xy': 2, 'xz': 1, 'yz': 0}[request.axis_plane]
        probe_plane = (
            (fixed_axis, slices[0].render_origin[fixed_axis])
            if request.probe_enabled
            else None
        )
        # ASCII-only: VTK viewport text drops CJK glyphs entirely, so the
        # honesty notice must be readable in ASCII (Qt labels keep Japanese).
        status = (
            f'{view.producer} | mode '
            f'({view.mode_indices[0]},{view.mode_indices[1]},{view.mode_indices[2]}) '
            f'| {view.frequency_hz:.1f} Hz',
            f'{view.quantity} [{view.unit}] | '
            + (
                'NORMALIZED DISPLAY (not absolute SPL)'
                if not view.absolute_pressure_reference
                else 'absolute pressure reference'
            )
            + f' | grid {view.dims[0]}x{view.dims[1]}x{view.dims[2]}'
            + (
                f' (decimate x{view.display_stride})'
                if view.sample_state == 'decimated'
                else ''
            )
            + (f' | masked {view.masked_count}' if view.masked_count else ''),
            f'run {view.prediction_run_id[:8]} | '
            f'rev {view.scene_revision_id[:8]} | CURRENT',
        ) + ((iso_note,) if iso_note else ())
        return FieldOverlayResolution(
            FieldOverlayScene(
                view=view,
                slices=slices,
                iso_values=iso_values,
                volume_enabled=request.volume_enabled,
                probe=probe,
                probe_plane=probe_plane,
                status_lines=status,
            ),
            None,
            probe_readout,
        )

    def probe_world(
        self, world: tuple[float, float, float]
    ) -> str:
        """Probe at a render-space world position; returns a readout string."""

        request = self._request
        if request is None:
            return ''
        session = self._session()
        if session is None:
            return '音場セッションが見つかりません'
        domain = Position3(x_m=world[0], y_m=-world[1], z_m=world[2])
        try:
            value = explorer_probe(
                session,
                position=domain,
                quantity=request.quantity,
                interpolation='exact_samples',
            )
        except ValueError:
            return 'プローブ位置は音場グリッドの範囲外です'
        self._probe_value = value
        self.generation += 1
        return (
            f'{value.value:.4g} {value.unit} · '
            f'({value.sampled_position.x_m:.3f}, '
            f'{value.sampled_position.y_m:.3f}, '
            f'{value.sampled_position.z_m:.3f}) m · {value.sample_state}'
        )
