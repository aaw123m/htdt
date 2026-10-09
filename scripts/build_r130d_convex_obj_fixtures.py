#!/usr/bin/env python3
"""Deterministically generate convex triangular OBJ CAD-import test fixtures.

Independent Qhull hull facets from separately generated P2 tetra mesh
vertices. This is a *synthetic* OBJ fixture, NOT a user's external CAD.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
from scipy.spatial import ConvexHull


def mesh_to_obj(raw:str) -> str:
    lines=raw.splitlines()
    nv,nt=map(int,lines[0].split())
    vertices=np.array([[float(x) for x in s.split()] for s in lines[1:1+nv]])
    hull=ConvexHull(vertices)
    out=["# R130D independent convex room OBJ triangular CAD-import fixture"]+[
        "v "+" ".join(format(float(v),".17g") for v in row)
        for row in vertices
    ]
    faces=[]
    for tri,plane in zip(hull.simplices,hull.equations):
        a,b,c=vertices[tri]
        if np.dot(np.cross(b-a,c-a),plane[:3])<0:
            tri=np.array([tri[0],tri[2],tri[1]])
        faces.append(tuple(int(i)+1 for i in tri))
    for face in sorted(faces):
        out.append("f "+" ".join(str(i) for i in face))
    return "\n".join(out)+"\n"


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--tet-sources",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    for name in ("planar_wedge","three_axis_diagonal"):
        obj=mesh_to_obj((a.tet_sources/(name+".tet")).read_text())
        (a.output/(name+".obj")).write_text(obj,encoding="ascii")
        print(name,len(obj),obj.count("\nf "),flush=True)


if __name__=="__main__":main()
