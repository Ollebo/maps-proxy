"""Which maps are used, what comes from cache, and what fails.

Two outputs, on purpose:

Prometheus series carry only bounded labels -- layer, kind, result and space.
A map id is NEVER a label: maps grow without bound and a series per map is how
a Prometheus dies (same rule as map-maker/code/metrics.py). A space is only
used as a label once the request has proved the space exists (membership check
passed, or the object/dataset was really there), so a scanner inventing paths
cannot mint series; `space_label` caps the set as a last resort.

Per-map detail lives in a small in-memory table that /stats/maps serves as
JSON, plus one JSON log line per failure. gunicorn runs several workers, so
each worker writes its table to MAP_STATS_DIR/<pid>.json and /stats/maps merges
the files -- otherwise the answer would depend on which worker took the call.
"""

import collections
import json
import os
import threading
import time

from prometheus_client import Counter, Gauge, Histogram


# --- prometheus ------------------------------------------------------------

TILE_REQUESTS = Counter(
    "maps_tile_requests_total", "Self-hosted vector tile requests by layer and cache result",
    ["kind", "layer", "result"])  # hit | miss | empty | out_of_range | upstream_error
TILE_UPSTREAM_LATENCY = Histogram(
    "maps_tile_upstream_seconds", "martin render latency on a tile cache miss", ["layer"])
TILE_CACHE_WRITE_FAILURES = Counter(
    "maps_tile_cache_write_failures_total", "Rendered tiles that could not be written to the cache",
    ["layer"])

OBJECT_REQUESTS = Counter(
    "maps_object_requests_total", "Catch-all object requests (map files, layers, models)",
    ["kind", "space", "result"])
# kind:   public | private | model
# result: hit | miss | not_found | s3_error | cache_error | denied | blocked
OBJECT_FETCH_LATENCY = Histogram(
    "maps_object_fetch_seconds", "S3 download latency on a file cache miss", ["kind"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
OBJECT_BYTES = Counter(
    "maps_object_bytes_total", "Bytes served by the catch-all", ["kind", "result"])

GATE_DECISIONS = Counter(
    "maps_gate_decisions_total", "forwardAuth decisions for terracotta raster tiles",
    ["route", "space", "result"])
# route:  public | private
# result: allow | deny | above_maxzoom | not_found | bad_request | lookup_error
GATE_MAXZOOM_CACHE = Counter(
    "maps_gate_maxzoom_cache_total", "Lookups in the public maxzoom cache", ["result"])

# Every worker walks the same directory, so the newest live value is the answer.
FILE_CACHE_BYTES = Gauge(
    "maps_file_cache_bytes", "Bytes held in the local object cache",
    multiprocess_mode="livemax")
FILE_CACHE_FILES = Gauge(
    "maps_file_cache_files", "Files held in the local object cache",
    multiprocess_mode="livemax")
CACHE_PURGES = Counter(
    "maps_cache_purges_total", "Local object cache purges", ["scope"])  # prefix | all


MAX_SPACE_LABELS = int(os.environ.get("MAP_METRICS_MAX_SPACES", "500"))
NO_SPACE = "-"
_space_labels = set()


def space_label(space):
    """Label value for a space the request has already proved to exist."""
    if not space:
        return NO_SPACE
    if space in _space_labels:
        return space
    if len(_space_labels) >= MAX_SPACE_LABELS:
        return "other"
    _space_labels.add(space)
    return space


# --- per-map table ---------------------------------------------------------

STATS_DIR = os.environ.get("MAP_STATS_DIR", "/tmp/map-stats")
MAX_MAPS = int(os.environ.get("MAP_STATS_MAX", "2000"))
DUMP_INTERVAL_S = float(os.environ.get("MAP_STATS_DUMP_INTERVAL", "15"))
FILE_CACHE_DIR = os.environ.get("FILE_CACHE_DIR", "/file_cache")
FILE_CACHE_SCAN_INTERVAL_S = float(os.environ.get("FILE_CACHE_SCAN_INTERVAL", "300"))

_lock = threading.Lock()
_maps = collections.OrderedDict()  # (space, map) -> entry, least recently seen first
_dirty = False
_worker_pid = None


def _log(event, **fields):
    fields["event"] = event
    print(json.dumps(fields, sort_keys=True), flush=True)


def record_map(space, map_id, kind, result, error=False):
    """Count one request against a map. `kind:result` is the breakdown key.

    Unlike the Prometheus labels this takes ids straight from the path, so a
    map that fails because it does not exist still shows up. The table is
    capped; the least recently seen map is dropped first.
    """
    global _dirty
    if not map_id:
        return
    key = (space or NO_SPACE, map_id)
    now = time.time()
    with _lock:
        entry = _maps.get(key)
        if entry is None:
            entry = {"space": key[0], "map": map_id, "requests": 0, "errors": 0,
                     "results": {}, "last_seen": now, "last_error": None,
                     "last_error_at": None}
            _maps[key] = entry
            while len(_maps) > MAX_MAPS:
                _maps.popitem(last=False)
        else:
            _maps.move_to_end(key)
        entry["requests"] += 1
        entry["last_seen"] = now
        bucket = "{0}:{1}".format(kind, result)
        entry["results"][bucket] = entry["results"].get(bucket, 0) + 1
        if error:
            entry["errors"] += 1
            entry["last_error"] = bucket
            entry["last_error_at"] = now
        _dirty = True
    if error:
        _log("map_error", space=key[0], map=map_id, kind=kind, result=result)


def log_error(kind, result, **fields):
    """A failure with no map id to hang it on (a vector layer, a stray key)."""
    _log("map_error", kind=kind, result=result, **fields)


def _stats_file(pid):
    return os.path.join(STATS_DIR, "{0}.json".format(pid))


def _dump():
    global _dirty
    with _lock:
        if not _dirty:
            return
        rows = list(_maps.values())
        payload = json.dumps(rows)
        _dirty = False
    try:
        os.makedirs(STATS_DIR, exist_ok=True)
        tmp = _stats_file(os.getpid()) + ".tmp"
        with open(tmp, "w") as handle:
            handle.write(payload)
        os.replace(tmp, _stats_file(os.getpid()))
    except OSError as error:
        print("map stats dump failed: {0}".format(error))


def _scan_file_cache():
    total = files = 0
    for root, _, names in os.walk(FILE_CACHE_DIR):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass  # purged while we were walking
    FILE_CACHE_BYTES.set(total)
    FILE_CACHE_FILES.set(files)


def _background():
    next_scan = 0
    while True:
        try:
            if time.time() >= next_scan:
                _scan_file_cache()
                next_scan = time.time() + FILE_CACHE_SCAN_INTERVAL_S
            _dump()
        except Exception as error:  # never let the reporter thread die
            print("map metrics background error: {0}".format(error))
        time.sleep(DUMP_INTERVAL_S)


def ensure_started():
    """Start the per-worker reporter thread. Called per request: gunicorn forks
    after import, and a thread started at import time would not survive it."""
    global _worker_pid
    pid = os.getpid()
    if _worker_pid == pid:
        return
    with _lock:
        if _worker_pid == pid:
            return
        _worker_pid = pid
    threading.Thread(target=_background, name="map-metrics", daemon=True).start()


def _merge(into, row):
    key = (row["space"], row["map"])
    have = into.get(key)
    if have is None:
        into[key] = dict(row, results=dict(row["results"]))
        return
    have["requests"] += row["requests"]
    have["errors"] += row["errors"]
    for bucket, count in row["results"].items():
        have["results"][bucket] = have["results"].get(bucket, 0) + count
    have["last_seen"] = max(have["last_seen"], row["last_seen"])
    if (row.get("last_error_at") or 0) > (have.get("last_error_at") or 0):
        have["last_error"] = row["last_error"]
        have["last_error_at"] = row["last_error_at"]


def snapshot(top=50, sort="requests"):
    """Per-map counts merged across every worker, since the pod started."""
    _dump()
    merged = {}
    workers = 0
    try:
        names = [n for n in os.listdir(STATS_DIR) if n.endswith(".json")]
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(STATS_DIR, name)) as handle:
                rows = json.load(handle)
        except (OSError, ValueError):
            continue
        workers += 1
        for row in rows:
            _merge(merged, row)
    if not names:
        # No stats directory (tests, read-only fs): this worker only.
        with _lock:
            for row in _maps.values():
                _merge(merged, row)
    rows = list(merged.values())
    if sort == "errors":
        rows = [r for r in rows if r["errors"]]
        rows.sort(key=lambda r: (r["errors"], r["last_error_at"] or 0), reverse=True)
    elif sort == "recent":
        rows.sort(key=lambda r: r["last_seen"], reverse=True)
    else:
        sort = "requests"
        rows.sort(key=lambda r: r["requests"], reverse=True)
    return {
        "sort": sort,
        "workers": workers,
        "tracked": len(merged),
        "max_tracked_per_worker": MAX_MAPS,
        "maps": rows[:max(0, top)],
    }
