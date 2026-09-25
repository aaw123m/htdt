"""Developer/synthetic acceptance lane gating (#901)."""

from __future__ import annotations

from htdt.developer_mode import developer_mode_enabled


def test_developer_mode_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv('HTDT_DEVELOPER_MODE', raising=False)
    assert developer_mode_enabled() is False


def test_developer_mode_requires_explicit_truthy(monkeypatch) -> None:
    for value in ('1', 'true', 'yes', 'on'):
        monkeypatch.setenv('HTDT_DEVELOPER_MODE', value)
        assert developer_mode_enabled() is True
    # Lookalikes and vague values do not enable it — the lane must never
    # be reached accidentally.
    for value in ('0', 'false', '', 'maybe', '2'):
        monkeypatch.setenv('HTDT_DEVELOPER_MODE', value)
        assert developer_mode_enabled() is False
