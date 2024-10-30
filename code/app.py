from flask import Flask, request
from prometheus_flask_exporter import PrometheusMetrics


app = Flask(__name__)
metrics = PrometheusMetrics(app)

import k8s
import minioBackend









#k8s routes
app.add_url_rule('/ready', view_func=k8s.ready)
app.add_url_rule('/healthz', view_func=k8s.healthz)


#backend
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def maps(path):
    return minioBackend.getFile(path)

