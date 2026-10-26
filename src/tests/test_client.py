from __future__ import annotations

import io
import json
import unittest

from bdeploy.client import (
    USER_AGENT,
    ApiError,
    BDeployClient,
    Config,
    ConfigurationError,
    ProtocolError,
    normalize_serial,
    parse_setup_json,
)
from bdeploy.scenarios import Reporter, run_live_check
from tests.fake_bsn import TOKEN_LIFETIME_SECONDS, Fault, FakeBsnCloud

CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
NETWORK = "test network"
USERNAME = "user@example.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeBsnCloud(CLIENT_ID, CLIENT_SECRET, NETWORK).start()
        self.addCleanup(self.fake.stop)
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self.config = Config(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            network=NETWORK,
            username=USERNAME,
            auth_url=self.fake.base_url,
            api_url=self.fake.base_url,
            provision_url=self.fake.base_url,
        )
        self.client = BDeployClient(self.config, clock=self.clock, sleep=self.sleeps.append)

    def create_partner_setup(self, package_name: str) -> str:
        return self.client.create_setup(
            {
                "version": "3.0.0",
                "setupType": "partnerApplication",
                "bDeploy": {
                    "username": USERNAME,
                    "networkName": NETWORK,
                    "packageName": package_name,
                    "url": "https://cms.example.com/autorun.zip",
                },
            }
        )


class ConfigTests(unittest.TestCase):
    def test_missing_environment_variables_are_listed(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "BS_CLIENT_ID, BS_SECRET, BS_NETWORK"):
            Config.from_environment({})

    def test_environment_defaults_to_production_hosts(self) -> None:
        config = Config.from_environment({"BS_CLIENT_ID": "a", "BS_SECRET": "b", "BS_NETWORK": "n"})
        self.assertEqual(config.provision_url, "https://provision.bsn.cloud")
        self.assertEqual(config.api_url, "https://api.bsn.cloud")
        self.assertEqual(config.auth_url, "https://auth.bsn.cloud")
        self.assertIsNone(config.username)

    def test_plain_http_is_rejected_for_remote_hosts(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "https"):
            Config(client_id="a", client_secret="b", network="n", provision_url="http://provision.bsn.cloud")

    def test_base_url_must_not_have_a_path(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "bare scheme and host"):
            Config(client_id="a", client_secret="b", network="n", api_url="https://api.bsn.cloud/2022")


class HelperTests(unittest.TestCase):
    def test_serial_is_upper_cased(self) -> None:
        self.assertEqual(normalize_serial(" utd416000978 "), "UTD416000978")

    def test_serial_length_is_enforced(self) -> None:
        with self.assertRaises(ValueError):
            normalize_serial("UTD4160009")

    def test_parse_setup_json_requires_a_string(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_setup_json({"_id": "1", "setupJson": {"version": "3.0.0"}})


class RequestConventionTests(FakeServerTestCase):
    def test_every_request_sends_accept_and_user_agent(self) -> None:
        self.create_partner_setup("Lobby")
        self.client.list_setups_page(NETWORK, 1, 10)
        self.assertGreaterEqual(len(self.fake.requests), 4)
        for request in self.fake.requests:
            self.assertEqual(request.headers.get("accept"), "application/json", request.path)
            self.assertEqual(request.headers.get("user-agent"), USER_AGENT, request.path)

    def test_json_bodies_use_exact_content_type(self) -> None:
        self.create_partner_setup("Lobby")
        posts = self.fake.requests_to("POST", "/rest-setup/v3/setup")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].headers["content-type"], "application/json")

    def test_first_call_authenticates_and_selects_network(self) -> None:
        self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(len(self.fake.requests_to("POST", "/realms/")), 1)
        selects = self.fake.requests_to("PUT", "/2022/06/REST/Self/Session/Network")
        self.assertEqual(len(selects), 1)
        self.assertEqual(json.loads(selects[0].body), {"name": NETWORK})

    def test_wrong_credentials_raise_with_oauth_message(self) -> None:
        config = Config(
            client_id=CLIENT_ID,
            client_secret="wrong",
            network=NETWORK,
            auth_url=self.fake.base_url,
            api_url=self.fake.base_url,
            provision_url=self.fake.base_url,
        )
        with self.assertRaises(ApiError) as raised:
            BDeployClient(config).authenticate()
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("Invalid client credentials", str(raised.exception))

    def test_session_network_300_is_a_failure(self) -> None:
        self.fake.add_fault(Fault("PUT", "/2022/06/REST/Self/Session/Network", 300))
        with self.assertRaises(ApiError) as raised:
            self.client.authenticate()
        self.assertEqual(raised.exception.status, 300)

    def test_redirects_are_not_followed(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(
            Fault("GET", "/rest-device/v2/device", 302, headers={"Location": self.fake.base_url + "/elsewhere"})
        )
        with self.assertRaises(ApiError) as raised:
            self.client.list_devices_page(NETWORK, 1, 10)
        self.assertEqual(raised.exception.status, 302)
        self.assertEqual(self.fake.requests_to("GET", "/elsewhere"), [])

    def test_unknown_network_name_is_a_failure(self) -> None:
        config = Config(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            network="not my network",
            auth_url=self.fake.base_url,
            api_url=self.fake.base_url,
            provision_url=self.fake.base_url,
        )
        with self.assertRaises(ApiError) as raised:
            BDeployClient(config).authenticate()
        self.assertEqual(raised.exception.status, 400)


class EnvelopeTests(FakeServerTestCase):
    def test_error_on_a_200_is_raised(self) -> None:
        self.client.authenticate()
        body = json.dumps({"error": "Error: Invalid user", "result": None}).encode()
        self.fake.add_fault(Fault("GET", "/rest-device/v2/device", 200, body))
        with self.assertRaises(ApiError) as raised:
            self.client.get_device("1234")
        self.assertEqual(raised.exception.status, 200)
        self.assertEqual(raised.exception.error, "Error: Invalid user")

    def test_empty_body_403_is_reported(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 403, headers={"Server": "Kestrel"}))
        with self.assertRaises(ApiError) as raised:
            self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.server, "Kestrel")
        self.assertIn("empty body", str(raised.exception))

    def test_problem_details_message_is_extracted(self) -> None:
        self.client.authenticate()
        body = json.dumps({"title": "Unsupported Media Type", "status": 415}).encode()
        self.fake.add_fault(Fault("POST", "/rest-setup/v3/setup", 415, body))
        with self.assertRaisesRegex(ApiError, "Unsupported Media Type"):
            self.create_partner_setup("Lobby")

    def test_non_envelope_2xx_is_a_protocol_error(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 200, b'[{"_id": "1"}]'))
        with self.assertRaises(ProtocolError):
            self.client.list_setups_page(NETWORK, 1, 10)

    def test_html_500_is_reported(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("POST", "/rest-setup/v3/setup", 500, b"<html>TypeError</html>"))
        with self.assertRaises(ApiError) as raised:
            self.create_partner_setup("Lobby")
        self.assertEqual(raised.exception.status, 500)


class TokenTests(FakeServerTestCase):
    def test_token_is_refreshed_before_expiry_and_network_reselected(self) -> None:
        self.client.list_setups_page(NETWORK, 1, 10)
        self.clock.now += TOKEN_LIFETIME_SECONDS
        self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(len(self.fake.requests_to("POST", "/realms/")), 2)
        self.assertEqual(len(self.fake.requests_to("PUT", "/2022/06/REST/Self/Session/Network")), 2)

    def test_token_is_reused_while_valid(self) -> None:
        self.client.list_setups_page(NETWORK, 1, 10)
        self.clock.now += 60
        self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(len(self.fake.requests_to("POST", "/realms/")), 1)

    def test_single_401_reauthenticates_and_retries_once(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 401))
        self.assertEqual(self.client.list_setups_page(NETWORK, 1, 10), [])
        self.assertEqual(len(self.fake.requests_to("POST", "/realms/")), 2)
        self.assertEqual(len(self.fake.requests_to("PUT", "/2022/06/REST/Self/Session/Network")), 2)

    def test_second_401_is_a_failure(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 401, remaining=2))
        with self.assertRaises(ApiError) as raised:
            self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual(len(self.fake.requests_to("GET", "/rest-setup/v3/setup")), 2)

    def test_403_is_not_retried(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 403))
        with self.assertRaises(ApiError):
            self.client.list_setups_page(NETWORK, 1, 10)
        self.assertEqual(len(self.fake.requests_to("GET", "/rest-setup/v3/setup")), 1)


class RateLimitTests(FakeServerTestCase):
    def test_get_honours_retry_after(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 429, headers={"Retry-After": "7"}))
        self.assertEqual(self.client.list_setups_page(NETWORK, 1, 10), [])
        self.assertEqual(self.sleeps, [7.0])

    def test_post_is_not_retried(self) -> None:
        self.client.authenticate()
        self.fake.add_fault(Fault("POST", "/rest-setup/v3/setup", 429))
        with self.assertRaises(ApiError) as raised:
            self.create_partner_setup("Lobby")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.fake.setups, {})


class SetupTests(FakeServerTestCase):
    def test_create_get_update_delete(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        setup = self.client.get_setup(setup_id)
        self.assertEqual(setup["bDeploy"]["packageName"], "Lobby")
        setup["deviceDescription"] = "changed"
        result = self.client.update_setup(setup)
        self.assertTrue(result["acknowledged"])
        self.assertEqual(self.client.get_setup(setup_id)["deviceDescription"], "changed")
        self.client.delete_setup(setup_id)
        self.assertEqual(self.fake.setups, {})

    def test_get_setup_uses_path_form(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        self.client.get_setup(setup_id)
        gets = self.fake.requests_to("GET", f"/rest-setup/v3/setup/{setup_id}")
        self.assertEqual(len(gets), 1)
        self.assertNotIn("_id", gets[0].query)

    def test_find_by_package_name_compares_exactly(self) -> None:
        lobby_id = self.create_partner_setup("Lobby")
        self.create_partner_setup("Lobby annex")
        records = self.client.find_setups_by_package_name(NETWORK, "Lobby")
        self.assertEqual([record["_id"] for record in records], [lobby_id])
        list_request = self.fake.requests_to("GET", "/rest-setup/v3/setup")[-1]
        self.assertEqual(list_request.query["query[packageName]"], ["Lobby"])
        self.assertNotIn("packageName", list_request.query)
        self.assertEqual(parse_setup_json(records[0])["bDeploy"]["packageName"], "Lobby")

    def test_iterate_reads_every_page_and_always_sends_page_size(self) -> None:
        total = 120
        for index in range(total):
            self.create_partner_setup(f"setup {index:03d}")
        names = [record["packageName"] for record in self.client.iterate_setups(NETWORK, page_size=50)]
        self.assertEqual(len(names), total)
        self.assertEqual(len(set(names)), total)
        lists = self.fake.requests_to("GET", "/rest-setup/v3/setup")
        self.assertEqual([request.query["pageNum"] for request in lists], [["1"], ["2"], ["3"]])
        self.assertTrue(all(request.query["pageSize"] == ["50"] for request in lists))
        self.assertTrue(all(request.query["NetworkName"] == [NETWORK] for request in lists))

    def test_oversized_page_is_a_protocol_error(self) -> None:
        self.client.authenticate()
        body = json.dumps({"error": None, "result": [{"_id": str(i)} for i in range(3)]}).encode()
        self.fake.add_fault(Fault("GET", "/rest-setup/v3/setup", 200, body))
        with self.assertRaisesRegex(ProtocolError, "more than pageSize"):
            list(self.client.iterate_setups(NETWORK, page_size=2))

    def test_delete_of_setup_in_use_raises_500(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        self.client.register_device(
            {"serial": "UTD416000978", "username": USERNAME, "NetworkName": NETWORK, "setupId": setup_id}
        )
        with self.assertRaises(ApiError) as raised:
            self.client.delete_setup(setup_id)
        self.assertEqual(raised.exception.status, 500)
        self.assertIn("in use", raised.exception.error)
        self.assertEqual(len(self.fake.requests_to("DELETE", "/rest-setup/v3/setup")), 1)

    def test_setup_preconditions(self) -> None:
        with self.assertRaisesRegex(ValueError, "3.x"):
            self.client.create_setup({"version": "2.0.0", "setupType": "lfn", "bDeploy": {}})
        with self.assertRaisesRegex(ValueError, "username"):
            self.client.create_setup({"version": "3.0.0", "setupType": "lfn", "bDeploy": {"packageName": "x"}})
        with self.assertRaisesRegex(ProtocolError, "_id"):
            self.client.update_setup({"version": "3.0.0", "setupType": "lfn", "bDeploy": {}})
        self.assertEqual(self.fake.requests, [])


class DeviceTests(FakeServerTestCase):
    def test_register_upper_cases_serial_and_find_returns_setup_name(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        device_id = self.client.register_device(
            {"serial": "utd416000978", "username": USERNAME, "NetworkName": NETWORK, "setupId": setup_id}
        )
        posted = json.loads(self.fake.requests_to("POST", "/rest-device/v2/device")[0].body)
        self.assertEqual(posted["serial"], "UTD416000978")
        device = self.client.find_device_by_serial(NETWORK, "utd416000978")
        self.assertEqual(device["_id"], device_id)
        self.assertEqual(device["setupName"], "Lobby")
        self.assertNotIn("setupId", device)

    def test_find_unknown_serial_returns_none(self) -> None:
        self.assertIsNone(self.client.find_device_by_serial(NETWORK, "UTD416000978"))

    def test_device_lists_always_send_network_name(self) -> None:
        self.client.list_devices_page(NETWORK, 1, 10)
        request = self.fake.requests_to("GET", "/rest-device/v2/device")[0]
        self.assertEqual(request.query["NetworkName"], [NETWORK])
        self.assertEqual(request.query["pageSize"], ["10"])

    def test_duplicate_serial_is_rejected(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        device = {"serial": "UTD416000978", "username": USERNAME, "NetworkName": NETWORK, "setupId": setup_id}
        self.client.register_device(device)
        with self.assertRaisesRegex(ApiError, "Player already exists"):
            self.client.register_device(device)

    def test_update_delete_and_bulk_delete(self) -> None:
        setup_id = self.create_partner_setup("Lobby")
        first = self.client.register_device(
            {"serial": "UTD416000978", "username": USERNAME, "NetworkName": NETWORK, "setupId": setup_id}
        )
        second = self.client.register_device(
            {"serial": "XTC2000123AB", "username": USERNAME, "NetworkName": NETWORK, "url": "https://x.example/a.zip"}
        )
        record = self.client.find_device_by_serial(NETWORK, "UTD416000978")
        record["name"] = "renamed"
        record["setupId"] = setup_id
        self.client.update_device(record)
        self.assertEqual(self.client.get_device(first)["name"], "renamed")
        self.client.delete_device(serial="UTD416000978")
        result = self.client.delete_devices([second, "999999"])
        self.assertEqual(result["deletedIds"], [second])
        self.assertEqual(result["failedIds"], ["999999"])
        self.assertEqual(self.fake.devices, {})

    def test_device_preconditions(self) -> None:
        with self.assertRaisesRegex(ValueError, "setupId or url"):
            self.client.register_device({"serial": "UTD416000978", "username": USERNAME, "NetworkName": NETWORK})
        with self.assertRaisesRegex(ValueError, "12 characters"):
            self.client.register_device({"serial": "SHORT", "username": USERNAME, "NetworkName": NETWORK, "url": "u"})
        with self.assertRaisesRegex(ValueError, "userdata"):
            self.client.register_device(
                {"serial": "UTD416000978", "username": USERNAME, "NetworkName": NETWORK, "url": "u", "userdata": "x" * 1025}
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.client.delete_device()
        self.assertEqual(self.fake.requests, [])


class LiveCheckTests(FakeServerTestCase):
    def test_live_check_passes_and_identifies_csharp(self) -> None:
        output = io.StringIO()
        reporter = Reporter(output)
        run_live_check(self.client, reporter)
        self.assertTrue(reporter.all_passed)
        self.assertEqual(len(reporter.results), 5)
        self.assertIn("Kestrel", output.getvalue())
        self.assertTrue(self.client.is_csharp_service)

    def test_live_check_fails_when_network_not_selected(self) -> None:
        self.fake.add_fault(Fault("GET", "/2022/06/REST/Self/Session/Network", 204))
        reporter = Reporter(io.StringIO())
        with self.assertRaisesRegex(Exception, "no network selected"):
            run_live_check(self.client, reporter)
        self.assertFalse(reporter.all_passed)


if __name__ == "__main__":
    unittest.main()
