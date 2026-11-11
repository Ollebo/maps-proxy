"""Behaviour of the /ext cached proxy. Run: python3 -m pytest code/test_ext.py

S3 and the upstream provider are replaced by in-memory fakes, so these tests
count upstream calls exactly -- the acceptance criterion is "a repeated view
costs the provider nothing", and that is a number, not a feeling.
"""
import io
import json
import os
import sys
import threading
import time
import types

import pytest

os.environ.setdefault("S3_ENDPOINT", "localhost:9000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_S3_FILE_BUCKET", "ollebo-maps")
os.environ["GUNICORN_WORKERS"] = "1"
os.environ["EXT_TOKEN_WAIT"] = "0.2"

_auth_stub = types.ModuleType("auth")
_auth_stub.check_space_access = lambda request, space_id: None
sys.modules.setdefault("auth", _auth_stub)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module  # noqa: E402
import ext  # noqa: E402


class FakeObject:
    def __init__(self, data, headers):
        self._data = data
        self.headers = headers

    def read(self):
        return self._data

    def close(self):
        pass

    def release_conn(self):
        pass


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError(key)
        data, meta = self.objects[(bucket, key)]
        headers = {"X-Amz-Meta-" + k.title(): v for k, v in meta.items()}
        return FakeObject(data, headers)

    def put_object(self, bucket, key, stream, length, content_type=None, metadata=None):
        self.objects[(bucket, key)] = (stream.read(), dict(metadata or {}))


class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


class FakeUpstream:
    def __init__(self, status=200, content=b"PNGDATA", delay=0.0):
        self.calls = []
        self.status = status
        self.content = content
        self.delay = delay
        self.lock = threading.Lock()

    def get(self, url, headers=None, timeout=None):
        with self.lock:
            self.calls.append((url, dict(headers or {})))
        if self.delay:
            time.sleep(self.delay)
        return FakeResponse(self.status, self.content if self.status == 200 else b"")


def _row(source_id, status="enabled", commercial_ok=True, **upstream):
    up = {"url": "https://upstream.invalid/{z}/{y}/{x}.png", "auth": "none",
          "rateLimit": 50, "burst": 50, "ttlDays": 30, "format": "png"}
    up.update(upstream)
    return {"id": source_id, "kind": "proxy", "mode": "proxy", "status": status,
            "commercial_ok": commercial_ok, "version": 1, "minzoom": 0,
            "maxzoom": 18, "upstream": up}


@pytest.fixture
def env(monkeypatch):
    s3 = FakeS3()
    upstream = FakeUpstream()
    monkeypatch.setattr(ext.minioBackend, "client", s3)
    monkeypatch.setattr(ext.requests, "get", upstream.get)
    for store in (ext._buckets, ext._breakers, ext._inflight):
        store.clear()
    ext._index.update({"expires": 0.0, "sources": {}})

    def publish(*rows):
        s3.objects[(ext.TILE_CACHE_BUCKET, "tiles/index.json")] = (
            json.dumps(list(rows)).encode(), {})
        ext._index.update({"expires": 0.0})

    client = app_module.app.test_client()
    return types.SimpleNamespace(s3=s3, upstream=upstream, publish=publish,
                                 client=client)


def test_repeat_view_does_not_reach_upstream(env):
    env.publish(_row("topo"))
    first = env.client.get("/ext/topo/5/17/9.png")
    second = env.client.get("/ext/topo/5/17/9.png")
    assert first.status_code == 200 and first.headers["X-Ext-Cache"] == "miss"
    assert second.status_code == 200 and second.headers["X-Ext-Cache"] == "hit"
    assert second.data == b"PNGDATA"
    assert len(env.upstream.calls) == 1
    assert env.upstream.calls[0][0] == "https://upstream.invalid/5/9/17.png"


@pytest.mark.parametrize("row", [
    _row("topo", status="blocked"),
    _row("topo", status="disabled"),
    _row("topo", commercial_ok=False),
])
def test_not_servable_source_is_404_even_with_a_warm_cache(env, row):
    env.publish(row)
    key = ext._cache_key("topo", 1, 5, 17, 9, "png")
    env.s3.objects[(ext.TILE_CACHE_BUCKET, key)] = (b"OLD", {"fetched-at": str(int(time.time()))})
    response = env.client.get("/ext/topo/5/17/9.png")
    assert response.status_code == 404
    assert env.upstream.calls == []


def test_unknown_source_is_404(env):
    env.publish(_row("topo"))
    assert env.client.get("/ext/other/5/17/9.png").status_code == 404
    assert env.upstream.calls == []


def test_etag_revalidation(env):
    env.publish(_row("topo"))
    etag = env.client.get("/ext/topo/5/17/9.png").headers["ETag"]
    again = env.client.get("/ext/topo/5/17/9.png", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_concurrent_misses_are_coalesced(env):
    env.publish(_row("topo"))
    env.upstream.delay = 0.3
    results = []

    def hit():
        with app_module.app.test_client() as c:
            results.append(c.get("/ext/topo/6/34/18.png").status_code)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 8
    assert len(env.upstream.calls) == 1


def test_empty_upstream_tile_is_cached(env):
    env.publish(_row("topo"))
    env.upstream.status = 404
    assert env.client.get("/ext/topo/5/1/1.png").status_code == 204
    assert env.client.get("/ext/topo/5/1/1.png").status_code == 204
    assert len(env.upstream.calls) == 1


def test_stale_tile_is_served_and_refreshed(env):
    env.publish(_row("topo", ttlDays=1))
    key = ext._cache_key("topo", 1, 5, 17, 9, "png")
    old = str(int(time.time()) - 3 * 86400)
    env.s3.objects[(ext.TILE_CACHE_BUCKET, key)] = (b"OLD", {"fetched-at": old})
    response = env.client.get("/ext/topo/5/17/9.png")
    assert response.data == b"OLD" and response.headers["X-Ext-Cache"] == "stale"
    for _ in range(50):
        if env.s3.objects[(ext.TILE_CACHE_BUCKET, key)][0] == b"PNGDATA":
            break
        time.sleep(0.02)
    assert env.s3.objects[(ext.TILE_CACHE_BUCKET, key)][0] == b"PNGDATA"


def test_breaker_stops_upstream_calls(env):
    env.publish(_row("topo"))
    env.upstream.status = 500
    for i in range(ext.BREAKER_THRESHOLD):
        assert env.client.get("/ext/topo/5/{0}/9.png".format(i)).status_code == 503
    calls = len(env.upstream.calls)
    assert env.client.get("/ext/topo/5/30/9.png").status_code == 503
    assert len(env.upstream.calls) == calls


def test_rate_limit_refuses_instead_of_queueing_forever(env):
    env.publish(_row("topo", rateLimit=1, burst=1))
    codes = [env.client.get("/ext/topo/8/{0}/9.png".format(i)).status_code
             for i in range(3)]
    assert codes[0] == 200
    assert 503 in codes
    assert len(env.upstream.calls) < 3


def test_geotorget_credentials_go_in_a_header_not_the_url(env, monkeypatch):
    monkeypatch.setenv("GEOTORGET_TOKEN", "s3cret")
    env.publish(_row("topo", auth="geotorget"))
    env.client.get("/ext/topo/5/17/9.png")
    url, headers = env.upstream.calls[0]
    assert "s3cret" not in url
    assert headers["Authorization"] == "Bearer s3cret"


def test_geotorget_without_credentials_is_503_not_a_crash(env, monkeypatch):
    monkeypatch.delenv("GEOTORGET_TOKEN", raising=False)
    monkeypatch.delenv("GEOTORGET_USERNAME", raising=False)
    env.publish(_row("topo", auth="geotorget"))
    assert env.client.get("/ext/topo/5/17/9.png").status_code == 503
