# Lumitec Strategy Studio — Ops Runbook

| | |
|---|---|
| **Last Updated** | 2026-09-18 |
| **Maintained By** | Lumitec |
| **Status** | Active development |

## Change Log

| Date | Changes |
|------|---------|
| 2026-09-18 | **External diagnosis (not fixed here): event-broadcast ordering bug in `order-strategy-system`.** A template strategy whose single order fills and completes it in one pass never showed an `order.fill` event in the browser feed — jumped straight from `order.accepted` to `leg.partial`. Confirmed via a raw Kafka consume (see "Kafka event pipeline" under Known Open Issues) that the event *does* reach `lumitec.oms.events`, but lands after `strategy.completed`. Root cause is deterministic, not a race: `supervisor/core/event_router.py`'s `ingest_event()` runs the aggregation pipeline (which sends `leg.partial`/`strategy.completed`, derived from the raw event) *before* reaching the unconditional fallback that sends the raw event itself — so any raw event whose aggregation cascade includes a strategy completion is always broadcast after its own effects. A precise handoff brief (exact fix: move the raw-event send to right after `ui_event["seq"] = next(_UI_SEQ)`, before aggregation) was written for an `order-strategy-system` session — **not yet applied**. See Known Open Issues. |
| 2026-09-18 | **External fix (deployed): registry publish-idempotency + purge Decimal-format bug, `lumitec-desk-cloud`.** Republishing byte-identical code was minting a new registry revision (+ S3 objects) every time; `publish_strategy()` in `lambdas/strategy_server/lambda_function.py` now compares the incoming publish against the `LATEST` item's content (code hash, params, leg_schema, mission/objective/leg_mode, submission_method) and returns `{"status": "unchanged"}` instead of writing when nothing changed (`force: true` bypasses the check). The same deploy also carried the `_s3_key()` `Decimal`/`:010d` formatting fix for `purge_strategy()` that had been diagnosed earlier (crashed after deleting the `LATEST` DynamoDB row but before touching `VERSION` rows or S3 — see `simple_agent`'s leftover state under Known Open Issues). Confirmed live: Lambda `lumitec-demo-strategy-server` redeployed 2026-09-18T14:50:33Z (new `CodeSha256`, new runtime init). No code changed in this repo. |
| 2026-09-18 | **External TODO (handoff written, not implemented): `lumitec-desk-ui` doesn't surface `logic_id`/`version`.** `src/wiring/api/strategyServer.ts`'s `StrategySummary`/`GetCodeResponse` drop the registry's identity fields entirely. Handoff brief written (add the four identity fields to both types + populate from the registry response; show a version badge in `NewStrategyDialog.tsx`'s picker around line 314; deliberately do **not** thread identity into `mapDraftToOrchestrator.ts`/`orchestrator.ts`'s submit payload — mirrors the same decision made in this repo, see below). Not yet applied. |
| 2026-09-18 | **`feat: unpublish and permanent-delete (purge) for published strategies`** (`d37050a`). Two owner-only registry mutation proxies: `DELETE /published-strategies/{sid}` (unpublish — hides from everyone but the owner, reversible by republishing) and `DELETE /published-strategies/{sid}/purge` (permanent — requires already-unpublished + `{"confirm": true}`; uses `httpx`'s `.request("DELETE", ..., json=...)` since `.delete()` has no body param). Extracted `CodePanel.tsx`'s inline `ConfirmDialog`/`AlertDialog` into shared `frontend/src/components/Dialogs.tsx` so `IntentInput.tsx`'s new Unpublish / "Delete permanently…" buttons (Sandbox / Published Agents picker) can reuse them. Tests: `backend/tests/test_unpublish_purge.py` (9). Verified live end-to-end via direct DynamoDB/S3 checks (see the two 2026-09-18 external-fix rows above for the purge Decimal-bug context). |
| 2026-09-18 | **`feat: immutable strategy identity (logic_id/version/sha256) across publish and browse`** (`0d93126`). `publish-strategy` computes `sha256` of the stripped code, forwards it (and a remembered `logic_id` from a per-trader `.registry_links.json`) so republishing under the same local name updates the existing registry entry instead of minting a duplicate; response now includes `logic_id`/`version`/`sha256`. `GET /published-strategies` and `GET /published-strategies/{sid}` normalize the same three fields (plus `submission_method`/`unpublished` on the single-strategy route), falling back to legacy `strategy_id`/`user_version`/`strategy_hash` field names for registry entries that predate the canonical ones. Studio's publish badge and the Sandbox/Published-Agents picker both surface `logic_id`/`sha256`/a short version label (`"1.0.0 rev2"`); the badge also distinguishes a no-op ("unchanged") republish from a real new version once the registry started returning `status: "unchanged"` (see the idempotency fix above). Deliberately **not** threaded into the resubmit-to-orchestrator payload — identity describes the artifact, submission describes what to run. Tests: `backend/tests/test_publish_visibility.py` (+4), `test_published_library.py` (+2). |
| 2026-09-18 | **`feat: clarify mission/objective guidance; require forced_stop() everywhere`** (`6993776`). Root cause of "strategy fills its order, logs it's stopping, but never emits a terminal event" (UI hangs forever): every example in `backend/prompts/strategy_structure.md`/`strategy_generation.md` used `objective = SIGNAL_DRIVEN` — the one objective the supervisor's aggregator never auto-completes — and the prompt told the model to call plain `self.stop()` on success, which skips the `FORCED_STOP` lifecycle event the aggregator needs to ever emit a terminal SSE event (`constraints.md` already said to always use `forced_stop()`; this file contradicted it). Replaced the bare mission/objective enum lists with decision tables (when to use each, and whether the supervisor auto-completes it or the strategy must call `forced_stop()` itself), swapped the repeated example to `EXECUTION`/`TARGET_QTY`/`FINITE`, and rewrote "Graceful stop" to always require `forced_stop(reason, stop_reason)` — `stop_reason="MANUAL"` for normal completion, now a documented valid value alongside `TIME`/`RISK`/`SYSTEM`. Verified: re-ran the same template-agent prompt, confirmed `strategy.completed` fired (`stop_reason: null` — aggregator auto-completed via `TARGET_QTY`, no explicit `forced_stop()` call even needed). |
| 2026-09-18 | **`fix: catch Config re-nesting introduced by the validation fix loop`** (`f4f8b64`). `_unnest_config_classes()` only ran once, before `_phase_submit`'s retry loop started; a fix-loop patch that itself re-nested `Config`/`ConfigParams` (e.g. adding a missing method inside the wrong class) went uncaught on every subsequent retry, so the same still-broken code kept getting resubmitted. Now re-run after every patch. Also strengthened `validation_loop.md`'s Pattern-0 instructions to say explicitly where `Config` belongs. |
| 2026-09-18 | **`fix: unsubscribe_market_data() wrong keyword args in prompt + validator`** (`fe698ae`). Both the generation prompt's examples and Studio's own local validator regex (`agent.py`'s `_QUOTE_UNSUB_RE`) used `subscribe_quotes=`/`subscribe_trades=` for the *un*subscribe call — the real keyword args are `unsubscribe_quotes`/`unsubscribe_trades` (`lumitec/strategy/base.py`). The wrong names would `TypeError` at runtime, yet the validator's regex was written to match them, so it passed broken code and rejected correct code. |
| 2026-09-18 | **`fix: update stale Claude model config (4.6 -> 5)`** (`4a0c292`). Sonnet/Opus 4.6 were superseded by the 5 family; `DEFAULT_GENERATE_MODEL`/`AVAILABLE_MODELS` in `agent.py`, the model picker in `IntentInput.tsx`, and `App.tsx`'s default all still pointed at the old names. |
| 2026-09-09 | **Publish visibility forwarding.** The strategy server gained a per-strategy visibility model (`private`/`shared`/`public`/`platform`). `PublishStrategyRequest` now carries `visibility` (default `private`); `publish_strategy` forwards it verbatim in the publish payload (identity stays server-derived from Cognito). `CodePanel.tsx` adds a `private`/`shared`/`public` selector next to Publish (`platform` deliberately omitted — admin-assigned only), sends `visibility` in the body, and shows a `"…visibility not permitted"` message on a 403. Tests: `backend/tests/test_publish_visibility.py` (3). **End-to-end verify (publish each value on the live server + cross-org read) still pending — needs live Cognito auth + two org-scoped users.** |
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
| `COGNITO_USER_POOL_ID` / `COGNITO_REGION` / `COGNITO_APP_CLIENT_ID` | Cognito verification config — populated, terraform applied, see [Current Operational Status](#current-operational-status) |
| `DEMO_USER_ENTITLEMENTS` | JSON map, Cognito email → `{account_id, trader_id, supervisor_ids}`. Manually-maintained stopgap — adding a user here does NOT grant access; access is enforced by the orchestrator's entitlements table (grant via `lumitec-desk-cloud/scripts/seed_entitlement.py`) |

**`frontend/.env.local`**
| Var | Purpose |
|---|---|
| `VITE_COGNITO_DOMAIN` | Hosted UI domain (shared with `lumitec-desk-ui`) |
| `VITE_COGNITO_CLIENT_ID` | Studio's own dedicated app client — populated, terraform applied |
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
| POST | `/publish-strategy` | Publish current code to the strategy server. Body takes `visibility` (`private` default \| `shared` \| `public` \| `platform`) forwarded as-is in the payload; owner/account/org are derived server-side from the Cognito token. Server 422s an unknown value, 403s a `platform` publish without `platform-admins`. Studio UI offers `private`/`shared`/`public` only. Also computes+forwards `sha256`, reuses a remembered `logic_id` for the same local name (updates instead of duplicating), and returns `logic_id`/`version`/`sha256` — see identity metadata in the change log |
| GET | `/published-strategies` | Read-only registry proxy — list entries visible to the caller, each tagged `mine`/`group` and normalized `logic_id`/`version`/`sha256` |
| GET | `/published-strategies/{sid}` | Read-only registry proxy — merged metadata + source for opening a published strategy in the editor (Sandbox / Published Agents picker) |
| DELETE | `/published-strategies/{sid}` | Unpublish (owner-only) — hides from everyone but the owner; reversible by publishing again under the same `logic_id` |
| DELETE | `/published-strategies/{sid}/purge` | Permanently delete (owner-only) — body requires `{"confirm": true}`; registry enforces already-unpublished as a two-step gate |
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

- **Cognito auth + real-infra submit flow: live.** `backend/auth.py` and
  `frontend/src/auth/cognito.ts` are implemented and merged (commits
  `08bc034`, `14e432d`, `4028e38`), and `_phase_submit` in `agent.py` submits
  directly to the real orchestrator with real
  `account_id`/`trader_id`/`supervisor_id`. The strategy-events SSE relay is
  auth-gated and forwards the caller's token to the cloud fanout (`4028e38`).
  The terraform plan that creates the Studio's dedicated Cognito app client
  and web UI infra has since been applied — `COGNITO_APP_CLIENT_ID` /
  `VITE_COGNITO_CLIENT_ID` are populated in `backend/.env` /
  `frontend/.env.local`, and this whole 2026-09-18 session's live testing
  (publish/unpublish/purge, etc.) went through real Cognito login. Proactive
  ID-token refresh was added 2026-09-18 (see change log) so a long session no
  longer 401s once the 1-hour ID token goes stale.
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
- **Registry identity + unpublish/purge are live and verified end-to-end.**
  Publish/browse now carry `logic_id`/`version`/`sha256`; unpublish and purge
  were confirmed directly against DynamoDB/S3 (`lumitec-demo-strategy-registry`
  / `lumitec-demo-strategy-artifacts`, `AWS_PROFILE=clouddesk-demo`). Republish
  idempotency (identical code → no new revision) and the purge Decimal-format
  bug were both fixed and deployed on the `lumitec-desk-cloud` side
  2026-09-18 — see change log. `lumitec-desk-ui` does not yet surface any of
  this (handoff written, not applied — see Known Open Issues).

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
  waiting it out. Also seems to kill the frontend dev process — restart both
  together.
- **`order.fill` (and any raw order/strategy event whose aggregation cascade
  completes the strategy) can arrive after `strategy.completed` — deterministic
  ordering bug in `order-strategy-system`, not fixed here.** `ingest_event()`
  in `supervisor/core/event_router.py` sends the aggregation-derived events
  (`leg.partial`, `strategy.completed`, …) *before* the unconditional fallback
  that sends the raw triggering event itself, so the raw event's own effects
  always reach the wire first. Studio's `/events` relay (`main.py`,
  `api_strategy_events`) closes the stream on the first terminal event, so it
  never sees the straggler. **Fix belongs in `order-strategy-system`** (move
  the raw-event send to immediately after `ui_event["seq"] = next(_UI_SEQ)`,
  before aggregation) — a precise handoff brief was written 2026-09-18, not
  yet applied. No Studio-side change needed once that lands (Studio's
  close-on-terminal behavior becomes correct once ordering is guaranteed at
  the source).
- **`simple_agent` registry entry is stuck half-purged.** An earlier purge
  attempt hit the (now-fixed) Decimal-format bug: it deleted the `LATEST`
  DynamoDB item but crashed before touching the `VERSION#*` rows or their S3
  objects (`lambda_function.py`'s old `_s3_key()` couldn't format a
  `Decimal` revision). Now that the fix is deployed, purging `simple_agent`
  again from Studio should clean up the rest — hasn't been re-run.
- **`lumitec-desk-ui` doesn't surface `logic_id`/`version`.** Handoff brief
  written 2026-09-18 (`src/wiring/api/strategyServer.ts` types + a version
  badge in `NewStrategyDialog.tsx`'s picker); not yet applied.

**Diagnosing "an event never arrived" reports — check Kafka directly.**
The real-deployment event pipeline is: supervisor's local OMS/Admin SSE
gateways → `event-bridge` (SSE client, on the supervisor EC2 host,
`i-0be3aa163d4167a9d`) → self-managed single-node KRaft Kafka broker on EC2
(`i-02bc6f3412abeef82`, private IP `10.10.10.63:9092`, topic
`lumitec.oms.events` — 7-day retention) → `event-bridge-fanout` (Kafka
consumer + WebSocket broadcaster, on the Kafka host) → Studio's `/events`
relay. To check whether an event actually made it into Kafka (vs. never
being produced at all), use SSM on the Kafka host:
```bash
aws ssm send-command --instance-ids i-02bc6f3412abeef82 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["timeout 25 /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 --topic lumitec.oms.events \
    --from-beginning --timeout-ms 20000 | grep -i <strategy_id suffix>"]'
# then: aws ssm get-command-invocation --command-id <id> --instance-id i-02bc6f3412abeef82
```
Compare each event's `source_seq` (original order) against `ts_ingested`
(actual Kafka arrival order) — a mismatch is exactly the ordering bug above.
Both EC2 instances stop nightly outside `lumitec-desk-cloud`'s scheduler
window — check they're running first (`aws ec2 describe-instances`).

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
