import settings_store


def test_second_write_replaces():
    settings_store.save_pref("u", "lang", "zh")
    settings_store.save_pref("u", "lang", "en")
    got = settings_store.load_pref("u", "lang")
    assert got == "en"
    assert got != "zhen"
    assert "zh" not in got
