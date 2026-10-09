"""Bounded eigenmode and discrete-energy benchmark for the new FV operator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys

import numpy as np
from scipy.sparse.linalg import eigsh

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from htdt.r130d_embedded_neumann_fv import (
    SlopedPrism, build_sloped_embedded_neumann,
)


def positive_frequencies(s, count: int = 4) -> list[float]:
    lambdas = eigsh(s.stiffness, k=count + 2, M=s.mass,
                    sigma=-1.0, which="LM", return_eigenvectors=False,
                    tol=1e-10)
    frequencies = np.sort(np.sqrt(np.maximum(lambdas, 0)) / (2*np.pi))
    return [float(x) for x in frequencies if x > 1e-4][:count]


def measure_energy(s) -> dict:
    # Cut-cell slivers impose a restrictive explicit CFL; midpoint is
    # implicit and conserves the undamped semidiscrete Hamiltonian.
    xyz = s.cell_center_xyz_m
    u = np.cos(np.pi*xyz[:, 0]/s.geometry.length_m)
    v = np.zeros(s.degrees_of_freedom)
    dt = 0.005
    solver = s.midpoint_integrator(dt)
    start = s.energy(u, v)
    energies = []
    for _ in range(50):
        u, v = solver.step(u, v)
        energies.append(s.energy(u, v))
    return {
        "dt_s": dt,
        "steps": len(energies),
        "initial_energy": start,
        "max_relative_energy_drift": float(max(
            abs(e-start)/start for e in energies)),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--levels", nargs="+", type=int, default=[6, 8, 12, 16])
    a = p.parse_args()
    rows = []
    for n in a.levels:
        s = build_sloped_embedded_neumann(n)
        freqs = positive_frequencies(s)
        energy = measure_energy(s)
        normal_error = float(np.max(np.abs(s.stiffness @
                                  np.ones(s.degrees_of_freedom))))
        symmetry_error = float(np.max(np.abs(
            (s.stiffness - s.stiffness.T).data), initial=0))
        row = {
            "cells_per_axis": n,
            "degrees_of_freedom": s.degrees_of_freedom,
            "grid_spacing_m": s.grid_spacing_m,
            "integrated_fluid_volume_m3": float(s.cell_volumes_m3.sum()),
            "first_positive_eigenfrequencies_hz": freqs,
            "neumann_constant_mode_max_abs_residual": normal_error,
            "symmetric_stiffness_max_abs_error": symmetry_error,
            "undriven_midpoint_energy": energy,
        }
        rows.append(row)
        print("LEVEL",n,"DOF",s.degrees_of_freedom,"Hz",freqs,
              "E_DRIFT",energy["max_relative_energy_drift"],flush=True)
    reference = {"mfem_refinement3_first_eigenfrequencies_hz":
                 [41.9606, 42.9007, 49.1127],
                 "note":"reference from pinned independent MFEM sparse eigen diagnostic, not a fit"}
    report={
        "schema_version":"htdt.r130d.embedded-neumann-fv-bounded-eigen-evidence-1",
        "operator":"exact cut-face aperture / volume weighted Neumann finite volume",
        "fixture":"R130D 4x4m, roof z=4-y/4m, c=343.2m/s",
        "physics_adoption":False,
        "production_pffdtd_modified":False,
        "cross_solver_eligible":False,
        "external_reference":reference,
        "runtime":{"numpy":np.__version__,"python":sys.version.split()[0],
                   "platform":platform.platform()},
        "levels":rows,
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
