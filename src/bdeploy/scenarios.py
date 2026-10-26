"""Live test scenarios: the guide's read-only live check and an opt-in create/update/delete lifecycle."""

from __future__ import annotations

import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

from .client import BDeployClient, normalize_serial, parse_setup_json

CHECK_PAGE_SIZE = 5
TEST_SERIAL_PREFIX = "ZZ"
TEST_SERIAL_RANDOM_BYTES = 5
TEST_PACKAGE_PREFIX = "bdeploy-test-client"
DEFAULT_TEST_URL = "https://example.com/bdeploy-test-client/autorun.zip"
SETUP_SCHEMA_VERSION = "3.0.0"

# The C# service demands a registration token for any setup whose bDeploy.username matches this.
REGISTRATION_TOKEN_USERNAME_PATTERN = re.compile(r"brightsign|bsn|bacon", re.IGNORECASE)


class CheckFailed(Exception):
    """A response was valid but did not match what the scenario expected."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


@dataclass
class StepResult:
    name: str
    passed: bool
    detail: str


@dataclass
class Reporter:
    output: TextIO = field(default_factory=lambda: sys.stdout)
    results: list[StepResult] = field(default_factory=list)

    def run(self, name: str, action: Callable[[], str | None]) -> None:
        """Run one step, print PASS/FAIL, and re-raise failures so the scenario stops."""
        try:
            detail = action() or ""
        except Exception as error:
            self._record(StepResult(name, False, f"{type(error).__name__}: {error}"))
            raise
        self._record(StepResult(name, True, detail))

    def note(self, message: str) -> None:
        print(f"      {message}", file=self.output, flush=True)

    def _record(self, result: StepResult) -> None:
        self.results.append(result)
        status = "PASS" if result.passed else "FAIL"
        line = f"{status}  {result.name}"
        if result.detail:
            line += f" -- {result.detail}"
        print(line, file=self.output, flush=True)

    @property
    def all_passed(self) -> bool:
        return all(result.passed for result in self.results)


def generate_test_serial() -> str:
    # serial is unique across all networks, so use a prefix no real player is expected to have.
    return normalize_serial(TEST_SERIAL_PREFIX + secrets.token_hex(TEST_SERIAL_RANDOM_BYTES))


def run_live_check(client: BDeployClient, reporter: Reporter) -> None:
    """Section 4 of the guide: token, select network, read it back, list setups and devices. Changes nothing."""
    network = client.config.network

    def fetch_token() -> str:
        client.fetch_access_token()
        return "scopes: " + (" ".join(sorted(client.granted_scopes)) or "(none listed)")

    def select_network() -> str:
        client.set_session_network(network)
        return f"{network!r} selected (204)"

    def read_network_back() -> str:
        selected = client.get_session_network()
        check(selected is not None, "no network selected (204); every B-Deploy call would fail")
        check(selected.get("name") == network, f"session network is {selected.get('name')!r}, expected {network!r}")
        return f"id={selected.get('id')} name={selected.get('name')!r}"

    def list_setups() -> str:
        records = client.list_setups_page(network, 1, CHECK_PAGE_SIZE)
        implementation = "C#" if client.is_csharp_service else "Node"
        return f"{len(records)} setup(s) on first page; server={client.last_provision_server!r} ({implementation})"

    def list_devices() -> str:
        players = client.list_devices_page(network, 1, CHECK_PAGE_SIZE)
        return f"{len(players)} device(s) on first page"

    reporter.run("get access token", fetch_token)
    reporter.run("select session network", select_network)
    reporter.run("read session network back", read_network_back)
    reporter.run("list setups", list_setups)
    reporter.run("list devices", list_devices)


@dataclass
class _Cleanup:
    name: str
    action: Callable[[], None]


def run_lifecycle(
    client: BDeployClient,
    reporter: Reporter,
    serial: str,
    partner_url: str = DEFAULT_TEST_URL,
) -> None:
    """Create a throwaway setup and device, read/update/verify them, then delete them.

    Only records this run created are ever deleted. If a step fails, the remaining
    cleanup runs in reverse order and the original failure is re-raised.
    """
    network = client.config.network
    username = client.config.username
    if not username:
        raise CheckFailed("BS_USERNAME (the BSN.cloud account e-mail) is required for the lifecycle test")
    serial = normalize_serial(serial)
    run_label = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
    package_name = f"{TEST_PACKAGE_PREFIX}-{run_label}"
    device_name = f"{TEST_PACKAGE_PREFIX} device {run_label}"
    renamed_device_name = device_name + " (renamed)"
    updated_description = f"updated by {TEST_PACKAGE_PREFIX} {run_label}"
    state: dict[str, Any] = {}
    cleanups: list[_Cleanup] = []
    reporter.note(f"packageName={package_name!r} serial={serial}")

    def ensure_serial_is_free() -> str:
        existing = client.find_device_by_serial(network, serial)
        check(existing is None, f"serial {serial} is already registered in {network!r}; refusing to touch it")
        return f"{serial} not registered in {network!r}"

    def create_registration_token() -> str:
        entity = client.create_registration_token()
        state["registration_token"] = entity
        cleanups.append(_Cleanup("revoke registration token", lambda: client.revoke_registration_token(entity["token"])))
        return f"scope={entity['scope']} validTo={entity['validTo']}"

    def create_setup() -> str:
        setup: dict[str, Any] = {
            "version": SETUP_SCHEMA_VERSION,
            "setupType": "partnerApplication",
            "bDeploy": {
                "username": username,
                "networkName": network,
                "packageName": package_name,
                "url": partner_url,
            },
            "bsnGroupName": "Default",
            # POST defaults turn these on; a test setup should not expose anything.
            "lwsEnabled": False,
            "remoteDwsEnabled": False,
        }
        if "registration_token" in state:
            setup["bsnDeviceRegistrationTokenEntity"] = state["registration_token"]
        setup_id = client.create_setup(setup)
        state["setup_id"] = setup_id
        cleanups.append(_Cleanup("delete setup", lambda: client.delete_setup(setup_id)))
        return f"id={setup_id}"

    def find_setup_by_name() -> str:
        records = client.find_setups_by_package_name(network, package_name)
        check(len(records) == 1, f"expected 1 setup named {package_name!r}, found {len(records)}")
        record = records[0]
        check(record.get("_id") == state["setup_id"], f"list returned id {record.get('_id')!r}")
        parsed = parse_setup_json(record)
        check(parsed.get("bDeploy", {}).get("packageName") == package_name, "setupJson packageName mismatch")
        return "exact packageName match; setupJson parsed"

    def get_setup_by_id() -> str:
        setup = client.get_setup(state["setup_id"])
        check(setup.get("setupType") == "partnerApplication", f"setupType is {setup.get('setupType')!r}")
        check(setup.get("bDeploy", {}).get("url") == partner_url, "bDeploy.url mismatch")
        check(setup.get("lwsEnabled") is False, f"lwsEnabled is {setup.get('lwsEnabled')!r}, expected false")
        check(setup.get("remoteDwsEnabled") is False, f"remoteDwsEnabled is {setup.get('remoteDwsEnabled')!r}")
        return f"version={setup.get('version')} timeZone={setup.get('timeZone')!r}"

    def update_setup() -> str:
        setup = client.get_setup(state["setup_id"])
        setup["_id"] = state["setup_id"]
        setup["deviceDescription"] = updated_description
        result = client.update_setup(setup)
        return f"update result {result}"

    def verify_setup_update() -> str:
        setup = client.get_setup(state["setup_id"])
        check(setup.get("deviceDescription") == updated_description, "deviceDescription was not updated")
        check(setup.get("bDeploy", {}).get("packageName") == package_name, "packageName lost on PUT")
        check(setup.get("lwsEnabled") is False, "lwsEnabled changed on PUT")
        return "deviceDescription updated; other fields kept"

    def register_device() -> str:
        device_id = client.register_device(
            {
                "serial": serial,
                "username": username,
                "NetworkName": network,
                "name": device_name,
                "desc": "temporary record from the B-Deploy test client",
                "setupId": state["setup_id"],
            }
        )
        state["device_id"] = device_id
        cleanups.append(_Cleanup("delete device", lambda: client.delete_device(serial=serial)))
        return f"id={device_id}"

    def find_device() -> str:
        device = client.find_device_by_serial(network, serial)
        check(device is not None, f"serial {serial} not found after registering")
        check(device.get("name") == device_name, f"name is {device.get('name')!r}")
        check(device.get("setupName") == package_name, f"setupName is {device.get('setupName')!r}")
        check("setupId" not in device, "device GET returned setupId; the guide says it never does")
        state["device"] = device
        return f"setupName={device.get('setupName')!r}; setupId absent as documented"

    def get_device_by_id() -> str:
        device = client.get_device(state["device_id"])
        check(device is not None, f"GET ?_id={state['device_id']} returned null")
        check(device.get("serial") == serial, f"serial is {device.get('serial')!r}")
        return "found"

    def update_device() -> str:
        record = dict(state["device"])
        record["name"] = renamed_device_name
        # setupId is never returned, so it must be put back or the link is lost.
        record["setupId"] = state["setup_id"]
        result = client.update_device(record)
        return f"result={result!r}"

    def verify_device_update() -> str:
        device = client.find_device_by_serial(network, serial)
        check(device is not None, "device vanished after PUT")
        check(device.get("name") == renamed_device_name, f"name is {device.get('name')!r}")
        check(device.get("setupName") == package_name, f"setup link lost: setupName={device.get('setupName')!r}")
        return "renamed; still linked to the setup"

    def delete_device() -> str:
        client.delete_device(serial=serial)
        cleanups.pop()
        check(client.find_device_by_serial(network, serial) is None, "device still listed after delete")
        return "deleted and no longer listed"

    def delete_setup() -> str:
        client.delete_setup(state["setup_id"])
        cleanups.pop()
        remaining = client.find_setups_by_package_name(network, package_name)
        check(not remaining, f"{len(remaining)} setup(s) named {package_name!r} still listed")
        return "deleted and no longer listed"

    def revoke_registration_token() -> str:
        client.revoke_registration_token(state["registration_token"]["token"])
        cleanups.pop()
        return "revoked"

    try:
        reporter.run("serial is not registered", ensure_serial_is_free)
        if REGISTRATION_TOKEN_USERNAME_PATTERN.search(username):
            reporter.note("username matches brightsign|bsn|bacon: the C# service requires a registration token")
            reporter.run("create registration token", create_registration_token)
        reporter.run("create setup", create_setup)
        reporter.run("find setup by packageName", find_setup_by_name)
        reporter.run("get setup by id", get_setup_by_id)
        reporter.run("update setup (GET, modify, PUT complete)", update_setup)
        reporter.run("verify setup update", verify_setup_update)
        reporter.run("register device", register_device)
        reporter.run("find device by serial", find_device)
        reporter.run("get device by id", get_device_by_id)
        reporter.run("update device (complete record + setupId)", update_device)
        reporter.run("verify device update", verify_device_update)
        reporter.run("delete device", delete_device)
        reporter.run("delete setup", delete_setup)
        if "registration_token" in state:
            reporter.run("revoke registration token", revoke_registration_token)
    finally:
        _run_cleanups(reporter, cleanups)


def _run_cleanups(reporter: Reporter, cleanups: list[_Cleanup]) -> None:
    """Undo what a failed run created, newest first; keep going so nothing is left behind."""
    while cleanups:
        cleanup = cleanups.pop()

        def action(cleanup: _Cleanup = cleanup) -> str:
            cleanup.action()
            return "done"

        try:
            reporter.run(f"cleanup: {cleanup.name}", action)
        except Exception:
            reporter.note(f"cleanup '{cleanup.name}' failed; remove it by hand")
