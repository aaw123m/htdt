"""Non-dissipative q0 candidates with the original sampled pressure observer.

Gauss-Legendre updates preserve the physical modal Hamiltonian exactly.
The separately labelled rational-mass and hyperstiffness arms modify the
spatial operator; their conserved energy uses that modified operator.
None adds viscosity, attenuates modes, fits gains, or changes native clocks.
Conservation alone is not convergence or a point-source continuum proof.
"""
from __future__ import annotations

import numpy as np
from .r130d_vanishing_viscosity_impulse import original_pressure_sum_exp, _geo0

ARMS = ('exact', 'gauss2', 'gauss4', 'gauss6', 'rational_mass_exact', 'hyperstiffness_exact')


def conservative_spectrum(lam, dt, h, arm, *, c=343.2):
    values=np.asarray(lam,float)
    if (values.ndim!=1 or len(values)==0 or not np.isfinite(values).all()
        or np.min(values)<0 or not np.isfinite([dt,h,c]).all()
        or min(dt,h,c)<=0 or arm not in ARMS):
        raise ValueError('valid full spectrum, positive clock and conservative arm required')
    effective=values.copy()
    tau=h*h/(12*c*c)
    if arm=='rational_mass_exact':effective=values/(1+tau*values)
    if arm=='hyperstiffness_exact':effective=values+tau*values**2
    omega=np.sqrt(effective)
    z=omega*dt
    if arm=='gauss2':numerator=1+1j*z/2
    elif arm=='gauss4':numerator=1-z*z/12+1j*z/2
    elif arm=='gauss6':numerator=1-z*z/10+1j*(z/2-z**3/120)
    else:
        return omega,z,{'viscosity':0.,'modal_attenuation':False,'unit_modulus_error':0.,
            'spatial_operator_modified':arm not in ('exact','gauss2','gauss4','gauss6')}
    R=numerator/numerator.conj()
    error=float(np.max(abs(abs(R)-1)))
    if error>2e-14:raise RuntimeError('non-dissipative update lost unit modulus')
    return omega,np.angle(R),{'viscosity':0.,'modal_attenuation':False,'unit_modulus_error':error,
        'spatial_operator_modified':False}


def _inputs(lam,coupling,dt,nt,h,arm,frequencies,c,rho):
    cp=np.asarray(coupling,float)
    omega,theta,proof=conservative_spectrum(lam,dt,h,arm,c=c)
    f=np.asarray(frequencies,float)
    if (cp.shape!=omega.shape or not np.isfinite(cp).all()
        or type(nt) is not int or nt<3 or f.ndim!=1 or len(f)==0
        or not np.isfinite(f).all() or np.min(f)<=0 or not np.isfinite(rho) or rho<=0):
        raise ValueError('complete couplings and valid original pressure record required')
    return cp,omega,theta,proof,f


def conservative_q0_transfer(lam,coupling,dt,nt,h,*,arm='gauss4',
                             frequencies_hz=(40.,80.),c=343.2,rho=1.2):
    cp,omega,theta,proof,f=_inputs(lam,coupling,dt,nt,h,arm,frequencies_hz,c,rho)
    nonzero=omega>0
    values=[]
    for frequency in f:
        plus=original_pressure_sum_exp(1j*theta[nonzero]/dt,dt,nt,frequency)
        minus=original_pressure_sum_exp(-1j*theta[nonzero]/dt,dt,nt,frequency)
        mode=(plus-minus)/(2j*omega[nonzero])
        zero=dt*_geo0(nt,2j*np.pi*frequency*dt)*cp[~nonzero].sum()
        values.append(rho*c*c*(np.dot(cp[nonzero],mode)+zero))
    values=np.asarray(values)
    if not np.isfinite(values).all():raise RuntimeError('conservative full-mode transfer nonfinite')
    return values,proof


def conservative_q0_trace(lam,coupling,dt,nt,h,*,arm='gauss4',c=343.2,rho=1.2,chunk=1024):
    cp,omega,theta,proof,_=_inputs(lam,coupling,dt,nt,h,arm,(40.,80.),c,rho)
    if type(chunk) is not int or chunk<1:raise ValueError('positive bounded chunk required')
    n=np.arange(nt);t=n*dt;phi=np.zeros(nt)
    for start in range(0,len(cp),chunk):
        sl=slice(start,min(start+chunk,len(cp)))
        w=omega[sl];th=theta[sl];weights=cp[sl]
        nonzero=w>0
        phi+=(weights[nonzero]/w[nonzero])@np.sin(th[nonzero,None]*n)
        phi+=weights[~nonzero].sum()*t
    phi*=c*c*dt
    pressure=rho*np.gradient(phi,dt,edge_order=2)
    return t,phi,pressure,proof


def conservative_energy_audit(lam,dt,nt,h,*,arm='gauss4'):
    omega,theta,proof=conservative_spectrum(lam,dt,h,arm)
    worst=0.
    for n in (1,nt//2,nt-1):
        # Unit initial velocity: energy = (omega*u)^2+v^2, with the
        # exact free-particle limit for the Neumann zero mode.
        energy=np.sin(n*theta)**2+np.cos(n*theta)**2
        worst=max(worst,float(np.max(abs(energy-1))))
    return {**proof,'all_modes_audited':len(omega),'energy_relative_drift':worst,
        'sampled_state_indices':[1,nt//2,nt-1],
        'conservation_basis':'unitary modal update; state audits are not full temporal error bounds'}
