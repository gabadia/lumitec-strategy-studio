"""
Strategy created using Lumitec's Strategy Studio version X.

Mission: EXECUTION | Objective: SIGNAL_DRIVEN

Logic:
A pure latency measurement probe. On each qualifying quote tick it submits a
single deeply-passive BUY limit order (offset_ticks * 0.01 below best bid —
zero fill risk), records nanosecond timestamps at tick-receipt, pre-submit,
order-accepted, and order-canceled, then writes one CSV row. The strategy builds
no inventory and takes no P&L risk. After max_samples rows or duration_mins
wall-clock minutes it terminates via forced_stop() and emits full percentile
statistics for every derived latency column. Use this to A/B supervisor-side
changes (event-router lazy logging, removed per-event allocations,
NautilusTrader msgbus subscription changes) by running identical configs on
candidate vs rollback builds during the same market window and comparing the
tick_to_submit_us distribution.

Key parameters:
- sample_every_n_ticks  - take one probe per N quote ticks (reduces load)
- min_interval_ms       - minimum wall-clock gap between probes (ms)
- offset_ticks          - how far below best bid the probe order is priced
                          (in units of 0.01; default 50 → $0.50 below bid)
- order_qty             - probe order size (default 1 share)
- max_samples           - stop after this many completed rows (default 20 000)
- duration_mins         - hard wall-clock stop (default 30 min)
- summary_every         - emit a rolling percentile summary every N samples

Market data:
- Quote ticks only (subscribe_quotes=True, subscribe_trades=False)

Termination:
- A data-independent clock timer (fires every 10 s) enforces duration_mins even
  if the quote feed goes silent.
- All termination paths funnel through the idempotent _finish(), which cancels
  the timer, cancels open orders, and calls self.forced_stop(reason, stop_reason)
  — the Lumitec lifecycle-aware stop. Never self.stop() directly.
- on_stop() writes the final summary + closes the CSV first; nothing in it raises.

Risk controls:
- max_position: 10 (never reached — single-inflight, immediate cancel)
- max_loss: 1.0    (probe orders rest far from market; fills abort the strategy)
- max_active_orders_per_side: 2
- max_order_rate_per_second: 5.0
- on_order_filled: immediately aborts via _finish(..., "FAILED")

Important notes:
- Single-inflight guard: a new probe is never submitted until the previous
  cancel ACK arrives, so order count is always 0 or 1.
- Replay guard: ticks whose ts_event is more than 10 s before on_start are
  ignored to avoid spurious measurements during a warm-up burst.
- Watch for a recurring spike in tick_to_submit_us roughly every 5 s — that is
  the supervisor monitor loop's blocking psutil.cpu_percent(interval=0.1) call,
  the primary jitter source targeted by the A/B changes.
- CSV is opened once in on_start and flushed after every row; it is closed
  cleanly in on_stop even if the strategy is force-stopped.
- All Nautilus price fields (tick.bid_price etc.) are decimal.Decimal; they are
  cast to float before arithmetic.

Usage:
  Run identical config on candidate build and rollback build during the same
  market window. Compare the tick_to_submit_us p50/p95/p99/max distributions.
"""

import csv
import math
import os
import statistics
import time
from dataclasses import dataclass, field, replace, fields as dc_fields
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective


# ---------------------------------------------------------------------------
# Config — layer 1: platform metadata + all configurable fields
# ---------------------------------------------------------------------------

class Config(LumitecStrategyConfig):
    strategy_name: str = "LatencyBaselineProbe"
    file_name: str = "latency_baseline_probe.py"

    # probe shape
    symbol: str = "AAPL"
    sample_every_n_ticks: int = 10
    min_interval_ms: int = 200
    offset_ticks: int = 50
    order_qty: int = 1
    max_samples: int = 20000
    duration_mins: int = 30
    summary_every: int = 1000

    # mandatory production risk fields
    max_position: int = 10
    max_loss: float = 1.0
    max_active_orders_per_side: int = 2
    max_order_rate_per_second: float = 5.0


# ---------------------------------------------------------------------------
# ConfigParams — layer 2: frozen runtime parameters with hot-update support
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigParams:
    # probe shape
    symbol: str = "AAPL"
    sample_every_n_ticks: int = 10
    min_interval_ms: int = 200
    offset_ticks: int = 50
    order_qty: int = 1
    max_samples: int = 20000
    duration_mins: int = 30
    summary_every: int = 1000

    # mandatory production risk fields
    max_position: int = 10
    max_loss: float = 1.0
    max_active_orders_per_side: int = 2
    max_order_rate_per_second: float = 5.0

    def validate(self) -> None:
        if self.sample_every_n_ticks <= 0:
            raise ValueError("sample_every_n_ticks must be > 0")
        if self.min_interval_ms <= 0:
            raise ValueError("min_interval_ms must be > 0")
        if self.offset_ticks <= 0:
            raise ValueError("offset_ticks must be > 0")
        if self.order_qty <= 0:
            raise ValueError("order_qty must be > 0")
        if self.max_samples <= 0:
            raise ValueError("max_samples must be > 0")
        if self.duration_mins <= 0:
            raise ValueError("duration_mins must be > 0")
        if self.summary_every <= 0:
            raise ValueError("summary_every must be > 0")
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")

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
# CSV column definitions
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "seq",
    "wall_utc_iso",
    "symbol",
    "tick_ts_init_ns",
    "tick_recv_ns",
    "submit_ns",
    "accept_ns",
    "cancel_ack_ns",
    "venue_ack_ts_ns",
    "ingest_lag_us",
    "tick_to_submit_us",
    "submit_to_accept_us",
    "submit_to_cancel_us",
    "wire_rt_us",
    "client_order_id",
]


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class LatencyBaselineProbe(LumitecBaseStrategy):
    mission   = StrategyMission.EXECUTION
    objective = StrategyObjective.SIGNAL_DRIVEN
    leg_mode  = LegMode.CONTINUOUS
    leg_schema = [{"label": "Leg A", "side": "BUY", "fixed_side": True}]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)

        # probe state
        self._tick_count: int = 0
        self._sample_count: int = 0
        self._inflight_oid: Optional[str] = None
        self._last_sample_ns: int = 0

        # pending rows: oid -> partial measurement dict
        self._pending: Dict[str, dict] = {}

        # in-memory accumulation for percentile summaries
        self._tick_to_submit_us_all: List[float] = []
        self._submit_to_accept_us_all: List[float] = []
        self._submit_to_cancel_us_all: List[float] = []
        self._ingest_lag_us_all: List[float] = []
        self._wire_rt_us_all: List[float] = []

        # CSV handle (opened in on_start)
        self._csv_file = None
        self._csv_writer = None

        # timing sentinels (set in on_start)
        self._start_time_ns: int = 0
        self._duration_ns: int = 0

        # symbol (resolved in on_start from leg)
        self._symbol: str = self.params.symbol

        # order sequence counter
        self._oid_counter: int = 0

        # last tick ts for dedup (not strictly required, but defensive)
        self._last_tick_ts: float = 0.0

        # idempotent finish guard + deadline timer name
        self._finishing: bool = False
        self._deadline_timer_name: str = "probe_deadline"

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError("LatencyBaselineProbe requires exactly 1 leg")
        if legs[0].get("side") != "BUY":
            raise ValueError("LatencyBaselineProbe leg must be BUY")

    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        super().on_start()

        # resolve symbol from leg (override default if leg provides one)
        leg_a = self.legs[0]
        self._symbol = leg_a.get("symbol") or self.params.symbol

        # timing
        self._start_time_ns = self.clock.timestamp_ns()
        self._duration_ns = int(self.params.duration_mins * 60 * 1_000_000_000)

        # open CSV
        self._open_csv()

        # schedule data-independent deadline timer (fires every 10 s) — this is
        # what guarantees termination when the quote feed goes silent.
        self._deadline_timer_name = f"probe_deadline_{self.id}"
        try:
            self.clock.set_timer(
                name=self._deadline_timer_name,
                interval=timedelta(seconds=10),
                callback=self._on_deadline_check,
            )
        except Exception as exc:
            self.log.error(f"Failed to schedule deadline timer: {exc}")

        # subscribe to quote ticks only
        self.subscribe_market_data(
            self._symbol,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        self.observe(
            "LatencyBaselineProbe started",
            context={
                "symbol": self._symbol,
                "max_samples": self.params.max_samples,
                "duration_mins": self.params.duration_mins,
                "sample_every_n_ticks": self.params.sample_every_n_ticks,
                "csv": self._csv_path,
            },
        )

    def on_stop(self) -> None:
        # (a) final percentile report — MUST run before anything that can raise
        try:
            self._emit_final_summary()
        except Exception as exc:
            try:
                self.log.error(f"final summary failed: {exc}")
            except Exception:
                pass

        # (b) close CSV
        try:
            self._close_csv()
        except Exception:
            pass

        # (c) cancel deadline timer
        try:
            self.clock.cancel_timer(self._deadline_timer_name)
        except Exception:
            pass

        # (d) unsubscribe market data (correct kwarg names)
        try:
            self.unsubscribe_market_data(
                self._symbol,
                unsubscribe_quotes=True,
                unsubscribe_trades=False,
            )
        except Exception:
            pass

        # (e) log stop
        try:
            self.observe("LatencyBaselineProbe stopped", context={"samples": self._sample_count})
        except Exception:
            pass

    def on_pause(self, reason: str = "") -> None:
        self.act("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Resumed")

    # ------------------------------------------------------------------
    # Termination
    # ------------------------------------------------------------------

    def _on_deadline_check(self, event=None) -> None:
        """Clock-driven deadline check — fires every 10 s regardless of tick flow."""
        try:
            if (self.clock.timestamp_ns() - self._start_time_ns) >= self._duration_ns:
                self._finish("duration limit reached (timer)", "TIME")
        except Exception as exc:
            try:
                self.log.error(f"deadline check failed: {exc}")
            except Exception:
                pass

    def _finish(self, reason: str, stop_reason: str = "COMPLETED") -> None:
        """Idempotent, lifecycle-aware termination. Safe from any code path."""
        if self._finishing:
            return
        self._finishing = True

        try:
            self.act("Probe finishing", context={"reason": reason, "stop_reason": stop_reason})
        except Exception:
            pass

        try:
            self.clock.cancel_timer(self._deadline_timer_name)
        except Exception:
            pass

        try:
            self.cancelAllOrders()
        except Exception:
            pass

        # Lumitec lifecycle-aware stop — emits FORCED_STOP so the supervisor
        # cleans up (SSE strategy.stopped, controller removal, mapping unregister).
        # NEVER call self.stop() directly.
        try:
            self.forced_stop(reason, stop_reason)
        except Exception as exc:
            try:
                self.log.error(f"forced_stop failed: {exc}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Market data handler
    # ------------------------------------------------------------------

    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        # ── timestamp FIRST — must be on the very first line ──
        tick_recv_ns: int = self.clock.timestamp_ns()

        # duration guard — checked BEFORE isPaused so a paused probe still terminates
        if (tick_recv_ns - self._start_time_ns) >= self._duration_ns:
            self._finish("duration limit reached", "TIME")
            return

        if self.isPaused():
            return

        # replay guard: ignore ticks created before (start_time - 10s)
        replay_cutoff_ns = self._start_time_ns - 10_000_000_000
        if int(tick.ts_event) < replay_cutoff_ns:
            return

        # sampling gate 1: tick index
        self._tick_count += 1
        if self._tick_count % self.params.sample_every_n_ticks != 0:
            return

        # sampling gate 2: minimum interval
        elapsed_ms = (tick_recv_ns - self._last_sample_ns) / 1_000_000.0
        if elapsed_ms < self.params.min_interval_ms:
            return

        # sampling gate 3: single-inflight guard
        if self._inflight_oid is not None:
            return

        # ── compute probe price ──
        bid = float(tick.bid_price)
        if bid <= 0.0:
            return  # defensive: skip malformed tick

        probe_price = bid - self.params.offset_ticks * 0.01
        if probe_price <= 0.0:
            return  # defensive: price must be positive

        # ── record submit timestamp immediately before submission ──
        submit_ns: int = self.clock.timestamp_ns()

        try:
            order = self.submit_limit_order(
                symbol=self._symbol,
                side=OrderSide.BUY,
                qty=self.params.order_qty,
                price=Price.from_str(f"{probe_price:.2f}"),
                tif=TimeInForce.DAY,
                leg_id="A",
                role="OPEN",
            )
            oid_str: str = order.client_order_id.value
        except Exception as exc:
            self.observe("submit_limit_order failed", context={"error": str(exc)})
            return

        # register inflight
        self._inflight_oid = oid_str
        self._last_sample_ns = tick_recv_ns
        self._pending[oid_str] = {
            "tick_ts_init_ns": int(tick.ts_init),
            "tick_recv_ns": tick_recv_ns,
            "submit_ns": submit_ns,
            "accept_ns": 0,
            "cancel_ack_ns": 0,
            "venue_ack_ts_ns": 0,
        }

        self.observe(
            "Probe submitted",
            context={"oid": oid_str, "probe_price": f"{probe_price:.2f}", "bid": f"{bid:.2f}"},
        )

    # ------------------------------------------------------------------
    # Order event handlers
    # ------------------------------------------------------------------

    def on_order_accepted(self, event) -> None:
        accept_ns: int = self.clock.timestamp_ns()
        oid = event.client_order_id.value

        if oid not in self._pending:
            return

        self._pending[oid]["accept_ns"] = accept_ns
        self._pending[oid]["venue_ack_ts_ns"] = int(event.ts_event)

        # immediately cancel — single-inflight keeps only this order live
        self.cancelOrdersForSymbol(self._symbol)

    def on_order_canceled(self, event) -> None:
        cancel_ack_ns: int = self.clock.timestamp_ns()
        oid = event.client_order_id.value

        if oid not in self._pending:
            # unknown order — clear inflight guard defensively
            if self._inflight_oid == oid:
                self._inflight_oid = None
            return

        row_data = self._pending.pop(oid)
        row_data["cancel_ack_ns"] = cancel_ack_ns

        # clear inflight guard
        self._inflight_oid = None

        # write CSV row
        self._write_row(oid, row_data)

        # check sample ceiling
        if self._sample_count >= self.params.max_samples:
            self._finish("sample limit reached", "COMPLETED")

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        reason = getattr(event, "reason", "unknown")
        self.observe(
            "Order rejected",
            context={"oid": oid, "reason": str(reason)},
        )

        # write a partial row so the rejection is visible in the CSV
        if oid in self._pending:
            row_data = self._pending.pop(oid)
            row_data["cancel_ack_ns"] = 0
            row_data["venue_ack_ts_ns"] = 0
            row_data["accept_ns"] = 0
            row_data["reject_reason"] = str(reason)
            self._write_row(oid, row_data, rejected=True)

        if self._inflight_oid == oid:
            self._inflight_oid = None

    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        self.log.error(
            f"UNEXPECTED FILL on probe order {oid} — "
            f"probe price was far below bid; aborting strategy immediately."
        )
        self.observe(
            "UNEXPECTED FILL - aborting",
            context={"oid": oid, "fill_px": str(getattr(event, "last_px", "?"))},
        )
        self._finish("unexpected fill on probe order", "FAILED")

    def on_order_canceled_local(self, event) -> None:
        # some platform versions surface locally-generated cancels separately
        self.on_order_canceled(event)

    # ------------------------------------------------------------------
    # CSV helpers
    # ------------------------------------------------------------------

    def _open_csv(self) -> None:
        supervisor_id = os.environ.get("SUPERVISOR_ID", "local")
        strategy_id = getattr(self, "id", "unknown")
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        filename = f"latency_baseline_{supervisor_id}_{strategy_id}_{date_str}.csv"
        log_dir = "/app/logs"
        os.makedirs(log_dir, exist_ok=True)
        self._csv_path = os.path.join(log_dir, filename)

        file_is_new = not os.path.exists(self._csv_path) or os.path.getsize(self._csv_path) == 0
        self._csv_file = open(self._csv_path, "a", newline="", buffering=1)  # line-buffered
        self._csv_writer = csv.writer(self._csv_file)

        if file_is_new:
            self._csv_writer.writerow(_CSV_COLUMNS)
            self._csv_file.flush()

    def _close_csv(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

    def _write_row(self, oid: str, row: dict, rejected: bool = False) -> None:
        tick_ts_init_ns  = row.get("tick_ts_init_ns", 0)
        tick_recv_ns     = row.get("tick_recv_ns", 0)
        submit_ns        = row.get("submit_ns", 0)
        accept_ns        = row.get("accept_ns", 0)
        cancel_ack_ns    = row.get("cancel_ack_ns", 0)
        venue_ack_ts_ns  = row.get("venue_ack_ts_ns", 0)

        def _us(a: int, b: int) -> str:
            if a == 0 or b == 0:
                return ""
            return f"{(b - a) / 1000.0:.3f}"

        ingest_lag_us       = _us(tick_ts_init_ns, tick_recv_ns)
        tick_to_submit_us   = _us(tick_recv_ns, submit_ns)
        submit_to_accept_us = _us(submit_ns, accept_ns)
        submit_to_cancel_us = _us(submit_ns, cancel_ack_ns)
        wire_rt_us          = _us(submit_ns, venue_ack_ts_ns)

        # only count and accumulate non-rejected complete rows
        if not rejected and accept_ns > 0 and cancel_ack_ns > 0:
            self._sample_count += 1

            if ingest_lag_us:
                self._ingest_lag_us_all.append(float(ingest_lag_us))
            if tick_to_submit_us:
                self._tick_to_submit_us_all.append(float(tick_to_submit_us))
            if submit_to_accept_us:
                self._submit_to_accept_us_all.append(float(submit_to_accept_us))
            if submit_to_cancel_us:
                self._submit_to_cancel_us_all.append(float(submit_to_cancel_us))
            if wire_rt_us:
                self._wire_rt_us_all.append(float(wire_rt_us))

            # periodic summary
            if (
                self.params.summary_every > 0
                and self._sample_count % self.params.summary_every == 0
            ):
                self._emit_rolling_summary()

        wall_utc_iso = datetime.now(timezone.utc).isoformat()

        csv_row = [
            self._sample_count if not rejected else f"REJ-{oid}",
            wall_utc_iso,
            self._symbol,
            tick_ts_init_ns,
            tick_recv_ns,
            submit_ns,
            accept_ns,
            cancel_ack_ns,
            venue_ack_ts_ns,
            ingest_lag_us,
            tick_to_submit_us,
            submit_to_accept_us,
            submit_to_cancel_us,
            wire_rt_us,
            oid,
        ]

        if self._csv_writer is not None:
            self._csv_writer.writerow(csv_row)
            self._csv_file.flush()

        self.act(
            "Row written",
            context={
                "seq": self._sample_count,
                "tick_to_submit_us": tick_to_submit_us,
                "submit_to_accept_us": submit_to_accept_us,
                "submit_to_cancel_us": submit_to_cancel_us,
            },
        )

    # ------------------------------------------------------------------
    # Percentile helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pct(data: List[float], q: float) -> float:
        """Return the q-th percentile (0–100) of data. Returns nan if empty."""
        if not data:
            return float("nan")
        sorted_d = sorted(data)
        n = len(sorted_d)
        idx = (q / 100.0) * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_d[lo] + frac * (sorted_d[hi] - sorted_d[lo])

    def _summary_dict(self, label: str, data: List[float]) -> dict:
        if not data:
            return {f"{label}_count": 0}
        return {
            f"{label}_count": len(data),
            f"{label}_mean":  round(statistics.mean(data), 3),
            f"{label}_stdev": round(statistics.stdev(data) if len(data) > 1 else 0.0, 3),
            f"{label}_p50":   round(self._pct(data, 50), 3),
            f"{label}_p90":   round(self._pct(data, 90), 3),
            f"{label}_p95":   round(self._pct(data, 95), 3),
            f"{label}_p99":   round(self._pct(data, 99), 3),
            f"{label}_p999":  round(self._pct(data, 99.9), 3),
            f"{label}_max":   round(max(data), 3),
        }

    def _emit_rolling_summary(self) -> None:
        ctx: dict = {"count": self._sample_count}
        for label, data in [
            ("tick_to_submit_us", self._tick_to_submit_us_all),
            ("submit_to_accept_us", self._submit_to_accept_us_all),
        ]:
            if not data:
                continue
            ctx[f"{label}_p50"]  = round(self._pct(data, 50), 3)
            ctx[f"{label}_p95"]  = round(self._pct(data, 95), 3)
            ctx[f"{label}_p99"]  = round(self._pct(data, 99), 3)
            ctx[f"{label}_max"]  = round(max(data), 3)

        self.observe("latency summary", context=ctx)

    def _emit_final_summary(self) -> None:
        ctx: dict = {"total_samples": self._sample_count}
        datasets = [
            ("ingest_lag_us",       self._ingest_lag_us_all),
            ("tick_to_submit_us",   self._tick_to_submit_us_all),
            ("submit_to_accept_us", self._submit_to_accept_us_all),
            ("submit_to_cancel_us", self._submit_to_cancel_us_all),
            ("wire_rt_us",          self._wire_rt_us_all),
        ]
        for label, data in datasets:
            ctx.update(self._summary_dict(label, data))

        self.decide("latency baseline complete", context=ctx)

        # append human-readable summary comment to CSV
        if self._csv_file is not None:
            try:
                summary_parts = [f"total_samples={self._sample_count}"]
                for label, data in datasets:
                    if data:
                        summary_parts.append(
                            f"{label}: p50={self._pct(data,50):.3f} "
                            f"p95={self._pct(data,95):.3f} "
                            f"p99={self._pct(data,99):.3f} "
                            f"max={max(data):.3f} us"
                        )
                self._csv_file.write("# SUMMARY " + " | ".join(summary_parts) + "\n")
                self._csv_file.flush()
            except Exception:
                pass
