"""Original native explicit leapfrog q0 signed 250ms transfer, all modes.

The spatial operator can be experimentally altered but source, receiver,
first/center/last derivative and full original Nt remain frozen.
No truncation of modes unstable at original dt is ever permitted.
"""
from __future__ import annotations

import numpy as np

def original_native_leapfrog_full_modal(
    lam_per_s2:np.ndarray, coupling:np.ndarray, dt_s:float,
    native_Nt:int, c_m_s:float=343.2, rho:float=1.2,
    frequencies_hz:tuple[float,float]=(40.,80.),
)->np.ndarray:
    """Exact q0 leapfrog finite original-record pressure spectrum.

    Every mode starts phi0=0, phi1=dt²c²*coupling. After the impulse:
    phi[n+1] = (2-dt² lambda)*phi[n] - phi[n-1].
    p0 = rho*(-3phi0+4phi1-phi2)/(2dt);
    p[n] = rho*(phi[n+1]-phi[n-1])/(2dt);
    p[N-1] = rho*(3phi[N-1]-4phi[N-2]+phi[N-3])/(2dt).

    Sign convention matches the previously frozen spectral Newmark
    pressure benchmark (all comparisons use the same native sign).
    """
    lam=np.asarray(lam_per_s2,dtype=np.float64).ravel()
    amp=np.asarray(coupling,dtype=np.float64).ravel()
    if (len(lam)==0 or lam.shape!=amp.shape or not np.isfinite(lam).all()
        or not np.isfinite(amp).all() or dt_s<=0 or native_Nt<3
        or native_Nt>2000 or c_m_s<=0 or rho<=0
        or float(np.min(lam))<-1e-7):
        raise ValueError("invalid original q0 leapfrog complete eigenmode/clock")
    theta2=np.maximum(lam,0.)*dt_s*dt_s
    if np.max(theta2)>=4.:
        raise ValueError("ORIGINAL_NATIVE_LEAPFROG_UNSTABLE_CFL: cannot drop modes")
    theta=2.*np.arcsin(.5*np.sqrt(theta2))
    q=(rho/dt_s)*(dt_s*dt_s*c_m_s*c_m_s)*amp
    cosine=np.cos(theta)
    def ratio(m):
        return m*np.sinc(m*theta/np.pi)/np.sinc(theta/np.pi)
    def progression(m,beta):
        return (m*np.exp(.5j*(m+1)*beta)*np.sinc(m*beta/(2*np.pi))/
                np.sinc(beta/(2*np.pi)))
    endpoint=ratio(native_Nt-1)+(cosine-2)*ratio(native_Nt-2)
    result=np.empty((2,len(theta)),dtype=np.complex128)
    for i,frequency in enumerate(frequencies_hz):
        k=2*np.pi*frequency*dt_s
        interior=.5*(progression(native_Nt-2,k+theta)+
                        progression(native_Nt-2,k-theta))
        result[i]=q*((2-cosine)+interior+
                     np.exp(1j*k*(native_Nt-1))*endpoint)
    if not np.isfinite(result).all():
        raise ValueError("nonfinite stable native leapfrog transfer")
    return result
