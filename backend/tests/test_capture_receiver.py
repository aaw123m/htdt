"""Native Capture receiver + pairing contract (issue #593)."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import ssl
import tempfile
import threading
import urllib.request
import uuid

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from htdt.capture_inbox import (
    CAPTURE_INBOX_UNASSIGNED_SCOPE,
    CaptureInboxRepository,
)
from htdt.capture_receiver import (
    CaptureReceiverError,
    CaptureReceiverService,
    cert_pin_from_pem,
    generate_self_signed_cert,
    verification_code,
)


SERIES_ID = '10000000-0000-4000-8000-000000000001'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'


def _plan_and_payloads(
    *,
    revision_id: str | None = None,
    created_at: str = '2026-09-20T00:00:00Z',
) -> tuple[dict, dict[str, bytes]]:
    """Stage a contract-valid bundle: the four foundation payloads plus one
    mesh anchor, with the caller's revision identity."""
    revision_id = revision_id or str(uuid.uuid4())
    geometry_path = f'mesh/geometry/{ANCHOR_ID}.meshbin'
    plan, payloads, _manifest = support.plan_and_payloads(
        Path(tempfile.mkdtemp()),
        files=support.mesh_specs_files(
            ((ANCHOR_ID, geometry_path, 3, 1),),
            anchor_transform=(
                1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
            ),
            session_id=SESSION_ID,
            space_id=SPACE_ID,
        ),
        manifest_overrides={
            'capture_series_id': SERIES_ID,
            'capture_revision_id': revision_id,
            'parent_revision_id': None,
            'capture_session_ids': [SESSION_ID],
            'coordinate_space_ids': [SPACE_ID],
            'created_at': created_at,
            'finalized_at': created_at,
        },
        id_map={
            support.SESSION_ID: SESSION_ID,
            support.SPACE_ID: SPACE_ID,
        },
    )
    return plan, payloads


class _Reader:
    """Test bundle reader: maps archive bytes to a prebuilt plan."""

    def __init__(self) -> None:
        self.plans: dict[str, tuple[dict, dict]] = {}

    def register(self, archive: bytes, plan: dict, payloads: dict) -> None:
        self.plans[sha256(archive).hexdigest()] = (plan, payloads)

    def __call__(self, body: bytes):
        key = sha256(body).hexdigest()
        if key not in self.plans:
            raise ValueError('unreadable archive')
        return self.plans[key]


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    reader = _Reader()
    service = CaptureReceiverService(
        scene,
        inbox,
        ingestion,
        bundle_reader=reader,
        data_dir=tmp_path / 'receiver',
    )
    return ingestion, inbox, reader, service


def _delivery_headers(
    archive: bytes,
    *,
    revision_id: str | None = None,
    bundle_digest: str | None = None,
    delivery_id: str | None = None,
) -> dict:
    headers = {
        'Content-Type': 'application/vnd.htdt.capture-bundle',
        'X-HTDT-Artifact-Kind': 'capture_bundle',
        'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
        'X-HTDT-Archive-Bytes': str(len(archive)),
    }
    if revision_id:
        headers['X-HTDT-Capture-Revision-ID'] = revision_id
    if bundle_digest:
        headers['X-HTDT-Bundle-Digest'] = bundle_digest
    if delivery_id:
        headers['X-HTDT-Delivery-ID'] = delivery_id
    return headers


class TestPairing:
    def test_begin_pairing_emits_wire_payload(self, tmp_path):
        _ingestion, _inbox, _reader, service = _rig(tmp_path)
        pairing, payload = service.begin_pairing(project_ref='doc-1')
        assert pairing.state == 'offered'
        assert pairing.confirmation_code == verification_code(
            payload.receiver_instance_id,
            pairing.pairing_token,
            payload.pinned_identity,
        )
        wire = payload.model_dump(mode='json', by_alias=True)
        assert wire['schema'] == 'htdt.receiver-pairing'
        assert wire['schema_version'] == '1.0.0'
        assert wire['endpoint_url'].startswith('https://')
        assert wire['endpoint_url'].endswith('/deliveries')
        assert wire['missions_endpoint_url'].endswith('/missions')
        assert wire['pinned_identity'].startswith('sha256:')
        assert wire['pairing_token'] == pairing.pairing_token
        assert wire['project_ref'] == 'doc-1'
        assert len(pairing.confirmation_code) == 9  # XXXX-XXXX

    def test_confirmation_code_matches_capture_formula(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        pairing, payload = service.begin_pairing()
        material = (
            f'{payload.receiver_instance_id}'
            f'|{payload.pairing_token}'
            f'|{payload.pinned_identity}'
        )
        hex8 = sha256(material.encode()).hexdigest()[:8].upper()
        assert pairing.confirmation_code == f'{hex8[:4]}-{hex8[4:]}'

    def test_delivery_rejected_until_confirmed(self, tmp_path):
        _i, inbox, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        plan, payloads = _plan_and_payloads()
        archive = b'archive-bytes-1'
        reader.register(archive, plan, payloads)
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _delivery_headers(archive),
            archive,
        )
        assert status == 404

    def test_expired_offer_cannot_confirm(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing(ttl_minutes=-1)
        with pytest.raises(CaptureReceiverError, match='expired'):
            service.confirm_pairing(pairing.pairing_id)
        assert service.get_pairing(pairing.pairing_id).state == 'expired'

    def test_revoked_pairing_stops_serving(self, tmp_path):
        _i, _b, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        service.confirm_pairing(pairing.pairing_id)
        service.revoke_pairing(pairing.pairing_id)
        assert service.capabilities_document(pairing.pairing_token) is None
        plan, payloads = _plan_and_payloads()
        archive = b'archive-bytes-2'
        reader.register(archive, plan, payloads)
        status, _receipt = service.handle_delivery(
            pairing.pairing_token, _delivery_headers(archive), archive
        )
        assert status == 404


class TestCapabilities:
    def test_capability_document_shape(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing(project_ref='doc-9')
        document = service.capabilities_document(pairing.pairing_token)
        assert document['schema'] == 'htdt.endpoint-capabilities'
        assert document['schema_version'] == '1.0.0'
        assert document['handoff_protocol_versions'] == ['1']
        assert document['accepted_bundle_schema_versions'] == ['1.0.0']
        assert document['mission_receipts_supported'] is True
        assert document['mission_packages_served'] is True
        assert document['project_ref'] == 'doc-9'
        assert document['manual_review_required'] is True
        kinds = document['accepted_artifact_kinds']
        assert kinds[0]['artifact_kind'] == 'capture_bundle'

    def test_capabilities_served_for_offered_pairing(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        assert service.capabilities_document(pairing.pairing_token) is not None


class TestDeliveries:
    def _active(self, tmp_path):
        ingestion, inbox, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing(project_ref='doc-1')
        service.confirm_pairing(pairing.pairing_id)
        return ingestion, inbox, reader, service, pairing

    def test_delivery_ingests_and_stages_with_receipt(self, tmp_path):
        ingestion, inbox, reader, service, pairing = self._active(tmp_path)
        plan, payloads = _plan_and_payloads()
        typed = CaptureIngestionPlan.model_validate(plan)
        archive = b'archive-1'
        reader.register(archive, plan, payloads)
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _delivery_headers(
                archive,
                revision_id=typed.bundle.capture_revision_id,
                bundle_digest=typed.bundle.bundle_digest,
                delivery_id='delivery-1',
            ),
            archive,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'
        assert receipt['capture_revision_id'] == typed.bundle.capture_revision_id
        assert receipt['bundle_digest'] == typed.bundle.bundle_digest
        assert receipt['artifact_kind'] == 'capture_bundle'
        assert receipt['staging_ref'].startswith('capture-inbox-item:')
        item = inbox.get(typed.lineage_digest)
        assert item is not None
        assert item.scope == 'doc-1'  # project routing from the pairing
        assert item.arrival_source == 'paired_receiver'

    def test_unassigned_delivery_goes_to_global_inbox(self, tmp_path):
        ingestion, inbox, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()  # no project_ref
        service.confirm_pairing(pairing.pairing_id)
        plan, payloads = _plan_and_payloads()
        archive = b'archive-global'
        reader.register(archive, plan, payloads)
        status, receipt = service.handle_delivery(
            pairing.pairing_token, _delivery_headers(archive), archive
        )
        assert status == 200
        item = inbox.get(CaptureIngestionPlan.model_validate(plan).lineage_digest)
        assert item.scope == CAPTURE_INBOX_UNASSIGNED_SCOPE

    def test_idempotent_redelivery_replays_receipt(self, tmp_path):
        _i, _b, reader, service, pairing = self._active(tmp_path)
        plan, payloads = _plan_and_payloads()
        archive = b'archive-redeliver'
        reader.register(archive, plan, payloads)
        headers = _delivery_headers(archive, delivery_id='d-42')
        first = service.handle_delivery(pairing.pairing_token, headers, archive)
        second = service.handle_delivery(pairing.pairing_token, headers, archive)
        assert first[0] == 200 and second[0] == 200
        assert second[1]['ingestion_outcome'] == 'already_staged'
        assert second[1]['staging_ref'] == first[1]['staging_ref']

    def test_concurrent_same_delivery_replays_receipt(self, tmp_path):
        # Two simultaneous deliveries of one key both pass the dedup
        # read: the ledger must still record a single acceptance, the
        # listener must fire exactly once, and the loser must see the
        # same 'already_staged' replay receipt a sequential re-delivery
        # gets.
        _i, _b, reader, service, pairing = self._active(tmp_path)
        fired: list[str] = []
        service._delivery_listener = lambda record: fired.append(
            record.outcome
        )
        plan, payloads = _plan_and_payloads()
        archive = b'archive-race'
        reader.register(archive, plan, payloads)
        headers = _delivery_headers(archive, delivery_id='race-1')

        pre = threading.Barrier(2)
        post = threading.Barrier(2)
        real_stage = service.inbox_repository.stage

        def gated_stage(*args, **kwargs):
            pre.wait(30)
            result = real_stage(*args, **kwargs)
            post.wait(30)
            return result

        service.inbox_repository.stage = gated_stage
        try:
            results: list = [None, None]

            def deliver(index: int) -> None:
                results[index] = service.handle_delivery(
                    pairing.pairing_token, headers, archive
                )

            threads = [
                threading.Thread(target=deliver, args=(i,)) for i in (0, 1)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        finally:
            service.inbox_repository.stage = real_stage

        assert all(r is not None for r in results)
        assert [r[0] for r in results] == [200, 200]
        assert sorted(r[1]['ingestion_outcome'] for r in results) == [
            'accepted',
            'already_staged',
        ]
        assert results[0][1]['staging_ref'] == results[1][1]['staging_ref']
        assert fired == ['accepted']
        stored = service._get_delivery(f'{pairing.pairing_id}:race-1')
        assert stored is not None and stored.outcome == 'accepted'

    def test_digest_header_mismatch_rejected(self, tmp_path):
        _i, _b, _r, service, pairing = self._active(tmp_path)
        archive = b'archive-bad'
        headers = _delivery_headers(archive)
        headers['X-HTDT-Archive-SHA256'] = 'f' * 64
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'

    def test_byte_count_mismatch_rejected(self, tmp_path):
        _i, _b, _r, service, pairing = self._active(tmp_path)
        archive = b'archive-count'
        headers = _delivery_headers(archive)
        headers['X-HTDT-Archive-Bytes'] = str(len(archive) + 1)
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'

    def test_same_revision_different_digest_fails_closed(self, tmp_path):
        _i, _b, reader, service, pairing = self._active(tmp_path)
        revision = str(uuid.uuid4())
        plan1, payloads1 = _plan_and_payloads(revision_id=revision)
        plan2, payloads2 = _plan_and_payloads(
            revision_id=revision, created_at='2026-09-20T00:00:02Z'
        )
        assert (
            CaptureIngestionPlan.model_validate(plan2).bundle.bundle_digest
            != CaptureIngestionPlan.model_validate(plan1)
            .bundle.bundle_digest
        )
        archive1, archive2 = b'arc-1', b'arc-2'
        reader.register(archive1, plan1, payloads1)
        reader.register(archive2, plan2, payloads2)
        ok = service.handle_delivery(
            pairing.pairing_token,
            _delivery_headers(
                archive1,
                revision_id=revision,
                bundle_digest=CaptureIngestionPlan.model_validate(plan1)
                .bundle.bundle_digest,
            ),
            archive1,
        )
        assert ok[0] == 200
        conflict = service.handle_delivery(
            pairing.pairing_token,
            _delivery_headers(
                archive2,
                revision_id=revision,
                bundle_digest=CaptureIngestionPlan.model_validate(plan2)
                .bundle.bundle_digest,
            ),
            archive2,
        )
        assert conflict[0] == 400
        assert conflict[1]['ingestion_outcome'] == 'rejected'
        assert 'different bundle digest' in conflict[1]['detail']

    def test_unsupported_artifact_kind_rejected(self, tmp_path):
        _i, _b, _r, service, pairing = self._active(tmp_path)
        archive = b'x'
        headers = _delivery_headers(archive)
        headers['X-HTDT-Artifact-Kind'] = 'field_return'
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 415

    def test_oversize_declared_upload_rejected(self, tmp_path):
        _i, _b, _r, service, pairing = self._active(tmp_path)
        archive = b'small'
        headers = _delivery_headers(archive)
        headers['X-HTDT-Archive-Bytes'] = str(service.max_archive_bytes + 1)
        service.max_archive_bytes = len(archive)  # tighten for the test
        headers['X-HTDT-Archive-Bytes'] = str(len(archive))
        # body itself over the tightened ceiling
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive + b'x'
        )
        assert status in (400, 413)


class TestMissionPull:
    def _active(self, tmp_path):
        ingestion, inbox, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        service.confirm_pairing(pairing.pairing_id)
        return service, pairing

    def test_listing_package_download_receipt_cycle(self, tmp_path):
        service, pairing = self._active(tmp_path)
        package = service.queue_mission_package(
            'pkg-1',
            b'mission-package-bytes',
            descriptor={'mission_id': 'mission-1', 'purpose': 'recapture'},
        )
        status, listing = service.handle_mission_listing(
            pairing.pairing_token, 'capture-instance-9'
        )
        assert status == 200
        assert listing['schema'] == 'htdt.mission-listing'
        assert listing['capture_instance_id'] == 'capture-instance-9'
        assert listing['packages'][0]['package_id'] == 'pkg-1'
        assert (
            listing['packages'][0]['package_sha256']
            == f"sha256:{package.package_sha256}"
        )
        assert listing['packages'][0]['byte_size'] == len(
            b'mission-package-bytes'
        )
        # the same device identity now binds the pairing
        status, _ = service.handle_mission_listing(
            pairing.pairing_token, 'different-device'
        )
        assert status == 403

        status, body = service.handle_mission_package(
            pairing.pairing_token, 'pkg-1', 'capture-instance-9'
        )
        assert status == 200
        assert body == b'mission-package-bytes'

        receipt = {
            'schema': 'htdt.capture.mission-receipt',
            'schema_version': '1.0.0',
            'receipt_id': str(uuid.uuid4()),
            'package_id': 'pkg-1',
            'package_sha256': package.package_sha256,
            'capture_instance_id': 'capture-instance-9',
            'paired_destination_id': pairing.pairing_id,
            'receiver_instance_id': pairing.receiver_instance_id,
            'mission_record_id': None,
            'received_at': '2026-01-01T00:00:00+00:00',
            'validation_result': 'imported',
            'detail': None,
        }
        status, response = service.handle_mission_receipt(
            pairing.pairing_token,
            'pkg-1',
            json.dumps(receipt).encode(),
            'capture-instance-9',
        )
        assert status == 200
        stored = service.list_mission_packages()[0]
        assert stored.status == 'received'

    def test_failed_validation_marks_failed(self, tmp_path):
        service, pairing = self._active(tmp_path)
        package = service.queue_mission_package('pkg-2', b'bytes')
        receipt = {
            'schema': 'htdt.capture.mission-receipt',
            'schema_version': '1.0.0',
            'receipt_id': str(uuid.uuid4()),
            'package_id': 'pkg-2',
            'package_sha256': package.package_sha256,
            'capture_instance_id': 'dev-1',
            'paired_destination_id': pairing.pairing_id,
            'receiver_instance_id': pairing.receiver_instance_id,
            'received_at': '2026-01-01T00:00:00+00:00',
            'validation_result': 'integrity_mismatch',
            'detail': 'digest mismatch',
        }
        status, _ = service.handle_mission_receipt(
            pairing.pairing_token, 'pkg-2', json.dumps(receipt).encode(), 'dev-1'
        )
        assert status == 200
        assert service.list_mission_packages()[0].status == 'failed'

    def test_export_fallback_keeps_identity(self, tmp_path):
        service, _pairing = self._active(tmp_path)
        package = service.queue_mission_package('pkg-3', b'payload')
        out = service.export_mission_package(
            'pkg-3', tmp_path / 'export.pkg'
        )
        assert sha256(out.read_bytes()).hexdigest() == package.package_sha256


class TestHttpsEndpoint:
    def test_real_https_roundtrip(self, tmp_path):
        ingestion, inbox, reader, service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        service.confirm_pairing(pairing.pairing_id)
        port = service.start(host='127.0.0.1', port=0)
        try:
            base = f'https://127.0.0.1:{port}/htdt-capture/v1/{pairing.pairing_token}'
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            plan, payloads = _plan_and_payloads()
            archive = b'https-archive'
            reader.register(archive, plan, payloads)
            headers = {
                'Content-Type': 'application/vnd.htdt.capture-bundle',
                'X-HTDT-Artifact-Kind': 'capture_bundle',
                'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
                'X-HTDT-Archive-Bytes': str(len(archive)),
            }
            request = urllib.request.Request(
                f'{base}/deliveries',
                data=archive,
                headers=headers,
                method='POST',
            )
            with urllib.request.urlopen(request, context=context) as response:
                receipt = json.loads(response.read())
            assert receipt['ingestion_outcome'] == 'accepted'

            listing_request = urllib.request.Request(
                f'{base}/missions?capture_instance_id=dev-7',
                headers={'X-HTDT-Capture-Instance-ID': 'dev-7'},
            )
            with urllib.request.urlopen(
                listing_request, context=context
            ) as response:
                listing = json.loads(response.read())
            assert listing['schema'] == 'htdt.mission-listing'
            assert listing['capture_instance_id'] == 'dev-7'
            assert listing['packages'] == []
        finally:
            service.stop()
        assert not service.running
        assert not service.get_config().enabled

    def test_unpaired_route_is_404(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        port = service.start(host='127.0.0.1', port=0)
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            request = urllib.request.Request(
                f'https://127.0.0.1:{port}/htdt-capture/v1/nope/deliveries',
                data=b'x',
                method='POST',
            )
            try:
                urllib.request.urlopen(request, context=context)
                assert False, 'expected 404'
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
        finally:
            service.stop()

    def test_cert_pin_is_stable_der_sha256(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        config = service.get_config()
        # pin is derived from the persisted certificate DER
        cert_path = tmp_path / 'receiver' / 'receiver-cert.pem'
        der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
        expected = 'sha256:' + sha256(der).hexdigest()
        assert config.pinned_identity == expected
        assert cert_pin_from_pem(cert_path.read_bytes()) == expected
        # persists across config reloads
        service2 = CaptureReceiverService(
            service.scene_repository,
            data_dir=tmp_path / 'receiver',
        )
        assert service2.get_config().pinned_identity == expected
        assert (
            service2.get_config().receiver_instance_id
            == config.receiver_instance_id
        )

    def test_config_pin_reconciles_after_silent_cert_regeneration(
        self, tmp_path
    ):
        _i, _b, _r, service = _rig(tmp_path)
        config = service.get_config()
        cert_path = tmp_path / 'receiver' / 'receiver-cert.pem'
        key_path = tmp_path / 'receiver' / 'receiver-key.pem'
        stale_pin = config.pinned_identity
        cert_path.unlink()
        key_path.unlink()
        reconciled = service.get_config()
        live_pin = cert_pin_from_pem(cert_path.read_bytes())
        assert reconciled.pinned_identity == live_pin
        assert reconciled.pinned_identity != stale_pin
        assert (
            reconciled.receiver_instance_id == config.receiver_instance_id
        )

    def test_torn_cert_pair_regenerates_instead_of_loading(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        service.get_config()
        data_dir = tmp_path / 'receiver'
        staged = tmp_path / 'staged'
        staged.mkdir()
        generate_self_signed_cert(staged / 'c.pem', staged / 'k.pem')
        # splice an unrelated cert over the persisted key: the pair passes
        # the exists-guard but cannot load and must be regenerated
        (data_dir / 'receiver-cert.pem').write_bytes(
            (staged / 'c.pem').read_bytes()
        )
        cert_pem, _key_pem = service._ensure_certificate()
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            str(data_dir / 'receiver-cert.pem'),
            str(data_dir / 'receiver-key.pem'),
        )
        assert (
            service.get_config().pinned_identity
            == cert_pin_from_pem(cert_pem)
        )

    def test_concurrent_first_get_config_mints_one_identity(self, tmp_path):
        _i, _b, _r, service = _rig(tmp_path)
        barrier = threading.Barrier(6)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=15)
                results.append(service.get_config())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors
        assert len({c.receiver_instance_id for c in results}) == 1
        assert {c.pinned_identity for c in results} == {
            results[0].pinned_identity
        }
        assert results[0].pinned_identity == cert_pin_from_pem(
            (tmp_path / 'receiver' / 'receiver-cert.pem').read_bytes()
        )
