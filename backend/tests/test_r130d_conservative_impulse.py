"""Independent matrix propagation verifies original pressure DTFT and zero mode."""
import numpy as np
import pytest
from scipy.linalg import expm
from htdt.r130d_conservative_impulse import ARMS, conservative_q0_trace, conservative_q0_transfer, original_pressure_endpoint_audit

@pytest.mark.parametrize('arm',ARMS)
def test_against_full_coupled_matrix_propagation(arm):
    # Orthogonal mixture makes this a coupled physical-state update, including
    # the rigid Neumann free particle. No modal trace/DTFT helper in reference.
    Q=np.array([[1,1,1],[1,-1,0],[1,1,-2]],float).T
    Q,_=np.linalg.qr(Q)
    lam=np.array([0.,700.**2,17000.**2]);c=343.2;rho=1.2;h=.08;dt=.0001;nt=73
    eff=lam.copy();tau=h*h/(12*c*c)
    if arm=='rational_mass_exact':eff=lam/(1+tau*lam)
    if arm=='hyperstiffness_exact':eff=lam+tau*lam**2
    K=Q@np.diag(eff)@Q.T
    A=np.block([[np.zeros((3,3)),np.eye(3)],[-K,np.zeros((3,3))]])
    # Scale velocities to keep expm/Padé evaluation well conditioned.
    scale=np.diag([1.,1.,1.,1/17000.,1/17000.,1/17000.])
    X=dt*scale@A@np.linalg.inv(scale);I=np.eye(6)
    if arm=='gauss2':R=np.linalg.solve(I-X/2,I+X/2)
    elif arm=='gauss4':R=np.linalg.solve(I-X/2+X@X/12,I+X/2+X@X/12)
    elif arm=='gauss6':R=np.linalg.solve(I-X/2+X@X/10-X@X@X/120,I+X/2+X@X/10+X@X@X/120)
    else:R=expm(X)
    b=np.array([.2,-.4,.7]);r=np.array([.3,.6,-.1]);cp=(b@Q)*(r@Q)
    u=scale@np.r_[np.zeros(3),c*c*dt*b];phi=[];energies=[]
    for _ in range(nt):
        state=np.linalg.solve(scale,u)
        phi.append(r@state[:3]);energies.append(state[3:]@state[3:]+state[:3]@K@state[:3])
        u=R@u
    pressure=rho*np.gradient(phi,dt,edge_order=2)
    t,actual_phi,actual_pressure,_=conservative_q0_trace(lam,cp,dt,nt,h,arm=arm,chunk=2)
    np.testing.assert_allclose(actual_phi,phi,rtol=2e-10,atol=2e-10)
    np.testing.assert_allclose(actual_pressure,pressure,rtol=2e-9,atol=1e-5)
    frequencies=[40.,80.]
    H,_=conservative_q0_transfer(lam,cp,dt,nt,h,arm=arm,frequencies_hz=frequencies)
    reference=np.exp(2j*np.pi*np.asarray(frequencies)[:,None]*t)@pressure
    np.testing.assert_allclose(H,reference,rtol=2e-9,atol=1e-5)
    assert np.max(abs(np.asarray(energies)/energies[0]-1))<2e-11

@pytest.mark.parametrize('changes',[
    {'lam':[0.,float('nan')]},{'lam':[-1.,1.]},{'coupling':[1.]},
    {'dt':0.},{'nt':2},{'arm':'viscous'},{'frequencies_hz':[float('inf')]},
])
def test_invalid_inputs_rejected(changes):
    args=dict(lam=np.array([0.,1.]),coupling=np.array([1.,1.]),dt=.001,nt=9,h=.1)
    args.update(changes)
    with pytest.raises(ValueError):conservative_q0_transfer(**args)

def test_endpoint_audit_preserves_each_signed_contribution():
    # Arbitrary record, including nonzero initial potential, so an endpoint
    # term cannot disappear accidentally under a zero initial condition.
    dt=.0002;phi=np.random.default_rng(203).normal(size=41)
    result=original_pressure_endpoint_audit(phi,dt)
    for row in result['frequencies']:
        z=lambda key:complex(*row[key])
        np.testing.assert_allclose(z('bulk')+z('initial_stencils')+z('final_stencils'),z('total'),rtol=1e-12,atol=1e-8)
        direct=np.exp(2j*np.pi*row['frequency_hz']*np.arange(len(phi))*dt)@(1.2*np.gradient(phi,dt,edge_order=2))
        np.testing.assert_allclose(z('total'),direct,rtol=1e-12,atol=1e-8)
        assert row['endpoint_terms_retained'] is True
