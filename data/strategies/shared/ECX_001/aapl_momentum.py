"""
AAPL Momentum Strategy
======================
Buys AAPL when price is trending up over the last N bars (positive OLS slope),
exits when momentum reverses. Enforces a max notional cap and hard stop-loss.

Parameters (all hot-updatable):
  lookback                — bars used for momentum signal (default 20)
  sampling_period_seconds — bar period in seconds (default 5)
  max_notional            — max position value in USD (default 50_000)
  stop_loss_pct           — max loss as fraction of notional before auto-stop (default 0.03 = 3%)
  slope_entry_threshold   — min normalised slope to enter long (default 0.0)
"""
from __future__ import annotations

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
# 1. Config (metadata — filled by platform on submission)
# ---------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "AaplMomentum"
    file_name: str = "aapl_momentum.py"


# ---------------------------------------------------------------------------
# 2. ConfigParams (strategy-specific, hot-updatable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    lookback: int = 20
    sampling_period_seconds: int = 5
    max_notional: float = 50_000.0
    stop_loss_pct: float = 0.01       # stop when unrealised loss > stop_loss_pct * notional
    slope_entry_threshold: float = 0.00003  # calibrated for live AAPL (~$260, noise floor ±0.00001)
    exit_slippage_pct: float = 0.0001  # ~$0.03 below mid on live AAPL; 0.005 was too aggressive

    def validate(self) -> None:
        if self.lookback < 5:
            raise ValueError("lookback must be >= 5")
        if self.max_notional <= 0:
            raise ValueError("max_notional must be > 0")
        if not (0 < self.stop_loss_pct < 1.0):
            raise ValueError("stop_loss_pct must be between 0 and 1")
        if self.sampling_period_seconds < 1:
            raise ValueError("sampling_period_seconds must be >= 1")
        if not (0 < self.exit_slippage_pct < 0.5):
            raise ValueError("exit_slippage_pct must be between 0 and 0.5")

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfigParams":
        values: Dict[str, Any] = {
            f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)
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
            ft = allowed[k].type
            if ft in (int, "int"):
                v = int(v)
            elif ft in (float, "float"):
                v = float(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new


# ---------------------------------------------------------------------------
# 3. Strategy
# ---------------------------------------------------------------------------
class AaplMomentum(LumitecBaseStrategy):
    mission    = StrategyMission.SPECULATIVE
    objective  = StrategyObjective.SIGNAL_DRIVEN
    leg_mode   = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": "BUY", "fixed_side": True}]

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        self.symbol_a: Optional[str] = None
        self._prices: list[float] = []

        self._position_qty: int = 0
        self._entry_price: float = 0.0
        self._realised_pnl: float = 0.0
        self._exit_pending: bool = False
        self._exit_pending_bars: int = 0  # bars elapsed since exit submitted; retry only after >= 1
        self._entry_pending: bool = False
        self._stop_after_close: bool = False
        self._last_mid: float = 0.0

    # ------------------------------------------------------------------
    # Required hooks
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        (self.leg_a,) = self.legs[:1]
        self.symbol_a = self.leg_a["symbol"]

        self.subscribe_market_data_bars(
            symbol=self.symbol_a,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )
        self.observe("Strategy started", context={
            "symbol": self.symbol_a,
            "lookback": self.params.lookback,
            "max_notional": self.params.max_notional,
            "stop_loss_pct": self.params.stop_loss_pct,
        })

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def on_symbol_bar(self, symbol: str, bar: Bar) -> None:
        if symbol != self.symbol_a:
            return
        if self.isPaused():
            return

        # Log every received bar with all 5 OHLCV values and its timestamp
        self.observe(
            "Bar received",
            context={
                "ts": str(bar.ts_event),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            },
        )

        mid = float(bar.close)
        self._last_mid = mid
        self._prices.append(mid)

        self._check_stop_loss(mid)

        if len(self._prices) < self.params.lookback:
            return

        self._prices = self._prices[-self.params.lookback:]
        slope = self._ols_slope(self._prices)

        self.observe(
            f"slope={slope:.6f} qty={self._position_qty} pnl={self._realised_pnl:.2f}",
            context={"slope": slope, "price": mid, "qty": self._position_qty},
        )

        # Track how many bars have elapsed since the exit order was submitted
        if self._exit_pending:
            self._exit_pending_bars += 1

        # If exit is pending but shares remain for > 1 bar, the limit wasn't fully filled — retry
        # (> 1 avoids retrying on the same bar the exit was first submitted)
        if self._exit_pending and self._position_qty > 0 and self._exit_pending_bars > 1:
            self.observe("Exit stalled — cancelling and retrying", context={"qty": self._position_qty, "price": mid, "bars_waited": self._exit_pending_bars})
            self.cancelAllOrders()
            self._exit_pending = False
            self._exit_pending_bars = 0
            self._exit_long(mid)
            return

        in_long = self._position_qty > 0 or self._entry_pending
        if not in_long and not self._exit_pending and slope > self.params.slope_entry_threshold:
            self._enter_long(mid)
        elif in_long and not self._exit_pending and not self._entry_pending and slope <= self.params.slope_entry_threshold:
            # Guard: wait for entry to fully fill before exiting on signal reversal
            self._exit_long(mid)

    # ------------------------------------------------------------------
    # Signal
    # ------------------------------------------------------------------
    def _ols_slope(self, prices: list[float]) -> float:
        n = len(prices)
        x_mean = (n - 1) / 2.0
        y_mean = sum(prices) / n
        num = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den < 1e-10:
            return 0.0
        return (num / den) / y_mean if y_mean > 0 else 0.0

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _enter_long(self, price: float) -> None:
        qty = int(self.params.max_notional / price)
        if qty <= 0:
            self.observe("Computed qty=0, skipping entry")
            return
        self._entry_pending = True
        self.decide("Entering long — momentum up", context={"price": price, "qty": qty})
        order = self.submit_limit_order(
            symbol=self.symbol_a,
            side=OrderSide.BUY,
            qty=qty,
            price=Price.from_str(f"{price:.2f}"),
            tif=TimeInForce.DAY,
            leg_id="A",
            role="OPEN",
        )
        self.act("BUY submitted", context={"order_id": order.client_order_id.value, "qty": qty})

    def _exit_long(self, price: float) -> None:
        if self._position_qty <= 0 or self._exit_pending:
            return
        self._exit_pending = True
        self._exit_pending_bars = 0
        aggressive_px = round(price * (1 - self.params.exit_slippage_pct), 2)
        self.decide("Exiting long — momentum reversed", context={"price": price, "limit_px": aggressive_px})
        order = self.submit_limit_order(
            symbol=self.symbol_a,
            side=OrderSide.SELL,
            qty=self._position_qty,
            price=Price.from_str(f"{aggressive_px:.2f}"),
            tif=TimeInForce.DAY,
            leg_id="A",
            role="CLOSE",
        )
        self.act("SELL limit submitted", context={"order_id": order.client_order_id.value, "qty": self._position_qty, "limit_px": aggressive_px})

    def _check_stop_loss(self, price: float) -> None:
        if self._position_qty <= 0 or self._entry_price <= 0 or self._exit_pending:
            return
        unrealised = (price - self._entry_price) * self._position_qty
        notional = self._entry_price * self._position_qty
        stop_threshold = -self.params.stop_loss_pct * notional
        if self._realised_pnl + unrealised <= stop_threshold:
            self.observe("Stop-loss triggered", context={
                "total_loss": round(self._realised_pnl + unrealised, 2),
                "threshold": round(stop_threshold, 2),
            })
            self._stop_after_close = True
            self._exit_long(price)

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)
        if leg_id is None:
            self.log.warning(f"Unknown leg_id for order {oid}")
            return

        fill_px = float(event.last_px)
        if fill_px == 0.0 and self._last_mid > 0:
            self.observe(f"Fill px=0 received, using last bar mid={self._last_mid:.4f} as fallback")
            fill_px = self._last_mid
        fill_qty = int(event.last_qty)

        if role == "OPEN":
            total_cost = self._entry_price * self._position_qty + fill_px * fill_qty
            self._position_qty += fill_qty
            self._entry_price = total_cost / self._position_qty if self._position_qty else 0.0
            self._entry_pending = False
        elif role in ("CLOSE", "NEUTRALIZE"):
            self._realised_pnl += (fill_px - self._entry_price) * fill_qty
            self._position_qty = max(0, self._position_qty - fill_qty)
            if self._position_qty == 0:
                self._entry_price = 0.0
                self._exit_pending = False
                self._exit_pending_bars = 0

        self.act(f"Fill leg={leg_id} role={role} qty={fill_qty} px={fill_px:.2f}", context={
            "order_id": oid,
            "position_qty": self._position_qty,
            "realised_pnl": self._realised_pnl,
        })

        if self._stop_after_close and self._position_qty == 0:
            self.forced_stop(f"Stop-loss exit complete, pnl={self._realised_pnl:.2f} USD", "RISK")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._entry_pending = False
        self._exit_pending = False
        self._exit_pending_bars = 0
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self._prices = []               # rebuild momentum window from fresh bars
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Leg validation
    # ------------------------------------------------------------------
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(f"AaplMomentum requires exactly 1 leg, got {len(legs)}")
        if legs[0].get("side", "").upper() != "BUY":
            raise ValueError("AaplMomentum leg must be BUY (long-only strategy)")

    def on_order_rejected(self, event) -> None:
        self._entry_pending = False
        self._exit_pending = False
        self._exit_pending_bars = 0
        self.observe(f"Order rejected: {event.client_order_id.value}")

    def on_order_cancelled(self, event) -> None:
        self._entry_pending = False
        self._exit_pending = False
        self._exit_pending_bars = 0
        self.observe(f"Order cancelled: {event.client_order_id.value}")

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        self.unsubscribe_market_data_bars(
            symbol=self.symbol_a,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )
        self.cancelAllOrders()
        if self._position_qty > 0 and not self._exit_pending:
            self.observe("Flattening open position on stop", context={"qty": self._position_qty})
            self._exit_pending = True
            aggressive_px = round(self._last_mid * (1 - self.params.exit_slippage_pct), 2) if self._last_mid > 0 else 1.0
            self.submit_limit_order(
                symbol=self.symbol_a,
                side=OrderSide.SELL,
                qty=self._position_qty,
                price=Price.from_str(f"{aggressive_px:.2f}"),
                tif=TimeInForce.DAY,
                leg_id="A",
                role="NEUTRALIZE",
            )
        self.observe("Strategy stopped", context={
            "final_qty": self._position_qty,
            "realised_pnl": self._realised_pnl,
        })

    # ------------------------------------------------------------------
    # Hot parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old_lookback = self.params.lookback
            self.params = self.params.merged(updates)
            if self.params.lookback != old_lookback:
                self._prices = self._prices[-self.params.lookback:]

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)