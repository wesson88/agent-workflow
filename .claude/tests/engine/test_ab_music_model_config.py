from scripts import ab_gemini_music


def test_ab_music_resolves_models_from_provider_config(monkeypatch):
    monkeypatch.setenv("GEMINI_AB_FLASH_PROVIDER", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_AB_PRO_PROVIDER", "gemini-3.1-pro-preview")

    models = ab_gemini_music.load_ab_models()

    assert models["flash"]["name"] == "gemini-3.5-flash"
    assert models["flash"]["key_env"] == "GEMINI_API_KEY_FREE"
    assert models["pro"]["name"] == "gemini-3.1-pro-preview"
    assert models["pro"]["key_env"] == "GEMINI_API_KEY_PAID"
