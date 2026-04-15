"""
Lumitec Strategy Studio — FastAPI backend.

Endpoints:
  POST /run-strategy          — streams SSE events as Claude executes the workflow
  GET  /strategies            — list .py files for the trader (private + org-shared)
  GET  /strategies/{name}     — return source code (private takes priority over shared)
  PUT  /strategies/{name}     — save to trader's private directory
  POST /stop-strategy         — call stop_strategy on the MCP server
  GET  /health                — liveness check

Directory layout (under STRATEGIES_DIR):
  {trader_id}/           ← trader's private strategies
  shared/{org_id}/       ← org-shared strategies (read-only via this API)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mcp import ClientSession
from mcp.client.sse import sse_client

from agent import (
    run_strategy_workflow,
    run_resubmit_workflow,
    _parse_submission,
    _stream_text,
    AVAILABLE_MODELS,
    DEFAULT_GENERATE_MODEL,
    DEFAULT_VALIDATE_MODEL,
    DEFAULT_MONITOR_MODEL,
)

load_dotenv()

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
async def stop_strategy(request: StopStrategyRequest):
    """Call stop_strategy on the MCP server for the given strategy_id."""
    mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8002/sse")
    try:
        async with sse_client(mcp_url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "stop_strategy",
                    arguments={"strategy_id": request.strategy_id},
                )
                return {"stopped": True, "strategy_id": request.strategy_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
