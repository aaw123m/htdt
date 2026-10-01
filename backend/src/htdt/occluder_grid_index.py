"""Uniform-grid spatial index over occluder triangle rows.

Shared by the geometric-acoustics occlusion paths
(``cad_geometric_acoustics_adapter``, ``cad_geometric_acoustics_portal``,
``cad_late_field_energy``): triangle rows are partitioned into uniform
cells over their shared bounding box and segment queries walk only the
cells the segment traverses (3D DDA). The grid narrows candidates — a
strict superset of the rows an exact segment–triangle or point–triangle
test can hit — so verdicts computed over the candidates are identical to
a linear scan of the same rows.

Row shape is ``(..., vertex_a, vertex_b, vertex_c)``: the last three
items of each row are the triangle vertices, so keyed rows like
``(surface_id, va, vb, vc)`` and ``(triangle_index, va, vb, vc)`` and
bare ``(va, vb, vc)`` rows all index the same way.

Candidate-superset bound: the exact Möller–Trumbore tests on this path
cap their barycentric tolerance at ``0.25`` (``min(0.25, tol / scale)``;
unscaled callers only ever pass geometric epsilons far below that cap).
A hit point can therefore lie at most ``0.25 * (|e1| + |e2|)`` outside a
triangle's axis-aligned bounding box along any axis. Each row is
inserted into every cell overlapped by its bounding box inflated by
that bound, and the grid itself is padded by the largest row bound, so
every cell reachable by a hit point contains the row in its bucket.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from math import floor, sqrt
from typing import Sequence

_BARYCENTRIC_TOLERANCE_CAP = 0.25


class _UniformOccluderGrid:
    """Uniform-grid spatial index over occluder triangle rows.

    Rows' last three items are triangle vertices. Each row is stored in
    every cell overlapped by its bounding box inflated by
    ``0.25 * (|e1| + |e2|)`` — the maximum distance an exact hit point
    can lie outside the box under the capped barycentric tolerances the
    occlusion path uses — so ``candidates`` and ``point_candidates``
    return a strict superset of the rows an exact test can hit.
    """

    def __init__(self, rows: Sequence[tuple]) -> None:
        self._rows = rows
        minimum = [float('inf')] * 3
        maximum = [float('-inf')] * 3
        row_bounds: list[tuple[tuple[float, float, float], tuple[float, float, float], float]] = []
        max_pad = 0.0
        for row in rows:
            vertex_a, vertex_b, vertex_c = row[-3:]
            edge1 = tuple(
                float(vertex_b[axis]) - float(vertex_a[axis]) for axis in range(3)
            )
            edge2 = tuple(
                float(vertex_c[axis]) - float(vertex_a[axis]) for axis in range(3)
            )
            pad = _BARYCENTRIC_TOLERANCE_CAP * (
                sqrt(sum(component * component for component in edge1))
                + sqrt(sum(component * component for component in edge2))
            )
            low = (
                min(vertex_a[0], vertex_b[0], vertex_c[0]),
                min(vertex_a[1], vertex_b[1], vertex_c[1]),
                min(vertex_a[2], vertex_b[2], vertex_c[2]),
            )
            high = (
                max(vertex_a[0], vertex_b[0], vertex_c[0]),
                max(vertex_a[1], vertex_b[1], vertex_c[1]),
                max(vertex_a[2], vertex_b[2], vertex_c[2]),
            )
            row_bounds.append((low, high, pad))
            if pad > max_pad:
                max_pad = pad
            for vertex in row[-3:]:
                for axis in range(3):
                    coordinate = float(vertex[axis])
                    if coordinate < minimum[axis]:
                        minimum[axis] = coordinate
                    if coordinate > maximum[axis]:
                        maximum[axis] = coordinate
        # Prepared per-row geometry for the exact intersection tests: the
        # edge/normal quantities Möller–Trumbore and point-in-triangle derive
        # per row, computed once here with the identical float operations so
        # every later verdict is bit-identical to recomputing per query.
        prepared: list[tuple] = []
        for row in rows:
            vertex_a, vertex_b, vertex_c = row[-3:]
            edge1 = (
                float(vertex_b[0]) - float(vertex_a[0]),
                float(vertex_b[1]) - float(vertex_a[1]),
                float(vertex_b[2]) - float(vertex_a[2]),
            )
            edge2 = (
                float(vertex_c[0]) - float(vertex_a[0]),
                float(vertex_c[1]) - float(vertex_a[1]),
                float(vertex_c[2]) - float(vertex_a[2]),
            )
            edge1_sq = sum(component * component for component in edge1)
            edge2_sq = sum(component * component for component in edge2)
            normal = (
                edge1[1] * edge2[2] - edge1[2] * edge2[1],
                edge1[2] * edge2[0] - edge1[0] * edge2[2],
                edge1[0] * edge2[1] - edge1[1] * edge2[0],
            )
            normal_sq = sum(component * component for component in normal)
            prepared.append(
                (
                    edge1,
                    edge2,
                    sqrt(edge1_sq),
                    sqrt(edge2_sq),
                    edge1_sq,
                    edge2_sq,
                    normal,
                    sqrt(normal_sq),
                )
            )
        self._prepared = tuple(prepared)
        diagonal = sqrt(
            sum((maximum[axis] - minimum[axis]) ** 2 for axis in range(3))
        )
        padding = max(diagonal * 1.0e-9, max_pad, 1.0e-12)
        self._origin = tuple(
            minimum[axis] - padding for axis in range(3)
        )
        extent = tuple(
            maximum[axis] - minimum[axis] + 2.0 * padding
            for axis in range(3)
        )
        volume = extent[0] * extent[1] * extent[2]
        cell_volume = volume / max(len(rows) * 4.0, 1.0)
        cell_size = max(cell_volume ** (1.0 / 3.0), padding)
        self._cell_size = cell_size
        self._dims = tuple(
            min(64, max(1, floor(extent[axis] / cell_size) + 1))
            for axis in range(3)
        )
        cells: dict[int, list[int]] = {}
        for row_index, (low, high, pad) in enumerate(row_bounds):
            low_cell = self._cell_index(
                tuple(low[axis] - pad for axis in range(3))
            )
            high_cell = self._cell_index(
                tuple(high[axis] + pad for axis in range(3))
            )
            for ix in range(low_cell[0], high_cell[0] + 1):
                for iy in range(low_cell[1], high_cell[1] + 1):
                    for iz in range(low_cell[2], high_cell[2] + 1):
                        cells.setdefault(
                            ix + self._dims[0] * (iy + self._dims[1] * iz),
                            [],
                        ).append(row_index)
        self._cells = cells

    def _cell_index(
        self,
        point: Sequence[float],
    ) -> tuple[int, int, int]:
        return tuple(
            min(
                self._dims[axis] - 1,
                max(
                    0,
                    floor(
                        (float(point[axis]) - self._origin[axis])
                        / self._cell_size
                    ),
                ),
            )
            for axis in range(3)
        )

    def _segment_row_indices(
        self,
        start: Sequence[float],
        end: Sequence[float],
    ) -> Iterator[int]:
        if not self._rows:
            return
        origin = self._origin
        cell_size = self._cell_size
        grid_maximum = tuple(
            origin[axis] + self._dims[axis] * cell_size for axis in range(3)
        )
        direction = tuple(
            float(end[axis]) - float(start[axis]) for axis in range(3)
        )
        t_enter = 0.0
        t_exit = 1.0
        for axis in range(3):
            step = direction[axis]
            if step == 0.0:
                if (
                    float(start[axis]) < origin[axis]
                    or float(start[axis]) > grid_maximum[axis]
                ):
                    return
                continue
            inverse = 1.0 / step
            near = (origin[axis] - float(start[axis])) * inverse
            far = (grid_maximum[axis] - float(start[axis])) * inverse
            if near > far:
                near, far = far, near
            t_enter = max(t_enter, near)
            t_exit = min(t_exit, far)
            if t_enter > t_exit:
                return
        cell = list(
            self._cell_index(
                tuple(
                    float(start[axis]) + t_enter * direction[axis]
                    for axis in range(3)
                )
            )
        )
        step_sign: list[int] = []
        t_delta: list[float] = []
        t_next: list[float] = []
        for axis in range(3):
            step = direction[axis]
            if step == 0.0:
                step_sign.append(0)
                t_delta.append(float('inf'))
                t_next.append(float('inf'))
                continue
            if step > 0.0:
                step_sign.append(1)
                boundary = origin[axis] + (cell[axis] + 1) * cell_size
            else:
                step_sign.append(-1)
                boundary = origin[axis] + cell[axis] * cell_size
            t_delta.append(abs(cell_size / step))
            t_next.append((boundary - float(start[axis])) / step)
        seen: set[int] = set()
        dim_x, dim_y = self._dims[0], self._dims[1]
        cells = self._cells
        while True:
            bucket = cells.get(
                cell[0] + dim_x * (cell[1] + dim_y * cell[2])
            )
            if bucket is not None:
                for row_index in bucket:
                    if row_index not in seen:
                        seen.add(row_index)
                        yield row_index
            if t_next[0] <= t_next[1] and t_next[0] <= t_next[2]:
                axis = 0
            elif t_next[1] <= t_next[2]:
                axis = 1
            else:
                axis = 2
            if t_next[axis] > t_exit:
                return
            cell[axis] += step_sign[axis]
            t_next[axis] += t_delta[axis]

    def _point_row_indices(
        self,
        point: Sequence[float],
        radius: float,
    ) -> Iterator[int]:
        """Yield row indices stored in cells overlapped by ``point ± radius``.

        A point–triangle hit at geometric tolerance ``radius`` keeps the
        point within ``pad_i + radius`` of the row's bounding box, so the
        row's inflated box intersects the query box in a shared cell.
        """
        if not self._rows:
            return
        low = self._cell_index(
            tuple(float(point[axis]) - radius for axis in range(3))
        )
        high = self._cell_index(
            tuple(float(point[axis]) + radius for axis in range(3))
        )
        seen: set[int] = set()
        dim_x, dim_y = self._dims[0], self._dims[1]
        cells = self._cells
        for ix in range(low[0], high[0] + 1):
            for iy in range(low[1], high[1] + 1):
                for iz in range(low[2], high[2] + 1):
                    bucket = cells.get(ix + dim_x * (iy + dim_y * iz))
                    if bucket is not None:
                        for row_index in bucket:
                            if row_index not in seen:
                                seen.add(row_index)
                                yield row_index

    def candidates(
        self,
        start: Sequence[float],
        end: Sequence[float],
    ) -> Iterator[tuple]:
        rows = self._rows
        for row_index in self._segment_row_indices(start, end):
            yield rows[row_index]

    def point_candidates(
        self,
        point: Sequence[float],
        radius: float,
    ) -> Iterator[tuple]:
        rows = self._rows
        for row_index in self._point_row_indices(point, radius):
            yield rows[row_index]

    def prepared_candidates(
        self,
        start: Sequence[float],
        end: Sequence[float],
    ) -> Iterator[tuple[tuple, tuple]]:
        """Yield ``(row, prepared)`` pairs for the segment's candidate cells.

        ``prepared`` is
        ``(edge1, edge2, edge1_length, edge2_length, edge1_sq, edge2_sq,
        normal, normal_length)`` — the per-row quantities the exact
        intersection tests derive, computed once at index build with the
        identical float operations (verdicts are bit-identical to deriving
        them per query).
        """
        rows = self._rows
        prepared = self._prepared
        for row_index in self._segment_row_indices(start, end):
            yield rows[row_index], prepared[row_index]

    def prepared_point_candidates(
        self,
        point: Sequence[float],
        radius: float,
    ) -> Iterator[tuple[tuple, tuple]]:
        """``prepared_candidates`` for the point query cells."""
        rows = self._rows
        prepared = self._prepared
        for row_index in self._point_row_indices(point, radius):
            yield rows[row_index], prepared[row_index]


class _IndexedOccluderRows(tuple):
    """Occluder row tuple carrying a lazily-built uniform-grid index.

    Behaves exactly like the plain row tuple for iteration and unpacking;
    the grid only narrows candidates for segment and point queries — the
    exact intersection tests still decide each verdict, so verdicts are
    identical to scanning the same rows linearly.
    """

    def __new__(cls, rows: Iterable[tuple]) -> '_IndexedOccluderRows':
        instance = super().__new__(cls, tuple(rows))
        instance._uniform_grid = None
        return instance

    def _grid(self) -> _UniformOccluderGrid:
        grid = self._uniform_grid
        if grid is None:
            grid = _UniformOccluderGrid(self)
            self._uniform_grid = grid
        return grid

    def _segment_candidates(
        self,
        start: Sequence[float],
        end: Sequence[float],
    ) -> Iterator[tuple]:
        return self._grid().candidates(start, end)

    def _point_candidates(
        self,
        point: Sequence[float],
        radius: float,
    ) -> Iterator[tuple]:
        return self._grid().point_candidates(point, radius)

    def _segment_prepared_candidates(
        self,
        start: Sequence[float],
        end: Sequence[float],
    ) -> Iterator[tuple[tuple, tuple]]:
        return self._grid().prepared_candidates(start, end)

    def _point_prepared_candidates(
        self,
        point: Sequence[float],
        radius: float,
    ) -> Iterator[tuple[tuple, tuple]]:
        return self._grid().prepared_point_candidates(point, radius)
