from minio import Minio
from minio.error import S3Error
from flask import make_response, request, jsonify
import errno
import os
import os.path
import time

import urllib3

import auth
import map_metrics


S3_FILE_BUCKET = os.environ.get('AWS_S3_FILE_BUCKET')
S3_PRIVATE_BUCKET = os.environ.get('AWS_S3_PRIVATE_BUCKET')
S3_MODELS_BUCKET = os.environ.get('AWS_S3_MODELS_BUCKET')
S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT')
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')


# The prod endpoint is a bare host ("hel1.your-objectstorage.com") and TLS, which
# is what Minio() assumes. A local MinIO is plain HTTP, and pointing this at one
# used to fail as an SSLError swallowed into "file not found" -- so an explicit
# scheme on S3_ENDPOINT is honoured, and no scheme still means HTTPS.
_secure = not S3_ENDPOINT_URL.startswith("http://")
_endpoint = S3_ENDPOINT_URL.split("://", 1)[-1]

client = Minio(_endpoint,
    access_key=AWS_ACCESS_KEY_ID,
    secret_key=AWS_SECRET_ACCESS_KEY,
    secure=_secure,
)


FILE_PREFIX = "/file_cache/"


def resolveBucket(path):
    if path.startswith("private/"):
        return S3_PRIVATE_BUCKET, path[len("private/"):]
    if path.startswith("models/"):
        return S3_MODELS_BUCKET, path[len("models/"):]
    return S3_FILE_BUCKET, path


INDEX_HTML = (
    "<!doctype html>"
    "<html><head><meta charset=\"utf-8\">"
    "<title>maps.ollebo.com map server</title></head>"
    "<body><h1>maps.ollebo.com map server</h1></body></html>"
)


# Extension -> Content-Type. A file served with no type is guessed by the
# browser, which is how .geojson used to arrive as HTML and fail to parse in the
# viewer. dw writes layer data as .geojson; the vector-tile types are here so the
# same route works when layers grow past plain GeoJSON.
CONTENT_TYPES = {
    ".css": "text/css",
    ".csv": "text/csv",
    ".geojson": "application/geo+json",
    ".gif": "image/gif",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mvt": "application/vnd.mapbox-vector-tile",
    ".pbf": "application/vnd.mapbox-vector-tile",
    ".png": "image/png",
    ".pmtiles": "application/vnd.pmtiles",
}


_TERRACOTTA_PREFIXES = ("rgb/", "singleband/", "metadata/", "keys", "colormap",
                        "datasets", "compute/")


def _blocked_public_key(key):
    lower = key.lower()
    if lower.startswith(_TERRACOTTA_PREFIXES):
        return True
    base = lower.rsplit("/", 1)[-1]
    if base == "detections.json" or base.endswith(".geojson") and "detect" in base:
        return True
    if lower.endswith((".tif", ".tiff")) and not lower.startswith("public/"):
        return True
    if "/cogs/" in "/" + lower:
        return True
    return False


def _space_of(key):
    """Space id that guards a private/ or models/ key.

    Layers and models put the space first (<space>/...); map outputs from
    map-maker/geo-detect use <realm>/cogs/<space>/<map>/..., where the first
    segment is the Keycloak realm. Reading the realm as the space 403'd every
    member, so the cogs layout is recognised explicitly.
    """
    parts = (key or "").split("/")
    if len(parts) >= 3 and parts[1] == "cogs":
        return parts[2]
    return parts[0] if parts else ""


def _kind_of(filename):
    if filename.startswith("private/"):
        return "private"
    if filename.startswith("models/"):
        return "model"
    return "public"


def _map_of(kind, key):
    """(space, map/model id) a key belongs to, or (None, None).

    Only for /stats/maps and logs -- these come straight from the path and are
    never used as a Prometheus label.
      private  <realm>/cogs/<space>/<map>/<file>
      public   public/<realm>/<space>/<map>/<file>
      model    <space>/<model>/<file>
    """
    parts = (key or "").split("/")
    if kind == "private" and len(parts) >= 5 and parts[1] == "cogs":
        return parts[2], parts[3]
    if kind == "public" and len(parts) >= 5 and parts[0] == "public":
        return parts[2], parts[3]
    if kind == "model" and len(parts) >= 3:
        return parts[0], parts[1]
    return None, None


# OSErrors that mean "this path cannot name an object" (a key that runs through
# an already cached file, a key that is a cached directory, an absurd name).
# Anything else from the filesystem -- a full cache volume above all -- is ours.
_PATH_ERRNOS = frozenset([
    errno.ENOTDIR, errno.EISDIR, errno.ENAMETOOLONG, errno.ENOENT, errno.EINVAL])


def _classify_fetch_error(error):
    """not_found | s3_error | cache_error for a failed download.

    Everything used to be a 404, so an object-storage outage looked exactly
    like a missing file. Only a failure we can positively pin on storage or on
    the cache volume is reported as one; anything unrecognised stays not_found.
    """
    if isinstance(error, S3Error):
        return "not_found" if error.code in ("NoSuchKey", "NoSuchBucket") else "s3_error"
    if isinstance(error, urllib3.exceptions.HTTPError):
        return "s3_error"
    if isinstance(error, OSError):
        return "not_found" if error.errno in _PATH_ERRNOS else "cache_error"
    return "not_found"


def getFile(filename):
    print(filename)
    if filename == "" or filename == "index.html":
        response = make_response(INDEX_HTML)
        response.headers.set("Content-Type", "text/html")
        return response

    bucket, key = resolveBucket(filename)
    kind = _kind_of(filename)
    map_space, map_id = _map_of(kind, key)

    def count(result, space=None):
        # `space` is passed only once the request has proved it exists.
        map_metrics.OBJECT_REQUESTS.labels(
            kind, map_metrics.space_label(space), result).inc()

    # spridningstillstand.md 5.3/5.6: the anonymous catch-all never serves
    # COGs or detections outside the coarse public/ prefix, nor the legacy
    # <realm>/cogs/... layout (pre-compliance public maps were too fine).
    if bucket == S3_FILE_BUCKET and _blocked_public_key(key):
        count("blocked")
        return jsonify({"error": "not found"}), 404

    # Membership has been checked for private/ and models/, so that space is
    # real. A public key's space is only trusted once the object turns up.
    known_space = None
    if kind != "public":
        space_id = _space_of(key)
        if not space_id:
            count("denied")
            return jsonify({"error": "missing space id"}), 400
        err = auth.check_space_access(request, space_id)
        if err is not None:
            status, body = err
            count("denied")
            return jsonify(body), status
        known_space = space_id

    FILE_DEST = FILE_PREFIX + bucket + "/" + key
    result = "hit"
    if not os.path.isfile(FILE_DEST):
        result = "miss"
        started = time.time()
        try:
            os.makedirs(os.path.dirname(FILE_DEST), exist_ok=True)
            #Get the file from the bucket
            client.fget_object(bucket, key, FILE_DEST)
        except Exception as error:
            failure = _classify_fetch_error(error)
            count(failure, known_space)
            if map_id:
                map_metrics.record_map(map_space, map_id, kind + "_object", failure, error=True)
            if failure == "not_found":
                # A bare string is a 200 in Flask, which made every miss look like a
                # successful fetch of the body "File not found" -- fine for an <img>,
                # fatal for anything that parses the response (MVT, GeoJSON).
                return jsonify({"error": "not found"}), 404
            print("ERROR fetching {0}/{1}: {2!r}".format(bucket, key, error))
            if not map_id:
                map_metrics.log_error(kind + "_object", failure, key=key, error=repr(error))
            return jsonify({"error": "storage unavailable"}), 502
        map_metrics.OBJECT_FETCH_LATENCY.labels(kind).observe(time.time() - started)

    with open(FILE_DEST, "rb") as f:
        theFile = f.read()

    count(result, known_space or map_space)
    map_metrics.OBJECT_BYTES.labels(kind, result).inc(len(theFile))
    if map_id:
        map_metrics.record_map(map_space, map_id, kind + "_object", result)

    response = make_response(theFile)
    _, file_extension = os.path.splitext(FILE_DEST)
    content_type = CONTENT_TYPES.get(file_extension.lower())
    if content_type:
        response.headers.set('Content-Type', content_type)
    return response
