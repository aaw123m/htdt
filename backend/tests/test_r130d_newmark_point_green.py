import numpy as np
import pytest
from scipy.special import eval_genlaguerre
from htdt.r130d_newmark_point_green import newmark_point_green_stream

def test_against_closed_z_transform_and_its_radial_derivative():
    r=np.array([.1,1.,3.]);dt=.00013;c=343.2;nt=2048
    states=list(newmark_point_green_stream(r,dt,nt,c=c))
    phi=np.array([a for a,b in states]);gradient=np.array([b for a,b in states])
    for z in (.8*np.exp(.2j),.98*np.exp(.04j)):
        kernel=z**np.arange(nt)
        s=2/(c*dt)*(1-z)/(1+z)
        exact=z*np.exp(-s*r)/(np.pi*r*(1+z)**2)
        np.testing.assert_allclose(kernel@phi,exact,rtol=2e-10,atol=2e-12)
        np.testing.assert_allclose(kernel@gradient,(-s-1/r)*exact,rtol=2e-10,atol=2e-10)

def test_long_path_survives_initial_exponential_underflow():
    # e^-a is zero in ordinary floating point here, but the later wave is not.
    r=np.array([84.]);dt=.000131;nt=1908;c=343.2
    phi=np.array([a[0] for a,b in newmark_point_green_stream(r,dt,nt,c=c)])
    assert phi[1]==0 and max(abs(phi[-50:]))>1e-5
    # Independent contour coefficient extraction from the Yukawa resolvent.
    N=16384;radius=np.exp(-7/nt);z=radius*np.exp(2j*np.pi*np.arange(N)/N)
    F=z*np.exp(-2*r[0]/(c*dt)*(1-z)/(1+z))/(np.pi*r[0]*(1+z)**2)
    reference=np.fft.fft(F)/N/radius**np.arange(N)
    np.testing.assert_allclose(phi[-80:],reference[:nt].real[-80:],rtol=2e-7,atol=2e-10)

def test_short_records_agree_with_standard_laguerre_library():
    r=np.array([.05,.5,1.]);dt=.0002;nt=29;x=4*r/(343.2*dt)
    actual=np.array([a for a,b in newmark_point_green_stream(r,dt,nt)])
    for n in range(1,nt):
        expected=(-1)**(n-1)*np.exp(-x/2)*eval_genlaguerre(n-1,1,x)/(np.pi*r)
        np.testing.assert_allclose(actual[n],expected,rtol=1e-11,atol=1e-13)

@pytest.mark.parametrize('r',[[0.],[-1.],[float('nan')]])
def test_coincident_or_invalid_points_rejected(r):
    with pytest.raises(ValueError):next(newmark_point_green_stream(r,.001,9))
