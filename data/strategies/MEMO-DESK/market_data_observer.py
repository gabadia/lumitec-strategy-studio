"""
MarketDataObserver — Lumitec Strategy
Passive market-data collection, CSV writing, and ODA statistics reporting.
No orders are ever submitted.
"""

import time
from dataclasses import dataclass, replace, fields as dc_fields
from datetime import datetime, timezone, timedelta
from threading import RLock

from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective
from nautilus_trader.model.enums import BarAggregation, PriceType


# ── 1. Config ────────────────────────────────────────────────────────────────

class Config(LumitecStrategyConfig):
    strategy_name: str = "MarketDataObserver"
    file_name: str = "market_data_observer.py"

    # What to collect
    collect_quotes: bool = True
    collect_trades: bool = True

    # Collection window (ISO-8601 naive strings — interpreted in collection_timezone)
    collection_start_time: str = "2026-07-17T10:00:00"
    collection_end_time: str = "2026-07-17T10:10:00"
    collection_timezone: str = "America/New_York"

    # Output
    output_file: str = "/var/log/market-data-observer/aapl-test.csv"

    # Intervals
    flush_interval_seconds: float = 1.0
    oda_stats_interval_seconds: float = 10.0

    # Behaviour
    send_final_stats_via_oda: bool = True

    # Tick handler throttle (seconds between ODA/observe calls)
    tick_throttle_interval: float = 1.0


# ── 2. ConfigParams ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConfigParams:
    collect_quotes: bool = True
    collect_trades: bool = True
    collection_start_time: str = "2026-07-17T10:00:00"
    collection_end_time: str = "2026-07-17T10:10:00"
    collection_timezone: str = "America/New_York"
    output_file: str = "/var/log/market-data-observer/aapl-test.csv"
    flush_interval_seconds: float = 1.0
    oda_stats_interval_seconds: float = 10.0
    send_final_stats_via_oda: bool = True
    tick_throttle_interval: float = 1.0

    def validate(self) -> None:
        if not self.collect_quotes and not self.collect_trades:
            raise ValueError("At least one of collect_quotes or collect_trades must be True")
        if self.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be > 0")
        if self.oda_stats_interval_seconds <= 0:
            raise ValueError("oda_stats_interval_seconds must be > 0")
        if self.tick_throttle_interval <= 0:
            raise ValueError("tick_throttle_interval must be > 0")
        start_utc = _parse_window_time(self.collection_start_time, self.collection_timezone)
        end_utc = _parse_window_time(self.collection_end_time, self.collection_timezone)
        if end_utc <= start_utc:
            raise ValueError("collection_end_time must be after collection_start_time")

    @classmethod
    def from_config(cls, cfg) -> "ConfigParams":
        values = {f.name: getattr(cfg, f.name, f.default) for f in dc_fields(cls)}
        params = cls(**values)
        params.validate()
        return params

    def merged(self, updates: dict) -> "ConfigParams":
        allowed = {f.name: f for f in dc_fields(self)}
        coerced: dict = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            ft = allowed[k].type
            if ft in (float, "float"):
                v = float(v)
            elif ft in (bool, "bool"):
                if isinstance(v, str):
                    v = v.lower() in ("true", "1", "yes")
                else:
                    v = bool(v)
            coerced[k] = v
        new = replace(self, **coerced)
        new.validate()
        return new


# ── 3. Module-level helpers ───────────────────────────────────────────────────

def _parse_window_time(time_str: str, tz_name: str) -> float:
    """
    Parse a naive ISO-8601 datetime string, attach the named timezone,
    and return a UTC POSIX timestamp (float seconds).
    """
    dt_naive = datetime.fromisoformat(time_str)
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        tz = ZoneInfo(tz_name)
        dt_aware = dt_naive.replace(tzinfo=tz)
    except Exception:
        _OFFSETS = {
            "America/New_York": -4,
            "America/Chicago": -5,
            "America/Los_Angeles": -7,
            "UTC": 0,
        }
        offset_h = _OFFSETS.get(tz_name, 0)
        tz = timezone(timedelta(hours=offset_h))
        dt_aware = dt_naive.replace(tzinfo=tz)
    return dt_aware.timestamp()


def _utc_iso(ts_s: float) -> str:
    """Return a UTC ISO-8601 string from a POSIX timestamp."""
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_CSV_HEADER = (
    "event_number,event_type,instrument_id,"
    "source_timestamp_ns,strategy_receive_timestamp_ns,source_sequence,"
    "bid_price,bid_size,ask_price,ask_size,"
    "trade_price,trade_size,trade_id\n"
)


# ── 4. Strategy ──────────────────────────────────────────────────────────────

class MarketDataObserver(LumitecBaseStrategy):
    """
    Passive market-data collection strategy.

    Subscribes to quotes and/or trades for the instrument provided via the
    single declared leg.  Writes every event that falls inside the configured
    UTC window to a CSV file.  Sends only aggregate statistics through ODA.
    Never trades.
    """

    mission   = StrategyMission.SPECULATIVE
    objective = StrategyObjective.SIGNAL_DRIVEN
    leg_mode  = LegMode.FINITE

    leg_schema = [
        {"label": "Instrument", "side": None, "fixed_side": False},
    ]

    # ------------------------------------------------------------------ #
    #  Construction                                                        #
    # ------------------------------------------------------------------ #

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = ConfigParams.from_config(config)
        self._param_lock = RLock()

        self._symbol: str = ""

        self._window_start_utc: float = _parse_window_time(
            self.params.collection_start_time,
            self.params.collection_timezone,
        )
        self._window_end_utc: float = _parse_window_time(
            self.params.collection_end_time,
            self.params.collection_timezone,
        )

        # Track which subscriptions were actually registered so teardown
        # mirrors setup exactly — even if params change between start/stop.
        self._subscribed_quotes: bool = False
        self._subscribed_trades: bool = False

        self._collecting: bool = False
        self._collection_done: bool = False

        self._csv_file = None

        self._last_tick_ts: float = 0.0
        self._last_flush_ts: float = 0.0
        self._last_oda_ts: float = 0.0

        self._event_counter: int = 0

        # Quote statistics
        self._quotes_received: int = 0
        self._first_quote_recv_ns: int = 0
        self._last_quote_recv_ns: int = 0
        self._min_bid: float = float("inf")
        self._max_bid: float = float("-inf")
        self._min_ask: float = float("inf")
        self._max_ask: float = float("-inf")
        self._min_spread: float = float("inf")
        self._max_spread: float = float("-inf")
        self._sum_spread: float = 0.0
        self._quote_latency_count: int = 0
        self._min_quote_latency_ns: int = 0
        self._max_quote_latency_ns: int = 0
        self._sum_quote_latency_ns: int = 0

        # Trade statistics
        self._trades_received: int = 0
        self._first_trade_recv_ns: int = 0
        self._last_trade_recv_ns: int = 0
        self._min_trade_price: float = float("inf")
        self._max_trade_price: float = float("-inf")
        self._total_trade_volume: float = 0.0
        self._sum_pv: float = 0.0
        self._trade_latency_count: int = 0
        self._min_trade_latency_ns: int = 0
        self._max_trade_latency_ns: int = 0
        self._sum_trade_latency_ns: int = 0

        # General statistics
        self._total_events_recorded: int = 0
        self._file_records_written: int = 0
        self._file_write_errors: int = 0
        self._oda_messages_sent: int = 0
        self._oda_send_errors: int = 0

        self._collection_start_utc_reported: float = self._window_start_utc
        self._collection_end_utc_reported: float = self._window_end_utc

    # ------------------------------------------------------------------ #
    #  OMS type                                                            #
    # ------------------------------------------------------------------ #

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    # ------------------------------------------------------------------ #
    #  Leg validation                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def validate_legs(cls, legs: list) -> None:
        if len(legs) != 1:
            raise ValueError(
                f"MarketDataObserver requires exactly 1 leg (the instrument to observe). "
                f"Got {len(legs)}."
            )

    # ------------------------------------------------------------------ #
    #  Parameter hot-update                                                #
    # ------------------------------------------------------------------ #

    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)
            self._window_start_utc = _parse_window_time(
                self.params.collection_start_time,
                self.params.collection_timezone,
            )
            self._window_end_utc = _parse_window_time(
                self.params.collection_end_time,
                self.params.collection_timezone,
            )

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        super().on_start()

        self.leg_a = self.legs[0]
        self._symbol = self.leg_a["symbol"]

        # Subscribe and record which subscriptions were made so on_stop
        # can mirror them exactly regardless of any param changes.
        if self.params.collect_quotes:
            self.subscribe_market_data(
                self._symbol,
                subscribe_quotes=True,
                subscribe_trades=False,
            )
            self._subscribed_quotes = True

        if self.params.collect_trades:
            self.subscribe_market_data(
                self._symbol,
                subscribe_quotes=False,
                subscribe_trades=True,
            )
            self._subscribed_trades = True

        self.observe(
            "MarketDataObserver started — waiting for collection window",
            context={
                "symbol": self._symbol,
                "window_start_utc": _utc_iso(self._window_start_utc),
                "window_end_utc": _utc_iso(self._window_end_utc),
                "output_file": self.params.output_file,
                "collect_quotes": self.params.collect_quotes,
                "collect_trades": self.params.collect_trades,
            },
        )

    def on_stop(self) -> None:


        self.observe("MarketDataObserver stopped")
        # teardown must mirror setup exactly
        if self._collecting and not self._collection_done:
            self._finish_collection(status="INTERRUPTED")

        if self._symbol:
            if self._subscribed_quotes:
                try:
                    self.unsubscribe_market_data(
                        self._symbol,
                        subscribe_quotes=True,
                        subscribe_trades=False,
                    )
                except Exception as exc:
                    self.observe(
                        "Warning: could not unsubscribe quotes",
                        context={"error": str(exc)},
                    )

            if self._subscribed_trades:
                try:
                    self.unsubscribe_market_data(
                        self._symbol,
                        subscribe_quotes=False,
                        subscribe_trades=True,
                    )
                except Exception as exc:
                    self.observe(
                        "Warning: could not unsubscribe trades",
                        context={"error": str(exc)},
                    )

    def on_pause(self, reason: str = "") -> None:
        if self._collecting and self._csv_file is not None:
            try:
                self._csv_file.flush()
            except Exception:
                pass
        self.observe("Paused", context={"reason": reason})

    def on_resume(self) -> None:
        self.observe("Resumed")

    def on_order_rejected(self, event) -> None:
        self.observe(f"Order rejected: {event.client_order_id.value}")

    def on_order_canceled(self, event) -> None:
        self.observe(f"Order canceled: {event.client_order_id.value}")

    # ------------------------------------------------------------------ #
    #  Market data handlers                                                #
    # ------------------------------------------------------------------ #

    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if self.isPaused():
            return

        now_utc = time.time()

        if now_utc < self._window_start_utc:
            return

        if now_utc >= self._window_end_utc:
            if not self._collection_done:
                self._finish_collection(status="COMPLETED")
            return

        if not self._collecting:
            self._start_collection()

        recv_ns: int = time.time_ns()

        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        bid_sz = float(tick.bid_size)
        ask_sz = float(tick.ask_size)
        spread = ask - bid

        src_ns: int = int(tick.ts_event) if hasattr(tick, "ts_event") and tick.ts_event else 0
        src_seq: str = str(tick.sequence) if hasattr(tick, "sequence") and tick.sequence else ""

        # Update quote statistics
        self._quotes_received += 1
        if self._first_quote_recv_ns == 0:
            self._first_quote_recv_ns = recv_ns
        self._last_quote_recv_ns = recv_ns

        if bid < self._min_bid:
            self._min_bid = bid
        if bid > self._max_bid:
            self._max_bid = bid
        if ask < self._min_ask:
            self._min_ask = ask
        if ask > self._max_ask:
            self._max_ask = ask
        if spread < self._min_spread:
            self._min_spread = spread
        if spread > self._max_spread:
            self._max_spread = spread
        self._sum_spread += spread

        if src_ns > 0:
            latency_ns = recv_ns - src_ns
            self._quote_latency_count += 1
            if self._quote_latency_count == 1:
                self._min_quote_latency_ns = latency_ns
                self._max_quote_latency_ns = latency_ns
            else:
                if latency_ns < self._min_quote_latency_ns:
                    self._min_quote_latency_ns = latency_ns
                if latency_ns > self._max_quote_latency_ns:
                    self._max_quote_latency_ns = latency_ns
            self._sum_quote_latency_ns += latency_ns

        # Write CSV row
        self._event_counter += 1
        self._total_events_recorded += 1
        src_ns_str = str(src_ns) if src_ns > 0 else ""
        row = (
            f"{self._event_counter},QUOTE,{symbol},"
            f"{src_ns_str},{recv_ns},{src_seq},"
            f"{bid},{bid_sz},{ask},{ask_sz},"
            f",,\n"
        )
        self._write_csv_row(row)

        self._maybe_flush()
        self._maybe_send_oda_stats("ACTIVE")

    def on_symbol_trade_tick(self, symbol: str, tick) -> None:
        if self.isPaused():
            return

        now_utc = time.time()

        if now_utc < self._window_start_utc:
            return

        if now_utc >= self._window_end_utc:
            if not self._collection_done:
                self._finish_collection(status="COMPLETED")
            return

        if not self._collecting:
            self._start_collection()

        recv_ns: int = time.time_ns()

        price = float(tick.price)
        size = float(tick.size)

        src_ns: int = int(tick.ts_event) if hasattr(tick, "ts_event") and tick.ts_event else 0
        src_seq: str = str(tick.sequence) if hasattr(tick, "sequence") and tick.sequence else ""
        trade_id: str = str(tick.trade_id) if hasattr(tick, "trade_id") and tick.trade_id else ""

        # Update trade statistics
        self._trades_received += 1
        if self._first_trade_recv_ns == 0:
            self._first_trade_recv_ns = recv_ns
        self._last_trade_recv_ns = recv_ns

        if price < self._min_trade_price:
            self._min_trade_price = price
        if price > self._max_trade_price:
            self._max_trade_price = price
        self._total_trade_volume += size
        self._sum_pv += price * size

        if src_ns > 0:
            latency_ns = recv_ns - src_ns
            self._trade_latency_count += 1
            if self._trade_latency_count == 1:
                self._min_trade_latency_ns = latency_ns
                self._max_trade_latency_ns = latency_ns
            else:
                if latency_ns < self._min_trade_latency_ns:
                    self._min_trade_latency_ns = latency_ns
                if latency_ns > self._max_trade_latency_ns:
                    self._max_trade_latency_ns = latency_ns
            self._sum_trade_latency_ns += latency_ns

        # Write CSV row
        self._event_counter += 1
        self._total_events_recorded += 1
        src_ns_str = str(src_ns) if src_ns > 0 else ""
        row = (
            f"{self._event_counter},TRADE,{symbol},"
            f"{src_ns_str},{recv_ns},{src_seq},"
            f",,,,,"
            f"{price},{size},{trade_id}\n"
        )
        self._write_csv_row(row)

        self._maybe_flush()
        self._maybe_send_oda_stats("ACTIVE")

    # ------------------------------------------------------------------ #
    #  Collection management                                               #
    # ------------------------------------------------------------------ #

    def _start_collection(self) -> None:
        """Activate collection: reset stats, open CSV, write header."""
        self._collecting = True

        self._event_counter = 0
        self._quotes_received = 0
        self._first_quote_recv_ns = 0
        self._last_quote_recv_ns = 0
        self._min_bid = float("inf")
        self._max_bid = float("-inf")
        self._min_ask = float("inf")
        self._max_ask = float("-inf")
        self._min_spread = float("inf")
        self._max_spread = float("-inf")
        self._sum_spread = 0.0
        self._quote_latency_count = 0
        self._min_quote_latency_ns = 0
        self._max_quote_latency_ns = 0
        self._sum_quote_latency_ns = 0

        self._trades_received = 0
        self._first_trade_recv_ns = 0
        self._last_trade_recv_ns = 0
        self._min_trade_price = float("inf")
        self._max_trade_price = float("-inf")
        self._total_trade_volume = 0.0
        self._sum_pv = 0.0
        self._trade_latency_count = 0
        self._min_trade_latency_ns = 0
        self._max_trade_latency_ns = 0
        self._sum_trade_latency_ns = 0

        self._total_events_recorded = 0
        self._file_records_written = 0
        self._file_write_errors = 0
        self._oda_messages_sent = 0
        self._oda_send_errors = 0

        self._last_flush_ts = time.monotonic()
        self._last_oda_ts = time.monotonic()

        try:
            self._csv_file = open(
                self.params.output_file, "w", buffering=1, encoding="utf-8"
            )
            self._csv_file.write(_CSV_HEADER)
        except Exception as exc:
            self._csv_file = None
            self._file_write_errors += 1
            self.observe(
                "Failed to open CSV file",
                context={"error": str(exc), "path": self.params.output_file},
            )

        self.observe(
            "Collection started",
            context={
                "symbol": self._symbol,
                "start_utc": _utc_iso(self._window_start_utc),
                "end_utc": _utc_iso(self._window_end_utc),
                "output_file": self.params.output_file,
            },
        )

    def _finish_collection(self, status: str = "COMPLETED") -> None:
        """Stop recording, flush/close file, send final ODA stats."""
        self._collecting = False
        self._collection_done = True

        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception as exc:
                self._file_write_errors += 1
                self.observe("Error closing CSV file", context={"error": str(exc)})
            finally:
                self._csv_file = None

        if self.params.send_final_stats_via_oda:
            self._send_oda_stats(status)

        duration_s = self._window_end_utc - self._window_start_utc

        self.observe(
            f"Collection {status}",
            context={
                "total_events": self._total_events_recorded,
                "quotes": self._quotes_received,
                "trades": self._trades_received,
                "file_records_written": self._file_records_written,
                "duration_seconds": duration_s,
            },
        )

        if status == "COMPLETED":
            self.forced_stop("Collection window completed", "TIME")
        elif status == "INTERRUPTED":
            self.forced_stop("Collection interrupted before configured end time", "SYSTEM")

    # ------------------------------------------------------------------ #
    #  CSV writing                                                         #
    # ------------------------------------------------------------------ #

    def _write_csv_row(self, row: str) -> None:
        if self._csv_file is None:
            self._file_write_errors += 1
            return
        try:
            self._csv_file.write(row)
            self._file_records_written += 1
        except Exception as exc:
            self._file_write_errors += 1
            self.observe("CSV write error", context={"error": str(exc)})

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush_ts >= self.params.flush_interval_seconds:
            if self._csv_file is not None:
                try:
                    self._csv_file.flush()
                except Exception as exc:
                    self._file_write_errors += 1
                    self.observe("CSV flush error", context={"error": str(exc)})
            self._last_flush_ts = now

    # ------------------------------------------------------------------ #
    #  ODA statistics                                                      #
    # ------------------------------------------------------------------ #

    def _maybe_send_oda_stats(self, status: str) -> None:
        now = time.monotonic()
        if now - self._last_oda_ts >= self.params.oda_stats_interval_seconds:
            self._send_oda_stats(status)
            self._last_oda_ts = now

    def _send_oda_stats(self, status: str) -> None:
        """Build the aggregate statistics payload and publish via ODA."""
        duration_s = self._window_end_utc - self._window_start_utc

        avg_spread = (
            self._sum_spread / self._quotes_received
            if self._quotes_received > 0
            else None
        )
        avg_quote_latency_ns = (
            self._sum_quote_latency_ns / self._quote_latency_count
            if self._quote_latency_count > 0
            else None
        )
        vwap = (
            self._sum_pv / self._total_trade_volume
            if self._total_trade_volume > 0
            else None
        )
        avg_trade_latency_ns = (
            self._sum_trade_latency_ns / self._trade_latency_count
            if self._trade_latency_count > 0
            else None
        )

        payload: dict = {
            "message_type": "MARKET_DATA_OBSERVER_STATS",
            "strategy_name": "MarketDataObserver",
            "instrument_id": self._symbol,
            "collection_status": status,
            "collection_start_time_utc": _utc_iso(self._collection_start_utc_reported),
            "collection_end_time_utc": _utc_iso(self._collection_end_utc_reported),
            "collection_duration_seconds": duration_s,
            "quotes_received": self._quotes_received,
            "trades_received": self._trades_received,
            "total_events_recorded": self._total_events_recorded,
            "file_records_written": self._file_records_written,
            "file_write_errors": self._file_write_errors,
            "oda_messages_sent": self._oda_messages_sent,
            "oda_send_errors": self._oda_send_errors,
        }

        if self._quote_latency_count > 0:
            payload["minimum_quote_latency_ns"] = self._min_quote_latency_ns
            payload["average_quote_latency_ns"] = round(avg_quote_latency_ns)
            payload["maximum_quote_latency_ns"] = self._max_quote_latency_ns

        if self._quotes_received > 0:
            payload["minimum_spread"] = f"{self._min_spread:.4f}"
            payload["average_spread"] = f"{avg_spread:.4f}"
            payload["maximum_spread"] = f"{self._max_spread:.4f}"
            payload["minimum_bid_price"] = f"{self._min_bid:.4f}"
            payload["maximum_bid_price"] = f"{self._max_bid:.4f}"
            payload["minimum_ask_price"] = f"{self._min_ask:.4f}"
            payload["maximum_ask_price"] = f"{self._max_ask:.4f}"

        if self._trade_latency_count > 0:
            payload["minimum_trade_latency_ns"] = self._min_trade_latency_ns
            payload["average_trade_latency_ns"] = round(avg_trade_latency_ns)
            payload["maximum_trade_latency_ns"] = self._max_trade_latency_ns

        if self._trades_received > 0:
            payload["minimum_trade_price"] = f"{self._min_trade_price:.4f}"
            payload["maximum_trade_price"] = f"{self._max_trade_price:.4f}"
            payload["total_trade_volume"] = f"{self._total_trade_volume:.0f}"
            if vwap is not None:
                payload["volume_weighted_average_trade_price"] = f"{vwap:.4f}"

        try:
            self.observe("ODA_STATS", context=payload)
            self._oda_messages_sent += 1
        except Exception as exc:
            self._oda_send_errors += 1
            self.observe("ODA send error", context={"error": str(exc)})