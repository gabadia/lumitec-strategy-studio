"""
Bid/Ask Spread Capture (Market Making) Strategy
================================================
Mission  : MARKET_MAKING
Objective: SIGNAL_DRIVEN

Logic:
  Quote at best bid and best ask simultaneously, capturing the spread on
  round-trips. Inventory is kept near flat via a max_inventory guard that
  suppresses the quote on the side that would breach the limit.

  On each quote tick:
    1. Compute spread = ask - bid. Skip if < min_spread.
    2. If cancel_on_move=True and bid/ask moved since last quote,
       cancel any resting order on the moved side.
    3. Respect quote_refresh_ms — don't re-quote more often than this.
    4. Place BUY limit at best bid  if: no active bid order AND
       current inventory < max_inventory.
    5. Place SELL limit at best ask if: no active ask order AND
       current inventory > -max_inventory.
    6. On every tick check mark-to-market P&L:
       - Stop and cancel all orders if pnl >= max_gain or pnl <= -max_loss.

  Inventory & P&L tracking:
    - _inventory  : net shares (+ long, – short)
    - _cash       : running cash (−buy fills, +sell fills)
    - MTM P&L     : _cash + _inventory * current_mid
    - Realized P&L: _cash (when _inventory == 0)

Assumptions:
  - Symbol : AAPL (as specified)
  - Data   : quote ticks via subscribe_market_data(subscribe_quotes=True)
  - Orders : limit at best bid / best ask; DAY TIF
  - Leg    : single leg, side BUY, quantity = max_inventory (platform uses
             this for position tracking; actual trading is two-sided)
  - max_gain default: 500 (matches platform default max_loss)
  - Slippage not modelled here — limit orders at touch assume no queue risk
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
    strategy_name: str = "BidAskSpreadCapture"
    file_name: str = "bid_ask_spread_capture.py"


# ---------------------------------------------------------------------------
# 2. ConfigParams
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    """All hot-updatable strategy parameters."""

    symbol: str = "AAPL"
    order_size: int = 100           # Shares per child order
    min_spread: float = 0.01        # Minimum bid/ask spread to quote
    max_inventory: int = 500        # Max long or short inventory
    quote_refresh_ms: int = 500     # Minimum ms between requotes
    cancel_on_move: bool = True     # Cancel stale orders when prices change
    max_gain: float = 500.0         # Stop when MTM P&L exceeds this
    max_loss: float = 500.0         # Stop when MTM P&L falls below –this

    def validate(self) -> None:
        if self.order_size <= 0:
            raise ValueError("order_size must be > 0")
        if self.min_spread < 0:
            raise ValueError("min_spread must be >= 0")
        if self.max_inventory <= 0:
            raise ValueError("max_inventory must be > 0")
        if self.order_size > self.max_inventory:
            raise ValueError("order_size cannot exceed max_inventory")
        if self.quote_refresh_ms <= 0:
            raise ValueError("quote_refresh_ms must be > 0")
        if self.max_gain <= 0:
            raise ValueError("max_gain must be > 0")
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
            elif field_type in (bool, "bool"):
                v = bool(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new


# ---------------------------------------------------------------------------
# 3. Strategy
# ---------------------------------------------------------------------------
class BidAskSpreadCapture(LumitecBaseStrategy):
    mission    = StrategyMission.MARKET_MAKING
    objective  = StrategyObjective.SIGNAL_DRIVEN
    leg_mode   = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    def __init__(self, config: Config) -> None:
        super().__init__(config)

        self._param_lock = RLock()
        self.params = ConfigParams.from_config(config)

        self.symbol: Optional[str] = None

        # Quote state
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_mid: float = 0.0
        self._last_quote_ts_ns: int = 0     # wall-clock ts of last requote
        self._live_after_ts_ns: int = 0     # replay guard

        # Active order IDs
        self._bid_order_id: Optional[str] = None
        self._ask_order_id: Optional[str] = None

        # Inventory & P&L
        self._inventory: int = 0            # net shares
        self._cash: float = 0.0             # running cash (sell receipts – buy costs)

        self._shutting_down: bool = False

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

        self._live_after_ts_ns = time.time_ns() - 10 * 1_000_000_000

        self.subscribe_market_data(
            symbol=self.symbol,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        self.observe("BidAskSpreadCapture started", context={
            "symbol":          self.symbol,
            "order_size":      self.params.order_size,
            "min_spread":      self.params.min_spread,
            "max_inventory":   self.params.max_inventory,
            "quote_refresh_ms": self.params.quote_refresh_ms,
            "cancel_on_move":  self.params.cancel_on_move,
            "max_gain":        self.params.max_gain,
            "max_loss":        self.params.max_loss,
        })

    # ------------------------------------------------------------------
    # Quote tick handler
    # ------------------------------------------------------------------
    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if symbol != self.symbol:
            return
        if self.isPaused():
            return
        if self._shutting_down:
            return
        if tick.ts_event < self._live_after_ts_ns:
            return   # replay guard

        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        mid = (bid + ask) / 2.0
        spread = ask - bid

        self._last_mid = mid

        # ── P&L check ────────────────────────────────────────────────
        mtm_pnl = self._cash + self._inventory * mid
        self.observe("Quote tick", context={
            "bid": round(bid, 4), "ask": round(ask, 4),
            "spread": round(spread, 4),
            "inventory": self._inventory,
            "mtm_pnl": round(mtm_pnl, 2),
        })

        if mtm_pnl >= self.params.max_gain:
            self._shutdown(f"Max gain reached: ${mtm_pnl:.2f}")
            return
        if mtm_pnl <= -self.params.max_loss:
            self._shutdown(f"Max loss reached: ${mtm_pnl:.2f}")
            return

        # ── Spread filter ─────────────────────────────────────────────
        if spread < self.params.min_spread:
            self.observe("Spread too tight — not quoting", context={
                "spread": round(spread, 4),
                "min_spread": self.params.min_spread,
            })
            return

        # ── Cancel on move ────────────────────────────────────────────
        bid_moved = abs(bid - self._last_bid) > 1e-6
        ask_moved = abs(ask - self._last_ask) > 1e-6

        if self.params.cancel_on_move:
            if bid_moved and self._bid_order_id:
                self.cancelOrdersForSymbol(self.symbol)
                self._bid_order_id = None
                self._ask_order_id = None
            elif ask_moved and self._ask_order_id:
                self.cancelOrdersForSymbol(self.symbol)
                self._bid_order_id = None
                self._ask_order_id = None

        self._last_bid = bid
        self._last_ask = ask

        # ── Requote interval guard ─────────────────────────────────────
        now_ns = time.time_ns()
        refresh_ns = self.params.quote_refresh_ms * 1_000_000
        if now_ns - self._last_quote_ts_ns < refresh_ns:
            return

        # ── Place quotes ──────────────────────────────────────────────
        self._place_quotes(bid, ask)
        self._last_quote_ts_ns = now_ns

    def _place_quotes(self, bid: float, ask: float) -> None:
        """Place bid and/or ask limit orders if slots are open."""

        # BUY at best bid — only if under inventory limit
        if self._bid_order_id is None and self._inventory < self.params.max_inventory:
            qty = min(self.params.order_size,
                      self.params.max_inventory - self._inventory)
            if qty > 0:
                bid_price = Price.from_str(f"{bid:.2f}")
                order = self.submit_limit_order(
                    symbol=self.symbol,
                    side=OrderSide.BUY,
                    qty=qty,
                    price=bid_price,
                    tif=TimeInForce.DAY,
                    leg_id="A",
                    role="OPEN",
                )
                self._bid_order_id = order.client_order_id.value
                self.act("BUY quote placed", context={
                    "order_id": self._bid_order_id,
                    "qty": qty, "price": str(bid_price),
                })

        # SELL at best ask — only if above inventory floor
        if self._ask_order_id is None and self._inventory > -self.params.max_inventory:
            qty = min(self.params.order_size,
                      self.params.max_inventory + self._inventory)
            if qty > 0:
                ask_price = Price.from_str(f"{ask:.2f}")
                order = self.submit_limit_order(
                    symbol=self.symbol,
                    side=OrderSide.SELL,
                    qty=qty,
                    price=ask_price,
                    tif=TimeInForce.DAY,
                    leg_id="A",
                    role="CLOSE",
                )
                self._ask_order_id = order.client_order_id.value
                self.act("SELL quote placed", context={
                    "order_id": self._ask_order_id,
                    "qty": qty, "price": str(ask_price),
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

        fill_qty  = int(event.last_qty)
        fill_px   = float(event.last_px)
        is_buy    = (oid == self._bid_order_id)

        if is_buy:
            self._inventory += fill_qty
            self._cash      -= fill_px * fill_qty
            self._bid_order_id = None
        else:
            self._inventory -= fill_qty
            self._cash      += fill_px * fill_qty
            self._ask_order_id = None

        mtm_pnl = self._cash + self._inventory * self._last_mid

        self.act(f"{'BUY' if is_buy else 'SELL'} fill", context={
            "order_id":  oid,
            "qty":       fill_qty,
            "price":     round(fill_px, 4),
            "inventory": self._inventory,
            "mtm_pnl":   round(mtm_pnl, 2),
        })

        # Re-check P&L limits after fill
        if mtm_pnl >= self.params.max_gain:
            self._shutdown(f"Max gain reached after fill: ${mtm_pnl:.2f}")
        elif mtm_pnl <= -self.params.max_loss:
            self._shutdown(f"Max loss reached after fill: ${mtm_pnl:.2f}")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        if oid == self._bid_order_id:
            self._bid_order_id = None
        elif oid == self._ask_order_id:
            self._ask_order_id = None
        self.observe(f"Order rejected: {oid} reason={event.reason}")

    def on_order_cancelled(self, event) -> None:
        oid = event.client_order_id.value
        if oid == self._bid_order_id:
            self._bid_order_id = None
        if oid == self._ask_order_id:
            self._ask_order_id = None
        self.observe(f"Order cancelled: {oid}")

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._bid_order_id = None
        self._ask_order_id = None
        self._last_quote_ts_ns = 0      # force immediate requote on resume
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Leg validation
    # ------------------------------------------------------------------
    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(f"BidAskSpreadCapture requires exactly 1 leg, got {len(legs)}")

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    def _shutdown(self, reason: str) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.cancelAllOrders()
        self._bid_order_id = None
        self._ask_order_id = None
        self.decide(f"Shutting down: {reason}", context={
            "inventory": self._inventory,
            "cash":      round(self._cash, 2),
            "mtm_pnl":   round(self._cash + self._inventory * self._last_mid, 2),
        })
        self.forced_stop(reason, "RISK")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------
    def on_stop(self) -> None:
        mtm_pnl = self._cash + self._inventory * self._last_mid
        self.observe("Strategy stopped", context={
            "inventory":  self._inventory,
            "cash":       round(self._cash, 2),
            "mtm_pnl":    round(mtm_pnl, 2),
            "last_bid":   round(self._last_bid, 4),
            "last_ask":   round(self._last_ask, 4),
        })

    # ------------------------------------------------------------------
    # Hot parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old_spread   = self.params.min_spread
            old_refresh  = self.params.quote_refresh_ms
            self.params  = self.params.merged(updates)

            # Rebuild quote schedule when key params change
            if (self.params.min_spread != old_spread or
                    self.params.quote_refresh_ms != old_refresh):
                self._last_quote_ts_ns = 0   # force immediate requote
                self.cancelAllOrders()
                self._bid_order_id = None
                self._ask_order_id = None
                self.observe("Quotes reset after param update", context={
                    "new_min_spread":      self.params.min_spread,
                    "new_quote_refresh_ms": self.params.quote_refresh_ms,
                })

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def get_metrics(self) -> Dict[str, Any]:
        mtm_pnl = self._cash + self._inventory * self._last_mid
        return {
            "inventory":      self._inventory,
            "mtm_pnl":        round(mtm_pnl, 2),
            "cash":           round(self._cash, 2),
            "last_bid":       round(self._last_bid, 4),
            "last_ask":       round(self._last_ask, 4),
            "last_mid":       round(self._last_mid, 4),
            "bid_order_id":   self._bid_order_id,
            "ask_order_id":   self._ask_order_id,
            "shutting_down":  self._shutting_down,
        }
