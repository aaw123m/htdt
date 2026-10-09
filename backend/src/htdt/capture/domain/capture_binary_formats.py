"""Structural validators for HTDT-Capture canonical binary payloads.

Mirrors the v1 Swift codecs (stdlib-only):

- ``MeshBinaryCodec``        (.meshbin,      magic ``HTDTMSH1``)
- ``PixelBufferBinaryCodec`` (.pixelbin,     magic ``HTDTPXL1``)
- ``DepthBinaryCodec``       (.depthbin,     magic ``HTDTDPT1``)
- ``ConfidenceBinaryCodec``  (.confidencebin, magic ``HTDTCNF1``)

and the payload invariants enforced by ``MeshGeometryPayload``,
``PackedPixelBuffer``/``PackedPixelPlane``, ``DepthMapPayload`` and
``ConfidenceMapPayload`` initializers (finite vertex/normal/depth values,
in-bounds triangle indices, validity mask values in {0,1}, ...).

Each validator parses the header first and rejects before touching payload
bytes, so malformed count fields cannot drive unbounded work. The input is
already bounded by the validator's per-file limit; parsing is a single linear
pass over that bounded buffer.
"""

from __future__ import annotations

import math
import struct


class BinaryFormatError(ValueError):
    """Raised when a canonical binary payload violates the v1 format."""


class _Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def read_bytes(self, count: int) -> bytes:
        if count < 0 or self.remaining < count:
            raise BinaryFormatError("truncated")
        chunk = self.data[self.offset : self.offset + count]
        self.offset += count
        return chunk

    def read_u8(self) -> int:
        if self.remaining < 1:
            raise BinaryFormatError("truncated")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_u16(self) -> int:
        return self.read_u8() | (self.read_u8() << 8)

    def read_u32(self) -> int:
        return (
            self.read_u8()
            | (self.read_u8() << 8)
            | (self.read_u8() << 16)
            | (self.read_u8() << 24)
        )

    def read_f32(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def skip(self, count: int) -> None:
        self.read_bytes(count)


class MeshHeader:
    __slots__ = ("vertex_count", "face_count")

    def __init__(self, vertex_count: int, face_count: int):
        self.vertex_count = vertex_count
        self.face_count = face_count


class PixelHeader:
    __slots__ = ("width", "height", "pixel_format_fourcc")

    def __init__(self, width: int, height: int, pixel_format_fourcc: int):
        self.width = width
        self.height = height
        self.pixel_format_fourcc = pixel_format_fourcc


class DepthHeader:
    __slots__ = ("width", "height")

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height


class ConfidenceHeader:
    __slots__ = ("width", "height")

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height


def _read_common_header(reader: _Reader, magic: bytes):
    if reader.read_bytes(8) != magic:
        raise BinaryFormatError("invalid_magic")
    major = reader.read_u16()
    minor = reader.read_u16()
    if major != 1 or minor != 0:
        raise BinaryFormatError(f"unsupported_version({major},{minor})")
    return reader.read_u32()


def validate_meshbin(data: bytes) -> MeshHeader:
    """Mirror MeshBinaryCodec.decode + MeshGeometryPayload invariants."""
    reader = _Reader(data)
    header_length = _read_common_header(reader, b"HTDTMSH1")
    if header_length != 32:
        raise BinaryFormatError(f"invalid_header_length({header_length})")

    vertex_count = reader.read_u32()
    face_count = reader.read_u32()
    index_width = reader.read_u8()
    if index_width != 4:
        raise BinaryFormatError(f"unsupported_index_width({index_width})")
    flags = reader.read_u8()
    flag_normals = 1 << 0
    flag_classifications = 1 << 1
    if flags & ~(flag_normals | flag_classifications):
        raise BinaryFormatError(f"invalid_flags({flags})")
    if reader.read_u16() != 0 or reader.read_u32() != 0:
        raise BinaryFormatError("nonzero_reserved")

    vertex_bytes = vertex_count * 12
    normals_bytes = vertex_bytes if flags & flag_normals else 0
    index_bytes = face_count * 12
    classification_bytes = face_count if flags & flag_classifications else 0
    required = vertex_bytes + normals_bytes + index_bytes + classification_bytes
    if reader.remaining < required:
        raise BinaryFormatError("declared_payload_exceeds_available_bytes")

    # MeshGeometryPayload requires at least one vertex and finite geometry.
    if vertex_count == 0:
        raise BinaryFormatError("invalid_geometry(no_vertices)")

    for index in range(vertex_count):
        for _ in range(3):
            if not math.isfinite(reader.read_f32()):
                raise BinaryFormatError(f"invalid_geometry(nonfinite vertex {index})")

    if flags & flag_normals:
        for index in range(vertex_count):
            for _ in range(3):
                if not math.isfinite(reader.read_f32()):
                    raise BinaryFormatError(
                        f"invalid_geometry(nonfinite normal {index})"
                    )

    for _ in range(face_count * 3):
        index = reader.read_u32()
        if index >= vertex_count:
            raise BinaryFormatError(
                f"invalid_geometry(index {index} out of bounds)"
            )

    if flags & flag_classifications:
        reader.skip(face_count)

    if reader.remaining != 0:
        raise BinaryFormatError(f"trailing_bytes({reader.remaining})")

    return MeshHeader(vertex_count=vertex_count, face_count=face_count)


def validate_pixelbin(data: bytes) -> PixelHeader:
    """Mirror PixelBufferBinaryCodec.decode + PackedPixel* invariants."""
    reader = _Reader(data)
    header_length = _read_common_header(reader, b"HTDTPXL1")
    image_width = reader.read_u32()
    image_height = reader.read_u32()
    pixel_format = reader.read_u32()
    plane_count = reader.read_u16()
    if reader.read_u16() != 0:
        raise BinaryFormatError("invalid_reserved")
    if plane_count == 0:
        raise BinaryFormatError("no_planes")

    expected_header = 32 + plane_count * 24
    if header_length != expected_header:
        raise BinaryFormatError(f"invalid_header_length({header_length})")
    if len(data) < expected_header:
        raise BinaryFormatError("truncated")

    expected_payload_offset = expected_header
    for index in range(plane_count):
        width = reader.read_u32()
        height = reader.read_u32()
        source_bytes_per_row = reader.read_u32()
        packed_bytes_per_row = reader.read_u32()
        payload_offset = reader.read_u32()
        payload_bytes = reader.read_u32()

        if (
            width == 0
            or height == 0
            or packed_bytes_per_row == 0
            or source_bytes_per_row < packed_bytes_per_row
        ):
            raise BinaryFormatError(f"invalid_plane_layout({index})")
        if payload_bytes != height * packed_bytes_per_row:
            raise BinaryFormatError(f"invalid_plane_layout({index})")
        if payload_offset != expected_payload_offset:
            raise BinaryFormatError(f"non_contiguous_payload({index})")
        expected_payload_offset += payload_bytes

    if len(data) < expected_payload_offset:
        raise BinaryFormatError("truncated")
    if len(data) != expected_payload_offset:
        raise BinaryFormatError(
            f"trailing_bytes({len(data) - expected_payload_offset})"
        )

    # PackedPixelBuffer additionally requires a non-empty image.
    if image_width == 0 or image_height == 0:
        raise BinaryFormatError("invalid_payload(image dimensions)")

    return PixelHeader(
        width=image_width,
        height=image_height,
        pixel_format_fourcc=pixel_format,
    )


def validate_depthbin(data: bytes) -> DepthHeader:
    """Mirror DepthBinaryCodec.decode + DepthMapPayload invariants."""
    reader = _Reader(data)
    header_length = _read_common_header(reader, b"HTDTDPT1")
    if header_length != 32:
        raise BinaryFormatError(f"invalid_header_length({header_length})")

    width = reader.read_u32()
    height = reader.read_u32()
    component_type = reader.read_u8()
    if component_type != 1:
        raise BinaryFormatError(f"unsupported_component_type({component_type})")
    flags = reader.read_u8()
    flag_validity_mask = 1 << 0
    if flags & ~flag_validity_mask:
        raise BinaryFormatError(f"invalid_flags({flags})")
    if reader.read_u16() != 0 or reader.read_u32() != 0:
        raise BinaryFormatError("nonzero_reserved")

    if width == 0 or height == 0:
        raise BinaryFormatError("invalid_payload(dimensions)")
    count = width * height
    expected = count * 4 + (count if flags & flag_validity_mask else 0)
    if reader.remaining < expected:
        raise BinaryFormatError("truncated")
    if reader.remaining != expected:
        raise BinaryFormatError(f"trailing_bytes({reader.remaining - expected})")

    for index in range(count):
        if not math.isfinite(reader.read_f32()):
            raise BinaryFormatError(f"invalid_payload(nonfinite depth {index})")

    if flags & flag_validity_mask:
        for index in range(count):
            if reader.read_u8() not in (0, 1):
                raise BinaryFormatError(
                    f"invalid_payload(validity[{index}] not in {{0,1}})"
                )

    return DepthHeader(width=width, height=height)


def validate_confidencebin(data: bytes) -> ConfidenceHeader:
    """Mirror ConfidenceBinaryCodec.decode + ConfidenceMapPayload invariants."""
    reader = _Reader(data)
    header_length = _read_common_header(reader, b"HTDTCNF1")
    if header_length != 32:
        raise BinaryFormatError(f"invalid_header_length({header_length})")

    width = reader.read_u32()
    height = reader.read_u32()
    component_type = reader.read_u8()
    if component_type != 1:
        raise BinaryFormatError(f"unsupported_component_type({component_type})")
    if (
        reader.read_u8() != 0
        or reader.read_u16() != 0
        or reader.read_u32() != 0
    ):
        raise BinaryFormatError("nonzero_reserved")

    if width == 0 or height == 0:
        raise BinaryFormatError("invalid_payload(dimensions)")
    count = width * height
    if reader.remaining < count:
        raise BinaryFormatError("truncated")
    if reader.remaining != count:
        raise BinaryFormatError(f"trailing_bytes({reader.remaining - count})")
    reader.skip(count)

    return ConfidenceHeader(width=width, height=height)
