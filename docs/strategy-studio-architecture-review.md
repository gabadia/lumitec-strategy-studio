# Lumitec Strategy Studio — Full Architectural & Workflow Review

**Date:** 2025-01-25  
**Scope:** Full read-only review of backend, frontend, prompts, and strategy contracts  
**Reviewer:** AI architectural review — read-only, no code changes  
**Review Basis:** agent.py (~1,200 lines), main.py (~1,100 lines), frontend App.tsx + 8 components, 11 prompt files, ClaudePairsStrategy.py, types.ts, auth.ts  

---

## 1. Executive Summary

Lumitec Strategy Studio is an AI-assisted algorithmic trading IDE that enables non-programmers to generate, validate, and submit live trading strategies through natural language intents. The platform composes Claude (Anthropic) for generation, GPT-4o-mini (OpenAI) for validation repair, and a NautilusTrader supervisor for live execution. The core workflow — Generate → Validate → Submit → Monitor — is well-conceived and functional for its current single-user development context.

**Strengths:**
- The Observe/Decide/Act reasoning instrumentation is a powerful pattern that gives the strategy executor a structured audit trail.
- The two-stage validation (generation fix-loop + hard submit gate) is correctly structured to be fail-closed.
- The SSE relay with SQLite replay correctly solves the late-connect/reconnect problem.
- The strategy contract pattern (Config + ConfigParams + 23 checklist items) is comprehensive and well-documented.
- The `RunningSpreadStats` Welford online algorithm is a sophisticated, correct implementation.

**Critical Gaps:**
- The system is architecturally a single-process monolith masking as a service mesh. All five services (MCP, orchestrator, SSE gateway, validator, strategy file store) must be up for any workflow to succeed. There is no graceful degradation.
- The MCP session is opened for every generation request but only used for tool discovery — it is effectively dead weight with a liveness dependency.
- Authentication is entirely cosmetic: `X-Trader-Id` is read from a header with no verification. Any caller can impersonate any trader.
- Strategy code is executed in the supervisor without exhaustive sandboxing — the forbidden import list is incomplete against the full Python attack surface.
- The SSE relay connects to the gateway on **every** browser reconnect, with no deduplication guard until the replay path detects a terminal event.

---

## 2. Architecture Assessment

### 2.1 Service Topology

```
Browser (React/Vite :5174)
  │ fetch /api/*  (proxy via Vite → :8089)
  ▼
FastAPI Backend (:8089)          ← agent.py + main.py (single Python process)
  ├── POST /run-strategy ────────── Claude / GPT generation + validation
  ├── POST /resubmit-strategy ───── Validation gate + orchestrator submit
  ├── GET  /strategies/{id}/events ← SSE relay from gateway :9001
  ├── POST /analyze-execution ───── LLM analysis with SQL tool loop
  ├── *  /strategies/{id}/stop|pause|resume|params ← proxy to :8000
  │
  ├── httpx → Orchestrator (:8000) — strategy lifecycle (submit, stop, pause)
  ├── httpx → Validator (:8003)    — /validate endpoint (two env URLs)
  ├── httpx → SSE Gateway (:9001)  — /stream (strategy events fan-in)
  └── mcp.ClientSession → MCP Server (:8002/sse) — TOOL DISCOVERY ONLY
```

### 2.2 Trust Boundary Analysis

| Layer | Trust Assertion | Actual Enforcement |
|---|---|---|
| Frontend → Backend | `X-Trader-Id` / `X-Org-Id` headers | **None** — validated for format only (regex), not identity |
| Backend → Orchestrator | Implicit trust (same network) | No auth token, no mTLS |
| Backend → Validator | Implicit trust | No auth token |
| Backend → MCP | SSE connection, no auth | No token |
| Strategy execution | Forbidden import list (23 patterns) | Incomplete — see §9.5 |

**Finding:** There is no authentication layer anywhere in the stack. The frontend `LoginGate` stores a `Trader` object in `localStorage` and sends it as headers. This is authorization theater — any browser DevTools call can impersonate any `traderId`. The platform cannot distinguish legitimate users from unauthorized users.

### 2.3 Process Isolation

The entire backend runs as a single `uvicorn` process. All concerns coexist:
- AI generation (blocking async LLM calls)
- SSE relay (long-held streaming connections)
- SQLite persistence (aiosqlite)
- Strategy file I/O
- Orchestrator proxying

A long-running Claude generation (60+ seconds at 8,096 tokens) will not block other requests due to asyncio, but a blocking file system operation or a slow Anthropic response will hold the event loop. There is no worker pool, no process isolation per request, and no timeout on the generation phase itself (only on the validator HTTP call: 30s).

### 2.4 State Management

| State | Storage | Concurrency Risk |
|---|---|---|
| Strategy code files | Filesystem (`.py` files per trader) | Race on concurrent saves to same file |
| Run events | SQLite per `strategy_id` | Low — single writer via SSE relay |
| Active strategy ID | React zustand store (in-browser) | N/A |
| Pending submission params | React zustand store | Cleared on page reload — not persisted |
| Trader identity | localStorage | Spoofable, not server-validated |

A significant gap: if two tabs are open for the same trader and both generate strategies simultaneously, the second `params_ready` event will overwrite `pendingSubmission` in the Zustand store. The user will see the second strategy's params while the first strategy's code may still be in the editor.

### 2.5 MCP Dependency

`run_strategy_workflow` wraps the entire workflow in:

```python
async with sse_client(MCP_SERVER_URL) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools_response = await session.list_tools()
        # ... all phases run here
```

This means:
1. If `MCP_SERVER_URL` (`:8002/sse`) is unreachable, **all strategy generation fails** — not just tool calls.
2. The MCP `ClientSession` stays open for the entire workflow duration (potentially 60–120 seconds).
3. The tools are listed but **never invoked** through MCP. All validation calls go via `httpx`, all submit calls go via `httpx`. The MCP session exists only to populate the `tools_ready` SSE event shown in the activity feed.
4. The `BLOCKED` set `{"publish_strategy", "stream_events", "submit_strategy"}` filters three tool names but they are filtered from the display list, not from actual invocation (since MCP is never invoked).

**This is the highest architectural debt item in the codebase.** The MCP dependency should be made optional or removed. `tools_ready` is currently driven by MCP discovery but could be driven by a static capability manifest.

---

## 3. AI Workflow Assessment

### 3.1 Generation Pipeline

```
Phase 1 — _phase_claude_generate()
  └── _stream_text(generate_model, system_prompt, user_content)
      ├── Streams text deltas → activity feed
      └── _extract_code_from_text(full_text) → last ```python block

Phase 2 — _phase_openai_validate() [up to 3 attempts]
  ├── httpx POST /validate → validator service
  ├── If failed: _complete(fix_model, validation_system, ...)
  └── _code_ready with validated code

Phase 3 (full mode) — _phase_generate_test_scenarios()
  └── _complete(json_mode=True) → structured scenarios

Phase 4 (full mode) — _phase_reason_test_scenarios()
  └── _complete() per scenario → pass/fail with reasoning

Phase 5 — params_ready event → frontend stores pendingSubmission
  └── User reviews legs/params → triggers /resubmit-strategy

Submit — _phase_submit()
  ├── Hard gate: POST VALIDATOR_PROD_URL/validate
  └── POST ORCHESTRATOR_URL/v1/supervisors/{id}/strategies/submit
```

### 3.2 Code Extraction — Fragility

`_extract_code_from_text` finds "the last `\`\`\`python` block in text." This has several failure modes:

1. Claude sometimes emits explanatory code snippets mid-reasoning (e.g. showing a partial fix), then the full strategy. The "last" heuristic is correct for this case.
2. Claude sometimes emits the code without a language tag (`\`\`\`` only). The fallback scans for Python keywords — this is unreliable for complex mixed-content responses.
3. If the strategy code itself contains a docstring with a fenced Python example, the extraction finds the wrong block.
4. **Silent failure path**: if extraction returns `None` and `existing_code` is also `None`, `code = ""`. The check `if not code: yield error` catches this, but the error message "Failed to extract strategy code" gives no diagnostic context.

**Recommended fix:** Request structured output from Claude (JSON with a `code` field) or use a `<strategy_code>` XML tag in the response, which Claude follows reliably.

### 3.3 Metadata Extraction — Multiple Bugs

`_extract_metadata()` has several issues:

**Symbol detection bug:** The loop variable `src` switches based on whether `symbol is None`:
```python
src = code if symbol is None else intent
```
Since `symbol` is only set on a successful match with `break`, the second pattern also runs against `code` (not `intent`) when the first regex fails. The comment `# bare uppercase word in intent` is wrong — it runs on `code`. The `intent` field is only searched if `symbol` was already found from `code` (which would have triggered `break`). **The `intent` field is never searched.** The fallback to `"AAPL"` masks this.

**Leg symbol assignment:** `_extract_metadata` assigns the single extracted symbol to all legs:
```python
legs.append({"leg_id": leg_id, "symbol": symbol, "quantity": 100, "side": side, ...})
```
For a two-leg pairs strategy (AAPL/MSFT), both legs get the symbol extracted from the first regex match. The submission payload would incorrectly assign the wrong symbol to leg B. This works for ClaudePairsStrategy only because the strategy ignores the submission leg symbols and uses its own hard-coded subscription logic.

**`leg_schema` regex:** Uses `re.findall(r'\{[^}]+\}', ...)` — fails on multi-line `leg_schema` dict entries (which is the common case). Single-line entries are rare in generated code.

**ConfigParams field parsing:** Uses a regex `r'[ \t]+(\w+)\s*:\s*([\w\[\], ]+?)\s*=\s*(.+)'` — this fails on type annotations with quotes (e.g. `"ConfigParams"`), on fields with Union types (e.g. `Optional[float]`), and on multi-line defaults.

### 3.4 Validation Loop

The current three-attempt loop has a systematic weakness: each attempt uses the same model (default: `gpt-4o-mini`). If the model fails to fix error X on attempt 1, it will likely fail the same way on attempts 2 and 3. There is no escalation strategy (e.g. switch to `gpt-4o` on attempt 3) and no prompt variation between attempts.

The fix loop emits the full current code on every attempt as `tool_call`, which causes the `CodePanel` to re-render with intermediate (broken) code versions. Users see the code flicker through invalid states during repair, which is confusing.

### 3.5 Test Scenario Reasoning — Shallow Testing

Phases 3 and 4 (full workflow mode only):
- Scenarios are LLM-generated JSON descriptions, not executable test cases.
- The "reasoning" phase uses `_complete()` with the strategy code and scenario as text — the LLM simulates execution mentally. It cannot catch runtime errors, wrong arithmetic, or state machine bugs.
- `passed = "pass" in result.lower() and "fail" not in result.lower()` — this trivially fails if the LLM writes "this would pass if..." or "not a fail scenario".
- There is no sandboxed Python execution. The test framework is purely text-based simulation.

### 3.6 Model Routing

| Phase | Default Model | Provider |
|---|---|---|
| Generation | claude-sonnet-4-6 | Anthropic |
| Validation fix | gpt-4o-mini | OpenAI |
| Test scenario gen | gpt-4o-mini | OpenAI |
| Scenario reasoning | gpt-4o-mini | OpenAI |
| Submit monitoring | gpt-4o-mini | OpenAI |
| Run analysis | claude-sonnet-4-6 | Anthropic |

There is no fallback: if the configured provider is unreachable, the phase fails. The `_openai` client is initialized conditionally (only if `OPENAI_API_KEY` is set), but if it's None and a phase requires OpenAI, it raises `RuntimeError("No OpenAI API key configured")` — uncaught until it surfaces as an SSE error event.

### 3.7 Q&A Tool Loop — Token Safety

`_stream_with_query_tool` and `_stream_with_query_tool` both cap at `range(20)` iterations. At 4,096 max tokens per round, 20 rounds of tool calls would produce 81,920 tokens. For OpenAI at ~$15/1M tokens (gpt-4o), this is ~$1.23 per maximum-length analysis session. For Claude at higher rates, significantly more. There is **no cost tracking, no budget cap, and no alert**.

---

## 4. Validation Assessment

### 4.1 Validator Service Contract

The validator service (`:8003`) is an external dependency with the contract:
```
POST /validate
Body: {code: str, correlation_id: str}
Response: {validated: bool, errors: [{phase, message}], warnings: [{phase, message}]}
```

The validator is called in three places:
1. `_phase_openai_validate()` — generation fix loop (uses `VALIDATOR_URL`)
2. `_phase_submit()` — submit hard gate (uses `VALIDATOR_PROD_URL`)
3. `run_resubmit_workflow()` — resubmit gate (uses `VALIDATOR_PROD_URL`)

Both `VALIDATOR_URL` and `VALIDATOR_PROD_URL` default to `http://localhost:8003` — pointing at the same instance in the default configuration. A "dev profile" and "prod profile" that share the same service provides no actual gate separation.

**All three call sites are correctly fail-closed.** If the validator is unreachable, the workflow stops (not a regression from previous behavior).

### 4.2 Checklist Coverage Gaps

The 23-item checklist in `constraints.md` and `validation_loop.md` covers structural patterns. Notable gaps:

| Gap | Risk |
|---|---|
| No check that `validate_legs()` is consistent with `leg_schema` | Strategy may pass validation but fail at supervisor instantiation |
| No check that `leg_id` values in order submissions match `leg_schema` | Orders silently discarded by supervisor |
| No check that `Config` is actually the first class in the file | Ordering requirement documented but not validated |
| No check for use of `datetime.now()` without UTC | Timezone bugs in session window logic |
| No check for `self.log.info` vs `self.observe()` | Reasoning events silently lost |
| No check that `stop_reason` values are valid strings | `forced_stop()` with wrong `stop_reason` produces unknown termination type |
| No semantic check that `entry_z > exit_z` | A strategy with inverted thresholds passes all 23 checks |

### 4.3 AST vs. Pattern Matching

The validator appears to use text/regex pattern matching based on the error format `[{phase}] {message}`. True AST validation would:
- Detect `Config` declared after the strategy class at parse time
- Verify method signatures (e.g. `apply_params` takes a `dict`)
- Check that `validate_legs` is a `@classmethod`
- Trace data flow to detect `Decimal`/`float` mixed arithmetic at static analysis time

Without AST analysis, strategies can pass validation with structurally present but semantically wrong implementations.

---

## 5. Runtime Safety Assessment

### 5.1 Code Execution Sandboxing

The submitted strategy code is executed in the NautilusTrader supervisor. The forbidden import gate in the validator blocks:
- `subprocess`, `socket`, `requests`, `os.system`, `urllib`

**Not blocked:**
- `os.popen()`, `os.execv()`, `os.execl()` — OS command execution
- `ctypes` — native code execution
- `importlib.import_module()` — dynamic module loading
- `__import__('os')` — import bypass
- `eval()`, `exec()` — dynamic code execution
- `pathlib.Path` with write operations — file system access
- `httpx`, `aiohttp` — HTTP calls (only `requests` and `socket` are blocked)
- `threading.Thread` — background threads could escape supervisor lifecycle

The validator only checks for literal import names as text. `from os import popen` would not be caught by a check for `os.system`.

### 5.2 Order Safety

Order submission safety is strategy-enforced (not platform-enforced from the Studio side). The Studio:
- Extracts `max_position`, `max_loss`, `max_active_orders_per_side`, `max_order_rate_per_second` from the code for display.
- Does **not** validate that the extracted values are being enforced correctly in the strategy logic.
- Does **not** set any platform-level circuit breakers in the submission payload.

The submission payload has `duration_minutes: 10` hardcoded — a 10-minute session limit. This is the primary safety backstop for runaway strategies.

### 5.3 Concurrent Submission Race

When `_phase_submit()` is called:
1. It calls `_init_strategy_db(strategy_id, code)` on `strategy_submitted` event.
2. The SSE relay opens the event DB in `event_stream()`.

There is a race: the browser opens `EventSource` on `submit_strategy` tool_call (before the HTTP submit completes), and `event_stream()` creates the DB if it doesn't exist. `_init_strategy_db` runs after `strategy_submitted` and wipes `DELETE FROM events`. If events arrive before `_init_strategy_db` runs, those events are written to the DB by the relay, then **deleted** by `_init_strategy_db`. The comment in the code acknowledges this: "may already exist if init_strategy_db fired first" — but the race window where events arrive before `_init_strategy_db` runs is real.

### 5.4 SQL Injection via Query Tool

The `_exec_events_query` function blocks write operations via:
```python
_SQL_WRITE_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA)\b',
    re.IGNORECASE,
)
```

**Not blocked:**
- `EXPLAIN` / `EXPLAIN QUERY PLAN` — reveals schema
- `WITH RECURSIVE ...` — could cause CPU-intensive queries
- Arbitrary complex `JOIN` or subquery chains — no query complexity limit
- SQLite `json_each()` / `json_tree()` on very large payloads

The 200-row return cap helps limit data exfiltration but does not limit computation time.

### 5.5 Strategy File Path Traversal

`_check_name(name)` validates `^[A-Za-z0-9_]{1,128}$`. This correctly blocks path traversal (`../`, `/etc/passwd`). The trader directory construction `STRATEGIES_DIR / trader_id / f"{name}.py"` is safe given the `_check_id` regex on `trader_id`. Well-handled.

---

## 6. Observability & Eventing Assessment

### 6.1 Observe/Decide/Act Architecture

The ODA (Observe/Decide/Act) pattern is the most distinctive architectural element of the platform. Every strategy is required to emit:
- `observe()` — raw signal values from market data
- `decide()` — reasoning about entry/exit
- `act()` — confirmation of order actions

These events flow: Strategy → NautilusTrader supervisor → SSE Gateway (:9001) → Backend relay → SQLite → Frontend.

This is architecturally sound and creates a rich audit trail. The `analyze-execution` endpoint with `query_events` tool turns this into an interactive post-mortem tool.

**Gap:** The ODA events have no enforced schema. Each strategy calls `observe("label", context={...})` with arbitrary context dicts. The `analyze-execution` LLM must infer field names from the raw JSON. Standardizing context schemas per event type would significantly improve analysis quality.

### 6.2 SSE Relay Architecture

```
Browser EventSource → /api/strategies/{id}/events
  ├── SQLite replay (all stored events for the strategy) [reconnect safety]
  ├── If terminal event in replay: return immediately (no gateway connection)
  └── httpx streaming from :9001/stream
      ├── Filter by strategy_id
      ├── Enrich terminal events with termination_type
      ├── Persist to SQLite
      └── Close relay after terminal event
```

**Strength:** The replay-then-live pattern is correct. A browser refresh after strategy completion gets all events from SQLite without reconnecting to the gateway.

**Weakness 1:** The gateway connection at `:9001/stream` subscribes to **all events from all strategies** and filters by `strategy_id` in the relay:
```python
if event_sid and event_sid != strategy_id:
    continue
```
If there are 10 concurrent strategies, each browser tab's SSE relay processes all 10 strategies' events and discards 9/10. This is O(n) fan-in with O(1) useful work per relay instance.

**Weakness 2:** The SSE gateway URL filter happens after event deserialization. A malformed event (missing `strategy_id` field) with `event_sid = ""` passes the filter and is persisted to every strategy's DB that has an open relay. The check `if event_sid and event_sid != strategy_id` skips filtering when `event_sid` is empty.

**Weakness 3:** `es.onerror = () => { /* let browser reconnect */ }` — the browser will reconnect indefinitely if the stream drops. On reconnect, the full SQLite replay fires again, potentially pushing hundreds of duplicate events to the frontend. The `addStrategyEvents` function does not deduplicate.

**Weakness 4:** `MAX_LIVE_EVENTS = 300` caps the in-memory event store but does not prevent unbounded SQLite growth. A long-running strategy or one that emits high-frequency `observe` events will grow the DB indefinitely.

### 6.3 Client-Side Timestamps

`StrategyRawEvent.timestamp = Date.now()` is the browser receive time. The `analysis_latency.md` prompt asks for "observe → decide latency" which requires strategy-side timestamps. The `ts` field in the SQLite events table is also insertion time (backend receive time), not strategy emission time. True latency analysis requires the supervisor to embed event emission timestamps in the event payload, which may or may not happen depending on the NautilusTrader adapter.

### 6.4 Run Database Management

The `DELETE /run-databases` endpoint deletes SQLite files. It validates strategy_id format (`_ID_RE`) to prevent path traversal — correct. However:
- There is no authorization check: any caller can delete any strategy's run DB.
- The `ClearRunsDialog` component lets users bulk-delete all run databases — there is no confirmation of "are you sure you want to delete runs you don't own."
- SQLite files are never automatically pruned or archived.

---

## 7. UX Assessment

### 7.1 Workflow Coverage

| Workflow | Supported | Notes |
|---|---|---|
| Generate new strategy | ✅ | Intent → Code → Review → Submit |
| Fix existing code (Fix Code) | ✅ | Fixed this session — was broken |
| Load and inspect strategy | ✅ | Strategy picker → Monaco editor |
| Resubmit with modified params | ✅ | Change Submission form |
| Save strategy to private store | ✅ | PUT /strategies/{name} |
| Monitor running strategy | ✅ | StrategyEventsFeed + SSE |
| Post-run analysis | ✅ | RunQA with curated + custom prompts |
| View container logs | ⚠️ | UI present, returns 404 (no orchestrator /logs endpoint) |
| Stop / pause / resume | ✅ | Via orchestrator proxy |
| Multi-strategy concurrent | ❌ | One activeStrategyId per browser session |
| Strategy version history | ❌ | No versioning |
| Strategy diff (before/after fix) | ❌ | No diff view |

### 7.2 State Persistence

The `pendingSubmission` state (legs and params awaiting user review) is held only in the Zustand store. A browser refresh loses the pending submission and the user must re-run the workflow. For strategies with long generation times (30-60 seconds), this is a significant friction point.

`savedCode` tracks whether the code has been saved to the strategy file. If the user generates code but does not explicitly save before submitting, `savedCode` diverges from `code`. After the strategy runs, the "saved" version is the file, but the version that ran may differ. There is no "you have unsaved changes" warning at submit time.

### 7.3 Activity Feed Accumulation

The activity feed accumulates entries across the full session with no pagination or virtualization. For a "full" mode workflow with 4 phases plus an active run, the activity list can grow to hundreds of entries. React renders all of them on every state update. This will cause noticeable jank in the UI for long sessions.

### 7.4 Authentication UX

`LoginGate` presents a form for `Trader ID` and `Org ID`. These are stored in `localStorage` and sent as headers. The UI treats this as "authentication," but it is only identification. There is no password, no session token, no expiry. A user who inspects localStorage can read or forge any identity.

### 7.5 Model Selection

The `IntentInput` component likely exposes model selection to the user (three dropdowns for generate/validate/monitor models). The available models list is fetched from `/models`. This is a developer-facing power feature that may confuse non-technical users. There is no "recommended" or "default" preset that hides model complexity.

---

## 8. Production Readiness Assessment

### 8.1 Deployment

| Concern | Status |
|---|---|
| Containerization | ❌ No Dockerfile, no docker-compose |
| Process supervision | ❌ No systemd, supervisord, or PM2 config |
| Horizontal scaling | ❌ Filesystem-based strategy store, SQLite run DBs |
| Health checking | ⚠️ `/health` returns OK without checking dependency liveness |
| Configuration management | ⚠️ `.env` file, no secrets management |
| API versioning | ❌ No version prefix (`/v1/`, `/v2/`) |
| TLS | ❌ No TLS configuration (assumed external termination) |
| Log management | ❌ `print(..., flush=True)` throughout — no structured logging |

### 8.2 Reliability

**Single points of failure:** MCP server (generation fails), validator (submit fails), SSE gateway (monitoring fails), orchestrator (execution fails). All five external services must be up for the full workflow. There is no circuit breaker, no retry (except Claude generation retries at the Anthropic SDK level), and no graceful degradation path.

**No timeout on generation:** The `_phase_claude_generate` stream has no timeout. A hung Anthropic connection will hold the SSE response open indefinitely. The browser will eventually timeout on the fetch, but the server-side generator continues running until the Anthropic SDK times out at the TCP level.

**No request queuing:** Multiple concurrent users each trigger a separate Claude stream, validator call, and MCP session. There is no queue depth limit, no concurrency cap, and no backpressure mechanism.

### 8.3 Data Durability

Strategy code files are stored as plain `.py` files with no versioning. A `PUT /strategies/{name}` overwrites the file without backup. An inadvertent or malicious overwrite permanently destroys the previous strategy.

Run databases in `data/runs/` are SQLite files with no backup mechanism. A disk failure or accidental `rm -rf` loses all execution history.

### 8.4 CORS Configuration

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    ...
)
```

Wildcard CORS on a trading API allows any webpage on any domain to call the strategy control endpoints (stop, pause, resume, submit) via authenticated user sessions. This is a CSRF vector — a malicious page can send stop-strategy requests if the user is "logged in" (has trader headers in localStorage). Correct for development; **must be restricted to known origins before any production deployment.**

---

## 9. Critical Risks

### RISK-1 (RESOLVED) — Validator Fail-Open in Resubmit
**Status:** Fixed this session. `run_resubmit_workflow` now calls `VALIDATOR_PROD_URL` fail-closed. ✅

### RISK-2 (OPEN) — MCP Hard Dependency on Generation
**Severity:** High  
**Description:** All strategy generation fails if `MCP_SERVER_URL` is unreachable. The MCP session is opened inside `run_strategy_workflow` with no fallback. Since MCP tools are never actually called (only listed), this dependency provides no functional value.  
**Blast radius:** 100% of `/run-strategy` calls fail.  
**Mitigation:** Make the MCP session optional. If `sse_client` raises `ConnectionRefused`, continue without emitting `tools_ready` event or emit it with an empty tools list.

### RISK-3 (OPEN) — No Authentication on Trading Control Endpoints
**Severity:** High  
**Description:** `POST /strategies/{id}/stop`, `pause`, `resume` require no authentication. Any caller who can reach port 8089 can stop or pause any strategy by guessing or discovering a `strategy_id`.  
**Mitigation:** Add API key authentication (`X-API-Key` header) or JWT bearer tokens, validated server-side on all execution control endpoints.

### RISK-4 (OPEN) — Init/Relay Race Condition in Run Database
**Severity:** Medium  
**Description:** The browser opens `EventSource` on the `submit_strategy` tool_call event. Events can arrive at the SSE relay before `_init_strategy_db` is called (which runs on `strategy_submitted` event). `_init_strategy_db` executes `DELETE FROM events` — wiping early-arrival events from the DB.  
**Mitigation:** Call `_init_strategy_db` before the submit HTTP call is made, not after the response event fires.

### RISK-5 (OPEN) — Incomplete Code Sandboxing
**Severity:** High  
**Description:** The forbidden import list (`subprocess`, `socket`, `requests`, `os.system`, `urllib`) is incomplete. `os.popen`, `ctypes`, `importlib.import_module`, `__import__`, `eval`, `exec`, and `httpx` (HTTP from inside strategies) are not blocked. An adversarial or hallucinated strategy could make external network calls, read/write files, or execute arbitrary OS commands.  
**Mitigation:** Implement AST-level import scanning (not text matching) that blocks all `os.*` subattributes, all dynamic import mechanisms, all standard-library networking modules, and all code execution primitives. Supplement with a sandbox runtime (e.g. Docker security profile, seccomp, or restricted Python interpreter).

### RISK-6 (OPEN) — Dual Validator URL Default Identity
**Severity:** Medium  
**Description:** `VALIDATOR_URL` and `VALIDATOR_PROD_URL` both default to `http://localhost:8003`. "Dev" and "prod" validation use the same service instance. A bug in the validator that rejects valid strategies will block all submissions in both profiles simultaneously. Conversely, a misconfigured permissive validator in dev will also be permissive at submit time.  
**Mitigation:** Default `VALIDATOR_URL` to the dev validator instance and `VALIDATOR_PROD_URL` to a separate stricter instance. Document this explicitly in the deployment guide.

### RISK-7 (OPEN) — Init Race: `_param_lock` vs `configure()`
**Severity:** Low (currently handled, but fragile)  
**Description:** The strategy contract requires `self._param_lock = RLock()` in `__init__`, not `on_start()`. The validation checklist (rule 17) and prompts document this. However, the validation only checks for the presence of `RLock()` in the file — it cannot verify it's in `__init__`. An LLM that misses the guidance and puts it in `on_start` will pass all 23 validation checks but crash at startup when `configure()` is called before `on_start`.  
**Mitigation:** Add an explicit AST check: `_param_lock` assignment must appear in `__init__` body, not in any other method.

---

## 10. Technical Debt Map

| Item | Severity | File | Description |
|---|---|---|---|
| TD-1 | High | agent.py:903–924 | MCP session opened for all generation; tools never called through MCP; vestigial dependency |
| TD-2 | High | agent.py:173–207 | `_extract_metadata` symbol detection runs on `code` twice, never on `intent`; leg symbol not per-leg |
| TD-3 | Medium | agent.py:580–635 | `_unnest_config_classes` uses indentation regex as proxy for nesting; breaks on 2-space indent or tabs |
| TD-4 | Medium | agent.py:153–171 | `_extract_code_from_text` uses "last block" heuristic; silently fails with no diagnostic info |
| TD-5 | Medium | main.py:799–835 | SSE relay: empty `event_sid` passes `strategy_id` filter; any untagged event persists to all open relay DBs |
| TD-6 | Medium | main.py:258–266 | `_init_strategy_db` called after `strategy_submitted` event, not before submit; early events can be wiped by `DELETE FROM events` |
| TD-7 | Medium | App.tsx:295–302 | `pendingSubmission` not persisted to sessionStorage; browser refresh loses the submission form |
| TD-8 | Low | agent.py throughout | `print(f"[submit]...", flush=True)` — no structured logging, no log levels, no rotation |
| TD-9 | Low | main.py:1068–1075 | `ask_run` endpoint builds flat string context; superseded by `analyze_execution` but not removed |
| TD-10 | Low | App.tsx:320–425 | `handleRun` and `handleResubmit` duplicate ~80% of event handling logic |
| TD-11 | Low | agent.py:538–578 | `_phase_reason_test_scenarios` uses `"pass" in result.lower()` for pass/fail detection; trivially incorrect |
| TD-12 | Low | main.py:61–64 | `/health` endpoint reports `ok` without checking any dependency liveness |
| TD-13 | Low | main.py:52 | `CORS allow_origins=["*"]` — wildcard; must be restricted before production |
| TD-14 | Low | auth.ts | localStorage-based trader identity — no server-side session validation |
| TD-15 | Low | agent.py:37 | `VALIDATOR_URL` and `VALIDATOR_PROD_URL` default to same host — no env separation |

---

## 11. Priority Improvements

### Priority 1 — Security & Safety (Do First)

1. **Make MCP optional in `run_strategy_workflow`** — wrap the `sse_client` open in a try/except; if MCP is unreachable, continue without emitting `tools_ready` or emit with an empty list. This eliminates the largest single point of failure.

2. **Restrict CORS origins** — replace `allow_origins=["*"]` with the known frontend origin(s). In development, read from `CORS_ALLOWED_ORIGINS` env var.

3. **Add API key auth to execution control endpoints** — `/strategies/{id}/stop|pause|resume|params` should require a valid `X-API-Key` header checked against a server-side secret. Do not rely on `X-Trader-Id` for authorization.

4. **Expand the forbidden import list** — add `os.popen`, `os.exec*`, `ctypes`, `importlib`, `eval`, `exec`, `compile`, `__import__`, `aiohttp`, `httpx` to the validator's blocked list. Switch from text matching to AST scanning.

5. **Fix the DB init race** — call `_init_strategy_db(strategy_id, code)` before the HTTP submit POST, not after `strategy_submitted` fires.

### Priority 2 — Reliability

6. **Structured logging** — replace `print(f"[submit]...", flush=True)` with `import logging; logger = logging.getLogger(__name__)` with INFO/WARNING/ERROR levels. Add a startup log of all configured service URLs.

7. **Health check with dependency liveness** — `/health` should ping each configured service URL and report which dependencies are up/down.

8. **Generation timeout** — add a `asyncio.timeout(120)` guard around `_phase_claude_generate` to prevent hung Anthropic connections from holding server resources indefinitely.

9. **Persist `pendingSubmission` to sessionStorage** — on `params_ready`, write the legs/params/code to `sessionStorage`. On mount, if `sessionStorage` has a pending submission, restore it.

10. **SQLite run DB size limit** — add a `MAX_DB_EVENTS` constant (e.g. 10,000). When `INSERT INTO events` would exceed the limit, either drop oldest or refuse with a warning event.

### Priority 3 — Correctness

11. **Fix `_extract_metadata` symbol loop** — either search `intent` explicitly as a second pass, or accept that symbol detection for multi-leg strategies requires explicit per-leg extraction from `leg_schema`.

12. **Fix leg symbol assignment** — for multi-leg strategies, parse each leg's symbol from `leg_schema` labels or code comments rather than assigning the same symbol to all legs.

13. **Fix SSE relay untagged event filter** — change `if event_sid and event_sid != strategy_id: continue` to `if event_sid != strategy_id: continue` so events with no strategy_id are dropped, not broadcast.

14. **Add validation checklist item for `Config`-first ordering** — the validator should AST-check that `Config(LumitecStrategyConfig)` is defined before `ConfigParams` and the strategy class.

15. **Persist code on params_ready** — when `params_ready` fires, also call `PUT /strategies/{name}` to save the generated code. This closes the gap between code-in-editor and code-in-file.

---

## 12. Strategic Recommendations

### 12.1 Decouple Generation from MCP

The current architecture chains all workflow phases inside a single MCP session context. This was appropriate when MCP tools were being called during generation. Now that validation and submission use direct HTTP, the MCP session should be managed independently (or removed entirely from the generation workflow). The `tools_ready` event can be driven by a static capability manifest or a lightweight `GET /capabilities` call.

### 12.2 Introduce an Async Job Queue

The current implementation streams the AI workflow synchronously via SSE. For production use with multiple concurrent users, this creates backpressure on the single FastAPI process. Consider:
- A lightweight async task queue (e.g. `arq` backed by Redis) for generation jobs
- A job status endpoint (`GET /jobs/{job_id}/status`) that streams SSE from the queue worker
- This decouples the browser connection lifetime from the workflow lifetime

### 12.3 Real Strategy Versioning

Strategy files currently have one version (the latest). Implement a simple version store:
- On `PUT /strategies/{name}`, write to `{name}/{name}-{timestamp}.py` and maintain `{name}/latest.py` as a symlink
- `GET /strategies/{name}/history` returns a list of versions
- `GET /strategies/{name}?version={ts}` returns a specific version

This enables rollback after a bad generation and provides an audit trail without requiring a full Git integration.

### 12.4 AST-Based Contract Validation

Replace the text/regex pattern matching in the validator with a proper Python AST visitor:

```python
import ast

class StrategyContractChecker(ast.NodeVisitor):
    # Check Config class exists and is first class in file
    # Check ConfigParams has @dataclass(frozen=True)
    # Check __init__ contains self._param_lock = RLock()
    # etc.
```

This would correctly handle:
- Multi-line expressions
- Different whitespace styles
- Forward reference detection
- Method placement (e.g. `_param_lock` in `__init__` not `on_start`)

### 12.5 Standardize ODA Event Schema

Define a JSON schema for observe/decide/act context payloads:

```json
{
  "observe": {"signal_name": "z_score", "value": 2.72, "pair_state": "FLAT", "timestamp_ns": ...},
  "decide":  {"decision": "entry_signal", "z": 2.72, "threshold": 2.0, "action": "ENTER"},
  "act":     {"action": "submit_limit", "symbol": "AAPL", "side": "BUY", "qty": 100, "price": "184.25", "order_id": "O-..."}
}
```

Standardized schemas dramatically improve the quality of `analyze-execution` analysis since the LLM can write precise SQL filters against known field names.

---

## 13. Recommended Future Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser (React/Vite)                               │
│  - JWT-based session (short-lived, server-issued)   │
│  - Pending state persisted to sessionStorage        │
│  - Strategy diff view (before/after)                │
└────────────────┬────────────────────────────────────┘
                 │ HTTPS (TLS terminated at load balancer)
┌────────────────▼────────────────────────────────────┐
│  API Gateway / Auth Layer                           │
│  - JWT validation                                   │
│  - Rate limiting per trader                         │
│  - CORS restricted to known origins                 │
└──┬─────────────────────────────────────┬────────────┘
   │                                     │
┌──▼──────────┐                ┌─────────▼──────────┐
│  Studio API  │                │  Generation Worker  │
│  (main.py)  │                │  (agent.py, async)  │
│  - Strategy  │                │  - Job queue backed │
│    CRUD      │   Job submit   │    by Redis/arq     │
│  - SSE relay │ ─────────────> │  - No MCP dep       │
│  - Proxying  │                │  - Timeout guards   │
│  - Run DBs   │                │  - Cost tracking    │
└─────────────┘                └────────────────────┘
        │
        ├── PostgreSQL (strategy versions, run metadata)
        ├── Object store (strategy code, run DB archives)
        └── Prometheus metrics (latency, cost, error rate)
```

The key changes:
1. **Separate generation workers from the serving API** — generation is async and long-running; it belongs in a worker process, not in the request handler.
2. **Replace SQLite with PostgreSQL** — for multi-replica deployment, shared RDBMS is necessary.
3. **Replace filesystem strategy store with versioned object store** — S3-compatible stores provide versioning and durability out of the box.
4. **Add proper auth** — JWT tokens issued by a proper auth service (or integrated with an identity provider).

---

## 14. Immediate High-Impact Fixes

The following changes can be made in the current codebase with minimal risk and high impact. Listed in order of impact-to-effort ratio:

### Fix A — Make MCP Optional
**File:** `backend/agent.py`, `run_strategy_workflow`  
**Change:** Wrap `sse_client(MCP_SERVER_URL)` in try/except. On `ConnectionRefused` or `OSError`, skip tool discovery and emit `{"type": "tools_ready", "tools": [], "count": 0}`. Continue the workflow.  
**Impact:** Eliminates RISK-2; generation works even when MCP is down.

### Fix B — DB Init Before Submit
**File:** `backend/main.py`, `run_strategy` event handler  
**Change:** Move `await _init_strategy_db(strategy_id, code)` to execute when the `tool_call` event with `name == "submit_strategy"` fires (same place the `EventSource` is opened), not on the `strategy_submitted` event.  
**Impact:** Eliminates RISK-4; no early events are wiped.

### Fix C — SSE Untagged Event Filter
**File:** `backend/main.py`, SSE relay `event_stream()`  
**Change:** Change `if event_sid and event_sid != strategy_id: continue` to `if event_sid != strategy_id: continue`.  
**Impact:** Stops untagged gateway events from being persisted to every open strategy relay DB.

### Fix D — Restrict CORS
**File:** `backend/main.py`  
**Change:** `allow_origins=[os.getenv("CORS_ORIGINS", "http://localhost:5174")]` and parse as comma-separated list.  
**Impact:** Closes CSRF vector on trading control endpoints; zero functional impact in development.

### Fix E — Health Check Liveness
**File:** `backend/main.py`, `/health`  
**Change:** Add a check for each configured service URL. Return `{"status": "ok", "dependencies": {"validator": "up", "orchestrator": "up", "mcp": "down"}}` with 200 if core services are up, 503 if critical ones are down.  
**Impact:** Enables load balancer health checks and ops monitoring.

### Fix F — Add Generation Timeout
**File:** `backend/agent.py`, `_phase_claude_generate()`  
**Change:** Wrap the `_stream_text` async for loop in `asyncio.timeout(120)`. On `TimeoutError`, yield a `{"type": "error", "message": "Generation timed out after 120 seconds"}` event and return.  
**Impact:** Prevents hung Claude connections from holding server resources; improves user experience with a clear error.

---

## 15. Long-Term Platform Evolution Recommendations

### 15.1 Multi-Tenant Isolation

The current architecture has a single supervisor (`NUAM-DEV`), single strategy store namespace (trader_id subdirectory), and single run DB directory. For multi-tenant use:
- Each tenant needs an isolated supervisor process or namespace
- Strategy stores need tenant-level encryption at rest
- Run DBs should be partitioned by tenant with access controls
- The `strategy_id` scheme (`{ClassName}-{uid8}-ECX_001`) encodes a hardcoded account ID (`ECX_001`) — this prevents routing to different accounts without code changes

### 15.2 Strategy Governance Workflow

The current workflow is: generate → validate → submit. For institutional use, a governance layer is needed:
- **Draft** state: generated and validated but not submitted
- **Review** state: submitted for human review (compliance, risk team)
- **Approved** state: cleared for live execution
- **Archived** state: stopped and filed with run record

The platform has the building blocks (strategy files, run DBs, SSE events) but no governance state machine.

### 15.3 Backtesting Integration

The current Studio submits strategies to a live simulation supervisor. A backtesting tier would:
- Accept the same strategy code
- Run against historical data (NautilusTrader backtest mode)
- Return the same ODA events to the same `analyze-execution` endpoint
- Provide pre-submit validation of strategy logic on historical data

This would significantly reduce the risk of submitting untested logic to the live supervisor.

### 15.4 Model Cost Governance

With two LLM providers and up to 20-round tool loops, a single analysis session can generate significant API costs. Recommended:
- Per-request cost estimation before execution ("this analysis may use ~$0.15 of tokens — continue?")
- Per-trader daily budget cap (configurable, hard stop when exceeded)
- Cost logging per strategy_id in the run DB meta table
- Weekly cost report aggregated across all traders

### 15.5 Strategy Performance Benchmarking

The current `analyze-execution` workflow answers qualitative questions about a run. A benchmark layer would:
- Compute standardized metrics (Sharpe ratio, max drawdown, fill ratio, slippage per fill) across all runs for a strategy
- Compare against a paper trading baseline (random entry/exit)
- Track performance degradation over time (alpha decay detection)
- Surface this data in the Studio UI as a "Strategy Performance Card"

---

## Appendix A — File Inventory

| File | Lines | Purpose |
|---|---|---|
| `backend/agent.py` | ~1,200 | AI orchestration, workflow phases, metadata extraction |
| `backend/main.py` | ~1,100 | FastAPI server, endpoints, SSE relay, SQLite helpers |
| `backend/requirements.txt` | 8 | Python dependencies |
| `backend/prompts/strategy_structure.md` | ~450 | Strategy contract: base class, hooks, API reference |
| `backend/prompts/strategy_generation.md` | ~200 | Generation process: 5-step method, forbidden patterns |
| `backend/prompts/validation_loop.md` | ~110 | Fix agent: 23-item checklist with minimal fixes |
| `backend/prompts/constraints.md` | ~120 | Required limits, forbidden imports, ordering rules |
| `backend/prompts/strategy_testing.md` | ~60 | Test scenario JSON schema |
| `backend/prompts/strategy_reasoning.md` | ~20 | Scenario reasoning pass/fail template |
| `backend/prompts/simulation_monitor.md` | ~80 | Live audit agent: state schema, tracking rules |
| `backend/prompts/analysis_performance.md` | ~40 | Post-run performance analysis prompt |
| `backend/prompts/analysis_latency.md` | ~40 | Post-run latency analysis prompt |
| `backend/prompts/analysis_behavior.md` | ~35 | Post-run behavior/decision quality prompt |
| `frontend/src/App.tsx` | ~650 | Root component, global state, workflow orchestration |
| `frontend/src/types.ts` | ~90 | SSE event type definitions |
| `frontend/src/auth.ts` | ~30 | localStorage-based trader identity |
| `frontend/src/components/` | ~8 files | ActivityFeed, CodePanel, RunQA, StatusStepper, etc. |
| `data/strategies/shared/ECX_001/claude_pairs_strategy.py` | ~1,000+ | AAPL/MSFT stat-arb reference strategy |

## Appendix B — Open Issues Summary

| ID | Severity | Category | Summary |
|---|---|---|---|
| RISK-2 | High | Reliability | MCP hard dependency in generation workflow |
| RISK-3 | High | Security | No auth on execution control endpoints |
| RISK-4 | Medium | Correctness | DB init race: early events wiped on strategy start |
| RISK-5 | High | Security | Incomplete code sandboxing — `os.popen`, `ctypes`, etc. not blocked |
| RISK-6 | Medium | Operations | Dev and prod validator defaults point to same service |
| RISK-7 | Low | Correctness | `_param_lock` placement validation is text-only |
| TD-1 | High | Architecture | MCP vestigial dependency |
| TD-2 | High | Correctness | `_extract_metadata` symbol bug + leg symbol assignment |
| TD-3–15 | Low–Med | Various | See Technical Debt Map §10 |

---

*Review conducted in read-only mode. No application or platform files were modified during this review.*
