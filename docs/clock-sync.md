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

## Duplicate-timestamp handling

Five `client.clock_sync` requests are sent in rapid succession each cycle.
Because `Date.now()` has millisecond resolution, all five can share the same
`client_ts` value. The debug page maintains a per-timestamp FIFO queue of
monotonic send times so every response is matched to the correct request even
when all five carry identical timestamps:

```js
// send
const q = pending.get(ts) || [];
q.push(performance.now());
pending.set(ts, q);

// receive
const q = pending.get(p.client_ts_echo);
if (!q || !q.length) return;
const sent = q.shift();
if (!q.length) pending.delete(p.client_ts_echo);
```

No protocol change is required; the fix is entirely client-side and
preserves the T-13 wire format unchanged.
