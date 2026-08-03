import os
import re

from flask import Flask, make_response, request
from prometheus_flask_exporter import PrometheusMetrics


app = Flask(__name__)
metrics = PrometheusMetrics(app)

import k8s
import minioBackend
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


#backend
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def maps(path):
    return minioBackend.getFile(path)

