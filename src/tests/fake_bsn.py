"""In-process fake of auth.bsn.cloud, api.bsn.cloud and the C# B-Deploy service.

Models the behaviour documented in docs/BDEPLOY-API-GUIDE.md and
docs/IMPLEMENTATION-NOTES.md (C# column), so the client can be tested offline.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN_LIFETIME_SECONDS = 330
C_SHARP_PAGE_SIZE_DEFAULT = 50
SERIAL_LENGTH = 12
NETWORK_ID = 32776
SHUTDOWN_POLL_SECONDS = 0.01
DEFAULT_SERVER_HEADER = "envoy"
BLOCKED_AGENT_PREFIX = "Python-urllib"
REGISTRATION_TOKEN_USERNAME_PATTERN = re.compile(r"brightsign|bsn|bacon", re.IGNORECASE)
SETUP_DEFAULTS: dict[str, Any] = {
    "bsnGroupName": "Default",
    "timeZone": "PST",
    "lwsEnabled": True,
    "remoteDwsEnabled": True,
    "bsnCloudEnabled": True,
    "deviceName": "",
    "deviceDescription": "",
    "network": {"interfaces": []},
}
UPDATE_RESULT = {"acknowledged": True, "modifiedCount": 1, "upsertedId": None, "upsertedCount": 0, "matchedCount": 1}


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass
class Fault:
    """A canned response for the next request(s) matching method and path prefix."""

    method: str
    path_prefix: str
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    remaining: int = 1


class FakeBsnCloud:
    def __init__(self, client_id: str, client_secret: str, network: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.network = network
        self.lock = threading.Lock()
        self.requests: list[RecordedRequest] = []
        self.faults: list[Fault] = []
        self.tokens: dict[str, str | None] = {}
        self.registration_tokens: set[str] = set()
        self.setups: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.next_id = 1000
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_SECONDS}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeBsnCloud:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_fault(self, fault: Fault) -> None:
        with self.lock:
            self.faults.append(fault)

    def new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def requests_to(self, method: str, path_prefix: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == method and r.path.startswith(path_prefix)]

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            response_server = DEFAULT_SERVER_HEADER

            def version_string(self) -> str:
                return self.response_server

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                fake.handle(self, "GET")

            def do_POST(self) -> None:
                fake.handle(self, "POST")

            def do_PUT(self) -> None:
                fake.handle(self, "PUT")

            def do_DELETE(self) -> None:
                fake.handle(self, "DELETE")

        return Handler

    # ---- dispatch ----

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        split = urllib.parse.urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in handler.headers.items()}
        request = RecordedRequest(method, split.path, urllib.parse.parse_qs(split.query), headers, body)
        with self.lock:
            self.requests.append(request)
            status, response_body, response_headers = self._route(request)
        # BaseHTTPRequestHandler always sends its own Server header, so route ours through it.
        handler.response_server = response_headers.pop("Server", DEFAULT_SERVER_HEADER)
        handler.send_response(status)
        for key, value in response_headers.items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(response_body)))
        handler.end_headers()
        handler.wfile.write(response_body)

    def _route(self, request: RecordedRequest) -> tuple[int, bytes, dict[str, str]]:
        for fault in self.faults:
            if fault.method == request.method and request.path.startswith(fault.path_prefix) and fault.remaining > 0:
                fault.remaining -= 1
                return fault.status, fault.body, fault.headers
        if request.headers.get("user-agent", "").startswith(BLOCKED_AGENT_PREFIX):
            return 403, b"error code: 1010", {"Content-Type": "text/plain"}
        if request.path == "/realms/bsncloud/protocol/openid-connect/token":
            return self._token(request)
        if "accept" not in request.headers:
            return 300, b"", {}
        network = self._token_network(request)
        if network is False:
            return 401, b"", {}
        if request.path.startswith("/2022/06/REST/"):
            return self._main_api(request, network)
        if network is None:
            return 403, b"", {"Server": "Kestrel"}
        if request.method in ("POST", "PUT") and request.headers.get("content-type") != "application/json":
            return 415, b"", {"Server": "Kestrel"}
        payload = json.loads(request.body) if request.body else None
        if request.path.startswith("/rest-setup/v3/setup"):
            return self._kestrel(*self._setups(request, network, payload))
        if request.path.startswith("/rest-device/v2/device"):
            return self._kestrel(*self._devices(request, network, payload))
        return 404, b"", {}

    def _token_network(self, request: RecordedRequest) -> str | None | bool:
        """The token's session network, None if none selected, False if the token is unknown."""
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ")
        if not authorization.startswith("Bearer ") or token not in self.tokens:
            return False
        return self.tokens[token]

    @staticmethod
    def _json(status: int, value: Any, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
        return status, json.dumps(value).encode(), {"Content-Type": "application/json", **(headers or {})}

    def _kestrel(self, status: int, error: str | None, result: Any) -> tuple[int, bytes, dict[str, str]]:
        return self._json(status, {"error": error, "result": result}, {"Server": "Kestrel"})

    # ---- auth and Main API ----

    def _token(self, request: RecordedRequest) -> tuple[int, bytes, dict[str, str]]:
        if request.headers.get("content-type") != "application/x-www-form-urlencoded":
            return 415, b"", {}
        expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        form = urllib.parse.parse_qs(request.body.decode())
        if request.headers.get("authorization") != f"Basic {expected}" or form.get("grant_type") != ["client_credentials"]:
            return self._json(401, {"error": "invalid_client", "error_description": "Invalid client credentials"})
        token = f"token-{self.new_id()}"
        self.tokens[token] = None
        return self._json(
            200,
            {"access_token": token, "expires_in": TOKEN_LIFETIME_SECONDS, "token_type": "Bearer", "scope": "openid"},
        )

    def _main_api(self, request: RecordedRequest, network: str | None) -> tuple[int, bytes, dict[str, str]]:
        token = request.headers["authorization"].removeprefix("Bearer ")
        path = request.path
        if path == "/2022/06/REST/Self/Session/Network":
            if request.method == "PUT":
                if json.loads(request.body).get("name") != self.network:
                    return 400, b"", {}
                self.tokens[token] = self.network
                return 204, b"", {}
            if network is None:
                return 204, b"", {}
            return self._json(200, {"id": NETWORK_ID, "name": network})
        if path == "/2022/06/REST/Self/Networks":
            return self._json(200, [{"id": NETWORK_ID, "name": self.network}])
        if network is None:
            return 403, b"", {}
        if path == "/2022/06/REST/Provisioning/Setups/Tokens" and request.method == "POST":
            value = f"regtoken-{self.new_id()}"
            self.registration_tokens.add(value)
            return self._json(
                201,
                {"token": value, "scope": "cert", "validFrom": "2026-10-26T00:00:00Z", "validTo": "2028-10-26T00:00:00Z"},
            )
        prefix = "/2022/06/REST/Provisioning/Setups/Tokens/"
        if path.startswith(prefix):
            value = urllib.parse.unquote(path.removeprefix(prefix))
            if value not in self.registration_tokens:
                return 404, b"", {}
            if request.method == "DELETE":
                self.registration_tokens.discard(value)
                return 204, b"", {}
            return self._json(200, {"token": value, "scope": "cert"})
        return 404, b"", {}

    # ---- setups ----

    @staticmethod
    def _page(items: list[Any], query: dict[str, list[str]]) -> list[Any]:
        size = int(query.get("pageSize", [C_SHARP_PAGE_SIZE_DEFAULT])[0])
        number = int(query.get("pageNum", ["1"])[0])
        return items[(number - 1) * size : number * size]

    def _setups(self, request: RecordedRequest, network: str, payload: Any) -> tuple[int, str | None, Any]:
        method = request.method
        if method == "POST":
            return self._create_setup(network, payload)
        if method == "GET" and request.path != "/rest-setup/v3/setup":
            setup_id = urllib.parse.unquote(request.path.rsplit("/", 1)[1])
            setup = self.setups.get(setup_id)
            if setup is None:
                return 404, "Setup not found", None
            return 200, None, [dict(setup)]
        if method == "GET":
            # C# ignores ?_id= and filters packageName with a case-sensitive "contains".
            wanted = request.query.get("query[packageName]", [""])[0]
            matches = [
                self._setup_record(setup)
                for setup in self.setups.values()
                if setup["bDeploy"]["networkName"] == network and wanted in setup["bDeploy"]["packageName"]
            ]
            return 200, None, self._page(matches, request.query)
        if method == "PUT":
            setup_id = payload.get("_id")
            if setup_id not in self.setups:
                return 400, "Setup not found", None
            merged = {**SETUP_DEFAULTS, **payload}
            merged["bDeploy"] = {**payload["bDeploy"], "networkName": network}
            self.setups[setup_id] = merged
            return 200, None, dict(UPDATE_RESULT)
        if method == "DELETE":
            setup_id = request.query.get("_id", [""])[0]
            if any(device.get("setupId") == setup_id for device in self.devices.values()):
                return 500, "Cannot delete device setup because it is in use", None
            if self.setups.pop(setup_id, None) is None:
                return 400, "Setup not found", None
            return 200, None, True
        return 405, "method not allowed", None

    def _create_setup(self, network: str, setup: dict[str, Any]) -> tuple[int, str | None, Any]:
        bdeploy = setup.get("bDeploy") or {}
        if not bdeploy.get("username"):
            return 400, "Invalid setup data format", None
        if not str(setup.get("version", "")).startswith("3."):
            return 400, "Invalid version", None
        if bdeploy.get("networkName") not in (None, network):
            return 403, "Network mismatch", None
        needs_token = setup.get("setupType") == "bsn" or REGISTRATION_TOKEN_USERNAME_PATTERN.search(bdeploy["username"])
        if needs_token and not setup.get("bsnDeviceRegistrationTokenEntity"):
            return 400, "A device registration token is required", None
        if any(s["bDeploy"]["packageName"] == bdeploy.get("packageName") for s in self.setups.values()):
            return 400, "Package name is not unique", None
        setup_id = self.new_id()
        stored = {**SETUP_DEFAULTS, **setup, "_id": setup_id}
        stored["bDeploy"] = {**bdeploy, "networkName": network}
        self.setups[setup_id] = stored
        return 201, None, setup_id

    @staticmethod
    def _setup_record(setup: dict[str, Any]) -> dict[str, Any]:
        return {
            "_id": setup["_id"],
            "packageName": setup["bDeploy"]["packageName"],
            "setupType": setup.get("setupType"),
            "networkName": setup["bDeploy"]["networkName"],
            "username": setup["bDeploy"]["username"],
            "version": setup.get("version"),
            "setupJson": json.dumps({key: value for key, value in setup.items() if key != "_id"}),
        }

    # ---- devices ----

    @staticmethod
    def _public(device: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in device.items() if key != "setupId"}

    def _devices(self, request: RecordedRequest, network: str, payload: Any) -> tuple[int, str | None, Any]:
        method = request.method
        query = request.query
        if method == "POST":
            return self._create_device(network, payload)
        if method == "GET" and "_id" in query:
            device = self.devices.get(query["_id"][0])
            return 200, None, self._public(device) if device else None
        if method == "GET":
            if "NetworkName" not in query and "serial" not in query:
                return 200, None, {"total": 0, "matched": 0, "players": [], "priv": False}
            matches = [
                self._public(device)
                for device in self.devices.values()
                if device["NetworkName"] == query.get("NetworkName", [network])[0]
                and ("serial" not in query or device["serial"] == query["serial"][0])
            ]
            page = self._page(matches, query)
            return 200, None, {"total": len(matches), "matched": len(page), "players": page, "priv": False}
        if method == "PUT":
            existing = next((d for d in self.devices.values() if d["serial"] == payload.get("serial")), None)
            if existing is None:
                return 404, "Device not found", None
            if payload.get("_id") != existing["_id"]:
                return 400, "_id and serial do not match", None
            existing.update({key: value for key, value in payload.items() if key != "NetworkName"})
            existing["setupName"] = self.setups[existing["setupId"]]["bDeploy"]["packageName"] if existing.get("setupId") else None
            return 200, None, None
        if method == "DELETE" and request.path.startswith("/rest-device/v2/device/ids/"):
            ids = json.loads(urllib.parse.unquote(request.path.removeprefix("/rest-device/v2/device/ids/")))
            deleted = [device_id for device_id in ids if self.devices.pop(device_id, None) is not None]
            failed = [device_id for device_id in ids if device_id not in deleted]
            return 200, None, {"deletedCount": len(deleted), "deletedIds": deleted, "failedIds": failed, "message": ""}
        if method == "DELETE":
            if "_id" in query:
                target = query["_id"][0]
            else:
                target = next((d["_id"] for d in self.devices.values() if d["serial"] == query.get("serial", [""])[0]), None)
            if self.devices.pop(target, None) is None:
                return 400, "Device not found", None
            return 200, None, True
        return 405, "method not allowed", None

    def _create_device(self, network: str, device: dict[str, Any]) -> tuple[int, str | None, Any]:
        errors = []
        if len(device.get("serial", "")) != SERIAL_LENGTH:
            errors.append("serial must be 12 characters")
        if not device.get("setupId") and not device.get("url"):
            errors.append("setupId or url is required")
        if errors:
            return 400, "; ".join(errors), None
        if device.get("NetworkName") != network:
            return 403, "NetworkName does not match the token network", None
        if any(d["serial"] == device["serial"] for d in self.devices.values()):
            return 400, "Player already exists", None
        if device.get("setupId") and device["setupId"] not in self.setups:
            return 400, "Setup not found", None
        device_id = self.new_id()
        stored = {**device, "_id": device_id, "NetworkName": network, "createdAt": "2026-10-26T00:00:00Z"}
        if device.get("setupId"):
            stored["setupName"] = self.setups[device["setupId"]]["bDeploy"]["packageName"]
        self.devices[device_id] = stored
        return 201, None, device_id
