# agent_execution.md — RETIRED

This prompt described a deprecated agentic execution phase in which the LLM autonomously
called MCP tools (`mcp__lumitec__submit_strategy`, `mcp__lumitec__stream_events`,
`mcp__lumitec__publish_strategy`). All three tools are in the `BLOCKED` set and are never
available to the agent. The phase was removed.

`_load_execution_system()` and `AGENT_EXECUTION_PROMPT_PATH` have been removed from
`agent.py`. **This file is no longer loaded by any code path.**

---

## Current workflow (for reference)

1. **Phase 1** — LLM generates strategy code (`run_strategy_workflow`)
2. **Phase 2** — Validation fix loop via `validate_strategy` MCP tool
3. **`params_ready`** — Frontend receives legs/params for user review
4. **Phase 3** — User submits via frontend → `run_resubmit_workflow` →
   `validate_strategy` (MCP, blocks on failure) → Orchestrator REST POST
5. **Monitoring** — Live SSE relay in `main.py` (no agent involvement)
