#!/usr/bin/env python3
"""Audits untouched actual PFFDTD native Neumann boundary graph, not FV or FEM."""
from __future__ import annotations
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import numpy as np
import h5py

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"/"src"))
from run_r130d_original_point_quadratic_pffdtd import PPW,PIN,file_hash

SCHEMA="htdt.r130d.original-pffdtd-neumann-graph-audit-plan-1"
DIRECTIONS=((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1))
def validate_plan(p):
    if (p.get("schema_version")!=SCHEMA
       or p["geometry_source"]["five_frozen_original_ppw"]!=list(PPW)
       or p["geometry_source"]["pin_upstream_sha"]!=PIN
       or p["geometry_source"]["source_position_m"]!=[1.5,2,2]
       or p["matrix"]["offset_order"]!=["+Ny*Nz","-Ny*Nz","+Nz","-Nz","+1","-1"]
       or p["gate"]["expected_symmetry_mismatch_count"]!=0
       or p["resource_limits"]["max_cases"]!=5
       or p["authority"]["original_8_node_fullband"]!="SELF_CONVERGENCE_FAILED"
       or p["authority"]["product"]!="NO_GO"):
        raise ValueError("original Neumann operator graph prospective plan drift")
    return p

def reconstruct_graph(p,vox,source):
    bn=np.asarray(vox["bn_ixyz"][...],dtype=np.int64)
    adj=np.asarray(vox["adj_bn"][...],dtype=np.int64)
    dims=tuple(int(vox[name][()]) for name in ("Nx","Ny","Nz"))
    nx,ny,nz=dims
    total=int(np.prod(dims))
    if (total>p["resource_limits"]["max_native_original_grid_cells"]
        or bn.size!=len(set(map(int,bn))) or adj.shape!=(len(bn),6)
        or np.any((adj!=0)&(adj!=1)) or np.any(bn<0) or np.any(bn>=total)):
        raise ValueError("original boundary mask/grid is inconsistent or exceeds ceiling")
    boundary={int(node):adj[j] for j,node in enumerate(bn)}
    axes=[np.asarray(vox[k][...],dtype=float) for k in ("xv","yv","zv")]
    if tuple(len(v) for v in axes)!=dims:raise ValueError("grid source coordinate axis size mismatch")
    center=tuple(int(np.argmin(abs(ax-float(z)))) for ax,z in zip(axes,source))
    start=int(np.ravel_multi_index(center,dims))
    if start in boundary:raise ValueError("source starts on a boundary node")
    dx=(ny*nz,-ny*nz,nz,-nz,1,-1)
    def native_edges(i):
        ix=i//(ny*nz);iy=(i//nz)%ny;iz=i%nz
        if not (1<=ix<nx-1 and 1<=iy<ny-1 and 1<=iz<nz-1):
            return []
        v=boundary.get(i)
        weights=[1]*6 if v is None else list(map(int,v))
        return [(k,i+dx[k]) for k in range(6) if weights[k]>0]
    def graph_edges(i):
        # PFFDTD flips boundary ghost index 0 / N-1 before the stencil;
        # these do not represent independent room-neighbor graph nodes.
        edges=[]
        for direction,j in native_edges(i):
            ix=j//(ny*nz);iy=(j//nz)%ny;iz=j%nz
            if 1<=ix<nx-1 and 1<=iy<ny-1 and 1<=iz<nz-1:
                edges.append((direction,j))
        return edges
    reached={start}; queue=deque([start])
    while queue:
        node=queue.popleft()
        for direction,j in graph_edges(node):
            if not (0<=j<total):raise ValueError("native graph index escapes allocated grid")
            if j not in reached:
                reached.add(j);queue.append(j)
                if len(reached)>p["resource_limits"]["max_graph_visited_nodes"]:
                    raise ValueError("source-reachable room graph exceeds ceiling")
    visited=sorted(reached)
    missing=[];ghost_count=0;total_edges=0;max_rowsum=0
    for i in visited:
        neighbors=graph_edges(i)
        raw_edges=native_edges(i)
        ghost_count+=len(raw_edges)-len(neighbors)
        total_edges+=len(neighbors)
        # Native row sum includes ghost values BEFORE halo reflection; all
        # native Cartesian interior/rigid rows annihilate a constant field.
        diag=-(int(boundary[i].sum()) if i in boundary else 6)
        max_rowsum=max(max_rowsum,abs(diag+len(raw_edges)))
        for direction,j in neighbors:
            # Undirected physics requires the reverse edge in the native stencil.
            opposite=direction^1
            if not any(k==opposite and v==i for k,v in graph_edges(j)):
                if len(missing)<50000:missing.append({"i":i,"j":j,"direction":direction})
    return dims,boundary,bn,adj,start,visited,total_edges,ghost_count,missing,max_rowsum,graph_edges

def native_action(u,dims,bn,adj):
    from fdtd.sim_fdtd import (
        nb_flip_halos,nb_stencil_air_cart,nb_stencil_bn_cart)
    # Must match the original native PFFDTD kernel ordering (halo then air
    # seven-point Laplacian then boundary adjacency override).
    uu=u.copy().reshape(dims)
    mask=np.zeros(dims,dtype=np.bool_)
    mask.flat[bn]=True
    lu=np.zeros(dims,dtype=np.float64)
    nb_flip_halos(uu)
    nb_stencil_air_cart(lu,uu,mask)
    nb_stencil_bn_cart(lu,uu,bn,adj)
    return uu.ravel(),lu.ravel()

def analyze_one(p,vox_path,expected_sha,ppw):
    if file_hash(vox_path)!=expected_sha:
        raise ValueError("the original raw PFFDTD boundary-mask HDF5 SHA changed")
    with h5py.File(vox_path,"r") as vox:
        (dims,boundary,bn,adj,start,visited,edges,ghost,missing,row_err,
         graph_edges)=reconstruct_graph(p,vox,p["geometry_source"]["source_position_m"])
        with h5py.File(vox_path,"r") as copy:
            mat=np.asarray(copy["mat_bn"][...],dtype=np.int64)
            h=float(copy["h"][()])
    if (not np.all(mat==-1)
        or not np.isclose(h,343.2/(100*ppw),rtol=0,atol=1e-12)):
        raise ValueError("native PFFDTD experiment is not the frozen all-rigid room")
    n=int(np.prod(dims))
    mask=np.zeros(n,dtype=np.bool_);mask[visited]=True
    rng=np.random.default_rng(938+ppw)
    u=np.zeros(n,dtype=np.float64)
    v=np.zeros(n,dtype=np.float64)
    u[visited]=rng.normal(size=len(visited))
    v[visited]=rng.normal(size=len(visited))
    u_with_halo,lu=native_action(u,dims,bn,adj)
    v_with_halo,lv=native_action(v,dims,bn,adj)
    skew1=float(np.dot(u[visited],lv[visited]))
    skew2=float(np.dot(v[visited],lu[visited]))
    skew_rel=abs(skew1-skew2)/max(abs(skew1),abs(skew2),1)
    checks=[]
    for i in visited[:p["resource_limits"]["max_sampled_row_checks"]]:
        node=boundary.get(i)
        diag=-(int(node.sum()) if node is not None else 6)
        # Original native Neumann row on actual updated/halo-flipped field.
        j=i//(dims[1]*dims[2]);k=(i//dims[2])%dims[1];l=i%dims[2]
        nn=(dims[1]*dims[2],-dims[1]*dims[2],dims[2],-dims[2],1,-1)
        row=sum((int(node[z]) if node is not None else 1)*v_with_halo[i+nn[z]]
            for z in range(6))+diag*v_with_halo[i]
        checks.append(abs(row-lv[i]))
    max_kernel_difference=max(checks) if checks else 0
    result={
        "ppw":ppw,
        "original_exact_voxel_sha256":expected_sha,
        "grid_dimensions":list(dims),"physical_spacing_m":h,
        "full_voxel_grid_nodes":n,
        "original_boundary_node_count":len(bn),
        "source_nearest_original_grid_linear_node":start,
        "source_connected_room_nodes":len(visited),
        "source_connected_boundary_nodes":sum(i in boundary for i in visited),
        "connected_non_ghost_directed_edges":edges,
        "native_halo_ghost_edge_count_excluded_from_room_graph":ghost,
        "unreciprocated_directed_room_edge_count":len(missing),
        "unreciprocated_directed_edge_examples":missing[:15],
        "max_native_constant_field_row_sum_abs":int(row_err),
        "max_original_numba_vs_independent_boundary_row_action_abs":float(max_kernel_difference),
        "native_operator_relative_bilinear_skew":skew_rel,
        "original_rigid_boundary_condition_unchanged":True,
        "any_operator_self_adjointness_defect":{
            "unreciprocated_room_edges":bool(missing),
            "nonzero_native_kernel_difference":bool(max_kernel_difference>1e-10),
            "nonzero_relative_bilinear_skew":bool(skew_rel>1e-10),
        },
    }
    print("ORIGINAL_NATIVE_NEUMANN_GRAPH",ppw,
         "room",len(visited),"boundary",result["source_connected_boundary_nodes"],
         "edge",edges,"unreciprocated",len(missing),"ghost",ghost,
         "kernel max error",max_kernel_difference,"relative skew",skew_rel,flush=True)
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan",type=Path,required=True)
    ap.add_argument("--original-sims-root",type=Path,required=True)
    ap.add_argument("--upstream",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    raw=a.plan.read_bytes()
    p=validate_plan(json.loads(raw))
    from run_r130d_original_point_quadratic_pffdtd import PIN
    upstream=subprocess.check_output(
        ["git","-C",str(a.upstream),"rev-parse","HEAD"],text=True).strip()
    if upstream!=PIN:raise ValueError("original PFFDTD update-kernel upstream changed")
    sys.path.insert(0,str(a.upstream/"python"))
    fixed=json.loads((ROOT/p["geometry_source"]["committed_prior_two_native_wave_evidence"]).read_text())
    cases={int(row["ppw"]):row for row in fixed["actual_native_wave_cases"]}
    if set(cases)!=set(PPW):raise ValueError("frozen original waveform/geometry set changed")
    voxels={}
    for path in a.original_sims_root.rglob("vox_out.h5"):
        sha=file_hash(path)
        matches=[ppw for ppw in PPW if sha==cases[ppw]["original_solver_geometry_sha256"]]
        if len(matches)==1 and matches[0] not in voxels:
            voxels[matches[0]]=path
    if set(voxels)!=set(PPW):raise ValueError("incomplete exact SHA-bound original voxel files")
    result={"schema_version":"htdt.r130d.native-pffdtd-neumann-graph-audit-evidence-1",
            "plan_sha256_lf":hashlib.sha256(raw.replace(b"\r\n",b"\n")).hexdigest(),
            "preregistered_plan":p,"pinned_unmodified_upstream":upstream,
            "actual_original_voxel_grid_audits":[]}
    for ppw in PPW:
        result["actual_original_voxel_grid_audits"].append(
            analyze_one(p,voxels[ppw],cases[ppw]["original_solver_geometry_sha256"],ppw))
        part=a.output.with_name(a.output.stem+"_partial.json")
        part.parent.mkdir(parents=True,exist_ok=True)
        part.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    rows=result["actual_original_voxel_grid_audits"]
    result.update({"all_five_native_original_room_graphs_have_zero_unreciprocated_edges":
                      all(row["unreciprocated_directed_room_edge_count"]==0 for row in rows),
                   "all_five_native_kernel_probe_reconstructions_match":
                      all(row["max_original_numba_vs_independent_boundary_row_action_abs"]<=1e-10 for row in rows),
                   "original_fullband_q0":"SELF_CONVERGENCE_FAILED",
                   "physical_validation":"NOT_VALIDATED",
                   "production_ready":False,
                   "upstream_pffdtd_source_changed":False})
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("AUDIT_DONE native original graph edge reciprocity",result["all_five_native_original_room_graphs_have_zero_unreciprocated_edges"],
          "product remains NO_GO",flush=True)
if __name__=="__main__":
    main()
