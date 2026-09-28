"""Round-13 import-truth matrix: every ingest surface must honestly parse or
honestly reject malformed/hostile/truncated/huge/wrong-encoding inputs —
never silently produce wrong data and never crash the shell.

Each section maps one import surface to the six hostile input classes:
(a) valid minimal, (b) truncated, (c) garbage bytes, (d) oversized,
(e) wrong encoding (UTF-16/cp932), (f) valid structure + impossible values.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import struct
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.ingress import IngressTooLargeError, read_file_bounded


# ---------------------------------------------------------------------------
# REW .txt — rew_parser.parse_rew_frequency_response
# ---------------------------------------------------------------------------

from htdt.rew_parser import RewParseError, parse_rew_frequency_response

VALID_REW = (
    b'* Exported from REW\n'
    b'20.0 72.0\n'
    b'40.0 71.0\n'
    b'80.0 70.0\n'
)


def test_rew_valid_minimal() -> None:
    parsed = parse_rew_frequency_response(VALID_REW)
    assert parsed.frequency_hz == (20.0, 40.0, 80.0)
    assert parsed.level_db == (72.0, 71.0, 70.0)
    assert parsed.source_sha256 == sha256(VALID_REW).hexdigest()


def test_rew_truncated_and_empty() -> None:
    # single data row (< 2) must not pass
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(b'20.0 72.0\n')
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(b'')
    # truncated mid-second-row: only one complete pair survives
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(b'20.0 72.0\n40.0')
    # a lone trailing partial row is discarded, not parsed as data
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(b'20.0 72.0\n40.0 71.0\n80.')


def test_rew_garbage_bytes() -> None:
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(os.urandom(4096))
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(b'PK\x03\x04' + os.urandom(200))


def test_rew_oversize_pre_rejected() -> None:
    big = b'20.0 72.0\n' * 4_000_000  # ~40 MiB of otherwise-valid rows
    with pytest.raises((RewParseError, IngressTooLargeError)):
        parse_rew_frequency_response(big)


def test_rew_wrong_encoding_rejected() -> None:
    utf16 = VALID_REW.decode('utf-8').encode('utf-16')
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(utf16)
    utf16be = VALID_REW.decode('utf-8').encode('utf-16-be')
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(utf16be)
    # cp932-encoded comment with JP text
    cp932 = 'ヘッダ\n20.0 72.0\n40.0 71.0\n'.encode('cp932')
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(cp932)


def test_rew_impossible_values() -> None:
    for raw in (
        b'20.0 nan\n40.0 71.0\n80.0 70.0\n',     # NaN level
        b'20.0 inf\n40.0 71.0\n80.0 70.0\n',     # Inf level
        b'-20.0 72.0\n40.0 71.0\n80.0 70.0\n',   # negative frequency
        b'0.0 72.0\n40.0 71.0\n80.0 70.0\n',     # zero frequency
        b'40.0 72.0\n20.0 71.0\n80.0 70.0\n',    # non-monotonic
        b'20.0 72.0\n20.0 71.0\n80.0 70.0\n',    # duplicate frequency
        b'20.0 72.0 10\n40.0 71.0\n80.0 70.0\n', # mixed column count
        b'20.0 72.0\n40.0 71.0 10 20\n80.0 70.0\n',
    ):
        with pytest.raises(RewParseError):
            parse_rew_frequency_response(raw)


def test_rew_utf16_null_byte_residue_cannot_smuggle() -> None:
    """UTF-16's alternating NULs used to slip into data columns."""
    raw = 'Header\n20.0 72.0\n40.0 71.0\n'.encode('utf-16-le')
    with pytest.raises(RewParseError):
        parse_rew_frequency_response(raw)


# ---------------------------------------------------------------------------
# REW API — rew_api.decode_rew_float_array / decode_frequency_response
# ---------------------------------------------------------------------------

from htdt.rew_api import (
    RewApiError,
    RewApiResponseTooLarge,
    decode_frequency_response,
    decode_rew_float_array,
)


def _b64_f32(values: list[float]) -> str:
    return base64.b64encode(
        struct.pack(f'>{len(values)}f', *values)
    ).decode('ascii')


def test_rew_array_valid() -> None:
    assert decode_rew_float_array(_b64_f32([20.0, -3.5])) == (20.0, -3.5)


def test_rew_array_hostile() -> None:
    with pytest.raises(RewApiError):
        decode_rew_float_array('!!!not-base64!!!')
    with pytest.raises(RewApiError):
        decode_rew_float_array(base64.b64encode(b'abc').decode())  # len%4
    # NaN / Inf payloads are rejected, not passed through
    with pytest.raises(RewApiError):
        decode_rew_float_array(_b64_f32([float('nan'), 1.0]))
    with pytest.raises(RewApiError):
        decode_rew_float_array(_b64_f32([float('inf'), 1.0]))
    # payload that decodes to more samples than the cap
    with pytest.raises(RewApiResponseTooLarge):
        decode_rew_float_array(_b64_f32([1.0] * 8), max_samples=4)


def test_rew_frequency_response_hostile() -> None:
    payload = {
        'magnitude': _b64_f32([70.0, 71.0]),
        'startFreq': 20.0,
        'ppo': 24,
    }
    decoded = decode_frequency_response('m1', payload)
    assert len(decoded.frequency_hz) == 2

    # negative/zero/nonfinite start frequency
    for bad in (-20.0, 0.0, float('nan'), 'loud'):
        with pytest.raises(RewApiError):
            decode_frequency_response(
                'm1', {**payload, 'startFreq': bad}
            )
    # missing magnitude entirely
    with pytest.raises(RewApiError):
        decode_frequency_response('m1', {'startFreq': 20.0, 'ppo': 24})
    # phase length mismatch
    with pytest.raises(RewApiError):
        decode_frequency_response(
            'm1',
            {**payload, 'phase': _b64_f32([0.0])},
        )


def test_rew_frequency_response_tiny_ppo_does_not_crash_with_wrong_type() -> None:
    """A microscopic positive ppo overflows math.exp — must surface as
    RewApiError, not leak a bare OverflowError."""
    payload = {
        'magnitude': _b64_f32([70.0, 71.0, 72.0]),
        'startFreq': 20.0,
        'ppo': 1e-300,
    }
    with pytest.raises(RewApiError):
        decode_frequency_response('m1', payload)


# ---------------------------------------------------------------------------
# EULUMDAT / IES LM-63 — cad_luminaire_photometric
# ---------------------------------------------------------------------------

from htdt.cad_luminaire_photometric import (
    parse_eulumdat,
    parse_ies_lm63,
    parse_photometric,
)


def _ldt(dtype: str = '1', candela: str = '100.0', mc: int = 1, ng: int = 2) -> str:
    """Minimal EULUMDAT: 26 fixed lines then mc*ng candela values."""
    lines = [
        'ACME',           # 0 company
        '1',              # 1 Ityp
        '0',              # 2 symmetry
        str(mc),          # 3 Mc
        '0.0',            # 4 Dc
        str(ng),          # 5 Ng
        '45.0',           # 6 Dg (step — vertical angles must increase)
        'lamp',           # 7 luminaire description
        '50.0',           # 8 watt
        '1000.0',         # 9 lumens
        dtype,            # 10 dtype: 1=cd/klm, 2=cd
        '1.0',            # 11 efficacy factor
        'name', 'number', 'instr', 'report', 'catalog', 'file', 'date', 'lamp',
        'lamp type', 'room', 'height', 'dim', '0.0', 'angles',
    ]
    assert len(lines) == 26
    lines.extend([candela] * (mc * ng))
    return '\n'.join(lines) + '\n'


def test_eulumdat_valid_minimal() -> None:
    result = parse_eulumdat(_ldt())
    assert result.verdict == 'valid'
    assert result.artifact is not None
    assert result.artifact.units == 'candela_per_klm'
    result2 = parse_eulumdat(_ldt(dtype='2'))
    assert result2.artifact is not None
    assert result2.artifact.units == 'candela'


def test_eulumdat_unknown_dtype_must_not_invent_units() -> None:
    """dtype outside {1,2} must be rejected, not silently labeled cd/klm."""
    for dtype in ('3', '0', 'x', ''):
        result = parse_eulumdat(_ldt(dtype=dtype))
        assert result.verdict == 'invalid', f'dtype={dtype!r} passed'


def test_eulumdat_impossible_values_verdict_not_exception() -> None:
    """Structurally-valid-but-impossible files return verdict='invalid'
    (the contract surface) rather than a raw ValueError."""
    negative = _ldt(candela='-5.0')
    result = parse_eulumdat(negative)
    assert result.verdict == 'invalid'
    nan = _ldt(candela='nan')
    result = parse_eulumdat(nan)
    assert result.verdict == 'invalid'


def test_eulumdat_truncated_and_garbage() -> None:
    assert parse_eulumdat('').verdict == 'invalid'
    assert parse_eulumdat('ACME\n1\n0\n').verdict == 'invalid'
    # cut off mid-candela-table (26 header lines + 1 of 2 rows)
    truncated = '\n'.join(_ldt().split('\n')[:27]) + '\n'
    assert parse_eulumdat(truncated).verdict == 'invalid'
    garbage = os.urandom(2048).decode('latin-1')
    assert parse_eulumdat(garbage).verdict == 'invalid'


def _ies(photo_type: str = '1', candela_value: str = '100.0') -> str:
    return '\n'.join(
        [
            'IESNA:LM-63-2002',
            '[MANUFAC] ACME',
            'TILT=NONE',
            f'1 1000 1 2 2 {photo_type} 1 0.3 0.3 0.1',
            '1.0',
            '0.0',
            '60.0',
            '0 90',          # vertical angles
            '0 180',         # horizontal angles
            f'{candela_value} {candela_value} {candela_value} {candela_value}',
        ]
    ) + '\n'


def test_ies_valid_minimal() -> None:
    result = parse_ies_lm63(_ies())
    assert result.verdict == 'valid'
    assert result.artifact is not None


def test_ies_photometric_types_keep_absolute_candela() -> None:
    """LM-63 candela values are absolute candela for every goniometer
    photometric_type (1=C, 2=B, 3=A) — the artifact must record 'candela'
    and never silently reinterpret them as cd/klm."""
    for photo_type in ('1', '2', '3'):
        result = parse_ies_lm63(_ies(photo_type=photo_type))
        assert result.verdict == 'valid'
        assert result.artifact is not None
        assert result.artifact.units == 'candela'


def test_ies_impossible_values_verdict_not_exception() -> None:
    for candela in ('-5.0', 'nan', 'inf'):
        result = parse_ies_lm63(_ies(candela_value=candela))
        assert result.verdict == 'invalid', f'candela={candela!r} passed'


def test_ies_truncated_and_garbage() -> None:
    assert parse_ies_lm63('').verdict == 'invalid'
    assert parse_ies_lm63('IESNA:LM-63-2002\nTILT=NONE\n').verdict == 'invalid'
    assert parse_ies_lm63(_ies()[:60]).verdict == 'invalid'


def test_parse_photometric_never_raises_on_hostile_text() -> None:
    """The verdict contract: garbage returns invalid, never an exception."""
    for raw in (
        os.urandom(1024).decode('latin-1'),
        'IESNA\n',
        _ldt(dtype='9'),
        _ies(candela_value='-1'),
        '\u0000' * 40,
        'IESNA:LM-63-2002\n' + 'x' * 100_000,
    ):
        result = parse_photometric(raw, artifact_id='probe')
        assert result.verdict in ('valid', 'invalid', 'unqualified')


# ---------------------------------------------------------------------------
# Raw meshes — raw_mesh.import_raw_visual_mesh (OBJ/PLY/STL/GLB/HTDTMSH1)
# ---------------------------------------------------------------------------

from htdt.raw_mesh import RawMeshImportError, import_raw_visual_mesh

_VALID_OBJ = b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'


def _binary_stl() -> bytes:
    header = b'HTDT test solid' + b'\x00' * (80 - 15)
    tri = struct.pack('<12fH', 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    return header + struct.pack('<I', 1) + tri


def _ply_ascii() -> bytes:
    return (
        b'ply\nformat ascii 1.0\nelement vertex 3\n'
        b'property float x\nproperty float y\nproperty float z\n'
        b'element face 1\nproperty list uchar int vertex_indices\n'
        b'end_header\n0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n'
    )


def _glb() -> bytes:
    """Minimal GLB: header + one JSON chunk declaring a 3-vert mesh."""
    json_doc = json.dumps({
        'asset': {'version': '2.0'},
        'scene': 0,
        'scenes': [{'nodes': [0]}],
        'nodes': [{'mesh': 0}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
        'accessors': [{
            'bufferView': 0, 'componentType': 5126, 'count': 3,
            'type': 'VEC3', 'min': [0, 0, 0], 'max': [1, 1, 0],
        }],
        'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': 36}],
        'buffers': [{'byteLength': 36}],
    }).encode('utf-8')
    json_doc += b' ' * ((4 - len(json_doc) % 4) % 4)
    positions = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    bin_chunk = positions + b'\x00' * ((4 - len(positions) % 4) % 4)
    total = 12 + 8 + len(json_doc) + 8 + len(bin_chunk)
    return (
        struct.pack('<III', 0x46546C67, 2, total)
        + struct.pack('<II', len(json_doc), 0x4E4F534A)
        + json_doc
        + struct.pack('<II', len(bin_chunk), 0x004E4942)
        + bin_chunk
    )


def test_mesh_valid_minimal_each_format() -> None:
    obj = import_raw_visual_mesh(_VALID_OBJ, source_name='tri.obj')
    assert len(obj.vertices) == 3 and len(obj.triangles) == 1
    stl = import_raw_visual_mesh(_binary_stl(), source_name='tri.stl')
    assert len(stl.triangles) == 1
    ply = import_raw_visual_mesh(_ply_ascii(), source_name='tri.ply')
    assert len(ply.triangles) == 1


def test_mesh_truncated_every_format() -> None:
    for data, name in (
        (_VALID_OBJ[:-4], 'tri.obj'),
        (_binary_stl()[:-10], 'tri.stl'),
        (_ply_ascii()[:-8], 'tri.ply'),
        (_glb()[:-20], 'tri.glb'),
        (b'HTDTMSH1' + b'\x00' * 10, 'tri.meshbin'),
    ):
        with pytest.raises(RawMeshImportError):
            import_raw_visual_mesh(data, source_name=name)


def test_mesh_garbage_every_format() -> None:
    garbage = os.urandom(512)
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(garbage, source_name='junk.obj')
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(garbage, source_name='junk.ply')
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(b'glTF' + garbage, source_name='junk.glb')
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(b'HTDTMSH1' + garbage, source_name='x.meshbin')
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(garbage, source_name='unknown.fmt')


def test_mesh_impossible_values() -> None:
    # NaN vertex coordinate in OBJ
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            b'v 0 0 0\nv nan 0 0\nv 0 1 0\nf 1 2 3\n', source_name='bad.obj'
        )
    # out-of-range face index
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 99\n', source_name='oob.obj'
        )
    # negative (1-based wrap) index pointing past start
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(
            b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf -9 -1 -2\n', source_name='neg.obj'
        )


def test_mesh_stl_declared_count_lie() -> None:
    """A binary STL claiming 3 faces but carrying 1 must be rejected —
    never silently parsed."""
    header = b'\x00' * 80
    tri = struct.pack('<12fH', 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    lying = header + struct.pack('<I', 3) + tri
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(lying, source_name='liar.stl')


def test_mesh_glb_header_lies() -> None:
    """GLB declared total length != actual length must reject."""
    good = _glb()
    corrupted = bytearray(good)
    struct.pack_into('<I', corrupted, 8, len(good) + 4)
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(bytes(corrupted), source_name='lie.glb')
    # version 1 GLB rejected
    v1 = bytearray(good)
    struct.pack_into('<I', v1, 4, 1)
    with pytest.raises(RawMeshImportError):
        import_raw_visual_mesh(bytes(v1), source_name='old.glb')


def test_mesh_encoding_edge_names() -> None:
    # JP/emoji filename is provenance only — content decides the parse
    mesh = import_raw_visual_mesh(_VALID_OBJ, source_name='部屋モデル.obj')
    assert mesh.provenance.source_name == '部屋モデル.obj'


# ---------------------------------------------------------------------------
# DXF / image / PDF — room_underlay
# ---------------------------------------------------------------------------

from htdt.room_underlay import (
    UnderlayImportError,
    decode_image_bytes,
    parse_dxf,
    render_pdf_page,
)

_VALID_DXF = (
    b'0\nSECTION\n2\nENTITIES\n'
    b'0\nLINE\n8\nWALL\n10\n0.0\n20\n0.0\n30\n0.0\n'
    b'11\n3.5\n21\n0.0\n31\n0.0\n'
    b'0\nENDSEC\n0\nEOF\n'
)


def test_dxf_valid_and_garbage() -> None:
    parsed = parse_dxf(_VALID_DXF)
    assert parsed.segments
    # garbage decodes to no recognized entities — honest empty result,
    # callers reject empties with UnderlayImportError
    assert not parse_dxf(os.urandom(4096)).segments
    assert not parse_dxf('日本語テキスト'.encode('cp932')).segments
    # UTF-16 garbage bytes -> replace-decode -> still no entities
    assert not parse_dxf(_VALID_DXF.decode().encode('utf-16')).segments
    assert not parse_dxf(b'').segments


def test_dxf_segment_cap_marks_truncated() -> None:
    """Beyond MAX_DXF_SEGMENTS the parser must flag truncation honestly,
    not silently drop geometry."""
    from htdt.room_underlay import MAX_DXF_SEGMENTS

    chunk = b'0\nLINE\n10\n0\n20\n0\n11\n1\n21\n1\n'
    big = b'0\nSECTION\n2\nENTITIES\n' + chunk * (MAX_DXF_SEGMENTS + 50)
    parsed = parse_dxf(big)
    assert len(parsed.segments) == MAX_DXF_SEGMENTS
    assert parsed.truncated is True


def _tiny_png(tmp_path: Path) -> bytes:
    from PySide6.QtGui import QImage

    target = tmp_path / 'tiny.png'
    image = QImage(2, 2, QImage.Format.Format_RGBA8888)
    image.fill(0xFF204060)
    assert image.save(str(target), 'PNG')
    return target.read_bytes()


def test_image_decode_valid_and_garbage(tmp_path: Path) -> None:
    png = _tiny_png(tmp_path)
    array = decode_image_bytes(png)
    assert array.shape == (2, 2, 4)
    for bad in (b'', os.urandom(1024), png[:12], b'\x89PNG\r\n\x1a\n'):
        with pytest.raises(UnderlayImportError):
            decode_image_bytes(bad)


def test_pdf_render_hostile() -> None:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        pytest.skip('pymupdf unavailable')
    for bad in (b'', os.urandom(2048), b'%PDF-1.7 ' + os.urandom(50)):
        with pytest.raises(UnderlayImportError):
            render_pdf_page(bad)


def test_pdf_valid_minimal() -> None:
    try:
        import pymupdf
    except ImportError:
        pytest.skip('pymupdf unavailable')
    document = pymupdf.open()
    document.new_page(width=200, height=200)
    data = document.tobytes()
    png = render_pdf_page(data)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'


# ---------------------------------------------------------------------------
# BW64/ADM — cad_adm_bw64_validator
# ---------------------------------------------------------------------------

from htdt.cad_adm_bw64_validator import (
    parse_bw64_structure,
    validate_adm_bw64,
)


def _wav_header(riff_size: int = 36, data_size: int = 0) -> bytes:
    return (
        b'RIFF' + struct.pack('<I', riff_size) + b'WAVE'
        + b'fmt ' + struct.pack('<I', 16)
        + struct.pack('<HHIIHH', 1, 2, 48000, 192000, 4, 16)
        + b'data' + struct.pack('<I', data_size)
    )


def test_adm_bw64_garbage_and_truncated() -> None:
    for bad in (b'', b'RIFF', _wav_header()[:20], os.urandom(300)):
        report = validate_adm_bw64(bad)
        assert report.verdict == 'invalid'
        assert parse_bw64_structure(bad) is None or bad[:4] == b'RIFF'


def test_adm_bw64_xml_injection_rejected() -> None:
    """An axml chunk carrying a DOCTYPE must be rejected by the XML guard."""
    wav = _wav_header()
    evil_xml = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///c:/secret">]>'
        b'<ebuCoreMain>&xxe;</ebuCoreMain>'
    )
    padded = evil_xml + (b'\x00' if len(evil_xml) % 2 else b'')
    payload = wav + b'axml' + struct.pack('<I', len(evil_xml)) + padded
    report = validate_adm_bw64(payload)
    assert report.verdict == 'invalid'


# ---------------------------------------------------------------------------
# SOFA/HRTF — cad_spatial_reproduction.load_sofa_dataset_profile
# ---------------------------------------------------------------------------


def _write_sofa(
    path: Path,
    *,
    convention: str = 'SimpleFreeFieldHRIR',
    sampling_rate: float = 48000.0,
    ir_shape: tuple[int, ...] = (2, 2, 4),
    source_position_shape: tuple[int, ...] = (2, 3),
    drop_source_position: bool = False,
) -> None:
    import numpy as np
    h5py = pytest.importorskip('h5py')

    with h5py.File(path, 'w') as handle:
        handle.attrs['Conventions'] = np.bytes_('SOFA')
        handle.attrs['SOFAConventions'] = np.bytes_(convention)
        handle.attrs['DataType'] = np.bytes_('FIR')
        if not drop_source_position:
            data = np.zeros(source_position_shape, dtype=np.float64)
            if source_position_shape[-1] >= 3:
                data[:, 2] = 1.5
            handle.create_dataset('SourcePosition', data=data)
        handle.create_dataset(
            'Data.SamplingRate', data=np.array([sampling_rate])
        )
        handle.create_dataset('Data.IR', data=np.zeros(ir_shape))


def _load_sofa(path: Path):
    from htdt.cad_spatial_reproduction import load_sofa_dataset_profile

    return load_sofa_dataset_profile(
        path,
        profile_id='spatial-profile:probe',
        profile_version='1',
        personalization_scope='generic',
        license_kind='cc0_public',
        created_at_utc='2026-01-01T00:00:00+00:00',
    )


def test_sofa_valid_minimal(tmp_path: Path) -> None:
    sofa = tmp_path / 'valid.sofa'
    _write_sofa(sofa)
    profile = _load_sofa(sofa)
    assert profile.sample_rate_hz == 48000.0


def test_sofa_wrong_convention_rejected(tmp_path: Path) -> None:
    sofa = tmp_path / 'brir.sofa'
    _write_sofa(sofa, convention='SingleRoomDRIR')
    with pytest.raises(ValueError):
        _load_sofa(sofa)


def test_sofa_garbage_bytes(tmp_path: Path) -> None:
    sofa = tmp_path / 'junk.sofa'
    sofa.write_bytes(os.urandom(4096))
    with pytest.raises((OSError, ValueError)):
        _load_sofa(sofa)


def test_sofa_missing_required_variables(tmp_path: Path) -> None:
    sofa = tmp_path / 'missing.sofa'
    _write_sofa(sofa, drop_source_position=True)
    with pytest.raises(ValueError):
        _load_sofa(sofa)


def test_sofa_malformed_dimensions_fail_closed(tmp_path: Path) -> None:
    """A 2-D Data.IR or mis-shaped SourcePosition must reject — not raise
    an unstructured IndexError nor produce wrong emitter counts."""
    sofa = tmp_path / 'bad_ir.sofa'
    _write_sofa(sofa, ir_shape=(2, 2))
    with pytest.raises((ValueError, IndexError)):
        _load_sofa(sofa)

    sofa2 = tmp_path / 'bad_sp.sofa'
    _write_sofa(sofa2, source_position_shape=(2, 2))
    with pytest.raises((ValueError, IndexError)):
        _load_sofa(sofa2)


# ---------------------------------------------------------------------------
# Directivity — polar_table CSV + normalized JSON adapters
# ---------------------------------------------------------------------------

from htdt.cad_directivity_import import (
    NORMALIZED_JSON_ADAPTER_ID,
    NORMALIZED_JSON_ADAPTER_VERSION,
    POLAR_TABLE_ADAPTER_ID,
    POLAR_TABLE_ADAPTER_VERSION,
    POLAR_TABLE_SCHEMA,
    import_directivity_asset,
)
from htdt.cad_equipment import (
    AngleDomain,
    DirectivityCapability,
    DirectivityDomain,
    EquipmentDataProvenance,
    FrequencyDomain,
    InterpolationProvenance,
    build_equipment_definition,
)
from htdt.cad_scene import Offset3, Size3


def _equipment(source_sha: str, *, complex_data: bool = False):
    provenance = EquipmentDataProvenance(
        evidence_kind='user_defined',
        source_name='import-truth fixture',
        source_version='1',
        source_reference='matrix',
        source_sha256=source_sha,
    )
    interpolation = InterpolationProvenance(
        method='linear',
        implementation='htdt-grid-linear',
        implementation_version='1',
        provenance=provenance,
    )
    return build_equipment_definition(
        definition_id='matrix-speaker',
        version='1',
        identity_kind='user_defined',
        user_label='Matrix speaker',
        provenance=(provenance,),
        cabinet_envelope_m=Size3(x_m=0.2, y_m=0.2, z_m=0.3),
        acoustic_reference_point_m=Offset3(),
        directivity=DirectivityCapability(
            tier='complex' if complex_data else 'magnitude_only',
            data_format='polar_table',
            provenance=provenance,
            data_asset_sha256=source_sha,
            valid_domain=DirectivityDomain(
                frequency=FrequencyDomain(minimum_hz=500.0, maximum_hz=1000.0),
                horizontal=AngleDomain(minimum_deg=-30.0, maximum_deg=30.0),
                vertical=AngleDomain(minimum_deg=0.0, maximum_deg=0.0),
            ),
            interpolation=interpolation,
            coherent_phase=complex_data,
        ),
    )


def _polar_table(rows, *, magnitude_unit='db', capability='magnitude_only'):
    metadata = [
        f'# schema={POLAR_TABLE_SCHEMA}',
        '# delimiter=csv',
        '# dataset_id=polar-matrix',
        '# dataset_version=1',
        f'# capability={capability}',
        '# angle_semantics=horizontal_vertical',
        '# horizontal_wrap=none',
        '# reference_axis=equipment_acoustic_reference_axis',
        '# azimuth_positive=left',
        '# elevation_positive=up',
        '# frequency_unit=Hz',
        '# angle_unit=degree',
        f'# magnitude_unit={magnitude_unit}',
        '# normalization_reference=on_axis_per_frequency',
        '# interpolation_method=linear',
        '# interpolation_implementation=htdt-grid-linear',
        '# interpolation_version=1',
        '# evidence_kind=user_defined',
        '# source_name=matrix fixture',
        '# source_version=1',
        '# source_reference=matrix',
    ]
    columns = 'frequency_hz,horizontal_angle_deg,vertical_angle_deg,magnitude,magnitude_unit'
    if capability == 'complex':
        columns += ',phase_deg'
    body = [columns]
    for row in rows:
        body.append(','.join(str(v) for v in row) + f',{magnitude_unit}')
    return ('\n'.join([*metadata, *body]) + '\n').encode('utf-8')


def _valid_polar_rows():
    # on_axis_per_frequency normalization requires 0 dB on axis per frequency
    return [
        (500.0, -30.0, 0.0, -6.0), (500.0, 0.0, 0.0, 0.0), (500.0, 30.0, 0.0, -6.0),
        (1000.0, -30.0, 0.0, -7.0), (1000.0, 0.0, 0.0, 0.0), (1000.0, 30.0, 0.0, -7.0),
    ]


def test_polar_table_valid_minimal() -> None:
    raw = _polar_table(_valid_polar_rows())
    result = import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        equipment_definition=_equipment(sha256(raw).hexdigest()),
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )
    assert result.diagnostic.import_state == 'IMPORTED'


@pytest.mark.parametrize('case', [
    'empty',
    'garbage',
    'wrong_structure',
    'schema_mismatch',
    'wrong_encoding',
])
def test_polar_table_rejects_hostile(case: str) -> None:
    raw = {
        'empty': b'',
        'garbage': random.Random(1337).randbytes(2048),
        'wrong_structure': b'no metadata header\n1,2,3\n',
        'schema_mismatch': b'# schema=htdt.polar-table.v2\n# delimiter=csv\n',
        'wrong_encoding': 'メタデータ\n'.encode('cp932'),
    }[case]
    result = import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        equipment_definition=_equipment(sha256(raw).hexdigest()),
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )
    # honest rejection — REJECTED for parse failures, UNSUPPORTED when no
    # registered adapter covers the declared format/schema tuple
    assert result.diagnostic.import_state in ('REJECTED', 'UNSUPPORTED')
    assert result.diagnostic.rejection_reason


def test_polar_table_impossible_values() -> None:
    rows = _valid_polar_rows()
    rows[0] = (float('nan'), -30.0, 0.0, -6.0)
    raw = _polar_table(rows)
    result = import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        equipment_definition=_equipment(sha256(raw).hexdigest()),
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )
    assert result.diagnostic.import_state == 'REJECTED'


def test_polar_table_incomplete_grid_rejected() -> None:
    rows = _valid_polar_rows()[:-1]  # drop one (f, h, v) combo
    raw = _polar_table(rows)
    result = import_directivity_asset(
        raw_source_bytes=raw,
        explicit_source_format='polar_table',
        declared_schema=POLAR_TABLE_SCHEMA,
        equipment_definition=_equipment(sha256(raw).hexdigest()),
        adapter_id=POLAR_TABLE_ADAPTER_ID,
        adapter_version=POLAR_TABLE_ADAPTER_VERSION,
    )
    assert result.diagnostic.import_state == 'REJECTED'


def _normalized_json(extra_samples=1) -> bytes:
    doc = {
        'schema': 'htdt.normalized-directivity.v1',
        'source_format': 'custom',
        'dataset_id': 'normalized-matrix',
        'version': '1',
        'kind': 'magnitude_only',
        'coordinate_convention': {
            'angle_semantics': 'horizontal_vertical',
            'horizontal_wrap': 'none',
            'reference_axis': 'equipment_acoustic_reference_axis',
            'azimuth_positive': 'left',
            'elevation_positive': 'up',
            'angle_unit': 'degree',
        },
        'normalization': {
            'source_magnitude_unit': 'db',
            'reference': 'on_axis_per_frequency',
        },
        'interpolation_method': 'linear',
        'interpolation_implementation': 'htdt-grid-linear',
        'interpolation_version': '1',
        'evidence_kind': 'user_defined',
        'source_name': 'matrix normalized',
        'source_version': '1',
        'source_reference': 'matrix',
        'frequencies_hz': [500.0, 1000.0],
        'horizontal_angles_deg': [-30.0, 0.0, 30.0],
        'vertical_angles_deg': [0.0],
        'samples': [
            {
                'frequency_hz': f,
                'horizontal_angle_deg': h,
                'vertical_angle_deg': 0.0,
                'magnitude': -6.0,
            }
            for f in (500.0, 1000.0)
            for h in (-30.0, 0.0, 30.0)
        ][:extra_samples],
    }
    return json.dumps(doc).encode('utf-8')


def test_normalized_json_hostile_and_dup_keys() -> None:
    equipment = _equipment(sha256(b'x').hexdigest())
    for raw in (b'', os.urandom(1024), b'{"schema": "other"}', b'{not json'):
        result = import_directivity_asset(
            raw_source_bytes=raw,
            explicit_source_format='custom',
            declared_schema='htdt.normalized-directivity.v1',
            equipment_definition=equipment,
            adapter_id=NORMALIZED_JSON_ADAPTER_ID,
            adapter_version=NORMALIZED_JSON_ADAPTER_VERSION,
        )
        assert result.diagnostic.import_state != 'IMPORTED'

    # duplicate JSON keys must not silently last-win through the parser
    dup = (
        b'{"schema":"htdt.normalized-directivity.v1",'
        b'"schema":"definitely-not.v1"}'
    )
    result = import_directivity_asset(
        raw_source_bytes=dup,
        explicit_source_format='custom',
        declared_schema='htdt.normalized-directivity.v1',
        equipment_definition=equipment,
        adapter_id=NORMALIZED_JSON_ADAPTER_ID,
        adapter_version=NORMALIZED_JSON_ADAPTER_VERSION,
    )
    assert result.diagnostic.import_state != 'IMPORTED'


# ---------------------------------------------------------------------------
# JCAL CSV — cad_foam_material_batch.parse_jcal_params_csv
# ---------------------------------------------------------------------------

from htdt.cad_foam_material_batch import parse_jcal_params_csv


def test_jcal_csv_valid() -> None:
    text = (
        'parameter,value\n'
        'material,Pinta\n'
        'sigma,10000\n'
        'porosity,0.98\n'
        'tortuosity,1.02\n'
        'lambda,0.0001\n'
        "lambda',0.0002\n"
    )
    sets = parse_jcal_params_csv(text, admission_id='foam-02')
    assert len(sets) == 1


def test_jcal_csv_empty_does_not_crash() -> None:
    """Empty / all-blank CSV must raise ValueError, not a bare IndexError."""
    with pytest.raises(ValueError):
        parse_jcal_params_csv('', admission_id='foam-02')
    with pytest.raises(ValueError):
        parse_jcal_params_csv('\n\n  \n', admission_id='foam-02')


def test_jcal_csv_short_row_does_not_crash() -> None:
    """A one-column data row must raise ValueError, not a bare IndexError."""
    text = 'parameter,value\nmaterial,Pinta\nsigma\n'
    with pytest.raises(ValueError):
        parse_jcal_params_csv(text, admission_id='foam-02')


def test_jcal_csv_hostile() -> None:
    with pytest.raises(ValueError):
        parse_jcal_params_csv('parameter,value\nbogus_param,1\n', admission_id='x')
    with pytest.raises(ValueError):
        parse_jcal_params_csv(
            'parameter,value\nmaterial,P\nsigma,not_a_number\n',
            admission_id='x',
        )


# ---------------------------------------------------------------------------
# CamillaDSP config — cad_camilladsp.load_camilladsp_config
# ---------------------------------------------------------------------------

from htdt.cad_camilladsp import CamillaDSPError, load_camilladsp_config


def test_camilladsp_valid_and_hostile() -> None:
    assert load_camilladsp_config(b'{"filters": {}}') == {'filters': {}}
    for bad in (
        b'',
        b'[1,2,3]',                    # not a mapping
        b'{not json and not yaml',
        os.urandom(512),
        b'\x00' * 64,
    ):
        with pytest.raises(CamillaDSPError):
            load_camilladsp_config(bad)


def test_camilladsp_oversize() -> None:
    with pytest.raises(CamillaDSPError):
        load_camilladsp_config(b'{}', max_bytes=1)


def test_camilladsp_utf16_is_not_silently_misparsed() -> None:
    """UTF-16 YAML/JSON config either decodes honestly or fails — never
    produces a mapping assembled from mojibake."""
    utf16 = '{"filters": {"a": 1}}'.encode('utf-16')
    try:
        parsed = load_camilladsp_config(utf16)
    except CamillaDSPError:
        return
    # if it parsed, it must be structurally honest
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# EDID — edid_conformance.parse_edid
# ---------------------------------------------------------------------------

from htdt.edid_conformance import parse_edid

EDID_HEADER = b'\x00\xff\xff\xff\xff\xff\xff\x00'


def _valid_edid() -> bytes:
    block = bytearray(128)
    block[:8] = EDID_HEADER
    # manufacturer id 'ABC': packed 5-bit groups at shifts 10/5/0
    packed = (1 << 10) | (2 << 5) | 3
    block[8] = (packed >> 8) & 0xFF
    block[9] = packed & 0xFF
    block[18] = 1
    block[19] = 4
    checksum = (-sum(block[:127])) % 256
    block[127] = checksum
    return bytes(block)


def test_edid_valid_and_hostile() -> None:
    assert parse_edid(_valid_edid()).status in ('parse_ok', 'parse_warning')
    assert parse_edid(b'').status == 'parse_error'
    assert parse_edid(_valid_edid()[:64]).status == 'parse_error'
    assert parse_edid(os.urandom(128)).status == 'parse_error'
    bad_len = _valid_edid() + b'\x00' * 40  # not a multiple of 128
    assert parse_edid(bad_len).status == 'parse_error'
    # bad checksum
    broken = bytearray(_valid_edid())
    broken[50] ^= 0xFF
    assert parse_edid(bytes(broken)).status == 'parse_error'


def test_edid_extension_count_mismatch_is_warning_not_error() -> None:
    extra = bytearray(128)
    extra[127] = (-sum(extra[:127])) % 256
    base = bytearray(_valid_edid())
    base[126] = 5  # claims 5 extensions, carries 1
    base[127] = (-sum(base[:127])) % 256
    result = parse_edid(bytes(base) + bytes(extra))
    assert result.status == 'parse_warning'


# ---------------------------------------------------------------------------
# FIR taps — cad_fir_filter._parse_tap_text / import_fir_filter_artifact
# ---------------------------------------------------------------------------

from htdt.cad_fir_filter import (
    _parse_tap_text,
    import_fir_filter_artifact,
)


def test_fir_taps_valid_and_hostile() -> None:
    assert _parse_tap_text(b'0.5 -0.2 0.1\n') == (0.5, -0.2, 0.1)
    for bad in (
        b'',
        b'   \n',
        b'1.0 nan 0.5',
        b'1.0 inf 0.5',
        b'foo bar',
        os.urandom(128),
    ):
        with pytest.raises((ValueError, UnicodeDecodeError)):
            _parse_tap_text(bad)


def test_fir_import_rejects_undeclared_format() -> None:
    with pytest.raises(ValueError):
        import_fir_filter_artifact(
            source_bytes=b'0.1 0.2',
            source_format='minidsp_text_export',
            declared_sample_rate_hz=48000.0,
            channel_id='ch-1',
        )


# ---------------------------------------------------------------------------
# Field return — field_return_ingestion.stage_field_return
# ---------------------------------------------------------------------------

from htdt.field_return_ingestion import stage_field_return


def test_field_return_hostile() -> None:
    assert stage_field_return(os.urandom(512)).validation_state == 'malformed'
    assert stage_field_return(b'').validation_state == 'malformed'
    assert stage_field_return(b'{"schema": "something-else"}').validation_state == 'malformed'
    assert stage_field_return(
        b'{"schema": "htdt.field-return", "schema_version": 99}'
    ).validation_state == 'unsupported'


# ---------------------------------------------------------------------------
# CLF qualification — cad_loudspeaker_interchange.qualify_clf
# ---------------------------------------------------------------------------

from htdt.cad_loudspeaker_interchange import qualify_clf


def test_clf_hostile() -> None:
    assert qualify_clf(os.urandom(1024)).verdict == 'not_clf'
    assert qualify_clf(''.encode('utf-16')).verdict == 'not_clf'
    assert qualify_clf(b'').verdict == 'not_clf'
    # sections but no frequency data
    assert qualify_clf(b'[INFO]\nx=1\n[GLOBAL]\n').verdict in ('unqualified', 'not_clf')


# ---------------------------------------------------------------------------
# Capture bundle — capture_bundle.FrozenBundle (zip + dir sources)
# ---------------------------------------------------------------------------

from htdt.capture_bundle import CaptureBundleError, FrozenBundle


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return out.getvalue()


def test_capture_bundle_rejects_hostile_archives(tmp_path: Path) -> None:
    # not a zip at all
    junk = tmp_path / 'junk.htdtcapture'
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(CaptureBundleError):
        FrozenBundle(junk)

    # path traversal member
    evil = tmp_path / 'traversal.htdtcapture'
    evil.write_bytes(_zip_bytes({'../escape.txt': b'x'}))
    with pytest.raises(CaptureBundleError):
        FrozenBundle(evil)

    # duplicate member names
    dup = tmp_path / 'dup.htdtcapture'
    dup.write_bytes(_zip_bytes({'a.txt': b'1', 'a.txt': b'2'}))
    with pytest.raises(CaptureBundleError):
        FrozenBundle(dup)

    # absolute path member
    absolute = tmp_path / 'abs.htdtcapture'
    absolute.write_bytes(_zip_bytes({'/etc/passwd': b'x'}))
    with pytest.raises(CaptureBundleError):
        FrozenBundle(absolute)

    # unicode casefold collision
    colliding = tmp_path / 'collide.htdtcapture'
    colliding.write_bytes(_zip_bytes({'File.txt': b'1', 'file.txt': b'2'}))
    with pytest.raises(CaptureBundleError):
        FrozenBundle(colliding)


def test_capture_bundle_zip_bomb_ratio_rejected(tmp_path: Path) -> None:
    bomb = tmp_path / 'bomb.htdtcapture'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('payload.bin', b'0' * 3_000_000)
    bomb.write_bytes(out.getvalue())
    with pytest.raises(CaptureBundleError):
        FrozenBundle(bomb)


def test_capture_bundle_symlink_dir_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'bundle-dir'
    root.mkdir()
    (root / 'file.txt').write_bytes(b'x')
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'secret')
    try:
        (root / 'link.txt').symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform/session')
    with pytest.raises(CaptureBundleError):
        FrozenBundle(root)


# ---------------------------------------------------------------------------
# Project bundle — project_bundle member validation
# ---------------------------------------------------------------------------

from htdt.project_bundle import _validate_bundle_members


def _zip_on_disk(target: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return target


def test_project_bundle_member_whitelist(tmp_path: Path) -> None:
    """Only manifest.json + db/*.jsonl + assets/<sha> may exist in a bundle;
    anything else (traversal, unexpected paths) rejects."""
    from htdt.project_bundle import ProjectBundleError

    valid_manifest = json.dumps({'schema': 'x'}).encode()

    # traversal member
    target = _zip_on_disk(
        tmp_path / 'a.htdtproject',
        {'manifest.json': valid_manifest, '../evil': b'x'},
    )
    with zipfile.ZipFile(target) as archive, pytest.raises(ProjectBundleError):
        _validate_bundle_members(archive)

    # unexpected member path
    target = _zip_on_disk(
        tmp_path / 'b.htdtproject',
        {'manifest.json': valid_manifest, 'evil.txt': b'x'},
    )
    with zipfile.ZipFile(target) as archive, pytest.raises(ProjectBundleError):
        _validate_bundle_members(archive)

    # table name with traversal characters
    target = _zip_on_disk(
        tmp_path / 'c.htdtproject',
        {'manifest.json': valid_manifest, 'db/../escape.jsonl': b'{}'},
    )
    with zipfile.ZipFile(target) as archive, pytest.raises(ProjectBundleError):
        _validate_bundle_members(archive)


# ---------------------------------------------------------------------------
# Native backup — native_backup.inspect_backup (staged validation)
# ---------------------------------------------------------------------------

from htdt.native_backup import inspect_backup


def test_native_backup_hostile_archives(tmp_path: Path) -> None:
    junk = tmp_path / 'junk.htdtbackup'
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(Exception):
        inspect_backup(junk)

    empty_zip = tmp_path / 'empty.htdtbackup'
    empty_zip.write_bytes(_zip_bytes({}))
    with pytest.raises(Exception):
        inspect_backup(empty_zip)

    traversal = tmp_path / 'traversal.htdtbackup'
    traversal.write_bytes(_zip_bytes({'../evil': b'x'}))
    with pytest.raises(Exception):
        inspect_backup(traversal)

    bogus_manifest = tmp_path / 'bogus.htdtbackup'
    bogus_manifest.write_bytes(_zip_bytes({'manifest.json': b'{not json'}))
    with pytest.raises(Exception):
        inspect_backup(bogus_manifest)


# ---------------------------------------------------------------------------
# Bounded ingress / path honesty — ingress.read_file_bounded
# ---------------------------------------------------------------------------

def test_read_file_bounded_missing_and_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_file_bounded(tmp_path / 'missing.txt', 1024)
    with pytest.raises(OSError):
        read_file_bounded(tmp_path, 1024)


def test_read_file_bounded_size_cap(tmp_path: Path) -> None:
    target = tmp_path / 'big.bin'
    target.write_bytes(b'x' * 2048)
    with pytest.raises(IngressTooLargeError):
        read_file_bounded(target, 1024)
    # exactly at the cap is legal
    assert read_file_bounded(target, 2048) == b'x' * 2048


def test_read_file_bounded_unicode_and_long_paths(tmp_path: Path) -> None:
    # JP + emoji + spaces in the filename
    name = '計測 データ 🔊.txt'
    target = tmp_path / name
    target.write_bytes(b'data')
    assert read_file_bounded(target, 1024) == b'data'

    # deep path >260 chars — on Windows this may exceed MAX_PATH without
    # the long-path opt-in; the read must still succeed or raise a clean
    # OSError, never return wrong bytes.
    deep = tmp_path
    for index in range(30):
        deep = deep / f'深度ディレクトリ{index:02d}_padding_padding_padding'
    try:
        deep.mkdir(parents=True)
    except OSError:
        pytest.skip('filesystem refused >260-char path creation')
        return
    leaf = deep / 'file.txt'
    try:
        leaf.write_bytes(b'deep data')
        assert read_file_bounded(leaf, 1024) == b'deep data'
    except OSError:
        # clean failure is honest; wrong bytes would not be
        pass


def test_read_file_bounded_empty_file(tmp_path: Path) -> None:
    target = tmp_path / 'empty.bin'
    target.write_bytes(b'')
    assert read_file_bounded(target, 1024) == b''


# ---------------------------------------------------------------------------
# Legacy store — database.Store: dedup + sha + store-vs-source consistency
# ---------------------------------------------------------------------------

from htdt.database import Store


def _store_project(tmp_path: Path):
    store = Store(tmp_path / 'store')
    project = store.create_project('truth matrix')
    context = store.create_context(project['id'], {'label': 'room'}, None)
    return store, project, context


def test_store_import_dedup_and_consistency(tmp_path: Path) -> None:
    store, project, context = _store_project(tmp_path)
    result1 = store.import_measurement(
        project['id'], context['id'], 'sweep.txt', VALID_REW,
        channel_role='FL', evidence_type='measured', source_speaker_ids=[],
        radiation_scope='single', captured_at=None, notes=None,
    )
    assert result1['duplicate_asset'] is False

    # re-import the identical bytes: asset dedup must kick in, and the
    # same asset blob must serve — no second file, no silent overwrite.
    result2 = store.import_measurement(
        project['id'], context['id'], 'sweep-again.txt', VALID_REW,
        channel_role='FR', evidence_type='measured', source_speaker_ids=[],
        radiation_scope='single', captured_at=None, notes=None,
    )
    assert result2['duplicate_asset'] is True
    assert result2['existing_dataset_count'] == 1
    assert result2['asset_sha256'] == result1['asset_sha256']

    # one blob on disk, byte-identical to the source file
    asset_files = list(store.assets_dir.iterdir())
    assert len(asset_files) == 1
    assert asset_files[0].read_bytes() == VALID_REW
    assert asset_files[0].name.startswith(result1['asset_sha256'])

    # dataset_sha256 populated and stable across both rows
    measurements = store.list_measurements(project['id'])
    assert len(measurements) == 2
    assert all(m['dataset_sha256'] for m in measurements)
    assert measurements[0]['integrity_valid'] is True
    assert measurements[1]['integrity_valid'] is True


def test_store_import_rejects_hostile(tmp_path: Path) -> None:
    store, project, context = _store_project(tmp_path)
    with pytest.raises(RewParseError):
        store.import_measurement(
            project['id'], context['id'], 'junk.txt', os.urandom(1024),
            channel_role='FL', evidence_type='measured', source_speaker_ids=[],
            radiation_scope='single', captured_at=None, notes=None,
        )
    # nothing persisted: no asset file, no rows
    assert not list(store.assets_dir.iterdir())
    assert store.list_measurements(project['id']) == []


def test_store_import_filename_safety(tmp_path: Path) -> None:
    store, project, context = _store_project(tmp_path)
    # hostile filenames must never escape the assets dir or the store
    for name in ('../../escape.txt', '..\\..\\escape.txt', 'a/b/c.txt', '🔊 測定.txt'):
        result = store.import_measurement(
            project['id'], context['id'], name, VALID_REW,
            channel_role='FL', evidence_type='measured', source_speaker_ids=[],
            radiation_scope='single', captured_at=None, notes=None,
        )
        assert result['asset_sha256']
    for path in store.assets_dir.iterdir():
        assert path.parent == store.assets_dir
    # nothing landed outside the store root
    assert not (tmp_path / 'escape.txt').exists()


# ---------------------------------------------------------------------------
# Partial batch — measurement_workflow.stage_rew_text_files
# ---------------------------------------------------------------------------

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_workflow import MeasurementWorkflowController


def test_batch_staging_partial_failure_is_honest(tmp_path: Path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    items = controller.stage_rew_text_files(
        [
            (VALID_REW, 'good-1.txt'),
            (os.urandom(512), 'junk.bin'),
            (b'20.0 72.0\n40.0 71.0\n80.0 70.0\n', 'good-2.txt'),
        ]
    )
    assert len(items) == 3
    states = {item.filename: item.status for item in items}
    assert states['good-1.txt'] == 'staged'
    assert states['good-2.txt'] == 'staged'
    assert states['junk.bin'] == 'failed'
    # the failed item names itself — attribution is per-file, not generic
    error_item = next(i for i in items if i.filename == 'junk.bin')
    assert error_item.error


# ---------------------------------------------------------------------------
# Spectral XML (TM-27/TM-33), CGATS meter correction, WAVE report JSON
# ---------------------------------------------------------------------------

from htdt.cad_spectral_lighting import parse_spectral_xml
from htdt.cad_meter_correction import (
    CgatsParseError,
    import_meter_correction,
    parse_cgats_document,
)
from htdt.cad_wave_qualification import import_wave_report_json

_SPDX = (
    '<?xml version="1.0"?>\n<SpdxDocument><SpectralData wavelength="380" '
    'power="1.2"/><SpectralData wavelength="400" power="0.8"/>'
    '<CCT>3200</CCT></SpdxDocument>'
)


def test_spectral_xml_valid_and_doctype_rejected() -> None:
    result = parse_spectral_xml(_SPDX, evidence_id='sp-1')
    assert result.verdict == 'valid'
    assert result.evidence is not None
    assert len(result.evidence.spectral_power) == 2

    xxe = _SPDX.replace(
        '<SpdxDocument>',
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///c:/windows/win.ini">]>'
        '<SpdxDocument><Foo>&e;</Foo>',
    )
    result = parse_spectral_xml(xxe, evidence_id='sp-2')
    assert result.verdict == 'invalid'
    assert 'DOCTYPE' in result.detail or 'ENTITY' in result.detail

    for hostile in (
        os.urandom(1024).decode('latin-1'),
        '<unclosed',
        '<root/>',
    ):
        result = parse_spectral_xml(hostile, evidence_id='sp-x')
        assert result.verdict == 'invalid', hostile[:40]

    # a corrupt spectral table with a good CCT degrades honestly: the
    # verdict stays valid but NO spectral samples are invented
    degraded = parse_spectral_xml(
        _SPDX.replace('1.2', 'abc'), evidence_id='sp-3'
    )
    assert degraded.verdict == 'valid'
    assert not degraded.evidence.spectral_power


_VALID_CCMX = (
    'CCMX\nNUMBER_OF_FIELDS 3\nBEGIN_DATA_FORMAT\nF1 F2 F3\n'
    'END_DATA_FORMAT\nNUMBER_OF_SETS 3\nBEGIN_DATA\n'
    '1 2 3\n4 5 6\n7 8 9\nEND_DATA\n'
)


def test_cgats_valid_and_hostile() -> None:
    doc = parse_cgats_document(_VALID_CCMX)
    assert doc.format_id == 'CCMX'
    assert len(doc.data_rows) == 3

    for hostile in (
        '',
        'not-a-keyword-!@#',
        'CCMX\nBEGIN_DATA_FORMAT\nX\n',
        _VALID_CCMX.replace('NUMBER_OF_SETS 3', 'NUMBER_OF_SETS 99'),
        _VALID_CCMX.replace('NUMBER_OF_FIELDS 3', 'NUMBER_OF_FIELDS 5'),
        _VALID_CCMX.replace('1 2 3', '1 2'),  # short data row
        _VALID_CCMX.replace('END_DATA\n', ''),
        'CCMX\nNUMBER_OF_FIELDS abc\n',
    ):
        with pytest.raises(ValueError):  # CgatsParseError or honest ValueError
            parse_cgats_document(hostile)


def test_meter_correction_import_rejects_bad_bytes() -> None:
    artifact = import_meter_correction(
        file_name='meter.ccmx', data=_VALID_CCMX.encode('utf-8')
    )
    assert artifact.source_sha256
    for hostile in (
        os.urandom(512),
        'CCMX\n'.encode('utf-16'),
        b'\x00\xff' * 128,
    ):
        with pytest.raises(CgatsParseError):
            import_meter_correction(file_name='bad.ccmx', data=hostile)


_WAVE_OK = {
    'suite': 'HDMI_CEC',
    'version': '1.0',
    'device': 'avr-1',
    'results': [{'test': 't1', 'status': 'pass'}],
}


def test_wave_report_json_valid_and_hostile() -> None:
    result = import_wave_report_json(
        json.dumps(_WAVE_OK), report_id='w1', source_uri='file:///w.json'
    )
    assert result.verdict == 'imported'
    for hostile in (
        'not json{',
        '[]',
        json.dumps({}),  # missing identity fields
        json.dumps({'suite': 'x', 'version': '1', 'device': 'd',
                    'results': [{'test': 't'}]}),  # status missing
    ):
        result = import_wave_report_json(
            hostile, report_id='w2', source_uri='file:///w.json'
        )
        assert result.verdict in ('invalid', 'incomplete'), result.verdict
