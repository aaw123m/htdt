"""Experimental exact planar *convex* CAD cut-cell Neumann FV geometry.

This is a restricted, independently testable extension of the sloped R130D
operator; arbitrary nonconvex CAD, holes, curved surfaces, impedance, fluid
connectivity and thin-wall topology remain unsupported and are rejected
rather than silently voxelized.

Room = box [0,L]^3 intersect {a.x <= b} for supplied additional planes.
Intersections of convex cube faces with every room plane define an oriented
closed polyhedral surface. Cell volumes integrate this surface using the
divergence theorem; open Cartesian flux faces use exactly clipped polygons.
There is no staircase approximation of the oblique physical boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from scipy.spatial import ConvexHull

from .r130d_embedded_neumann_fv import EmbeddedNeumannSystem


@dataclass(frozen=True)
class ConvexPlanarRoom:
    """Bounded convex air volume with an axis-aligned cube outer domain.

    Planes are (a_x,a_y,a_z,b), with a dot xyz <= b. Only *additional*
    clipping planes are specified; all 6 cube walls are added implicitly.
    """
    additional_planes: tuple[tuple[float,float,float,float], ...]
    length_m: float = 4.0
    sound_speed_m_s: float = 343.2

    def __post_init__(self) -> None:
        if (not math.isfinite(self.length_m) or self.length_m<=0
                or not math.isfinite(self.sound_speed_m_s)
                or self.sound_speed_m_s<=0):
            raise ValueError("invalid cube extent or acoustic speed")
        if not 1 <= len(self.additional_planes) <= 8:
            raise ValueError("supported convex geometry requires 1..8 clipping planes")
        for plane in self.additional_planes:
            if (len(plane)!=4 or not all(math.isfinite(float(v)) for v in plane)
                    or float(np.linalg.norm(plane[:3])) < 1e-10):
                raise ValueError("clipping planes require finite nonzero normals")

    def plane_arrays(self) -> tuple[np.ndarray,np.ndarray]:
        planes=np.asarray(self.additional_planes,dtype=np.float64)
        length=np.linalg.norm(planes[:,:3],axis=1)
        return planes[:,:3]/length[:,None],planes[:,3]/length

    def contains(self, point: np.ndarray, *, tol: float=1e-12) -> bool:
        p=np.asarray(point,dtype=np.float64)
        if p.shape!=(3,) or not np.all(np.isfinite(p)):
            return False
        if np.any(p < -tol) or np.any(p > self.length_m+tol):
            return False
        normals,offsets=self.plane_arrays()
        return bool(np.all(normals@p <= offsets+tol))

    def independent_convex_hull_volume(self) -> float:
        """Independent triple-plane intersection + Qhull geometry witness."""
        normals,offsets=self.plane_arrays()
        box_normals=np.vstack((np.eye(3),-np.eye(3)))
        box_offsets=np.array([self.length_m]*3+[0.0]*3)
        a=np.vstack((normals,box_normals))
        b=np.concatenate((offsets,box_offsets))
        vertices=[]
        for triple in combinations(range(len(b)),3):
            mat=a[list(triple)]
            if abs(np.linalg.det(mat)) < 1e-12:
                continue
            point=np.linalg.solve(mat,b[list(triple)])
            if np.all(a@point<=b+1e-9):
                if not any(np.linalg.norm(point-v)<1e-8 for v in vertices):
                    vertices.append(point)
        if len(vertices)<4:
            raise ValueError("room has no nondegenerate bounded air volume")
        vertex_array=np.asarray(vertices)
        if np.linalg.matrix_rank(vertex_array-vertex_array[0],tol=1e-9)<3:
            raise ValueError("degenerate planar room, not a 3D air volume")
        hull=ConvexHull(vertex_array)
        if not math.isfinite(hull.volume) or hull.volume<=1e-8:
            raise ValueError("degenerate planar room volume")
        return float(hull.volume)


def _clip_polygon(
    polygon: list[np.ndarray], normal: np.ndarray, offset: float,
    *,
    tol: float,
) -> tuple[list[np.ndarray],list[np.ndarray]]:
    """Sutherland–Hodgman exact polygon clipping in 3D Cartesian space."""
    if not polygon:
        return [],[]
    output=[]
    hits=[]
    a=polygon[-1]
    da=float(normal@a-offset)
    for b in polygon:
        db=float(normal@b-offset)
        inside_a=da<=tol
        inside_b=db<=tol
        if inside_a != inside_b:
            if abs(da-db)<1e-30:
                raise ValueError("unstable geometry clip intersection")
            t=da/(da-db)
            hit=a+np.clip(t,0.0,1.0)*(b-a)
            output.append(hit)
            hits.append(hit)
        if inside_b:
            output.append(b)
        a,da=b,db
    return output,hits


def _unique_points(points:list[np.ndarray], *, tol:float) -> list[np.ndarray]:
    result=[]
    for p in points:
        if not any(np.linalg.norm(p-q)<=tol for q in result):
            result.append(p)
    return result


def _cap_polygon(points:list[np.ndarray],normal:np.ndarray,tol:float) -> list[np.ndarray]:
    points=_unique_points(points,tol=tol)
    if len(points)<3:
        return []
    center=np.mean(points,axis=0)
    axis=np.eye(3)[int(np.argmin(abs(normal)))]
    u=np.cross(axis,normal)
    u=u/np.linalg.norm(u)
    v=np.cross(normal,u)
    angles=[math.atan2(float((p-center)@v),float((p-center)@u))
            for p in points]
    return [points[i] for i in np.argsort(angles)]


def _cube_faces(lower:np.ndarray,upper:np.ndarray) -> list[list[np.ndarray]]:
    x0,y0,z0=lower
    x1,y1,z1=upper
    a=np.array([x0,y0,z0]); b=np.array([x1,y0,z0])
    c=np.array([x0,y1,z0]); d=np.array([x1,y1,z0])
    e=np.array([x0,y0,z1]); f=np.array([x1,y0,z1])
    g=np.array([x0,y1,z1]); h=np.array([x1,y1,z1])
    # Every face is wound outward for the signed divergence-theorem volume.
    return [[a,c,d,b],[e,f,h,g],[a,e,g,c],
            [b,d,h,f],[a,b,f,e],[c,g,h,d]]


def _cube_cut_volume(lower:np.ndarray,upper:np.ndarray,
                     normals:np.ndarray,offsets:np.ndarray,
                     tol:float) -> float:
    faces=_cube_faces(lower,upper)
    for n,b in zip(normals,offsets):
        new_faces=[]
        crossings=[]
        for face in faces:
            clipped,hits=_clip_polygon(face,n,float(b),tol=tol)
            if len(clipped)>=3:
                new_faces.append(clipped)
            crossings.extend(hits)
        cap=_cap_polygon(crossings,n,tol=tol)
        if cap:
            new_faces.append(cap)
        faces=new_faces
        if not faces:
            return 0.0
    volume=0.0
    for face in faces:
        if len(face)<3:
            continue
        p0=face[0]-lower
        for i in range(1,len(face)-1):
            volume+=float(p0@np.cross(face[i]-lower,face[i+1]-lower))/6
    # Numerical tolerance only: negative volumes beyond roundoff fail closed.
    if volume < -1e-11*float(np.prod(upper-lower)):
        raise ValueError("nonoriented or invalid clipped polyhedron")
    return max(volume,0.0)


def _axis_face_area(
    lower:np.ndarray,upper:np.ndarray,axis:int,
    normals:np.ndarray,offsets:np.ndarray,tol:float,
) -> float:
    axes=[j for j in range(3) if j!=axis]
    value=upper[axis]
    points=[]
    for u,v in ((0,0),(1,0),(1,1),(0,1)):
        p=lower.copy()
        p[axis]=value
        p[axes[0]]=lower[axes[0]]+u*(upper[axes[0]]-lower[axes[0]])
        p[axes[1]]=lower[axes[1]]+v*(upper[axes[1]]-lower[axes[1]])
        points.append(p)
    for n,b in zip(normals,offsets):
        points,_=_clip_polygon(points,n,float(b),tol=tol)
        if len(points)<3:
            return 0.0
    anchor=points[0]
    return 0.5*sum(float(np.linalg.norm(np.cross(
        points[i]-anchor,points[i+1]-anchor)))
        for i in range(1,len(points)-1))


def build_convex_embedded_neumann(
    cells_per_axis:int,*,geometry:ConvexPlanarRoom,
    max_cells:int=30_000,min_cut_volume_fraction:float=1e-12,
) -> EmbeddedNeumannSystem:
    """Build symmetric, conservative rigid-wall wave matrices for convex planes."""
    if (not isinstance(cells_per_axis,int) or isinstance(cells_per_axis,bool)
            or cells_per_axis<3 or cells_per_axis**3>max_cells
            or not math.isfinite(min_cut_volume_fraction)
            or not 0<min_cut_volume_fraction<0.1):
        raise ValueError("bounded grid or cut cell resources invalid")
    if not isinstance(geometry,ConvexPlanarRoom):
        raise ValueError("explicit convex geometry authority required")
    n=cells_per_axis
    h=geometry.length_m/n
    normals,offsets=geometry.plane_arrays()
    expected_volume=geometry.independent_convex_hull_volume()
    mass_cells={}
    tol=1e-12*h
    for i in range(n):
        for j in range(n):
            for k in range(n):
                lower=np.array([i,j,k],dtype=np.float64)*h
                upper=lower+h
                # Analytic bounding interval of plane over this voxel.
                dmin=np.sum(normals*np.where(normals>=0,lower,upper),axis=1)-offsets
                dmax=np.sum(normals*np.where(normals>=0,upper,lower),axis=1)-offsets
                if np.any(dmin > tol):
                    continue
                if np.all(dmax <= tol):
                    vol=h**3
                else:
                    vol=_cube_cut_volume(lower,upper,normals,offsets,tol)
                if vol < -1e-10*h**3 or vol > h**3*(1+1e-10):
                    raise ValueError("cut volume outside voxel bound")
                if vol<=1e-14*h**3:
                    continue
                if vol < min_cut_volume_fraction*h**3:
                    raise ValueError("unresolved tiny cut cell; no silent solidification")
                mass_cells[(i,j,k)]=float(vol)
    if not mass_cells or len(mass_cells)>max_cells:
        raise ValueError("empty or resource-exhausting planar room")
    coords=np.asarray(sorted(mass_cells),dtype=np.int32)
    idx={tuple(ijk):t for t,ijk in enumerate(coords)}
    vol=np.asarray([mass_cells[tuple(ijk)] for ijk in coords])
    if abs(vol.sum()-expected_volume)>5e-9*expected_volume:
        raise ValueError("exact cut volumes disagree with independent convex hull")
    left=[];right=[];areas=[]
    for t,ijk in enumerate(coords):
        lower=ijk.astype(np.float64)*h
        upper=lower+h
        for ax in range(3):
            nxt=ijk.copy()
            nxt[ax]+=1
            other=idx.get(tuple(nxt))
            if other is None:
                continue
            # This face lies between exactly two active cells.
            area=_axis_face_area(lower,upper,ax,normals,offsets,tol)
            if area<0 or area>h*h*(1+1e-10):
                raise ValueError("invalid cut face aperture")
            if area<=1e-14*h*h:
                continue
            left.append(t);right.append(other);areas.append(area)
    a=np.asarray(left,dtype=np.int32)
    b=np.asarray(right,dtype=np.int32)
    area=np.asarray(areas,dtype=np.float64)
    weights=geometry.sound_speed_m_s**2*area/h
    rows=np.concatenate((a,b,a,b))
    cols=np.concatenate((a,b,b,a))
    values=np.concatenate((weights,weights,-weights,-weights))
    K=sparse.coo_matrix((values,(rows,cols)),shape=(len(coords),len(coords))).tocsr()
    graph=sparse.coo_matrix((np.ones(len(a)*2),
             (np.concatenate((a,b)),np.concatenate((b,a)))),
             shape=K.shape).tocsr()
    components,_=connected_components(graph,directed=False)
    if components!=1:
        raise ValueError("disconnected sliver cells; no artificial coupling")
    if np.linalg.norm(K@np.ones(len(coords)),ord=np.inf)>1e-10*max(
        1.0,float(np.max(np.abs(K.diagonal())))):
        raise ValueError("Neumann constant potential not conserved")
    return EmbeddedNeumannSystem(
        geometry=geometry,cells_per_axis=n,grid_spacing_m=h,
        cell_coordinates_ijk=coords,
        cell_center_xyz_m=(coords+0.5)*h,
        cell_volumes_m3=vol,fluid_face_areas_m2=area,
        mass=sparse.diags(vol,format='csr'),stiffness=K,
    )
