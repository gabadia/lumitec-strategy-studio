Perform a comprehensive performance analysis of this strategy run. Cover the following areas — query the event log as needed to get precise numbers before writing your answer:

**Profitability & P&L**
- Total realised P&L and any unrealised exposure at termination
- Fill count, average fill price, and fill quality
- Slippage per fill (expected vs actual price); flag any outliers

**Execution Quality**
- Fill ratio: orders submitted vs orders filled
- Cancellation and rejection rates and reasons
- Order churn (how many orders were cancelled and resubmitted)
- Any signs of overtrading (excessive order submission relative to fills)

**Risk & Drawdown**
- Maximum drawdown incurred
- Whether the hard-stop risk limit was approached or triggered
- Exposure duration (how long was the strategy holding a position)

**Signal Efficiency**
- Missed opportunities: bars where entry signal fired but no order was submitted (e.g. due to rate limits or pending orders)
- How many signal-positive bars resulted in actual BUY submissions vs how many were skipped

**Termination**
- How and why the strategy ended; was it expected or a risk/error stop

Summarise with a clear verdict: was this run profitable, execution-quality acceptable, and risk behaviour appropriate?
