"""The results summary: totals over the trips compared with a standard route."""

from app.evaluate import MEASURES, Outcome, headline


def outcome(*, standard_found: bool = True, differs: bool, **measured: tuple[float, float]) -> Outcome:
    numbers = {key: measured.get(key, (0.0, 0.0)) for key, _ in MEASURES}
    return Outcome("A → B", "Our safer design" if differs else "Standard route", standard_found, differs, numbers)


def test_the_headline_totals_the_trips_compared_with_a_standard_route():
    safer = outcome(differs=True, risky_turns=(3, 1), minutes=(20, 26), lane_closures=(1, 0))
    same = outcome(differs=False, risky_turns=(2, 2), minutes=(15, 15))
    unmatched = outcome(standard_found=False, differs=False, risky_turns=(9, 9), minutes=(50, 50))
    numbers = headline([safer, same, unmatched], live_traffic=False)
    assert (numbers["trips"], numbers["different"]) == (2, 1)
    assert (numbers["risky_turns_standard"], numbers["risky_turns_ours"]) == (5, 3)
    assert (numbers["lane_closures_standard"], numbers["lane_closures_ours"]) == (1, 0)
    assert numbers["minutes_added_per_trip"] == 3.0 and numbers["live_traffic"] is False
