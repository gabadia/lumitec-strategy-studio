"""
Midpoint Peg Order Strategy
============================
Executes a one-shot order (BUY or SELL) by continuously pegging a passive limit
order to the bid/ask midpoint. Reprices only when the midpoint moves by at least
`min_reprice_move_cents`. Optionally crosses the spread after
`aggression_timeout_seconds` if not fully filled.

Leg config (set in dashboard):
  side                        — "BUY" or "SELL"
  quantity                    — total shares to execute

Strategy params (hot-updatable):
  min_reprice_move_cents      — minimum midpoint move (cents) to trigger reprice (default 1.0)
  aggression_timeout_seconds  — seconds before crossing spread; 0 = never (default 0)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from typing import Any, Dict, Optional

from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Price

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
class Config(LumitecStrategyConfig):
    strategy_name: str = "MidpointPeg"
    file_name: str = "midpoint_peg.py"


# ---------------------------------------------------------------------------
# 2. ConfigParams
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    min_reprice_move_cents: float = 1.0
    aggression_timeout_seconds: int = 0   # 0 = stay passive forever

    def validate(self) -> None:
        if self.min_reprice_move_cents <= 0:
            raise ValueError("min_reprice_move_cents must be > 0")
        if self.aggression_timeout_seconds < 0:
            raise ValueError("aggression_timeout_seconds must be >= 0")

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
            elif ft in (str, "str"):
                v = str(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new


# ---------------------------------------------------------------------------
# 3. Strategy
# ---------------------------------------------------------------------------
class MidpointPeg(LumitecBaseStrategy):
    mission    = StrategyMission.EXECUTION
    objective  = StrategyObjective.TARGET_QTY
    leg_mode   = LegMode.FINITE
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        self.symbol_a: Optional[str] = None
        self._side: str = "BUY"

        # Execution state
        self._remaining_qty: int = 0
        self._filled_qty: int = 0
        self._avg_fill_px: float = 0.0
        self._active_order_id: Optional[str] = None
        self._last_peg_price: float = 0.0
        self._pending_reprice_mid: float = 0.0

        # Flags
        self._cancel_pending: bool = False
        self._aggressive: bool = False
        self._stopping: bool = False
        self._start_time: float = 0.0

        # Last known quote
        self._last_mid: float = 0.0
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0

    # ------------------------------------------------------------------
    # Required hooks
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        (self.leg_a,) = self.legs[:1]
        self.symbol_a = self.leg_a["symbol"]
        self._side = str(self.leg_a.get("side", "BUY")).upper()
        self._remaining_qty = int(self.leg_a["quantity"])
        self._start_time = time.time()

        self.subscribe_market_data(
            symbol=self.symbol_a,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self.observe("MidpointPeg started", context={
            "symbol": self.symbol_a,
            "side": self._side,
            "quantity": self._remaining_qty,
            "min_reprice_move_cents": self.params.min_reprice_move_cents,
            "aggression_timeout_seconds": self.params.aggression_timeout_seconds,
        })

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if symbol != self.symbol_a:
            return
        if self.isPaused():
            return
        if self._stopping or self._remaining_qty <= 0:
            return

        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        if bid <= 0 or ask <= 0 or ask <= bid:
            return

        mid = round((bid + ask) / 2.0, 2)
        self._last_mid = mid
        self._last_bid = bid
        self._last_ask = ask

        # Check aggression timeout
        if (self.params.aggression_timeout_seconds > 0
                and not self._aggressive
                and time.time() - self._start_time > self.params.aggression_timeout_seconds):
            self.observe("Aggression timeout — crossing spread", context={
                "bid": bid, "ask": ask,
                "filled": self._filled_qty, "remaining": self._remaining_qty,
            })
            self._trigger_aggression(bid, ask)
            return

        if self._cancel_pending or self._aggressive:
            return

        if self._active_order_id is not None:
            # Reprice only if mid has moved enough
            move_cents = abs(mid - self._last_peg_price) * 100
            if move_cents < self.params.min_reprice_move_cents:
                return
            # Cancel existing order; on_order_canceled will re-peg
            self._pending_reprice_mid = mid
            self._cancel_pending = True
            self.cancelOrdersForSymbol(self.symbol_a)
        else:
            self._submit_peg(mid)

    # ------------------------------------------------------------------
    # Order submission helpers
    # ------------------------------------------------------------------
    def _submit_peg(self, mid: float) -> None:
        if self._remaining_qty <= 0 or self._stopping:
            return
        side = OrderSide.BUY if self._side == "BUY" else OrderSide.SELL
        role = "OPEN" if self._side == "BUY" else "CLOSE"
        self.decide("Pegging at midpoint", context={
            "mid": mid, "qty": self._remaining_qty, "side": self._side,
        })
        order = self.submit_limit_order(
            symbol=self.symbol_a,
            side=side,
            qty=self._remaining_qty,
            price=Price.from_str(f"{mid:.2f}"),
            tif=TimeInForce.DAY,
            leg_id="A",
            role=role,
        )
        self._active_order_id = order.client_order_id.value
        self._last_peg_price = mid
        self.act("Peg order submitted", context={
            "order_id": self._active_order_id,
            "price": mid,
            "qty": self._remaining_qty,
        })

    def _submit_aggressive(self, bid: float, ask: float) -> None:
        if self._remaining_qty <= 0 or self._stopping:
            return
        side = OrderSide.BUY if self._side == "BUY" else OrderSide.SELL
        role = "OPEN" if self._side == "BUY" else "CLOSE"
        # BUY lifts the ask; SELL hits the bid
        px = ask if self._side == "BUY" else bid
        self.decide("Submitting aggressive order to guarantee fill", context={
            "price": px, "qty": self._remaining_qty, "side": self._side,
        })
        order = self.submit_limit_order(
            symbol=self.symbol_a,
            side=side,
            qty=self._remaining_qty,
            price=Price.from_str(f"{px:.2f}"),
            tif=TimeInForce.DAY,
            leg_id="A",
            role=role,
        )
        self._active_order_id = order.client_order_id.value
        self.act("Aggressive order submitted", context={
            "order_id": self._active_order_id,
            "price": px,
            "qty": self._remaining_qty,
        })

    def _trigger_aggression(self, bid: float, ask: float) -> None:
        self._aggressive = True
        if self._active_order_id is not None:
            # Cancel current peg first; on_order_canceled will submit aggressive
            self._cancel_pending = True
            self.cancelOrdersForSymbol(self.symbol_a)
        else:
            self._submit_aggressive(bid, ask)

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
        fill_qty = int(event.last_qty)

        # Update running average fill price
        total_cost = self._avg_fill_px * self._filled_qty + fill_px * fill_qty
        self._filled_qty += fill_qty
        self._avg_fill_px = total_cost / self._filled_qty if self._filled_qty else 0.0
        target_qty = int(self.leg_a["quantity"])
        self._remaining_qty = max(0, target_qty - self._filled_qty)

        self.act(f"Fill: {fill_qty} @ {fill_px:.2f}", context={
            "order_id": oid,
            "filled_qty": self._filled_qty,
            "remaining_qty": self._remaining_qty,
            "avg_fill_px": round(self._avg_fill_px, 4),
        })

        if self._remaining_qty == 0:
            self._active_order_id = None
            self.observe(
                f"Fully filled {self._filled_qty} shares @ avg {self._avg_fill_px:.4f}"
            )
            self.stop()
        elif oid != self._active_order_id and self._active_order_id is not None and not self._cancel_pending:
            # Late fill from a previous order while a new order is already live.
            # Cancel the live order and re-peg for the corrected remaining qty.
            self._pending_reprice_mid = self._last_mid
            self._cancel_pending = True
            self.cancelOrdersForSymbol(self.symbol_a)
        # else: partial fill for the active order — leave it working at the exchange.
        # It will continue filling; reprice only fires when mid moves enough.

    def on_order_canceled(self, event) -> None:
        self._active_order_id = None
        was_our_cancel = self._cancel_pending
        self._cancel_pending = False
        if self._stopping or self._remaining_qty <= 0:
            return
        if self._aggressive:
            self._submit_aggressive(self._last_bid, self._last_ask)
        elif self._pending_reprice_mid > 0:
            self._submit_peg(self._pending_reprice_mid)
            self._pending_reprice_mid = 0.0
        elif not was_our_cancel and self._last_mid > 0:
            # Exchange-initiated cancel (e.g. DAY order expired, venue reset).
            # Re-peg immediately rather than waiting for the next quote tick.
            self.observe("Order canceled by exchange — re-pegging", context={
                "remaining_qty": self._remaining_qty,
                "mid": self._last_mid,
            })
            self._submit_peg(self._last_mid)

    def on_order_rejected(self, event) -> None:
        self._active_order_id = None
        self._cancel_pending = False
        self.observe(f"Order rejected: {event.client_order_id.value}")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._active_order_id = None
        self._cancel_pending = False
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        # Re-peg immediately using the last known mid if available
        self.act("Resumed")
        if self._last_mid > 0 and self._remaining_qty > 0 and not self._stopping:
            self._submit_peg(self._last_mid)

    # ------------------------------------------------------------------
    # Leg validation
    # ------------------------------------------------------------------
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(f"MidpointPeg requires exactly 1 leg, got {len(legs)}")
        side = legs[0].get("side", "").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"MidpointPeg leg side must be BUY or SELL, got '{side}'")

    def on_stop(self) -> None:
        self._stopping = True
        self.cancelAllOrders()
        self.observe("MidpointPeg stopped", context={
            "filled_qty": self._filled_qty,
            "remaining_qty": self._remaining_qty,
            "avg_fill_px": round(self._avg_fill_px, 4),
        })

    # ------------------------------------------------------------------
    # Hot parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)
