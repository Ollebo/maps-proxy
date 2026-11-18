# maps-proxy

## Map metrics

Besides the default per-route HTTP metrics, `/metrics` reports which maps are
used, what is served from cache and what fails (`code/map_metrics.py`):

| Metric | What it counts |
|---|---|
| `maps_tile_requests_total{kind,layer,result}` | Vector tiles per layer: `hit`, `miss`, `empty`, `out_of_range`, `upstream_error` |
| `maps_tile_upstream_seconds{layer}` | martin render time on a cache miss |
| `maps_tile_cache_write_failures_total{layer}` | Rendered tiles that could not be stored |
| `maps_object_requests_total{kind,space,result}` | Map files, layers and models: `hit`, `miss`, `not_found`, `s3_error`, `cache_error`, `denied`, `blocked` |
| `maps_object_fetch_seconds{kind}`, `maps_object_bytes_total{kind,result}` | Download time on a miss, bytes served |
| `maps_gate_decisions_total{route,space,result}` | forwardAuth checks for terracotta raster tiles |
| `maps_gate_maxzoom_cache_total{result}` | Public maxzoom cache hit/miss |
| `maps_file_cache_bytes`, `maps_file_cache_files`, `maps_cache_purges_total{scope}` | Local `/file_cache` size and purges |

A map id is never a label. To see which map is failing, ask the pod (the
endpoint answers 404 through the ingress, like `/metrics`):

    kubectl exec -n ollebo deploy/maps-proxy -- python3 -c \
      "import urllib.request as u; print(u.urlopen('http://localhost:8080/stats/maps?sort=errors').read().decode())"

`sort` is `requests`, `errors` or `recent`; `top` limits the rows. Counts are
since the pod started, merged across the gunicorn workers. Every failure also
writes one JSON log line with `"event": "map_error"`.

The catch-all answers 502 when object storage or the cache volume fails, and
404 only when the object does not exist.

Dashboard: "Ollebo maps" in Grafana (`monitoring/dashboards-src/ollebo.py` in
the manifests repo).
