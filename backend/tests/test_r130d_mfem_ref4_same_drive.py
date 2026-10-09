"""Falsifiable ref4 sparse solver preregistration and algebra regression."""
from __future__ import annotations
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy import sparse
from scipy.sparse.linalg import cg,LinearOperator

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))

from run_r130d_mfem_ref4_same_drive import matrix_from_csr


def test_ref4_preregistered_limit_and_nonproduction_gates():
    p=json.loads((ROOT/"benchmarks"/"acoustics"/
       "r130d_mfem_ref4_same_drive_plan_2026-10-09.json").read_text())
    assert p["schema_version"]=="htdt.r130d.same-source-independent-mfem-r4-experiment-1"
    assert p["mfem_git_source_pin"]=="d964264cdb9a13e94a201b6c236c7721e0c8765f"
    assert p["mfem_compile_reference"].endswith("sloped_tet_system_r4.cpp")
    assert p["expected_dofs"]==35937
    assert p["expected_tetra_elements"]==24576
    assert (p["dt_s"],p["duration_s"],p["steps"])==(0.00025,0.25,1000)
    assert p["frequencies_hz"]==[40,80]
    assert p["gate"]["production_ready"] is False
    assert p["gate"]["independent_cross_solver_qualified"] is False
    assert p["gate"]["canonical_pffdtd"]=="SELF_CONVERGENCE_FAILED"


def test_sparse_operator_nonzero_bound_and_identity_reconstruction():
    data={"rows":3,"cols":3,"nnz":4,"row_offsets":[0,1,3,4],
          "column_indices":[0,1,2,2],"values":[1,2,3,4]}
    mat=matrix_from_csr(data,ndofs=3,max_nnz=4)
    np.testing.assert_allclose(mat.toarray(),np.array([[1,0,0],[0,2,3],[0,0,4]]))
    with pytest.raises(ValueError):
        matrix_from_csr(data,ndofs=2,max_nnz=4)
    with pytest.raises(ValueError):
        matrix_from_csr(data,ndofs=3,max_nnz=3)


def test_jacobi_pcg_satisfies_midpoint_2by2_physics_residual():
    mass=sparse.diags([1.1,0.7],format="csr")
    stiff=sparse.csr_matrix([[5.0,-5.0],[-5.0,5.0]])
    dt=0.00025
    A=mass+(dt*dt/4)*stiff
    J=LinearOperator((2,2),matvec=lambda x:x/A.diagonal())
    oldphi=np.array([1.0,-0.5])
    vel=np.array([0.0,0.0])
    rhs=(mass-(dt*dt/4)*stiff)@oldphi+dt*(mass@vel)
    result,status=cg(A,rhs,x0=oldphi,M=J,rtol=1e-11,atol=1e-12,maxiter=350)
    assert status==0
    np.testing.assert_allclose(result,np.linalg.solve(A.toarray(),rhs),rtol=1e-11)
    assert np.linalg.norm(A@result-rhs)/np.linalg.norm(rhs)<1e-10
