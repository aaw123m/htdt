"""Exact modal frequency-band and finite-record pressure endpoint attribution.

Diagnostics are ADDITIVE partitions of the SAME original full 250ms q0
transfer, never modified acceptance output, source/receiver or modal content.
The first/last are the original 2nd-order one-sided pressure derivatives.
"""
from __future__ import annotations
import numpy as np

BAND_EDGES_HZ=(0.,100.,200.,400.,800.,1600.,3200.,float("inf"))
BAND_TITLES=("0-100","100-200","200-400","400-800",
             "800-1600","1600-3200","3200-infinity")
ENDPOINT_NAMES=("first_forward_derivative_sample_n0",
                "interior_centered_derivative_samples_n1_to_N_minus_2",
                "last_backward_derivative_sample_nN_minus_1")
FREQ_HZ=(40.,80.)


def _progression(m:int,beta:np.ndarray)->np.ndarray:
    # sum_{n=1}^m exp(+i*n*beta); also correct in exact zero-mode limit
    return (m*np.exp(.5j*(m+1)*beta)*np.sinc(m*beta/(2*np.pi))/
            np.sinc(beta/(2*np.pi)))


def _sin_ratio(m:int,theta:np.ndarray)->np.ndarray:
    return m*np.sinc(m*theta/np.pi)/np.sinc(theta/np.pi)


def original_q0_full_modal_endpoint_terms(
    eigenvalue_per_s2:np.ndarray, native_newmark_kick:np.ndarray,
    native_dt_s:float,native_Nt:int,rho_kg_m3:float=1.2,
)->np.ndarray:
    """Complex terms [2 frequency, 3 endpoint part, all 3D modes].

    Analytic exactly matches original Newmark spectral pressure convention:
    phi[0]=0, phi[n]=kick*sin(n theta)/sin(theta), n>=1;
    theta=2 atan(dt sqrt(lambda)/2). p[0] uses second-order
    forward derivative, p[1:-1] centered, p[-1] backward.
    Not a substitute for 250ms whole-record P/Q or a windowed result.
    """
    eigen=np.asarray(eigenvalue_per_s2,dtype=np.float64).ravel()
    kick=np.asarray(native_newmark_kick,dtype=np.float64).ravel()
    if (eigen.shape!=kick.shape or not len(eigen)
        or native_Nt<3 or native_Nt>2000
        or not np.isfinite(eigen).all() or not np.isfinite(kick).all()
        or float(eigen.min())<-5e-7
        or not np.isfinite(native_dt_s) or native_dt_s<=0
        or not np.isfinite(rho_kg_m3) or rho_kg_m3<=0):
        raise ValueError("invalid allmode original 250ms q0 native eigen/kick/clock")
    dt=float(native_dt_s)
    theta=2*np.arctan(.5*dt*np.sqrt(np.maximum(eigen,0.)))
    cosine=np.cos(theta)
    endpoint=_sin_ratio(native_Nt-1,theta)+(cosine-2)*_sin_ratio(native_Nt-2,theta)
    result=np.empty((2,3,len(eigen)),dtype=np.complex128)
    factor=(rho_kg_m3/dt)*kick
    for fi,freq in enumerate(FREQ_HZ):
        beta=2*np.pi*freq*dt
        mid=.5*(_progression(native_Nt-2,beta+theta)
               +_progression(native_Nt-2,beta-theta))
        result[fi,0]=factor*(2-cosine)
        result[fi,1]=factor*mid
        result[fi,2]=factor*np.exp(1j*beta*(native_Nt-1))*endpoint
    if not np.isfinite(result).all():
        raise ValueError("nonfinite exact modal endpoint partition")
    return result


def partition_original_q0_hybrid_allmodes(
    eigenvalue_per_s2:np.ndarray,native_newmark_kick:np.ndarray,
    native_dt_s:float,native_Nt:int,
)->dict:
    lam=np.asarray(eigenvalue_per_s2,dtype=float).ravel()
    terms=original_q0_full_modal_endpoint_terms(
        lam,native_newmark_kick,native_dt_s,native_Nt)
    freq=np.sqrt(np.maximum(lam,0.))/(2*np.pi)
    total=terms.sum(axis=(1,2))
    by_band=[]
    for lo,hi,title in zip(BAND_EDGES_HZ[:-1],BAND_EDGES_HZ[1:],BAND_TITLES):
        mask=(freq>=lo)&(freq<hi)
        part=terms[:,:,mask].sum(axis=2)
        by_band.append({
            "band_name":title,"frequency_low_hz":lo,
            "frequency_high_hz":None if np.isinf(hi) else hi,
            "retained_mode_count":int(mask.sum()),
            "signed_total_40_80":part.sum(axis=1),
            "signed_three_endpoint_components_40_80":part,
            "sum_modal_absolute_40_80":np.sum(abs(terms[:,:,mask].sum(axis=1)),axis=1),
        })
    nyquist=lam*native_dt_s**2>np.pi**2
    parts=[terms[:,:,~nyquist].sum(axis=(1,2)),
           terms[:,:,nyquist].sum(axis=(1,2))]
    endpoint=terms.sum(axis=2)
    from_bands=sum((b["signed_total_40_80"] for b in by_band),np.zeros(2,complex))
    from_parts=sum(parts,np.zeros(2,complex))
    scale=max(float(np.linalg.norm(total)),1.)
    if (sum(b["retained_mode_count"] for b in by_band)!=len(lam)
        or np.linalg.norm(from_bands-total)/scale>2e-8
        or np.linalg.norm(from_parts-total)/scale>2e-8
        or np.linalg.norm(endpoint.sum(axis=1)-total)/scale>2e-8):
        raise ValueError("all retained modes, exact band or endpoint partition lost")
    return {
        "full_signed_40_80":total,
        "endpoint_signed_40_80":endpoint,
        "bands":by_band,
        "native_nyquist_or_below_signed_40_80":parts[0],
        "native_above_nyquist_signed_40_80":parts[1],
        "native_nyquist_or_below_mode_count":int((~nyquist).sum()),
        "native_above_nyquist_mode_count":int(nyquist.sum()),
        "mode_count":len(lam),
        "full_mode_sum_abs_contributions_40_80":
            np.sum(abs(terms.sum(axis=1)),axis=1),
        "full_band_reconstruction_relative":float(np.linalg.norm(from_bands-total)/scale),
        "full_endpoint_reconstruction_relative":
            float(np.linalg.norm(endpoint.sum(axis=1)-total)/scale),
        "full_nyquist_partition_reconstruction_relative":
            float(np.linalg.norm(from_parts-total)/scale),
        "not_a_new_acceptance_or_filtered_solution":True,
    }


def signed_real_projection_on_full_delta(
    coarse:np.ndarray,fine:np.ndarray,
    coarse_terms:list[np.ndarray],fine_terms:list[np.ndarray]
)->np.ndarray:
    """Signed fractional contribution of each partition to total change.

    For each f, Σ_j Re(conj(D_f)*d_{j,f})/|D_f|² == 1 exactly,
    where D_f=T_coarse-T_fine. Negative fractions or >1 are legitimate.
    """
    c=np.asarray(coarse,dtype=complex).ravel()
    f=np.asarray(fine,dtype=complex).ravel()
    if (c.shape!=(2,) or f.shape!=(2,) or len(coarse_terms)!=len(fine_terms)
        or not np.all(np.isfinite(c+f)) or np.any(abs(c-f)<=1e-12)):
        raise ValueError("modal 40/80 signed difference cannot be attributed")
    delta=c-f
    d=np.asarray([np.asarray(a)-np.asarray(b)
                  for a,b in zip(coarse_terms,fine_terms)])
    if d.shape!=(len(coarse_terms),2):
        raise ValueError("invalid signed partition dimensions")
    if np.linalg.norm(d.sum(axis=0)-delta)>2e-8*max(1.,np.linalg.norm(delta)):
        raise ValueError("missing modal/endpoint difference terms")
    result=np.real(np.conj(delta)[None,:]*d)/abs(delta[None,:])**2
    if max(abs(result.sum(axis=0)-1))>2e-8:
        raise ValueError("signed projection does not sum to full difference")
    return result
