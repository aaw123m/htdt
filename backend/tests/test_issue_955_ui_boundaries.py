"""#955 tranche 3: typed error boundaries in the launch/viewport/adapter lanes.

Sequel to ``test_issue_955_capture_boundaries.py``. This file locks the
same contract for the remaining UI-adjacent modules:

* every broad catch in the converted modules carries an
  ``error-boundary:`` marker naming the boundary;
* narrowed transport/degraded-read catches keep their designed outcome
  for expected failures (typed apply abort, ``'failed'`` rollback,
  empty clearance map) while sealed-authority and bug-class failures
  propagate — never laundered into falsified results.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from htdt.cad_avr_lan_adapter import (
    AVR_LAN_ADAPTER_ID,
    AVR_LAN_DEVICE_FAMILY,
    AvrLanApplyError,
    AvrLanCalibrationAdapter,
)
from htdt.cad_device_adapter import AdapterCapabilityError, build_device_binding
from htdt.room_workspace import RoomWorkspace

SRC = Path(__file__).resolve().parents[1] / "src" / "htdt"

# Modules converted in the launch/viewport/adapter tranche — every broad
# catch inside them must carry an error-boundary marker.
BOUNDARY_MODULES = (
    "cad_adapter_conformance.py",
    "cad_avr_lan_adapter.py",
    "native_cad.py",
    "native_diagnostics.py",
    "room_viewport.py",
    "room_workspace.py",
)

_BROAD_NAMES = {"Exception", "BaseException"}


def _broad(handler_type) -> bool:
    if handler_type is None:
        return True
    if isinstance(handler_type, ast.Name):
        return handler_type.id in _BROAD_NAMES
    if isinstance(handler_type, ast.Attribute):
        return handler_type.attr in _BROAD_NAMES
    if isinstance(handler_type, ast.Tuple):
        return any(
            isinstance(elt, (ast.Name, ast.Attribute))
            and getattr(elt, "id", getattr(elt, "attr", None)) in _BROAD_NAMES
            for elt in handler_type.elts
        )
    return False


def test_no_unmarked_broad_catches_in_ui_scope() -> None:
    unmarked: list[str] = []
    for name in BOUNDARY_MODULES:
        source = SRC / name
        text = source.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ExceptHandler) and _broad(node.type):
                if "error-boundary:" not in lines[node.lineno - 1]:
                    unmarked.append(f"{name}:{node.lineno}")
    assert not unmarked, f"unmarked broad catches remain: {unmarked}"


# -- cad_avr_lan_adapter ------------------------------------------------------


class _FailingTransport:
    """Transport stub whose failure class the test chooses per call."""

    def __init__(self, send_exc=None, query_exc=None):
        self._send_exc = send_exc
        self._query_exc = query_exc

    def send(self, command: str) -> None:
        if self._send_exc is not None:
            raise self._send_exc

    def query(self, command: str) -> tuple[str, ...]:
        if self._query_exc is not None:
            raise self._query_exc
        return ()


def _adapter(transport) -> AvrLanCalibrationAdapter:
    return AvrLanCalibrationAdapter({"localhost:23": transport})


def _binding():
    return build_device_binding(
        adapter_id=AVR_LAN_ADAPTER_ID,
        device_family=AVR_LAN_DEVICE_FAMILY,
        device_model="AVR-X3800H",
        device_serial="localhost:23",
        firmware_version="1.4.0",
        routing=(("fl", "out-fl"),),
        bound_at_utc="2026-10-09T00:00:00Z",
    )


def _materialization() -> Mock:
    mat = Mock()
    mat.payload_text = "CVFL 50\r\nCVFR 51"
    mat.materialization_id = "mat-955"
    return mat


def test_apply_transport_failure_wraps_as_typed_abort() -> None:
    adapter = _adapter(_FailingTransport(send_exc=OSError("link down")))
    with pytest.raises(AvrLanApplyError) as err:
        adapter.apply(
            _materialization(), _binding(),
            operator_confirmed=True,
            applied_at_utc="2026-10-09T00:00:00Z",
        )
    assert err.value.applied_units == 0
    assert err.value.total_units == 2


def test_apply_bug_class_failure_propagates_unwrapped() -> None:
    """Before the boundary conversion a transport bug was laundered into
    a typed apply abort — indistinguishable from a real link failure."""
    adapter = _adapter(_FailingTransport(send_exc=TypeError("bad api")))
    with pytest.raises(TypeError):
        adapter.apply(
            _materialization(), _binding(),
            operator_confirmed=True,
            applied_at_utc="2026-10-09T00:00:00Z",
        )


def test_read_back_transport_failure_wraps_as_capability_error() -> None:
    adapter = _adapter(_FailingTransport(query_exc=OSError("link down")))
    with pytest.raises(AdapterCapabilityError):
        adapter.read_back(
            _binding(), observed_at_utc="2026-10-09T00:00:00Z",
        )


def test_read_back_bug_class_failure_propagates_unwrapped() -> None:
    adapter = _adapter(_FailingTransport(query_exc=TypeError("bad api")))
    with pytest.raises(TypeError):
        adapter.read_back(
            _binding(), observed_at_utc="2026-10-09T00:00:00Z",
        )


def test_rollback_transport_failure_reports_failed() -> None:
    adapter = _adapter(_FailingTransport(send_exc=OSError("link down")))
    adapter._baseline_trims = {"fl": 1.5}
    result = adapter.rollback_previous(_binding())
    assert result.outcome == "failed"


def test_rollback_bug_class_failure_propagates() -> None:
    """A transport bug previously reported as ``'failed'`` — falsifying
    the rollback outcome. It now propagates."""
    adapter = _adapter(_FailingTransport(send_exc=TypeError("bad api")))
    adapter._baseline_trims = {"fl": 1.5}
    with pytest.raises(TypeError):
        adapter.rollback_previous(_binding())


# -- room_workspace._installation_feasibility_preview --------------------------


class SealedReadStaleHeadError(RuntimeError):
    """Authority-family name suffix → must never degrade to empty state."""


def _workspace_stub(context_repo, repository):
    controller = SimpleNamespace(
        document_id="doc-955",
        repository=repository,
        document=Mock(),
    )
    installation_panel = SimpleNamespace(context_repository=context_repo)
    return SimpleNamespace(
        feasibility_panel=Mock(),
        controller=controller,
        installation_panel=installation_panel,
    )


def _context_repo():
    repo = Mock()
    repo.latest_contexts_for_document.return_value = {"ctx-1": Mock()}
    return repo


def test_clearance_repo_authority_failure_propagates(monkeypatch) -> None:
    """Before the conversion a sealed-store failure was swallowed into
    ``qualification = None`` — falsifying 'no service clearances'."""
    import htdt.room_workspace as workspace_mod

    monkeypatch.setattr(
        workspace_mod,
        "CadRoomQualificationRepository",
        Mock(side_effect=SealedReadStaleHeadError("head moved")),
    )
    stub = _workspace_stub(_context_repo(), repository=Mock())
    with pytest.raises(SealedReadStaleHeadError):
        RoomWorkspace._installation_feasibility_preview(stub)


def test_clearance_repo_expected_failure_degrades_honestly(monkeypatch) -> None:
    """An expected store failure degrades to no service clearances and
    reports the boundary — it never propagates as a crash."""
    import htdt.room_workspace as workspace_mod

    monkeypatch.setattr(
        workspace_mod,
        "CadRoomQualificationRepository",
        Mock(side_effect=OSError("store locked")),
    )
    preview = Mock(return_value="preview-sentinel")
    monkeypatch.setattr(
        workspace_mod, "build_installation_feasibility_preview", preview,
    )
    stub = _workspace_stub(_context_repo(), repository=Mock())
    assert RoomWorkspace._installation_feasibility_preview(stub) == (
        "preview-sentinel"
    )
    assert preview.call_args.kwargs["service_clearances"] == {}


def test_clearance_list_authority_failure_propagates(monkeypatch) -> None:
    qualification = Mock()
    qualification.service_envelopes.list.side_effect = (
        SealedReadStaleHeadError("head moved")
    )
    import htdt.room_workspace as workspace_mod

    monkeypatch.setattr(
        workspace_mod,
        "CadRoomQualificationRepository",
        Mock(return_value=qualification),
    )
    stub = _workspace_stub(_context_repo(), repository=Mock())
    with pytest.raises(SealedReadStaleHeadError):
        RoomWorkspace._installation_feasibility_preview(stub)


def test_clearance_list_expected_failure_empties_clearances(monkeypatch) -> None:
    qualification = Mock()
    qualification.service_envelopes.list.side_effect = OSError("locked")
    import htdt.room_workspace as workspace_mod

    monkeypatch.setattr(
        workspace_mod,
        "CadRoomQualificationRepository",
        Mock(return_value=qualification),
    )
    preview = Mock(return_value="preview-sentinel")
    monkeypatch.setattr(
        workspace_mod, "build_installation_feasibility_preview", preview,
    )
    stub = _workspace_stub(_context_repo(), repository=Mock())
    assert RoomWorkspace._installation_feasibility_preview(stub) == (
        "preview-sentinel"
    )
    assert preview.call_args.kwargs["service_clearances"] == {}
