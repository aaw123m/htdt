"""Application-scoped Capture receiver lifecycle owner (#926).

One ``CaptureReceiverService`` is owned by the application data root — never
per project window. The ``integrations.capture_receiver_enabled``
ApplicationPreference is the user-requested policy; the durable
``capture_receiver_config`` row remains the receiver's operational record —
``start``/``stop`` on the service keep it in sync with the actual state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .application_preferences import ApplicationPreferenceStore
from .cad_repository import SceneRepository
from .capture_receiver import CaptureReceiverService, ReceiverDeliveryRecord


PREFERENCE_KEY = 'integrations.capture_receiver_enabled'

_LOGGER = logging.getLogger('htdt.capture_receiver')


class CaptureReceiverController(QObject):
    """Composes the native Capture receiver into the workflow app.

    Lifecycle policy:

    - ``start_if_requested()`` after the main window is shown: when the
      preference asks for the receiver the service starts; a bind or
      certificate failure is recorded in ``last_error`` and never aborts
      project access.
    - ``set_enabled`` from Settings flips the requested policy and applies
      the effect immediately.
    - ``shutdown()`` on app exit stops the server without changing the
      requested policy — a crash or failed start cannot silently flip the
      user's choice.
    """

    changed = Signal()
    delivery_staged = Signal(object)

    def __init__(
        self,
        scene_repository: SceneRepository,
        preferences: ApplicationPreferenceStore,
        *,
        service: CaptureReceiverService | None = None,
    ) -> None:
        super().__init__()
        self.preferences = preferences
        if service is None:
            service = CaptureReceiverService(
                scene_repository,
                data_dir=(
                    Path(scene_repository.path).parent / 'capture-receiver'
                ),
                delivery_listener=self._on_delivery,
            )
        self.service = service
        self.last_error: str | None = None

    # -- requested policy ------------------------------------------------

    @property
    def requested_enabled(self) -> bool:
        return bool(self.preferences.get(PREFERENCE_KEY))

    @property
    def running(self) -> bool:
        return self.service.running

    def set_enabled(self, enabled: bool) -> str | None:
        """Flip the requested policy; returns an error detail on failure."""
        self.preferences.set(PREFERENCE_KEY, bool(enabled))
        if enabled:
            return self._start()
        self.service.stop()
        self.last_error = None
        self.changed.emit()
        return None

    def set_port(self, port: int) -> str | None:
        """Update the durable port; re-binds live when already running."""
        self.service.set_port(int(port))
        if self.running:
            self.service.stop()
            return self._start()
        self.changed.emit()
        return None

    # -- lifecycle --------------------------------------------------------

    def start_if_requested(self) -> str | None:
        """Apply the persisted policy at app startup; failures stay soft."""
        if not self.requested_enabled:
            return None
        return self._start()

    def _start(self) -> str | None:
        try:
            self.service.start()
        except Exception as exc:
            self.last_error = str(exc)
            _LOGGER.warning('capture receiver failed to start: %s', exc)
            self.changed.emit()
            return self.last_error
        self.last_error = None
        self.changed.emit()
        return None

    def shutdown(self) -> None:
        """Stop the socket on app exit without changing requested policy."""
        try:
            self.service.stop()
        except Exception as exc:
            _LOGGER.warning('capture receiver failed to stop: %s', exc)

    # -- status -----------------------------------------------------------

    def status_lines(self) -> tuple[str, ...]:
        """Effective receiver state for Settings/Support surfaces."""
        try:
            config = self.service.get_config()
        except Exception:
            config = None
        if self.running:
            state = '有効（待受中）'
        elif self.requested_enabled and self.last_error:
            state = '起動失敗'
        elif self.requested_enabled:
            state = '有効（停止中）'
        else:
            state = '無効'
        lines = [f'キャプチャ受信: {state}']
        if config is not None:
            lines.append(f'待受: {config.host}:{config.port}')
            lines.append(f'受信者ID: {config.receiver_instance_id}')
            try:
                active = sum(
                    1
                    for pairing in self.service.list_pairings()
                    if pairing.state == 'active'
                )
            except Exception:
                active = 0
            lines.append(f'ペアリング済みデバイス: {active} 台')
        if self.last_error:
            lines.append(f'起動エラー: {self.last_error}')
        return tuple(lines)

    # -- events ------------------------------------------------------------

    def _on_delivery(self, record: ReceiverDeliveryRecord) -> None:
        # Runs on the HTTP server thread; Qt queues it onto the UI thread.
        self.delivery_staged.emit(record)
