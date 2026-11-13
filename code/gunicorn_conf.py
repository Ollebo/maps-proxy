# Gunicorn runs several worker processes. prometheus_client multiprocess mode
# aggregates them from files in PROMETHEUS_MULTIPROC_DIR (set by start.sh);
# without it every scrape only sees whichever worker answered.
from prometheus_flask_exporter.multiprocess import GunicornInternalPrometheusMetrics


def child_exit(server, worker):
    GunicornInternalPrometheusMetrics.mark_process_dead_on_child_exit(worker.pid)
