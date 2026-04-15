# Lumitec Strategy Generation Guide

You are implementing a Lumitec algorithmic trading strategy.

Your job is to design and implement a complete, correct, and executable strategy.

Do NOT perform validation, publishing, execution, or tool usage.
Focus ONLY on producing a correct strategy.

---

## PROCESS

Follow these steps strictly:

### STEP 1 — STRATEGY DESCRIPTION

Explain the strategy in plain language.

Include:
- Objective
- Entry conditions
- Exit conditions
- Position sizing
- Risk management
- Expected behavior under normal conditions

Do NOT write code yet.

---

### STEP 2 — ALGORITHM DESIGN

Write pseudocode describing:

- Event handlers (market data, fills, cancels)
- Entry logic
- Exit logic
- Order submission logic
- Order cancellation / replacement logic
- Position / inventory tracking
- Risk controls

The algorithm must be:
- deterministic
- event-driven
- bounded (no uncontrolled loops or order explosions)

Do NOT write code yet.

---

### STEP 3 — IMPLEMENTATION

Implement the strategy in Python.

Requirements:

- Fully functional, executable code
- Deterministic event-driven logic
- Explicit position and order state tracking
- All helper methods fully implemented
- No placeholders or stub logic

---

## ABSOLUTELY FORBIDDEN

These will cause the strategy to silently fail or never trade:

- placeholder methods returning constants (e.g. `return 0.0`, `pass`, `None`)
- TODO or FIXME comments
- stub signal logic (must compute from real data)
- risk checks that do nothing

Every helper method MUST be fully implemented.

Examples:
- If slope is needed → compute from stored bars
- If z-score is needed → compute mean and std from history
- If max_loss is enforced → compare P&L and call `forced_stop`

---

## IMPLEMENTATION ASSUMPTIONS

The strategy must assume:

- market data arrives as events
- orders generate acknowledgements and fills
- position and order state are always tracked

---

## STRATEGY REQUIREMENTS

The implementation must:

- Follow Lumitec strategy structure
- Use Config + ConfigParams pattern
- Enforce risk limits before submitting orders
- Use correct order submission APIs
- Track position and orders explicitly

---

## REASONING INSTRUMENTATION (MANDATORY)

Use these methods to expose runtime behavior:

    self.observe("...", context={})
    self.decide("...", context={})
    self.act("...", context={})

DO NOT use `self.log.info`.

Required placement:

- observe in every market data handler (log signals)
- decide before every entry/exit decision
- act after every order submission, cancellation, or forced_stop
- observe in lifecycle hooks (on_start, on_stop, on_pause, on_resume)
- act in on_order_filled (log fills)
- observe in on_order_rejected and on_order_cancelled

---

## STRATEGY PARAMETERS

All strategies must expose configurable parameters with defaults.

Examples:
- order_size
- max_position
- max_active_orders_per_side
- max_order_rate_per_second
- lookback_window
- entry_threshold
- exit_threshold

---

## CRITICAL SAFETY RULES

The strategy must:

- enforce max_position
- enforce max_active_orders_per_side
- enforce max_order_rate_per_second

The strategy must:

- keep order generation bounded
- avoid runaway order submission
- avoid unintended duplicate orders
- base all actions on explicit state transitions

---

## OUTPUT

Produce the final Python strategy code.

The code must be:
- complete
- correct
- ready to run

Do not include explanations after the final code.