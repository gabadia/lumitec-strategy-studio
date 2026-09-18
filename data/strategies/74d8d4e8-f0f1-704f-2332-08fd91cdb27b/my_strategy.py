from dataclasses import dataclass, replace, fields as dc_fields
from threading import RLock
from nautilus_trader.model.enums import OrderSide, TimeInForce, BarAggregation, PriceType
from nautilus_trader.model.objects import Price
from lumitec.strategy.base import LumitecBaseStrategy
from lumitec.strategy.config import LumitecStrategyConfig
from lumitec.strategy.definitions import LegMode, StrategyMission, StrategyObjective

class PairsTradingStrategy(LumitecBaseStrategy):

    mission   = StrategyMission.INTRADAY_ARBITRAGE
    objective = StrategyObjective.INTRADAY_ARBITRAGE
    leg_mode  = LegMode.CONTINUOUS

    leg_schema = [
        {"label": "Leg A (Long)",  "side": "BUY",  "fixed_side": True},
        {"label": "Leg B (Short)", "side": "SELL", "fixed_side": True},
    ]

    class Config(LumitecStrategyConfig):
        strategy_name: str = "MyStrategy"
        file_name: str = "my_strategy.py"

    @dataclass(frozen=True)
    class ConfigParams:
        instrument_a: str
        instrument_b: str
        entry_hurdle: float
        exit_hurdle: float
        sizing_mode: str
        execution_mode: str
        max_position: int
        max_loss: float
        max_active_orders_per_side: int
        max_order_rate_per_second: int

        @classmethod
        def from_config(cls, config: 'Config') -> 'ConfigParams':
            return cls(
                instrument_a=config.instrument_a,
                instrument_b=config.instrument_b,
                entry_hurdle=config.entry_hurdle,
                exit_hurdle=config.exit_hurdle,
                sizing_mode=config.sizing_mode,
                execution_mode=config.execution_mode,
                max_position=config.max_position,
                max_loss=config.max_loss,
                max_active_orders_per_side=config.max_active_orders_per_side,
                max_order_rate_per_second=config.max_order_rate_per_second,
            )

        def merged(self, updates: dict) -> 'ConfigParams':
            coerced = {field.name: getattr(self, field.name) for field in dc_fields(self)}
            coerced.update(updates)
            return replace(self, **coerced)

        def validate(self) -> None:
            if self.max_position <= 0:
                raise ValueError("max_position must be greater than 0")
            if self.max_loss < 0:
                raise ValueError("max_loss must be non-negative")
            if self.max_active_orders_per_side <= 0:
                raise ValueError("max_active_orders_per_side must be greater than 0")
            if self.max_order_rate_per_second <= 0:
                raise ValueError("max_order_rate_per_second must be greater than 0")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: Config):
        super().__init__(config)
        self.params = self.ConfigParams.from_config(config)

        # Market data snapshots
        self._quote_A: dict = {}          # keys: bid, ask, bid_size, ask_size
        self._quote_B: dict = {}
        self._quote_A_ts: float = 0.0     # monotonic seconds
        self._quote_B_ts: float = 0.0

        # Execution state machine
        self._exec_state: ExecState = ExecState.IDLE

        # Fill tracking (entry)
        self._filled_A_qty: int   = 0
        self._filled_B_qty: int   = 0
        self._entry_A_avg: float  = 0.0
        self._entry_B_avg: float  = 0.0
        self._pending_A_qty: int  = 0     # submitted but not yet filled
        self._pending_B_qty: int  = 0

        # Fill tracking (exit)
        self._exit_filled_A_qty: int  = 0
        self._exit_filled_B_qty: int  = 0

        # Order IDs
        self._first_leg_order_id: str  = ""
        self._hedge_order_id: str      = ""
        self._exit_A_order_id: str     = ""
        self._exit_B_order_id: str     = ""
        self._recovery_order_id: str   = ""

        # Which leg is "first" in LESS_LIQUID_FIRST mode
        self._first_leg_symbol: str   = ""
        self._first_leg_side: str     = ""   # "A" or "B"

        # Timing
        self._pair_entry_time: float  = 0.0
        self._first_leg_fill_time: float = 0.0
        self._exit_submit_time: float    = 0.0

        # P&L
        self._realized_pnl: float = 0.0
        self._max_adverse_pnl: float = 0.0  # tracks worst unrealized

        # Rate limiting
        self._order_timestamps: list = []   # monotonic times of recent orders

        # Active order counts
        self._active_buy_orders: int  = 0
        self._active_sell_orders: int = 0

        # Tick throttle
        self._last_tick_ts: float = 0.0

        # Legs (set in on_start)
        self._leg_A: dict = {}
        self._leg_B: dict = {}
        self._symbol_A: str = ""
        self._symbol_B: str = ""

        # Parameter lock
        self._param_lock = RLock()

    # ------------------------------------------------------------------
    # Platform lifecycle
    # ------------------------------------------------------------------

    def set_oms_type(self, oms_type) -> None:
        self._oms_type = oms_type

    def on_start(self) -> None:
        super().on_start()
        self._leg_A, self._leg_B = self.legs
        self._symbol_A = self._leg_A.get("symbol", self.params.instrument_a)
        self._symbol_B = self._leg_B.get("symbol", self.params.instrument_b)

        self.subscribe_market_data(
            self._symbol_A,
            subscribe_quotes=True,
            subscribe_trades=False,
        )
        self.subscribe_market_data(
            self._symbol_B,
            subscribe_quotes=True,
            subscribe_trades=False,
        )

        self.observe("PairsTradingStrategy started", context={
            "symbol_A": self._symbol_A,
            "symbol_B": self._symbol_B,
            "entry_hurdle": self.params.entry_hurdle,
            "exit_hurdle":  self.params.exit_hurdle,
            "sizing_mode":  self.params.sizing_mode,
            "exec_mode":    self.params.execution_mode,
        })

    def on_stop(self) -> None:
        # teardown must mirror setup exactly
        try:
            self.unsubscribe_market_data(
                self._symbol_A,
                subscribe_quotes=True,
                subscribe_trades=False,
            )
        except Exception:
            pass
        try:
            self.unsubscribe_market_data(
                self._symbol_B,
                subscribe_quotes=True,
                subscribe_trades=False,
            )
        except Exception:
            pass

        self.cancelAllOrders()
        self._report_exposure_on_stop()
        self._exec_state = ExecState.STOPPED
        self.observe("PairsTradingStrategy stopped", context={
            "realized_pnl": self._realized_pnl,
            "filled_A_qty": self._filled_A_qty,
            "filled_B_qty": self._filled_B_qty,
        })

    def on_pause(self, reason: str = "") -> None:
        self.cancelAllOrders()
        self.act("Strategy paused — all orders cancelled", context={"reason": reason})

    def on_resume(self) -> None:
        self.act("Strategy resumed")
        # Reset state to IDLE so we can re-evaluate entries
        if self._exec_state not in (ExecState.PAIR_OPEN, ExecState.RECOVERY):
            self._exec_state = ExecState.IDLE

    def on_order_rejected(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)
        self.observe(f"Order rejected: {oid}", context={"leg_id": leg_id, "role": role})

        if role == "OPEN":
            if leg_id == "A" and self._exec_state in (
                ExecState.FIRST_LEG_PENDING, ExecState.FIRST_LEG_PARTIALLY_FILLED
            ):
                self._active_buy_orders = max(0, self._active_buy_orders - 1)
                self._exec_state = ExecState.IDLE
                self.act("First leg A rejected — returning to IDLE")
            elif leg_id == "B" and self._exec_state in (
                ExecState.FIRST_LEG_PENDING, ExecState.FIRST_LEG_PARTIALLY_FILLED
            ):
                self._active_sell_orders = max(0, self._active_sell_orders - 1)
                self._exec_state = ExecState.IDLE
                self.act("First leg B rejected — returning to IDLE")
            elif self._exec_state == ExecState.HEDGE_PENDING:
                self.act("Hedge order rejected — entering RECOVERY")
                self._enter_recovery("hedge_rejected")

        elif role == "CLOSE":
            self.observe("Exit order rejected", context={"oid": oid})
            # Attempt will be re-evaluated on next quote

        elif role == "RECOVERY":
            self.observe("CRITICAL: recovery order rejected", context={"oid": oid})
            # Strategy remains in RECOVERY; operator must intervene

    def on_order_canceled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)
        self.observe(f"Order canceled: {oid}", context={"leg_id": leg_id, "role": role})

        if role == "OPEN":
            if leg_id == "A":
                self._active_buy_orders = max(0, self._active_buy_orders - 1)
            elif leg_id == "B":
                self._active_sell_orders = max(0, self._active_sell_orders - 1)
            # If we were in FIRST_LEG_PENDING and cancel was ours, state handled elsewhere
            if self._exec_state in (
                ExecState.FIRST_LEG_PENDING, ExecState.FIRST_LEG_PARTIALLY_FILLED
            ):
                # Only reset if both legs were cancelled / no partial fill
                if self._filled_A_qty == 0 and self._filled_B_qty == 0:
                    self._exec_state = ExecState.IDLE

        elif role == "CLOSE":
            self.observe("Exit order cancelled — will retry on next quote tick", context={"oid": oid})

    # ------------------------------------------------------------------
    # Parameter hot-update
    # ------------------------------------------------------------------

    def apply_params(self, updates: dict) -> None:
        with self._param_lock:
            self.params = self.params.merged(updates)
        self.observe("Parameters updated", context=updates)

    def configure(self, **extras) -> None:
        sp = extras.get("strategy_params")
        if isinstance(sp, dict):
            self.apply_params(sp)

    @classmethod
    def validate_legs(cls) -> None:
        if len(cls.leg_schema) != 2:
            raise ValueError("Exactly two legs must be defined.")
        if not all(leg['side'] in ['BUY', 'SELL'] for leg in cls.leg_schema):
            raise ValueError("Legs must have valid sides (BUY or SELL).")

    # ------------------------------------------------------------------
    # Market data handlers
    # ------------------------------------------------------------------

    def on_symbol_quote_tick(self, symbol: str, tick) -> None:
        if self.isPaused():
            return

        # Store quote unconditionally (freshness checked in logic below)
        ts = time.monotonic()
        if symbol == self._symbol_A:
            self._quote_A = {
                "bid":      float(tick.bid_price),
                "ask":      float(tick.ask_price),
                "bid_size": float(tick.bid_size),
                "ask_size": float(tick.ask_size),
            }
            self._quote_A_ts = ts
        elif symbol == self._symbol_B:
            self._quote_B = {
                "bid":      float(tick.bid_price),
                "ask":      float(tick.ask_price),
                "bid_size": float(tick.bid_size),
                "ask_size": float(tick.ask_size),
            }
            self._quote_B_ts = ts

        # Throttle observe/decide/act calls
        now = time.monotonic()
        if now - self._last_tick_ts < self.params.tick_throttle_interval:
            return
        self._last_tick_ts = now

        self._check_session_and_timeouts()

        state = self._exec_state
        if state == ExecState.IDLE:
            self._evaluate_entry()
        elif state == ExecState.PAIR_OPEN:
            self._evaluate_exit()
        elif state in (ExecState.FIRST_LEG_PENDING,
                       ExecState.FIRST_LEG_PARTIALLY_FILLED):
            self._check_first_leg_validity()
        elif state == ExecState.HEDGE_PENDING:
            self._check_hedge_timeout()
        elif state == ExecState.EXIT_PENDING:
            self._check_exit_timeout()

    # ------------------------------------------------------------------
    # Session/timing checks (called from tick handler)
    # ------------------------------------------------------------------

    def _check_session_and_timeouts(self) -> None:
        if self._check_end_time_reached():
            if self._exec_state == ExecState.PAIR_OPEN:
                self._initiate_exit("end_time_reached")
            elif self._exec_state not in (ExecState.EXIT_PENDING,
                                          ExecState.RECOVERY,
                                          ExecState.STOPPED):
                self.cancelAllOrders()
                self.forced_stop("End time reached", "TIME")
            return

        if self.params.flatten_before_close:
            self._check_flatten_before_close()

        if self._exec_state == ExecState.PAIR_OPEN:
            self._check_holding_time()
            self._check_strategy_max_loss()

    def _check_flatten_before_close(self) -> None:
        # Use base class helper — if end_time is within flatten_seconds_before_close, flatten
        # We approximate by checking if remaining session < threshold
        try:
            import datetime
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            end_dt = self._end_time  # base class stores this
            if end_dt is None:
                return
            remaining = (end_dt - now_utc).total_seconds()
            if remaining <= self.params.flatten_seconds_before_close:
                if self._exec_state == ExecState.PAIR_OPEN:
                    self._initiate_exit("flatten_before_close")
        except Exception:
            pass  # If end_time attribute unavailable, skip

    def _check_holding_time(self) -> None:
        if self._pair_entry_time == 0.0:
            return
        elapsed = time.monotonic() - self._pair_entry_time
        if elapsed >= self.params.maximum_holding_time:
            self.decide("Maximum holding time reached", context={"elapsed_s": elapsed})
            self._initiate_exit("max_holding_time")

    def _check_strategy_max_loss(self) -> None:
        unrealized = self._compute_unrealized_pnl()
        total = self._realized_pnl + unrealized
        if total <= -self.params.max_loss:
            self.decide("Strategy max loss reached", context={"total_pnl": total})
            self._initiate_exit("max_loss")

    def _check_first_leg_validity(self) -> None:
        """Cancel the first-leg order if the spread opportunity has disappeared."""
        if not self._quotes_valid():
            self.decide("Quotes invalid — cancelling first leg")
            self.cancelAllOrders()
            self._exec_state = ExecState.IDLE
            return

        qa = self._quote_A
        qb = self._quote_B
        with self._param_lock:
            p = self.params
        c = (qa["ask"] * p.a_quantity) - (qb["bid"] * p.b_quantity)
        if c > p.entry_hurdle:
            self.decide("Spread no longer favourable — cancelling first leg",
                        context={"C": c, "H_entry": p.entry_hurdle})
            self.cancelAllOrders()
            self._exec_state = ExecState.IDLE

    def _check_hedge_timeout(self) -> None:
        if self._first_leg_fill_time == 0.0:
            return
        elapsed_ms = (time.monotonic() - self._first_leg_fill_time) * 1000.0
        if elapsed_ms > self.params.hedge_timeout_ms:
            self.decide("Hedge timeout — entering RECOVERY",
                        context={"elapsed_ms": elapsed_ms})
            self._enter_recovery("hedge_timeout")

    def _check_exit_timeout(self) -> None:
        if self._exit_submit_time == 0.0:
            return
        elapsed_ms = (time.monotonic() - self._exit_submit_time) * 1000.0
        if elapsed_ms > self.params.exit_timeout_ms:
            # Cancel any pending exit orders and re-submit at market
            self.observe("Exit timeout — switching to market orders",
                         context={"elapsed_ms": elapsed_ms})
            self.cancelAllOrders()
            self._submit_market_exit()

    # ------------------------------------------------------------------
    # Quote validation helpers
    # ------------------------------------------------------------------

    def _quotes_valid(self) -> bool:
        now = time.monotonic()
        max_age_s = self.params.max_quote_age_ms / 1000.0

        if not self._quote_A or not self._quote_B:
            return False

        if (now - self._quote_A_ts) > max_age_s:
            return False
        if (now - self._quote_B_ts) > max_age_s:
            return False

        qa = self._quote_A
        qb = self._quote_B

        # Prices must be positive
        if qa["bid"] <= 0 or qa["ask"] <= 0:
            return False
        if qb["bid"] <= 0 or qb["ask"] <= 0:
            return False

        # No crossed markets
        if qa["bid"] >= qa["ask"]:
            return False
        if qb["bid"] >= qb["ask"]:
            return False

        # Must have displayed size
        if qa["ask_size"] <= 0 or qb["bid_size"] <= 0:
            return False

        return True

    def _quote_window_valid_for_hedge(self) -> bool:
        """Check whether quotes are fresh enough to survive hedge latency."""
        with self._param_lock:
            p = self.params
        required_ms = (
            2 * p.estimated_round_trip_latency_ms + p.latency_safety_buffer_ms
        )
        now = time.monotonic()
        age_A_ms = (now - self._quote_A_ts) * 1000.0
        age_B_ms = (now - self._quote_B_ts) * 1000.0
        # Quote age + required window must be under max_quote_age
        return (age_A_ms + required_ms) < p.max_quote_age_ms and \
               (age_B_ms + required_ms) < p.max_quote_age_ms

    # ------------------------------------------------------------------
    # Entry evaluation
    # ------------------------------------------------------------------

    def _evaluate_entry(self) -> None:
        if not self._quotes_valid():
            self.observe("Entry check: quotes invalid or stale")
            return

        with self._param_lock:
            p = self.params

        qa = self._quote_A
        qb = self._quote_B

        c = (qa["ask"] * p.a_quantity) - (qb["bid"] * p.b_quantity)
        self.observe("Entry spread computed", context={
            "C": round(c, 4),
            "H_entry": p.entry_hurdle,
            "A_ask": qa["ask"],
            "B_bid": qb["bid"],
        })

        if c > p.entry_hurdle:
            return  # Spread not favourable

        # ── Sizing ──
        try:
            actual_a_qty, actual_b_qty, pair_units = self._compute_entry_sizing(p, qa, qb)
        except ValueError as exc:
            self.observe(f"Sizing rejected: {exc}")
            return

        # ── Dollar-imbalance check ──
        dollar_imbalance = abs(
            (qa["ask"] * actual_a_qty) - (qb["bid"] * actual_b_qty)
        )
        if dollar_imbalance > p.max_dollar_imbalance:
            self.observe("Dollar imbalance exceeds limit",
                         context={"imbalance": dollar_imbalance,
                                  "limit": p.max_dollar_imbalance})
            return

        # ── Risk checks ──
        notional = (qa["ask"] * actual_a_qty) + (qb["bid"] * actual_b_qty)
        if notional > p.max_gross_notional:
            self.observe("Max gross notional exceeded", context={"notional": notional})
            return
        if self._filled_A_qty + actual_a_qty > p.max_position:
            self.observe("Max position A exceeded")
            return
        if self._filled_B_qty + actual_b_qty > p.max_position:
            self.observe("Max position B exceeded")
            return
        total_pnl = self._realized_pnl + self._compute_unrealized_pnl()
        if total_pnl <= -p.max_loss:
            self.observe("Max loss reached — no new entries",
                         context={"pnl": total_pnl})
            return
        if not self._within_order_rate_limit():
            self.observe("Order rate limit exceeded")
            return

        self.decide("Entry signal: submitting pair", context={
            "C": round(c, 4),
            "A_qty": actual_a_qty,
            "B_qty": actual_b_qty,
            "pair_units": pair_units,
            "A_ask": qa["ask"],
            "B_bid": qb["bid"],
        })

        self._exec_state = ExecState.ENTRY_SIGNAL
        self._pending_A_qty = actual_a_qty
        self._pending_B_qty = actual_b_qty

        if p.execution_mode == ExecutionMode.SIMULTANEOUS:
            self._submit_simultaneous_entry(p, qa, qb, actual_a_qty, actual_b_qty)
        else:
            self._submit_less_liquid_first(p, qa, qb, actual_a_qty, actual_b_qty)

    def _compute_entry_sizing(self, p: ConfigParams, qa: dict, qb: dict):
        """
        Returns (actual_a_qty, actual_b_qty, pair_units).
        Raises ValueError if sizing is not feasible.
        """
        base_a = p.a_quantity
        base_b = p.b_quantity

        if p.sizing_mode == SizingMode.DYNAMIC_DOLLAR_NEUTRAL:
            raw_b = (qa["ask"] * base_a) / qb["bid"]
            # Round to lot increment
            increments = math.floor(raw_b / p.lot_size_b)
            base_b = max(increments * p.lot_size_b, p.lot_size_b)

        # Max pair units from displayed liquidity
        units_from_a = math.floor(qa["ask_size"] / base_a)
        units_from_b = math.floor(qb["bid_size"] / base_b)
        max_units_liquidity = min(units_from_a, units_from_b)

        if max_units_liquidity < 1:
            raise ValueError(
                f"Insufficient liquidity: A_ask_size={qa['ask_size']} "
                f"B_bid_size={qb['bid_size']} a_qty={base_a} b_qty={base_b}"
            )

        pair_units = min(max_units_liquidity, p.max_pair_units)
        actual_a_qty = pair_units * base_a
        actual_b_qty = pair_units * base_b

        return actual_a_qty, actual_b_qty, pair_units

    def _submit_simultaneous_entry(
        self,
        p: ConfigParams,
        qa: dict,
        qb: dict,
        a_qty: int,
        b_qty: int,
    ) -> None:
        buy_price  = Price.from_str(f"{qa['ask']:.2f}")
        sell_price = Price.from_str(f"{qb['bid']:.2f}")

        try:
            order_a = self.submit_limit_order(
                symbol=self._symbol_A,
                side=OrderSide.BUY,
                qty=a_qty,
                price=buy_price,
                tif=TimeInForce.DAY,
                leg_id="A",
                role="OPEN",
            )
            self._first_leg_order_id = order_a.client_order_id.value
            self._active_buy_orders += 1
            self._record_order_rate()

            order_b = self.submit_limit_order(
                symbol=self._symbol_B,
                side=OrderSide.SELL,
                qty=b_qty,
                price=sell_price,
                tif=TimeInForce.DAY,
                leg_id="B",
                role="OPEN",
            )
            self._hedge_order_id = order_b.client_order_id.value
            self._active_sell_orders += 1
            self._record_order_rate()

            self._exec_state = ExecState.FIRST_LEG_PENDING
            self.act("Simultaneous entry submitted", context={
                "A_order": self._first_leg_order_id,
                "B_order": self._hedge_order_id,
                "A_qty": a_qty,
                "B_qty": b_qty,
            })
        except Exception as exc:
            self.observe(f"Entry submission error: {exc}")
            self.cancelAllOrders()
            self._exec_state = ExecState.IDLE

    def _submit_less_liquid_first(
        self,
        p: ConfigParams,
        qa: dict,
        qb: dict,
        a_qty: int,
        b_qty: int,
    ) -> None:
        # Determine less liquid leg by displayed bid/ask size
        a_liquidity = qa["ask_size"]
        b_liquidity = qb["bid_size"]

        first_is_a = a_liquidity <= b_liquidity

        if first_is_a:
            self._first_leg_side = "A"
            price = Price.from_str(f"{qa['ask']:.2f}")
            try:
                order = self.submit_limit_order(
                    symbol=self._symbol_A,
                    side=OrderSide.BUY,
                    qty=a_qty,
                    price=price,
                    tif=TimeInForce.DAY,
                    leg_id="A",
                    role="OPEN",
                )
                self._first_leg_order_id = order.client_order_id.value
                self._active_buy_orders += 1
                self._record_order_rate()
            except Exception as exc:
                self.observe(f"First-leg A submission error: {exc}")
                self._exec_state = ExecState.IDLE
                return
        else:
            self._first_leg_side = "B"
            price = Price.from_str(f"{qb['bid']:.2f}")
            try:
                order = self.submit_limit_order(
                    symbol=self._symbol_B,
                    side=OrderSide.SELL,
                    qty=b_qty,
                    price=price,
                    tif=TimeInForce.DAY,
                    leg_id="B",
                    role="OPEN",
                )
                self._first_leg_order_id = order.client_order_id.value
                self._active_sell_orders += 1
                self._record_order_rate()
            except Exception as exc:
                self.observe(f"First-leg B submission error: {exc}")
                self._exec_state = ExecState.IDLE
                return

        self._exec_state = ExecState.FIRST_LEG_PENDING
        self.act(f"Less-liquid-first: submitted first leg ({self._first_leg_side})",
                 context={"order_id": self._first_leg_order_id})

    def _submit_hedge_leg(self, first_leg_filled_qty: int) -> None:
        """
        Submit the hedge leg sized to the actual fill on the first leg.
        Called from on_order_filled when first leg fills in LESS_LIQUID_FIRST mode.
        """
        if not self._quotes_valid():
            self.observe("Hedge: quotes invalid at hedge submission time")
            self._enter_recovery("hedge_quotes_stale")
            return

        if not self._quote_window_valid_for_hedge():
            self.observe("Hedge: quote window too narrow for safe hedge")
            self._enter_recovery("hedge_window_too_narrow")
            return

        with self._param_lock:
            p = self.params

        qa = self._quote_A
        qb = self._quote_B

        if self._first_leg_side == "A":
            # First leg was A (BUY); hedge is B (SELL)
            if p.sizing_mode == SizingMode.DYNAMIC_DOLLAR_NEUTRAL:
                raw_b = (qa["ask"] * first_leg_filled_qty) / qb["bid"]
                increments = math.floor(raw_b / p.lot_size_b)
                hedge_qty = max(increments * p.lot_size_b, p.lot_size_b)
            else:
                ratio = p.b_quantity / p.a_quantity
                hedge_qty = max(
                    int(round(first_leg_filled_qty * ratio / p.lot_size_b)) * p.lot_size_b,
                    p.lot_size_b,
                )
            price = Price.from_str(f"{qb['bid']:.2f}")
            try:
                order = self.submit_limit_order(
                    symbol=self._symbol_B,
                    side=OrderSide.SELL,
                    qty=hedge_qty,
                    price=price,
                    tif=TimeInForce.DAY,
                    leg_id="B",
                    role="OPEN",
                )
                self._hedge_order_id = order.client_order_id.value
                self._active_sell_orders += 1
                self._record_order_rate()
            except Exception as exc:
                self.observe(f"Hedge B submission error: {exc}")
                self._enter_recovery("hedge_submit_error")
                return
        else:
            # First leg was B (SELL); hedge is A (BUY)
            if p.sizing_mode == SizingMode.DYNAMIC_DOLLAR_NEUTRAL:
                raw_a = (qb["bid"] * first_leg_filled_qty) / qa["ask"]
                increments = math.floor(raw_a / p.lot_size_a)
                hedge_qty = max(increments * p.lot_size_a, p.lot_size_a)
            else:
                ratio = p.a_quantity / p.b_quantity
                hedge_qty = max(
                    int(round(first_leg_filled_qty * ratio / p.lot_size_a)) * p.lot_size_a,
                    p.lot_size_a,
                )
            price = Price.from_str(f"{qa['ask']:.2f}")
            try:
                order = self.submit_limit_order(
                    symbol=self._symbol_A,
                    side=OrderSide.BUY,
                    qty=hedge_qty,
                    price=price,
                    tif=TimeInForce.DAY,
                    leg_id="A",
                    role="OPEN",
                )
                self._hedge_order_id = order.client_order_id.value
                self._active_buy_orders += 1
                self._record_order_rate()
            except Exception as exc:
                self.observe(f"Hedge A submission error: {exc}")
                self._enter_recovery("hedge_submit_error")
                return

        self._exec_state = ExecState.HEDGE_PENDING
        self.act("Hedge leg submitted", context={
            "hedge_order_id": self._hedge_order_id,
            "hedge_qty": hedge_qty,
        })

    # ------------------------------------------------------------------
    # Exit evaluation
    # ------------------------------------------------------------------

    def _evaluate_exit(self) -> None:
        if self._filled_A_qty == 0 and self._filled_B_qty == 0:
            return
        if not self._quotes_valid():
            # If quotes stale for too long, exit anyway
            now = time.monotonic()
            a_age_ms = (now - self._quote_A_ts) * 1000.0
            b_age_ms = (now - self._quote_B_ts) * 1000.0
            if a_age_ms > self.params.max_quote_age_ms * 3 or \
               b_age_ms > self.params.max_quote_age_ms * 3:
                self.decide("Quotes extremely stale — force exit")
                self._initiate_exit("stale_quotes")
            return

        with self._param_lock:
            p = self.params

        qa = self._quote_A
        qb = self._quote_B
        v = (qa["bid"] * self._filled_A_qty) - (qb["ask"] * self._filled_B_qty)

        self.observe("Exit spread computed", context={
            "V": round(v, 4),
            "H_exit": p.exit_hurdle,
            "A_bid": qa["bid"],
            "B_ask": qb["ask"],
        })

        # Pair stop-loss
        if v <= -p.pair_stop_loss:
            self.decide("Pair stop-loss triggered", context={"V": v})
            self._initiate_exit("pair_stop_loss")
            return

        # Profit target
        if v >= p.exit_hurdle:
            self.decide("Exit hurdle reached", context={"V": v})
            self._initiate_exit("profit_target")
            return

    def _initiate_exit(self, reason: str) -> None:
        if self._exec_state == ExecState.EXIT_PENDING:
            return  # Already exiting — prevent duplicate

        if self._filled_A_qty <= 0 and self._filled_B_qty <= 0:
            self._exec_state = ExecState.IDLE
            return

        with self._param_lock:
            p = self.params

        if not self._quotes_valid():
            self.observe("Exit quotes invalid — using market orders")
            self._submit_market_exit()
            return

        qa = self._quote_A
        qb = self._quote_B

        self.decide(f"Initiating exit: reason={reason}", context={
            "filled_A": self._filled_A_qty,
            "filled_B": self._filled_B_qty,
        })

        self._exec_state = ExecState.EXIT_PENDING
        self._exit_submit_time = time.monotonic()

        try:
            if self._filled_A_qty > 0:
                sell_price = Price.from_str(f"{qa['bid']:.2f}")
                order_a = self.submit_limit_order(
                    symbol=self._symbol_A,
                    side=OrderSide.SELL,
                    qty=self._filled_A_qty,
                    price=sell_price,
                    tif=TimeInForce.DAY,
                    leg_id="A",
                    role="CLOSE",
                )
                self._exit_A_order_id = order_a.client_order_id.value
                self._active_sell_orders += 1
                self._record_order_rate()

            if self._filled_B_qty > 0:
                buy_price = Price.from_str(f"{qb['ask']:.2f}")
                order_b = self.submit_limit_order(
                    symbol=self._symbol_B,
                    side=OrderSide.BUY,
                    qty=self._filled_B_qty,
                    price=buy_price,
                    tif=TimeInForce.DAY,
                    leg_id="B",
                    role="CLOSE",
                )
                self._exit_B_order_id = order_b.client_order_id.value
                self._active_buy_orders += 1
                self._record_order_rate()

            self.act(f"Exit orders submitted (reason={reason})", context={
                "exit_A": self._exit_A_order_id,
                "exit_B": self._exit_B_order_id,
            })
        except Exception as exc:
            self.observe(f"Exit submission error: {exc}")
            self._submit_market_exit()

    def _submit_market_exit(self) -> None:
        self._exec_state = ExecState.EXIT_PENDING
        self._exit_submit_time = time.monotonic()

        try:
            if self._filled_A_qty > 0:
                clid = self.submit_market_order(
                    symbol=self._symbol_A,
                    side=OrderSide.SELL,
                    qty=self._filled_A_qty,
                    leg_id="A",
                    role="CLOSE",
                )
                self._exit_A_order_id = clid.value
                self._active_sell_orders += 1
                self._record_order_rate()

            if self._filled_B_qty > 0:
                clid = self.submit_market_order(
                    symbol=self._symbol_B,
                    side=OrderSide.BUY,
                    qty=self._filled_B_qty,
                    leg_id="B",
                    role="CLOSE",
                )
                self._exit_B_order_id = clid.value
                self._active_buy_orders += 1
                self._record_order_rate()

            self.act("Market exit submitted")
        except Exception as exc:
            self.observe(f"CRITICAL: market exit submission failed: {exc}")

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _enter_recovery(self, reason: str) -> None:
        self._exec_state = ExecState.RECOVERY
        self.cancelAllOrders()

        # Determine which exposure is unhedged
        exposed_A = self._filled_A_qty - self._exit_filled_A_qty
        exposed_B = self._filled_B_qty - self._exit_filled_B_qty

        self.act(f"RECOVERY entered: {reason}", context={
            "exposed_A": exposed_A,
            "exposed_B": exposed_B,
            "policy": self.params.recovery_policy,
        })

        if exposed_A == 0 and exposed_B == 0:
            self._exec_state = ExecState.IDLE
            return

        try:
            if self.params.recovery_policy == RecoveryPolicy.MARKET_FLATTEN:
                self._recovery_market_flatten(exposed_A, exposed_B)
            else:
                self._recovery_aggressive_limit(exposed_A, exposed_B)
        except Exception as exc:
            self.observe(f"CRITICAL: recovery submission failed: {exc}")

    def _recovery_market_flatten(self, exposed_A: int, exposed_B: int) -> None:
        # If long A exposure, sell it at market
        if exposed_A > 0:
            clid = self.submit_market_order(
                symbol=self._symbol_A,
                side=OrderSide.SELL,
                qty=exposed_A,
                leg_id="A",
                role="RECOVERY",
            )
            self._recovery_order_id = clid.value
            self._active_sell_orders += 1
            self._record_order_rate()
            self.act("Recovery: market SELL A submitted",
                     context={"qty": exposed_A, "order_id": clid.value})

        # If short B exposure (filled_B > 0 means we sold B), buy it back
        if exposed_B > 0:
            clid = self.submit_market_order(
                symbol=self._symbol_B,
                side=OrderSide.BUY,
                qty=exposed_B,
                leg_id="B",
                role="RECOVERY",
            )
            self._recovery_order_id = clid.value
            self._active_buy_orders += 1
            self._record_order_rate()
            self.act("Recovery: market BUY B submitted",
                     context={"qty": exposed_B, "order_id": clid.value})

    def _recovery_aggressive_limit(self, exposed_A: int, exposed_B: int) -> None:
        qa = self._quote_A
        qb = self._quote_B

        if exposed_A > 0 and qa:
            # Sell A at bid (aggressive limit)
            price = Price.from_str(f"{qa.get('bid', 0.01):.2f}")
            order = self.submit_limit_order(
                symbol=self._symbol_A,
                side=OrderSide.SELL,
                qty=exposed_A,
                price=price,
                tif=TimeInForce.DAY,
                leg_id="A",
                role="RECOVERY",
            )
            self._recovery_order_id = order.client_order_id.value
            self._active_sell_orders += 1
            self._record_order_rate()
            self.act("Recovery: aggressive limit SELL A",
                     context={"qty": exposed_A, "price": str(price)})

        if exposed_B > 0 and qb:
            # Buy B at ask (aggressive limit)
            price = Price.from_str(f"{qb.get('ask', 0.01):.2f}")
            order = self.submit_limit_order(
                symbol=self._symbol_B,
                side=OrderSide.BUY,
                qty=exposed_B,
                price=price,
                tif=TimeInForce.DAY,
                leg_id="B",
                role="RECOVERY",
            )
            self._recovery_order_id = order.client_order_id.value
            self._active_buy_orders += 1
            self._record_order_rate()
            self.act("Recovery: aggressive limit BUY B",
                     context={"qty": exposed_B, "price": str(price)})

    # ------------------------------------------------------------------
    # Fill handler
    # ------------------------------------------------------------------

    def on_order_filled(self, event) -> None:
        oid = event.client_order_id.value
        leg_id, role = self.extract_leg_info_from_order_id(oid)

        if leg_id is None:
            self.observe(f"Unknown leg_id for order {oid}")
            return

        fill_qty   = int(event.last_qty)
        fill_price = float(event.last_px)

        self.act(f"Fill received leg={leg_id} role={role}", context={
            "oid": oid,
            "qty": fill_qty,
            "price": fill_price,
            "state": self._exec_state.name,
        })

        if role == "OPEN":
            self._handle_open_fill(leg_id, fill_qty, fill_price, oid)

        elif role == "CLOSE":
            self._handle_close_fill(leg_id, fill_qty, fill_price)

        elif role == "RECOVERY":
            self._handle_recovery_fill(leg_id, fill_qty, fill_price)

    def _handle_open_fill(
        self, leg_id: str, fill_qty: int, fill_price: float, oid: str
    ) -> None:
        if leg_id == "A":
            prev = self._filled_A_qty
            self._entry_A_avg = self._weighted_avg(
                self._entry_A_avg, prev, fill_price, fill_qty
            )
            self._filled_A_qty += fill_qty
            self._pending_A_qty = max(0, self._pending_A_qty - fill_qty)
            # Check if this was full fill
            if self._pending_A_qty == 0:
                self._active_buy_orders = max(0, self._active_buy_orders - 1)

        elif leg_id == "B":
            prev = self._filled_B_qty
            self._entry_B_avg = self._weighted_avg(
                self._entry_B_avg, prev, fill_price, fill_qty
            )
            self._filled_B_qty += fill_qty
            self._pending_B_qty = max(0, self._pending_B_qty - fill_qty)
            if self._pending_B_qty == 0:
                self._active_sell_orders = max(0, self._active_sell_orders - 1)

        with self._param_lock:
            exec_mode = self.params.execution_mode

        # LESS_LIQUID_FIRST: trigger hedge after first leg fill
        if exec_mode == ExecutionMode.LESS_LIQUID_FIRST and \
           self._exec_state in (ExecState.FIRST_LEG_PENDING,
                                ExecState.FIRST_LEG_PARTIALLY_FILLED):
            # Record fill time for hedge timeout tracking
            if self._first_leg_fill_time == 0.0:
                self._first_leg_fill_time = time.monotonic()