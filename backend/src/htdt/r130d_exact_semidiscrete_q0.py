"""Exact *experimental* semidiscrete Neumann modal point-q0 time propagators.

Unmodified spatial graph and point source coefficients. These integrators use
two mathematically distinct temporal interpretations of a single q[0]=1
sample, neither silently replaces the original PFFDTD leapfrog source
stencil. Both use the same original finite rectangular pressure observation.

No damping, modal truncation or sample-window modifications. Both include
the true rigid Neumann zero-eigenvalue analytic limit.
"""
from __future__ import annotations

import numpy as np

FREQ_HZ=np.array([40.,80.],dtype=float)


def _arrays(theta,amplitude,dt,nt,rho):
    theta=np.asarray(theta,dtype=float).ravel()
    amplitude=np.asarray(amplitude,dtype=float).ravel()
    if (theta.shape!=amplitude.shape or theta.size==0 or
        not np.isfinite(theta).all() or not np.isfinite(amplitude).all()
        or np.min(theta)<-1e-8 or np.max(theta)>np.pi+1e-8 or
        nt<5 or nt>2000 or not np.isfinite(dt) or dt<=0 or
        not np.isfinite(rho) or rho<=0):
        raise ValueError("invalid original native all-mode q0 or sample clock")
    return np.maximum(theta,0),amplitude


def _qprog(m,beta):
    """Stable sum_{k=1..m} exp(i*k*beta), including exact beta=0."""
    if m<0:raise ValueError("invalid exact finite observation interval")
    beta=np.asarray(beta,dtype=float)
    return (m*np.exp(.5j*(m+1)*beta)*np.sinc(m*beta/(2*np.pi))/
            np.sinc(beta/(2*np.pi)))


def exact_semidiscrete_velocity_impulse_signed(
        theta,amplitude,*,native_dt_s,native_nt,density_kg_m3=1.2):
    """Causal v(0+)=A/dt, phi[n]=A sin(n theta)/theta.

    Original discrete q[0] amplitude A, original graph and 8node weights.
    Original PFFDTD pressure sample stencil preserved.
    """
    th,a=_arrays(theta,amplitude,native_dt_s,native_nt,density_kg_m3)
    rho=float(density_kg_m3);dt=float(native_dt_s);nt=int(native_nt)
    # Existing original leapfrog pressure formula with true omega*dt
    # and the mathematically correct physical delta velocity initial kick.
    effective=a*np.sinc(th/np.pi)  # A sin(theta)/theta; exact zero-mode limit A.
    cos=np.cos(th)
    def ratio(m):
        return m*np.sinc(m*th/np.pi)/np.sinc(th/np.pi)
    end=ratio(nt-1)+(cos-2)*ratio(nt-2)
    signed=np.empty((2,len(th)),dtype=complex)
    for i,f in enumerate(FREQ_HZ):
        w=2*np.pi*f*dt
        center=.5*(_qprog(nt-2,w+th)+_qprog(nt-2,w-th))
        signed[i]=(rho*effective/dt)*(
            (2-cos)+center+np.exp(1j*w*(nt-1))*end)
    if not np.isfinite(signed).all():
        raise ValueError("experimental causal delta q0 P_T/Q_T nonfinite")
    return signed


def exact_semidiscrete_one_sample_hold_phi(
        sample_n,theta,amplitude):
    """Exact causal oscillator with held acceleration A/dt² over [0,dt].

    phi[0]=0; for integer n>=1:
      phi[n]=A*[cos((n-1)theta)-cos(n theta)]/theta².
    Stable around theta=0 with phi[n]=A*(n-1/2).
    """
    n=int(sample_n)
    if n!=sample_n or n<0:raise ValueError("invalid causal native q0 time")
    th=np.asarray(theta,dtype=float)
    a=np.asarray(amplitude,dtype=float)
    if th.shape!=a.shape:
        raise ValueError("held physical source amplitude and eigenangle mismatch")
    if n==0:return np.zeros_like(a,dtype=float)
    return (a*(n-.5)*np.sinc(th/(2*np.pi))*
            np.sinc((n-.5)*th/np.pi))


def exact_semidiscrete_one_sample_hold_signed(
        theta,amplitude,*,native_dt_s,native_nt,density_kg_m3=1.2):
    """Causal exact-oscillator response to q0=1 held over [0,dt], then zero.

    Acceleration is A/dt² for 0<=t<dt, no source thereafter.
    No input normalization, taper or receiver changes; score same N samples.
    Exact O(number_of_modes) finite-sample DFT of 2nd order pressure stencil,
    including the special causal initial n=0 and n=1 samples.
    """
    th,a=_arrays(theta,amplitude,native_dt_s,native_nt,density_kg_m3)
    rho=float(density_kg_m3);dt=float(native_dt_s);nt=int(native_nt)
    p1=exact_semidiscrete_one_sample_hold_phi(1,th,a)
    p2=exact_semidiscrete_one_sample_hold_phi(2,th,a)
    # Original PFFDTD second-order forward and centered stencil; phi0=0.
    start=rho*(4*p1-p2)/(2*dt)
    first_center=rho*p2/(2*dt)
    p_end=rho*(3*exact_semidiscrete_one_sample_hold_phi(nt-1,th,a)
               -4*exact_semidiscrete_one_sample_hold_phi(nt-2,th,a)
               +exact_semidiscrete_one_sample_hold_phi(nt-3,th,a))/(2*dt)
    # For n>=2, original centered derivative is exactly:
    # rho*A/dt * sinc(theta/(2*pi))*sinc(theta/pi)*cos((n-1/2)*theta).
    common=(rho*a/dt)*np.sinc(th/(2*np.pi))*np.sinc(th/np.pi)
    result=np.empty((2,len(th)),dtype=complex)
    for i,f in enumerate(FREQ_HZ):
        w=2*np.pi*f*dt
        # Exact n=2..Nt-2, inclusive; Q(nt-2) covers n=1..Nt-2,
        # subtract the first n=1 term, which is special because phi0=0.
        cos_sum=.5*(
            np.exp(-.5j*th)*(_qprog(nt-2,w+th)-np.exp(1j*(w+th)))+
            np.exp(.5j*th)*(_qprog(nt-2,w-th)-np.exp(1j*(w-th))))
        result[i]=start+np.exp(1j*w)*first_center+common*cos_sum+(
            np.exp(1j*w*(nt-1))*p_end)
    if not np.isfinite(result).all():
        raise ValueError("experimental exact q0 one-step source hold response nonfinite")
    return result
