from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import struct
from urllib.request import Request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from htdt.ingress import IngressTooLargeError, read_file_bounded, read_response_bounded, read_stream_bounded
from htdt.limits import (
    MAX_NATIVE_REW_TEXT_FILE_BYTES,
    MAX_REW_API_RESPONSE_BYTES,
    MAX_REW_ARRAY_BYTES,
    MAX_REW_ARRAY_SAMPLES,
    MAX_REW_TEXT_BYTES,
    max_base64_chars,
)
from htdt.rew_api import (
    RewApiClient,
    RewApiError,
    RewApiResponseTooLarge,
    decode_rew_float_array,
)
from htdt.rew_parser import parse_rew_frequency_response


def encode(values: list[float]) -> str:
    return base64.b64encode(struct.pack(f'>{len(values)}f', *values)).decode()


# ----------------------------------------------------------------------
# limits: browser and native ingress are one resource-safety policy


def test_native_and_rew_limits_align_with_browser_policy() -> None:
    assert MAX_NATIVE_REW_TEXT_FILE_BYTES == MAX_REW_TEXT_BYTES
    assert MAX_REW_API_RESPONSE_BYTES == MAX_REW_TEXT_BYTES
    assert MAX_REW_ARRAY_BYTES == 4 * MAX_REW_ARRAY_SAMPLES
    # Two capped arrays (magnitude + phase) plus the JSON envelope fit inside
    # the response ceiling, so ordinary REW responses are never constrained.
    assert MAX_REW_API_RESPONSE_BYTES >= 2 * max_base64_chars(MAX_REW_ARRAY_BYTES)


# ----------------------------------------------------------------------
# read_file_bounded


def test_read_file_bounded_accepts_file_at_limit(tmp_path: Path) -> None:
    target = tmp_path / 'boundary.txt'
    payload = b'x' * 64
    target.write_bytes(payload)
    assert read_file_bounded(target, 64, label='REW text file') == payload


def test_read_file_bounded_rejects_one_byte_over_limit(tmp_path: Path) -> None:
    target = tmp_path / 'too-big.txt'
    target.write_bytes(b'x' * 65)
    with pytest.raises(IngressTooLargeError, match='too large'):
        read_file_bounded(target, 64, label='REW text file')


def test_read_file_bounded_rejects_before_opening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / 'huge.txt'
    target.write_bytes(b'x' * 1024)
    opened: list[Path] = []
    real_open = Path.open

    def spy(self: Path, *args: object, **kwargs: object):
        opened.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', spy)
    with pytest.raises(IngressTooLargeError):
        read_file_bounded(target, 16, label='REW text file')
    assert opened == []


def test_read_file_bounded_catches_growth_between_stat_and_read() -> None:
    import io
    import types

    class GrowingPath:
        name = 'growing.txt'

        def stat(self) -> object:
            return types.SimpleNamespace(st_size=8)

        def open(self, mode: str) -> io.BytesIO:
            return io.BytesIO(b'x' * 64)

    with pytest.raises(IngressTooLargeError):
        read_file_bounded(GrowingPath(), 16, label='REW text file')  # type: ignore[arg-type]


def test_read_file_bounded_native_rew_limit_boundary(tmp_path: Path) -> None:
    target = tmp_path / 'rew.txt'
    target.write_bytes(b'0' * MAX_NATIVE_REW_TEXT_FILE_BYTES)
    assert len(read_file_bounded(target, MAX_NATIVE_REW_TEXT_FILE_BYTES)) == MAX_NATIVE_REW_TEXT_FILE_BYTES
    target.write_bytes(b'0' * (MAX_NATIVE_REW_TEXT_FILE_BYTES + 1))
    with pytest.raises(IngressTooLargeError):
        read_file_bounded(target, MAX_NATIVE_REW_TEXT_FILE_BYTES)


# ----------------------------------------------------------------------
# REW text parser: decoded payload ceiling shared with the browser policy


def test_parser_rejects_payload_over_byte_ceiling() -> None:
    raw = b'20 70\n40 71\n'
    parsed = parse_rew_frequency_response(raw, max_bytes=len(raw))
    assert len(parsed.frequency_hz) == 2
    with pytest.raises(IngressTooLargeError, match='too large'):
        parse_rew_frequency_response(raw, max_bytes=len(raw) - 1)


def test_parser_accepts_ordinary_rew_text() -> None:
    parsed = parse_rew_frequency_response(b'Frequency SPL\n20 70.0\n40 71.5\n')
    assert parsed.frequency_hz == (20.0, 40.0)


def test_workflow_stage_rejects_oversized_staged_bytes(tmp_path: Path) -> None:
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene
    from htdt.measurement_workflow import MeasurementWorkflowController

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    controller = MeasurementWorkflowController(scene_repository, revision.document_id)

    with pytest.raises(IngressTooLargeError):
        controller.stage_rew_text(b'0' * (MAX_REW_TEXT_BYTES + 1), 'huge.txt')
    assert controller.pending_import is None


# ----------------------------------------------------------------------
# read_response_bounded


class FakeResponse:
    def __init__(self, raw: bytes, *, content_length: int | None = None) -> None:
        self.raw = raw
        self.content_length = content_length
        self.reads = 0

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getheader(self, name: str) -> str | None:
        if name == 'Content-Length' and self.content_length is not None:
            return str(self.content_length)
        return None

    def read(self, amt: int = -1) -> bytes:
        self.reads += 1
        return self.raw if amt is None or amt < 0 else self.raw[:amt]


def test_read_response_bounded_boundary() -> None:
    raw = b'{"ok": true}' + b' ' * 48
    assert read_response_bounded(FakeResponse(raw), len(raw)) == raw
    with pytest.raises(IngressTooLargeError, match='too large'):
        read_response_bounded(FakeResponse(raw), len(raw) - 1)


def test_read_response_bounded_reads_at_most_limit_plus_one() -> None:
    response = FakeResponse(b'x' * 4096)
    with pytest.raises(IngressTooLargeError):
        read_response_bounded(response, 64)
    assert response.reads == 1


def test_read_response_bounded_rejects_declared_length_without_reading() -> None:
    response = FakeResponse(b'{}', content_length=4096)
    with pytest.raises(IngressTooLargeError):
        read_response_bounded(response, 64)
    assert response.reads == 0


# ----------------------------------------------------------------------
# read_stream_bounded: undeclared/chunked streams are bounded on delivery


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def test_read_stream_bounded_boundary() -> None:
    assert asyncio.run(read_stream_bounded(_chunks(b'ab', b'cd'), 4)) == b'abcd'
    with pytest.raises(IngressTooLargeError, match='too large'):
        asyncio.run(read_stream_bounded(_chunks(b'ab', b'cde'), 4))


def test_read_stream_bounded_rejects_as_soon_as_limit_is_crossed() -> None:
    consumed: list[bytes] = []

    async def stream():
        for part in (b'x' * 8, b'y' * 8, b'z' * 8):
            consumed.append(part)
            yield part

    with pytest.raises(IngressTooLargeError):
        asyncio.run(read_stream_bounded(stream(), 8))
    # The second chunk crossed the limit; the third was never consumed.
    assert consumed == [b'x' * 8, b'y' * 8]


def test_read_stream_bounded_empty_stream_is_within_limit() -> None:
    assert asyncio.run(read_stream_bounded(_chunks(), 0)) == b''


def test_read_stream_bounded_validates_limit() -> None:
    with pytest.raises(ValueError, match='non-negative'):
        asyncio.run(read_stream_bounded(_chunks(b'ab'), -1))


# ----------------------------------------------------------------------
# RewApiClient: bounded JSON responses


def _list_opener(raw: bytes):
    def opener(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(raw)

    return opener


def test_rew_client_accepts_response_at_limit() -> None:
    payload = [{'uuid': 'abc'}, 'x' * 40]
    raw = json.dumps(payload).encode()
    client = RewApiClient(opener=_list_opener(raw), max_response_bytes=len(raw))
    assert client.list_measurements() == [{'uuid': 'abc'}]


def test_rew_client_rejects_response_one_byte_over_limit() -> None:
    payload = [{'uuid': 'abc'}, 'x' * 40]
    raw = json.dumps(payload).encode()
    client = RewApiClient(opener=_list_opener(raw), max_response_bytes=len(raw) - 1)
    with pytest.raises(RewApiResponseTooLarge, match='too large'):
        client.list_measurements()


def test_rew_too_large_error_is_typed_for_both_surfaces() -> None:
    assert issubclass(RewApiResponseTooLarge, RewApiError)
    assert issubclass(RewApiResponseTooLarge, IngressTooLargeError)
    assert issubclass(RewApiResponseTooLarge, ValueError)


def test_rew_client_rejects_declared_oversized_response() -> None:
    response = FakeResponse(b'{}', content_length=MAX_REW_API_RESPONSE_BYTES + 1)

    def opener(request: Request, timeout: float) -> FakeResponse:
        return response

    client = RewApiClient(opener=opener)
    with pytest.raises(RewApiResponseTooLarge):
        client.list_measurements()
    assert response.reads == 0


def test_rew_status_reports_oversized_response_as_offline() -> None:
    raw = json.dumps([{'uuid': 'abc'}, 'x' * 40]).encode()
    client = RewApiClient(opener=_list_opener(raw), max_response_bytes=len(raw) - 1)
    status = client.status()
    assert status['connected'] is False
    assert 'too large' in str(status['error'])


def test_rew_client_default_response_limit_matches_policy() -> None:
    assert RewApiClient(opener=_list_opener(b'{}')).max_response_bytes == MAX_REW_API_RESPONSE_BYTES


# ----------------------------------------------------------------------
# decode_rew_float_array: bounded decoded sample count


def test_decode_array_sample_limit_boundary() -> None:
    assert decode_rew_float_array(encode([1.0, 2.0]), max_samples=2) == pytest.approx((1.0, 2.0))
    with pytest.raises(RewApiResponseTooLarge, match='sample'):
        decode_rew_float_array(encode([1.0, 2.0, 3.0]), max_samples=2)


def test_decode_array_encoded_preflight_rejects_over_limit() -> None:
    encoded = encode([0.0] * 8)
    assert len(encoded) > max_base64_chars(4 * 1)
    with pytest.raises(RewApiResponseTooLarge):
        decode_rew_float_array(encoded, max_samples=1)


def test_decode_array_real_sample_limit_boundary() -> None:
    ok = decode_rew_float_array(encode([0.0] * MAX_REW_ARRAY_SAMPLES))
    assert len(ok) == MAX_REW_ARRAY_SAMPLES
    with pytest.raises(RewApiResponseTooLarge):
        decode_rew_float_array(encode([0.0] * (MAX_REW_ARRAY_SAMPLES + 1)))


def test_decode_array_default_limit_is_policy_constant() -> None:
    import inspect

    signature = inspect.signature(decode_rew_float_array)
    assert signature.parameters['max_samples'].default == MAX_REW_ARRAY_SAMPLES


# ----------------------------------------------------------------------
# RewRoomSimControlClient POST responses share the same bound


def test_roomsim_post_response_is_bounded() -> None:
    from htdt.rew_roomsim_batch import RewRoomSimControlClient

    def opener(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(json.dumps({'ok': True, 'pad': 'x' * 64}).encode())

    client = RewRoomSimControlClient(opener=opener, max_response_bytes=64)
    with pytest.raises(RewApiResponseTooLarge):
        client.set_roomsim_head_position({'unit': 'metres', 'fromRear': 1.0, 'fromLeft': 2.0, 'fromFloor': 1.0})


# ----------------------------------------------------------------------
# Native UI ingress paths share the bounded read


def _saved_f1(tmp_path: Path):
    from htdt.cad_repository import SceneRepository
    from htdt.cad_scene import make_f1_scene

    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def test_workspace_file_dialog_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt import file_dialog_memory, measurement_page_workspace
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    app = QApplication.instance() or QApplication([])
    scene_repository, revision = _saved_f1(tmp_path)
    controller = MeasurementWorkflowController(scene_repository, revision.document_id)
    workspace = MeasurementPageWorkspace(controller)

    oversized = tmp_path / 'huge.txt'
    oversized.write_bytes(b'0' * (MAX_NATIVE_REW_TEXT_FILE_BYTES + 1))
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: (str(oversized), '')),
    )
    workspace.import_rew_text_dialog()
    assert controller.pending_import is None
    assert not workspace.notice.isHidden()
    assert 'too large' in caplog.text  # localized notice; the cause lives in the log

    small = tmp_path / 'small.txt'
    small.write_bytes(b'20 70\n40 71\n')
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: (str(small), '')),
    )
    workspace.import_rew_text_dialog()
    assert controller.pending_import is not None
    assert controller.pending_import.sample_count == 2

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_measurement_page_dialog_uses_shared_bounded_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from htdt import file_dialog_memory
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.user_facing_error import operation_error_message

    oversized = tmp_path / 'huge.txt'
    oversized.write_bytes(b'0' * (MAX_NATIVE_REW_TEXT_FILE_BYTES + 1))

    imported: list[tuple[bytes, str]] = []
    errors: list[str] = []

    workspace = SimpleNamespace(
        controller=SimpleNamespace(
            stage_rew_text=lambda raw, filename: imported.append((raw, filename)),
        ),
        _operation_error_notice=lambda prefix, exc: errors.append(
            f'{prefix} · {operation_error_message(exc)}'
        ),
        _set_notice=lambda *args, **kwargs: None,
        refresh=lambda: None,
        set_context=lambda _ctx: None,
    )

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: (str(oversized), '')),
    )
    MeasurementPageWorkspace.import_rew_text_dialog(workspace)  # type: ignore[arg-type]

    assert imported == []
    # The dialog surfaces the mapped operator message (R6: no raw exc text);
    # IngressTooLargeError maps to the size-specific JP message.
    assert errors and '大きすぎ' in errors[0]


def test_calibration_pick_retains_bytes_and_binds_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REV43: the onboarding checklist tells the user to attach the
    calibration file's bytes — the pick button is that affordance: the
    file lands in the managed-asset store and the returned SHA-256 fills
    the field so the declared digest can resolve via
    ``validate_calibration_file`` (and therefore the quality producer's
    calibration claim) instead of pointing at nothing."""
    from hashlib import sha256

    from PySide6.QtWidgets import QApplication

    from htdt import file_dialog_memory
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    app = QApplication.instance() or QApplication([])
    scene_repository, revision = _saved_f1(tmp_path)
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    workspace = MeasurementPageWorkspace(controller)

    payload = b'UMIK-1 calibration data\n'
    cal = tmp_path / 'umik-1_90deg.txt'
    cal.write_bytes(payload)
    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: (str(cal), '')),
    )

    workspace._retain_calibration_file_dialog()

    digest = sha256(payload).hexdigest()
    assert workspace.mic_cal_file_edit.text() == 'umik-1_90deg.txt'
    assert workspace.mic_cal_sha_edit.text() == digest
    # The retained asset proves the declared hash — before this affordance
    # nothing in production could ever call save_calibration_file.
    asset = controller.quality_repository.validate_calibration_file(digest)
    assert asset.filename == 'umik-1_90deg.txt'
    assert asset.size_bytes == len(payload)

    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_calibration_pick_cancel_leaves_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt import file_dialog_memory
    from htdt.measurement_page_workspace import MeasurementPageWorkspace
    from htdt.measurement_workflow import MeasurementWorkflowController

    app = QApplication.instance() or QApplication([])
    scene_repository, revision = _saved_f1(tmp_path)
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    workspace = MeasurementPageWorkspace(controller)

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getOpenFileName',
        staticmethod(lambda *args, **kwargs: ('', '')),
    )
    workspace._retain_calibration_file_dialog()
    assert workspace.mic_cal_file_edit.text() == ''
    assert workspace.mic_cal_sha_edit.text() == ''

    workspace.close()
    workspace.deleteLater()
    app.processEvents()
