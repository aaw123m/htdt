"""Physical Neumann fitted x and original eightpoint x reprojection tests."""
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pytest
from htdt.r130d_uniform_boundary_fitted_x_p1 import (
    build_uniform_boundary_fitted_x_p1,project_original_8point_HDF5_to_fitted_x)

ROOT=Path(__file__).resolve().parents[2]
PLAN=ROOT/"benchmarks/acoustics/r130d_original_q0_boundary_fitted_uniform_x_true_roof_plan_2026-10-10.json"

@pytest.mark.parametrize("h",[.14,.12,.101,.085,.076])
def test_true_uniform_x_positive_neumann_and_exact_affine_energy(h):
    x=build_uniform_boundary_fitted_x_p1(h)
    assert x.segments==int(np.ceil(4/h))
    assert x.nodes_m[0]==0 and x.nodes_m[-1]==4
    assert abs(x.Mx.sum()-4)<1e-12
    v=x.Mx.diagonal()
    assert min(v)>0
    np.testing.assert_allclose(v[[0,-1]],x.dx_m/2,atol=1e-14)
    np.testing.assert_allclose(v[1:-1],x.dx_m,atol=1e-14)
    assert max(abs(x.Kx@np.ones(len(v))))<1e-7
    assert abs(x.nodes_m@(x.Kx@x.nodes_m)-4*343.2**2)<1e-5

def test_original_native_eightpoint_physical_centroid_and_yz_weights():
    axes=[np.array([.5,1.5,2.5,3.5]),np.array([1.,2.]),np.array([1.,2.])]
    hx=[.4,.6];hy=[.3,.7];hz=[.25,.75]
    ids=[];ws=[]
    for i in range(2):
        for j in range(2):
            for k in range(2):
                ids.append(np.ravel_multi_index((i,j,k),(4,2,2)))
                ws.append(hx[i]*hy[j]*hz[k])
    yz=SimpleNamespace(native_yz_modes=4,z_native_positions_m=axes[2],
      original_native_yz_to_active=dict(enumerate(range(4))),
      physical_active_yz_node_positions_m=np.array([[1,1],[1,2],[2,1],[2,2]],float))
    f=build_uniform_boundary_fitted_x_p1(.31)
    px,py,proof=project_original_8point_HDF5_to_fitted_x(
        axes,np.array(ids),np.array(ws),f,yz,(1.1,1.7,1.75))
    assert proof["number_original_native_HDF5_nodes"]==8
    assert proof["first_physical_moment_max_absolute_error_m"]<1e-12
    assert proof["source_receiver_original_Q1_tensor_factorization_max"]<1e-12
    np.testing.assert_allclose(py,np.outer(hy,hz).ravel(),atol=1e-12)
    assert abs(px@f.nodes_m-1.1)<1e-12

def test_remote_frozen_experiment_parameters():
    p=json.loads(PLAN.read_text(encoding="utf8"))
    assert p["original"]["native_ppw"]==[28,32,36,40,44]
    assert p["original"]["full_record_s"]==.25
    assert p["new_operator"]["x_segments_rule"].startswith("N=ceil(4/native_h)")
    assert p["authority"]["product"]=="NO_GO"
