# Lumitec Strategy Studio — Ops Runbook

| | |
|---|---|
| **Last Updated** | 2026-09-03 |
| **Maintained By** | Lumitec |
| **Status** | Active development |

## Change Log

| Date | Changes |
|------|---------|
| 2026-09-03 | **Fix: dev `validation_profile` was dropped on resubmit.** `ResubmitStrategyRequest` in `main.py` didn't declare `validation_profile`, so Pydantic silently discarded the field the frontend sends and every resubmit validated as `prod` (rejecting dev-only code like `open()`). Added the field, passed it into `run_resubmit_workflow()`, and replaced the inline ternary in `agent.py` with `_normalize_validation_profile()` — `dev`/`development`/`research` (case-insensitive) → `development`, everything else incl. `prod`/`None` → `production` (the old expression mishandled a literal `"development"`). Tests: `backend/tests/test_resubmit_validation_profile.py` (3). **NOTE:** `pytest` is not in `backend/requirements.txt` or the venv — installed ad hoc this session (`pip install pytest` into `backend/.venv`); add it to a dev-requirements file if tests become routine. |
| 2026-09-02 | **Security fix (`4028e38`): the strategy-events SSE relay is now auth-gated.** `/strategies/{id}/events` requires a valid Cognito token, accepted as a `?token=` query param (EventSource can't send an `Authorization` header). In cloud mode `_iter_websocket_events` now forwards that token to the Kafka fanout (`wss://events.clouddesk.lumitec.com/`), which requires it at handshake and filters events by the caller's entitled supervisors (`lumitec-event-bridge`) — previously the relay connected anonymously and was almost certainly being rejected in cloud deployments. `auth.py` gained `resolve_claims_and_token()`; frontend `App.tsx` appends `peekIdToken()` to the EventSource URL. Local-mode `_iter_gateway_events` (talks to `oms-sse-gateway`, no auth concept) is unchanged. |
| 2026-08-28 | Created this runbook. Studio's Cognito auth + real command-plane migration landed in two commits (`08bc034`, `14e432d`) — code complete, **not yet live**: the Cognito app client / web UI infra in `lumitec-desk-cloud/terraform/my.plan` has not been applied. Added prompt caching (`cache_control: ephemeral`) on the static Anthropic system prompts in `backend/agent.py`. |

---

## Contents

1. [What This App Is](#what-this-app-is)
2. [Architecture](#architecture)
3. [Repository Structure](#repository-structure)
4. [Environment Setup](#environment-setup)
5. [Running Locally](#running-locally)
6. [API Surface (backend/main.py)](#api-surface-backendmainpy)
7. [Current Operational Status](#current-operational-status)
8. [Known Open Issues](#known-open-issues)
9. [Hard Constraints](#hard-constraints)
10. [Related Docs](#related-docs)

---

## What This App Is

A web app where traders describe or paste a trading strategy and Claude autonomously
executes the lifecycle (generate → validate → submit → simulate). The frontend is a
Monaco-editor-based React app; the backend is a thin FastAPI relay between the browser,
Claude (Anthropic API), and the real trading command plane in `lumitec-desk-cloud`.

**Stack**
- Frontend: React 18 + Zustand + Monaco Editor (`@monaco-editor/react`), Vite dev server on port `5174`
- Backend: FastAPI on port `8089`, SSE streaming to the browser
- Agent: Anthropic streaming API (`messages.stream` / `messages.create`) — see `backend/agent.py`
- Auth: Cognito Hosted UI (OAuth code flow) — see `backend/auth.py`, `frontend/src/auth/`
- Command plane: real `lumitec-desk-cloud` orchestrator + strategy_server, reached over HTTPS API Gateway

## Architecture

```
Browser (Vite :5174)
  ├── fetch  → :8089 (FastAPI) → MCP (:8002)              control plane: generate / test / reason
  ├── fetch  → :8089 (FastAPI) → ORCHESTRATOR_URL          submit / stop / pause / resume / status / logs
  └── SSE    → :8089 (FastAPI) → SSE_GATEWAY_URL           real-time strategy events (ws:// or http://)
```

The browser talks only to `:8089`. The backend is the single intermediary:
proxies REST calls to the orchestrator, relays real-time events (WebSocket or SSE
depending on `SSE_GATEWAY_URL`'s scheme — see `main.py`'s `is_websocket_source` check),
and holds the MCP session used for the LLM-driven generate/test/reason phases.

**Port map**

| Port | Role |
|---|---|
| 5174 | Frontend dev server (Vite) |
| 8089 | Studio backend (FastAPI) — the only thing the browser talks to |
| 8002 | MCP server (control-plane tools: generate / test scenarios / reasoning) |
| — | Orchestrator + SSE gateway are **remote** now (see below), not local ports |

**Local dev vs. real deployment — one codebase, config-selected.** Nothing branches
on "which environment am I in"; behavior is entirely driven by env vars:
- `ORCHESTRATOR_URL` / `STRATEGY_SERVER_URL` point at either `localhost:8000`/`8001`
  (local `order-strategy-system` sandbox) or the real API Gateway URL in
  `lumitec-desk-cloud`.
- `SSE_GATEWAY_URL`'s **scheme** picks the transport: `ws://`/`wss://` → WebSocket
  fanout (real deployment), `http://`/`https://` → local SSE gateway.
- `COGNITO_USER_POOL_ID` / `COGNITO_REGION` being unset effectively disables Cognito
  JWT verification (local/no-auth mode); setting them turns on real verification.

## Repository Structure

```
backend/
  main.py              FastAPI app — all HTTP/SSE endpoints, thin proxy to orchestrator
  agent.py             Claude/OpenAI orchestration: generate, validate, submit, monitor phases
  auth.py              Cognito JWT verification + demo entitlement resolution
  prompts/             Static system prompt files (structure, generation, validation_loop, testing, reasoning, monitor)
  requirements.txt
  .env / .env.example
frontend/
  src/App.tsx          Top-level store + layout
  src/auth/            cognito.ts (Hosted UI OAuth flow), sessionStore.ts
  src/components/      CodePanel, IntentInput, RunQA, StrategyLogs, ClearRunsDialog, LoginGate, ...
  .env.local / .env.example
data/
  strategies/          Strategy source files the supervisor loads — see note below
  runs/                Per-run SQLite result databases
```

**Strategy files the supervisor loads live at**
`data/strategies/shared/ECX_001/` and `data/strategies/<TRADER_ID>/` —
edit these, not any copies under `lumitec-strategy-workspace/`.

## Environment Setup

Copy both example files and fill in real values:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
```

**`backend/.env`**
| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key |
| `MCP_SERVER_URL` | Control-plane MCP server (default `http://localhost:8002/sse`) |
| `PORT` | Backend port (default `8089`) |
| `ORCHESTRATOR_URL` / `STRATEGY_SERVER_URL` / `STRATEGY_SERVER_PUBLISH_PATH` | Real command-plane base URLs — see `lumitec-desk-cloud/OPS_RUNBOOK.md` for the current invoke URL if it's changed |
| `SSE_GATEWAY_URL` | Real-time event source; scheme (`ws(s)://` vs `http(s)://`) picks transport |
| `COGNITO_USER_POOL_ID` / `COGNITO_REGION` / `COGNITO_APP_CLIENT_ID` | Cognito verification config — **`COGNITO_APP_CLIENT_ID` is blank until `terraform apply` runs**, see [Current Operational Status](#current-operational-status) |
| `DEMO_USER_ENTITLEMENTS` | JSON map, Cognito email → `{account_id, trader_id, supervisor_ids}`. Manually-maintained stopgap — adding a user here does NOT grant access; access is enforced by the orchestrator's entitlements table (grant via `lumitec-desk-cloud/scripts/seed_entitlement.py`) |

**`frontend/.env.local`**
| Var | Purpose |
|---|---|
| `VITE_COGNITO_DOMAIN` | Hosted UI domain (shared with `lumitec-desk-ui`) |
| `VITE_COGNITO_CLIENT_ID` | Studio's own dedicated app client — **blank until `terraform apply` runs** |
| `VITE_COGNITO_REDIRECT_URI` / `VITE_COGNITO_LOGOUT_URI` | OAuth callback/logout URLs, default `http://localhost:5174/...` |

## Running Locally

```bash
# Backend (FastAPI, :8089)
cd backend
source .venv/bin/activate
python main.py

# Frontend (Vite, :5174)
cd frontend
npm run dev
```

## API Surface (backend/main.py)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + reports configured `mcp_server` |
| GET | `/models` | Available LLM models |
| GET/PUT | `/strategies`, `/strategies/{name}` | List / load / save strategy source |
| POST | `/parse-strategy` | Parse pasted code into metadata |
| POST | `/resubmit-strategy` | Re-run submit for existing code |
| POST | `/publish-strategy` | Publish to shared strategies dir |
| POST | `/run-strategy` | Kick off the full generate→validate→submit→simulate workflow (SSE) |
| POST | `/strategies/{id}/stop` \| `/pause` \| `/resume` | Proxy to orchestrator |
| GET | `/strategies/{id}/status` \| `/logs` \| `/events` | Proxy / relay from orchestrator + SSE gateway. `/events` requires a Cognito token as a `?token=` query param (EventSource can't set a header); in cloud mode the token is forwarded to the fanout, which filters by entitlement |
| GET | `/strategies/{id}/run-context` | Metadata for a past run |
| GET/DELETE | `/run-databases` | List / clear run result DBs |
| GET | `/analysis-prompts` | Curated post-run analysis prompts |
| POST | `/analyze-execution`, `/ask-run` | Post-run Q&A over a run's data |

All routes are registered **without** an `/api` prefix — the Vite dev proxy strips
`/api/*` → `/*` before forwarding to `:8089`. See [[feedback_vite_proxy_and_json]] memory.

## Current Operational Status

- **Cognito auth + real-infra submit flow: code complete, not yet live.**
  `backend/auth.py` and `frontend/src/auth/cognito.ts` are implemented and merged
  (commits `08bc034`, `14e432d`, `4028e38`), and `_phase_submit` in `agent.py` now
  submits directly to the real orchestrator with real
  `account_id`/`trader_id`/`supervisor_id`. The strategy-events SSE relay is also
  auth-gated now and forwards the caller's token to the cloud fanout (`4028e38`).
  However, `COGNITO_APP_CLIENT_ID` / `VITE_COGNITO_CLIENT_ID` are still blank —
  the terraform plan that creates the Studio's dedicated Cognito app client and
  web UI infra (`lumitec-desk-cloud/terraform/my.plan`, generated 2026-08-23) has
  **not been applied**. Do not apply while EC2 instances are down (nightly
  scheduler stops them outside `cron(0 9 ? * MON-THU *)`–`cron(0 17 ...)` ET) —
  check `lumitec-desk-cloud/OPS_RUNBOOK.md`'s latest §17 SOD entry first.
  Next steps once applied: fill in both client-ID env vars with the real
  `module.cognito.studio_user_pool_client_id` output, then test the login flow
  end-to-end.
- **Standalone strategy validator is gone from the pre-submit loop.** There is no
  HTTP validate endpoint reachable from Studio in the real deployment (the
  validator Lambda is IAM-restricted, only invokable from inside the
  orchestrator/strategy_server Lambdas). The generate-time loop now only runs
  free local checks (e.g. market-data lifecycle shape); full validation happens
  at submit time against the real orchestrator, which returns 422 with
  structured errors that feed back into the LLM fix loop.
- **Prompt caching added** on the static generation/fixing/testing/reasoning
  system prompts (`agent.py`, `_stream_text` / `_complete`) via
  `cache_control: {"type": "ephemeral"}`. Not yet measured against real traffic —
  worth checking `usage.cache_read_input_tokens` on a live run.

## Known Open Issues

- No timeout warning to the user if the SSE/WebSocket relay silently loses
  connection during a simulation — not actively broken, just unguarded.
- VSCode may show false-positive import squiggles in `agent.py`/`main.py` if the
  editor isn't pointed at `backend/.venv/bin/python` (it defaults to system Python).
- **`duration_minutes` name collision.** `LumitecStrategyConfig`
  (`order-strategy-system/lumitec/strategy/config.py`) reserves `duration_minutes`
  (`0` = run indefinitely); the supervisor injects the orchestration value into
  every strategy's `Config` (`lumitec_controller.py`). A strategy that also
  declares its own `duration_minutes` param with a `> 0` rule (e.g. the latency
  probe) fails to start: `Failed to start strategy …: duration_minutes must be
  > 0`. `_phase_submit` hardcodes `"duration_minutes": 10` in the submit payload
  regardless of the strategy's parsed value. Not yet fixed — options: Studio
  sends the strategy's real `strategy_params["duration_minutes"]` instead of the
  hardcode, or the strategy renames its param. Same trap applies to any other
  name a strategy shares with a reserved `LumitecStrategyConfig` field.
- Backend `--reload` (WatchFiles) can hang on "Waiting for connections to close"
  when an outbound cloud-fanout WebSocket from an `/events` relay is still open;
  kill and restart the process (`lsof -ti tcp:8089 | xargs kill -9`) rather than
  waiting it out.

## Hard Constraints

- **Never touch supervisor code** (`order-strategy-system/`). It's a separate,
  stable system — bugs are always in the Studio, not there.
- **Never commit or push** in this repo (or `lumitec-desk-cloud`) without a fresh,
  explicit ask in the current turn. Working-tree edits are fine; committing/pushing
  is not assumed.
- **`lumitec-strategy-workspace/CLAUDE.md` is not used by the Studio** — ignore it
  entirely; all Studio behavior is defined by this repo's own code.
- FastAPI routes must **not** include an `/api` prefix (the Vite proxy strips it).
- Always parse JSON before comparing response fields — never string-match on
  serialized JSON (spacing isn't guaranteed).

## Related Docs

- `lumitec-desk-cloud/OPS_RUNBOOK.md` — authoritative for the command-plane infra
  itself: terraform apply procedure, EC2 inventory, SOD verification status,
  current invoke URLs.
