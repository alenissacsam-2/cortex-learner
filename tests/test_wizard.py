"""The local-model recommendation by hardware kind."""

from cortex.app import wizard


def test_without_an_nvidia_card_the_recommendation_stops_at_4b():
    assert wizard.recommend(16, "ram") == "4b"
    assert wizard.recommend(4, "ram") == "2b"
    assert wizard.recommend(12, "gpu") == "9b"
    assert wizard.recommend(None, "ram") == "2b"
