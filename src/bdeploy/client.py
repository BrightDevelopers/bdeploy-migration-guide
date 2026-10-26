"""Client for B-Deploy v2/v3 and the BSN.cloud calls it depends on.

Behaviour follows docs/BDEPLOY-API-GUIDE.md, swagger/bdeploy-v2-v3.yaml and
swagger/bsn-main-provisioning.yaml. Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable, Iterator, Mapping

# Cloudflare in front of auth.bsn.cloud and api.bsn.cloud blocks Python-urllib's default agent.
USER_AGENT = "bdeploy-test-client/1.0"
JSON_MEDIA_TYPE = "application/json"
FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"

DEFAULT_AUTH_URL = "https://auth.bsn.cloud"
DEFAULT_API_URL = "https://api.bsn.cloud"
DEFAULT_PROVISION_URL = "https://provision.bsn.cloud"

TOKEN_PATH = "/realms/bsncloud/protocol/openid-connect/token"
SESSION_NETWORK_PATH = "/2022/06/REST/Self/Session/Network"
NETWORKS_PATH = "/2022/06/REST/Self/Networks"
REGISTRATION_TOKENS_PATH = "/2022/06/REST/Provisioning/Setups/Tokens"
SETUP_PATH = "/rest-setup/v3/setup"
DEVICE_PATH = "/rest-device/v2/device"

SETUP_SCHEMA_MAJOR_PREFIX = "3."
MINIMUM_PAGE_SIZE = 1
MAXIMUM_PAGE_SIZE = 100
MAXIMUM_PAGES = 1000
SERIAL_LENGTH = 12
USERDATA_MAXIMUM_LENGTH = 1024
REGISTRATION_TOKEN_FIELDS = ("token", "scope", "validFrom", "validTo")

TOKEN_REFRESH_MARGIN_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 30
MAXIMUM_RATE_LIMIT_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 5
ERROR_BODY_PREVIEW_LENGTH = 300

HTTP_OK_MINIMUM = 200
HTTP_OK_MAXIMUM = 299
HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429

# POST and PUT are not idempotent, so only these methods are retried after a 429.
RATE_LIMIT_RETRYABLE_METHODS = frozenset({"GET", "DELETE"})

# Plain HTTP is accepted only for these hosts, so tests can use a local fake server.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigurationError(Exception):
    """The client configuration is missing or invalid."""


class ProtocolError(Exception):
    """A response did not have the shape the specs promise."""


class ApiError(Exception):
    """A call failed: non-2xx status, or a non-null `error` in the envelope."""

    def __init__(
        self,
        method: str,
        url: str,
        status: int,
        error: str | None,
        body: str,
        server: str | None,
    ) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.error = error
        self.body = body
        self.server = server
        message = f"{method} {url}: HTTP {status}"
        if error:
            message += f": {error}"
        elif not body:
            message += " (empty body)"
        super().__init__(message)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    network: str
    username: str | None = None
    auth_url: str = DEFAULT_AUTH_URL
    api_url: str = DEFAULT_API_URL
    provision_url: str = DEFAULT_PROVISION_URL

    def __post_init__(self) -> None:
        for name in ("client_id", "client_secret", "network"):
            if not getattr(self, name):
                raise ConfigurationError(f"{name} must not be empty")
        for name in ("auth_url", "api_url", "provision_url"):
            _require_base_url(name, getattr(self, name))

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Config:
        """Read BS_CLIENT_ID, BS_SECRET, BS_NETWORK and BS_USERNAME; hosts are the production defaults."""
        environment = os.environ if environment is None else environment
        required = ("BS_CLIENT_ID", "BS_SECRET", "BS_NETWORK")
        missing = [name for name in required if not environment.get(name)]
        if missing:
            raise ConfigurationError("missing environment variables: " + ", ".join(missing))
        return cls(
            client_id=environment["BS_CLIENT_ID"],
            client_secret=environment["BS_SECRET"],
            network=environment["BS_NETWORK"],
            username=environment.get("BS_USERNAME") or None,
        )


def _require_base_url(name: str, url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise ConfigurationError(f"{name} is not an absolute URL: {url!r}")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
        raise ConfigurationError(f"{name} must use https: {url!r}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must be a bare scheme and host: {url!r}")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def is_success(self) -> bool:
        return HTTP_OK_MINIMUM <= self.status <= HTTP_OK_MAXIMUM


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as responses: the guide says every non-2xx, 3xx included, is a failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _error_text(body: bytes) -> str | None:
    """Pull a message out of an envelope, OAuth error, ProblemDetails or plain-text body."""
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:ERROR_BODY_PREVIEW_LENGTH]
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("error_description", "detail", "error", "title"):
            if parsed.get(key):
                return str(parsed[key])
    return text[:ERROR_BODY_PREVIEW_LENGTH]


def _decode_json(response: HttpResponse, method: str, url: str) -> Any:
    try:
        return json.loads(response.body)
    except json.JSONDecodeError as error:
        raise ProtocolError(
            f"{method} {url}: expected JSON, got {response.text[:ERROR_BODY_PREVIEW_LENGTH]!r}"
        ) from error


def _require_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{context}: expected a non-empty id string, got {value!r}")
    return value


def _require_page_size(page_size: int) -> None:
    if not MINIMUM_PAGE_SIZE <= page_size <= MAXIMUM_PAGE_SIZE:
        raise ValueError(f"page_size must be {MINIMUM_PAGE_SIZE}..{MAXIMUM_PAGE_SIZE}, got {page_size}")


def normalize_serial(serial: str) -> str:
    """Serials are stored upper-case and matched case-sensitively."""
    normalized = serial.strip().upper()
    if len(normalized) != SERIAL_LENGTH:
        raise ValueError(f"serial must be {SERIAL_LENGTH} characters, got {serial!r}")
    return normalized


def _bracketed_filters(prefix: str, filters: Mapping[str, str] | None) -> dict[str, str]:
    # Flat parameters other than NetworkName/serial/username/paging are silently ignored.
    return {f"{prefix}[{key}]": value for key, value in (filters or {}).items()}


def paginate(fetch_page: Callable[[int], list[Any]], page_size: int) -> Iterator[Any]:
    """Read pages until one is shorter than page_size; `total` is not a usable count."""
    _require_page_size(page_size)
    for page_number in range(1, MAXIMUM_PAGES + 1):
        page = fetch_page(page_number)
        if len(page) > page_size:
            raise ProtocolError(f"page {page_number} has {len(page)} items, more than pageSize {page_size}")
        yield from page
        if len(page) < page_size:
            return
    raise ProtocolError(f"more than {MAXIMUM_PAGES} pages; the server may be ignoring pageNum")


def parse_setup_json(record: Mapping[str, Any]) -> dict[str, Any]:
    """A setup list record carries the setup as a JSON string in `setupJson`."""
    raw = record.get("setupJson")
    if not isinstance(raw, str):
        raise ProtocolError(f"setup record {record.get('_id')!r} has no setupJson string")
    setup = json.loads(raw)
    if not isinstance(setup, dict):
        raise ProtocolError(f"setup record {record.get('_id')!r} setupJson is not an object")
    return setup


class BDeployClient:
    def __init__(
        self,
        config: Config,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_RefuseRedirects)
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self.granted_scopes: frozenset[str] = frozenset()
        self.last_provision_server: str | None = None

    @property
    def config(self) -> Config:
        return self._config

    @property
    def is_csharp_service(self) -> bool:
        """The C# port answers with `server: Kestrel`; otherwise assume Node."""
        return (self.last_provision_server or "").lower().startswith("kestrel")

    # ---- transport ----

    def _send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return HttpResponse(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            with error:
                return HttpResponse(error.code, error.headers, error.read())

    def _retry_after_seconds(self, response: HttpResponse) -> float:
        value = response.headers.get("Retry-After")
        if value is not None and value.strip().isdigit():
            return float(value.strip())
        return float(DEFAULT_RETRY_AFTER_SECONDS)

    def _call(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Any = None,
        allow_reauthentication: bool = True,
    ) -> HttpResponse:
        self._ensure_authenticated()
        url = base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": JSON_MEDIA_TYPE,
            "User-Agent": USER_AGENT,
        }
        body = None
        if payload is not None:
            # Node's setup service rejects any parameter such as "; charset=utf-8" with 415.
            headers["Content-Type"] = JSON_MEDIA_TYPE
            body = json.dumps(payload).encode("utf-8")

        response = self._send(method, url, headers, body)
        retries = 0
        while (
            response.status == HTTP_TOO_MANY_REQUESTS
            and method in RATE_LIMIT_RETRYABLE_METHODS
            and retries < MAXIMUM_RATE_LIMIT_RETRIES
        ):
            retries += 1
            self._sleep(self._retry_after_seconds(response))
            response = self._send(method, url, headers, body)

        if response.status == HTTP_UNAUTHORIZED and allow_reauthentication:
            # One retry with a new token and re-selected network; a second 401 is real.
            self.authenticate()
            return self._call(method, base_url, path, query=query, payload=payload, allow_reauthentication=False)

        if base_url == self._config.provision_url:
            self.last_provision_server = response.headers.get("Server")
        if not response.is_success:
            raise ApiError(
                method, url, response.status, _error_text(response.body), response.text, response.headers.get("Server")
            )
        return response

    def _provision(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Any = None,
    ) -> Any:
        """Call B-Deploy and unwrap the {"error", "result"} envelope."""
        response = self._call(method, self._config.provision_url, path, query=query, payload=payload)
        url = self._config.provision_url + path
        envelope = _decode_json(response, method, url)
        if not isinstance(envelope, dict) or "error" not in envelope or "result" not in envelope:
            raise ProtocolError(f"{method} {url}: response is not an error/result envelope: {envelope!r}")
        # The C# service returns some errors with 200, so a 2xx alone is not success.
        if envelope["error"] is not None:
            raise ApiError(
                method, url, response.status, str(envelope["error"]), response.text, response.headers.get("Server")
            )
        return envelope["result"]

    # ---- authentication and session ----

    def fetch_access_token(self) -> None:
        """OAuth2 client credentials. Tokens last about 5.5 minutes with no refresh token."""
        credentials = f"{self._config.client_id}:{self._config.client_secret}".encode("utf-8")
        headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "Accept": JSON_MEDIA_TYPE,
            "User-Agent": USER_AGENT,
            "Content-Type": FORM_MEDIA_TYPE,
        }
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
        url = self._config.auth_url + TOKEN_PATH
        response = self._send("POST", url, headers, body)
        if not response.is_success:
            raise ApiError("POST", url, response.status, _error_text(response.body), response.text, None)
        payload = _decode_json(response, "POST", url)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        lifetime = payload.get("expires_in") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ProtocolError(f"POST {url}: no access_token in response")
        if not isinstance(lifetime, int) or lifetime <= TOKEN_REFRESH_MARGIN_SECONDS:
            raise ProtocolError(f"POST {url}: expires_in {lifetime!r} is not usable")
        self._access_token = token
        self._token_expires_at = self._clock() + lifetime
        self.granted_scopes = frozenset(str(payload.get("scope") or "").split())

    def authenticate(self) -> None:
        """Get a new token and select the session network; every new token needs both."""
        self.fetch_access_token()
        self.set_session_network(self._config.network)

    def _ensure_authenticated(self) -> None:
        if self._access_token is None or self._clock() >= self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            self.authenticate()

    def set_session_network(self, network_name: str) -> None:
        """B-Deploy takes the network from the token's session, not from the request."""
        response = self._call(
            "PUT",
            self._config.api_url,
            SESSION_NETWORK_PATH,
            payload={"name": network_name},
            allow_reauthentication=False,
        )
        if response.status != HTTP_NO_CONTENT:
            raise ProtocolError(f"PUT {SESSION_NETWORK_PATH}: expected 204, got {response.status}")

    def get_session_network(self) -> dict[str, Any] | None:
        """The selected network, or None when nothing is selected (204)."""
        response = self._call("GET", self._config.api_url, SESSION_NETWORK_PATH)
        if response.status == HTTP_NO_CONTENT:
            return None
        if response.status != HTTP_OK:
            raise ProtocolError(f"GET {SESSION_NETWORK_PATH}: expected 200 or 204, got {response.status}")
        network = _decode_json(response, "GET", SESSION_NETWORK_PATH)
        if not isinstance(network, dict):
            raise ProtocolError(f"GET {SESSION_NETWORK_PATH}: expected an object, got {network!r}")
        return network

    def list_networks(self) -> list[dict[str, Any]]:
        response = self._call("GET", self._config.api_url, NETWORKS_PATH)
        networks = _decode_json(response, "GET", NETWORKS_PATH)
        if not isinstance(networks, list):
            raise ProtocolError(f"GET {NETWORKS_PATH}: expected an array, got {networks!r}")
        return networks

    # ---- registration tokens ----

    def create_registration_token(self) -> dict[str, Any]:
        """Returns {token, scope, validFrom, validTo}, ready for bsnDeviceRegistrationTokenEntity."""
        response = self._call("POST", self._config.api_url, REGISTRATION_TOKENS_PATH)
        entity = _decode_json(response, "POST", REGISTRATION_TOKENS_PATH)
        if not isinstance(entity, dict) or any(not entity.get(field) for field in REGISTRATION_TOKEN_FIELDS):
            raise ProtocolError(f"POST {REGISTRATION_TOKENS_PATH}: incomplete token {entity!r}")
        return {field: entity[field] for field in REGISTRATION_TOKEN_FIELDS}

    def validate_registration_token(self, token: str) -> dict[str, Any]:
        path = f"{REGISTRATION_TOKENS_PATH}/{urllib.parse.quote(token, safe='')}"
        response = self._call("GET", self._config.api_url, path)
        entity = _decode_json(response, "GET", path)
        if not isinstance(entity, dict):
            raise ProtocolError(f"GET {path}: expected an object, got {entity!r}")
        return entity

    def revoke_registration_token(self, token: str) -> None:
        path = f"{REGISTRATION_TOKENS_PATH}/{urllib.parse.quote(token, safe='')}"
        response = self._call("DELETE", self._config.api_url, path)
        if response.status != HTTP_NO_CONTENT:
            raise ProtocolError(f"DELETE {path}: expected 204, got {response.status}")

    # ---- setups (/rest-setup/v3) ----

    @staticmethod
    def _require_setup(setup: Mapping[str, Any]) -> None:
        version = setup.get("version")
        if not isinstance(version, str) or not version.startswith(SETUP_SCHEMA_MAJOR_PREFIX):
            raise ValueError(f"setup version must be 3.x on /rest-setup/v3, got {version!r}")
        bdeploy = setup.get("bDeploy")
        if not isinstance(bdeploy, dict):
            raise ValueError("setup must have a bDeploy object")
        # A missing bDeploy.username is an unhandled HTML 500 on Node.
        for field in ("username", "packageName"):
            if not bdeploy.get(field):
                raise ValueError(f"setup bDeploy.{field} is required")
        if not setup.get("setupType"):
            raise ValueError("setup setupType is required")

    def create_setup(self, setup: Mapping[str, Any]) -> str:
        self._require_setup(setup)
        return _require_id(self._provision("POST", SETUP_PATH, payload=dict(setup)), f"POST {SETUP_PATH}")

    def list_setups_page(
        self,
        network: str,
        page_number: int,
        page_size: int,
        filters: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        _require_page_size(page_size)
        query = {"NetworkName": network, "pageNum": page_number, "pageSize": page_size}
        query.update(_bracketed_filters("query", filters))
        records = self._provision("GET", SETUP_PATH, query=query)
        if not isinstance(records, list):
            raise ProtocolError(f"GET {SETUP_PATH}: expected an array, got {type(records).__name__}")
        return records

    def iterate_setups(
        self,
        network: str,
        filters: Mapping[str, str] | None = None,
        page_size: int = MAXIMUM_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        return paginate(lambda page_number: self.list_setups_page(network, page_number, page_size, filters), page_size)

    def find_setups_by_package_name(self, network: str, package_name: str) -> list[dict[str, Any]]:
        """C# filters with "contains" and old data has duplicate names, so compare exactly and return all."""
        matches = self.iterate_setups(network, {"packageName": package_name})
        return [record for record in matches if record.get("packageName") == package_name]

    def get_setup(self, setup_id: str) -> dict[str, Any]:
        """Get-by-id returns a one-element array. `?_id=` is ignored by C#, so use the path form."""
        path = f"{SETUP_PATH}/{urllib.parse.quote(setup_id, safe='')}"
        result = self._provision("GET", path)
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise ProtocolError(f"GET {path}: expected a one-element array, got {result!r}")
        return result[0]

    def update_setup(self, setup: Mapping[str, Any]) -> dict[str, Any]:
        """Send the complete setup: Node applies no defaults on PUT. Returns an update result, not the setup."""
        _require_id(setup.get("_id"), "update_setup _id")
        self._require_setup(setup)
        result = self._provision("PUT", SETUP_PATH, payload=dict(setup))
        if not isinstance(result, dict):
            raise ProtocolError(f"PUT {SETUP_PATH}: expected an update result object, got {result!r}")
        return result

    def delete_setup(self, setup_id: str) -> None:
        """Fails with a deterministic 500 while any device still references the setup."""
        result = self._provision("DELETE", SETUP_PATH, query={"_id": _require_id(setup_id, "delete_setup")})
        if result is not True:
            raise ProtocolError(f"DELETE {SETUP_PATH}: expected result true, got {result!r}")

    # ---- devices (/rest-device/v2) ----

    @staticmethod
    def _normalized_device(device: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(device)
        record["serial"] = normalize_serial(str(record.get("serial", "")))
        if not record.get("username"):
            raise ValueError("device username is required")
        userdata = record.get("userdata")
        if userdata is not None and len(userdata) > USERDATA_MAXIMUM_LENGTH:
            raise ValueError(f"device userdata exceeds {USERDATA_MAXIMUM_LENGTH} characters")
        return record

    def register_device(self, device: Mapping[str, Any]) -> str:
        """POST never updates: a serial registered on any network gives 400 "Player already exists"."""
        record = self._normalized_device(device)
        if not record.get("NetworkName"):
            raise ValueError("device NetworkName is required")
        if not record.get("setupId") and not record.get("url"):
            raise ValueError("device needs setupId or url")
        return _require_id(self._provision("POST", DEVICE_PATH, payload=record), f"POST {DEVICE_PATH}")

    def list_devices_page(
        self,
        network: str,
        page_number: int,
        page_size: int,
        serial: str | None = None,
        filters: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Always filtered by NetworkName: an unfiltered device GET returns an empty list."""
        _require_page_size(page_size)
        query: dict[str, Any] = {"NetworkName": network, "pageNum": page_number, "pageSize": page_size}
        if serial is not None:
            query["serial"] = normalize_serial(serial)
        query.update(_bracketed_filters("query", filters))
        result = self._provision("GET", DEVICE_PATH, query=query)
        players = result.get("players") if isinstance(result, dict) else None
        if not isinstance(players, list):
            raise ProtocolError(f"GET {DEVICE_PATH}: expected {{players: [...]}}, got {result!r}")
        return players

    def iterate_devices(
        self,
        network: str,
        filters: Mapping[str, str] | None = None,
        page_size: int = MAXIMUM_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        return paginate(
            lambda page_number: self.list_devices_page(network, page_number, page_size, filters=filters), page_size
        )

    def find_device_by_serial(self, network: str, serial: str) -> dict[str, Any] | None:
        """The result never includes setupId; read setupName instead."""
        normalized = normalize_serial(serial)
        players = self.list_devices_page(network, 1, MAXIMUM_PAGE_SIZE, serial=normalized)
        matches = [player for player in players if player.get("serial") == normalized]
        if len(matches) > 1:
            raise ProtocolError(f"serial {normalized} matched {len(matches)} devices")
        return matches[0] if matches else None

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        result = self._provision("GET", DEVICE_PATH, query={"_id": _require_id(device_id, "get_device")})
        if result is not None and not isinstance(result, dict):
            raise ProtocolError(f"GET {DEVICE_PATH}?_id: expected an object or null, got {result!r}")
        return result

    def update_device(self, device: Mapping[str, Any]) -> Any:
        """Send the complete record with _id, serial and setupId: Node replaces the stored record."""
        record = self._normalized_device(device)
        _require_id(record.get("_id"), "update_device _id")
        return self._provision("PUT", DEVICE_PATH, payload=record)

    def delete_device(self, *, device_id: str | None = None, serial: str | None = None) -> None:
        if (device_id is None) == (serial is None):
            raise ValueError("pass exactly one of device_id or serial")
        query = {"_id": device_id} if device_id is not None else {"serial": normalize_serial(serial or "")}
        result = self._provision("DELETE", DEVICE_PATH, query=query)
        if result is not True:
            raise ProtocolError(f"DELETE {DEVICE_PATH}: expected result true, got {result!r}")

    def delete_devices(self, device_ids: list[str]) -> dict[str, Any]:
        """Always 200 once the array is valid; read failedIds."""
        if not device_ids:
            raise ValueError("device_ids must not be empty")
        for device_id in device_ids:
            _require_id(device_id, "delete_devices")
        path = f"{DEVICE_PATH}/ids/{urllib.parse.quote(json.dumps(device_ids), safe='')}"
        result = self._provision("DELETE", path)
        if not isinstance(result, dict) or "failedIds" not in result:
            raise ProtocolError(f"DELETE {DEVICE_PATH}/ids: unexpected result {result!r}")
        return result
