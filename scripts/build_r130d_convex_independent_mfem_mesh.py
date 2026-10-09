#!/usr/bin/env python3
"""Independent tetra mesh of preregistered halfspaces for pinned MFEM P2.

Does NOT reuse candidate FV plane clipping, mass matrix or flux operator.
Uses scipy HalfspaceIntersection + Qhull/Delaunay, checks each tetra orientation,
convex hull volume and full tetra sum, and emits a deterministic readable mesh.
Only convex polyhedra; no claim to general CAD or product acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy.spatial import HalfspaceIntersection, ConvexHull, Delaunay


def tetra_mesh(planes:list[list[float]], *, L:float, center=(2.,2.,2.),
               exact_volume:float) -> tuple[np.ndarray,np.ndarray,dict]:
    hs=[]
    for ax in range(3):
        plus=np.zeros(3);plus[ax]=1
        minus=-plus
        hs.append([*plus,-L])
        hs.append([*minus,0.])
    for row in planes:
        if len(row)!=4:
            raise ValueError("invalid plane")
        hs.append([*row[:3],-row[3]])
    halfspaces=np.asarray(hs,dtype=float)
    center=np.asarray(center,dtype=float)
    if not np.all(halfspaces[:,:3]@center+halfspaces[:,3]<-1e-5):
        raise ValueError("reference mesh center must be strictly in air")
    hsi=HalfspaceIntersection(halfspaces,center)
    hull=ConvexHull(hsi.intersections)
    vertices=np.asarray(hsi.intersections,dtype=float)
    # canonical vertex order independent of Qhull facet traversal
    order=np.lexsort((vertices[:,2],vertices[:,1],vertices[:,0]))
    vertices=vertices[order]
    delaunay=Delaunay(vertices)
    tets=delaunay.simplices.copy()
    each=[]
    for i,t in enumerate(tets):
        xyz=vertices[t]
        det=float(np.linalg.det(np.stack((xyz[1]-xyz[0],
              xyz[2]-xyz[0],xyz[3]-xyz[0]),axis=1)))
        if abs(det)<1e-11:
            raise ValueError("degenerate independent tetrahedron")
        if det<0:
            tets[i,[2,3]]=tets[i,[3,2]]
        each.append(abs(det)/6)
    # deterministic lexicographic vertex/tetra ordering
    tets=np.asarray(sorted((tuple(int(x) for x in t) for t in tets)),dtype=int)
    total=float(sum(each))
    if abs(total-exact_volume)>5e-10*exact_volume or abs(hull.volume-exact_volume)>5e-10*exact_volume:
        raise ValueError("independent MFEM tetra mesh fails analytic volume")
    result={
        "mesh_origin":"independent scipy.spatial.HalfspaceIntersection+Qhull Delaunay",
        "vertices":int(len(vertices)), "tetrahedra":int(len(tets)),
        "tetra_sum_volume_m3":total, "hull_volume_m3":float(hull.volume),
        "analytic_volume_m3":exact_volume,"tetra_min_volume_m3":min(each),
        "bounds_m":L,
    }
    return vertices,tets,result


def main() -> None:
    p=argparse.ArgumentParser()
    p.add_argument("--plan",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--manifest",type=Path,required=True)
    args=p.parse_args()
    plan=json.loads(args.plan.read_text(encoding="utf-8"))
    if plan["schema_version"]!="htdt.r130d.convex-independent-mfem-plan-1":
        raise ValueError("plan changed")
    args.out.mkdir(parents=True,exist_ok=True)
    result={"schema_version":"htdt.r130d.convex-independent-mfem-tetra-mesh-provenance-1",
            "mfem_pin":plan["mfem_pin"],"rooms":{}}
    for room in plan["geometry_cases"]:
        v,t,metadata=tetra_mesh(room["planes"],L=plan["physical"]["L_m"],
              exact_volume=room["exact_volume_m3"])
        if len(t)>plan["resource_limits"]["max_tet_mesh_initial_elements"]:
            raise ValueError("too many base tetrahedra")
        lines=[f"{len(v)} {len(t)}"]+[(" ".join(format(float(x),".17g") for x in row)) for row in v]
        lines+=[" ".join(str(int(x)) for x in row) for row in t]
        filename=room["name"]+".tet"
        blob=("\n".join(lines)+"\n").encode("ascii")
        (args.out/filename).write_bytes(blob)
        metadata["sha256"]=hashlib.sha256(blob).hexdigest()
        metadata["path"]=filename
        result["rooms"][room["name"]]=metadata
        print(room["name"],metadata,flush=True)
    args.manifest.parent.mkdir(parents=True,exist_ok=True)
    args.manifest.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
