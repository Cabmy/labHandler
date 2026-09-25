import time

import kv


def test_overwrite_after_expiry_keeps_new_ttl():
    kv.put_with_ttl("h1", "old", 20)
    time.sleep(0.05)
    assert kv.get("h1") is None
    kv.put_with_ttl("h1", "new", 60_000)
    assert kv.get("h1") == "new"


def test_second_key_survives():
    kv.put_with_ttl("h2", "left", 60_000)
    kv.put_with_ttl("h3", "right", 60_000)
    assert kv.get("h2") == "left"
    assert kv.get("h3") == "right"
