"""Regression tests for the dev validation profile being dropped on resubmit.

Bug: the frontend sends ``validation_profile`` to ``POST /resubmit-strategy`` but
``ResubmitStrategyRequest`` did not declare the field, so Pydantic silently
dropped it and ``run_resubmit_workflow`` always ran with the ``prod`` default —
validating dev-mode code as production and rejecting e.g. ``open()``.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ``agent`` instantiates anthropic.AsyncAnthropic at import time — stub it out.
if "anthropic" not in sys.modules:
    anthropic_stub = types.ModuleType("anthropic")

    class AsyncAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    anthropic_stub.AsyncAnthropic = AsyncAnthropic
    sys.modules["anthropic"] = anthropic_stub

import main  # noqa: E402
from agent import _normalize_validation_profile  # noqa: E402
from auth import TradingIdentity  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


SUPERVISOR = "USA-1"


def _install_fakes(monkeypatch):
    """Bypass Cognito, and capture the kwargs handed to run_resubmit_workflow."""
    captured = {}

    async def fake_resolve_claims(request):
        return {"email": "dev@lumitec.com"}

    def fake_resolve_trading_identity(claims):
        return TradingIdentity(
            account_id="ACC-1", trader_id="TRD-1", supervisor_ids=[SUPERVISOR]
        )

    async def fake_run_resubmit_workflow(**kwargs):
        captured.update(kwargs)
        yield {"type": "done"}

    monkeypatch.setattr(main, "resolve_claims", fake_resolve_claims)
    monkeypatch.setattr(main, "resolve_trading_identity", fake_resolve_trading_identity)
    monkeypatch.setattr(main, "run_resubmit_workflow", fake_run_resubmit_workflow)
    return captured


def _base_body(**overrides):
    body = {
        "code": "class Config: pass",
        "legs": [],
        "supervisor_id": SUPERVISOR,
    }
    body.update(overrides)
    return body


def test_dev_profile_reaches_the_workflow_unchanged(monkeypatch):
    captured = _install_fakes(monkeypatch)
    client = TestClient(main.app)

    resp = client.post("/resubmit-strategy", json=_base_body(validation_profile="dev"))

    assert resp.status_code == 200
    assert captured["validation_profile"] == "dev"


def test_development_alias_normalizes_to_validator_development_profile():
    assert _normalize_validation_profile("development") == "development"
    # and the raw value still flows through the route untouched
    assert _normalize_validation_profile("dev") == "development"
    assert _normalize_validation_profile("research") == "development"
    assert _normalize_validation_profile("DevelopMent") == "development"


def test_omitted_profile_keeps_the_production_default(monkeypatch):
    captured = _install_fakes(monkeypatch)
    client = TestClient(main.app)

    resp = client.post("/resubmit-strategy", json=_base_body())

    assert resp.status_code == 200
    assert captured["validation_profile"] == "prod"
    assert _normalize_validation_profile(captured["validation_profile"]) == "production"
    assert _normalize_validation_profile(None) == "production"
