"""Cached proxy for external tile services: /ext/<source_id>/{z}/{x}/{y}.<fmt>

Some layers we only need to *show* (a topographic background, historical
orthophotos), not analyse. They are not worth importing, but no user request may
ever reach the provider either: every tile is fetched upstream at most once per
cache TTL, then served from our own bucket.

Which sources exist, and whether they may be served, comes from geodata's
published catalog (tiles/index.json): only rows with mode=proxy, status=enabled
and commercial_ok=true are answered. Anything else is a 404 -- that check is
here, in the server, so a direct tile URL for a blocked source never works no
matter what the UI shows.

Upstream protection, per source:
  * token bucket -- the source's rateLimit, divided across gunicorn workers;
  * request coalescing -- concurrent misses on one tile make one upstream call
    (per worker process; the deployment runs a single replica);
  * stale-while-revalidate -- an expired tile is served immediately and
    refreshed in the background;
  * circuit breaker -- after repeated upstream errors the source is left alone
    for a while, and we serve stale tiles or a 503.

Credentials for upstreams that need them (Lantmäteriet Geotorget) come from the
pod's environment, are added here, and never appear in a URL we log or publish.
"""
import base64
import hashlib
import io
import json
import os
import threading
import time

import requests
from flask import Response, jsonify, make_response, request
from prometheus_client import Counter, Histogram

import minioBackend


TILE_CACHE_BUCKET = os.environ.get("TILE_CACHE_BUCKET") or minioBackend.S3_FILE_BUCKET
TILES_PREFIX = os.environ.get("GEODATA_TILES_PREFIX", "tiles")
INDEX_TTL_SECONDS = float(os.environ.get("EXT_INDEX_TTL", "60"))
UPSTREAM_TIMEOUT = float(os.environ.get("EXT_UPSTREAM_TIMEOUT", "20"))
# How long a request may wait for a rate-limit token before it gives up.
TOKEN_WAIT_SECONDS = float(os.environ.get("EXT_TOKEN_WAIT", "3"))
BREAKER_THRESHOLD = int(os.environ.get("EXT_BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN_SECONDS = float(os.environ.get("EXT_BREAKER_COOLDOWN", "60"))
# Each gunicorn worker is its own process with its own buckets, so the
# configured per-source rate is split between them.
WORKERS = max(1, int(os.environ.get("GUNICORN_WORKERS", "4")))

CONTENT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp"}
FETCHED_AT_META = "x-amz-meta-fetched-at"

USER_AGENT = "ollebo-maps-proxy (tile cache; https://ollebo.com)"


# --- metrics ---------------------------------------------------------------

EXT_REQUESTS = Counter(
    "ext_tile_requests_total", "Requests to /ext by source and cache result",
    ["source", "result"])  # hit | stale | miss | refused | error
EXT_UPSTREAM = Counter(
    "ext_upstream_requests_total", "Upstream fetches by source and outcome",
    ["source", "outcome"])  # ok | empty | error | throttled | breaker_open
EXT_UPSTREAM_LATENCY = Histogram(
    "ext_upstream_seconds", "Upstream fetch latency", ["source"])
EXT_BYTES_STORED = Counter(
    "ext_cache_bytes_written_total", "Bytes written to the tile cache", ["source"])


# --- catalog ---------------------------------------------------------------

_index = {"expires": 0.0, "sources": {}}
_index_lock = threading.Lock()


def _read_object(key):
    response = None
    try:
        response = minioBackend.client.get_object(TILE_CACHE_BUCKET, key)
        headers = {k.lower(): v for k, v in response.headers.items()}
        return response.read(), headers
    except Exception:
        return None, None
    finally:
        if response is not None:
            response.close()
            response.release_conn()


def servable_sources():
    """{id: catalog row} for proxy sources we are allowed to serve."""
    now = time.time()
    if _index["expires"] > now:
        return _index["sources"]
    with _index_lock:
        if _index["expires"] > now:
            return _index["sources"]
        raw, _ = _read_object("{0}/index.json".format(TILES_PREFIX))
        sources = {}
        if raw:
            try:
                rows = json.loads(raw)
            except ValueError:
                rows = []
            for row in rows:
                if (row.get("mode") == "proxy"
                        and row.get("status") == "enabled"
                        and row.get("commercial_ok") is True
                        and (row.get("upstream") or {}).get("url")):
                    sources[row["id"]] = row
        elif _index["sources"]:
            # An S3 hiccup must not turn every proxy layer off; keep the last
            # good list until the next refresh. A *readable* index without the
            # source is what removes it.
            sources = _index["sources"]
        _index["sources"] = sources
        _index["expires"] = now + INDEX_TTL_SECONDS
        return sources


# --- upstream protection ---------------------------------------------------

class TokenBucket:
    def __init__(self, rate, burst):
        self.rate = rate
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def take(self, wait):
        deadline = time.monotonic() + wait
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                needed = (1.0 - self.tokens) / self.rate
            if time.monotonic() + needed > deadline:
                return False
            time.sleep(min(needed, 0.25))


class Breaker:
    def __init__(self):
        self.failures = 0
        self.open_until = 0.0
        self.lock = threading.Lock()

    def allow(self):
        return time.monotonic() >= self.open_until

    def success(self):
        with self.lock:
            self.failures = 0

    def failure(self):
        with self.lock:
            self.failures += 1
            if self.failures >= BREAKER_THRESHOLD:
                self.open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
                self.failures = 0
                return True
        return False


_buckets = {}
_breakers = {}
_inflight = {}
_state_lock = threading.Lock()


def _bucket_for(source_id, upstream):
    rate = float(upstream.get("rateLimit", 1)) / WORKERS
    burst = max(1, int(upstream.get("burst", 1)) // WORKERS)
    with _state_lock:
        bucket = _buckets.get(source_id)
        if bucket is None or bucket.rate != rate:
            bucket = _buckets[source_id] = TokenBucket(rate, burst)
        return bucket


def _breaker_for(source_id):
    with _state_lock:
        return _breakers.setdefault(source_id, Breaker())


def _auth(scheme):
    """Request kwargs for an upstream auth scheme. Values come from env only."""
    if scheme == "geotorget":
        token = os.environ.get("GEOTORGET_TOKEN")
        if token:
            return {"Authorization": "Bearer " + token}
        user = os.environ.get("GEOTORGET_USERNAME")
        password = os.environ.get("GEOTORGET_PASSWORD")
        if user and password:
            pair = "{0}:{1}".format(user, password).encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
        raise RuntimeError("geotorget credentials are not configured")
    return {}


class UpstreamUnavailable(Exception):
    pass


def _fetch_upstream(source_id, upstream, z, x, y):
    """One upstream call, behind the breaker and the rate limit.

    Returns bytes, or None for "the provider has no tile here".
    """
    breaker = _breaker_for(source_id)
    if not breaker.allow():
        EXT_UPSTREAM.labels(source_id, "breaker_open").inc()
        raise UpstreamUnavailable("circuit open")
    if not _bucket_for(source_id, upstream).take(TOKEN_WAIT_SECONDS):
        EXT_UPSTREAM.labels(source_id, "throttled").inc()
        raise UpstreamUnavailable("rate limited")

    url = upstream["url"].replace("{z}", str(z)).replace("{x}", str(x)).replace(
        "{y}", str(y))
    headers = {"User-Agent": USER_AGENT}
    try:
        headers.update(_auth(upstream.get("auth", "none")))
    except RuntimeError as error:
        EXT_UPSTREAM.labels(source_id, "error").inc()
        print("[ext] {0}: {1}".format(source_id, error))
        raise UpstreamUnavailable(str(error))
    started = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as error:
        EXT_UPSTREAM.labels(source_id, "error").inc()
        if breaker.failure():
            print("[ext] {0}: circuit opened after repeated errors".format(source_id))
        # The exception text can include the URL; the URL never carries a
        # credential (geodata refuses that), but log the class only anyway.
        print("[ext] {0} {1}/{2}/{3}: upstream error {4}".format(
            source_id, z, x, y, type(error).__name__))
        raise UpstreamUnavailable("upstream error")
    finally:
        EXT_UPSTREAM_LATENCY.labels(source_id).observe(time.monotonic() - started)

    # One line per upstream call: together with the counters this is the
    # evidence that repeated views cost the provider nothing.
    print("[ext] upstream {0} {1}/{2}/{3} -> {4}".format(
        source_id, z, x, y, response.status_code))
    if response.status_code in (204, 404):
        breaker.success()
        EXT_UPSTREAM.labels(source_id, "empty").inc()
        return None
    if response.status_code >= 500 or response.status_code in (401, 403, 429):
        EXT_UPSTREAM.labels(source_id, "error").inc()
        if breaker.failure():
            print("[ext] {0}: circuit opened after repeated errors".format(source_id))
        raise UpstreamUnavailable("upstream status {0}".format(response.status_code))
    if response.status_code != 200 or not response.content:
        EXT_UPSTREAM.labels(source_id, "error").inc()
        raise UpstreamUnavailable("upstream status {0}".format(response.status_code))
    breaker.success()
    EXT_UPSTREAM.labels(source_id, "ok").inc()
    return response.content


def _coalesced(key, fn):
    """Run fn once for concurrent callers asking for the same key."""
    with _state_lock:
        entry = _inflight.get(key)
        leader = entry is None
        if leader:
            entry = _inflight[key] = {"event": threading.Event(), "result": None,
                                      "error": None}
    if not leader:
        entry["event"].wait(UPSTREAM_TIMEOUT + TOKEN_WAIT_SECONDS + 5)
        if entry["error"] is not None:
            raise entry["error"]
        return entry["result"]
    try:
        entry["result"] = fn()
        return entry["result"]
    except Exception as error:
        entry["error"] = error
        raise
    finally:
        entry["event"].set()
        with _state_lock:
            _inflight.pop(key, None)


# --- cache -----------------------------------------------------------------

def _cache_key(source_id, version, z, x, y, fmt):
    return "{0}/ext/{1}/v{2}/{3}/{4}/{5}.{6}".format(
        TILES_PREFIX, source_id, version, z, x, y, fmt)


def _store(source_id, key, data, content_type):
    try:
        minioBackend.client.put_object(
            TILE_CACHE_BUCKET, key, io.BytesIO(data), len(data),
            content_type=content_type,
            metadata={"fetched-at": str(int(time.time()))})
        EXT_BYTES_STORED.labels(source_id).inc(len(data))
    except Exception as error:
        print("[ext] cache write failed for {0}: {1}".format(key, type(error).__name__))


def _fetch_and_store(source_id, upstream, key, z, x, y, content_type):
    data = _fetch_upstream(source_id, upstream, z, x, y)
    # "No tile here" is cached too, as an empty object: otherwise every view of
    # the edge of the coverage area would go upstream again.
    _store(source_id, key, data or b"", content_type)
    return data


def _refresh_in_background(source_id, upstream, key, z, x, y, content_type):
    def run():
        try:
            _coalesced(key, lambda: _fetch_and_store(
                source_id, upstream, key, z, x, y, content_type))
        except Exception:
            pass  # the stale tile stays; the next request tries again
    threading.Thread(target=run, daemon=True).start()


def _tile_response(data, content_type, ttl_seconds, status_header):
    etag = '"{0}"'.format(hashlib.sha1(data).hexdigest()[:20])
    if request.headers.get("If-None-Match") == etag:
        response = Response(status=304)
    else:
        response = make_response(data)
        response.headers.set("Content-Type", content_type)
    response.headers.set("ETag", etag)
    # Browsers/CDN keep a tile a day and may serve it a week past that while
    # they revalidate; our own TTL (days to months) is enforced server side.
    response.headers.set(
        "Cache-Control",
        "public, max-age={0}, stale-while-revalidate=604800".format(
            min(86400, ttl_seconds)))
    response.headers.set("X-Ext-Cache", status_header)
    return response


def tile(source_id, z, x, y, fmt):
    sources = servable_sources()
    row = sources.get(source_id)
    if row is None:
        EXT_REQUESTS.labels("unknown" if len(source_id) > 64 else source_id,
                            "refused").inc()
        return jsonify({"error": "unknown or not servable source"}), 404

    upstream = row["upstream"]
    if fmt != upstream.get("format", "png"):
        return jsonify({"error": "wrong tile format"}), 404
    if z < row.get("minzoom", 0) or z > row.get("maxzoom", 22) or not (
            0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return Response(status=204)

    content_type = CONTENT_TYPES.get(fmt, "application/octet-stream")
    ttl_seconds = int(upstream.get("ttlDays", 30)) * 86400
    key = _cache_key(source_id, row.get("version", 1), z, x, y, fmt)

    data, headers = _read_object(key)
    if data is not None:
        fetched = int((headers or {}).get(FETCHED_AT_META, "0") or 0)
        if fetched and time.time() - fetched > ttl_seconds:
            EXT_REQUESTS.labels(source_id, "stale").inc()
            _refresh_in_background(source_id, upstream, key, z, x, y, content_type)
            result = "stale"
        else:
            EXT_REQUESTS.labels(source_id, "hit").inc()
            result = "hit"
        if not data:
            return Response(status=204, headers={"X-Ext-Cache": result})
        return _tile_response(data, content_type, ttl_seconds, result)

    EXT_REQUESTS.labels(source_id, "miss").inc()
    try:
        data = _coalesced(key, lambda: _fetch_and_store(
            source_id, upstream, key, z, x, y, content_type))
    except UpstreamUnavailable:
        EXT_REQUESTS.labels(source_id, "error").inc()
        response = jsonify({"error": "tile temporarily unavailable"})
        response.status_code = 503
        response.headers.set("Retry-After", "30")
        return response
    if not data:
        return Response(status=204)
    return _tile_response(data, content_type, ttl_seconds, "miss")
