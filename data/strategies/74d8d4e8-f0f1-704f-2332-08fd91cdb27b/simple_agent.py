"""
Strategy created using Lumitec's Strategy Studio version X - WhatsApp.

Logic:
Simple Agent executes a fixed two-phase trade on a single instrument: it submits
a market BUY order for a configurable number of shares (order_size) as soon as the
strategy becomes active, waits a configurable number of seconds (hold_seconds), then
submits a market SELL order for the same quantity. The strategy stops cleanly after
the sell fill is confirmed. An early exit is triggered if the unrealised P&L falls
below -max_loss before the hold period expires.

Key parameters:
- order_size       - number of shares to buy and then sell
- hold_seconds     - how many seconds to hold the position before selling
- max_loss         - maximum tolerated unrealised loss (in dollars) before forced exit
- max_position     - hard cap on shares held (safety guard, should equal order_size)
- max_active_orders_per_side  - capped at 1; only one order per side at a time
- max_order_rate_per_second   - rate limiter on order submissions
- bar_step_seconds - bar aggregation period driving the periodic check loop
- tick_throttle_interval - minimum seconds between observe/decide/act calls in tick handler

Market data:
- 1-second OHLCV bars (MID price) to drive the periodic check loop
- Quote ticks for real-time mid-price estimation during the hold phase

Risk controls:
- max_position: 500
- max_loss: 500.0
- max_active_orders_per_side: 1
- max_order_rate_per_second: 2.0

Important notes:
- The strategy uses LegMode.FINITE because it executes one buy-hold-sell cycle and stops.
- Phase transitions are driven by bar events to avoid tick-level over-firing.
- If the BUY order is rejected or canceled the strategy retries on the next bar.
- If the SELL order is rejected or canceled the strategy retries on the next bar.
- Price arithmetic always casts Nautilus Decimal fields to float before operations.
"""

import time
from dataclasses import dataclass, replace, fields as dc_fields
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType


# ── Phase sentinel values ─────────────────────────────────────────────────────
_IDLE    = "IDLE"
_BUYING  = "BUYING"
_HOLDING = "HOLDING"
_SELLING = "SELLING"
_DONE    = "DONE"


class Config(LumitecStrategyConfig):
    strategy_name: str = "SimpleAgent"
    file_name: str = "simple_agent.py"
    # risk fields
    max_position: int = 500
    max_loss: float = 500.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 2.0
    # strategy-specific fields
    order_size: int = 100
    hold_seconds: float = 30.0
    bar_step_seconds: int = 1
    tick_throttle_interval: float = 1.0


@dataclass(frozen=True)
class ConfigParams:
    max_position: int = 500
    max_loss: float = 500.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 2.0
    order_size: int = 100
    hold_seconds: float = 30.0
    bar_step_seconds: int = 1
    tick_throttle_interval: float = 1.0

    def validate(self) -> None:
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")
        if self.order_size <= 0:
            raise ValueError("order_size must be > 0")
        if self.order_size > self.max_position:
            raise ValueError("order_size must not exceed max_position")
        if self.hold_seconds <= 0:
            raise ValueError("hold_seconds must be > 0")
        if self.bar_step_seconds <= 0:
            raise ValueError("bar_step_seconds must be > 0")
        if self.tick_throttle_interval <= 0:
            raise ValueError("tick_throttle_interval must be > 0")

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


class SimpleAgent(LumitecBaseStrategy):
    mission    = StrategyMission.EXECUTION
    objective  = StrategyObjective.TARGET_QTY
    leg_mode   = LegMode.FINITE
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    # ── Construction ──────────────────────────────────────────────────────────
    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)

        # phase state
        self._phase: str = _IDLE

        # position tracking
        self._position: int = 0
        self._entry_price: float = 0.0
        self._entry_time: float = 0.0          # monotonic seconds

        # order tracking
        self._active_order_id: str | None = None

        # rate limiter state
        self._last_order_ts: float = 0.0

        # tick throttle
        self._last_tick_ts: float = 0.0

        # instrument reference — populated in on_start
        self._symbol: str = ""

    # ── Platform hooks ────────────────────────────────────────────────────────
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        (self._leg_a,) = self.legs
        self._symbol = self._leg_a["symbol"]

        self.subscribe_market_data_bars(
            symbol=self._symbol,
            aggregation=BarAggregation.SECOND,
            step=self.params.bar_step_seconds,
            price_type=PriceType.MID,
        )
        self.subscribe_market_data(
            self._symbol,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        self._phase = _IDLE
        self.observe(
            "SimpleAgent started",
            context={
                "symbol": self._symbol,
                "order_size": self.params.order_size,
                "hold_seconds": self.params.hold_seconds,
            },
        )

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        self.unsubscribe_market_data_bars(
            symbol=self._symbol,
            aggregation=BarAggregation.SECOND,
            step=self.params.bar_step_seconds,
            price_type=PriceType.MID,
        )
        self.unsubscribe_market_data(
            self._symbol,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self.observe("SimpleAgent stopped", context={"phase": self._phase})

    # ── Param hot-update ──────────────────────────────────────────────────────
    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ── Leg validation ────────────────────────────────────────────────────────
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError("SimpleAgent requires exactly 1 leg")

    # ── Pause / Resume ────────────────────────────────────────────────────────
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._active_order_id = None
        # if mid-buy, reset to IDLE so we retry on resume
        if self._phase == _BUYING:
            self._phase = _IDLE
        # if mid-sell, stay HOLDING so we retry on resume
        if self._phase == _SELLING:
            self._phase = _HOLDING
        self.act("Paused — orders canceled", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Resumed", context={"phase": self._phase})

    # ── Market data handlers ──────────────────────────────────────────────────
    def on_symbol_bar(self, symbol: str, bar) -> None:
        if self.isPaused():
            return
        if symbol != self._symbol:
            return
        if self._check_end_time_reached():
            self._emergency_exit("End time reached", "TIME")
            return

        if self._phase == _IDLE:
            self._try_buy()

        elif self._phase == _HOLDING:
            self._check_hold_and_exit(bar)

    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if self.isPaused():
            return
        now = time.monotonic()
        if now - self._last_tick_ts < self.params.tick_throttle_interval:
            return
        self._last_tick_ts = now

        if symbol != self._symbol:
            return

        mid = (float(tick.ask_price) + float(tick.bid_price)) / 2
        self.observe(
            "Quote tick",
            context={"mid": round(mid, 4), "phase": self._phase},
        )

    # ── Entry logic ───────────────────────────────────────────────────────────
    def _try_buy(self) -> None:
        if self._active_order_id is not None:
            return  # order already in flight
        if self._position >= self.params.max_position:
            self.observe("Max position reached — cannot buy")
            return
        if not self._rate_ok():
            return

        self.decide(
            "Submitting market BUY",
            context={"qty": self.params.order_size, "symbol": self._symbol},
        )
        clid = self.submit_market_order(
            symbol=self._symbol,
            side=OrderSide.BUY,
            qty=self.params.order_size,
            leg_id="A",
            role="OPEN",
        )
        self._active_order_id = clid.value
        self._phase = _BUYING
        self._last_order_ts = time.monotonic()
        self.act(
            "Market BUY submitted",
            context={"order_id": self._active_order_id, "qty": self.params.order_size},
        )

    # ── Hold / exit logic ─────────────────────────────────────────────────────
    def _check_hold_and_exit(self, bar) -> None:
        if self._active_order_id is not None:
            return  # sell already in flight

        elapsed = time.monotonic() - self._entry_time
        current_mid = float(bar.close)
        unrealised_pnl = (current_mid - self._entry_price) * self._position

        self.observe(
            "Holding",
            context={
                "elapsed_s": round(elapsed, 1),
                "hold_seconds": self.params.hold_seconds,
                "unrealised_pnl": round(unrealised_pnl, 2),
            },
        )

        time_exit  = elapsed >= self.params.hold_seconds
        loss_exit  = unrealised_pnl <= -self.params.max_loss

        if time_exit:
            self.decide("Hold period elapsed — initiating sell")
            self._try_sell("TIME_EXIT")
        elif loss_exit:
            self.decide(
                "Max loss breached — initiating emergency sell",
                context={"unrealised_pnl": round(unrealised_pnl, 2)},
            )
            self._try_sell("LOSS_EXIT")

    def _try_sell(self, reason: str) -> None:
        if self._active_order_id is not None:
            return
        if not self._rate_ok():
            return

        clid = self.submit_market_order(
            symbol=self._symbol,
            side=OrderSide.SELL,
            qty=self._position,
            leg_id="A",
            role="CLOSE",
        )
        self._active_order_id = clid.value
        self._phase = _SELLING
        self._last_order_ts = time.monotonic()
        self.act(
            "Market SELL submitted",
            context={
                "order_id": self._active_order_id,
                "qty": self._position,
                "reason": reason,
            },
        )

    def _emergency_exit(self, reason: str, stop_reason: str) -> None:
        if self._phase == _HOLDING:
            self._try_sell("EMERGENCY")
        self.forced_stop(reason, stop_reason)

    # ── Fill handler ──────────────────────────────────────────────────────────
    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)

        if leg_id is None:
            self.log.warning(f"Unknown leg_id for order {oid}")
            return

        fill_price = float(event.last_px)
        fill_qty   = int(event.last_qty)

        self.act(
            "Order filled",
            context={
                "order_id": oid,
                "leg_id": leg_id,
                "role": role,
                "fill_price": fill_price,
                "fill_qty": fill_qty,
            },
        )

        self._active_order_id = None

        if role == "OPEN":
            # BUY confirmed
            self._position   = fill_qty
            self._entry_price = fill_price
            self._entry_time  = time.monotonic()
            self._phase       = _HOLDING
            self.observe(
                "Position opened",
                context={
                    "entry_price": fill_price,
                    "qty": fill_qty,
                    "hold_seconds": self.params.hold_seconds,
                },
            )

        elif role == "CLOSE":
            # SELL confirmed
            realised_pnl = (fill_price - self._entry_price) * fill_qty
            self._position = 0
            self._phase    = _DONE
            self.act(
                "Position closed — strategy complete",
                context={
                    "exit_price": fill_price,
                    "entry_price": self._entry_price,
                    "qty": fill_qty,
                    "realised_pnl": round(realised_pnl, 2),
                },
            )
            self.stop()

    # ── Rejection / cancellation ──────────────────────────────────────────────
    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        self.observe(f"Order rejected: {oid}")
        self._active_order_id = None
        # revert phase so the next bar retries
        if self._phase == _BUYING:
            self._phase = _IDLE
        elif self._phase == _SELLING:
            self._phase = _HOLDING

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        self.observe(f"Order canceled: {oid}")
        self._active_order_id = None
        if self._phase == _BUYING:
            self._phase = _IDLE
        elif self._phase == _SELLING:
            self._phase = _HOLDING

    # ── Rate limiter ──────────────────────────────────────────────────────────
    def _rate_ok(self) -> bool:
        min_interval = 1.0 / self.params.max_order_rate_per_second
        return (time.monotonic() - self._last_order_ts) >= min_interval