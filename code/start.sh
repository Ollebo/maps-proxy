#!/bin/bash
echo "Atring up the http server"


# One sync worker serialises every request behind a whole-object S3 download,
# so a single large model blocks all tiles. Threads, because the work is IO-bound.
# Prometheus multiprocess mode: every worker writes its metrics here and
# /metrics aggregates them. Must be empty at start.
export PROMETHEUS_MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus-multiproc}"
rm -rf "$PROMETHEUS_MULTIPROC_DIR" && mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
# Per-worker map tables that /stats/maps merges (map_metrics.py). Same rule.
export MAP_STATS_DIR="${MAP_STATS_DIR:-/tmp/map-stats}"
rm -rf "$MAP_STATS_DIR" && mkdir -p "$MAP_STATS_DIR"

gunicorn -c /code/gunicorn_conf.py -w "${GUNICORN_WORKERS:-4}" -k gthread --threads "${GUNICORN_THREADS:-8}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" -b 0.0.0.0:8080 'app:app'