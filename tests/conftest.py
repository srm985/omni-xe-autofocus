import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Never read the developer's real %APPDATA% settings during tests."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("OMNI_AUTOFOCUS_CONFIG", raising=False)
