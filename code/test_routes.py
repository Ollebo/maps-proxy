"""Route-precedence checks. Run inside the image: python3 /code/test_routes.py

The tile routes were added in front of a catch-all that had owned every path.
Werkzeug orders rules by its own weighting rather than by registration order, so
"the specific rule wins" is an assumption worth pinning down: get it wrong and
/models/<space>/... stops being auth-gated.
"""
import os
import sys
import types


# app is imported for its url_map only. Two things happen at import time that
# this test has no business doing: minioBackend builds an S3 client (so it needs
# an endpoint), and auth fetches the Keycloak discovery document over the
# network. The first is given dummy values, the second is stubbed out.
os.environ.setdefault("S3_ENDPOINT", "localhost:9000")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_S3_FILE_BUCKET", "ollebo-maps")

_auth_stub = types.ModuleType("auth")
_auth_stub.check_space_access = lambda request, space_id: None
sys.modules.setdefault("auth", _auth_stub)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402


CASES = [
    # path                                   expected view function
    ("/sweden/8/136/74.pbf",                 "vector_tile"),
    ("/nvv-skyddad-natur/6/35/18.pbf",       "vector_tile"),
    ("/sweden/tile.json",                    "tile_json"),
    ("/sweden/style.json",                   "style_json"),
    ("/proxy/pcn/ogc",                       "proxy_upstream"),
    # The catch-all must keep everything it had. The auth-gated prefixes are the
    # ones that actually matter.
    ("/models/0000-0000/model.obj",          "maps"),
    ("/private/0000-0000/cogs/a/b.png",      "maps"),
    ("/tiles/index.json",                    "maps"),
    ("/master/cogs/space/map/rgb__r.tif",    "maps"),
    # Not a tile: three path segments before the extension.
    ("/a/b/c/d/8/136/74.pbf",                "maps"),
    # Not a tile: non-integer z/x/y.
    ("/sweden/a/b/c.pbf",                    "maps"),
]


def main():
    adapter = app.app.url_map.bind("maps.ollebo.com")
    failures = []
    for path, expected in CASES:
        endpoint, _ = adapter.match(path, method="GET")
        status = "ok " if endpoint == expected else "FAIL"
        if endpoint != expected:
            failures.append((path, expected, endpoint))
        print("{0} {1:40} -> {2}".format(status, path, endpoint))

    if failures:
        print("\n{0} route(s) matched the wrong view:".format(len(failures)))
        for path, expected, actual in failures:
            print("  {0}: expected {1}, got {2}".format(path, expected, actual))
        return 1
    print("\nall {0} routes match as intended".format(len(CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
