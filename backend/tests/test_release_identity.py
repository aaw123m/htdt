from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

import htdt
import htdt.native_cad as native_cad
from htdt.build_info import BUILD_INFO_ENV_VAR, get_build_info, version_string
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.native_backup import (
    BackupManifest,
    _manifest_hash,
    create_backup,
    validate_backup,
)


PACKAGE_ROOT = Path(htdt.__file__).resolve().parent
BACKEND_ROOT = PACKAGE_ROOT.parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[2]

_PEP440_RELEASE = re.compile(r'\d+\.\d+\.\d+(?:\.dev\d+)?')
_DISPLAY_VERSION = re.compile(
    re.escape(htdt.__version__) + r'(?:\+g[0-9a-f]{8}(?:\.dirty)?)?'
)


def _seed(data_dir: Path) -> None:
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)


@pytest.fixture(autouse=True)
def _fresh_build_info(monkeypatch):
    monkeypatch.delenv(BUILD_INFO_ENV_VAR, raising=False)
    get_build_info.cache_clear()
    try:
        yield
    finally:
        get_build_info.cache_clear()


def test_canonical_version_is_pep440_dev_identity() -> None:
    # After the stable 0.1.0 release, main carries an explicit development
    # identity; a stable release is a bare X.Y.Z.
    assert _PEP440_RELEASE.fullmatch(htdt.__version__)


def test_pyproject_derives_package_version_from_canonical_attribute() -> None:
    pyproject_path = BACKEND_ROOT / 'pyproject.toml'
    if not pyproject_path.is_file():
        pytest.skip('backend source tree not available')
    pyproject = tomllib.loads(pyproject_path.read_text(encoding='utf-8'))
    project = pyproject['project']
    # The Python package version authority cannot diverge: there is no
    # separately maintained literal, setuptools reads htdt.__version__.
    assert 'version' not in project
    assert 'version' in project['dynamic']
    assert pyproject['tool']['setuptools']['dynamic']['version'] == {
        'attr': 'htdt.__version__'
    }


def test_inno_setup_fallback_version_tracks_canonical_version() -> None:
    iss_path = REPO_ROOT / 'installer' / 'HTDT.iss'
    if not iss_path.is_file():
        pytest.skip('installer script not available')
    match = re.search(r'#define AppVersion "([^"]+)"', iss_path.read_text(encoding='utf-8'))
    assert match is not None
    assert match.group(1) == htdt.__version__


def test_display_version_extends_canonical_version() -> None:
    info = get_build_info()
    assert info.version == htdt.__version__
    assert _DISPLAY_VERSION.fullmatch(version_string())


def test_build_info_resolves_git_checkout_identity() -> None:
    if not (REPO_ROOT / '.git').exists() or shutil.which('git') is None:
        pytest.skip('no source checkout metadata available')
    expected_sha = subprocess.run(
        ['git', '-C', str(REPO_ROOT), 'rev-parse', 'HEAD'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    info = get_build_info()
    assert info.commit_sha == expected_sha
    assert info.source == 'checkout'
    expected = f'{htdt.__version__}+g{expected_sha[:8]}'
    if info.dirty:
        expected += '.dirty'
    assert info.display_version == expected


def test_packaged_build_info_file_drives_display_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / '_internal'
    (bundle / 'htdt_build').mkdir(parents=True)
    (bundle / 'htdt_build' / 'build_info.json').write_text(
        json.dumps({
            'version': htdt.__version__,
            'commit_sha': 'a' * 40,
            'build_id': '123456',
            'dirty': False,
            'source': 'packaged',
        }),
        encoding='utf-8',
    )
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    get_build_info.cache_clear()

    info = get_build_info()
    assert info.source == 'packaged'
    assert info.commit_sha == 'a' * 40
    assert info.build_id == '123456'
    assert info.display_version == f'{htdt.__version__}+gaaaaaaaa'
    assert version_string() == info.display_version


def test_env_override_build_info(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / 'build_info.json'
    override.write_text(
        json.dumps({'commit_sha': 'b' * 40, 'dirty': True}),
        encoding='utf-8',
    )
    monkeypatch.setenv(BUILD_INFO_ENV_VAR, str(override))

    info = get_build_info()
    assert info.commit_sha == 'b' * 40
    assert info.dirty is True
    assert info.display_version == f'{htdt.__version__}+gbbbbbbbb.dirty'


def test_packaged_without_build_info_reports_plain_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    get_build_info.cache_clear()

    info = get_build_info()
    assert info.source == 'packaged'
    assert info.commit_sha is None
    assert info.display_version == htdt.__version__


def test_native_version_flag_reports_display_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        native_cad.main(['--version'])
    assert excinfo.value.code == 0
    assert version_string() in capsys.readouterr().out


def test_backup_manifest_records_build_provenance(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    _seed(data_dir)
    archive = tmp_path / 'provenance.htdt-backup'

    manifest = create_backup(data_dir, archive)
    info = get_build_info()

    assert manifest.application_version == htdt.__version__
    assert manifest.build is not None
    assert manifest.build.display_version == info.display_version
    assert manifest.build.commit_sha == info.commit_sha
    assert manifest.build.source == info.source
    assert validate_backup(archive).build == manifest.build


def test_schema1_manifest_without_build_still_validates() -> None:
    # Backups written before build provenance existed remain valid: the
    # optional field is excluded from the identity hash when absent.
    payload = {
        'schema_version': 1,
        'application_version': '0.1.0',
        'created_at_utc': '2026-01-01T00:00:00+00:00',
        'files': [
            {
                'path': 'cad-scenes.sqlite3',
                'kind': 'database',
                'size_bytes': 4,
                'sha256': 'a' * 64,
            }
        ],
    }
    manifest = BackupManifest(**payload, manifest_sha256=_manifest_hash(payload))
    assert manifest.build is None
    assert manifest.identity_payload() == payload
