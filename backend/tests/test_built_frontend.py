from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from htdt.main import create_app


def test_built_frontend_is_served_with_assets_and_api(tmp_path: Path) -> None:
    frontend_dist = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
    if not frontend_dist.is_dir():
        pytest.skip('frontend/dist is not built yet')

    client = TestClient(create_app(tmp_path / 'data'))

    root = client.get('/')
    assert root.status_code == 200
    assert '<div id="root"></div>' in root.text
    assert '<title>Home Theater Digital Twin</title>' in root.text

    scripts = re.findall(r'<script[^>]+src="([^"]+)"', root.text)
    assert scripts, 'built index.html must reference a JavaScript bundle'
    bundle = client.get(scripts[0])
    assert bundle.status_code == 200
    assert bundle.content

    health = client.get('/api/health')
    assert health.status_code == 200
    assert health.json()['status'] == 'ok'

    fallback = client.get('/some/client/route')
    assert fallback.status_code == 200
    assert '<div id="root"></div>' in fallback.text
