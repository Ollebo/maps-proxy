import os
import re

from flask import Flask, make_response, request
from prometheus_flask_exporter import PrometheusMetrics


app = Flask(__name__)
metrics = PrometheusMetrics(app)

import k8s
import minioBackend
import tiles
import utils


# Private maps/models are fetched cross-origin from the app (dash.ollebo.com) to
# maps.ollebo.com and must carry the `access_token` cookie, so the browser needs
# CORS with credentials. Allow-Origin must echo the caller's origin (not "*")
# when credentials are allowed, so mirror back any ollebo.com origin.
_ALLOWED_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)*ollebo\.com$", re.IGNORECASE)

# The same app run from a dev server is http://localhost:<port> (or 127.0.0.1 --
# a different origin to the browser), which no ollebo.com pattern matches, so
# private maps/models/detections would fail CORS in local development. Match any
# loopback port; set CORS_ALLOW_LOOPBACK=0 to serve only the ollebo.com hosts.
_LOOPBACK_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", re.IGNORECASE)
_ALLOW_LOOPBACK = os.environ.get("CORS_ALLOW_LOOPBACK", "1") != "0"


def _allowed_origin():
    origin = request.headers.get("Origin", "")
    if not origin:
        return None
    if _ALLOWED_ORIGIN.match(origin):
        return origin
    if _ALLOW_LOOPBACK and _LOOPBACK_ORIGIN.match(origin):
        return origin
    return None


@app.before_request
def _cors_preflight():
    if request.method == "OPTIONS":
        resp = make_response("", 204)
        origin = _allowed_origin()
        if origin:
            resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = request.headers.get(
                "Access-Control-Request-Headers", "*"
            )
            resp.headers["Access-Control-Max-Age"] = "600"
        return resp
    return None


@app.after_request
def _cors_headers(resp):
    origin = _allowed_origin()
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    return resp









#k8s routes
app.add_url_rule('/ready', view_func=k8s.ready)
app.add_url_rule('/healthz', view_func=k8s.healthz)

#Clear cache
@app.route('/cache/clear/')
def cache():
    return utils.cleanCache()


# Maps we host ourselves: vector tiles rendered by martin from our own PostGIS,
# plus the TileJSON and style the viewer reads. These are declared before the
# catch-all so /<layer>/{z}/{x}/{y}.pbf is a tile rather than an object lookup;
# anything they do not recognise falls back to the catch-all, so no path that
# worked before stops working. Werkzeug sorts by specificity rather than
# registration order -- test_routes.py pins that down.
@app.route('/<layer>/<int:z>/<int:x>/<int:y>.pbf')
def vector_tile(layer, z, x, y):
    return tiles.tile(layer, z, x, y)


@app.route('/<layer>/tile.json')
def tile_json(layer):
    return tiles.metadata_document(layer, 'tile.json')


@app.route('/<layer>/style.json')
def style_json(layer):
    return tiles.metadata_document(layer, 'style.json')


# Upstreams we have to reach over plain HTTP on the browser's behalf; see
# tiles.PROXY_UPSTREAMS.
@app.route('/proxy/<name>/<path:path>')
def proxy_upstream(name, path):
    return tiles.proxy(name, path)


#backend
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def maps(path):
    return minioBackend.getFile(path)

