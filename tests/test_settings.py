from app.settings import Settings, get_settings, secret_path


def test_secret_path():
    assert secret_path("proj", "gmail-token") == "projects/proj/secrets/gmail-token/versions/latest"
    full = "projects/p/secrets/s/versions/3"
    assert secret_path("proj", full) == full
    assert secret_path("proj", "projects/p/secrets/s") == "projects/p/secrets/s/versions/latest"


def test_settings_from_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SHEET_ID", "sheet-1")
    monkeypatch.setenv("GCP_PROJECT", "proj")
    settings = get_settings()
    assert (settings.sheet_id, settings.gcp_project) == ("sheet-1", "proj")
    assert settings.sheet_url == "https://docs.google.com/spreadsheets/d/sheet-1"
    get_settings.cache_clear()
    assert Settings().service_url == ""
