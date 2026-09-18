"""
Strategy created using Lumitec's Strategy Studio version X.

CHANGE LOG
  2026-09-14  cancel_on_move_ticks + integer-tick drift detection.
              - Drift is measured against the price the quotes were PLACED at,
                not against the previous tick. Tick-to-tick measurement meant a
                market walking one tick per tick never accumulated enough drift
                to trigger at any threshold above 1, stranding quotes
                arbitrarily far from the touch.
              - Move detection used `abs(bid - last_bid) > 1e-6`, which fires on
                sub-penny float artifacts, so the trigger did not mean what it
                said. Drift is now measured in whole ticks.
              - New `cancel_on_move_ticks` (default 1) sets how much drift is
                tolerated before quotes are pulled. Default 1 preserves existing
                behaviour exactly. Raising it trades adverse-selection
                protection for queue position; do NOT tune it against a
                simulator with no queue model and no informed flow.

  2026-09-14  Tight-spread log throttle.
              - The throttle was re-armed on every wide tick, so a spread
                oscillating between 1 and 2 ticks logged on every tight tick
                (21-25 messages in one second observed). It now re-arms only
                after the spread has been wide for a full interval.

  2026-09-14  Cancel dedup + tick-based spread comparison.
              - `_cancel_live_orders` now sends nothing when a cancel is
                already in flight for every live order. Previously a burst of
                quote ticks during the send->confirm window re-sent
                `cancelOrdersForSymbol()` on each tick (up to 35 redundant
                calls for one order; ~776 calls for ~117 order-sets in a 110s
                run), risking the max_order_rate_per_second budget.
              - Spread filter now compares whole ticks. Float subtraction of
                two prices near 334 gives 0.01999999999998181 for a genuine
                2-cent spread at some price levels and 0.020000000000038654 at
                others, so the old float compare rejected valid spreads
                depending on where the stock happened to be trading — 83
                spurious "spread too tight" blocks out of 147 in one run.

  2026-09-12  One-cycle-at-a-time sequencing + confirmation-gated order state.
              - A cycle is OPEN(bid) + CLOSE(ask) submitted together. No new
                cycle starts until the previous one is flat and has no orders
                working at the venue.
              - Order liveness is now tracked in `_order_remaining` (remaining
                qty per live order id) and `_pending_cancel` (cancel sent, not
                yet confirmed). Order ids are cleared ONLY on a terminal event
                (full fill / confirmed cancel / reject), never on cancel send.
                Previously `_bid_order_id` / `_ask_order_id` were nulled on the
                same line as `cancelOrdersForSymbol()`, so the next requote
                could cross a still-resting order from the prior cycle.
              - `on_order_filled` no longer releases the order slot on a
                PARTIAL fill; it decrements remaining qty instead.
              - Added `on_order_cancel_rejected`: without it a rejected cancel
                would strand an id in `_pending_cancel` and the strategy would
                silently stop quoting for the rest of the session.
              - Mid-cycle re-quote: if the close is cancelled on a price move
                while inventory is open, the close is re-quoted at the new
                touch, sized to `abs(inventory)`, rather than starting a new
                cycle.
              - Shutdown flatten is gated on the same liveness check and
                retried from `on_order_canceled` / `on_order_rejected`, so the
                market order can no longer race `cancelAllOrders()` and hit our
                own resting quote.

Logic:
  Bid/Ask Spread Capture (Market Making). Each cycle quotes at the best bid and
  the best ask simultaneously, capturing the spread on the round-trip. The two
  legs of a cycle cannot cross each other because they sit at bid and ask with
  at least `min_spread` between them. What is serialized is the CYCLE: a new
  OPEN/CLOSE pair is only submitted once the previous cycle's inventory is flat
  and none of its orders are still working (including cancels we have sent but
  not yet had confirmed).

  On each quote tick the strategy checks spread width, optionally cancels stale
  quotes when prices move, enforces a minimum requote interval, and monitors
  mark-to-market P&L against max_gain and max_loss limits.

Key parameters:
  - symbol              - instrument to make markets on
  - order_size          - shares per child order
  - min_spread          - minimum bid/ask spread required to quote
  - max_inventory       - maximum long or short inventory allowed
  - quote_refresh_ms    - minimum milliseconds between requotes
  - cancel_on_move      - cancel stale orders when best bid/ask moves
  - cancel_on_move_ticks - ticks of drift tolerated before cancelling (>=1)
  - max_gain            - stop when MTM P&L exceeds this value
  - max_loss            - stop when MTM P&L falls below negative this value
  - max_position        - maximum gross position (risk scaffold field)
  - max_active_orders_per_side - maximum simultaneous orders per side
  - max_order_rate_per_second  - maximum order submission rate

Market data:
  - Quote ticks via subscribe_market_data(subscribe_quotes=True)

Risk controls:
  - max_position: 500
  - max_loss: 500.0
  - max_active_orders_per_side: 1
  - max_order_rate_per_second: 2.0
  - max_gain guard: strategy force-stops when MTM P&L >= max_gain
  - max_loss guard: strategy force-stops when MTM P&L <= -max_loss

Important notes:
  - Limit orders placed at best bid/ask; no queue-position risk modelled
  - Inventory tracking is internal; platform leg qty used for UI display only
  - Replay guard skips ticks older than 10 seconds at startup
  - A cycle submits 2 orders. At quote_refresh_ms=500 that is up to 4 orders
    per second against max_order_rate_per_second=2.0 — confirm how the base
    class enforces that rate before running hot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace, fields as dc_fields
from typing import Any, Dict, Optional, Set

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
    max_position: int = 500
    max_loss: float = 500.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 2.0
    symbol: str = "AAPL"
    order_size: int = 100
    min_spread: float = 0.01
    max_inventory: int = 500
    quote_refresh_ms: int = 500
    cancel_on_move: bool = True
    cancel_on_move_ticks: int = 1
    max_gain: float = 500.0


# ---------------------------------------------------------------------------
# 2. ConfigParams
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    """All hot-updatable strategy parameters."""

    max_position: int = 500                  # Maximum gross position (risk scaffold)
    max_loss: float = 500.0                  # Stop when MTM P&L falls below –this
    max_active_orders_per_side: int = 1      # Max simultaneous orders per side
    max_order_rate_per_second: float = 2.0   # Max order submission rate
    symbol: str = "AAPL"
    order_size: int = 100           # Shares per child order
    min_spread: float = 0.01        # Minimum bid/ask spread to quote
    max_inventory: int = 500        # Max long or short inventory
    quote_refresh_ms: int = 500     # Minimum ms between requotes
    cancel_on_move: bool = True     # Cancel stale orders when prices change
    cancel_on_move_ticks: int = 1   # Ticks of drift tolerated before cancelling
    max_gain: float = 500.0         # Stop when MTM P&L exceeds this

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
        if self.min_spread < 0:
            raise ValueError("min_spread must be >= 0")
        if self.max_inventory <= 0:
            raise ValueError("max_inventory must be > 0")
        if self.order_size > self.max_inventory:
            raise ValueError("order_size cannot exceed max_inventory")
        if self.quote_refresh_ms <= 0:
            raise ValueError("quote_refresh_ms must be > 0")
        if self.cancel_on_move_ticks < 1:
            # 0 would cancel on every tick regardless of price movement.
            raise ValueError("cancel_on_move_ticks must be >= 1")
        if self.max_gain <= 0:
            raise ValueError("max_gain must be > 0")

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

    # Ticks per price unit. 100 == penny ticks, matching the `f"{x:.2f}"`
    # formatting used when building Price objects. Change both together if the
    # instrument's tick size ever differs.
    _PRICE_TICKS_PER_UNIT = 100

    def __init__(self, config: Config) -> None:
        super().__init__(config)

        self.params = ConfigParams.from_config(config)

        self.symbol: Optional[str] = None

        # Quote state
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_mid: float = 0.0
        self._last_quote_ts_ns: int = 0     # wall-clock ts of last requote
        # Price our live quotes were actually placed at. Drift for
        # cancel_on_move is measured against these, not against the last tick.
        self._quoted_bid: float = 0.0
        self._quoted_ask: float = 0.0
        self._live_after_ts_ns: int = 0     # replay guard

        # Active order IDs (display / correlation only — NOT the liveness test)
        self._bid_order_id: Optional[str] = None
        self._ask_order_id: Optional[str] = None

        # Liveness state. `_order_remaining` maps a live order id to its
        # unfilled qty; an id leaves only on a terminal event. `_pending_cancel`
        # holds ids we have sent a cancel for but not yet had confirmed — those
        # are STILL RESTING at the venue and must block the next cycle.
        self._order_remaining: Dict[str, int] = {}
        self._pending_cancel: Set[str] = set()

        # Inventory & P&L
        self._inventory: int = 0            # net shares
        self._cash: float = 0.0             # running cash (sell receipts – buy costs)

        self._shutting_down: bool = False
        self._shutdown_reason: Optional[str] = None
        self._flatten_order_id: Optional[str] = None

        self._last_spread_tight_log_ns: int = 0
        self._spread_wide_since_ns: int = 0
        self._spread_tight_log_interval_ns: int = 1_000_000_000  # 1 second

    # ------------------------------------------------------------------
    # Required hook
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------
    def _has_live_orders(self) -> bool:
        """True if any order of ours may still be resting at the venue.

        Includes orders we have sent a cancel for but not yet had confirmed —
        an unconfirmed cancel is not a dead order.
        """
        return bool(self._order_remaining) or bool(self._pending_cancel)

    def _release_order(self, oid: str) -> None:
        """Terminal event for `oid`: drop all liveness state for it."""
        self._order_remaining.pop(oid, None)
        self._pending_cancel.discard(oid)
        if oid == self._bid_order_id:
            self._bid_order_id = None
            self._quoted_bid = 0.0
        if oid == self._ask_order_id:
            self._ask_order_id = None
            self._quoted_ask = 0.0

    def _cancel_live_orders(self, reason: str) -> None:
        """Cancel everything working and mark it pending until confirmed.

        Sends nothing if a cancel is already in flight for every live order.
        Without this, a burst of quote ticks during the send->confirm window
        re-sent `cancelOrdersForSymbol()` on every tick — observed at up to 35
        redundant calls for a single order.

        Note `cancelOrdersForSymbol()` is symbol-wide, so `to_cancel` being
        non-empty is enough to justify the call. If the cycle gate is ever
        loosened so a new order can go out while a cancel is pending, switch
        this to per-order cancels.
        """
        to_cancel = set(self._order_remaining) - self._pending_cancel
        if not to_cancel:
            return
        self.cancelOrdersForSymbol(self.symbol)
        self._pending_cancel.update(to_cancel)
        self.observe("Cancel requested", context={
            "reason": reason,
            "orders": sorted(to_cancel),
        })

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
            "cancel_on_move_ticks": self.params.cancel_on_move_ticks,
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

        if mtm_pnl >= self.params.max_gain:
            self._shutdown(f"Max gain reached: ${mtm_pnl:.2f}")
            return
        if mtm_pnl <= -self.params.max_loss:
            self._shutdown(f"Max loss reached: ${mtm_pnl:.2f}")
            return

        # ── Spread filter ─────────────────────────────────────────────
        # Compared in whole ticks. Float subtraction of two prices near 334
        # yields 0.01999999999998181 for a genuine 2-cent spread at some price
        # levels and 0.020000000000038654 at others, so a direct float compare
        # rejected valid spreads unpredictably depending on where the stock was
        # trading (83 spurious blocks in one 110s run).
        spread_ticks = round(spread * self._PRICE_TICKS_PER_UNIT)
        min_ticks    = round(self.params.min_spread * self._PRICE_TICKS_PER_UNIT)

        if spread_ticks < min_ticks:
            now_ns = time.monotonic_ns()
            self._spread_wide_since_ns = 0

            if (
                self._last_spread_tight_log_ns == 0
                or now_ns - self._last_spread_tight_log_ns
                >= self._spread_tight_log_interval_ns
            ):
                self.observe("Spread too tight — not quoting", context={
                    "spread":       round(spread, 4),
                    "spread_ticks": spread_ticks,
                    "min_spread":   self.params.min_spread,
                    "min_ticks":    min_ticks,
                })

                self._last_spread_tight_log_ns = now_ns

            return

        # Re-arm the tight-spread log only after the spread has been WIDE for a
        # full interval. Resetting on every wide tick (as this previously did)
        # meant a spread oscillating between 1 and 2 ticks logged on every
        # tight tick — observed at 21-25 messages in a single second.
        if self._last_spread_tight_log_ns != 0:
            now_ns = time.monotonic_ns()
            if self._spread_wide_since_ns == 0:
                self._spread_wide_since_ns = now_ns
            elif now_ns - self._spread_wide_since_ns >= self._spread_tight_log_interval_ns:
                self._last_spread_tight_log_ns = 0
                self._spread_wide_since_ns = 0

        # ── Cancel on move ────────────────────────────────────────────
        # Drift is measured in whole ticks, not floats. `abs(bid - last) > 1e-6`
        # fired on sub-penny float artifacts, so the trigger did not mean what
        # it said. Both sides are evaluated together: the original `if/elif`
        # meant an ask move was never considered on a tick where the bid also
        # moved. Order ids are NOT cleared here — they clear on confirmation.
        #
        # cancel_on_move_ticks is the drift tolerated before pulling quotes.
        # 1 = pull on any move (default, and the behaviour this strategy has
        # always had). Higher values trade adverse-selection protection for
        # queue position. Do not tune this against a simulator with no queue
        # model and no informed flow — neither side of that tradeoff is
        # observable there.
        #
        # Drift is measured against the price our quotes were PLACED at, not
        # against the previous tick. Measuring tick-to-tick means a market that
        # walks one tick per tick never accumulates enough drift to trigger at
        # any threshold above 1, leaving quotes stranded arbitrarily far from
        # the touch — the precise exposure this guard exists to prevent.
        drift_ticks = 0
        if self._quoted_bid or self._quoted_ask:
            drift_ticks = max(
                abs(round((bid - self._quoted_bid) * self._PRICE_TICKS_PER_UNIT))
                if self._quoted_bid else 0,
                abs(round((ask - self._quoted_ask) * self._PRICE_TICKS_PER_UNIT))
                if self._quoted_ask else 0,
            )

        if (
            self.params.cancel_on_move
            and drift_ticks >= self.params.cancel_on_move_ticks
        ):
            self._cancel_live_orders(
                f"price drifted {drift_ticks} tick(s) from quote"
            )

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

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------
    def _submit(self, side, qty: int, price, role: str) -> str:
        """Submit one limit order and register it as live."""
        order = self.submit_limit_order(
            symbol=self.symbol,
            side=side,
            qty=qty,
            price=price,
            tif=TimeInForce.DAY,
            leg_id="A",
            role=role,
        )
        oid = order.client_order_id.value
        self._order_remaining[oid] = qty
        if side == OrderSide.BUY:
            self._bid_order_id = oid
            self._quoted_bid = float(price.as_double()) if hasattr(price, "as_double") else float(str(price))
        else:
            self._ask_order_id = oid
            self._quoted_ask = float(price.as_double()) if hasattr(price, "as_double") else float(str(price))
        self.act(f"{role} quote placed", context={
            "order_id":  oid,
            "side":      "BUY" if side == OrderSide.BUY else "SELL",
            "qty":       qty,
            "price":     str(price),
            "inventory": self._inventory,
        })
        return oid

    def _place_quotes(self, bid: float, ask: float) -> None:
        """Both legs per cycle; no new cycle until the previous one is flat.

        The OPEN and CLOSE of a single cycle sit at bid and ask respectively, so
        they cannot cross each other. What must not happen is a NEW cycle going
        out while a previous cycle's order is still resting — every self-cross
        observed in production was of that cross-cycle form.
        """
        # Nothing goes out while any prior order may still rest at the venue.
        if self._has_live_orders():
            return

        # ── Mid-cycle: inventory is open but nothing is working (the close was
        #    cancelled on a price move). Re-quote the close only — this is the
        #    same cycle continuing, not a new one.
        if self._inventory != 0:
            if self._inventory > 0:
                side, price = OrderSide.SELL, Price.from_str(f"{ask:.2f}")
            else:
                side, price = OrderSide.BUY, Price.from_str(f"{bid:.2f}")
            self._submit(side, abs(self._inventory), price, "CLOSE")
            return

        # ── Flat and idle: start a new cycle with both legs at once.
        qty = min(self.params.order_size, self.params.max_inventory)
        if qty <= 0:
            return

        self._submit(OrderSide.BUY,  qty, Price.from_str(f"{bid:.2f}"), "OPEN")
        self._submit(OrderSide.SELL, qty, Price.from_str(f"{ask:.2f}"), "CLOSE")

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
        is_buy    = (event.order_side == OrderSide.BUY)

        if is_buy:
            self._inventory += fill_qty
            self._cash      -= fill_px * fill_qty
        else:
            self._inventory -= fill_qty
            self._cash      += fill_px * fill_qty

        # Release the slot only when the order is actually done. A PARTIAL fill
        # leaves the balance resting at the venue; clearing the id here (as the
        # previous version did) let the next cycle quote over it.
        remaining = self._order_remaining.get(oid)
        if remaining is None:
            fully_done = True          # unknown order — treat as terminal
        else:
            remaining -= fill_qty
            if remaining <= 0:
                fully_done = True
            else:
                self._order_remaining[oid] = remaining
                fully_done = False
        if fully_done:
            self._release_order(oid)

        mtm_pnl = self._cash + self._inventory * self._last_mid

        self.act(f"{'BUY' if is_buy else 'SELL'} fill", context={
            "order_id":  oid,
            "qty":       fill_qty,
            "price":     round(fill_px, 4),
            "remaining": max(0, remaining) if remaining is not None else 0,
            "inventory": self._inventory,
            "mtm_pnl":   round(mtm_pnl, 2),
        })

        # A risk shutdown remains active until the flatten order's fills have
        # actually brought internal inventory to zero.
        if self._shutting_down:
            if self._inventory == 0:
                self._flatten_order_id = None
                self._finish_shutdown()
            elif fully_done:
                # Terminal event may have cleared the last blocker.
                self._submit_flatten_order()
            return

        # Re-check P&L limits after fill
        if mtm_pnl >= self.params.max_gain:
            self._shutdown(f"Max gain reached after fill: ${mtm_pnl:.2f}")
        elif mtm_pnl <= -self.params.max_loss:
            self._shutdown(f"Max loss reached after fill: ${mtm_pnl:.2f}")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        self._release_order(oid)
        if oid == self._flatten_order_id:
            self._flatten_order_id = None
            self.observe(f"Flatten order rejected: {oid} reason={event.reason}")
            self._submit_flatten_order()
            return
        self.observe(f"Order rejected: {oid} reason={event.reason}")
        if self._shutting_down:
            self._submit_flatten_order()

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        self._release_order(oid)
        if oid == self._flatten_order_id:
            self._flatten_order_id = None
            self.observe(f"Flatten order cancelled: {oid}")
            self._submit_flatten_order()
            return
        self.observe(f"Order cancelled: {oid}")
        # The cancel we were waiting on may have been the last blocker on the
        # shutdown flatten.
        if self._shutting_down:
            self._submit_flatten_order()

    def on_order_cancel_rejected(self, event) -> None:
        """A rejected cancel means the order is STILL WORKING.

        Without this handler the id would sit in `_pending_cancel` forever and
        `_has_live_orders()` would block every subsequent cycle for the rest of
        the session.
        """
        oid = event.client_order_id.value
        self._pending_cancel.discard(oid)
        self.observe("Cancel rejected — order still working", context={
            "order_id": oid,
            "reason":   getattr(event, "reason", ""),
        })

    # ------------------------------------------------------------------
    # Pause / Resume
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._pending_cancel.update(self._order_remaining.keys())
        self._last_quote_ts_ns = 0      # force immediate requote on resume
        self.act("Paused", context={
            "reason": reason,
            "awaiting_cancel": sorted(self._pending_cancel),
        })

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
        self._shutdown_reason = reason
        self.cancelAllOrders()
        self._pending_cancel.update(self._order_remaining.keys())
        if self._inventory == 0 and not self._has_live_orders():
            self._finish_shutdown()
            return
        # No-ops while orders are still working; retried from the terminal
        # event handlers.
        self._submit_flatten_order()

    def _submit_flatten_order(self) -> None:
        """Submit one market order for the remaining shutdown inventory."""
        if not self._shutting_down or self._flatten_order_id is not None:
            return
        # Never fire the flatten while one of our own quotes may still be
        # resting — an aggressive market order could match it.
        if self._has_live_orders():
            return
        if self._inventory == 0:
            self._finish_shutdown()
            return

        flatten_side = OrderSide.SELL if self._inventory > 0 else OrderSide.BUY
        flatten_qty = abs(self._inventory)
        clid = self.submit_market_order(
            symbol=self.symbol,
            side=flatten_side,
            qty=flatten_qty,
            leg_id="A",
            role="CLOSE",
        )
        self._flatten_order_id = clid.value
        self._order_remaining[self._flatten_order_id] = flatten_qty
        self.act("Flattening inventory on shutdown", context={
            "side": "SELL" if self._inventory > 0 else "BUY",
            "qty": flatten_qty,
            "order_id": self._flatten_order_id,
            "inventory": self._inventory,
        })

    def _finish_shutdown(self) -> None:
        """Stop only after the risk-shutdown position is confirmed flat."""
        reason = self._shutdown_reason or "Risk shutdown"
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
        self.cancelAllOrders()
        self._pending_cancel.update(self._order_remaining.keys())
        # `_shutdown` already submitted the flatten order before invoking
        # `forced_stop`; do not submit a duplicate while handling that stop.
        if self._inventory != 0 and not self._shutting_down:
            if self._has_live_orders():
                # on_stop cannot wait for cancel confirmations, so rather than
                # race our own resting quotes with a market order, leave the
                # position and make the operator aware of it.
                self.observe("FLATTEN SKIPPED — own orders may still be resting; "
                             "flatten manually", context={
                    "inventory":       self._inventory,
                    "working_orders":  sorted(self._order_remaining.keys()),
                    "awaiting_cancel": sorted(self._pending_cancel),
                })
            else:
                flatten_side = OrderSide.SELL if self._inventory > 0 else OrderSide.BUY
                flatten_qty  = abs(self._inventory)
                clid = self.submit_market_order(
                    symbol=self.symbol,
                    side=flatten_side,
                    qty=flatten_qty,
                    leg_id="A",
                    role="CLOSE",
                )
                self.act("Flattening residual inventory on stop", context={
                    "side":      "SELL" if self._inventory > 0 else "BUY",
                    "qty":       flatten_qty,
                    "order_id":  clid.value,
                    "inventory": self._inventory,
                })
        mtm_pnl = self._cash + self._inventory * self._last_mid
        self.observe("Strategy stopped", context={
            "inventory":  self._inventory,
            "cash":       round(self._cash, 2),
            "mtm_pnl":    round(mtm_pnl, 2),
            "last_bid":   round(self._last_bid, 4),
            "last_ask":   round(self._last_ask, 4),
        })
        self.unsubscribe_market_data(symbol=self.symbol, unsubscribe_quotes=True)

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
                self._pending_cancel.update(self._order_remaining.keys())
                self.observe("Quotes reset after param update", context={
                    "new_min_spread":      self.params.min_spread,
                    "new_quote_refresh_ms": self.params.quote_refresh_ms,
                    "awaiting_cancel":     sorted(self._pending_cancel),
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
            "flatten_order_id": self._flatten_order_id,
            "working_orders":   dict(self._order_remaining),
            "awaiting_cancel":  sorted(self._pending_cancel),
            "cycle_open":       self._has_live_orders() or self._inventory != 0,
            "shutting_down":  self._shutting_down,
        }