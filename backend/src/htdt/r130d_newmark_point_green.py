"""Exact infinite-space Newmark q0 point field, without spatial truncation.

For r>0 its generating function is
  z/(pi*r*(1+z)^2)*exp[-2*r/(c*dt)*(1-z)/(1+z)].
Laguerre coefficients follow DLMF 18.12.13. This retains the original
q0 impulse area dt and clocks; no damping or source/observer smoothing.
It is not the closed sloped-room Green function by itself.
"""
import numpy as np


def newmark_point_green_stream(distances,dt,nt,*,c=343.2):
    r=np.asarray(distances,float)
    if (r.ndim!=1 or len(r)==0 or not np.isfinite(r).all() or min(r)<=0
        or not np.isfinite([dt,c]).all() or min(dt,c)<=0 or type(nt) is not int or nt<3):
        raise ValueError('positive noncoincident distances and original clock required')
    xp=4/(c*dt);x=xp*r
    if not np.isfinite(x).all():raise ValueError('Green phase overflow')
    fast=bool(max(x)<1200)
    logscale=np.zeros_like(r) if fast else -x/2
    f=np.exp(-x/2) if fast else np.ones_like(r);previous=np.zeros_like(r)
    df=-xp/2*f;dprevious=np.zeros_like(r)
    yield np.zeros_like(r),np.zeros_like(r)
    for n in range(1,nt):
        if fast:actual=f;derivative=df
        else:
            def scaled(v):
                with np.errstate(divide='ignore',under='ignore'):
                    return np.sign(v)*np.exp(logscale+np.log(abs(v)))
            actual=scaled(f);derivative=scaled(df)
        sign=1. if n%2 else -1.
        phi=sign*actual/(np.pi*r)
        gradient=sign*(derivative-actual/r)/(np.pi*r)
        if not np.isfinite(phi).all() or not np.isfinite(gradient).all():raise RuntimeError('point Green overflow')
        yield phi,gradient
        coefficient=2-x/n
        next_f=coefficient*f-previous
        next_df=coefficient*df-dprevious-xp/n*f
        previous,f=f,next_f;dprevious,df=df,next_df
        if not fast:
            large=np.maximum.reduce([abs(previous),abs(f),abs(dprevious),abs(df)])>1e100
            previous[large]/=1e100;f[large]/=1e100;dprevious[large]/=1e100;df[large]/=1e100
            logscale[large]+=np.log(1e100)
