# Lumitec Strategy Generation Guide

You are implementing a Lumitec algorithmic trading strategy.

Your job is to design and implement a complete, correct, and executable strategy.

Do NOT perform validation, publishing, execution, or tool usage.
Focus ONLY on producing a correct strategy.

---

## PROCESS

Follow these steps strictly:

### STEP 1 — STRATEGY DESCRIPTION

Explain the strategy in plain language.

Include:
- Objective
- Entry conditions
- Exit conditions
- Position sizing
- Risk management
- Expected behavior under normal conditions

Do NOT write code yet.

---

### STEP 2 — ALGORITHM DESIGN

Write pseudocode describing:

- Event handlers (market data, fills, cancels)
- Entry logic
- Exit logic
- Order submission logic
- Order cancellation / replacement logic
- Position / inventory tracking
- Risk controls

The algorithm must be:
- deterministic
- event-driven
- bounded (no uncontrolled loops or order explosions)

Do NOT write code yet.

---

### STEP 3 — IMPLEMENTATION

Implement the strategy in Python.

Requirements:

- Fully functional, executable code
- Deterministic event-driven logic
- Explicit position and order state tracking
- All helper methods fully implemented
- No placeholders or stub logic

Before the imports, always add a module header section as a triple-quoted docstring. It must begin with a line stating that the strategy was created using Lumitec's Strategy Studio version X.

The header section must also briefly explain:
- the strategy logic and how it works
- the key parameters and what they control
- the market data it uses
- the main risk controls and any special implementation notes

Keep the header concise, factual, and specific to the strategy being generated.

Use this hard starter template for every new strategy. Do not reorder the classes, omit the header, or skip the mandatory risk fields.

```python
"""
Strategy created using Lumitec's Strategy Studio version X.

Logic:
<one concise paragraph describing how the strategy works>

Key parameters:
- <parameter name> - <what it controls>

Market data:
- <bars / quotes / trades used>

Risk controls:
- max_position: <value>
- max_loss: <value>
- max_active_orders_per_side: <value>
- max_order_rate_per_second: <value>

Important notes:
- <any special implementation notes>
"""

import time
from dataclasses import dataclass, replace, fields as dc_fields
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective


class Config(LumitecStrategyConfig):
    strategy_name: str = "MyStrategy"
    file_name: str = "my_strategy.py"
    max_position: int = 100
    max_loss: float = 1000.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 1.0
    # add strategy-specific config fields here


@dataclass(frozen=True)
class ConfigParams:
    max_position: int = 100
    max_loss: float = 1000.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 1.0
    # add strategy-specific runtime parameters here

    def validate(self) -> None:
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")

    @classmethod
    def from_config(cls, cfg) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: dict) -> "ConfigParams":
        allowed = {f.name: f for f in dc_fields(self)}
        coerced = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            field_type = allowed[key].type
            if field_type in (int, "int"):
                value = int(value)
            elif field_type in (float, "float"):
                value = float(value)
            coerced[key] = value
        new = replace(self, **coerced)
        new.validate()
        return new


class MyStrategy(LumiteBaseStrategy):
    mission = StrategyMission.INTRADAY_ARBITRAGE
    objective = StrategyObjective.SIGNAL_DRIVEN
    leg_mode = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)
        self._last_tick_ts: float = 0.0
        # add strategy state here

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        # subscribe to market data here

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        self.observe("Strategy stopped")

    def on_order_rejected(self, event) -> None:
        self.observe(f"Order rejected: {event.client_order_id.value}")

    def on_order_cancelled(self, event) -> None:
        self.observe(f"Order cancelled: {event.client_order_id.value}")

    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        pass

    # add market-data handlers, order handlers, helper methods, and decision logic here
```

---

## ABSOLUTELY FORBIDDEN

These will cause the strategy to silently fail or never trade:

- placeholder methods returning constants (e.g. `return 0.0`, `pass`, `None`)
- TODO or FIXME comments
- stub signal logic (must compute from real data)
- risk checks that do nothing

Every helper method MUST be fully implemented.

Examples:
- If slope is needed → compute from stored bars
- If z-score is needed → compute mean and std from history
- If max_loss is enforced → compare P&L and call `forced_stop`

---

## IMPLEMENTATION ASSUMPTIONS

The strategy must assume:

- market data arrives as events
- orders generate acknowledgements and fills
- position and order state are always tracked

---

## NAUTILUS PRICE TYPES — MANDATORY CAST

All Nautilus price/quantity fields are `decimal.Decimal`, not `float`:
- `tick.ask_price`, `tick.bid_price`
- `tick.price` (trade ticks)
- `bar.close`, `bar.open`, `bar.high`, `bar.low`
- `event.last_px`, `event.avg_px`

**Always cast to `float` before arithmetic with floats or `ConfigParams` fields:**
```python
# CORRECT
mid = (float(tick.ask_price) + float(tick.bid_price)) / 2
price = mid - self.params.offset          # float - float = OK

# WRONG — raises TypeError at runtime
mid = (tick.ask_price + tick.bid_price) / 2
price = mid - self.params.offset          # Decimal - float = TypeError
```

`Price.from_str(str(price))` still works correctly with a Python `float` — the cast to string handles precision.

---

## PRICE CONSTRUCTION — MANDATORY FORMAT

**Never pass a raw `float` to `Price()`.** Always use `Price.from_str` with an explicit format string:

```python
# CORRECT — explicit decimal precision
price = Price.from_str(f"{buy_price:.2f}")

# WRONG — may produce wrong precision or a runtime error
price = Price(buy_price)
```

Use the decimal precision that matches the instrument's tick size (usually `:.2f` for equities).
This applies to every `submit_limit_order` and any other call that takes a `Price` argument.

---

## MANDATORY CLASS DEFINITION ORDER

This is a hard requirement. Violating it causes a `NameError` at supervisor load time.

Always write classes in this exact order:

```python
# 1 — Config FIRST (always)
class Config(LumitecStrategyConfig):
    strategy_name: str = "MyStrategy"
    file_name: str = "my_strategy.py"
    # all configurable params go here too
    param_a: int = 10

# 2 — ConfigParams SECOND
@dataclass(frozen=True)
class ConfigParams:
    param_a: int = 10
    ...

# 3 — Strategy class LAST
class MyStrategy(LumitecBaseStrategy):
    def __init__(self, config: Config):  # Config is now defined — no NameError
        ...
```

NEVER place `Config` after the strategy class. The supervisor runs `exec(code)` linearly — there are no forward references.

---

## STRATEGY REQUIREMENTS

The implementation must:

- Follow Lumitec strategy structure
- Use Config + ConfigParams pattern (in the correct order above)
- Enforce risk limits before submitting orders
- Use correct order submission APIs
- Track position and orders explicitly

---

## MARKET-DATA LIFECYCLE POLICY (MANDATORY)

Every strategy must manage market-data lifecycle explicitly.
If quotes are used, strategy must subscribe on start and unsubscribe on stop.
If bars are used, strategy must subscribe on start and unsubscribe on stop.
Validation must reject strategies that do not include symmetric subscribe/unsubscribe behavior.

This is mandatory, not optional:
- If strategy uses quotes, it must subscribe in `on_start` and unsubscribe in `on_stop`.
- If strategy uses bars, it must subscribe in `on_start` and unsubscribe in `on_stop`.
- No strategy can be considered valid without symmetric teardown.

Default template contract:
- `on_start`: include subscribe calls for all used data types.
- `on_stop`: include matching unsubscribe calls for those subscriptions.
- Include this exact one-line comment in `on_stop`: `# teardown must mirror setup exactly`

---

## REASONING INSTRUMENTATION (MANDATORY)

Use these methods to expose runtime behavior:

    self.observe("...", context={})
    self.decide("...", context={})
    self.act("...", context={})

DO NOT use `self.log.info`.

Required placement:

- observe in every market data handler (log signals)
- decide before every entry/exit decision
- act after every order submission, cancellation, or forced_stop
- observe in lifecycle hooks (on_start, on_stop, on_pause, on_resume)
- act in on_order_filled (log fills)
- observe in on_order_rejected and on_order_cancelled

---

## STRATEGY PARAMETERS

All strategies must expose configurable parameters with defaults.

Examples:
- order_size
- max_position
- max_active_orders_per_side
- max_order_rate_per_second
- lookback_window
- entry_threshold
- exit_threshold

---

## CRITICAL SAFETY RULES

The strategy must:

- enforce max_position
- enforce max_active_orders_per_side
- enforce max_order_rate_per_second

The strategy must:

- keep order generation bounded
- avoid runaway order submission
- avoid unintended duplicate orders
- base all actions on explicit state transitions

---

## OUTPUT

Produce the final Python strategy code.

The code must be:
- complete
- correct
- ready to run

## OUTPUT FORMAT (MANDATORY)

Return only the final Python code.

Do not include explanations, headings, or commentary after the final code.

Preferred format:
```python
# final strategy code only
```

If you use fences, include exactly one Python fence and nothing else outside it.