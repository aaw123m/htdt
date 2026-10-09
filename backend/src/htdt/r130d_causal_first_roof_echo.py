"""Physical true planar Neumann first roof echo vs original native q0 wave.

Analytic +1 rigid Neumann image-source reflection at z=4-.25*y.
Its finite specular foot must be on the actual finite true sloping roof.
Original 64 native point pairs evaluated, with exact physical source weights.
This only characterizes the first single roof echo in a finite causal window,
NOT the whole original rigid room 250ms Green or source bandwidth.
Numerical native pressure may contain the dispersive direct pulse tail;
no unsupported exact single-image decomposition claim.
"""
from __future__ import annotations

import numpy as np

from .r130d_causal_prefirst_weak import compact_odd_witness
from .r130d_retarded_point_green import (
    C,RHO,ROOM_WALLS,true_wall_specular_reflection)

ROOF_CENTER_S=.0089668767033651
ROOF_RADIUS_S=.0011
ROOF_WIDTHS_S=(.00035,.0006,.00085)
ROOF_END_S=ROOF_CENTER_S+ROOF_RADIUS_S


def analytic_native_64point_physical_roof_echo_weak(
    src_nodes_m:np.ndarray,src_weights:np.ndarray,
    rec_nodes_m:np.ndarray,rec_weights:np.ndarray,
    *,width_s:float,
)->dict:
    src=np.asarray(src_nodes_m,dtype=float)
    rec=np.asarray(rec_nodes_m,dtype=float)
    sw=np.asarray(src_weights,dtype=float)
    rw=np.asarray(rec_weights,dtype=float)
    if (src.shape!=(8,3) or rec.shape!=(8,3)
        or sw.shape!=(8,) or rw.shape!=(8,)
        or not np.isfinite(src).all() or not np.isfinite(rec).all()
        or not np.isfinite(sw).all() or not np.isfinite(rw).all()
        or abs(sw.sum()-1)>1e-12 or abs(rw.sum()-1)>1e-12
        or width_s not in ROOF_WIDTHS_S):
        raise ValueError("true original q0 64point Neumann roof source/witness changed")
    rook=next(w for w in ROOM_WALLS if w.name=="true_sloping_roof")
    weighted_roof=0.
    weighted_direct=0.
    nvalid=0
    first_nonroof=float("inf")
    first_nonroof_name=None
    first_roof=float("inf")
    latest_roof=0.
    roof_signed64=[]
    for i in range(8):
        for j in range(8):
            original_factor=float(sw[i]*rw[j])
            d_direct=float(np.linalg.norm(src[i]-rec[j]))
            if d_direct<=0:
                raise ValueError("physical native original eightpoint Green singular collocated nodes")
            direct_dtest=float(compact_odd_witness(
                np.array([d_direct/C]),width_s,
                derivative=True,center_s=ROOF_CENTER_S,
                radius_s=ROOF_RADIUS_S)[0])
            weighted_direct+=original_factor*direct_dtest/(4*np.pi*d_direct)
            for wall in ROOM_WALLS:
                v=true_wall_specular_reflection(src[i],rec[j],wall)
                if not v["reflecting_point_on_true_finite_room_wall"]:
                    continue
                arrival=v["travel_time_s"]
                if wall.name=="true_sloping_roof":
                    nvalid+=1
                    first_roof=min(first_roof,arrival)
                    latest_roof=max(latest_roof,arrival)
                    witness_prime=float(compact_odd_witness(
                        np.array([arrival]),width_s,derivative=True,
                        center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)[0])
                    component=(-RHO*original_factor*witness_prime/
                               (4*np.pi*v["single_bounce_path_m"]))
                    weighted_roof+=component
                    roof_signed64.append(component)
                else:
                    if arrival<first_nonroof:
                        first_nonroof=arrival
                        first_nonroof_name=wall.name
    if nvalid<1 or not np.isfinite(weighted_roof):
        raise ValueError("physical finite sloped roof was not causally visible")
    if first_nonroof<=ROOF_END_S:
        raise ValueError("original 64pair nonroof earliest true wall reflection arrives inside roof-only witness")
    if abs(weighted_direct)>1e-14:
        raise ValueError("analytic original source-to-receiver direct delta not separated from roof witness")
    source=np.array([1.5,2,2])
    receiver=np.array([2.5,2,2])
    v=true_wall_specular_reflection(source,receiver,rook)
    if (not v["reflecting_point_on_true_finite_room_wall"]
        or abs(v["travel_time_s"]-ROOF_CENTER_S)>3e-15):
        raise ValueError("true roof specular reflection center changed from frozen physical geometry")
    true_prime=float(compact_odd_witness(
        np.array([ROOF_CENTER_S]),width_s,derivative=True,
        center_s=ROOF_CENTER_S,radius_s=ROOF_RADIUS_S)[0])
    physical_true_point_roof=-RHO*true_prime/(4*np.pi*v["single_bounce_path_m"])
    if abs(physical_true_point_roof)<1e-10 or abs(weighted_roof)<1e-10:
        raise ValueError("true Neumann roof first echo physically vanishes under frozen witness")
    return {
        "original_64pair_finite_roof_single_bounce_signed_weak_analytic":float(weighted_roof),
        "true_physical_point_roof_single_bounce_signed_weak_analytic":float(physical_true_point_roof),
        "all_64_native_direct_wave_analytic_contribution_within_roof_window":float(-RHO*weighted_direct),
        "physical_reflection_sign":+1,
        "true_finite_roof_valid_original_native_64_pair_count":nvalid,
        "earliest_actual_original_8node_roof_single_echo_s":first_roof,
        "latest_actual_original_8node_roof_single_echo_s":latest_roof,
        "earliest_actual_original_nonroof_single_bounce_s":first_nonroof,
        "earliest_actual_original_nonroof_single_bounce_wall":first_nonroof_name,
        "roof_echo_only_analytic_causality_NOT_native_pulse_decomposition":True,
        "pre_registered_physical_witness_center_s":ROOF_CENTER_S,
        "pre_registered_physical_witness_sigma_s":width_s,
        "pre_registered_physical_witness_upper_support_s":ROOF_END_S,
        "original_source_receiver_8node_weights_unmodified":True}
