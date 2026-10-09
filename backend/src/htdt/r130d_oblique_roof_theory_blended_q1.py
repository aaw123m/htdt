"""True oblique Neumann roof diagnostic and predetermined 50:50 Q1 mass.

Non-fit midpoint mass blending is derived from uniform P1/Q1 wave symbols:
K=4c²/h sin²(xi/2); M_cons=h(2/3+cos(xi)/3), M_lump=h.
M_half/h=5/6+cos(xi)/6, cancels the order-xi² phase error
K/(M_half*c²*(xi/h)²)=1+O(xi^4) for uniform waveguide cells.
No claim of fourth-order convergence on roof cut cells.

Physical true roof normal in yz is (.25,1)/sqrt(1+.25²). The
affine tangential function u(y,z)=y-.25*z has exact ∂n u=0.
At a Cartesian stair boundary the nonzero axis-directed pseudo-normal
derivative exposes geometry inconsistency irrespective of h.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy import sparse

ROOF_SLOPE=.25
PHYSICAL_ROOF_NORMAL=np.array([ROOF_SLOPE,1.],dtype=float)/np.hypot(ROOF_SLOPE,1.)
PHYSICAL_ROOF_TANGENT=np.array([1.,-ROOF_SLOPE])
THEORETICAL_HALF=.5


@dataclass(frozen=True)
class TheoryHalfPhysicalRoofQ1:
    Mx:sparse.csr_matrix
    Myz:sparse.csr_matrix
    Kx:sparse.csr_matrix
    Kyz:sparse.csr_matrix
    true_volume_m3:float
    x_true_mass_rows:np.ndarray
    yz_true_mass_rows:np.ndarray

    @property
    def mode_count(self)->int:
        return self.Mx.shape[0]*self.Myz.shape[0]


def wave_uniform_1d_symbol_ratio(wavenumber_h:np.ndarray, *,mass_kind:str)->np.ndarray:
    """Exact one-dimensional infinite-uniform-grid λ/(c²k²), no fit."""
    x=np.asarray(wavenumber_h,dtype=float)
    if not np.isfinite(x).all() or np.any(x<=0) or np.any(x>=.5):
        raise ValueError("theoretical dispersion test needs positive small kh")
    stiffness=4*np.sin(.5*x)**2
    if mass_kind=="lumped":
        mass=np.ones_like(x)
    elif mass_kind=="consistent":
        mass=2/3+np.cos(x)/3
    elif mass_kind=="half":
        mass=5/6+np.cos(x)/6
    else:
        raise ValueError("unknown theoretical exact mass family")
    return stiffness/(mass*x*x)


def half_Q1_true_roof_mass(
    x_consistent:sparse.csr_matrix, yz_consistent:sparse.csr_matrix,
    x_true_stiff:sparse.csr_matrix, yz_true_roof_Q1_stiff:sparse.csr_matrix,
)->TheoryHalfPhysicalRoofQ1:
    """Mass blend fixed BEFORE scores, exact true wet volume and all nodes.

    M(half)=.5 M_consistent +.5 diag(M_consistent 1), full 3D tensor
    K changes its mass factors as appropriate for tensor P1xQ1yz.
    Cut-roof weak stiffness Kyz itself remains unchanged (physical normal
    Neumann built in); does NOT pretend to change original PFFDTD kernel.
    """
    mx=x_consistent.tocsr()
    my=yz_consistent.tocsr()
    kx=x_true_stiff.tocsr()
    ky=yz_true_roof_Q1_stiff.tocsr()
    if (mx.shape!=kx.shape or my.shape!=ky.shape or
        mx.shape[0]<4 or my.shape[0]<50 or
        max(abs((mx-mx.T).data),default=0)>1e-11 or
        max(abs((my-my.T).data),default=0)>1e-11):
        raise ValueError("invalid exact physical original roof Q1 matrices")
    wx=np.asarray(mx.sum(axis=1)).ravel()
    wy=np.asarray(my.sum(axis=1)).ravel()
    if (min(wx)<=0 or min(wy)<=0 or not np.isfinite(wx).all()
        or not np.isfinite(wy).all()
        or abs(float(wx.sum())-4)>1e-9 or abs(float(wy.sum())-14)>2e-8):
        raise ValueError("true roof tiny mass cannot be floored or discarded")
    Mx=(THEORETICAL_HALF*mx+
        THEORETICAL_HALF*sparse.diags(wx,format="csr")).tocsr()
    My=(THEORETICAL_HALF*my+
        THEORETICAL_HALF*sparse.diags(wy,format="csr")).tocsr()
    if (abs(float(Mx.sum())-4)>1e-9
        or abs(float(My.sum())-14)>2e-8
        or min(Mx.diagonal())<=0 or min(My.diagonal())<=0):
        raise ValueError("physical 50/50 consistent/lump exact volume or positivity failed")
    if max(abs(kx@np.ones(len(wx))))>1e-8 or max(abs(ky@np.ones(len(wy))))>1e-8:
        raise ValueError("true physical Neumann Q1 K does not annihilate constants")
    return TheoryHalfPhysicalRoofQ1(Mx,My,kx,ky,56.,wx,wy)


def full_theory_half_Q1_mass_and_true_roof_stiffness(
    system:TheoryHalfPhysicalRoofQ1,
)->tuple[sparse.csr_matrix,sparse.csr_matrix]:
    M=sparse.kron(system.Mx,system.Myz,format="csr")
    K=(sparse.kron(system.Kx,system.Myz,format="csr")+
       sparse.kron(system.Mx,system.Kyz,format="csr")).tocsr()
    if (M.shape[0]!=system.mode_count
        or abs(float(M.sum())-56)>2e-8
        or np.min(M.diagonal())<=0
        or max(abs(K@np.ones(system.mode_count)))/
           max(1.,max(abs(K.diagonal())))>1e-10):
        raise ValueError("half-Q1 true roof full physical Neumann mass/stiffness invalid")
    return M,K


def inspect_original_staircase_true_roof_tangential_flux(
    y_axis_m:np.ndarray,z_axis_m:np.ndarray,
    original_bn_ixyz:np.ndarray, original_adj_bn:np.ndarray,
    native_dims:tuple[int,int,int], *,
    c_m_s:float=343.2,
)->dict:
    """Inspect actual SHA-original PFFDTD roof boundary face pseudo-normals.

    Only edges present in actual original HDF5 bn_ixyz/adj_bn used.
    A missing stencil neighbor (positive +y or +z direction) contributes
    ONLY if it is physically across the exact planar roof and the resident
    boundary point lies strictly away from all other box walls. This
    demonstrates the original staircase axes are NOT the true roof normal.
    Count separate x layers; no claim of exact original full-wave cause.
    """
    yy=np.asarray(y_axis_m,dtype=float)
    zz=np.asarray(z_axis_m,dtype=float)
    bn=np.asarray(original_bn_ixyz,dtype=np.int64)
    adj=np.asarray(original_adj_bn,dtype=bool)
    nx,ny,nz=native_dims
    if (len(yy)!=ny or len(zz)!=nz or bn.ndim!=1
        or adj.shape!=(len(bn),6) or not len(bn) or
        np.any(bn<0) or np.any(bn>=nx*ny*nz)
        or c_m_s!=343.2):
        raise ValueError("original native HDF5 roof boundary adjacency invalid")
    iy=(bn//nz)%ny
    iz=bn%nz
    ix=bn//(ny*nz)
    y=yy[iy];z=zz[iz]
    hy=float(yy[1]-yy[0]);hz=float(zz[1]-zz[0])
    if hy<=0 or abs(hy-hz)>1e-10:
        raise ValueError("original native physical Cartesian h changed")
    # Resident physical Cartesian center and missing neighbor across
    # z=4−.25y. Halo walls deliberately not classified as roof.
    physical=(ix>=1)&(ix<nx-1)&(y>hy)&(y<4-hy)&(z>hz)&(
        z<=4-ROOF_SLOPE*y+1e-10)
    near=physical&(4-ROOF_SLOPE*y-z <= 2*hy+1e-10)
    y_next=np.minimum(iy+1,ny-1)
    z_next=np.minimum(iz+1,nz-1)
    across_y=(zz[iz] > 4-ROOF_SLOPE*yy[y_next]+1e-10)
    across_z=(zz[z_next] > 4-ROOF_SLOPE*yy[iy]+1e-10)
    axis_y=near & (~adj[:,2]) & across_y
    axis_z=near & (~adj[:,4]) & across_z
    y_count=int(np.count_nonzero(axis_y))
    z_count=int(np.count_nonzero(axis_z))
    if y_count+z_count<10:
        raise ValueError("original native full room sloping stair Neumann roof axes not exposed")
    true_derivative=float(PHYSICAL_ROOF_TANGENT@PHYSICAL_ROOF_NORMAL)
    rms=float(np.sqrt((y_count*(PHYSICAL_ROOF_TANGENT[0]**2)+
                       z_count*(PHYSICAL_ROOF_TANGENT[1]**2))/
                      (y_count+z_count)))
    return {
      "original_true_native_raw_boundary_node_count":len(bn),
      "original_staircase_roof_positive_y_axis_missing_face_count":y_count,
      "original_staircase_roof_positive_z_axis_missing_face_count":z_count,
      "actual_original_stair_boundary_axis_flux_affine_u_y_minus_quarter_z_rms":rms,
      "physical_sloped_roof_exact_affine_u_y_minus_quarter_z_normal_flux":true_derivative,
      "physical_true_roof_unit_normal_yz":PHYSICAL_ROOF_NORMAL.tolist(),
      "physical_tangential_affine_gradient_yz":PHYSICAL_ROOF_TANGENT.tolist(),
      "true_tangent_is_in_Q1_bilinear_space":True,
      "original_native_6neighbor_graph_roof_missing_flux_is_cartesian_axis":True,
      "rms_is_per_missing_cartesian_face_not_3d_physical_flux_integral":True,
      "not_a_canonical_fullwave_nonconvergence_causality_proof":True}
