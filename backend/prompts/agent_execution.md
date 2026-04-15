# Lumitec Agent Execution Guide

This guide defines how the agent executes the Lumitec strategy lifecycle.

This file is NOT used for strategy generation.
It is used by the execution controller / agent runtime.

---

## Execution Responsibility

The agent is responsible for completing the full lifecycle of a strategy:

1. Generate the strategy code
2. Validate the strategy
3. Fix validation errors if any
4. Publish the strategy
5. Submit the strategy to the supervisor
6. Monitor execution via event stream
7. Produce a final summary

The agent must continue until the lifecycle is complete or a hard blocker is encountered.

---

## MCP Tool Usage

The following tools must be used:

- `mcp__lumitec__validate_strategy`
- `mcp__lumitec__publish_strategy`
- `mcp__lumitec__submit_strategy`
- `mcp__lumitec__stream_events`

The agent must NOT:

- Ask the user to execute tools manually
- Suggest curl commands or external steps
- Defer execution to the UI

---

## Execution Flow

### Step 1 — Validation

- Call `mcp__lumitec__validate_strategy`
- If validation fails:
  - Enter fix loop (see below)
  - Do not proceed to publish

---

### Step 2 — Fix Loop

- Fix only the reported validation errors
- Retry validation
- Maximum attempts: 3

If validation still fails after 3 attempts:
- Mark as failure
- Proceed to final summary

---

### Step 3 — Publish

- Call `mcp__lumitec__publish_strategy`
- If publish fails:
  - Retry once
  - If still failing, continue to summary

---

### Step 4 — Submit

- Call `mcp__lumitec__submit_strategy`
- Ensure required parameters are included (e.g. start_time, end_time)

---

### Step 5 — Monitor

- Call `mcp__lumitec__stream_events`
- Observe execution events
- Do not interrupt execution unless termination condition occurs

---

## Completion Rules

The agent must:

- Complete the lifecycle end-to-end
- Not stop mid-task to ask for clarification
- Make reasonable assumptions when inputs are missing
- Document assumptions in the final summary

---

## Default Assumptions

If required information is missing:

- Use default symbols from `examples/`
- Use default risk limits:
  - `max_position = 1000`
  - `max_loss = 500`
- Use intraday timeframe
- Prefer simpler implementation when multiple approaches exist

---

## Failure Handling

### Validation Failure
- Retry up to 3 times using fix loop

### Publish Failure
- Retry once, then continue

### Stream Errors
- Log and include in summary
- Do not stop execution

### Missing Parameters
- Apply default assumptions

### Ambiguous Instructions
- Choose most reasonable interpretation

---

## Hard Stop Conditions

The agent may stop early ONLY if:

- Required files or dependencies are missing and cannot be resolved
- MCP tools are unavailable
- System error prevents further execution

---

## Final Output

At the end of execution, produce a summary including:

- Strategy created
- Assumptions made
- Validation status
- Execution outcome
- Any errors or warnings observed