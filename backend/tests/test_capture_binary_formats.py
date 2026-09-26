"""Direct tests for the Capture Bundle v1 canonical binary validators.

Each validator parses headers before payload bytes and must fail closed on
truncation, bad magic/versions, out-of-bounds indices, non-finite geometry
and trailing garbage — these tests pin each rejection path plus a valid
round trip for every codec.
"""

from __future__ import annotations

import struct

import pytest

from htdt.capture_binary_formats import (
    BinaryFormatError,
    validate_confidencebin,
    validate_depthbin,
    validate_meshbin,
    validate_pixelbin,
)


def _common(magic: bytes, header_length: int) -> bytes:
    return magic + struct.pack('<HHI', 1, 0, header_length)


# ---------------------------------------------------------------- mesh

def _meshbin(
    vertex_count: int = 3,
    face_count: int = 1,
    flags: int = 0,
    vertices: tuple[float, ...] | None = None,
    indices: tuple[int, ...] | None = None,
    normals: tuple[float, ...] | None = None,
    classifications: bytes = b'',
    trailing: bytes = b'',
) -> bytes:
    header = _common(b'HTDTMSH1', 32)
    header += struct.pack('<II', vertex_count, face_count)
    header += struct.pack('<BBHI', 4, flags, 0, 0)
    body = b''
    for v in (vertices if vertices is not None else (0.0,) * vertex_count * 3):
        body += struct.pack('<f', v)
    if flags & 1:
        for n in (normals if normals is not None else (0.0,) * vertex_count * 3):
            body += struct.pack('<f', n)
    for i in (indices if indices is not None else (0, 1, 2) * face_count):
        body += struct.pack('<I', i)
    if flags & 2:
        body += classifications or bytes(face_count)
    return header + body + trailing


def test_meshbin_valid_minimal() -> None:
    header = validate_meshbin(_meshbin())
    assert header.vertex_count == 3
    assert header.face_count == 1


def test_meshbin_valid_with_normals_and_classifications() -> None:
    data = _meshbin(vertex_count=4, face_count=2, flags=3)
    header = validate_meshbin(data)
    assert (header.vertex_count, header.face_count) == (4, 2)


def test_meshbin_rejects_bad_magic() -> None:
    with pytest.raises(BinaryFormatError, match='invalid_magic'):
        validate_meshbin(_meshbin().replace(b'HTDTMSH1', b'XXXXXXXX', 1))


def test_meshbin_rejects_unsupported_version() -> None:
    bad = _common(b'HTDTMSH1', 32).replace(struct.pack('<H', 1), struct.pack('<H', 2), 1) + _meshbin()[16:]
    with pytest.raises(BinaryFormatError, match='unsupported_version'):
        validate_meshbin(bad)


def test_meshbin_rejects_bad_header_length() -> None:
    bad = _common(b'HTDTMSH1', 24) + _meshbin()[16:]
    with pytest.raises(BinaryFormatError, match='invalid_header_length'):
        validate_meshbin(bad)


def test_meshbin_rejects_unsupported_index_width() -> None:
    data = bytearray(_meshbin())
    data[24] = 2  # index_width u8 at offset 16+8=24
    with pytest.raises(BinaryFormatError, match='unsupported_index_width'):
        validate_meshbin(bytes(data))


def test_meshbin_rejects_unknown_flag_bits() -> None:
    data = bytearray(_meshbin())
    data[25] = 0b100  # flags
    with pytest.raises(BinaryFormatError, match='invalid_flags'):
        validate_meshbin(bytes(data))


def test_meshbin_rejects_zero_vertices() -> None:
    with pytest.raises(BinaryFormatError, match='no_vertices'):
        validate_meshbin(_meshbin(vertex_count=0, face_count=0))


def test_meshbin_rejects_nonfinite_vertex() -> None:
    verts = (0.0, 0.0, float('nan')) + (0.0,) * 6
    with pytest.raises(BinaryFormatError, match='nonfinite vertex'):
        validate_meshbin(_meshbin(vertices=verts))


def test_meshbin_rejects_nonfinite_normal() -> None:
    normals = (float('inf'), 0.0, 0.0) + (0.0,) * 6
    with pytest.raises(BinaryFormatError, match='nonfinite normal'):
        validate_meshbin(_meshbin(flags=1, normals=normals))


def test_meshbin_rejects_out_of_bounds_index() -> None:
    with pytest.raises(BinaryFormatError, match='out of bounds'):
        validate_meshbin(_meshbin(indices=(0, 1, 9)))


def test_meshbin_rejects_truncation_and_trailing() -> None:
    data = _meshbin()
    with pytest.raises(BinaryFormatError, match='exceeds_available|truncated'):
        validate_meshbin(data[:-1])
    with pytest.raises(BinaryFormatError, match='trailing_bytes'):
        validate_meshbin(data + b'x')


# ---------------------------------------------------------------- pixel

def _pixelbin(
    width: int = 4,
    height: int = 2,
    pixel_format: int = 0x34325258,  # 'XR24' style fourcc placeholder
    planes: tuple[tuple[int, int, int, int], ...] | None = None,
    payload: bytes | None = None,
) -> bytes:
    """planes: (width, height, source_bpr, packed_bpr) per plane."""
    planes = planes if planes is not None else ((width, height, width, width),)
    header_length = 32 + 24 * len(planes)
    out = _common(b'HTDTPXL1', header_length)
    out += struct.pack('<IIIHH', width, height, pixel_format, len(planes), 0)
    offset = header_length
    for pw, ph, sbpr, pbpr in planes:
        pbytes = ph * pbpr
        out += struct.pack('<IIIIII', pw, ph, sbpr, pbpr, offset, pbytes)
        offset += pbytes
    if payload is None:
        payload = bytes(offset - header_length)
    return out + payload


def test_pixelbin_valid_single_plane() -> None:
    header = validate_pixelbin(_pixelbin())
    assert (header.width, header.height) == (4, 2)


def test_pixelbin_valid_multiplane() -> None:
    header = validate_pixelbin(
        _pixelbin(width=4, height=2,
                  planes=((4, 2, 4, 4), (2, 1, 2, 2)))
    )
    assert header.width == 4


def test_pixelbin_rejects_bad_magic() -> None:
    with pytest.raises(BinaryFormatError, match='invalid_magic'):
        validate_pixelbin(_pixelbin().replace(b'HTDTPXL1', b'ZZZZZZZZ', 1))


def test_pixelbin_rejects_zero_planes() -> None:
    data = _pixelbin()
    # plane_count u16 sits at offset 28 (16-byte common prefix + w/h/format)
    data = data[:28] + struct.pack('<H', 0) + data[30:]
    with pytest.raises(BinaryFormatError, match='no_planes'):
        validate_pixelbin(data)


def test_pixelbin_rejects_mismatched_header_length() -> None:
    data = bytearray(_pixelbin())
    data[12:16] = struct.pack('<I', 32)  # claims no plane descriptors
    with pytest.raises(BinaryFormatError, match='invalid_header_length'):
        validate_pixelbin(bytes(data))


def test_pixelbin_rejects_invalid_plane_layout() -> None:
    # packed_bytes_per_row > source_bytes_per_row is impossible
    with pytest.raises(BinaryFormatError, match='invalid_plane_layout'):
        validate_pixelbin(_pixelbin(planes=((4, 2, 4, 8),)))


def test_pixelbin_rejects_noncontiguous_payload() -> None:
    # Two planes where the second offset does not follow the first payload.
    data = _common(b'HTDTPXL1', 80)
    data += struct.pack('<IIIHH', 4, 2, 0x34325258, 2, 0)
    data += struct.pack('<IIIIII', 4, 2, 4, 4, 80, 8)
    data += struct.pack('<IIIIII', 2, 1, 2, 2, 96, 2)  # gap of 8
    data += bytes(10)
    with pytest.raises(BinaryFormatError, match='non_contiguous_payload'):
        validate_pixelbin(data)


def test_pixelbin_rejects_truncated_and_trailing_payload() -> None:
    data = _pixelbin()
    with pytest.raises(BinaryFormatError, match='truncated'):
        validate_pixelbin(data[:-1])
    with pytest.raises(BinaryFormatError, match='trailing_bytes'):
        validate_pixelbin(data + b'x')


def test_pixelbin_rejects_zero_image_dimensions() -> None:
    # Plane layout stays valid so the image-dimension guard is what trips.
    with pytest.raises(BinaryFormatError, match='image dimensions'):
        validate_pixelbin(_pixelbin(width=0, height=2, planes=((4, 2, 4, 4),)))


# ---------------------------------------------------------------- depth

def _depthbin(
    width: int = 2,
    height: int = 2,
    flags: int = 0,
    depths: tuple[float, ...] | None = None,
    mask: bytes = b'',
    trailing: bytes = b'',
) -> bytes:
    out = _common(b'HTDTDPT1', 32)
    out += struct.pack('<IIBBHI', width, height, 1, flags, 0, 0)
    count = width * height
    for d in (depths if depths is not None else (1.0,) * count):
        out += struct.pack('<f', d)
    if flags & 1:
        out += mask or bytes(count)
    return out + trailing


def test_depthbin_valid() -> None:
    header = validate_depthbin(_depthbin())
    assert (header.width, header.height) == (2, 2)


def test_depthbin_valid_with_validity_mask() -> None:
    header = validate_depthbin(_depthbin(flags=1, mask=bytes([0, 1, 1, 0])))
    assert header.width == 2


def test_depthbin_rejects_bad_magic_and_version() -> None:
    with pytest.raises(BinaryFormatError, match='invalid_magic'):
        validate_depthbin(_depthbin().replace(b'HTDTDPT1', b'AAAAAAAA', 1))
    bad = _common(b'HTDTDPT1', 32).replace(struct.pack('<H', 1), struct.pack('<H', 0), 1) + _depthbin()[16:]
    with pytest.raises(BinaryFormatError, match='unsupported_version'):
        validate_depthbin(bad)


def test_depthbin_rejects_zero_dimensions() -> None:
    with pytest.raises(BinaryFormatError, match='dimensions'):
        validate_depthbin(_depthbin(width=0, height=2))


def test_depthbin_rejects_nonfinite_depth() -> None:
    with pytest.raises(BinaryFormatError, match='nonfinite depth'):
        validate_depthbin(_depthbin(depths=(1.0, float('inf'), 1.0, 1.0)))


def test_depthbin_rejects_bad_validity_value() -> None:
    with pytest.raises(BinaryFormatError, match='not in \\{0,1\\}'):
        validate_depthbin(_depthbin(flags=1, mask=bytes([0, 2, 1, 0])))


def test_depthbin_rejects_truncation_and_trailing() -> None:
    data = _depthbin(flags=1, mask=bytes([1, 1, 1, 1]))
    with pytest.raises(BinaryFormatError, match='truncated'):
        validate_depthbin(data[:-1])
    with pytest.raises(BinaryFormatError, match='trailing_bytes'):
        validate_depthbin(data + b'x')


# ----------------------------------------------------------- confidence

def _confidencebin(
    width: int = 2,
    height: int = 2,
    payload: bytes | None = None,
    trailing: bytes = b'',
) -> bytes:
    out = _common(b'HTDTCNF1', 32)
    out += struct.pack('<IIBBHI', width, height, 1, 0, 0, 0)
    out += payload if payload is not None else bytes(width * height)
    return out + trailing


def test_confidencebin_valid() -> None:
    header = validate_confidencebin(_confidencebin())
    assert (header.width, header.height) == (2, 2)


def test_confidencebin_rejects_bad_magic() -> None:
    with pytest.raises(BinaryFormatError, match='invalid_magic'):
        validate_confidencebin(_confidencebin().replace(b'HTDTCNF1', b'BBBBBBBB', 1))


def test_confidencebin_rejects_zero_dimensions() -> None:
    with pytest.raises(BinaryFormatError, match='dimensions'):
        validate_confidencebin(_confidencebin(height=0))


def test_confidencebin_rejects_truncation_and_trailing() -> None:
    data = _confidencebin()
    with pytest.raises(BinaryFormatError, match='truncated'):
        validate_confidencebin(data[:-1])
    with pytest.raises(BinaryFormatError, match='trailing_bytes'):
        validate_confidencebin(data + b'x')


def test_all_validators_reject_empty_input() -> None:
    for validator in (
        validate_meshbin,
        validate_pixelbin,
        validate_depthbin,
        validate_confidencebin,
    ):
        with pytest.raises(BinaryFormatError):
            validator(b'')
