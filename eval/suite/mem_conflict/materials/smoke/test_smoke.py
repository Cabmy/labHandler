import settings_store


def test_save_once():
    settings_store.save_pref("u", "lang", "zh")
    assert settings_store.load_pref("u", "lang") == "zh"
