"""
Intraday Liquidity Sweep Reversion (LSR)
========================================
Detects short-term price sweeps (z-score of bar return + volume spike),
waits one bar for exhaustion confirmation (price reversal), then fades
the move back to mean.

Exit via:
  - Mean-reversion z-score (abs(z) <= exit_z)
  - Stop-loss % of entry notional
  - Time-based max_hold_bars

State machine: FLAT -> PENDING_ENTRY -> IN_POSITION -> PENDING_EXIT -> FLAT

Validation checklist (all 16 required patterns present):
  1.  @dataclass(frozen=True) on ConfigParams
  2.  validate() in ConfigParams
  3.  merged() in ConfigParams
  4.  from_config() in ConfigParams
  5.  apply_params() with RLock
  6.  configure() accepting strategy_params dict
  7.  on_stop()
  8.  on_order_rejected()
  9.  on_order_canceled()
  10. set_oms_type()
  11. Rebuild signal windows in apply_params() when lookback changes
  12. Guard on_order_filled for unknown leg_id
  13. leg_schema class attribute
  14. validate_legs() classmethod
  15. isPaused() guard in on_symbol_bar
  16. on_pause() / on_resume() hooks
"""
from __future__ import annotations

from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from typing import Any, Dict, List, Optional

from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import BarAggregation, OrderSide, PriceType, TimeInForce
from nautilus_trader.model.objects import Price

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective

# ---------------------------------------------------------------------------
# Internal state constants
# ---------------------------------------------------------------------------
_FLAT = "FLAT"
_PENDING_ENTRY = "PENDING_ENTRY"
_IN_POSITION = "IN_POSITION"
_PENDING_EXIT = "PENDING_EXIT"


# ---------------------------------------------------------------------------
# 1. Config — metadata, filled by the platform on submission
# ---------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "LSRStrategy"
    file_name: str = "lsr_strategy.py"


# ---------------------------------------------------------------------------
# 2. ConfigParams — strategy-specific, hot-updatable frozen dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    """All parameters that govern LSR behaviour. All fields are hot-updatable."""

    lookback: int = 40                  # rolling window for z-score / volume avg (bars)
    sweep_z: float = 2.0               # abs(z-score) threshold to flag a sweep
    volume_spike_mult: float = 1.8     # current bar volume / rolling avg to confirm spike
    exit_z: float = 0.5               # abs(z-score) at which we consider mean-reverted
    stop_loss_pct: float = 0.005      # adverse move fraction of entry price -> stop
    max_hold_bars: int = 20           # time-based exit if not already exited
    qty: int = 100                    # shares per trade
    max_position: int = 100          # max net shares (= qty for one trade at a time)
    max_loss: float = 500.0          # cumulative realized loss limit -> strategy stops
    aggression_cents: float = 2.0    # limit offset from mid for entry (cents)
    exit_slippage_cents: float = 2.0 # limit offset from mid for exit (cents)
    sampling_period_seconds: int = 5  # bar aggregation period in seconds

    def validate(self) -> None:
        if self.lookback < 10:
            raise ValueError("lookback must be >= 10")
        if self.sweep_z <= 0:
            raise ValueError("sweep_z must be > 0")
        if self.exit_z >= self.sweep_z:
            raise ValueError("exit_z must be < sweep_z")
        if self.volume_spike_mult <= 1.0:
            raise ValueError("volume_spike_mult must be > 1.0")
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0")
        if self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: Dict[str, Any]) -> "ConfigParams":
        """Return a new ConfigParams with the given fields overridden and validated."""
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
class LSRStrategy(LumitecBaseStrategy):
    mission    = StrategyMission.INTRADAY_ARBITRAGE
    objective  = StrategyObjective.SIGNAL_DRIVEN
    leg_mode   = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        self.symbol: Optional[str] = None

        # Rolling history
        self._closes: List[float] = []
        self._volumes: List[float] = []
        self._returns: List[float] = []   # bar-over-bar fractional returns

        # State machine
        self._state: str = _FLAT

        # Sweep tracking
        self._pending_sweep: Optional[str] = None   # "UP" or "DOWN"
        self._prev_close: Optional[float] = None

        # Position / trade tracking
        self._position: int = 0
        self._entry_price: Optional[float] = None
        self._entry_side: Optional[str] = None      # "LONG" or "SHORT"
        self._bars_held: int = 0
        self._active_order_id: Optional[str] = None

        # P&L
        self._realized_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Required hook — must set _oms_type or strategy crashes on startup
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------
    # Lifecycle — start
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        self.symbol = self.legs[0]["symbol"]

        self.subscribe_market_data_bars(
            symbol=self.symbol,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )
        self.observe("LSR started", context={"symbol": self.symbol,
                                              "lookback": self.params.lookback,
                                              "sweep_z": self.params.sweep_z})

    # ------------------------------------------------------------------
    # Market data — main signal loop
    # ------------------------------------------------------------------
    def on_symbol_bar(self, symbol: str, bar: Bar) -> None:
        """Called for every bar — anchor all logic to the single primary symbol."""
        if symbol != self.symbol:
            return
        if self.isPaused():
            return

        close = float(bar.close)
        volume = float(bar.volume)

        # Accumulate bar return (requires previous close)
        if self._prev_close is not None and self._prev_close > 0:
            self._returns.append((close - self._prev_close) / self._prev_close)
        prev_close = self._prev_close
        self._prev_close = close

        self._closes.append(close)
        self._volumes.append(volume)

        # Trim to rolling window
        lb = self.params.lookback
        self._closes = self._closes[-lb:]
        self._volumes = self._volumes[-lb:]
        self._returns = self._returns[-lb:]

        # Need full window before trading
        if len(self._returns) < lb:
            return

        # --- Z-score of latest bar return ---
        mean_r = sum(self._returns) / lb
        var_r = sum((r - mean_r) ** 2 for r in self._returns) / lb
        std_r = var_r ** 0.5
        if std_r < 1e-10:
            return
        z = (self._returns[-1] - mean_r) / std_r

        # --- Volume ratio vs rolling average (exclude current bar) ---
        hist_vols = self._volumes[:-1]
        mean_v = sum(hist_vols) / len(hist_vols) if hist_vols else volume
        vol_ratio = volume / mean_v if mean_v > 0 else 1.0

        self.observe(
            f"z={z:.2f} vol_ratio={vol_ratio:.2f} state={self._state}",
            context={"z": round(z, 3), "vol_ratio": round(vol_ratio, 2),
                     "close": close, "pnl": round(self._realized_pnl, 2)},
        )

        # --- In-position: evaluate exits each bar ---
        if self._state == _IN_POSITION:
            self._bars_held += 1
            self._evaluate_exits(close, z)
            return

        # --- Any other non-FLAT state: wait for fills/cancels ---
        if self._state != _FLAT:
            return

        # --- Exhaustion confirmation (bar after detected sweep) ---
        if self._pending_sweep is not None and prev_close is not None:
            if self._pending_sweep == "UP" and close < prev_close:
                # Sweep up, now reversing down -> SHORT
                self._enter(OrderSide.SELL, close, "UP_FADE")
            elif self._pending_sweep == "DOWN" and close > prev_close:
                # Sweep down, now reversing up -> LONG
                self._enter(OrderSide.BUY, close, "DOWN_FADE")
            else:
                self.observe(
                    "Sweep not confirmed — reset",
                    context={"pending": self._pending_sweep, "z": round(z, 3)},
                )
            self._pending_sweep = None
            return

        # --- Sweep detection: z-score threshold + volume spike ---
        if abs(z) >= self.params.sweep_z and vol_ratio >= self.params.volume_spike_mult:
            direction = "UP" if z > 0 else "DOWN"
            self._pending_sweep = direction
            self.decide(
                f"Sweep detected dir={direction} — awaiting confirmation",
                context={"z": round(z, 3), "vol_ratio": round(vol_ratio, 2)},
            )

    # ------------------------------------------------------------------
    # Exit evaluation (called every bar while IN_POSITION)
    # ------------------------------------------------------------------
    def _evaluate_exits(self, close: float, z: float) -> None:
        if self._entry_price is None:
            return

        direction = 1 if self._entry_side == "LONG" else -1
        unrealized = (close - self._entry_price) * direction * abs(self._position)
        stop_threshold = -(self._entry_price * self.params.stop_loss_pct * abs(self._position))

        if unrealized <= stop_threshold:
            self.decide(
                "Stop-loss triggered",
                context={"unrealized": round(unrealized, 2), "threshold": round(stop_threshold, 2)},
            )
            self._exit(close, "stop_loss")
            return

        if abs(z) <= self.params.exit_z:
            self.decide("Mean-reversion exit", context={"z": round(z, 3)})
            self._exit(close, "mean_reversion")
            return

        if self._bars_held >= self.params.max_hold_bars:
            self.decide("Time-based exit", context={"bars_held": self._bars_held})
            self._exit(close, "time_exit")

    # ------------------------------------------------------------------
    # Entry helper
    # ------------------------------------------------------------------
    def _enter(self, side: OrderSide, close: float, reason: str) -> None:
        if abs(self._position) >= self.params.max_position:
            self.observe("Max position reached — skipping entry")
            return
        if self._realized_pnl <= -self.params.max_loss:
            self.observe("Cumulative max_loss reached — stopping")
            self.forced_stop("Max loss reached", "RISK")
            return

        offset = self.params.aggression_cents / 100.0
        if side == OrderSide.BUY:
            price = Price.from_str(f"{close + offset:.2f}")
            self._entry_side = "LONG"
        else:
            price = Price.from_str(f"{close - offset:.2f}")
            self._entry_side = "SHORT"

        self._state = _PENDING_ENTRY
        self._bars_held = 0

        order = self.submit_limit_order(
            symbol=self.symbol,
            side=side,
            qty=self.params.qty,
            price=price,
            tif=TimeInForce.DAY,
            leg_id="A",
            role="OPEN",
        )
        self._active_order_id = order.client_order_id.value
        self.act(
            f"Entry {side.name} {self.params.qty}@{price} [{reason}]",
            context={"order_id": self._active_order_id},
        )

    # ------------------------------------------------------------------
    # Exit helper
    # ------------------------------------------------------------------
    def _exit(self, close: float, reason: str) -> None:
        if self._state == _PENDING_EXIT:
            return

        self.cancelAllOrders()

        exit_side = OrderSide.SELL if self._entry_side == "LONG" else OrderSide.BUY
        offset = self.params.exit_slippage_cents / 100.0
        if exit_side == OrderSide.SELL:
            price = Price.from_str(f"{close - offset:.2f}")
        else:
            price = Price.from_str(f"{close + offset:.2f}")

        self._state = _PENDING_EXIT
        order = self.submit_limit_order(
            symbol=self.symbol,
            side=exit_side,
            qty=abs(self._position),
            price=price,
            tif=TimeInForce.DAY,
            leg_id="A",
            role="CLOSE",
        )
        self._active_order_id = order.client_order_id.value
        self.act(
            f"Exit {exit_side.name} {abs(self._position)}@{price} [{reason}]",
            context={"order_id": self._active_order_id, "reason": reason},
        )

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)

        # Guard: unknown leg_id
        if leg_id is None:
            self.log.warning(f"Unknown leg_id for order {oid}")
            return

        fill_px = float(event.last_px)
        fill_qty = int(event.last_qty)

        if role == "OPEN":
            if self._entry_side == "LONG":
                self._position += fill_qty
            else:
                self._position -= fill_qty
            self._entry_price = fill_px
            self._state = _IN_POSITION
            self.act(
                f"Opened leg={leg_id} {fill_qty}@{fill_px:.2f}",
                context={"position": self._position, "entry_price": fill_px},
            )

        elif role == "CLOSE":
            pnl = 0.0
            if self._entry_price is not None:
                direction = 1 if self._entry_side == "LONG" else -1
                pnl = (fill_px - self._entry_price) * direction * fill_qty
            self._realized_pnl += pnl

            if self._entry_side == "LONG":
                self._position -= fill_qty
            else:
                self._position += fill_qty

            if self._position == 0:
                self._state = _FLAT
                self._entry_price = None
                self._entry_side = None
                self._active_order_id = None
                self.act(
                    f"Closed leg={leg_id} pnl={pnl:+.2f} cum={self._realized_pnl:+.2f}",
                    context={"trade_pnl": round(pnl, 2),
                             "cumulative_pnl": round(self._realized_pnl, 2)},
                )
                if self._realized_pnl <= -self.params.max_loss:
                    self.observe("Cumulative loss limit hit post-close — stopping")
                    self.forced_stop("Max loss reached", "RISK")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        self.observe(f"Order rejected: {oid}")
        if self._state == _PENDING_ENTRY:
            self._state = _FLAT
            self._entry_side = None
        elif self._state == _PENDING_EXIT:
            # Revert to IN_POSITION so next bar re-evaluates and retries exit
            self._state = _IN_POSITION

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        self.observe(f"Order canceled: {oid}")
        if self._state == _PENDING_ENTRY:
            self._state = _FLAT
            self._entry_side = None

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._pending_sweep = None      # discard stale sweep state across pause
        if self._state == _PENDING_ENTRY:
            self._state = _FLAT
            self._entry_side = None
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Leg validation — called by supervisor before instantiation
    # ------------------------------------------------------------------
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(f"LSRStrategy requires exactly 1 leg, got {len(legs)}")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    def on_stop(self) -> None:
        self.observe(
            "LSR stopped",
            context={
                "realized_pnl": round(self._realized_pnl, 2),
                "position": self._position,
                "state": self._state,
            },
        )

    # ------------------------------------------------------------------
    # Hot parameter updates — called by supervisor at runtime
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old_lookback = self.params.lookback
            self.params = self.params.merged(updates)
            # Rebuild signal windows when lookback changes (pattern #11)
            if self.params.lookback != old_lookback:
                lb = self.params.lookback
                self._closes = self._closes[-lb:]
                self._volumes = self._volumes[-lb:]
                self._returns = self._returns[-lb:]
                self.observe(
                    f"Lookback changed {old_lookback}->{lb} — windows trimmed",
                    context={"new_lookback": lb},
                )

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)
