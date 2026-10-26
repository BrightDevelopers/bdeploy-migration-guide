---
name: migrating-bdeploy-v1-to-v2
description: Use when porting or reviewing client code that calls the BrightSign B-Deploy provisioning API v1 (/rest/v1/authenticate, /rest-device/v1, /rest-setup/v1, PascalCase setup fields like PackageName or NetworkSettings) to the current device v2 / setup v3 API, in any language.
---

# Migrating B-Deploy v1 clients to v2/v3

## Overview
v1 → v2/v3 is a different contract, not a version bump: new auth, a new setup model,
and request/response shapes that differ per operation. Most broken migrations guess
routes and response shapes. **Do not guess: every rule below comes from the server
code; confirm details in `swagger/bdeploy-v2-v3.yaml` in the bdeploy-migration-guide repo.**

The skill is language-agnostic: it defines the wire contract; map it onto the client's
HTTP code. In Go, prefer the `gopurple` SDK's B-Deploy service, but avoid its known gaps
(`WithPackageName` filter is ignored, `pageSize` is never sent so lists truncate at the
server page size, setup lists drop `setupJson`, setup and device PUTs send partial
records, network proxy field names) — see `docs/IMPLEMENTATION-NOTES.md`.

## Wire contract (the part agents get wrong)

| Operation | Correct request | Response `result` |
|---|---|---|
| Auth | OAuth2 client-credentials at `https://auth.bsn.cloud/realms/bsncloud/protocol/openid-connect/token`, then `PUT https://api.bsn.cloud/2022/06/REST/Self/Session/Network` `{"name":"<network>"}` | — |
| Create setup | `POST /rest-setup/v3/setup` | id string (array of ids for multi-element body) |
| Get setup | `GET /rest-setup/v3/setup/{id}` (not `?_id=`: one implementation ignores it and returns the list) | **one-element array** of the setup |
| List setups | `GET /rest-setup/v3/setup?NetworkName=<net>&query[packageName]=...&pageSize=100` | array of records; setup is a JSON **string** in `setupJson` |
| Update setup | `PUT /rest-setup/v3/setup`, `_id` **in body**, complete setup | update result, **not** the setup |
| Delete setup | `DELETE /rest-setup/v3/setup?_id=<id>` (no path form) | `true`; 500 "in use" while devices reference it |
| Create device | `POST /rest-device/v2/device` with `serial`, `username`, `NetworkName`, and `setupId` or `url` | id string |
| Find device | `GET /rest-device/v2/device?serial=<s>&NetworkName=<net>` | `{total, matched, players[], priv}` |
| Update device | `PUT /rest-device/v2/device`, `_id` **and** `serial` in body, **complete record** | `null` |
| Delete device | `DELETE /rest-device/v2/device?_id=<id>` or `?serial=<s>` | `true` |

There are **no** `PUT/DELETE .../{id}` path routes and no `/rest-device/v3`.
Every response is `{"error": string|null, "result": ...}`; 201 on create.

## Rules
1. **Partial device PUT destroys data.** One implementation replaces the whole record.
   To change one field: GET the device, modify, PUT the full record (with `serial`).
2. **`setupId` is never returned by device GETs.** Read `setupName` instead; if callers
   need the id, keep it client-side or resolve `setupName` via a setup list. A device
   read from GET and PUT back has lost its `setupId`: re-add it before the PUT (the
   server's fallback re-resolution from `setupName` + `NetworkName` silently fails when
   names are ambiguous or renamed).
3. **Choose `setupType` from what the v1 setup did — never hard-code `bsn`.**
   Use only `partnerApplication`, `bsn`, `standalone`, `lfn`, `sfn` (one implementation
   rejects `legacyLfn`/`localToBsn`).
   `bsn` requires `bsnDeviceRegistrationTokenEntity` {token, scope, validFrom, validTo}
   (create via BSN.cloud `POST /2022/06/REST/Provisioning/Setups/Tokens/`). Partner
   content URL → `partnerApplication` with `bDeploy.url`. Otherwise ask.
4. **Static addressing needs addresses.** `proto: "Static"` needs `ip` (CIDR),
   `gateway`, `dns` for the player to come up; the servers don't validate this, so a bad
   setup is only caught on the player. If the v1 data lacks them, keep `DHCPv4` and flag it.
5. **Setup field mapping:** use the table in `docs/MIGRATION-v1-to-v2.md` §3.
6. **Portability (two server implementations):** treat ids as opaque strings; always send
   `pageSize` and the network; `Content-Type: application/json` exactly; branch on status
   class + `error`, and tolerate empty or `application/problem+json` error bodies.
7. **Auth semantics change:** v1 user/password become OAuth client id/secret. Surface
   this to the user as a breaking change for callers.

## Common mistakes
| Mistake | Fix |
|---|---|
| Decoding get-setup as an object | Decode array, take `[0]`, error if empty |
| Getting a setup with `?_id=` | Use `/setup/{id}`; one implementation ignores `?_id=` and returns the list |
| Assuming POST defaults match v1 | Defaults turn on LWS, remote DWS, BSN.cloud; send `false` explicitly |
| Lower-case serials | Serials are stored upper-case and matched case-sensitively |
| Expecting the setup back from PUT | Re-GET after PUT |
| Filtering setups with flat `packageName=` | `query[packageName]=` |
| Device list with no filter | Always pass `NetworkName` or `serial` (no filter → empty list) |
| Sending v2 setup (`/rest-setup/v2`) without `version: "2.0.0"` | Use v3, or send version |

## Done when
Each migrated call matches a row above and its operation in `swagger/bdeploy-v2-v3.yaml`,
and every behaviour change visible to callers (auth inputs, missing `setupId`) is reported.
