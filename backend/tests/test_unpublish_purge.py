"""DELETE /published-strategies/{sid} (unpublish) and .../purge (permanent
delete) — owner-only, forward-to-registry proxies. Unpublish is reversible
(republishing un-hides it); purge is only allowed once already unpublished
and permanently forgets any local registry-link mapping pointing at it.
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

SID = "70b905b6-16f1-4548-b183-84437e11d5a2"


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeClient:
    """Fakes both .delete() (unpublish — no body) and .request("DELETE", ...)
    (purge — httpx's .delete() has no body param). Routes by URL suffix."""

    routes: dict = {}
    last_headers: dict = {}
    last_json = None
    last_method_for_purge = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def delete(self, url, headers=None):
        _FakeClient.last_headers = headers or {}
        return self._route(url)

    async def request(self, method, url, json=None, headers=None):
        _FakeClient.last_headers = headers or {}
        _FakeClient.last_json = json
        _FakeClient.last_method_for_purge = method
        return self._route(url)

    def _route(self, url):
        for suffix, resp in _FakeClient.routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"no fake route for {url}")


def _install(monkeypatch, routes, *, sub="sub-1"):
    async def fake_resolve_claims(request):
        return Claims(sub=sub, email="me@lumitec.com", org_id="org-1", groups=[])

    monkeypatch.setattr(main, "resolve_claims", fake_resolve_claims)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)
    _FakeClient.routes = routes
    _FakeClient.last_headers = {}
    _FakeClient.last_json = None
    _FakeClient.last_method_for_purge = None


# --- unpublish -----------------------------------------------------------

def test_unpublish_passes_through_success_and_forwards_bearer(monkeypatch):
    _install(monkeypatch, {f"/{SID}": _Resp(200, {
        "ok": True, "strategy_id": SID, "unpublished": True, "versions_updated": 2,
    })})
    r = TestClient(main.app).delete(f"/published-strategies/{SID}", headers={"Authorization": "Bearer xyz"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "strategy_id": SID, "unpublished": True, "versions_updated": 2}
    assert _FakeClient.last_headers.get("Authorization") == "Bearer xyz"


def test_unpublish_403_is_surfaced_structured(monkeypatch):
    _install(monkeypatch, {f"/{SID}": _Resp(403, {"detail": "Only the owner may unpublish a strategy"})})
    r = TestClient(main.app).delete(f"/published-strategies/{SID}")
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "Only the owner may unpublish a strategy"


def test_unpublish_404_is_surfaced(monkeypatch):
    _install(monkeypatch, {f"/{SID}": _Resp(404, {"detail": "not found"})})
    r = TestClient(main.app).delete(f"/published-strategies/{SID}")
    assert r.status_code == 404


def test_unpublish_success_does_not_touch_registry_links(monkeypatch, tmp_path):
    # Unpublish is reversible — only purge should ever mutate .registry_links.json.
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, {f"/{SID}": _Resp(200, {"ok": True, "strategy_id": SID, "unpublished": True, "versions_updated": 1})})
    r = TestClient(main.app).delete(f"/published-strategies/{SID}")
    assert r.status_code == 200
    assert not (tmp_path / "sub-1" / ".registry_links.json").exists()


# --- purge -----------------------------------------------------------------

def test_purge_sends_confirm_true_and_passes_through_success(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, {f"/{SID}/purge": _Resp(200, {
        "ok": True, "strategy_id": SID, "purged": True, "versions_deleted": 3, "s3_objects_deleted": 9,
    })})
    r = TestClient(main.app).request(
        "DELETE", f"/published-strategies/{SID}/purge", json={"confirm": True},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "strategy_id": SID, "purged": True, "versions_deleted": 3, "s3_objects_deleted": 9}
    assert _FakeClient.last_json == {"confirm": True}
    assert _FakeClient.last_method_for_purge == "DELETE"


def test_purge_409_not_yet_unpublished(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, {f"/{SID}/purge": _Resp(409, {"detail": "Strategy must be unpublished before it can be permanently deleted"})})
    r = TestClient(main.app).request("DELETE", f"/published-strategies/{SID}/purge", json={"confirm": True})
    assert r.status_code == 409
    assert "unpublished" in r.json()["detail"]["error"]


def test_purge_422_missing_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, {f"/{SID}/purge": _Resp(422, {"detail": "confirm must be true"})})
    r = TestClient(main.app).request("DELETE", f"/published-strategies/{SID}/purge", json={"confirm": False})
    assert r.status_code == 422


def test_purge_403_and_404_are_surfaced(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, {f"/{SID}/purge": _Resp(403, {"detail": "Only the owner may purge a strategy"})})
    r = TestClient(main.app).request("DELETE", f"/published-strategies/{SID}/purge", json={"confirm": True})
    assert r.status_code == 403

    _install(monkeypatch, {f"/{SID}/purge": _Resp(404, {"detail": "not found"})})
    r = TestClient(main.app).request("DELETE", f"/published-strategies/{SID}/purge", json={"confirm": True})
    assert r.status_code == 404


def test_purge_removes_matching_registry_link(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    # Seed a link as a prior successful publish would have.
    main._save_registry_link("sub-1", "my_strategy", logic_id=SID, version="1.0.0+0000000001", sha256="deadbeef")
    other_sid = "other-uuid-0000-0000-0000-000000000000"
    main._save_registry_link("sub-1", "other_strategy", logic_id=other_sid, version="1.0.0+0000000001", sha256="cafebabe")

    _install(monkeypatch, {f"/{SID}/purge": _Resp(200, {"ok": True, "strategy_id": SID, "purged": True, "versions_deleted": 1, "s3_objects_deleted": 3})})
    r = TestClient(main.app).request("DELETE", f"/published-strategies/{SID}/purge", json={"confirm": True})
    assert r.status_code == 200

    assert main._load_registry_link("sub-1", "my_strategy") is None
    # unrelated links are untouched
    assert main._load_registry_link("sub-1", "other_strategy")["logic_id"] == other_sid
