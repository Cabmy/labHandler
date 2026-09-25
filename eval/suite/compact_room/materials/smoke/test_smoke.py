import time

import kv


def test_put_get():
    assert kv.get("missing") is None
    kv.put_with_ttl("a", "one", 60_000)
    assert kv.get("a") == "one"


def test_expires():
    kv.put_with_ttl("b", "two", 20)
    time.sleep(0.05)
    assert kv.get("b") is None
