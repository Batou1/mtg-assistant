from app import analysis


def _card(*colors):
    return {"color_identity": list(colors)}


def test_subset_keeps_requested_colors():
    assert analysis._color_ok(_card("B"), ["B", "W"], None)
    assert analysis._color_ok(_card("W"), ["B", "W"], None)
    assert analysis._color_ok(_card("B", "W"), ["B", "W"], None)


def test_subset_excludes_off_colors():
    # Regression: a blue or green commander must never pass a black/white wish.
    assert not analysis._color_ok(_card("U"), ["B", "W"], None)
    assert not analysis._color_ok(_card("G"), ["B", "W"], None)
    assert not analysis._color_ok(_card("B", "G"), ["B", "W"], None)


def test_colorless_excluded_when_colors_requested():
    assert not analysis._color_ok(_card(), ["B", "W"], None)


def test_mono_excludes_two_color():
    # "monocouleur noir ou blanc" -> colours {B,W}, max_colors=1
    assert analysis._color_ok(_card("B"), ["B", "W"], 1)
    assert analysis._color_ok(_card("W"), ["B", "W"], 1)
    assert not analysis._color_ok(_card("B", "W"), ["B", "W"], 1)


def test_no_colors_requested_allows_anything():
    assert analysis._color_ok(_card("U", "G"), [], None)
    assert analysis._color_ok(_card(), [], None)
