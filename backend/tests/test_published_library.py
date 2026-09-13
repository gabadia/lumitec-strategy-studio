"""GET /published-strategies[/{id}] — read-only proxy to the strategy server
registry, with mine/group tagging and a metadata+source merge for the editor.
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

ME = "me@lumitec.com"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeClient:
    """Routes .get() by URL suffix; captures the last request headers."""

    routes: dict = {}
    last_headers: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.last_headers = headers or {}
        for suffix, resp in _FakeClient.routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"no fake route for {url}")


def _install(monkeypatch, routes):
    async def fake_resolve_claims(request):
        return Claims(sub="sub-1", email=ME, org_id="org-1", groups=[])

    monkeypatch.setattr(main, "resolve_claims", fake_resolve_claims)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)
    _FakeClient.routes = routes
    _FakeClient.last_headers = {}


def test_list_tags_mine_and_group(monkeypatch):
    listing = {"strategies": [
        {"strategy_id": "a", "display_name": "mine_priv", "owner_user": ME, "visibility": "private"},
        {"strategy_id": "b", "display_name": "mine_pub", "owner_user": ME, "visibility": "public"},
        {"strategy_id": "c", "display_name": "colleague_shared", "owner_user": "other@lumitec.com", "visibility": "shared"},
        {"strategy_id": "d", "display_name": "someones_public", "owner_user": "x@y.com", "visibility": "public"},
        {"strategy_id": "e", "display_name": "official", "owner_user": "lumitec", "visibility": "platform"},
    ]}
    _install(monkeypatch, {"/strategies": _Resp(200, listing)})

    data = TestClient(main.app).get("/published-strategies").json()
    by_id = {s["strategy_id"]: s for s in data["strategies"]}
    assert data["count"] == 5
    assert (by_id["a"]["mine"], by_id["a"]["group"]) == (True, "mine")
    assert (by_id["b"]["mine"], by_id["b"]["group"]) == (True, "mine")
    assert (by_id["c"]["mine"], by_id["c"]["group"]) == (False, "org")
    assert (by_id["d"]["mine"], by_id["d"]["group"]) == (False, "public")
    assert (by_id["e"]["mine"], by_id["e"]["group"]) == (False, "platform")


def test_list_forwards_bearer(monkeypatch):
    _install(monkeypatch, {"/strategies": _Resp(200, {"strategies": []})})
    TestClient(main.app).get("/published-strategies", headers={"Authorization": "Bearer xyz"})
    assert _FakeClient.last_headers.get("Authorization") == "Bearer xyz"


def test_list_surfaces_upstream_error(monkeypatch):
    _install(monkeypatch, {"/strategies": _Resp(503, "boom")})
    r = TestClient(main.app).get("/published-strategies")
    assert r.status_code == 503


def test_get_merges_metadata_and_source(monkeypatch):
    sid = "70b905b6-16f1-4548-b183-84437e11d5a2"
    meta = {
        "strategy_id": sid, "display_name": "bid_ask_spread_capture", "class_name": "BidAskSpreadCapture",
        "visibility": "public", "owner_user": ME, "organization": "org-1",
        "user_version": "1.0.0", "revision": 3, "strategy_hash": "9f2c",
        "execution_mode": "inline_code", "params": [{"name": "edge"}], "leg_schema": [],
        "mission": "EXECUTION", "objective": "SIGNAL_DRIVEN",
        "created_at": "2026-09-10T15:14:53Z", "updated_at": "2026-09-10T15:14:53Z",
    }
    code = {"strategy_id": sid, "class_name": "BidAskSpreadCapture", "execution_mode": "inline_code",
            "user_version": "1.0.0", "revision": 3, "strategy_hash": "9f2c", "code": "class Config: pass\n"}
    _install(monkeypatch, {f"/{sid}/code": _Resp(200, code), f"/{sid}": _Resp(200, meta)})

    data = TestClient(main.app).get(f"/published-strategies/{sid}").json()
    assert data["code"] == "class Config: pass\n"
    assert data["display_name"] == "bid_ask_spread_capture"
    assert data["revision"] == 3
    assert data["strategy_hash"] == "9f2c"
    assert data["params"] == [{"name": "edge"}]


def test_get_404_is_surfaced(monkeypatch):
    sid = "deadbeef-0000-0000-0000-000000000000"
    _install(monkeypatch, {f"/{sid}/code": _Resp(404, "nope"), f"/{sid}": _Resp(404, "nope")})
    r = TestClient(main.app).get(f"/published-strategies/{sid}")
    assert r.status_code == 404
