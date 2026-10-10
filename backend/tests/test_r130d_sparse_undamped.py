import numpy as np
import pytest
from scipy.linalg import eigh
from scipy.sparse import csr_matrix
from htdt.r130d_sparse_undamped import sparse_undamped_q0
from htdt.r130d_conservative_impulse import conservative_q0_trace

@pytest.mark.parametrize('substeps',[1,2,4])
@pytest.mark.parametrize('linear_solver',['direct','cg'])
def test_full_operator_against_independent_generalized_modes(substeps,linear_solver):
    M=csr_matrix([[2.,.2,0.],[.2,1.,.1],[0.,.1,1.5]])
    K=csr_matrix(np.array([[1.,-1.,0.],[-1.,3.,-2.],[0.,-2.,2.]])*40000)
    b=np.array([1.,0.,0.]);r=np.array([0.,0.,1.]);dt=.0003;nt=57
    lam,V=eigh(K.toarray(),M.toarray());lam[0]=0.
    t,phi,p,H,proof=sparse_undamped_q0(M,K,b,r,dt,nt,substeps=substeps,linear_solver=linear_solver)
    # Internal impulse amplitude is rescaled solely to retain the ORIGINAL
    # native area dt, then subsampled before the original pressure gradient.
    _,reference,_,_=conservative_q0_trace(lam,(b@V)*(r@V)*substeps,
        dt/substeps,(nt-1)*substeps+1,.1,arm='gauss2')
    reference=reference[::substeps]
    np.testing.assert_allclose(phi,reference,rtol=2e-10,atol=1e-10)
    np.testing.assert_allclose(p,1.2*np.gradient(reference,dt,edge_order=2),rtol=2e-9,atol=1e-7)
    assert proof['max_energy_relative_drift']<2e-11
    assert proof['source_impulse_area_s']==dt

def test_memory_guard_precedes_factorization(monkeypatch):
    import htdt.r130d_sparse_undamped as module
    def forbidden(*args):raise AssertionError('factorization was reached')
    monkeypatch.setattr(module,'splu',forbidden)
    with pytest.raises(ValueError,match='memory budget'):
        sparse_undamped_q0(csr_matrix(np.eye(5)),csr_matrix(np.eye(5)),np.ones(5),np.ones(5),.001,9,memory_budget_bytes=1)
