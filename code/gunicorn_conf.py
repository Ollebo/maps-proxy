# Gunicorn runs several worker processes. prometheus_client multiprocess mode
# aggregates them from files in PROMETHEUS_MULTIPROC_DIR (set by start.sh);
# without it every scrape only sees whichever worker answered.
import os

from prometheus_flask_exporter.multiprocess import GunicornInternalPrometheusMetrics


def child_exit(server, worker):
    GunicornInternalPrometheusMetrics.mark_process_dead_on_child_exit(worker.pid)
    # The worker's per-map table (map_metrics.py); /stats/maps merges these.
    try:
        os.remove(os.path.join(
            os.environ.get("MAP_STATS_DIR", "/tmp/map-stats"), "{0}.json".format(worker.pid)))
    except OSError:
        pass
