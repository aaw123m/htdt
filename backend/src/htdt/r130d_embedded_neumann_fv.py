"""Experimental conservative embedded-boundary rigid-wall acoustic operator.

Only the R130D six-faced sloped prism is currently supported:
  0 <= x <= L, 0 <= y <= L, 0 <= z <= H - slope*y.
This intentionally does not modify the pinned PFFDTD production adapter.

A cell-centred finite-volume discretisation uses *exact* yz cut-polygon
areas for cell volumes and *exact* open face areas for intercell fluxes.
The non-axis-aligned sloped rigid roof has zero normal flux by construction:
no staircase surface-face flux is invented. The resulting generalized
semidiscrete wave system is

    M phi_tt + K phi = F,
    M=diag(fluid volume), K=c^2 sum_faces (open area/h)(e_i-e_j)(e_i-e_j)^T.

K is symmetric positive semidefinite, conserves the constant mode, and
yields exact discrete energy conservation under implicit midpoint for F=0.
Small sliver-cell masses can be stiff; implicit midpoint avoids the
explicit cut-cell CFL restriction, though it does not fix spatial accuracy.

This is a bounded, independent solver *prototype*, not a production
replacement or an approved cross-solver reference for issue #938.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import factorized


@dataclass(frozen=True)
class SlopedPrism:
    length_m: float = 4.0
    roof_height_at_y0_m: float = 4.0
    roof_drop_per_y_m: float = 0.25
    sound_speed_m_s: float = 343.2

    def __post_init__(self) -> None:
        vals = (self.length_m, self.roof_height_at_y0_m,
                self.roof_drop_per_y_m, self.sound_speed_m_s)
        if not all(math.isfinite(float(v)) for v in vals):
            raise ValueError("geometry parameters must be finite")
        if not (self.length_m > 0 and self.sound_speed_m_s > 0
                and self.roof_drop_per_y_m >= 0
                and self.roof_height_at_y0_m > self.roof_drop_per_y_m * self.length_m
                and self.roof_height_at_y0_m <= self.length_m):
            raise ValueError("unsupported or degenerate sloped prism")


@dataclass(frozen=True)
class EmbeddedNeumannSystem:
    geometry: SlopedPrism
    cells_per_axis: int
    grid_spacing_m: float
    cell_coordinates_ijk: np.ndarray
    cell_center_xyz_m: np.ndarray
    cell_volumes_m3: np.ndarray
    fluid_face_areas_m2: np.ndarray
    mass: sparse.csr_matrix
    stiffness: sparse.csr_matrix

    @property
    def degrees_of_freedom(self) -> int:
        return len(self.cell_volumes_m3)

    def energy(self, potential: np.ndarray, velocity: np.ndarray) -> float:
        p = np.asarray(potential, dtype=np.float64)
        v = np.asarray(velocity, dtype=np.float64)
        if p.shape != (self.degrees_of_freedom,) or v.shape != p.shape:
            raise ValueError("state shapes mismatch")
        return 0.5 * float(v @ (self.mass @ v) + p @ (self.stiffness @ p))

    def midpoint_integrator(self, dt_s: float) -> "MidpointIntegrator":
        return MidpointIntegrator(self, dt_s)


def _clip_roof(
    polygon: list[tuple[float, float]],
    *,
    roof0: float,
    slope: float,
) -> list[tuple[float, float]]:
    """Clip yz polygon against roof0-slope*y-z >= 0."""
    def signed(pt: tuple[float, float]) -> float:
        return roof0 - slope * pt[0] - pt[1]

    output = []
    for i, b in enumerate(polygon):
        a = polygon[i - 1]
        fa, fb = signed(a), signed(b)
        ia, ib = fa >= 0.0, fb >= 0.0
        if ia != ib:
            t = fa / (fa - fb)
            output.append((a[0] + t * (b[0] - a[0]),
                           a[1] + t * (b[1] - a[1])))
        if ib:
            output.append(b)
    return output


def _polygon_area(p: list[tuple[float, float]]) -> float:
    if len(p) < 3:
        return 0.0
    # Translate to a corner before taking the shoelace sum: avoids
    # subtracting large absolute coordinates for narrow cut cells.
    y0, z0 = p[0]
    return 0.5 * abs(sum(
        (p[i][0] - y0) * (p[i + 1][1] - z0)
        - (p[i + 1][0] - y0) * (p[i][1] - z0)
        for i in range(1, len(p) - 1)
    ))


def _fluid_section_area(
    yl: float, yr: float, zl: float, zr: float,
    geometry: SlopedPrism,
) -> float:
    clipped = _clip_roof(
        [(yl, zl), (yr, zl), (yr, zr), (yl, zr)],
        roof0=geometry.roof_height_at_y0_m,
        slope=geometry.roof_drop_per_y_m,
    )
    return _polygon_area(clipped)


def build_sloped_embedded_neumann(
    cells_per_axis: int,
    *,
    geometry: SlopedPrism | None = None,
    max_cells: int = 100_000,
) -> EmbeddedNeumannSystem:
    """Construct exact cut volumes/face apertures and a Neumann FV operator.

    The current roof model is a plane constant along x; arbitrary meshes,
    impedance boundaries and other materials are deliberately unsupported.
    """
    if not isinstance(cells_per_axis, int) or isinstance(cells_per_axis, bool):
        raise ValueError("cells_per_axis must be an integer")
    if cells_per_axis < 3 or cells_per_axis ** 3 > max_cells:
        raise ValueError("grid size outside bounded experimental resources")
    g = geometry if geometry is not None else SlopedPrism()
    h = g.length_m / cells_per_axis
    n = cells_per_axis
    yl = np.arange(n) * h
    zl = np.arange(n) * h
    areas = np.zeros((n, n), dtype=np.float64)
    for j in range(n):
        for k in range(n):
            areas[j, k] = _fluid_section_area(
                yl[j], yl[j] + h, zl[k], zl[k] + h, g,
            )
    # The area is exactly the yz cross-sectional fluid area; it is
    # identical across x because the roof slope is independent of x.
    active = areas > 1e-12 * h * h
    yz = np.argwhere(active)
    if len(yz) == 0:
        raise ValueError("empty embedded fluid volume")
    coordinates = np.asarray(
        [(i, j, k) for i in range(n) for j, k in yz], dtype=np.int32,
    )
    dofs = len(coordinates)
    if dofs > max_cells:
        raise ValueError("active grid exceeds bounded resource ceiling")
    lookup = np.full((n, n, n), -1, dtype=np.int32)
    for ix, (i, j, k) in enumerate(coordinates):
        lookup[i, j, k] = ix
    volume = np.asarray([areas[j, k] * h for i, j, k in coordinates])
    if not (np.all(np.isfinite(volume)) and np.all(volume > 0)):
        raise ValueError("invalid embedded-cell fluid volumes")

    # Face area associated with flux from cell (i,j,k) toward +axis.
    # x-face: yz cut polygon area (same polygon in neighbouring x cells).
    # y-face: vertical z interval below roof at constant y-face.
    # z-face: horizontal y interval below roof at constant z-face.
    edges_u: list[int] = []
    edges_v: list[int] = []
    face_areas: list[float] = []
    for ix, (i, j, k) in enumerate(coordinates):
        for di, dj, dk in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            ii, jj, kk = i + di, j + dj, k + dk
            if ii >= n or jj >= n or kk >= n:
                continue
            other = int(lookup[ii, jj, kk])
            if other == -1:
                continue
            if di:
                area = areas[j, k]
            elif dj:
                yface = (j + 1) * h
                available_z = g.roof_height_at_y0_m - g.roof_drop_per_y_m * yface
                area = h * max(0.0, min((k + 1) * h, available_z) - k * h)
            else:
                zface = (k + 1) * h
                if g.roof_drop_per_y_m == 0.0:
                    accessible_y = g.length_m if zface <= g.roof_height_at_y0_m else 0.0
                else:
                    accessible_y = (
                        (g.roof_height_at_y0_m - zface) / g.roof_drop_per_y_m
                    )
                area = h * max(
                    0.0, min((j + 1) * h, accessible_y) - j * h,
                )
            if area > 1e-14 * h * h:
                edges_u.append(ix)
                edges_v.append(other)
                face_areas.append(area)

    a = np.asarray(edges_u, dtype=np.int32)
    b = np.asarray(edges_v, dtype=np.int32)
    apertures = np.asarray(face_areas, dtype=np.float64)
    weights = (g.sound_speed_m_s ** 2 / h) * apertures
    rows = np.concatenate((a, b, a, b))
    cols = np.concatenate((a, b, b, a))
    vals = np.concatenate((weights, weights, -weights, -weights))
    stiffness = sparse.coo_matrix(
        (vals, (rows, cols)), shape=(dofs, dofs),
    ).tocsr()
    # Avoid an ill-conditioned PSD operator caused by floating cancellation
    # on an inactive or disconnected island.
    mass = sparse.diags(volume, format="csr")
    centers = (coordinates.astype(np.float64) + 0.5) * h
    result = EmbeddedNeumannSystem(
        geometry=g, cells_per_axis=n, grid_spacing_m=h,
        cell_coordinates_ijk=coordinates, cell_center_xyz_m=centers,
        cell_volumes_m3=volume, fluid_face_areas_m2=apertures,
        mass=mass, stiffness=stiffness,
    )
    expected_volume = (
        g.length_m ** 2 *
        (g.roof_height_at_y0_m - 0.5 * g.roof_drop_per_y_m * g.length_m)
    )
    if abs(float(volume.sum()) - expected_volume) > 1e-11 * expected_volume:
        raise ValueError("cut-cell volume does not integrate exact room volume")
    return result


class MidpointIntegrator:
    """Implicit midpoint, exactly energy conserving for the undriven semidiscrete system."""

    def __init__(self, system: EmbeddedNeumannSystem, dt_s: float):
        if not (math.isfinite(dt_s) and dt_s > 0.0):
            raise ValueError("time step must be finite positive")
        self.system = system
        self.dt_s = float(dt_s)
        self._dt2_over_4 = dt_s ** 2 / 4
        left = system.mass + self._dt2_over_4 * system.stiffness
        self._solve = factorized(left.tocsc())

    def step(
        self, potential: np.ndarray, velocity: np.ndarray,
        *, midpoint_force: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        p = np.asarray(potential, dtype=np.float64)
        v = np.asarray(velocity, dtype=np.float64)
        n = self.system.degrees_of_freedom
        if p.shape != (n,) or v.shape != (n,):
            raise ValueError("state vector shape mismatch")
        rhs = (self.system.mass @ p
               - self._dt2_over_4 * (self.system.stiffness @ p)
               + self.dt_s * (self.system.mass @ v))
        if midpoint_force is not None:
            f = np.asarray(midpoint_force, dtype=np.float64)
            if f.shape != (n,) or not np.all(np.isfinite(f)):
                raise ValueError("forcing vector invalid")
            rhs = rhs + (self.dt_s ** 2 / 2) * f
        p1 = self._solve(rhs)
        v1 = 2 * (p1 - p) / self.dt_s - v
        return np.asarray(p1), np.asarray(v1)


def interior_point_stencil(
    system: EmbeddedNeumannSystem,
    point_xyz_m: tuple[float, float, float],
) -> np.ndarray:
    """Exact trilinear interpolation between active cell centres.

    Reject points whose interpolation cube crosses a missing cut cell or
    room boundary. A one-sided or fitted interpolation authority is not
    silently substituted. Source and receiver use the same weights.
    """
    p = np.asarray(point_xyz_m, dtype=np.float64)
    n = system.cells_per_axis
    h = system.grid_spacing_m
    if p.shape != (3,) or not np.all(np.isfinite(p)):
        raise ValueError("point must be finite xyz")
    if np.any(p < h / 2) or np.any(p > system.geometry.length_m - h / 2):
        raise ValueError("point is outside supported interior centre cube")
    if p[2] > (system.geometry.roof_height_at_y0_m
               - system.geometry.roof_drop_per_y_m * p[1]):
        raise ValueError("point is above the physical roof")
    cell_ijk = system.cell_coordinates_ijk
    lookup = {tuple(row): index for index, row in enumerate(cell_ijk)}
    axis_pairs = []
    for x in p:
        x_index = float(x / h - 0.5)
        lo = max(0, min(n - 2, int(math.floor(x_index))))
        frac = x_index - lo
        axis_pairs.append(((lo, 1 - frac), (lo + 1, frac)))
    weights = np.zeros(system.degrees_of_freedom, dtype=np.float64)
    for i, wi in axis_pairs[0]:
        for j, wj in axis_pairs[1]:
            for k, wk in axis_pairs[2]:
                product = wi * wj * wk
                if product <= 1e-14:
                    continue
                dof = lookup.get((i, j, k))
                if dof is None:
                    raise ValueError("point stencil intersects solid cut cell")
                weights[dof] += product
    if not np.isclose(weights.sum(), 1.0, atol=1e-12, rtol=0.0):
        raise ValueError("invalid point interpolation partition of unity")
    return weights


def smooth_source_complex_transfer(
    system: EmbeddedNeumannSystem,
    *,
    source_xyz_m: tuple[float, float, float] = (1.5, 2.0, 2.0),
    receiver_xyz_m: tuple[float, float, float] = (2.5, 2.0, 2.0),
    frequency_hz: tuple[float, ...] = (40.0, 80.0),
    duration_s: float = 0.25,
    time_step_s: float = 0.00025,
    drive_center_s: float = 0.012,
    drive_sigma_s: float = 0.003,
    density_kg_m3: float = 1.2,
) -> np.ndarray:
    """Compute a *new*, actual band-limited drive of the embedded FV solver.

    Semidiscrete wave equation:
      M phi_tt + K phi = c^2 b q(t).
      p_receiver(t) = rho * r^T phi_t(t).

    q(t) is explicitly sampled at implicit-midpoint times. Pressure and
    source use the same midpoint [0,T) Fourier integration. This is not a
    postprocessed impulse trace. Complex transfer is P_T/Q_T in
    Pa/(m^3/s), exp(+i*omega*t) analysis kernel.

    This is an *experimental changed-source and changed-solver contract*:
    frozen R130D PFFDTD/MFEM impulse gate is not replaced.
    """
    vals = (duration_s, time_step_s, drive_center_s, drive_sigma_s,
            density_kg_m3)
    if not all(math.isfinite(x) for x in vals) or not (
        duration_s > 0 and time_step_s > 0
        and drive_sigma_s > 0 and density_kg_m3 > 0
        and 0 < drive_center_s < duration_s
    ):
        raise ValueError("invalid physical source/time contract")
    ratio = duration_s / time_step_s
    if not math.isclose(ratio, round(ratio), rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("time-step must exactly divide requested duration")
    frequencies = np.asarray(frequency_hz, dtype=np.float64)
    if (frequencies.ndim != 1 or frequencies.size == 0
            or not np.all(np.isfinite(frequencies))
            or np.any(frequencies <= 0)
            or np.any(frequencies >= 1 / (2 * time_step_s))):
        raise ValueError("frequency axis invalid or violates Nyquist")
    source = interior_point_stencil(system, source_xyz_m)
    receiver = interior_point_stencil(system, receiver_xyz_m)
    n = int(round(ratio))
    time_mid = (np.arange(n) + 0.5) * time_step_s
    drive = np.exp(-0.5 * ((time_mid - drive_center_s) / drive_sigma_s) ** 2)
    if not np.all(np.isfinite(drive)):
        raise ValueError("nonfinite source")
    phi = np.zeros(system.degrees_of_freedom)
    velocity = np.zeros_like(phi)
    integrate = system.midpoint_integrator(time_step_s)
    pressure = np.empty(n, dtype=np.float64)
    rhs_scale = system.geometry.sound_speed_m_s ** 2
    for sample in range(n):
        next_phi, next_velocity = integrate.step(
            phi, velocity,
            midpoint_force=(rhs_scale * drive[sample]) * source,
        )
        pressure[sample] = (
            density_kg_m3 * float(receiver @ (velocity + next_velocity)) / 2
        )
        phi, velocity = next_phi, next_velocity
    kernel = np.exp(2j * math.pi * frequencies[:, None] * time_mid[None, :])
    p_spectrum = time_step_s * (kernel @ pressure)
    q_spectrum = time_step_s * (kernel @ drive)
    if np.any(np.abs(q_spectrum) <=
              1e-12 * max(float(np.max(np.abs(q_spectrum))), 1e-30)):
        raise ValueError("source spectrum too small for normalized transfer")
    transfer = p_spectrum / q_spectrum
    if not np.all(np.isfinite(transfer)):
        raise ValueError("nonfinite transfer")
    return np.asarray(transfer, dtype=np.complex128)
