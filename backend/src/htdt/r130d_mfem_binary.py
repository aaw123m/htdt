"""SHA-bound memory-mapped MFEM operators; no qualification is granted by IO."""
from pathlib import Path
import hashlib
import numpy as np
from scipy.sparse import csr_matrix


def load_mfem_binary(path: Path, expected_sha256: str, expected_points, *, max_dofs=40000):
    with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    if sha!=expected_sha256:raise ValueError('MFEM binary SHA256 mismatch')
    blob=np.memmap(path,dtype=np.uint8,mode='r');offset=0
    def take(dtype,count):
        nonlocal offset
        size=np.dtype(dtype).itemsize*count
        if count<0 or offset+size>len(blob):raise ValueError('truncated MFEM binary')
        values=np.frombuffer(blob,dtype=dtype,count=count,offset=offset);offset+=size
        return values
    if bytes(take('u1',8))!=b'R130DM01':raise ValueError('MFEM binary magic changed')
    version,n,order,refinement,elements=map(int,take('<u4',5))
    if (version!=1 or not 0<n<=max_dofs or not 0<=refinement<=4 or order<1
        or n!=(2**refinement*order+1)**3 or elements!=6*8**refinement):
        raise ValueError('MFEM binary discretization invalid')
    density,c,volume=take('<f8',3)
    if (density,c,volume)!=(1.2,343.2,56.):raise ValueError('MFEM rigid physical fixture changed')
    operators=[]
    for _ in range(2):
        nnz=int(take('<u8',1)[0])
        if not 0<nnz<=n*n:raise ValueError('MFEM CSR extent invalid')
        rows=take('<i4',n+1);cols=take('<i4',nnz);values=take('<f8',nnz)
        if (rows[0]!=0 or rows[-1]!=nnz or np.any(np.diff(rows)<0)
            or np.min(cols)<0 or np.max(cols)>=n or not np.isfinite(values).all()):
            raise ValueError('MFEM CSR structure invalid')
        operators.append(csr_matrix((values,cols,rows),shape=(n,n),copy=False))
    count=int(take('<u4',1)[0]);points=np.asarray(expected_points,float)
    if count!=82 or points.shape!=(82,3) or not np.isfinite(points).all():raise ValueError('fixed cloud required')
    cloud=[]
    for i in range(count):
        xyz=take('<f8',3);size=int(take('<u4',1)[0])
        if not 0<size<=min(n,1000):raise ValueError('point functional extent invalid')
        indices=take('<i4',size);values=take('<f8',size)
        if (not np.array_equal(xyz,points[i]) or np.min(indices)<0 or np.max(indices)>=n
            or len(np.unique(indices))!=size or not np.isfinite(values).all()
            or abs(sum(values)-1)>2e-10):raise ValueError('point functional identity failed')
        cloud.append((indices,values))
    if offset!=len(blob):raise ValueError('unexpected MFEM trailing payload')
    M,K=operators;volume_error=float(abs(M.sum()-56.))
    normK=float(abs(K).sum(axis=1).max());rigid=float(max(abs(K@np.ones(n))))/max(normK,1.)
    if volume_error>1e-8 or rigid>1e-12:raise ValueError('MFEM volume or natural Neumann failed')
    return M,K,cloud,{'sha256':sha,'bytes':len(blob),'all_dofs':n,'order':order,
        'refinement':refinement,'volume_error_m3':volume_error,'rigid_scaled_residual':rigid,
        'mass_data_memory_mapped':bool(np.shares_memory(M.data,blob)),
        'stiffness_data_memory_mapped':bool(np.shares_memory(K.data,blob)),
        'convergence_qualified':False}


def cloud_functional(cloud,indices,weights,n):
    if (len(indices)!=8 or len(weights)!=8 or type(n) is not int or n<1
        or any(type(index) is not int or not 0<=index<len(cloud) for index in indices)
        or not np.isfinite(weights).all() or abs(sum(weights)-1)>2e-10):
        raise ValueError('original eight-point weights invalid')
    b=np.zeros(n)
    for index,weight in zip(indices,weights):
        dofs,values=cloud[index];np.add.at(b,dofs,weight*values)
    return b
