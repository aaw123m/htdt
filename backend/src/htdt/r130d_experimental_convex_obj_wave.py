"""Isolated, versioned, explicitly opted-in convex OBJ wave candidate entrypoint.

Not wired into production CAD wave dispatch, acoustic solver capability rows,
run25 canonical authority, or the existing PFFDTD adapter. Returned provenance
always says experimental/NOT_VALIDATED/NO_GO; any malformed/unapproved source
configuration fails closed. This is a testable nonproduction integration seam.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np

from .r130d_convex_obj_import import import_convex_obj_air_mesh
from .r130d_embedded_neumann_convex import build_convex_embedded_neumann
from .r130d_embedded_neumann_fv import smooth_source_complex_transfer


EXPERIMENTAL_ADAPTER_ID="htdt.r130d.convex-obj-cutcell-neumann-experimental"
EXPERIMENTAL_ADAPTER_VERSION="1"
SOURCE_ID="htdt.r130d.gaussian-volume-velocity-3ms-250ms-40-80hz-1"


@dataclass(frozen=True)
class ExperimentalConvexWaveRequest:
    obj_path:Path
    grid_cells_per_axis:int=16
    source_xyz_m:tuple[float,float,float]=(1.5,2.0,2.0)
    receiver_xyz_m:tuple[float,float,float]=(2.5,2.0,2.0)
    explicit_experimental_opt_in:bool=False
    output_authority:str="EXPERIMENTAL_DIAGNOSTIC_ONLY"
    source_authority:str=SOURCE_ID


def execute_experimental_convex_obj_wave(
    request:ExperimentalConvexWaveRequest,
) -> dict:
    """Actually drive physical acoustic candidate only under explicit opt-in."""
    if not isinstance(request,ExperimentalConvexWaveRequest):
        raise ValueError("explicit versioned experimental request required")
    if (request.explicit_experimental_opt_in is not True
            or request.output_authority!="EXPERIMENTAL_DIAGNOSTIC_ONLY"
            or request.source_authority!=SOURCE_ID):
        raise PermissionError("not approved to enter an experimental acoustic calculation")
    n=request.grid_cells_per_axis
    if not isinstance(n,int) or isinstance(n,bool) or not 6<=n<=20:
        raise ValueError("experimental resolution exceeds approved numerical bounds")
    src=np.asarray(request.source_xyz_m,dtype=float)
    recv=np.asarray(request.receiver_xyz_m,dtype=float)
    if (src.shape!=(3,) or recv.shape!=(3,)
            or not np.all(np.isfinite(src)) or not np.all(np.isfinite(recv))):
        raise ValueError("invalid source/receiver 3D position")
    imported=import_convex_obj_air_mesh(Path(request.obj_path))
    if not imported.geometry.contains(src) or not imported.geometry.contains(recv):
        raise ValueError("one or both acoustic points are outside the true room")
    system=build_convex_embedded_neumann(n,geometry=imported.geometry,
                                         max_cells=20**3)
    transfer=smooth_source_complex_transfer(
        system,source_xyz_m=tuple(src),receiver_xyz_m=tuple(recv),
        frequency_hz=(40.,80.),duration_s=.25,
        time_step_s=.00025,drive_center_s=.012,
        drive_sigma_s=.003,density_kg_m3=1.2,
    )
    result={
      "schema_version":"htdt.r130d.experimental-convex-obj-wave-result-1",
      "solver_adapter_id":EXPERIMENTAL_ADAPTER_ID,
      "solver_adapter_version":EXPERIMENTAL_ADAPTER_VERSION,
      "mesh_authority":{
          "type":imported.imported_format,
          "source_sha256":imported.source_sha256,
          "source_size_bytes":imported.source_bytes,
          "vertices":imported.vertex_count,
          "triangles":imported.triangle_count,
          "unique_nonbox_planes":imported.plane_count,
          "exact_polyhedron_volume_m3":imported.surface_signed_volume_m3,
          "halfspaces":[list(x) for x in imported.geometry.additional_planes],
      },
      "wave_authority":{
          "source_id":SOURCE_ID,
          "source_xyz_m":src.tolist(),
          "receiver_xyz_m":recv.tolist(),
          "Gaussian_volume_velocity_center_s":.012,
          "Gaussian_volume_velocity_sigma_s":.003,
          "time_step_s":.00025,"duration_s":.25,
          "frequency_hz":[40.,80.],
          "actual_time_domain_injection":True,
          "complex_transfer":[[float(z.real),float(z.imag)] for z in transfer],
          "transfer_units":"Pa / (m3/s)",
      },
      "grid_cells_per_axis":n,
      "degrees_of_freedom":system.degrees_of_freedom,
      "fluid_volume_m3":float(system.cell_volumes_m3.sum()),
      "experiment_opt_in_recorded":True,
      "production_dispatch_registered":False,
      "general_cad_authority":"NOT_VALIDATED",
      "canonical_r130d_impulse":"SELF_CONVERGENCE_FAILED",
      "independent_mfem_for_this_obj":"NOT_VALIDATED",
      "physical_validation":"NOT_VALIDATED",
      "production_ready":False,
    }
    canonical=json.dumps(result,sort_keys=True,separators=(',',':'))
    result["provenance_sha256"]=sha256(canonical.encode()).hexdigest()
    return result
