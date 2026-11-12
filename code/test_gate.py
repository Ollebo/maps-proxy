"""Gate + catch-all checks for spridningstillstand.md. Run inside the image:
    python3 /code/test_gate.py
"""
import os
import sys
import tempfile
import types

os.environ.setdefault("S3_ENDPOINT", "localhost:9000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_S3_FILE_BUCKET", "ollebo-maps")
os.environ["PURGE_API_KEY"] = "secret-key"

MEMBER = "11111111-1111-1111-1111-111111111111"


def _check(request, space_id):
    token = request.headers.get("Authorization", "")
    if not token:
        return 401, {"error": "unauthenticated"}
    if space_id != MEMBER:
        return 403, {"error": "forbidden"}
    return None


_auth_stub = types.ModuleType("auth")
_auth_stub.check_space_access = _check
sys.modules["auth"] = _auth_stub

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import gate  # noqa: E402
import utils  # noqa: E402


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


# Fake terracotta-public metadata: one dataset near Stockholm (maxzoom 17),
# one near Kiruna (maxzoom 16).
_BOUNDS = {
    ("S", "M", "rgb__r"): [18.0, 59.30, 18.01, 59.305],
    ("S", "K", "grayscale"): [20.2, 67.85, 20.21, 67.855],
}


def _fake_get(url, timeout=None):
    parts = url.rstrip("/").split("/")
    key = tuple(parts[-3:])
    if key in _BOUNDS:
        return _Resp(200, {"bounds": _BOUNDS[key]})
    return _Resp(404)


gate.requests.get = _fake_get

client = app.app.test_client()
failures = []


def expect(label, resp, status):
    ok = resp.status_code == status
    print("{0} {1:55} -> {2} (want {3})".format("ok " if ok else "FAIL", label, resp.status_code, status))
    if not ok:
        failures.append(label)


def fwd(uri, **headers):
    h = {"X-Forwarded-Uri": uri}
    h.update(headers)
    return h


RGBQ = "?r=rgb__r&g=rgb__g&b=rgb__b&r_range=[0,255]&g_range=[0,255]&b_range=[0,255]"

# --- private tiles -------------------------------------------------------
expect("private tile, no token", client.get("/auth/tiles", headers=fwd(
    "/private-tiles/rgb/{0}/m/10/1/1.png".format(MEMBER))), 401)
expect("private tile, member", client.get("/auth/tiles", headers=fwd(
    "/private-tiles/rgb/{0}/m/10/1/1.png".format(MEMBER), Authorization="Bearer x")), 200)
expect("private tile, non-member", client.get("/auth/tiles", headers=fwd(
    "/private-tiles/singleband/22222222-2222-2222-2222-222222222222/m/v/1/1/1.png",
    Authorization="Bearer x")), 403)
expect("private datasets listing", client.get("/auth/tiles", headers=fwd(
    "/private-tiles/datasets", Authorization="Bearer x")), 404)

# --- public tiles --------------------------------------------------------
expect("public rgb at maxzoom 17 (Stockholm)", client.get("/auth/public-tile", headers=fwd(
    "/public/rgb/S/M/17/1/1.png" + RGBQ)), 200)
expect("public rgb above maxzoom", client.get("/auth/public-tile", headers=fwd(
    "/public/rgb/S/M/18/1/1.png" + RGBQ)), 404)
expect("public singleband at 16 (Kiruna)", client.get("/auth/public-tile", headers=fwd(
    "/public/singleband/S/K/grayscale/16/1/1.png?colormap=greys_r&stretch_range=[0,255]")), 200)
expect("public singleband at 17 (Kiruna)", client.get("/auth/public-tile", headers=fwd(
    "/public/singleband/S/K/grayscale/17/1/1.png")), 404)
expect("public tile_size param refused", client.get("/auth/public-tile", headers=fwd(
    "/public/rgb/S/M/10/1/1.png" + RGBQ + "&tile_size=[512,512]")), 400)
expect("public unknown dataset", client.get("/auth/public-tile", headers=fwd(
    "/public/rgb/S/NOPE/10/1/1.png" + RGBQ)), 404)
expect("public metadata endpoint", client.get("/auth/public-tile", headers=fwd(
    "/public/metadata/S/M/rgb__r")), 404)

# --- catch-all ----------------------------------------------------------
for path in (
    "/master/cogs/S/M/rgb__r.tif",
    "/master/cogs/S/M/detections.json",
    "/whatever/file.tif",
    "/rgb/S/M/1/1/1.png",
    "/datasets",
    "/metadata/S/M/rgb__r",
):
    expect("catch-all blocks " + path, client.get(path), 404)

# The S3 endpoint is fake, so a miss is also a 404: check the rule itself too.
import minioBackend  # noqa: E402
for key, blocked in (
    ("master/cogs/S/M/rgb__r.tif", True),
    ("x/detections.json", True),
    ("whatever/file.tif", True),
    ("datasets", True),
    ("public/master/S/M/rgb__r.tif", False),
    ("layers/S/L/layer.geojson", False),
    ("tiles/sweden/v1/8/1/1.pbf", False),
):
    if minioBackend._blocked_public_key(key) != blocked:
        print("FAIL _blocked_public_key({0}) != {1}".format(key, blocked))
        failures.append(key)

for key, space in (
    ("master/cogs/{0}/m/detections.json".format(MEMBER), MEMBER),
    ("{0}/layers/l/layer.geojson".format(MEMBER), MEMBER),
    ("{0}/model/file.glb".format(MEMBER), MEMBER),
):
    if minioBackend._space_of(key) != space:
        print("FAIL _space_of({0})".format(key))
        failures.append(key)

# --- cache ---------------------------------------------------------------
expect("cache clear without key", client.get("/cache/clear/"), 403)
expect("cache purge without key", client.post("/cache/purge", json={"prefix": "public/x/"}), 403)

tmp = tempfile.mkdtemp()
utils.FILE_CACHE = tmp + "/"
os.makedirs(os.path.join(tmp, "ollebo-maps", "public", "master", "S", "M"))
open(os.path.join(tmp, "ollebo-maps", "public", "master", "S", "M", "rgb__r.tif"), "w").close()
gate._dataset_maxzoom("S", "M", "rgb__r")
r = client.post("/cache/purge", headers={"X-Api-Key": "secret-key"},
                json={"prefix": "public/master/S/M/", "space": "S", "map": "M"})
expect("cache purge with key", r, 200)
if r.get_json().get("removed") != 1 or ("S", "M", "rgb__r") in gate._maxzoom_cache:
    print("FAIL purge did not remove cached file/maxzoom")
    failures.append("purge effect")
r = client.post("/cache/purge", headers={"X-Api-Key": "secret-key"}, json={"prefix": "../../etc/"})
if r.get_json().get("removed") != 0:
    failures.append("purge traversal")

if failures:
    print("\n{0} failure(s)".format(len(failures)))
    sys.exit(1)
print("\nall gate checks passed")
