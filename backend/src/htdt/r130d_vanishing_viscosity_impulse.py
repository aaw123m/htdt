"""All-mode exact causal q0 with mesh-vanishing velocity hyperviscosity.

Finite-h damping is explicitly part of a NEW numerical method. It does not
change the source, discard modes, or taper the sampled pressure record.
L is the positive physical Neumann stiffness/mass wave operator; nu=k*h^3/c^3.
For every fixed eigenvalue the added damping tends to zero as h tends to zero.
"""
from __future__ import annotations
import numpy as np


def _geo0(n, exponent):
    s=np.asarray(exponent,complex)
    s=s.real+1j*((s.imag+np.pi)%(2*np.pi)-np.pi)
    den=np.expm1(s)
    near=abs(s)<1e-12
    out=np.empty(s.shape,complex)
    out[near]=n*(1.+(n-1)*s[near]/2.)
    out[~near]=np.expm1(n*s[~near])/den[~near]
    return out


def viscous_roots(lam,nu):
    lam=np.asarray(lam,float)
    alpha=nu*lam**2
    root=np.sqrt((alpha**2-4*lam).astype(complex))
    fast=(-alpha-root)/2.
    slow=np.divide(2.*lam,-alpha-root,out=np.zeros(lam.shape,complex),where=abs(alpha+root)>0)
    return slow,fast,alpha


def original_pressure_sum_exp(root,dt,nt,frequency_hz):
    """dt times DTFT of original one-sided/centered derivative of exp(root*t)."""
    r=np.asarray(root,complex);z=np.exp(2j*np.pi*frequency_hz*dt)
    E=np.exp(r*dt)
    first=(-3.+4.*E-E**2)/2.
    interior=z*np.expm1(2*r*dt)*_geo0(nt-2,r*dt+2j*np.pi*frequency_hz*dt)/2.
    end=(z**(nt-1))*np.exp(r*dt*(nt-3))*(3.*E**2-4.*E+1.)/2.
    return first+interior+end


def viscous_original_q0_transfer(lam,coupling,dt,nt,h,*,kappa=1.,
                                frequencies_hz=(40.,80.),c=343.2,rho=1.2):
    lam=np.asarray(lam,float).ravel();cp=np.asarray(coupling,float).ravel()
    if lam.shape!=cp.shape or not len(lam) or np.min(lam)<0 or dt<=0 or nt<3 or h<=0 or kappa<0:
        raise ValueError('valid complete modes, clocks and nonnegative stabilization required')
    nu=kappa*h**3/c**3
    rp,rm,alpha=viscous_roots(lam,nu)
    gap=rp-rm
    zero=lam==0
    critical=(abs(gap)<1e-6*np.maximum(np.sqrt(lam),1.))&~zero
    regular=~(zero|critical)
    times=np.arange(nt)*dt
    result=[]
    for f in frequencies_hz:
        mode=np.zeros(len(lam),complex)
        mode[regular]=(original_pressure_sum_exp(rp[regular],dt,nt,f)-original_pressure_sum_exp(rm[regular],dt,nt,f))/gap[regular]
        mode[zero]=dt*_geo0(nt,2j*np.pi*f*dt)
        for j in np.flatnonzero(critical):
            # Exact critically damped limiting impulse potential, all samples.
            potential=times*np.exp(-alpha[j]*times/2.)
            derivative=np.gradient(potential,dt,edge_order=2)
            mode[j]=dt*np.dot(np.exp(2j*np.pi*f*times),derivative)
        result.append(rho*c*c*np.dot(cp,mode))
    values=np.asarray(result)
    if not np.isfinite(values).all():raise RuntimeError('viscous all-mode observer nonfinite')
    return values


def viscous_original_q0_trace(lam,coupling,dt,nt,h,*,kappa=1.,c=343.2,rho=1.2,chunk=1024):
    """Actual all-mode impulse potential and the original sampled pressure.

    q[0]=1 has area dt. The initial jump is c^2*dt*M^-1*b; no subsequent
    source samples act. Chunking only bounds memory and never removes modes.
    """
    lam=np.asarray(lam,float).ravel();cp=np.asarray(coupling,float).ravel()
    if lam.shape!=cp.shape or len(lam)==0 or np.min(lam)<0 or dt<=0 or nt<3 or h<=0 or kappa<0 or chunk<1:
        raise ValueError('valid complete modes and original clocks required')
    rp,rm,alpha=viscous_roots(lam,kappa*h**3/c**3);gap=rp-rm
    zero=lam==0;critical=(abs(gap)<1e-6*np.maximum(np.sqrt(lam),1.))&~zero
    t=np.arange(nt)*dt;phi=np.zeros(nt)
    for start in range(0,len(lam),chunk):
        ix=np.arange(start,min(start+chunk,len(lam)));regular=~(zero[ix]|critical[ix])
        j=ix[regular]
        # expm1 avoids cancellation in very slowly overdamped modal roots.
        F=np.exp(rp[j,None]*t)*(-np.expm1(-gap[j,None]*t))/gap[j,None]
        phi+=(cp[j]@F).real
        j=ix[critical[ix]]
        if len(j):phi+=cp[j]@(t[None,:]*np.exp(-alpha[j,None]*t[None,:]/2.))
        phi+=float(cp[ix[zero[ix]]].sum())*t
    phi*=c*c*dt
    pressure=rho*np.gradient(phi,dt,edge_order=2)
    return t,phi,pressure
