import os
import threading
import uuid

import jwt
import requests
from jwt.algorithms import RSAAlgorithm


OIDC_DISCOVERY_URL = os.environ["OIDC_DISCOVERY_URL"]
OIDC_COOKIE_NAME = os.environ.get("OIDC_COOKIE_NAME", "access_token")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE")

# Issuer strings this proxy will accept, comma-separated. One Keycloak serves
# both auth.ollebo.com and auth.northamlin.com -- one realm, one user pool, one
# set of signing keys -- so a token minted on either host verifies against the
# JWKS below and differs only in its `iss` claim. Without the second name here,
# every private map, model and layer on the US site 401s.
#
# Only the ISSUERS are configured, not a second discovery URL: the keys are the
# same, so fetching the other host's metadata would buy nothing and would make
# this module's import-time fetch depend on two hostnames instead of one. A pod
# enforces whatever it read at boot, so widen this and roll the deployment
# BEFORE a second issuer starts minting tokens, never after.
OIDC_ALLOWED_ISSUERS = os.environ.get("OIDC_ALLOWED_ISSUERS", "")


_discovery = requests.get(OIDC_DISCOVERY_URL, timeout=10).json()
_ISSUER = _discovery["issuer"]
_JWKS_URI = _discovery["jwks_uri"]

# Defaults to the discovery document's own issuer, so an unset env var keeps the
# single-issuer behaviour this had before.
_ISSUERS = {i.strip() for i in OIDC_ALLOWED_ISSUERS.split(",") if i.strip()} or {_ISSUER}

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
    # `iss` is checked here rather than through jwt.decode(issuer=...), which
    # takes a single string and cannot express an allowlist. Everything else
    # PyJWT validates -- signature, exp, and aud when one is configured -- is
    # unchanged; this only widens which issuer is acceptable.
    options = {"verify_aud": OIDC_AUDIENCE is not None, "verify_iss": False}
    claims = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        audience=OIDC_AUDIENCE,
        options=options,
    )
    if claims.get("iss") not in _ISSUERS:
        raise jwt.InvalidIssuerError(claims.get("iss"))
    return claims


def _canonical_uuid(value):
    # Mirror the ollebo API's jwt_auth._as_space_uuid: a space id is a Keycloak
    # group whose name is the space UUID. Canonicalize to a lowercase hyphenated
    # UUID and drop anything that isn't one (legacy group paths, default groups).
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip())).lower()
    except (ValueError, AttributeError):
        return None


def _space_grants(claims):
    # Space membership arrives in the "groups" claim (group name == space id).
    # Keycloak may emit a bare name or a "/name" path; strip the slash and keep
    # only valid UUIDs. Realm roles are included as a fallback.
    grants = set()
    for group in claims.get("groups", []) or []:
        raw = group[1:] if isinstance(group, str) and group.startswith("/") else group
        canon = _canonical_uuid(raw)
        if canon:
            grants.add(canon)
    for role in claims.get("realm_access", {}).get("roles", []) or []:
        canon = _canonical_uuid(role)
        if canon:
            grants.add(canon)
    return grants


def _extract_token(request):
    # Prefer the Authorization: Bearer header -- identical to how the ollebo API
    # (jwt_auth.py) authenticates -- so the SPA sends the same token to both.
    # Fall back to the access_token cookie for browser subresource requests that
    # cannot set headers (e.g. <img> map tiles).
    header = request.headers.get("Authorization", "")
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return request.cookies.get(OIDC_COOKIE_NAME)


def check_space_access(request, space_id):
    token = _extract_token(request)
    if not token:
        return 401, {"error": "unauthenticated"}
    try:
        claims = validate_token(token)
    except jwt.PyJWTError:
        return 401, {"error": "unauthenticated"}
    want = _canonical_uuid(space_id) or space_id
    if want not in _space_grants(claims):
        return 403, {"error": "forbidden"}
    return None
