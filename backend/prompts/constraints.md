# Lumitec Strategy Constraints

This file is injected into all agent prompts that operate on Lumitec strategies.
Do not modify the rules — they reflect platform requirements enforced by the supervisor.

---

## Required Safety Limits

Every strategy must declare and enforce:

| Limit | Description |
|---|---|
| `max_position` | Maximum allowed position (shares / contracts) at any time |
| `max_loss` | Maximum allowed cumulative loss before forced stop |
| `max_active_orders_per_side` | Maximum open orders on BUY side or SELL side simultaneously |
| `max_order_rate_per_second` | Maximum order submissions per second |

These must appear as fields in `ConfigParams` with explicit defaults.
The strategy logic must check these limits before every order submission.

---

## Forbidden Imports

The following imports will cause the supervisor to reject the strategy at load time:

- `subprocess`
- `socket`
- `requests`
- `os.system`
- `urllib`

---

## Required Imports

```python
from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
```

---

## Validation Checklist (22 required patterns)

| # | Pattern |
|---|---|
| 0 | `Config` class present as a **top-level class** (not nested), inheriting from `LumitecStrategyConfig`, with `strategy_name` and `file_name` fields. **`Config` MUST be the first class defined in the file — before `ConfigParams` and before the strategy class.** |
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
| 16 | `on_pause()` / `on_resume()` hooks |
| 17 | `self.params` and `self._param_lock = RLock()` initialised in `__init__`, not `on_start` |
| 18 | `self.observe()` calls in every market data handler logging signal values |
| 19 | `self.decide()` calls before every entry and exit decision |
| 20 | `self.act()` calls after every order submission, cancellation, and forced_stop |
| 22 | All arithmetic using `tick.ask_price`, `tick.bid_price`, `tick.price`, `bar.close`, `bar.open`, `bar.high`, `bar.low` MUST cast to `float` first — these are `decimal.Decimal` in Nautilus. Use `float(tick.ask_price)`. Never mix `Decimal` with `float` in `-`, `+`, `*`, `/` expressions. |
| 21 | Tick throttle guard in `on_quote_tick`/`on_trade_tick`/`on_symbol_quote_tick`/`on_symbol_trade_tick` — `if time.monotonic() - self._last_tick_ts < self.params.tick_throttle_interval: return`; add `self._last_tick_ts: float = 0.0` in `__init__`; add `tick_throttle_interval: float = 1.0` to `ConfigParams` |
| 23 | Every `Price(...)` construction MUST use `Price.from_str(f"{price_float:.2f}")` — NEVER `Price(float_value)`. Passing a raw `float` to `Price()` can produce incorrect precision or a runtime error. Always format the float to the required decimal places first: `Price.from_str(f"{buy_price:.2f}")`. |

> **Authoritative fix guide with minimal-fix examples**: see [`validation_loop.md`](validation_loop.md).

---

## Mandatory Class Definition Order

Python evaluates class bodies and function annotations at `exec()` time, so forward references to not-yet-defined classes
cause a `NameError` in the supervisor (`strategy_loader.py`).

**The file MUST declare classes in this order, with no exceptions:**

```
1. imports
2. class Config(LumitecStrategyConfig):          ← MUST BE FIRST
3. @dataclass(frozen=True)
   class ConfigParams:                          ← MUST BE SECOND
4. class MyStrategy(LumitecBaseStrategy):       ← MUST BE LAST
```

If `Config` is placed anywhere after the strategy class, the supervisor will raise:
```
NameError: name 'Config' is not defined
```
because the strategy class body contains `def __init__(self, config: Config)` which Python evaluates at class-definition time.

---

## Graceful Stop

Use `forced_stop(reason, stop_reason)` — never plain `stop()`.

`stop_reason` must be one of: `"MANUAL"` | `"TIME"` | `"RISK"` | `"SYSTEM"`

---

## Order ID Format

Order IDs encode leg and role: `O-StrategyName-001-1:A-OPEN`

Always use `self.extract_leg_info_from_order_id(oid)` to parse them.
