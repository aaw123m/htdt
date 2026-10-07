"""Native Capture receiver + pairing (issue #593).

The LAN receiver that HTDT-Capture's iOS handoff targets, owned by the
native app lifecycle:

- explicit pairing — the app issues a ``htdt.receiver-pairing`` payload
  (receiver URL + pinned TLS leaf identity + one-time token) shown as a
  QR on the Capture device; both sides display the same confirmation
  code derived from ``sha256(instance|token|pin)``;
- HTTPS only — a locally generated, durably persisted self-signed
  certificate whose SHA-256 the QR pins (pinning replaces CA trust for
  this receiver only); no cleartext HTTP, no cloud relay;
- capability handshake — the receiver advertises an
  ``htdt.endpoint-capabilities`` document before any bytes move;
- upload endpoint accepts the exact ``.htdtcapture`` archive bytes under
  the Capture header contract, verifies archive SHA / byte count / digest
  headers, runs the canonical ingest + Capture Inbox staging pipeline,
  and answers a deterministic ``HTDTIngestionResponse`` receipt;
- mission receive leg — serves queued Mission packages to paired Capture
  identities via the pull endpoints and records their
  ``htdt.capture.mission-receipt`` reports;
- offline safety — a paired delivery that cannot reach the endpoint is
  retryable: receipts are idempotent (``already_staged``) and packages
  are also exportable to file with unchanged identity;
- lifecycle — disabled by default; port rebinding is safe; shutdown is
  clean.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Literal, Mapping
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cad_repository import SceneRepository
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from .capture_inbox import (
    CAPTURE_INBOX_UNASSIGNED_SCOPE,
    PROMOTION_AUTHORITY_KINDS,
    CaptureInboxRepository,
)
from .capture_mission import (
    CaptureMissionPackage,
    mission_package_descriptor,
    mission_package_wire_payload,
)
from .field_return_ingestion import (
    FieldReturnConflictError,
    FieldReturnRepository,
    SUPPORTED_FIELD_RETURN_CONTAINER_VERSIONS,
    stage_field_return_artifact,
)
from .ingress import IngressTooLargeError, read_file_bounded
from .export_io import write_bytes_atomic
from .content_blobs import (
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)
from .limits import MAX_CAPTURE_INGEST_SOURCE_BYTES
from .canonical_json import canonical_json as _canonical_json
from .clock import utc_now_iso as _utc_now


_LOGGER = logging.getLogger('htdt.capture_receiver')


RECEIVER_DOMAIN = 'htdt.capture.receiver.v1'
PAIRING_SCHEMA = 'htdt.receiver-pairing'
PAIRING_SCHEMA_VERSION = '1.0.0'
CAPABILITIES_SCHEMA = 'htdt.endpoint-capabilities'
CAPABILITIES_SCHEMA_VERSION = '1.0.0'
MISSION_LISTING_SCHEMA = 'htdt.mission-listing'
MISSION_LISTING_SCHEMA_VERSION = '1.0.0'
MISSION_RECEIPT_SCHEMA = 'htdt.capture.mission-receipt'
MISSION_RECEIPT_SCHEMA_VERSION = '1.0.0'
HANDOFF_PROTOCOL_VERSION = '1'
BUNDLE_SCHEMA = 'htdt.capture.bundle'
BUNDLE_SCHEMA_VERSION = '1.0.0'

# Receiving a delivery means the bytes arrived — it never means the bundle
# passed validation. Promotion gates live in the Capture Inbox (#589).

RECEIVER_PATH_PREFIX = '/htdt-capture/v1'

# Artifact kinds ``handle_delivery`` admits end-to-end: capture bundles
# ingest through the plan pipeline into the Capture Inbox; field returns
# stage into ``field_return_contributions`` via FieldReturnRepository.
DELIVERABLE_KINDS = ('capture_bundle', 'field_return')

# Authority families an end-to-end promotion path can actually execute
# today through CaptureEntityPromotionService (the Capture Inbox promote
# executor): ``annotations`` materialize into real SceneEntity objects and
# ``measurements`` materialize into durable provenance bindings on the
# document's head revision. The remaining kinds stay staging-only until
# their production executors land.
# ``PROMOTION_AUTHORITY_KINDS`` stays the staging-level inventory (what the
# inbox classifies); this is the executable subset capabilities advertises.
EXECUTABLE_AUTHORITY_KINDS: tuple[str, ...] = (
    'annotations',
    'measurements',
)

# How many bytes a single upload may declare/be — same ingest ceiling the
# file-import path enforces, applied to the wire.
RECEIVER_MAX_ARCHIVE_BYTES = MAX_CAPTURE_INGEST_SOURCE_BYTES

# Mission receipts are small JSON verdicts; they do not share the archive
# ceiling.
RECEIVER_MAX_RECEIPT_BYTES = 1024 * 1024

# Per-operation socket timeout for request handler connections. Threading
# accept is unbounded per connection, so an idle or trickling client must
# not hold a handler thread open forever; each blocking socket operation
# gets this window, which no honest upload ever trips.
RECEIVER_SOCKET_TIMEOUT_SECONDS = 30.0

# PEM credentials minted by the receiver are a few KiB; a MiB ceiling is
# far beyond any honest cert/key pair yet still bounded.
TLS_CREDENTIAL_MAX_BYTES = 1024 * 1024

MISSION_PACKAGE_MAX_BYTES = 16 * 1024 * 1024
MISSION_LISTING_MAX_BYTES = 256 * 1024

# The iOS app's import contract for queued mission payloads. The queue
# guard below mirrors `HTDTMissionPackage`'s synthesized-Codable
# required keys and `HTDTCaptureTaskPlan`'s required decode keys so a
# payload the app can never import never enters the queue.
_APP_MISSION_SCHEMA_VERSIONS = ('1.0.0',)
_APP_PLAN_SCHEMA = 'htdt.capture-task-plan'
_APP_PLAN_SCHEMA_VERSIONS = ('1.0.0', '2.0.0')
_APP_MISSION_KINDS = (
    'initial_survey',
    'follow_up',
    'repair',
    'commissioning',
    'other',
)
_APP_DEPENDENCY_KINDS = (
    'mission_completed',
    'capture_finalized',
    'capture_delivered',
)
_APP_PLAN_REQUIRED_TEXT = (
    'plan_id',
    'plan_version',
    'project_ref',
    'room_name',
)
_APP_PLAN_REQUIRED_LISTS = (
    'entity_checklist',
    'measurement_requests',
    'surface_review_tasks',
)


def _app_task_plan_coherent(plan) -> bool:
    """Whether a bare/embedded plan dict satisfies the app's decode contract."""
    if (
        not isinstance(plan, dict)
        or plan.get('schema') != _APP_PLAN_SCHEMA
        or plan.get('schema_version') not in _APP_PLAN_SCHEMA_VERSIONS
    ):
        return False
    for key in _APP_PLAN_REQUIRED_TEXT:
        if not isinstance(plan.get(key), str) or not plan[key]:
            return False
    for key in _APP_PLAN_REQUIRED_LISTS:
        if not isinstance(plan.get(key), list):
            return False
    return True


def _app_mission_dependency_coherent(dependency) -> bool:
    """Whether one `HTDTMissionDependency` dict satisfies its required keys."""
    return (
        isinstance(dependency, dict)
        and isinstance(dependency.get('ref'), str)
        and bool(dependency['ref'])
        and dependency.get('kind') in _APP_DEPENDENCY_KINDS
        and isinstance(dependency.get('required'), bool)
    )


DEFAULT_RECEIVER_PORT = 8443
PAIRING_TTL_MINUTES = 10

# Serializes identity minting and pin reconciliation across service objects
# in this process; a sqlite IntegrityError still covers cross-process races.
_CONFIG_LOCK = threading.Lock()


class CaptureReceiverError(ValueError):
    pass


def _sha256_text(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _label(value: str, field: str) -> str:
    normalized = unicodedata.normalize('NFC', value).strip()
    if not normalized:
        raise ValueError(f'{field} must be non-empty')
    if len(normalized) > 240:
        raise ValueError(f'{field} exceeds 240 characters')
    return normalized


def cert_pin_from_pem(cert_pem: bytes) -> str:
    """``sha256:<64hex>`` of the leaf certificate DER — the pairing pin."""
    der = ssl.PEM_cert_to_DER_cert(cert_pem.decode('ascii'))
    return 'sha256:' + sha256(der).hexdigest()


def verification_code(
    receiver_instance_id: str, pairing_token: str, pinned_identity: str
) -> str:
    """The code both devices show: first-8 uppercase hex of the material hash."""
    material = f'{receiver_instance_id}|{pairing_token}|{pinned_identity}'
    hex8 = sha256(material.encode('utf-8')).hexdigest()[:8].upper()
    return f'{hex8[:4]}-{hex8[4:]}'


def generate_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    *,
    common_name: str = 'HTDT Capture Receiver',
    subject_alt_names: tuple[str, ...] = ('DNS:localhost', 'IP:127.0.0.1'),
    days: int = 3650,
) -> None:
    """Create a locally-generated self-signed receiver certificate."""
    san = ','.join(subject_alt_names)
    command = [
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key_path),
        '-out', str(cert_path),
        '-days', str(days),
        '-subj', f'/CN={common_name}',
    ]
    if san:
        command += ['-addext', f'subjectAltName={san}']
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            # The packaged app runs windowed (no console): without
            # CREATE_NO_WINDOW every openssl spawn flashes a console
            # window at the operator. POSIX ignores the flag entirely.
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except OSError as exc:
        # openssl is an external prerequisite (not bundled): on a machine
        # without it the raw FileNotFoundError would surface as a generic
        # "file not found" — name the actual missing tool instead.
        raise CaptureReceiverError(
            '証明書の生成に必要な openssl コマンドが見つかりません'
        ) from exc
    if result.returncode != 0:
        raise CaptureReceiverError(
            f'self-signed certificate generation failed: {result.stderr}'
        )


class ReceiverPairingPayload(BaseModel):
    """``htdt.receiver-pairing`` — the exact QR payload Capture consumes."""

    model_config = ConfigDict(frozen=True)

    schema_: str = Field(default=PAIRING_SCHEMA, alias='schema')
    schema_version: str = Field(
        default=PAIRING_SCHEMA_VERSION, alias='schema_version'
    )
    receiver_instance_id: str
    display_name: str
    endpoint_url: str
    capability_endpoint_url: str | None = None
    missions_endpoint_url: str | None = None
    pinned_identity: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    pairing_token: str = Field(min_length=16)
    project_ref: str | None = None
    expires_at: str | None = None

    model_config = ConfigDict(
        frozen=True, populate_by_name=True, alias_generator=None
    )


class CaptureReceiverConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    receiver_instance_id: str
    display_name: str = 'HTDT Receiver'
    host: str = '0.0.0.0'
    port: int = DEFAULT_RECEIVER_PORT
    enabled: bool = False  # receiver is off until the operator enables it
    pinned_identity: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')


class ReceiverPairing(BaseModel):
    model_config = ConfigDict(frozen=True)

    pairing_id: str
    pairing_token: str
    receiver_instance_id: str
    project_ref: str | None
    endpoint_url: str
    capability_endpoint_url: str | None
    missions_endpoint_url: str | None
    pinned_identity: str
    confirmation_code: str
    state: Literal['offered', 'active', 'revoked', 'expired']
    created_at_utc: str
    confirmed_at_utc: str | None
    expires_at_utc: str | None
    capture_instance_id: str | None = None


class ReceiverDeliveryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_key: str
    pairing_id: str
    artifact_kind: str
    artifact_id: str | None
    artifact_digest: str | None
    capture_revision_id: str | None
    bundle_digest: str | None
    archive_sha256: str
    archive_bytes: int
    outcome: Literal['accepted', 'rejected', 'already_staged']
    staging_ref: str | None
    lineage_digest: str | None
    detail: str
    received_at_utc: str


class MissionPackage(BaseModel):
    """An explicitly versioned mission package queued for a paired device."""

    model_config = ConfigDict(frozen=True)

    package_id: str = Field(min_length=1)
    mission_id: str | None = None
    purpose: str | None = None
    project_ref: str | None = None
    room_label: str | None = None
    issued_at_utc: str | None = None
    supersedes_package_id: str | None = None
    package_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_size: int = Field(ge=0)
    required_schema_version: str | None = None
    receiver_requirement: dict | None = None
    pairing_id: str | None = None
    status: Literal[
        'pending', 'received', 'failed', 'superseded', 'completed'
    ] = ('pending')
    status_detail: str = ''
    updated_at_utc: str | None = None

    def descriptor(self) -> dict:
        """The ``htdt.mission-listing`` descriptor fields Capture reads."""
        descriptor: dict[str, object] = {
            'package_id': self.package_id,
            'byte_size': self.byte_size,
            # Wire grammar is ``sha256:<hex>`` — the pinned-digest
            # form the app requires everywhere (pairing pins,
            # manifest source_refs). Storage stays bare hex.
            'package_sha256': f'sha256:{self.package_sha256}',
        }
        for key, value in (
            ('mission_id', self.mission_id),
            ('purpose', self.purpose),
            ('project_ref', self.project_ref),
            ('room_label', self.room_label),
            ('issued_at', self.issued_at_utc),
            ('supersedes_package_id', self.supersedes_package_id),
            ('required_schema_version', self.required_schema_version),
            ('receiver_requirement', self.receiver_requirement),
        ):
            if value is not None:
                descriptor[key] = value
        return descriptor


class CaptureReceiverService:
    """Pairing, delivery, and mission-pull plumbing behind the LAN endpoint."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        inbox_repository: CaptureInboxRepository | None = None,
        ingestion_repository: CaptureIngestionRepository | None = None,
        *,
        bundle_reader: (
            Callable[
                [bytes],
                tuple[CaptureIngestionPlan, Mapping[str, bytes]]
                | tuple[CaptureIngestionPlan, Mapping[str, bytes], bytes],
            ]
            | None
        ) = None,
        base_url: str | None = None,
        data_dir: Path | None = None,
        max_archive_bytes: int = RECEIVER_MAX_ARCHIVE_BYTES,
        delivery_listener: Callable[[ReceiverDeliveryRecord], None] | None = (
            None
        ),
    ) -> None:
        self.scene_repository = scene_repository
        self.ingestion_repository = ingestion_repository or (
            CaptureIngestionRepository(scene_repository)
        )
        self.inbox_repository = inbox_repository or CaptureInboxRepository(
            scene_repository, self.ingestion_repository
        )
        self.bundle_reader = bundle_reader or _default_bundle_reader
        self._delivery_listener = delivery_listener
        self._base_url_override = base_url
        self._data_dir = (
            Path(data_dir)
            if data_dir is not None
            else Path(scene_repository.path).parent / 'capture-receiver'
        )
        self.max_archive_bytes = max_archive_bytes
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._initialize()
        self.field_return_repository = FieldReturnRepository(self.path)

    # -- persistence ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'capture_receiver_config', 'capture_receiver_pairings', 'capture_receiver_deliveries', 'capture_mission_packages')

    def _config_row(self) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                'SELECT * FROM capture_receiver_config WHERE id=1'
            ).fetchone()

    def get_config(self) -> CaptureReceiverConfig:
        """Load (or lazily mint) the durable receiver identity."""
        with _CONFIG_LOCK:
            row = self._config_row()
            if row is None:
                receiver_instance_id = str(uuid.uuid4())
                cert_pem, _key_pem = self._ensure_certificate()
                pinned = cert_pin_from_pem(cert_pem)
                try:
                    with closing(self._connect()) as connection, connection:
                        connection.execute(
                            'INSERT INTO capture_receiver_config('
                            'id, receiver_instance_id, display_name, host, port, '
                            'enabled, pinned_identity) VALUES (1, ?, ?, ?, ?, 0, ?)',
                            (
                                receiver_instance_id,
                                'HTDT Receiver',
                                '0.0.0.0',
                                DEFAULT_RECEIVER_PORT,
                                pinned,
                            ),
                        )
                except sqlite3.IntegrityError:
                    # A concurrent first call minted the row first; its
                    # identity stands and is re-read below.
                    pass
                row = self._config_row()
            assert row is not None
            cert_pem, _key_pem = self._ensure_certificate()
            live_pin = cert_pin_from_pem(cert_pem)
            if row['pinned_identity'] != live_pin:
                # The served credential is the live artifact; a silent
                # regeneration (deleted/torn files) leaves the stored pin
                # stale until reconciled here.
                with closing(self._connect()) as connection, connection:
                    connection.execute(
                        'UPDATE capture_receiver_config '
                        'SET pinned_identity=? WHERE id=1',
                        (live_pin,),
                    )
                row = self._config_row()
                assert row is not None
        return CaptureReceiverConfig(
            receiver_instance_id=row['receiver_instance_id'],
            display_name=row['display_name'],
            host=row['host'],
            port=int(row['port']),
            enabled=bool(row['enabled']),
            pinned_identity=row['pinned_identity'],
        )

    def set_enabled(self, enabled: bool) -> None:
        self.get_config()  # ensure row exists
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE capture_receiver_config SET enabled=? WHERE id=1',
                (1 if enabled else 0,),
            )

    def set_port(self, port: int) -> None:
        if not (1 <= int(port) <= 65535):
            raise CaptureReceiverError('port must be in 1..65535')
        self.get_config()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE capture_receiver_config SET port=? WHERE id=1',
                (int(port),),
            )

    def _ensure_certificate(self) -> tuple[bytes, bytes]:
        cert_path = self._data_dir / 'receiver-cert.pem'
        key_path = self._data_dir / 'receiver-key.pem'
        try:
            if cert_path.exists() and key_path.exists():
                # Bound both credentials before the SSL parser sees their paths.
                read_file_bounded(
                    cert_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS certificate'
                )
                read_file_bounded(
                    key_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS private key'
                )
                # A torn pair (interrupted promote, manual splice) passes
                # the exists-guard but cannot load; regenerate it so every
                # caller sees a consistent credential.
                try:
                    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
                        str(cert_path), str(key_path)
                    )
                except ssl.SSLError:
                    cert_path.unlink(missing_ok=True)
                    key_path.unlink(missing_ok=True)
                else:
                    return (
                        read_file_bounded(
                            cert_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS certificate'
                        ),
                        read_file_bounded(
                            key_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS private key'
                        ),
                    )
            # openssl writes the pair non-atomically; stage beside the
            # canonical names and promote, or a torn credential survives
            # under the exists-guard and bricks every later start.
            cert_fd, staged_cert_name = tempfile.mkstemp(
                prefix=f'.{cert_path.name}.', suffix='.tmp', dir=self._data_dir
            )
            os.close(cert_fd)
            staged_cert = Path(staged_cert_name)
            key_fd, staged_key_name = tempfile.mkstemp(
                prefix=f'.{key_path.name}.', suffix='.tmp', dir=self._data_dir
            )
            os.close(key_fd)
            staged_key = Path(staged_key_name)
            try:
                generate_self_signed_cert(staged_cert, staged_key)
                os.replace(staged_cert, cert_path)
                os.replace(staged_key, key_path)
            finally:
                staged_cert.unlink(missing_ok=True)
                staged_key.unlink(missing_ok=True)
            return (
                read_file_bounded(
                    cert_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS certificate'
                ),
                read_file_bounded(
                    key_path, TLS_CREDENTIAL_MAX_BYTES, label='TLS private key'
                ),
            )
        except IngressTooLargeError as exc:
            raise CaptureReceiverError(str(exc)) from exc

    # -- pairing --------------------------------------------------------

    def _base_url(self) -> str:
        config = self.get_config()
        if self._base_url_override:
            return self._base_url_override.rstrip('/')
        host = config.host
        if host in ('0.0.0.0', '::'):
            host = '127.0.0.1'
        return f'https://{host}:{config.port}'

    def begin_pairing(
        self,
        *,
        project_ref: str | None = None,
        display_name: str | None = None,
        ttl_minutes: int = PAIRING_TTL_MINUTES,
    ) -> tuple[ReceiverPairing, ReceiverPairingPayload]:
        """Issue a pairing offer: QR payload + confirmation code material."""
        config = self.get_config()
        base = self._base_url()
        pairing_id = str(uuid.uuid4())
        pairing_token = secrets.token_hex(16)
        code = verification_code(
            config.receiver_instance_id,
            pairing_token,
            config.pinned_identity,
        )
        expires = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()
        base_path = f'{RECEIVER_PATH_PREFIX}/{pairing_token}'
        endpoint_url = f'{base}{base_path}/deliveries'
        capability_url = f'{base}{base_path}/capabilities'
        missions_url = f'{base}{base_path}/missions'
        payload = ReceiverPairingPayload(
            receiver_instance_id=config.receiver_instance_id,
            display_name=display_name or config.display_name,
            endpoint_url=endpoint_url,
            capability_endpoint_url=capability_url,
            missions_endpoint_url=missions_url,
            pinned_identity=config.pinned_identity,
            pairing_token=pairing_token,
            project_ref=project_ref,
            expires_at=expires,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                '''
                INSERT INTO capture_receiver_pairings(
                    pairing_id, pairing_token, receiver_instance_id,
                    project_ref, endpoint_url, capability_endpoint_url,
                    missions_endpoint_url, pinned_identity,
                    confirmation_code, state, created_at_utc,
                    confirmed_at_utc, expires_at_utc, capture_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'offered', ?, NULL, ?, NULL)
                ''',
                (
                    pairing_id,
                    pairing_token,
                    config.receiver_instance_id,
                    project_ref,
                    endpoint_url,
                    capability_url,
                    missions_url,
                    config.pinned_identity,
                    code,
                    _utc_now(),
                    expires,
                ),
            )
        pairing = self.get_pairing(pairing_id)
        if pairing is None:
            raise CaptureReceiverError('pairing offer vanished after insert')
        return pairing, payload

    def confirm_pairing(self, pairing_id: str) -> ReceiverPairing:
        """Operator confirmed both sides show the same code → activate."""
        pairing = self.get_pairing(pairing_id)
        if pairing is None:
            raise CaptureReceiverError('unknown pairing')
        if pairing.state == 'revoked':
            raise CaptureReceiverError('pairing is revoked')
        if pairing.expires_at_utc and (
            datetime.fromisoformat(pairing.expires_at_utc)
            < datetime.now(timezone.utc)
        ):
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE capture_receiver_pairings SET state='expired' "
                    'WHERE pairing_id=?',
                    (pairing_id,),
                )
            raise CaptureReceiverError(
                'pairing offer expired; issue a new one'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE capture_receiver_pairings SET state='active', "
                'confirmed_at_utc=? WHERE pairing_id=?',
                (_utc_now(), pairing_id),
            )
        pairing = self.get_pairing(pairing_id)
        if pairing is None:
            raise CaptureReceiverError('pairing vanished after confirmation')
        return pairing

    def revoke_pairing(self, pairing_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            result = connection.execute(
                "UPDATE capture_receiver_pairings SET state='revoked' "
                'WHERE pairing_id=?',
                (pairing_id,),
            )
            if result.rowcount == 0:
                raise CaptureReceiverError('unknown pairing')

    def get_pairing(self, pairing_id: str) -> ReceiverPairing | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM capture_receiver_pairings WHERE pairing_id=?',
                (pairing_id,),
            ).fetchone()
            return self._pairing_from_row(row) if row else None

    def _pairing_from_row(self, row: sqlite3.Row) -> ReceiverPairing:
        return ReceiverPairing(
            pairing_id=row['pairing_id'],
            pairing_token=row['pairing_token'],
            receiver_instance_id=row['receiver_instance_id'],
            project_ref=row['project_ref'],
            endpoint_url=row['endpoint_url'],
            capability_endpoint_url=row['capability_endpoint_url'],
            missions_endpoint_url=row['missions_endpoint_url'],
            pinned_identity=row['pinned_identity'],
            confirmation_code=row['confirmation_code'],
            state=row['state'],
            created_at_utc=row['created_at_utc'],
            confirmed_at_utc=row['confirmed_at_utc'],
            expires_at_utc=row['expires_at_utc'],
            capture_instance_id=row['capture_instance_id'],
        )

    def list_pairings(self) -> tuple[ReceiverPairing, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM capture_receiver_pairings ORDER BY created_at_utc, pairing_id'
            ).fetchall()
            return tuple(self._pairing_from_row(row) for row in rows)

    def _pairing_for_token(self, token: str) -> ReceiverPairing | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM capture_receiver_pairings WHERE pairing_token=?',
                (token,),
            ).fetchone()
            return self._pairing_from_row(row) if row else None

    def _require_active_pairing(self, token: str) -> ReceiverPairing:
        pairing = self._pairing_for_token(token)
        if pairing is None or pairing.state != 'active':
            raise CaptureReceiverError(
                'unknown or inactive pairing endpoint'
            )
        return pairing

    def capabilities_document(self, token: str) -> dict | None:
        """``htdt.endpoint-capabilities`` for a paired endpoint."""
        pairing = self._pairing_for_token(token)
        if pairing is None or pairing.state not in ('offered', 'active'):
            return None
        # The advertised contract must equal the implemented one: payload
        # schemas are every versioned bundle family the ingestion pipeline
        # validates, and authority families are the kinds the Capture Inbox
        # can promote — both read from their single authorities instead of
        # being hand-mirrored here.
        from .capture_bundle import _load_support_matrix

        def _version_key(version: str) -> list[int]:
            return [int(part) if part.isdigit() else 0 for part in version.split('.')]

        accepted_payload_schemas = [
            {
                'schema': contract['schema_id'],
                'versions': sorted(contract['read'], key=_version_key),
            }
            for contract in _load_support_matrix()['families'].values()
            if contract.get('schema_id') and contract.get('read')
        ]
        return {
            'schema': CAPABILITIES_SCHEMA,
            'schema_version': CAPABILITIES_SCHEMA_VERSION,
            'endpoint_identity': pairing.receiver_instance_id,
            'handoff_protocol_versions': [HANDOFF_PROTOCOL_VERSION],
            'accepted_bundle_schema_versions': [BUNDLE_SCHEMA_VERSION],
            'accepted_payload_schemas': accepted_payload_schemas,
            # The app's `supported_authority_families` vocabulary is the
            # family tokens its mission requirements and bundle
            # inventories can be checked against. Semantic-task families
            # (authorities.json records) are staged verbatim today — no
            # promotion executor — so advertising them would lie about
            # fidelity and fail every semantic requirement honestly.
            # The honest promotable set is the executable authority
            # kinds; `staged_authority_families` keeps the rest visible
            # in the promotion-kind vocabulary.
            'supported_authority_families': sorted(
                EXECUTABLE_AUTHORITY_KINDS
            ),
            'staged_authority_families': sorted(PROMOTION_AUTHORITY_KINDS),
            'max_archive_bytes': self.max_archive_bytes,
            'mission_receipts_supported': True,
            'accepted_artifact_kinds': [
                {
                    'artifact_kind': 'capture_bundle',
                    'accepted_schema_versions': [BUNDLE_SCHEMA_VERSION],
                    'max_archive_bytes': self.max_archive_bytes,
                },
                {
                    # The emitted .htdtfieldreturn container document
                    # versions (htdt.field_return 1.0.0/2.0.0) — the flat
                    # htdt.field-return manifest stays stageable too but is
                    # a diagnostic form, not an advertised one.
                    'artifact_kind': 'field_return',
                    'accepted_schema_versions': list(
                        SUPPORTED_FIELD_RETURN_CONTAINER_VERSIONS
                    ),
                    'max_archive_bytes': self.max_archive_bytes,
                },
            ],
            'mission_packages_served': True,
            'equipment_catalogs_recognized': [],
            'project_ref': pairing.project_ref,
            'manual_review_required': True,
        }

    # -- deliveries ------------------------------------------------------

    def handle_delivery(
        self, token: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict]:
        """POST endpoint: accept declared artifact bytes for staging.

        ``capture_bundle`` archives ingest into the Capture Inbox;
        ``field_return`` containers stage as contributions. Both share the
        same archive-identity, dedup, and receipt-echo contract."""
        try:
            pairing = self._require_active_pairing(token)
        except CaptureReceiverError:
            return 404, {'detail': 'unknown endpoint'}

        lowered = {key.lower(): value for key, value in headers.items()}
        artifact_kind = lowered.get('x-htdt-artifact-kind', 'capture_bundle')
        artifact_id = lowered.get('x-htdt-artifact-id')
        artifact_digest = lowered.get('x-htdt-artifact-digest')
        # legacy capture_bundle headers are equivalent synonyms
        capture_revision_id = lowered.get('x-htdt-capture-revision-id')
        bundle_digest = lowered.get('x-htdt-bundle-digest')
        declared_sha = lowered.get('x-htdt-archive-sha256')
        declared_bytes = lowered.get('x-htdt-archive-bytes')
        delivery_id = lowered.get('x-htdt-delivery-id')

        def reject(
            detail: str,
            status: int = 400,
            *,
            stage_envelope: bool = False,
        ) -> tuple[int, dict]:
            if stage_envelope:
                try:
                    self.inbox_repository.stage_rejected(
                        envelope_sha256=declared_sha or _sha256_text(body),
                        validation_error=detail,
                        arrival_source='paired_receiver',
                        scope=pairing.project_ref
                        or CAPTURE_INBOX_UNASSIGNED_SCOPE,
                        source_detail=f'pairing {pairing.pairing_id}',
                        capture_revision_id=capture_revision_id or 'unknown',
                    )
                except Exception:
                    # Envelope staging is audit, never a reason to change
                    # the wire outcome — the delivery-ledger row still
                    # records the rejection below.
                    pass
            try:
                self._record_delivery(
                    pairing=pairing,
                    delivery_id=delivery_id,
                    artifact_kind=artifact_kind,
                    artifact_id=artifact_id,
                    artifact_digest=artifact_digest,
                    capture_revision_id=capture_revision_id,
                    bundle_digest=bundle_digest,
                    archive_sha256=declared_sha or _sha256_text(body),
                    archive_bytes=len(body),
                    outcome='rejected',
                    staging_ref=None,
                    lineage_digest=None,
                    detail=detail,
                )
            except CaptureReceiverError as exc:
                # delivery id replayed with different bytes: report the
                # conflict instead of dropping the connection unanswered.
                # Full identity echoes let the sender's receipt
                # validation classify this as the dedup conflict it is.
                return 409, {
                    'ingestion_outcome': 'rejected',
                    'artifact_kind': artifact_kind,
                    'artifact_id': artifact_id,
                    'artifact_digest': artifact_digest,
                    'capture_revision_id': capture_revision_id,
                    'bundle_digest': bundle_digest,
                    'detail': str(exc),
                }
            return status, {
                'ingestion_outcome': 'rejected',
                'artifact_kind': artifact_kind,
                'artifact_id': artifact_id,
                'artifact_digest': artifact_digest,
                'capture_revision_id': capture_revision_id,
                'bundle_digest': bundle_digest,
                'detail': detail,
            }

        if artifact_kind not in DELIVERABLE_KINDS:
            return reject(
                f'unsupported artifact kind {artifact_kind}', status=415
            )
        if declared_sha is None or declared_bytes is None:
            return reject(
                'missing archive identity headers '
                '(X-HTDT-Archive-SHA256 / X-HTDT-Archive-Bytes)'
            )
        if len(body) > self.max_archive_bytes:
            return reject('archive exceeds receiver byte ceiling', 413)
        try:
            declared_length = int(declared_bytes)
        except ValueError:
            return reject('archive byte count header is not an integer')
        if declared_length != len(body):
            return reject('archive byte count does not match the body')
        if _sha256_text(body) != declared_sha:
            return reject('archive SHA-256 does not match the body')

        # idempotent re-delivery: the receipt echoes the original staging
        # identity, never a new one
        delivery_key = f'{pairing.pairing_id}:{delivery_id or declared_sha}'
        prior = self._get_delivery(delivery_key)
        if prior is not None:
            # The recorded archive hash must agree: a delivery id replayed
            # under different bytes is a conflict, not a receipt for bytes
            # that were never ingested.
            if prior.archive_sha256 != declared_sha:
                return 409, {
                    'ingestion_outcome': 'rejected',
                    'artifact_kind': artifact_kind,
                    'artifact_id': artifact_id,
                    'artifact_digest': artifact_digest,
                    'capture_revision_id': capture_revision_id,
                    'bundle_digest': bundle_digest,
                    'detail': 'delivery id replayed with different bytes',
                }
            # a re-delivery of an accepted delivery resolves to the same
            # staging slot and reports the dedup outcome, never a fresh
            # 'accepted'
            if prior.outcome == 'accepted':
                prior = prior.model_copy(update={'outcome': 'already_staged'})
            if prior.outcome != 'rejected':
                return 200, self._receipt_for(prior)
            # A rejected delivery of the same bytes retries the pipeline:
            # a transient failure must not become a permanent dead letter.

        if artifact_kind == 'field_return':
            return self._handle_field_return_delivery(
                pairing=pairing,
                delivery_id=delivery_id,
                artifact_id=artifact_id,
                artifact_digest=artifact_digest,
                declared_sha=declared_sha,
                body=body,
                delivery_key=delivery_key,
                reject=reject,
            )

        # hard fail-closed rule: same capture revision arriving under a
        # different bundle digest is a conflict, never a silent variant
        if capture_revision_id and bundle_digest:
            conflict = self._conflicting_revision(
                capture_revision_id, bundle_digest
            )
            if conflict is not None:
                return reject(
                    'same capture revision under a different bundle '
                    'digest; rejected and recorded on the delivery ledger'
                )

        try:
            read = self.bundle_reader(body)
            parts = tuple(read)
            plan, payloads = parts[0], parts[1]
            # Readers may hand back the canonical manifest bytes so the
            # wire path retains the same bundle evidence the file-import
            # lane does (#338); older two-part readers keep working.
            manifest = parts[2] if len(parts) > 2 else None
            if not isinstance(plan, CaptureIngestionPlan):
                plan = CaptureIngestionPlan.model_validate(plan)
        except Exception as exc:
            return reject(
                f'capture bundle could not be read: {exc}',
                stage_envelope=True,
            )

        # the wire headers and the bundle identity must agree
        if capture_revision_id and (
            capture_revision_id != plan.bundle.capture_revision_id
        ):
            return reject('revision header disagrees with the bundle')
        if bundle_digest and bundle_digest != plan.bundle.bundle_digest:
            return reject('bundle digest header disagrees with the bundle')
        if artifact_digest and artifact_digest != plan.bundle.bundle_digest:
            return reject('artifact digest header disagrees with the bundle')

        # heal earlier crashes first: an ingest committed before its
        # stage leaves an invisible orphan until a delivery arrives
        self.inbox_repository.reconcile_orphaned_ingestions()

        try:
            self.ingestion_repository.ingest(
                plan, payloads, manifest=manifest
            )
        except Exception as exc:
            return reject(
                f'capture bundle failed ingestion: {exc}',
                stage_envelope=True,
            )

        scope = pairing.project_ref or CAPTURE_INBOX_UNASSIGNED_SCOPE
        try:
            staged = self.inbox_repository.stage(
                plan,
                arrival_source='paired_receiver',
                scope=scope,
                source_detail=f'pairing {pairing.pairing_id}',
            )
        except Exception as exc:
            return reject(f'capture bundle failed inbox staging: {exc}')

        record = self._record_delivery(
            pairing=pairing,
            delivery_id=delivery_id,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            artifact_digest=artifact_digest or plan.bundle.bundle_digest,
            capture_revision_id=plan.bundle.capture_revision_id,
            bundle_digest=plan.bundle.bundle_digest,
            archive_sha256=declared_sha,
            archive_bytes=len(body),
            outcome='accepted' if staged.created else 'already_staged',
            staging_ref=staged.item.inbox_item_id,
            lineage_digest=plan.lineage_digest,
            detail='',
            delivery_key=delivery_key,
        )
        # A racer whose stage() call lost the dedup gets the same replay
        # receipt a sequential re-delivery would, and only the stager
        # announces the acceptance — the stored row is canonical either
        # way (see _record_delivery's 'already_staged' upgrade).
        if not staged.created and record.outcome == 'accepted':
            record = record.model_copy(update={'outcome': 'already_staged'})
        if (
            staged.created
            and record.outcome == 'accepted'
            and self._delivery_listener is not None
        ):
            try:
                self._delivery_listener(record)
            except Exception:
                _LOGGER.exception('capture delivery listener failed')
        return 200, self._receipt_for(record)

    def _handle_field_return_delivery(
        self,
        *,
        pairing: ReceiverPairing,
        delivery_id: str | None,
        artifact_id: str | None,
        artifact_digest: str | None,
        declared_sha: str,
        body: bytes,
        delivery_key: str,
        reject: Callable[..., tuple[int, dict]],
    ) -> tuple[int, dict]:
        """Stage a `.htdtfieldreturn` contribution through the delivery lane.

        Staging is the durable verdict point: a well-formed contribution
        lands in ``field_return_contributions``; unsupported or malformed
        artifacts are rejected on the wire but still preserved as staging
        diagnostics when a contribution id can be read. The declared
        ``artifact_digest`` is the emitter's semantic digest — the receiver
        stores and echoes it without recomputation (archive bytes are bound
        by the verified SHA-256 header)."""

        try:
            prepared = stage_field_return_artifact(
                body,
                self._known_project_references(),
                channel_project_ref=pairing.project_ref,
                declared_content_digest=artifact_digest,
            )
        except Exception as exc:
            _LOGGER.exception('field return staging failed')
            return reject(f'field return could not be staged: {exc}')

        if (
            artifact_id
            and prepared.contribution_id
            and artifact_id != prepared.contribution_id
        ):
            # A header/body identity mismatch is a sender-side
            # inconsistency: the contribution is kept only as a
            # malformed diagnostic — never as a validated row an
            # operator could reconcile or apply.
            detail = (
                'artifact id header disagrees with the contribution '
                'identity declared inside the artifact'
            )
            try:
                self.field_return_repository.stage_prepared(
                    prepared.model_copy(
                        update={
                            'validation_state': 'malformed',
                            'detail': detail,
                        }
                    ),
                    body,
                )
            except FieldReturnConflictError:
                pass  # the ledger already knows these bytes differently
            return reject(detail)

        try:
            staged, created = self.field_return_repository.stage_prepared(
                prepared, body
            )
        except FieldReturnConflictError as exc:
            return reject(
                f'field return contribution conflict: {exc}'
            )
        except Exception as exc:
            _LOGGER.exception('field return staging failed')
            return reject(f'field return could not be staged: {exc}')

        if staged.validation_state != 'validated':
            return reject(
                staged.detail
                or 'field return artifact did not validate'
            )

        record = self._record_delivery(
            pairing=pairing,
            delivery_id=delivery_id,
            artifact_kind='field_return',
            artifact_id=staged.contribution_id,
            artifact_digest=artifact_digest,
            capture_revision_id=None,
            bundle_digest=None,
            archive_sha256=declared_sha,
            archive_bytes=len(body),
            outcome='accepted' if created else 'already_staged',
            staging_ref=f'field-return:{staged.contribution_id}',
            lineage_digest=None,
            detail=staged.detail or '',
            delivery_key=delivery_key,
        )
        # Same racer normalization as the bundle path: the delivery that
        # lost the staging dedup reports 'already_staged'; only the stager
        # announces 'accepted'.
        if not created and record.outcome == 'accepted':
            record = record.model_copy(update={'outcome': 'already_staged'})
        if (
            created
            and record.outcome == 'accepted'
            and self._delivery_listener is not None
        ):
            try:
                self._delivery_listener(record)
            except Exception:
                _LOGGER.exception('field return delivery listener failed')
        return 200, self._receipt_for(record)

    def _known_project_references(self) -> tuple:
        """Project identities the library knows, for field-return routing.

        Read straight from ``htdt_project_documents`` — a delivery must
        never trigger library migrations as a side effect. Container-form
        field returns carry no project reference today, so this exists for
        the flat manifest form and future container versions.
        """

        from .project_identity import HTDTProjectReference

        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    'SELECT project_id, document_id, display_name, '
                    'cloned_from_project_id FROM htdt_project_documents '
                    'WHERE archived=0'
                ).fetchall()
        except sqlite3.OperationalError:
            return ()
        references: list[HTDTProjectReference] = []
        for row in rows:
            try:
                references.append(
                    HTDTProjectReference(
                        project_id=row['project_id'],
                        document_id=row['document_id'],
                        cloned_from_project_id=row['cloned_from_project_id'],
                        project_name=row['display_name'],
                    )
                )
            except ValueError:
                _LOGGER.warning(
                    'project row %s is not a uuid4 identity; skipped for '
                    'field-return routing',
                    row['project_id'],
                )
        return tuple(references)

    def _receipt_for(self, record: ReceiverDeliveryRecord) -> dict:
        return {
            'ingestion_outcome': record.outcome,
            'capture_revision_id': record.capture_revision_id,
            'bundle_digest': record.bundle_digest,
            'artifact_kind': record.artifact_kind,
            'artifact_id': record.artifact_id,
            'artifact_digest': record.artifact_digest,
            'staging_ref': record.staging_ref,
            'detail': record.detail or None,
        }

    def _conflicting_revision(
        self, capture_revision_id: str, bundle_digest: str
    ) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT delivery_key FROM capture_receiver_deliveries '
                'WHERE capture_revision_id=? AND bundle_digest<>? '
                'AND outcome<>"rejected"',
                (capture_revision_id, bundle_digest),
            ).fetchone()
        return str(row['delivery_key']) if row else None

    def _get_delivery(self, delivery_key: str) -> ReceiverDeliveryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM capture_receiver_deliveries WHERE delivery_key=?',
                (delivery_key,),
            ).fetchone()
        return self._delivery_from_row(row) if row else None

    def _record_delivery(
        self,
        *,
        pairing: ReceiverPairing,
        delivery_id: str | None,
        artifact_kind: str,
        artifact_id: str | None,
        artifact_digest: str | None,
        capture_revision_id: str | None,
        bundle_digest: str | None,
        archive_sha256: str,
        archive_bytes: int,
        outcome: str,
        staging_ref: str | None,
        lineage_digest: str | None,
        detail: str,
        delivery_key: str | None = None,
    ) -> ReceiverDeliveryRecord:
        key = delivery_key or f'{pairing.pairing_id}:{delivery_id or archive_sha256}'
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE serializes the dedup read with the ledger write:
            # two concurrent re-deliveries of the same key must not both pass
            # the existence check and collide on the delivery_key insert.
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT * FROM capture_receiver_deliveries WHERE delivery_key=?',
                (key,),
            ).fetchone()
            if existing is not None:
                stored = self._delivery_from_row(existing)
                if (
                    stored.archive_sha256 != archive_sha256
                    or stored.artifact_kind != artifact_kind
                ):
                    raise CaptureReceiverError(
                        'delivery id replayed with different bytes'
                    )
                if stored.outcome == 'rejected':
                    # A retry of the same bytes may now succeed: rewrite
                    # the ledger row instead of replaying a stale verdict.
                    connection.execute(
                        '''
                        UPDATE capture_receiver_deliveries
                        SET artifact_digest=?, capture_revision_id=?,
                            bundle_digest=?, outcome=?, staging_ref=?,
                            lineage_digest=?, detail=?
                        WHERE delivery_key=?
                        ''',
                        (
                            artifact_digest,
                            capture_revision_id,
                            bundle_digest,
                            outcome,
                            staging_ref,
                            lineage_digest,
                            detail,
                            key,
                        ),
                    )
                    updated = connection.execute(
                        'SELECT * FROM capture_receiver_deliveries '
                        'WHERE delivery_key=?',
                        (key,),
                    ).fetchone()
                    return self._delivery_from_row(updated)
                if stored.outcome == 'already_staged' and outcome == 'accepted':
                    # Two concurrent deliveries of one key both passed the
                    # dedup read: the loser recorded 'already_staged'
                    # first, and this call is the stager whose verdict is
                    # canonical. Rewrite the row to 'accepted' so the
                    # ledger shows the delivery was accepted once, not
                    # only ever deduplicated.
                    connection.execute(
                        'UPDATE capture_receiver_deliveries '
                        'SET outcome=? WHERE delivery_key=?',
                        ('accepted', key),
                    )
                    updated = connection.execute(
                        'SELECT * FROM capture_receiver_deliveries '
                        'WHERE delivery_key=?',
                        (key,),
                    ).fetchone()
                    return self._delivery_from_row(updated)
                return stored
            connection.execute(
                '''
                INSERT INTO capture_receiver_deliveries(
                    delivery_key, pairing_id, artifact_kind, artifact_id,
                    artifact_digest, capture_revision_id, bundle_digest,
                    archive_sha256, archive_bytes, outcome, staging_ref,
                    lineage_digest, detail, received_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    key,
                    pairing.pairing_id,
                    artifact_kind,
                    artifact_id,
                    artifact_digest,
                    capture_revision_id,
                    bundle_digest,
                    archive_sha256,
                    archive_bytes,
                    outcome,
                    staging_ref,
                    lineage_digest,
                    detail,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                'SELECT * FROM capture_receiver_deliveries WHERE delivery_key=?',
                (key,),
            ).fetchone()
        return self._delivery_from_row(row)

    def _delivery_from_row(self, row: sqlite3.Row) -> ReceiverDeliveryRecord:
        return ReceiverDeliveryRecord(
            delivery_key=row['delivery_key'],
            pairing_id=row['pairing_id'],
            artifact_kind=row['artifact_kind'],
            artifact_id=row['artifact_id'],
            artifact_digest=row['artifact_digest'],
            capture_revision_id=row['capture_revision_id'],
            bundle_digest=row['bundle_digest'],
            archive_sha256=row['archive_sha256'],
            archive_bytes=int(row['archive_bytes']),
            outcome=row['outcome'],
            staging_ref=row['staging_ref'],
            lineage_digest=row['lineage_digest'],
            detail=row['detail'],
            received_at_utc=row['received_at_utc'],
        )

    # -- mission packages (pull model) -----------------------------------

    def queue_mission_package(
        self,
        package_id: str,
        payload: bytes,
        *,
        descriptor: dict | None = None,
        pairing_id: str | None = None,
        native_package_json: str | None = None,
    ) -> MissionPackage:
        """Queue a Mission package for a paired device to pull.

        Packages are versioned artifacts; their bytes are content-stored so
        file-based fallback export keeps the same identity. When the queue
        carries a native ``htdt.capture.mission-package`` (``queue_mission``),
        its canonical JSON is persisted alongside so the project-side
        mission-return reconciliation can replay the issuing baseline later.
        """
        if len(payload) > MISSION_PACKAGE_MAX_BYTES:
            raise CaptureReceiverError(
                'mission package exceeds the receive-leg byte ceiling'
            )
        # Fail-closed grammar guard: the pull lane serves Capture apps,
        # which decode only the `htdt.capture-mission` envelope or a bare
        # `htdt.capture-task-plan` document. Queueing another grammar
        # mints a package every device rejects at import — forever
        # re-listed, never settled (round-trip audit).
        try:
            decoded = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CaptureReceiverError(
                'mission package payload is not valid JSON'
            ) from exc
        if (
            not isinstance(decoded, dict)
            or decoded.get('schema')
            not in ('htdt.capture-mission', 'htdt.capture-task-plan')
            or not isinstance(decoded.get('schema_version'), str)
            or not decoded.get('schema_version')
        ):
            raise CaptureReceiverError(
                'mission package payload is not a decodable capture '
                'mission document (htdt.capture-mission / '
                'htdt.capture-task-plan)'
            )
        # Shape coherence: mirror the app's hard-decode contract — a
        # schema-matching payload missing these still fails import on
        # every device, so reject it here instead of letting it ride
        # the pull lane to an `invalid_payload` failure.
        if decoded['schema'] == 'htdt.capture-mission':
            if (
                decoded['schema_version']
                not in _APP_MISSION_SCHEMA_VERSIONS
            ):
                raise CaptureReceiverError(
                    'mission envelope schema_version is not a version '
                    'the app decodes (supported: 1.0.0)'
                )
            dependencies = decoded.get('dependencies')
            if (
                not isinstance(decoded.get('mission_id'), str)
                or not decoded['mission_id']
                or decoded.get('mission_kind') not in _APP_MISSION_KINDS
                or not _app_task_plan_coherent(decoded.get('plan'))
                or not isinstance(dependencies, list)
                or any(
                    not _app_mission_dependency_coherent(dependency)
                    for dependency in dependencies
                )
            ):
                raise CaptureReceiverError(
                    'mission envelope is missing a decodable '
                    'mission_id/mission_kind/plan/dependencies'
                )
        elif not _app_task_plan_coherent(decoded):
            raise CaptureReceiverError(
                'bare task plan is missing a decodable required field '
                '(plan_id/plan_version/project_ref/room_name/'
                'entity_checklist/measurement_requests/'
                'surface_review_tasks) or a supported schema_version'
            )
        descriptor = dict(descriptor or {})
        # The app's receiver_requirement struct strictly decodes the
        # fields below — a divergent value used to break the WHOLE
        # listing decode on the device. Validate the shape at write
        # time so a queuer can never store a requirement that breaks
        # every pending package.
        requirement = descriptor.get('receiver_requirement')
        if requirement is not None:
            if not isinstance(requirement, dict):
                raise CaptureReceiverError(
                    'receiver_requirement must be an object'
                )
            for key in (
                'required_authority_families',
                'required_payload_schemas',
            ):
                if key not in requirement:
                    raise CaptureReceiverError(
                        f'receiver_requirement missing required {key}'
                    )
                if not isinstance(requirement[key], list) or any(
                    not isinstance(item, str) for item in requirement[key]
                ):
                    raise CaptureReceiverError(
                        f'receiver_requirement {key} must be a list '
                        'of strings'
                    )
            if not isinstance(
                requirement.get('require_mission_receipts'), bool
            ):
                raise CaptureReceiverError(
                    'receiver_requirement missing required '
                    'require_mission_receipts'
                )
            for key in ('destination_project_ref', 'min_handoff_protocol'):
                if (
                    requirement.get(key) is not None
                    and not isinstance(requirement[key], str)
                ):
                    raise CaptureReceiverError(
                        f'receiver_requirement {key} must be a string'
                    )
        # Listing metadata must agree with the payload it advertises:
        # a descriptor declaring a different mission_id/project_ref/
        # room_label would mint a listing that lies about its bytes.
        # Derive the values from the payload when the descriptor omits
        # them and refuse on divergence.
        plan_doc = (
            decoded.get('plan')
            if decoded['schema'] == 'htdt.capture-mission'
            else decoded
        )
        payload_fields = {
            'mission_id': decoded.get('mission_id'),
            'project_ref': plan_doc.get('project_ref'),
            'room_label': plan_doc.get('room_name'),
        }
        for key, payload_value in payload_fields.items():
            if payload_value is None:
                continue
            declared = descriptor.get(key)
            if declared is not None and declared != payload_value:
                raise CaptureReceiverError(
                    f'descriptor {key} diverges from the package payload'
                )
            descriptor[key] = payload_value
        descriptor['package_id'] = package_id
        descriptor.setdefault('byte_size', len(payload))
        descriptor.setdefault('package_sha256', _sha256_text(payload))
        package = MissionPackage(
            package_id=package_id,
            mission_id=descriptor.get('mission_id'),
            purpose=descriptor.get('purpose'),
            project_ref=descriptor.get('project_ref'),
            room_label=descriptor.get('room_label'),
            issued_at_utc=descriptor.get('issued_at') or _utc_now(),
            supersedes_package_id=descriptor.get('supersedes_package_id'),
            package_sha256=descriptor['package_sha256'],
            byte_size=int(descriptor['byte_size']),
            required_schema_version=descriptor.get('required_schema_version'),
            receiver_requirement=descriptor.get('receiver_requirement'),
            pairing_id=pairing_id,
        )
        if package.package_sha256 != _sha256_text(payload):
            raise CaptureReceiverError(
                'declared package digest does not match the bytes'
            )
        if package.byte_size != len(payload):
            raise CaptureReceiverError(
                'declared package size does not match the bytes'
            )
        with closing(self._connect()) as connection, connection:
            if pairing_id is not None:
                pairing_row = connection.execute(
                    'SELECT state FROM capture_receiver_pairings '
                    'WHERE pairing_id=?',
                    (pairing_id,),
                ).fetchone()
                if pairing_row is None or pairing_row['state'] not in (
                    'offered',
                    'active',
                ):
                    raise CaptureReceiverError(
                        'mission package pairing is unknown or no '
                        'longer deliverable'
                    )
            existing = connection.execute(
                'SELECT payload_sha256, status, pairing_id FROM '
                'capture_mission_packages WHERE package_id=?',
                (package_id,),
            ).fetchone()
            if (
                existing is not None
                and existing['payload_sha256'] == package.package_sha256
                and existing['pairing_id'] == pairing_id
            ):
                # Byte-identical re-queue under the same scope is a
                # no-op: returning early keeps a settled verdict
                # instead of resetting the row to 'pending'.
                return package
            if (
                existing is not None
                and existing['status'] != 'pending'
                and (
                    existing['payload_sha256'] != package.package_sha256
                    or existing['pairing_id'] != pairing_id
                )
            ):
                raise CaptureReceiverError(
                    'mission package already settled under this '
                    'identity — re-issue under a new package id instead '
                    'of rewriting a settled verdict'
                )
            store_content_blob(connection, payload)
            connection.execute(
                '''
                INSERT OR REPLACE INTO capture_mission_packages(
                    package_id, pairing_id, descriptor_json, payload_sha256,
                    byte_size, status, status_detail, native_package_json,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT created_at_utc FROM capture_mission_packages
                     WHERE package_id=?), ?), ?)
                ''',
                (
                    package.package_id,
                    pairing_id,
                    _canonical_json(package.descriptor()),
                    package.package_sha256,
                    package.byte_size,
                    'pending',
                    '',
                    native_package_json,
                    package_id,
                    _utc_now(),
                    _utc_now(),
                ),
            )
            if package.supersedes_package_id:
                # A repair mission takes its parent's place on the
                # pending listing — settle the parent without erasing
                # a verdict the device already reported.
                connection.execute(
                    "UPDATE capture_mission_packages SET "
                    "status='superseded', status_detail=?, "
                    "updated_at_utc=? WHERE package_id=? AND "
                    "status='pending'",
                    (
                        f'superseded by {package.package_id}',
                        _utc_now(),
                        package.supersedes_package_id,
                    ),
                )
            # A mission queued AFTER the device already confirmed a
            # settled child superseded it must not go pending — the
            # settle paths above only fire while the supersession is
            # in flight, so a re-issued parent would sit on the pull
            # listing as alive forever otherwise.
            superseding = connection.execute(
                "SELECT package_id FROM capture_mission_packages "
                "WHERE status IN ('received', 'completed') AND "
                "json_extract(descriptor_json, "
                "'$.supersedes_package_id')=?",
                (package_id,),
            ).fetchone()
            if superseding is not None:
                connection.execute(
                    "UPDATE capture_mission_packages SET "
                    "status='superseded', status_detail=?, "
                    "updated_at_utc=? WHERE package_id=? AND "
                    "status='pending'",
                    (
                        f'superseded by {superseding["package_id"]}',
                        _utc_now(),
                        package_id,
                    ),
                )
        return package

    def queue_mission(
        self,
        package: CaptureMissionPackage,
        *,
        pairing_id: str | None = None,
    ) -> MissionPackage:
        """Queue a native mission package for the pull lane.

        Projects the hash-pinned ``htdt.capture.mission-package`` onto
        the ``htdt.capture-mission`` envelope the Capture app decodes
        (see ``capture_mission.mission_package_wire_payload``), then
        queues it under the mission id so a re-issued mission replaces
        its pending package and a repair mission's supersession names
        its parent by the same identity.
        """

        return self.queue_mission_package(
            package.mission.mission_id,
            mission_package_wire_payload(package),
            descriptor=mission_package_descriptor(package),
            pairing_id=pairing_id,
            native_package_json=_canonical_json(
                package.model_dump(mode='json')
            ),
        )

    def native_mission_package(
        self, package_id: str
    ) -> CaptureMissionPackage | None:
        """The native ``htdt.capture.mission-package`` queued under this id,
        when the package was issued through ``queue_mission`` — ``None``
        for foreign-issued or pre-existing wire-only packages."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT native_package_json FROM capture_mission_packages '
                'WHERE package_id=?',
                (package_id,),
            ).fetchone()
        if row is None or row['native_package_json'] is None:
            return None
        return CaptureMissionPackage.model_validate_json(
            row['native_package_json']
        )

    def mission_package_bytes(self, package_id: str) -> bytes | None:
        """Exact package bytes — used by the pull endpoint and file export."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_sha256 FROM capture_mission_packages '
                'WHERE package_id=?',
                (package_id,),
            ).fetchone()
            if row is None:
                return None
            return read_content_blob(connection, row['payload_sha256'])

    def _mission_from_row(self, row: sqlite3.Row) -> MissionPackage:
        descriptor = json.loads(row['descriptor_json'])
        return MissionPackage(
            package_id=row['package_id'],
            mission_id=descriptor.get('mission_id'),
            purpose=descriptor.get('purpose'),
            project_ref=descriptor.get('project_ref'),
            room_label=descriptor.get('room_label'),
            issued_at_utc=descriptor.get('issued_at'),
            supersedes_package_id=descriptor.get('supersedes_package_id'),
            package_sha256=row['payload_sha256'],
            byte_size=int(row['byte_size']),
            required_schema_version=descriptor.get('required_schema_version'),
            receiver_requirement=descriptor.get('receiver_requirement'),
            pairing_id=row['pairing_id'],
            status=row['status'],
            status_detail=row['status_detail'],
            updated_at_utc=row['updated_at_utc'],
        )

    def handle_mission_listing(
        self, token: str, capture_instance_id: str | None
    ) -> tuple[int, dict]:
        """``GET missions`` — metadata-only pending listing for this device."""
        try:
            pairing = self._require_active_pairing(token)
        except CaptureReceiverError:
            return 404, {'detail': 'unknown endpoint'}
        if not capture_instance_id:
            return 400, {'detail': 'X-HTDT-Capture-Instance-ID required'}
        bound = self._bind_capture_instance(pairing, capture_instance_id)
        if bound is not None:
            return bound
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM capture_mission_packages WHERE status='pending' "
                'AND (pairing_id IS NULL OR pairing_id=?) '
                'ORDER BY created_at_utc, package_id',
                (pairing.pairing_id,),
            ).fetchall()
        # ORDER BY keeps the listing's canonical bytes stable across
        # identical queue contents — an unordered SELECT would emit a
        # different canonical JSON (and a different digest/byte size)
        # whenever SQLite reorders the scan.
        packages = [
            self._mission_from_row(row).descriptor() for row in rows
        ]
        listing = {
            'schema': MISSION_LISTING_SCHEMA,
            'schema_version': MISSION_LISTING_SCHEMA_VERSION,
            'capture_instance_id': capture_instance_id,
            'packages': packages,
        }
        if len(_canonical_json(listing).encode('utf-8')) > (
            MISSION_LISTING_MAX_BYTES
        ):
            return 413, {'detail': 'listing exceeds the receive ceiling'}
        return 200, listing

    def _bind_capture_instance(
        self, pairing: ReceiverPairing, capture_instance_id: str
    ) -> tuple[int, dict] | None:
        """Bind the first-seen Capture identity to a pairing; refuse others."""
        if (
            pairing.capture_instance_id is not None
            and pairing.capture_instance_id != capture_instance_id
        ):
            return 403, {
                'detail': 'pairing is bound to a different capture instance'
            }
        if pairing.capture_instance_id != capture_instance_id:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    'UPDATE capture_receiver_pairings '
                    'SET capture_instance_id=? WHERE pairing_id=?',
                    (capture_instance_id, pairing.pairing_id),
                )
        return None

    def handle_mission_package(
        self, token: str, package_id: str, capture_instance_id: str | None
    ) -> tuple[int, bytes | dict]:
        """``GET missions/{package_id}`` — the exact package bytes."""
        try:
            pairing = self._require_active_pairing(token)
        except CaptureReceiverError:
            return 404, {'detail': 'unknown endpoint'}
        if not capture_instance_id:
            return 400, {'detail': 'X-HTDT-Capture-Instance-ID required'}
        bound = self._bind_capture_instance(pairing, capture_instance_id)
        if bound is not None:
            return bound
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM capture_mission_packages WHERE package_id=?',
                (package_id,),
            ).fetchone()
            if row is None:
                return 404, {'detail': 'unknown package'}
            package = self._mission_from_row(row)
            if package.pairing_id and package.pairing_id != pairing.pairing_id:
                return 404, {'detail': 'package not offered to this pairing'}
            if package.status == 'superseded':
                # Listing→download race: a superseded package leaves the
                # listing but its bytes still name a stale mission —
                # serving them lets the device import a plan the issuer
                # replaced. 'received'/'failed'/'completed' stay
                # servable: verdict-settled bytes are idempotent, and a
                # failed transfer must stay retryable.
                return 410, {'detail': 'package superseded by a re-issue'}
            payload = read_content_blob(connection, package.package_sha256)
        if payload is None:
            return 500, {'detail': 'package payload missing'}
        return 200, payload

    def handle_mission_receipt(
        self,
        token: str,
        package_id: str,
        body: bytes,
        capture_instance_id: str | None,
    ) -> tuple[int, dict]:
        """``POST missions/{package_id}/receipt`` — record the receive verdict."""
        try:
            pairing = self._require_active_pairing(token)
        except CaptureReceiverError:
            return 404, {'detail': 'unknown endpoint'}
        if not capture_instance_id:
            return 400, {'detail': 'X-HTDT-Capture-Instance-ID required'}
        bound = self._bind_capture_instance(pairing, capture_instance_id)
        if bound is not None:
            return bound
        try:
            receipt = json.loads(body.decode('utf-8'))
        except Exception:
            return 400, {'detail': 'receipt is not valid JSON'}
        if not isinstance(receipt, dict):
            return 400, {'detail': 'receipt is not a JSON object'}
        if receipt.get('schema') != MISSION_RECEIPT_SCHEMA:
            return 400, {'detail': 'unexpected receipt schema'}
        if receipt.get('schema_version') != MISSION_RECEIPT_SCHEMA_VERSION:
            return 400, {'detail': 'unsupported receipt schema version'}
        if receipt.get('package_id') != package_id:
            return 400, {'detail': 'receipt package mismatch'}
        if receipt.get('capture_instance_id') != capture_instance_id:
            return 400, {'detail': 'receipt device mismatch'}
        # ``paired_destination_id`` names the device-local destination
        # the app bound at pairing time — a UUID minted on-device that
        # this service never learns. The pairing token in the URL
        # already authenticates which pairing the receipt reports for,
        # so there is no server-side identity to compare it against.
        if (
            receipt.get('receiver_instance_id') is not None
            and receipt['receiver_instance_id'] != pairing.receiver_instance_id
        ):
            return 400, {'detail': 'receipt receiver mismatch'}
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM capture_mission_packages WHERE package_id=?',
                (package_id,),
            ).fetchone()
            if row is None:
                return 404, {'detail': 'unknown package'}
            package = self._mission_from_row(row)
            if package.pairing_id and package.pairing_id != pairing.pairing_id:
                return 404, {'detail': 'package not offered to this pairing'}
            # Receipts echo the descriptor's ``sha256:<hex>`` digest;
            # the legacy bare-hex form is accepted for older emitters.
            reported_digest = receipt.get('package_sha256')
            if isinstance(reported_digest, str) and reported_digest.startswith(
                'sha256:'
            ):
                reported_digest = reported_digest[len('sha256:'):]
            if reported_digest != package.package_sha256:
                return 400, {'detail': 'receipt digest mismatch'}
            result = receipt.get('validation_result')
            detail = receipt.get('detail')
            # Both report fields are text on the wire: anything else is a
            # malformed claim, not a verdict — and non-strings cannot be
            # bound to the status_detail column anyway.
            if not isinstance(result, str) or (
                detail is not None and not isinstance(detail, str)
            ):
                return 400, {'detail': 'receipt verdict fields must be strings'}
            status = (
                'received'
                if result in ('imported', 'duplicate', 'superseding')
                else 'failed'
            )
            if (
                result == 'superseding'
                and package.supersedes_package_id
            ):
                # The device reports this package replaced its parent —
                # settle the parent whether this receipt is first, an
                # idempotent re-send, or a conflict on a settled row:
                # the parent's pending zombie is dead weight in every
                # case, and the settle never overwrites a verdict.
                connection.execute(
                    "UPDATE capture_mission_packages SET "
                    "status='superseded', status_detail=?, "
                    "updated_at_utc=? WHERE package_id=? AND "
                    "status='pending'",
                    (
                        f'superseded by {package.package_id}',
                        _utc_now(),
                        package.supersedes_package_id,
                    ),
                )
            if package.status != 'pending':
                # A verdict is settled state: a first receipt that
                # agrees is idempotent; a receipt reporting a
                # different outcome on an already-settled package is
                # a conflict, never a last-write-wins overwrite.
                if package.status == status:
                    return 200, {'ok': True}
                return 409, {
                    'detail': 'package verdict already settled'
                }
            connection.execute(
                'UPDATE capture_mission_packages SET status=?, '
                'status_detail=?, updated_at_utc=? WHERE package_id=?',
                (
                    status,
                    detail or result,
                    _utc_now(),
                    package_id,
                ),
            )
        return 200, {'ok': True}

    def list_mission_packages(
        self, status: str | None = None
    ) -> tuple[MissionPackage, ...]:
        sql = 'SELECT * FROM capture_mission_packages'
        args: tuple = ()
        if status:
            sql += ' WHERE status=?'
            args = (status,)
        sql += ' ORDER BY updated_at_utc DESC, package_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, args).fetchall()
            return tuple(self._mission_from_row(row) for row in rows)

    def export_mission_package(self, package_id: str, destination: Path) -> Path:
        """File-based fallback: write the package bytes with unchanged identity."""
        payload = self.mission_package_bytes(package_id)
        if payload is None:
            raise CaptureReceiverError('unknown mission package')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Atomic publish: a failed write never leaves a truncated package.
        write_bytes_atomic(destination, payload)
        return destination

    # -- HTTP lifecycle ---------------------------------------------------

    def start(self, host: str | None = None, port: int | None = None) -> int:
        """Bind the HTTPS endpoint. Returns the bound port."""
        if self._server is not None:
            raise CaptureReceiverError('receiver already running')
        config = self.get_config()
        host = host or config.host
        port = config.port if port is None else port
        cert_pem, key_pem = self._ensure_certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert_path = self._data_dir / 'receiver-cert.pem'
        key_path = self._data_dir / 'receiver-key.pem'
        if not (cert_path.exists() and key_path.exists()):
            write_bytes_atomic(cert_path, cert_pem)
            write_bytes_atomic(key_path, key_pem)
        try:
            context.load_cert_chain(str(cert_path), str(key_path))
        except ssl.SSLError:
            # A credential that cannot load at all is unrecoverable state
            # (e.g. an interrupted write from an older build); regenerate
            # once instead of failing every subsequent start.
            cert_path.unlink(missing_ok=True)
            key_path.unlink(missing_ok=True)
            cert_pem, key_pem = self._ensure_certificate()
            context.load_cert_chain(str(cert_path), str(key_path))
        handler = _make_handler(self)
        server = ThreadingHTTPServer((host, port), handler)
        server.daemon_threads = True
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._server_thread = threading.Thread(
            target=server.serve_forever, daemon=True, name='capture-receiver'
        )
        self._server_thread.start()
        self.set_enabled(True)
        if self._base_url_override is None:
            self.set_port(server.server_address[1])
        return server.server_address[1]

    def stop(self) -> None:
        """Clean shutdown: stop serving and close the socket."""
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
            self._server_thread = None
        self.set_enabled(False)

    @property
    def running(self) -> bool:
        return self._server is not None


def _default_bundle_reader(
    payload: bytes,
) -> tuple[CaptureIngestionPlan, Mapping[str, bytes]]:
    """Resolve the archive bytes to an ingestion plan via the import path.

    Mirrors the read/manifest/validate/plan stages of
    ``import_capture_artifact`` without the commit: ``handle_delivery``
    owns the ingest call so delivery rejections never write rows. When
    the .htdtcapture import pipeline is unavailable the delivery is
    rejected with a clear reason instead of silently parsed.
    """
    import tempfile

    try:
        from .capture_bundle import FrozenBundle
        from .capture_reference import build_ingestion_plan
    except ImportError as exc:
        raise CaptureReceiverError(
            'no capture bundle reader is available on this build; '
            'the .htdtcapture import pipeline is required'
        ) from exc
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'delivery.htdtcapture'
        path.write_bytes(payload)
        frozen = FrozenBundle(path)
        manifest_bytes = frozen.manifest_bytes
        if sha256(manifest_bytes).hexdigest() != frozen.report['bundle_digest']:
            raise CaptureReceiverError(
                'manifest SHA-256 does not equal the bundle digest'
            )
        manifest_document = json.loads(manifest_bytes)
        plan = build_ingestion_plan(frozen)
        payloads = {
            entry['path']: frozen.read(entry['path'])
            for entry in manifest_document['files']
        }
    return plan, payloads, manifest_bytes


def _make_handler(service: CaptureReceiverService):
    prefix = RECEIVER_PATH_PREFIX + '/'

    class ReceiverHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        # http.server applies this in setup() as a per-socket-operation
        # timeout: a stalled or idle connection gives up its handler thread
        # after the window instead of pinning it forever (slow-loris).
        timeout = RECEIVER_SOCKET_TIMEOUT_SECONDS

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _respond(self, status: int, content_type: str, payload: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                if self.close_connection:
                    self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                # The peer went away mid-response; there is nobody left to
                # report the failure to, so just drop the connection.
                self.close_connection = True

        def _json(self, status: int, body: dict) -> None:
            self._respond(status, 'application/json', json.dumps(body).encode('utf-8'))

        def _declares_body(self) -> bool:
            """True when the request announces a body this handler does not read."""
            if self.headers.get('Transfer-Encoding'):
                return True
            raw = self.headers.get('Content-Length')
            if raw is None:
                return False
            try:
                return int(raw) > 0
            except ValueError:
                return True

        def _route(self) -> tuple[str, str, str] | None:
            """→ (token, resource, suffix) for paired routes, else None."""
            parsed = urlparse(self.path)
            if not parsed.path.startswith(prefix):
                return None
            remainder = parsed.path[len(prefix):]
            token, _, resource = remainder.partition('/')
            resource_name, _, suffix = resource.partition('/')
            return token, resource_name, suffix

        def do_GET(self) -> None:  # noqa: N802
            if self._declares_body():
                # No GET route consumes a body: answer, then close so the
                # leftover bytes cannot be parsed as the next request on
                # this kept-alive connection.
                self.close_connection = True
            route = self._route()
            if route is None:
                self._json(404, {'detail': 'unknown endpoint'})
                return
            token, resource, suffix = route
            if resource == 'capabilities' and not suffix:
                document = service.capabilities_document(token)
                if document is None:
                    self._json(404, {'detail': 'unknown endpoint'})
                else:
                    self._json(200, document)
                return
            capture_instance_id = self.headers.get(
                'X-HTDT-Capture-Instance-ID'
            )
            if resource == 'missions':
                if suffix:
                    status, body = service.handle_mission_package(
                        token, suffix, capture_instance_id
                    )
                    if isinstance(body, (bytes, bytearray)):
                        self._respond(status, 'application/octet-stream', bytes(body))
                    else:
                        self._json(status, body)
                else:
                    status, body = service.handle_mission_listing(
                        token,
                        capture_instance_id
                        or parse_qs(urlparse(self.path).query).get(
                            'capture_instance_id', [None]
                        )[0],
                    )
                    self._json(status, body)
                return
            self._json(404, {'detail': 'unknown endpoint'})

        def do_POST(self) -> None:  # noqa: N802
            route = self._route()
            if route is None:
                if self._declares_body():
                    self.close_connection = True
                self._json(404, {'detail': 'unknown endpoint'})
                return
            token, resource, suffix = route
            is_receipt = resource == 'missions' and suffix.endswith('/receipt')
            ceiling = (
                RECEIVER_MAX_RECEIPT_BYTES
                if is_receipt
                else service.max_archive_bytes
            )
            if self.headers.get('Transfer-Encoding'):
                # Framed bodies are not decoded here: answering without
                # reading them would desync the next request on this
                # kept-alive connection, so answer once and close.
                self.close_connection = True
                self._json(400, {
                    'detail': 'Transfer-Encoding is not supported; send Content-Length'
                })
                return
            raw_length = self.headers.get('Content-Length')
            try:
                length = int(raw_length) if raw_length is not None else 0
            except ValueError:
                self.close_connection = True
                self._json(400, {'detail': 'invalid Content-Length'})
                return
            if length < 0:
                self.close_connection = True
                self._json(400, {'detail': 'invalid Content-Length'})
                return
            if length > ceiling:
                self.close_connection = True
                self._json(413, {'detail': 'request exceeds byte ceiling'})
                return
            body = self.rfile.read(length) if length else b''
            if len(body) != length:
                # The peer went away mid-transfer: never feed a truncated
                # payload to the delivery or receipt handlers.
                self.close_connection = True
                self._json(400, {'detail': 'incomplete request body'})
                return
            headers = {key: value for key, value in self.headers.items()}
            if resource == 'deliveries' and not suffix:
                status, response = service.handle_delivery(
                    token, headers, body
                )
                self._json(status, response)
                return
            if resource == 'missions' and suffix.endswith('/receipt'):
                package_id, _, _leaf = suffix.partition('/receipt')
                status, response = service.handle_mission_receipt(
                    token,
                    package_id,
                    body,
                    self.headers.get('X-HTDT-Capture-Instance-ID'),
                )
                self._json(status, response)
                return
            self._json(404, {'detail': 'unknown endpoint'})

    return ReceiverHandler
