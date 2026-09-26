"""#1081 — ADM/BW64 fixture validator tests."""

import struct

from htdt.cad_adm_bw64_validator import (
    chunk_payload,
    parse_bw64_structure,
    parse_chna,
    summarize_adm,
    validate_adm_bw64,
)


def _chunk(cid: bytes, payload: bytes) -> bytes:
    pad = b'\x00' if len(payload) & 1 else b''
    return cid + struct.pack('<I', len(payload)) + payload + pad


def _chna_entry(audio_id, track_index, uid, track_ref, pack_ref):
    rec = struct.pack('<HH', audio_id, track_index)
    rec += uid.encode('ascii').ljust(12, b'\x00')
    rec += track_ref.encode('ascii').ljust(11, b'\x00')
    rec += pack_ref.encode('ascii').ljust(11, b'\x00')
    rec += b'\x00' * 2
    return rec


def _chna(entries) -> bytes:
    return struct.pack('<HH', len(entries), len(entries)) + b''.join(
        entries
    )


ADM_XML = b"""<?xml version="1.0"?>
<ebuCoreMain xmlns="urn:ebu:metadata-schema:ebuCore_2014"
 xmlns:adm="urn:metadata-schema:adm">
 <coreMetadata>
  <format>
   <audioFormatExtended>
    <audioTrackUID UID="ATU_1" audioTrackIDRef="AT_00010001_01"/>
    <audioChannelFormat audioChannelFormatID="AC_00010001"
     audioChannelFormatName="RoomCentric" typeLabel="0004"
     typeDefinition="HOA"/>
    <audioChannelFormat audioChannelFormatID="AC_00010002"
     audioChannelFormatName="ObjectA" typeLabel="0003"
     typeDefinition="Objects"/>
   </audioFormatExtended>
  </format>
 </coreMetadata>
</ebuCoreMain>
"""


def _bw64(*, chna: bytes | None, axml: bytes | None) -> bytes:
    chunks = _chunk(b'fmt ', b'\x01\x00\x02\x00' + b'\x00' * 12)
    if chna is not None:
        chunks += _chunk(b'chna', chna)
    if axml is not None:
        chunks += _chunk(b'axml', axml)
    chunks += _chunk(b'data', b'\x00' * 8)
    total = 4 + len(chunks)
    return b'BW64' + struct.pack('<I', total) + b'WAVE' + chunks


GOOD_CHNA = _chna([_chna_entry(1, 1, 'ATU_1', 'AT_00010001',
                               'AP_0001')])


class TestBw64Structure:
    def test_parse_valid(self):
        data = _bw64(chna=GOOD_CHNA, axml=ADM_XML)
        s = parse_bw64_structure(data)
        assert s is not None
        assert s.form == 'bw64'
        assert s.has_chna and s.has_axml
        ids = {c.chunk_id for c in s.chunks}
        assert {'fmt ', 'chna', 'axml', 'data'} <= ids

    def test_not_wave(self):
        assert parse_bw64_structure(b'NOPE............') is None

    def test_truncated(self):
        assert parse_bw64_structure(b'BW64') is None


class TestChna:
    def test_parse_entry(self):
        data = _bw64(chna=GOOD_CHNA, axml=ADM_XML)
        s = parse_bw64_structure(data)
        raw = chunk_payload(data, s, 'chna')
        entries = parse_chna(raw)
        assert len(entries) == 1
        e = entries[0]
        assert e.audio_id == 1
        assert e.uid == 'ATU_1'
        assert e.track_ref == 'AT_00010001'
        assert e.pack_ref == 'AP_0001'

    def test_truncated_chna(self):
        assert parse_chna(b'\x01\x00') is None


class TestAdmSummary:
    def test_kinds(self):
        s = summarize_adm(ADM_XML)
        assert s is not None
        assert 'ATU_1' in s.audio_track_uids
        assert s.has_objects
        assert s.has_hoa
        assert not s.has_directspeakers
        kinds = {f.kind for f in s.channel_formats}
        assert kinds == {'Objects', 'HOA'}

    def test_bad_xml(self):
        assert summarize_adm(b'<unclosed>') is None


class TestEndToEnd:
    def test_valid_fixture(self):
        r = validate_adm_bw64(_bw64(chna=GOOD_CHNA, axml=ADM_XML))
        assert r.verdict == 'valid'
        assert r.summary is not None

    def test_missing_chna(self):
        r = validate_adm_bw64(_bw64(chna=None, axml=ADM_XML))
        assert r.verdict == 'invalid'
        assert 'chna' in r.detail

    def test_missing_axml(self):
        r = validate_adm_bw64(_bw64(chna=GOOD_CHNA, axml=None))
        assert r.verdict == 'invalid'
        assert 'axml' in r.detail

    def test_unresolved_chna_uid(self):
        bad = _chna(
            [_chna_entry(9, 9, 'ATU_NOPE', 'AT_x', 'AP_x')]
        )
        r = validate_adm_bw64(_bw64(chna=bad, axml=ADM_XML))
        assert r.verdict == 'invalid'
        assert 'audioIDs' in r.detail

    def test_not_container(self):
        r = validate_adm_bw64(b'\x00' * 50)
        assert r.verdict == 'invalid'
