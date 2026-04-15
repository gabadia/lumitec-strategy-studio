## Base class
```python
from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
```

Every strategy must extend `LumitecBaseStrategy` and declare these class attributes:
```python
class MyStrategy(LumitecBaseStrategy):
    mission    = StrategyMission.INTRADAY_ARBITRAGE   # WHY it trades
    objective  = StrategyObjective.SIGNAL_DRIVEN      # WHEN it is done
    leg_mode   = LegMode.CONTINUOUS                   # FINITE | CONTINUOUS | CONDITIONAL

    # Declares expected legs to the UI — drives auto-population and side locking
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]
```

`leg_schema` fields: `label` (string shown in UI), `side` (`"BUY"`, `"SELL"`, or `None` for user-selectable), `fixed_side` (`True` locks the side in the UI).

Available `LegMode` values:

| Value | Description |
|-------|-------------|
| `LegMode.FINITE` | Single-shot execution — strategy ends when the leg completes (e.g. `midpoint_peg.py`) |
| `LegMode.CONTINUOUS` | Ongoing execution — strategy keeps running across multiple cycles (e.g. `claude_pairs_strategy.py`) |
| `LegMode.CONDITIONAL` | Conditional execution — strategy activates only when a condition is met |

Available missions: `EXECUTION`, `MARKET_MAKING`, `INTRADAY_ARBITRAGE`,
`CROSS_MARKET_ARBITRAGE`, `INVENTORY_MANAGEMENT`, `SPECULATIVE`

Available objectives: `UNKNOWN`, `TARGET_QTY`, `TARGET_VALUE`, `TARGET_PARTICIPATION`,
`VWAP`, `TWAP`, `SIGNAL_DRIVEN`, `INVENTORY_TARGET`, `INTRADAY_ARBITRAGE`,
`CROSS_MARKET_ARBITRAGE`

---

## Config pattern

Two-layer config is REQUIRED. Both classes must be present or the supervisor will reject the strategy.

**CRITICAL: `Config` and `ConfigParams` MUST be top-level classes — NOT nested inside the strategy class.**

✅ Correct:
```python
class Config(LumitecStrategyConfig):          # top-level
    ...

@dataclass(frozen=True)
class ConfigParams:                            # top-level
    ...

class MyStrategy(LumitecBaseStrategy):        # top-level
    ...
```

❌ Wrong — supervisor cannot find Config:
```python
class MyStrategy(LumitecBaseStrategy):
    class Config(LumitecStrategyConfig):      # NESTED — FORBIDDEN
        ...
    class ConfigParams:                       # NESTED — FORBIDDEN
        ...
```

```python
# Layer 1: metadata (platform fills this on submission — set your defaults here)
# MANDATORY — without this class the supervisor cannot load the strategy
class Config(LumitecStrategyConfig):
    strategy_name: str = "MyStrategy"
    file_name: str = "my_strategy.py"

# Layer 2: strategy-specific params (frozen dataclass, all hot-updatable)
@dataclass(frozen=True)
class ConfigParams:
    param_a: int = 10
    param_b: float = 2.0

    def validate(self) -> None:
        if self.param_a <= 0:
            raise ValueError("param_a must be > 0")

    @classmethod
    def from_config(cls, cfg) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: dict) -> "ConfigParams":
        allowed = {f.name: f for f in dc_fields(self)}
        coerced = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            if allowed[k].type in (int, "int"):
                v = int(v)
            elif allowed[k].type in (float, "float"):
                v = float(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new
```

---

## Required hooks

### set_oms_type — mandatory, without it the strategy crashes on startup
```python
def set_oms_type(self, oms_type) -> None:
    self._oms_type = oms_type
```

### on_start — subscribe to market data here
```python
def on_start(self) -> None:
    super().on_start()              # must call super()
    self.leg_a, self.leg_b = self.legs   # legs injected by platform
    self.symbol_a = self.leg_a["symbol"]

    self.subscribe_market_data_bars(
        symbol=self.symbol_a,
        aggregation=BarAggregation.SECOND,
        step=self.params.sampling_period_seconds,
        price_type=PriceType.MID,
    )
```

### apply_params + configure — required for hot parameter updates
```python
def apply_params(self, updates: dict) -> None:
    with self._param_lock:          # RLock — required
        self.params = self.params.merged(updates)

def configure(self, **extras) -> None:
    sp = extras.get("strategy_params")
    if isinstance(sp, dict):
        self.apply_params(sp)
```

### validate_legs — enforce leg constraints before the strategy is instantiated
```python
@classmethod
def validate_legs(cls, legs: list) -> None:
    if len(legs) != 1:
        raise ValueError("MyStrategy requires exactly 1 leg")
    if legs[0].get("side") != "SELL":
        raise ValueError("MyStrategy leg must be SELL")
```
Called by the supervisor controller before instantiation. Raise `ValueError` with a clear message — it surfaces as a 400 to the caller. Must be consistent with what `leg_schema` declares.

**CRITICAL:** `leg_schema` and `validate_legs` must be consistent:
- If `leg_schema` has `"side": "BUY", "fixed_side": True` → `validate_legs` must check `legs[0].get("side") == "BUY"`
- If `leg_schema` has `"side": None, "fixed_side": False` → `validate_legs` must NOT check the side value
- Never declare a fixed side in `leg_schema` and then require user-selectable in `validate_legs`, or vice versa

### on_stop, on_order_rejected, on_order_cancelled — must be present
```python
def on_stop(self) -> None:
    self.observe("Strategy stopped")

def on_order_rejected(self, event) -> None:
    self.observe(f"Order rejected: {event.client_order_id.value}")

def on_order_cancelled(self, event) -> None:
    self.observe(f"Order cancelled: {event.client_order_id.value}")
```

---

## Market data
```python
# Bar data (recommended — fires on_symbol_bar)
self.subscribe_market_data_bars(symbol, BarAggregation.SECOND, step=5, price_type=PriceType.MID)

# Quote / trade ticks (fires on_symbol_quote_tick / on_symbol_trade_tick)
self.subscribe_market_data(symbol, subscribe_quotes=True, subscribe_trades=False)

# Last quote
quote = self.last_quote(symbol)   # None if no data yet
mid = (quote.ask_price + quote.bid_price) / 2
```

Override the handler that matches your subscription:
```python
def on_symbol_bar(self, symbol: str, bar) -> None: ...
def on_symbol_quote_tick(self, symbol: str, tick) -> None: ...
def on_symbol_trade_tick(self, symbol: str, tick) -> None: ...
```

**Important:** anchor all signal logic to one symbol's bars to avoid double-firing:
```python
def on_symbol_bar(self, symbol: str, bar) -> None:
    if symbol != self.symbol_a:
        return
    # ... evaluate signal
```

---

## Order submission
```python
from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price

# Limit order — returns order object (.client_order_id.value is the ID string)
order = self.submit_limit_order(
    symbol=symbol,
    side=OrderSide.BUY,
    qty=100,
    price=Price.from_str("150.25"),
    tif=TimeInForce.DAY,
    leg_id="A",     # used to track fills per leg
    role="OPEN",    # OPEN / CLOSE / NEUTRALIZE
)
order_id_str = order.client_order_id.value

# Market order — returns ClientOrderId directly
clid = self.submit_market_order(
    symbol=symbol,
    side=OrderSide.SELL,
    qty=100,
    leg_id="A",
    role="CLOSE",
)
order_id_str = clid.value
```

---

## Order ID and leg tracking

Order IDs encode leg and role: `O-MyStrategy-001-1:A-OPEN`
```python
def on_order_filled(self, event) -> None:
    oid = event.client_order_id.value
    leg_id, role = self.extract_leg_info_from_order_id(oid)

    if leg_id is None:
        self.log.warning(f"Unknown leg_id for order {oid}")
        return

    self.act(f"Fill confirmed leg={leg_id} role={role}")
```

---

## Reasoning (observe / decide / act)

Use these instead of `self.log.info` — they appear in the demo-ui reasoning panel:
```python
self.observe("Scanning for entry", context={"z": 2.3})   # informational
self.decide("Entry signal detected", context={"z": 2.3}) # decision point
self.act("Submitted BUY 100 MSFT", context={"order_id": oid})  # action taken
```

---

## Session Window (start_time / end_time)

Every strategy submission must include `start_time` and `end_time`. These are required fields — the supervisor rejects submissions that omit them.

### Format — full ISO 8601 with any timezone offset
The server normalizes all times to UTC. Display times to the user in their local timezone; always include the offset when submitting.
```
2026-03-24T09:30:00-04:00   # EDT (summer) — server converts to 13:30 UTC
2026-03-24T09:30:00-05:00   # EST (winter) — server converts to 14:30 UTC
2026-03-24T13:30:00Z        # UTC directly — also accepted
```
Time-only strings (`"09:30:00"`) are not accepted.

### Rules
- Both must be today's date (extraday not yet supported)
- `end_time` must be after `start_time` and in the future

### Defaults (NYSE hours)
```python
start_time = "TODAY T09:30:00-04:00"   # NYSE open (EDT) — adjust offset for EST
end_time   = "TODAY T16:00:00-04:00"   # NYSE close (EDT)
```
The demo-ui pre-populates these. When submitting via MCP (`mcp__lumitec__submit_strategy`), always pass today's full ISO datetime — the tool accepts them in the `start_time` / `end_time` fields of the payload.

**When asking the user to confirm or change start/end times, always present them in the user's local timezone.** Never show UTC or ET to the user unless they explicitly ask for it. The ISO string you submit must still include the correct offset for whatever timezone you used.

### Session time helpers (use these — never call `datetime.now()` directly)
```python
# ✅ Use base class helpers — UTC-based, always correct
if not self._is_trading_time():
    return
if self._check_end_time_reached():
    self.forced_stop("End time reached", "TIME")
```

```python
# ❌ Never do this — naive local time, breaks across timezones
if datetime.now().time() > end_time:          # WRONG
    self.forced_stop("End time reached", "TIME")
if datetime.now(ZoneInfo("America/New_York")).time() > end_time:  # ALSO WRONG
    self.forced_stop("End time reached", "TIME")
```

Strategies using `_is_trading_time()` and `_check_end_time_reached()` need no changes — they automatically get correct UTC-based behaviour.

### What the platform does with them
- Strategy starts **paused** if submitted before `start_time`; base class unpauses at open
- Supervisor **hard-stops** the strategy at `end_time` regardless of state

---

## Cancel orders
```python
self.cancelAllOrders()              # cancel everything
self.cancelOrdersForSymbol(symbol)  # cancel for one symbol
```

---

## Pause / Resume

The platform pauses strategies before their session `start_time` and resumes them at open. The supervisor can also pause/resume at any time via the API.

### Guard every market data handler
```python
def on_symbol_bar(self, symbol: str, bar) -> None:
    if self.isPaused():
        return
    # ... signal logic

def on_symbol_quote_tick(self, symbol: str, tick) -> None:
    if self.isPaused():
        return
    # ... tick logic
```

### React to pause/resume — override hooks, not the base methods
```python
def on_pause(self, reason: str = "") -> None:
    self.cancelAllOrders()
    self.act("Paused", context={"reason": reason})

def on_resume(self) -> None:
    self.act("Resumed")
    self._rebuild_signal_engine()   # if applicable
```

Never override `pause()` or `resume()` directly — those are base class methods that manage state and call your hooks.

---

## Graceful stop

`forced_stop(reason, stop_reason)` is the only correct way to stop a strategy from within. It emits a `FORCED_STOP` lifecycle event (consumed by the aggregator and SSE gateway), then calls Nautilus `stop()`.

`stop_reason` must be one of: `"MANUAL"` | `"TIME"` | `"RISK"` | `"SYSTEM"`

```python
# Strategy completed its objective (target filled, positions neutralised, etc.)
self.forced_stop("Target quantity filled", "MANUAL")

# Risk limit hit (max loss, max gain, stop-loss, drawdown, etc.)
self.forced_stop("Max loss reached", "RISK")
```

> **`manual_stop()` no longer exists.** Every call site must use `forced_stop(reason, stop_reason)`.
> If a strategy calls plain `stop()` instead, the blotter/SSE will not receive a `FORCED_STOP` event
> and `stop_reason` will be lost.

---

## Validation checklist

Before publishing, all 16 of these must be present in your file:

| # | Pattern |
|---|---------|
| 1 | `@dataclass(frozen=True)` on ConfigParams |
| 2 | `validate()` method in ConfigParams |
| 3 | `merged()` method in ConfigParams |
| 4 | `from_config()` classmethod in ConfigParams |
| 5 | `apply_params()` with `RLock` |
| 6 | `configure()` accepting `strategy_params` dict |
| 7 | `on_stop()` |
| 8 | `on_order_rejected()` |
| 9 | `on_order_cancelled()` |
| 10 | `set_oms_type()` |
| 11 | Rebuild signal data in `apply_params()` when thresholds change |
| 12 | Guard `on_order_filled` for unknown `leg_id` |
| 13 | `leg_schema` class attribute declaring expected legs |
| 14 | `validate_legs()` classmethod enforcing leg count and side |
| 15 | `isPaused()` guard at top of every market data handler |
| 16 | `on_pause()` / `on_resume()` hooks (not `pause()`/`resume()`) |

Forbidden imports (publish will be rejected if found):
`subprocess`, `socket`, `requests`, `os.system`, `urllib`

---

## Template

See `examples/pairs_template.py` for a complete working skeleton with all required patterns.