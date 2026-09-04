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


def test_five_samples_with_same_client_send_ms_all_accepted():
    """Regression for T-15: when five client.clock_sync requests are sent
    within the same millisecond they all share the same client_ts value.
    The queue-based pending structure on the JS side must match every
    response to a distinct send timestamp so all five samples are accepted
    and ClockSyncState reaches 'synchronised'.

    This test exercises the Python math layer with five samples that share
    the same client_send_ms, confirming none are silently dropped.
    """
    state = ClockSyncState()
    same_send = 1_700_000_000_000  # all five requests carry this timestamp
    for i in range(5):
        # Each iteration simulates one queue.shift() entry from the fixed JS:
        # epoch send time is the same, but each sample has its own monotonic pair.
        s = make_sample(
            same_send,
            same_send + 10,  # server_ms: 10 ms offset
            same_send + 20,  # client_receive_ms: 20 ms RTT
            monotonic_send=10.0 + i * 0.001,
            monotonic_receive=10.02 + i * 0.001,
        )
        assert state.add(s), f"sample {i} was rejected"
    assert state.sample_count == 5
    state.finalize()
    assert state.status == "synchronised"
    # offset = server_ms - (client_send_ms + client_receive_ms) / 2
    #        = (same_send + 10) - (same_send + same_send + 20) / 2 = 0
    assert state.offset_ms == pytest.approx(0.0)


def test_queue_shift_order_preserves_correct_rtt():
    """Verify that using an ordered queue (FIFO) means the first send
    timestamp is consumed by the first response, preserving RTT accuracy
    even when all requests share the same client_ts."""
    # Simulate two requests with the same ts but different monotonic send times.
    # This is exactly what the fixed JS pending queue produces: each queue.shift()
    # returns the monotonic_send for that particular request.
    send_ts = 1_700_000_000_000
    # First request: 100 ms monotonic elapsed; second request: 50 ms elapsed
    s1 = make_sample(
        send_ts, send_ts + 10, send_ts + 100, monotonic_send=0.0, monotonic_receive=0.1
    )
    s2 = make_sample(
        send_ts, send_ts + 10, send_ts + 50, monotonic_send=0.05, monotonic_receive=0.1
    )
    assert s1.rtt_ms == pytest.approx(100.0)
    assert s2.rtt_ms == pytest.approx(50.0)
    # offset = server_ms - (send_ts + recv_ts) / 2
    assert s1.offset_ms == pytest.approx(
        -40.0
    )  # (send_ts+10) - (send_ts + send_ts+100)/2
    assert s2.offset_ms == pytest.approx(
        -15.0
    )  # (send_ts+10) - (send_ts + send_ts+50)/2
