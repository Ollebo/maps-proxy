#!/bin/bash
echo "Atring up the http server"


# One sync worker serialises every request behind a whole-object S3 download,
# so a single large model blocks all tiles. Threads, because the work is IO-bound.
gunicorn -w "${GUNICORN_WORKERS:-4}" -k gthread --threads "${GUNICORN_THREADS:-8}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" -b 0.0.0.0:8080 'app:app'