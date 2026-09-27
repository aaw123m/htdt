"""ADM/BW64 immersive-source fixture validator (#1081).

Independent structural validation of Audio Definition Model (ADM,
ITU-R BS.2076) metadata carried inside a Broadcast Wave 64 (BW64 /
RF64, EBU Tech 3306) container — verifying that object, DirectSpeakers
and HOA channel metadata lineage is internally consistent before an
immersive fixture feeds the rendering path.

Rules:

- parsing is an independent implementation: the BW64 chunk table and
  the ADM XML are read directly; EBU's ``libadm``/``libear`` are the
  external reference oracles — cited, never vendored;
- verdicts are fail-closed: a malformed container, an ``axml`` payload
  that fails XML parsing, or a ``chna`` reference that has no ADM
  track — all produce ``invalid``, never a partial pass;
- lineage is *structural* — the validator confirms every
  ``chna``-declared audioID resolves to ADM ``audioTrack``/
  ``audioChannelFormat`` references and that channel-format type
  labels (``Objects``/``DirectSpeakers``/``HOA``) are consistent; it
  does not judge artistic metadata.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Literal, NamedTuple
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .xml_guard import contains_xml_doctype


ADM_VALIDATOR_AUTHORITY_VERSION = 'adm-bw64-validator-1'


# EBU reference implementations (external oracles — never vendored).
LIBADM_REFERENCE = 'https://github.com/ebu/libadm'
LIBEAR_REFERENCE = 'https://github.com/ebu/libear'


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strip_ns(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


AdmChannelKind = Literal['Objects', 'DirectSpeakers', 'HOA', 'other']

# BS.2076 typeLabel / typeDefinition -> internal kind.
_TYPE_KIND: dict[str, AdmChannelKind] = {
    '0001': 'DirectSpeakers',
    '0003': 'Objects',
    '0004': 'HOA',
    'directspeakers': 'DirectSpeakers',
    'objects': 'Objects',
    'hoa': 'HOA',
}

ValidationVerdict = Literal['valid', 'invalid']


# --- BW64 container -------------------------------------------------------

class Bw64Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str = Field(min_length=4, max_length=4)
    size: int = Field(ge=0)
    offset: int = Field(ge=0)


class Bw64Structure(BaseModel):
    """Chunk table of one parsed BW64/RF64 container."""

    model_config = ConfigDict(frozen=True)

    form: Literal['bw64', 'rf64', 'riff']
    chunks: tuple[Bw64Chunk, ...] = Field(min_length=1)
    has_ds64: bool
    has_chna: bool
    has_axml: bool
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    def chunk(self, chunk_id: str) -> Bw64Chunk | None:
        for c in self.chunks:
            if c.chunk_id == chunk_id:
                return c
        return None


def parse_bw64_structure(data: bytes) -> Bw64Structure | None:
    """Walk the RIFF/BW64 chunk table. ``None`` on any structural
    defect — a truncated table is never 'best effort'."""
    if len(data) < 12:
        return None
    riff_id = data[0:4]
    form = {
        b'BW64': 'bw64',
        b'RF64': 'rf64',
        b'RIFF': 'riff',
    }.get(riff_id)
    if form is None or data[8:12] != b'WAVE':
        return None
    chunks: list[Bw64Chunk] = []
    offset = 12
    while offset + 8 <= len(data):
        try:
            cid = data[offset:offset + 4].decode('latin-1')
        except UnicodeDecodeError:
            return None
        (size,) = struct.unpack_from('<I', data, offset + 4)
        chunks.append(
            Bw64Chunk(
                chunk_id=cid,
                size=size,
                offset=offset + 8,
            )
        )
        # RIFF chunks are word-aligned to even boundaries.
        offset += 8 + size + (size & 1)
    ids = {c.chunk_id for c in chunks}
    return Bw64Structure(
        form=form,  # type: ignore[arg-type]
        chunks=tuple(chunks),
        has_ds64='ds64' in ids,
        has_chna='chna' in ids,
        has_axml='axml' in ids,
        source_sha256=_sha256_bytes(data),
    )


def chunk_payload(data: bytes, structure: Bw64Structure, chunk_id: str) -> bytes | None:
    chunk = structure.chunk(chunk_id)
    if chunk is None or chunk.offset + chunk.size > len(data):
        return None
    return data[chunk.offset:chunk.offset + chunk.size]


# --- chna chunk ------------------------------------------------------------

class ChnaEntry(NamedTuple):
    audio_id: int
    track_index: int
    uid: str
    track_ref: str
    pack_ref: str


def parse_chna(data: bytes) -> tuple[ChnaEntry, ...] | None:
    """Parse the EBU Tech 3306 ``chna`` chunk (audioID table)."""
    if len(data) < 4:
        return None
    num_tracks, num_uids = struct.unpack_from('<HH', data, 0)
    if num_tracks > num_uids or num_uids > 0xFFFF:
        return None
    entries: list[ChnaEntry] = []
    offset = 4
    record_size = 40  # 2 + 2 + 12 + 11 + 11 + 2 pad... exact per spec
    # chna record: audioID(2) trackIndex(2) UID(12) trackRef(11)
    #              packRef(11) + 2 pad = 40
    for _ in range(num_uids):
        if offset + record_size > len(data):
            return None
        audio_id, track_index = struct.unpack_from(
            '<HH', data, offset
        )
        uid = data[offset + 4:offset + 16].split(b'\x00')[0].decode(
            'ascii', errors='replace'
        )
        track_ref = data[offset + 16:offset + 27].split(b'\x00')[0].decode(
            'ascii', errors='replace'
        )
        pack_ref = data[offset + 27:offset + 38].split(b'\x00')[0].decode(
            'ascii', errors='replace'
        )
        entries.append(
            ChnaEntry(
                audio_id=audio_id,
                track_index=track_index,
                uid=uid,
                track_ref=track_ref,
                pack_ref=pack_ref,
            )
        )
        offset += record_size
    return tuple(entries)


# --- ADM XML -------------------------------------------------------------

class AdmChannelFormat(NamedTuple):
    format_id: str
    name: str
    kind: AdmChannelKind


class AdmSummary(BaseModel):
    """Structural summary of the ADM (axml) payload."""

    model_config = ConfigDict(frozen=True)

    audio_track_uids: frozenset[str]
    audio_track_refs: frozenset[str]
    channel_formats: tuple[AdmChannelFormat, ...]
    has_objects: bool
    has_directspeakers: bool
    has_hoa: bool
    adm_version_attr: str | None


def summarize_adm(xml_payload: bytes) -> AdmSummary | None:
    if contains_xml_doctype(xml_payload):
        return None
    try:
        root = ElementTree.fromstring(xml_payload)
    except ElementTree.ParseError:
        return None
    track_uids: set[str] = set()
    track_refs: set[str] = set()
    formats: list[AdmChannelFormat] = []
    for el in root.iter():
        name = _strip_ns(el.tag)
        if name == 'audioTrackUID':
            uid = (el.get('UID') or el.text or '').strip()
            if uid:
                track_uids.add(uid)
            ref = el.get('audioTrackIDRef') or ''
            if ref:
                track_refs.add(ref.strip())
        elif name == 'audioChannelFormat':
            fid = (el.get('audioChannelFormatID') or '').strip()
            fname = (el.get('audioChannelFormatName') or '').strip()
            type_label = (el.get('typeLabel') or '').strip()
            type_def = (el.get('typeDefinition') or '').strip()
            # BS.2076: typeLabel is numeric (0001..0005), typeDefinition
            # carries the name — accept either spelling.
            label = type_label or type_def
            kind: AdmChannelKind = _TYPE_KIND.get(
                label.lower(), 'other'
            )
            if fid:
                formats.append(
                    AdmChannelFormat(fid, fname, kind)
                )
    return AdmSummary(
        audio_track_uids=frozenset(track_uids),
        audio_track_refs=frozenset(track_refs),
        channel_formats=tuple(formats),
        has_objects=any(f.kind == 'Objects' for f in formats),
        has_directspeakers=any(
            f.kind == 'DirectSpeakers' for f in formats
        ),
        has_hoa=any(f.kind == 'HOA' for f in formats),
        adm_version_attr=root.get('version'),
    )


# --- top-level verdict ----------------------------------------------------

class AdmValidationReport(NamedTuple):
    verdict: ValidationVerdict
    detail: str
    structure: Bw64Structure | None
    summary: AdmSummary | None


def validate_adm_bw64(data: bytes) -> AdmValidationReport:
    """Validate one BW64+ADM fixture end to end.

    Verdicts:
    - ``invalid`` — malformed container, missing chna/axml, XML parse
      failure, or any chna audioID lacking a matching ADM track ref;
    - ``valid`` — container is sound and every chna entry resolves.
    """
    structure = parse_bw64_structure(data)
    if structure is None:
        return AdmValidationReport(
            'invalid', 'not a RIFF/BW64 WAVE container', None, None
        )
    if not structure.has_chna:
        return AdmValidationReport(
            'invalid', 'no chna chunk — no ADM track table',
            structure, None,
        )
    chna_data = chunk_payload(data, structure, 'chna')
    entries = parse_chna(chna_data) if chna_data else None
    if entries is None:
        return AdmValidationReport(
            'invalid', 'chna chunk malformed', structure, None
        )
    axml = chunk_payload(data, structure, 'axml')
    if axml is None:
        return AdmValidationReport(
            'invalid',
            'chna present but no axml chunk — ADM payload missing',
            structure,
            None,
        )
    summary = summarize_adm(axml)
    if summary is None:
        return AdmValidationReport(
            'invalid', 'axml payload is not parseable XML',
            structure, None,
        )
    unresolved = [
        e.uid
        for e in entries
        if e.uid not in summary.audio_track_uids
    ]
    if unresolved:
        return AdmValidationReport(
            'invalid',
            f'chna audioIDs without ADM audioTrackUID: '
            f'{unresolved[:4]}',
            structure,
            summary,
        )
    return AdmValidationReport(
        'valid',
        f'{len(entries)} chna entries resolve to ADM tracks',
        structure,
        summary,
    )


__all__ = [
    'ADM_VALIDATOR_AUTHORITY_VERSION',
    'AdmChannelFormat',
    'AdmChannelKind',
    'AdmSummary',
    'AdmValidationReport',
    'Bw64Chunk',
    'Bw64Structure',
    'ChnaEntry',
    'LIBADM_REFERENCE',
    'LIBEAR_REFERENCE',
    'ValidationVerdict',
    'chunk_payload',
    'parse_bw64_structure',
    'parse_chna',
    'summarize_adm',
    'validate_adm_bw64',
]
