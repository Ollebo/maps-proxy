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


client = Minio(S3_ENDPOINT_URL,
    access_key=AWS_ACCESS_KEY_ID,
    secret_key=AWS_SECRET_ACCESS_KEY,
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


def getFile(filename):
    print(filename)
    if filename == "" or filename == "index.html":
        response = make_response(INDEX_HTML)
        response.headers.set("Content-Type", "text/html")
        return response

    bucket, key = resolveBucket(filename)

    if filename.startswith("private/") or filename.startswith("models/"):
        space_id = key.split("/", 1)[0] if key else ""
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
            return "File not found"
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
        return "File not found"
