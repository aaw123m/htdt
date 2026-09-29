"""Regression coverage for the round-1 security & robustness review.

Each test pins the behavior of one verified fix: bounded reads of
attacker-declared archive sizes, receiver hardening (integer headers,
delivery-id replay conflicts, per-route byte ceilings, socket timeouts),
GLB node-DAG expansion limits, and bounded config/descriptor parsing.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import socket
import ssl
import struct
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_camilladsp import CamillaDSPError, load_camilladsp_config
from htdt.cad_repository import SceneRepository
from htdt.capture_bundle import CaptureBundleError, ZipSource
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_inbox import CaptureInboxRepository
from htdt.capture_receiver import (
    CaptureReceiverService,
    RECEIVER_SOCKET_TIMEOUT_SECONDS,
    _default_bundle_reader,
)
from htdt.launch_intents import (
    MAX_INTENT_DESCRIPTOR_BYTES,
    drain_launch_intents,
    intents_dir,
)
from htdt.raw_mesh import RawMeshImportError, import_raw_visual_mesh


def _rig(tmp_path: Path, **service_kwargs):
    """Receiver service over real repositories (defaults preserved)."""
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    service = CaptureReceiverService(
        scene,
        inbox,
        ingestion,
        data_dir=tmp_path / 'receiver',
        **service_kwargs,
    )
    return ingestion, inbox, service


def _delivery_headers(
    archive: bytes,
    *,
    artifact_kind: str = 'capture_bundle',
    delivery_id: str | None = None,
    archive_bytes: str | None = None,
) -> dict:
    headers = {
        'Content-Type': 'application/vnd.htdt.capture-bundle',
        'X-HTDT-Artifact-Kind': artifact_kind,
        'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
        'X-HTDT-Archive-Bytes': (
            archive_bytes if archive_bytes is not None else str(len(archive))
        ),
    }
    if delivery_id:
        headers['X-HTDT-Delivery-ID'] = delivery_id
    return headers


def _active_service(tmp_path: Path, **service_kwargs):
    _ingestion, _inbox, service = _rig(tmp_path, **service_kwargs)
    pairing, _payload = service.begin_pairing()
    service.confirm_pairing(pairing.pairing_id)
    return service, pairing


def _zip_bundle_dir(bundle_dir: Path, dest: Path) -> bytes:
    """Wrap a validated bundle directory as a .htdtcapture ZIP."""
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(bundle_dir.rglob('*')):
            if member.is_file():
                archive.write(
                    member, member.relative_to(bundle_dir).as_posix()
                )
    return dest.read_bytes()


class TestZipSourceBoundedRead:
    def test_lying_declared_size_is_rejected(self, tmp_path):
        # Forge a member whose central-directory file_size under-reports
        # the actual deflate stream — the declared header can lie.
        payload = b'A' * (1 << 21)  # ~2 MiB of highly compressible data
        honest = tmp_path / 'honest.zip'
        with zipfile.ZipFile(honest, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('data.bin', payload)
        blob = bytearray(honest.read_bytes())
        central = blob.find(b'PK\x01\x02')
        assert central > 0
        # CD entry: uncompressed size field at offset 24
        struct.pack_into('<I', blob, central + 24, 1024)
        forged = tmp_path / 'forged.zip'
        forged.write_bytes(bytes(blob))

        source = ZipSource(forged)
        try:
            assert source.list_files() == ['data.bin']
            # The member must be rejected — either the declared/actual
            # length check or the integrity check fires. It must never be
            # served as valid bytes.
            with pytest.raises(
                CaptureBundleError,
                match='unreadable archive member|expanded length mismatch',
            ):
                source.read_bytes('data.bin')
        finally:
            source.zf.close()

    def test_honest_member_round_trips(self, tmp_path):
        payload = b'not compressible enough to matter' * 64
        honest = tmp_path / 'honest.zip'
        with zipfile.ZipFile(honest, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('dir/data.bin', payload)
        source = ZipSource(honest)
        try:
            assert source.list_files() == ['dir/data.bin']
            assert source.read_bytes('dir/data.bin') == payload
        finally:
            source.zf.close()


class TestDefaultBundleReader:
    def test_zip_wrapper_produces_plan_and_payloads(self, tmp_path):
        plan, payloads, manifest = support.plan_and_payloads(tmp_path)
        archive_bytes = _zip_bundle_dir(
            tmp_path / 'bundle', tmp_path / 'bundle.htdtcapture'
        )
        read_plan, read_payloads, read_manifest = _default_bundle_reader(
            archive_bytes
        )
        assert read_plan == plan
        assert read_payloads == payloads
        assert read_manifest == manifest
        assert sha256(manifest).hexdigest() == read_plan['bundle']['bundle_digest']

    def test_non_zip_payload_rejected_cleanly(self):
        with pytest.raises(CaptureBundleError, match='invalid ZIP'):
            _default_bundle_reader(b'not a zip')


class TestReceiverDeliveryHardening:
    def test_non_integer_archive_bytes_header_rejected(self, tmp_path):
        service, pairing = _active_service(tmp_path)
        archive = b'archive-bytes'
        headers = _delivery_headers(archive, archive_bytes='abc')
        status, body = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 400
        assert body['ingestion_outcome'] == 'rejected'

    def test_delivery_id_replay_with_different_bytes_conflicts(
        self, tmp_path
    ):
        service, pairing = _active_service(
            tmp_path, bundle_reader=_rejecting_reader
        )
        first = b'archive-one'
        headers = _delivery_headers(first, delivery_id='delivery-1')
        status, _ = service.handle_delivery(
            pairing.pairing_token, headers, first
        )
        assert status == 400  # reader rejects; rejection is recorded

        second = b'archive-two'
        headers = _delivery_headers(second, delivery_id='delivery-1')
        status, body = service.handle_delivery(
            pairing.pairing_token, headers, second
        )
        assert status == 409
        assert 'different bytes' in body['detail']

    def test_delivery_id_replay_with_different_kind_conflicts(
        self, tmp_path
    ):
        service, pairing = _active_service(
            tmp_path, bundle_reader=_rejecting_reader
        )
        first = b'archive-one'
        headers = _delivery_headers(first, delivery_id='delivery-2')
        status, _ = service.handle_delivery(
            pairing.pairing_token, headers, first
        )
        assert status == 400

        headers = _delivery_headers(
            first, artifact_kind='other_kind', delivery_id='delivery-2'
        )
        status, body = service.handle_delivery(
            pairing.pairing_token, headers, first
        )
        assert status == 409

    def test_handler_timeout_is_bounded(self):
        assert RECEIVER_SOCKET_TIMEOUT_SECONDS > 0


def _rejecting_reader(_body: bytes):
    raise ValueError('unreadable archive')


class TestReceiverSocketCeilings:
    def _request_raw(self, port: int, request: bytes) -> bytes:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', port), timeout=10) as raw:
            with context.wrap_socket(
                raw, server_hostname='127.0.0.1'
            ) as tls:
                tls.sendall(request)
                tls.settimeout(10)
                return tls.recv(4096)

    def test_malformed_content_length_returns_400(self, tmp_path):
        _i, _b, service = _rig(tmp_path)
        port = service.start(host='127.0.0.1', port=0)
        try:
            response = self._request_raw(
                port,
                b'POST /htdt-capture/v1/token/deliveries HTTP/1.1\r\n'
                b'Host: 127.0.0.1\r\n'
                b'Content-Length: abc\r\n'
                b'Connection: close\r\n\r\n',
            )
            assert b' 400 ' in response.split(b'\r\n', 1)[0]
        finally:
            service.stop()

    def test_receipt_body_ceiling_is_tighter_than_delivery(self, tmp_path):
        _i, _b, service = _rig(tmp_path)
        port = service.start(host='127.0.0.1', port=0)
        try:
            # 2 MiB > RECEIVER_MAX_RECEIPT_BYTES (1 MiB) but << the 2 GiB
            # archive ceiling — a receipt must not share the archive bound.
            declared = str(2 * 1024 * 1024).encode()
            response = self._request_raw(
                port,
                b'POST /htdt-capture/v1/token/missions/pkg/receipt '
                b'HTTP/1.1\r\n'
                b'Host: 127.0.0.1\r\n'
                + b'Content-Length: ' + declared + b'\r\n'
                b'Connection: close\r\n\r\n',
            )
            assert b' 413 ' in response.split(b'\r\n', 1)[0]
        finally:
            service.stop()


class TestGlbExpansionBounds:
    def _glb(self, nodes: list[dict]) -> bytes:
        document = {
            'asset': {'version': '2.0'},
            'meshes': [{'primitives': []}],
            'nodes': nodes,
            'scenes': [{'nodes': [0]}],
            'scene': 0,
        }
        json_bytes = json.dumps(
            document, separators=(',', ':')
        ).encode('utf-8')
        json_bytes += b' ' * ((-len(json_bytes)) % 4)
        total = 12 + 8 + len(json_bytes)
        return (
            struct.pack('<4sII', b'glTF', 2, total)
            + struct.pack('<II', len(json_bytes), 0x4E4F534A)
            + json_bytes
        )

    def test_node_dag_expansion_budget_rejects_diamond(self):
        # 60 nodes where every node lists the next node twice: the DAG is
        # acyclic but has 2^59 distinct root-to-leaf expansion paths —
        # path-local cycle detection permits it, so a global expansion
        # budget must stop the blow-up.
        depth = 60
        nodes = [
            {'children': [index + 1, index + 1]}
            for index in range(depth - 1)
        ] + [{}]
        with pytest.raises(RawMeshImportError, match='instancing bound'):
            import_raw_visual_mesh(
                self._glb(nodes), source_name='dag.glb'
            )

    def test_deep_node_chain_maps_recursion_to_import_error(self):
        depth = 5000
        nodes = [
            {'children': [index + 1]} for index in range(depth - 1)
        ] + [{}]
        with pytest.raises(RawMeshImportError, match='too deep'):
            import_raw_visual_mesh(
                self._glb(nodes), source_name='deep.glb'
            )


class TestMeshVertexBound:
    def test_vertex_count_cap_rejects_oversized_mesh(
        self, tmp_path, monkeypatch
    ):
        import htdt.raw_mesh as raw_mesh

        monkeypatch.setattr(raw_mesh, 'MAX_RAW_MESH_VERTICES', 2)
        asset = b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
        with pytest.raises(RawMeshImportError, match='more than 2 vertices'):
            import_raw_visual_mesh(asset, source_name='tri.obj')


class TestLaunchIntentBound:
    def test_oversized_queued_intent_goes_to_dead(self, tmp_path):
        incoming = intents_dir(tmp_path) / 'incoming'
        incoming.mkdir(parents=True)
        oversized = incoming / 'big.json'
        oversized.write_bytes(b' ' * (MAX_INTENT_DESCRIPTOR_BYTES + 8))

        assert drain_launch_intents(tmp_path) == ()
        dead = intents_dir(tmp_path) / 'dead' / 'big.json'
        assert dead.exists()
        assert not oversized.exists()


class TestCamillaDSPBounds:
    def test_oversized_config_rejected(self):
        with pytest.raises(CamillaDSPError, match='config_too_large'):
            load_camilladsp_config(b' ' * (8 * 1024 * 1024 + 1))

    def test_deep_json_nesting_maps_to_config_error(self):
        depth = 2000
        text = '[' * depth + ']' * depth
        with pytest.raises(CamillaDSPError, match='malformed_config'):
            load_camilladsp_config(text.encode('utf-8'))

    def test_deep_yaml_nesting_maps_to_config_error(self):
        pytest.importorskip('yaml')
        text = '- ' * 1500
        with pytest.raises(CamillaDSPError, match='malformed_config'):
            load_camilladsp_config(text.encode('utf-8'))
