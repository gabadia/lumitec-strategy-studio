"""
Strategy created using Lumitec's Strategy Studio version X.

Logic:
PilotInsidePeg is a multi-shot inside-market pegging strategy for the NUAM
Latency Equalization Pilot. It executes num_shots independent passive->cross
sequences, each launched exactly on an absolute epoch-aligned wall-clock
boundary of the shot interval. Each shot rests a limit order at the inside
quote (BUY @ BID, SELL @ ASK), then on timeout cancels it and submits an IOC
crossing order (BUY @ ASK+offset, SELL @ BID-offset). Every lifecycle event
is captured in memory and flushed to a JSONL file between shots for
cross-pathway latency analysis.

Measurement coverage (Charter intervals in parentheses):
- Market data feed latency, exchange event -> strategy receipt (1)
- Platform internal distribution latency, ingest -> handler (2)
- Decision latency, trigger -> order handed to OMS (3)
- Order round trip, submit -> exchange acknowledgment (4+5 combined)
- Reaction end-to-end, market event -> exchange ack (6; trigger_mode="tick")
- Cancel round trip, cancel request -> cancel confirmation
- IOC round trip, cross submit -> terminal (ack latency also captured)
- Quote age at decision, timer jitter, clock-sync probe, wall-clock drift

Key parameters:
- num_shots             - number of independent shot cycles to execute
- shot_interval_minutes - minutes between absolute boundary markers
- shot_quantity         - shares per shot
- start_offset_seconds  - boundary grid offset for pathway staggering
- timeout_seconds       - passive phase duration before crossing
- cross_buffer_seconds  - reserved tail of interval for IOC round trip
- cross_offset          - price improvement added when crossing the spread
- alternate_sides       - flip BUY/SELL on alternating shots
- measure_mode          - rest passive BEHIND the inside so it never fills;
                          every shot then exercises the full passive ->
                          cancel -> IOC sequence deterministically
- measure_offset        - distance behind the inside in measure_mode
- trigger_mode          - "timer": launch at the boundary instant (paired
                          samples); "tick": arm at the boundary, launch on
                          the next quote tick (true market-event-to-ack)
- pathway_label         - "CONVENTIONAL" | "TRADINGNODE" stamped on events
- session_id            - test-session identifier for cross-pathway pairing
- event_log_dir         - output directory for JSONL event files

Market data:
- Quote ticks (bid/ask) subscribed for the single leg symbol; cached for
  boundary-time price reference. Per-tick feed/internal latencies are
  buffered (two float appends per tick) and aggregated at shot close.
  No bars used.

Risk controls:
- max_position: 500
- max_loss: 1000.0
- max_active_orders_per_side: 5
- max_order_rate_per_second: 10.0
- timeout_seconds + cross_buffer_seconds must fit within shot_interval
- unfilled remainder is abandoned at shot close; never rolled forward
- shots that are still live at the next boundary are force-cleaned

Important notes:
- Boundary schedule is epoch-aligned and drift-free; start_offset_seconds
  staggers two pathways running the same code so they never contend.
- No file or network I/O occurs in the measured (hot) path; JSONL flush
  happens only in the dead-air window after each shot closes. Per-tick MD
  capture is bounded-list appends only.
- Cross-clock measurements (feed latency, market-event-to-ack) depend on
  host clock sync; wall/monotonic anchor pairs and a file-read probe of
  chrony/timesyncd state (no subprocess use) are recorded at RUN_START
  and RUN_STOP so every session carries its own error bound.
- Author: Guillermo Abadia, Lumitec Inc. Copyright (c) 2026 Lumitec Inc.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, replace, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from nautilus_trader.core.message import Event
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Price

from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig


# -----------------------------------------------------------------------------
# Config (metadata only, consistent with established pattern)
# -----------------------------------------------------------------------------
class Config(LumitecStrategyConfig):  # type: ignore[misc]
    strategy_name: str = "PilotInsidePeg"
    file_name: str = "pilot_inside_peg_strategy.py"

    # NOTE: No strategy-specific params here. They live in ConfigParams.


# -----------------------------------------------------------------------------
# Strategy-specific params (come via supervisor.strategy_params)
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigParams:
    # --- Shot schedule ---
    num_shots: int = 10
    shot_interval_minutes: float = 1.0
    shot_quantity: int = 1
    start_offset_seconds: float = 0.0
    alternate_sides: bool = False
    # --- Per-shot timing budget ---
    timeout_seconds: float = 45.0
    cross_buffer_seconds: float = 10.0
    # --- Pricing ---
    cross_offset: float = 0.01
    # --- Measurement configuration ---
    measure_mode: bool = False
    measure_offset: float = 0.05
    trigger_mode: str = "timer"          # "timer" | "tick"
    md_stats_enabled: bool = True
    clock_probe_enabled: bool = True
    # --- Measurement provenance ---
    pathway_label: str = "UNSPECIFIED"
    session_id: str = ""
    event_log_dir: str = ""
    # --- Required production risk controls ---
    max_position: int = 500
    max_loss: float = 1000.0
    max_active_orders_per_side: int = 5
    max_order_rate_per_second: float = 10.0

    def interval_seconds(self) -> float:
        return float(self.shot_interval_minutes) * 60.0

    def validate(self) -> None:
        if self.num_shots <= 0:
            raise ValueError("num_shots must be > 0")
        if not (float(self.shot_interval_minutes) > 0):
            raise ValueError("shot_interval_minutes must be > 0")
        if self.shot_quantity <= 0:
            raise ValueError("shot_quantity must be > 0")
        if not (float(self.timeout_seconds) > 0):
            raise ValueError("timeout_seconds must be > 0")
        if not (float(self.cross_buffer_seconds) >= 0):
            raise ValueError("cross_buffer_seconds must be >= 0")
        if not (0.0 < float(self.cross_offset) <= 1.0):
            raise ValueError("cross_offset must be in (0,1]")
        if not (0.0 < float(self.measure_offset) <= 5.0):
            raise ValueError("measure_offset must be in (0,5]")
        if self.trigger_mode not in ("timer", "tick"):
            raise ValueError("trigger_mode must be 'timer' or 'tick'")
        if not (0.0 <= float(self.start_offset_seconds) < self.interval_seconds()):
            raise ValueError(
                "start_offset_seconds must be in [0, shot_interval) "
                f"= [0, {self.interval_seconds():.1f})"
            )
        budget = float(self.timeout_seconds) + float(self.cross_buffer_seconds)
        if budget > self.interval_seconds():
            raise ValueError(
                "timeout_seconds + cross_buffer_seconds "
                f"({budget:.1f}s) must fit within the shot interval "
                f"({self.interval_seconds():.1f}s)"
            )
        if self.max_position <= 0:
            raise ValueError("max_position must be > 0")
        if self.max_loss <= 0:
            raise ValueError("max_loss must be > 0")
        if self.max_active_orders_per_side <= 0:
            raise ValueError("max_active_orders_per_side must be > 0")
        if self.max_order_rate_per_second <= 0:
            raise ValueError("max_order_rate_per_second must be > 0")

    @classmethod
    def from_config(cls, cfg: Any) -> "ConfigParams":
        values: Dict[str, Any] = {}
        for f in dc_fields(cls):
            values[f.name] = getattr(cfg, f.name, f.default)
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: Dict[str, Any]) -> "ConfigParams":
        allowed = {f.name for f in dc_fields(self)}
        cleaned: Dict[str, Any] = {}
        for k, v in (updates or {}).items():
            if k in allowed and v is not None:
                current = getattr(self, k)
                if isinstance(current, bool):
                    cleaned[k] = bool(v)
                elif isinstance(current, float):
                    cleaned[k] = float(v)
                elif isinstance(current, int):
                    cleaned[k] = int(v)
                else:
                    cleaned[k] = v
        new_params = replace(self, **cleaned)
        new_params.validate()
        return new_params


# -----------------------------------------------------------------------------
# Per-shot state capsule
# -----------------------------------------------------------------------------
SHOT_PENDING = "PENDING"        # scheduled, not yet started
SHOT_AWAIT_TICK = "AWAIT_TICK"  # boundary passed; waiting for trigger tick
SHOT_ACTIVE = "ACTIVE"          # passive order in flight / resting
SHOT_CROSSING = "CROSSING"      # deadline hit, IOC submitted
SHOT_COMPLETE = "COMPLETE"      # fully filled
SHOT_PARTIAL = "PARTIAL"        # window closed with partial execution
SHOT_UNFILLED = "UNFILLED"      # window closed with zero execution
SHOT_SKIPPED = "SKIPPED"        # no quote/tick in window; never started
SHOT_INCOMPLETE = "INCOMPLETE"  # force-cleaned at next boundary / rejected


@dataclass
class ShotRecord:
    index: int
    side: Optional[OrderSide] = None
    quantity: float = 0.0
    remaining: float = 0.0
    status: str = SHOT_PENDING
    passive_oid: str = ""            # ClientOrderId.value (string)
    cross_oid: str = ""              # ClientOrderId.value (string)
    passive_oid_obj: Optional[ClientOrderId] = None   # for cancel_order calls
    cross_oid_obj: Optional[ClientOrderId] = None
    deadline_armed: bool = False
    cross_submitted: bool = False
    cancel_initiated: bool = False   # True when WE cancel (cross / cleanup)
    passive_fill_qty: float = 0.0
    cross_fill_qty: float = 0.0
    closed: bool = False             # SHOT_CLOSED already recorded
    # --- Measurement timestamps (monotonic ns; 0 = never happened) ---
    t_boundary_ns: int = 0
    t_passive_submit_ns: int = 0
    t_passive_ack_ns: int = 0
    t_first_fill_ns: int = 0
    t_deadline_ns: int = 0
    t_cancel_req_ns: int = 0
    t_cancel_ack_ns: int = 0
    t_cross_submit_ns: int = 0
    t_cross_ack_ns: int = 0
    t_cross_terminal_ns: int = 0     # cross ack/fill-complete/cancel returned
    t_close_ns: int = 0
    # --- Wall-clock / cross-clock endpoints (ns since epoch; 0 = n/a) ---
    trigger_event_ns: int = 0        # ts_event of the triggering tick (tick mode)
    t_submit_wall_ns: int = 0
    t_passive_ack_wall_ns: int = 0
    # --- Context measurements ---
    quote_age_at_decision_ms: Optional[float] = None
    boundary_jitter_ms: Optional[float] = None

    def is_live(self) -> bool:
        return self.status in (SHOT_ACTIVE, SHOT_CROSSING, SHOT_AWAIT_TICK)


def _ms(t_from_ns: int, t_to_ns: int) -> Optional[float]:
    """Interval in milliseconds, or None if either endpoint never happened."""
    if t_from_ns <= 0 or t_to_ns <= 0:
        return None
    return round((t_to_ns - t_from_ns) / 1e6, 3)


# -----------------------------------------------------------------------------
# Strategy
# -----------------------------------------------------------------------------
class PilotInsidePeg(LumitecBaseStrategy):
    """
    Multi-shot inside-market pegging strategy for the NUAM pilot.

    Executes num_shots independent passive->cross sequences, each launched
    exactly on an absolute wall-clock boundary of the shot interval, each
    guaranteed to terminate before the next boundary. Captures a complete
    per-shot latency decomposition plus market-data feed statistics to a
    local JSONL file for statistical analysis, with zero file/network I/O
    in the measured path.

    Normal mode:
      BUY:  passive limit @ BID  ->  on timeout, cross @ ASK+offset (IOC)
      SELL: passive limit @ ASK  ->  on timeout, cross @ BID-offset (IOC)
    Measure mode (deterministic full-lifecycle exercise):
      BUY:  passive limit @ BID-measure_offset (never fills) -> timeout ->
            cancel (round trip measured) -> cross @ ASK+offset (IOC)
      SELL: mirror image.
    """

    mission:   StrategyMission   = StrategyMission.EXECUTION
    objective: StrategyObjective = StrategyObjective.TARGET_QTY
    leg_mode:  LegMode           = LegMode.FINITE
    leg_schema = [{"label": "Leg", "side": None, "fixed_side": False}]

    _SHOT_PREFIX = "PIPEG_SHOT_"
    _DEADLINE_PREFIX = "PIPEG_DEADLINE_"
    _FINAL_ALERT = "PIPEG_FINAL"
    _MD_WINDOW_CAP = 100_000     # per-shot-window sample cap
    _MD_RUN_CAP = 300_000        # run-level reservoir cap

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError("PilotInsidePeg requires exactly 1 leg")
        side = str(legs[0].get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("PilotInsidePeg leg side must be BUY or SELL")

    def __init__(self, config: Config) -> None:
        super().__init__(config)

        self.params: ConfigParams = ConfigParams.from_config(config)

        self._order_mode: str = "single"
        self._duration_minutes: int = config.duration_minutes

        self.symbol: str = ""
        self._base_side: OrderSide = OrderSide.BUY
        self._tif: TimeInForce = TimeInForce.DAY

        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None
        self._last_quote_mono_ns: int = 0

        self._log_trades: bool = getattr(config, 'log_trades', False)
        self._log_quotes: bool = getattr(config, 'log_quotes', False)

        # Shot bookkeeping (keys are ClientOrderId.value strings -- the
        # framework's leg-info extraction requires strings, not objects)
        self._shots: List[ShotRecord] = []
        self._oid_to_shot: Dict[str, ShotRecord] = {}
        self._current_shot: Optional[ShotRecord] = None
        self._first_boundary: Optional[pd.Timestamp] = None
        self._await_tick_shot: Optional[int] = None   # tick-trigger arming

        # Event-log subsystem (ported from MarkTwoLegLatencyTest):
        # memory buffer in the hot path, JSONL flush at shot close / stop.
        self._run_id: str = uuid4().hex
        self._session_id: str = self.params.session_id or self._run_id
        self._event_log_buffer: list = []
        self._event_log_path: Optional[Path] = None
        self._event_log_failure_reported: bool = False

        # Latency accumulators for the stop-time percentile summary (ms)
        self._passive_ack_latencies_ms: List[float] = []
        self._boundary_to_submit_ms: List[float] = []
        self._cross_roundtrip_ms: List[float] = []
        self._cross_ack_latencies_ms: List[float] = []
        self._cancel_roundtrip_ms: List[float] = []
        self._event_to_ack_ms: List[float] = []

        # Market-data measurement buffers (hot path: appends only).
        # Window buffers reset at each shot close; run reservoir is capped.
        self._md_enabled: bool = bool(self.params.md_stats_enabled)
        self._md_feed_ms_window: List[float] = []
        self._md_int_ms_window: List[float] = []
        self._md_feed_ms_run: List[float] = []
        self._md_count_window: int = 0
        self._md_count_run: int = 0
        self._md_prev_mono_ns: int = 0
        self._md_max_gap_ms_window: float = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _oid_str(oid: Any) -> str:
        """ClientOrderId -> its string value. Framework helpers (e.g.
        extract_leg_info_from_order_id) require the string form."""
        if oid is None:
            return ""
        return getattr(oid, "value", None) or str(oid)

    @staticmethod
    def _percentiles(data: List[float]) -> dict:
        if not data:
            return {"count": 0}
        s = sorted(data)
        n = len(s)

        def _p(frac: float) -> float:
            idx = int(n * frac)
            return round(s[min(idx, n - 1)], 3)

        return {
            "count": n,
            "min_ms": _p(0.0),
            "median_ms": _p(0.5),
            "p90_ms": _p(0.90),
            "p95_ms": _p(0.95),
            "p99_ms": _p(0.99),
            "max_ms": _p(1.0),
        }

    def _probe_clock_sync(self) -> dict:
        """Best-effort clock-sync snapshot without spawning processes
        (subprocess is not permitted in the strategy sandbox).

        Always records paired wall/monotonic anchors: the divergence of
        (wall2-wall1) vs (mono2-mono1) between RUN_START and RUN_STOP
        reveals any wall-clock step/slew during the run.

        Additionally reads sync-state files when present and readable:
        - /run/chrony/chronyd.pid            -> chronyd is running
        - /var/lib/chrony/drift              -> chrony drift file (ppm)
        - /run/systemd/timesync/synchronized -> systemd-timesyncd synced
        Runs only at start/stop -- never in the measured path.
        """
        info: Dict[str, Any] = {
            "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
        }
        if not self.params.clock_probe_enabled:
            info["clock_probe"] = "disabled"
            return info
        probes: Dict[str, Any] = {}
        try:
            probes["chronyd_running"] = Path("/run/chrony/chronyd.pid").exists()
        except Exception:
            pass
        try:
            drift_path = Path("/var/lib/chrony/drift")
            if drift_path.exists():
                probes["chrony_drift_ppm"] = drift_path.read_text().strip()[:64]
        except Exception:
            pass
        try:
            probes["timesyncd_synchronized"] = Path(
                "/run/systemd/timesync/synchronized"
            ).exists()
        except Exception:
            pass
        info["clock_probe"] = probes if probes else "no sync-state files readable"
        return info

    # ------------------------------------------------------------------
    # Event log subsystem (capture in memory, flush between shots)
    # ------------------------------------------------------------------
    def _initialize_event_log(self) -> None:
        """Prepare a unique JSONL path without writing in the hot path."""
        try:
            base = (
                Path(self.params.event_log_dir)
                if self.params.event_log_dir
                else Path.cwd() / "logs"
            )
            base.mkdir(parents=True, exist_ok=True)
            started_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            self._event_log_path = base / (
                f"pilot_inside_peg_{self.params.pathway_label.lower()}"
                f"_{started_at}_{self._run_id}.jsonl"
            )
        except Exception as exc:
            self._event_log_path = None
            self.log.error(
                "Event log init FAILED for dir="
                f"'{self.params.event_log_dir or str(Path.cwd() / 'logs')}'"
                f" -- measurement data will NOT be persisted: {exc}"
            )
            self._report_event_log_failure(exc)

    def _record_event(self, record_type: str, **details) -> None:
        """Append a structured record in memory; perform NO file I/O.
        Every record carries provenance for cross-pathway pairing."""
        self._event_log_buffer.append(
            {
                "record_type": record_type,
                "pathway": self.params.pathway_label,
                "session_id": self._session_id,
                "run_id": self._run_id,
                "symbol": self.symbol,
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
                "monotonic_ns": time.monotonic_ns(),
                **details,
            }
        )

    def _flush_event_log(self) -> None:
        """Append buffered records to JSONL. Called only at shot close,
        shot skip, and strategy stop -- never in the measured path."""
        if not self._event_log_buffer:
            return
        if self._event_log_path is None:
            # Lazy (re-)initialization: covers strategy_params arriving via
            # configure() after on_start, and log-dir corrections at runtime.
            self._initialize_event_log()
            if self._event_log_path is None:
                return
        try:
            payload = "".join(
                json.dumps(record, separators=(",", ":"), default=str) + "\n"
                for record in self._event_log_buffer
            )
            with self._event_log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(payload)
            self._event_log_buffer.clear()
        except Exception as exc:
            self._report_event_log_failure(exc)

    def _report_event_log_failure(self, exc: Exception) -> None:
        """Report at most one persistence failure to the UI."""
        if self._event_log_failure_reported:
            return
        self._event_log_failure_reported = True
        self.observe(
            "Event file logging unavailable",
            context={"error": str(exc)},
        )

    # ------------------------------------------------------------------
    # Parameter updates
    # ------------------------------------------------------------------
    def apply_params(self, updates: Dict[str, Any]) -> None:
        with self._param_lock:
            old = self.params
            new = old.merged(updates)

            timeout_changed = (new.timeout_seconds != old.timeout_seconds)

            self.params = new
            self._md_enabled = bool(new.md_stats_enabled)

            # Re-derive the log path if provenance changed before the first
            # successful write (configure() may run after on_start).
            provenance_changed = (
                new.event_log_dir != old.event_log_dir
                or new.pathway_label != old.pathway_label
                or new.session_id != old.session_id
            )
            if provenance_changed and self._event_log_path is not None \
                    and not self._event_log_buffer:
                pass  # already writing; path frozen for the run
            if provenance_changed and self._event_log_path is None:
                self._session_id = new.session_id or self._run_id
                self._event_log_failure_reported = False

            # Re-arm the current shot's deadline if its passive order is
            # resting and the timeout changed. The boundary grid itself is
            # immutable once started: changing it mid-run would break
            # comparability across pathways.
            shot = self._current_shot
            if (
                timeout_changed
                and shot is not None
                and shot.status == SHOT_ACTIVE
                and shot.passive_oid
                and shot.deadline_armed
            ):
                try:
                    name = f"{self._DEADLINE_PREFIX}{shot.index}"
                    if hasattr(self.clock, "cancel_time_alert"):
                        self.clock.cancel_time_alert(name=name)
                    secs = float(new.timeout_seconds)
                    alert_time = self.clock.utc_now() + pd.Timedelta(seconds=secs)
                    self.clock.set_time_alert(name=name, alert_time=alert_time)
                except Exception as e:
                    self.log.warning(
                        f"Failed to re-arm deadline after timeout change: {e}"
                    )

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)
        else:
            allowed = {f.name for f in dc_fields(ConfigParams)}
            flat = {k: v for k, v in extras.items() if k in allowed}
            if flat:
                self.apply_params(flat)

        self._order_mode = str(extras.get("order_mode", self._order_mode))

        dur = extras.get("duration_minutes")
        if isinstance(dur, int) and dur >= 0:
            self._duration_minutes = dur

    # ------------------------------------------------------------------
    # Required hook
    # ------------------------------------------------------------------
    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()

        if not hasattr(self, "legs") or not isinstance(self.legs, list) or len(self.legs) != 1:
            raise ValueError("PilotInsidePeg requires exactly 1 leg")

        leg = self.legs[0]
        self.symbol = leg["symbol"]

        side_s = str(leg.get("side", "BUY")).upper()
        self._base_side = OrderSide.BUY if side_s == "BUY" else OrderSide.SELL

        tif_s = str(leg.get("tif", "DAY")).upper()
        self._tif = getattr(TimeInForce, tif_s, TimeInForce.DAY)

        self._last_bid = None
        self._last_ask = None
        self._last_quote_mono_ns = 0
        self._await_tick_shot = None

        self._shots = [ShotRecord(index=i) for i in range(self.params.num_shots)]
        self._oid_to_shot = {}
        self._current_shot = None

        self._first_boundary = self._compute_first_boundary()

        self._initialize_event_log()
        self._record_event(
            "RUN_START",
            base_side=self._base_side.name,
            num_shots=self.params.num_shots,
            shot_interval_seconds=self.params.interval_seconds(),
            shot_quantity=self.params.shot_quantity,
            start_offset_seconds=self.params.start_offset_seconds,
            alternate_sides=self.params.alternate_sides,
            timeout_seconds=self.params.timeout_seconds,
            cross_buffer_seconds=self.params.cross_buffer_seconds,
            cross_offset=self.params.cross_offset,
            measure_mode=self.params.measure_mode,
            measure_offset=self.params.measure_offset,
            trigger_mode=self.params.trigger_mode,
            first_boundary_utc=str(self._first_boundary),
            tif=self._tif.name,
            clock_sync=self._probe_clock_sync(),
        )
        self._flush_event_log()

        self.observe(
            "Pilot strategy started",
            context={
                "symbol": self.symbol,
                "pathway": self.params.pathway_label,
                "session_id": self._session_id,
                "base_side": self._base_side.name,
                "num_shots": self.params.num_shots,
                "shot_interval_seconds": self.params.interval_seconds(),
                "shot_quantity": self.params.shot_quantity,
                "start_offset_seconds": self.params.start_offset_seconds,
                "measure_mode": self.params.measure_mode,
                "trigger_mode": self.params.trigger_mode,
                "first_boundary_utc": str(self._first_boundary),
                "event_log_path": (
                    str(self._event_log_path) if self._event_log_path else None
                ),
            },
        )

        assert self.symbol, "symbol must be set before subscribing"
        self.subscribe_market_data(self.symbol, True, False)
        self.log.info(f"Subscribed to market data for {self.symbol}")

        # Arm the alert for shot 0. Subsequent shots are armed as each
        # boundary fires, always computed from the absolute series.
        self._arm_shot_alert(0)

    def on_stop(self) -> None:
        if getattr(self, "symbol", None):
            self.unsubscribe_quote_ticks(self._md_instrument(self.symbol))

        summary: Dict[str, int] = {}
        for s in self._shots:
            summary[s.status] = summary.get(s.status, 0) + 1

        stop_clock = self._probe_clock_sync()
        self._record_event(
            "RUN_STOP",
            shots_total=len(self._shots),
            shots_by_status=summary,
            passive_fill_qty_total=sum(s.passive_fill_qty for s in self._shots),
            cross_fill_qty_total=sum(s.cross_fill_qty for s in self._shots),
            md_ticks_total=self._md_count_run,
            clock_sync=stop_clock,
        )
        self._emit_summary(summary)
        self._flush_event_log()

        self.act(
            "Pilot strategy stopped",
            context={
                "symbol": self.symbol,
                "pathway": self.params.pathway_label,
                "shots_total": len(self._shots),
                "shots_by_status": summary,
                "event_log_path": (
                    str(self._event_log_path) if self._event_log_path else None
                ),
            },
        )

    def on_pause(self, reason: str = "") -> None:
        if getattr(self, "symbol", None):
            self.unsubscribe_quote_ticks(self._md_instrument(self.symbol))
        self._await_tick_shot = None
        for shot in self._shots:
            if shot.status in (SHOT_ACTIVE, SHOT_AWAIT_TICK):
                self._cancel_shot_orders(shot)

    def on_resume(self) -> None:
        assert self.symbol, "symbol must be set before subscribing"
        self.subscribe_market_data(self.symbol, True, False)
        self.log.info(f"Resubscribed to market data for {self.symbol}")

    def _emit_summary(self, shots_by_status: Dict[str, int]) -> None:
        """Percentile summary of the run (ported pattern). Recorded to the
        event log AND narrated to the reasoning panel."""
        summary_ctx = {
            "pathway": self.params.pathway_label,
            "session_id": self._session_id,
            "trigger_mode": self.params.trigger_mode,
            "measure_mode": self.params.measure_mode,
            "shots_by_status": shots_by_status,
            "boundary_to_submit": self._percentiles(self._boundary_to_submit_ms),
            "passive_ack_latency": self._percentiles(self._passive_ack_latencies_ms),
            "cancel_roundtrip": self._percentiles(self._cancel_roundtrip_ms),
            "cross_ack_latency": self._percentiles(self._cross_ack_latencies_ms),
            "cross_roundtrip": self._percentiles(self._cross_roundtrip_ms),
            "market_event_to_ack": self._percentiles(self._event_to_ack_ms),
            "md_feed_latency": self._percentiles(self._md_feed_ms_run),
            "md_ticks_total": self._md_count_run,
        }
        self._record_event("RUN_SUMMARY", **summary_ctx)
        self.observe("Pilot run summary", context=summary_ctx)

    # ------------------------------------------------------------------
    # Boundary schedule (absolute, drift-free)
    # ------------------------------------------------------------------
    def _compute_first_boundary(self) -> pd.Timestamp:
        """Next epoch-aligned interval boundary (+ start offset), at least
        ~1s in the future so the alert can be armed safely."""
        interval = self.params.interval_seconds()
        offset = float(self.params.start_offset_seconds)
        now = self.clock.utc_now()
        now_s = now.timestamp()
        k = math.floor((now_s - offset) / interval) + 1
        boundary_s = k * interval + offset
        if boundary_s - now_s < 1.0:
            boundary_s += interval
        return pd.Timestamp(boundary_s, unit="s", tz="UTC")

    def _boundary_for(self, shot_index: int) -> pd.Timestamp:
        assert self._first_boundary is not None
        return self._first_boundary + pd.Timedelta(
            seconds=shot_index * self.params.interval_seconds()
        )

    def _arm_shot_alert(self, shot_index: int) -> None:
        name = f"{self._SHOT_PREFIX}{shot_index}"
        alert_time = self._boundary_for(shot_index)
        try:
            if hasattr(self.clock, "cancel_time_alert"):
                self.clock.cancel_time_alert(name=name)
        except Exception:
            pass
        self.clock.set_time_alert(name=name, alert_time=alert_time)
        self.log.info(f"Shot {shot_index} armed for {alert_time}")

    def _arm_final_alert(self) -> None:
        """One interval after the last boundary: close out and stop."""
        alert_time = self._boundary_for(self.params.num_shots)
        try:
            if hasattr(self.clock, "cancel_time_alert"):
                self.clock.cancel_time_alert(name=self._FINAL_ALERT)
        except Exception:
            pass
        self.clock.set_time_alert(name=self._FINAL_ALERT, alert_time=alert_time)

    # ------------------------------------------------------------------
    # Quote handling -- per-tick HOT PATH.
    # Work done here: cache update, bounded-list appends, one flag check.
    # No observe/act/decide, no _record_event, no I/O, no sorting.
    # ------------------------------------------------------------------
    def on_symbol_quote_tick(self, symbol: str, tick: QuoteTick) -> None:
        if self.isPaused():
            return
        t_mono = time.monotonic_ns()
        t_wall = time.time_ns()
        try:
            self._last_bid = float(tick.bid_price) if tick.bid_price is not None else None
            self._last_ask = float(tick.ask_price) if tick.ask_price is not None else None
        except Exception:
            return
        self._last_quote_mono_ns = t_mono

        # --- Market-data latency capture (Charter intervals 1 and 2) ---
        if self._md_enabled:
            if self._md_prev_mono_ns:
                gap_ms = (t_mono - self._md_prev_mono_ns) / 1e6
                if gap_ms > self._md_max_gap_ms_window:
                    self._md_max_gap_ms_window = gap_ms
            self._md_prev_mono_ns = t_mono
            self._md_count_window += 1
            self._md_count_run += 1
            if len(self._md_feed_ms_window) < self._MD_WINDOW_CAP:
                ev = getattr(tick, "ts_event", 0) or 0
                if ev:
                    feed_ms = (t_wall - ev) / 1e6
                    self._md_feed_ms_window.append(feed_ms)
                    if len(self._md_feed_ms_run) < self._MD_RUN_CAP:
                        self._md_feed_ms_run.append(feed_ms)
                init = getattr(tick, "ts_init", 0) or 0
                if init:
                    self._md_int_ms_window.append((t_wall - init) / 1e6)

        # --- Tick-trigger mode: launch the armed shot on this tick ---
        idx = self._await_tick_shot
        if idx is not None:
            self._await_tick_shot = None
            ev = getattr(tick, "ts_event", 0) or t_wall
            self._launch_shot(idx, trigger_event_ns=int(ev))

    def on_symbol_trade_tick(self, symbol: str, tick: TradeTick) -> None:
        pass

    # ------------------------------------------------------------------
    # Time alerts: shot boundaries, deadlines, final closeout
    # ------------------------------------------------------------------
    def on_event(self, event: Event) -> None:
        name = getattr(event, "name", None)
        if not name or not isinstance(name, str):
            return

        if name.startswith(self._SHOT_PREFIX):
            try:
                idx = int(name[len(self._SHOT_PREFIX):])
            except ValueError:
                return
            self._begin_shot(idx)
            return

        if name.startswith(self._DEADLINE_PREFIX):
            try:
                idx = int(name[len(self._DEADLINE_PREFIX):])
            except ValueError:
                return
            self._on_deadline(idx)
            return

        if name == self._FINAL_ALERT:
            self._finalize()
            return

    # ------------------------------------------------------------------
    # Shot lifecycle
    # ------------------------------------------------------------------
    def _side_for_shot(self, shot_index: int) -> OrderSide:
        if not self.params.alternate_sides or shot_index % 2 == 0:
            return self._base_side
        return (
            OrderSide.SELL if self._base_side == OrderSide.BUY else OrderSide.BUY
        )

    def _begin_shot(self, shot_index: int) -> None:
        if shot_index >= len(self._shots):
            return

        t_boundary = time.monotonic_ns()
        t_boundary_wall = time.time_ns()

        # 1) Hard cleanup of the previous shot if it is somehow still live
        #    (includes an AWAIT_TICK shot whose trigger never arrived --
        #    that one closes as SKIPPED, not INCOMPLETE).
        self._await_tick_shot = None
        prev = self._current_shot
        if prev is not None and prev.is_live():
            if prev.status == SHOT_AWAIT_TICK:
                prev.status = SHOT_SKIPPED
                prev.closed = True
                self._record_event(
                    "SHOT_SKIPPED",
                    shot_index=prev.index,
                    boundary_utc=str(self._boundary_for(prev.index)),
                    reason="no quote tick arrived during window (tick mode)",
                )
                self._flush_event_log()
            else:
                self._force_close_shot(prev, reason="next boundary reached")

        shot = self._shots[shot_index]
        shot.t_boundary_ns = t_boundary
        nominal_ns = int(self._boundary_for(shot_index).value)
        shot.boundary_jitter_ms = round((t_boundary_wall - nominal_ns) / 1e6, 3)
        self._current_shot = shot

        # 2) Arm the next boundary FIRST so the schedule survives anything
        #    that happens inside this shot.
        next_index = shot_index + 1
        if next_index < self.params.num_shots:
            self._arm_shot_alert(next_index)
        else:
            self._arm_final_alert()

        # 3) Tick-trigger mode: arm and wait for the next quote tick.
        if self.params.trigger_mode == "tick":
            shot.status = SHOT_AWAIT_TICK
            self._await_tick_shot = shot_index
            return

        # 4) Timer mode: no usable quote -> skip cleanly, keep the schedule.
        if self._last_bid is None or self._last_ask is None:
            shot.status = SHOT_SKIPPED
            shot.closed = True
            self._record_event(
                "SHOT_SKIPPED",
                shot_index=shot_index,
                boundary_utc=str(self._boundary_for(shot_index)),
                reason="no valid quote cached at boundary",
            )
            self._flush_event_log()
            self.observe(
                "Shot skipped: no valid quote cached at boundary",
                context={
                    "shot_index": shot_index,
                    "boundary_utc": str(self._boundary_for(shot_index)),
                },
            )
            return

        self._launch_shot(shot_index, trigger_event_ns=0)

    def _launch_shot(self, shot_index: int, trigger_event_ns: int) -> None:
        """Passive-phase launch. Shared by timer mode (called from the
        boundary alert) and tick mode (called from the triggering tick).
        Between entry and the submit call there is only price computation
        -- no I/O, no narration."""
        if shot_index >= len(self._shots):
            return
        shot = self._shots[shot_index]
        if shot.closed:
            return

        if self._last_bid is None or self._last_ask is None:
            shot.status = SHOT_SKIPPED
            shot.closed = True
            self._record_event(
                "SHOT_SKIPPED",
                shot_index=shot_index,
                reason="no valid quote at launch",
            )
            self._flush_event_log()
            return

        t_launch = time.monotonic_ns()
        side = self._side_for_shot(shot_index)
        qty = int(self.params.shot_quantity)

        shot.side = side
        shot.quantity = float(qty)
        shot.remaining = float(qty)
        shot.status = SHOT_ACTIVE
        shot.trigger_event_ns = trigger_event_ns
        if self._last_quote_mono_ns > 0:
            shot.quote_age_at_decision_ms = round(
                (t_launch - self._last_quote_mono_ns) / 1e6, 3
            )

        bid = self._last_bid
        ask = self._last_ask

        # Normal mode: BUY rests at BID; SELL rests at ASK.
        # Measure mode: rest BEHIND the inside so the order never fills and
        # the timeout -> cancel -> IOC sequence runs deterministically.
        if self.params.measure_mode:
            m_off = float(self.params.measure_offset)
            px_num = (bid - m_off) if side == OrderSide.BUY else (ask + m_off)
        else:
            px_num = bid if side == OrderSide.BUY else ask
        px = Price.from_str(f"{px_num:.4f}")

        shot.t_submit_wall_ns = time.time_ns()
        shot.t_passive_submit_ns = time.monotonic_ns()
        try:
            order = self.submit_limit_order(
                symbol=self.symbol,
                side=side,
                qty=qty,
                price=px,
                tif=self._tif,
                leg_id="A",
                role="PASSIVE",
            )
        except Exception as e:
            shot.status = SHOT_INCOMPLETE
            self._record_event(
                "SUBMISSION_FAILED",
                shot_index=shot_index,
                phase="PASSIVE",
                failure_reason=str(e),
            )
            self._close_shot(shot)
            self.log.error(f"Shot {shot_index}: passive submit failed: {e}")
            return

        oid = self._oid_str(order.client_order_id)
        shot.passive_oid = oid
        shot.passive_oid_obj = order.client_order_id
        self._oid_to_shot[oid] = shot

        bts = _ms(shot.t_boundary_ns, shot.t_passive_submit_ns)
        if bts is not None:
            self._boundary_to_submit_ms.append(bts)

        evt_to_submit_ms = None
        if trigger_event_ns > 0:
            evt_to_submit_ms = round(
                (shot.t_submit_wall_ns - trigger_event_ns) / 1e6, 3
            )

        # Narration and event capture AFTER the submit: the measured
        # trigger->submit interval contains zero instrumentation.
        self._record_event(
            "PASSIVE_SUBMITTED",
            shot_index=shot_index,
            client_order_id=oid,
            boundary_utc=str(self._boundary_for(shot_index)),
            side=side.name,
            qty=qty,
            limit_price=float(px),
            bid=bid,
            ask=ask,
            spread=round(ask - bid, 6),
            tif=self._tif.name,
            trigger_mode=self.params.trigger_mode,
            measure_mode=self.params.measure_mode,
            boundary_to_submit_ms=bts,
            boundary_jitter_ms=shot.boundary_jitter_ms,
            quote_age_at_decision_ms=shot.quote_age_at_decision_ms,
            market_event_to_submit_ms=evt_to_submit_ms,
        )
        self.decide(
            "Shot launched, passive order submitted",
            context={
                "shot_index": shot_index,
                "order_id": oid,
                "side": side.name,
                "price": float(px),
                "quantity": qty,
                "bid": bid,
                "ask": ask,
                "trigger_mode": self.params.trigger_mode,
                "measure_mode": self.params.measure_mode,
            },
        )

    def _on_deadline(self, shot_index: int) -> None:
        if shot_index >= len(self._shots):
            return
        shot = self._shots[shot_index]
        if shot.status != SHOT_ACTIVE:
            return  # already complete / cleaned / crossing
        if shot.remaining <= 0:
            return

        shot.t_deadline_ns = time.monotonic_ns()
        self._record_event(
            "DEADLINE_FIRED",
            shot_index=shot_index,
            remaining_qty=shot.remaining,
            bid=self._last_bid,
            ask=self._last_ask,
        )
        self.decide(
            "Passive order timed out, switching to cross",
            context={
                "shot_index": shot_index,
                "remaining_qty": shot.remaining,
                "last_bid": self._last_bid,
                "last_ask": self._last_ask,
            },
        )
        self._cross_shot(shot)

    def _cross_shot(self, shot: ShotRecord) -> None:
        if shot.cross_submitted:
            return  # single-fire guard
        if self._last_bid is None or self._last_ask is None:
            self.log.warning(
                f"Shot {shot.index}: no cached quote at deadline; cannot cross."
            )
            shot.status = SHOT_UNFILLED if shot.passive_fill_qty <= 0 else SHOT_PARTIAL
            self._cancel_shot_orders(shot)
            self._close_shot(shot)
            return

        side = shot.side or self._base_side
        qty = float(shot.remaining)
        if qty <= 0:
            return

        shot.cross_submitted = True
        shot.status = SHOT_CROSSING

        offset = float(self.params.cross_offset)
        bid = self._last_bid
        ask = self._last_ask

        # BUY crosses above ASK; SELL crosses below BID
        if side == OrderSide.BUY:
            cross_px = ask + offset
        else:
            cross_px = bid - offset

        if shot.passive_oid_obj is not None:
            shot.t_cancel_req_ns = time.monotonic_ns()
            if self.cancel_order_by_id(shot.passive_oid_obj):
                shot.cancel_initiated = True
                self._record_event("CANCEL_SUBMITTED",
                                shot_index=shot.index,
                                client_order_id=shot.passive_oid)
            else:
                shot.t_cancel_req_ns = 0   # nothing in flight; don't fake a request time
                self._record_event("CANCEL_NOT_ISSUED",
                                shot_index=shot.index,
                                client_order_id=shot.passive_oid)

        cross_price = Price.from_str(f"{cross_px:.4f}")

        shot.t_cross_submit_ns = time.monotonic_ns()
        try:
            order = self.submit_limit_order(
                symbol=self.symbol,
                side=side,
                qty=int(qty),
                price=cross_price,
                tif=TimeInForce.IOC,
                leg_id="A",
                role="CROSS",
            )
        except Exception as e:
            shot.status = SHOT_INCOMPLETE
            self._record_event(
                "SUBMISSION_FAILED",
                shot_index=shot.index,
                phase="CROSS",
                failure_reason=str(e),
            )
            self._close_shot(shot)
            self.log.error(f"Shot {shot.index}: cross submit failed: {e}")
            return

        oid = self._oid_str(order.client_order_id)
        shot.cross_oid = oid
        shot.cross_oid_obj = order.client_order_id
        self._oid_to_shot[oid] = shot

        self._record_event(
            "CROSS_SUBMITTED",
            shot_index=shot.index,
            client_order_id=oid,
            side=side.name,
            qty=qty,
            limit_price=float(cross_price),
            cross_offset=offset,
            bid=bid,
            ask=ask,
            deadline_to_cross_submit_ms=_ms(
                shot.t_deadline_ns, shot.t_cross_submit_ns
            ),
        )
        self.act(
            "Submitted crossing order",
            context={
                "shot_index": shot.index,
                "order_id": oid,
                "side": side.name,
                "quantity": qty,
                "price": float(cross_price),
                "time_in_force": "IOC",
            },
        )

    def _cancel_shot_orders(self, shot: ShotRecord) -> None:
        """Best-effort cleanup cancel of any live orders for this shot
        (pause, force-close, no-quote-at-deadline). Uses the base-class
        cancel_order_by_id; only stamps cancel-measurement fields when a
        cancel command was actually issued."""
        any_issued = False
        for oid_obj, label in (
            (shot.passive_oid_obj, "PASSIVE"),
            (shot.cross_oid_obj, "CROSS"),
        ):
            if oid_obj is None:
                continue
            t_req = time.monotonic_ns()
            try:
                issued = self.cancel_order_by_id(oid_obj)
            except Exception as e:
                self.log.error(
                    f"Shot {shot.index}: cleanup cancel failed for "
                    f"{label} {self._oid_str(oid_obj)}: {e}"
                )
                self._record_event(
                    "CANCEL_FAILED",
                    shot_index=shot.index,
                    client_order_id=self._oid_str(oid_obj),
                    phase=label,
                    failure_reason=str(e),
                )
                continue
            if issued:
                any_issued = True
                # Stamp the request time only for the passive: that's the
                # order whose cancel round trip feeds the measurement column.
                if label == "PASSIVE" and shot.t_cancel_req_ns == 0:
                    shot.t_cancel_req_ns = t_req
                self._record_event(
                    "CANCEL_SUBMITTED",
                    shot_index=shot.index,
                    client_order_id=self._oid_str(oid_obj),
                    phase=label,
                )
            else:
                self._record_event(
                    "CANCEL_NOT_ISSUED",
                    shot_index=shot.index,
                    client_order_id=self._oid_str(oid_obj),
                    phase=label,
                    reason="order not found or not open",
                )

        if any_issued:
            shot.cancel_initiated = True

        try:
            name = f"{self._DEADLINE_PREFIX}{shot.index}"
            if hasattr(self.clock, "cancel_time_alert"):
                self.clock.cancel_time_alert(name=name)
        except Exception:
            pass

    def _force_close_shot(self, shot: ShotRecord, reason: str) -> None:
        self._record_event(
            "SHOT_FORCE_CLOSED",
            shot_index=shot.index,
            reason=reason,
            prior_status=shot.status,
            remaining_qty=shot.remaining,
        )
        self.observe(
            "Force-closing live shot",
            context={
                "shot_index": shot.index,
                "reason": reason,
                "prior_status": shot.status,
                "remaining_qty": shot.remaining,
            },
        )
        self._cancel_shot_orders(shot)
        shot.status = SHOT_INCOMPLETE
        self._close_shot(shot)

    def _settle_shot_at_close(self, shot: ShotRecord) -> None:
        """Terminal status for a shot whose lifecycle has resolved."""
        if shot.remaining <= 0:
            shot.status = SHOT_COMPLETE
        elif shot.passive_fill_qty + shot.cross_fill_qty > 0:
            shot.status = SHOT_PARTIAL
        else:
            shot.status = SHOT_UNFILLED
        self._close_shot(shot)

    def _close_shot(self, shot: ShotRecord) -> None:
        """Record the SHOT_CLOSED interval decomposition plus the MD stats
        window, then flush the event buffer. Runs in the dead air after
        the shot resolves -- this is the designated I/O window."""
        if shot.closed:
            return
        shot.closed = True
        shot.t_close_ns = time.monotonic_ns()

        if shot.t_cross_submit_ns > 0 and shot.t_cross_terminal_ns > 0:
            crt = _ms(shot.t_cross_submit_ns, shot.t_cross_terminal_ns)
            if crt is not None:
                self._cross_roundtrip_ms.append(crt)

        evt_to_ack_ms = None
        if shot.trigger_event_ns > 0 and shot.t_passive_ack_wall_ns > 0:
            evt_to_ack_ms = round(
                (shot.t_passive_ack_wall_ns - shot.trigger_event_ns) / 1e6, 3
            )

        self._record_event(
            "SHOT_CLOSED",
            shot_index=shot.index,
            final_status=shot.status,
            side=(shot.side.name if shot.side else None),
            quantity=shot.quantity,
            passive_fill_qty=shot.passive_fill_qty,
            cross_fill_qty=shot.cross_fill_qty,
            unfilled_qty=shot.remaining,
            trigger_mode=self.params.trigger_mode,
            measure_mode=self.params.measure_mode,
            # --- interval decomposition (ms; None = leg never happened) ---
            boundary_to_submit_ms=_ms(shot.t_boundary_ns, shot.t_passive_submit_ns),
            submit_to_ack_ms=_ms(shot.t_passive_submit_ns, shot.t_passive_ack_ns),
            ack_to_first_fill_ms=_ms(shot.t_passive_ack_ns, shot.t_first_fill_ns),
            submit_to_deadline_ms=_ms(shot.t_passive_submit_ns, shot.t_deadline_ns),
            deadline_to_cross_submit_ms=_ms(shot.t_deadline_ns, shot.t_cross_submit_ns),
            cancel_roundtrip_ms=_ms(shot.t_cancel_req_ns, shot.t_cancel_ack_ns),
            cross_submit_to_ack_ms=_ms(shot.t_cross_submit_ns, shot.t_cross_ack_ns),
            cross_submit_to_terminal_ms=_ms(
                shot.t_cross_submit_ns, shot.t_cross_terminal_ns
            ),
            shot_duration_ms=_ms(shot.t_boundary_ns, shot.t_close_ns),
            # --- cross-clock and context measurements ---
            market_event_to_ack_ms=evt_to_ack_ms,
            quote_age_at_decision_ms=shot.quote_age_at_decision_ms,
            boundary_jitter_ms=shot.boundary_jitter_ms,
        )

        # --- MD stats for the window since the previous shot close ---
        if self._md_enabled:
            self._record_event(
                "MD_STATS",
                shot_index=shot.index,
                tick_count=self._md_count_window,
                feed_latency=self._percentiles(self._md_feed_ms_window),
                internal_latency=self._percentiles(self._md_int_ms_window),
                max_inter_tick_gap_ms=round(self._md_max_gap_ms_window, 3),
                window_truncated=(
                    len(self._md_feed_ms_window) >= self._MD_WINDOW_CAP
                ),
            )
            self._md_feed_ms_window = []
            self._md_int_ms_window = []
            self._md_count_window = 0
            self._md_max_gap_ms_window = 0.0

        self._flush_event_log()

        self.observe(
            "Shot closed",
            context={
                "shot_index": shot.index,
                "final_status": shot.status,
                "passive_fill_qty": shot.passive_fill_qty,
                "cross_fill_qty": shot.cross_fill_qty,
                "unfilled_qty": shot.remaining,
            },
        )

    def _finalize(self) -> None:
        shot = self._current_shot
        if shot is not None and shot.is_live():
            if shot.status == SHOT_AWAIT_TICK:
                shot.status = SHOT_SKIPPED
                shot.closed = True
                self._record_event(
                    "SHOT_SKIPPED",
                    shot_index=shot.index,
                    reason="no quote tick arrived during window (tick mode)",
                )
                self._flush_event_log()
            else:
                self._force_close_shot(shot, reason="final closeout")
        self._await_tick_shot = None

        summary: Dict[str, int] = {}
        for s in self._shots:
            summary[s.status] = summary.get(s.status, 0) + 1
        launched = sum(
            n for status, n in summary.items()
            if status not in (SHOT_SKIPPED, SHOT_PENDING)
        )

        self.observe(
            "All shot windows elapsed",
            context={"shots_total": len(self._shots), "shots_by_status": summary},
        )

        if launched == 0:
            # Nothing measured: do not report success.
            self.forced_stop(
                f"No shots launched ({summary}) — no market data?",
                stop_reason="NO_DATA",
            )
        else:
            self.complete(f"Shots executed: {summary}")

    # ------------------------------------------------------------------
    # Order events
    # ------------------------------------------------------------------
    def on_order_accepted(self, event: Any) -> None:
        t_ack = time.monotonic_ns()
        t_ack_wall = time.time_ns()
        oid = self._oid_str(event.client_order_id)
        if not oid:
            return
        shot = self._oid_to_shot.get(oid)
        if shot is None:
            return

        # --- Cross (IOC) acknowledgment ---
        if oid == shot.cross_oid:
            if shot.t_cross_ack_ns == 0:
                shot.t_cross_ack_ns = t_ack
                ack_ms = _ms(shot.t_cross_submit_ns, t_ack)
                if ack_ms is not None:
                    self._cross_ack_latencies_ms.append(ack_ms)
                self._record_event(
                    "CROSS_ACCEPTED",
                    shot_index=shot.index,
                    client_order_id=oid,
                    submit_to_ack_ms=ack_ms,
                )
            return

        if oid != shot.passive_oid:
            return
        if shot.deadline_armed or shot.status != SHOT_ACTIVE:
            return

        # --- Passive acknowledgment ---
        shot.t_passive_ack_ns = t_ack
        shot.t_passive_ack_wall_ns = t_ack_wall
        ack_ms = _ms(shot.t_passive_submit_ns, t_ack)
        if ack_ms is not None:
            self._passive_ack_latencies_ms.append(ack_ms)

        evt_to_ack_ms = None
        if shot.trigger_event_ns > 0:
            evt_to_ack_ms = round(
                (t_ack_wall - shot.trigger_event_ns) / 1e6, 3
            )
            self._event_to_ack_ms.append(evt_to_ack_ms)

        shot.deadline_armed = True
        self._arm_deadline_for(shot)

        self._record_event(
            "PASSIVE_ACCEPTED",
            shot_index=shot.index,
            client_order_id=oid,
            submit_to_ack_ms=ack_ms,
            market_event_to_ack_ms=evt_to_ack_ms,
        )
        self.observe(
            "Passive order accepted, deadline armed",
            context={
                "shot_index": shot.index,
                "order_id": oid,
                "submit_to_ack_ms": ack_ms,
                "market_event_to_ack_ms": evt_to_ack_ms,
                "timeout_seconds": self.params.timeout_seconds,
            },
        )

    def _arm_deadline_for(self, shot: ShotRecord) -> None:
        secs = float(self.params.timeout_seconds)
        alert_time = self.clock.utc_now() + pd.Timedelta(seconds=secs)
        name = f"{self._DEADLINE_PREFIX}{shot.index}"

        try:
            if hasattr(self.clock, "cancel_time_alert"):
                self.clock.cancel_time_alert(name=name)
        except Exception:
            pass

        self.clock.set_time_alert(name=name, alert_time=alert_time)
        self.log.info(
            f"Deadline armed shot={shot.index} oid={shot.passive_oid} "
            f"timeout={secs:.1f}s at={alert_time}"
        )

    def on_order_filled(self, event: Any) -> None:
        t_fill = time.monotonic_ns()
        oid = self._oid_str(event.client_order_id)
        shot = self._oid_to_shot.get(oid) if oid else None
        if shot is None:
            return

        leg_id, role = self.extract_leg_info_from_order_id(oid)  # satisfies pattern #12

        last_qty = float(getattr(event, "last_qty", 0.0))
        last_px = getattr(event, "last_px", None)

        shot.remaining = max(0.0, shot.remaining - last_qty)

        is_cross = (oid == shot.cross_oid)
        if is_cross:
            shot.cross_fill_qty += last_qty
        else:
            shot.passive_fill_qty += last_qty
            if shot.t_first_fill_ns == 0:
                shot.t_first_fill_ns = t_fill

        fill_type = "cross" if is_cross else "passive"

        self._record_event(
            "ORDER_FILLED",
            shot_index=shot.index,
            client_order_id=oid,
            fill_type=fill_type,
            fill_qty=last_qty,
            fill_price=(float(last_px) if last_px else None),
            remaining_qty=shot.remaining,
        )
        self.act(
            "Crossed market" if is_cross else "Filled passively at inside price",
            context={
                "shot_index": shot.index,
                "fill_type": fill_type,
                "fill_price": float(last_px) if last_px else None,
                "fill_qty": last_qty,
                "remaining_qty": shot.remaining,
            },
        )

        if shot.remaining <= 0.0:
            if is_cross:
                shot.t_cross_terminal_ns = t_fill
            # Full fill during the passive phase: disarm the deadline so
            # the cross never fires for a completed shot.
            if not shot.cross_submitted:
                try:
                    name = f"{self._DEADLINE_PREFIX}{shot.index}"
                    if hasattr(self.clock, "cancel_time_alert"):
                        self.clock.cancel_time_alert(name=name)
                except Exception:
                    pass
            self._settle_shot_at_close(shot)
            # NOTE: no stop() here -- the schedule owns the lifecycle.

    def on_order_canceled(self, event: Any) -> None:
        t_cancel = time.monotonic_ns()
        oid = self._oid_str(event.client_order_id)
        shot = self._oid_to_shot.get(oid) if oid else None
        if shot is None:
            return

        self._record_event(
            "ORDER_CANCELED",
            shot_index=shot.index,
            client_order_id=oid,
            was_cross=(oid == shot.cross_oid),
            cancel_initiated_by_strategy=shot.cancel_initiated,
        )

        if oid == shot.cross_oid:
            # IOC came back with its unfilled remainder: shot is over.
            shot.t_cross_terminal_ns = t_cancel
            if shot.status == SHOT_CROSSING:
                self._settle_shot_at_close(shot)
            return

        if oid == shot.passive_oid:
            if shot.cancel_initiated or shot.cross_submitted:
                # Our own cancel as part of crossing / cleanup: measure the
                # cancel round trip (request -> confirmation).
                if shot.t_cancel_ack_ns == 0 and shot.t_cancel_req_ns > 0:
                    shot.t_cancel_ack_ns = t_cancel
                    crt_ms = _ms(shot.t_cancel_req_ns, t_cancel)
                    if crt_ms is not None:
                        self._cancel_roundtrip_ms.append(crt_ms)
                    self._record_event(
                        "CANCEL_CONFIRMED",
                        shot_index=shot.index,
                        client_order_id=oid,
                        cancel_roundtrip_ms=crt_ms,
                    )
                return
            if shot.status == SHOT_ACTIVE and shot.remaining > 0:
                # Externally cancelled (e.g. exchange session transition):
                # cross what remains, preserving the original behavior.
                self.decide(
                    "Passive order cancelled externally, crossing remainder",
                    context={
                        "shot_index": shot.index,
                        "remaining_qty": shot.remaining,
                    },
                )
                self._cross_shot(shot)

    def on_order_rejected(self, event: Any) -> None:
        t_reject = time.monotonic_ns()
        oid = self._oid_str(event.client_order_id)
        shot = self._oid_to_shot.get(oid) if oid else None
        reason = str(getattr(event, "reason", ""))
        self.log.error("Order rejected oid=%s reason=%s", oid, reason)

        reject_latency_ms = None
        if shot is not None:
            ref = (
                shot.t_cross_submit_ns
                if oid == shot.cross_oid
                else shot.t_passive_submit_ns
            )
            reject_latency_ms = _ms(ref, t_reject)

        self._record_event(
            "ORDER_REJECTED",
            shot_index=(shot.index if shot else None),
            client_order_id=oid,
            reason=reason,
            reject_latency_ms=reject_latency_ms,
        )
        if shot is None:
            return
        # A rejection ends THIS shot only; the schedule continues. Rejects
        # are data for the pilot (e.g. auction phase), not a reason to
        # abort the measurement series.
        shot.status = SHOT_INCOMPLETE
        try:
            name = f"{self._DEADLINE_PREFIX}{shot.index}"
            if hasattr(self.clock, "cancel_time_alert"):
                self.clock.cancel_time_alert(name=name)
        except Exception:
            pass
        self._close_shot(shot)
