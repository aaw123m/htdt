"""Causal distributional pre-first-reflection witness for ORIGINAL q0 wave.

An auxiliary C∞ compact test functional; never truncate original 250ms P/Q
nor filter, taper, smooth, rescale or modify the native wave/source itself.
Analytic conditional continuum amplitude follows original source-update
convention φ_native[0]≈(c dt)^2/h^3 * q0 at a Cartesian node, hence
∫φ weak dt≈c² dt * G and p=ρ φ̇. The native q0=[1,0,...] normalization
Q_T=dt gives W=Σ p[n]w(t[n]); analytic continuum conditional W_ref
=−ρ c² Σ_i,j s_i r_j w'(r_ij/c)/(4πr_ij).
A mismatch in this conditional mapping is reported, never fitted away.
"""
from __future__ import annotations

import numpy as np

from .r130d_retarded_point_green import C,RHO,ROOM_WALLS,true_wall_specular_reflection

TAU=1./C
HALF_SUPPORT_S=.0032
WIDTHS_S=(.0005,.0008,.0012)


def compact_odd_witness(
    times_s:np.ndarray,
    width_s:float, *, derivative:bool=False,
    center_s:float=TAU,radius_s:float=HALF_SUPPORT_S,
)->np.ndarray:
    """Smooth odd x/sigma Gaussian times C∞ compact cutoff on |x|<R.

    Its exact derivative at x=0 is +1/sigma. It and all derivatives are
    mathematically zero outside support; the center/width are *physical*
    times and MUST NOT be shifted/fitted using actual numerical wave peaks.
    """
    t=np.asarray(times_s,dtype=float)
    if (t.ndim<1 or not np.isfinite(t).all()
        or width_s<=0 or radius_s<=0 or center_s<0
        or not np.isfinite(width_s+radius_s+center_s)):
        raise ValueError("registered physical causal weak witness is invalid")
    x=t-center_s
    inside=np.abs(x)<radius_s
    out=np.zeros_like(x,dtype=float)
    if not np.any(inside):return out
    xi=x[inside]
    u=xi/radius_s
    bump=np.exp(1.-1./(1.-u*u))
    envelope=np.exp(-.5*(xi/width_s)**2)*bump
    if derivative:
        # derivative x * exp(-x²/(2σ²))*exp(1−1/(1−(x/R)²))
        derivative_factor=1-(xi/width_s)**2-2*xi*xi/(
            radius_s*radius_s*(1-u*u)**2)
        out[inside]=(envelope/width_s)*derivative_factor
    else:
        out[inside]=(xi/width_s)*envelope
    if not np.isfinite(out).all():
        raise ValueError("nonfinite original q0 independent analytic witness")
    return out


def native_original_wave_weak_transfer(
    original_velocity_potential_8byNt:np.ndarray,
    original_receiver_8_coefficients:np.ndarray,
    *,dt_s:float,width_s:float,
    density_kg_m3:float=RHO,center_s:float=TAU,
    radius_s:float=HALF_SUPPORT_S,
)->dict:
    """Actual ORIGINAL 8node raw u_out full 250ms, untouched spatial weights."""
    from .acoustic_pffdtd_adapter import (
        pffdtd_velocity_potential_to_pressure_trace)
    u=np.asarray(original_velocity_potential_8byNt,dtype=float)
    rw=np.asarray(original_receiver_8_coefficients,dtype=float)
    if (u.ndim!=2 or u.shape[0]!=8 or u.shape[1]<4
        or rw.shape!=(8,) or abs(rw.sum()-1)>1e-12
        or not np.isfinite(u).all() or not np.isfinite(rw).all()
        or not np.isfinite(dt_s) or dt_s<=0):
        raise ValueError("original point q0 raw eight receiver trace invalid")
    nt=u.shape[1]
    t=np.arange(nt)*dt_s
    phi=rw@u
    p=pffdtd_velocity_potential_to_pressure_trace(
        phi,time_step_s=dt_s,density_kg_m3=density_kg_m3)
    witness=compact_odd_witness(t,width_s,center_s=center_s,radius_s=radius_s)
    if np.count_nonzero(witness)<8:
        raise ValueError("native unchanged q0 insufficient samples in fixed causal witness")
    # ratio [dt * sum(p_n w_n)] / [dt * sum(q_n)] for q0[0]=1.
    value=float(np.dot(p,witness))
    # Independent finite-propagation negative control: p(t) must vanish
    # before any direct path of the true physical original source/receiver.
    pre=t<=.0015
    premax=float(max(abs(p[pre]),default=0.))
    peak=float(np.max(abs(p)))
    early=(t>=center_s-radius_s)&(t<=center_s+radius_s)
    earliest_idx=int(np.flatnonzero(early)[np.argmax(abs(p[early]))])
    return {
        "original_full_native_u_P_250ms_nonmodified":True,
        "original_velocity_trace_Nt":int(nt),
        "native_receiver_point_p_peak_signed_pa":float(p[np.argmax(abs(p))]),
        "native_receiver_point_p_peak_abs_pa":peak,
        "native_original_direct_window_peak_signed_pa":float(p[earliest_idx]),
        "native_original_direct_window_peak_time_s":float(t[earliest_idx]),
        "negative_control_native_abs_p_before_1p5ms_pa":premax,
        "negative_control_before_1p5ms_over_all_250ms_abs_peak":premax/max(peak,1e-300),
        "original_q0_native_weak_pressure_over_unit_input":value,
        "physical_witness_nonzero_samples":int(np.count_nonzero(witness)),
        "physical_witness_center_s":center_s,
        "physical_witness_sigma_s":width_s,
        "physical_witness_support_end_s":center_s+radius_s}


def original_native_64node_analytic_weak_reference(
    source_nodes_m:np.ndarray,source_weights:np.ndarray,
    receiver_nodes_m:np.ndarray,receiver_weights:np.ndarray,
    *,width_s:float,c_m_s:float=C,rho:float=RHO,
)->dict:
    """Analytic exact original Cartesian 64 pair weak delta continuum direct."""
    src=np.asarray(source_nodes_m,dtype=float)
    rec=np.asarray(receiver_nodes_m,dtype=float)
    sw=np.asarray(source_weights,dtype=float)
    rw=np.asarray(receiver_weights,dtype=float)
    if (src.shape!=(8,3) or rec.shape!=(8,3)
        or sw.shape!=(8,) or rw.shape!=(8,)
        or not np.isfinite(src).all() or not np.isfinite(rec).all()
        or not np.isfinite(sw).all() or not np.isfinite(rw).all()
        or abs(float(sw.sum())-1)>1e-12
        or abs(float(rw.sum())-1)>1e-12):
        raise ValueError("unchanged original 8node source/receiver HDF5 analytic Green invalid")
    distance=np.linalg.norm(src[:,None,:]-rec[None,:,:],axis=-1)
    if min(distance.ravel())<=0:raise ValueError("original source and receiver collocated")
    timing=distance/c_m_s
    derivative=compact_odd_witness(
        timing.reshape(-1),width_s,derivative=True).reshape((8,8))
    contribution=sw[:,None]*rw[None,:]*derivative/(4*np.pi*distance)
    expected=-rho*c_m_s*c_m_s*float(np.sum(contribution))
    true_point=-rho*c_m_s*c_m_s*float(compact_odd_witness(
        np.array([TAU]),width_s,derivative=True)[0])/(4*np.pi)
    # All 64 original exact node-pair single reflections must reach AFTER
    # the actual compact witness support ends, otherwise first-bounce
    # contaminates the expected causal free-field-only continuum witness.
    min_bounce=float("inf")
    min_bounce_wall=None
    for i in range(8):
        for j in range(8):
            for wall in ROOM_WALLS:
                result=true_wall_specular_reflection(src[i],rec[j],wall)
                if result["reflecting_point_on_true_finite_room_wall"]:
                    if result["travel_time_s"]<min_bounce:
                        min_bounce=result["travel_time_s"]
                        min_bounce_wall=wall.name
    if not (min_bounce>TAU+HALF_SUPPORT_S):
        raise ValueError("native 64pair original roof first arrival precedes original fixed compact weak window")
    return {
        "original_64_native_pairs_continuum_causal_green_weak_distribution":expected,
        "true_physical_source_to_receiver_point_continuum_weak_distribution":true_point,
        "analytic_original_8node_over_true_point_weak_relative":float(
            abs(expected-true_point)/max(abs(true_point),1e-300)),
        "earliest_real_original_64node_single_wall_reflection_s":min_bounce,
        "earliest_real_original_64node_single_wall_reflection_name":min_bounce_wall,
        "analytic_all_64_original_pair_wave_derivative_witness_nontruncated":True}
