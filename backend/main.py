"""
Lumitec Strategy Studio — FastAPI backend.

Endpoints:
  POST /run-strategy                          — streams SSE as Claude executes workflow
  GET  /strategies                            — list .py files for the trader
  GET  /strategies/{name}                     — return source code
  PUT  /strategies/{name}                     — save to trader's private directory
  GET  /health                                — liveness check

  Execution plane proxy (→ Orchestrator port 8000):
  POST /api/strategies/{strategy_id}/stop     — stop a running strategy
  POST /api/strategies/{strategy_id}/pause    — pause a running strategy
  POST /api/strategies/{strategy_id}/resume   — resume a paused strategy
  PATCH /api/strategies/{strategy_id}/params  — update strategy parameters
  GET  /api/strategies/{strategy_id}/status   — get strategy status

  SSE relay + run persistence (→ SSE Gateway port 9001 + SQLite):
  GET  /api/strategies/{strategy_id}/events         — real-time event stream (relayed + persisted)
  GET  /api/strategies/{strategy_id}/run-context    — structured run data from SQLite
  POST /api/analyze-execution                        — LLM analysis using SQLite run context

Directory layout (under STRATEGIES_DIR):
  {trader_id}/           ← trader's private strategies
  shared/{org_id}/       ← org-shared strategies (read-only via this API)

Run DB layout (under RUNS_DIR):
  {strategy_id}.db       ← SQLite: events table + meta (code, termination_type, stored_at)
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import (
    run_strategy_workflow,
    run_resubmit_workflow,
    _parse_submission,
    _stream_text,
    _anthropic,
    _openai,
    _provider,
    _TERMINAL_EVENTS,
    _resolve_termination_type,
    ORCHESTRATOR_URL,
    AVAILABLE_MODELS,
    DEFAULT_GENERATE_MODEL,
    DEFAULT_VALIDATE_MODEL,
    DEFAULT_MONITOR_MODEL,
)
from auth import resolve_claims, resolve_trading_identity

load_dotenv()

SSE_GATEWAY_URL = os.getenv("SSE_GATEWAY_URL", "http://localhost:9001")
STRATEGY_SERVER_URL = os.getenv("STRATEGY_SERVER_URL", "http://localhost:8001")
STRATEGY_SERVER_PUBLISH_PATH = os.getenv("STRATEGY_SERVER_PUBLISH_PATH", "/strategies/publish")

app = FastAPI(title="Lumitec Strategy Studio")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STRATEGIES_DIR = Path(
    os.getenv(
        "STRATEGIES_DIR",
        str(Path(__file__).parent.parent / "data" / "strategies"),
    )
)
RUNS_DIR = Path(
    os.getenv(
        "RUNS_DIR",
        str(Path(__file__).parent.parent / "data" / "runs"),
    )
)
PROMPTS_DIR = Path(__file__).parent / "prompts"

_ANALYSIS_PROMPTS: dict[str, dict] = {
    "performance": {
        "id": "performance",
        "label": "Analyze Performance",
        "file": "analysis_performance.md",
    },
    "latency": {
        "id": "latency",
        "label": "Analyze Latency",
        "file": "analysis_latency.md",
    },
    "behavior": {
        "id": "behavior",
        "label": "Analyze Behavior",
        "file": "analysis_behavior.md",
    },
}

_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,128}$')

# SQL safety: block any statement that mutates or inspects the schema
_SQL_WRITE_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA)\b',
    re.IGNORECASE,
)

# Tool definition reused by Anthropic and OpenAI paths
_QUERY_TOOL_DESC = (
    "Run a read-only SQL SELECT against this strategy's execution event database.\n"
    "Schema:\n"
    "  events(id INTEGER, ts INTEGER -- milliseconds since epoch, event_type TEXT, raw TEXT -- JSON payload)\n"
    "  meta(key TEXT, value TEXT)    -- keys: strategy_id, code, stored_at, termination_type\n"
    "Access JSON fields inside raw with: json_extract(raw, '$.field_name')\n"
    "Common event_type values depend on the adapter; e.g. order_submitted, order_filled, "
    "order_cancelled, position_update, error.\n"
    "Maximum 200 rows returned per query."
)
_QUERY_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": "A SQL SELECT statement only. Write operations are blocked.",
        }
    },
    "required": ["sql"],
}


def _check_id(value: str, field: str) -> str:
    if not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
    return value


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid strategy name")
    return name


def _trader_dir(trader_id: str) -> Path:
    return STRATEGIES_DIR / trader_id


def _shared_dir(org_id: str) -> Path:
    return STRATEGIES_DIR / "shared" / org_id


async def _get_trader_id(request: Request) -> str:
    """Studio's own private-strategy-folder scoping key — the caller's
    verified Cognito sub, not the trading-domain trader_id (see auth.py's
    TradingIdentity for that, used only at submit time)."""
    claims = await resolve_claims(request)
    return _check_id(claims.sub, "trader_id")


async def _get_org_id(request: Request) -> str | None:
    claims = await resolve_claims(request)
    if not claims.org_id:
        return None
    return _check_id(claims.org_id, "org_id")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RunStrategyRequest(BaseModel):
    intent: str
    strategy_name: str | None = None
    existing_code: str | None = None
    workflow_mode: str = "fast"   # "fast" (1→2→5→6) | "full" (1→2→3→4→5→6)
    generate_model: str | None = None
    validate_model: str | None = None
    monitor_model: str | None = None


class SaveStrategyRequest(BaseModel):
    code: str


class ParseStrategyRequest(BaseModel):
    code: str


class ResubmitStrategyRequest(BaseModel):
    code: str
    legs: list[dict]
    strategy_params: dict = {}
    monitor_model: str | None = None
    start_time: str | None = None   # ISO 8601 UTC — if omitted, defaults to NYSE open
    end_time: str | None = None     # ISO 8601 UTC — if omitted, defaults to NYSE close
    supervisor_id: str              # which supervisor to submit to (e.g. "USA-1", "SPAIN-1")


class PublishStrategyRequest(BaseModel):
    name: str
    code: str


class AskRunRequest(BaseModel):
    question: str
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]
    context: str = ""          # serialized run context (events, state, code)
    model: str | None = None


class AnalyzeExecutionRequest(BaseModel):
    strategy_id: str
    question: str
    history: list[dict] = []
    model: str | None = None


# ---------------------------------------------------------------------------
# SQLite run persistence helpers
# ---------------------------------------------------------------------------

def _db_path(strategy_id: str) -> Path:
    return RUNS_DIR / f"{strategy_id}.db"


async def _init_strategy_db(strategy_id: str, code: str) -> None:
    """Create the run DB and persist the strategy source code before the relay opens."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_db_path(strategy_id)) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts INTEGER NOT NULL,"
            "event_type TEXT NOT NULL,"
            "raw TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY,"
            "value TEXT NOT NULL)"
        )
        # Wipe any events from a previous run with the same strategy_id
        await db.execute("DELETE FROM events")
        now = datetime.now(timezone.utc).isoformat()
        await db.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [("strategy_id", strategy_id), ("code", code), ("stored_at", now)],
        )
        await db.commit()


async def _build_run_context(strategy_id: str) -> dict | None:
    """Read the run DB and return a structured context dict."""
    db_file = _db_path(strategy_id)
    if not db_file.exists():
        return None
    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT key, value FROM meta") as cur:
            meta = {row[0]: row[1] for row in await cur.fetchall()}
        async with db.execute("SELECT ts, event_type, raw FROM events ORDER BY id") as cur:
            raw_rows = await cur.fetchall()
    rows = [{"ts": r[0], "event_type": r[1], "raw": json.loads(r[2])} for r in raw_rows]

    total_fills = sum(1 for r in rows if "filled" in r["event_type"].lower())
    errors = [r["raw"] for r in rows if "fail" in r["event_type"].lower() or "error" in r["event_type"].lower()]
    event_counts: Counter[str] = Counter(r["event_type"] for r in rows)

    slippages: list[float] = []
    pnl_values: list[float] = []
    for r in rows:
        data = r["raw"].get("data") if isinstance(r["raw"].get("data"), dict) else r["raw"]
        if isinstance(data, dict):
            slip = data.get("slippage")
            if slip is not None:
                try:
                    slippages.append(float(slip))
                except (TypeError, ValueError):
                    pass
            pnl = data.get("realized_pnl") or data.get("pnl")
            if pnl is not None:
                try:
                    pnl_values.append(float(pnl))
                except (TypeError, ValueError):
                    pass

    avg_slippage = sum(slippages) / len(slippages) if slippages else None
    max_drawdown: float | None = None
    if pnl_values:
        peak = pnl_values[0]
        max_dd = 0.0
        for v in pnl_values:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
        max_drawdown = max_dd

    return {
        "strategy_id": strategy_id,
        "stored_at": meta.get("stored_at", ""),
        "termination_type": meta.get("termination_type", "UNKNOWN"),
        "code": meta.get("code", ""),
        "stats": {
            "total_events": len(rows),
            "total_fills": total_fills,
            "avg_slippage": avg_slippage,
            "max_drawdown": max_drawdown,
            "event_counts": dict(event_counts),
        },
        "errors": errors[:10],
        "recent_events": rows[-50:],
    }


def _format_context_for_llm(ctx: dict) -> str:
    """Convert a run context dict into a prompt-ready string for the LLM."""
    parts: list[str] = []

    code = ctx.get("code", "")
    if code:
        parts.append(f"## Strategy Code\n```python\n{code}\n```")

    stats = ctx.get("stats", {})
    stat_lines = [
        f"- Termination: {ctx.get('termination_type', 'UNKNOWN')}",
        f"- Total events received: {stats.get('total_events', 0)}",
        f"- Fill count: {stats.get('total_fills', 0)}",
    ]
    avg_slip = stats.get("avg_slippage")
    if avg_slip is not None:
        stat_lines.append(f"- Avg slippage: {avg_slip:.4f}")
    max_dd = stats.get("max_drawdown")
    if max_dd is not None:
        stat_lines.append(f"- Max drawdown: ${max_dd:.2f}")
    event_counts = stats.get("event_counts", {})
    if event_counts:
        top = sorted(event_counts.items(), key=lambda x: -x[1])[:10]
        stat_lines.append("- Event breakdown: " + ", ".join(f"{k}\u00d7{v}" for k, v in top))
    parts.append("## Execution Stats\n" + "\n".join(stat_lines))

    errors = ctx.get("errors", [])
    if errors:
        err_lines = [f"- {json.dumps(e)[:300]}" for e in errors[:5]]
        parts.append("## Errors\n" + "\n".join(err_lines))
    else:
        parts.append("## Errors\nNone recorded")

    recent = ctx.get("recent_events", [])
    if recent:
        lines = [
            f"[{r['ts']}] {r['event_type']}: {json.dumps(r['raw'])[:300]}"
            for r in recent[-30:]
        ]
        parts.append("## Recent Event Log\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _format_orientation_summary(ctx: dict) -> str:
    """Condensed run overview used as the LLM orientation header.
    Deliberately omits the raw event log — the LLM queries that via query_events."""
    parts: list[str] = []

    code = ctx.get("code", "")
    if code:
        parts.append(f"## Strategy Code\n```python\n{code}\n```")

    stats = ctx.get("stats", {})
    stat_lines = [
        f"- Termination: {ctx.get('termination_type', 'UNKNOWN')}",
        f"- Total events recorded: {stats.get('total_events', 0)}",
        f"- Fill count: {stats.get('total_fills', 0)}",
    ]
    avg_slip = stats.get("avg_slippage")
    if avg_slip is not None:
        stat_lines.append(f"- Avg slippage: {avg_slip:.4f}")
    max_dd = stats.get("max_drawdown")
    if max_dd is not None:
        stat_lines.append(f"- Max drawdown: ${max_dd:.2f}")
    event_counts = stats.get("event_counts", {})
    if event_counts:
        top = sorted(event_counts.items(), key=lambda x: -x[1])[:10]
        stat_lines.append("- Event breakdown: " + ", ".join(f"{k}\u00d7{v}" for k, v in top))
    parts.append("## Execution Stats\n" + "\n".join(stat_lines))

    errors = ctx.get("errors", [])
    if errors:
        err_lines = [f"- {json.dumps(e)[:300]}" for e in errors[:5]]
        parts.append("## Errors\n" + "\n".join(err_lines))
    else:
        parts.append("## Errors\nNone recorded")

    return "\n\n".join(parts)


async def _exec_events_query(strategy_id: str, sql: str) -> list[dict]:
    """Execute a read-only SELECT against the strategy's run DB. Returns rows as list of dicts."""
    if _SQL_WRITE_RE.search(sql):
        return [{"error": "Only SELECT queries are permitted"}]
    db_file = _db_path(strategy_id)
    if not db_file.exists():
        return [{"error": f"No run database for strategy_id '{strategy_id}'"}]
    try:
        async with aiosqlite.connect(db_file) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql) as cur:
                rows = await cur.fetchmany(200)
                if not rows:
                    return []
                columns = [d[0] for d in cur.description]
                return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        return [{"error": str(exc)}]


async def _stream_with_query_tool(
    strategy_id: str,
    model: str,
    system: str,
    user: str,
) -> AsyncIterator[str]:
    """Multi-turn LLM loop with a query_events tool.
    Yields text deltas; may execute one or more SQL queries before the final answer."""
    provider = _provider(model)

    if provider == "anthropic":
        tool_def = {
            "name": "query_events",
            "description": _QUERY_TOOL_DESC,
            "input_schema": _QUERY_TOOL_SCHEMA,
        }
        messages: list[dict] = [{"role": "user", "content": user}]

        for _round in range(20):  # safety cap on tool-call rounds
            resp = await _anthropic.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                messages=messages,
                tools=[tool_def],
            )

            text_parts: list[str] = []
            tool_uses = []
            for block in resp.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            for part in text_parts:
                yield part

            if not tool_uses:
                break

            yield "\n\n*[querying run database\u2026]*\n\n"

            # Serialize assistant content blocks back to plain dicts
            assistant_content = []
            for block in resp.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for tu in tool_uses:
                sql = tu.input.get("sql", "")
                rows = await _exec_events_query(strategy_id, sql)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(rows),
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            yield "\n\n*[Query budget exhausted — analysis may be incomplete.]*"

    else:  # OpenAI
        if not _openai:
            yield "OpenAI API key not configured."
            return

        tool_def_oa = {
            "type": "function",
            "function": {
                "name": "query_events",
                "description": _QUERY_TOOL_DESC,
                "parameters": _QUERY_TOOL_SCHEMA,
            },
        }
        messages_oa: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        for _round in range(20):
            resp_oa = await _openai.chat.completions.create(
                model=model,
                max_tokens=4096,
                messages=messages_oa,
                tools=[tool_def_oa],
                tool_choice="auto",
                temperature=0.1,
            )
            msg = resp_oa.choices[0].message
            if msg.content:
                yield msg.content
            if not msg.tool_calls:
                break

            yield "\n\n*[querying run database\u2026]*\n\n"

            messages_oa.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                sql = args.get("sql", "")
                rows = await _exec_events_query(strategy_id, sql)
                messages_oa.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(rows),
                })
        else:
            yield "\n\n*[Query budget exhausted — analysis may be incomplete.]*"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/models")
async def list_models():
    return {
        "models": AVAILABLE_MODELS,
        "defaults": {
            "generate": DEFAULT_GENERATE_MODEL,
            "validate": DEFAULT_VALIDATE_MODEL,
            "monitor": DEFAULT_MONITOR_MODEL,
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "mcp_server": os.getenv("MCP_SERVER_URL", "http://localhost:8002/sse")}


@app.get("/strategies")
async def list_strategies(request: Request):
    """Return strategies available to the trader: private first, then org-shared."""
    trader_id = await _get_trader_id(request)
    org_id = await _get_org_id(request)

    results: list[dict] = []

    private_dir = _trader_dir(trader_id)
    if private_dir.exists():
        for p in sorted(private_dir.glob("*.py")):
            if not p.name.startswith("_"):
                results.append({"name": p.stem, "source": "private"})

    if org_id:
        shared_dir = _shared_dir(org_id)
        private_names = {r["name"] for r in results}
        if shared_dir.exists():
            for p in sorted(shared_dir.glob("*.py")):
                if not p.name.startswith("_") and p.stem not in private_names:
                    results.append({"name": p.stem, "source": "shared"})

    return {"strategies": results}


@app.get("/strategies/{name}")
async def get_strategy(name: str, request: Request):
    """Return source code — private copy takes priority over shared."""
    _check_name(name)
    trader_id = await _get_trader_id(request)
    org_id = await _get_org_id(request)

    path = _trader_dir(trader_id) / f"{name}.py"
    source = "private"

    if not path.exists() and org_id:
        path = _shared_dir(org_id) / f"{name}.py"
        source = "shared"

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Strategy '{name}' not found")

    return {"name": name, "code": path.read_text(), "source": source}


@app.put("/strategies/{name}")
async def save_strategy(name: str, request: Request, body: SaveStrategyRequest):
    """Save to trader's private directory (creates the file if it does not exist yet)."""
    _check_name(name)
    trader_id = await _get_trader_id(request)

    private_dir = _trader_dir(trader_id)
    private_dir.mkdir(parents=True, exist_ok=True)

    path = private_dir / f"{name}.py"
    path.write_text(body.code)
    return {"name": name, "saved": True, "source": "private"}


# ---------------------------------------------------------------------------
# Execution plane proxy (→ real Orchestrator, API Gateway + Cognito authorizer)
# Every call forwards the caller's Authorization bearer token unchanged —
# the orchestrator's own Cognito authorizer + entitlement check is the real
# enforcement point, not this backend. supervisor_id is required on every
# call since a strategy could be running on any supervisor the caller
# submitted it to (USA-1, SPAIN-1, ...) — there is no longer a single
# fixed default.
# ---------------------------------------------------------------------------

def _auth_headers(request: Request) -> dict[str, str]:
    auth = request.headers.get("Authorization", "")
    return {"Authorization": auth} if auth else {}


@app.post("/strategies/{strategy_id}/stop")
async def api_stop_strategy(strategy_id: str, supervisor_id: str, request: Request):
    """Stop a running strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}/stop"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=_auth_headers(request), timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"stopped": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/strategies/{strategy_id}/pause")
async def api_pause_strategy(strategy_id: str, supervisor_id: str, request: Request):
    """Pause a running strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}/pause"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=_auth_headers(request), timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"paused": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/strategies/{strategy_id}/resume")
async def api_resume_strategy(strategy_id: str, supervisor_id: str, request: Request):
    """Resume a paused strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}/resume"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=_auth_headers(request), timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"resumed": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.patch("/strategies/{strategy_id}/params")
async def api_update_params(strategy_id: str, supervisor_id: str, request: Request):
    """Update strategy parameters via Orchestrator."""
    body = await request.json()
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}/params"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(url, json=body, headers=_auth_headers(request), timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json() if response.content else {"updated": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/strategies/{strategy_id}/logs")
async def api_strategy_logs(strategy_id: str, supervisor_id: str, request: Request, since: str = "1h", n: int = 200):
    """Proxy strategy container logs from the Orchestrator supervisor API."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}/logs"
    params = {"since": since, "n": min(n, 2000)}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=_auth_headers(request), timeout=30.0)
        for code in (404, 500, 502, 504):
            if response.status_code == code:
                raise HTTPException(status_code=code, detail=response.text)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/strategies/{strategy_id}/status")
async def api_get_status(strategy_id: str, supervisor_id: str, request: Request):
    """Get strategy status from Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{supervisor_id}/strategies/{strategy_id}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=_auth_headers(request), timeout=10.0)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Strategy not found")
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# SSE relay (→ SSE Gateway port 9001)
# ---------------------------------------------------------------------------

async def _iter_gateway_events():
    """Yield raw parsed event dicts from a local SSE gateway (HTTP GET,
    text/event-stream) — the order-strategy-system local-dev topology."""
    gateway_url = f"{SSE_GATEWAY_URL}/stream"
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            gateway_url,
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            timeout=None,
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue


async def _iter_websocket_events():
    """Yield raw parsed event dicts from the real-time WebSocket fanout
    (lumitec-desk-cloud's Kafka fanout, e.g. wss://events.clouddesk.lumitec.com/).
    No auth handshake, no subscribe message, no per-connection filtering —
    it broadcasts every event for every strategy; the caller filters by
    strategy_id, same as the gateway path. Matches lumitec-desk-ui's client
    (src/services/sseClient.ts) — plain WebSocket, JSON messages with a
    `type` field."""
    async with websockets.connect(SSE_GATEWAY_URL, open_timeout=10) as ws:
        async for raw in ws:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


@app.get("/strategies/{strategy_id}/events")
async def api_strategy_events(strategy_id: str):
    """
    Subscribe to real-time events for a specific strategy, filtered by
    strategy_id, enriched with termination_type on terminal events, and
    streamed to the browser as SSE.

    Upstream source is picked by SSE_GATEWAY_URL's scheme: ws:// or wss://
    connects to the real-time WebSocket fanout (real deployment); http:// or
    https:// polls a local SSE gateway (order-strategy-system local dev).
    """
    is_websocket_source = SSE_GATEWAY_URL.startswith(("ws://", "wss://"))

    async def event_stream():
        db = None
        try:
            # Open (or create) the run DB — may already exist if init_strategy_db fired first
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(_db_path(strategy_id))
            await db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "ts INTEGER NOT NULL,"
                "event_type TEXT NOT NULL,"
                "raw TEXT NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "key TEXT PRIMARY KEY,"
                "value TEXT NOT NULL)"
            )
            await db.commit()

            # ── Replay stored events first ────────────────────────────────────
            # If the browser reconnects (or opens late), SQLite already has
            # everything the relay previously received.  Replay it so the
            # frontend never misses events — including terminal ones.
            already_terminal = False
            async with db.execute("SELECT raw FROM events ORDER BY id") as cur:
                async for (raw_json,) in cur:
                    yield f"data: {raw_json}\n\n"
                    try:
                        ev = json.loads(raw_json)
                        if "termination_type" in ev:
                            already_terminal = True
                    except Exception:
                        pass
            if already_terminal:
                # Strategy already finished — no need to connect to live gateway.
                return

            event_source = _iter_websocket_events() if is_websocket_source else _iter_gateway_events()
            async for event in event_source:
                # Resolve event type — skip heartbeats and empty events
                event_type = event.get("event_type") or event.get("type", "")
                if not event_type:
                    continue

                # Filter by strategy_id
                data_field = event.get("data")
                event_sid = event.get("strategy_id") or (data_field.get("strategy_id", "") if isinstance(data_field, dict) else "")
                if event_sid and event_sid != strategy_id:
                    continue

                # Enrich terminal events with termination_type
                if event_type in _TERMINAL_EVENTS:
                    content_str = json.dumps(event)
                    event["termination_type"] = _resolve_termination_type(event_type, content_str)

                # Persist to SQLite
                ts_ms = int(time.time() * 1000)
                await db.execute(
                    "INSERT INTO events (ts, event_type, raw) VALUES (?, ?, ?)",
                    (ts_ms, event_type, json.dumps(event)),
                )
                if "termination_type" in event:
                    await db.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES ('termination_type', ?)",
                        (event["termination_type"],),
                    )
                await db.commit()

                yield f"data: {json.dumps(event)}\n\n"

                # Stop relaying after terminal event
                if "termination_type" in event:
                    break

        except Exception as exc:
            yield f"data: {json.dumps({'type': 'relay_error', 'message': str(exc)})}\n\n"
        finally:
            if db:
                await db.close()
            yield "data: {\"type\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/parse-strategy")
async def parse_strategy(body: ParseStrategyRequest):
    """Parse legs and ConfigParams from strategy code for the Change Submission form."""
    return _parse_submission(body.code)


@app.post("/resubmit-strategy")
async def resubmit_strategy(body: ResubmitStrategyRequest, request: Request):
    """Resubmit an existing validated strategy with updated legs and params (skips generation + validation)."""
    claims = await resolve_claims(request)
    identity = resolve_trading_identity(claims)
    if body.supervisor_id not in identity.supervisor_ids:
        raise HTTPException(
            status_code=403,
            detail=f"Not entitled to supervisor '{body.supervisor_id}' (entitled: {identity.supervisor_ids})",
        )
    auth_header = request.headers.get("Authorization", "")

    async def event_stream():
        try:
            async for event in run_resubmit_workflow(
                code=body.code,
                legs=body.legs,
                strategy_params=body.strategy_params,
                monitor_model=body.monitor_model,
                start_time=body.start_time,
                end_time=body.end_time,
                account_id=identity.account_id,
                trader_id=identity.trader_id,
                supervisor_id=body.supervisor_id,
                auth_header=auth_header,
            ):
                # Initialize the run DB immediately when the strategy is accepted by the
                # Orchestrator — before the browser opens the EventSource relay, ensuring
                # the DB (with source code) exists when the first events arrive.
                if event.get("type") == "strategy_submitted":
                    try:
                        await _init_strategy_db(event["strategy_id"], body.code)
                    except Exception as db_exc:
                        print(f"[db] init failed for {event['strategy_id']}: {db_exc}", flush=True)
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: {\"type\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/publish-strategy")
async def publish_strategy(body: PublishStrategyRequest, request: Request):
    """Publish/register current strategy code via strategy server API."""
    _check_name(body.name)
    trader_id = await _get_trader_id(request)
    org_id = await _get_org_id(request)
    if not org_id:
        raise HTTPException(status_code=400, detail="Caller's identity has no organization_id — cannot publish")

    publish_url = f"{STRATEGY_SERVER_URL.rstrip('/')}{STRATEGY_SERVER_PUBLISH_PATH}"
    file_name = body.name if body.name.endswith(".py") else f"{body.name}.py"
    payload = {
        "code": body.code,
        "file_name": file_name,
        "display_name": body.name,
    }
    # strategy_server resolves identity from the Cognito claims API Gateway
    # injects for a verified bearer token, not from these X- headers — kept
    # only as a harmless local-storage-scoping hint, forwarding is what matters.
    upstream_headers = {
        "X-Trader-Id": trader_id,
        "X-Org-Id": org_id,
        **_auth_headers(request),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(publish_url, json=payload, headers=upstream_headers)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reach strategy server: {exc}")

    if response.status_code not in (200, 201):
        detail = response.text[:2000] if response.text else f"HTTP {response.status_code}"
        raise HTTPException(status_code=response.status_code, detail=f"Strategy server publish failed: {detail}")

    try:
        upstream = response.json()
    except Exception:
        upstream = {"raw": response.text[:2000] if response.text else ""}

    return {
        "status": "success",
        "published": True,
        "name": body.name,
        "file_name": file_name,
        "source": "strategy_server",
        "org_id": org_id,
        "trader_id": trader_id,
        "upstream": upstream,
    }


@app.post("/run-strategy")
async def run_strategy(request: RunStrategyRequest):
    """
    Accepts a trader intent (and optional pre-loaded strategy code) and streams
    SSE events as Claude autonomously executes the strategy lifecycle via MCP tools.
    """
    async def event_stream():
        try:
            async for event in run_strategy_workflow(
                intent=request.intent,
                strategy_name=request.strategy_name,
                existing_code=request.existing_code,
                workflow_mode=request.workflow_mode,
                generate_model=request.generate_model,
                validate_model=request.validate_model,
                monitor_model=request.monitor_model,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: {\"type\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/strategies/{strategy_id}/run-context")
async def api_run_context(strategy_id: str):
    """Return structured run context for a strategy (reads from SQLite run DB)."""
    ctx = await _build_run_context(strategy_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"No run data found for strategy '{strategy_id}'")
    return ctx


# ---------------------------------------------------------------------------
# Run database management
# ---------------------------------------------------------------------------

@app.get("/analysis-prompts")
async def get_analysis_prompts():
    """Return the three curated post-run analysis prompts, loaded from disk."""
    result = []
    for meta in _ANALYSIS_PROMPTS.values():
        path = PROMPTS_DIR / meta["file"]
        try:
            question = path.read_text(encoding="utf-8").strip()
        except OSError:
            question = ""
        result.append({"id": meta["id"], "label": meta["label"], "question": question})
    return {"prompts": result}


@app.get("/run-databases")
async def list_run_databases():
    """List all run database files under RUNS_DIR with basic metadata."""
    if not RUNS_DIR.exists():
        return {"databases": []}
    entries: list[dict] = []
    for db_file in sorted(RUNS_DIR.glob("*.db")):
        stat = db_file.stat()
        strategy_id = db_file.stem
        stored_at: str | None = None
        event_count = 0
        try:
            async with aiosqlite.connect(db_file) as db:
                async with db.execute("SELECT value FROM meta WHERE key = 'stored_at'") as cur:
                    row = await cur.fetchone()
                    if row:
                        stored_at = row[0]
                async with db.execute("SELECT COUNT(*) FROM events") as cur:
                    cnt = await cur.fetchone()
                    if cnt:
                        event_count = cnt[0]
        except Exception:
            pass
        entries.append({
            "strategy_id": strategy_id,
            "stored_at": stored_at,
            "event_count": event_count,
            "size_bytes": stat.st_size,
        })
    return {"databases": entries}


class ClearRunDatabasesRequest(BaseModel):
    strategy_ids: list[str]   # list of strategy_ids to delete


@app.delete("/run-databases")
async def clear_run_databases(body: ClearRunDatabasesRequest):
    """Delete the run database files for the given strategy IDs."""
    deleted: list[str] = []
    not_found: list[str] = []
    for sid in body.strategy_ids:
        # Validate ID to prevent path traversal
        if not _ID_RE.match(sid):
            raise HTTPException(status_code=400, detail=f"Invalid strategy_id: {sid!r}")
        db_file = _db_path(sid)
        if db_file.exists():
            db_file.unlink()
            deleted.append(sid)
        else:
            not_found.append(sid)
    return {"deleted": deleted, "not_found": not_found}


@app.post("/analyze-execution")
async def analyze_execution(body: AnalyzeExecutionRequest):
    """
    Answer a question about a strategy execution.
    Builds an orientation summary from SQLite (code, stats, errors), then gives the LLM
    a query_events tool so it can run arbitrary SELECT queries against the raw event log
    rather than being limited to pre-aggregated data.
    """
    model = body.model or DEFAULT_GENERATE_MODEL

    ctx = await _build_run_context(body.strategy_id)
    if ctx is None:
        async def no_data():
            msg = f"No run data found for strategy '{body.strategy_id}'. The strategy may not have been submitted yet."
            yield f"data: {json.dumps({'type': 'text_delta', 'delta': msg})}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
        return StreamingResponse(
            no_data(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    orientation = _format_orientation_summary(ctx)
    system = (
        "You are a trading strategy analyst with direct database access to the strategy's execution log.\n"
        "Use the query_events tool to run SQL SELECT queries against the events table whenever you need "
        "specific data to answer accurately — fills, timestamps, signal values, error details, order sequences, etc.\n"
        "The events table has: id, ts (ms epoch), event_type (TEXT), raw (JSON TEXT).\n"
        "Use json_extract(raw, '$.field') to reach into the JSON payload.\n"
        "Always prefer querying over guessing. Answer concisely and precisely.\n"
        "IMPORTANT: You have a limited number of query rounds. "
        "Before writing your answer, gather ALL data you need in as FEW queries as possible — "
        "combine multiple metrics into a single SQL statement using GROUP BY, COUNT, AVG, MIN, MAX, etc. "
        "Do not query one metric per round. Once you have enough data, write the full answer in one shot.\n\n"
        "## Run Orientation\n" + orientation
    )

    history_text = ""
    for msg in body.history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        history_text += f"\n\n[{role.upper()}]: {content}"

    user = (history_text.strip() + "\n\n" if history_text.strip() else "") + body.question

    async def event_stream():
        try:
            async for delta in _stream_with_query_tool(body.strategy_id, model, system, user):
                yield f"data: {json.dumps({'type': 'text_delta', 'delta': delta})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/ask-run")
async def ask_run(body: AskRunRequest):
    """
    Answer a question about a completed strategy run.
    Streams the answer as SSE text_delta events.
    """
    model = body.model or DEFAULT_GENERATE_MODEL

    system = (
        "You are a trading strategy analyst. "
        "The user ran a live simulation of a trading strategy. "
        "You have access to the full run context below — events, market data observations, "
        "simulation commentary, and the strategy code. "
        "Answer the user's question concisely and specifically based only on this context. "
        "If the information is not available in the context, say so clearly.\n\n"
        "RUN CONTEXT:\n" + body.context
    )

    # Build conversation: history + new question
    history_text = ""
    for msg in body.history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        history_text += f"\n\n[{role.upper()}]: {content}"

    user = (history_text.strip() + "\n\n" if history_text.strip() else "") + body.question

    async def event_stream():
        try:
            async for delta in _stream_text(model, system, user):
                yield f"data: {json.dumps({'type': 'text_delta', 'delta': delta})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8089"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
