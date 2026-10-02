"""Regression coverage for the round-2 security & robustness review.

Each test pins one verified round-2 fix:

- ``migration_guard._restore_pre_migration_backup`` now extracts members
  through a bounded, typed, duplicate-free stream instead of
  ``ZipFile.extractall`` (round-1 F4 residual): forged declared sizes,
  symlinks, special files and duplicate names are rejected before any
  restore touches the live data directory.
- The only two XML parsers in the codebase
  (``cad_adm_bw64_validator.summarize_adm`` and
  ``cad_spectral_lighting.parse_spectral_xml``) reject DOCTYPE/ENTITY
  declarations up front — ``xml.etree`` still expands internal entities,
  so a billion-laughs payload must fail closed before parsing.
- User-picked file reads in workspace/import paths are bounded through
  ``read_file_bounded`` (underlay, entity/room meshes, SOFA datasets).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import types
import zipfile

import pytest

from htdt.cad_adm_bw64_validator import summarize_adm
from htdt.cad_spectral_lighting import parse_spectral_xml
from htdt.ingress import IngressTooLargeError, read_file_bounded
from htdt.migration_guard import (
    MigrationOpenError,
    _restore_pre_migration_backup,
)
from htdt.xml_guard import contains_xml_doctype


# --- helpers --------------------------------------------------------------

_V1_MANIFEST = {
    'schema_version': 1,
    'target_schema_version': 5,
    'reason': 'pre_migration',
}


def _v1_sqlite_bytes(tmp_path: Path) -> bytes:
    db_path = tmp_path / 'v1-source.sqlite3'
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES ('schema_version', '1');"
        )
        connection.commit()
    finally:
        connection.close()
    return db_path.read_bytes()


def _rollback_archive(
    path: Path,
    db_bytes: bytes,
    *,
    members: list[tuple[str, bytes, int]] | None = None,
    asset_payloads: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('htdt.sqlite3', db_bytes)
        archive.writestr('manifest.json', json.dumps(_V1_MANIFEST))
        archive.writestr('assets/', b'')
        for name, payload in (asset_payloads or {}).items():
            archive.writestr(name, payload)
        for name, payload, mode in members or []:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, payload)
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / 'data'
    root.mkdir(parents=True)
    return root


class TestRollbackArchiveMemberTypes:
    def test_symlink_member_rejected(self, tmp_path: Path) -> None:
        archive = _rollback_archive(
            tmp_path / 'backup.zip',
            _v1_sqlite_bytes(tmp_path),
            members=[
                ('assets/evil', b'payload', stat.S_IFLNK | 0o777),
            ],
        )
        root = _root(tmp_path)
        with pytest.raises(MigrationOpenError, match='symlink'):
            _restore_pre_migration_backup(root, archive, 1)
        assert not (root / 'htdt.sqlite3').exists()
        assert not (root / 'assets').exists()

    def test_special_file_member_rejected(self, tmp_path: Path) -> None:
        archive = _rollback_archive(
            tmp_path / 'backup.zip',
            _v1_sqlite_bytes(tmp_path),
            members=[
                ('assets/fifo', b'', stat.S_IFIFO | 0o644),
            ],
        )
        root = _root(tmp_path)
        with pytest.raises(MigrationOpenError, match='special file'):
            _restore_pre_migration_backup(root, archive, 1)

    def test_duplicate_member_name_rejected(self, tmp_path: Path) -> None:
        archive = _rollback_archive(
            tmp_path / 'backup.zip',
            _v1_sqlite_bytes(tmp_path),
            members=[
                ('assets/dup.bin', b'first', 0),
                ('assets/dup.bin', b'second', 0),
            ],
        )
        root = _root(tmp_path)
        with pytest.raises(MigrationOpenError, match='duplicate'):
            _restore_pre_migration_backup(root, archive, 1)

    def test_traversal_member_still_rejected(self, tmp_path: Path) -> None:
        archive = _rollback_archive(
            tmp_path / 'backup.zip',
            _v1_sqlite_bytes(tmp_path),
            members=[
                ('../escape.bin', b'x', 0),
            ],
        )
        root = _root(tmp_path)
        with pytest.raises(MigrationOpenError, match='Unsafe path'):
            _restore_pre_migration_backup(root, archive, 1)


class TestRollbackBoundedExtraction:
    def test_forged_declared_size_aborts_before_write(
        self, tmp_path: Path
    ) -> None:
        # Understate the central-directory file_size so the declared size
        # fits but the actual deflate stream is much larger — the bounded
        # extractor must stop on actual bytes, not the lying header.
        payload = b'A' * (1 << 21)  # ~2 MiB highly compressible
        archive_path = tmp_path / 'backup.zip'
        _rollback_archive(
            archive_path,
            _v1_sqlite_bytes(tmp_path),
            asset_payloads={'assets/swell.bin': payload},
        )
        blob = bytearray(archive_path.read_bytes())
        # Find the central-directory record for assets/swell.bin and shrink
        # its declared uncompressed size (CD field at offset 24).
        name = b'assets/swell.bin'
        central = blob.find(b'PK\x01\x02')
        while central >= 0:
            name_len = struct.unpack_from('<H', blob, central + 28)[0]
            if bytes(blob[central + 46 : central + 46 + name_len]) == name:
                break
            central = blob.find(b'PK\x01\x02', central + 1)
        assert central > 0
        struct.pack_into('<I', blob, central + 24, 4)
        archive_path.write_bytes(bytes(blob))

        root = _root(tmp_path)
        # The member is rejected — either the actual-bytes cap trips first
        # or zipfile's CRC check fails the stream; both fail closed before
        # anything reaches the live tree.
        with pytest.raises(
            MigrationOpenError,
            match='exceeds its declared size|Unreadable member',
        ):
            _restore_pre_migration_backup(root, archive_path, 1)
        # The abort happens in staging; nothing reaches the live tree.
        assert not (root / 'htdt.sqlite3').exists()
        assert not (root / 'assets' / 'swell.bin').exists()
        assert not (root / 'assets').exists()

    def test_honest_backup_round_trips(self, tmp_path: Path) -> None:
        asset = b'legit-asset-bytes'
        asset_name = f'assets/{hashlib.sha256(asset).hexdigest()}.bin'
        archive = _rollback_archive(
            tmp_path / 'backup.zip',
            _v1_sqlite_bytes(tmp_path),
            asset_payloads={asset_name: asset},
        )
        root = _root(tmp_path)
        _restore_pre_migration_backup(root, archive, 1)
        assert (root / asset_name).read_bytes() == asset
        connection = sqlite3.connect(root / 'htdt.sqlite3')
        try:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
        finally:
            connection.close()
        assert row == ('1',)


class TestXmlEntityGuard:
    def test_adm_summary_rejects_doctype(self) -> None:
        payload = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE adm [<!ENTITY x "boom">]>\n'
            b'<adm>&x;</adm>'
        )
        assert contains_xml_doctype(payload)
        assert summarize_adm(payload) is None

    def test_adm_summary_parses_clean_document(self) -> None:
        payload = (
            b'<?xml version="1.0"?>\n'
            b'<ituADM><audioTrackUID UID="TU_0001"/></ituADM>'
        )
        summary = summarize_adm(payload)
        assert summary is not None
        assert 'TU_0001' in summary.audio_track_uids

    def test_spectral_xml_rejects_entity_bomb(self) -> None:
        bomb = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE lolz [\n'
            '<!ENTITY lol "lollollollollollollollollollol">\n'
            '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
            '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
            ']>\n'
            '<spdx>&lol3;</spdx>'
        )
        doc = parse_spectral_xml(bomb, evidence_id='ev-1')
        assert doc.verdict == 'invalid'
        assert 'DOCTYPE' in (doc.detail or '')

    def test_spectral_xml_rejects_lowered_doctype(self) -> None:
        doc = parse_spectral_xml(
            '<!doctype x [<!entity y "z">]><spdx/>',
            evidence_id='ev-2',
        )
        assert doc.verdict == 'invalid'

    def test_plain_payload_not_flagged(self) -> None:
        assert not contains_xml_doctype(b'<spdx><foo/></spdx>')
        assert not contains_xml_doctype('plain text')

    def test_utf16_doctype_is_rejected(self) -> None:
        # A BOM-prefixed wide-char payload must not slip past the byte scan:
        # pyexpat expands internal entities instead of rejecting them.
        for encoding in ('utf-16', 'utf-16-le', 'utf-32'):
            payload = (
                '<?xml version="1.0"?>\n'
                '<!DOCTYPE adm [<!ENTITY x "boom">]>\n'
                '<adm>&x;</adm>'
            ).encode(encoding)
            assert contains_xml_doctype(payload), encoding
        clean = '<?xml version="1.0"?><adm/>'.encode('utf-16')
        assert not contains_xml_doctype(clean)

    def test_bomless_utf16_doctype_is_rejected(self) -> None:
        payload = (
            '<?xml version="1.0" encoding="utf-16"?>\n'
            '<!DOCTYPE adm [<!ENTITY x "boom">]>\n'
            '<adm>&x;</adm>'
        ).encode('utf-16-le')
        assert payload.startswith(b'<\x00')
        assert contains_xml_doctype(payload)


class TestBoundedUserFileReads:
    def test_read_file_bounded_preflights_size(self, tmp_path: Path) -> None:
        big = tmp_path / 'big.bin'
        big.write_bytes(b'x' * 1024)
        with pytest.raises(IngressTooLargeError, match='limit 512'):
            read_file_bounded(big, 512)
        assert read_file_bounded(big, 1024) == b'x' * 1024

    def test_sofa_loader_rejects_oversized_before_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # h5py is lazily imported inside the loader; a stub module lets the
        # call reach the bounded read without the heavy dependency.
        import htdt.cad_spatial_reproduction as reproduction

        fake_h5py = types.ModuleType('h5py')
        monkeypatch.setitem(sys.modules, 'h5py', fake_h5py)
        monkeypatch.setattr(reproduction, 'MAX_ATTACHMENT_BYTES', 64)

        sofa = tmp_path / 'huge.sofa'
        sofa.write_bytes(b'x' * 4096)
        with pytest.raises(IngressTooLargeError, match='limit 64'):
            reproduction.load_sofa_dataset_profile(
                sofa,
                profile_id='profile',
                profile_version='v1',
                personalization_scope='generic',
                license_kind='cc0_public',
                created_at_utc='2026-01-01T00:00:00+00:00',
            )
