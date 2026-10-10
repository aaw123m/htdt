import numpy as np
import pytest
from htdt.r130d_impulse_clock_diagnostic import late_point_green,free_q0_trace
from htdt.r130d_newmark_point_green import newmark_point_green_stream

def test_unfitted_stationary_phase_matches_exact_coefficients_over_nonzero_late_window():
    dt=2e-5;r=np.array([2.4]);nt=4500
    exact=np.array([p[0] for p,g in newmark_point_green_stream(r,dt,nt)])
    n=np.arange(4000,nt)
    approximation,envelope=late_point_green(r,n,dt)
    assert np.linalg.norm(exact[n])>1e-3
    assert np.linalg.norm(exact[n]-approximation)/np.linalg.norm(exact[n])<.01
    assert np.all(abs(approximation)<=envelope)

def test_same_physical_time_envelope_scales_as_sqrt_dt_without_fitting():
    dt=2e-5;n=np.arange(4000,4500)
    p,a=late_point_green(2.4,n,dt)
    p2,a2=late_point_green(2.4,2*n,dt/2)
    np.testing.assert_allclose(a2,a/np.sqrt(2),rtol=1e-13)

@pytest.mark.parametrize('r,n,dt',[(1.,1,.00001),(0.,2000,.0001),(1.,0,.0001),(1.,2000,-.0001)])
def test_nonlate_or_invalid_inputs_are_rejected(r,n,dt):
    with pytest.raises(ValueError):late_point_green(r,n,dt)

def test_free_trace_keeps_original_native_gradient_and_all_endpoints():
    s=np.tile([1.,1.,1.],(8,1));r=np.tile([2.,1.,1.],(8,1));w=np.ones(8)/8
    t,phi,p,H=free_q0_trace(s,r,w,w,.0002,600)
    direct=np.array([a[0] for a,b in newmark_point_green_stream(np.array([1.]),.0002,600)])
    np.testing.assert_allclose(phi,direct,rtol=1e-14)
    np.testing.assert_allclose(p,1.2*np.gradient(direct,.0002,edge_order=2),rtol=1e-14)
    np.testing.assert_allclose(H,np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p,rtol=1e-14)
