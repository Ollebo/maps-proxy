from minio import Minio
from flask import make_response, request, jsonify
import os
import os.path

import auth


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


def getFile(filename):
    print(filename)
    if filename == "" or filename == "index.html":
        response = make_response(INDEX_HTML)
        response.headers.set("Content-Type", "text/html")
        return response

    bucket, key = resolveBucket(filename)

    # spridningstillstand.md 5.3/5.6: the anonymous catch-all never serves
    # COGs or detections outside the coarse public/ prefix, nor the legacy
    # <realm>/cogs/... layout (pre-compliance public maps were too fine).
    if bucket == S3_FILE_BUCKET and _blocked_public_key(key):
        return jsonify({"error": "not found"}), 404

    if filename.startswith("private/") or filename.startswith("models/"):
        space_id = _space_of(key)
        if not space_id:
            return jsonify({"error": "missing space id"}), 400
        err = auth.check_space_access(request, space_id)
        if err is not None:
            status, body = err
            return jsonify(body), status

    WeHaveFile = False
    fileIsDownloaded = False
    FILE_DEST = FILE_PREFIX + bucket + "/" + key
    if os.path.isfile(FILE_DEST):
        fileIsDownloaded = True
        WeHaveFile = True
        print("We have a local copy of the file" )


    if not fileIsDownloaded:
        try:
            os.makedirs(os.path.dirname(FILE_DEST), exist_ok=True)
            #Get the file from the bucket
            client.fget_object(bucket, key, FILE_DEST)
            WeHaveFile = True
            print("file downloaded")
        except:
            print(FILE_DEST)
            # A bare string is a 200 in Flask, which made every miss look like a
            # successful fetch of the body "File not found" -- fine for an <img>,
            # fatal for anything that parses the response (MVT, GeoJSON).
            return jsonify({"error": "not found"}), 404
    #
    ##Read the file
    if WeHaveFile:
        f = open(FILE_DEST, "rb")
        theFile = f.read()
        f.close()
        #set the content type
        response = make_response(theFile)

        #set the content type
        filename, file_extension = os.path.splitext(FILE_DEST)
        content_type = CONTENT_TYPES.get(file_extension.lower())
        if content_type:
            response.headers.set('Content-Type', content_type)
        return response

    else:
        return jsonify({"error": "not found"}), 404
