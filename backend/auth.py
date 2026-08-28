"""
Identity for Lumitec Strategy Studio, against the real deployed Cognito
user pool shared with lumitec-desk-cloud / lumitec-desk-ui.

Verifies the bearer JWT (Studio's own Cognito app client) in the
Authorization header, then resolves the caller's trading identity —
account_id / trader_id / entitled supervisor_ids — from a small,
admin-maintained map (DEMO_USER_ENTITLEMENTS). That map is NOT the
source of truth for entitlement enforcement (the real orchestrator's
DynamoDB entitlements table is, and it re-checks on every submit) — it
only tells Studio what to put in the submit payload for a known user.
There is no live lookup endpoint for this yet (that's a future
admin/management application's job); until then, adding a new demo user
means adding them here AND to the entitlements table via
lumitec-desk-cloud/scripts/seed_entitlement.py.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastapi import HTTPException, Request

COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
COGNITO_REGION = os.getenv("COGNITO_REGION", "")
COGNITO_APP_CLIENT_ID = os.getenv("COGNITO_APP_CLIENT_ID", "")


@dataclass(frozen=True)
class Claims:
    sub: str
    email: str
    org_id: str | None
    groups: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradingIdentity:
    account_id: str
    trader_id: str
    supervisor_ids: list[str]


def _load_demo_entitlements() -> dict[str, TradingIdentity]:
    """DEMO_USER_ENTITLEMENTS is a JSON object keyed by email:
    {"user@example.com": {"account_id": "...", "trader_id": "...", "supervisor_ids": ["USA-1", "SPAIN-1"]}}
    """
    raw = os.getenv("DEMO_USER_ENTITLEMENTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DEMO_USER_ENTITLEMENTS is not valid JSON: {exc}")
    result: dict[str, TradingIdentity] = {}
    for email, entry in parsed.items():
        result[email.lower()] = TradingIdentity(
            account_id=entry["account_id"],
            trader_id=entry["trader_id"],
            supervisor_ids=list(entry["supervisor_ids"]),
        )
    return result


_DEMO_ENTITLEMENTS = _load_demo_entitlements()


def resolve_trading_identity(claims: Claims) -> TradingIdentity:
    identity = _DEMO_ENTITLEMENTS.get(claims.email.lower())
    if identity is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"'{claims.email}' is not provisioned for trading in Studio. "
                "Ask an admin to add them to DEMO_USER_ENTITLEMENTS and grant "
                "entitlements via seed_entitlement.py."
            ),
        )
    return identity


class _JWKSCache:
    """Caches the Cognito user pool's signing keys, refreshed hourly or on a
    kid miss (covers Cognito's periodic key rotation)."""

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    async def get_key(self, kid: str) -> dict:
        if not self._keys or time.monotonic() - self._fetched_at > 3600:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise HTTPException(status_code=401, detail="Unknown token signing key")
        return key

    async def _refresh(self) -> None:
        if not COGNITO_USER_POOL_ID or not COGNITO_REGION:
            raise HTTPException(
                status_code=500,
                detail="Cognito auth is not configured (COGNITO_USER_POOL_ID/COGNITO_REGION missing)",
            )
        url = (
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        self._keys = {k["kid"]: k for k in resp.json()["keys"]}
        self._fetched_at = time.monotonic()


_jwks_cache = _JWKSCache()


async def _verify_cognito_jwt(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Malformed token: {exc}")

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="Token missing key id")

    jwk = await _jwks_cache.get_key(kid)
    public_key = RSAAlgorithm.from_jwk(jwk)

    issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            issuer=issuer,
            # Cognito ID tokens carry the client id in `aud`; access tokens
            # carry it in `client_id` instead — checked explicitly below.
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    if claims.get("token_use") not in ("id", "access"):
        raise HTTPException(status_code=401, detail="Unsupported token_use")

    if COGNITO_APP_CLIENT_ID:
        client_id = claims.get("aud") or claims.get("client_id")
        if client_id != COGNITO_APP_CLIENT_ID:
            raise HTTPException(status_code=401, detail="Token was not issued for Studio's app client")

    return claims


def _claims_from_jwt(raw: dict) -> Claims:
    return Claims(
        sub=raw["sub"],
        email=raw.get("email", ""),
        org_id=raw.get("custom:organization_id"),
        groups=list(raw.get("cognito:groups") or []),
    )


async def resolve_claims(request: Request) -> Claims:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth_header[len("Bearer "):].strip()
    raw_claims = await _verify_cognito_jwt(token)
    return _claims_from_jwt(raw_claims)
