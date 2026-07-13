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
_ALLOWED_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)*ollebo\.com$")


def _allowed_origin():
    origin = request.headers.get("Origin", "")
    return origin if origin and _ALLOWED_ORIGIN.match(origin) else None


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

