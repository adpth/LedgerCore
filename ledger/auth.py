import os
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request, Security
from fastapi.openapi.models import OAuthFlowClientCredentials, OAuthFlows
from fastapi.security import OAuth2
from fastapi.security.utils import get_authorization_scheme_param
from jwt import PyJWKClient
from starlette.requests import HTTPConnection

from ledger.console_auth import console_subject


class ClientCredentials(OAuth2):
    async def __call__(self, request: HTTPConnection) -> str | None:
        scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
        if not request.headers.get("Authorization"):
            return None
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"}
            )
        return token


oauth = ClientCredentials(
    flows=OAuthFlows(
        clientCredentials=OAuthFlowClientCredentials(
            tokenUrl=os.getenv("OAUTH_TOKEN_URL", "/oauth/token"),
            scopes={
                "ledger:read": "Read balances and journal",
                "ledger:transfer": "Post movements",
            },
        )
    )
)


@lru_cache
def jwks(url: str) -> PyJWKClient:
    return PyJWKClient(url, timeout=4)


def require(scope: str):
    def authenticate(request: Request, token: str | None = Security(oauth, scopes=[scope])) -> str:
        if token is None:
            if scope == "ledger:read" and request.method == "GET":
                return console_subject(request)
            raise HTTPException(
                401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            if os.getenv("LEDGER_DEMO") == "1":
                key = os.environ["DEMO_JWT_SECRET"]
                issuer, audience, algorithm = "ledgercore-demo", "ledgercore", "HS256"
            else:
                key = jwks(os.environ["OAUTH_JWKS_URL"]).get_signing_key_from_jwt(token).key
                issuer = os.environ["OAUTH_ISSUER"]
                audience, algorithm = os.environ["OAUTH_AUDIENCE"], "RS256"
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                issuer=issuer,
                audience=audience,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or "\x00" in subject:
                raise jwt.InvalidTokenError("Invalid subject")
            subject.encode("utf-8")
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise HTTPException(
                401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        scopes = claims.get("scope", "")
        roles = claims.get("roles", [])
        finance = isinstance(roles, list) and "finance" in roles
        if not (
            (finance and scope == "ledger:read")
            or (not finance and isinstance(scopes, str) and scope in scopes.split())
        ):
            raise HTTPException(403, "Required ledger permission is missing")
        return subject

    return authenticate
