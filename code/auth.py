import os
import threading

import jwt
import requests
from jwt.algorithms import RSAAlgorithm


OIDC_DISCOVERY_URL = os.environ["OIDC_DISCOVERY_URL"]
OIDC_COOKIE_NAME = os.environ.get("OIDC_COOKIE_NAME", "access_token")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE")


_discovery = requests.get(OIDC_DISCOVERY_URL, timeout=10).json()
_ISSUER = _discovery["issuer"]
_JWKS_URI = _discovery["jwks_uri"]

_jwks_lock = threading.Lock()
_jwks_by_kid = {}


def _refresh_jwks():
    keys = requests.get(_JWKS_URI, timeout=10).json().get("keys", [])
    fresh = {}
    for jwk in keys:
        kid = jwk.get("kid")
        if kid:
            fresh[kid] = RSAAlgorithm.from_jwk(jwk)
    with _jwks_lock:
        _jwks_by_kid.clear()
        _jwks_by_kid.update(fresh)


def _signing_key(kid):
    with _jwks_lock:
        key = _jwks_by_kid.get(kid)
    if key is not None:
        return key
    _refresh_jwks()
    with _jwks_lock:
        return _jwks_by_kid.get(kid)


def validate_token(token):
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("missing kid")
    key = _signing_key(kid)
    if key is None:
        raise jwt.InvalidTokenError("unknown kid")
    options = {"verify_aud": OIDC_AUDIENCE is not None}
    return jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        issuer=_ISSUER,
        audience=OIDC_AUDIENCE,
        options=options,
    )


def _realm_roles(claims):
    return claims.get("realm_access", {}).get("roles", []) or []


def check_space_access(request, space_id):
    token = request.cookies.get(OIDC_COOKIE_NAME)
    if not token:
        return 401, {"error": "unauthenticated"}
    try:
        claims = validate_token(token)
    except jwt.PyJWTError:
        return 401, {"error": "unauthenticated"}
    if space_id not in _realm_roles(claims):
        return 403, {"error": "forbidden"}
    return None
