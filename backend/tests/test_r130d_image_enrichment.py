import numpy as np
import pytest
from scipy.sparse import csr_matrix
from htdt.r130d_image_enrichment import (PLANES,first_reflection_sources,image_field_stream,
    integrate_boundary_correction,enriched_image_q0_trace)
from htdt.r130d_newmark_point_green import newmark_point_green_stream
from htdt.r130d_boundary_enrichment import enriched_q0_trace

def source():return np.tile([1.5,2.,2.],(8,1))

def test_every_first_reflection_is_exterior_and_neumann_flux_cancels():
    groups=first_reflection_sources(source())
    assert len(groups)==7
    for i,plane in enumerate(PLANES):
        normal,offset=plane[:3],plane[3];s=groups[0][0];im=groups[i+1][0]
        assert normal@im>offset
        point=s+(offset-normal@s)*normal
        da=point-s;db=point-im;r=np.array([np.linalg.norm(da),np.linalg.norm(db)])
        projection=np.array([normal@da,normal@db])/r
        derivatives=np.array([g for p,g in newmark_point_green_stream(r,.0003,512)])
        flux=derivatives*projection[None,:]
        assert np.max(abs(flux))>1e-3
        np.testing.assert_allclose(flux.sum(axis=1),0.,atol=1e-10,rtol=0.)

@pytest.mark.parametrize('location',[[0.,2.,2.],[5.,2.,2.],[1.5,2.,4.]])
def test_boundary_or_exterior_original_source_cannot_be_added(location):
    with pytest.raises(ValueError):first_reflection_sources(np.tile(location,(8,1)))

def test_all_images_match_closed_point_green_generating_function_without_renormalization():
    groups=first_reflection_sources(source());recv=np.tile([2.5,2.,2.],(8,1));sw=rw=np.ones(8)/8
    xyz=np.array([[0.,2.,2.]]);normal=np.array([[-1.,0.,0.]])
    states=list(image_field_stream(groups,recv,sw,rw,xyz,normal,csr_matrix((3,1)),.0003,1024))
    phi=np.array([p for p,g in states]);z=.9*np.exp(.1j);s=2/(343.2*.0003)*(1-z)/(1+z)
    distances=np.array([np.linalg.norm(recv[0]-group[0]) for group in groups])
    expected=np.sum(z*np.exp(-s*distances)/(np.pi*distances*(1+z)**2))
    np.testing.assert_allclose(z**np.arange(1024)@phi,expected,rtol=2e-11,atol=1e-12)
    assert all(np.count_nonzero(g)==0 for p,g in states)

def test_streamed_forcing_matches_independent_coupled_first_order_update():
    M=np.diag([1.,2.,3.]);K=np.array([[2.,-1.,-1.],[-1.,3.,-2.],[-1.,-2.,3.]])
    dt=.01;nt=80;r=np.array([.2,.3,.5]);t=np.arange(nt)*dt
    forces=np.sin(t[:,None]*np.array([3.,4.,7.]));free=.3*np.sin(t*2)
    stream=iter(zip(free,forces))
    _,phi,p,w,H,audit=integrate_boundary_correction(csr_matrix(M),csr_matrix(K),r,dt,nt,stream)
    operator=np.block([[np.zeros((3,3)),np.eye(3)],[-np.linalg.solve(M,K),np.zeros((3,3))]])
    left=np.eye(6)-dt*operator/2;right=np.eye(6)+dt*operator/2;state=np.zeros(6);expected=np.zeros(nt)
    for j in range(1,nt):
        state=np.linalg.solve(left,right@state+dt/2*np.r_[np.zeros(3),np.linalg.solve(M,forces[j-1]+forces[j])])
        expected[j]=r@state[:3]+free[j]
    np.testing.assert_allclose(phi,expected,rtol=2e-12,atol=1e-12)
    np.testing.assert_allclose(p,1.2*np.gradient(expected,dt,edge_order=2),rtol=2e-11,atol=1e-12)
    assert audit['max_correction_energy_work_defect']<1e-12

def test_memory_guard_does_not_consume_the_image_generator():
    def inaccessible():
        raise AssertionError('image generator consumed before budget guard')
        yield None
    with pytest.raises(ValueError,match='memory'):
        integrate_boundary_correction(csr_matrix(np.eye(3)),csr_matrix(np.eye(3)),np.ones(3),.001,10,inaccessible(),memory_budget_bytes=1)

def test_original_only_stream_reproduces_the_registered_unenriched_solver():
    M=csr_matrix(np.diag([1.,2.,3.]));K=csr_matrix([[2.,-1.,-1.],[-1.,3.,-2.],[-1.,-2.,3.]])
    receiver=np.array([.1,.3,.6]);native={'dt':.0005,'Nt':50}
    s=np.tile([0.,0.,0.],(8,1));r=np.tile([.8,0.,0.],(8,1));sw=rw=np.ones(8)/8
    xyz=np.array([[1.,0.,0.],[0.,1.2,0.]]);normals=np.array([[1.,0.,0.],[0.,1.,0.]])
    B=csr_matrix([[.3,.1],[.2,.2],[.5,.7]])
    old=enriched_q0_trace(M,K,receiver,native,s,r,sw,rw,xyz,normals,B)
    stream=image_field_stream([s],r,sw,rw,xyz,normals,B,native['dt'],native['Nt'])
    new=integrate_boundary_correction(M,K,receiver,native['dt'],native['Nt'],stream)
    for a,b in zip(old[:5],new[:5]):np.testing.assert_allclose(a,b,rtol=1e-12,atol=1e-12)
