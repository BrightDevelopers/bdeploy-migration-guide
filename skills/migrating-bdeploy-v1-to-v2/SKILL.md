---
name: migrating-bdeploy-v1-to-v2
description: Use when writing, porting or reviewing client code for BrightSign's B-Deploy provisioning API (provision.bsn.cloud) in any language. Covers v1 code (/rest/v1/authenticate, /rest-device/v1, /rest-setup/v1, PascalCase setup fields like PackageName or NetworkSettings) moving to device v2 / setup v3, and new or existing v2/v3 clients (BSN.cloud OAuth2 token, Self/Session/Network, /rest-setup/v3/setup, /rest-device/v2/device, registration tokens, bsnDeviceRegistrationTokenEntity).
---

# B-Deploy clients: migrating v1 and writing v2/v3

## Overview
Going from v1 to v2/v3 is a different contract, not a version bump. Authentication, the
setup model, and request and response shapes all change per operation. Most broken
clients guess at routes and response shapes. **Do not guess.** Every rule below comes
from the server source and was checked against production on 2026-10-26. Where the
`bdeploy-migration-guide` repository is available, confirm details there:
`swagger/bdeploy-v2-v3.yaml`, `swagger/bsn-main-provisioning.yaml`,
`docs/BDEPLOY-API-GUIDE.md` (troubleshooting table in section 13), and
`docs/MIGRATION-v1-to-v2.md` (the v1-to-v3 setup field mapping in section 3). Its `src/`
holds a reference Python client (`bdeploy/client.py`) that implements every rule here.

The skill is language-agnostic: it defines the wire contract, and you map it onto the
client's HTTP code. In Go, the `gopurple` SDK's B-Deploy service is usable but has known
gaps. Its `WithPackageName` filter is ignored, and `pageSize` is never sent, so lists
truncate at the server page size. Setup lists drop `setupJson`. Setup and device PUTs
send partial records. The network proxy field names are wrong. It also retries POST and
PUT on any 5xx. Work around these or call the API directly.

## Hosts and implementations
| Purpose | Host |
|---|---|
| OAuth2 token | `https://auth.bsn.cloud` |
| Session network, network list, registration tokens | `https://api.bsn.cloud` |
| B-Deploy | `https://provision.bsn.cloud` (staging/QA/dev: `provision.bsn-staging.net`, `provision.bsn-qa.net`, `provision.bsn-dev.net`) |

Two server implementations exist: the original Node services and a C# port. Production
v2/v3 is served by **C#** (response header `server: Kestrel`); v1 stays on Node. Other
environments may lag, so client code must work against both (rule 9).

## Authentication sequence (do this before any B-Deploy call)
1. `POST https://auth.bsn.cloud/realms/bsncloud/protocol/openid-connect/token`. Send the
   client id and secret as HTTP Basic auth and `grant_type=client_credentials` as a form
   body (`application/x-www-form-urlencoded`; anything else gets 415). The response
   holds `access_token` and `expires_in` (330 s). There is **no refresh token**.
2. `PUT https://api.bsn.cloud/2022/06/REST/Self/Session/Network` with
   `{"name":"<network>"}`. **Expect exactly 204.** The network lives on the token's
   session, not on the request, so repeat this after every new token.
3. Optionally, `GET` the same URL to confirm the selection: 200 `{id, name}` means it
   took, and **204 means no network is selected**. A client's first test should do
   steps 1–3, then list setups and devices.

Sessions end after 24 hours: `api.bsn.cloud` then answers 410 Gone, and the fix is a
new token plus a new network selection.

Configuration convention: `BS_CLIENT_ID`, `BS_SECRET`, `BS_NETWORK`, and `BS_USERNAME`
(the account e-mail, needed for setup and device writes). Optional: `BS_AUTH_URL`,
`BS_API_URL` and `BS_PROVISION_URL` for non-production hosts. Never hard-code the secret.

## Wire contract
| Operation | Correct request | Response `result` |
|---|---|---|
| Create setup | `POST /rest-setup/v3/setup` | id string (array of ids for a multi-element array body) |
| Get setup | `GET /rest-setup/v3/setup/{id}` (not `?_id=`: C# ignores it and returns the list) | **one-element array** of the parsed setup |
| List setups | `GET /rest-setup/v3/setup?NetworkName=<net>&pageSize=100&pageNum=<n>[&query[packageName]=…]` | array of records; the setup is a JSON **string** in `setupJson` |
| Update setup | `PUT /rest-setup/v3/setup`, `_id` **in body**, complete setup | update result (`{acknowledged, matchedCount, modifiedCount, …}`), **not** the setup |
| Delete setup | `DELETE /rest-setup/v3/setup?_id=<id>` (no path form) | `true`; deterministic 500 "in use" while devices reference it |
| Create device | `POST /rest-device/v2/device` with `serial`, `username`, `NetworkName`, and `setupId` or `url` | id string |
| Find device | `GET /rest-device/v2/device?serial=<S>&NetworkName=<net>&pageSize=…` | `{total, matched, players[], priv}` |
| Get device by id | `GET /rest-device/v2/device?_id=<id>` | device object or `null` |
| Update device | `PUT /rest-device/v2/device`, `_id` **and** `serial` in body, **complete record** | `null` |
| Delete device | `DELETE /rest-device/v2/device?_id=<id>` or `?serial=<S>` | `true` |
| Bulk delete devices | `DELETE /rest-device/v2/device/ids/<URL-encoded JSON array of id strings>` | `{deletedCount, deletedIds, failedIds, message}`; always 200 once valid, so read `failedIds` |
| Create registration token | `POST https://api.bsn.cloud/2022/06/REST/Provisioning/Setups/Tokens` (no body) | 201 `{token, scope, validFrom, validTo}` (bare JSON, no envelope) |
| Revoke registration token | `DELETE …/Provisioning/Setups/Tokens/{token}` | 204 |

There are **no** `PUT .../{id}` or `DELETE .../{id}` path routes, and there is no
`/rest-device/v3`. Every B-Deploy response is `{"error": string|null, "result": ...}`,
and creates return 201. Main API (`api.bsn.cloud`) responses are bare JSON.

## Rules
1. **Headers on every request**, on every host:
   - Send `Accept: application/json`. Without it, `api.bsn.cloud` answers **300** with an
     empty body and the action does not run, so a session-network PUT silently leaves
     no network selected.
   - Send an explicit `User-Agent`. Cloudflare blocks some library defaults with 403
     `error code: 1010` (Python's `Python-urllib` is one).
   - On POST and PUT, send exactly `Content-Type: application/json`. Node rejects
     `; charset=utf-8` with 415.
2. **Failure detection.**
   - Treat every non-2xx as a failure, 3xx included. Do not follow redirects.
   - Also treat a non-null `error` as a failure **even on 200**. C# returns
     `200 {"error":"Error: Invalid user"}` on some device routes.
   - Tolerate empty bodies (C# 401/403), `application/problem+json`, and HTML 500s.
   - Branch on status class and `error`, not on exact 4xx codes; the two implementations
     disagree on 400, 403 and 404.
3. **Token lifecycle and retries.**
   - Refresh the token before `expires_in` runs out, then select the network again.
   - On a 401, get a new token, select the network, and retry **once**. A second 401
     is a real failure.
   - **Never retry a 403.** On C#, an empty-body 403 means the session has no network,
     or the token's owner is not a user of that network.
   - On a 429, back off and honour `Retry-After`.
   - Never automatically retry POST or PUT, or the deterministic 500s ("in use", Node's
     missing-`username` crash).
4. **Partial PUTs destroy data.** Node replaces the whole device record (C# merges), and
   Node applies no setup defaults on PUT. To change one field, GET the record, modify it,
   and PUT the complete record. Include `_id` and `serial` for devices, and send
   `createdAt` back or it resets. Re-GET afterwards if you need the stored result.
5. **`setupId` is never returned by device GETs.** Both implementations strip it. Read
   `setupName` instead. A device read with GET and PUT back has lost its `setupId`, so
   re-add it before every PUT. Don't rely on the server re-resolving it from
   `setupName`: that fails when names are ambiguous or have been renamed.
6. **Lists and lookups.**
   - Always send `NetworkName` and `pageSize` (1–100; defaults are Node 100, C# 50).
     Page until a page comes back shorter than `pageSize`. The device `total` is not a
     usable count.
   - A device GET with no filter returns an **empty** list.
   - Filter setups with `query[packageName]=` (also `query[setupType]`,
     `query[username]`, `query[bsnGroupName]`, …). A flat `?packageName=` is silently
     ignored and returns everything.
   - C# filters with case-sensitive "contains", and live networks hold duplicate
     package names. So compare `packageName` exactly on the client, and handle more
     than one match.
   - Parse `setupJson` from its string. Older setups store booleans as `"1"`/`""` and
     numbers as strings, so accept both when reading and write canonical types.
7. **Choose `setupType` from what the v1 setup did; never hard-code `bsn`.**
   - Use only `partnerApplication`, `bsn`, `standalone`, `lfn`, or `sfn`. C# rejects
     `legacyLfn` and `localToBsn`.
   - A partner content URL means `partnerApplication` with `bDeploy.url`, which must be
     an absolute URL. `sfn` needs `sfnWebFolderUrl`. Otherwise, ask.
   - A `bsn` setup requires `bsnDeviceRegistrationTokenEntity`, copied from the token
     create call. That needs the `bsn.api.main.devices.setups.token.create` scope.
   - **C# also demands a registration token when `bDeploy.username` contains
     `brightsign`, `bsn` or `bacon`** (case-insensitive), whatever the `setupType`.
     Test with the account you will really use.
8. **Setup POST defaults turn features on.** `lwsEnabled`, `remoteDwsEnabled`,
   `bsnCloudEnabled` and `inheritNetworkProperties` default to `true`, and `timeZone`
   defaults to `PST` (a BrightSign code, not an IANA name). Send `false` for anything the
   v1 setup had off.
   - Required fields: `version` (`"3.0.0"` on `/v3`), `setupType`, `bDeploy.username`
     (Node crashes with an HTML 500 without it), and a `bDeploy.packageName` unique per
     network.
   - `bsnGroupName` must not start or end with whitespace or a period.
9. **Device creation.**
   - Upper-case serials; matching is case-sensitive.
   - C# requires a 12-character serial, `setupId` or `url`, and a `NetworkName` equal to
     the token's network (403 otherwise). It joins multiple errors with `"; "`.
   - `serial` is unique across **all** networks: POST fails with "Player already
     exists" and never updates. To move a player, delete it in the old network, select
     the new network, and register it there.
   - Array bodies skip the duplicate check, so use them only for serials known to be new.
   - `userdata` can be at most 1024 characters.
10. **Portability across the two implementations.** Treat ids as opaque strings (Node
    uses 24-hex ObjectIds; C# uses decimal strings). Get setups by path, never `?_id=`.
    Don't rely on the list record's top-level `version` describing `setupJson`.
11. **Static addressing needs addresses.** `proto: "Static"` needs `ip` (CIDR), `gateway`
    and `dns` for the player to come up. The servers don't validate this, so a bad setup
    is only caught on the player. If the v1 data lacks them, keep `DHCPv4` and flag it.
    Schema 3.0.0 uses `network.interfaces[]`, `network.proxyServer` (not `proxy`), and
    `network.proxyBypass` (an array).
12. **Changes apply at the next provisioning.** A player reads its device record and
    setup only when it provisions. To force that now, call Remote DWS:
    `GET https://ws.bsn.cloud/rest/v1/re-provision/?destinationType=player&destinationName=<SERIAL>`
    with the same bearer token. The player must be online and in the session network.
    **This wipes the player's storage and reboots it**, so confirm with the user before
    adding it to any code path.
13. **Authentication semantics change.** v1's user name and password become an OAuth
    client id and secret, plus a network selection. Report this to the user as a
    breaking change for callers.

## Common mistakes
| Mistake | Fix |
|---|---|
| No `Accept` header on the session-network PUT | Send it; check for 204; read the network back |
| Default HTTP library User-Agent | Set an explicit one |
| Checking only the HTTP status | Also check `error`; C# returns errors with 200 |
| Selecting the network once at startup | Select it again after every new token |
| Decoding get-setup as an object | Decode the array, take `[0]`, and error if it is not exactly one element |
| Getting a setup with `?_id=` | Use `/setup/{id}` |
| Assuming POST defaults match v1 | Send `false` explicitly for defaulted-on features |
| Lower-case serials | Upper-case them before sending |
| Expecting the setup back from PUT | Re-GET after PUT |
| Device PUT built from a GET response | Re-add `setupId`; send the complete record |
| Filtering setups with flat `packageName=` | Use `query[packageName]=`, then compare exactly |
| Device list with no filter, or no `pageSize` | Always pass `NetworkName` and `pageSize`, and page until short |
| Retrying POST/PUT or a "setup in use" 500 | Don't; delete or relink devices first |
| Sending a v2 setup (`/rest-setup/v2`) without `version: "2.0.0"` | Use v3, or send the version |
| `partnerApplication` setup rejected for a `@brightsign…` account | Add a registration token (C# rule) |

## Done when
- Every migrated or written call matches a row in the wire contract and its operation
  in `swagger/bdeploy-v2-v3.yaml`.
- Every rule above is honoured. The guide's pitfalls checklist (section 14) passes.
- Every caller-visible behaviour change has been reported: the new authentication
  inputs, the missing `setupId`, and the registration-token requirement.
- Where credentials are available, the client's first test performs the live check:
  token, select network, read it back, list setups, list devices.
