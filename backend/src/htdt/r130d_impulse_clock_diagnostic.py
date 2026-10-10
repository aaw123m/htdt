"""Unchanged nodal q0 free-field clocks and an unfitted late-time asymptotic.

These are attribution diagnostics, not closed-room convergence qualification.
The leading stationary-phase term is derived from the exact discrete Green
generating function, away from its arrival turning point. No time filtering,
endpoint subtraction or output correction is performed.
"""
import numpy as np
from .r130d_newmark_point_green import newmark_point_green_stream

def late_point_green(distances,indices,dt,*,c=343.2):
    r=np.asarray(distances,float);n=np.asarray(indices,float)
    if (not np.isfinite(r).all() or not np.isfinite(n).all() or np.any(r<=0)
        or np.any(n<1) or not np.isfinite([dt,c]).all() or min(dt,c)<=0):
        raise ValueError('positive late-time distances, indices and clock required')
    a=2*r/(c*dt);ratio=2*n/a
    if np.any(ratio<=1):raise ValueError('stationary phase requires time strictly after arrival')
    tangent=np.sqrt(ratio-1);theta=2*np.arctan(tangent)
    amplitude=ratio/(4*np.pi*r)*np.sqrt(2/(np.pi*n*tangent))
    phase=a*tangent-n*theta+np.pi/4
    return amplitude*np.cos(phase),amplitude

def free_q0_trace(source_xyz,receiver_xyz,sw,rw,dt,nt,*,c=343.2,rho=1.2):
    s=np.asarray(source_xyz,float);r=np.asarray(receiver_xyz,float)
    sw=np.asarray(sw,float);rw=np.asarray(rw,float)
    if (s.shape!=(8,3) or r.shape!=(8,3) or sw.shape!=(8,) or rw.shape!=(8,)
        or any(not np.isfinite(a).all() for a in (s,r,sw,rw))
        or abs(sum(sw)-1)>2e-10 or abs(sum(rw)-1)>2e-10
        or not np.isfinite(rho) or rho<=0):raise ValueError('original eight-point cloud and unit weights required')
    distance=np.linalg.norm(r[None,:,:]-s[:,None,:],axis=2).ravel()
    weight=np.outer(sw,rw).ravel()
    phi=np.array([weight@p for p,g in newmark_point_green_stream(distance,dt,nt,c=c)])
    t=np.arange(nt)*dt;p=rho*np.gradient(phi,dt,edge_order=2)
    H=np.exp(2j*np.pi*np.array([40.,80.])[:,None]*t)@p
    return t,phi,p,H
