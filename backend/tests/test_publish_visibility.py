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
    # {"detail": "<str>"} from the server is unwrapped one level into error
    assert resp.json()["detail"]["error"] == "requires platform-admin access"


def test_validator_errors_are_forwarded_structured(monkeypatch):
    server_body = {
        "detail": {
            "message": "Strategy validation failed",
            "validation_profile": "production",
            "errors": [
                {"code": "security_forbidden_builtin", "phase": "security",
                 "message": "Use of built-in 'open()' is not permitted in strategies.",
                 "line": 586, "col": 25, "detail": None},
            ],
        }
    }
    _install(monkeypatch, reply=_FakeResponse(422, server_body))
    resp = _post("private")
    assert resp.status_code == 422
    err = resp.json()["detail"]["error"]
    assert err["message"] == "Strategy validation failed"
    assert err["errors"][0]["phase"] == "security"
    assert err["errors"][0]["line"] == 586


# --- sha256 + logic_id (immutable identity) -----------------------------------
# The registry strips the code before hashing on its end and accepts a
# `logic_id` in the publish body to mean "update this existing entry". These
# tests monkeypatch STRATEGIES_DIR to a tmp_path so the .registry_links.json
# bookkeeping file never touches the real data/ directory.

def test_sha256_is_computed_from_stripped_code(monkeypatch, tmp_path):
    import hashlib

    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, reply=_FakeResponse(201, {"visibility": "private"}))
    code = "  \nclass Config: pass\n  \n"
    resp = TestClient(main.app).post("/publish-strategy", json={"name": "probe_strat", "code": code})
    assert resp.status_code == 200
    expected = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
    assert _FakeAsyncClient.captured["json"]["sha256"] == expected
    # registry reply carried no sha256 of its own — Studio's own hash is the fallback
    assert resp.json()["sha256"] == expected


def test_first_publish_sends_no_logic_id(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)
    _install(monkeypatch, reply=_FakeResponse(
        201, {"visibility": "private", "logic_id": "abc-123", "version": "1.0.0+0000000001"},
    ))
    resp = _post()
    assert resp.status_code == 200
    assert "logic_id" not in _FakeAsyncClient.captured["json"]
    data = resp.json()
    assert data["logic_id"] == "abc-123"
    assert data["version"] == "1.0.0+0000000001"


def test_republish_of_same_name_reuses_logic_id(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)

    _install(monkeypatch, reply=_FakeResponse(
        201, {"visibility": "private", "logic_id": "abc-123", "version": "1.0.0+0000000001", "sha256": "deadbeef"},
    ))
    first = _post()
    assert first.status_code == 200
    assert "logic_id" not in _FakeAsyncClient.captured["json"]

    _install(monkeypatch, reply=_FakeResponse(
        201, {"visibility": "private", "logic_id": "abc-123", "version": "1.0.0+0000000002", "sha256": "cafebabe"},
    ))
    second = _post()
    assert second.status_code == 200
    assert _FakeAsyncClient.captured["json"]["logic_id"] == "abc-123"
    assert second.json()["version"] == "1.0.0+0000000002"


def test_different_name_does_not_reuse_logic_id(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "STRATEGIES_DIR", tmp_path)

    _install(monkeypatch, reply=_FakeResponse(201, {"visibility": "private", "logic_id": "abc-123"}))
    r1 = TestClient(main.app).post("/publish-strategy", json={"name": "strat_a", "code": "x = 1"})
    assert r1.status_code == 200

    _install(monkeypatch, reply=_FakeResponse(201, {"visibility": "private", "logic_id": "xyz-789"}))
    r2 = TestClient(main.app).post("/publish-strategy", json={"name": "strat_b", "code": "y = 2"})
    assert r2.status_code == 200
    assert "logic_id" not in _FakeAsyncClient.captured["json"]
