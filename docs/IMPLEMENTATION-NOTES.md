# B-Deploy specs: provenance and implementation notes

This file records where [`v1Documentation.yaml`](../swagger/v1Documentation.yaml) and
[`bdeploy-v2-v3.yaml`](../swagger/bdeploy-v2-v3.yaml) come from. It
also lists behaviour that a spec cannot express cleanly: server defects, differences
between the two server implementations, and mismatches in the `gopurple` SDK.

## Sources

The specs were written by reading the server source, not by observing live traffic, and
have not been validated against a running server. Last verified on 2026-10-26 against
`provisioning-server` `a7811d7`, `bsn-cloud-inherited` `develop` `12324a084` and
`gopurple` `4bc27aa`. C# framework behaviours (empty 401/403 bodies,
`application/problem+json` errors, 406 on failed package downloads) are inferred.

| Source | Location | Used for |
|---|---|---|
| PVS (device service, Node) | `bsn-core/provisioning-server/server/` (`api-utils-v1.js`, `api-utils-v2.js`, `service.js`, `db-utils.js`, `app-middleware.js`, `device-schema.js`) | `/rest`, `/rest-device` |
| PSS (setup service, Node) | `bsn-core/provisioning-server/external/pss/pss-server/` (`api-utils-v{1,2,3}.js`, `service*.js`, `db-utils.js`, `upgrade-middleware.js`, `query-params-utils.js`, `device-setup-schema*.js`) | `/rest-setup` |
| C# port | `bsn-cloud-inherited` branch `develop`, `WebHosts/BSN.Cloud.Hosts.API.Provisioning` | v2/v3 routes, setup JSON schema (`Application/Compatibility/DTO`) |
| Gateway routing | `provisioning-server/k8s/kustomize/pvs-kustomize/base/httproute.yaml` (PVS), `provisioning-server/external/pss/k8s/kustomize/pss-kustomize/base/httproute.yaml` (PSS), `bsn-cloud-inherited` `develop:k8s/kustomize/provisioning-service-kustomize/` (C#), `bsn-local-was-here/services/infra/config/envoy.yaml` (local dev) | Path prefixes, rewrites, hostnames |
| v1 auth | `provisioning-server/server/authn-utils.js` | v1 JWT, security notes |
| Setup defaults | `provisioning-server/external/bs-device-setup/src/dataModel/deviceSetupDefault.ts` | POST default merge |
| Client | `BrightDevelopers/gopurple` (`internal/services/bdeploy.go`, `internal/types/types.go`) | Cross-check |
| Published docs | `provisioning-server/external/bs-api-docs-20250614/Cloud-APIs/B-Deploy_Provisioning-APIs/` | Cross-check only; where they disagree with the code, the code wins |

### Which implementation serves which path

All three services hang off the same gateway, and each strips its prefix before
forwarding:

| Service | HTTPRoute prefixes | Rewrite |
|---|---|---|
| PVS (Node) | `/rest/`, `/rest-device/` | `^/(rest\|rest-device)/(.*)$` → `/\2` |
| PSS (Node) | `/rest-setup/` | `^/rest-setup/(.*)$` → `/\1` |
| C# port | `/rest/v2/`, `/rest/v3/`, `/rest-device/v2/`, `/rest-device/v3/`, `/rest-setup/v2/`, `/rest-setup/v3/`, `/2026/` | `^/(rest\|rest-device\|rest-setup)/(.*)$` → `/\2` |

The C# route is on `bsn-cloud-inherited` `develop` and has hostnames for every
environment, production included. With Gateway API longest-prefix matching, that means:

- **v2/v3 device and setup traffic goes to C#**, including the player hops
  `/rest-device/v2` and `/rest-setup/v2/*-script`.
- **Bare `/rest-device/`, `/rest/v1/*`, `/rest-device/v1/*` and `/rest-setup/v1/*` stay
  on Node.**

**Production is cut over.** On 2026-10-26, `GET /rest-setup/v3/setup` and
`GET /rest-device/v2/device` on `provision.bsn.cloud` answered with `server: Kestrel`
(ASP.NET), i.e. the C# service. Local-dev routing (`envoy.yaml`, paths prefixed with
`/provision/`) has the same split.

The specs were written with Node as the reference and call out C# differences where they
affect clients. Since C# is the configured v2/v3 target, write clients that work with
both (see the "Two implementations" section of `bdeploy-v2-v3.yaml`).

Hostnames: production is `provision.bsn.cloud` and also `provision.brightsignnetwork.com`;
staging, QA and dev are `provision.bsn-staging.net`, `provision.bsn-qa.net` and
`provision.bsn-dev.net`. Disaster-recovery hosts `provision.bsn.repair` and
`provision.bsn-dr-staging.repair` also exist.

## Observed on the live service (2026-10-26)

Found while running a read-only live check (token, session network, setup and device
lists) against production:

- **v2/v3 is served by the C# port** (`server: Kestrel`). The C# column of the table
  below is the behaviour production clients get.
- **Main API needs an `Accept` header.** Without one, `api.bsn.cloud` returns
  `300 Multiple Choices` with an empty body (`AcceptlessRequestFilter`) and does nothing.
  A `PUT Self/Session/Network` without `Accept` therefore leaves the session with no
  network, and every B-Deploy call then fails with an empty-body 403.
- **C# authorization is by principal type.** Setup routes need a "User" principal
  (identity name `network/login`, built from the session network). A token whose session
  has no network is a "Person" and gets an empty-body 403 on setup and device lists.
- **Cloudflare blocks the `Python-urllib` User-Agent** on `auth.bsn.cloud` and
  `api.bsn.cloud` (403, `error code: 1010`). `provision.bsn.cloud` does not.
- **`packageName` is not unique in practice.** The test network holds two setups named
  `preflight-20260410-032116`, so look-ups by name must handle duplicates.
- Existing records have 24-hex ObjectIds (migrated from Node).

## Corrections to the previous specs

These rows compare the specs in this repository with the earlier published B-Deploy
OpenAPI descriptions they replace.

| Previous spec | Actual behaviour |
|---|---|
| Server `http://player_ip/api` | `https://provision.bsn.cloud` (plus staging/qa/dev) |
| Bare response bodies | Every JSON response is `{"error": ..., "result": ...}` |
| UUID ids | 24-hex MongoDB ObjectIds (Node); decimal strings for records created by C# |
| `POST /rest/v2/authenticate` | Does not exist. v2/v3 use BSN.cloud OAuth2 tokens with a session network |
| `POST /rest-device/v2/device/{_id}` (get by id) | It is `GET` |
| v2 `DeviceSetup` = PascalCase v1 model | v2/v3 setups are the `BsDsSetupParams` JSON (`bDeploy`, `setupType`, `version`, `network`, ...) |
| Device `url` required (v2) | Only `serial` and `username` are required (C# also needs `setupId` or `url` for new records) |
| v1 setup requires NetworkName/PackageName/Login/Password | Only for BSN client accounts; otherwise only `username` |
| 200 on create | 201 |
| No error responses | Documented per operation |
| No v3 setup endpoints | `/rest-setup/v3/*` added; there is no `/rest-device/v3` |

## Behaviour clients often get wrong

- **`setupId` is never returned by device GETs.** Both implementations strip it
  (Node `omitIds` in `service.js`). Use `setupName`. This explains gopurple's
  "setupId not returned" limitation in gopurple's `docs/bdeploy-api-limitations.md`.
- **A device GET with no filter returns an empty list.** Supply `NetworkName` or another filter.
- **Setup GET has two shapes.** A list returns stored records with `setupJson` as a JSON
  *string*. Get-by-id returns a one-element *array* of the parsed setup. Use the path form
  `/setup/{id}`: the C# service ignores `?_id=` and returns the list instead.
- **Flat `packageName` is ignored on setup lists.** Use `query[packageName]=`.
- **Device PUT on Node is a full replacement.** Omitted fields are deleted, and
  `createdAt` is reset unless sent back. Send the complete record, upper-case `serial`.
- **Node accepts only 24-hex ObjectIds** on device GET/PUT/DELETE by id. A decimal id
  created by the C# service gets 400 "Invalid Device ID" from Node.
- **Serial matching is case-sensitive** (Node filters, `DELETE ?serial=`, C# lookups).
  Serials are stored upper-case, so send them upper-case.
- **Setup PUT returns a MongoDB update result, not the setup.**
- **Setup POST merges server defaults; setup PUT does not on Node** (C# merges on PUT
  too). Send complete setups on PUT. A v2 (`/rest-setup/v2`) POST must send
  `version: "2.0.0"`, because the default is `3.0.0`.
- **POST defaults switch things on**: `lwsEnabled`, `remoteDwsEnabled`,
  `bsnCloudEnabled` and `inheritNetworkProperties` default to `true`, and `timeZone` to
  `PST`. A migrated setup that had these off must send `false` explicitly.
- **Deleting a setup still referenced by a device returns 500** ("Cannot delete device
  setup because it is in use").
- **`serial` is globally unique.** POST fails with "Player already exists" if the serial
  is registered on any network.
- **Network context is required.** v2/v3 resolve the network from the token's BSN.cloud
  session. Call `PUT https://api.bsn.cloud/2022/06/REST/Self/Session/Network` first.

## Server defects (Node), as of the source read on 2026-10-26

| Area | Defect |
|---|---|
| `DELETE /rest-device/v1/device/ids/{idarray}` | Handler passes its callback in the `jwtDecoded` position; every valid request fails with a 500. A malformed array calls an undefined `createResponse` (also 500). Marked deprecated in the spec. |
| `POST /rest-device/v{1,2}/device` with an array body | The "existing serial" checks read `req.body.serial` (undefined for arrays), so the "Player already exists" check is skipped and a registered serial fails on the unique index with a duplicate-key error. The upsert branch that would update an existing record is unreachable for any body, because the global serial check rejects it first. |
| `PUT /rest-device/v{1,2}/device` | Existence and network checks use the record found by body `serial`, but the delete uses body `_id`. If they point at different records, the `_id` record is deleted (even in another network) and the save then fails on the unique serial. See Security observations. |
| `DELETE /rest-device/v{1,2}/device/ids/{idarray}` with a malformed, non-array or empty array | Calls an undefined `createResponse`, giving a non-JSON 500. Only non-string entries give 400 "Invalid IDS". |
| `POST /rest-device/v2/device`, token without username or client id | No response is ever sent (request hangs). |
| v2 device lists | Pagination (skip/limit) runs over all networks before the network filter, so pages can be short or empty and `matched` counts only the page. Always pass `NetworkName`. |
| `PUT /rest-setup/v1/setup` | Missing `return` after the not-found callback in `db-utils.js`. A nonexistent id is caught earlier (404), so this only fires on a count error or a concurrent delete, which can produce a double response. |
| PSS `checkPostContentType` | Requires the header to equal `application/json` exactly; `; charset=utf-8` returns 415. |
| `GET /rest-setup/v{2,3}/setup` list | A 403 network mismatch is overwritten to 400 by `getDeviceSetupGTEV2`. |
| `POST /rest-setup/v{2,3}/setup` without `bDeploy.username` | `.toLowerCase()` on undefined throws, giving an unhandled 500. |
| Device `total` in v2 lists | Sums unrelated counts, so it is not a usable total. |
| Version mapping on setup lists | Applied to the record's top-level `version`, not to the `setupJson` string, so the two can disagree. (C# does the opposite: converts `setupJson`, leaves `version`.) |
| `POST /rest-setup/v{2,3}/setup` and PUT, `bDeploy.networkName` | Never compared with the token's network, so a client can create or overwrite setups in another network. See Security observations. |

## Node vs C# differences that affect clients

From a full read of the C# controllers. Framework-level items (empty 401/403 bodies,
ProblemDetails, 406) follow from ASP.NET Core behaviour and have not been observed on a
live instance.

| Area | Node | C# |
|---|---|---|
| Default `pageSize` | 100 | 50 |
| Query formats | flat + `qs` brackets with type coercion; setups (PSS) also accept base64 `filter` | flat + `query[]`/`sort[]`/`page[]`, string values, no `filter`; flat `setupId`/`model` not recognised on devices (use `query[...]`) |
| Device `username` filter | case-insensitive prefix | contains |
| Setup filters `packageName`, `deviceName`, ... | exact (Mongo equality) | contains, case-sensitive |
| New record ids | ObjectId | decimal string |
| Auth failure body | `{error,result}` | 401/403 empty body |
| Missing session network | 401 | 403 "Token does not include a network"; 200 `{"error":"Error: Invalid user"}` on device GET-by-id, POST, DELETE and bulk delete |
| Token with no username and no client id | 200 `{"error":"Error: Invalid user"}` on v2 device routes (POST hangs) | 200 `{"error":"Error: Invalid user"}` on device routes |
| `bsn.pxs.admin` scope | bypasses network scoping on devices only (PSS still requires a session network) | bypasses network scoping on devices and setups |
| Content-Type | exact `application/json` (PSS) | parameters allowed; 415 as ProblemDetails |
| `POST /v2/device` | no network or setup checks; first error wins; body `username`/`client`/`NetworkName` used | 403 if `NetworkName` ≠ token network; 12-char serial; `setupId` or `url` required for new records; all errors joined by `"; "`; `username`, `client`, `NetworkName` taken from the token; unresolvable `setupName` without `setupId` is 400 |
| `PUT /v2/device` | full replace; `_id` required (404 if unknown) | merge of present keys; looked up by `serial`; may create (201); `NetworkName` immutable; `_id`/`serial` mismatch is 400 |
| `GET /v2/device?_id=` unknown | 404 | 200 `null` |
| `GET /v2/device/{id}` other network | 200 `null` | 404 |
| `DELETE /v2/device` unknown | 404 | 400 |
| Setup list without network | 400 | allowed, auto-scoped |
| Setup get-by-id network check | none (a v1-only id passes the existence check but returns 404 `result: null`) | 403 on mismatch |
| Setup PUT/DELETE unknown id | 404 | 400 |
| Setup PUT validation version | route | body `version` |
| Setup PUT defaults | none | top-level defaults merged; `bDeploy.networkName` forced to token network |
| `setupType` values | `legacyLfn`, `localToBsn` accepted | rejected; allow-list has `LegacyLocalFileNetworking`, `LocalToBrightSignNetworking` instead |
| Registration token required | `setupType: bsn`, and BSN client accounts from config | also any `bDeploy.username` containing `brightsign`, `bsn` or `bacon` (case-insensitive), even for `partnerApplication` |
| `bDeploy.networkName` on POST | required; any value accepted | optional (filled from token); 403 if different |
| Missing `bDeploy.username` | unhandled 500 (HTML, no envelope) | 400 "Invalid setup data format" |
| Setup PUT uniqueness check | no | yes |
| Setup PUT result | real Mongo result | constant `{acknowledged:true, modifiedCount:1, upsertedId:null, upsertedCount:0, matchedCount:1}` |
| `bDeploy.client` default | token client id | `"default-client"` |
| v3 get-by-id error bodies | envelope | bare JSON strings |
| Setup-package download errors | envelope | likely 406 (action produces `application/zip`) |
| `/v2/application` | raw manifest incl. `type`; 500 on failure | `type` dropped; 200 empty list on failure |
| Device entry redirect | `/rest-device/` and `/v2`, 301 | `/v2` only, 302; may also target `fast-setup-script` or `runtime-script`; 204 on any exception |
| Null fields | omitted | emitted as `null` (except `model`, `customization`, `url`, `userdata`, activation fields) |
| Absent in C# | v1 APIs (except the v1 runtime-script route), `DELETE /v2/admin/device`, `DELETE /v2/setup/rma` | — |

The C# service also exposes a newer, unrelated surface that is not covered by these
specs: `GET /2026/03/REST/ProvisioningRecords/` and `/2026/03/REST/SetupPackages/`.
These use paged results without the envelope.

## gopurple mismatches

Checked against `BrightDevelopers/gopurple` commit `4bc27aa`. gopurple calls only
`/rest-device/v2/device` and `/rest-setup/v3/setup`, and its auth flow (client
credentials, then the session-network PUT) matches the wire contract.

| gopurple | Issue |
|---|---|
| `WithPackageName` → flat `packageName=` | Ignored by Node, and not a recognised flat parameter in C# either. Use `query[packageName]`. |
| `WithBDeployPageSize` / `WithBDeployPage` | Stored but never sent. Lists get the server default page size (100 on Node, 50 on C#) and `TotalCount` is set to `len(result)`, so larger accounts are silently truncated. `GetAllDevices` has no paging option. |
| `BDeployRecord` (setup list item) | Has no `setupJson` field, so the setup itself is dropped. It also maps `createdDate`/`updatedDate`/`isActive`, which the server does not send (Mongo uses `createdAt`/`updatedAt`). |
| `UpdateSetupRecord` sends the typed `BDeploySetupRecord` | The struct models only part of the setup (no `firmwareUpdatesByFamily`, `omitempty` on bools). Setup PUT does not merge defaults, so unmodelled fields are erased. |
| `UpdateSetupRecord` decodes `result` as `BDeploySetupRecord` | Server returns an update result, so the decoded record is empty. |
| `NetworkConfig.Proxy` (`proxy`), `ProxyBypass` string | Server fields are `proxyServer` (string) and `proxyBypass` (array). |
| `GetAllDevices` without a network or `setupName` | Sends no filter, so the server returns an empty list. The network filter comes only from `BDeploy.SetNetworkContext`; `Client.SetNetwork` does not set it. |
| `GetDeviceBySerial` / `GetAllDevices` retry with an unwrapped decode | Unnecessary; responses are always enveloped. `GetDeviceBySerial` sends a second GET on every not-found. |
| `UpdateDevice` sends the request as given | No GET-modify-PUT, and `BDeployDeviceRequest` has no `client` or `createdAt`. On Node (full replace) fields the caller omits, such as `model`, `url`, `userdata`, are deleted and `createdAt` is reset. |
| `UpdateDevice` sends `?_id=` | Harmless; both servers ignore it. Node finds the record by body `serial` and replaces it by body `_id`; C# uses `serial`. |
| `UpdateDevice` backfills `setupId` from the request | Correct workaround, since `setupId` is never returned. |
| Setup `DELETE` decodes into `BDeployDeleteResponse{Success,...}` | Server returns `result: true`, so `Success` is always false and `error` is never checked. `DeleteDevice` also ignores `result`. |
| HTTP retry on any 5xx | Applies to POST and PUT too (not idempotent), and retries the deterministic "setup in use" 500 on DELETE. |

## Security observations (for follow-up, not spec content)

- **v1 JWT payload contains the password.** `authn-utils.js` signs `{username, password}`
  into the token, so anyone holding a v1 token can read the account password.
- **v1 has no account isolation on device operations.** v1 tokens carry no network, so
  get-by-id, PUT, DELETE, and the serial fallback in queries work on any account's
  devices.
- **Node v2/v3 setups lack network checks** on get-by-id (which exposes registration
  tokens), PUT, and POST (any `networkName` accepted). The C# port adds these checks.
- **Node v2 device POST** accepts any `NetworkName`. The C# port adds a check.
- **Node device PUT can delete another network's device.** Checks use the record found
  by body `serial`; the delete uses body `_id`. A mismatched `_id` deletes that record
  (any network, any account on v1), and the save then fails, so it is lost.
- **Committed credentials.** The C# `appsettings.json` and `appsettings.Development.json`
  contain the same literal `RmaToken`, which authorises RMA deletes (Node reads it from
  the environment). An identical hard-coded `api-test-token` value that bypasses the
  player client-certificate check is in C# `Security/ClientCertificateAuthenticationHandler.cs`
  (default of `Options.TestToken`), PSS `app-middleware.js` and PVS
  `server/app-middleware.js`. Verify these don't match production values.
- **Setup DELETE leaks "in use" across networks.** Both implementations run the in-use
  check before the network check, so the response reveals whether another network's
  setup is in use. On Node the check calls PVS `GET /v2/count-setups/:setupId`, which is
  not network-scoped.
