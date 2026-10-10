import numpy as np
import pytest
from scipy.sparse import csr_matrix
from htdt.r130d_boundary_enrichment import enriched_q0_trace
from htdt.r130d_newmark_point_green import newmark_point_green_stream


def fixture():
    M=csr_matrix(np.diag([1.,2.,3.]))
    K=csr_matrix([[2.,-1.,-1.],[-1.,3.,-2.],[-1.,-2.,3.]])
    receiver=np.array([.1,.3,.6]);native={'dt':.0005,'Nt':50}
    source=np.tile([0.,0.,0.],(8,1));points=np.tile([.8,0.,0.],(8,1))
    weights=np.ones(8)/8
    boundary=np.array([[1.,0.,0.],[0.,1.2,0.]])
    normals=np.array([[1.,0.,0.],[0.,1.,0.]])
    B=csr_matrix([[.3,.1],[.2,.2],[.5,.7]])
    return M,K,receiver,native,source,points,weights,weights,boundary,normals,B


def test_forced_field_matches_independent_first_order_block_midpoint():
    args=fixture();M,K,receiver,native,source,points,sw,rw,xyz,normals,B=args
    t,phi,p,w,H,audit=enriched_q0_trace(*args)
    # Independent first-order (u,v) update, rather than eliminated Newmark.
    dt=native['dt'];nt=native['Nt'];mass=M.toarray()
    operator=np.block([[np.zeros((3,3)),np.eye(3)],[-np.linalg.solve(mass,K.toarray()),np.zeros((3,3))]])
    left=np.eye(6)-dt*operator/2;right=np.eye(6)+dt*operator/2
    state=np.zeros(6);old=np.zeros(3);expected=np.zeros(nt)
    gradients=list(newmark_point_green_stream(np.linalg.norm(xyz,axis=1),dt,nt))
    direct=list(newmark_point_green_stream(np.array([.8]),dt,nt))
    for j in range(1,nt):
        force=-343.2**2*(B@gradients[j][1])
        rhs=right@state+dt/2*np.r_[np.zeros(3),np.linalg.solve(mass,old+force)]
        state=np.linalg.solve(left,rhs);old=force
        expected[j]=receiver@state[:3]+direct[j][0][0]
    np.testing.assert_allclose(phi,expected,rtol=2e-11,atol=2e-10)
    np.testing.assert_allclose(p,1.2*np.gradient(expected,dt,edge_order=2),rtol=2e-10,atol=2e-8)
    np.testing.assert_allclose(H,np.exp(2j*np.pi*np.array([40,80])[:,None]*t)@p,rtol=1e-12)
    assert audit['max_correction_energy_work_defect']<1e-12
    assert not audit['total_singular_field_energy_claimed_finite']


def test_zero_boundary_correction_recovers_free_green_without_attenuation():
    args=list(fixture());args[-1]=csr_matrix(args[-1].shape)
    t,phi,p,w,H,audit=enriched_q0_trace(*args)
    expected=np.array([a[0] for a,b in newmark_point_green_stream(np.array([.8]),args[3]['dt'],args[3]['Nt'])])
    np.testing.assert_allclose(phi,expected,atol=1e-12,rtol=1e-12)
    assert np.count_nonzero(w)==0 and audit['viscosity']==0.


def test_memory_guard_precedes_green_allocations():
    with pytest.raises(ValueError,match='memory'):enriched_q0_trace(*fixture(),memory_budget_bytes=1)


def test_original_weights_cannot_be_silently_renormalized():
    args=list(fixture());args[6]=np.ones(8)
    with pytest.raises(ValueError,match='source'):enriched_q0_trace(*args)
