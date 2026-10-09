"""Bounded fail-closed import of *convex watertight triangular OBJ* room air meshes.

Only positive-index triangle faces and XYZ vertices are permitted; a CAD
triangular surface is used to derive oriented bounding halfspaces, not
rasterized surface cells. Concave, curved, open, non-manifold,
orientation-inconsistent, self-crossing, or multi-shell geometry is rejected.
Source file SHA-256 is retained for explicit experimental provenance.

This is NOT a production CAD ingestion pathway, no general nonconvex room.
"""
from __future__ import annotations

from collections import Counter,defaultdict,deque
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import math

import numpy as np
from scipy.spatial import ConvexHull

from .r130d_embedded_neumann_convex import ConvexPlanarRoom


@dataclass(frozen=True)
class ConvexObjAirMesh:
    geometry: ConvexPlanarRoom
    source_sha256: str
    source_bytes: int
    triangle_count: int
    vertex_count: int
    surface_signed_volume_m3: float
    convex_hull_volume_m3: float
    plane_count: int
    imported_format: str = "bounded-watertight-convex-triangle-obj-1"


def import_convex_obj_air_mesh(
    path: Path, *, length_m: float = 4.0, sound_speed_m_s: float=343.2,
    max_file_bytes: int = 2_000_000, max_vertices: int=2_048,
    max_triangles: int=4_096,
) -> ConvexObjAirMesh:
    """Validate a closed outward oriented convex OBJ shell and derive planes."""
    if not isinstance(path,(str,Path)):
        raise ValueError("a concrete local OBJ path is required")
    source=Path(path)
    if source.suffix.lower() != ".obj":
        raise ValueError("only explicit .obj triangle air surfaces are accepted")
    raw=source.read_bytes()
    if len(raw)==0 or len(raw)>max_file_bytes or b"\x00" in raw:
        raise ValueError("CAD OBJ source exceeds strict byte envelope")
    text=raw.decode("ascii",errors="strict")
    if not math.isfinite(length_m) or length_m<=0:
        raise ValueError("invalid physical box extent")
    vertices=[]
    faces=[]
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith("#"):
            continue
        parts=line.split()
        if parts[0]=="v":
            if len(parts)!=4 or len(vertices)>=max_vertices:
                raise ValueError("OBJ vertices must contain exactly 3 bounded coordinates")
            try:point=np.asarray([float(v) for v in parts[1:]],dtype=float)
            except ValueError as e:raise ValueError("OBJ XYZ coordinate is invalid") from e
            if not np.all(np.isfinite(point)) or np.any(point<-1e-9) or np.any(point>length_m+1e-9):
                raise ValueError("OBJ vertex outside stated local meter extent")
            vertices.append(point)
        elif parts[0]=="f":
            if len(parts)!=4 or len(faces)>=max_triangles:
                raise ValueError("OBJ must contain triangulated bounded faces")
            if any(not v.isdecimal() for v in parts[1:]):
                raise ValueError("OBJ must use positive plain integer vertex indices only")
            tri=tuple(int(v)-1 for v in parts[1:])
            if min(tri)<0 or max(tri)>=len(vertices) or len(set(tri))!=3:
                raise ValueError("OBJ invalid triangle indices")
            faces.append(tri)
        else:
            raise ValueError("unsupported OBJ CAD construct; fail closed")
    if len(vertices)<4 or len(faces)<4:
        raise ValueError("OBJ must be a complete nondegenerate polyhedral shell")
    xyz=np.asarray(vertices)
    # duplicate vertex coordinates can conceal open/nonmanifold topology
    from scipy.spatial import cKDTree
    if cKDTree(xyz).query_pairs(r=1e-9):
        raise ValueError("OBJ duplicates geometrical vertices")
    directed=Counter()
    undirected=defaultdict(list)
    faces_by_vertex=defaultdict(list)
    for i,(a,b,c) in enumerate(faces):
        for u,v in ((a,b),(b,c),(c,a)):
            directed[(u,v)]+=1
            undirected[tuple(sorted((u,v)))].append((u,v))
            faces_by_vertex[u].append(i)
    if not undirected or any(
        len(e)!=2 or e[0]!=(e[1][1],e[1][0]) for e in undirected.values()
    ):
        raise ValueError("OBJ shell must be manifold and consistently outward wound")
    graph=[set() for _ in faces]
    for edge in undirected.values():
        u,v=edge[0]
        # Each edge belongs to exactly 2 triangles.
        containing=[i for i,f in enumerate(faces) if u in f and v in f]
        if len(containing)!=2:
            raise ValueError("OBJ edge incidence inconsistent")
        graph[containing[0]].add(containing[1])
        graph[containing[1]].add(containing[0])
    reached={0}
    queue=deque([0])
    while queue:
        for n in graph[queue.popleft()]:
            if n not in reached:
                reached.add(n);queue.append(n)
    if len(reached)!=len(faces):
        raise ValueError("OBJ has more than one shell")
    center=xyz.mean(axis=0)
    signed_volume=0.
    planes=[]
    for ia,ib,ic in faces:
        a,b,c=xyz[[ia,ib,ic]]
        normal=np.cross(b-a,c-a)
        norm=np.linalg.norm(normal)
        if norm<1e-10:
            raise ValueError("degenerate or nearly zero-area OBJ triangle")
        normal=normal/norm
        off=float(normal@a)
        if np.max(xyz@normal-off)>1e-8:
            raise ValueError("OBJ triangle cannot support a convex outward boundary")
        signed_volume+=float((a-center)@np.cross(b-center,c-center))/6
        if not any(np.linalg.norm(normal-v[:3])<1e-8 and
                   abs(off-v[3])<1e-8 for v in planes):
            planes.append((float(normal[0]),float(normal[1]),
                           float(normal[2]),off))
    if signed_volume<=1e-9:
        raise ValueError("OBJ closed surface is negatively oriented or degenerate")
    hull=ConvexHull(xyz)
    if abs(signed_volume-hull.volume)>1e-8*hull.volume:
        raise ValueError("OBJ signed mesh volume differs from independent convex hull")
    # Explicit cube supports ±x,±y,±z faces at [0,L]. Any other
    # unique triangular support plane is an *additional* halfspace.
    extra=[]
    for p in planes:
        normal=np.asarray(p[:3])
        offset=p[3]
        is_box=False
        for axis in range(3):
            unit=np.eye(3)[axis]
            if np.linalg.norm(normal-unit)<1e-8 and abs(offset-length_m)<1e-8:
                is_box=True
            if np.linalg.norm(normal+unit)<1e-8 and abs(offset)<1e-8:
                is_box=True
        if not is_box:extra.append(p)
    if not 1<=len(extra)<=8:
        raise ValueError("OBJ convex room requires 1..8 non-box support planes")
    room=ConvexPlanarRoom(tuple(extra),length_m=length_m,
                           sound_speed_m_s=sound_speed_m_s)
    exact=room.independent_convex_hull_volume()
    if abs(exact-signed_volume)>1e-8*exact:
        raise ValueError("OBJ plane-volume mismatch or unsupported topology")
    return ConvexObjAirMesh(
        geometry=room,source_sha256=sha256(raw).hexdigest(),
        source_bytes=len(raw),triangle_count=len(faces),
        vertex_count=len(vertices),
        surface_signed_volume_m3=float(signed_volume),
        convex_hull_volume_m3=float(hull.volume),plane_count=len(extra),
    )
