from __future__ import annotations

from dataclasses import dataclass, replace, fields as dc_fields
from typing import Any, Dict, Optional, Tuple
from collections import deque
from threading import RLock
import math
import time
from enum import Enum, auto

from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price

from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "ClaudePairsStrategy"
    file_name: str = "claude_pairs_strategy.py"


# -----------------------------------------------------------------------------
# PARAMS
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    lookback: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    max_notional: float = 100_000.0
    max_order_notional: float = 10_000.0
    tif: str = "DAY"
    sampling_period_seconds: float = 5.0
    marketable_limit_bps: float = 2.0
    cooldown_after_exit_bars: int = 3
    # fix: capped at 1 — single state machine cannot support concurrent cycles
    max_concurrent_cycles: int = 1
    min_hold_bars: int = 5
    max_gain_dollars: float = 0.0
    max_loss_dollars: float = 0.0
    max_exit_retries: int = 5       # new: max resubmit attempts on exit order failure
    adf_pvalue_threshold: float = 0.05  # new: block entry if spread ADF p-value exceeds this (0 = disabled)
    tick_throttle_interval: float = 5.0  # seconds between z-score observe events

    def validate(self) -> None:
        if self.lookback <= 1:
            raise ValueError("lookback must be > 1")
        if self.entry_z <= 0:
            raise ValueError("entry_z must be > 0")
        if self.exit_z < 0:
            raise ValueError("exit_z must be >= 0")
        if self.entry_z <= self.exit_z:
            raise ValueError("entry_z must be strictly greater than exit_z")
        if self.max_notional <= 0:
            raise ValueError("max_notional must be > 0")
        if self.max_order_notional <= 0:
            raise ValueError("max_order_notional must be > 0")
        if self.sampling_period_seconds <= 0:
            raise ValueError("sampling_period_seconds must be > 0")
        if self.marketable_limit_bps < 0:
            raise ValueError("marketable_limit_bps must be >= 0")
        if self.cooldown_after_exit_bars < 0:
            raise ValueError("cooldown_after_exit_bars must be >= 0")
        # fix: max_concurrent_cycles capped at 1 — multi-cycle needs a full state-machine refactor
        if self.max_concurrent_cycles != 1:
            raise ValueError("max_concurrent_cycles must be 1 (multi-cycle not yet supported)")
        if self.min_hold_bars < 0:
            raise ValueError("min_hold_bars must be >= 0")
        if self.max_gain_dollars < 0:
            raise ValueError("max_gain_dollars must be >= 0")
        if self.max_loss_dollars < 0:
            raise ValueError("max_loss_dollars must be >= 0")
        if self.max_exit_retries < 0:
            raise ValueError("max_exit_retries must be >= 0")
        if not 0.0 <= self.adf_pvalue_threshold <= 1.0:
            raise ValueError("adf_pvalue_threshold must be between 0 and 1")
        if self.tick_throttle_interval <= 0:
            raise ValueError("tick_throttle_interval must be > 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: Dict[str, Any]) -> "ConfigParams":
        allowed = {f.name for f in dc_fields(self)}
        cleaned: Dict[str, Any] = {}
        for k, v in (updates or {}).items():
            if k not in allowed or v is None:
                continue
            field_type = type(getattr(self, k))
            try:
                cleaned[k] = field_type(v)
            except (TypeError, ValueError):
                cleaned[k] = v
        new_params = replace(self, **cleaned)
        new_params.validate()
        return new_params


# -----------------------------------------------------------------------------
# STATE
# -----------------------------------------------------------------------------
class PairState(Enum):
    FLAT = auto()
    ENTERING = auto()
    OPEN = auto()
    EXITING = auto()


class SignalIntent(Enum):
    HOLD = auto()
    ENTER = auto()
    EXIT = auto()


# -----------------------------------------------------------------------------
# RUNNING STATS
# fix: replaces O(n) full-pass z-score with O(1) Welford online algorithm.
# Handles deque eviction by tracking the value that rolls out of the window.
# -----------------------------------------------------------------------------
class RunningSpreadStats:
    """Welford online mean/variance over a fixed-size window of (price_a - price_b) spreads."""

    def __init__(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self._window: deque = deque(maxlen=maxlen)
        self._mean: float = 0.0
        self._m2: float = 0.0   # sum of squared deviations from mean (for sample variance)

    def reset(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self._window = deque(maxlen=maxlen)
        self._mean = 0.0
        self._m2 = 0.0

    def update(self, price_a: float, price_b: float) -> None:
        spread = price_a - price_b
        n_before = len(self._window)

        if n_before == self._maxlen:
            # Evict oldest value using reverse-Welford
            evicted = self._window[0]
            n_after_evict = n_before - 1
            if n_after_evict == 0:
                self._mean = 0.0
                self._m2 = 0.0
            else:
                old_mean = self._mean
                self._mean = (old_mean * n_before - evicted) / n_after_evict
                self._m2 -= (evicted - old_mean) * (evicted - self._mean)
                self._m2 = max(0.0, self._m2)
            n_before = n_after_evict

        # Add new value using Welford
        self._window.append(spread)
        n_new = n_before + 1
        delta = spread - self._mean
        self._mean += delta / n_new
        delta2 = spread - self._mean
        self._m2 += delta * delta2

    def zscore(self, current_spread: float) -> float:
        n = len(self._window)
        if n < 2:
            return 0.0
        # fix: use sample variance (n-1) instead of population variance (n)
        var = self._m2 / (n - 1)
        std = math.sqrt(var) if var > 0 else 1.0
        return (current_spread - self._mean) / std

    @property
    def full(self) -> bool:
        return len(self._window) >= self._maxlen


# -----------------------------------------------------------------------------
# ADF TEST
# Pure-Python Augmented Dickey-Fuller test (constant included, zero extra lags).
# P-value approximated via MacKinnon (1994) response surface — no external deps.
#
# H0: series has a unit root (non-stationary / random walk).
# Low p-value → reject H0 → series is stationary → spread is mean-reverting.
# -----------------------------------------------------------------------------
class AdfTest:
    # MacKinnon (1994) Table 4, tau_c (regression with constant), 1 variable.
    # Each row: (cumulative_probability, beta_inf, beta_1, beta_2)
    # CV(p, n) = beta_inf + beta_1/n + beta_2/n^2
    _TABLE = [
        (0.010, -3.4335, -5.999, -29.25),
        (0.025, -3.1929, -4.432, -14.01),
        (0.050, -2.8621, -2.738,  -8.36),
        (0.100, -2.5671, -1.438,  -4.48),
        (0.200, -2.1996, -0.305,   0.00),
        (0.500, -1.6062,  0.641,   0.00),
        (0.900, -0.6346,  1.085,   0.00),
    ]

    @classmethod
    def _critical_value(cls, row: tuple, n: int) -> float:
        _, b0, b1, b2 = row
        return b0 + b1 / n + b2 / (n * n)

    @classmethod
    def pvalue(cls, stat: float, n: int) -> float:
        """Interpolate p-value from tabulated critical values (log-linear in p)."""
        cvs = [(cls._critical_value(row, n), row[0]) for row in cls._TABLE]
        if stat <= cvs[0][0]:
            return cls._TABLE[0][0]   # below lowest CV → p <= 0.01
        if stat >= cvs[-1][0]:
            return 1.0
        for i in range(len(cvs) - 1):
            cv_lo, p_lo = cvs[i]
            cv_hi, p_hi = cvs[i + 1]
            if cv_lo <= stat <= cv_hi:
                frac = (stat - cv_lo) / (cv_hi - cv_lo)
                log_p = math.log(p_lo) + frac * (math.log(p_hi) - math.log(p_lo))
                return min(1.0, math.exp(log_p))
        return 1.0

    @classmethod
    def test(cls, series: list) -> Tuple[float, float]:
        """
        Returns (test_statistic, p_value).
        Model: Δy_t = α + β·y_{t-1} + ε_t
        t-stat of β is the ADF statistic.
        """
        n = len(series)
        if n < 5:
            return 0.0, 1.0

        y_lag = series[:-1]
        dy = [series[t] - series[t - 1] for t in range(1, n)]
        n_obs = len(dy)

        # OLS with constant via demeaned regression
        mean_yl = sum(y_lag) / n_obs
        mean_dy = sum(dy) / n_obs
        yl_dm = [v - mean_yl for v in y_lag]

        xx = sum(v * v for v in yl_dm)
        xy = sum(yl_dm[i] * dy[i] for i in range(n_obs))

        if xx == 0.0:
            return 0.0, 1.0

        beta = xy / xx
        alpha = mean_dy - beta * mean_yl
        residuals = [dy[i] - alpha - beta * y_lag[i] for i in range(n_obs)]

        sse = sum(r * r for r in residuals)
        s2 = sse / max(n_obs - 2, 1)
        se_beta = math.sqrt(s2 / xx) if s2 > 0 else 0.0

        if se_beta == 0.0:
            return 0.0, 1.0

        t_stat = beta / se_beta
        p_val = cls.pvalue(t_stat, n_obs)
        return t_stat, p_val


# -----------------------------------------------------------------------------
# SIGNAL ENGINE
# -----------------------------------------------------------------------------
class PairsSignalEngine:
    def __init__(self, entry_z: float, exit_z: float):
        self.entry_z = entry_z
        self.exit_z = exit_z

    def evaluate(self, z: float, state: PairState, entry_dir: int) -> SignalIntent:
        if state == PairState.FLAT:
            if abs(z) >= self.entry_z:
                return SignalIntent.ENTER

        elif state == PairState.OPEN:
            if entry_dir > 0:
                if z <= self.exit_z:
                    return SignalIntent.EXIT
            elif entry_dir < 0:
                if z >= -self.exit_z:
                    return SignalIntent.EXIT
            else:
                if abs(z) <= self.exit_z:
                    return SignalIntent.EXIT

        return SignalIntent.HOLD


# -----------------------------------------------------------------------------
# STRATEGY
# -----------------------------------------------------------------------------
class ClaudePairsStrategy(LumitecBaseStrategy):

    mission = StrategyMission.INTRADAY_ARBITRAGE
    objective = StrategyObjective.INTRADAY_ARBITRAGE
    leg_mode = LegMode.CONTINUOUS
    leg_schema = [
        {"label": "Leg A", "side": "BUY",  "fixed_side": False},
        {"label": "Leg B", "side": "SELL", "fixed_side": False},
    ]

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        """Called by the controller before strategy instantiation."""
        if len(legs) != 2:
            raise ValueError(
                f"ClaudePairsStrategy requires exactly 2 legs, got {len(legs)}"
            )
        # fix: explicit missing-side check before comparing sides
        for i, leg in enumerate(legs):
            if not leg.get("side"):
                raise ValueError(f"Leg {i} is missing a 'side' configuration.")
        side_a = str(legs[0]["side"]).upper()
        side_b = str(legs[1]["side"]).upper()
        if side_a == side_b:
            raise ValueError(
                f"Both legs configured with the same side ({side_a}) — "
                "dollar-neutral pairs require opposite sides."
            )

    def __init__(self, config: Config):
        super().__init__(config)

        self.params = ConfigParams.from_config(config)
        self._param_lock = RLock()

        self._pair_state = PairState.FLAT
        self._entry_dir: int = 0
        self._cooldown_bars: int = 0
        self._open_cycles: int = 0
        self._bars_since_entry: int = 0
        self._pnl_limit_reached: bool = False
        self._shutting_down: bool = False
        self._exit_retry_count: int = 0     # new: tracks consecutive exit order failures
        self._adf_block_cooldown: int = 0   # throttle: bars remaining before next ADF-block log

        self._signal_engine = PairsSignalEngine(
            entry_z=self.params.entry_z,
            exit_z=self.params.exit_z,
        )

        # fix: replaced raw deques + O(n) zscore with RunningSpreadStats (O(1) per bar)
        self._spread_stats = RunningSpreadStats(maxlen=self.params.lookback)
        # Individual price deques are still kept for hedge-ratio computation
        self._prices_a: deque = deque(maxlen=self.params.lookback)
        self._prices_b: deque = deque(maxlen=self.params.lookback)

        self._last_mid_a: Optional[float] = None
        self._last_mid_b: Optional[float] = None

        self._pos_qty: Dict[str, float] = {"A": 0.0, "B": 0.0}
        self._pos_side: Dict[str, Optional[OrderSide]] = {"A": None, "B": None}
        self._avg_entry: Dict[str, float] = {"A": 0.0, "B": 0.0}
        self._target_qty: Dict[str, float] = {"A": 0.0, "B": 0.0}

        self._realized_pnl: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._current_pnl: float = 0.0
        self._last_z: float = 0.0
        self._last_spread: float = 0.0
        self._beta: float = 1.0             # new: rolling hedge ratio (price_a / price_b)

        self._orders: Dict[str, Dict[str, set]] = {
            "A": {"entry": set(), "exit": set()},
            "B": {"entry": set(), "exit": set()},
        }

        self._history_started: Dict[str, bool] = {"A": False, "B": False}
        self._history_complete: Dict[str, bool] = {"A": False, "B": False}
        self._last_heartbeat_ts: Dict[str, float] = {"A": 0.0, "B": 0.0}
        self._last_z_log_ts: float = 0.0
        # Guard against historical bar replay storm on subscription connect.
        # Set to current wall-clock time in on_start; bars older than this are skipped.
        self._live_after_ts_ns: int = 0

        self.observe("Initialized", context={"pair_state": self._pair_state.name})

    # ------------------------------------------------------------------
    # PARAMETER UPDATES
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old = self.params
            new = old.merged(updates)
            self.params = new

            if new.entry_z != old.entry_z or new.exit_z != old.exit_z:
                self._signal_engine = PairsSignalEngine(
                    entry_z=new.entry_z,
                    exit_z=new.exit_z,
                )

            # fix: rebuild RunningSpreadStats when lookback changes, preserving tail
            if new.lookback != old.lookback:
                old_spreads = [a - b for a, b in zip(self._prices_a, self._prices_b)]
                tail = old_spreads[-new.lookback:]
                self._spread_stats = RunningSpreadStats(maxlen=new.lookback)
                # Replay the tail to repopulate running stats
                prices_a_list = list(self._prices_a)
                prices_b_list = list(self._prices_b)
                offset = len(prices_a_list) - len(tail)
                for i, _ in enumerate(tail):
                    self._spread_stats.update(prices_a_list[offset + i], prices_b_list[offset + i])
                new_pa = deque(prices_a_list[-new.lookback:], maxlen=new.lookback)
                new_pb = deque(prices_b_list[-new.lookback:], maxlen=new.lookback)
                self._prices_a = new_pa
                self._prices_b = new_pb
                self._history_complete["A"] = len(self._prices_a) >= new.lookback
                self._history_complete["B"] = len(self._prices_b) >= new.lookback

            self.observe("Params updated", context={
                k: getattr(new, k) for k in updates if hasattr(new, k)
            })

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)
        else:
            allowed = {f.name for f in dc_fields(ConfigParams)}
            flat = {k: v for k, v in extras.items() if k in allowed}
            if flat:
                self.apply_params(flat)

    # ------------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()

        self.leg_a, self.leg_b = self.legs
        self.symbol_a: str = self.leg_a["symbol"]
        self.symbol_b: str = self.leg_b["symbol"]

        self.observe("Strategy started", context={
            "symbol_a": self.symbol_a,
            "symbol_b": self.symbol_b,
            "entry_z": self.params.entry_z,
            "exit_z": self.params.exit_z,
            "lookback": self.params.lookback,
            "tif": self.params.tif,
        })

        # Record wall-clock time so on_symbol_bar can reject historical replay bars.
        # Allow a 10s grace window for any subscription latency.
        self._live_after_ts_ns = time.time_ns() - 10 * 1_000_000_000

        self.subscribe_market_data_bars(
            symbol=self.symbol_a,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )
        self.subscribe_market_data_bars(
            symbol=self.symbol_b,
            aggregation=BarAggregation.SECOND,
            step=self.params.sampling_period_seconds,
            price_type=PriceType.MID,
        )

    def on_stop(self) -> None:
        self.act("Strategy stopped", context={
            "pair_state": self._pair_state.name,
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
        })

    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        # If mid-entry, roll back to FLAT so we don't resume with a dangling partial state
        if self._pair_state == PairState.ENTERING:
            self._pair_state = PairState.FLAT
            self._entry_dir = 0
        self.act("Paused", context={"reason": reason, "pair_state": self._pair_state.name})

    def on_resume(self) -> None:
        # Reset spread stats so the signal rebuilds from fresh bars
        self._spread_stats.reset(self.params.lookback)
        self._prices_a.clear()
        self._prices_b.clear()
        self._history_started = {"A": False, "B": False}
        self.act("Resumed")

    # ------------------------------------------------------------------
    # BAR
    # ------------------------------------------------------------------
    def on_symbol_bar(self, symbol: str, bar) -> None:
        if self.isPaused():
            return
        if self._shutting_down:
            return
        # Reject historical replay bars delivered on subscription connect.
        # ts_event is nanoseconds; _live_after_ts_ns is set in on_start.
        if bar.ts_event < self._live_after_ts_ns:
            return
        mid = float(bar.close)

        if symbol == self.symbol_a:
            self._last_mid_a = mid
            self._update_history("A", mid, bar.ts_event, self._prices_a)
        elif symbol == self.symbol_b:
            self._last_mid_b = mid
            self._update_history("B", mid, bar.ts_event, self._prices_b)
        else:
            return

        # Signal evaluation runs only on symbol_a bars to avoid double-firing.
        if symbol != self.symbol_a:
            return

        if self._last_mid_a is None or self._last_mid_b is None:
            return

        if not self._spread_stats.full:
            return

        self._last_spread = self._last_mid_a - self._last_mid_b
        # fix: O(1) z-score via RunningSpreadStats instead of O(n) full pass
        self._last_z = self._spread_stats.zscore(self._last_spread)
        # new: update rolling hedge ratio (simple price ratio; OLS beta approximation)
        self._beta = self._last_mid_a / self._last_mid_b if self._last_mid_b > 0 else 1.0

        _now = time.monotonic()
        if _now - self._last_z_log_ts >= self.params.tick_throttle_interval:
            self._last_z_log_ts = _now
            self.observe("Z-score", context={
                "z": round(self._last_z, 4),
                "spread": round(self._last_spread, 4),
                "mid_a": round(self._last_mid_a, 4),
                "mid_b": round(self._last_mid_b, 4),
                "pair_state": self._pair_state.name,
            })

        self._update_pnl()

        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

        if self._adf_block_cooldown > 0:
            self._adf_block_cooldown -= 1

        if self._pair_state == PairState.OPEN:
            self._bars_since_entry += 1

        intent = self._signal_engine.evaluate(self._last_z, self._pair_state, self._entry_dir)

        if intent == SignalIntent.ENTER:
            self._enter(self._last_z)
        elif intent == SignalIntent.EXIT:
            self._exit()

    def _update_history(self, leg: str, mid: float, ts_event: int, prices: deque) -> None:
        if not self._history_started[leg]:
            self._history_started[leg] = True
            self.observe("History collection started", context={
                "leg": leg, "lookback": self.params.lookback,
            })
        prices.append(mid)
        # Update spread stats ONLY on leg B arrival (one update per bar period).
        # Using leg A's price here is correct: on_symbol_bar sets _last_mid_a
        # before calling _update_history, so it's already the current bar's price.
        # Updating on both legs would double-count, halving the effective warmup time
        # and causing premature signal evaluation on historical replay bars.
        if leg == "B" and self._last_mid_a is not None:
            self._spread_stats.update(self._last_mid_a, mid)

        if not self._history_complete[leg] and len(prices) >= self.params.lookback:
            self._history_complete[leg] = True
            self.observe("History collection complete", context={
                "leg": leg, "bars": len(prices),
            })
        bar_ts_s = ts_event / 1e9
        if bar_ts_s - self._last_heartbeat_ts[leg] >= 60:
            self._last_heartbeat_ts[leg] = bar_ts_s
            self.observe("Heartbeat", context={
                "leg": leg,
                "bars_collected": len(prices),
                "pair_state": self._pair_state.name,
                "last_z": round(self._last_z, 4),
                "last_spread": round(self._last_spread, 4),
            })

    # ------------------------------------------------------------------
    # ENTRY
    # ------------------------------------------------------------------
    def _enter(self, z: float) -> None:
        if self._pair_state != PairState.FLAT:
            return
        if self._cooldown_bars > 0:
            return
        if self._has_any_position() or self._has_pending_orders():
            return
        if self._open_cycles >= self.params.max_concurrent_cycles:
            return
        if self._pnl_limit_reached:
            self.observe("Entry blocked: P&L limit reached", context={
                "realized_pnl": round(self._realized_pnl, 2),
                "max_gain_dollars": self.params.max_gain_dollars,
                "max_loss_dollars": self.params.max_loss_dollars,
            })
            return

        # ADF stationarity check — skip if threshold is 0 (disabled)
        adf_pval: Optional[float] = None
        if self.params.adf_pvalue_threshold > 0:
            spread_series = list(self._spread_stats._window)
            adf_stat, adf_pval = AdfTest.test(spread_series)
            if adf_pval > self.params.adf_pvalue_threshold:
                if self._adf_block_cooldown == 0:
                    self.observe("Entry blocked: spread not stationary", context={
                        "adf_stat": round(adf_stat, 4),
                        "adf_pval": round(adf_pval, 4),
                        "threshold": self.params.adf_pvalue_threshold,
                        "z": round(z, 4),
                    })
                    self._adf_block_cooldown = 12  # ~60s at 5s bars; re-logs if still blocked
                return

        side_a = OrderSide.SELL if z > 0 else OrderSide.BUY
        side_b = OrderSide.BUY if z > 0 else OrderSide.SELL

        qty_a, qty_b = self._compute_entry_quantities()
        if qty_a <= 0 or qty_b <= 0:
            self.observe("Entry skipped: quantity too small at current price", context={
                "mid_a": self._last_mid_a,
                "mid_b": self._last_mid_b,
                "max_order_notional": self.params.max_order_notional,
            })
            return

        self._entry_dir = 1 if z > 0 else -1
        self._target_qty["A"] = qty_a
        self._target_qty["B"] = qty_b
        self._pos_side["A"] = side_a
        self._pos_side["B"] = side_b

        self._open_cycles += 1
        self.decide("Opening position", context={
            "z": round(z, 4),
            "beta": round(self._beta, 4),
            "adf_pval": round(adf_pval, 4) if adf_pval is not None else "disabled",
            "side_a": side_a.name,
            "side_b": side_b.name,
            "qty_a": qty_a,
            "qty_b": qty_b,
            "open_cycles": self._open_cycles,
        })
        self._transition(PairState.ENTERING, "entry_signal")
        self._submit_entry_orders(side_a, qty_a, side_b, qty_b)

    # ------------------------------------------------------------------
    # EXIT
    # ------------------------------------------------------------------
    def _exit(self) -> None:
        if self._pair_state != PairState.OPEN:
            return
        if self._has_pending_orders():
            return
        if self._bars_since_entry < self.params.min_hold_bars:
            self.observe("Exit deferred: min_hold_bars not reached", context={
                "bars_since_entry": self._bars_since_entry,
                "min_hold_bars": self.params.min_hold_bars,
            })
            return

        self.decide("Closing position", context={
            "pos_qty_a": self._pos_qty["A"],
            "pos_qty_b": self._pos_qty["B"],
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
        })
        self._exit_retry_count = 0      # reset retry counter on fresh exit
        self._transition(PairState.EXITING, "exit_signal")
        self._submit_exit_orders()

    # ------------------------------------------------------------------
    # ORDER EVENTS
    # ------------------------------------------------------------------
    def on_order_filled(self, event) -> None:
        # fix: use .value not str() — str() may return "ClientOrderId('O-...')" not the raw ID
        oid = event.client_order_id.value
        leg_id, _ = self.extract_leg_info_from_order_id(oid)

        if leg_id not in self._orders:
            self.log.warning(f"on_order_filled: unrecognised leg_id '{leg_id}' for order {oid}")
            return

        qty = float(event.last_qty)
        px = float(event.last_px)

        if oid in self._orders[leg_id]["entry"]:
            self._apply_entry_fill(leg_id, qty, px)
            self._discard_order_id(oid)
            if (
                self._pos_qty["A"] >= self._target_qty["A"]
                and self._pos_qty["B"] >= self._target_qty["B"]
            ):
                self._transition(PairState.OPEN, "entry_complete")

        elif oid in self._orders[leg_id]["exit"]:
            self._apply_exit_fill(leg_id, qty, px)
            if self._pos_qty[leg_id] <= 0:
                self._discard_order_id(oid)
            if not self._has_any_position():
                if self._shutting_down:
                    self.forced_stop("Shutdown complete — positions neutralized", "SYSTEM")
                else:
                    self._complete_cycle()

    def on_order_rejected(self, event) -> None:
        # fix: use .value not str()
        oid = event.client_order_id.value
        self.observe("Order rejected", context={
            "order_id": oid,
            "reason": str(getattr(event, "reason", "unknown")),
            "pair_state": self._pair_state.name,
            "shutting_down": self._shutting_down,
        })
        self._discard_order_id(oid)
        if self._shutting_down:
            self._neutralize_and_stop()
        else:
            self._handle_order_failure(oid)

    def on_order_cancelled(self, event) -> None:
        # fix: use .value not str()
        oid = event.client_order_id.value
        self.observe("Order cancelled", context={
            "order_id": oid,
            "pair_state": self._pair_state.name,
            "shutting_down": self._shutting_down,
        })
        self._discard_order_id(oid)
        if self._shutting_down:
            self._neutralize_and_stop()
        else:
            self._handle_order_failure(oid)

    def _handle_order_failure(self, oid: str) -> None:
        if self._pair_state == PairState.ENTERING:
            if self._has_any_position():
                self.observe("Partial entry aborted — exiting filled legs", context={
                    "order_id": oid,
                    "pos_qty_a": self._pos_qty["A"],
                    "pos_qty_b": self._pos_qty["B"],
                })
                self._transition(PairState.EXITING, "partial_entry_abort")
                self._submit_exit_orders()
            else:
                self.observe("Entry aborted — no fills, resetting to FLAT", context={
                    "order_id": oid,
                })
                self._complete_cycle()

        elif self._pair_state == PairState.EXITING:
            if not self._has_pending_orders() and self._has_any_position():
                # fix: enforce max_exit_retries to avoid infinite retry loop
                self._exit_retry_count += 1
                if self._exit_retry_count > self.params.max_exit_retries:
                    self.observe("Exit retry limit reached — initiating shutdown", context={
                        "order_id": oid,
                        "exit_retry_count": self._exit_retry_count,
                        "max_exit_retries": self.params.max_exit_retries,
                    })
                    self._initiate_shutdown(reason="exit_retry_limit_exceeded")
                else:
                    self.observe("Exit order failed — resubmitting", context={
                        "order_id": oid,
                        "retry": self._exit_retry_count,
                        "max_retries": self.params.max_exit_retries,
                    })
                    self._submit_exit_orders()

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    def _compute_entry_quantities(self) -> Tuple[int, int]:
        """
        fix: size leg B using the rolling beta (price_a / price_b) so the pair
        is dollar-neutral rather than notional-symmetric.

        Dollar-neutral means: qty_a * price_a ≈ qty_b * price_b
        Given qty_a = notional / price_a, we get:
            qty_b = qty_a * (price_a / price_b) = qty_a * beta
        """
        per_leg = min(self.params.max_order_notional, self.params.max_notional)
        if self._last_mid_a <= 0 or self._last_mid_b <= 0:
            return 0, 0
        qty_a = int(per_leg / self._last_mid_a)
        # fix: scale qty_b by beta to achieve dollar-neutral sizing
        qty_b = int(qty_a * self._beta)
        return qty_a, qty_b

    def _resolve_tif(self) -> TimeInForce:
        return getattr(TimeInForce, self.params.tif.upper(), TimeInForce.DAY)

    def _update_pnl(self) -> None:
        unreal = 0.0
        for leg in ("A", "B"):
            qty = self._pos_qty[leg]
            side = self._pos_side[leg]
            avg = self._avg_entry[leg]
            mid = self._last_mid_a if leg == "A" else self._last_mid_b
            if qty > 0 and side is not None and mid is not None:
                unreal += (mid - avg) * qty if side == OrderSide.BUY else (avg - mid) * qty
        self._unrealized_pnl = unreal
        self._current_pnl = self._realized_pnl + unreal

    def _apply_entry_fill(self, leg_id: str, qty: float, px: float) -> None:
        prev = self._pos_qty[leg_id]
        new = prev + qty
        if new > 0:
            self._avg_entry[leg_id] = (self._avg_entry[leg_id] * prev + px * qty) / new
        self._pos_qty[leg_id] = new

    def _apply_exit_fill(self, leg_id: str, qty: float, px: float) -> None:
        prev = self._pos_qty[leg_id]
        side = self._pos_side[leg_id]
        avg = self._avg_entry[leg_id]
        pnl = (px - avg) * qty if side == OrderSide.BUY else (avg - px) * qty
        self._realized_pnl += pnl
        self._pos_qty[leg_id] = max(0.0, prev - qty)

    def _submit_entry_orders(
        self, side_a: OrderSide, qty_a: int, side_b: OrderSide, qty_b: int
    ) -> None:
        tif = self._resolve_tif()
        px_a = self._marketable_limit_price(self._last_mid_a, side_a)
        px_b = self._marketable_limit_price(self._last_mid_b, side_b)

        order_a = self.submit_limit_order(
            symbol=self.symbol_a, side=side_a, qty=qty_a,
            price=px_a, tif=tif, leg_id="A", role="OPEN",
        )
        order_b = self.submit_limit_order(
            symbol=self.symbol_b, side=side_b, qty=qty_b,
            price=px_b, tif=tif, leg_id="B", role="OPEN",
        )

        self._orders["A"]["entry"].add(order_a.client_order_id.value)
        self._orders["B"]["entry"].add(order_b.client_order_id.value)

        self.act("Entry orders submitted", context={
            "mid_a": round(self._last_mid_a, 4),
            "mid_b": round(self._last_mid_b, 4),
            "spread": round(self._last_spread, 4),
            "z": round(self._last_z, 4),
            "beta": round(self._beta, 4),
            "qty_a": qty_a,
            "qty_b": qty_b,
            "tif": tif.name,
            "current_pnl": round(self._current_pnl, 2),
        })

    def _submit_exit_orders(self) -> None:
        tif = self._resolve_tif()
        for leg in ("A", "B"):
            qty = self._pos_qty[leg]
            if qty <= 0:
                continue
            side = self._pos_side[leg]
            close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            mid = self._last_mid_a if leg == "A" else self._last_mid_b
            px = self._marketable_limit_price(mid, close_side)

            order = self.submit_limit_order(
                symbol=self.symbol_a if leg == "A" else self.symbol_b,
                side=close_side, qty=int(qty),
                price=px, tif=tif, leg_id=leg, role="CLOSE",
            )
            self._orders[leg]["exit"].add(order.client_order_id.value)

        self.act("Exit orders submitted", context={
            "mid_a": round(self._last_mid_a, 4),
            "mid_b": round(self._last_mid_b, 4),
            "spread": round(self._last_spread, 4),
            "z": round(self._last_z, 4),
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
            "current_pnl": round(self._current_pnl, 2),
        })

    def _marketable_limit_price(self, mid: float, side: OrderSide) -> Price:
        bps = self.params.marketable_limit_bps / 10000
        px = mid * (1 + bps) if side == OrderSide.BUY else mid * (1 - bps)
        return Price.from_str(f"{px:.2f}")

    def _discard_order_id(self, oid: str) -> None:
        for leg in ("A", "B"):
            self._orders[leg]["entry"].discard(oid)
            self._orders[leg]["exit"].discard(oid)

    def _has_any_position(self) -> bool:
        return self._pos_qty["A"] > 0 or self._pos_qty["B"] > 0

    def _has_pending_orders(self) -> bool:
        return any(
            len(self._orders[leg][role]) > 0
            for leg in ("A", "B")
            for role in ("entry", "exit")
        )

    def _transition(self, new_state: PairState, reason: str) -> None:
        old = self._pair_state
        self._pair_state = new_state
        self.observe("State transition", context={
            "from": old.name, "to": new_state.name, "reason": reason,
        })

    def _complete_cycle(self) -> None:
        self._pos_qty = {"A": 0.0, "B": 0.0}
        self._pos_side = {"A": None, "B": None}
        self._avg_entry = {"A": 0.0, "B": 0.0}
        self._target_qty = {"A": 0.0, "B": 0.0}
        self._orders = {"A": {"entry": set(), "exit": set()}, "B": {"entry": set(), "exit": set()}}
        self._entry_dir = 0
        self._cooldown_bars = self.params.cooldown_after_exit_bars
        self._open_cycles = max(0, self._open_cycles - 1)
        self._bars_since_entry = 0
        self._exit_retry_count = 0
        self._transition(PairState.FLAT, "cycle_complete")

        self.observe("Cycle complete", context={
            "cooldown_bars": self._cooldown_bars,
            "open_cycles": self._open_cycles,
            "realized_pnl": round(self._realized_pnl, 2),
        })

        gain_limit = self.params.max_gain_dollars
        loss_limit = self.params.max_loss_dollars
        if gain_limit > 0 and self._realized_pnl >= gain_limit:
            self._pnl_limit_reached = True
            self._initiate_shutdown(
                reason=f"Max gain reached: pnl={round(self._realized_pnl, 2)} >= {gain_limit}"
            )
        elif loss_limit > 0 and self._realized_pnl <= -loss_limit:
            self._pnl_limit_reached = True
            self._initiate_shutdown(
                reason=f"Max loss reached: pnl={round(self._realized_pnl, 2)} <= -{loss_limit}"
            )

    def _initiate_shutdown(self, reason: str) -> None:
        self._shutting_down = True
        self.observe("Initiating shutdown", context={"reason": reason})
        if self._has_pending_orders():
            self.cancelAllOrders()
        else:
            self._neutralize_and_stop()

    def _neutralize_and_stop(self) -> None:
        if self._has_pending_orders():
            return
        if not self._has_any_position():
            self.forced_stop("Shutdown complete — positions flat", "SYSTEM")
            return
        self.observe("Neutralizing positions with market orders", context={
            "pos_qty_a": self._pos_qty["A"],
            "pos_qty_b": self._pos_qty["B"],
        })
        for leg in ("A", "B"):
            qty = int(self._pos_qty[leg])
            if qty <= 0:
                continue
            side = self._pos_side[leg]
            if side is None:
                continue
            close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            symbol = self.symbol_a if leg == "A" else self.symbol_b
            clid = self.submit_market_order(
                symbol=symbol, side=close_side, qty=qty, leg_id=leg, role="NEUTRALIZE"
            )
            self._orders[leg]["exit"].add(clid.value)

    def get_metrics(self) -> Dict[str, Any]:
        bars_collected = len(self._prices_a)
        lookback = self.params.lookback
        return {
            # State
            "pair_state": self._pair_state.name,
            "shutting_down": self._shutting_down,
            # Warmup progress
            "warmup_pct": round(min(bars_collected / lookback, 1.0) * 100, 1),
            "bars_a": bars_collected,
            "bars_b": len(self._prices_b),
            "history_ready": self._spread_stats.full,
            # Live signal
            "last_z": round(self._last_z, 4),
            "last_spread": round(self._last_spread, 4),
            "beta": round(self._beta, 4),
            "last_mid_a": round(self._last_mid_a, 4) if self._last_mid_a is not None else None,
            "last_mid_b": round(self._last_mid_b, 4) if self._last_mid_b is not None else None,
            # Position
            "pos_qty_a": self._pos_qty["A"],
            "pos_qty_b": self._pos_qty["B"],
            "avg_entry_a": round(self._avg_entry["A"], 4) if self._pos_qty["A"] > 0 else None,
            "avg_entry_b": round(self._avg_entry["B"], 4) if self._pos_qty["B"] > 0 else None,
            # P&L
            "realized_pnl": round(self._realized_pnl, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
            "current_pnl": round(self._current_pnl, 2),
            # Cycle tracking
            "open_cycles": self._open_cycles,
            "cooldown_bars": self._cooldown_bars,
            "bars_since_entry": self._bars_since_entry,
            "exit_retry_count": self._exit_retry_count,
            "pnl_limit_reached": self._pnl_limit_reached,
        }

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type
