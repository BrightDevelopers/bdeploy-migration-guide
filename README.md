# B-Deploy API: developer guide and migration kit

B-Deploy is BrightSign's cloud provisioning service. When a BrightSign player starts with
blank storage, it asks B-Deploy what it should be. B-Deploy answers with a **setup**:
networking, diagnostics, firmware policy, and either a BSN.cloud registration or the URL
of your application. The player applies it, reboots, and runs.

This repository gives you what you need to automate B-Deploy from your own systems:

- a practical guide to the current API (device API v2, setup API v3);
- a step-by-step guide for moving an existing v1 integration;
- OpenAPI 3.0 specifications you can browse or feed to a code generator;
- a ready-to-run Python test client that checks your credentials and exercises the API;
- a Claude Code skill that applies all of this to your code ([install it](#claude-code-skill)).

## Where to start

| You want to… | Read or run |
|---|---|
| Check that your credentials and network work | [Verify your access](#1-verify-your-access) (about a minute) |
| Build a new integration | [`docs/BDEPLOY-API-GUIDE.md`](docs/BDEPLOY-API-GUIDE.md) |
| Move a v1 integration to v2/v3 | [`docs/MIGRATION-v1-to-v2.md`](docs/MIGRATION-v1-to-v2.md) |
| See a working client to copy from | [`src/`](src/README.md): Python, standard library only |
| Generate client code or browse the API | [`swagger/bdeploy-v2-v3.yaml`](swagger/bdeploy-v2-v3.yaml) and [`swagger/bsn-main-provisioning.yaml`](swagger/bsn-main-provisioning.yaml) |
| Have Claude Code port or review your client code | [Claude Code skill](#claude-code-skill) |

## Claude Code skill

If you use [Claude Code](https://claude.com/claude-code), install the bundled skill. It
gives Claude the B-Deploy wire contract and every pitfall in this repository, so it can
port a v1 client, write a new v2/v3 client, or review existing client code in any
language. It also reports changes your callers will notice, such as the move from a
user name and password to API client credentials.

**Install it** by copying the skill folder into a skills directory:

```bash
# For one project: run from the root of the project that holds your B-Deploy client
mkdir -p .claude/skills
cp -r /path/to/bdeploy-migration-guide/skills/migrating-bdeploy-v1-to-v2 .claude/skills/

# Or for every project on this machine
mkdir -p ~/.claude/skills
cp -r /path/to/bdeploy-migration-guide/skills/migrating-bdeploy-v1-to-v2 ~/.claude/skills/
```

The result must be `.claude/skills/migrating-bdeploy-v1-to-v2/SKILL.md`. Commit the
project copy if your team should share it.

**Use it** by starting a new Claude Code session in your project. Then either:

- ask in plain words, for example *"migrate our B-Deploy client to the v2/v3 API"* or
  *"review this B-Deploy code against the current API"*, and Claude loads the skill when
  the task matches; or
- type `/migrating-bdeploy-v1-to-v2` to load it explicitly.

To update the skill, copy the folder again over the old one.

## What you need

| Item | Where it comes from |
|---|---|
| **API client id and secret** | Create an API client in BSN.cloud. B-Deploy acts as the client's owner, so the owning account must be a user in the network you will provision into. |
| **Network name** | Exactly as BSN.cloud shows it; case and spaces matter. |
| **Account e-mail** | The BSN.cloud user name. Needed to create setups and register players. |
| **Registration-token scope** (optional) | `bsn.api.main.devices.setups.token.create` on the API client. Needed only for setups whose players join BSN.cloud. |

Every example in this repository reads these from the same environment variables:

```bash
export BS_CLIENT_ID='<API client id>'
export BS_SECRET='<API client secret>'
export BS_NETWORK='<network name>'
export BS_USERNAME='<account e-mail>'
```

Optional variables:

| Variable | Default | Used for |
|---|---|---|
| `BS_TEST_SERIAL` | random `ZZ` + 10 hex characters | Serial the test client's lifecycle run registers and removes |
| `BS_TEST_URL` | `https://example.com/bdeploy-test-client/autorun.zip` | Application URL in the lifecycle run's temporary setup |
| `BS_AUTH_URL`, `BS_API_URL`, `BS_PROVISION_URL` | `https://auth.bsn.cloud`, `https://api.bsn.cloud`, `https://provision.bsn.cloud` | Pointing the test client at a non-production environment |

Keep the secret out of source control. Put these lines in an untracked `.env` or `.envrc`
(both are already in this repository's `.gitignore`).

## 1. Verify your access

Run the test client's read-only check before writing any code. It changes nothing. It
gets a token, selects your network, reads the selection back, and lists your setups and
players:

```bash
make -C src live-check
```

```text
PASS  get access token -- scopes: ...
PASS  select session network -- 'mynetwork' selected (204)
PASS  read session network back -- id=32776 name='mynetwork'
PASS  list setups -- 5 setup(s) on first page; server='Kestrel' (C#)
PASS  list devices -- 5 device(s) on first page
```

You need Python 3.10 or later and `make`; there is nothing to install. If a step fails,
look the message up in the guide's [Troubleshooting](docs/BDEPLOY-API-GUIDE.md#13-troubleshooting)
table. The same check as plain `curl` commands is in
[Prerequisites and live check](docs/BDEPLOY-API-GUIDE.md#4-prerequisites-and-live-check).

To exercise the write API end to end, run `make -C src lifecycle`. It creates a temporary
setup and player record, reads, updates and verifies them, then deletes them again. See
[`src/README.md`](src/README.md) for what it touches and how it cleans up.

## 2. Provision a player in four calls

These commands use `curl` and `jq`.

**Get an access token.** Tokens last about 5.5 minutes and cannot be refreshed; request a
new one when it expires.

```bash
TOKEN=$(curl -s https://auth.bsn.cloud/realms/bsncloud/protocol/openid-connect/token \
  -u "$BS_CLIENT_ID:$BS_SECRET" -d grant_type=client_credentials | jq -r .access_token)
```

**Select your network.** B-Deploy takes the network from your token's session, not from
the request. Expect HTTP `204`. Repeat this after every new token.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT \
  https://api.bsn.cloud/2022/06/REST/Self/Session/Network \
  -H "Authorization: Bearer $TOKEN" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' -d "{\"name\":\"$BS_NETWORK\"}"
```

**Create a setup.** This one points players at your application. It returns
`{"error":null,"result":"<setupId>"}`.

```bash
SETUP_ID=$(curl -s -X POST https://provision.bsn.cloud/rest-setup/v3/setup \
  -H "Authorization: Bearer $TOKEN" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' -d @- <<EOF | jq -r .result
{
  "version": "3.0.0",
  "setupType": "partnerApplication",
  "bDeploy": {
    "username": "$BS_USERNAME",
    "networkName": "$BS_NETWORK",
    "packageName": "Lobby setup",
    "url": "https://cms.example.com/bdeploy/autorun.zip"
  },
  "bsnGroupName": "Default",
  "lwsEnabled": false,
  "remoteDwsEnabled": false
}
EOF
)
```

**Register a player** by serial number (12 characters, upper case). It returns
`{"error":null,"result":"<deviceId>"}`.

```bash
curl -s -X POST https://provision.bsn.cloud/rest-device/v2/device \
  -H "Authorization: Bearer $TOKEN" -H 'Accept: application/json' \
  -H 'Content-Type: application/json' -d @- <<EOF
{"serial": "UTD416000978", "username": "$BS_USERNAME", "NetworkName": "$BS_NETWORK",
 "name": "Lobby", "setupId": "$SETUP_ID"}
EOF
```

Power the player on with blank storage and it provisions itself:

```mermaid
sequenceDiagram
    participant P as Player (blank storage)
    participant B as B-Deploy
    participant F as Your file server
    P->>B: Who am I? (serial number)
    B-->>P: Setup script for the linked setup
    P->>B: Download setup package
    Note over P: Applies the setup and reboots
    P->>F: GET autorun.zip (device or setup URL)
```

## Key concepts

```mermaid
flowchart LR
    D1[Device<br/>serial UTD416000978] -->|setupId| S[Setup<br/>packageName Lobby]
    D2[Device<br/>serial XTC2000123AB] -->|setupId| S
    D3[Device<br/>serial XD4000555CD] -->|url| U[Your application URL]
    S -->|setupType bsn| T[Joins BSN.cloud]
    S -->|setupType partnerApplication| A[Runs your application]
```

- **Setup:** a reusable player configuration, named by `bDeploy.packageName`. One
  setup serves any number of players.
- **Device:** one player serial number, registered in one network. It is linked to a
  setup (`setupId`) or points straight at an application (`url`). A serial can be
  registered in only one network at a time.
- **Setup type** decides what the player does after provisioning:

  | `setupType` | The player… |
  |---|---|
  | `partnerApplication` | downloads and runs your application from `bDeploy.url` |
  | `bsn` | joins BSN.cloud and is managed from there (needs a registration token) |
  | `sfn` | pulls content from a web folder (Simple File Networking) |
  | `lfn` | receives content over the local network (Local File Networking) |
  | `standalone` | runs local content |

- **Hosts:** tokens come from `auth.bsn.cloud`, network selection and registration
  tokens from `api.bsn.cloud`, and all B-Deploy calls go to `provision.bsn.cloud`.

## Things that catch people out

Read these before writing a client. The guide has the details, and its
[pitfalls checklist](docs/BDEPLOY-API-GUIDE.md#14-pitfalls-checklist) is a good code-review
list.

- **Send `Accept: application/json` and your own `User-Agent` on every call.** Without
  `Accept`, the network-selection call answers `300` and silently does nothing. Some
  default library agents, including Python's `Python-urllib`, are blocked with
  `403 error code: 1010`.
- **Check the `error` field, not only the status.** Every response is
  `{"error": ..., "result": ...}`, and some errors arrive with HTTP 200. Treat any
  non-2xx, including 3xx, as a failure.
- **Select the network after every new token.** Without it, B-Deploy calls fail, often
  with an empty `403`.
- **Defaults switch features on.** A new setup gets the Local Web Server, remote
  diagnostics and BSN.cloud turned on unless you send `false`.
- **Setups come back in two shapes.** A list returns records holding the setup as a JSON
  *string* in `setupJson`. `GET /setup/{id}` returns a *one-element array*.
- **Device reads never include `setupId`.** Use `setupName`. Put `setupId` back before
  every device update, or the player loses its link.
- **Always send complete records on PUT.** A partial body can erase the fields you left
  out. Read, modify, then write the whole record.
- **Filter setups with `query[packageName]=`.** A plain `?packageName=` is ignored and
  returns everything. Name filters can match partially, so compare names exactly in your
  code, and expect duplicates in older data.
- **Always pass `NetworkName` and `pageSize`.** A device list with no filter is empty,
  and page sizes default differently between environments.
- **Upper-case serial numbers.** Matching is case-sensitive.
- **Account names containing `brightsign`, `bsn` or `bacon`** need a registration
  token on every setup, whatever the setup type.

## Repository contents

| Path | Contents |
|---|---|
| [`docs/BDEPLOY-API-GUIDE.md`](docs/BDEPLOY-API-GUIDE.md) | The current API: authentication, every call, the setup field reference, workflows, troubleshooting |
| [`docs/MIGRATION-v1-to-v2.md`](docs/MIGRATION-v1-to-v2.md) | Porting a v1 client: authentication, paths, behaviour changes, field-by-field setup mapping |
| [`docs/IMPLEMENTATION-NOTES.md`](docs/IMPLEMENTATION-NOTES.md) | Background on how the specs were produced and how server behaviour varies |
| [`swagger/bdeploy-v2-v3.yaml`](swagger/bdeploy-v2-v3.yaml) | OpenAPI 3.0: device API v2, setup API v2 and v3 |
| [`swagger/bsn-main-provisioning.yaml`](swagger/bsn-main-provisioning.yaml) | OpenAPI 3.0: access token, network selection, network list, registration tokens |
| [`swagger/v1Documentation.yaml`](swagger/v1Documentation.yaml) | OpenAPI 3.0: the legacy v1 API, for maintaining or migrating old clients |
| [`src/`](src/README.md) | Python test client: live check, end-to-end lifecycle test, reusable client class |
| [`skills/migrating-bdeploy-v1-to-v2/`](skills/migrating-bdeploy-v1-to-v2/SKILL.md) | Claude Code skill for migrating and reviewing B-Deploy client code |

## Browsing the specs and generating code

- **In a browser:** open <https://editor.swagger.io/> and paste or upload a YAML file.
- **Locally:** `npx @redocly/cli@1.34.3 preview-docs swagger/bdeploy-v2-v3.yaml`
  (stop with Ctrl-C).
- **Code generators:** each spec is self-contained OpenAPI 3.0. The `oneOf` result types
  and the `JsonNull` schema are deliberate: some responses change shape depending on the
  request.
