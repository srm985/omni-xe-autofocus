import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Never read the developer's real %APPDATA% settings during tests."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # no LightBurn prefs
    monkeypatch.delenv("OMNI_AUTOFOCUS_CONFIG", raising=False)
    from omni_autofocus import config

    monkeypatch.setattr(config, "COMMARKER_DIR", tmp_path / "no-commarker")
