"""Application-scoped Capture receiver composition (issue #926).

The pairing/LAN service already existed; what regressed was the shipped
workflow app composing it. These tests pin the lifecycle: requested
policy lives in `integrations.capture_receiver_enabled`, the durable
receiver record tracks actual state, startup failure is soft and
diagnosable, deliveries announce, and exit stops the socket.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
from hashlib import sha256
import tempfile
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_fixture_support as support  # noqa: E402

from htdt.application_preferences import ApplicationPreferenceStore  # noqa: E402
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_receiver_controller import (  # noqa: E402
    PREFERENCE_KEY,
    CaptureReceiverController,
)
from htdt.workflow_application import WorkflowApplicationComposition  # noqa: E402


SERIES_ID = '10000000-0000-4000-8000-000000000001'
SESSION_ID = '10000000-0000-4000-8000-000000000003'
SPACE_ID = '10000000-0000-4000-8000-000000000004'
ANCHOR_ID = '10000000-0000-4000-8000-000000000005'


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _rig(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'data' / 'cad-scenes.sqlite3')
    preferences = ApplicationPreferenceStore.for_data_dir(
        tmp_path / 'data'
    )
    controller = CaptureReceiverController(repository, preferences)
    return repository, preferences, controller


class _Reader:
    """Bundle reader stand-in: maps archive bytes to a prebuilt plan."""

    def __init__(self) -> None:
        self.plans: dict[str, tuple[dict, dict]] = {}

    def register(self, archive: bytes, plan: dict, payloads: dict) -> None:
        self.plans[sha256(archive).hexdigest()] = (plan, payloads)

    def __call__(self, body: bytes):
        return self.plans[sha256(body).hexdigest()]


def _plan_and_payloads():
    revision_id = str(uuid.uuid4())
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
            'created_at': '2026-09-20T00:00:00Z',
            'finalized_at': '2026-09-20T00:00:00Z',
        },
        id_map={
            support.SESSION_ID: SESSION_ID,
            support.SPACE_ID: SPACE_ID,
        },
    )
    return plan, payloads


def _delivery_headers(archive: bytes) -> dict:
    return {
        'Content-Type': 'application/vnd.htdt.capture-bundle',
        'X-HTDT-Artifact-Kind': 'capture_bundle',
        'X-HTDT-Archive-SHA256': sha256(archive).hexdigest(),
        'X-HTDT-Archive-Bytes': str(len(archive)),
    }


class TestLifecycle:
    def test_disabled_default_opens_no_listener(self, tmp_path: Path):
        _app()
        _, _prefs, controller = _rig(tmp_path)
        assert controller.requested_enabled is False
        assert controller.service.running is False
        assert controller.start_if_requested() is None
        assert controller.service.running is False
        assert controller.last_error is None

    def test_enabled_preference_starts_and_shutdown_stops(
        self, tmp_path: Path
    ):
        _app()
        _, preferences, controller = _rig(tmp_path)
        preferences.set(PREFERENCE_KEY, True)
        try:
            assert controller.start_if_requested() is None
            assert controller.service.running is True
            # The durable receiver record reflects the actual state.
            assert controller.service.get_config().enabled is True
        finally:
            controller.shutdown()
        assert controller.service.running is False
        # Shutdown never flips the user's requested policy.
        assert controller.requested_enabled is True

    def test_settings_toggle_round_trips_policy_and_state(
        self, tmp_path: Path
    ):
        _app()
        _, preferences, controller = _rig(tmp_path)
        try:
            assert controller.set_enabled(True) is None
            assert controller.service.running is True
            assert preferences.get(PREFERENCE_KEY) is True
            # A reopened store still requests the receiver — the
            # preference is the persisted policy, not a session flag.
            reopened = ApplicationPreferenceStore(preferences.path)
            assert reopened.get(PREFERENCE_KEY) is True
            assert controller.set_enabled(False) is None
            assert controller.service.running is False
            assert controller.service.get_config().enabled is False
        finally:
            controller.shutdown()

    def test_bind_failure_is_soft_and_diagnosable(self, tmp_path: Path):
        _app()
        _, preferences, controller = _rig(tmp_path)
        blocker = socket.socket()
        # The receiver binds 0.0.0.0: an occupied wildcard port guarantees
        # EADDRINUSE on Windows and POSIX alike.
        blocker.bind(('0.0.0.0', 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        controller.service.set_port(port)
        preferences.set(PREFERENCE_KEY, True)
        try:
            error = controller.start_if_requested()
            assert error
            assert controller.last_error == error
            assert controller.service.running is False
            lines = controller.status_lines()
            assert any('起動' in line for line in lines)
            assert any(controller.last_error in line for line in lines)
        finally:
            blocker.close()
            controller.shutdown()
        # Recovery: once the port frees, the same controller starts.
        assert controller.start_if_requested() is None
        assert controller.service.running is True
        assert controller.last_error is None
        controller.shutdown()


class TestDeliveryAnnounce:
    def test_accepted_delivery_stages_inbox_and_fires_signal(
        self, tmp_path: Path
    ):
        _app()
        _, _preferences, controller = _rig(tmp_path)
        reader = _Reader()
        # Same wiring the controller's own ctor installs.
        assert (
            controller.service._delivery_listener
            == controller._on_delivery
        )
        controller.service.bundle_reader = reader
        records = []
        controller.delivery_staged.connect(records.append)

        pairing, _payload = controller.service.begin_pairing(
            project_ref='doc-canonical'
        )
        controller.service.confirm_pairing(pairing.pairing_id)
        plan, payloads = _plan_and_payloads()
        archive = b'archive-composition-1'
        reader.register(archive, plan, payloads)

        status, receipt = controller.service.handle_delivery(
            pairing.pairing_token, _delivery_headers(archive), archive
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'accepted'
        assert len(records) == 1
        assert records[0].staging_ref == receipt['staging_ref']
        staged = controller.service.inbox_repository.list_items()
        assert len(staged) == 1
        assert staged[0].scope == 'doc-canonical'

        # Exact redelivery stays idempotent and does not re-announce.
        status, receipt = controller.service.handle_delivery(
            pairing.pairing_token, _delivery_headers(archive), archive
        )
        assert status == 200
        assert receipt['ingestion_outcome'] == 'already_staged'
        assert len(records) == 1


class TestWorkflowComposition:
    def test_composition_exposes_capture_destination_and_support_status(
        self, tmp_path: Path
    ):
        _app()
        repository, _preferences, controller = _rig(tmp_path)
        composition = WorkflowApplicationComposition(
            repository, 'document-1', capture_receiver=controller
        )
        try:
            assert composition.capture_receiver is controller
            assert (
                composition._open_settings_destination('settings.capture')
                is True
            )
            composition.settings_dialog.close()
            lines = controller.status_lines()
            assert lines
        finally:
            controller.shutdown()

    def test_capture_destination_absent_without_receiver(
        self, tmp_path: Path
    ):
        _app()
        repository = SceneRepository(tmp_path / 'cad.sqlite3')
        composition = WorkflowApplicationComposition(
            repository, 'document-1'
        )
        assert composition.capture_receiver is None
        assert (
            composition._open_settings_destination('settings.capture')
            is False
        )
