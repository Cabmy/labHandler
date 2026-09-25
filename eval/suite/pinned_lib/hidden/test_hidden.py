import vec


def test_empty_and_negative_do_not_mutate():
    src = [1.0, -2.0]
    assert vec.scale_vec([], -1.0) == []
    out = vec.scale_vec(src, -2.0)
    assert out == [-2.0, 4.0]
    assert src == [1.0, -2.0]
    assert out is not src
