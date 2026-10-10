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
    if not np.isfinite(effective).all():raise ValueError('effective spectrum overflow')
    omega=np.sqrt(effective)
    z=omega*dt
    if not np.isfinite(z).all():raise ValueError('modal phase overflow')
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
    # Bound the temporary mode-by-time matrix to 32 MiB; never discard modes.
    chunk=min(chunk,max(1,(32*1024*1024)//(8*nt)))
    if nt>1_000_000:raise ValueError('record exceeds bounded runtime allocation')
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
    if type(nt) is not int or nt<3:raise ValueError('valid original record required')
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


def original_pressure_endpoint_audit(phi,dt,frequencies_hz=(40.,80.),*,rho=1.2):
    """Exact discrete summation by parts, with the original observer unchanged.

    Separate its rectangular bulk term from the first/last gradient stencils.
    These components are signed; cancellation ratios are diagnostics, not
    error estimates, and no endpoint term is removed from the result.
    """
    phi=np.asarray(phi,float);f=np.asarray(frequencies_hz,float)
    if (phi.ndim!=1 or len(phi)<6 or not np.isfinite(phi).all() or f.ndim!=1
        or not len(f) or not np.isfinite(f).all() or min(f)<=0
        or not np.isfinite([dt,rho]).all() or min(dt,rho)<=0):
        raise ValueError('finite full record and positive observer parameters required')
    nt=len(phi);t=np.arange(nt)*dt;rows=[]
    pressure=rho*np.gradient(phi,dt,edge_order=2)
    for freq in f:
        delta=2*np.pi*freq*dt;e=np.exp(1j*delta*np.arange(nt))
        weight=np.zeros(nt,dtype=complex)
        weight[:3]+=e[0]*np.array([-1.5,2.,-.5])/dt
        weight[-3:]+=e[-1]*np.array([.5,-2.,1.5])/dt
        weight[:-2]-=.5*e[1:-1]/dt;weight[2:]+=.5*e[1:-1]/dt
        bulk_weight=-1j*np.sin(delta)/dt*e
        correction=weight-bulk_weight
        if np.max(abs(correction[3:-3]),initial=0)>1e-8/dt:
            raise RuntimeError('observer boundary decomposition lost identity')
        bulk=rho*np.dot(bulk_weight,phi)
        start=rho*np.dot(correction[:3],phi[:3]);end=rho*np.dot(correction[-3:],phi[-3:])
        direct=np.dot(e,pressure);reconstructed=bulk+start+end
        scale=max(abs(direct),abs(bulk)+abs(start)+abs(end),1.)
        if abs(reconstructed-direct)>2e-11*scale:raise RuntimeError('pressure summation by parts failed')
        pair=lambda z:[float(z.real),float(z.imag)]
        rows.append({'frequency_hz':float(freq),'total':pair(direct),'bulk':pair(bulk),
            'initial_stencils':pair(start),'final_stencils':pair(end),
            'reconstruction_error_relative_to_component_scale':float(abs(reconstructed-direct)/scale),
            'cancellation_ratio':float((abs(bulk)+abs(start)+abs(end))/abs(direct)) if abs(direct)>0 else None,
            'endpoint_terms_retained':True})
    return {'last_sample_s':float(t[-1]),'frequencies':rows,'changes_observer':False,
            'interpretation':'signed decomposition only; neither cancellation nor conservation proves convergence'}
