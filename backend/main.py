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

  SSE relay (→ SSE Gateway port 9001):
  GET  /api/strategies/{strategy_id}/events   — real-time event stream filtered by strategy_id

Directory layout (under STRATEGIES_DIR):
  {trader_id}/           ← trader's private strategies
  shared/{org_id}/       ← org-shared strategies (read-only via this API)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import uvicorn
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
    _TERMINAL_EVENTS,
    _resolve_termination_type,
    ORCHESTRATOR_URL,
    SUPERVISOR_ID,
    AVAILABLE_MODELS,
    DEFAULT_GENERATE_MODEL,
    DEFAULT_VALIDATE_MODEL,
    DEFAULT_MONITOR_MODEL,
)

load_dotenv()

SSE_GATEWAY_URL = os.getenv("SSE_GATEWAY_URL", "http://localhost:9001")

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

_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,128}$')


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


def _get_trader_id(request: Request) -> str:
    raw = request.headers.get("X-Trader-Id", "default")
    return _check_id(raw, "trader_id")


def _get_org_id(request: Request) -> str | None:
    raw = request.headers.get("X-Org-Id", "").strip()
    if not raw:
        return None
    return _check_id(raw, "org_id")


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


class StopStrategyRequest(BaseModel):
    strategy_id: str


class ParseStrategyRequest(BaseModel):
    code: str


class ResubmitStrategyRequest(BaseModel):
    code: str
    legs: list[dict]
    strategy_params: dict = {}
    monitor_model: str | None = None
    start_time: str | None = None   # ISO 8601 UTC — if omitted, defaults to NYSE open
    end_time: str | None = None     # ISO 8601 UTC — if omitted, defaults to NYSE close


class AskRunRequest(BaseModel):
    question: str
    history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]
    context: str = ""          # serialized run context (events, state, code)
    model: str | None = None


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
    trader_id = _get_trader_id(request)
    org_id = _get_org_id(request)

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
    trader_id = _get_trader_id(request)
    org_id = _get_org_id(request)

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
    trader_id = _get_trader_id(request)

    private_dir = _trader_dir(trader_id)
    private_dir.mkdir(parents=True, exist_ok=True)

    path = private_dir / f"{name}.py"
    path.write_text(body.code)
    return {"name": name, "saved": True, "source": "private"}


@app.post("/stop-strategy")
async def stop_strategy_legacy(request: StopStrategyRequest):
    """Legacy stop endpoint — proxies to Orchestrator port 8000."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{request.strategy_id}/stop"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, timeout=10.0)
        return {"stopped": response.status_code in (200, 204), "strategy_id": request.strategy_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Execution plane proxy (→ Orchestrator port 8000)
# ---------------------------------------------------------------------------

@app.post("/strategies/{strategy_id}/stop")
async def api_stop_strategy(strategy_id: str):
    """Stop a running strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{strategy_id}/stop"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"stopped": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/strategies/{strategy_id}/pause")
async def api_pause_strategy(strategy_id: str):
    """Pause a running strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{strategy_id}/pause"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"paused": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/strategies/{strategy_id}/resume")
async def api_resume_strategy(strategy_id: str):
    """Resume a paused strategy via Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{strategy_id}/resume"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return {"resumed": True, "strategy_id": strategy_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.patch("/strategies/{strategy_id}/params")
async def api_update_params(strategy_id: str, request: Request):
    """Update strategy parameters via Orchestrator."""
    body = await request.json()
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{strategy_id}/params"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(url, json=body, timeout=10.0)
        if response.status_code not in (200, 204):
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return response.json() if response.content else {"updated": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/strategies/{strategy_id}/status")
async def api_get_status(strategy_id: str):
    """Get strategy status from Orchestrator."""
    url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/{strategy_id}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
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

@app.get("/strategies/{strategy_id}/events")
async def api_strategy_events(strategy_id: str):
    """
    Subscribe to real-time events for a specific strategy.
    Connects to the SSE gateway on port 9001, filters by strategy_id,
    enriches terminal events with termination_type, and streams to browser.
    """
    async def event_stream():
        gateway_url = f"{SSE_GATEWAY_URL}/stream"
        try:
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
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Resolve event type — skip heartbeats and empty events
                        event_type = event.get("event_type") or event.get("type", "")
                        if not event_type:
                            continue

                        # Filter by strategy_id
                        data_field = event.get("data")
                        event_sid = event.get("strategy_id") or (data_field.get("strategy_id", "") if isinstance(data_field, dict) else "")
                        print(f"[sse-relay] type={event_type!r} sid={event_sid!r} target={strategy_id!r}", flush=True)
                        if event_sid and event_sid != strategy_id:
                            continue

                        # Enrich terminal events with termination_type
                        if event_type in _TERMINAL_EVENTS:
                            content_str = json.dumps(event)
                            event["termination_type"] = _resolve_termination_type(event_type, content_str)

                        yield f"data: {json.dumps(event)}\n\n"

                        # Stop relaying after terminal event
                        if "termination_type" in event:
                            break

        except Exception as exc:
            yield f"data: {json.dumps({'type': 'relay_error', 'message': str(exc)})}\n\n"
        finally:
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
async def resubmit_strategy(body: ResubmitStrategyRequest):
    """Resubmit an existing validated strategy with updated legs and params (skips generation + validation)."""
    async def event_stream():
        try:
            async for event in run_resubmit_workflow(
                code=body.code,
                legs=body.legs,
                strategy_params=body.strategy_params,
                monitor_model=body.monitor_model,
                start_time=body.start_time,
                end_time=body.end_time,
            ):
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
