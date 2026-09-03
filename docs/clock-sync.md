# T-15 server clock synchronization

The authenticated T-14 connection performs five `client.clock_sync` requests
after each handshake and every 30 seconds thereafter. T-13 timestamps are
Unix epoch milliseconds. For client send `t1`, echoed server time `ts`, and
client receive `t4`, `offset = ts - (t1 + t4) / 2` and `RTT = t4 - t1`.
Elapsed measurements use a monotonic clock; the operating-system clock is
never changed. Samples with RTT above 2000 ms are rejected and the final
offset is the median of valid samples (at least three are required for
`synchronised`; otherwise status is `insufficient`).

Open `/clock-sync/` after visiting a valid participant join link. It displays
local time, estimated server time (`local + offset`), offset, RTT and sample
count. Move the client clock several minutes and run another cycle; the
estimate should remain close to the unchanged server time (normally within
200 ms, but asymmetric or highly congested networks can be less accurate).
