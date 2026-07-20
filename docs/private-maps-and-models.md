# Private maps & models — how it works (runbook)

How an uploaded asset becomes a private, auth-gated 3D model / map tile set that
loads in the app, and how to repeat / debug it when adding new assets.

Spans repos: **dw** (create + auth cookie), **map-maker** (worker), **maps-proxy**
(serve + authorize), **ollebo-maps** (SPA viewer), **api** (same auth pattern),
and **Keycloak** (`master` realm).

## Hosts / origins

| Host | What |
|------|------|
| `dash.ollebo.com` | the SPA (`ollebo-maps`) |
| `www.ollebo.com` | dw (`dw-client` Keycloak session; issues the cookie) |
| `maps.ollebo.com` | tiles + model files (path-split: terracotta vs maps-proxy) |
| `api.ollebo.com` | the ollebo API (same Bearer/groups auth) |
| `auth.ollebo.com` | Keycloak, realm `master` |

## 1. The asset pipeline (upload → viewable)

1. **Upload** an asset in dw.
2. **Create model** in dw → inserts a `model` row (`status: pending`) and publishes
   a NATS `models` event `{modelID, spaceID, access, originFile, ...}`.
3. **map-maker** (`consume.py` → `models/makingModel.py`) consumes it, **copies** the
   file within bucket `map-storage` to the canonical key
   `"<space>/<modelid>/<file>"`, then PATCHes the API `→ status: ready`,
   `originFile = "models/<space>/<modelid>/<file>"`.
4. The **SPA** lists models from the dw API and builds the URL
   `MAPS_ORIGIN + "/" + originFile` = `https://maps.ollebo.com/models/<space>/<modelid>/<file>`.

## 2. Serving & routing (`maps.ollebo.com`)

The host is **path-split at the ingress** (see `charts/templates/ingress.yaml`):

- `/rgb /singleband /metadata /keys /colormap /datasets /compute` → **terracotta**
  (public raster tiles; `terracotta-cors` middleware = `Access-Control-Allow-Origin: *`).
- everything else (`/`, incl. `/models/*`, `/private/*`) → **maps-proxy**
  (a **separate ingress with NO CORS middleware** — maps-proxy does its own).

`maps-proxy` `resolveBucket()` maps the URL prefix to a bucket:

| URL prefix | Bucket (`AWS_S3_*`) | Auth? |
|------------|---------------------|-------|
| `models/<space>/…` | `AWS_S3_MODELS_BUCKET` (== `map-storage`) | **yes** |
| `private/<space>/…` | `AWS_S3_PRIVATE_BUCKET` | **yes** |
| anything else | `AWS_S3_FILE_BUCKET` (public) | no |

## 3. Authorization model (the important part)

A **space is a Keycloak group**. The critical invariants:

- **`space.id` (URL) == the Keycloak group's `id`** (set in `dw/src/helpers/provisionSpace.js`).
- **The Keycloak group's `name` MUST equal its `id`** (the space UUID). Keycloak's
  group-membership mapper emits group **names**, not ids — so if the name isn't the
  UUID, the space id never reaches the token. New groups are named this way
  automatically by `provisionSpace`; older ones were migrated (see §6).

The token (`dw-client`) carries a **`groups`** claim = the user's group names
(= the space UUIDs they belong to), via a **Group Membership** mapper on the
`Groups` and `microprofile-jwt` client scopes.

`maps-proxy` (`code/auth.py`) authorizes exactly like `api/code/jwt_auth.py`:
1. Extract the token from **`Authorization: Bearer`**, else the **`access_token` cookie**.
2. Validate against Keycloak JWKS (issuer `https://auth.ollebo.com/realms/master`, no audience check).
3. `space_id` = first path segment after `models/`|`private/`. Allow iff
   `space_id ∈ groups` (both canonicalized to lowercase UUIDs; non-UUID entries dropped).
4. Result: **401** = no/invalid token · **403** = valid token but not a space member · **200** = ok.

## 4. Getting the token to the browser (cookie path)

The SPA has no Keycloak client of its own; it reuses the **dw** session:

- **dw** mirrors the current access token into an **`access_token` cookie**,
  `Domain=.ollebo.com`, `HttpOnly; Secure; SameSite=Lax`, refreshed on every dw
  request (`dw/src/server.js` preHandler hook). The SPA hits the dw API on load,
  which sets/refreshes it.
- The SPA **sends** it cross-origin to `maps.ollebo.com`:
  - `ollebo-maps/src/lib/authFetch.ts` patches `window.fetch` to add
    `credentials:'include'` for `MAPS_ORIGIN` (covers online-3d-viewer + deck.gl/loaders.gl).
  - `ObjScene.tsx` (three.js) uses `setWithCredentials(true)` + `setCrossOrigin('use-credentials')` (obj/mtl + textures).
  - private OpenLayers tiles use `crossOrigin: 'use-credentials'`.
- **CORS**: maps-proxy echoes any `*.ollebo.com` Origin + `Allow-Credentials: true`
  and answers OPTIONS preflight. The maps-proxy route must **not** carry the
  `terracotta-cors` (`*`) middleware — `*` is illegal with credentials and would
  duplicate the header (that was the last "CORS error" bug; fixed by the ingress split).

> Also valid: send `Authorization: Bearer <token>` instead of the cookie (the API
> path, e.g. `detections.ts`). maps-proxy accepts either.

## 5. Adding NEW assets — what's automatic

Once the one-time setup (§6) is in place, **nothing manual is needed per asset**:

- New **spaces**: `provisionSpace` already names the Keycloak group by its id.
- New **models**: upload → create → the pipeline copies + serves them.
- New **members**: added to the space's Keycloak group → their next token carries
  the space UUID → they can view.

Just upload + create the model in dw; log in on `dash.ollebo.com`; open it.

## 6. One-time setup (already done — don't repeat unless rebuilding the realm)

1. **Keycloak mappers** — on client scopes **`Groups`** and **`microprofile-jwt`**,
   the `groups` mapper must be **Group Membership** (Type: Group Membership,
   Token Claim Name `groups`, Full group path **OFF**, add to access/id/userinfo token).
   (They were wrongly `User Realm Role` mappers, which put realm roles in `groups`.)
2. **Rename existing groups** so `name == id`:
   ```
   kubectl -n ollebo exec deploy/dw-app -- node src/scripts/renameSpaceGroups.js
   ```
   (`dw/src/scripts/renameSpaceGroups.js`, DB-scoped, idempotent.)
3. **Ingress split** — the maps-proxy catch-all lives in its own ingress without
   `terracotta-cors` (`maps-proxy/charts/templates/ingress.yaml`).

## 7. Troubleshooting

Decode a user's token (Keycloak → Clients → dw-client → Client scopes → Evaluate,
or jwt.io) and check the **`groups`** claim:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `groups` = `default-roles-master, offline_access, uma_authorization` | scope mapper is *User Realm Role* | §6.1 (make it Group Membership) |
| `groups` missing the space UUID (has other names like `Private_x`/`hrb_x`) | group `name != id` | §6.2 (rename group to its id) |
| **CORS error** in the browser | `terracotta-cors` `*` on the maps-proxy route | §6.3 (ingress split) |
| **401** on the `maps.ollebo.com` request | no valid token reaching the proxy: cookie not set/sent, or expired | check DevTools → the request has `Cookie: access_token`; re-login |
| **403** | valid token, but `space_id ∉ groups` (not a member, or group name≠id) | add user to the space group / rename group |

Quick server-side checks (run in the dw pod, which has the admin client + creds):
```js
// is a user in the space group?  kc.users.listGroups({ id: <keycloakId> })
// is the group named by its id?   kc.groups.findOne({ id: <spaceId> })  // name === id ?
// what do the scope mappers do?   kc.clientScopes.listProtocolMappers({ id: <scopeId> })
```
Verify a token end to end: set `Cookie: access_token=<jwt>` and GET
`http://maps-proxy:8080/models/<space>/<model>/<file>` → expect **200**.

## 8. Gotchas

- **Group name must equal group id (the space UUID).** This is the #1 trap.
- **Token lifetime ~5 min.** dw refreshes the cookie on each dw request, so active
  use is fine, but an idle-then-load after 5 min can 401 until the next dw call.
- `maps.ollebo.com` is **path-split** (terracotta vs maps-proxy) — private map
  tile URLs must go through a maps-proxy prefix (`private/…`), not a raw terracotta
  path, or they're served with no auth.
- Models are **always** auth-gated via the `models/` prefix (no public bypass in
  the proxy), regardless of the model's `access` field.
