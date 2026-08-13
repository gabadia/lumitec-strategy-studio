"""
Strategy created using Lumitec's Strategy Studio version X.

Logic:
MarkTwoLegLatencyTest is a finite two-leg execution strategy that simultaneously
submits IOC limit orders on two instruments (Leg A: BUY 100 shares/unit, Leg B: SELL
200 shares/unit) whenever the per-unit entry edge (B_bid * 200 - A_ask * 100) exceeds
a configurable hurdle. The strategy tracks decision latency, leg-send skew, ack
latency, and batch completion latency, emitting a full percentile summary on stop.
Orders are submitted in discrete batches; each batch is tracked independently through
reservation, submission, acknowledgement, fill, and terminal states.

Key parameters:
- max_pair_units              - total pair units to fill before the strategy completes
- entry_hurdle_dollars        - minimum per-unit edge (dollars) required to submit a batch
- max_units_per_batch         - maximum pair units in a single batch submission
- max_in_flight_units         - maximum pair units simultaneously outstanding
- max_submission_attempts     - cap on total batch submissions (prevents IOC retry loops)
- a_max_buy_slippage          - added to A ask to form IOC limit price for Leg A
- b_max_sell_slippage         - subtracted from B bid to form IOC limit price for Leg B
- max_quote_age_ms            - maximum acceptable age of a quote before it is considered stale
- max_cross_instrument_skew_ms - maximum acceptable time difference between the two quotes
- size_haircut                - fraction of displayed size treated as tradeable
- tick_throttle_interval      - minimum seconds between signal evaluations

Market data:
- Quote ticks (bid/ask) for both Leg A symbol and Leg B symbol

Risk controls:
- max_position: 10000 (total shares across both legs combined)
- max_loss: 5000.0 (not actively enforced intraday — strategy is capture-only)
- max_active_orders_per_side: 1 (single batch model; in-flight cap enforces concurrency)
- max_order_rate_per_second: 10.0
- max_submission_attempts cap prevents runaway IOC retry loops
- Stale quote guard (max_quote_age_ms) prevents trading on stale data
- Cross-instrument skew guard prevents trading on temporally mismatched quotes
- CANCEL_REMAINDERS imbalance policy: cancels by exact order_id to avoid
  disrupting other batches

Important notes:
- IOC orders self-expire; cancelled remainders are expected and handled
- Self-match detection is a local pre-screen only; venue SMP is the binding control
- Latency measurements use time.monotonic() and are emitted as percentiles on stop
- All Nautilus price/qty fields are cast to float before arithmetic
"""

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace, fields as dc_fields
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Set
from uuid import uuid4

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Price


# ---------------------------------------------------------------------------
# Internal state enumerations
# ---------------------------------------------------------------------------

class RunState(Enum):
    INITIALIZING         = "INITIALIZING"
    WAITING_FOR_MARKET_DATA = "WAITING_FOR_MARKET_DATA"
    READY                = "READY"
    ACTIVE               = "ACTIVE"
    IMBALANCED           = "IMBALANCED"
    COMPLETED            = "COMPLETED"
    FAILED               = "FAILED"


class BatchStatus(Enum):
    RESERVED        = "RESERVED"
    SUBMITTING      = "SUBMITTING"
    PENDING_ACK     = "PENDING_ACK"
    WORKING         = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETED       = "COMPLETED"
    CANCELED        = "CANCELED"
    IMBALANCED      = "IMBALANCED"
    FAILED          = "FAILED"


# ---------------------------------------------------------------------------
# Lightweight data containers
# ---------------------------------------------------------------------------

@dataclass
class QuoteSnapshot:
    symbol: str = ""
    ask_price: float = 0.0
    ask_size: int = 0
    bid_price: float = 0.0
    bid_size: int = 0
    # Local monotonic callback receipt time. Retained for throttling and
    # elapsed-time diagnostics within this process; never compare it across hosts.
    receive_ts: float = 0.0
    # Nautilus timestamps are Unix UTC nanoseconds. The adapter sets ts_event
    # from Polygon's SIP timestamp and ts_init when it processes the ZMQ frame.
    ts_event_ns: int = 0
    ts_init_ns: int = 0
    callback_utc_ns: int = 0
    delivery_age_ns: int = 0
    valid: bool = False


@dataclass
class ExecutionBatch:
    batch_id: int = 0
    units_requested: int = 0
    a_requested_qty: int = 0
    b_requested_qty: int = 0

    signal_timestamp: float = 0.0
    decision_start_timestamp: float = 0.0
    decision_end_timestamp: float = 0.0

    a_client_order_id: str = ""
    b_client_order_id: str = ""

    a_order_status: str = "NONE"
    b_order_status: str = "NONE"

    a_submit_timestamp: float = 0.0
    b_submit_timestamp: float = 0.0
    a_ack_timestamp: float = 0.0
    b_ack_timestamp: float = 0.0
    a_first_fill_timestamp: float = 0.0
    b_first_fill_timestamp: float = 0.0
    a_final_fill_timestamp: float = 0.0
    b_final_fill_timestamp: float = 0.0

    a_acknowledged_qty: int = 0
    b_acknowledged_qty: int = 0
    a_filled_qty: int = 0
    b_filled_qty: int = 0
    a_canceled_qty: int = 0
    b_canceled_qty: int = 0
    a_rejected_qty: int = 0
    b_rejected_qty: int = 0

    batch_status: BatchStatus = BatchStatus.RESERVED
    failure_reason: str = ""
    in_flight_released: bool = False
    in_flight_batch_released: bool = False

    processed_fill_ids: Set[str] = None  # populated in __post_init__

    def __post_init__(self):
        if self.processed_fill_ids is None:
            self.processed_fill_ids = set()


# ---------------------------------------------------------------------------
# Config layer 1 — platform metadata + all configurable fields
# ---------------------------------------------------------------------------

class Config(LumitecStrategyConfig):
    strategy_name: str = "MarkTwoLegLatencyTest"
    file_name: str = "mark_two_leg_latency_test.py"

    # mandatory risk fields
    max_position: int = 300
    max_loss: float = 100.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 2.0

    # strategy-specific fields
    max_pair_units: int = 1
    entry_hurdle_dollars: float = 1_000_000.0
    max_units_per_batch: int = 1
    max_in_flight_units: int = 1
    max_in_flight_batches: int = 1
    max_submission_attempts: int = 1
    a_max_buy_slippage: float = 0.01
    b_max_sell_slippage: float = 0.01
    max_quote_age_ms: float = 1000.0
    max_cross_instrument_skew_ms: float = 250.0
    size_haircut: float = 1.0
    tick_throttle_interval: float = 0.01


# ---------------------------------------------------------------------------
# Config layer 2 — frozen runtime params
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigParams:
    # mandatory risk fields
    max_position: int = 300
    max_loss: float = 100.0
    max_active_orders_per_side: int = 1
    max_order_rate_per_second: float = 2.0

    # strategy-specific params
    max_pair_units: int = 1
    entry_hurdle_dollars: float = 1_000_000.0
    max_units_per_batch: int = 1
    max_in_flight_units: int = 1
    max_in_flight_batches: int = 1
    max_submission_attempts: int = 1
    a_max_buy_slippage: float = 0.01
    b_max_sell_slippage: float = 0.01
    max_quote_age_ms: float = 1000.0
    max_cross_instrument_skew_ms: float = 250.0
    size_haircut: float = 1.0
    tick_throttle_interval: float = 0.01

    def validate(self) -> None:
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")
        if self.max_pair_units <= 0:
            raise ValueError("max_pair_units must be > 0")
        if self.entry_hurdle_dollars < 0:
            raise ValueError("entry_hurdle_dollars must be >= 0")
        if self.max_units_per_batch <= 0:
            raise ValueError("max_units_per_batch must be > 0")
        if self.max_in_flight_units <= 0:
            raise ValueError("max_in_flight_units must be > 0")
        if self.max_in_flight_batches <= 0:
            raise ValueError("max_in_flight_batches must be > 0")
        if self.max_submission_attempts <= 0:
            raise ValueError("max_submission_attempts must be > 0")
        if self.a_max_buy_slippage < 0:
            raise ValueError("a_max_buy_slippage must be >= 0")
        if self.b_max_sell_slippage < 0:
            raise ValueError("b_max_sell_slippage must be >= 0")
        if self.max_quote_age_ms <= 0:
            raise ValueError("max_quote_age_ms must be > 0")
        if self.max_cross_instrument_skew_ms <= 0:
            raise ValueError("max_cross_instrument_skew_ms must be > 0")
        if not (0.0 < self.size_haircut <= 1.0):
            raise ValueError("size_haircut must be in (0, 1]")
        if self.tick_throttle_interval < 0:
            raise ValueError("tick_throttle_interval must be >= 0")

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


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class MarkTwoLegLatencyTest(LumitecBaseStrategy):
    mission    = StrategyMission.EXECUTION
    objective  = StrategyObjective.TARGET_QTY
    leg_mode   = LegMode.FINITE

    leg_schema = [
        {"label": "Leg A (Buy)",  "side": "BUY",  "fixed_side": True},
        {"label": "Leg B (Sell)", "side": "SELL", "fixed_side": True},
    ]

    # Shares-per-unit constants — fixed by specification
    A_SHARES_PER_UNIT: int = 100
    B_SHARES_PER_UNIT: int = 200

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)

        # run-state machine
        self._run_state: RunState = RunState.INITIALIZING
        self._decision_in_progress: bool = False

        # quote snapshots (one per leg)
        self._quote_a: QuoteSnapshot = QuoteSnapshot()
        self._quote_b: QuoteSnapshot = QuoteSnapshot()

        # symbols (populated in on_start)
        self._symbol_a: str = ""
        self._symbol_b: str = ""

        # leg-level quantity accumulators
        self._a_target_qty: int = 0
        self._b_target_qty: int = 0

        self._a_pending_submit_qty: int = 0
        self._b_pending_submit_qty: int = 0

        self._a_working_qty: int = 0
        self._b_working_qty: int = 0

        self._a_filled_qty: int = 0
        self._b_filled_qty: int = 0

        self._a_canceled_qty: int = 0
        self._b_canceled_qty: int = 0

        self._a_rejected_qty: int = 0
        self._b_rejected_qty: int = 0

        # in-flight tracking
        self._current_in_flight_units: int = 0
        self._live_batch_count: int = 0

        # submission attempt counter (guards IOC no-fill retry loops)
        self._submission_attempts: int = 0

        # batch registry: order_id -> ExecutionBatch, batch_id -> ExecutionBatch
        self._order_to_batch: Dict[str, ExecutionBatch] = {}
        self._batches: Dict[int, ExecutionBatch] = {}
        self._next_batch_id: int = 1

        # per-instrument active order sets for local self-match pre-check
        self._active_buy_orders_a: Dict[str, float] = {}   # order_id -> limit_price
        self._active_sell_orders_b: Dict[str, float] = {}  # order_id -> limit_price

        # tick throttle
        self._last_tick_ts: float = 0.0

        # latency accumulators (seconds)
        self._decision_latencies: list = []
        self._leg_send_skews: list = []
        self._a_ack_latencies: list = []
        self._b_ack_latencies: list = []
        self._batch_completion_latencies: list = []

        # signal evaluation counters for summary
        self._signals_evaluated: int = 0
        self._signals_qualifying: int = 0

        # High-frequency diagnostics are buffered and written periodically
        # only after signal evaluation returns. No auxiliary thread is used.
        self._event_log_buffer: list = []
        self._event_log_path: Optional[Path] = None
        self._event_log_failure_reported: bool = False
        self._event_log_dropped_records: int = 0
        self._last_event_log_flush_ts: float = 0.0
        self._event_log_flush_interval_seconds: float = 2.0
        self._run_id: str = uuid4().hex

    # ------------------------------------------------------------------
    # Mandatory platform hooks
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        self._leg_a, self._leg_b = self.legs
        self._symbol_a = self._leg_a["symbol"]
        self._symbol_b = self._leg_b["symbol"]

        # derive target quantities
        self._a_target_qty = self.params.max_pair_units * self.A_SHARES_PER_UNIT
        self._b_target_qty = self.params.max_pair_units * self.B_SHARES_PER_UNIT

        self.subscribe_market_data(
            self._symbol_a,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self.subscribe_market_data(
            self._symbol_b,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        self._run_state = RunState.WAITING_FOR_MARKET_DATA
        self._initialize_event_log()
        self._record_event(
            "RUN_START",
            symbol_a=self._symbol_a,
            symbol_b=self._symbol_b,
            max_pair_units=self.params.max_pair_units,
            hurdle=self.params.entry_hurdle_dollars,
        )
        self._flush_event_log()
        self.observe(
            "MarkTwoLegLatencyTest started",
            context={
                "symbol_a": self._symbol_a,
                "symbol_b": self._symbol_b,
                "a_target_qty": self._a_target_qty,
                "b_target_qty": self._b_target_qty,
                "max_pair_units": self.params.max_pair_units,
                "hurdle": self.params.entry_hurdle_dollars,
                "max_submission_attempts": self.params.max_submission_attempts,
                "event_log_path": (
                    str(self._event_log_path) if self._event_log_path else None
                ),
            },
        )

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        self.cancelAllOrders()
        self.unsubscribe_market_data(
            self._symbol_a,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self.unsubscribe_market_data(
            self._symbol_b,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self._record_event(
            "RUN_STOP",
            run_state=self._run_state.value,
            signals_evaluated=self._signals_evaluated,
            signals_qualifying=self._signals_qualifying,
            batches_total=len(self._batches),
            a_filled_qty=self._a_filled_qty,
            b_filled_qty=self._b_filled_qty,
            a_canceled_qty=self._a_canceled_qty,
            b_canceled_qty=self._b_canceled_qty,
            a_rejected_qty=self._a_rejected_qty,
            b_rejected_qty=self._b_rejected_qty,
            dropped_log_records=self._event_log_dropped_records,
        )
        self._flush_event_log()
        self._emit_summary()
        self.observe("Strategy stopped")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        self._record_event("ORDER_REJECTED", client_order_id=oid)
        self.observe(f"Order rejected: {oid}")
        self._handle_rejection(oid)

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        self._record_event("ORDER_CANCELED", client_order_id=oid)
        self.observe(f"Order canceled: {oid}")
        self._handle_cancellation(oid)

    # ------------------------------------------------------------------
    # Hot-update hooks
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
        if len(legs) != 2:
            raise ValueError(
                f"MarkTwoLegLatencyTest requires exactly 2 legs, got {len(legs)}"
            )
        if legs[0].get("side") != "BUY":
            raise ValueError("MarkTwoLegLatencyTest Leg A must be BUY")
        if legs[1].get("side") != "SELL":
            raise ValueError("MarkTwoLegLatencyTest Leg B must be SELL")

    # ------------------------------------------------------------------
    # Pause / resume hooks
    # ------------------------------------------------------------------
    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self._decision_in_progress = False
        self.act("Strategy paused", context={"reason": reason})

    def on_resume(self) -> None:
        if self._run_state not in {RunState.COMPLETED, RunState.FAILED}:
            self._run_state = RunState.READY
        self.act("Strategy resumed")

    # ------------------------------------------------------------------
    # Acknowledgement handler — pending → working transition
    # ------------------------------------------------------------------
    def on_order_accepted(self, event) -> None:
        oid = event.client_order_id.value
        ack_ts = time.monotonic()

        batch = self._order_to_batch.get(oid)
        if batch is None:
            return

        leg_id, _ = self.extract_leg_info_from_order_id(oid)
        if leg_id is None:
            self.observe(f"Unknown leg_id for ack on order {oid} — ignoring")
            return

        if leg_id == "A":
            # Guard: reject duplicate or terminal acks
            if batch.a_order_status in ("WORKING", "FILLED", "CANCELED", "REJECTED"):
                self.observe(
                    "Duplicate or late ack for A — ignored",
                    context={"order_id": oid, "status": batch.a_order_status},
                )
                return

            already_resolved = (
                batch.a_filled_qty
                + batch.a_canceled_qty
                + batch.a_rejected_qty
                + batch.a_acknowledged_qty
            )
            qty_to_move = max(0, batch.a_requested_qty - already_resolved)
            qty_to_move = min(qty_to_move, self._a_pending_submit_qty)

            if qty_to_move > 0:
                self._a_pending_submit_qty = max(
                    0, self._a_pending_submit_qty - qty_to_move
                )
                self._a_working_qty += qty_to_move
                batch.a_acknowledged_qty += qty_to_move

            batch.a_order_status = "WORKING"
            batch.a_ack_timestamp = ack_ts
            if batch.a_submit_timestamp > 0:
                self._a_ack_latencies.append(ack_ts - batch.a_submit_timestamp)

        elif leg_id == "B":
            # Guard: reject duplicate or terminal acks
            if batch.b_order_status in ("WORKING", "FILLED", "CANCELED", "REJECTED"):
                self.observe(
                    "Duplicate or late ack for B — ignored",
                    context={"order_id": oid, "status": batch.b_order_status},
                )
                return

            already_resolved = (
                batch.b_filled_qty
                + batch.b_canceled_qty
                + batch.b_rejected_qty
                + batch.b_acknowledged_qty
            )
            qty_to_move = max(0, batch.b_requested_qty - already_resolved)
            qty_to_move = min(qty_to_move, self._b_pending_submit_qty)

            if qty_to_move > 0:
                self._b_pending_submit_qty = max(
                    0, self._b_pending_submit_qty - qty_to_move
                )
                self._b_working_qty += qty_to_move
                batch.b_acknowledged_qty += qty_to_move

            batch.b_order_status = "WORKING"
            batch.b_ack_timestamp = ack_ts
            if batch.b_submit_timestamp > 0:
                self._b_ack_latencies.append(ack_ts - batch.b_submit_timestamp)

        # Update batch status if both legs now working
        a_live = batch.a_order_status in ("WORKING", "PARTIALLY_FILLED")
        b_live = batch.b_order_status in ("WORKING", "PARTIALLY_FILLED")
        if a_live and b_live:
            if batch.batch_status not in (
                BatchStatus.PARTIALLY_FILLED,
                BatchStatus.COMPLETED,
                BatchStatus.IMBALANCED,
            ):
                batch.batch_status = BatchStatus.WORKING

        ref_ts = batch.a_submit_timestamp if leg_id == "A" else batch.b_submit_timestamp
        self._record_event(
            "ORDER_ACCEPTED",
            batch_id=batch.batch_id,
            client_order_id=oid,
            leg=leg_id,
            ack_latency_us=(
                round((ack_ts - ref_ts) * 1e6, 1) if ref_ts > 0 else None
            ),
        )
        self.observe(
            "Order accepted",
            context={
                "order_id": oid,
                "leg": leg_id,
                "batch_id": batch.batch_id,
                "ack_latency_us": (
                    round((ack_ts - ref_ts) * 1e6, 1) if ref_ts > 0 else None
                ),
            },
        )

    # ------------------------------------------------------------------
    # Market data handler
    # ------------------------------------------------------------------
    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        """
        Quote state is updated on every tick before the throttle guard.
        This ensures cross-instrument skew is not inflated by discarded ticks.
        The throttle guards only the signal evaluation path.
        """
        if self.isPaused():
            return

        now = time.monotonic()
        callback_utc_ns = time.time_ns()
        ts_event_ns = int(tick.ts_event)
        ts_init_ns = int(tick.ts_init)
        delivery_age_ns = ts_init_ns - ts_event_ns

        # A negative delivery age means the adapter/source timestamps cannot be
        # used safely for this latency run. Do not silently fall back to receipt
        # time, because that would hide a constant geographic delivery delay.
        if ts_event_ns <= 0 or ts_init_ns <= 0 or delivery_age_ns < 0:
            self._record_event(
                "INVALID_QUOTE_TIMESTAMPS",
                symbol=symbol,
                ts_event_ns=ts_event_ns,
                ts_init_ns=ts_init_ns,
                delivery_age_ns=delivery_age_ns,
            )
            return

        # Always update quote state before any throttle check
        if symbol == self._symbol_a:
            self._quote_a = QuoteSnapshot(
                symbol=symbol,
                ask_price=float(tick.ask_price),
                ask_size=int(tick.ask_size),
                bid_price=float(tick.bid_price),
                bid_size=int(tick.bid_size),
                receive_ts=now,
                ts_event_ns=ts_event_ns,
                ts_init_ns=ts_init_ns,
                callback_utc_ns=callback_utc_ns,
                delivery_age_ns=delivery_age_ns,
                valid=True,
            )
        elif symbol == self._symbol_b:
            self._quote_b = QuoteSnapshot(
                symbol=symbol,
                ask_price=float(tick.ask_price),
                ask_size=int(tick.ask_size),
                bid_price=float(tick.bid_price),
                bid_size=int(tick.bid_size),
                receive_ts=now,
                ts_event_ns=ts_event_ns,
                ts_init_ns=ts_init_ns,
                callback_utc_ns=callback_utc_ns,
                delivery_age_ns=delivery_age_ns,
                valid=True,
            )
        else:
            return

        # Transition from waiting once both quotes have arrived
        if self._run_state == RunState.WAITING_FOR_MARKET_DATA:
            if self._quote_a.valid and self._quote_b.valid:
                self._run_state = RunState.READY
                self.observe("Both quotes received — transitioning to READY")

        # Throttle guards signal evaluation only
        if now - self._last_tick_ts < self.params.tick_throttle_interval:
            return
        self._last_tick_ts = now

        if self._run_state not in {RunState.READY, RunState.ACTIVE}:
            return

        if self._decision_in_progress:
            return

        if self._check_end_time_reached():
            self.forced_stop("End time reached", "TIME")
            return

        self._evaluate_signal(signal_ts=now)
        # Periodic disk I/O happens only after the measured decision path returns.
        self._maybe_flush_event_log()

    # ------------------------------------------------------------------
    # Signal evaluation — atomic decision and reservation
    # ------------------------------------------------------------------
    def _evaluate_signal(self, signal_ts: float) -> None:
        """
        Validates quotes, computes entry edge, sizes the batch, checks
        self-match, atomically reserves quantities, and submits both orders.
        """
        self._signals_evaluated += 1
        decision_start = time.monotonic()

        now = time.monotonic()
        now_utc_ns = time.time_ns()

        if not self._quote_a.valid or not self._quote_b.valid:
            return

        # Market age includes the complete SIP-to-adapter path plus any time the
        # snapshot has waited inside this strategy. This reveals constant network
        # delay that a local receipt-only timer cannot see.
        a_market_age_ms = (
            now_utc_ns - self._quote_a.ts_event_ns
        ) / 1_000_000.0
        b_market_age_ms = (
            now_utc_ns - self._quote_b.ts_event_ns
        ) / 1_000_000.0

        a_local_silence_ms = (now - self._quote_a.receive_ts) * 1000.0
        b_local_silence_ms = (now - self._quote_b.receive_ts) * 1000.0

        if a_market_age_ms > self.params.max_quote_age_ms:
            self._record_event(
                "QUOTE_STALE",
                leg="A",
                symbol=self._symbol_a,
                market_age_ms=round(a_market_age_ms, 3),
                delivery_age_ms=round(
                    self._quote_a.delivery_age_ns / 1_000_000.0, 3
                ),
                local_silence_ms=round(a_local_silence_ms, 3),
            )
            return
        if b_market_age_ms > self.params.max_quote_age_ms:
            self._record_event(
                "QUOTE_STALE",
                leg="B",
                symbol=self._symbol_b,
                market_age_ms=round(b_market_age_ms, 3),
                delivery_age_ms=round(
                    self._quote_b.delivery_age_ns / 1_000_000.0, 3
                ),
                local_silence_ms=round(b_local_silence_ms, 3),
            )
            return

        # Apply the coherence guard to source event time. Also retain receipt
        # skew as a separate local diagnostic.
        source_skew_ms = abs(
            self._quote_a.ts_event_ns - self._quote_b.ts_event_ns
        ) / 1_000_000.0
        receipt_skew_ms = abs(
            self._quote_a.receive_ts - self._quote_b.receive_ts
        ) * 1000.0
        if source_skew_ms > self.params.max_cross_instrument_skew_ms:
            self._record_event(
                "CROSS_INSTRUMENT_SKEW",
                source_skew_ms=round(source_skew_ms, 3),
                receipt_skew_ms=round(receipt_skew_ms, 3),
                skew_limit_ms=self.params.max_cross_instrument_skew_ms,
            )
            return

        if self._quote_a.ask_price <= 0.0 or self._quote_a.ask_size <= 0:
            return
        if self._quote_b.bid_price <= 0.0 or self._quote_b.bid_size <= 0:
            return

        a_ask_price = self._quote_a.ask_price
        a_ask_size  = self._quote_a.ask_size
        b_bid_price = self._quote_b.bid_price
        b_bid_size  = self._quote_b.bid_size

        # Hurdle check
        entry_edge = (
            self.B_SHARES_PER_UNIT * b_bid_price
            - self.A_SHARES_PER_UNIT * a_ask_price
        )
        self._record_event(
            "SIGNAL_EVALUATED",
            a_ask=round(a_ask_price, 4),
            a_ask_size=a_ask_size,
            b_bid=round(b_bid_price, 4),
            b_bid_size=b_bid_size,
            entry_edge=round(entry_edge, 4),
            hurdle=self.params.entry_hurdle_dollars,
            a_ts_event_ns=self._quote_a.ts_event_ns,
            b_ts_event_ns=self._quote_b.ts_event_ns,
            a_ts_init_ns=self._quote_a.ts_init_ns,
            b_ts_init_ns=self._quote_b.ts_init_ns,
            a_delivery_age_ms=round(
                self._quote_a.delivery_age_ns / 1_000_000.0, 3
            ),
            b_delivery_age_ms=round(
                self._quote_b.delivery_age_ns / 1_000_000.0, 3
            ),
            a_callback_delay_ms=round(
                (
                    self._quote_a.callback_utc_ns
                    - self._quote_a.ts_init_ns
                ) / 1_000_000.0,
                3,
            ),
            b_callback_delay_ms=round(
                (
                    self._quote_b.callback_utc_ns
                    - self._quote_b.ts_init_ns
                ) / 1_000_000.0,
                3,
            ),
            a_market_age_ms=round(a_market_age_ms, 3),
            b_market_age_ms=round(b_market_age_ms, 3),
            source_skew_ms=round(source_skew_ms, 3),
            receipt_skew_ms=round(receipt_skew_ms, 3),
        )

        if entry_edge < self.params.entry_hurdle_dollars:
            return

        # Submission attempt guard
        if self._submission_attempts >= self.params.max_submission_attempts:
            self._record_event(
                "SUBMISSION_ATTEMPT_LIMIT",
                attempts=self._submission_attempts,
            )
            return

        self._signals_qualifying += 1

        # Available units from displayed size with haircut
        adj_a_size = int(a_ask_size * self.params.size_haircut)
        adj_b_size = int(b_bid_size * self.params.size_haircut)
        a_available_units = adj_a_size // self.A_SHARES_PER_UNIT
        b_available_units = adj_b_size // self.B_SHARES_PER_UNIT
        displayed_units = min(a_available_units, b_available_units)

        # Remaining target capacity
        a_effective = (
            self._a_filled_qty + self._a_pending_submit_qty + self._a_working_qty
        )
        b_effective = (
            self._b_filled_qty + self._b_pending_submit_qty + self._b_working_qty
        )
        a_remaining_cap = (self._a_target_qty - a_effective) // self.A_SHARES_PER_UNIT
        b_remaining_cap = (self._b_target_qty - b_effective) // self.B_SHARES_PER_UNIT
        remaining_pair_capacity = min(a_remaining_cap, b_remaining_cap)

        # In-flight batch cap
        if self._live_batch_count >= self.params.max_in_flight_batches:
            self._record_event(
                "IN_FLIGHT_BATCH_LIMIT",
                live_batch_count=self._live_batch_count,
                max_in_flight_batches=self.params.max_in_flight_batches,
            )
            return

        # In-flight capacity
        in_flight_capacity = (
            self.params.max_in_flight_units - self._current_in_flight_units
        )

        units_to_submit = min(
            displayed_units,
            remaining_pair_capacity,
            self.params.max_units_per_batch,
            in_flight_capacity,
        )

        if units_to_submit <= 0:
            self._record_event(
                "NO_UNITS_TO_SUBMIT",
                a_ask_size=a_ask_size,
                b_bid_size=b_bid_size,
                size_haircut=self.params.size_haircut,
                adj_a_size=adj_a_size,
                adj_b_size=adj_b_size,
                a_available_units=a_available_units,
                b_available_units=b_available_units,
                displayed_units=displayed_units,
                remaining_pair_capacity=remaining_pair_capacity,
                in_flight_capacity=in_flight_capacity,
            )
            return

        proposed_a_qty = self.A_SHARES_PER_UNIT * units_to_submit
        proposed_b_qty = self.B_SHARES_PER_UNIT * units_to_submit

        # Self-match pre-check (venue SMP is the primary safeguard)
        if self._self_match_conflict_a_buy(a_ask_price):
            self.observe(
                "Self-match conflict on A BUY — batch blocked",
                context={"symbol": self._symbol_a},
            )
            return
        if self._self_match_conflict_b_sell(b_bid_price):
            self.observe(
                "Self-match conflict on B SELL — batch blocked",
                context={"symbol": self._symbol_b},
            )
            return

        # --- Atomic reservation — before any order is sent ---
        self._a_pending_submit_qty += proposed_a_qty
        self._b_pending_submit_qty += proposed_b_qty
        self._current_in_flight_units += units_to_submit
        self._live_batch_count += 1
        self._submission_attempts += 1

        # Create batch record
        batch = ExecutionBatch(
            batch_id=self._next_batch_id,
            units_requested=units_to_submit,
            a_requested_qty=proposed_a_qty,
            b_requested_qty=proposed_b_qty,
            signal_timestamp=signal_ts,
            decision_start_timestamp=decision_start,
            batch_status=BatchStatus.RESERVED,
        )
        self._next_batch_id += 1
        self._batches[batch.batch_id] = batch
        self._record_event(
            "BATCH_RESERVED",
            batch_id=batch.batch_id,
            units=units_to_submit,
            a_qty=proposed_a_qty,
            b_qty=proposed_b_qty,
            entry_edge=round(entry_edge, 4),
        )

        decision_end = time.monotonic()
        batch.decision_end_timestamp = decision_end
        self._decision_latencies.append(decision_end - decision_start)

        self.decide(
            "Hurdle met — submitting batch",
            context={
                "batch_id": batch.batch_id,
                "units": units_to_submit,
                "a_qty": proposed_a_qty,
                "b_qty": proposed_b_qty,
                "entry_edge": round(entry_edge, 4),
                "attempt": self._submission_attempts,
            },
        )

        # Construct IOC limit prices
        a_limit_price = a_ask_price + self.params.a_max_buy_slippage
        b_limit_price = b_bid_price - self.params.b_max_sell_slippage
        batch.batch_status = BatchStatus.SUBMITTING

        # Submit Leg A
        t_a_submit = time.monotonic()
        batch.a_submit_timestamp = t_a_submit
        try:
            order_a = self.submit_limit_order(
                symbol=self._symbol_a,
                side=OrderSide.BUY,
                qty=proposed_a_qty,
                price=Price.from_str(f"{a_limit_price:.2f}"),
                tif=TimeInForce.IOC,
                leg_id="A",
                role="OPEN",
            )
            a_oid = order_a.client_order_id.value
            batch.a_client_order_id = a_oid
            batch.a_order_status = "PENDING_ACK"
            self._order_to_batch[a_oid] = batch
            self._active_buy_orders_a[a_oid] = a_limit_price
            self._record_event(
                "ORDER_SUBMITTED",
                batch_id=batch.batch_id,
                client_order_id=a_oid,
                leg="A",
                symbol=self._symbol_a,
                side="BUY",
                qty=proposed_a_qty,
                limit_price=round(a_limit_price, 4),
            )
        except Exception as exc:
            # Nothing sent — release all reservations and abort cleanly
            self._a_pending_submit_qty = max(
                0, self._a_pending_submit_qty - proposed_a_qty
            )
            self._b_pending_submit_qty = max(
                0, self._b_pending_submit_qty - proposed_b_qty
            )
            self._current_in_flight_units = max(
                0, self._current_in_flight_units - units_to_submit
            )
            batch.in_flight_released = True
            self._release_live_batch(batch)
            batch.batch_status = BatchStatus.FAILED
            batch.failure_reason = f"A submit exception: {exc}"
            batch.a_order_status = "REJECTED"
            batch.a_rejected_qty = proposed_a_qty
            self._a_rejected_qty += proposed_a_qty
            batch.b_order_status = "REJECTED"
            batch.b_rejected_qty = proposed_b_qty
            self._b_rejected_qty += proposed_b_qty
            self._record_event(
                "SUBMISSION_FAILED",
                batch_id=batch.batch_id,
                leg="A",
                failure_reason=str(exc),
            )
            self.observe(f"Failed to submit A order — batch aborted: {exc}")
            self._stop_if_attempt_limit_complete()
            return

        # Submit Leg B
        t_b_submit = time.monotonic()
        batch.b_submit_timestamp = t_b_submit
        try:
            order_b = self.submit_limit_order(
                symbol=self._symbol_b,
                side=OrderSide.SELL,
                qty=proposed_b_qty,
                price=Price.from_str(f"{b_limit_price:.2f}"),
                tif=TimeInForce.IOC,
                leg_id="B",
                role="OPEN",
            )
            b_oid = order_b.client_order_id.value
            batch.b_client_order_id = b_oid
            batch.b_order_status = "PENDING_ACK"
            self._order_to_batch[b_oid] = batch
            self._active_sell_orders_b[b_oid] = b_limit_price
            self._record_event(
                "ORDER_SUBMITTED",
                batch_id=batch.batch_id,
                client_order_id=b_oid,
                leg="B",
                symbol=self._symbol_b,
                side="SELL",
                qty=proposed_b_qty,
                limit_price=round(b_limit_price, 4),
            )
        except Exception as exc:
            # A is live — release only B's pending reservation
            self._b_pending_submit_qty = max(
                0, self._b_pending_submit_qty - proposed_b_qty
            )
            batch.b_order_status = "REJECTED"
            batch.b_rejected_qty = proposed_b_qty
            self._b_rejected_qty += proposed_b_qty
            batch.batch_status = BatchStatus.FAILED
            batch.failure_reason = f"B submit exception after A sent: {exc}"
            self._run_state = RunState.IMBALANCED
            self._record_event(
                "SUBMISSION_FAILED",
                batch_id=batch.batch_id,
                leg="B",
                failure_reason=str(exc),
            )
            self.observe(
                f"Failed to submit B after A was sent — imbalanced: {exc}"
            )
            self._apply_imbalance_policy(batch)
            return

        # Record leg-send skew
        leg_send_skew = abs(t_b_submit - t_a_submit)
        self._leg_send_skews.append(leg_send_skew)
        batch.batch_status = BatchStatus.PENDING_ACK
        self._run_state = RunState.ACTIVE

        self.act(
            "Both legs submitted",
            context={
                "batch_id": batch.batch_id,
                "a_order_id": batch.a_client_order_id,
                "b_order_id": batch.b_client_order_id,
                "a_limit_price": round(a_limit_price, 4),
                "b_limit_price": round(b_limit_price, 4),
                "leg_send_skew_us": round(leg_send_skew * 1e6, 1),
            },
        )

    # ------------------------------------------------------------------
    # Order fill handler
    # ------------------------------------------------------------------
    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)

        if leg_id is None:
            self.observe(f"Unknown leg_id for order {oid} — ignoring fill")
            return

        fill_ts = time.monotonic()
        fill_qty = int(event.last_qty)
        fill_price = float(event.last_px)

        batch = self._order_to_batch.get(oid)
        if batch is None:
            self.observe(f"No batch found for order {oid} — ignoring fill")
            return

        # Idempotency: key on (order_id, cumulative_qty)
        cum_qty = int(event.cum_qty) if hasattr(event, "cum_qty") else fill_qty
        fill_key = f"{oid}:{cum_qty}"
        if fill_key in batch.processed_fill_ids:
            self.observe(f"Duplicate fill ignored: {fill_key}")
            return
        batch.processed_fill_ids.add(fill_key)
        self._record_event(
            "ORDER_FILLED",
            batch_id=batch.batch_id,
            client_order_id=oid,
            leg=leg_id,
            fill_qty=fill_qty,
            fill_price=round(fill_price, 4),
        )

        if leg_id == "A":
            # Fill may arrive before ack: drain pending first, then working
            remaining = fill_qty
            from_pending = min(remaining, self._a_pending_submit_qty)
            self._a_pending_submit_qty = max(
                0, self._a_pending_submit_qty - from_pending
            )
            remaining -= from_pending
            from_working = min(remaining, self._a_working_qty)
            self._a_working_qty = max(0, self._a_working_qty - from_working)

            self._a_filled_qty += fill_qty
            batch.a_filled_qty += fill_qty

            if batch.a_first_fill_timestamp == 0.0:
                batch.a_first_fill_timestamp = fill_ts
            if batch.a_filled_qty >= batch.a_requested_qty:
                batch.a_final_fill_timestamp = fill_ts
                batch.a_order_status = "FILLED"
                self._active_buy_orders_a.pop(oid, None)

        elif leg_id == "B":
            remaining = fill_qty
            from_pending = min(remaining, self._b_pending_submit_qty)
            self._b_pending_submit_qty = max(
                0, self._b_pending_submit_qty - from_pending
            )
            remaining -= from_pending
            from_working = min(remaining, self._b_working_qty)
            self._b_working_qty = max(0, self._b_working_qty - from_working)

            self._b_filled_qty += fill_qty
            batch.b_filled_qty += fill_qty

            if batch.b_first_fill_timestamp == 0.0:
                batch.b_first_fill_timestamp = fill_ts
            if batch.b_filled_qty >= batch.b_requested_qty:
                batch.b_final_fill_timestamp = fill_ts
                batch.b_order_status = "FILLED"
                self._active_sell_orders_b.pop(oid, None)

        # Derived metrics
        balanced_units = min(
            self._a_filled_qty // self.A_SHARES_PER_UNIT,
            self._b_filled_qty // self.B_SHARES_PER_UNIT,
        )
        a_unit_eq = self._a_filled_qty / self.A_SHARES_PER_UNIT
        b_unit_eq = self._b_filled_qty / self.B_SHARES_PER_UNIT
        unit_imbalance = a_unit_eq - b_unit_eq

        self.act(
            "Order filled",
            context={
                "order_id": oid,
                "leg": leg_id,
                "fill_qty": fill_qty,
                "fill_price": round(fill_price, 4),
                "a_filled_total": self._a_filled_qty,
                "b_filled_total": self._b_filled_qty,
                "balanced_units": balanced_units,
                "unit_imbalance": round(unit_imbalance, 3),
            },
        )

        # Batch completion check
        a_done = batch.a_filled_qty >= batch.a_requested_qty
        b_done = batch.b_filled_qty >= batch.b_requested_qty

        if a_done and b_done:
            batch.batch_status = BatchStatus.COMPLETED
            completion_latency = (
                max(batch.a_final_fill_timestamp, batch.b_final_fill_timestamp)
                - batch.signal_timestamp
            )
            self._batch_completion_latencies.append(completion_latency)
            self._release_batch_in_flight(batch)
            self._release_live_batch(batch)
            self._record_event(
                "BATCH_RESOLVED",
                batch_id=batch.batch_id,
                batch_status=batch.batch_status.value,
                completion_latency_ms=round(completion_latency * 1000, 3),
            )
            self._flush_event_log()
            self.observe(
                "Batch completed",
                context={
                    "batch_id": batch.batch_id,
                    "completion_latency_ms": round(completion_latency * 1000, 3),
                },
            )
        elif a_done or b_done:
            batch.batch_status = BatchStatus.PARTIALLY_FILLED

        # Overall strategy completion check
        no_active_orders = (
            self._a_working_qty == 0
            and self._b_working_qty == 0
            and self._a_pending_submit_qty == 0
            and self._b_pending_submit_qty == 0
        )
        both_targets_met = (
            self._a_filled_qty >= self._a_target_qty
            and self._b_filled_qty >= self._b_target_qty
        )
        if both_targets_met and no_active_orders:
            self._run_state = RunState.COMPLETED
            self.observe(
                "All targets filled — strategy complete",
                context={
                    "a_filled": self._a_filled_qty,
                    "b_filled": self._b_filled_qty,
                },
            )
            self.stop()
            return

        # Ready for next batch if capacity available
        if not both_targets_met:
            if self._run_state not in {RunState.IMBALANCED, RunState.FAILED}:
                self._run_state = (
                    RunState.READY
                    if self._current_in_flight_units == 0
                    else RunState.ACTIVE
                )

    # ------------------------------------------------------------------
    # Internal order event handlers
    # ------------------------------------------------------------------
    def _handle_rejection(self, oid: str) -> None:
        batch = self._order_to_batch.get(oid)
        if batch is None:
            return

        leg_id, _ = self.extract_leg_info_from_order_id(oid)
        if leg_id is None:
            return

        if leg_id == "A":
            rejected_qty = max(
                0,
                batch.a_requested_qty
                - batch.a_filled_qty
                - batch.a_canceled_qty
                - batch.a_rejected_qty,
            )
            from_pending = min(rejected_qty, self._a_pending_submit_qty)
            self._a_pending_submit_qty = max(
                0, self._a_pending_submit_qty - from_pending
            )
            from_working = min(rejected_qty - from_pending, self._a_working_qty)
            self._a_working_qty = max(0, self._a_working_qty - from_working)
            self._a_rejected_qty += rejected_qty
            batch.a_rejected_qty += rejected_qty
            batch.a_order_status = "REJECTED"
            self._active_buy_orders_a.pop(oid, None)

            b_active = batch.b_order_status in (
                "PENDING_ACK", "WORKING", "PARTIALLY_FILLED", "PENDING"
            )
            if b_active or batch.b_filled_qty > 0:
                self._run_state = RunState.IMBALANCED
                self._apply_imbalance_policy(batch)

            self._resolve_batch_after_terminal_event(batch)

        elif leg_id == "B":
            rejected_qty = max(
                0,
                batch.b_requested_qty
                - batch.b_filled_qty
                - batch.b_canceled_qty
                - batch.b_rejected_qty,
            )
            from_pending = min(rejected_qty, self._b_pending_submit_qty)
            self._b_pending_submit_qty = max(
                0, self._b_pending_submit_qty - from_pending
            )
            from_working = min(rejected_qty - from_pending, self._b_working_qty)
            self._b_working_qty = max(0, self._b_working_qty - from_working)
            self._b_rejected_qty += rejected_qty
            batch.b_rejected_qty += rejected_qty
            batch.b_order_status = "REJECTED"
            self._active_sell_orders_b.pop(oid, None)

            a_active = batch.a_order_status in (
                "PENDING_ACK", "WORKING", "PARTIALLY_FILLED", "PENDING"
            )
            if a_active or batch.a_filled_qty > 0:
                self._run_state = RunState.IMBALANCED
                self._apply_imbalance_policy(batch)

            self._resolve_batch_after_terminal_event(batch)

        self.observe(
            "Order rejected — batch updated",
            context={
                "order_id": oid,
                "batch_id": batch.batch_id,
                "batch_status": batch.batch_status.value,
            },
        )

    def _handle_cancellation(self, oid: str) -> None:
        """
        IOC remainder cancellations clean up pending AND working quantities
        because a cancel can arrive before or instead of the ack.
        """
        batch = self._order_to_batch.get(oid)
        if batch is None:
            return

        leg_id, _ = self.extract_leg_info_from_order_id(oid)
        if leg_id is None:
            return

        if leg_id == "A":
            unfilled = max(
                0,
                batch.a_requested_qty
                - batch.a_filled_qty
                - batch.a_canceled_qty
                - batch.a_rejected_qty,
            )
            if unfilled > 0:
                from_pending = min(unfilled, self._a_pending_submit_qty)
                self._a_pending_submit_qty = max(
                    0, self._a_pending_submit_qty - from_pending
                )
                from_working = min(unfilled - from_pending, self._a_working_qty)
                self._a_working_qty = max(0, self._a_working_qty - from_working)
                self._a_canceled_qty += unfilled
                batch.a_canceled_qty += unfilled
            batch.a_order_status = "CANCELED"
            self._active_buy_orders_a.pop(oid, None)

        elif leg_id == "B":
            unfilled = max(
                0,
                batch.b_requested_qty
                - batch.b_filled_qty
                - batch.b_canceled_qty
                - batch.b_rejected_qty,
            )
            if unfilled > 0:
                from_pending = min(unfilled, self._b_pending_submit_qty)
                self._b_pending_submit_qty = max(
                    0, self._b_pending_submit_qty - from_pending
                )
                from_working = min(unfilled - from_pending, self._b_working_qty)
                self._b_working_qty = max(0, self._b_working_qty - from_working)
                self._b_canceled_qty += unfilled
                batch.b_canceled_qty += unfilled
            batch.b_order_status = "CANCELED"
            self._active_sell_orders_b.pop(oid, None)

        self._resolve_batch_after_terminal_event(batch)

        self.observe(
            "Order cancelled — batch updated",
            context={
                "order_id": oid,
                "batch_id": batch.batch_id,
                "batch_status": batch.batch_status.value,
            },
        )

    # ------------------------------------------------------------------
    # Batch resolution helper
    # ------------------------------------------------------------------
    def _resolve_batch_after_terminal_event(self, batch: ExecutionBatch) -> None:
        """
        Called after every terminal event (fill-complete, cancel, reject).
        When both legs reach a terminal state the batch is fully resolved,
        in-flight capacity is released, and the run state returns to READY.
        Safe to call unconditionally — returns early if one leg still active.
        """
        a_resolved = batch.a_order_status in ("FILLED", "CANCELED", "REJECTED")
        b_resolved = batch.b_order_status in ("FILLED", "CANCELED", "REJECTED")

        if not (a_resolved and b_resolved):
            return

        # Mark batch terminal if not already in a terminal state
        if batch.batch_status not in (
            BatchStatus.COMPLETED,
            BatchStatus.FAILED,
            BatchStatus.IMBALANCED,
        ):
            batch.batch_status = BatchStatus.CANCELED

        self._release_batch_in_flight(batch)
        self._release_live_batch(batch)

        if self._run_state not in {RunState.COMPLETED, RunState.FAILED}:
            self._run_state = RunState.READY

        self._record_event(
            "BATCH_RESOLVED",
            batch_id=batch.batch_id,
            batch_status=batch.batch_status.value,
            a_order_status=batch.a_order_status,
            b_order_status=batch.b_order_status,
            a_filled_qty=batch.a_filled_qty,
            b_filled_qty=batch.b_filled_qty,
            a_canceled_qty=batch.a_canceled_qty,
            b_canceled_qty=batch.b_canceled_qty,
            a_rejected_qty=batch.a_rejected_qty,
            b_rejected_qty=batch.b_rejected_qty,
        )
        self._flush_event_log()
        self._stop_if_attempt_limit_complete()

    # ------------------------------------------------------------------
    # Imbalance policy — CANCEL_REMAINDERS
    # ------------------------------------------------------------------
    def _apply_imbalance_policy(self, batch: ExecutionBatch) -> None:
        """
        CANCEL_REMAINDERS: request cancellation of any working or pending
        order on both legs using the exact client_order_id. Uses cancel_order
        by ID rather than cancelOrdersForSymbol to avoid disrupting other batches.
        """
        batch.batch_status = BatchStatus.IMBALANCED
        self.observe(
            "Applying CANCEL_REMAINDERS imbalance policy",
            context={"batch_id": batch.batch_id},
        )

        cancelable_statuses = (
            "PENDING_ACK", "WORKING", "PARTIALLY_FILLED", "PENDING", "CANCEL_PENDING"
        )

        if batch.a_client_order_id and batch.a_order_status in cancelable_statuses:
            try:
                self.cancel_order(batch.a_client_order_id)
                batch.a_order_status = "CANCEL_PENDING"
            except Exception as exc:
                self.observe(
                    f"Cancel A failed: {exc}",
                    context={"order_id": batch.a_client_order_id},
                )

        if batch.b_client_order_id and batch.b_order_status in cancelable_statuses:
            try:
                self.cancel_order(batch.b_client_order_id)
                batch.b_order_status = "CANCEL_PENDING"
            except Exception as exc:
                self.observe(
                    f"Cancel B failed: {exc}",
                    context={"order_id": batch.b_client_order_id},
                )

        self.act(
            "Imbalance policy applied — cancellations requested by order_id",
            context={
                "batch_id": batch.batch_id,
                "a_order_id": batch.a_client_order_id,
                "b_order_id": batch.b_client_order_id,
            },
        )

    # ------------------------------------------------------------------
    # Self-match detection
    # Venue SMP tag is the primary safeguard; local check is a pre-screen.
    # ------------------------------------------------------------------
    def _self_match_conflict_a_buy(self, proposed_buy_price: float) -> bool:
        """
        Returns True if any active sell order on symbol_a has a limit price
        at or below proposed_buy_price (could cross against the new buy).
        Venue SMP is the binding control; this is a local early-exit pre-screen.
        """
        # No active sell orders on symbol_a in this strategy's leg schema
        # (Leg A is always BUY, Leg B is always SELL on a different symbol).
        # The check is a structural safety net — always returns False here.
        return False

    def _self_match_conflict_b_sell(self, proposed_sell_price: float) -> bool:
        """
        Returns True if any active buy order on symbol_b has a limit price
        at or above proposed_sell_price (could cross against the new sell).
        Leg B is always SELL on symbol_b; no buy orders exist on symbol_b
        in this strategy, so the conflict check always returns False.
        """
        return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def _initialize_event_log(self) -> None:
        """Prepare a unique JSONL path without starting another thread."""
        try:
            log_directory = Path.cwd() / "strategy_logs"
            log_directory.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            self._event_log_path = log_directory / (
                f"mark_two_leg_latency_{started_at}_{self._run_id}.jsonl"
            )
            self._last_event_log_flush_ts = time.monotonic()
        except Exception as exc:
            self._event_log_path = None
            self._report_event_log_failure(exc)

    def _record_event(self, record_type: str, **details) -> None:
        """Buffer a record without performing file I/O."""
        if self._event_log_path is None:
            return
        if len(self._event_log_buffer) >= 100_000:
            self._event_log_dropped_records += 1
            return
        self._event_log_buffer.append(
            {
                "record_type": record_type,
                "run_id": self._run_id,
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
                "monotonic_ns": time.monotonic_ns(),
                **details,
            }
        )

    def _flush_event_log(self) -> None:
        """Append the current buffer to JSONL in one bounded write."""
        if self._event_log_path is None or not self._event_log_buffer:
            return
        records = self._event_log_buffer
        try:
            payload = "".join(
                json.dumps(record, separators=(",", ":"), default=str) + "\n"
                for record in records
            )
            with self._event_log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(payload)
                log_file.flush()
            del self._event_log_buffer[:len(records)]
            self._last_event_log_flush_ts = time.monotonic()
        except Exception as exc:
            self._report_event_log_failure(exc)

    def _maybe_flush_event_log(self) -> None:
        """Periodically persist records after signal evaluation has completed."""
        if (
            self._event_log_buffer
            and time.monotonic() - self._last_event_log_flush_ts
            >= self._event_log_flush_interval_seconds
        ):
            self._flush_event_log()

    def _report_event_log_failure(self, exc: Exception) -> None:
        """Report at most one persistence failure to the UI."""
        if self._event_log_failure_reported:
            return
        self._event_log_failure_reported = True
        self.observe(
            "Event file logging unavailable",
            context={"error": str(exc)},
        )

    def _stop_if_attempt_limit_complete(self) -> None:
        """Stop once all configured attempts have resolved and no batch is live."""
        if (
            self._submission_attempts >= self.params.max_submission_attempts
            and self._live_batch_count == 0
            and self._run_state not in {RunState.COMPLETED, RunState.FAILED}
        ):
            self._run_state = RunState.COMPLETED
            self.observe(
                "Submission attempt limit completed — stopping",
                context={
                    "attempts": self._submission_attempts,
                    "max_submission_attempts": self.params.max_submission_attempts,
                },
            )
            self.stop()

    def _release_batch_in_flight(self, batch: ExecutionBatch) -> None:
        """
        Decrement the in-flight counter when a batch is fully resolved.
        Idempotent — guarded by in_flight_released flag.
        """
        if batch.in_flight_released:
            return
        batch.in_flight_released = True
        self._current_in_flight_units = max(
            0, self._current_in_flight_units - batch.units_requested
        )

    def _release_live_batch(self, batch: ExecutionBatch) -> None:
        """
        Decrement the live batch counter when a batch is fully resolved.
        Idempotent — guarded by in_flight_batch_released flag.
        """
        if batch.in_flight_batch_released:
            return
        batch.in_flight_batch_released = True
        self._live_batch_count = max(0, self._live_batch_count - 1)

    def _emit_summary(self) -> None:
        """Emit final latency and quantity summary to the reasoning panel."""

        def _percentiles(data: list) -> dict:
            if not data:
                return {"count": 0}
            s = sorted(data)
            n = len(s)

            def _p(frac: float) -> float:
                idx = int(n * frac)
                return round(s[min(idx, n - 1)] * 1000.0, 3)  # seconds → ms

            return {
                "count": n,
                "min_ms": _p(0.0),
                "median_ms": _p(0.5),
                "p90_ms": _p(0.90),
                "p95_ms": _p(0.95),
                "p99_ms": _p(0.99),
                "max_ms": _p(1.0),
            }

        self.observe(
            "Strategy summary",
            context={
                "signals_evaluated": self._signals_evaluated,
                "signals_qualifying": self._signals_qualifying,
                "batches_total": len(self._batches),
                "a_filled_qty": self._a_filled_qty,
                "b_filled_qty": self._b_filled_qty,
                "a_canceled_qty": self._a_canceled_qty,
                "b_canceled_qty": self._b_canceled_qty,
                "a_rejected_qty": self._a_rejected_qty,
                "b_rejected_qty": self._b_rejected_qty,
                "decision_latency": _percentiles(self._decision_latencies),
                "leg_send_skew": _percentiles(self._leg_send_skews),
                "a_ack_latency": _percentiles(self._a_ack_latencies),
                "b_ack_latency": _percentiles(self._b_ack_latencies),
                "batch_completion_latency": _percentiles(
                    self._batch_completion_latencies
                ),
            },
        )
