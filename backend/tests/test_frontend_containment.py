from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import htdt.main
from htdt.main import _resolve_frontend_path, create_app

INDEX_HTML = '<html><body>HTDT-SPA-INDEX</body></html>'
SECRET = 'OUTSIDE-DIST-SECRET'


@pytest.fixture
def frontend_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Serve a fake ``frontend/dist`` layout without building the frontend.

    ``create_app`` locates ``frontend/dist`` relative to ``htdt/main.py``, so
    the module ``__file__`` is pointed at a stub inside a throwaway repo
    layout under ``tmp_path``. Files that live outside ``dist`` (such as
    ``frontend/package.json``) must never be served by the SPA fallback.
    """
    repo_root = tmp_path / 'repo'
    package_dir = repo_root / 'backend' / 'src' / 'htdt'
    package_dir.mkdir(parents=True)
    (package_dir / 'main.py').write_text('# stub for frontend/dist discovery', encoding='utf-8')

    frontend_dist = repo_root / 'frontend' / 'dist'
    (frontend_dist / 'assets' / 'js').mkdir(parents=True)
    (frontend_dist / 'nested' / 'deep').mkdir(parents=True)
    (frontend_dist / 'index.html').write_text(INDEX_HTML, encoding='utf-8')
    (frontend_dist / 'favicon.svg').write_text('<svg>icon</svg>', encoding='utf-8')
    (frontend_dist / 'assets' / 'js' / 'app.js').write_text('console.log("app");\n', encoding='utf-8')
    (frontend_dist / 'nested' / 'deep' / 'note.txt').write_text('nested asset body', encoding='utf-8')

    (repo_root / 'frontend' / 'package.json').write_text(f'{{"name": "{SECRET}"}}', encoding='utf-8')
    (repo_root / 'secret.txt').write_text(SECRET, encoding='utf-8')

    monkeypatch.setattr(htdt.main, '__file__', str(package_dir / 'main.py'))
    return TestClient(create_app(tmp_path / 'data'))


def _assert_contained(response) -> None:
    assert response.status_code in (200, 403, 404)
    assert SECRET not in response.text
    if response.status_code == 200:
        assert response.text == INDEX_HTML


def test_root_and_spa_routes_return_index(frontend_client: TestClient) -> None:
    for route in ('/', '/some/client/route', '/deeply/nested/ui/route'):
        response = frontend_client.get(route)
        assert response.status_code == 200
        assert response.text == INDEX_HTML


def test_files_inside_frontend_dist_are_served(frontend_client: TestClient) -> None:
    response = frontend_client.get('/favicon.svg')
    assert response.status_code == 200
    assert response.text == '<svg>icon</svg>'

    response = frontend_client.get('/nested/deep/note.txt')
    assert response.status_code == 200
    assert response.text == 'nested asset body'


def test_assets_mount_serves_nested_file(frontend_client: TestClient) -> None:
    response = frontend_client.get('/assets/js/app.js')
    assert response.status_code == 200
    assert 'console.log' in response.text


@pytest.mark.parametrize(
    'raw_url',
    [
        '/%2e%2e/package.json',
        '/%2E%2E/package.json',
        '/.%2e/package.json',
        '/..%2fpackage.json',
        '/%2e%2e%2fpackage.json',
        '/%2e%2e/%2e%2e/secret.txt',
        '/nested/%2e%2e/%2e%2e/package.json',
        '/%252e%252e/package.json',
        '/%2e%2e',
        '/assets/%2e%2e/package.json',
        '/assets/%2e%2e/%2e%2e/secret.txt',
    ],
)
def test_encoded_dotdot_traversal_cannot_escape(frontend_client: TestClient, raw_url: str) -> None:
    _assert_contained(frontend_client.get(raw_url))


@pytest.mark.parametrize(
    'raw_url',
    [
        '/%2e%2e%5cpackage.json',
        '/..%5c..%5csecret.txt',
        '/nested%5c..%5c..%5cpackage.json',
        '/%2e%2e%5c%2e%2e%5csecret.txt',
    ],
)
def test_windows_separator_traversal_cannot_escape(frontend_client: TestClient, raw_url: str) -> None:
    _assert_contained(frontend_client.get(raw_url))


@pytest.mark.parametrize(
    'raw_url',
    [
        '/%2fetc%2fpasswd',
        '/%2f%2fserver%2fshare%2ffile.txt',
        '/%5c%5cserver%5cshare%5cfile.txt',
        '/C:%5cWindows%5cwin.ini',
        '/C:%2fWindows%2fwin.ini',
    ],
)
def test_absolute_drive_and_unc_paths_cannot_escape(frontend_client: TestClient, raw_url: str) -> None:
    _assert_contained(frontend_client.get(raw_url))


def test_embedded_null_byte_falls_back_safely(frontend_client: TestClient) -> None:
    _assert_contained(frontend_client.get('/foo%00.txt'))


def test_resolver_rejects_unsafe_forms(tmp_path: Path) -> None:
    root = tmp_path / 'dist'
    root.mkdir()
    root = root.resolve()
    (root / 'index.html').write_text(INDEX_HTML, encoding='utf-8')
    (root / 'a.txt').write_text('a', encoding='utf-8')

    assert _resolve_frontend_path(root, 'a.txt') == (root / 'a.txt').resolve()
    assert _resolve_frontend_path(root, '') is None
    assert _resolve_frontend_path(root, '..\\secret.txt') is None
    assert _resolve_frontend_path(root, 'nested\\..\\..\\secret.txt') is None
    assert _resolve_frontend_path(root, 'C:\\Windows\\win.ini') is None
    assert _resolve_frontend_path(root, 'C:/Windows/win.ini') is None
    assert _resolve_frontend_path(root, '\\\\server\\share\\x') is None
    assert _resolve_frontend_path(root, '/etc/passwd') is None
    assert _resolve_frontend_path(root, '../package.json') is None
    assert _resolve_frontend_path(root, 'foo\x00.txt') is None
    assert _resolve_frontend_path(root, 'missing.txt') is None
    # Traversal that would stay inside the root is rejected outright too.
    assert _resolve_frontend_path(root, 'a/../index.html') is None
