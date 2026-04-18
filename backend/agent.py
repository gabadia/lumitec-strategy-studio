"""
Lumitec Strategy Studio — Multi-model agentic workflow.

Phase 1  OpenAI GPT-4o       — strategy generation (text only, no tool calls)
Phase 2  OpenAI GPT-4o-mini  — validation fix loop (stateless per iteration)
Phase 3  Backend direct      — submit_strategy MCP call
Phase 4  OpenAI GPT-4o-mini  — simulation monitor (stateful audit)

Requires OPENAI_API_KEY in .env.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import AsyncIterator, Any

import anthropic
import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client

load_dotenv(override=True)

# ─── API keys ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
MCP_SERVER_URL    = os.getenv("MCP_SERVER_URL", "http://localhost:8002/sse")
ORCHESTRATOR_URL  = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SUPERVISOR_ID     = os.getenv("SUPERVISOR_ID", "NUAM-DEV")

# ─── Prompt paths ────────────────────────────────────────────────────────────
_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
STRATEGY_STRUCTURE_PROMPT_PATH   = os.getenv("STRATEGY_STRUCTURE_PROMPT_PATH",   os.path.join(_PROMPTS_DIR, "strategy_structure.md"))
STRATEGY_GENERATION_PROMPT_PATH  = os.getenv("STRATEGY_GENERATION_PROMPT_PATH",  os.path.join(_PROMPTS_DIR, "strategy_generation.md"))
AGENT_EXECUTION_PROMPT_PATH      = os.getenv("AGENT_EXECUTION_PROMPT_PATH",      os.path.join(_PROMPTS_DIR, "agent_execution.md"))
VALIDATION_LOOP_PROMPT_PATH      = os.getenv("VALIDATION_LOOP_PROMPT_PATH",      os.path.join(_PROMPTS_DIR, "validation_loop.md"))
STRATEGY_TESTING_PROMPT_PATH     = os.getenv("STRATEGY_TESTING_PROMPT_PATH",     os.path.join(_PROMPTS_DIR, "strategy_testing.md"))
STRATEGY_REASONING_PROMPT_PATH   = os.getenv("STRATEGY_REASONING_PROMPT_PATH",   os.path.join(_PROMPTS_DIR, "strategy_reasoning.md"))
SIMULATION_MONITOR_PROMPT_PATH   = os.getenv("SIMULATION_MONITOR_PROMPT_PATH",   os.path.join(_PROMPTS_DIR, "simulation_monitor.md"))

# ─── Models ──────────────────────────────────────────────────────────────────
CLAUDE_MODEL       = "claude-sonnet-4-6"
OPENAI_FAST_MODEL  = "gpt-4o-mini"
OPENAI_SMART_MODEL = "gpt-4o"

DEFAULT_GENERATE_MODEL = "claude-sonnet-4-6"
DEFAULT_VALIDATE_MODEL = "gpt-4o-mini"
DEFAULT_MONITOR_MODEL  = "gpt-4o-mini"

AVAILABLE_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "gpt-4o",
    "gpt-4o-mini",
]


def _provider(model: str) -> str:
    """Return 'anthropic' or 'openai' based on model name prefix."""
    return "anthropic" if model.startswith("claude") else "openai"

# ─── Limits ──────────────────────────────────────────────────────────────────
MAX_TOKENS              = 8096
MAX_RETRIES             = 5
RETRY_BASE_DELAY        = 60
MAX_VALIDATION_ATTEMPTS = 3
MAX_SIMULATION_POLLS    = 60  # kept for reference, no longer used in agent

# ─── Clients ─────────────────────────────────────────────────────────────────
_anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_openai = None
if OPENAI_API_KEY:
    try:
        import openai as _openai_module
        _openai = _openai_module.AsyncOpenAI(api_key=OPENAI_API_KEY)
    except ImportError:
        pass


# ─── Prompt loading ───────────────────────────────────────────────────────────

def _read_file(path: str, fallback: str = "") -> str:
    if os.path.exists(path):
        return open(path).read()
    return fallback


def _load_generation_system() -> str:
    """Phase 1 — Generation: structural rules + generation instructions."""
    parts = []
    structure = _read_file(STRATEGY_STRUCTURE_PROMPT_PATH)
    parts.append(structure if structure else "You are a Lumitec strategy developer.")
    gen_prompt = _read_file(STRATEGY_GENERATION_PROMPT_PATH)
    if gen_prompt:
        parts.append(gen_prompt)
    return "\n\n---\n\n".join(parts)


def _load_execution_system() -> str:
    """Phase 3 — Execution: agent lifecycle and execution rules."""
    return _read_file(AGENT_EXECUTION_PROMPT_PATH, "")


def _load_fixing_system() -> str:
    """Phase 2 — Fixing: validation loop instructions only."""
    return _read_file(
        VALIDATION_LOOP_PROMPT_PATH,
        "Fix the validation error in the strategy code. Return corrected code only.",
    )


def _load_testing_system() -> str:
    """Phase 2b — Test scenario generation."""
    return _read_file(STRATEGY_TESTING_PROMPT_PATH, "Generate test scenarios for this strategy as JSON.")


def _load_reasoning_system() -> str:
    """Phase 4 — Evaluate behavior."""
    return _read_file(STRATEGY_REASONING_PROMPT_PATH, "Reason through the scenario and return pass/fail with issues.")


def _load_monitor_system() -> str:
    """Phase 4 — Monitoring: simulation monitor instructions."""
    return _read_file(
        SIMULATION_MONITOR_PROMPT_PATH,
        "Monitor the trading strategy simulation and report status as JSON.",
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _content_to_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif isinstance(item, dict):
                parts.append(item.get("text", json.dumps(item)))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if hasattr(content, "text"):
        return content.text
    return json.dumps(content)


def _extract_code_from_text(text: str) -> str | None:
    """Return the last ```python...``` block found in text."""
    matches = re.findall(r'```python\s*\n(.*?)```', text, re.DOTALL)
    if matches:
        return matches[-1].strip()
    # Fallback: bare ``` block that looks like Python
    matches = re.findall(r'```\s*\n(.*?)```', text, re.DOTALL)
    for m in reversed(matches):
        if any(kw in m for kw in ('class ', 'def ', 'import ')):
            return m.strip()
    return None


def _extract_metadata(code: str, intent: str = "") -> dict:
    """Extract class name, objective, symbol, legs, and safety constraints."""
    # Class name
    m = re.search(r'class\s+(\w+)\s*\(LumitecBaseStrategy\)', code)
    class_name = m.group(1) if m else "UnknownStrategy"

    # Objective
    m = re.search(r'objective\s*=\s*StrategyObjective\.(\w+)', code)
    objective = m.group(1) if m else "SIGNAL_DRIVEN"

    # Constraints from ConfigParams defaults
    constraints: dict[str, float] = {}
    for fname in ('max_position', 'max_loss', 'max_active_orders_per_side', 'max_order_rate_per_second'):
        m = re.search(rf'{fname}\s*[=:]\s*([\d.]+)', code)
        if m:
            constraints[fname] = float(m.group(1))

    # Symbol — code first, then intent
    _noise = {'BUY', 'SELL', 'DAY', 'GTC', 'MID', 'BID', 'ASK', 'A', 'I'}
    symbol = None
    for pat in (
        r'["\']([A-Z]{2,5})["\']',      # quoted ticker in code
        r'\b([A-Z]{2,5})\b',             # bare uppercase word in intent
    ):
        src = code if symbol is None else intent
        m = re.search(pat, src)
        if m and m.group(1) not in _noise:
            symbol = m.group(1)
            break
    if not symbol:
        symbol = "AAPL"

    # Legs from leg_schema if present
    # Respect fixed_side — if fixed_side is False or side is None, leg is user-selectable (submit as "BUY" default)
    # but validate_legs will accept any side so we must not specify a fixed side that contradicts it
    legs: list[dict] = []
    lm = re.search(r'leg_schema\s*=\s*(\[.*?\])', code, re.DOTALL)
    if lm:
        try:
            entries = re.findall(r'\{[^}]+\}', lm.group(1))
            for i, entry in enumerate(entries):
                side_match = re.search(r'"side"\s*:\s*(?:"(BUY|SELL)"|None|null)', entry, re.IGNORECASE)
                fixed_match = re.search(r'"fixed_side"\s*:\s*(True|False|true|false)', entry, re.IGNORECASE)
                fixed_side = fixed_match and fixed_match.group(1).lower() == 'true' if fixed_match else False
                if side_match and side_match.group(1) and fixed_side:
                    side = side_match.group(1).upper()
                else:
                    side = "BUY"  # user-selectable — default to BUY for submission
                leg_id = chr(ord('A') + i)
                legs.append({"leg_id": leg_id, "symbol": symbol, "quantity": 100, "side": side, "tif": "DAY"})
        except Exception:
            pass
    if not legs:
        legs = [{"leg_id": "A", "symbol": symbol, "quantity": 100, "side": "BUY", "tif": "DAY"}]

    return {
        "class_name": class_name,
        "objective": objective,
        "symbol": symbol,
        "legs": legs,
        "constraints": constraints,
    }


def _parse_submission(code: str) -> dict:
    """
    Parse the submission schema from strategy code:
    - legs: from leg_schema (with symbol placeholder per leg)
    - params: ConfigParams fields with name, type, and current default value
    """
    metadata = _extract_metadata(code)

    # Parse ConfigParams fields
    params: list[dict] = []
    m = re.search(r'class ConfigParams[^:]*:(.*?)(?=\n[ \t]*(?:class |def |@|\Z))', code, re.DOTALL)
    if m:
        body = m.group(1)
        for line in body.splitlines():
            fm = re.match(r'[ \t]+(\w+)\s*:\s*([\w\[\], ]+?)\s*=\s*(.+)', line.rstrip())
            if not fm:
                continue
            name, type_str = fm.group(1).strip(), fm.group(2).strip()
            # Strip inline comment before parsing the default value
            default_raw = re.sub(r'\s*#.*$', '', fm.group(3)).strip()
            if name.startswith('_'):
                continue
            base_type = type_str.split('[')[0].strip().lower()
            try:
                if base_type == 'int':
                    value: object = int(default_raw)
                    field_type = 'int'
                elif base_type == 'float':
                    value = float(default_raw)
                    field_type = 'float'
                elif base_type == 'bool':
                    value = default_raw.strip().lower() == 'true'
                    field_type = 'bool'
                else:
                    value = default_raw.strip('"\'')
                    field_type = 'str'
            except (ValueError, AttributeError):
                value = default_raw.strip('"\'')
                field_type = 'str'
            params.append({"name": name, "type": field_type, "value": value})

    return {
        "class_name": metadata["class_name"],
        "legs": metadata["legs"],
        "params": params,
    }


def _clean_openai_code(text: str) -> str:
    text = text.strip()
    if text.startswith("```python"):
        text = text[9:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ─── Unified LLM helpers ─────────────────────────────────────────────────────

async def _stream_text(model: str, system: str, user: str, max_tokens: int = 16000) -> AsyncIterator[str]:
    """Yield text deltas from either Anthropic or OpenAI."""
    if _provider(model) == "anthropic":
        async with _anthropic.messages.stream(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        ) as s:
            async for delta in s.text_stream:
                yield delta
    else:
        if not _openai:
            raise RuntimeError("No OpenAI API key configured")
        stream = await _openai.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream=True, temperature=0.1,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta


async def _complete(model: str, system: str, user: str, json_mode: bool = False) -> str:
    """Single non-streaming completion from either provider. Returns full text."""
    if _provider(model) == "anthropic":
        sys_prompt = system
        if json_mode:
            sys_prompt = system + "\n\nRespond with valid JSON only. No markdown fences, no explanation."
        resp = await _anthropic.messages.create(
            model=model, max_tokens=8096, system=sys_prompt,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text
        if json_mode:
            # Strip accidental markdown fences
            text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
            text = re.sub(r'\n?```\s*$', '', text)
        return text
    else:
        if not _openai:
            raise RuntimeError("No OpenAI API key configured")
        kwargs: dict = dict(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await _openai.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


def _iso(dt: datetime) -> str:
    """Convert a timezone-aware datetime to a UTC ISO 8601 string (Z suffix)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Simulation state ─────────────────────────────────────────────────────────

@dataclass
class SimulationState:
    position: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fills: int = 0
    total_orders_submitted: int = 0
    total_orders_cancelled: int = 0
    total_orders_rejected: int = 0
    open_orders: dict = field(default_factory=dict)
    fills: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    terminated: bool = False
    termination_reason: str | None = None
    termination_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Phase 1: Generation ─────────────────────────────────────────────────────

async def _phase_claude_generate(
    intent: str,
    existing_code: str | None,
    strategy_name: str | None,
    system_prompt: str,
    generate_model: str,
) -> AsyncIterator[dict]:
    """
    Stream any configured model to generate strategy code.
    Yields SSE events.
    Yields {"type": "_code_ready", "code": str, "intent_text": str} at the end.
    """
    if existing_code:
        label = strategy_name or "this strategy"
        user_content = (
            f"Here is an existing strategy named **{label}**.\n\n"
            + (f"Trader note: {intent}\n\n" if intent else "")
            + f"```python\n{existing_code}\n```\n\n"
            "Review this strategy following the 5-step generation process in your system prompt. "
            "Output the final (possibly improved) code in a ```python block."
        )
    else:
        user_content = (
            f"{intent}\n\n"
            "Follow the 5-step strategy generation process defined in your system prompt. "
            "When you have the final implementation (Step 4), output it in a ```python code block."
        )

    full_text = ""
    turn = 1

    yield {"type": "thinking", "turn": turn, "model": generate_model}

    try:
        async for delta in _stream_text(generate_model, system_prompt, user_content):
            full_text += delta
            yield {"type": "text_delta", "delta": delta, "turn": turn, "model": generate_model}
    except Exception as exc:
        yield {"type": "error", "message": f"Generation failed ({generate_model}): {exc}"}
        return

    yield {"type": "turn_complete", "turn": turn, "stop_reason": "end_turn"}

    code = _extract_code_from_text(full_text) or existing_code or ""
    yield {"type": "_code_ready", "code": code, "intent_text": full_text}


# ─── Phase 2: OpenAI validation loop ─────────────────────────────────────────

async def _phase_openai_validate(
    session: ClientSession,
    code: str,
    validation_system: str,
    turn_offset: int,
    fix_model: str,
) -> AsyncIterator[dict]:
    """
    Stateless validation loop.
    Backend calls validate_strategy directly; OpenAI fixes failures.
    Yields SSE events.
    Yields {"type": "_validation_done", "passed": bool, "code": str} at the end.
    """
    last_error = ""
    for attempt in range(MAX_VALIDATION_ATTEMPTS):
        turn = turn_offset + attempt + 1

        yield {"type": "tool_executing", "name": "validate_strategy", "turn": turn}
        t0 = time.monotonic()
        try:
            result = await session.call_tool("validate_strategy", {"code": code})
            duration_ms = int((time.monotonic() - t0) * 1000)
            content = _content_to_str(result.content)
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            yield {"type": "tool_error", "id": f"val_{attempt}", "name": "validate_strategy",
                   "error": str(exc), "duration_ms": duration_ms, "turn": turn}
            yield {"type": "_validation_done", "passed": False, "code": code}
            return

        failed = result.isError or '"all_passed": false' in content or ('"all_passed"' not in content and '"detail"' in content and 'true' not in content)

        yield {
            "type": "tool_result",
            "id": f"val_{attempt}",
            "name": "validate_strategy",
            "content": content[:2000],
            "duration_ms": duration_ms,
            "turn": turn,
            "failed": failed,
        }

        if not failed:
            # Additional structural checks the MCP validator doesn't cover
            structural_errors = []
            if 'class Config(LumitecStrategyConfig)' not in code:
                structural_errors.append("Missing `class Config(LumitecStrategyConfig)` — supervisor cannot load strategy without it")
            if 'from_config' not in code:
                structural_errors.append("Missing `from_config` classmethod in ConfigParams")
            if structural_errors:
                content = "Structural errors:\n" + "\n".join(f"- {e}" for e in structural_errors)
                yield {
                    "type": "tool_result",
                    "id": f"val_{attempt}_struct",
                    "name": "validate_strategy",
                    "content": content,
                    "duration_ms": 0,
                    "turn": turn,
                    "failed": True,
                }
                yield {"type": "text_delta", "delta": f"\n**Structural check failed:**\n```\n{content}\n```\n", "turn": turn}
                last_error = content
            else:
                yield {"type": "_validation_done", "passed": True, "code": code}
                return

        last_error = content

        # Validation failed — emit errors as visible text
        yield {
            "type": "text_delta",
            "delta": f"\n**Validation attempt {attempt + 1} failed:**\n```\n{content[:1000]}\n```\n",
            "turn": turn,
        }

        yield {"type": "thinking", "turn": turn, "model": fix_model}

        try:
            fixed_text = await _complete(
                fix_model, validation_system,
                f"CURRENT CODE:\n```python\n{code}\n```\n\n"
                f"VALIDATION ERROR:\n{content}\n\n"
                "Return the corrected Python code only.",
            )
            fixed = _clean_openai_code(fixed_text)
            if fixed:
                code = fixed
                # Emit tool_call so App.tsx updates the code panel
                yield {
                    "type": "tool_call",
                    "id": f"fix_{attempt}",
                    "name": "validate_strategy",
                    "input": {"code": code},
                    "turn": turn,
                }
        except Exception as exc:
            yield {"type": "tool_error", "id": f"fix_{attempt}", "name": "openai_fix",
                   "error": str(exc), "duration_ms": 0, "turn": turn}
            yield {"type": "_validation_done", "passed": False, "code": code}
            return

    yield {"type": "_validation_done", "passed": False, "code": code, "last_error": last_error}


# ─── Phase 2b: Test scenario generation ──────────────────────────────────────

async def _phase_generate_test_scenarios(
    code: str,
    intent: str,
    testing_system: str,
    model: str,
    turn: int,
) -> AsyncIterator[dict]:
    """
    Use the testing model to generate structured test scenarios from the strategy code.
    Yields SSE events.
    Yields {"type": "_scenarios_ready", "scenarios": list} at the end.
    """
    yield {"type": "thinking", "turn": turn, "model": model}
    user = (
        f"Strategy intent: {intent[:500]}\n\n"
        f"Strategy code:\n```python\n{code}\n```\n\n"
        "Generate test scenarios as JSON."
    )
    try:
        raw = await _complete(model, testing_system, user, json_mode=True)
        data = json.loads(raw)
        scenarios = data.get("scenarios", [])
    except Exception as exc:
        yield {"type": "text_delta", "delta": f"\n**Test scenario generation failed:** {exc}\n", "turn": turn}
        yield {"type": "_scenarios_ready", "scenarios": []}
        return

    yield {
        "type": "text_delta",
        "delta": f"\n**Generated {len(scenarios)} test scenario(s):** " +
                 ", ".join(s.get("name", f"scenario_{i}") for i, s in enumerate(scenarios)) + "\n",
        "turn": turn,
    }
    yield {"type": "_scenarios_ready", "scenarios": scenarios}


# ─── Phase 2c: Scenario reasoning test ───────────────────────────────────────

async def _phase_reason_test_scenarios(
    code: str,
    scenarios: list,
    reasoning_system: str,
    model: str,
    turn: int,
) -> AsyncIterator[dict]:
    """
    For each scenario, ask the model to reason through the strategy's behaviour and return pass/fail.
    Yields SSE events.
    Yields {"type": "_reasoning_done", "passed": bool, "failures": list[str]} at the end.
    """
    failures: list[str] = []

    for i, scenario in enumerate(scenarios):
        scenario_turn = turn + i
        name = scenario.get("name", f"scenario_{i}")
        yield {"type": "thinking", "turn": scenario_turn, "model": model}

        # Fill the [code] and [scenario] placeholders from the prompt template
        user = (
            reasoning_system
            .replace("[code]", f"```python\n{code}\n```")
            .replace("[scenario]", json.dumps(scenario, indent=2))
        )
        # If placeholders weren't present (bare prompt), fall back to inline format
        if "[code]" not in reasoning_system and "[scenario]" not in reasoning_system:
            user = (
                f"Strategy code:\n```python\n{code}\n```\n\n"
                f"Scenario:\n{json.dumps(scenario, indent=2)}\n\n"
                + reasoning_system
            )

        try:
            result = await _complete(model, "", user)
        except Exception as exc:
            yield {"type": "text_delta", "delta": f"\n**Reasoning failed for {name}:** {exc}\n", "turn": scenario_turn}
            continue

        passed = "pass" in result.lower() and "fail" not in result.lower()
        status = "PASS" if passed else "FAIL"
        yield {
            "type": "text_delta",
            "delta": f"\n**[{status}] {name}**\n{result}\n",
            "turn": scenario_turn,
        }
        if not passed:
            failures.append(f"{name}: {result[:300]}")

    all_passed = len(failures) == 0
    yield {"type": "_reasoning_done", "passed": all_passed, "failures": failures}


# ─── Phase 3: Submit ──────────────────────────────────────────────────────────

def _unnest_config_classes(code: str) -> str:
    """
    If GPT nested Config or ConfigParams inside the strategy class, extract them
    to top-level so the supervisor can find them.
    """
    lines = code.splitlines()

    # Find the strategy class line (class Foo(LumitecBaseStrategy):)
    strategy_start = None
    for i, line in enumerate(lines):
        if re.match(r'^class \w+\(LumitecBaseStrategy\)', line):
            strategy_start = i
            break

    if strategy_start is None:
        return code  # nothing to fix

    # Find nested Config and ConfigParams blocks inside the strategy class
    # They appear as "    class Config..." or "    @dataclass...\n    class ConfigParams..."
    extracted: list[str] = []
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect nested class definitions inside strategy (indented with 4 spaces)
        if i > strategy_start and re.match(r'^    class (Config\(LumitecStrategyConfig\)|ConfigParams)', line):
            # Collect the full indented block
            block = []
            # Check if there's a decorator line just before (e.g. @dataclass)
            if new_lines and new_lines[-1].strip().startswith('@'):
                decorator = new_lines.pop()
                block.append(decorator.strip())
            block.append(line.strip())
            i += 1
            while i < len(lines) and (lines[i].startswith('        ') or lines[i].strip() == ''):
                block.append(lines[i][4:] if lines[i].startswith('    ') else lines[i])
                i += 1
            extracted.append('\n'.join(block))
            continue
        new_lines.append(line)
        i += 1

    if not extracted:
        return code  # no nesting found

    # Insert extracted classes before the strategy class
    result: list[str] = []
    for j, line in enumerate(new_lines):
        if j == strategy_start:
            for block in extracted:
                result.append(block)
                result.append('')
        result.append(line)

    print(f"[unnest] extracted {len(extracted)} nested class(es) to top-level", flush=True)
    return '\n'.join(result)


async def _phase_submit(
    code: str,
    metadata: dict,
    start_iso: str,
    end_iso: str,
    uid8: str,
    turn: int,
    strategy_params: dict | None = None,
) -> AsyncIterator[dict]:
    """
    Constructs the submit payload from extracted metadata and calls submit_strategy directly.
    Yields SSE events.
    Yields {"type": "_submit_done", "success": bool, "strategy_id": str|None} at the end.
    """
    # Auto-fix: extract nested Config/ConfigParams to top-level if GPT nested them
    code = _unnest_config_classes(code)

    # Hard gate — catch structural issues before they reach the supervisor
    pre_errors = []
    if 'class Config(LumitecStrategyConfig)' not in code:
        pre_errors.append("Missing `class Config(LumitecStrategyConfig)` — supervisor cannot load strategy without it")
    if 'from_config' not in code:
        pre_errors.append("Missing `from_config` classmethod in ConfigParams")
    if pre_errors:
        msg = "Cannot submit — structural errors in generated code:\n" + "\n".join(f"- {e}" for e in pre_errors)
        yield {"type": "text_delta", "delta": msg, "turn": turn}
        yield {"type": "_submit_done", "success": False, "strategy_id": None}
        return

    class_name = metadata["class_name"]
    strategy_id = f"{class_name}-{uid8}-ECX_001"

    config = {
        "strategy_name": class_name,
        "strategy_class": class_name,
        "strategy_id": strategy_id,
        "submission_method": "inline_code",
        "account_id": "ECX_001",
        "trader_id": "MEMO-DESK",
        "supervisor_id": "NUAM-DEV",
        "objective": metadata.get("objective", "SIGNAL_DRIVEN"),
        "code": code,
        "duration_minutes": 10,
        "order_mode": "single",
        "log_trades": False,
        "log_quotes": True,
        "strategy_params": strategy_params or {},
        "legs": metadata.get("legs", [
            {"leg_id": "A", "symbol": metadata.get("symbol", "AAPL"),
             "quantity": 100, "side": "BUY", "tif": "DAY"}
        ]),
        "start_time": start_iso,
        "end_time": end_iso,
    }

    # Emit tool_call so App.tsx updates step indicator and code panel
    yield {
        "type": "tool_call",
        "id": "submit",
        "name": "submit_strategy",
        "input": config,
        "turn": turn,
    }

    yield {"type": "tool_executing", "name": "submit_strategy", "turn": turn}
    t0 = time.monotonic()
    try:
        has_config = 'class Config(LumitecStrategyConfig)' in code
        config_pos = code.find('class Config(LumitecStrategyConfig)')
        strategy_pos = code.find('class ' + class_name)
        print(f"[submit] strategy_id={strategy_id} has_Config={has_config} config_pos={config_pos} strategy_pos={strategy_pos}", flush=True)
        print(f"[submit] CODE FIRST 3000 CHARS:\n{code[:3000]}", flush=True)
        if config_pos > strategy_pos:
            print(f"[submit] WARNING: Config class is defined AFTER strategy class — reordering", flush=True)
            # Extract and move Config before the strategy class
            config_match = re.search(r'(class Config\(LumitecStrategyConfig\).*?)(?=\n\n|\nclass |\Z)', code, re.DOTALL)
            if config_match:
                config_block = config_match.group(1)
                code = code[:config_match.start()] + code[config_match.end():]
                strategy_match = re.search(r'\nclass ' + re.escape(class_name), code)
                if strategy_match:
                    code = code[:strategy_match.start()] + '\n\n' + config_block + code[strategy_match.start():]
        submit_url = f"{ORCHESTRATOR_URL}/v1/supervisors/{SUPERVISOR_ID}/strategies/submit"
        print(f"[submit] POST {submit_url}", flush=True)
        async with httpx.AsyncClient() as client:
            response = await client.post(submit_url, json=config, timeout=30.0)
        duration_ms = int((time.monotonic() - t0) * 1000)
        content = response.text
        print(f"[submit] HTTP {response.status_code} content={content[:300]}", flush=True)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        print(f"[submit] EXCEPTION: {exc}", flush=True)
        yield {"type": "tool_error", "id": "submit", "name": "submit_strategy",
               "error": str(exc), "duration_ms": duration_ms, "turn": turn}
        yield {"type": "_submit_done", "success": False, "strategy_id": None}
        return

    # Parse response to check success — avoid string matching on JSON formatting
    try:
        resp_json = response.json()
        submit_ok = response.status_code in (200, 201) and resp_json.get("status") == "success"
    except Exception:
        submit_ok = False

    yield {
        "type": "tool_result",
        "id": "submit",
        "name": "submit_strategy",
        "content": content[:2000],
        "duration_ms": duration_ms,
        "turn": turn,
        "failed": not submit_ok,
    }

    if submit_ok:
        yield {"type": "strategy_submitted", "strategy_id": strategy_id}
        yield {"type": "_submit_done", "success": True, "strategy_id": strategy_id}
    else:
        yield {
            "type": "text_delta",
            "delta": f"\n**Submit failed:**\n```\n{content[:1500]}\n```\n",
            "turn": turn,
            "model": "GPT",
        }
        yield {"type": "_submit_done", "success": False, "strategy_id": None}


# ─── Phase 4: OpenAI simulation monitor ──────────────────────────────────────

_TERMINAL_EVENTS = {
    "strategy.stopped":   None,       # termination_type resolved via stop_reason
    "strategy.failed":    "FAILED",
    "strategy.completed": "COMPLETED",
}

# Maps stop_reason field (from FORCED_STOP event) to enriched termination_type.
# MANUAL is the retired legacy value — treat same as COMPLETED.
_STOP_REASON_TO_TYPE = {
    "COMPLETED": "COMPLETED",
    "USER":      "STOPPED_BY_USER",
    "RISK":      "STOPPED_RISK",
    "SYSTEM":    "STOPPED_SYSTEM",
    "TIME":      "STOPPED_SESSION_END",
    "ERROR":     "FAILED",
    "MANUAL":    "COMPLETED",   # retired — legacy events only
}


def _extract_stop_reason(content: str) -> str | None:
    """Extract stop_reason value from a raw stream_events string."""
    m = re.search(r'"stop_reason"\s*:\s*"([^"]+)"', content)
    if not m:
        m = re.search(r"stop_reason[=:\s]+([A-Z]+)", content)
    return m.group(1).upper() if m else None


def _resolve_termination_type(terminal_str: str, content: str) -> str:
    """Return enriched termination_type for a detected terminal event."""
    if terminal_str == "strategy.stopped":
        stop_reason = _extract_stop_reason(content)
        return _STOP_REASON_TO_TYPE.get(stop_reason or "", "FAILED")
    return _TERMINAL_EVENTS[terminal_str] or "FAILED"


# ─── Orchestrator ─────────────────────────────────────────────────────────────

async def run_strategy_workflow(
    intent: str,
    strategy_name: str | None = None,
    existing_code: str | None = None,
    workflow_mode: str = "fast",
    generate_model: str | None = None,
    validate_model: str | None = None,
    monitor_model: str | None = None,
) -> AsyncIterator[dict]:
    """
    Main entry point. Yields SSE-ready dicts.

    workflow_mode:
      "fast" → Phase 1 → 2 → 5  (generate, validate, submit)
      "full" → Phase 1 → 2 → 3 → 4 → 5  (+ test scenarios + evaluate behavior)
    Monitoring is handled by the SSE relay in main.py after strategy_submitted.
    """
    gen_model = generate_model or DEFAULT_GENERATE_MODEL
    val_model = validate_model or DEFAULT_VALIDATE_MODEL

    claude_system     = _load_generation_system()
    validation_system = _load_fixing_system()
    testing_system    = _load_testing_system()
    reasoning_system  = _load_reasoning_system()

    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                BLOCKED = {"publish_strategy", "stream_events", "submit_strategy"}
                tools_response = await session.list_tools()
                tool_names = [t.name for t in tools_response.tools if t.name not in BLOCKED]
                yield {"type": "tools_ready", "tools": tool_names, "count": len(tool_names)}

                # Time window — default to NYSE session (stored as UTC)
                ET = ZoneInfo("America/New_York")
                today_et = datetime.now(ET).date()
                start_dt = datetime(today_et.year, today_et.month, today_et.day, 9, 30, 0, tzinfo=ET)
                end_dt   = datetime(today_et.year, today_et.month, today_et.day, 16, 0, 0, tzinfo=ET)
                start_iso, end_iso = _iso(start_dt), _iso(end_dt)
                uid8 = uuid.uuid4().hex[:8]

                # ── Phase 1: Generate ──────────────────────────────────────
                code = existing_code or ""
                intent_text = intent
                claude_turn = 0

                if not existing_code:
                    async for event in _phase_claude_generate(
                        intent, existing_code, strategy_name, claude_system, gen_model
                    ):
                        if event["type"] == "_code_ready":
                            code        = event["code"]
                            intent_text = event["intent_text"]
                            claude_turn = 1
                        else:
                            yield event

                if not code:
                    yield {"type": "error", "message": "Failed to extract strategy code from Claude output"}
                    return

                metadata = _extract_metadata(code, intent)

                # ── Phase 2: Validate + fix ────────────────────────────────
                validated = False
                last_error = ""
                async for event in _phase_openai_validate(
                    session, code, validation_system, turn_offset=claude_turn, fix_model=val_model
                ):
                    if event["type"] == "_validation_done":
                        validated  = event["passed"]
                        code       = event["code"]
                        last_error = event.get("last_error", "")
                    else:
                        yield event

                summary_turn = claude_turn + MAX_VALIDATION_ATTEMPTS + 1

                if not validated:
                    msg = "Validation failed after maximum attempts. Cannot submit."
                    if last_error:
                        msg += f"\n\nLast error:\n```\n{last_error[:1500]}\n```"
                    yield {"type": "text_delta", "delta": msg, "turn": summary_turn}
                    yield {"type": "turn_complete", "turn": summary_turn, "stop_reason": "end_turn"}
                    yield {"type": "done"}
                    return

                # ── Phase 3: Test scenarios (full only) ────────────────────
                scenarios: list = []
                test_turn = summary_turn + 1
                print(f"[workflow] mode={workflow_mode} validated={validated} proceeding to phase 3", flush=True)
                if workflow_mode == "full":
                    async for event in _phase_generate_test_scenarios(
                        code, intent_text, testing_system, val_model, test_turn
                    ):
                        if event["type"] == "_scenarios_ready":
                            scenarios = event["scenarios"]
                        else:
                            yield event

                # ── Phase 4: Evaluate behavior (full only) ─────────────────
                reasoning_turn = test_turn + 1
                print(f"[workflow] phase 4: scenarios={len(scenarios)}", flush=True)
                if workflow_mode == "full" and scenarios:
                    async for event in _phase_reason_test_scenarios(
                        code, scenarios, reasoning_system, val_model, reasoning_turn
                    ):
                        if event["type"] != "_reasoning_done":
                            yield event

                # ── Phase 5: Emit params_ready — frontend handles explicit submission ─
                # strategy_params must always be explicitly provided by the caller;
                # we never silently infer them. Parse the code here and hand the
                # defaults to the frontend so the user can review/edit before submit.
                params_turn = (reasoning_turn + 1) if workflow_mode == "full" else (summary_turn + 1)
                parsed = _parse_submission(code)
                yield {
                    "type": "params_ready",
                    "code": code,
                    "legs": parsed["legs"],
                    "params": parsed["params"],
                    "turn": params_turn,
                }
                yield {"type": "turn_complete", "turn": params_turn, "stop_reason": "end_turn"}
                yield {"type": "done"}

    except BaseException as exc:
        import traceback
        traceback.print_exc()
        root = exc
        while isinstance(root, BaseExceptionGroup):
            root = root.exceptions[0]
        yield {"type": "error", "message": f"{type(root).__name__}: {root}"}


# ─── Resubmit workflow (skip generation + validation) ────────────────────────

async def run_resubmit_workflow(
    code: str,
    legs: list[dict],
    strategy_params: dict,
    monitor_model: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> AsyncIterator[dict]:
    """
    Skip Phase 1 (generation) and Phase 2 (validation).
    Use the existing validated code with user-supplied legs and params.
    Goes straight to submit via Orchestrator REST API.
    """

    ET = ZoneInfo("America/New_York")
    today_et = datetime.now(ET).date()
    if start_time:
        start_iso = start_time
    else:
        start_iso = _iso(datetime(today_et.year, today_et.month, today_et.day, 9, 30, 0, tzinfo=ET))
    if end_time:
        end_iso = end_time
    else:
        end_iso = _iso(datetime(today_et.year, today_et.month, today_et.day, 16, 0, 0, tzinfo=ET))
    uid8 = uuid.uuid4().hex[:8]

    try:
        metadata = _extract_metadata(code)
        metadata["legs"] = legs  # user-supplied legs override extracted ones

        yield {"type": "text_delta", "delta": "**Resubmitting with updated legs and parameters…**\n", "turn": 1}

        # Submit via Orchestrator REST API
        strategy_id = None
        async for event in _phase_submit(
            code, metadata, start_iso, end_iso, uid8, turn=1,
            strategy_params=strategy_params,
        ):
            if event["type"] == "_submit_done":
                strategy_id = event["strategy_id"] if event["success"] else None
            else:
                yield event

        if not strategy_id:
            yield {"type": "text_delta", "delta": "Resubmission failed.", "turn": 1}
            yield {"type": "turn_complete", "turn": 1, "stop_reason": "end_turn"}
            yield {"type": "done"}
            return

        # Workflow ends here — monitoring is handled via SSE relay in main.py
        yield {"type": "turn_complete", "turn": 1, "stop_reason": "end_turn"}
        yield {"type": "done"}

    except BaseException as exc:
        import traceback
        traceback.print_exc()
        root = exc
        while isinstance(root, BaseExceptionGroup):
            root = root.exceptions[0]
        yield {"type": "error", "message": f"{type(root).__name__}: {root}"}


def _default_summary(state: dict) -> str:
    return (
        f"Simulation complete. "
        f"Final position: {state.get('position', 0)}. "
        f"Realized P&L: ${state.get('realized_pnl', 0.0):.2f}. "
        f"Total fills: {state.get('total_fills', 0)}. "
        f"Termination: {state.get('termination_type', 'unknown')}."
    )


# Termination types that represent a clean stop — any open_orders remaining in
# the final state are stale (the supervisor cancels them as part of the stop
# sequence, but those events arrive after strategy.stopped and are never polled).
_CLEAN_TERMINATION_TYPES = {"COMPLETED", "STOPPED_BY_USER", "STOPPED_SESSION_END", "STOPPED_SYSTEM"}


def _prepare_final_state(state: dict) -> dict:
    """
    Return a copy of final monitor state ready for the summary LLM.
    For clean terminations, clear open_orders — they are stale because the
    supervisor's order cancellations arrive after strategy.stopped and the
    polling loop has already exited.
    """
    s = dict(state)
    if s.get("termination_type") in _CLEAN_TERMINATION_TYPES:
        s["open_orders"] = {}
    return s


_SUMMARY_SYSTEM_PROMPT = """Write a concise 3-5 sentence summary of a live trading strategy simulation.

Rules:
- Be specific: name the strategy, quote fill counts, prices, and P&L figures exactly as given.
- Do NOT mention open_orders — by the time the strategy stops, all orders have been cancelled or expired by the supervisor.
- Interpret termination_type correctly:
    COMPLETED          → strategy finished its own objective cleanly — positive outcome
    STOPPED_BY_USER    → operator stopped the strategy externally — neutral, not an error
    STOPPED_SESSION_END → session window expired, supervisor hard-stopped the strategy — expected
    STOPPED_SYSTEM     → supervisor stopped the strategy as part of a controlled sequence — not an error
    STOPPED_RISK       → a risk limit fired and stopped the strategy — describe which limit from the reason field
    FAILED             → something went wrong — describe the error
- Realized P&L of $0.00 on an execution strategy (TWAP, peg, etc.) is normal — these strategies are not P&L-seeking.
- Do not speculate about future activity. The strategy has stopped."""
