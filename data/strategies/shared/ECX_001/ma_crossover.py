"""
Moving Average Crossover Strategy
==================================
Signal-driven intraday momentum strategy for a single configurable symbol.

Entry : BUY  when fast MA crosses above slow MA
Exit  : SELL (flatten) when fast MA crosses below slow MA
Risk  : Stop gracefully when cumulative realized loss exceeds max_loss

Design guarantees
-----------------
• At most one order in flight at any time (pending_order_id gate)
• Position bounded to max_position at all times
• Crossover fires exactly once per transition (state-diff logic)
• No short selling — SELL only when position > 0
"""
from __future__ import annotations

from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from typing import Any, Dict, Optional

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide, BarAggregation, PriceType

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective


# ---------------------------------------------------------------------------
# 1. Platform metadata config
# ---------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "MovingAverageCrossover"
    file_name: str = "ma_crossover.py"


# ---------------------------------------------------------------------------
# 2. Strategy parameters (frozen, hot-updatable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    fast_ma_period: int = 10           # Fast MA window (bars)
    slow_ma_period: int = 30           # Slow MA window (bars)
    qty: int = 100                     # Shares per entry order
    max_position: int = 1000           # Maximum long position (shares)
    max_loss: float = 500.0            # Max cumulative realized loss ($)
    sampling_period_seconds: int = 5   # Bar aggregation period (seconds)

    def validate(self) -> None:
        if self.fast_ma_period < 2:
            raise ValueError("fast_ma_period must be >= 2")
        if self.slow_ma_period <= self.fast_ma_period:
            raise ValueError("slow_ma_period must be > fast_ma_period")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfigParams":
        values: Dict[str, Any] = {
            f.name: getattr(cfg, f.name, f.default)
            for f in dc_fields(cls)
        }
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: Dict[str, Any]) -> "ConfigParams":
        allowed = {f.name: f for f in dc_fields(self)}
        coerced: Dict[str, Any] = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            field_type = allowed[k].type
            if field_type in (int, "int"):
                v = int(v)
            elif field_type in (float, "float"):
                v = float(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new


# ---------------------------------------------------------------------------
# 3. Strategy class
# ---------------------------------------------------------------------------
class MovingAverageCrossover(LumitecBaseStrategy):
    mission    = StrategyMission.INTRADAY_ARBITRAGE
    objective  = StrategyObjective.SIGNAL_DRIVEN
    leg_mode   = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": "BUY", "fixed_side": True}]

    def __init__(self, config: Config) -> None:
        super().__init__(config)

        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        # Symbol resolved from leg at startup (never hardcoded)
        self.symbol: Optional[str] = None

        # Rolling price history (capped to slow_ma_period)
        self._prices: list[float] = []

        # Crossover state — None until first valid bar
        self._prev_fast_above: Optional[bool] = None

        # Order gate — prevents more than one order in flight at a time
        self._pending_order_id: Optional[str] = None

        # Position and P&L tracking
        self._position: int = 0
        self._avg_cost: float = 0.0
        self._realized_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Required platform hook
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()

        # Symbol comes from the leg — never hardcoded
        self.symbol = self.legs[0]["symbol"]

        self.subscribe_market_data_bars(
            symbol=self.symbol,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )

        self.observe("Strategy started", context={
            "symbol": self.symbol,
            "fast_ma_period": self.params.fast_ma_period,
            "slow_ma_period": self.params.slow_ma_period,
            "qty": self.params.qty,
            "max_position": self.params.max_position,
            "max_loss": self.params.max_loss,
        })

    # ------------------------------------------------------------------
    # Market data — MA crossover signal evaluation
    # ------------------------------------------------------------------
    def on_symbol_bar(self, symbol: str, bar: Bar) -> None:
        # Anchor to configured symbol only
        if symbol != self.symbol:
            return
        if self.isPaused():
            return

        # Do not submit new orders while one is in flight
        if self._pending_order_id is not None:
            return

        mid = float(bar.close)
        self._prices.append(mid)
        if len(self._prices) > self.params.slow_ma_period:
            self._prices = self._prices[-self.params.slow_ma_period:]

        # Wait until we have a full slow-MA window
        if len(self._prices) < self.params.slow_ma_period:
            return

        fast_ma = sum(self._prices[-self.params.fast_ma_period:]) / self.params.fast_ma_period
        slow_ma = sum(self._prices) / self.params.slow_ma_period
        fast_above = fast_ma > slow_ma

        self.observe(
            f"MA fast={fast_ma:.4f} slow={slow_ma:.4f} price={mid:.4f}",
            context={"fast_ma": fast_ma, "slow_ma": slow_ma, "price": mid,
                     "position": self._position, "realized_pnl": self._realized_pnl},
        )

        # First valid bar — initialise state, no signal yet
        if self._prev_fast_above is None:
            self._prev_fast_above = fast_above
            return

        crossed_above = fast_above and not self._prev_fast_above
        crossed_below = not fast_above and self._prev_fast_above
        self._prev_fast_above = fast_above

        if crossed_above and self._position < self.params.max_position:
            headroom = self.params.max_position - self._position
            qty = min(self.params.qty, headroom)
            self.decide("Bullish crossover — BUY", context={
                "fast_ma": fast_ma, "slow_ma": slow_ma, "qty": qty,
            })
            self._buy(qty, mid)

        elif crossed_below and self._position > 0:
            self.decide("Bearish crossover — SELL (flatten)", context={
                "fast_ma": fast_ma, "slow_ma": slow_ma, "position": self._position,
            })
            self._sell(self._position, mid)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------
    def _buy(self, qty: int, price_approx: float) -> None:
        clid = self.submit_market_order(
            symbol=self.symbol,
            side=OrderSide.BUY,
            qty=qty,
            leg_id="A",
            role="OPEN",
        )
        self._pending_order_id = clid.value
        self.act(f"Market BUY {qty} {self.symbol}", context={
            "order_id": clid.value, "price_approx": price_approx,
        })

    def _sell(self, qty: int, price_approx: float) -> None:
        clid = self.submit_market_order(
            symbol=self.symbol,
            side=OrderSide.SELL,
            qty=qty,
            leg_id="A",
            role="CLOSE",
        )
        self._pending_order_id = clid.value
        self.act(f"Market SELL {qty} {self.symbol}", context={
            "order_id": clid.value, "price_approx": price_approx,
        })

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)

        if leg_id is None:
            self.log.warning(f"Unknown leg_id for order {oid}")
            return

        # Clear in-flight gate
        if self._pending_order_id == oid:
            self._pending_order_id = None

        fill_px = float(event.last_px)
        fill_qty = int(event.last_qty)
        side = event.order_side

        if side == OrderSide.BUY:
            # Update weighted average cost
            total_cost = self._avg_cost * self._position + fill_px * fill_qty
            self._position += fill_qty
            self._avg_cost = total_cost / self._position

        else:  # SELL
            pnl = (fill_px - self._avg_cost) * fill_qty
            self._realized_pnl += pnl
            self._position -= fill_qty
            if self._position <= 0:
                self._position = 0
                self._avg_cost = 0.0

        self.act(
            f"Fill leg={leg_id} role={role} side={side} qty={fill_qty} px={fill_px:.4f}",
            context={
                "order_id": oid,
                "position": self._position,
                "avg_cost": self._avg_cost,
                "realized_pnl": self._realized_pnl,
            },
        )

        # Max-loss guard — evaluated after every fill
        if self._realized_pnl < -abs(self.params.max_loss):
            self.observe(
                f"Max loss breached: realized_pnl={self._realized_pnl:.2f}",
                context={"realized_pnl": self._realized_pnl, "max_loss": self.params.max_loss},
            )
            self.forced_stop("Max loss limit reached", "RISK")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        if self._pending_order_id == oid:
            self._pending_order_id = None
        self.observe(f"Order rejected: {oid} reason={event.reason}")

    def on_order_cancelled(self, event) -> None:
        oid = event.client_order_id.value
        if self._pending_order_id == oid:
            self._pending_order_id = None
        self.observe(f"Order cancelled: {oid}")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._pending_order_id = None
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self._prev_fast_above = None   # reset crossover state so signal rebuilds cleanly
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Leg validation
    # ------------------------------------------------------------------
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(f"MovingAverageCrossover requires exactly 1 leg, got {len(legs)}")
        if legs[0].get("side", "").upper() != "BUY":
            raise ValueError("MovingAverageCrossover leg must be BUY (long-only strategy)")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    def on_stop(self) -> None:
        self.observe("Strategy stopped", context={
            "final_position": self._position,
            "realized_pnl": self._realized_pnl,
        })

    # ------------------------------------------------------------------
    # Hot parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old_slow = self.params.slow_ma_period
            self.params = self.params.merged(updates)
            # If the MA window changed, reset history so signal rebuilds cleanly
            if self.params.slow_ma_period != old_slow:
                self._prices = self._prices[-self.params.slow_ma_period:]
                self._prev_fast_above = None

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)
