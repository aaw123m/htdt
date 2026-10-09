#!/usr/bin/env python3
"""Exact second moment of canonical PFFDTD eight-node interpolation, no wave run.

Reads immutable run76 original PPW8/10/12 evidence, not the changed Gaussian
candidate. Never changes original source/receiver, numerical gate or physics.
"""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCHEMA="htdt.r130d.pffdtd-trilinear-original-operator-effective-width-plan-1"
SOURCE_SHA="c95ad31aeedae3138a3141763c26cb2a03a44a9069aeabd2793e61cdb9de26d4"
PPW=(8,10,12)
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
        or p["immutable_source"]["raw_sha256"]!=SOURCE_SHA
        or p["grid"]["ppw"]!=list(PPW)
        or p["grid"]["source_xyz_m"]!=[1.5,2,2]
        or p["grid"]["receiver_xyz_m"]!=[2.5,2,2]
        or p["grid"]["fixed_reference_gaussian_sigma_m"]!=[0.35,0.7]
        or p["limits"]["max_levels"]!=3
        or p["formula"]["axis_variance_m2"]!="h*h*f*(1-f), with physical grid spacing h and fractional coordinate f"
        or p["authority"]["original_fullband_pffdtd"]!="SELF_CONVERGENCE_FAILED"
        or p["authority"]["production_ready"] is not False):
        raise ValueError("frozen canonical PFFDTD original operator width plan changed")
    return p

def operator_width(stencil,h,expected_xyz):
    f=stencil["fractional_cell_coordinate"]
    w=stencil["interpolation_weights"]
    if (len(f)!=3 or len(w)!=8 or not (h>0 and math.isfinite(h))
        or any(not math.isfinite(t) or t<0 or t>1 for t in f)
        or any(not math.isfinite(t) or t<0 for t in w)
        or abs(sum(w)-1)>1e-10
        or not all(math.isclose(float(a),float(b),rel_tol=0,abs_tol=1e-10)
                    for a,b in zip(stencil["reconstructed_coordinate_m"],expected_xyz))):
        raise ValueError("original PFFDTD native interpolation invalid")
    corners=list(itertools.product((0,1),repeat=3))
    weights=[math.prod((f[ax] if bit else 1-f[ax])
                       for ax,bit in enumerate(c)) for c in corners]
    if any(abs(a-b)>1e-10 for a,b in zip(sorted(weights),sorted(w))):
        raise ValueError("original eight-node weights are not trilinear")
    axis2=[h*h*v*(1-v) for v in f]
    direct2=[sum(wgt*(h*(c[axis]-f[axis]))**2
                  for c,wgt in zip(corners,weights))
             for axis in range(3)]
    if any(abs(a-b)>1e-12 for a,b in zip(axis2,direct2)):
        raise ValueError("native discrete weighted second moment inconsistent")
    return {"fractional_cell_coordinate":f,
            "source_weight_sum":float(sum(w)),
            "stencil_sha256":stencil["stencil_sha256"],
            "axis_variance_m2":axis2,
            "axis_effective_sigma_m":[math.sqrt(v) for v in axis2],
            "rms_radius_m":math.sqrt(sum(axis2)),
            "trilinear_stencil_weight_shape_verified":True}
def analyze(plan,original):
    if (original["decision"]["canonical_pffdtd_self_convergence"]!="SELF_CONVERGENCE_FAILED"
        or original["decision"]["canonical_reference_self_convergence"]!="SELF_CONVERGENCE_FAILED"
        or original["decision"]["general_3d_validation_state"]!="NOT_VALIDATED"
        or original["interpolation_stencil"]["solver_semantics_changed"] is not False):
        raise ValueError("original run76 authority changed")
    geometries=original["spatial_representation"]["levels"]
    stencils=original["interpolation_stencil"]["levels"]
    if (len(geometries)!=3 or len(stencils)!=3
        or [z["points_per_wavelength"] for z in geometries]!=list(PPW)
        or [z["points_per_wavelength"] for z in stencils]!=list(PPW)):
        raise ValueError("missing PPW8/10/12 canonical eight-node stencils")
    rows=[]
    for geometry,level in zip(geometries,stencils):
        h=float(geometry["grid_spacing_m"])
        ppw=level["points_per_wavelength"]
        source=operator_width(level["source"],h,plan["grid"]["source_xyz_m"])
        receiver=operator_width(level["receiver"],h,plan["grid"]["receiver_xyz_m"])
        rows.append({
            "ppw":ppw,"physical_grid_spacing_m":h,
            "original_source":source,"original_receiver":receiver,
            "fixed_gaussian_reference_rms_radius_m":{
                str(sig):math.sqrt(3)*sig for sig in (0.35,0.7)},
            "canonical_operator_is_not_fixed_physical_width":True,
            "original_evidence_volume_relative_error":geometry["relative_volume_error"]})
        print("CANONICAL_PFFDTD",ppw,"h",h,
              "source_rms",source["rms_radius_m"],
              "receiver_rms",receiver["rms_radius_m"],
              "air_volume_error",geometry["relative_volume_error"],flush=True)
    def trends(name):
        values=[q[name]["rms_radius_m"] for q in rows]
        return {"values_m":values,"strictly_decreasing":all(a>b for a,b in zip(values,values[1:])),
                "last_over_first":values[-1]/values[0],
                "all_levels_retained":True}
    return {"levels":rows,"source_trend":trends("original_source"),
            "receiver_trend":trends("original_receiver")}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    original_file=ROOT/p["immutable_source"]["path"]
    original_bytes=original_file.read_bytes()
    if hashlib.sha256(original_bytes).hexdigest()!=SOURCE_SHA:
        raise ValueError("independent preobserved PFFDTD original source changed")
    original=json.loads(original_bytes)
    result={"schema_version":"htdt.r130d.original-pffdtd-eight-node-width-evidence-1",
            "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
            "original_run76_sha256":SOURCE_SHA,
            "preregistered_plan":p,**analyze(p,original),
            "canonical_pffdtd_fullband":"SELF_CONVERGENCE_FAILED",
            "canonical_original_point_impulse":"NOT_QUALIFIED",
            "physical_validation":"NOT_VALIDATED",
            "changed_solver_or_original_stencils":False,
            "production_ready":False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("SAVED original PFFDTD effective source/receiver widths",a.output,flush=True)
if __name__=="__main__":
    main()
