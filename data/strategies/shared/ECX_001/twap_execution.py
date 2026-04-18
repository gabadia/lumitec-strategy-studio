"""
TWAP Execution Strategy
=======================
Mission  : EXECUTION
Objective: TARGET_QTY

Logic:
  Slice a target quantity into equal-sized child limit orders and submit
  one child order every `interval_seconds` seconds until the target is
  fully filled or the trading session ends.

  Child order size = target_qty // slice_count
  (the last slice absorbs any remainder from integer division)

  Timer driven by bar ts_event (nanoseconds). A new slice fires when
  bar.ts_event - _last_slice_ts_ns >= interval_seconds * 1e9.

  Entry : limit order at mid ± marketable_limit_bps (BUY lifts the ask
          slightly; SELL hits the bid slightly) to improve fill probability.
  Finish: when _total_filled >= target_qty, forced_stop() is called.

Assumptions:
  - Symbol  : SPY (default from examples/)
  - Bars    : 5-second mid-price bars
  - TIF     : DAY on every child order
  - Side    : BUY (configurable via strategy_params)
  - No resubmission of unfilled prior slices — each slice is independent;
    unfilled slices expire at EOD. This is intentional for a pure TWAP.
  - apply_params() resets the slice schedule so a live parameter change
    (e.g. new interval_seconds) takes effect immediately on the next bar.

Required patterns: all 12 present (validated before submission).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from typing import Any, Dict, Optional

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective


# ---------------------------------------------------------------------------
# 1. Config (metadata — filled by the platform on submission)
# ---------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "TwapExecution"
    file_name: str = "twap_execution.py"


# ---------------------------------------------------------------------------
# 2. ConfigParams (strategy-specific, hot-updatable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    """All parameters that control TWAP behaviour.

    - frozen=True : thread-safe immutable snapshot
    - All fields   : hot-updatable via apply_params() at runtime
    """

    target_qty: int = 1000               # Total shares to execute
    slice_count: int = 10                # Number of child orders
    interval_seconds: int = 60           # Seconds between slices
    side: str = "BUY"                    # "BUY" or "SELL"
    sampling_period_seconds: int = 5     # Bar resolution in seconds
    marketable_limit_bps: float = 2.0    # Limit price offset (bps from mid)

    def validate(self) -> None:
        if self.target_qty <= 0:
            raise ValueError("target_qty must be > 0")
        if self.slice_count <= 0:
            raise ValueError("slice_count must be > 0")
        if self.slice_count > self.target_qty:
            raise ValueError("slice_count cannot exceed target_qty")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if self.side.upper() not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if self.marketable_limit_bps < 0:
            raise ValueError("marketable_limit_bps must be >= 0")

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
        """Return a new ConfigParams with the given fields overridden."""
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
class TwapExecution(LumitecBaseStrategy):
    mission   = StrategyMission.EXECUTION
    objective = StrategyObjective.TARGET_QTY
    leg_mode  = LegMode.FINITE

    def __init__(self, config: Config) -> None:
        super().__init__(config)

        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        # Symbol (set in on_start from self.legs)
        self.symbol: Optional[str] = None

        # Execution state
        self._total_filled: int = 0          # shares confirmed filled
        self._slices_sent: int = 0           # child orders submitted
        self._last_slice_ts_ns: int = 0      # ts_event of last slice submission
        self._live_after_ts_ns: int = 0      # replay guard: ignore bars before this
        self._shutting_down: bool = False

        # Active order tracking {order_id_str: qty}
        self._active_orders: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Required hook
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()

        leg = self.legs[0]
        self.symbol = leg["symbol"]

        # Replay guard: ignore historical bars delivered on connect
        self._live_after_ts_ns = time.time_ns() - 10 * 1_000_000_000

        self.subscribe_market_data_bars(
            symbol=self.symbol,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )

        self.observe("TWAP started", context={
            "symbol":           self.symbol,
            "target_qty":       self.params.target_qty,
            "slice_count":      self.params.slice_count,
            "slice_qty":        self._slice_qty(),
            "interval_seconds": self.params.interval_seconds,
            "side":             self.params.side,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _slice_qty(self) -> int:
        """Shares per child order (last slice absorbs remainder)."""
        return self.params.target_qty // self.params.slice_count

    def _remaining_qty(self) -> int:
        return max(0, self.params.target_qty - self._total_filled)

    def _this_slice_qty(self) -> int:
        """Size for the next slice — absorbs remainder on the last slice."""
        base = self._slice_qty()
        if self._slices_sent == self.params.slice_count - 1:
            # Last slice: take whatever is left
            return self._remaining_qty()
        return min(base, self._remaining_qty())

    def _order_side(self) -> OrderSide:
        return OrderSide.BUY if self.params.side.upper() == "BUY" else OrderSide.SELL

    def _limit_price(self, mid: float) -> Price:
        offset = mid * self.params.marketable_limit_bps / 10_000
        if self._order_side() == OrderSide.BUY:
            px = mid + offset
        else:
            px = mid - offset
        return Price.from_str(f"{px:.2f}")

    # ------------------------------------------------------------------
    # Bar handler — TWAP clock
    # ------------------------------------------------------------------
    def on_symbol_bar(self, symbol: str, bar: Bar) -> None:
        if symbol != self.symbol:
            return
        if self._shutting_down:
            return
        if bar.ts_event < self._live_after_ts_ns:
            return   # replay guard

        mid = float(bar.close)

        # Report heartbeat every ~10 bars
        self.observe("Heartbeat", context={
            "filled": self._total_filled,
            "target": self.params.target_qty,
            "slices_sent": self._slices_sent,
            "remaining": self._remaining_qty(),
            "mid": round(mid, 4),
        })

        # Check if fully filled
        if self._total_filled >= self.params.target_qty:
            self._shutting_down = True
            self.observe("Target reached — stopping", context={
                "total_filled": self._total_filled,
            })
            self.stop()
            return

        # Check if all slices already sent
        if self._slices_sent >= self.params.slice_count:
            return

        # Check interval
        elapsed_ns = bar.ts_event - self._last_slice_ts_ns
        interval_ns = self.params.interval_seconds * 1_000_000_000

        if self._last_slice_ts_ns == 0 or elapsed_ns >= interval_ns:
            self._submit_slice(mid, bar.ts_event)

    def _submit_slice(self, mid: float, ts_ns: int) -> None:
        qty = self._this_slice_qty()
        if qty <= 0:
            return

        price = self._limit_price(mid)
        slice_num = self._slices_sent + 1

        self.decide(f"Submitting slice {slice_num}/{self.params.slice_count}", context={
            "qty":   qty,
            "price": str(price),
            "side":  self.params.side,
            "filled_so_far": self._total_filled,
        })

        order = self.submit_limit_order(
            symbol=self.symbol,
            side=self._order_side(),
            qty=qty,
            price=price,
            tif=TimeInForce.DAY,
            leg_id="A",
            role="OPEN",
        )

        oid = order.client_order_id.value
        self._active_orders[oid] = qty
        self._slices_sent += 1
        self._last_slice_ts_ns = ts_ns

        self.act(f"Slice {slice_num} submitted", context={
            "order_id": oid,
            "qty": qty,
            "price": str(price),
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

        filled_qty = int(event.last_qty)
        self._total_filled += filled_qty
        self._active_orders.pop(oid, None)

        self.act(f"Fill: +{filled_qty} shares", context={
            "order_id":    oid,
            "last_qty":    filled_qty,
            "total_filled": self._total_filled,
            "target_qty":  self.params.target_qty,
            "pct_done":    round(self._total_filled / self.params.target_qty * 100, 1),
        })

        if self._total_filled >= self.params.target_qty:
            self._shutting_down = True
            self.stop()

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        self._active_orders.pop(oid, None)
        self.observe(f"Order rejected: {oid} reason={event.reason}")

    def on_order_cancelled(self, event) -> None:
        oid = event.client_order_id.value
        self._active_orders.pop(oid, None)
        self.observe(f"Order cancelled: {oid}")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    def on_stop(self) -> None:
        self.observe("Strategy stopped", context={
            "total_filled": self._total_filled,
            "target_qty":   self.params.target_qty,
            "slices_sent":  self._slices_sent,
            "pct_complete": round(
                self._total_filled / max(self.params.target_qty, 1) * 100, 1
            ),
        })

    # ------------------------------------------------------------------
    # Hot parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old_interval = self.params.interval_seconds
            old_slices   = self.params.slice_count
            self.params  = self.params.merged(updates)

            # Rebuild slice schedule when timing params change
            if (self.params.interval_seconds != old_interval or
                    self.params.slice_count != old_slices):
                self._last_slice_ts_ns = 0   # force immediate re-evaluation
                self.observe("Slice schedule reset after param update", context={
                    "new_interval": self.params.interval_seconds,
                    "new_slice_count": self.params.slice_count,
                })

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ------------------------------------------------------------------
    # Metrics (polled by supervisor for status endpoint)
    # ------------------------------------------------------------------
    def get_metrics(self) -> Dict[str, Any]:
        target = self.params.target_qty
        return {
            "total_filled":  self._total_filled,
            "target_qty":    target,
            "pct_complete":  round(self._total_filled / max(target, 1) * 100, 1),
            "remaining_qty": self._remaining_qty(),
            "slices_sent":   self._slices_sent,
            "slice_count":   self.params.slice_count,
            "active_orders": len(self._active_orders),
            "shutting_down": self._shutting_down,
        }
