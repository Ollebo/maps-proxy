import io
import json
import os
import time

import requests
from flask import Response, jsonify, make_response, request

import minioBackend


# Vector tiles for the maps we host ourselves (the `geodata` repo imports them
# into PostGIS; martin turns a tile function into MVT). maps-proxy keeps the
# public URL space -- https://maps.ollebo.com/<layer>/{z}/{x}/{y}.pbf -- so
# adding a layer needs no ingress or manifest change, and the credentialed CORS
# this app already does keeps working for the private layers that will follow.
MARTIN_URL = os.environ.get("MARTIN_URL", "http://martin:3000").rstrip("/")

# Rendered tiles are written back to the public bucket, so a tile is served
# from object storage on every request after the first. The import version is
# part of the key, which means a re-import invalidates the whole layer without a
# purge and without a stampede.
TILE_CACHE_BUCKET = os.environ.get("TILE_CACHE_BUCKET") or minioBackend.S3_FILE_BUCKET
TILES_PREFIX = os.environ.get("GEODATA_TILES_PREFIX", "tiles")

MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"
TILE_CACHE_CONTROL = "public, max-age=86400, stale-while-revalidate=604800"

MARTIN_TIMEOUT = float(os.environ.get("MARTIN_TIMEOUT", "30"))

# Names this app (and the terracotta ingress in front of it) already answers on.
# A layer called "models" would otherwise shadow the auth-gated prefix, so the
# tile routes refuse them even though the importer refuses them too -- the two
# lists are cheap, and only one of them is deployed with the proxy.
RESERVED_LAYER_IDS = frozenset([
    "cache", "colormap", "compute", "datasets", "healthz", "keys", "metadata",
    "metrics", "models", "private", "proxy", "ready", "rgb", "singleband",
    "tiles",
])

# A layer's tile.json is read once per tile-cache lookup, so it is held briefly
# in memory. Short, because the value it carries is the import version: a
# re-import must start filling the new cache prefix without a proxy restart.
_METADATA_TTL_SECONDS = float(os.environ.get("TILE_METADATA_TTL", "60"))
_metadata_cache = {}


def _valid_layer(layer):
    return (
        layer
        and layer not in RESERVED_LAYER_IDS
        and all(c.isalnum() or c == "-" for c in layer)
        and layer[0].isalpha()
    )


def _get_object(bucket, key):
    response = None
    try:
        response = minioBackend.client.get_object(bucket, key)
        return response.read()
    except Exception:
        return None
    finally:
        if response is not None:
            response.close()
            response.release_conn()


def _put_object(bucket, key, data, content_type):
    try:
        minioBackend.client.put_object(
            bucket, key, io.BytesIO(data), len(data), content_type=content_type)
    except Exception as error:
        # A cache write failing must not fail the request -- the tile is already
        # rendered and in hand; the next request just pays for it again.
        print("tile cache write failed for {0}: {1}".format(key, error))


def _metadata_key(layer, name):
    return "{0}/{1}/{2}".format(TILES_PREFIX, layer, name)


def layer_metadata(layer):
    """The layer's published tile.json, or None if we host no such layer.

    Written to the bucket by `geodata import`, which is what keeps a database
    client out of this process.
    """
    cached = _metadata_cache.get(layer)
    if cached and cached[0] > time.time():
        return cached[1]

    raw = _get_object(TILE_CACHE_BUCKET, _metadata_key(layer, "tile.json"))
    document = None
    if raw:
        try:
            document = json.loads(raw)
        except ValueError:
            document = None
    _metadata_cache[layer] = (time.time() + _METADATA_TTL_SECONDS, document)
    return document


def _tile_response(data):
    response = make_response(data)
    response.headers.set("Content-Type", MVT_CONTENT_TYPE)
    response.headers.set("Cache-Control", TILE_CACHE_CONTROL)
    return response


def _fetch_from_martin(function_name, z, x, y):
    url = "{0}/{1}/{2}/{3}/{4}".format(MARTIN_URL, function_name, z, x, y)
    # martin gzips MVT by default, and ol/format/MVT cannot inflate gzip itself
    # -- only the browser does, and only when Content-Encoding survives. Asking
    # for identity keeps raw bytes in the cache; Traefik compresses on the wire.
    response = requests.get(
        url, headers={"Accept-Encoding": "identity"}, timeout=MARTIN_TIMEOUT)
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return response.content


def tile(layer, z, x, y):
    # These routes sit in front of the static catch-all, so anything they do not
    # recognise has to fall through to it rather than 404 -- otherwise adding
    # them would take away paths that used to serve real objects.
    static_path = "{0}/{1}/{2}/{3}.pbf".format(layer, z, x, y)
    if not _valid_layer(layer):
        return minioBackend.getFile(static_path)

    metadata = layer_metadata(layer)
    if metadata is None:
        return minioBackend.getFile(static_path)

    if z < metadata.get("minzoom", 0) or z > metadata.get("maxzoom", 22):
        return Response(status=204)

    version = (metadata.get("ollebo") or {}).get("version", 1)
    key = "{0}/{1}/v{2}/{3}/{4}/{5}.pbf".format(
        TILES_PREFIX, layer, version, z, x, y)

    cached = _get_object(TILE_CACHE_BUCKET, key)
    if cached is not None:
        return _tile_response(cached)

    try:
        data = _fetch_from_martin("tile_" + layer.replace("-", "_"), z, x, y)
    except requests.RequestException as error:
        print("martin request failed for {0}/{1}/{2}/{3}: {4}".format(
            layer, z, x, y, error))
        return jsonify({"error": "tile backend unavailable"}), 502

    if not data:
        # An empty tile is a real answer, not an error: the layer has nothing
        # here. 204 keeps it out of the cache so a later import can fill it.
        return Response(status=204)

    _put_object(TILE_CACHE_BUCKET, key, data, MVT_CONTENT_TYPE)
    return _tile_response(data)


def metadata_document(layer, name):
    """Serve a layer's tile.json / style.json out of the bucket."""
    if not _valid_layer(layer):
        return minioBackend.getFile("{0}/{1}".format(layer, name))
    raw = _get_object(TILE_CACHE_BUCKET, _metadata_key(layer, name))
    if raw is None:
        return minioBackend.getFile("{0}/{1}".format(layer, name))
    response = make_response(raw)
    response.headers.set("Content-Type", "application/json")
    response.headers.set("Cache-Control", "public, max-age=300")
    return response


# --- upstream proxying -------------------------------------------------------

# Geoportale Nazionale (pcn) is HTTP-only -- its HTTPS listener redirects to a
# dead host -- so the Italian IGM layers cannot be requested straight from
# https://dash.ollebo.com without mixed-content blocking. ollebo-maps has always
# pointed them at ${MAPS_ORIGIN}/proxy/pcn and vite proxies that in dev; this is
# the production half, which had never been written.
PROXY_UPSTREAMS = {
    "pcn": os.environ.get("PCN_UPSTREAM", "http://wms.pcn.minambiente.it"),
}

# Response headers that describe the upstream connection rather than the body.
# Forwarding them would contradict how we actually send the response.
_HOP_BY_HOP = frozenset([
    "connection", "content-encoding", "content-length", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
])


def proxy(name, path):
    upstream = PROXY_UPSTREAMS.get(name)
    if upstream is None:
        return jsonify({"error": "unknown proxy"}), 404

    url = "{0}/{1}".format(upstream.rstrip("/"), path)
    try:
        upstream_response = requests.get(
            url, params=request.args, timeout=60, stream=True)
    except requests.RequestException as error:
        print("proxy {0} failed for {1}: {2}".format(name, url, error))
        return jsonify({"error": "upstream unavailable"}), 502

    response = make_response(upstream_response.content, upstream_response.status_code)
    for header, value in upstream_response.headers.items():
        if header.lower() not in _HOP_BY_HOP:
            response.headers.set(header, value)
    return response
