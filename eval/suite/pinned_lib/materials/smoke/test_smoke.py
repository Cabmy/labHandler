import vec


def test_scale_examples():
    assert vec.scale_vec([1.0, 2.0], 3.0) == [3.0, 6.0]
    assert vec.scale_vec([0.0], 1.0) == [0.0]
