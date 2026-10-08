"""Issue #984 — presentation package output-target validation.

``validate_output_target`` (``htdt.output_target``) is the single typed
rule both export builders (``_build_review`` / ``_build_proposal``)
apply to the post-chooser directory. The check it replaced —
``target.startswith(('/', 'C:', 'D:'))`` — rejected every valid drive
letter except C/D, rejected UNC shares outright, and passed
``C:relative`` drive-relative spellings that are not absolute on
Windows at all.

Covered here:

- grammar rejects (empty / drive-relative / driveless / bare /
  invalid characters) — pure ``PureWindowsPath`` logic, platform-free;
- real-filesystem accepts and rejects on Windows (existence,
  writability probe, reparse-point containment, collision, length);
- both workspace builders applying THE SAME rule through a mocked
  file chooser, with verified success reporting and scene invariance
  on every failure path;
- physical acceptance legs that need real Windows drive/share
  topology (extra drive letters, reachable/denied UNC shares, ACL
  write-deny) — ``TestPhysicalTargets``, each guarded so off-topology
  machines skip instead of fail.
"""

from __future__ import annotations

import os
import string
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from htdt.output_target import (
    OutputTargetError,
    validate_output_target,
)

NOW = '2026-10-04T00:00:00+00:00'

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != 'nt', reason='Windows path semantics'
)


def _reason(raw, package_name: str = 'review-deadbeef') -> str:
    """The machine reason validate_output_target assigns, or 'ok'."""
    try:
        validate_output_target(raw, package_name=package_name)
    except OutputTargetError as exc:
        return exc.reason
    return 'ok'


# ----------------------------------------------------------------------
# Grammar — no filesystem involved, runs on any host.


class TestGrammar:
    @pytest.mark.parametrize('raw', ['', '   ', '"   "', '"'])
    def test_empty(self, raw: str) -> None:
        exc = _expect(raw, 'empty')
        assert exc.suggestion

    @pytest.mark.parametrize(
        'raw',
        [
            'relative/dir',
            'C:relative',          # drive-relative — not absolute on Windows
            'd:also-relative',
            '/posix/rooted',       # driveless — anchored on the *current* drive
            '\\\\server',          # UNC server without a share
        ],
    )
    def test_not_absolute(self, raw: str) -> None:
        exc = _expect(raw, 'not_absolute')
        assert exc.suggestion

    @pytest.mark.parametrize(
        'raw',
        ['C:/out?bad', 'D:/x*y', 'C:/a:b', 'E:/<dir>', 'C:/pi|pe'],
    )
    def test_invalid_chars(self, raw: str) -> None:
        exc = _expect(raw, 'invalid_chars')
        assert exc.suggestion

    def test_quoted_absolute_passes_grammar(
        self, tmp_path: Path
    ) -> None:
        """Chooser-pasted paths arrive quoted; quotes strip before checks."""
        try:
            target = validate_output_target(
                f'"{tmp_path}"', package_name='review-x'
            )
        except OutputTargetError as exc:  # pragma: no cover - host-dependent
            pytest.fail(f'quoted valid path rejected: {exc.reason}')
        assert target.package_dir.name == 'review-x'


def _expect(raw, reason: str) -> OutputTargetError:
    with pytest.raises(OutputTargetError) as hit:
        validate_output_target(raw, package_name='review-x')
    assert hit.value.reason == reason
    return hit.value


# ----------------------------------------------------------------------
# Real-filesystem matrix — Windows only (E:/UNC/junctions/probes need it).


@WINDOWS_ONLY
class TestFilesystemMatrix:
    def test_valid_chosen_dir(self, tmp_path: Path) -> None:
        target = validate_output_target(
            str(tmp_path), package_name='review-x'
        )
        assert target.root == Path(tmp_path)
        assert target.package_dir == tmp_path / 'review-x'

    def test_trailing_separator_accepted(self, tmp_path: Path) -> None:
        target = validate_output_target(
            str(tmp_path) + '\\', package_name='review-x'
        )
        assert target.package_dir == tmp_path / 'review-x'

    def test_forward_slashes_accepted(self, tmp_path: Path) -> None:
        target = validate_output_target(
            str(tmp_path).replace('\\', '/'), package_name='review-x'
        )
        assert target.package_dir == tmp_path / 'review-x'

    def test_missing_dir_named(self, tmp_path: Path) -> None:
        exc = _expect(str(tmp_path / 'does-not-exist'), 'missing')
        assert exc.suggestion

    def test_file_as_root_named(self, tmp_path: Path) -> None:
        file = tmp_path / 'a-file.txt'
        file.write_text('x')
        exc = _expect(str(file), 'not_directory')
        assert exc.suggestion

    def test_disconnected_drive_named(self, tmp_path: Path) -> None:
        drives = {
            f'{letter}:' for letter in string.ascii_uppercase
            if Path(f'{letter}:\\').exists()
        }
        free = next(
            f'{letter}:' for letter in 'QRSTUVWXYZEFGHIJKLMNOPAB'
            if f'{letter}:' not in drives
        )
        exc = _expect(f'{free}\\output\\dir', 'drive_unavailable')
        assert free in str(exc)
        assert exc.suggestion

    def test_unreachable_share_named(self) -> None:
        # Loopback host, guaranteed-absent share: a fast, deterministic
        # "share unreachable" probe — no real network lookup needed.
        exc = _expect(
            '\\\\127.0.0.1\\htdt984-no-such-share\\dir',
            'share_unavailable',
        )
        assert exc.suggestion

    def test_collision_named_no_overwrite(self, tmp_path: Path) -> None:
        occupied = tmp_path / 'review-x'
        occupied.mkdir()
        (occupied / 'manifest.json').write_text('{}')
        exc = _expect(str(tmp_path), 'collision')
        assert exc.suggestion
        # No fallback, no overwrite: the existing artifact is untouched.
        assert (occupied / 'manifest.json').read_text() == '{}'

    def test_empty_package_dir_allowed(self, tmp_path: Path) -> None:
        (tmp_path / 'review-x').mkdir()
        target = validate_output_target(
            str(tmp_path), package_name='review-x'
        )
        assert target.package_dir == tmp_path / 'review-x'

    def test_package_dir_file_named(self, tmp_path: Path) -> None:
        (tmp_path / 'review-x').write_text('x')
        exc = _expect(str(tmp_path), 'not_directory')
        assert exc.suggestion

    def test_path_too_long_named(self, tmp_path: Path) -> None:
        depth = 'd' * (210 - len(str(tmp_path / 'review-x')))
        root = tmp_path / depth
        root.mkdir()
        exc = _expect(str(root), 'path_too_long')
        assert exc.suggestion

    def test_symlinked_root_named(self, tmp_path: Path) -> None:
        real = tmp_path / 'real'
        real.mkdir()
        link = tmp_path / 'link'
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - privilege-dependent
            pytest.skip(f'symlink creation needs privilege: {exc}')
        exc = _expect(str(link), 'redirected')
        assert 'real' in str(exc.suggestion)
        assert exc.suggestion

    def test_junction_root_named(self, tmp_path: Path) -> None:
        real = tmp_path / 'real'
        real.mkdir()
        link = tmp_path / 'junction'
        try:
            subprocess.check_call(
                ['cmd', '/c', 'mklink', '/J', str(link), str(real)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.skip(f'junction creation unavailable: {exc}')
        assert link.is_junction()
        exc = _expect(str(link), 'redirected')
        assert exc.suggestion

    def test_package_dir_link_escape_named(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        link = tmp_path / 'review-x'
        try:
            link.symlink_to(elsewhere, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - privilege-dependent
            pytest.skip(f'symlink creation needs privilege: {exc}')
        exc = _expect(str(tmp_path), 'link_escape')
        assert exc.suggestion
        # The escape target stays untouched.
        assert not any(elsewhere.iterdir())

    def test_not_writable_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A denied write probe names the failure — never guesses."""
        from htdt import output_target

        def _denied(_root: Path) -> None:
            raise PermissionError('simulated ACL deny')

        monkeypatch.setattr(
            output_target, '_assert_writable', _denied
        )
        exc = _expect(str(tmp_path), 'not_writable')
        assert exc.suggestion

    def test_probe_failure_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from htdt import output_target

        def _broken(_root: Path) -> None:
            raise OSError('media removed mid-check')

        monkeypatch.setattr(
            output_target, '_assert_writable', _broken
        )
        exc = _expect(str(tmp_path), 'io_error')
        assert exc.suggestion


# ----------------------------------------------------------------------
# Workspace builders — mocked file chooser, real packages.


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _capture_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record (title, text) for every warning/info the workspace shows."""
    from PySide6.QtWidgets import QMessageBox

    shown: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        'warning',
        staticmethod(lambda *a, **k: shown.append(('warning',) + a[1:3]) or 0),
    )
    monkeypatch.setattr(
        QMessageBox,
        'information',
        staticmethod(lambda *a, **k: shown.append(('info',) + a[1:3]) or 0),
    )
    monkeypatch.setattr(QMessageBox, 'exec', lambda _box: 0)
    return shown


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2):
    from htdt.cad_scene import (
        Direction3,
        Position3,
        RoomPrism,
        SceneDocument,
        SceneEntity,
        Size3,
    )

    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _camera(x: float = 3.0):
    from htdt.cad_view_state import RoomCameraState

    return RoomCameraState(
        position=(x, 2.0, 3.0),
        focal_point=(0.0, 0.0, 0.0),
        view_up=(0.0, 0.0, 1.0),
        projection='perspective',
        standard_view='custom',
    )


def _workspace(tmp_path: Path):
    """PresentationWorkspace over a real repository with one saved session."""
    from htdt.cad_presentation_repository import CadPresentationRepository
    from htdt.cad_presentation_session import (
        PresentationRenderSettings,
        build_presentation_session,
        build_viewpoint,
    )
    from htdt.cad_repository import SceneRepository
    from htdt.presentation_workspace import PresentationWorkspace

    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    revision_a = scenes.save(
        _scene(fl_x=1.2), parent_revision_id=None
    ).revision
    scenes.save(
        _scene(fl_x=1.6), parent_revision_id=revision_a.revision_id
    )
    repository = CadPresentationRepository(scenes)
    session = build_presentation_session(
        document_id=revision_a.document_id,
        label='クライアントレビュー',
        scene_revision_id=revision_a.revision_id,
        scene_content_hash=revision_a.content_hash,
        viewpoints=(
            build_viewpoint(name='正面', camera=_camera()),
            build_viewpoint(name='後方', camera=_camera(-3.0)),
        ),
        render=PresentationRenderSettings(yaw_step_deg=60),
        created_at_utc=NOW,
    )
    repository.save_session(session)
    workspace = PresentationWorkspace(scenes, revision_a.document_id)
    workspace._refresh_export_sessions()
    workspace.export_session_combo.setCurrentIndex(0)
    return workspace, session


def _stub_renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    from htdt import presentation_workspace

    class _StubRenderer:
        def renderer_id(self) -> str:
            return 'stub/1.0'

        def render_frame(self, document, viewpoint, *, yaw_deg: int) -> bytes:
            return b'PNG-STUB:' + str(yaw_deg).encode()

    monkeypatch.setattr(
        presentation_workspace,
        'OffscreenSceneRenderer',
        lambda *a, **k: _StubRenderer(),
    )


def _mock_chooser(monkeypatch: pytest.MonkeyPatch, chosen: Path) -> None:
    from htdt import file_dialog_memory

    monkeypatch.setattr(
        file_dialog_memory.QFileDialog,
        'getExistingDirectory',
        staticmethod(lambda *a, **k: str(chosen)),
    )


class TestWorkspaceExport:
    def test_chooser_pick_fills_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _qapp()
        shown = _capture_boxes(monkeypatch)
        workspace, _session_obj = _workspace(tmp_path)
        target_root = tmp_path / 'chosen'
        target_root.mkdir()
        _mock_chooser(monkeypatch, target_root)
        workspace._pick_export_dir()
        assert workspace.export_dir_label.text() == str(target_root)
        assert not shown

    @pytest.mark.parametrize(
        'builder', ['_build_review', '_build_proposal']
    )
    def test_relative_target_rejected_same_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        builder: str,
    ) -> None:
        """Both builders run the identical validation rule — a
        drive-relative spelling is rejected the same way in each."""
        _qapp()
        shown = _capture_boxes(monkeypatch)
        workspace, _session_obj = _workspace(tmp_path)
        calls: list[str] = []
        from htdt import presentation_workspace

        real_validate = presentation_workspace.validate_output_target
        monkeypatch.setattr(
            presentation_workspace,
            'validate_output_target',
            lambda raw, *, package_name: (
                calls.append(package_name),
                real_validate(raw, package_name=package_name),
            )[1],
        )
        workspace.export_dir_label.setText('C:relative')
        getattr(workspace, builder)()
        prefix = 'review-' if builder == '_build_review' else 'proposal-'
        assert len(calls) == 1 and calls[0].startswith(prefix)
        kinds = [row for row in shown if row[0] == 'warning']
        assert len(kinds) == 1
        assert '絶対パス' in kinds[0][2]
        # No build ran — no status, no open affordance.
        assert workspace.export_status.text() == ''
        assert workspace.open_output_button.isHidden()

    def test_unselected_target_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _qapp()
        shown = _capture_boxes(monkeypatch)
        workspace, _session_obj = _workspace(tmp_path)
        assert workspace.export_dir_label.text() == '（未選択）'
        workspace._build_review()
        warnings = [row for row in shown if row[0] == 'warning']
        assert len(warnings) == 1
        assert '選択' in warnings[0][2]

    def test_error_leaves_scene_invariant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation failures touch neither the repository nor the
        session — and no package files are written anywhere."""
        _qapp()
        _capture_boxes(monkeypatch)
        workspace, session_obj = _workspace(tmp_path)
        head_before = workspace.repository.current_head(
            workspace.document_id
        )
        sessions_before = workspace.presentation_repository.list_sessions(
            workspace.document_id
        )
        workspace.export_dir_label.setText(
            str(tmp_path / 'missing-root')
        )
        workspace._build_review()
        workspace._build_proposal()
        assert (
            workspace.repository.current_head(workspace.document_id)
            == head_before
        )
        assert (
            workspace.presentation_repository.list_sessions(
                workspace.document_id
            ) == sessions_before
        )
        assert not (tmp_path / 'missing-root').exists()

    def test_collision_warns_without_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _qapp()
        shown = _capture_boxes(monkeypatch)
        workspace, session_obj = _workspace(tmp_path)
        target_root = tmp_path / 'chosen'
        target_root.mkdir()
        occupied = target_root / f'review-{session_obj.session_id[:8]}'
        occupied.mkdir()
        (occupied / 'viewer.html').write_text('keep me')
        _mock_chooser(monkeypatch, target_root)
        workspace._pick_export_dir()
        workspace._build_review()
        warnings = [row for row in shown if row[0] == 'warning']
        assert len(warnings) == 1
        assert '既存' in warnings[0][2]
        assert (occupied / 'viewer.html').read_text() == 'keep me'
        assert workspace.open_output_button.isHidden()

    def test_review_success_reports_exact_facts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _qapp()
        _capture_boxes(monkeypatch)
        workspace, session_obj = _workspace(tmp_path)
        _stub_renderers(monkeypatch)
        target_root = tmp_path / 'chosen'
        target_root.mkdir()
        _mock_chooser(monkeypatch, target_root)
        workspace._pick_export_dir()
        workspace._build_review()
        expected = target_root / f'review-{session_obj.session_id[:8]}'
        text = workspace.export_status.text()
        assert str(expected) in text
        assert 'エントリ' in text
        assert 'SHA-256' in text
        assert not workspace.open_output_button.isHidden()
        # Success was declared only after verification — the manifest
        # on disk proves what was written.
        import json

        manifest = json.loads(
            (expected / 'manifest.json').read_text('utf-8')
        )
        assert manifest['manifest_sha256'] in text
        assert len(manifest['entries']) >= 3  # viewer + semantic + manifest

    def test_proposal_success_reports_exact_facts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _qapp()
        _capture_boxes(monkeypatch)
        workspace, session_obj = _workspace(tmp_path)
        target_root = tmp_path / 'chosen'
        target_root.mkdir()
        _mock_chooser(monkeypatch, target_root)
        workspace._pick_export_dir()
        workspace._build_proposal()
        expected = target_root / f'proposal-{session_obj.session_id[:8]}'
        text = workspace.export_status.text()
        assert str(expected) in text
        assert 'エントリ' in text
        assert 'SHA-256' in text
        assert not workspace.open_output_button.isHidden()

    def test_verify_failure_shows_no_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A package that fails re-verification never surfaces as done."""
        _qapp()
        _capture_boxes(monkeypatch)
        workspace, _session_obj = _workspace(tmp_path)
        _stub_renderers(monkeypatch)
        from htdt import presentation_workspace
        from htdt.cad_review_package import ReviewPackageError

        def _fail(_dir):
            raise ReviewPackageError('manifest.json is missing')

        monkeypatch.setattr(
            presentation_workspace, 'verify_review_package', _fail
        )
        target_root = tmp_path / 'chosen'
        target_root.mkdir()
        _mock_chooser(monkeypatch, target_root)
        workspace._pick_export_dir()
        workspace._build_review()
        assert workspace.export_status.text() == ''
        assert workspace.open_output_button.isHidden()
        assert workspace._last_output_dir is None


# ----------------------------------------------------------------------
# Physical acceptance — real Windows drive/share topology. Each leg is
# guarded so machines without the topology skip rather than fail.


@WINDOWS_ONLY
class TestPhysicalTargets:
    def test_every_present_drive_letter_accepted(self) -> None:
        """The old check hard-coded C:/D:; every drive letter now
        reaches the filesystem checks instead of grammar rejection."""
        for letter in string.ascii_uppercase:
            if not Path(f'{letter}:\\').exists():
                continue
            try:
                validate_output_target(
                    f'{letter}:\\', package_name='review-x'
                )
            except OutputTargetError as exc:
                # Grammar must never be the reason on a real drive;
                # writable-state reasons are environment facts.
                assert exc.reason in ('not_writable', 'collision')

    def test_unc_admin_share_accepted(self) -> None:
        share = Path('\\\\localhost\\C$\\t')
        if not share.is_dir():
            pytest.skip('loopback admin share not reachable')
        target = validate_output_target(
            '\\\\localhost\\C$\\t', package_name='review-x'
        )
        assert str(target.package_dir) == str(share / 'review-x')

    def test_unc_missing_share_named(self) -> None:
        share = Path('\\\\127.0.0.1\\htdt984-no-such-share')
        try:
            reachable = share.exists()
        except OSError:
            reachable = True
        if reachable:
            pytest.skip('unexpected live share on loopback')
        exc = _expect(
            '\\\\127.0.0.1\\htdt984-no-such-share\\out', 'share_unavailable'
        )
        assert exc.suggestion

    def test_unc_share_deny_named(self, tmp_path: Path) -> None:
        """ACL write-deny probed through the UNC spelling → 'share_denied'
        (a UNC drive rejects with the share permission name, not the
        local-drive 'not_writable')."""
        unc_root = Path('\\\\localhost\\C$')
        if not unc_root.is_dir():
            pytest.skip('loopback admin share not reachable')
        user = os.environ.get('USERNAME')
        if not user:
            pytest.skip('USERNAME unset')
        denied = tmp_path / 'unc-locked'
        denied.mkdir()
        # 'C:\t\...\unc-locked' → '\\localhost\C$\t\...\unc-locked'
        unc_spelling = '\\\\localhost\\C$\\' + str(denied)[3:]
        try:
            subprocess.check_call(
                ['icacls', str(denied), '/deny', f'{user}:(OI)(CI)(W)'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.skip(f'icacls unavailable: {exc}')
        try:
            exc = _expect(unc_spelling, 'share_denied')
            assert exc.suggestion
        finally:
            subprocess.call(
                ['icacls', str(denied), '/remove:d', user],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_acl_write_deny_named(self, tmp_path: Path) -> None:
        """Real ACL write-deny → 'not_writable' via the create probe."""
        user = os.environ.get('USERNAME')
        if not user:
            pytest.skip('USERNAME unset')
        denied = tmp_path / 'locked'
        denied.mkdir()
        try:
            subprocess.check_call(
                ['icacls', str(denied), '/deny', f'{user}:(OI)(CI)(W)'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.skip(f'icacls unavailable: {exc}')
        try:
            exc = _expect(str(denied), 'not_writable')
            assert exc.suggestion
        finally:
            subprocess.call(
                ['icacls', str(denied), '/remove:d', user],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_unreachable_network_share(self) -> None:
        """Disconnected-network leg — off by default because the TCP 445
        timeout makes it slow; opt in with HTDT_TEST_UNREACHABLE_NET=1."""
        if os.environ.get('HTDT_TEST_UNREACHABLE_NET') != '1':
            pytest.skip('manual/physical: set HTDT_TEST_UNREACHABLE_NET=1')
        exc = _expect(
            '\\\\192.0.2.1\\share\\dir', 'share_unavailable'
        )
        assert exc.suggestion
