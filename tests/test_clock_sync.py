import pytest

from apps.events.clock_sync import ClockSyncState, make_sample, monotonic_ms


def sample(offset, rtt=40, send=1_700_000_000_000):
    receive = send + rtt
    return make_sample(
        send,
        send + rtt / 2 + offset,
        receive,
        monotonic_send=10.0,
        monotonic_receive=10.0 + rtt / 1000,
    )


def test_ntp_offset_and_rtt():
    s = sample(125, 40)
    assert s.offset_ms == 125
    assert s.rtt_ms == pytest.approx(40)


def test_median_resists_slow_outlier():
    state = ClockSyncState()
    for value in (100, 110, 105, 1000, 95):
        assert state.add(sample(value, 50))
    state.finalize()
    assert state.offset_ms == 105
    assert state.status == "synchronised"


def test_high_rtt_rejected_and_insufficient_status():
    state = ClockSyncState()
    assert not state.add(sample(10, 2501))
    state.add(sample(10, 20))
    state.finalize()
    assert state.status == "insufficient"


def test_estimated_server_time_does_not_change_system_clock():
    state = ClockSyncState()
    state.add(sample(300))
    state.add(sample(300))
    state.add(sample(300))
    state.finalize()
    assert state.estimated_server_time_ms(1_000) == 1_300


def test_monotonic_ms_units():
    assert monotonic_ms(lambda: 12.5) == 12500
