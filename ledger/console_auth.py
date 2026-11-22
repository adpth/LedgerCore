import hmac
import os
import secrets
from base64 import urlsafe_b64encode
from hashlib import sha256
from time import time
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

router = APIRouter(prefix="/console", include_in_schema=False)
FLOW_SECONDS = 300
SESSION_SECONDS = 3600
JWT_ISSUER = "ledgercore-console"


def demo() -> bool:
    return os.getenv("LEDGER_DEMO") == "1"


def signing_key() -> str:
    key = os.getenv("DEMO_JWT_SECRET" if demo() else "CONSOLE_SESSION_SECRET", "")
    if len(key.encode()) < 32:
        raise ValueError("Console signing key must be configured")
    return key


def settings() -> dict[str, str]:
    names = ("ORIGIN", "CLIENT_ID", "CLIENT_SECRET", "AUTHORIZE_URL", "TOKEN_URL")
    config = {name: os.environ[f"CONSOLE_OIDC_{name}"] for name in names}
    for name in ("ORIGIN", "AUTHORIZE_URL", "TOKEN_URL"):
        url = urlsplit(config[name])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("Console OIDC requires HTTPS URLs")
        if name == "ORIGIN" and (url.path or url.query or config[name].endswith("/")):
            raise ValueError("Console origin must be an HTTPS origin without a path")
    if (
        not all(config.values())
        or not os.environ["OAUTH_ISSUER"]
        or not os.environ["OAUTH_JWKS_URL"]
    ):
        raise ValueError("Console OIDC configuration is incomplete")
    if config["CLIENT_ID"] == os.environ["OAUTH_AUDIENCE"]:
        raise ValueError("Use separate console and service token audiences")
    signing_key()
    return config


def cookie_name(kind: str) -> str:
    return ("ledgercore_demo_" if demo() else "__Host-ledgercore_") + kind


def seal(kind: str, claims: dict, seconds: int) -> str:
    return jwt.encode(
        {**claims, "iss": JWT_ISSUER, "aud": kind, "exp": int(time()) + seconds},
        signing_key(),
        algorithm="HS256",
    )


def unseal(request: Request, kind: str) -> dict:
    raw = request.cookies.get(cookie_name(kind), "")
    return jwt.decode(
        raw,
        signing_key(),
        algorithms=["HS256"],
        audience=kind,
        issuer=JWT_ISSUER,
        options={"require": ["exp", "aud", "iss"]},
    )


def set_cookie(response: Response, kind: str, claims: dict, seconds: int) -> None:
    response.set_cookie(
        cookie_name(kind),
        seal(kind, claims, seconds),
        max_age=seconds,
        httponly=True,
        secure=not demo(),
        samesite="lax",
    )


def clear_cookie(response: Response, kind: str) -> None:
    response.delete_cookie(cookie_name(kind), httponly=True, secure=not demo(), samesite="lax")


def console_subject(request: Request) -> str:
    try:
        return str(unseal(request, "session")["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(
            401, "Finance sign-in required", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


def require_same_origin(request: Request) -> None:
    origin = str(request.base_url).rstrip("/") if demo() else settings()["ORIGIN"]
    if request.headers.get("origin") != origin:
        raise HTTPException(403, "Origin does not match the console")


def redirect(target: str) -> RedirectResponse:
    return RedirectResponse(
        target,
        status_code=303,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/session")
def session_info(request: Request):
    try:
        enabled = bool(signing_key()) if demo() else bool(settings())
    except (ValueError, KeyError):
        enabled = False
    try:
        authenticated = bool(console_subject(request)) if enabled else False
    except HTTPException:
        authenticated = False
    return JSONResponse(
        {"mode": "demo" if demo() else "sso", "enabled": enabled, "authenticated": authenticated},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/demo")
def demo_sign_in(request: Request):
    if not demo():
        raise HTTPException(404)
    require_same_origin(request)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    try:
        set_cookie(response, "session", {"sub": "demo-finance"}, SESSION_SECONDS)
    except ValueError as exc:
        raise HTTPException(503, "Demo sign-in is unavailable") from exc
    return response


@router.get("/login")
def login():
    if demo():
        return redirect("/")
    try:
        config = settings()
    except (KeyError, ValueError):
        return redirect("/?sign_in=unavailable")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = urlencode(
        {
            "client_id": config["CLIENT_ID"],
            "redirect_uri": config["ORIGIN"] + "/console/callback",
            "response_type": "code",
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    endpoint = config["AUTHORIZE_URL"]
    response = redirect(endpoint + ("&" if "?" in endpoint else "?") + params)
    set_cookie(
        response, "flow", {"state": state, "nonce": nonce, "verifier": verifier}, FLOW_SECONDS
    )
    return response


@router.get("/callback")
def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    from ledger.auth import jwks

    if demo():
        return redirect("/")
    failed = redirect("/?sign_in=failed")
    try:
        config = settings()
        flow = unseal(request, "flow")
        if not state.isascii() or not hmac.compare_digest(state, flow["state"]):
            return failed
        clear_cookie(failed, "flow")
        if error or not 1 <= len(code) <= 4096:
            return failed
        exchange = httpx.post(
            config["TOKEN_URL"],
            auth=(config["CLIENT_ID"], config["CLIENT_SECRET"]),
            timeout=5,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config["ORIGIN"] + "/console/callback",
                "code_verifier": flow["verifier"],
            },
        )
        exchange.raise_for_status()
        raw = exchange.json()["id_token"]
        if not isinstance(raw, str):
            return failed
        key = jwks(os.environ["OAUTH_JWKS_URL"]).get_signing_key_from_jwt(raw).key
        claims = jwt.decode(
            raw,
            key,
            algorithms=["RS256"],
            issuer=os.environ["OAUTH_ISSUER"],
            audience=config["CLIENT_ID"],
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
        )
        audience, party = claims["aud"], claims.get("azp")
        if (
            claims["nonce"] != flow["nonce"]
            or (isinstance(audience, list) and len(audience) > 1)
            or (party is not None and party != config["CLIENT_ID"])
        ):
            return failed
        roles = claims.get("roles", [])
        if not isinstance(roles, list) or "finance" not in roles:
            denied = redirect("/?sign_in=denied")
            clear_cookie(denied, "flow")
            return denied
        subject = claims["sub"]
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            return failed
        seconds = min(SESSION_SECONDS, int(claims["exp"] - time()))
        if seconds < 1:
            return failed
        response = redirect("/")
        clear_cookie(response, "flow")
        set_cookie(response, "session", {"sub": subject}, seconds)
        return response
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError):
        return failed


@router.post("/logout")
def logout(request: Request):
    # Deleting an existing session must remain possible during an identity-provider outage.
    origin = str(request.base_url).rstrip("/") if demo() else os.getenv("CONSOLE_OIDC_ORIGIN")
    if not origin or request.headers.get("origin") != origin:
        raise HTTPException(403, "Origin does not match the console")
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    clear_cookie(response, "session")
    clear_cookie(response, "flow")
    return response
