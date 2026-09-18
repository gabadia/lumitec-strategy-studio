"""
Strategy created using Lumitec's Strategy Studio version X.

Logic:
DisplayedLiquidityCaptureBenchmark is a measurement harness strategy that
systematically probes displayed liquidity by submitting IOC limit orders at
the best bid or ask. Each attempt is called a "trial". The strategy records
timestamps, fill quantities, and outcomes (full fill, partial fill, no fill,
reject) for every trial to benchmark the rate at which a trader can capture
displayed liquidity at the top of book. Trials alternate sides or follow a
configured initial side; the strategy enforces spread, size, staleness, and
position filters before each submission, and records detailed latency
metadata for post-trade analysis.

Key parameters:
- instrument_id            - symbol to benchmark
- order_quantity           - base IOC order size per trial
- minimum_displayed_size   - minimum top-of-book displayed qty required
- maximum_size_participation - max fraction of displayed size to order
- maximum_spread_ticks     - max allowable spread in ticks before blocking
- maximum_quote_age_ms     - max age of quote in ms before it is considered stale
- cooldown_ms              - mandatory wait between trials (ms)
- maximum_trials           - total trials before the strategy self-terminates
- maximum_long_position    - maximum net long inventory
- maximum_short_position   - maximum net short inventory (absolute value)
- ack_timeout_ms           - ms to wait for order ack before marking uncertain
- initial_side             - "BUY" or "SELL" — first trial side
- max_position             - hard position cap (risk)
- max_loss                 - maximum realised loss before forced stop
- max_active_orders_per_side - max simultaneous orders per side (always 1)
- max_order_rate_per_second  - order submission rate cap

Market data:
- Quote ticks (bid/ask price and size) via subscribe_market_data

Risk controls:
- max_position: 50
- max_loss: 999999.0
- max_active_orders_per_side: 1
- max_order_rate_per_second: 1.0
- Spread filter, stale-quote filter, displayed-size filter
- Self-match prevention via open order cache inspection
- Inventory hard limits (maximum_long_position / maximum_short_position)
- Ack timeout with STATUS_UNCERTAIN guard

Important notes:
- All IOC orders are submitted at the current best bid or ask price.
- Trial records are stored in self._trial_records for offline analysis.
- The strategy emits ARMED → ACTIVE → STOPPING → COMPLETED lifecycle phases.
- start_time / end_time must be timezone-aware ISO 8601 strings.
- _parse_utc() validates timezone awareness and converts to UTC datetime.
"""

import math
import time
from dataclasses import dataclass, field, replace, fields as dc_fields
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_utc(value: str, field_name: str) -> datetime:
    """Parse an ISO 8601 string and return a UTC datetime.

    Raises ValueError if the string has no timezone offset (naive).
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name}: cannot parse '{value}' as ISO 8601: {exc}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"{field_name}: datetime must be timezone-aware (got '{value}'). "
            "Include a UTC offset, e.g. '2026-01-01T09:30:00-05:00' or '...Z'."
        )
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BenchmarkPhase(str, Enum):
    ARMED     = "ARMED"
    ACTIVE    = "ACTIVE"
    STOPPING  = "STOPPING"
    COMPLETED = "COMPLETED"


class TrialResult(str, Enum):
    FULL_FILL    = "FULL_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    NO_FILL      = "NO_FILL"
    REJECTED     = "REJECTED"
    UNCERTAIN    = "UNCERTAIN"


class OrderSlotStatus(str, Enum):
    IDLE            = "IDLE"
    PENDING         = "PENDING"
    STATUS_UNCERTAIN = "STATUS_UNCERTAIN"


# ---------------------------------------------------------------------------
# Trial record
# ---------------------------------------------------------------------------

@dataclass
class TrialRecord:
    trial_index:                   int
    side:                          str
    reference_price:               float
    submitted_quantity:            int
    filled_quantity:               int
    result:                        str       # TrialResult.value
    quote_receive_timestamp_ns:    int
    order_submit_call_timestamp_ns: int
    terminal_timestamp_ns:         Optional[int]
    displayed_price_capture:       float     # filled / submitted ∈ [0, 1]


# ---------------------------------------------------------------------------
# Config (layer 1 — platform metadata)
# ---------------------------------------------------------------------------

class Config(LumitecStrategyConfig):
    strategy_name: str             = "DisplayedLiquidityCaptureBenchmark"
    file_name: str                 = "displayed_liquidity_capture_benchmark.py"

    # mandatory risk fields
    max_position: int              = 50
    max_loss: float                = 999_999.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 1.0

    # strategy-specific config
    instrument_id: str             = "AAPL.NASDAQ"
    order_quantity: int            = 10
    minimum_displayed_size: int    = 500
    maximum_size_participation: float = 0.10
    maximum_spread_ticks: int      = 1
    maximum_quote_age_ms: float    = 100.0
    cooldown_ms: float             = 0.0
    maximum_trials: int            = 100
    maximum_long_position: int     = 50
    maximum_short_position: int    = 50
    ack_timeout_ms: float          = 5_000.0
    start_time: str                = ""
    end_time: str                  = ""
    initial_side: str              = "BUY"
    tick_throttle_interval: float  = 0.0  # quotes drive orders; no coarse throttle


# ---------------------------------------------------------------------------
# ConfigParams (layer 2 — runtime hot-updatable params)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigParams:
    # mandatory risk fields
    max_position: int              = 50
    max_loss: float                = 999_999.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 1.0

    # strategy-specific params
    instrument_id: str             = "AAPL.NASDAQ"
    order_quantity: int            = 10
    minimum_displayed_size: int    = 500
    maximum_size_participation: float = 0.10
    maximum_spread_ticks: int      = 1
    maximum_quote_age_ms: float    = 100.0
    cooldown_ms: float             = 0.0
    maximum_trials: int            = 100
    maximum_long_position: int     = 50
    maximum_short_position: int    = 50
    ack_timeout_ms: float          = 5_000.0
    start_time: str                = ""
    end_time: str                  = ""
    initial_side: str              = "BUY"
    tick_throttle_interval: float  = 0.0

    def validate(self) -> None:
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")
        if self.order_quantity <= 0:
            raise ValueError("order_quantity must be > 0")
        if self.minimum_displayed_size <= 0:
            raise ValueError("minimum_displayed_size must be > 0")
        if not (0.0 < self.maximum_size_participation <= 1.0):
            raise ValueError("maximum_size_participation must be in (0, 1]")
        if self.maximum_spread_ticks <= 0:
            raise ValueError("maximum_spread_ticks must be > 0")
        if self.maximum_quote_age_ms < 0:
            raise ValueError("maximum_quote_age_ms must be >= 0")
        if self.cooldown_ms < 0:
            raise ValueError("cooldown_ms must be >= 0")
        if self.maximum_trials <= 0:
            raise ValueError("maximum_trials must be > 0")
        if self.maximum_long_position < 0:
            raise ValueError("maximum_long_position must be >= 0")
        if self.maximum_short_position < 0:
            raise ValueError("maximum_short_position must be >= 0")
        if self.ack_timeout_ms <= 0:
            raise ValueError("ack_timeout_ms must be > 0")
        if self.initial_side not in ("BUY", "SELL"):
            raise ValueError("initial_side must be 'BUY' or 'SELL'")
        # validate time strings if provided
        if self.start_time:
            start_dt = _parse_utc(self.start_time, "start_time")
        if self.end_time:
            end_dt = _parse_utc(self.end_time, "end_time")
        if self.start_time and self.end_time:
            start_dt = _parse_utc(self.start_time, "start_time")
            end_dt   = _parse_utc(self.end_time,   "end_time")
            if start_dt >= end_dt:
                raise ValueError(
                    f"start_time ({self.start_time}) must be before end_time ({self.end_time})"
                )

    @classmethod
    def from_config(cls, cfg) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: dict) -> "ConfigParams":
        allowed = {f.name: f for f in dc_fields(self)}
        coerced: dict = {}
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


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class DisplayedLiquidityCaptureBenchmark(LumitecBaseStrategy):
    mission    = StrategyMission.EXECUTION
    objective  = StrategyObjective.SIGNAL_DRIVEN
    leg_mode   = LegMode.FINITE
    leg_schema = [{"label": "Leg A", "side": None, "fixed_side": False}]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)

        # session window (converted to UTC at construction so validate fires early)
        self._start_dt: datetime = _parse_utc(self.params.start_time, "start_time")
        self._end_dt:   datetime = _parse_utc(self.params.end_time,   "end_time")
        self._start_time_ns: int = int(self._start_dt.timestamp() * 1e9)
        self._end_time_ns:   int = int(self._end_dt.timestamp()   * 1e9)

        # lifecycle
        self._phase: BenchmarkPhase = BenchmarkPhase.ARMED
        self._actual_activation_ts: Optional[datetime] = None
        self._stopping_after_terminal: bool = False

        # quote state
        self._best_bid_price: float = 0.0
        self._best_ask_price: float = 0.0
        self._best_bid_size:  int   = 0
        self._best_ask_size:  int   = 0
        self._last_quote_ts_ns: int = 0  # exchange timestamp of last quote
        self._first_post_activation_quote_seen: bool = False

        # order slot
        self._slot_status: OrderSlotStatus = OrderSlotStatus.IDLE
        self._active_client_order_id: Optional[str] = None
        self._ack_received: bool = False

        # current trial
        self._current_trial_record: Optional[TrialRecord] = None
        self._submitted_quantity: int = 0
        self._filled_quantity: int = 0

        # counters
        self._completed_trials: int = 0
        self._full_fill_count:  int = 0
        self._partial_fill_count: int = 0
        self._no_fill_count:    int = 0
        self._reject_count:     int = 0
        self._self_match_block_count: int = 0
        self._inventory_block_count:  int = 0

        # trial history
        self._trial_records: List[TrialRecord] = []

        # cooldown
        self._cooldown_until_ns: int = 0

        # inventory
        self._position: int = 0

        # instrument info (populated in on_start)
        self._instrument_id: Optional[InstrumentId] = None  # resolved in on_start
        self._tick_size: float = 0.01
        self._lot_size:  int   = 1
        self._price_precision: int = 2

        # side alternation
        self._next_side: str = self.params.initial_side

        # tick throttle (required by scaffold — quotes drive orders directly)
        self._last_tick_ts: float = 0.0

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()

        # Resolve the instrument ID via the base strategy's leg, which already
        # converts a bare symbol (e.g. "SPY") into a fully-qualified
        # "SYMBOL.VENUE" InstrumentId (e.g. "SPY.XNAS").
        # This mirrors how all other Lumitec strategies obtain their instrument.
        leg_a = self.legs[0]
        raw_id = leg_a.get("symbol", self.params.instrument_id)
        if "." in raw_id:
            self._instrument_id = InstrumentId.from_str(raw_id)
        else:
            # Base strategy did not resolve the venue — use the cache lookup
            # to find the first instrument whose symbol matches.
            matched = [
                inst.id
                for inst in (self.cache.instruments() or [])
                if inst.id.symbol.value == raw_id
            ]
            if not matched:
                raise ValueError(
                    f"instrument_id '{raw_id}': no matching instrument found in cache. "
                    "Ensure the instrument is available in the NautilusTrader cache."
                )
            self._instrument_id = matched[0]

        # resolve instrument info from cache
        instrument = self.cache.instrument(self._instrument_id)
        if instrument is not None:
            self._tick_size      = float(instrument.price_increment)
            self._lot_size       = int(instrument.size_increment)
            self._price_precision = int(instrument.price_precision)

        # subscribe quotes
        self.subscribe_market_data(
            self._instrument_id,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        now_ns = int(time.time() * 1e9)
        if now_ns >= self._start_time_ns:
            # already past start — activate immediately
            self._activate()
        else:
            # schedule activation timer
            delay_s = (self._start_time_ns - now_ns) / 1e9
            try:
                self.clock.set_time_alert(
                    name="activation",
                    alert_time=self._start_dt,
                    callback=self._on_activation_timer,
                )
            except Exception:
                # fallback: set_timer with delay in nanoseconds
                self.clock.set_timer(
                    name="activation",
                    interval_ns=int(delay_s * 1e9),
                    callback=self._on_activation_timer,
                )
            self.observe("Strategy armed — waiting for start_time", context={
                "start_time": self.params.start_time,
            })

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        if self._slot_status != OrderSlotStatus.IDLE:
            # an order is still in flight — defer completion
            self._stopping_after_terminal = True
            self.observe("on_stop called with active order — deferring completion")
        else:
            self._phase = BenchmarkPhase.COMPLETED
            self.observe("Strategy completed", context={
                "trials": self._completed_trials,
                "full_fills": self._full_fill_count,
                "partial_fills": self._partial_fill_count,
                "no_fills": self._no_fill_count,
                "rejects": self._reject_count,
            })

        self.unsubscribe_market_data(
            self._instrument_id,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Parameter management
    # ------------------------------------------------------------------

    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ------------------------------------------------------------------
    # Leg validation
    # ------------------------------------------------------------------

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(
                f"DisplayedLiquidityCaptureBenchmark requires exactly 1 leg; got {len(legs)}"
            )

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    def _activate(self) -> None:
        self._phase = BenchmarkPhase.ACTIVE
        self._actual_activation_ts = datetime.now(timezone.utc)
        self._first_post_activation_quote_seen = False
        self.observe("Strategy activated", context={
            "activation_ts": self._actual_activation_ts.isoformat(),
        })

    def _on_activation_timer(self, *args, **kwargs) -> None:
        """Called by the platform clock at start_time."""
        self._activate()

    # ------------------------------------------------------------------
    # Market data — quote ticks
    # ------------------------------------------------------------------

    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if self.isPaused():
            return
        if self._instrument_id is None or symbol != str(self._instrument_id):
            return

        # always record latest quote data
        self._best_bid_price = float(tick.bid_price)
        self._best_ask_price = float(tick.ask_price)
        self._best_bid_size  = int(tick.bid_size)
        self._best_ask_size  = int(tick.ask_size)
        self._last_quote_ts_ns = int(tick.ts_event)

        if self._phase == BenchmarkPhase.ARMED:
            # store data but do not trade
            return

        if self._phase != BenchmarkPhase.ACTIVE:
            return

        # require at least one genuinely new quote after activation
        if not self._first_post_activation_quote_seen:
            # only mark seen if the quote exchange timestamp post-dates start_time
            if self._last_quote_ts_ns < self._start_time_ns:
                return
            self._first_post_activation_quote_seen = True

        self._evaluate_opportunity()

    # ------------------------------------------------------------------
    # Core decision logic
    # ------------------------------------------------------------------

    def _evaluate_opportunity(self) -> None:
        """Evaluate whether to submit an IOC trial order on this quote."""
        now_ns = int(time.time() * 1e9)

        # end-time guard
        if now_ns >= self._end_time_ns:
            self._begin_stopping("End time reached")
            return

        # max trials guard
        if self._completed_trials >= self.params.maximum_trials:
            self._begin_stopping("Maximum trials reached")
            return

        # slot guard — only one order in flight at a time
        if self._slot_status != OrderSlotStatus.IDLE:
            return

        # cooldown guard
        if now_ns < self._cooldown_until_ns:
            return

        # choose side (may flip based on inventory)
        side = self._choose_side()
        if side is None:
            self._inventory_block_count += 1
            self.observe("Inventory blocked — both sides at limit")
            return

        # select reference price and displayed size for chosen side
        if side == "BUY":
            ref_price    = self._best_ask_price
            displayed_sz = self._best_ask_size
        else:
            ref_price    = self._best_bid_price
            displayed_sz = self._best_bid_size

        # --- filters ---

        # 1. crossed-market guard
        if self._best_bid_price >= self._best_ask_price:
            self.observe("Crossed market — skipping")
            return

        # 2. spread filter
        spread_ticks = round(
            (self._best_ask_price - self._best_bid_price) / self._tick_size
        )
        if spread_ticks > self.params.maximum_spread_ticks:
            self.observe("Spread too wide — skipping", context={
                "spread_ticks": spread_ticks,
                "max": self.params.maximum_spread_ticks,
            })
            return

        # 3. displayed-size filter
        if displayed_sz < self.params.minimum_displayed_size:
            self.observe("Insufficient displayed size — skipping", context={
                "displayed_sz": displayed_sz,
                "min": self.params.minimum_displayed_size,
            })
            return

        # 4. stale-quote filter
        age_ns = int(time.time() * 1e9) - self._last_quote_ts_ns
        age_ms = age_ns / 1_000_000
        if age_ms > self.params.maximum_quote_age_ms:
            self.observe("Quote too stale — skipping", context={"age_ms": age_ms})
            return

        # 5. self-match prevention
        open_orders = self.cache.orders_open(instrument_id=self._instrument_id)
        opposite = "SELL" if side == "BUY" else "BUY"
        for o in open_orders:
            if str(o.side) == opposite:
                self._self_match_block_count += 1
                self.observe("Self-match risk — skipping", context={"side": side})
                return

        # compute order quantity
        participation_qty = math.floor(displayed_sz * self.params.maximum_size_participation)
        qty = min(self.params.order_quantity, participation_qty)
        # normalise to lot size
        if self._lot_size > 1:
            qty = (qty // self._lot_size) * self._lot_size
        if qty <= 0:
            self.observe("Order quantity reduced to zero — skipping")
            return

        # submit
        self._submit_trial(side=side, price=ref_price, qty=qty)

    def _choose_side(self) -> Optional[str]:
        """Return the side to trade, respecting inventory limits.

        Returns None if both sides are exhausted.
        """
        preferred = self._next_side

        # check if preferred side is blocked by inventory
        if preferred == "BUY" and self._position >= self.params.maximum_long_position:
            preferred = "SELL"
        elif preferred == "SELL" and self._position <= -self.params.maximum_short_position:
            preferred = "BUY"

        # re-check after flip
        if preferred == "BUY" and self._position >= self.params.maximum_long_position:
            return None  # both sides blocked
        if preferred == "SELL" and self._position <= -self.params.maximum_short_position:
            return None

        return preferred

    def _submit_trial(self, side: str, price: float, qty: int) -> None:
        quote_ts_ns = self._last_quote_ts_ns
        submit_ts_ns = int(time.time() * 1e9)

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        fmt = f"{{:.{self._price_precision}f}}"
        price_obj = Price.from_str(fmt.format(price))

        self.decide("Submitting IOC trial order", context={
            "side": side,
            "price": price,
            "qty": qty,
            "trial": self._completed_trials + 1,
        })

        order = self.submit_limit_order(
            symbol=self._instrument_id,
            side=order_side,
            qty=qty,
            price=price_obj,
            tif=TimeInForce.IOC,
            leg_id="A",
            role="OPEN",
        )
        order_id = order.client_order_id.value

        self._slot_status = OrderSlotStatus.PENDING
        self._active_client_order_id = order_id
        self._ack_received = False
        self._submitted_quantity = qty
        self._filled_quantity = 0

        # record trial metadata
        self._current_trial_record = TrialRecord(
            trial_index=self._completed_trials,
            side=side,
            reference_price=price,
            submitted_quantity=qty,
            filled_quantity=0,
            result=TrialResult.NO_FILL.value,
            quote_receive_timestamp_ns=quote_ts_ns,
            order_submit_call_timestamp_ns=submit_ts_ns,
            terminal_timestamp_ns=None,
            displayed_price_capture=0.0,
        )

        # schedule ack timeout
        try:
            self.clock.set_timer(
                name=f"ack_timeout_{order_id}",
                interval_ns=int(self.params.ack_timeout_ms * 1_000_000),
                callback=self._on_ack_timeout,
            )
        except Exception:
            pass  # ack timeout is best-effort

        self.act("IOC trial submitted", context={
            "order_id": order_id,
            "side": side,
            "price": price,
            "qty": qty,
        })

    # ------------------------------------------------------------------
    # Ack timeout
    # ------------------------------------------------------------------

    def _on_ack_timeout(self, *args, **kwargs) -> None:
        if self._slot_status == OrderSlotStatus.PENDING:
            self._slot_status = OrderSlotStatus.STATUS_UNCERTAIN
            self.observe("Ack timeout — order status uncertain", context={
                "order_id": self._active_client_order_id,
            })

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        if oid != self._active_client_order_id:
            return
        if self._slot_status == OrderSlotStatus.IDLE:
            # duplicate fill — ignore
            return

        # Call extract_leg_info_from_order_id() here
        self.extract_leg_info_from_order_id(oid)

        # treat fill as implicit ack
        self._ack_received = True

        fill_qty = int(event.last_qty)
        self._filled_quantity = min(
            self._filled_quantity + fill_qty,
            self._submitted_quantity,
        )

        # update inventory
        if self._current_trial_record and self._current_trial_record.side == "BUY":
            self._position += fill_qty
        else:
            self._position -= fill_qty

        # check for full fill
        if self._filled_quantity >= self._submitted_quantity:
            self._close_trial(TrialResult.FULL_FILL)

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        if oid != self._active_client_order_id:
            return
        if self._slot_status == OrderSlotStatus.IDLE:
            # already completed (e.g. full fill then cancel race) — ignore
            return

        if self._filled_quantity > 0:
            self._close_trial(TrialResult.PARTIAL_FILL)
        else:
            self._close_trial(TrialResult.NO_FILL)

    def on_order_expired(self, event) -> None:
        """IOC expiry is equivalent to cancel for our purposes."""
        self.on_order_canceled(event)

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        if oid != self._active_client_order_id:
            self.observe(f"Order rejected (unknown id): {oid}")
            return
        self._close_trial(TrialResult.REJECTED)
        self._reject_count += 1  # _close_trial does not increment reject_count
        # Note: _close_trial decrements it; fix: reject_count incremented here,
        # _close_trial will not double-count because we pass REJECTED explicitly.

    # ------------------------------------------------------------------
    # Trial completion
    # ------------------------------------------------------------------

    def _close_trial(self, result: TrialResult) -> None:
        terminal_ns = int(time.time() * 1e9)
        capture = (
            self._filled_quantity / self._submitted_quantity
            if self._submitted_quantity > 0 else 0.0
        )

        if self._current_trial_record is not None:
            rec = TrialRecord(
                trial_index=self._current_trial_record.trial_index,
                side=self._current_trial_record.side,
                reference_price=self._current_trial_record.reference_price,
                submitted_quantity=self._submitted_quantity,
                filled_quantity=self._filled_quantity,
                result=result.value,
                quote_receive_timestamp_ns=self._current_trial_record.quote_receive_timestamp_ns,
                order_submit_call_timestamp_ns=self._current_trial_record.order_submit_call_timestamp_ns,
                terminal_timestamp_ns=terminal_ns,
                displayed_price_capture=capture,
            )
            self._trial_records.append(rec)
        self._current_trial_record = None

        # update counters
        self._completed_trials += 1
        if result == TrialResult.FULL_FILL:
            self._full_fill_count += 1
        elif result == TrialResult.PARTIAL_FILL:
            self._partial_fill_count += 1
        elif result == TrialResult.NO_FILL:
            self._no_fill_count += 1
        # REJECTED is tracked by on_order_rejected separately

        # alternate side for next trial
        self._next_side = "SELL" if self._next_side == "BUY" else "BUY"

        # apply cooldown
        if self.params.cooldown_ms > 0:
            self._cooldown_until_ns = terminal_ns + int(self.params.cooldown_ms * 1_000_000)

        # reset slot
        self._slot_status = OrderSlotStatus.IDLE
        self._active_client_order_id = None
        self._ack_received = False

        self.act(f"Trial {result.value}", context={
            "trial": self._completed_trials,
            "filled": self._filled_quantity,
            "submitted": self._submitted_quantity,
            "capture": round(capture, 4),
        })

        # check if we should stop after this terminal event
        if self._stopping_after_terminal:
            self._phase = BenchmarkPhase.COMPLETED
            self.observe("Deferred completion after terminal event")
            return

        # check trial limit
        if self._completed_trials >= self.params.maximum_trials:
            self._begin_stopping("Maximum trials reached")

    # ------------------------------------------------------------------
    # Stopping
    # ------------------------------------------------------------------

    def _begin_stopping(self, reason: str) -> None:
        if self._phase in (BenchmarkPhase.STOPPING, BenchmarkPhase.COMPLETED):
            return
        self._phase = BenchmarkPhase.STOPPING
        self.observe(f"Stopping: {reason}")
        if self._slot_status == OrderSlotStatus.IDLE:
            self._phase = BenchmarkPhase.COMPLETED
            self.stop()
        else:
            self._stopping_after_terminal = True