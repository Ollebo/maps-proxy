"""Tile gates for terracotta (spridningstillstand.md 5.2, 5.6 and 6).

Traefik calls these as forwardAuth endpoints before handing a tile request to
terracotta. A 2xx lets the request through; anything else is returned to the
client as is.

/auth/tiles        private, full-resolution tiles under /private-tiles/...
                   -> the caller must be a member of the space in the path.
/auth/public-tile  public, coarse tiles under /public/...
                   -> only rgb/singleband tiles at or below the dataset's
                      computed max public zoom, with an allowlisted query.

Both read the original request from X-Forwarded-Uri, which Traefik sets to the
path + query the client asked for (before any stripPrefix).
"""

import math
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import requests
from flask import jsonify, request

import auth

TERRACOTTA_PUBLIC_URL = os.environ.get("TERRACOTTA_PUBLIC_URL", "http://terracotta-public:5000")
MAXZOOM_TTL_S = int(os.environ.get("PUBLIC_MAXZOOM_TTL_S", "600"))

# Same formula as map-maker/code/compliance/zoom.py (policy 5.2). Tiles are
# always 256 px here: tile_size is not an allowed query parameter.
EARTH_RES_Z0 = 156543.03392804097
MIN_RES_M = 0.5
SAFETY = 1.2


def max_public_zoom(max_abs_lat_deg, tile_size=256, pixel_ratio=1):
    base = (EARTH_RES_Z0 * math.cos(math.radians(abs(max_abs_lat_deg)))
            * 256 / tile_size / pixel_ratio)
    return math.floor(math.log2(base / (MIN_RES_M * SAFETY)))


_SEG = r"[A-Za-z0-9_.-]+"
_PRIVATE_RE = re.compile(
    r"^/private-tiles/(?:rgb|singleband)/(?P<space>{0})/".format(_SEG)
)
_PUBLIC_RGB_RE = re.compile(
    r"^/public/rgb/(?P<space>{0})/(?P<map>{0})/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$".format(_SEG)
)
_PUBLIC_SB_RE = re.compile(
    r"^/public/singleband/(?P<space>{0})/(?P<map>{0})/(?P<variant>{0})/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$".format(_SEG)
)
_PUBLIC_PARAMS = frozenset([
    "r", "g", "b", "r_range", "g_range", "b_range", "colormap", "stretch_range",
])

_maxzoom_lock = threading.Lock()
_maxzoom_cache = {}  # (space, map, variant) -> (expires_at, maxzoom)


def _forwarded():
    uri = request.headers.get("X-Forwarded-Uri", "")
    parts = urlsplit(uri)
    return parts.path, parse_qs(parts.query, keep_blank_values=True)


def private_tiles():
    path, _ = _forwarded()
    m = _PRIVATE_RE.match(path)
    if not m:
        return jsonify({"error": "not found"}), 404
    err = auth.check_space_access(request, m.group("space"))
    if err is not None:
        status, body = err
        return jsonify(body), status
    resp = jsonify({"ok": True})
    return resp, 200


def _dataset_maxzoom(space, map_id, variant):
    key = (space, map_id, variant)
    now = time.time()
    with _maxzoom_lock:
        hit = _maxzoom_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    url = "{0}/metadata/{1}/{2}/{3}".format(TERRACOTTA_PUBLIC_URL, space, map_id, variant)
    resp = requests.get(url, timeout=5)
    if resp.status_code == 404:
        zoom = None
    else:
        resp.raise_for_status()
        west, south, east, north = resp.json()["bounds"]
        zoom = max_public_zoom(max(abs(float(south)), abs(float(north))))
    with _maxzoom_lock:
        _maxzoom_cache[key] = (now + MAXZOOM_TTL_S, zoom)
    return zoom


def forget_maxzoom(space, map_id):
    with _maxzoom_lock:
        for key in [k for k in _maxzoom_cache if k[0] == space and k[1] == map_id]:
            del _maxzoom_cache[key]


def public_tile():
    path, query = _forwarded()
    m = _PUBLIC_RGB_RE.match(path) or _PUBLIC_SB_RE.match(path)
    if not m:
        return jsonify({"error": "not found"}), 404
    unknown = set(query) - _PUBLIC_PARAMS
    if unknown:
        return jsonify({"error": "parameter not allowed", "params": sorted(unknown)}), 400

    groups = m.groupdict()
    variant = groups.get("variant")
    if variant is None:
        # rgb composite: the zoom gate is checked against the red channel; the
        # three channels are built from the same coarse source.
        variant = (query.get("r") or [""])[0]
        if not variant:
            return jsonify({"error": "missing r"}), 400
    try:
        maxzoom = _dataset_maxzoom(groups["space"], groups["map"], variant)
    except Exception as e:
        print("ERROR public maxzoom lookup {0}: {1}".format(path, e))
        return jsonify({"error": "unavailable"}), 503
    if maxzoom is None:
        return jsonify({"error": "not found"}), 404
    if int(groups["z"]) > maxzoom:
        # Overzoom happens in the client (policy 5.2); never render past maxzoom.
        resp = jsonify({"error": "zoom above public maximum", "maxzoom": maxzoom})
        resp.headers["X-Max-Zoom"] = str(maxzoom)
        return resp, 404
    return jsonify({"ok": True}), 200
