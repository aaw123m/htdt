"""Independent continuous 3D causal retarded point Green benchmark for R130D.

For the scalar wave equation in homogeneous full-space, the causal
fundamental solution is delta(t-r/c)/(4*pi*r), r>0; under the **original
signed** +i omega t transform this is exp(+i omega r/c)/(4*pi*r).
Its continuous pressure derivative carries a common -i rho omega factor.
The absolute scalar prefactor linking original PFFDTD in_sigs q0 to a
continuum volume-velocity impulse is NOT silently fitted; 8-node / true
point geometric RATIOS cancel that unknown fixed prefactor.

The six image sources give the mathematically correct SINGLE planar
Neumann reflection geometry for an infinite wall plane, with the
specular intersection checked against the *finite* true room wall. They
do NOT constitute a 250ms full-room multipath Green function.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence

import numpy as np


C=343.2
RHO=1.2
FREQUENCIES_HZ=(40.,80.)


@dataclass(frozen=True)
class PlanarWall:
    name: str
    normal: tuple[float,float,float]
    plane_rhs_m: float


ROOM_WALLS=(
    PlanarWall("x0",(1.,0.,0.),0.),
    PlanarWall("x4",(1.,0.,0.),4.),
    PlanarWall("y0",(0.,1.,0.),0.),
    PlanarWall("y4",(0.,1.,0.),4.),
    PlanarWall("z0",(0.,0.,1.),0.),
    PlanarWall("true_sloping_roof",(0.,.25,1.),4.),
)


def green_retarded_signed(
    source_xyz_m:np.ndarray|Sequence[float],
    receiver_xyz_m:np.ndarray|Sequence[float],
    frequencies_hz:tuple[float,...]=FREQUENCIES_HZ,
    *,c_m_s:float=C,
)->np.ndarray:
    """Causal distributional 3D retarded delta Green integral at signed f.

    Shape (number of original frequency bins,) for scalar point; use
    broadcast leading source/receiver shape ending in coordinate dimension 3.
    """
    s=np.asarray(source_xyz_m,dtype=float)
    r=np.asarray(receiver_xyz_m,dtype=float)
    f=np.asarray(frequencies_hz,dtype=float)
    if (s.shape[-1:]!=(3,) or r.shape[-1:]!=(3,)
        or f.ndim!=1 or not len(f) or not np.isfinite(f).all()
        or not np.isfinite(s).all() or not np.isfinite(r).all()
        or not np.isfinite(c_m_s) or c_m_s<=0):
        raise ValueError("continuous causal retarded point Green invalid original source/receiver")
    length=np.linalg.norm(r-s,axis=-1)
    if np.any(length<=0):
        raise ValueError("singular continuum point Green undefined for collocated source/receiver")
    return np.exp(2j*np.pi*f.reshape((-1,)+(1,)*length.ndim)
                  *length[None,...]/c_m_s)/(4*np.pi*length[None,...])


def _room_point_valid(xyz:np.ndarray,eps:float=1e-10)->bool:
    x,y,z=np.asarray(xyz,dtype=float)
    return (-eps<=x<=4+eps and -eps<=y<=4+eps and
            -eps<=z<=4-.25*y+eps)


def true_wall_specular_reflection(
    source_xyz_m:np.ndarray|Sequence[float],
    receiver_xyz_m:np.ndarray|Sequence[float],
    wall:PlanarWall,
)->dict:
    """Exact one-bounce reflection on physical finite planar wall.

    Plane: normal dot x = rhs. Mirror source as the single-wall Neumann
    image, find line joining image-source to receiver, intersection foot,
    and reject if foot not on the chosen finite physical wall segment.
    """
    s=np.asarray(source_xyz_m,dtype=float)
    r=np.asarray(receiver_xyz_m,dtype=float)
    n=np.asarray(wall.normal,dtype=float)
    d=float(wall.plane_rhs_m)
    if (s.shape!=(3,) or r.shape!=(3,) or n.shape!=(3,)
        or not np.isfinite(s).all() or not np.isfinite(r).all()
        or not _room_point_valid(s) or not _room_point_valid(r)
        or n@n<=0):
        raise ValueError("true planar Neumann first reflection invalid physical room points")
    image=s-2*((n@s-d)/(n@n))*n
    line=r-image
    denom=float(n@line)
    if abs(denom)<=1e-15:
        raise ValueError("specular image line parallel to original physical reflecting plane")
    factor=float((d-n@image)/denom)
    foot=image+factor*line
    distance=float(np.linalg.norm(line))
    supported=(0<=factor<=1 and _room_point_valid(foot,eps=2e-9)
               and abs(n@foot-d)<=2e-9)
    return {"wall":wall.name,"source_image_xyz_m":image.tolist(),
            "reflection_xyz_m":foot.tolist(),
            "single_bounce_path_m":distance,
            "specular_fraction_from_image":factor,
            "reflecting_point_on_true_finite_room_wall":bool(supported),
            "travel_time_s":distance/C}


def original_8node_retarded_signed(
    native_source_positions_m:np.ndarray,
    native_source_weights:np.ndarray,
    native_receiver_positions_m:np.ndarray,
    native_receiver_weights:np.ndarray,
    *,frequencies_hz:tuple[float,...]=FREQUENCIES_HZ,
)->dict:
    """Analytic independent 8x8 free-field Green & single-bounce references.

    An 8node original PFFDTD numerical source is a 64-point signed,
    weighted spatial distribution; DO NOT substitute the true physical
    Dirac without explicitly labeling it a distinct experimental arm.
    """
    s=np.asarray(native_source_positions_m,dtype=float)
    r=np.asarray(native_receiver_positions_m,dtype=float)
    sw=np.asarray(native_source_weights,dtype=float)
    rw=np.asarray(native_receiver_weights,dtype=float)
    if (s.shape!=(8,3) or r.shape!=(8,3) or sw.shape!=(8,) or rw.shape!=(8,)
        or not np.isfinite(s).all() or not np.isfinite(r).all()
        or not np.isfinite(sw).all() or not np.isfinite(rw).all()
        or abs(float(sw.sum())-1)>1e-12
        or abs(float(rw.sum())-1)>1e-12
        or not all(_room_point_valid(point) for point in np.r_[s,r])):
        raise ValueError("original SHA-pinned native 8node coefficients, physical point or walls changed")
    pair_weights=sw[:,None]*rw[None,:]
    distances=np.linalg.norm(s[:,None,:]-r[None,:,:],axis=-1)
    if np.min(distances)<=0:
        raise ValueError("point Green source collocated with native receiver node")
    direct=green_retarded_signed(s[:,None,:],r[None,:,:],frequencies_hz)
    weighted=np.einsum("ij,fij->f",pair_weights,direct)
    pair_image={}
    for wall in ROOM_WALLS:
        weighted_image=np.zeros(len(frequencies_hz),dtype=complex)
        supported=0
        min_d=np.inf
        max_d=0.
        for i in range(8):
            for j in range(8):
                spec=true_wall_specular_reflection(s[i],r[j],wall)
                if not spec["reflecting_point_on_true_finite_room_wall"]:
                    continue
                supported+=1
                img=np.asarray(spec["source_image_xyz_m"])
                single=green_retarded_signed(img,r[j],frequencies_hz)
                weighted_image+=pair_weights[i,j]*single
                min_d=min(min_d,spec["single_bounce_path_m"])
                max_d=max(max_d,spec["single_bounce_path_m"])
        pair_image[wall.name]={
            "original_native_64_source_receiver_pairs_specular_on_true_finite_face":supported,
            "physically_supported_first_bounce_G_signed":weighted_image,
            "earliest_supported_original_native_pair_bounce_s":(
                min_d/C if supported else None),
            "latest_supported_original_native_pair_bounce_s":(
                max_d/C if supported else None)}
    src_physical=sw@s
    recv_physical=rw@r
    return {"original_native_8node_source_first_moment_m":src_physical,
            "original_native_8node_receiver_first_moment_m":recv_physical,
            "all_64_original_pairs_retarded_free_space_G_signed":weighted,
            "native_source_receiver_min_direct_path_m":float(distances.min()),
            "native_source_receiver_max_direct_path_m":float(distances.max()),
            "original_8node_first_bounce_per_true_wall":pair_image,
            "true_numerical_original_source_weights_sum":float(sum(sw)),
            "true_numerical_original_receiver_weights_sum":float(sum(rw))}


def physical_point_first_reflections(
    source_xyz_m=(1.5,2.,2.),receiver_xyz_m=(2.5,2.,2.),
    *,frequency_hz=FREQUENCIES_HZ,
)->dict:
    s=np.asarray(source_xyz_m,dtype=float)
    r=np.asarray(receiver_xyz_m,dtype=float)
    direct=green_retarded_signed(s,r,frequency_hz)
    each={}
    for wall in ROOM_WALLS:
        item=true_wall_specular_reflection(s,r,wall)
        if item["reflecting_point_on_true_finite_room_wall"]:
            item["single_bounce_G_signed"]=green_retarded_signed(
                item["source_image_xyz_m"],r,frequency_hz)
        each[wall.name]=item
    earliest=min(item["travel_time_s"] for item in each.values()
                 if item["reflecting_point_on_true_finite_room_wall"])
    direct_s=float(np.linalg.norm(r-s)/C)
    if earliest<=direct_s:
        raise ValueError("causal Neumann first reflection arrived before actual source-to-receiver direct path")
    return {"direct_G_signed":direct,
            "direct_original_physical_distance_m":float(np.linalg.norm(r-s)),
            "direct_original_physical_arrival_s":direct_s,
            "first_supported_reflection_s":earliest,
            "strict_direct_before_any_single_wall_bounce_interval_s":earliest-direct_s,
            "six_true_planar_neumann_wall_reflections":each}
