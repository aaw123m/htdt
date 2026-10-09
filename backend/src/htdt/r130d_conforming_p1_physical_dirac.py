"""True physical P1 weak Dirac source and exact FE point receiver.

Given a physical position fixed *independently of the grid*, evaluate
the x line-element hat basis and the yz true-roof triangular barycentric
P1 basis. A physical point Dirac force has weak coefficients N_j(x_s);
point receiver uses N_j(x_r). This changes the original upstream 8-point
Cartesian interpolation and is EXPERIMENTAL, never original PFFDTD PASS.
No source width, smoothing, mass scaling or fitted transfer parameters.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .r130d_conforming_roof_p1_fem import ConformingRoofP1Separable


@dataclass(frozen=True)
class PhysicalP1PointCoupling:
    physical_xyz_m: tuple[float,float,float]
    x_weights: np.ndarray
    yz_weights: np.ndarray
    x_nnz: int
    yz_nnz: int
    max_moment_error_m: float
    partition_error: float
    support_native_3d_nodes: int
    triangle_index: int


def physical_roof_P1_point_shape(
    fem:ConformingRoofP1Separable,
    physical_xyz_m:tuple[float,float,float]|list[float]|np.ndarray,
) -> PhysicalP1PointCoupling:
    pos=np.asarray(physical_xyz_m,dtype=float)
    if (pos.shape!=(3,) or not np.isfinite(pos).all()
        or not (0 <= pos[0] <=4 and 0<=pos[1]<=4 and
                0<=pos[2]<=4-.25*pos[1])):
        raise ValueError("physical Dirac point moved outside actual 56m3 Neumann room")
    x,y,z=map(float,pos)
    xcoords=fem.x_positions_m
    if x<xcoords[0] or x>xcoords[-1]:
        raise ValueError("physical P1 line has wrong end boundary")
    i=int(np.searchsorted(xcoords,x,side="right")-1)
    i=max(0,min(i,len(xcoords)-2))
    h=float(xcoords[i+1]-xcoords[i])
    if h<=0:raise ValueError("invalid physical FE 1D line element")
    alpha=(x-xcoords[i])/h
    if alpha<-1e-12 or alpha>1+1e-12:
        raise ValueError("Dirac source is outside its physical line element")
    wx=np.zeros(len(xcoords),dtype=float)
    wx[i]=1-alpha
    wx[i+1]=alpha

    # Locate original P1 mesh triangle by exact barycentric moment test.
    # Doing this using the *existing* frozen triangles (not re-Delaunay or
    # changing mesh) makes the numerical arm genuinely only a new coupling.
    q=fem.yz_positions_m[fem.yz_triangles]
    d1=q[:,1]-q[:,0]
    d2=q[:,2]-q[:,0]
    r=np.array([y,z])[None,:]-q[:,0]
    determinant=d1[:,0]*d2[:,1]-d1[:,1]*d2[:,0]
    if not np.isfinite(determinant).all() or np.any(abs(determinant)<1e-14):
        raise ValueError("true physical FEM contains a degenerate source triangle")
    b=(r[:,0]*d2[:,1]-r[:,1]*d2[:,0])/determinant
    c=(d1[:,0]*r[:,1]-d1[:,1]*r[:,0])/determinant
    bary=np.stack([1-b-c,b,c],axis=1)
    good=np.flatnonzero((bary.min(axis=1)>=-2e-10)&
                        (bary.max(axis=1)<=1+2e-10))
    if len(good)==0:
        raise ValueError("physical point not contained by exact sloping roof P1 mesh")
    # A point on a common triangle edge has a unique global FE basis value.
    # Choose geometrically deepest matching element; tie earliest stable.
    k=int(good[np.argmax(bary[good].min(axis=1))])
    local=np.maximum(0.,bary[k])
    local/=local.sum()
    wy=np.zeros(len(fem.yz_positions_m),dtype=float)
    np.add.at(wy,fem.yz_triangles[k],local)
    reconstructed=np.array([
        np.dot(xcoords,wx),
        np.dot(fem.yz_positions_m[:,0],wy),
        np.dot(fem.yz_positions_m[:,1],wy)])
    error=float(np.max(abs(reconstructed-pos)))
    partition=float(max(abs(sum(wx)-1),abs(sum(wy)-1)))
    nnx=int(np.count_nonzero(wx>1e-13))
    nny=int(np.count_nonzero(wy>1e-13))
    if (error>2e-10 or partition>2e-12 or
        np.min(wx)<-1e-12 or np.min(wy)<-1e-12 or
        nnx>2 or nny>3 or nnx<1 or nny<1):
        raise ValueError(f"true point Dirac P1 affine or partition consistency failed: {error} {partition}")
    return PhysicalP1PointCoupling(
        (x,y,z),wx,wy,nnx,nny,error,partition,nnx*nny,k)


def physical_P1_point_tensor_modal_projection(
    fem:ConformingRoofP1Separable,
    point:PhysicalP1PointCoupling,
    generalized_x_modes:np.ndarray,
    generalized_yz_modes:np.ndarray,
)->tuple[np.ndarray,np.ndarray]:
    """Project exact weak Dirac and point observation into complete modes."""
    xmode=np.asarray(generalized_x_modes,dtype=float)
    yzmode=np.asarray(generalized_yz_modes,dtype=float)
    if (xmode.shape!=(len(fem.x_positions_m),len(fem.x_positions_m))
        or yzmode.shape!=(len(fem.yz_positions_m),len(fem.yz_positions_m))):
        raise ValueError("true complete original-grid FEM eigenmodes are required")
    return point.x_weights@xmode,point.yz_weights@yzmode
