from __future__ import annotations

import dataclasses
import io
import json
import unittest

from bdeploy.client import BDeployClient
from bdeploy.scenarios import CheckFailed, Reporter, generate_test_serial, run_lifecycle
from tests.fake_bsn import Fault
from tests.test_client import NETWORK, USERNAME, FakeServerTestCase

SERIAL = "ZZ0123456789"


class LifecycleTests(FakeServerTestCase):
    def run_lifecycle(self) -> tuple[Reporter, io.StringIO]:
        output = io.StringIO()
        reporter = Reporter(output)
        run_lifecycle(self.client, reporter, SERIAL)
        return reporter, output

    def test_lifecycle_passes_and_leaves_nothing_behind(self) -> None:
        reporter, _ = self.run_lifecycle()
        self.assertTrue(reporter.all_passed)
        self.assertEqual(self.fake.setups, {})
        self.assertEqual(self.fake.devices, {})
        self.assertFalse(any(result.name.startswith("cleanup") for result in reporter.results))

    def test_test_setup_turns_off_default_on_features(self) -> None:
        self.run_lifecycle()
        posted = json.loads(self.fake.requests_to("POST", "/rest-setup/v3/setup")[0].body)
        self.assertIs(posted["lwsEnabled"], False)
        self.assertIs(posted["remoteDwsEnabled"], False)
        self.assertNotIn("bsnDeviceRegistrationTokenEntity", posted)

    def test_device_update_restores_setup_id(self) -> None:
        self.run_lifecycle()
        put = json.loads(self.fake.requests_to("PUT", "/rest-device/v2/device")[0].body)
        self.assertIn("setupId", put)
        self.assertEqual(put["serial"], SERIAL)
        self.assertIn("createdAt", put)

    def test_brightsign_username_gets_and_revokes_a_registration_token(self) -> None:
        self.config = dataclasses.replace(self.config, username="someone@brightsign.biz")
        self.client = BDeployClient(self.config, clock=self.clock, sleep=self.sleeps.append)
        reporter, _ = self.run_lifecycle()
        self.assertTrue(reporter.all_passed)
        posted = json.loads(self.fake.requests_to("POST", "/rest-setup/v3/setup")[0].body)
        self.assertEqual(posted["bsnDeviceRegistrationTokenEntity"]["scope"], "cert")
        self.assertEqual(self.fake.registration_tokens, set())

    def test_failure_mid_run_cleans_up_device_then_setup(self) -> None:
        self.fake.add_fault(Fault("PUT", "/rest-device/v2/device", 500, b'{"error":"boom","result":null}'))
        output = io.StringIO()
        reporter = Reporter(output)
        with self.assertRaisesRegex(Exception, "boom"):
            run_lifecycle(self.client, reporter, SERIAL)
        cleanup_names = [result.name for result in reporter.results if result.name.startswith("cleanup")]
        self.assertEqual(cleanup_names, ["cleanup: delete device", "cleanup: delete setup"])
        self.assertFalse(reporter.all_passed)
        self.assertEqual(self.fake.setups, {})
        self.assertEqual(self.fake.devices, {})

    def test_existing_serial_is_never_touched(self) -> None:
        setup_id = self.create_partner_setup("someone else's setup")
        self.client.register_device(
            {"serial": SERIAL, "username": USERNAME, "NetworkName": NETWORK, "setupId": setup_id}
        )
        reporter = Reporter(io.StringIO())
        with self.assertRaisesRegex(CheckFailed, "already registered"):
            run_lifecycle(self.client, reporter, SERIAL)
        self.assertEqual(len(self.fake.devices), 1)
        self.assertEqual(len(self.fake.setups), 1)
        self.assertEqual(self.fake.requests_to("DELETE", "/rest-"), [])

    def test_username_is_required(self) -> None:
        self.config = dataclasses.replace(self.config, username=None)
        self.client = BDeployClient(self.config)
        with self.assertRaisesRegex(CheckFailed, "BS_USERNAME"):
            run_lifecycle(self.client, Reporter(io.StringIO()), SERIAL)
        self.assertEqual(self.fake.requests, [])

    def test_generated_serial_is_twelve_upper_case_characters(self) -> None:
        serial = generate_test_serial()
        self.assertEqual(len(serial), 12)
        self.assertTrue(serial.startswith("ZZ"))
        self.assertEqual(serial, serial.upper())


if __name__ == "__main__":
    unittest.main()
