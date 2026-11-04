"""Delivery measurements retain transit/query uncertainty and old-image compatibility."""

import pytest

from tests.scaling.clocks import offset_interval


def test_server_work_tightens_bounds_without_assuming_network_symmetry():
    observation = {
        "database_time": "1970-01-01T00:01:50.400000+00:00",
        "database_clock_timing": {"before_seconds": 0.2, "after_seconds": 0.3},
    }
    lower, upper = offset_interval(observation, sent=100, received=101)
    assert (lower, upper) == pytest.approx((9.7, 10.2))

    # A true offset anywhere in this interval fits some network/query split.
    # Neither an offset of 9.5 nor 10.3 can fit the reported server durations.
    assert lower <= 9.8 <= upper
    assert not lower <= 9.5 <= upper
    assert not lower <= 10.3 <= upper


def test_old_image_keeps_the_whole_round_trip_bound():
    observation = {"database_time": "1970-01-01T00:01:50.400000+00:00"}
    assert offset_interval(observation, sent=100, received=101) == pytest.approx((9.4, 10.4))


@pytest.mark.parametrize("before,after", [(-1, 0), (0.8, 0.3), (float("nan"), 0), (True, 0)])
def test_impossible_server_durations_cannot_qualify_delivery(before, after):
    observation = {
        "database_time": "1970-01-01T00:01:50.400000+00:00",
        "database_clock_timing": {"before_seconds": before, "after_seconds": after},
    }

    with pytest.raises(ValueError, match="clock timing"):
        offset_interval(observation, sent=100, received=101)
