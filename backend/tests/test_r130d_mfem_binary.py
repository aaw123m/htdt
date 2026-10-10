import hashlib,struct
import numpy as np
import pytest
from scipy.sparse import csr_matrix
from htdt.r130d_mfem_binary import load_mfem_binary,cloud_functional,csr_norm_inf

def fixture(tmp_path,alter=b''):
    path=tmp_path/'operators.bin';n=8;points=np.zeros((82,3))
    M=csr_matrix(7*np.eye(n));K=csr_matrix(np.diag([1.,2.,2.,2.,2.,2.,2.,1.])-np.eye(n,k=1)-np.eye(n,k=-1))
    for i in range(n):
        sl=slice(K.indptr[i],K.indptr[i+1])
        K.indices[sl]=K.indices[sl][::-1];K.data[sl]=K.data[sl][::-1]
    K.has_sorted_indices=False
    raw=b'R130DM01'+struct.pack('<5I3d',1,n,1,0,6,1.2,343.2,56.)
    for mat in (M,K):
        raw+=struct.pack('<Q',mat.nnz)+mat.indptr.astype('<i4').tobytes()+mat.indices.astype('<i4').tobytes()+mat.data.astype('<f8').tobytes()
    raw+=struct.pack('<I',82)
    for xyz in points:raw+=struct.pack('<3dI2i2d',*xyz,2,0,1,.2,.8)
    raw+=alter;path.write_bytes(raw)
    return path,hashlib.sha256(raw).hexdigest(),points,M,K

def test_matrix_values_and_weighted_functionals_are_preserved(tmp_path):
    path,sha,points,expectedM,expectedK=fixture(tmp_path)
    M,K,cloud,proof=load_mfem_binary(path,sha,points)
    np.testing.assert_array_equal(M.toarray(),expectedM.toarray())
    np.testing.assert_array_equal(K.toarray(),expectedK.toarray())
    assert proof['mass_data_memory_mapped'] and proof['stiffness_data_memory_mapped']
    assert K.has_sorted_indices is False
    before=K.indices.copy()
    assert csr_norm_inf(K)==4.
    np.testing.assert_array_equal(K.indices,before)
    b=cloud_functional(cloud,list(range(8)),np.ones(8)/8,8)
    np.testing.assert_allclose(b,[.2,.8,0,0,0,0,0,0],atol=1e-15)

@pytest.mark.parametrize('alter',[b'x',b'R130D'])
def test_trailing_payload_rejected_even_with_valid_digest(tmp_path,alter):
    path,sha,points,*_=fixture(tmp_path,alter)
    with pytest.raises(ValueError,match='trailing'):load_mfem_binary(path,sha,points)

def test_digest_checked_before_mapping(tmp_path,monkeypatch):
    path,sha,points,*_=fixture(tmp_path)
    monkeypatch.setattr(np,'memmap',lambda *a,**kw:pytest.fail('mapped unverified file'))
    with pytest.raises(ValueError,match='SHA256'):load_mfem_binary(path,'0'*64,points)

def test_point_cloud_and_truncated_data_rejected(tmp_path):
    path,sha,points,*_=fixture(tmp_path)
    points[0,0]=1.
    with pytest.raises(ValueError,match='identity'):load_mfem_binary(path,sha,points)
    path.write_bytes(path.read_bytes()[:-2]);sha=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='truncated'):load_mfem_binary(path,sha,np.zeros((82,3)))

def test_norm_preserves_empty_rows_and_readonly_unsorted_data():
    K=csr_matrix((np.array([2.,-3.,4.]),np.array([2,0,1]),np.array([0,2,2,3,3])),shape=(4,4))
    K.data.flags.writeable=False;K.indices.flags.writeable=False;K.indptr.flags.writeable=False
    assert csr_norm_inf(K)==5.
