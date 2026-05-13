Perform a latency and infrastructure timing analysis of this strategy run. Use the event log timestamps and the observe/decide/act event sequence to answer the following — query the database as needed for precise measurements:

**Observe → Decide Latency**
- Time delta between strategy.reasoning.observe events and subsequent strategy.reasoning.decide events for the same signal cycle
- Distribution: min, max, median latency across all observe→decide transitions

**Decide → Act Latency**
- Time delta between strategy.reasoning.decide events and the corresponding strategy.reasoning.act (order submission) events
- Flag any decide→act gaps over 100ms as potential bottlenecks

**Order Lifecycle Timing**
- Time from order submission (act) to first acknowledgement or fill event
- Time from submission to cancellation for any cancelled orders
- Any orders that lingered without a terminal event for more than 1 bar period

**Event Propagation & Timing Jitter**
- Gap distribution between consecutive bar events — are bars arriving consistently at the expected sampling_period_seconds interval?
- Any gaps or bursts that suggest dropped events, reconnection, or infrastructure hiccups
- Timestamp monotonicity: are any events out of chronological order?

**Latency Spikes**
- Identify the 3 worst latency outliers in the observe→decide→act chain with their timestamps and context

**Infrastructure Health**
- Were there any relay_error, connection, or heartbeat gap events?
- Did the event stream terminate cleanly or was it cut off?

Summarise: did latency or infrastructure behaviour materially affect execution quality during this run?
