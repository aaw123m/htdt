"""Field-return delivery lane on the Capture receiver.

The app emits ``.htdtfieldreturn`` containers — stored-entry ZIPs with a
``container-manifest.json`` integrity index, a ``field-return.json`` root
document (``htdt.field_return``), typed authority docs and evidence
payloads. The lane stages them into ``field_return_contributions`` and
echoes artifact identity in the receipt the same way capture bundles do.
"""

from __future__ import annotations

import io
import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import uuid
import zipfile
import zlib

import pytest

from htdt.cad_repository import SceneRepository
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.capture_inbox import CaptureInboxRepository
from htdt.capture_receiver import CaptureReceiverService
from htdt.field_return_ingestion import FieldReturnRepository


CONTAINER_MANIFEST_PATH = 'container-manifest.json'
ROOT_PATH = 'field-return.json'


def _root_document(
    *,
    contribution_id: str | None = None,
    schema_version: str = '2.0.0',
    authority_documents: list[dict] | None = None,
    evidence_assets: list[dict] | None = None,
    **overrides,
) -> dict:
    document = {
        'schema': 'htdt.field_return',
        'schema_version': schema_version,
        'authority_binding_scope': 'contribution_ref',
        'contribution_id': contribution_id or str(uuid.uuid4()),
        'contribution_ref': {
            'kind': 'field_return',
            'id': contribution_id or str(uuid.uuid4()),
        },
        'mission_id': None,
        'plan_id': None,
        'plan_version': None,
        'plan_sha256': None,
        'created_at': '2026-10-01T00:00:00Z',
        'finalized_at': '2026-10-01T00:01:00Z',
        'provenance': {
            'producer': 'field_return_finalizer',
            'app_name': 'HTDT Capture',
            'app_version': '1.0',
            'device_model_family': 'iPhone',
        },
        'related_capture_revision_ids': [],
        'task_fulfillment_ledger': [],
        'authority_documents': authority_documents or [],
        'evidence_assets': evidence_assets or [],
        'content_digest': sha256(b'fixture-content').hexdigest(),
    }
    document.update(overrides)
    return document


def _doc_ref(path: str, schema: str, payload: bytes) -> dict:
    return {
        'path': path,
        'schema': schema,
        'sha256': sha256(payload).hexdigest(),
        'bytes': len(payload),
    }


def _container(entries: dict[str, bytes]) -> bytes:
    """Stored-entry ZIP matching HTDTFieldReturnArchiveWriter's contract."""
    manifest = {
        'schema': 'htdt.field_return.container_manifest',
        'schema_version': '1.0.0',
        'entries': [
            {
                'path': path,
                'bytes': len(data),
                'crc32': zlib.crc32(data) & 0xFFFFFFFF,
                'sha256': sha256(data).hexdigest(),
            }
            for path, data in entries.items()
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
        archive.writestr(
            CONTAINER_MANIFEST_PATH, json.dumps(manifest).encode('utf-8')
        )
    return buffer.getvalue()


def _field_return_archive(
    *,
    contribution_id: str | None = None,
    schema_version: str = '2.0.0',
    include_authority: bool = True,
) -> tuple[bytes, dict]:
    cid = contribution_id or str(uuid.uuid4())
    extras: dict[str, bytes] = {}
    authority_documents: list[dict] = []
    evidence_assets: list[dict] = []
    if include_authority:
        evidence_payload = b'evidence-bytes-' + cid.encode()[:8]
        extras[f'evidence/{cid}.bin'] = evidence_payload
        evidence_assets = [
            _doc_ref(
                f'evidence/{cid}.bin', 'application/octet-stream',
                evidence_payload,
            )
        ]
        authority_payload = json.dumps(
            {
                'schema': 'htdt.field_return.field-evidence',
                'contribution_ref': {'kind': 'field_return', 'id': cid},
                'records': [],
            }
        ).encode('utf-8')
        extras['authority/field-evidence.json'] = authority_payload
        authority_documents = [
            _doc_ref(
                'authority/field-evidence.json',
                'htdt.field_return.field-evidence',
                authority_payload,
            )
        ]
    document = _root_document(
        contribution_id=cid,
        schema_version=schema_version,
        authority_documents=authority_documents,
        evidence_assets=evidence_assets,
    )
    entries = {ROOT_PATH: json.dumps(document).encode('utf-8'), **extras}
    return _container(entries), document


def _rig(tmp_path: Path):
    scene = SceneRepository(tmp_path / 'cad.sqlite3')
    ingestion = CaptureIngestionRepository(scene)
    inbox = CaptureInboxRepository(scene, ingestion)
    service = CaptureReceiverService(
        scene,
        inbox,
        ingestion,
        bundle_reader=lambda body: None,
        data_dir=tmp_path / 'receiver',
    )
    return service


def _active_pairing(service: CaptureReceiverService):
    pairing, _payload = service.begin_pairing()
    service.confirm_pairing(pairing.pairing_id)
    return pairing


def _headers(
    archive: bytes,
    *,
    contribution_id: str,
    artifact_digest: str,
    delivery_id: str | None = None,
) -> dict:
    headers = {
        'Content-Type': 'application/vnd.htdt.field-return',
        'X-HTDT-Artifact-Kind': 'field_return',
        'X-HTDT-Artifact-ID': contribution_id,
        'X-HTDT-Artifact-Digest': artifact_digest,
        'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
        'X-HTDT-Archive-Bytes': str(len(archive)),
    }
    if delivery_id:
        headers['X-HTDT-Delivery-ID'] = delivery_id
    return headers


class TestFieldReturnDelivery:
    def test_capabilities_advertise_field_return(self, tmp_path):
        service = _rig(tmp_path)
        pairing, _payload = service.begin_pairing()
        document = service.capabilities_document(pairing.pairing_token)
        kinds = {
            entry['artifact_kind']: entry
            for entry in document['accepted_artifact_kinds']
        }
        assert 'field_return' in kinds
        assert kinds['field_return']['accepted_schema_versions'] == [
            '1.0.0',
            '2.0.0',
        ]

    def test_validated_container_stages_and_receipt_echoes(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest=document['content_digest'],
                delivery_id='delivery-1',
            ),
            archive,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'
        assert receipt['artifact_kind'] == 'field_return'
        assert receipt['artifact_id'] == document['contribution_id']
        assert receipt['artifact_digest'] == document['content_digest']
        assert receipt['staging_ref'] == (
            f"field-return:{document['contribution_id']}"
        )
        staged = FieldReturnRepository(service.path).get(
            document['contribution_id']
        )
        assert staged is not None
        assert staged.validation_state == 'validated'
        assert staged.artifact_sha256 == sha256(archive).hexdigest()

    def test_redelivery_replays_already_staged(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        headers = _headers(
            archive,
            contribution_id=document['contribution_id'],
            artifact_digest=document['content_digest'],
            delivery_id='delivery-9',
        )
        first_status, _ = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert first_status == 200
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'already_staged'

    def test_same_contribution_without_delivery_id_dedupes(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        headers = _headers(
            archive,
            contribution_id=document['contribution_id'],
            artifact_digest=document['content_digest'],
        )
        service.handle_delivery(pairing.pairing_token, headers, archive)
        status, receipt = service.handle_delivery(
            pairing.pairing_token, headers, archive
        )
        assert status == 200
        # a distinct delivery key but an already-staged contribution is
        # recorded as already_staged, not re-accepted
        assert receipt['ingestion_outcome'] == 'already_staged'

    def test_delivery_id_replay_with_other_bytes_conflicts(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        headers = _headers(
            archive,
            contribution_id=document['contribution_id'],
            artifact_digest=document['content_digest'],
            delivery_id='delivery-fixed',
        )
        service.handle_delivery(pairing.pairing_token, headers, archive)
        other, _other_document = _field_return_archive()
        other_headers = _headers(
            other,
            contribution_id=document['contribution_id'],
            artifact_digest=document['content_digest'],
            delivery_id='delivery-fixed',
        )
        status, receipt = service.handle_delivery(
            pairing.pairing_token, other_headers, other
        )
        assert status == 409
        assert receipt['ingestion_outcome'] == 'rejected'
        # Full identity echoes: the sender's receipt validation must be
        # able to classify this as the dedup conflict it is, not a
        # payload-mutation failure.
        assert receipt['artifact_id'] == document['contribution_id']
        assert receipt['artifact_digest'] == document['content_digest']
        assert 'conflict' in (receipt['detail'] or '') or (
            'replayed' in (receipt['detail'] or '')
        )

    def test_declared_digest_must_match_container_content(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest='0' * 64,  # divergent semantic digest
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        assert 'digest' in (receipt['detail'] or '')

    def test_empty_schema_evidence_asset_stages(self, tmp_path):
        # Containers written before the media-type token landed carry
        # `schema: ""` on evidence refs — they must still stage.
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        cid = str(uuid.uuid4())
        payload = b'evidence-bytes-emptysch'
        document = _root_document(
            contribution_id=cid,
            evidence_assets=[_doc_ref(f'evidence/{cid}.bin', '', payload)],
        )
        entries = {
            ROOT_PATH: json.dumps(document).encode('utf-8'),
            f'evidence/{cid}.bin': payload,
        }
        archive = _container(entries)
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=cid,
                artifact_digest=document['content_digest'],
            ),
            archive,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'

    def test_empty_schema_authority_document_still_rejected(self, tmp_path):
        # Authority refs keep the strict schema contract — only evidence
        # assets tolerate the legacy empty token.
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        cid = str(uuid.uuid4())
        payload = b'{}'
        document = _root_document(
            contribution_id=cid,
            authority_documents=[_doc_ref('authority/a.json', '', payload)],
        )
        archive = _container(
            {
                ROOT_PATH: json.dumps(document).encode('utf-8'),
                'authority/a.json': payload,
            }
        )
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=cid,
                artifact_digest=document['content_digest'],
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'

    def test_malformed_container_rejected_and_staged(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive = b'PK\x03\x04' + b'not-a-zip' * 16
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=str(uuid.uuid4()),
                artifact_digest=sha256(b'x').hexdigest(),
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'

    def test_tampered_container_manifest_rejected(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        entries = {ROOT_PATH: json.dumps(_root_document()).encode('utf-8')}
        # manifest declares bytes that do not match the real entry
        manifest = {
            'schema': 'htdt.field_return.container_manifest',
            'schema_version': '1.0.0',
            'entries': [
                {
                    'path': ROOT_PATH,
                    'bytes': len(entries[ROOT_PATH]),
                    'crc32': zlib.crc32(entries[ROOT_PATH]) & 0xFFFFFFFF,
                    'sha256': '0' * 64,
                }
            ],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as z:
            z.writestr(ROOT_PATH, entries[ROOT_PATH])
            z.writestr(
                CONTAINER_MANIFEST_PATH,
                json.dumps(manifest).encode('utf-8'),
            )
        archive = buffer.getvalue()
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=str(uuid.uuid4()),
                artifact_digest='0' * 64,
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        assert 'manifest mismatch' in (receipt['detail'] or '')

    def test_unsupported_schema_version_stages_for_diagnosis(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive(schema_version='3.0.0')
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest=document['content_digest'],
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        staged = FieldReturnRepository(service.path).get(
            document['contribution_id']
        )
        assert staged is not None
        assert staged.validation_state == 'unsupported'
        assert '3.0.0' in (staged.detail or '')

    def test_artifact_id_header_must_match_contribution(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=str(uuid.uuid4()),  # wrong id on purpose
                artifact_digest=document['content_digest'],
            ),
            archive,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        assert 'artifact id' in (receipt['detail'] or '').lower()

    def test_same_contribution_different_bytes_is_a_conflict(
        self, tmp_path
    ):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest=document['content_digest'],
            ),
            archive,
        )
        # same contribution id, different artifact bytes — keep every
        # referenced payload intact so the divergence is byte-level, not a
        # missing-entry rejection
        tampered, tampered_document = _field_return_archive(
            contribution_id=document['contribution_id']
        )
        tampered_document['content_digest'] = sha256(b'other').hexdigest()
        cid = document['contribution_id']
        tampered_entries = {
            ROOT_PATH: json.dumps(tampered_document).encode('utf-8'),
            f'evidence/{cid}.bin': b'evidence-bytes-' + cid.encode()[:8],
            'authority/field-evidence.json': json.dumps(
                {
                    'schema': 'htdt.field_return.field-evidence',
                    'contribution_ref': {
                        'kind': 'field_return',
                        'id': cid,
                    },
                    'records': [],
                }
            ).encode('utf-8'),
        }
        tampered = _container(tampered_entries)
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                tampered,
                contribution_id=document['contribution_id'],
                artifact_digest=tampered_document['content_digest'],
            ),
            tampered,
        )
        assert status == 400
        assert receipt['ingestion_outcome'] == 'rejected'
        assert 'conflict' in (receipt['detail'] or '').lower()

    def test_flat_manifest_form_still_stages(self, tmp_path):
        """The flat `htdt.field-return` manifest form remains stageable."""
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        contribution_id = str(uuid.uuid4())
        flat = json.dumps(
            {
                'schema': 'htdt.field-return',
                'schema_version': 1,
                'contribution_id': contribution_id,
                'authority_binding_scope': 'contribution_id',
                'records': [],
                'task_outcomes': [],
            }
        ).encode('utf-8')
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            {
                'Content-Type': 'application/vnd.htdt.field-return',
                'X-HTDT-Artifact-Kind': 'field_return',
                'X-HTDT-Artifact-ID': contribution_id,
                'X-HTDT-Artifact-Digest': sha256(flat).hexdigest(),
                'X-HTDT-Archive-SHA256': sha256(flat).hexdigest(),
                'X-HTDT-Archive-Bytes': str(len(flat)),
            },
            flat,
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'
        staged = FieldReturnRepository(service.path).get(contribution_id)
        assert staged is not None
        assert staged.validation_state == 'validated'

    def test_container_routes_by_pairing_project_scope(self, tmp_path):
        """A container with no project ref routes via the pairing's scope."""
        service = _rig(tmp_path)
        project_id = str(uuid.uuid4())
        with sqlite3.connect(service.path) as connection:
            connection.execute(
                'INSERT INTO htdt_project_documents('
                'project_id, document_id, display_name, description,'
                ' created_at_utc, updated_at_utc, archived'
                ") VALUES (?, ?, 'scoped project', NULL, 'now', 'now', 0)",
                (project_id, str(uuid.uuid4())),
            )
        pairing, _payload = service.begin_pairing(project_ref=project_id)
        service.confirm_pairing(pairing.pairing_id)
        archive, document = _field_return_archive()
        status, receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest=document['content_digest'],
                delivery_id='delivery-scoped',
            ),
            archive,
        )
        assert status == 200
        staged = FieldReturnRepository(service.path).get(
            document['contribution_id']
        )
        assert staged is not None
        assert staged.routing == 'channel_project_match'
        assert staged.matched_project_id == project_id

    def test_unscoped_pairing_keeps_container_unrouted(self, tmp_path):
        service = _rig(tmp_path)
        pairing = _active_pairing(service)
        archive, document = _field_return_archive()
        status, _receipt = service.handle_delivery(
            pairing.pairing_token,
            _headers(
                archive,
                contribution_id=document['contribution_id'],
                artifact_digest=document['content_digest'],
                delivery_id='delivery-unscoped',
            ),
            archive,
        )
        assert status == 200
        staged = FieldReturnRepository(service.path).get(
            document['contribution_id']
        )
        assert staged is not None
        assert staged.routing == 'unrouted'
        assert staged.matched_project_id is None
