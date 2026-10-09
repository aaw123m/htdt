"""Exact-continuous-time causal 3D semidiscrete impulse, ORIGINAL native samples.

Experimental time integrator: true physical Q1 semidiscrete Neumann wave
M phi_tt+K phi=c²*s*q(t), with q(t)=dt*delta(t) at original sample 0.
Semidiscrete eigenmode φ[n]=c²dt*s_e*sin(ωe*n*dt)/ωe.
The rigid zero-mode has the exact continuous-time limit c²dt²*s_e*n.
Its first-sample φ[1] differs from the *prior* implicit Newmark update at
high frequencies, a disclosed change of experiment time integrator, NOT
the original upstream PFFDTD solver.

For the ORIGINAL sampled pressure observation p_n=rho*finite-difference
φ_n, the interior modal multiplier sinc(ωdt)=sin(ωdt)/(ωdt) is the
mathematically exact sampled centered-derivative multiplier. No high
frequency modes are cut, tuned, damped, shifted or masked.
"""
from __future__ import annotations

import numpy as np

from .r130d_causal_prefirst_weak import compact_odd_witness
from .r130d_causal_first_roof_echo import (
    ROOF_CENTER_S,ROOF_RADIUS_S,ROOF_WIDTHS_S)


def _geometric_positive_frequency_progression(
    number_terms:int,beta:np.ndarray,
)->np.ndarray:
    """Σ_{n=1}^m exp(i*n*beta), stable at aliases beta=2kπ.

    First reduce angles to one period before sine division; do not discard
    any eigenspectrum contributions even when their sampled θ aliases.
    """
    b=np.asarray(beta,dtype=float)
    if number_terms<1 or not np.isfinite(b).all():
        raise ValueError("original entire native q0 harmonic series invalid")
    period=(b+np.pi)%(2*np.pi)-np.pi
    half=period/2
    den=np.sin(half)
    near=np.abs(den)<1e-10
    ratio=np.empty_like(half)
    ratio[near]=number_terms
    ratio[~near]=np.sin(number_terms*half[~near])/den[~near]
    return np.exp(1j*(number_terms+1)*half)*ratio


def exact_continuous_modal_delta_impulse_signed_original_250ms(
    eigenvalues_3d:np.ndarray,
    original_source_receiver_modal_couplings:np.ndarray,
    dt_s:float,Nt:int, *,
    frequencies_hz:tuple[float,float]=(40.,80.),
    c_m_s:float=343.2,rho_kg_m3:float=1.2,
)->np.ndarray:
    """FULL original finite native rectangular pressure DTFT, 2×ALLMODES.

    Equivalent to independent brute-force native physical phi_n followed
    by ORIGINAL pressure one-sided boundaries/centered interior,
    P_T/Q_T when q[0]=1 and all q[n>0]=0.
    """
    lam=np.asarray(eigenvalues_3d,dtype=float).ravel()
    cp=np.asarray(original_source_receiver_modal_couplings,dtype=float).ravel()
    freqs=np.asarray(frequencies_hz,dtype=float)
    if (lam.shape!=cp.shape or not len(lam)
        or not np.isfinite(lam).all() or not np.isfinite(cp).all()
        or np.min(lam)<-5e-7 or dt_s<=0 or Nt<8 or Nt>3000
        or freqs.shape!=(2,) or not np.array_equal(freqs,[40.,80.])
        or c_m_s!=343.2 or rho_kg_m3!=1.2):
        raise ValueError("original q0 exact continuum-time full record signed experiment changed")
    theta=dt_s*np.sqrt(np.maximum(lam,0.))
    sinc=np.sinc(theta/np.pi)
    A=(c_m_s*dt_s)**2*cp
    pref=(rho_kg_m3/dt_s)*A
    theta_cos=np.cos(theta)
    first=sinc*(2-theta_cos)
    # At θ=0 limit of endpoint expression =1.
    end_numerator=(3*np.sin((Nt-1)*theta)-
                   4*np.sin((Nt-2)*theta)+
                   np.sin((Nt-3)*theta))
    end=np.divide(end_numerator,2*theta,
                  out=np.ones_like(theta),where=np.abs(theta)>1e-9)
    # Numerically stable alternative for tiny θ (zero Neumann limit).
    tiny=np.abs(theta)<=1e-9
    if np.any(tiny):
        end[tiny]=1.
    values=np.empty((2,len(lam)),dtype=np.complex128)
    for i,f in enumerate(freqs):
        wd=2*np.pi*f*dt_s
        plus=_geometric_positive_frequency_progression(Nt-2,wd+theta)
        minus=_geometric_positive_frequency_progression(Nt-2,wd-theta)
        freq_sum=first+.5*sinc*(plus+minus)+(
            np.exp(1j*wd*(Nt-1))*end)
        values[i]=pref*freq_sum
    if not np.isfinite(values).all():
        raise ValueError("all native original true physical Q1 modes exact-time finite transfer nonfinite")
    return values


def exact_continuous_modal_delta_impulse_roof_weak(
    eigenvalues_3d:np.ndarray,
    original_source_receiver_modal_couplings:np.ndarray,
    dt_s:float,Nt:int, *,
    c_m_s:float=343.2,rho_kg_m3:float=1.2,
)->list[dict]:
    """Previously frozen short-time first true roof weak test, no mode cuts."""
    lam=np.asarray(eigenvalues_3d,dtype=float).ravel()
    cp=np.asarray(original_source_receiver_modal_couplings,dtype=float).ravel()
    if (lam.shape!=cp.shape or not len(lam)
        or np.min(lam)<-5e-7 or not np.isfinite(lam).all()
        or not np.isfinite(cp).all() or dt_s<=0 or Nt<10
        or c_m_s!=343.2 or rho_kg_m3!=1.2):
        raise ValueError("exact-time original native first true roof Green weak invalid")
    theta=dt_s*np.sqrt(np.maximum(lam,0))
    sinc=np.sinc(theta/np.pi)
    modal_native=(rho_kg_m3*c_m_s*c_m_s*dt_s)*cp*sinc
    times=np.arange(Nt,dtype=float)*dt_s
    indices=np.flatnonzero((times>=ROOF_CENTER_S-ROOF_RADIUS_S)
                           &(times<=ROOF_CENTER_S+ROOF_RADIUS_S))
    if len(indices)<8 or indices[0]<1 or indices[-1]>=Nt-1:
        raise ValueError("original native roof witness not contained in sample interior")
    pressure=np.empty(len(indices),dtype=float)
    for j,n in enumerate(indices):
        pressure[j]=np.dot(modal_native,np.cos(theta*n))
    output=[]
    for width in ROOF_WIDTHS_S:
        w=compact_odd_witness(times[indices],width,
            center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)
        output.append({
            "physical_causal_roof_witness_width_s":float(width),
            "true_original_native_exact_time_allmode_roof_window_weak":float(pressure@w),
            "all_physical_3d_generalized_modes_retained":int(len(lam)),
            "time_sampling_and_physical_witness_unchanged":True,
            "mathematically_exact_time_delta_q0_no_damping_fit_or_highmode_cut":True,
            "actual_original_native_window_sample_count":int(len(indices))})
    return output
