"""POST /publish-strategy must forward the caller-chosen `visibility` to the
strategy server in the JSON body (owner/account/org stay server-derived), and
surface the strategy server's status code (403 for a disallowed visibility).
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "anthropic" not in sys.modules:
    anthropic_stub = types.ModuleType("anthropic")

    class AsyncAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    anthropic_stub.AsyncAnthropic = AsyncAnthropic
    sys.modules["anthropic"] = anthropic_stub

import main  # noqa: E402
from auth import Claims  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    """Captures the single POST publish_strategy makes and returns a canned reply."""

    captured: dict = {}
    reply = _FakeResponse(201, {"visibility": "private"})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeAsyncClient.reply


def _install(monkeypatch, *, reply=None):
    async def fake_resolve_claims(request):
        return Claims(sub="user-1", email="u@x.com", org_id="org-abc", groups=[])

    monkeypatch.setattr(main, "resolve_claims", fake_resolve_claims)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.reply = reply or _FakeResponse(201, {"visibility": "private"})


def _post(vis=None):
    body = {"name": "probe_strat", "code": "class Config: pass"}
    if vis is not None:
        body["visibility"] = vis
    return TestClient(main.app).post("/publish-strategy", json=body)


def test_visibility_defaults_to_private(monkeypatch):
    _install(monkeypatch)
    resp = _post()
    assert resp.status_code == 200
    assert _FakeAsyncClient.captured["json"]["visibility"] == "private"


def test_chosen_visibility_is_forwarded_in_body(monkeypatch):
    for vis in ("private", "shared", "public", "platform"):
        _install(monkeypatch, reply=_FakeResponse(201, {"visibility": vis}))
        resp = _post(vis)
        assert resp.status_code == 200
        sent = _FakeAsyncClient.captured["json"]
        assert sent["visibility"] == vis
        # identity stays server-derived — never sent in the body
        assert "owner" not in sent and "account_id" not in sent and "organization" not in sent


def test_strategy_server_403_is_surfaced(monkeypatch):
    _install(monkeypatch, reply=_FakeResponse(403, {"detail": "requires platform-admin access"}))
    resp = _post("platform")
    assert resp.status_code == 403
