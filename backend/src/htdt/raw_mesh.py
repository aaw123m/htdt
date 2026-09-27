from __future__ import annotations

from base64 import b64decode, b64encode
from collections import defaultdict, deque
from hashlib import sha256
import json
from math import floor, isfinite, sqrt
import struct
from typing import Annotated, Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .limits import (
    MAX_CAPTURE_INGEST_FACE_COUNT,
    MAX_CAPTURE_INGEST_VERTEX_COUNT,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _semantic_hash


RAW_MESH_IMPORTER_ID = 'htdt.raw_visual_mesh'
RAW_MESH_IMPORTER_VERSION = '1'
RAW_MESH_DIAGNOSTIC_ALGORITHM = 'htdt.raw_mesh_diagnostics'
RAW_MESH_DIAGNOSTIC_VERSION = '1'

RawMeshFormat = Literal[
    'obj', 'glb', 'ply', 'stl', 'htdt_meshbin_v1', 'capture_mesh_composition_v1'
]
DiagnosticState = Literal['pass', 'fail', 'unknown']
AcousticVolumeReadiness = Literal[
    'not_ready',
    'geometry_checks_pass_but_semantic_conversion_required',
]


class RawMeshImportError(ValueError):
    pass


# Parsed-output ceilings: parsers run on bytes from user-selected files, so
# the vertex/triangle lists they materialize must stay finite even when the
# input is hostile. The capture-ingestion ceilings are reused as the shared
# generous bound rather than inventing a second quota.
MAX_RAW_MESH_VERTICES = MAX_CAPTURE_INGEST_VERTEX_COUNT
MAX_RAW_MESH_TRIANGLES = MAX_CAPTURE_INGEST_FACE_COUNT






class RawMeshVertex(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    z: float

    @field_validator('x', 'y', 'z')
    @classmethod
    def finite(cls, value: float) -> float:
        value = float(value)
        if not isfinite(value):
            raise ValueError('raw mesh vertex coordinates must be finite')
        return value


# Retained per-face classification bytes are advisory provenance only: they
# describe what the capture device observed, never acoustic or semantic
# authority. Known ARKit ARMeshClassification values map to stable labels;
# any future or vendor-specific byte is retained verbatim and reported as
# ``unknown:<n>`` rather than rejected or guessed.
ARKIT_MESH_CLASSIFICATION_LABELS: Mapping[int, str] = {
    0: 'none',
    1: 'wall',
    2: 'floor',
    3: 'ceiling',
    4: 'table',
    5: 'seat',
    6: 'window',
    7: 'door',
}


def meshbin_face_classification_label(value: int) -> str:
    """Advisory label for one retained HTDTMSH1 face-classification byte."""
    return ARKIT_MESH_CLASSIFICATION_LABELS.get(value, f'unknown:{value}')


ClassificationByte = Annotated[int, Field(ge=0, le=255)]


class RawMeshTriangle(BaseModel):
    model_config = ConfigDict(frozen=True)

    a: int = Field(ge=0)
    b: int = Field(ge=0)
    c: int = Field(ge=0)
    source_primitive: str = Field(min_length=1)

    @model_validator(mode='after')
    def distinct_indices(self) -> 'RawMeshTriangle':
        if len({self.a, self.b, self.c}) != 3:
            raise ValueError('raw mesh triangle indices must be distinct')
        return self


class RawMeshImportProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_name: str = Field(min_length=1)
    asset_format: RawMeshFormat
    original_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    original_size_bytes: int = Field(ge=0)
    importer_id: Literal['htdt.raw_visual_mesh'] = RAW_MESH_IMPORTER_ID
    importer_version: Literal['1', '2', '3'] = RAW_MESH_IMPORTER_VERSION
    coordinate_authority: Literal[
        'source_asset_coordinates', 'capture_world_coordinates'
    ] = 'source_asset_coordinates'
    acoustic_semantics: Literal['unassigned'] = 'unassigned'
    solver_readiness: Literal['raw_visual_only'] = 'raw_visual_only'


class RawVisualMesh(BaseModel):
    """Immutable visual-geometry snapshot. It is deliberately not acoustic solver authority."""

    model_config = ConfigDict(frozen=True)

    mesh_id: str = Field(pattern=r'^raw-mesh:[0-9a-f]{64}$')
    provenance: RawMeshImportProvenance
    original_asset_base64: str
    vertices: tuple[RawMeshVertex, ...]
    triangles: tuple[RawMeshTriangle, ...]
    # Optional HTDTMSH1 attributes retained with exact source indexing.
    # ``source_normals[i]`` is the captured normal of ``vertices[i]`` and
    # ``source_face_classifications[i]`` the captured classification byte of
    # ``triangles[i]``. They are advisory provenance: retained verbatim for
    # inspection and suggestions, never acoustic or semantic authority.
    source_normals: tuple[RawMeshVertex, ...] = ()
    source_face_classifications: tuple[ClassificationByte, ...] = ()

    @model_validator(mode='after')
    def validate_snapshot(self) -> 'RawVisualMesh':
        try:
            original = b64decode(self.original_asset_base64.encode('ascii'), validate=True)
        except Exception as exc:  # pragma: no cover - pydantic wraps this path
            raise ValueError('original_asset_base64 must be valid base64') from exc
        actual_hash = sha256(original).hexdigest()
        if actual_hash != self.provenance.original_asset_sha256:
            raise ValueError('original asset bytes do not match provenance hash')
        if len(original) != self.provenance.original_size_bytes:
            raise ValueError('original asset bytes do not match provenance size')
        if self.mesh_id != f'raw-mesh:{actual_hash}':
            raise ValueError('mesh_id must be derived from original asset hash')
        vertex_count = len(self.vertices)
        for triangle in self.triangles:
            if max(triangle.a, triangle.b, triangle.c) >= vertex_count:
                raise ValueError('raw mesh triangle references an unknown vertex')
        if self.source_normals and len(self.source_normals) != vertex_count:
            raise ValueError('source normals must align one-to-one with vertices')
        if self.source_face_classifications and (
            len(self.source_face_classifications) != len(self.triangles)
        ):
            raise ValueError(
                'source face classifications must align one-to-one with triangles'
            )
        if (
            self.source_normals or self.source_face_classifications
        ) and self.provenance.asset_format != 'htdt_meshbin_v1':
            raise ValueError('source mesh attributes require HTDTMSH1 provenance')
        return self

    def semantic_hash(self) -> str:
        # Advisory source attributes stay out of the semantic identity: the
        # mesh_id already pins the exact source bytes that produced them, so
        # retaining decoded attributes must not change the identity a
        # persisted binding derived before this decoder exposed them.
        return _semantic_hash(
            self.model_dump(
                mode='json',
                exclude={'source_normals', 'source_face_classifications'},
            )
        )

    def original_asset_bytes(self) -> bytes:
        return b64decode(self.original_asset_base64.encode('ascii'), validate=True)


class RawMeshDiagnosticProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm_id: Literal['htdt.raw_mesh_diagnostics'] = RAW_MESH_DIAGNOSTIC_ALGORITHM
    algorithm_version: Literal['1'] = RAW_MESH_DIAGNOSTIC_VERSION
    weld_relative_tolerance: float = Field(default=1.0e-9, gt=0.0)
    weld_absolute_floor_source_units: float = Field(default=1.0e-12, gt=0.0)
    tiny_feature_relative_size: float = Field(default=1.0e-6, gt=0.0)
    sliver_quality_threshold: float = Field(default=1.0e-4, gt=0.0, lt=1.0)
    coplanar_relative_tolerance: float = Field(default=1.0e-9, gt=0.0)
    overlap_area_relative_tolerance: float = Field(default=1.0e-12, gt=0.0)

    def semantic_hash(self) -> str:
        return _semantic_hash(self.model_dump(mode='json'))


DiagnosticCode = Literal[
    'open_boundary',
    'non_manifold_edge',
    'duplicate_face',
    'overlapping_face',
    'inverted_normal',
    'sliver_face',
    'tiny_feature',
    'watertightness',
]


class RawMeshDiagnosticFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: DiagnosticCode
    state: DiagnosticState
    count: int | None = Field(default=None, ge=0)
    detail: str = Field(min_length=1)


class RawMeshDiagnosticResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    diagnostic_id: str = Field(pattern=r'^raw-mesh-diagnostic:[0-9a-f]{64}$')
    raw_mesh_id: str
    raw_mesh_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    profile: RawMeshDiagnosticProfile
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    findings: tuple[RawMeshDiagnosticFinding, ...]
    acoustic_volume_readiness: AcousticVolumeReadiness
    solver_ready: Literal[False] = False
    semantic_conversion_required: Literal[True] = True

    @model_validator(mode='after')
    def validate_identity(self) -> 'RawMeshDiagnosticResult':
        if self.profile_semantic_hash != self.profile.semantic_hash():
            raise ValueError('diagnostic profile hash mismatch')
        expected = _diagnostic_id(
            self.raw_mesh_id,
            self.raw_mesh_semantic_hash,
            self.profile_semantic_hash,
        )
        if self.diagnostic_id != expected:
            raise ValueError('diagnostic_id does not match its authority inputs')
        return self

    def semantic_hash(self) -> str:
        return _semantic_hash(self.model_dump(mode='json'))


def serialize_raw_visual_mesh(mesh: RawVisualMesh) -> str:
    return _canonical_json(mesh.model_dump(mode='json'))


def deserialize_raw_visual_mesh(payload: str) -> RawVisualMesh:
    return RawVisualMesh.model_validate(json.loads(payload))


def serialize_raw_visual_mesh_reference(mesh: RawVisualMesh) -> dict[str, Any]:
    """Compact persisted form of a RawVisualMesh.

    The mesh is reduced to its identity and provenance; the original asset
    bytes are expected to live once in the canonical content-addressed blob
    store under ``provenance.original_asset_sha256`` instead of being embedded
    again as Base64 JSON.
    """

    return {
        'mesh_id': mesh.mesh_id,
        'provenance': mesh.provenance.model_dump(mode='json'),
    }


def rehydrate_raw_visual_mesh(
    record: Mapping[str, Any],
    asset: bytes,
) -> RawVisualMesh:
    """Rebuild a RawVisualMesh from a reference record plus canonical bytes.

    The deterministic importer is re-run so the returned mesh is identical to
    the originally imported snapshot; identity and provenance are re-verified
    against the persisted record.
    """

    provenance = RawMeshImportProvenance.model_validate(record.get('provenance'))
    mesh = import_raw_visual_mesh(
        asset,
        source_name=provenance.source_name,
        format_hint=provenance.asset_format,
    )
    if mesh.mesh_id != record.get('mesh_id') or mesh.provenance != provenance:
        raise RawMeshImportError(
            'content-addressed raw mesh record does not match the canonical asset'
        )
    return mesh


def serialize_raw_mesh_diagnostics(result: RawMeshDiagnosticResult) -> str:
    return _canonical_json(result.model_dump(mode='json'))


def deserialize_raw_mesh_diagnostics(payload: str) -> RawMeshDiagnosticResult:
    return RawMeshDiagnosticResult.model_validate(json.loads(payload))


def import_raw_visual_mesh(
    asset: bytes,
    *,
    source_name: str,
    format_hint: RawMeshFormat | None = None,
) -> RawVisualMesh:
    if not isinstance(asset, bytes):
        raise TypeError('asset must be immutable bytes')
    if not source_name:
        raise RawMeshImportError('source_name is required')
    asset_format = format_hint or _detect_format(asset, source_name)
    normals: tuple[RawMeshVertex, ...] = ()
    classifications: tuple[int, ...] = ()
    if asset_format == 'obj':
        vertices, triangles = _parse_obj(asset)
    elif asset_format == 'glb':
        vertices, triangles = _parse_glb(asset)
    elif asset_format == 'ply':
        vertices, triangles = _parse_ply(asset)
    elif asset_format == 'stl':
        vertices, triangles = _parse_stl(asset)
    elif asset_format == 'htdt_meshbin_v1':
        vertices, triangles, normals, classifications = (
            _parse_htdt_meshbin_v1(asset)
        )
    else:  # pragma: no cover - Literal plus validation keeps this defensive
        raise RawMeshImportError(f'unsupported raw mesh format: {asset_format}')
    if len(vertices) > MAX_RAW_MESH_VERTICES:
        raise RawMeshImportError(
            f'{asset_format} produces more than {MAX_RAW_MESH_VERTICES} vertices'
        )
    if len(triangles) > MAX_RAW_MESH_TRIANGLES:
        raise RawMeshImportError(
            f'{asset_format} produces more than {MAX_RAW_MESH_TRIANGLES} triangles'
        )
    asset_hash = sha256(asset).hexdigest()
    importer_version: Literal['1', '2'] = (
        '2' if asset_format == 'htdt_meshbin_v1' else RAW_MESH_IMPORTER_VERSION
    )
    return RawVisualMesh(
        mesh_id=f'raw-mesh:{asset_hash}',
        provenance=RawMeshImportProvenance(
            source_name=source_name,
            asset_format=asset_format,
            original_asset_sha256=asset_hash,
            original_size_bytes=len(asset),
            importer_version=importer_version,
        ),
        original_asset_base64=b64encode(asset).decode('ascii'),
        vertices=tuple(vertices),
        triangles=tuple(triangles),
        source_normals=normals,
        source_face_classifications=classifications,
    )


def _detect_format(asset: bytes, source_name: str) -> RawMeshFormat:
    lower = source_name.lower()
    if asset[:8] == b'HTDTMSH1' or lower.endswith('.meshbin'):
        return 'htdt_meshbin_v1'
    if asset[:4] == b'glTF' or lower.endswith('.glb'):
        return 'glb'
    if lower.endswith('.obj'):
        return 'obj'
    if lower.endswith('.ply') or asset[:3] == b'ply':
        return 'ply'
    if (
        lower.endswith('.stl')
        or _looks_like_binary_stl(asset)
        or asset[:5].lower() == b'solid'
    ):
        return 'stl'
    raise RawMeshImportError(
        'raw mesh format must be OBJ, GLB, PLY, STL, or HTDTMSH1 meshbin'
    )


def _looks_like_binary_stl(asset: bytes) -> bool:
    if len(asset) < 84:
        return False
    face_count = struct.unpack_from('<I', asset, 80)[0]
    return face_count > 0 and 84 + 50 * face_count == len(asset)


def _parse_htdt_meshbin_v1(
    asset: bytes,
) -> tuple[
    list[RawMeshVertex],
    list[RawMeshTriangle],
    tuple[RawMeshVertex, ...],
    tuple[int, ...],
]:
    if len(asset) < 32:
        raise RawMeshImportError('HTDTMSH1 asset is shorter than its header')
    if asset[:8] != b'HTDTMSH1':
        raise RawMeshImportError('HTDTMSH1 magic is invalid')

    major, minor = struct.unpack_from('<HH', asset, 8)
    if (major, minor) != (1, 0):
        raise RawMeshImportError(
            f'unsupported HTDTMSH1 version: {major}.{minor}'
        )
    header_length = struct.unpack_from('<I', asset, 12)[0]
    if header_length != 32:
        raise RawMeshImportError('HTDTMSH1 header length must be 32')

    vertex_count = struct.unpack_from('<I', asset, 16)[0]
    face_count = struct.unpack_from('<I', asset, 20)[0]
    index_width = asset[24]
    flags = asset[25]
    reserved16 = struct.unpack_from('<H', asset, 26)[0]
    reserved32 = struct.unpack_from('<I', asset, 28)[0]

    if vertex_count == 0:
        raise RawMeshImportError('HTDTMSH1 contains no vertices')
    if index_width != 4:
        raise RawMeshImportError('HTDTMSH1 index width must be UInt32')
    if flags & ~0x03:
        raise RawMeshImportError('HTDTMSH1 contains unsupported flags')
    if reserved16 != 0 or reserved32 != 0:
        raise RawMeshImportError('HTDTMSH1 reserved header fields must be zero')

    has_normals = bool(flags & 0x01)
    has_classifications = bool(flags & 0x02)
    vertex_bytes = vertex_count * 12
    normal_bytes = vertex_count * 12 if has_normals else 0
    index_bytes = face_count * 12
    classification_bytes = face_count if has_classifications else 0
    expected_length = (
        32
        + vertex_bytes
        + normal_bytes
        + index_bytes
        + classification_bytes
    )
    if len(asset) != expected_length:
        raise RawMeshImportError(
            'HTDTMSH1 payload length does not match header counts'
        )

    cursor = 32
    vertices: list[RawMeshVertex] = []
    for _ in range(vertex_count):
        x, y, z = struct.unpack_from('<fff', asset, cursor)
        cursor += 12
        if not all(isfinite(value) for value in (x, y, z)):
            raise RawMeshImportError('HTDTMSH1 vertex must be finite')
        vertices.append(RawMeshVertex(x=x, y=y, z=z))

    normals: list[RawMeshVertex] = []
    if has_normals:
        for _ in range(vertex_count):
            normal = struct.unpack_from('<fff', asset, cursor)
            cursor += 12
            if not all(isfinite(value) for value in normal):
                raise RawMeshImportError('HTDTMSH1 normal must be finite')
            normals.append(
                RawMeshVertex(x=normal[0], y=normal[1], z=normal[2])
            )

    triangles: list[RawMeshTriangle] = []
    for face_index in range(face_count):
        a, b, c = struct.unpack_from('<III', asset, cursor)
        cursor += 12
        if max(a, b, c) >= vertex_count:
            raise RawMeshImportError(
                f'HTDTMSH1 face {face_index} index out of range'
            )
        try:
            triangles.append(
                RawMeshTriangle(
                    a=a,
                    b=b,
                    c=c,
                    source_primitive=f'htdt-meshbin-face:{face_index}',
                )
            )
        except ValueError as exc:
            raise RawMeshImportError(
                f'HTDTMSH1 face {face_index} is degenerate'
            ) from exc

    classifications: tuple[int, ...] = ()
    if has_classifications:
        classifications = tuple(asset[cursor:cursor + face_count])
        cursor += face_count

    if cursor != len(asset):  # pragma: no cover - length check above is exact
        raise RawMeshImportError('HTDTMSH1 parser did not consume payload')
    return vertices, triangles, tuple(normals), classifications


def _parse_obj(asset: bytes) -> tuple[list[RawMeshVertex], list[RawMeshTriangle]]:
    try:
        text = asset.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise RawMeshImportError('OBJ must be UTF-8 text') from exc
    vertices: list[RawMeshVertex] = []
    triangles: list[RawMeshTriangle] = []
    face_index = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        if fields[0] == 'v':
            if len(fields) < 4:
                raise RawMeshImportError(f'OBJ line {line_number}: vertex requires x y z')
            try:
                vertices.append(
                    RawMeshVertex(x=float(fields[1]), y=float(fields[2]), z=float(fields[3]))
                )
            except (ValueError, TypeError) as exc:
                raise RawMeshImportError(f'OBJ line {line_number}: invalid vertex') from exc
        elif fields[0] == 'f':
            if len(fields) < 4:
                raise RawMeshImportError(f'OBJ line {line_number}: face needs at least 3 vertices')
            indices: list[int] = []
            for token in fields[1:]:
                head = token.split('/', 1)[0]
                if not head:
                    raise RawMeshImportError(f'OBJ line {line_number}: missing vertex index')
                try:
                    raw_index = int(head)
                except ValueError as exc:
                    raise RawMeshImportError(f'OBJ line {line_number}: invalid face index') from exc
                if raw_index == 0:
                    raise RawMeshImportError(f'OBJ line {line_number}: OBJ indices are 1-based')
                resolved = raw_index - 1 if raw_index > 0 else len(vertices) + raw_index
                if resolved < 0 or resolved >= len(vertices):
                    raise RawMeshImportError(f'OBJ line {line_number}: face index out of range')
                indices.append(resolved)
            for offset in range(1, len(indices) - 1):
                try:
                    triangles.append(
                        RawMeshTriangle(
                            a=indices[0],
                            b=indices[offset],
                            c=indices[offset + 1],
                            source_primitive=f'obj-face:{face_index}',
                        )
                    )
                except ValueError as exc:
                    raise RawMeshImportError(
                        f'OBJ line {line_number}: degenerate face indices are not supported'
                    ) from exc
            face_index += 1
    if not vertices:
        raise RawMeshImportError('OBJ contains no vertices')
    if not triangles:
        raise RawMeshImportError('OBJ contains no triangle faces')
    return vertices, triangles


_PLY_SCALAR_TYPES = {
    'char': ('<b', 1), 'int8': ('<b', 1),
    'uchar': ('<B', 1), 'uint8': ('<B', 1),
    'short': ('<h', 2), 'int16': ('<h', 2),
    'ushort': ('<H', 2), 'uint16': ('<H', 2),
    'int': ('<i', 4), 'int32': ('<i', 4),
    'uint': ('<I', 4), 'uint32': ('<I', 4),
    'float': ('<f', 4), 'float32': ('<f', 4),
    'double': ('<d', 8), 'float64': ('<d', 8),
}


def _parse_ply(asset: bytes) -> tuple[list[RawMeshVertex], list[RawMeshTriangle]]:
    """Parse PLY (ASCII or binary_little_endian) vertex/face geometry.

    Only the vertex xyz coordinates and face vertex_indices are consumed;
    all other properties are skipped but must still be well-formed enough
    to step over. The format carries no unit declaration — source units are
    interpreted later by the import-authority boundary, never here.
    """

    header_end = asset.find(b'end_header')
    if not asset[:3] == b'ply' or header_end < 0:
        raise RawMeshImportError('PLY header is missing or malformed')
    header_text = asset[:header_end].decode('ascii', errors='strict')
    rest = asset[header_end + len(b'end_header'):]
    # The header is newline-terminated; skip exactly one line terminator.
    if rest[:2] == b'\r\n':
        rest = rest[2:]
    elif rest[:1] in (b'\n', b'\r'):
        rest = rest[1:]

    ply_format: str | None = None
    elements: list[tuple[str, int, list[tuple[str, str, str | None]]]] = []
    current: tuple[str, int, list[tuple[str, str, str | None]]] | None = None
    for raw_line in header_text.splitlines():
        fields = raw_line.split()
        if not fields or fields[0] in ('ply', 'comment', 'obj_info'):
            continue
        if fields[0] == 'format':
            if len(fields) < 2 or fields[1] not in ('ascii', 'binary_little_endian'):
                raise RawMeshImportError(
                    f'unsupported PLY format: {fields[1] if len(fields) > 1 else "missing"}'
                )
            ply_format = fields[1]
        elif fields[0] == 'element':
            if len(fields) < 3:
                raise RawMeshImportError('PLY element declaration is malformed')
            try:
                count = int(fields[2])
            except ValueError as exc:
                raise RawMeshImportError('PLY element count is malformed') from exc
            current = (fields[1], count, [])
            elements.append(current)
        elif fields[0] == 'property':
            if current is None:
                raise RawMeshImportError('PLY property outside an element')
            if len(fields) >= 5 and fields[1] == 'list':
                # property list <count_type> <item_type> <name>: the item type
                # is retained for decoding; the property name is unused.
                current[2].append(('list', fields[2], fields[3]))
            elif len(fields) >= 3:
                current[2].append(('scalar', fields[1], fields[2]))
            else:
                raise RawMeshImportError('PLY property declaration is malformed')
        else:
            raise RawMeshImportError(f'unsupported PLY header line: {raw_line!r}')

    if ply_format is None:
        raise RawMeshImportError('PLY format declaration is missing')

    vertex_spec: list[tuple[str, str, str | None]] | None = None
    vertex_count = 0
    face_spec: list[tuple[str, str, str | None]] | None = None
    face_count = 0
    for name, count, spec in elements:
        if name == 'vertex':
            vertex_spec, vertex_count = spec, count
        elif name == 'face':
            face_spec, face_count = spec, count
    if vertex_spec is None or vertex_count <= 0:
        raise RawMeshImportError('PLY contains no vertex element')

    if ply_format == 'ascii':
        text = rest.decode('ascii', errors='strict')
        tokens = iter(text.split())
        def next_token() -> str:
            try:
                return next(tokens)
            except StopIteration as exc:
                raise RawMeshImportError('PLY ascii payload is truncated') from exc
        def read_scalar(type_name: str):
            if type_name not in _PLY_SCALAR_TYPES:
                raise RawMeshImportError(f'unsupported PLY property type: {type_name}')
            token = next_token()
            try:
                return float(token) if type_name in ('float', 'float32', 'double', 'float64') else int(token)
            except ValueError as exc:
                raise RawMeshImportError('PLY ascii value is not numeric') from exc
        def read_element(spec):
            values = {}
            for kind, type_name, prop_name in spec:
                if kind == 'list':
                    count = int(read_scalar(type_name))
                    item_type = prop_name
                    values[prop_name] = [read_scalar(item_type) for _ in range(count)]
                    values.setdefault(f'{prop_name}__count_type', type_name)
                else:
                    values[prop_name] = read_scalar(type_name)
            return values
    else:
        cursor = 0
        def read_binary(type_name: str):
            nonlocal cursor
            if type_name not in _PLY_SCALAR_TYPES:
                raise RawMeshImportError(f'unsupported PLY property type: {type_name}')
            fmt, size = _PLY_SCALAR_TYPES[type_name]
            if cursor + size > len(rest):
                raise RawMeshImportError('PLY binary payload is truncated')
            (value,) = struct.unpack_from(fmt, rest, cursor)
            cursor += size
            return value
        def read_element(spec):
            values = {}
            for kind, type_name, prop_name in spec:
                if kind == 'list':
                    count = int(read_binary(type_name))
                    item_type = prop_name
                    values[prop_name] = [read_binary(item_type) for _ in range(count)]
                else:
                    values[prop_name] = read_binary(type_name)
            return values

    vertices: list[RawMeshVertex] = []
    triangles: list[RawMeshTriangle] = []
    for name, count, spec in elements:
        if name == 'vertex':
            for _ in range(count):
                record = read_element(spec)
                try:
                    vertices.append(
                        RawMeshVertex(
                            x=float(record['x']),
                            y=float(record['y']),
                            z=float(record['z']),
                        )
                    )
                except KeyError as exc:
                    raise RawMeshImportError('PLY vertex element lacks x/y/z properties') from exc
                except ValueError as exc:
                    raise RawMeshImportError('PLY vertex coordinate is not finite') from exc
        elif name == 'face':
            for face_index in range(count):
                record = read_element(spec)
                indices = None
                for key, value in record.items():
                    if isinstance(value, list):
                        indices = value
                if not indices:
                    continue
                resolved = [int(i) for i in indices]
                if min(resolved) < 0 or max(resolved) >= vertex_count:
                    raise RawMeshImportError(
                        f'PLY face {face_index} index out of range'
                    )
                for offset in range(1, len(resolved) - 1):
                    a, b, c = resolved[0], resolved[offset], resolved[offset + 1]
                    if len({a, b, c}) != 3:
                        continue  # degenerate corners carry no area
                    triangles.append(
                        RawMeshTriangle(
                            a=a, b=b, c=c,
                            source_primitive=f'ply-face:{face_index}',
                        )
                    )
        else:
            for _ in range(count):
                read_element(spec)

    if not vertices:
        raise RawMeshImportError('PLY contains no vertices')
    if not triangles:
        raise RawMeshImportError('PLY contains no triangle faces')
    return vertices, triangles


def _parse_stl(asset: bytes) -> tuple[list[RawMeshVertex], list[RawMeshTriangle]]:
    """Parse STL (binary or ASCII) triangle soup into deduplicated geometry.

    STL carries no unit or axis declaration; facet normals are recomputed
    downstream rather than trusted. Degenerate triangles (no area) are
    dropped — they cannot contribute a footprint or surface.
    """

    vertex_index: dict[tuple[float, float, float], int] = {}
    vertices: list[RawMeshVertex] = []
    triangles: list[RawMeshTriangle] = []

    def add_triangle(coords: Sequence[float], primitive: str) -> None:
        indices: list[int] = []
        for offset in range(0, 9, 3):
            key = (coords[offset], coords[offset + 1], coords[offset + 2])
            index = vertex_index.get(key)
            if index is None:
                index = len(vertices)
                vertex_index[key] = index
                vertices.append(RawMeshVertex(x=key[0], y=key[1], z=key[2]))
            indices.append(index)
        if len(set(indices)) != 3:
            return
        try:
            triangles.append(
                RawMeshTriangle(a=indices[0], b=indices[1], c=indices[2], source_primitive=primitive)
            )
        except ValueError as exc:
            raise RawMeshImportError(f'STL {primitive}: invalid triangle') from exc

    if asset[:5].lower() == b'solid' and b'facet' in asset[:4096]:
        try:
            text = asset.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise RawMeshImportError('ASCII STL must be UTF-8 text') from exc
        coords: list[float] = []
        face_index = 0
        for raw_line in text.splitlines():
            fields = raw_line.split()
            if fields[:1] == ['vertex']:
                if len(fields) != 4:
                    raise RawMeshImportError('ASCII STL vertex line is malformed')
                try:
                    coords.extend(float(component) for component in fields[1:])
                except ValueError as exc:
                    raise RawMeshImportError('ASCII STL vertex is not numeric') from exc
            elif fields[:1] == ['endfacet']:
                if len(coords) != 9:
                    raise RawMeshImportError('ASCII STL facet does not contain 3 vertices')
                add_triangle(coords, f'stl-tri:{face_index}')
                coords = []
                face_index += 1
    else:
        if len(asset) < 84:
            raise RawMeshImportError('binary STL is shorter than its header')
        face_count = struct.unpack_from('<I', asset, 80)[0]
        if 84 + 50 * face_count != len(asset):
            raise RawMeshImportError('binary STL payload length does not match face count')
        cursor = 84
        for face_index in range(face_count):
            values = struct.unpack_from('<12f', asset, cursor)
            add_triangle(values[3:12], f'stl-tri:{face_index}')
            cursor += 50

    if not vertices:
        raise RawMeshImportError('STL contains no vertices')
    if not triangles:
        raise RawMeshImportError('STL contains no triangle faces')
    return vertices, triangles


def _parse_glb(asset: bytes) -> tuple[list[RawMeshVertex], list[RawMeshTriangle]]:
    if len(asset) < 12:
        raise RawMeshImportError('GLB is shorter than its header')
    magic, version, declared_length = struct.unpack_from('<4sII', asset, 0)
    if magic != b'glTF':
        raise RawMeshImportError('GLB magic is invalid')
    if version != 2:
        raise RawMeshImportError(f'only GLB version 2 is supported, got {version}')
    if declared_length != len(asset):
        raise RawMeshImportError('GLB declared length does not match asset length')

    json_chunk: bytes | None = None
    bin_chunks: list[bytes] = []
    cursor = 12
    while cursor < len(asset):
        if cursor + 8 > len(asset):
            raise RawMeshImportError('GLB chunk header is truncated')
        chunk_length, chunk_type = struct.unpack_from('<II', asset, cursor)
        cursor += 8
        end = cursor + chunk_length
        if end > len(asset):
            raise RawMeshImportError('GLB chunk payload is truncated')
        chunk = asset[cursor:end]
        cursor = end
        if chunk_type == 0x4E4F534A:
            if json_chunk is not None:
                raise RawMeshImportError('GLB contains more than one JSON chunk')
            json_chunk = chunk
        elif chunk_type == 0x004E4942:
            bin_chunks.append(chunk)
    if json_chunk is None:
        raise RawMeshImportError('GLB JSON chunk is missing')
    if len(bin_chunks) > 1:
        raise RawMeshImportError('GLB with multiple BIN chunks is not supported')
    try:
        document = json.loads(json_chunk.rstrip(b'\x00 \t\r\n').decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawMeshImportError('GLB JSON chunk is invalid') from exc
    if document.get('asset', {}).get('version') != '2.0':
        raise RawMeshImportError('GLB asset.version must be 2.0')
    if document.get('extensionsRequired'):
        raise RawMeshImportError('required GLB extensions are not supported in the first slice')
    if document.get('animations'):
        raise RawMeshImportError('animated GLB assets are not supported in the first slice')
    buffers = document.get('buffers', [])
    if len(buffers) > 1:
        raise RawMeshImportError('first slice supports one embedded GLB buffer')
    if buffers and buffers[0].get('uri') is not None:
        raise RawMeshImportError('external/data URI GLB buffers are not supported')
    binary = bin_chunks[0] if bin_chunks else b''
    if buffers and int(buffers[0].get('byteLength', -1)) > len(binary):
        raise RawMeshImportError('GLB BIN chunk is shorter than buffer.byteLength')

    meshes = document.get('meshes', [])
    if not meshes:
        raise RawMeshImportError('GLB contains no meshes')
    vertices: list[RawMeshVertex] = []
    triangles: list[RawMeshTriangle] = []

    nodes = document.get('nodes', [])
    scenes = document.get('scenes', [])
    # Cycle detection is path-local (``ancestry``), so a node shared across
    # parents is legitimately re-expanded per placement — but a hostile DAG
    # shaped graph would then expand exponentially. Bound total node
    # expansions so the amplification stays finite while ordinary
    # instancing still works.
    expansions_left = [max(1024, 8 * len(nodes))]
    try:
        if scenes:
            scene_index = int(document.get('scene', 0))
            if scene_index < 0 or scene_index >= len(scenes):
                raise RawMeshImportError('GLB default scene index is out of range')
            root_nodes = scenes[scene_index].get('nodes', [])
            for root_index in root_nodes:
                _append_glb_node(
                    document,
                    binary,
                    int(root_index),
                    _identity4(),
                    vertices,
                    triangles,
                    ancestry=(),
                    expansions_left=expansions_left,
                )
        elif nodes:
            referenced = {
                int(child)
                for node in nodes
                for child in node.get('children', [])
            }
            roots = [index for index in range(len(nodes)) if index not in referenced]
            for root_index in roots:
                _append_glb_node(
                    document,
                    binary,
                    root_index,
                    _identity4(),
                    vertices,
                    triangles,
                    ancestry=(),
                    expansions_left=expansions_left,
                )
        else:
            for mesh_index in range(len(meshes)):
                _append_glb_mesh(
                    document,
                    binary,
                    mesh_index,
                    _identity4(),
                    vertices,
                    triangles,
                    source_prefix=f'glb-mesh:{mesh_index}',
                )
    except RecursionError as exc:
        raise RawMeshImportError('GLB node graph is too deep') from exc
    if not vertices or not triangles:
        raise RawMeshImportError('GLB active scene contains no triangle geometry')
    return vertices, triangles


def _append_glb_node(
    document: dict[str, object],
    binary: bytes,
    node_index: int,
    parent_transform: tuple[tuple[float, float, float, float], ...],
    vertices: list[RawMeshVertex],
    triangles: list[RawMeshTriangle],
    *,
    ancestry: tuple[int, ...],
    expansions_left: list[int],
) -> None:
    expansions_left[0] -= 1
    if expansions_left[0] < 0:
        raise RawMeshImportError(
            'GLB node graph expands beyond the instancing bound'
        )
    nodes = document.get('nodes', [])
    if node_index < 0 or node_index >= len(nodes):
        raise RawMeshImportError('GLB node index is out of range')
    if node_index in ancestry:
        raise RawMeshImportError('GLB node graph contains a cycle')
    node = nodes[node_index]
    if 'skin' in node:
        raise RawMeshImportError('skinned GLB nodes are not supported in the first slice')
    local = _glb_node_matrix(node)
    world = _matmul4(parent_transform, local)
    if 'mesh' in node:
        _append_glb_mesh(
            document,
            binary,
            int(node['mesh']),
            world,
            vertices,
            triangles,
            source_prefix=f'glb-node:{node_index}',
        )
    next_ancestry = ancestry + (node_index,)
    for child in node.get('children', []):
        _append_glb_node(
            document,
            binary,
            int(child),
            world,
            vertices,
            triangles,
            ancestry=next_ancestry,
            expansions_left=expansions_left,
        )


def _append_glb_mesh(
    document: dict[str, object],
    binary: bytes,
    mesh_index: int,
    transform: tuple[tuple[float, float, float, float], ...],
    vertices: list[RawMeshVertex],
    triangles: list[RawMeshTriangle],
    *,
    source_prefix: str,
) -> None:
    meshes = document.get('meshes', [])
    if mesh_index < 0 or mesh_index >= len(meshes):
        raise RawMeshImportError('GLB mesh index is out of range')
    mesh = meshes[mesh_index]
    primitives = mesh.get('primitives', [])
    for primitive_index, primitive in enumerate(primitives):
        if primitive.get('targets'):
            raise RawMeshImportError('GLB morph targets are not supported in the first slice')
        mode = int(primitive.get('mode', 4))
        if mode != 4:
            raise RawMeshImportError('first slice supports GLB TRIANGLES primitives only')
        attributes = primitive.get('attributes', {})
        if 'POSITION' not in attributes:
            raise RawMeshImportError('GLB primitive is missing POSITION')
        positions = _read_glb_positions(document, binary, int(attributes['POSITION']))
        base = len(vertices)
        for position in positions:
            x, y, z = _transform_point(transform, position)
            vertices.append(RawMeshVertex(x=x, y=y, z=z))
        if len(vertices) > MAX_RAW_MESH_VERTICES:
            raise RawMeshImportError(
                'GLB scene expands beyond the vertex bound'
            )
        if 'indices' in primitive:
            indices = _read_glb_indices(document, binary, int(primitive['indices']))
        else:
            indices = list(range(len(positions)))
        if len(indices) % 3 != 0:
            raise RawMeshImportError('GLB TRIANGLES index count must be divisible by 3')
        for offset in range(0, len(indices), 3):
            local = indices[offset : offset + 3]
            if max(local, default=-1) >= len(positions):
                raise RawMeshImportError('GLB primitive index references an unknown POSITION')
            try:
                triangles.append(
                    RawMeshTriangle(
                        a=base + local[0],
                        b=base + local[1],
                        c=base + local[2],
                        source_primitive=(
                            f'{source_prefix}/mesh:{mesh_index}/primitive:{primitive_index}'
                        ),
                    )
                )
            except ValueError as exc:
                raise RawMeshImportError('GLB primitive contains a degenerate triangle index') from exc
        if len(triangles) > MAX_RAW_MESH_TRIANGLES:
            raise RawMeshImportError(
                'GLB scene expands beyond the triangle bound'
            )


def _glb_accessor(document: dict[str, object], accessor_index: int) -> dict[str, object]:
    accessors = document.get('accessors', [])
    if accessor_index < 0 or accessor_index >= len(accessors):
        raise RawMeshImportError('GLB accessor index is out of range')
    accessor = accessors[accessor_index]
    if accessor.get('sparse') is not None:
        raise RawMeshImportError('sparse GLB accessors are not supported in the first slice')
    return accessor


def _read_glb_positions(
    document: dict[str, object], binary: bytes, accessor_index: int
) -> list[tuple[float, float, float]]:
    accessor = _glb_accessor(document, accessor_index)
    if int(accessor.get('componentType', -1)) != 5126 or accessor.get('type') != 'VEC3':
        raise RawMeshImportError('GLB POSITION must be FLOAT VEC3')
    rows = _read_glb_accessor_rows(document, binary, accessor, '<fff', 12)
    return [(float(row[0]), float(row[1]), float(row[2])) for row in rows]


def _read_glb_indices(
    document: dict[str, object], binary: bytes, accessor_index: int
) -> list[int]:
    accessor = _glb_accessor(document, accessor_index)
    if accessor.get('type') != 'SCALAR':
        raise RawMeshImportError('GLB indices accessor must be SCALAR')
    component_type = int(accessor.get('componentType', -1))
    formats = {5121: ('<B', 1), 5123: ('<H', 2), 5125: ('<I', 4)}
    if component_type not in formats:
        raise RawMeshImportError('GLB indices must use UNSIGNED_BYTE/SHORT/INT')
    fmt, size = formats[component_type]
    return [int(row[0]) for row in _read_glb_accessor_rows(document, binary, accessor, fmt, size)]


def _read_glb_accessor_rows(
    document: dict[str, object],
    binary: bytes,
    accessor: dict[str, object],
    fmt: str,
    element_size: int,
) -> list[tuple[object, ...]]:
    if 'bufferView' not in accessor:
        raise RawMeshImportError('GLB accessor without bufferView is not supported')
    views = document.get('bufferViews', [])
    view_index = int(accessor['bufferView'])
    if view_index < 0 or view_index >= len(views):
        raise RawMeshImportError('GLB bufferView index is out of range')
    view = views[view_index]
    if int(view.get('buffer', 0)) != 0:
        raise RawMeshImportError('first slice supports only GLB buffer 0')
    stride = int(view.get('byteStride', element_size))
    if stride < element_size:
        raise RawMeshImportError('GLB bufferView byteStride is smaller than accessor element')
    start = int(view.get('byteOffset', 0)) + int(accessor.get('byteOffset', 0))
    count = int(accessor.get('count', 0))
    if count < 0:
        raise RawMeshImportError('GLB accessor count is invalid')
    view_end = int(view.get('byteOffset', 0)) + int(view.get('byteLength', 0))
    rows: list[tuple[object, ...]] = []
    for index in range(count):
        offset = start + index * stride
        if offset < 0 or offset + element_size > view_end or offset + element_size > len(binary):
            raise RawMeshImportError('GLB accessor reads beyond its bufferView')
        rows.append(struct.unpack_from(fmt, binary, offset))
    return rows


def _identity4() -> tuple[tuple[float, float, float, float], ...]:
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _matmul4(
    left: tuple[tuple[float, float, float, float], ...],
    right: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(4)) for column in range(4))
        for row in range(4)
    )


def _glb_node_matrix(node: dict[str, object]) -> tuple[tuple[float, float, float, float], ...]:
    if 'matrix' in node:
        if any(field in node for field in ('translation', 'rotation', 'scale')):
            raise RawMeshImportError('GLB node must not combine matrix with TRS transforms')
        values = [float(value) for value in node['matrix']]
        if len(values) != 16 or any(not isfinite(value) for value in values):
            raise RawMeshImportError('GLB node matrix must contain 16 finite values')
        return tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4))
    translation = [float(value) for value in node.get('translation', [0.0, 0.0, 0.0])]
    rotation = [float(value) for value in node.get('rotation', [0.0, 0.0, 0.0, 1.0])]
    scale = [float(value) for value in node.get('scale', [1.0, 1.0, 1.0])]
    if len(translation) != 3 or len(rotation) != 4 or len(scale) != 3:
        raise RawMeshImportError('GLB node TRS dimensions are invalid')
    if any(not isfinite(value) for value in translation + rotation + scale):
        raise RawMeshImportError('GLB node TRS values must be finite')
    x, y, z, w = rotation
    norm = sqrt(x * x + y * y + z * z + w * w)
    if norm <= 1.0e-15:
        raise RawMeshImportError('GLB node rotation quaternion must be non-zero')
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    rotation_matrix = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    scale_matrix = (
        (scale[0], 0.0, 0.0, 0.0),
        (0.0, scale[1], 0.0, 0.0),
        (0.0, 0.0, scale[2], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    translation_matrix = (
        (1.0, 0.0, 0.0, translation[0]),
        (0.0, 1.0, 0.0, translation[1]),
        (0.0, 0.0, 1.0, translation[2]),
        (0.0, 0.0, 0.0, 1.0),
    )
    return _matmul4(translation_matrix, _matmul4(rotation_matrix, scale_matrix))


def _transform_point(
    matrix: tuple[tuple[float, float, float, float], ...],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    x, y, z = point
    values = (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )
    if any(not isfinite(value) for value in values):
        raise RawMeshImportError('GLB node transform produced non-finite coordinates')
    return values


def diagnose_raw_visual_mesh(
    mesh: RawVisualMesh,
    *,
    profile: RawMeshDiagnosticProfile | None = None,
) -> RawMeshDiagnosticResult:
    profile = profile or RawMeshDiagnosticProfile()
    mesh_hash = mesh.semantic_hash()
    profile_hash = profile.semantic_hash()
    diagnostic_id = _diagnostic_id(mesh.mesh_id, mesh_hash, profile_hash)

    points = [(vertex.x, vertex.y, vertex.z) for vertex in mesh.vertices]
    diagonal = _bbox_diagonal(points)
    weld_tolerance = max(
        diagonal * profile.weld_relative_tolerance,
        profile.weld_absolute_floor_source_units,
    )
    canonical_vertex_keys = tuple(_quantized_point(point, weld_tolerance) for point in points)

    edge_faces: dict[tuple[tuple[int, int, int], tuple[int, int, int]], list[tuple[int, int]]] = defaultdict(list)
    face_keys: dict[tuple[tuple[int, int, int], ...], list[int]] = defaultdict(list)
    triangle_points: list[tuple[tuple[float, float, float], ...]] = []
    sliver_faces: set[int] = set()
    tiny_faces: set[int] = set()

    tiny_length = max(diagonal * profile.tiny_feature_relative_size, profile.weld_absolute_floor_source_units)
    tiny_area = tiny_length * tiny_length
    for face_index, triangle in enumerate(mesh.triangles):
        indices = (triangle.a, triangle.b, triangle.c)
        keys = tuple(canonical_vertex_keys[index] for index in indices)
        face_keys[tuple(sorted(keys))].append(face_index)
        for start, end in ((0, 1), (1, 2), (2, 0)):
            left, right = keys[start], keys[end]
            if left == right:
                continue
            edge_key = tuple(sorted((left, right)))
            direction = 1 if (left, right) == edge_key else -1
            edge_faces[edge_key].append((face_index, direction))

        tri_points = tuple(points[index] for index in indices)
        triangle_points.append(tri_points)
        lengths_sq = (
            _distance_sq(tri_points[0], tri_points[1]),
            _distance_sq(tri_points[1], tri_points[2]),
            _distance_sq(tri_points[2], tri_points[0]),
        )
        area = _triangle_area(tri_points)
        denominator = sum(lengths_sq)
        quality = 0.0 if denominator <= 0.0 else (4.0 * sqrt(3.0) * area / denominator)
        if quality < profile.sliver_quality_threshold:
            sliver_faces.add(face_index)
        if area <= tiny_area:
            tiny_faces.add(face_index)

    boundary_edges = [edge for edge, incidences in edge_faces.items() if len(incidences) == 1]
    non_manifold_edges = [edge for edge, incidences in edge_faces.items() if len(incidences) > 2]
    duplicate_pairs = sum(len(indices) * (len(indices) - 1) // 2 for indices in face_keys.values() if len(indices) > 1)
    overlapping_pairs = _overlapping_face_pairs(
        triangle_points,
        duplicate_face_groups=face_keys,
        diagonal=diagonal,
        profile=profile,
    )

    watertight = bool(mesh.triangles) and not boundary_edges and not non_manifold_edges
    inverted_count, orientation_complete = _inverted_normal_face_count(
        mesh,
        canonical_vertex_keys,
        edge_faces,
        watertight=watertight,
    )

    findings = (
        _count_finding(
            'open_boundary',
            len(boundary_edges),
            'canonicalized edges incident to only one triangle',
        ),
        _count_finding(
            'non_manifold_edge',
            len(non_manifold_edges),
            'canonicalized edges incident to more than two triangles',
        ),
        _count_finding(
            'duplicate_face',
            duplicate_pairs,
            'triangle pairs with identical canonicalized geometric vertices',
        ),
        _count_finding(
            'overlapping_face',
            overlapping_pairs,
            'distinct coplanar triangle pairs with positive-area overlap',
        ),
        RawMeshDiagnosticFinding(
            code='inverted_normal',
            state=('fail' if inverted_count else ('pass' if orientation_complete else 'unknown')),
            count=inverted_count,
            detail=(
                'faces whose winding differs from deterministic component orientation; '
                'closed manifold components are additionally oriented by signed volume'
                if orientation_complete
                else 'global outward orientation is unknown for open/non-manifold components; local winding conflicts are still counted'
            ),
        ),
        _count_finding(
            'sliver_face',
            len(sliver_faces),
            'triangles below the dimensionless triangle-quality threshold',
        ),
        _count_finding(
            'tiny_feature',
            len(tiny_faces),
            'triangles whose area is below the profile-relative bounding-box threshold',
        ),
        RawMeshDiagnosticFinding(
            code='watertightness',
            state='pass' if watertight else 'fail',
            count=0 if watertight else len(boundary_edges) + len(non_manifold_edges),
            detail='topological watertightness requires every canonicalized edge to have exactly two incident triangles',
        ),
    )

    geometry_checks_pass = all(
        finding.state == 'pass'
        for finding in findings
        if finding.code != 'inverted_normal'
    ) and next(f for f in findings if f.code == 'inverted_normal').state == 'pass'
    readiness: AcousticVolumeReadiness = (
        'geometry_checks_pass_but_semantic_conversion_required'
        if geometry_checks_pass
        else 'not_ready'
    )
    return RawMeshDiagnosticResult(
        diagnostic_id=diagnostic_id,
        raw_mesh_id=mesh.mesh_id,
        raw_mesh_semantic_hash=mesh_hash,
        profile=profile,
        profile_semantic_hash=profile_hash,
        findings=findings,
        acoustic_volume_readiness=readiness,
    )


def _diagnostic_id(raw_mesh_id: str, raw_mesh_hash: str, profile_hash: str) -> str:
    identity = _semantic_hash(
        {
            'raw_mesh_id': raw_mesh_id,
            'raw_mesh_semantic_hash': raw_mesh_hash,
            'profile_semantic_hash': profile_hash,
        }
    )
    return f'raw-mesh-diagnostic:{identity}'


def _count_finding(code: DiagnosticCode, count: int, detail: str) -> RawMeshDiagnosticFinding:
    return RawMeshDiagnosticFinding(
        code=code,
        state='fail' if count else 'pass',
        count=count,
        detail=detail,
    )


def _bbox_diagonal(points: list[tuple[float, float, float]]) -> float:
    if not points:
        return 0.0
    xs, ys, zs = zip(*points, strict=True)
    return sqrt(
        (max(xs) - min(xs)) ** 2
        + (max(ys) - min(ys)) ** 2
        + (max(zs) - min(zs)) ** 2
    )


def _quantized_point(point: tuple[float, float, float], tolerance: float) -> tuple[int, int, int]:
    return tuple(floor(value / tolerance + 0.5) for value in point)


def _distance_sq(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum((left[index] - right[index]) ** 2 for index in range(3))


def _subtract(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _norm(vector: tuple[float, float, float]) -> float:
    return sqrt(_dot(vector, vector))


def _triangle_area(triangle: tuple[tuple[float, float, float], ...]) -> float:
    return 0.5 * _norm(_cross(_subtract(triangle[1], triangle[0]), _subtract(triangle[2], triangle[0])))


def _overlapping_face_pairs(
    triangle_points: list[tuple[tuple[float, float, float], ...]],
    *,
    duplicate_face_groups: dict[tuple[tuple[int, int, int], ...], list[int]],
    diagonal: float,
    profile: RawMeshDiagnosticProfile,
) -> int:
    if len(triangle_points) < 2:
        return 0
    duplicate_pairs = {
        (left, right)
        for indices in duplicate_face_groups.values()
        if len(indices) > 1
        for position, left in enumerate(indices)
        for right in indices[position + 1 :]
    }
    tolerance = max(
        diagonal * profile.coplanar_relative_tolerance,
        profile.weld_absolute_floor_source_units,
    )
    area_tolerance = max(
        diagonal * diagonal * profile.overlap_area_relative_tolerance,
        profile.weld_absolute_floor_source_units ** 2,
    )
    bounds = [_triangle_bounds(triangle) for triangle in triangle_points]
    order = sorted(range(len(bounds)), key=lambda index: (bounds[index][0], index))
    active: deque[int] = deque()
    count = 0
    for current in order:
        min_x, min_y, min_z, max_x, max_y, max_z = bounds[current]
        active = deque(index for index in active if bounds[index][3] >= min_x - tolerance)
        for other in active:
            if tuple(sorted((other, current))) in duplicate_pairs:
                continue
            other_bounds = bounds[other]
            if other_bounds[4] < min_y - tolerance or max_y < other_bounds[1] - tolerance:
                continue
            if other_bounds[5] < min_z - tolerance or max_z < other_bounds[2] - tolerance:
                continue
            if _coplanar_overlap_area(
                triangle_points[other],
                triangle_points[current],
                tolerance=tolerance,
            ) > area_tolerance:
                count += 1
        active.append(current)
    return count


def _triangle_bounds(triangle: tuple[tuple[float, float, float], ...]) -> tuple[float, ...]:
    xs, ys, zs = zip(*triangle, strict=True)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _coplanar_overlap_area(
    first: tuple[tuple[float, float, float], ...],
    second: tuple[tuple[float, float, float], ...],
    *,
    tolerance: float,
) -> float:
    first_normal = _cross(_subtract(first[1], first[0]), _subtract(first[2], first[0]))
    second_normal = _cross(_subtract(second[1], second[0]), _subtract(second[2], second[0]))
    first_norm = _norm(first_normal)
    second_norm = _norm(second_normal)
    if first_norm <= tolerance or second_norm <= tolerance:
        return 0.0
    parallel = _norm(_cross(first_normal, second_normal)) / (first_norm * second_norm)
    if parallel > 1.0e-8:
        return 0.0
    unit_normal = tuple(component / first_norm for component in first_normal)
    if any(abs(_dot(_subtract(point, first[0]), unit_normal)) > tolerance for point in second):
        return 0.0
    axis = max(range(3), key=lambda index: abs(unit_normal[index]))
    projected_first = [_project2(point, axis) for point in first]
    projected_second = [_project2(point, axis) for point in second]
    intersection = _convex_clip(projected_first, projected_second, epsilon=tolerance)
    if len(intersection) < 3:
        return 0.0
    projected_area = abs(_polygon_signed_area(intersection))
    scale = abs(unit_normal[axis])
    return 0.0 if scale <= 1.0e-15 else projected_area / scale


def _project2(point: tuple[float, float, float], drop_axis: int) -> tuple[float, float]:
    values = [point[index] for index in range(3) if index != drop_axis]
    return (values[0], values[1])


def _polygon_signed_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )


def _convex_clip(
    subject: list[tuple[float, float]],
    clip: list[tuple[float, float]],
    *,
    epsilon: float,
) -> list[tuple[float, float]]:
    output = list(subject)
    clip_polygon = list(clip)
    if _polygon_signed_area(clip_polygon) < 0.0:
        clip_polygon.reverse()
    for index, clip_start in enumerate(clip_polygon):
        clip_end = clip_polygon[(index + 1) % len(clip_polygon)]
        input_polygon = output
        output = []
        if not input_polygon:
            break
        previous = input_polygon[-1]
        previous_inside = _inside2(previous, clip_start, clip_end, epsilon)
        for current in input_polygon:
            current_inside = _inside2(current, clip_start, clip_end, epsilon)
            if current_inside:
                if not previous_inside:
                    output.append(_line_intersection2(previous, current, clip_start, clip_end))
                output.append(current)
            elif previous_inside:
                output.append(_line_intersection2(previous, current, clip_start, clip_end))
            previous = current
            previous_inside = current_inside
    return output


def _inside2(
    point: tuple[float, float],
    edge_start: tuple[float, float],
    edge_end: tuple[float, float],
    epsilon: float,
) -> bool:
    return (
        (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1])
        - (edge_end[1] - edge_start[1]) * (point[0] - edge_start[0])
    ) >= -epsilon


def _line_intersection2(
    line_start: tuple[float, float],
    line_end: tuple[float, float],
    edge_start: tuple[float, float],
    edge_end: tuple[float, float],
) -> tuple[float, float]:
    line_delta = (line_end[0] - line_start[0], line_end[1] - line_start[1])
    edge_delta = (edge_end[0] - edge_start[0], edge_end[1] - edge_start[1])
    denominator = line_delta[0] * edge_delta[1] - line_delta[1] * edge_delta[0]
    if abs(denominator) <= 1.0e-30:
        return line_end
    offset = (edge_start[0] - line_start[0], edge_start[1] - line_start[1])
    t = (offset[0] * edge_delta[1] - offset[1] * edge_delta[0]) / denominator
    return (line_start[0] + t * line_delta[0], line_start[1] + t * line_delta[1])


def _inverted_normal_face_count(
    mesh: RawVisualMesh,
    canonical_vertex_keys: tuple[tuple[int, int, int], ...],
    edge_faces: dict[tuple[tuple[int, int, int], tuple[int, int, int]], list[tuple[int, int]]],
    *,
    watertight: bool,
) -> tuple[int, bool]:
    adjacency: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for incidences in edge_faces.values():
        if len(incidences) != 2:
            continue
        (left_face, left_direction), (right_face, right_direction) = incidences
        needs_relative_flip = left_direction == right_direction
        adjacency[left_face].append((right_face, needs_relative_flip))
        adjacency[right_face].append((left_face, needs_relative_flip))

    assigned: dict[int, bool] = {}
    components: list[list[int]] = []
    orientation_complete = watertight
    for start in range(len(mesh.triangles)):
        if start in assigned:
            continue
        assigned[start] = False
        component: list[int] = []
        queue: deque[int] = deque([start])
        conflict = False
        while queue:
            face = queue.popleft()
            component.append(face)
            for neighbor, relative_flip in adjacency.get(face, []):
                expected = assigned[face] ^ relative_flip
                if neighbor in assigned:
                    if assigned[neighbor] != expected:
                        conflict = True
                    continue
                assigned[neighbor] = expected
                queue.append(neighbor)
        if conflict:
            orientation_complete = False
        components.append(component)

    flip_faces: set[int] = set()
    for component in components:
        component_assignment = {face: assigned[face] for face in component}
        closed_component = _component_is_closed(component, edge_faces)
        if closed_component:
            signed_volume = sum(
                _signed_triangle_volume(mesh, face, flip=component_assignment[face])
                for face in component
            )
            if signed_volume < 0.0:
                component_assignment = {face: not flip for face, flip in component_assignment.items()}
        else:
            flipped = sum(1 for flip in component_assignment.values() if flip)
            if flipped > len(component) - flipped:
                component_assignment = {face: not flip for face, flip in component_assignment.items()}
        flip_faces.update(face for face, flip in component_assignment.items() if flip)
    return len(flip_faces), orientation_complete


def _component_is_closed(
    component: list[int],
    edge_faces: dict[tuple[tuple[int, int, int], tuple[int, int, int]], list[tuple[int, int]]],
) -> bool:
    members = set(component)
    touched = [incidences for incidences in edge_faces.values() if any(face in members for face, _ in incidences)]
    return bool(touched) and all(len(incidences) == 2 and all(face in members for face, _ in incidences) for incidences in touched)


def _signed_triangle_volume(mesh: RawVisualMesh, face_index: int, *, flip: bool) -> float:
    triangle = mesh.triangles[face_index]
    order = (triangle.a, triangle.c, triangle.b) if flip else (triangle.a, triangle.b, triangle.c)
    a, b, c = (
        (mesh.vertices[index].x, mesh.vertices[index].y, mesh.vertices[index].z)
        for index in order
    )
    return _dot(a, _cross(b, c)) / 6.0
