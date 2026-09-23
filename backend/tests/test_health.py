from pathlib import Path

from fastapi.testclient import TestClient

from htdt import __version__
from htdt.database import SCHEMA_VERSION
from htdt.main import create_app


def test_health_endpoint(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json() == {
        'status': 'ok',
        'version': __version__,
        'platform_target': 'Windows 11 x64',
        'rew_required': False,
        'measurement_hardware_required': False,
        'schema_version': SCHEMA_VERSION,
    }


def test_root_is_available_without_built_frontend(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.get('/')
    assert response.status_code == 200
